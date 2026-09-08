"""Controlled dashboard demo orchestration over real serving components."""

from __future__ import annotations

import httpx
import pytest

from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.real_router.adaptive_policy import RealAdaptiveMinSet
from avdr.real_router.app import create_app
from avdr.real_router.runtime_adaptive import load_frozen_adaptive_runtime
from avdr.telemetry import TelemetrySink

DID = "did:example:demo-test"
SCENARIO_IDS = ["normal", "slow_failure", "fast_unacceptable"]


def _entry(provider_id: str, endpoint: str) -> ProviderEntry:
    return ProviderEntry(
        id=provider_id,
        implementation_id="avdr-mock-resolver",
        operator="controlled test",
        endpoint=endpoint,
        adapter="universal-resolver-v1",
        supported_did_methods=["example"],
        available=True,
    )


def _inventory(healthy_cluster) -> ProviderInventory:
    return ProviderInventory(
        inventory_version="local-controlled-demo-test",
        providers=[
            _entry("local-a", healthy_cluster["resolver-a"].url),
            _entry("local-b", healthy_cluster["resolver-b"].url),
            _entry("local-c", healthy_cluster["resolver-c"].url),
        ],
    )


async def _service(app):
    transport = httpx.ASGITransport(app=app)
    service = httpx.AsyncClient(
        transport=transport,
        base_url="http://service",
    )
    outbound = httpx.AsyncClient()
    app.state.client = outbound
    return service, outbound


def _app(healthy_cluster, tmp_path):
    runtime = load_frozen_adaptive_runtime()
    inventory = _inventory(healthy_cluster)
    app = create_app(
        inventory=inventory,
        demo_inventory=inventory,
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
        timeout_ms=1000,
        single_static_target="local-a",
    )
    return app, runtime


@pytest.mark.asyncio
async def test_demo_inventory_exposes_exactly_three_controlled_scenarios(
    healthy_cluster, tmp_path
):
    app, _ = _app(healthy_cluster, tmp_path)
    service, outbound = await _service(app)
    async with service, outbound:
        response = await service.get("/demo/scenarios")

    body = response.json()
    assert response.status_code == 200
    assert body["available"] is True
    assert body["label"] == "CONTROLLED DEMO"
    assert body["evidence_mode"] == "controlled_demo"
    assert body["reset_mode"] == "fresh_scenario_state"
    assert [scenario["id"] for scenario in body["scenarios"]] == SCENARIO_IDS
    assert all(
        scenario["evidence_mode"] == "controlled_demo"
        for scenario in body["scenarios"]
    )


@pytest.mark.asyncio
async def test_all_demo_scenarios_use_existing_policy_and_service_dto(
    healthy_cluster, tmp_path
):
    app, _ = _app(healthy_cluster, tmp_path)
    assert isinstance(app.state.demo_orchestrator.policy, RealAdaptiveMinSet)
    service, outbound = await _service(app)

    async with service, outbound:
        bodies = {}
        for scenario_id in SCENARIO_IDS:
            response = await service.post(
                "/demo/run",
                json={"scenario_id": scenario_id, "did": DID},
            )
            assert response.status_code == 200, response.text
            bodies[scenario_id] = response.json()

    for scenario_id, body in bodies.items():
        assert body["scenario"]["id"] == scenario_id
        assert body["scenario"]["label"] == "CONTROLLED DEMO"
        assert body["strategy"] == "adaptive-min-set"
        assert body["evidence"] == {"mode": "controlled_demo"}
        assert body["audit"]["recorded"] is False
        assert body["audit"]["status"] == "controlled_demo_not_recorded"
        assert body["demo_bootstrap"]["label"] == "demo bootstrap"
        assert body["demo_bootstrap"]["counted_as_real_traffic"] is False
        assert body["selection"]["candidate_count"] == 3
        assert isinstance(body["selection"]["selected_providers"], list)
        assert isinstance(body["attempts"], list)
        assert body["result"]["acceptance_profile"] == "w3c-basic-v1"
        assert body["result"]["didDocument"]["id"] == DID
        assert body["cost"]["calls_if_all_race"] == 3

    normal = bodies["normal"]
    assert normal["demo_bootstrap"]["observations"] == 5
    assert normal["selection"]["selected_providers"] == ["local-a"]
    assert normal["result"]["returned_by"] == "local-a"
    assert normal["cost"]["calls_used"] == 1
    assert normal["cost"]["calls_saved_vs_all_race"] == 2

    degraded = bodies["slow_failure"]
    assert degraded["demo_bootstrap"]["observations"] == 5
    assert degraded["selection"]["selected_providers"] == ["local-a", "local-b"]
    assert degraded["result"]["returned_by"] == "local-b"
    assert degraded["cost"]["calls_used"] == 2
    assert degraded["cost"]["calls_saved_vs_all_race"] == 1
    degraded_attempts = {row["provider"]: row for row in degraded["attempts"]}
    assert degraded_attempts["local-a"]["dispatched"] is True
    assert degraded_attempts["local-a"]["accepted"] is False
    assert degraded_attempts["local-a"]["outcome"] == "failed"
    assert degraded_attempts["local-b"]["accepted"] is True

    unacceptable = bodies["fast_unacceptable"]
    assert unacceptable["demo_bootstrap"]["observations"] == 5
    assert unacceptable["selection"]["selected_providers"] == [
        "local-a",
        "local-b",
    ]
    assert unacceptable["result"]["returned_by"] == "local-b"
    assert unacceptable["cost"]["calls_used"] == 2
    unacceptable_attempts = {
        row["provider"]: row for row in unacceptable["attempts"]
    }
    assert unacceptable_attempts["local-a"]["accepted"] is False
    assert unacceptable_attempts["local-a"]["outcome"] == "unacceptable"
    assert "does not match requested DID" in unacceptable_attempts["local-a"][
        "acceptance_reason"
    ]
    assert unacceptable_attempts["local-b"]["accepted"] is True
    assert (
        unacceptable_attempts["local-a"]["latency_ms"]
        < unacceptable_attempts["local-b"]["latency_ms"]
    )


