"""Frozen adaptive estimator integration in the real serving runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from avdr.adaptive.estimator import ControlledTableEstimator
from avdr.adaptive.optimizer import (
    ESTIMATOR_COVERAGE_INCOMPLETE,
    MinimumSetOptimizer,
    SLO_ESTIMATE_UNSATISFIABLE,
)
from avdr.candidates import CandidateSet
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.learning.estimators import RollingEmpiricalEstimator
from avdr.models import RealRoutingAttempt
from avdr.real_router.adaptive_policy import RealAdaptiveMinSet
from avdr.real_router.app import create_app
from avdr.real_router.policies import RealRoutingPlan
from avdr.real_router.runtime_adaptive import (
    RuntimeObservedHistory,
    load_frozen_adaptive_runtime,
)
from avdr.telemetry import TelemetrySink

DID = "did:example:runtime-adaptive"


def _entry(provider_id: str, endpoint: str) -> ProviderEntry:
    return ProviderEntry(
        id=provider_id,
        implementation_id="avdr-test-resolver",
        operator="controlled test",
        endpoint=endpoint,
        adapter="universal-resolver-v1",
        supported_did_methods=["example"],
        available=True,
    )


def _inventory(healthy_cluster) -> ProviderInventory:
    return ProviderInventory(
        inventory_version="local-runtime-adaptive-test",
        providers=[
            _entry("local-a", healthy_cluster["resolver-a"].url),
            _entry("local-b", healthy_cluster["resolver-b"].url),
            _entry("local-c", healthy_cluster["resolver-c"].url),
        ],
    )


async def _clients(app):
    """Yield an in-process service client with real loopback provider I/O."""
    service_transport = httpx.ASGITransport(app=app)
    service = httpx.AsyncClient(
        transport=service_transport,
        base_url="http://service",
    )
    outbound = httpx.AsyncClient()
    app.state.client = outbound
    return service, outbound


def test_frozen_runtime_loads_exact_estimator_contract():
    runtime = load_frozen_adaptive_runtime()

    assert isinstance(runtime.estimator, RollingEmpiricalEstimator)
    assert runtime.estimator.window == 10
    assert runtime.estimator.prior == pytest.approx(0.6879699248120301)
    assert runtime.estimator.prior_weight == 2.0
    assert runtime.estimator.config_hash() == (
        "sha256:2c73f54dc82b6983d3b1842443f0a81c6bc28cda123e9f1cf19d5bbdfcd3c23e"
    )
    assert runtime.artifact_sha256 == (
        "sha256:532cc2a5269bb39a8b9e6d1656fbdf08967b4115a6fb22784a5e6d07c0f60545"
    )
    assert runtime.target_slo_probability == 0.9
    assert runtime.deadline_tau_ms == 250.0


@pytest.mark.asyncio
async def test_policies_advertises_adaptive_only_with_available_runtime(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        body = (await service.get("/policies")).json()

    assert "adaptive-min-set" in body["available"]
    assert body["adaptive"]["estimator_id"] == "b1-rolling-empirical"
    assert body["adaptive"]["default_target_slo_probability"] == 0.9
    assert body["adaptive"]["runtime"]["history"]["durable"] is False
    assert isinstance(app.state.policies["adaptive-min-set"], RealAdaptiveMinSet)
    assert isinstance(
        app.state.policies["adaptive-min-set"].optimizer,
        MinimumSetOptimizer,
    )


@pytest.mark.asyncio
async def test_empty_history_preserves_typed_unsatisfiable_result(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/resolve",
            json={"did": DID, "strategy": "adaptive"},
        )

    body = response.json()
    assert response.status_code == 409
    assert body["error"] == SLO_ESTIMATE_UNSATISFIABLE
    assert body["exact"] is True
    assert body["selected_subset"] is None
    assert body["runtime_history"]["observed_request_count"] == 0
    assert (
        runtime.history_for("controlled_demo").snapshot().observed_request_count
        == 0
    )


@pytest.mark.asyncio
async def test_observed_baseline_traffic_seeds_real_adaptive_invocation(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
        single_static_target="local-a",
        timeout_ms=2000,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        for index in range(5):
            seeded = await service.post(
                "/resolve",
                json={
                    "did": f"did:example:seed-{index}",
                    "strategy": "single",
                },
            )
            assert seeded.status_code == 200

        response = await service.post(
            "/resolve",
            json={"did": DID, "strategy": "adaptive"},
        )

    body = response.json()
    expected_q_hat = (5 + 2 * runtime.estimator.prior) / (5 + 2)
    assert response.status_code == 200
    assert body["strategy"] == "adaptive-min-set"
    assert body["routing_policy"] == "adaptive-min-set"
    assert body["selection"]["selected_providers"] == ["local-a"]
    assert body["selection"]["estimated_success"] == pytest.approx(expected_q_hat)
    assert body["selection"]["target_success"] == 0.9
    assert body["selection"]["selection_mode"] == "exact"
    assert body["adaptive_plan"]["exact"] is True
    assert body["adaptive_plan"]["runtime_history_version"] == 5
    assert body["runtime_history"]["committed_history_version"] == 6
    assert body["cost"] == {
        "calls_used": 1,
        "calls_if_all_race": 3,
        "calls_saved_vs_all_race": 2,
    }
    assert body["attempts"][0]["provider"] == "local-a"
    assert body["evidence"] == {"mode": "controlled_demo"}
    assert body["audit"]["recorded"] is False

    record = runtime.history_for("controlled_demo").records()[-1]
    assert record.request_id == body["request_id"]
    assert record.selected_providers == ("local-a",)
    assert record.dispatched_providers == ("local-a",)
    assert record.observed_providers == ("local-a",)
    assert record.attempts[0].accepted is True
    assert record.attempts[0].normalized_document_hash


@pytest.mark.asyncio
async def test_unselected_outcomes_remain_unknown_and_candidate_changes_do_not_alias(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
        single_static_target="local-a",
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/resolve",
            json={"did": DID, "policy": "single-static"},
        )

    assert response.status_code == 200
    snapshot = runtime.history_for("controlled_demo").snapshot()
    context = snapshot.estimator_context()
    assert snapshot.subset_history[("local-a",)] == (1,)
    assert ("local-b",) not in snapshot.subset_history
    assert ("local-c",) not in snapshot.subset_history
    assert ("local-b", "local-c") not in snapshot.subset_history
    # A successful observed A proves supersets containing A succeeded; this
    # is the existing three-valued rule, not a hidden B/C observation.
    assert snapshot.subset_history[("local-a", "local-b")] == (1,)
    assert runtime.estimator.estimate(("new-provider",), context) == pytest.approx(
        runtime.estimator.prior
    )


@pytest.mark.asyncio
async def test_runtime_history_reset_and_new_instance_start_empty(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/resolve",
            json={"did": DID, "policy": "single-static"},
        )
    assert response.status_code == 200
    assert (
        runtime.history_for("controlled_demo").snapshot().observed_request_count
        == 1
    )

    fresh_runtime = load_frozen_adaptive_runtime()
    assert fresh_runtime.history.snapshot().observed_request_count == 0
    runtime.history_for("controlled_demo").reset()
    assert runtime.history_for("controlled_demo").snapshot().version == 0
    assert runtime.history_for("controlled_demo").snapshot().subset_history == {}


@pytest.mark.asyncio
async def test_incomplete_estimator_coverage_cannot_be_exact(
    healthy_cluster, tmp_path
):
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        estimator=ControlledTableEstimator({("local-a",): 0.99}),
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/resolve",
            json={
                "did": DID,
                "strategy": "adaptive",
                "target_slo_probability": 0.9,
            },
        )

    body = response.json()
    assert response.status_code == 409
    assert body["error"] == ESTIMATOR_COVERAGE_INCOMPLETE
    assert body["exact"] is False
    assert body["estimated_subset_count"] == 1
    assert body["expected_subset_count"] == 7


def test_concurrent_history_commits_are_atomic_and_snapshots_are_isolated():
    history = RuntimeObservedHistory()
    provider = _entry("local-a", "http://127.0.0.1:8001")
    candidates = CandidateSet(did_method="example", candidates=[provider])
    plan = RealRoutingPlan(
        policy="single-static",
        provider_order=["local-a"],
        execution="sequential",
        max_attempts=1,
        min_providers=1,
    )
    attempt = RealRoutingAttempt(
        request_id="template",
        launch_position=0,
        provider_id="local-a",
        implementation_id="avdr-test-resolver",
        resolver_endpoint_id="local-a",
        launch_offset_ms=1.0,
        latency_ms=10.0,
        transport_outcome="http_response",
        http_status=200,
        acceptance_profile="w3c-basic-v1",
        accepted=True,
        normalized_document_hash="sha256:test",
    )
    original_snapshot = history.snapshot()

    def commit(index: int):
        return history.update_from_execution(
            request_id=f"request-{index}",
            plan=plan,
            candidate_set=candidates,
            attempts=[attempt.model_copy(update={"request_id": f"request-{index}"})],
            deadline_tau_ms=250.0,
            decision_history_version=0,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(commit, range(32)))

    final_snapshot = history.snapshot()
    assert original_snapshot.version == 0
    assert original_snapshot.subset_history == {}
    assert final_snapshot.version == 32
    assert final_snapshot.observed_request_count == 32
    assert final_snapshot.subset_history[("local-a",)] == (1,) * 32
    assert sorted(record.committed_history_version for record in records) == list(
        range(1, 33)
    )


def test_policy_and_strategy_conflict_is_rejected(healthy_cluster, tmp_path):
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=load_frozen_adaptive_runtime(),
    )
    # This validation happens before any provider I/O, so a synchronous ASGI
    # transport is unnecessary; exercise the helper through the request type.
    from avdr.real_router.app import ResolveRequest, _resolve_requested_policy

    chosen, error = _resolve_requested_policy(
        ResolveRequest(did=DID, policy="single-static", strategy="adaptive")
    )
    assert chosen == "single-static"
    assert error["error"] == "conflictingPolicyAndStrategy"
    assert app.state.adaptive_runtime.history.snapshot().version == 0
