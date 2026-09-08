"""Service and controlled-demo integration tests for audit receipts."""

from __future__ import annotations

import copy

import httpx
import pytest

from avdr.audit import LocalAuditRecorder
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.real_router.app import create_app
from avdr.real_router.runtime_adaptive import load_frozen_adaptive_runtime
from avdr.telemetry import TelemetrySink

DID = "did:example:audit-api-subject"


def _entry(provider_id: str, endpoint: str) -> ProviderEntry:
    return ProviderEntry(
        id=provider_id,
        implementation_id="avdr-audit-test-resolver",
        operator="controlled loopback test",
        endpoint=endpoint,
        adapter="universal-resolver-v1",
        supported_did_methods=["example"],
        available=True,
    )


def _inventory(healthy_cluster, version="real-mode-loopback-audit-test"):
    return ProviderInventory(
        inventory_version=version,
        providers=[
            _entry("local-a", healthy_cluster["resolver-a"].url),
            _entry("local-b", healthy_cluster["resolver-b"].url),
            _entry("local-c", healthy_cluster["resolver-c"].url),
        ],
    )


async def _clients(app):
    service = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    )
    outbound = httpx.AsyncClient()
    app.state.client = outbound
    return service, outbound


@pytest.mark.asyncio
async def test_real_success_receipt_lookup_verify_and_sanity(healthy_cluster, tmp_path):
    recorder = LocalAuditRecorder()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="local-a",
        audit_recorder=recorder,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/resolve", json={"did": DID, "policy": "single-static"}
        )
        body = response.json()
        lookup = await service.get(
            f"/audit/receipts/{body['audit']['receipt_id']}"
        )
        verified = await service.post(
            "/audit/verify",
            json={
                "receipt_id": body["audit"]["receipt_id"],
                "disclosed_did": DID,
                "disclosed_result": body["result"],
            },
        )

    assert response.status_code == 200
    assert body["audit"]["recorded"] is True
    assert body["audit"]["verification"] == "local"
    assert body["audit"]["integrity_verified"] is True
    assert body["audit"]["anchor"] == {
        "status": "not_configured",
        "network": None,
        "transaction_id": None,
    }
    receipt = lookup.json()["receipt"]
    assert lookup.status_code == 200
    assert receipt["selected_count"] == body["selection"]["selected_count"]
    assert receipt["selected_resolver_ids"] == sorted(
        body["selection"]["selected_providers"]
    )
    assert receipt["launch_order"] == body["selection"]["selected_providers"]
    assert receipt["returned_provider"] == body["result"]["returned_by"]
    assert receipt["evidence_mode"] == body["evidence"]["mode"] == "real"
    assert receipt["policy_identity"]["name"] == body["strategy"]
    assert receipt["estimator_identity"] is None
    assert DID not in lookup.text
    assert verified.status_code == 200
    assert verified.json()["valid"] is True
    assert verified.json()["did_commitment_valid"] is True
    assert verified.json()["result_commitment_valid"] is True


@pytest.mark.asyncio
async def test_executed_failure_gets_receipt_and_resolution_remains_failure(
    healthy_cluster, tmp_path
):
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    try:
        recorder = LocalAuditRecorder()
        app = create_app(
            inventory=ProviderInventory(
                inventory_version="real-mode-loopback-failure-audit-test",
                providers=[_entry("local-a", healthy_cluster["resolver-a"].url)],
            ),
            sink=TelemetrySink(tmp_path / "telemetry"),
            single_static_target="local-a",
            audit_recorder=recorder,
        )
        service, outbound = await _clients(app)
        async with service, outbound:
            response = await service.post(
                "/resolve", json={"did": DID, "policy": "single-static"}
            )
            body = response.json()
            lookup = await service.get(
                f"/audit/receipts/{body['audit']['receipt_id']}"
            )
            verified = await service.post(
                "/audit/verify",
                json={
                    "receipt_id": body["audit"]["receipt_id"],
                    "disclosed_result": body["result"],
                },
            )
    finally:
        healthy_cluster["resolver-a"].reset()

    assert response.status_code == 502
    assert body["success"] is False
    assert body["audit"]["recorded"] is True
    assert lookup.json()["receipt"]["returned_provider"] is None
    assert verified.json()["valid"] is True
    assert verified.json()["result_commitment_valid"] is True


