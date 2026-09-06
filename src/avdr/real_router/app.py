"""Real-provider routing service.

User-facing API over the qualified real adapters. The mock router
(`avdr.router.app`) is untouched and still serves the deterministic local
path; this service shares the same separations -- policy holds no I/O,
transport holds no policy, providers hold no routing knowledge.

Three BASELINE policies (single-static, sequential-failover, all-race) plus the
proposed `adaptive-min-set`, which is added alongside them and never replaces
them. Nothing here ranks providers, and the adaptive policy uses a
server-configured estimator only -- clients supply a target, never a
probability table.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..acceptance import parse_did_method
from ..adapters import ADAPTERS
from ..budget import BudgetRegistry
from ..candidates import (
    INSUFFICIENT_QUALIFIED_PROVIDERS,
    InsufficientQualifiedProviders,
    select_candidates,
)
from ..config import REPO_ROOT
from ..adaptive.estimator import EstimatorError, SubsetEstimator
from ..adaptive.optimizer import TIE_BREAK_RULE, CostModel, validate_target
from ..inventory import ProviderInventory, load_provider_inventory
from ..profiles import CHECK_ORDER, PROFILE_W3C_BASIC_V1
from ..provenance import build_provenance, new_experiment_id
from ..telemetry import TelemetrySink
from .adaptive_policy import AdaptivePlanningError, RealAdaptiveMinSet
from .executor import RealRoutingExecutor
from .policies import POLICY_TYPES, build_policies

PHASE = "real-routing-service"


class ResolveRequest(BaseModel):
    did: str = Field(min_length=1)
    policy: str | None = None
    # Only the TARGET is client-supplied. The estimator and its q_hat table
    # are server-side configuration: a client must never be able to submit
    # its own probability table to a production-facing API.
    target_slo_probability: float | None = None


def create_app(
    inventory: ProviderInventory | None = None,
    sink: TelemetrySink | None = None,
    telemetry_dir: str | Path | None = None,
    timeout_ms: int = 15000,
    single_static_target: str | None = None,
    launch_order_seed: int | None = 20260907,
    budgets: BudgetRegistry | None = None,
    experiment_id: str | None = None,
    estimator: SubsetEstimator | None = None,
    default_target_slo: float = 0.99,
    cost_model: CostModel | None = None,
    allow_best_effort: bool = False,
) -> FastAPI:
    inventory = inventory or load_provider_inventory()
    sink = sink or TelemetrySink(telemetry_dir or (REPO_ROOT / "telemetry" / "routing"))
    budgets = budgets if budgets is not None else BudgetRegistry(inventory)
    policies = build_policies(single_static_target, launch_order_seed)
    # The adaptive policy is ADDED alongside the baselines, never replacing
    # them. Without a server-configured estimator it is simply unavailable.
    if estimator is not None:
        policies[RealAdaptiveMinSet.name] = RealAdaptiveMinSet(
            estimator=estimator,
            default_target=default_target_slo,
            cost_model=cost_model,
            allow_best_effort=allow_best_effort,
        )
    executor = RealRoutingExecutor(
        acceptance_profile=PROFILE_W3C_BASIC_V1,
        timeout_ms=timeout_ms,
        budgets=budgets,
    )
    experiment_id = experiment_id or new_experiment_id("routing")

    provenance_base = build_provenance(
        experiment_id=experiment_id,
        scenario_id="real-routing",
        phase=PHASE,
        repo_root=REPO_ROOT,
        router_config_payload=inventory.payload(),
        injection_config_payload={"note": "no fault injection in the routing service"},
    )
    provenance_base.provider_inventory_hash = inventory.inventory_hash()
    provenance_base.acceptance_profile = PROFILE_W3C_BASIC_V1

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient() as client:
            app.state.client = client
            yield

    app = FastAPI(title="AVDR real DID routing service", lifespan=lifespan)
    app.state.inventory = inventory
    app.state.sink = sink
    app.state.budgets = budgets
    app.state.policies = policies
    app.state.executor = executor
    app.state.provenance = provenance_base

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "acceptance_profile": PROFILE_W3C_BASIC_V1,
            "policies": sorted(policies),
            "provider_inventory_version": inventory.inventory_version,
            "provider_inventory_hash": inventory.inventory_hash(),
            "experiment_id": experiment_id,
            "attempt_timeout_ms": timeout_ms,
        }

    @app.get("/providers")
    async def providers() -> dict:
        """Read-only provider status. Exposes no credential material.

        Deliberately omits auth_scheme, endpoints' query strings and any
        configuration value that could carry a secret; `auth_available` is a
        boolean, never the credential itself.
        """
        snapshot = budgets.snapshot()
        rows = []
        for provider in inventory.providers:
            state = snapshot.get(provider.id, {})
            rows.append(
                {
                    "provider_id": provider.id,
                    "implementation_id": provider.implementation_id,
                    "operator": provider.operator,
                    "endpoint": provider.endpoint,
                    "adapter": provider.adapter,
                    "supported_did_methods": provider.supported_did_methods,
                    "configured": True,
                    "available": provider.available,
                    "auth_required": provider.auth_required,
                    "auth_available": bool(provider.credentials_available),
                    "full_resolution_result": provider.full_resolution_result,
                    "rate_budget": {
                        "limit": state.get("limit"),
                        "window_seconds": state.get("window_seconds"),
                        "spent_in_window": state.get("spent_in_window"),
                        "remaining": state.get("remaining"),
                        "exhausted": state.get("exhausted"),
                    },
                    "exclusion_reason": provider.unavailable_reason,
                    "independence_notes": provider.independence_notes,
                }
            )
        return {
            "provider_inventory_version": inventory.inventory_version,
            "provider_inventory_hash": inventory.inventory_hash(),
            "providers": rows,
        }

    @app.get("/policies")
    async def list_policies() -> dict:
        return {
            "available": sorted(policies),
            "acceptance_profile": PROFILE_W3C_BASIC_V1,
            "acceptance_checks": list(CHECK_ORDER),
            "minimum_providers": {
                name: policy.min_providers for name, policy in policies.items()
            },
            "adaptive": (
                {
                    **policies[RealAdaptiveMinSet.name].estimator.describe(),
                    "default_target_slo_probability": default_target_slo,
                    "optimizer_version": policies[
                        RealAdaptiveMinSet.name
                    ].optimizer.version,
                    "cost_model": policies[
                        RealAdaptiveMinSet.name
                    ].optimizer.cost_model.describe(),
                    "tie_break_rule": TIE_BREAK_RULE,
                    "max_adaptive_candidates": policies[
                        RealAdaptiveMinSet.name
                    ].optimizer.max_candidates,
                }
                if RealAdaptiveMinSet.name in policies
                else None
            ),
            "note": (
                "All three are baselines. None performs prediction, adaptive "
                "fan-out sizing or provider ranking."
            ),
        }

    @app.get("/telemetry/requests/{request_id}")
    async def get_request(request_id: str):
        record = sink.get_routing_request(request_id)
        if record is None:
            return JSONResponse(status_code=404, content={"error": "unknown request_id"})
        return record

    @app.get("/telemetry/recent")
    async def recent(limit: int = Query(default=20, ge=1, le=200)):
        return {"records": sink.recent_routing_requests(limit)}

    @app.post("/resolve")
    async def resolve(payload: ResolveRequest):
        did = payload.did
        policy_name = payload.policy or "sequential-failover"

        if policy_name not in policies:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unknownPolicy",
                    "detail": f"policy {policy_name!r} is not available",
                    "available": sorted(policies),
                },
            )

        did_method = parse_did_method(did)
        if did_method is None:
            return JSONResponse(
                status_code=400,
                content={"error": "invalidDid", "detail": f"malformed DID: {did!r}"},
            )

        candidate_set = select_candidates(
            inventory, did_method, budgets=budgets, known_adapters=set(ADAPTERS)
        )
        policy = policies[policy_name]

        # Capability gate: a policy that needs redundancy must not silently
        # pretend to have it.
        if candidate_set.qualified_provider_count < policy.min_providers:
            error = InsufficientQualifiedProviders(
                candidate_set, policy.min_providers, policy_name
            )
            return JSONResponse(
                status_code=409,
                content={
                    "error": INSUFFICIENT_QUALIFIED_PROVIDERS,
                    "detail": str(error),
                    "requested_did": did,
                    "did_method": did_method,
                    "routing_policy": policy_name,
                    "qualified_provider_count": candidate_set.qualified_provider_count,
                    "required_provider_count": policy.min_providers,
                    "candidate_providers": candidate_set.candidate_ids,
                    "skipped_providers": candidate_set.skipped_dicts(),
                },
            )

        try:
            if isinstance(policy, RealAdaptiveMinSet):
                try:
                    target = (
                        validate_target(payload.target_slo_probability)
                        if payload.target_slo_probability is not None
                        else None
                    )
                except (ValueError, EstimatorError) as exc:
                    return JSONResponse(
                        status_code=422,
                        content={
                            "error": "invalidTargetSloProbability",
                            "detail": str(exc),
                        },
                    )
                plan = policy.plan(
                    candidate_set.candidates, did, target_probability=target
                )
            else:
                plan = policy.plan(candidate_set.candidates, did)
        except AdaptivePlanningError as exc:
            # No executable subset. Never silently degrade into calling
            # everything and reporting the SLO as met.
            return JSONResponse(
                status_code=409,
                content={
                    "error": exc.status,
                    "detail": str(exc),
                    "requested_did": did,
                    "did_method": did_method,
                    "routing_policy": policy_name,
                    **exc.result.summary(),
                    "skipped_providers": candidate_set.skipped_dicts(),
                },
            )
        except EstimatorError as exc:
            return JSONResponse(
                status_code=500,
                content={"error": "invalidEstimatorOutput", "detail": str(exc)},
            )
        except KeyError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "policyTargetNotQualified",
                    "detail": str(exc),
                    "candidate_providers": candidate_set.candidate_ids,
                    "skipped_providers": candidate_set.skipped_dicts(),
                },
            )

        result = await executor.execute(
            client=app.state.client,
            did=did,
            plan=plan,
            candidate_set=candidate_set,
            provenance=provenance_base,
        )

        for attempt in result.attempts:
            sink.record_routing_attempt(attempt)
        sink.record_routing_request(result.record)

        warnings = _build_warnings(result, candidate_set, policy_name)
        winner = next((a for a in result.attempts if a.accepted), None)
        body = result.winning_payload if isinstance(result.winning_payload, dict) else {}

        response = {
            "request_id": result.record.request_id,
            "requested_did": did,
            "did_method": did_method,
            "routing_policy": policy_name,
            "execution": plan.execution,
            "acceptance_profile": PROFILE_W3C_BASIC_V1,
            "candidate_providers": candidate_set.candidate_ids,
            "qualified_provider_count": candidate_set.qualified_provider_count,
            "skipped_providers": candidate_set.skipped_dicts(),
            "attempted_providers": result.record.attempted_providers,
            "returned_provider": result.record.returned_provider,
            "accepted": result.record.success,
            "logical_completion_latency_ms": result.record.logical_completion_latency_ms,
            "attempt_count": result.record.attempt_count,
            "canceled_count": result.record.canceled_count,
            "did_document": body.get("didDocument") if result.success else None,
            "did_document_metadata": body.get("didDocumentMetadata")
            if result.success
            else None,
            "resolution_metadata": body.get("didResolutionMetadata")
            if result.success
            else None,
            "acceptance_checks": dict(winner.acceptance_checks) if winner else {},
            "warnings": warnings,
        }

        if plan.policy == RealAdaptiveMinSet.name:
            response["adaptive_plan"] = {
                key: plan.metadata.get(key)
                for key in (
                    "status", "target_slo_probability", "candidate_providers",
                    "candidate_count", "evaluated_subset_count",
                    "unestimated_subset_count", "selected_subset",
                    "selected_subset_size", "estimated_subset_success",
                    "selection_cost", "selection_reason", "best_subset",
                    "best_probability", "best_effort", "optimizer_version",
                    "cost_model_id", "tie_break_rule", "estimator_id",
                    "estimator_version", "estimator_config_hash",
                )
            }

        if not result.success:
            response["error"] = "noAcceptableResult"
            response["attempt_outcomes"] = [
                {
                    "provider_id": a.provider_id,
                    "transport_outcome": a.transport_outcome,
                    "http_status": a.http_status,
                    "resolution_error_family": a.resolution_error_family,
                    "acceptance_reason": a.acceptance_reason,
                    "accepted": a.accepted,
                    "canceled": a.canceled,
                }
                for a in result.attempts
            ]
            return JSONResponse(status_code=502, content=response)

        return response

    return app


def _build_warnings(result, candidate_set, policy_name: str) -> list[dict]:
    """Surface facts the caller should not have to dig for."""
    warnings: list[dict] = []

    if candidate_set.qualified_provider_count == 1:
        warnings.append(
            {
                "code": "SINGLE_QUALIFIED_PROVIDER",
                "detail": (
                    "only one provider qualified for this DID method; no "
                    "redundancy exists and none is implied"
                ),
            }
        )

    throttled = [a.provider_id for a in result.attempts if a.throttled]
    if throttled:
        warnings.append(
            {
                "code": "PROVIDER_THROTTLED",
                "detail": f"providers signalled throttling: {sorted(set(throttled))}",
            }
        )

    canceled_dispatched = [
        a.provider_id
        for a in result.attempts
        if a.canceled and a.dispatched
    ]
    if canceled_dispatched:
        warnings.append(
            {
                "code": "CANCELED_AFTER_DISPATCH",
                "detail": (
                    "requests to "
                    f"{sorted(set(canceled_dispatched))} were canceled after "
                    "dispatch; provider-side work may already have occurred"
                ),
            }
        )

    accepted = [a for a in result.attempts if a.accepted]
    hashes = {a.normalized_document_hash for a in accepted if a.normalized_document_hash}
    if len(hashes) > 1:
        warnings.append(
            {
                "code": "PROVIDER_RESULT_DIFFERENCE_OBSERVED",
                "detail": (
                    "accepted providers returned different normalized "
                    "documents; recorded as a representational difference "
                    "only -- no provider is judged incorrect without "
                    "method-specific ground truth"
                ),
                "normalized_document_hashes": {
                    a.provider_id: a.normalized_document_hash for a in accepted
                },
                "subject_ids": {a.provider_id: a.subject_id for a in accepted},
            }
        )

    return warnings


app = create_app()
