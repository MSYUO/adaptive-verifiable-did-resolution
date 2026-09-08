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

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..acceptance import parse_did_method
from ..adapters import ADAPTERS
from ..audit import (
    AuditReceipt,
    AuditRecorder,
    LocalAuditRecorder,
    NullAuditRecorder,
    build_estimator_identity,
    build_commitment,
    build_policy_identity,
    verify_receipt_payload,
)
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
from ..sepolia_anchor import build_audit_anchor_from_env
from ..telemetry import TelemetrySink
from .adaptive_policy import AdaptivePlanningError, RealAdaptiveMinSet
from .demo import ControlledDemoError, ControlledDemoOrchestrator
from .executor import RealRoutingExecutor
from .policies import POLICY_TYPES, build_policies
from .runtime_adaptive import (
    FrozenAdaptiveRuntime,
    try_load_frozen_adaptive_runtime,
)
from .service_contract import build_service_fields

PHASE = "real-routing-service"
WEB_ROOT = REPO_ROOT / "web"
EVIDENCE_MODES = frozenset({"real", "controlled_demo"})
STRATEGY_ALIASES = {
    "adaptive": RealAdaptiveMinSet.name,
    "adaptive-min-set": RealAdaptiveMinSet.name,
    "all-race": "all-race",
    "sequential": "sequential-failover",
    "sequential-failover": "sequential-failover",
    "single": "single-static",
    "single-static": "single-static",
}


class ResolveRequest(BaseModel):
    did: str = Field(min_length=1)
    policy: str | None = None
    # Product-facing alias. Existing clients may continue to send `policy`.
    strategy: str | None = None
    # Only the TARGET is client-supplied. The estimator and its q_hat table
    # are server-side configuration: a client must never be able to submit
    # its own probability table to a production-facing API.
    target_slo_probability: float | None = None


class DemoRunRequest(BaseModel):
    scenario_id: str
    did: str = Field(default="did:example:controlled-demo", min_length=1)