@pytest.mark.asyncio
async def test_adaptive_receipt_binds_packaged_runtime_provenance(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    recorder = LocalAuditRecorder()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
        single_static_target="local-a",
        audit_recorder=recorder,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        for index in range(5):
            warm = await service.post(
                "/resolve",
                json={
                    "did": f"did:example:audit-warm-{index}",
                    "policy": "single-static",
                },
            )
            assert warm.status_code == 200
        response = await service.post(
            "/resolve", json={"did": DID, "strategy": "adaptive"}
        )
        body = response.json()
        lookup = await service.get(
            f"/audit/receipts/{body['audit']['receipt_id']}"
        )

    assert response.status_code == 200, response.text
    receipt = lookup.json()["receipt"]
    runtime_identity = runtime.describe("real")
    estimator = receipt["estimator_identity"]
    assert receipt["strategy"] == "adaptive-min-set"
    assert estimator["estimator_id"] == runtime_identity["estimator_id"]
    assert estimator["estimator_version"] == runtime_identity["estimator_version"]
    assert estimator["estimator_class"] == runtime_identity["estimator_class"]
    assert estimator["estimator_config_hash"] == runtime_identity[
        "estimator_config_hash"
    ]
    assert estimator["packaged_spec_sha256"] == runtime_identity[
        "packaged_spec_sha256"
    ]
    assert receipt["estimator_config_hash"] == body["adaptive_plan"][
        "estimator_config_hash"
    ]


@pytest.mark.asyncio
async def test_controlled_demo_receipt_is_labeled_and_verifies(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    inventory = _inventory(healthy_cluster, "local-controlled-audit-test")
    recorder = LocalAuditRecorder()
    app = create_app(
        inventory=inventory,
        demo_inventory=inventory,
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
        audit_recorder=recorder,
        timeout_ms=1000,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/demo/run", json={"scenario_id": "normal", "did": DID}
        )
        body = response.json()
        verified = await service.post(
            "/audit/verify", json={"receipt_id": body["audit"]["receipt_id"]}
        )
        lookup = await service.get(
            f"/audit/receipts/{body['audit']['receipt_id']}"
        )

    assert response.status_code == 200
    assert body["evidence"]["mode"] == "controlled_demo"
    assert body["audit"]["recorded"] is True
    assert lookup.json()["receipt"]["evidence_mode"] == "controlled_demo"
    assert verified.json()["valid"] is True


@pytest.mark.asyncio
async def test_audit_recording_failure_is_explicit_and_non_fatal(
    healthy_cluster, tmp_path
):
    class BrokenRecorder:
        async def record(self, commitment):
            raise OSError("controlled audit sink failure")

    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="local-a",
        audit_recorder=BrokenRecorder(),
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        response = await service.post(
            "/resolve", json={"did": DID, "policy": "single-static"}
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["audit"] == {
        "recorded": False,
        "status": "recording_failed",
        "reference": None,
    }


@pytest.mark.asyncio
async def test_verify_api_rejects_tampered_external_receipt(healthy_cluster, tmp_path):
    recorder = LocalAuditRecorder()
    app = create_app(
        inventory=_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="local-a",
        audit_recorder=recorder,
    )
    service, outbound = await _clients(app)
    async with service, outbound:
        body = (
            await service.post(
                "/resolve", json={"did": DID, "policy": "single-static"}
            )
        ).json()
        stored = (
            await service.get(f"/audit/receipts/{body['audit']['receipt_id']}")
        ).json()
        tampered = copy.deepcopy(stored["receipt"])
        tampered["returned_provider"] = "local-c"
        verification = await service.post(
            "/audit/verify",
            json={"receipt": tampered, "receipt_hash": stored["receipt_hash"]},
        )

    assert verification.status_code == 200
    assert verification.json()["verification"] == "standalone_payload"
    assert verification.json()["recorded"] is False
    assert verification.json()["valid"] is False
    assert verification.json()["receipt_hash_valid"] is False
