"""Dashboard-facing, additive response contract for one routing request.

The real-router API predates the dashboard and exposes a stable set of flat
fields.  This module adds a self-contained product view without removing or
renaming any of those fields, so existing clients remain compatible.
"""

from __future__ import annotations

from typing import Any

from ..candidates import CandidateSet
from ..models import RealRoutingAttempt
from .executor import RoutingResult
from .policies import RealRoutingPlan


def build_service_fields(
    *,
    did: str,
    plan: RealRoutingPlan,
    candidate_set: CandidateSet,
    result: RoutingResult,
    acceptance_profile: str,
    evidence_mode: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Return the dashboard DTO fields added to the legacy API response."""
    selected = list(plan.attempt_order())
    winner = next((attempt for attempt in result.attempts if attempt.accepted), None)
    payload = result.winning_payload or {}
    calls_used = sum(1 for attempt in result.attempts if attempt.dispatched)
    calls_if_all_race = candidate_set.qualified_provider_count

    return {
        "did": did,
        "strategy": plan.policy,
        "success": result.success,
        "selection": {
            "candidate_count": candidate_set.qualified_provider_count,
            "selected_count": len(selected),
            "selected_providers": selected,
            "estimated_success": plan.metadata.get("estimated_subset_success"),
            "target_success": plan.metadata.get("target_slo_probability"),
            "selection_mode": _selection_mode(plan),
            "coverage_mode": plan.metadata.get("coverage_mode"),
        },
        "result": {
            "returned_by": result.record.returned_provider,
            "acceptance_profile": acceptance_profile,
            "accepted": result.success,
            "didResolutionMetadata": payload.get("didResolutionMetadata"),
            "didDocument": payload.get("didDocument"),
            "didDocumentMetadata": payload.get("didDocumentMetadata"),
        },
        "attempts": _attempt_rows(
            candidate_set=candidate_set,
            selected=selected,
            attempts=result.attempts,
        ),
        "cost": {
            "calls_used": calls_used,
            "calls_if_all_race": calls_if_all_race,
            "calls_saved_vs_all_race": max(0, calls_if_all_race - calls_used),
        },
        "evidence": {"mode": evidence_mode},
        "audit": audit,
        # A compact request-level latency is useful to the dashboard but is
        # deliberately separate from per-attempt latency.
        "completion_latency_ms": result.record.logical_completion_latency_ms,
        "acceptance_reason": winner.acceptance_reason if winner else None,
    }


def _selection_mode(plan: RealRoutingPlan) -> str:
    if plan.policy != "adaptive-min-set":
        return "policy_defined"
    if plan.metadata.get("best_effort"):
        return "best_effort"
    if plan.metadata.get("exact") is True:
        return "exact"
    # A successful adaptive plan with incomplete coverage can only come from
    # the optimizer's explicit partial-coverage mode.  Avoid claiming exact,
    # minimum, or optimal selection.
    return "best_known"


def _attempt_rows(
    *,
    candidate_set: CandidateSet,
    selected: list[str],
    attempts: list[RealRoutingAttempt],
) -> list[dict[str, Any]]:
    actual = {attempt.provider_id: attempt for attempt in attempts}
    skipped = {row.provider_id: row for row in candidate_set.skipped}
    ordered_ids = list(candidate_set.candidate_ids)
    ordered_ids.extend(
        row.provider_id
        for row in candidate_set.skipped
        if row.provider_id not in ordered_ids
    )

    rows: list[dict[str, Any]] = []
    for provider_id in ordered_ids:
        attempt = actual.get(provider_id)
        skipped_provider = skipped.get(provider_id)
        is_selected = provider_id in selected

        if attempt is not None:
            outcome, reason = _attempt_outcome(attempt)
            rows.append(
                {
                    "provider": provider_id,
                    "selected": is_selected,
                    "dispatched": attempt.dispatched,
                    "accepted": attempt.accepted,
                    "latency_ms": attempt.latency_ms,
                    "launch_offset_ms": attempt.launch_offset_ms,
                    "outcome": outcome,
                    "reason": reason,
                    "transport_outcome": attempt.transport_outcome,
                    "http_status": attempt.http_status,
                    "canceled": attempt.canceled,
                    "cancellation_outcome": attempt.cancellation_outcome,
                    "acceptance_reason": attempt.acceptance_reason,
                    "telemetry_error": attempt.telemetry_error,
                }
            )
            continue

        if skipped_provider is not None:
            outcome = "skipped"
            reason = skipped_provider.reason
        elif is_selected:
            outcome = "not_dispatched"
            reason = "not_dispatched_after_acceptable_result"
        else:
            outcome = "not_selected"
            reason = "not_selected"

        rows.append(
            {
                "provider": provider_id,
                "selected": is_selected,
                "dispatched": False,
                "accepted": None,
                "latency_ms": None,
                "launch_offset_ms": None,
                "outcome": outcome,
                "reason": reason,
                "transport_outcome": None,
                "http_status": None,
                "canceled": False,
                "cancellation_outcome": None,
                "acceptance_reason": None,
                "telemetry_error": None,
            }
        )

    return rows


def _attempt_outcome(attempt: RealRoutingAttempt) -> tuple[str, str | None]:
    if attempt.accepted:
        return "accepted", None
    if attempt.canceled:
        return "canceled", attempt.cancellation_outcome
    if attempt.telemetry_error:
        return "failed", "telemetry_error"
    if attempt.error:
        return "failed", attempt.error
    if attempt.transport_outcome != "http_response":
        return "failed", attempt.transport_outcome
    if attempt.http_status is not None and attempt.http_status >= 400:
        return "failed", attempt.resolution_error_family or f"http_{attempt.http_status}"
    return "unacceptable", attempt.acceptance_reason
