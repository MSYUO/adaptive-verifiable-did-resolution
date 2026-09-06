"""Executes a routing plan against real providers.

Handles both execution modes:

  sequential  provider i+1 is contacted only after provider i terminates, and
              never after a success.
  concurrent  all providers are launched, and the FIRST STRUCTURALLY
              ACCEPTABLE completion wins. A faster completion that fails
              acceptance does not win; the race continues past it.

Cancellation is reported honestly. Once a request has been dispatched the
provider may already have done the work, so cancelling the local task is
recorded as `canceled_after_dispatch_provider_side_unknown`, never as if it
had prevented the request.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from ..acceptance import parse_did_method
from ..budget import BudgetRegistry
from ..candidates import CandidateSet
from ..inventory import ProviderEntry
from ..models import RealRoutingAttempt, RealRoutingRequestRecord
from ..probe import (
    CANCELED_AFTER_DISPATCH,
    CANCELED_BEFORE_DISPATCH,
    COMPLETED,
    DispatchState,
    ProbeOutcome,
    probe_provider,
    utc_now_iso,
)
from ..provenance import Provenance
from .policies import CONCURRENT, RealRoutingPlan


class RoutingResult:
    def __init__(
        self,
        record: RealRoutingRequestRecord,
        attempts: list[RealRoutingAttempt],
        winning_payload: dict | None,
        raw_bodies: dict[str, object],
    ) -> None:
        self.record = record
        self.attempts = attempts
        self.winning_payload = winning_payload
        self.raw_bodies = raw_bodies

    @property
    def success(self) -> bool:
        return self.record.success


def _attempt_from_probe(
    *,
    outcome: ProbeOutcome,
    request_id: str,
    launch_position: int,
    acceptance_profile: str,
    canceled: bool = False,
    cancellation_outcome: str = COMPLETED,
) -> RealRoutingAttempt:
    normalized = outcome.normalized
    return RealRoutingAttempt(
        request_id=request_id,
        launch_position=launch_position,
        provider_id=outcome.provider_id,
        implementation_id=outcome.implementation_id,
        resolver_endpoint_id=outcome.resolver_endpoint_id,
        launch_offset_ms=outcome.launch_offset_ms,
        start_ts=outcome.start_ts,
        end_ts=outcome.end_ts,
        latency_ms=outcome.latency_ms,
        transport_outcome=outcome.transport_outcome,
        http_status=outcome.http_status,
        content_type=outcome.content_type,
        throttled=outcome.throttled,
        retry_after=outcome.retry_after,
        resolution_error_family=normalized.resolution_error_family,
        resolution_error_detail=(
            None
            if normalized.resolution_error is None
            else str(normalized.resolution_error)[:500]
        ),
        acceptance_profile=acceptance_profile,
        acceptance_checks=dict(normalized.acceptance.checks)
        if normalized.acceptance
        else {},
        acceptance_reason=normalized.acceptance.reason if normalized.acceptance else None,
        accepted=normalized.accepted,
        subject_id=normalized.subject_id,
        normalized_document_hash=normalized.normalized_document_hash,
        raw_response_hash=normalized.raw_response_hash,
        raw_response_bytes=normalized.raw_response_bytes,
        canceled=canceled,
        cancellation_outcome=cancellation_outcome,
        dispatched=outcome.dispatched,
    )


class RealRoutingExecutor:
    def __init__(
        self,
        acceptance_profile: str,
        timeout_ms: int = 15000,
        budgets: BudgetRegistry | None = None,
    ) -> None:
        self.acceptance_profile = acceptance_profile
        self.timeout_ms = timeout_ms
        self.budgets = budgets

    async def execute(
        self,
        client: httpx.AsyncClient,
        did: str,
        plan: RealRoutingPlan,
        candidate_set: CandidateSet,
        provenance: Provenance,
        request_id: str | None = None,
    ) -> RoutingResult:
        request_id = request_id or str(uuid.uuid4())
        by_id = {p.id: p for p in candidate_set.candidates}
        ordered = [by_id[pid] for pid in plan.attempt_order()]

        started = time.perf_counter()
        timestamp = utc_now_iso()

        if plan.execution == CONCURRENT:
            attempts, payload, raw_bodies = await self._run_concurrent(
                client, did, ordered, request_id, started
            )
        else:
            attempts, payload, raw_bodies = await self._run_sequential(
                client, did, ordered, request_id, started
            )

        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        winner = next((a for a in attempts if a.accepted), None)
        success = winner is not None

        record = RealRoutingRequestRecord(
            request_id=request_id,
            timestamp=timestamp,
            requested_did=did,
            did_method=parse_did_method(did),
            routing_policy=plan.policy,
            execution=plan.execution,
            acceptance_profile=self.acceptance_profile,
            candidate_providers=candidate_set.candidate_ids,
            qualified_provider_count=candidate_set.qualified_provider_count,
            skipped_providers=candidate_set.skipped_dicts(),
            attempted_providers=[a.provider_id for a in attempts],
            returned_provider=winner.provider_id if winner else None,
            success=success,
            failure_reason=None
            if success
            else "no provider returned a structurally acceptable result",
            logical_completion_latency_ms=latency_ms,
            attempt_count=len(attempts),
            canceled_count=sum(1 for a in attempts if a.canceled),
            experiment_id=provenance.experiment_id,
            phase=provenance.phase,
            git_commit=provenance.git_commit,
            git_dirty=provenance.git_dirty,
            config_hash=provenance.config_hash,
            provider_inventory_hash=provenance.provider_inventory_hash,
            fixture_manifest_hash=provenance.fixture_manifest_hash,
            policy_metadata=plan.metadata,
            attempt_timeout_ms=self.timeout_ms,
        )
        return RoutingResult(record, attempts, payload, raw_bodies)

    # ------------------------------------------------------------------
    async def _run_sequential(self, client, did, ordered, request_id, started):
        attempts: list[RealRoutingAttempt] = []
        payload = None
        raw_bodies: dict[str, object] = {}

        for position, provider in enumerate(ordered):
            if self.budgets is not None:
                self.budgets.charge(provider.id)
            offset_ms = (time.perf_counter() - started) * 1000.0
            outcome = await probe_provider(
                client, provider, did, self.timeout_ms, offset_ms
            )
            attempts.append(
                _attempt_from_probe(
                    outcome=outcome,
                    request_id=request_id,
                    launch_position=position,
                    acceptance_profile=self.acceptance_profile,
                )
            )
            raw_bodies[provider.id] = outcome.raw_body
            if outcome.accepted:
                payload = outcome.raw_body
                # No provider is contacted after a success.
                break
        return attempts, payload, raw_bodies

    # ------------------------------------------------------------------
    async def _run_concurrent(self, client, did, ordered, request_id, started):
        """Launch all providers; first STRUCTURALLY ACCEPTABLE completion wins."""
        dispatch_states = {p.id: DispatchState() for p in ordered}
        positions = {p.id: i for i, p in enumerate(ordered)}

        async def launch(provider: ProviderEntry):
            if self.budgets is not None:
                self.budgets.charge(provider.id)
            offset_ms = (time.perf_counter() - started) * 1000.0
            return await probe_provider(
                client,
                provider,
                did,
                self.timeout_ms,
                offset_ms,
                dispatch_state=dispatch_states[provider.id],
            )

        tasks = {
            asyncio.ensure_future(launch(provider)): provider for provider in ordered
        }

        attempts: list[RealRoutingAttempt] = []
        raw_bodies: dict[str, object] = {}
        payload = None
        pending = set(tasks)

        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            winner_found = False
            for task in done:
                provider = tasks[task]
                try:
                    outcome = task.result()
                except Exception as exc:  # noqa: BLE001 - recorded, not hidden
                    attempts.append(
                        _failed_attempt(
                            request_id, provider, positions[provider.id], exc,
                            self.acceptance_profile,
                        )
                    )
                    continue
                attempts.append(
                    _attempt_from_probe(
                        outcome=outcome,
                        request_id=request_id,
                        launch_position=positions[provider.id],
                        acceptance_profile=self.acceptance_profile,
                    )
                )
                raw_bodies[provider.id] = outcome.raw_body
                # A faster response that fails acceptance does NOT win; the
                # race simply continues without it.
                if outcome.accepted and payload is None:
                    payload = outcome.raw_body
                    winner_found = True

            if winner_found:
                break

        # Cancel whatever is still outstanding, and describe it truthfully.
        for task in pending:
            provider = tasks[task]
            task.cancel()
        for task in pending:
            provider = tasks[task]
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            dispatched = dispatch_states[provider.id].dispatched
            attempts.append(
                RealRoutingAttempt(
                    request_id=request_id,
                    launch_position=positions[provider.id],
                    provider_id=provider.id,
                    implementation_id=provider.implementation_id,
                    resolver_endpoint_id=provider.endpoint,
                    launch_offset_ms=None,
                    start_ts=None,
                    end_ts=utc_now_iso(),
                    latency_ms=None,
                    transport_outcome="canceled",
                    http_status=None,
                    content_type=None,
                    acceptance_profile=self.acceptance_profile,
                    acceptance_checks={},
                    acceptance_reason=None,
                    accepted=False,
                    canceled=True,
                    cancellation_outcome=(
                        CANCELED_AFTER_DISPATCH if dispatched
                        else CANCELED_BEFORE_DISPATCH
                    ),
                    dispatched=dispatched,
                )
            )

        attempts.sort(key=lambda a: a.launch_position)
        return attempts, payload, raw_bodies


def _failed_attempt(request_id, provider, position, exc, acceptance_profile):
    return RealRoutingAttempt(
        request_id=request_id,
        launch_position=position,
        provider_id=provider.id,
        implementation_id=provider.implementation_id,
        resolver_endpoint_id=provider.endpoint,
        launch_offset_ms=None,
        start_ts=None,
        end_ts=utc_now_iso(),
        latency_ms=None,
        transport_outcome="probe_error",
        http_status=None,
        content_type=None,
        acceptance_profile=acceptance_profile,
        acceptance_checks={},
        acceptance_reason=None,
        accepted=False,
        error=f"{type(exc).__name__}: {exc}",
        canceled=False,
        cancellation_outcome=COMPLETED,
        dispatched=True,
    )