@pytest.mark.asyncio
async def test_demo_runs_and_reset_never_change_real_history_or_readiness(
    healthy_cluster, tmp_path
):
    app, runtime = _app(healthy_cluster, tmp_path)
    real_before = runtime.history.snapshot()
    real_readiness_before = runtime.assess_readiness(
        candidate_providers=["local-a", "local-b", "local-c"],
        optimizer=app.state.policies["adaptive-min-set"].optimizer,
        evidence_mode="real",
    )
    service, outbound = await _service(app)

    async with service, outbound:
        for scenario_id in SCENARIO_IDS:
            response = await service.post(
                "/demo/run", json={"scenario_id": scenario_id, "did": DID}
            )
            assert response.status_code == 200
        reset = await service.post("/demo/reset")

    assert reset.status_code == 200
    assert reset.json()["history"]["observed_request_count"] == 0
    assert runtime.history.snapshot() == real_before
    real_readiness_after = runtime.assess_readiness(
        candidate_providers=["local-a", "local-b", "local-c"],
        optimizer=app.state.policies["adaptive-min-set"].optimizer,
        evidence_mode="real",
    )
    assert real_readiness_after == real_readiness_before


@pytest.mark.asyncio
async def test_fresh_run_reset_prevents_scenario_history_leakage(
    healthy_cluster, tmp_path
):
    app, runtime = _app(healthy_cluster, tmp_path)
    service, outbound = await _service(app)

    async with service, outbound:
        normal = await service.post(
            "/demo/run", json={"scenario_id": "normal", "did": DID}
        )
        assert normal.status_code == 200
        assert runtime.snapshot("controlled_demo").observed_request_count == 6

        reset = await service.post("/demo/reset")
        assert reset.status_code == 200
        assert runtime.snapshot("controlled_demo").observed_request_count == 0

        degraded = await service.post(
            "/demo/run", json={"scenario_id": "slow_failure", "did": DID}
        )

    assert degraded.status_code == 200
    body = degraded.json()
    assert body["demo_bootstrap"]["observations"] == 5
    assert body["runtime_history"]["decision_history_version"] == 5
    assert body["runtime_history"]["committed_history_version"] == 6
    records = runtime.history_for("controlled_demo").records()
    assert len(records) == 6
    assert all(record.evidence_mode == "controlled_demo" for record in records)
    assert all("normal" not in record.request_id for record in records)


@pytest.mark.asyncio
async def test_unknown_scenario_and_non_example_did_fail_without_history_update(
    healthy_cluster, tmp_path
):
    app, runtime = _app(healthy_cluster, tmp_path)
    service, outbound = await _service(app)
    async with service, outbound:
        unknown = await service.post(
            "/demo/run", json={"scenario_id": "not-a-scenario", "did": DID}
        )
        wrong_method = await service.post(
            "/demo/run",
            json={"scenario_id": "normal", "did": "did:web:example.com"},
        )

    assert unknown.status_code == 404
    assert unknown.json()["error"] == "unknownDemoScenario"
    assert wrong_method.status_code == 503
    assert wrong_method.json()["error"] == "controlledDemoExecutionFailed"
    assert runtime.history.snapshot().observed_request_count == 0
    assert runtime.snapshot("controlled_demo").observed_request_count == 0


@pytest.mark.asyncio
async def test_existing_resolve_api_remains_available_with_demo_enabled(
    healthy_cluster, tmp_path
):
    app, _ = _app(healthy_cluster, tmp_path)
    service, outbound = await _service(app)
    async with service, outbound:
        response = await service.post(
            "/resolve",
            json={"did": DID, "strategy": "single"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["strategy"] == "single-static"
    assert body["selection"]["selected_providers"] == ["local-a"]
    assert body["result"]["returned_by"] == "local-a"