class AuditVerifyRequest(BaseModel):
    receipt_id: str | None = None
    receipt: dict[str, Any] | None = None
    receipt_hash: str | None = None
    disclosed_did: str | None = None
    disclosed_result: dict[str, Any] | None = None


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
    adaptive_runtime: FrozenAdaptiveRuntime | None = None,
    default_target_slo: float = 0.99,
    cost_model: CostModel | None = None,
    allow_best_effort: bool = False,
    evidence_mode: str | None = None,
    audit_recorder: AuditRecorder | None = None,
    demo_inventory: ProviderInventory | None = None,
) -> FastAPI:
    if estimator is not None and adaptive_runtime is not None:
        raise ValueError("configure estimator or adaptive_runtime, not both")
    inventory = inventory or load_provider_inventory()
    sink = sink or TelemetrySink(telemetry_dir or (REPO_ROOT / "telemetry" / "routing"))
    budgets = budgets if budgets is not None else BudgetRegistry(inventory)
    evidence_mode = _resolve_evidence_mode(inventory, evidence_mode)
    audit_recorder = audit_recorder or NullAuditRecorder()
    policies = build_policies(single_static_target, launch_order_seed)
    configured_estimator = (
        adaptive_runtime.estimator if adaptive_runtime is not None else estimator
    )
    adaptive_default_target = (
        adaptive_runtime.target_slo_probability
        if adaptive_runtime is not None
        else default_target_slo
    )
    # The adaptive policy is ADDED alongside the baselines, never replacing
    # them. Without a server-configured estimator it is simply unavailable.
    if configured_estimator is not None:
        policies[RealAdaptiveMinSet.name] = RealAdaptiveMinSet(
            estimator=configured_estimator,
            default_target=adaptive_default_target,
            cost_model=cost_model,
            allow_best_effort=allow_best_effort,
        )
    executor = RealRoutingExecutor(
        acceptance_profile=PROFILE_W3C_BASIC_V1,
        timeout_ms=timeout_ms,
        budgets=budgets,
    )
    demo_orchestrator = (
        ControlledDemoOrchestrator(
            inventory=demo_inventory,
            runtime=adaptive_runtime,
            audit_recorder=audit_recorder,
            timeout_ms=min(timeout_ms, 2000),
        )
        if demo_inventory is not None and adaptive_runtime is not None
        else None
    )
    if demo_inventory is not None and adaptive_runtime is None:
        raise ValueError("controlled demo requires the frozen adaptive runtime")
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
        validate_anchor = getattr(audit_recorder, "validate_anchor", None)
        if callable(validate_anchor):
            await validate_anchor()
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
    app.state.evidence_mode = evidence_mode
    app.state.audit_recorder = audit_recorder
    app.state.adaptive_runtime = adaptive_runtime
    app.state.demo_orchestrator = demo_orchestrator

    if WEB_ROOT.is_dir():
        app.mount(
            "/dashboard",
            StaticFiles(directory=WEB_ROOT, html=True),
            name="dashboard",
        )

        @app.get("/", include_in_schema=False)
        async def dashboard_redirect():
            return RedirectResponse(url="/dashboard/")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "evidence_mode": evidence_mode,
            "acceptance_profile": PROFILE_W3C_BASIC_V1,
            "policies": sorted(policies),
            "provider_inventory_version": inventory.inventory_version,
            "provider_inventory_hash": inventory.inventory_hash(),
            "experiment_id": experiment_id,
            "attempt_timeout_ms": timeout_ms,
            "audit": (
                audit_recorder.describe()
                if hasattr(audit_recorder, "describe")
                else {"recorder": type(audit_recorder).__name__}
            ),
            "adaptive_runtime": _adaptive_service_status(
                runtime=adaptive_runtime,
                policies=policies,
                inventory=inventory,
                budgets=budgets,
                evidence_mode=evidence_mode,
            ),
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
            "evidence_mode": evidence_mode,
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
                    "default_target_slo_probability": policies[
                        RealAdaptiveMinSet.name
                    ].default_target,
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
                    "runtime": (
                        _adaptive_service_status(
                            runtime=adaptive_runtime,
                            policies=policies,
                            inventory=inventory,
                            budgets=budgets,
                            evidence_mode=evidence_mode,
                        )
                    ),
                }
                if RealAdaptiveMinSet.name in policies
                else None
            ),
            "note": (
                "single-static, sequential-failover, and all-race are "
                "baselines; adaptive-min-set is exposed only when a "
                "server-side estimator is configured."
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

    @app.get("/audit/receipts/{receipt_id}")
    async def get_audit_receipt(receipt_id: str):
        getter = getattr(audit_recorder, "get", None)
        if getter is None:
            return JSONResponse(
                status_code=503,
                content={"error": "auditReceiptLookupUnavailable"},
            )
        stored = await getter(receipt_id)
        if stored is None:
            return JSONResponse(
                status_code=404,
                content={"error": "unknownAuditReceipt"},
            )
        return stored

    @app.post("/audit/verify")
    async def verify_audit_receipt(payload: AuditVerifyRequest):
        if payload.receipt_id is not None:
            verifier = getattr(audit_recorder, "verify", None)
            if verifier is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "auditReceiptVerificationUnavailable"},
                )
            verification = await verifier(
                payload.receipt_id,
                disclosed_did=payload.disclosed_did,
                disclosed_result=payload.disclosed_result,
            )
            if verification is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "unknownAuditReceipt"},
                )
            return {
                "receipt_id": payload.receipt_id,
                "verification": "local_record",
                **verification,
            }

        if payload.receipt is None or payload.receipt_hash is None:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "receiptOrReceiptIdRequired",
                    "detail": (
                        "provide receipt_id, or provide both receipt and receipt_hash"
                    ),
                },
            )
        verification = verify_receipt_payload(
            payload.receipt,
            payload.receipt_hash,
            disclosed_did=payload.disclosed_did,
            disclosed_result=payload.disclosed_result,
        )
        return {
            "verification": "standalone_payload",
            "recorded": False,
            **verification,
        }

    @app.get("/demo/scenarios")
    async def demo_scenarios() -> dict:
        if demo_orchestrator is None:
            return {
                "available": False,
                "label": "CONTROLLED DEMO",
                "evidence_mode": "controlled_demo",
                "reset_mode": "fresh_scenario_state",
                "scenarios": [],
                "reason": "controlled_demo_inventory_not_configured",
            }
        return demo_orchestrator.inventory_payload()

    @app.post("/demo/reset")
    async def demo_reset():
        if demo_orchestrator is None:
            return JSONResponse(
                status_code=503,
                content={"error": "controlledDemoUnavailable"},
            )
        try:
            return await demo_orchestrator.reset(app.state.client)
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "controlledProviderUnavailable",
                    "detail": str(exc),
                },
            )

    @app.post("/demo/run")
    async def demo_run(payload: DemoRunRequest):
        if demo_orchestrator is None:
            return JSONResponse(
                status_code=503,
                content={"error": "controlledDemoUnavailable"},
            )
        known = [
            row["id"]
            for row in demo_orchestrator.inventory_payload()["scenarios"]
        ]
        if payload.scenario_id not in known:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "unknownDemoScenario",
                    "available": known,
                },
            )
        try:
            return await demo_orchestrator.run(
                app.state.client,
                payload.scenario_id,
                payload.did,
            )
        except (ControlledDemoError, httpx.HTTPError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "controlledDemoExecutionFailed",
                    "detail": str(exc),
                },
            )

    @app.post("/resolve")
    async def resolve(payload: ResolveRequest):
        did = payload.did
        policy_name, policy_error = _resolve_requested_policy(payload)
        if policy_error is not None:
            return JSONResponse(status_code=422, content=policy_error)

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
        runtime_snapshot = (
            adaptive_runtime.snapshot(evidence_mode)
            if adaptive_runtime is not None
            else None
        )

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
                    candidate_set.candidates,
                    did,
                    target_probability=target,
                    context=(
                        runtime_snapshot.estimator_context()
                        if runtime_snapshot is not None
                        else None
                    ),
                )
                if runtime_snapshot is not None:
                    plan.metadata.update(
                        {
                            "runtime_history_version": runtime_snapshot.version,
                            "runtime_history_request_count": (
                                runtime_snapshot.observed_request_count
                            ),
                        }
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
                    "runtime_history": (
                        adaptive_runtime.history_for(evidence_mode).describe()
                        if adaptive_runtime is not None
                        else None
                    ),
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

        # Commit backend execution observations only after provider I/O has
        # finished. Every service policy contributes what it actually saw, so
        # baseline traffic can honestly seed the frozen runtime estimator.
        runtime_record = (
            adaptive_runtime.observe(
                request_id=result.record.request_id,
                plan=plan,
                candidate_set=candidate_set,
                attempts=result.attempts,
                decision_history_version=(
                    runtime_snapshot.version if runtime_snapshot is not None else None
                ),
                evidence_mode=evidence_mode,
            )
            if adaptive_runtime is not None
            else None
        )

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

        # Build the exact normalized service result first. The audit receipt
        # commits to this object after execution; it does not redefine result
        # normalization or acceptance.
        service_fields = build_service_fields(
            did=did,
            plan=plan,
            candidate_set=candidate_set,
            result=result,
            acceptance_profile=PROFILE_W3C_BASIC_V1,
            evidence_mode=evidence_mode,
            audit=AuditReceipt(False, "pending").to_dict(),
        )
        try:
            runtime_metadata = (
                adaptive_runtime.describe(evidence_mode)
                if adaptive_runtime is not None
                and isinstance(policy, RealAdaptiveMinSet)
                else None
            )
            nonce_source = getattr(audit_recorder, "new_nonce", None)
            commitment = build_commitment(
                request_id=result.record.request_id,
                did=did,
                strategy=plan.policy,
                selected_resolver_ids=service_fields["selection"][
                    "selected_providers"
                ],
                launch_order=list(plan.attempt_order()),
                candidate_count=service_fields["selection"]["candidate_count"],
                policy_identity=build_policy_identity(policy, plan),
                estimator_identity=build_estimator_identity(
                    policy, plan, runtime_metadata
                ),
                estimator_config_hash=plan.metadata.get("estimator_config_hash"),
                acceptance_profile=PROFILE_W3C_BASIC_V1,
                returned_provider=service_fields["result"]["returned_by"],
                normalized_result=service_fields["result"],
                calls_used=service_fields["cost"]["calls_used"],
                evidence_mode=evidence_mode,
                timestamp=result.record.timestamp,
                selection_mode=service_fields["selection"]["selection_mode"],
                target_success=service_fields["selection"]["target_success"],
                estimated_success=service_fields["selection"]["estimated_success"],
                request_nonce=nonce_source() if nonce_source is not None else None,
            )
            audit_receipt = await audit_recorder.record(commitment)
        except Exception:  # audit failure must not suppress a DID result
            audit_receipt = AuditReceipt(False, "recording_failed")
        service_fields["audit"] = audit_receipt.to_dict()
        response.update(service_fields)

        if runtime_record is not None:
            response["runtime_history"] = {
                "storage": "process_memory",
                "durable": False,
                "evidence_mode": runtime_record.evidence_mode,
                "decision_history_version": runtime_record.decision_history_version,
                "committed_history_version": runtime_record.committed_history_version,
                "observed_providers": list(runtime_record.observed_providers),
                "known_subset_updates": len(runtime_record.subset_updates),
            }

        if plan.policy == RealAdaptiveMinSet.name:
            response["adaptive_plan"] = {
                key: plan.metadata.get(key)
                for key in (
                    "status", "target_slo_probability", "candidate_providers",
                    "candidate_count", "evaluated_subset_count",
                    "unestimated_subset_count", "coverage_mode",
                    "expected_subset_count", "estimated_subset_count",
                    "missing_subsets", "exact", "selected_subset",
                    "selected_subset_size", "estimated_subset_success",
                    "selection_cost", "selection_reason", "best_subset",
                    "best_probability", "best_effort", "optimizer_version",
                    "cost_model_id", "tie_break_rule", "estimator_id",
                    "estimator_version", "estimator_config_hash",
                    "runtime_history_version",
                    "runtime_history_request_count",
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


def _resolve_requested_policy(
    payload: ResolveRequest,
) -> tuple[str, dict | None]:
    policy = (
        STRATEGY_ALIASES.get(payload.policy, payload.policy)
        if payload.policy
        else None
    )
    strategy = (
        STRATEGY_ALIASES.get(payload.strategy, payload.strategy)
        if payload.strategy
        else None
    )
    if policy is not None and strategy is not None and policy != strategy:
        return policy, {
            "error": "conflictingPolicyAndStrategy",
            "detail": (
                f"policy {payload.policy!r} and strategy {payload.strategy!r} "
                "select different routing policies"
            ),
        }
    return policy or strategy or "sequential-failover", None


def _adaptive_service_status(
    *,
    runtime: FrozenAdaptiveRuntime | None,
    policies: dict,
    inventory: ProviderInventory,
    budgets: BudgetRegistry,
    evidence_mode: str,
) -> dict:
    """Report installed capability separately from live planning readiness."""
    policy = policies.get(RealAdaptiveMinSet.name)
    if runtime is None or not isinstance(policy, RealAdaptiveMinSet):
        return {
            "available": False,
            "adaptive_available": False,
            "adaptive_ready": False,
            "reason": "adaptive_dependencies_unavailable",
            "readiness_scope": "by_did_method",
            "ready_did_methods": [],
            "by_did_method": {},
        }

    methods = sorted(
        {
            method
            for provider in inventory.providers
            for method in provider.supported_did_methods
        }
    )
    by_method = {}
    for method in methods:
        candidates = select_candidates(
            inventory,
            method,
            budgets=budgets,
            known_adapters=set(ADAPTERS),
        )
        by_method[method] = runtime.assess_readiness(
            candidate_providers=candidates.candidate_ids,
            optimizer=policy.optimizer,
            evidence_mode=evidence_mode,
            target_slo_probability=policy.default_target,
            allow_best_effort=policy.allow_best_effort,
        )

    ready_methods = sorted(
        method
        for method, status in by_method.items()
        if status["adaptive_ready"]
    )
    if ready_methods:
        reason = (
            "ready"
            if len(ready_methods) == len(by_method)
            else "ready_for_some_did_methods"
        )
    elif by_method and all(
        status["reason"] == "insufficient_observed_history"
        for status in by_method.values()
    ):
        reason = "insufficient_observed_history"
    elif not by_method:
        reason = "no_configured_did_methods"
    else:
        reason = "no_ready_did_method"

    return {
        **runtime.describe(evidence_mode),
        "adaptive_ready": bool(ready_methods),
        "reason": reason,
        "readiness_scope": "by_did_method",
        "ready_did_methods": ready_methods,
        "by_did_method": by_method,
    }


def _resolve_evidence_mode(
    inventory: ProviderInventory, configured: str | None
) -> str:
    inferred = (
        "controlled_demo"
        if inventory.inventory_version.lower().startswith("local-")
        else "real"
    )
    if configured is None:
        return inferred
    if configured not in EVIDENCE_MODES:
        raise ValueError(
            f"evidence_mode must be one of {sorted(EVIDENCE_MODES)}, "
            f"got {configured!r}"
        )
    if inferred == "controlled_demo" and configured == "real":
        raise ValueError("a local controlled inventory cannot be labeled real")
    return configured


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


def create_default_app() -> FastAPI:
    """Create the normal MVP app, enabling the verified frozen runtime."""
    inventory_path = os.environ.get("AVDR_PROVIDER_INVENTORY")
    adaptive_runtime = try_load_frozen_adaptive_runtime()
    audit_anchor = build_audit_anchor_from_env()
    return create_app(
        inventory=load_provider_inventory(inventory_path),
        adaptive_runtime=adaptive_runtime,
        audit_recorder=LocalAuditRecorder(anchor=audit_anchor),
        demo_inventory=(
            load_provider_inventory(REPO_ROOT / "config" / "providers.local.yaml")
            if adaptive_runtime is not None
            else None
        ),
    )


app = create_default_app()
