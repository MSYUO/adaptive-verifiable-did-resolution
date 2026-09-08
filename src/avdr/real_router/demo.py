"""Controlled dashboard scenarios over the existing AVDR serving stack.

This is a thin orchestration boundary.  It configures the repository's local
mock resolvers, but selection, execution, acceptance, and runtime feedback are
delegated to the existing production-facing components.

Every run is a fresh, deterministic ``controlled_demo`` session.  It never
reads, writes, resets, or merges the real deployment history namespace.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from ..adapters import ADAPTERS
from ..audit import (
    AuditReceipt,
    AuditRecorder,
    NullAuditRecorder,
    build_commitment,
    build_estimator_identity,
    build_policy_identity,
)
from ..budget import BudgetRegistry
from ..candidates import CandidateSet, select_candidates
from ..config import REPO_ROOT
from ..inventory import ProviderInventory
from ..profiles import PROFILE_W3C_BASIC_V1
from ..provenance import build_provenance, new_experiment_id
from ..resolver.settings import ResolverBehavior
from .adaptive_policy import AdaptivePlanningError, RealAdaptiveMinSet
from .executor import RealRoutingExecutor, RoutingResult
from .policies import RealSequentialFailover, RealSingleStatic
from .runtime_adaptive import CONTROLLED_DEMO, FrozenAdaptiveRuntime
from .service_contract import build_service_fields

DEMO_PHASE = "controlled-dashboard-demo"
DEMO_DID = "did:example:controlled-demo"
DEMO_LABEL = "CONTROLLED DEMO"
MAX_BOOTSTRAP_OBSERVATIONS = 20  # safety bound, never a readiness threshold


@dataclass(frozen=True)
class DemoScenario:
    id: str
    title: str
    description: str
    bootstrap_mode: str
    bootstrap_behaviors: dict[str, ResolverBehavior]
    final_behaviors: dict[str, ResolverBehavior]

    def public_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "evidence_mode": CONTROLLED_DEMO,
            "label": DEMO_LABEL,
            "reset_mode": "fresh_scenario_state",
        }

    def injection_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.id,
            "bootstrap_mode": self.bootstrap_mode,
            "bootstrap_behaviors": {
                key: value.model_dump(mode="json")
                for key, value in sorted(self.bootstrap_behaviors.items())
            },
            "final_behaviors": {
                key: value.model_dump(mode="json")
                for key, value in sorted(self.final_behaviors.items())
            },
        }


HEALTHY = ResolverBehavior()
ALTERNATING_A_FAILURE = ResolverBehavior(deterministic_failure_every_n=2)

DEMO_SCENARIOS: dict[str, DemoScenario] = {
    "normal": DemoScenario(
        id="normal",
        title="Normal",
        description=(
            "Healthy resolver conditions. AVDR can avoid unnecessary fan-out "
            "when a smaller subset satisfies the target."
        ),
        bootstrap_mode="single_static_local_a",
        bootstrap_behaviors={
            "local-a": HEALTHY,
            "local-b": HEALTHY,
            "local-c": HEALTHY,
        },
        final_behaviors={
            "local-a": HEALTHY,
            "local-b": HEALTHY,
            "local-c": HEALTHY,
        },
    ),
    "slow_failure": DemoScenario(
        id="slow_failure",
        title="Slow / Failure",
        description=(
            "One resolver is intentionally degraded. AVDR may select "
            "additional paths based on observed controlled history."
        ),
        bootstrap_mode="alternating_local_a_failure",
        bootstrap_behaviors={
            "local-a": ALTERNATING_A_FAILURE,
            "local-b": HEALTHY,
            "local-c": HEALTHY,
        },
        # The sixth local-a request fails deterministically after the five
        # observations that make the frozen optimizer ready.
        final_behaviors={
            "local-a": ALTERNATING_A_FAILURE,
            "local-b": HEALTHY,
            "local-c": HEALTHY,
        },
    ),
    "fast_unacceptable": DemoScenario(
        id="fast_unacceptable",
        title="Fast but unacceptable",
        description=(
            "The fastest resolver intentionally returns a structurally "
            "unacceptable result. AVDR waits for the first acceptable DID "
            "resolution result."
        ),
        bootstrap_mode="alternating_local_a_failure",
        bootstrap_behaviors={
            "local-a": ALTERNATING_A_FAILURE,
            "local-b": HEALTHY,
            "local-c": HEALTHY,
        },
        final_behaviors={
            "local-a": ResolverBehavior(force_invalid=True),
            "local-b": ResolverBehavior(artificial_delay_ms=80),
            "local-c": HEALTHY,
        },
    ),
}


class ControlledDemoError(RuntimeError):
    """A controlled scenario could not be configured or executed safely."""


class ControlledDemoOrchestrator:
    """Run fresh controlled scenarios without touching real runtime state."""

    def __init__(
        self,
        *,
        inventory: ProviderInventory,
        runtime: FrozenAdaptiveRuntime,
        audit_recorder: AuditRecorder | None = None,
        timeout_ms: int = 2000,
    ) -> None:
        expected = ["local-a", "local-b", "local-c"]
        actual = [provider.id for provider in inventory.providers]
        if actual != expected:
            raise ValueError(
                "controlled demo inventory must contain exactly "
                f"{expected!r} in that order, got {actual!r}"
            )
        if not inventory.inventory_version.lower().startswith("local-"):
            raise ValueError("controlled demo inventory must be labeled local")
        if any(provider.is_external for provider in inventory.providers):
            raise ValueError("controlled demo providers must be loopback-only")

        self.inventory = inventory
        self.runtime = runtime
        self.audit_recorder = audit_recorder or NullAuditRecorder()
        self.budgets = BudgetRegistry(inventory)
        self.executor = RealRoutingExecutor(
            acceptance_profile=PROFILE_W3C_BASIC_V1,
            timeout_ms=timeout_ms,
            budgets=self.budgets,
        )
        self.policy = RealAdaptiveMinSet(
            estimator=runtime.estimator,
            default_target=runtime.target_slo_probability,
        )
        self._single = RealSingleStatic("local-a")
        self._sequential = RealSequentialFailover()
        self._lock = asyncio.Lock()

    def inventory_payload(self) -> dict[str, Any]:
        return {
            "available": True,
            "label": DEMO_LABEL,
            "evidence_mode": CONTROLLED_DEMO,
            "reset_mode": "fresh_scenario_state",
            "scenarios": [
                DEMO_SCENARIOS[scenario_id].public_metadata()
                for scenario_id in ("normal", "slow_failure", "fast_unacceptable")
            ],
        }

    async def reset(self, client: httpx.AsyncClient) -> dict[str, Any]:
        async with self._lock:
            return await self._reset_unlocked(client)

    async def run(
        self,
        client: httpx.AsyncClient,
        scenario_id: str,
        did: str = DEMO_DID,
    ) -> dict[str, Any]:
        scenario = DEMO_SCENARIOS.get(scenario_id)
        if scenario is None:
            raise KeyError(scenario_id)
        if not did.startswith("did:example:"):
            raise ControlledDemoError(
                "controlled demo supports only synthetic did:example identifiers"
            )

        async with self._lock:
            await self._reset_unlocked(client)
            await self._apply_behaviors(client, scenario.bootstrap_behaviors)
            candidates = self._candidates()
            provenance = self._provenance(scenario)

            bootstrap_observations = 0
            readiness = self._readiness(candidates)
            while not readiness["adaptive_ready"]:
                if bootstrap_observations >= MAX_BOOTSTRAP_OBSERVATIONS:
                    raise ControlledDemoError(
                        "demo bootstrap reached its safety bound before the "
                        "existing optimizer became ready"
                    )
                await self._bootstrap_once(
                    client=client,
                    scenario=scenario,
                    did=f"did:example:demo-bootstrap-{bootstrap_observations + 1}",
                    candidates=candidates,
                    provenance=provenance,
                )
                bootstrap_observations += 1
                readiness = self._readiness(candidates)

            await self._apply_behaviors(client, scenario.final_behaviors)
            snapshot = self.runtime.snapshot(CONTROLLED_DEMO)
            try:
                plan = self.policy.plan(
                    candidates.candidates,
                    did,
                    context=snapshot.estimator_context(),
                )
            except AdaptivePlanningError as exc:
                raise ControlledDemoError(
                    f"frozen adaptive policy abstained after demo bootstrap: {exc}"
                ) from exc

            plan.metadata.update(
                {
                    "runtime_history_version": snapshot.version,
                    "runtime_history_request_count": snapshot.observed_request_count,
                    "demo_bootstrap": True,
                    "demo_scenario_id": scenario.id,
                }
            )
            result = await self.executor.execute(
                client=client,
                did=did,
                plan=plan,
                candidate_set=candidates,
                provenance=provenance,
            )
            record = self.runtime.observe(
                request_id=result.record.request_id,
                plan=plan,
                candidate_set=candidates,
                attempts=result.attempts,
                decision_history_version=snapshot.version,
                evidence_mode=CONTROLLED_DEMO,
            )
            return await self._response(
                scenario=scenario,
                did=did,
                plan=plan,
                candidates=candidates,
                result=result,
                bootstrap_observations=bootstrap_observations,
                committed_history_version=record.committed_history_version,
            )

    async def _reset_unlocked(self, client: httpx.AsyncClient) -> dict[str, Any]:
        self.runtime.history_for(CONTROLLED_DEMO).reset()
        self.budgets.reset()
        self._single.reset()
        self._sequential.reset()
        for provider in self.inventory.providers:
            response = await client.post(
                f"{provider.endpoint.rstrip('/')}/admin/reset"
            )
            response.raise_for_status()
        return {
            "reset": True,
            "label": DEMO_LABEL,
            "evidence_mode": CONTROLLED_DEMO,
            "history": self.runtime.history_for(CONTROLLED_DEMO).describe(),
        }

    async def _apply_behaviors(
        self,
        client: httpx.AsyncClient,
        behaviors: dict[str, ResolverBehavior],
    ) -> None:
        for provider in self.inventory.providers:
            response = await client.post(
                f"{provider.endpoint.rstrip('/')}/admin/behavior",
                json=behaviors[provider.id].model_dump(mode="json"),
            )
            response.raise_for_status()

    def _candidates(self) -> CandidateSet:
        candidates = select_candidates(
            self.inventory,
            "example",
            budgets=self.budgets,
            known_adapters=set(ADAPTERS),
        )
        if candidates.candidate_ids != ["local-a", "local-b", "local-c"]:
            raise ControlledDemoError(
                "all three controlled providers must be eligible for the demo"
            )
        return candidates

    def _readiness(self, candidates: CandidateSet) -> dict[str, Any]:
        return self.runtime.assess_readiness(
            candidate_providers=candidates.candidate_ids,
            optimizer=self.policy.optimizer,
            evidence_mode=CONTROLLED_DEMO,
            target_slo_probability=self.policy.default_target,
            allow_best_effort=self.policy.allow_best_effort,
        )

    async def _bootstrap_once(
        self,
        *,
        client: httpx.AsyncClient,
        scenario: DemoScenario,
        did: str,
        candidates: CandidateSet,
        provenance,
    ) -> None:
        if scenario.bootstrap_mode == "single_static_local_a":
            plan = self._single.plan(candidates.candidates, did)
        elif scenario.bootstrap_mode == "alternating_local_a_failure":
            # The existing sequential baseline observes A and, only when A
            # fails, B.  The partial-feedback rule decides which exact subset
            # outcomes are knowable; the orchestrator never writes q_hat.
            plan = self._sequential.plan(candidates.candidates[:2], did)
        else:  # guarded by source-defined scenarios
            raise ControlledDemoError(
                f"unknown demo bootstrap mode {scenario.bootstrap_mode!r}"
            )

        result = await self.executor.execute(
            client=client,
            did=did,
            plan=plan,
            candidate_set=candidates,
            provenance=provenance,
        )
        snapshot = self.runtime.snapshot(CONTROLLED_DEMO)
        self.runtime.observe(
            request_id=result.record.request_id,
            plan=plan,
            candidate_set=candidates,
            attempts=result.attempts,
            decision_history_version=snapshot.version,
            evidence_mode=CONTROLLED_DEMO,
        )

    def _provenance(self, scenario: DemoScenario):
        provenance = build_provenance(
            experiment_id=new_experiment_id("controlled-demo"),
            scenario_id=scenario.id,
            phase=DEMO_PHASE,
            repo_root=REPO_ROOT,
            router_config_payload=self.inventory.payload(),
            injection_config_payload=scenario.injection_payload(),
        )
        provenance.provider_inventory_hash = self.inventory.inventory_hash()
        provenance.acceptance_profile = PROFILE_W3C_BASIC_V1
        return provenance

    async def _response(
        self,
        *,
        scenario: DemoScenario,
        did: str,
        plan,
        candidates: CandidateSet,
        result: RoutingResult,
        bootstrap_observations: int,
        committed_history_version: int,
    ) -> dict[str, Any]:
        body = result.winning_payload if isinstance(result.winning_payload, dict) else {}
        response = {
            "request_id": result.record.request_id,
            "requested_did": did,
            "did_method": "example",
            "routing_policy": plan.policy,
            "execution": plan.execution,
            "acceptance_profile": PROFILE_W3C_BASIC_V1,
            "candidate_providers": candidates.candidate_ids,
            "qualified_provider_count": candidates.qualified_provider_count,
            "skipped_providers": candidates.skipped_dicts(),
            "attempted_providers": result.record.attempted_providers,
            "returned_provider": result.record.returned_provider,
            "accepted": result.record.success,
            "logical_completion_latency_ms": result.record.logical_completion_latency_ms,
            "attempt_count": result.record.attempt_count,
            "canceled_count": result.record.canceled_count,
            "did_document": body.get("didDocument") if result.success else None,
            "did_document_metadata": (
                body.get("didDocumentMetadata") if result.success else None
            ),
            "resolution_metadata": (
                body.get("didResolutionMetadata") if result.success else None
            ),
            "scenario": scenario.public_metadata(),
            "demo_bootstrap": {
                "label": "demo bootstrap",
                "observations": bootstrap_observations,
                "readiness_rule": "existing_optimizer_result",
                "counted_as_real_traffic": False,
            },
            "runtime_history": {
                "storage": "process_memory",
                "durable": False,
                "evidence_mode": CONTROLLED_DEMO,
                "decision_history_version": bootstrap_observations,
                "committed_history_version": committed_history_version,
            },
            "adaptive_plan": {
                key: plan.metadata.get(key)
                for key in (
                    "status",
                    "target_slo_probability",
                    "candidate_providers",
                    "candidate_count",
                    "selected_subset",
                    "selected_subset_size",
                    "estimated_subset_success",
                    "selection_reason",
                    "exact",
                    "optimizer_version",
                    "estimator_id",
                    "estimator_version",
                    "estimator_config_hash",
                    "runtime_history_version",
                    "runtime_history_request_count",
                )
            },
        }
        service_fields = build_service_fields(
            did=did,
            plan=plan,
            candidate_set=candidates,
            result=result,
            acceptance_profile=PROFILE_W3C_BASIC_V1,
            evidence_mode=CONTROLLED_DEMO,
            audit=AuditReceipt(False, "pending").to_dict(),
        )
        try:
            nonce_source = getattr(self.audit_recorder, "new_nonce", None)
            commitment = build_commitment(
                request_id=result.record.request_id,
                did=did,
                strategy=plan.policy,
                selected_resolver_ids=service_fields["selection"][
                    "selected_providers"
                ],
                launch_order=list(plan.attempt_order()),
                candidate_count=service_fields["selection"]["candidate_count"],
                policy_identity=build_policy_identity(self.policy, plan),
                estimator_identity=build_estimator_identity(
                    self.policy,
                    plan,
                    self.runtime.describe(CONTROLLED_DEMO),
                ),
                estimator_config_hash=plan.metadata.get("estimator_config_hash"),
                acceptance_profile=PROFILE_W3C_BASIC_V1,
                returned_provider=service_fields["result"]["returned_by"],
                normalized_result=service_fields["result"],
                calls_used=service_fields["cost"]["calls_used"],
                evidence_mode=CONTROLLED_DEMO,
                timestamp=result.record.timestamp,
                selection_mode=service_fields["selection"]["selection_mode"],
                target_success=service_fields["selection"]["target_success"],
                estimated_success=service_fields["selection"]["estimated_success"],
                request_nonce=nonce_source() if nonce_source is not None else None,
            )
            audit_receipt = await self.audit_recorder.record(commitment)
            if (
                not audit_receipt.recorded
                and audit_receipt.status == "not_configured"
            ):
                audit_receipt = AuditReceipt(
                    False, "controlled_demo_not_recorded"
                )
        except Exception:  # audit failure never changes the scenario result
            audit_receipt = AuditReceipt(False, "recording_failed")
        service_fields["audit"] = audit_receipt.to_dict()
        response.update(service_fields)
        if not result.success:
            response["error"] = "noAcceptableResult"
        return response
