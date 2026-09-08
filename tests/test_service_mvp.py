"""Service-contract and dashboard integration tests for the hackathon MVP."""

from __future__ import annotations

import httpx
import pytest

from avdr.adaptive.estimator import ControlledTableEstimator
from avdr.audit import AuditReceipt
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.real_router.app import create_app
from avdr.telemetry import TelemetrySink

DID = "did:example:mvp-subject"


def entry(provider_id: str, endpoint: str, adapter: str = "universal-resolver-v1"):
    return ProviderEntry(
        id=provider_id,
        implementation_id="avdr-test-resolver",
        operator="controlled test",
        endpoint=endpoint,
        adapter=adapter,
        supported_did_methods=["example"],
        available=True,
    )


def local_inventory(healthy_cluster) -> ProviderInventory:
    return ProviderInventory(
        inventory_version="local-mvp-test",
        providers=[
            entry("local-a", healthy_cluster["resolver-a"].url),
            entry("local-b", healthy_cluster["resolver-b"].url),
            entry("local-c", healthy_cluster["resolver-c"].url),
        ],
    )


async def post(app, payload: dict, outbound: httpx.AsyncClient | None = None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
        if outbound is not None:
            app.state.client = outbound
            return await client.post("/resolve", json=payload)
        async with httpx.AsyncClient() as real_outbound:
            app.state.client = real_outbound
            return await client.post("/resolve", json=payload)


@pytest.mark.asyncio
async def test_dashboard_is_served_by_the_backend(healthy_cluster, tmp_path):
    app = create_app(
        inventory=local_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://service",
        follow_redirects=False,
    ) as client:
        root = await client.get("/")
        page = await client.get("/dashboard/")
        script = await client.get("/dashboard/app.js")
        styles = await client.get("/dashboard/styles.css")

    assert root.status_code in (302, 307)
    assert root.headers["location"] == "/dashboard/"
    assert page.status_code == 200
    assert "AVDR Resolution Console" in page.text
    assert "Demo Scenarios" in page.text
    assert "CONTROLLED DEMO" in page.text
    assert "fetch(\"/resolve\"" in script.text
    assert "fetch(\"/demo/run\"" in script.text
    assert styles.status_code == 200


@pytest.mark.asyncio
async def test_product_contract_is_additive_and_attempts_are_inline(
    healthy_cluster, tmp_path
):
    app = create_app(
        inventory=local_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="local-a",
    )
    response = await post(app, {"did": DID, "policy": "single-static"})
    body = response.json()

    assert response.status_code == 200
    # Existing response names remain intact.
    assert body["requested_did"] == DID
    assert body["routing_policy"] == "single-static"
    assert body["did_document"]["id"] == DID
    # Dashboard DTO is self-contained.
    assert body["did"] == DID
    assert body["strategy"] == "single-static"
    assert body["success"] is True
    assert body["selection"] == {
        "candidate_count": 3,
        "selected_count": 1,
        "selected_providers": ["local-a"],
        "estimated_success": None,
        "target_success": None,
        "selection_mode": "policy_defined",
        "coverage_mode": None,
    }
    assert body["result"]["returned_by"] == "local-a"
    assert body["result"]["didDocument"]["id"] == DID
    assert body["cost"] == {
        "calls_used": 1,
        "calls_if_all_race": 3,
        "calls_saved_vs_all_race": 2,
    }
    assert body["evidence"] == {"mode": "controlled_demo"}
    assert body["audit"]["recorded"] is False
    by_provider = {row["provider"]: row for row in body["attempts"]}
    assert by_provider["local-a"]["outcome"] == "accepted"
    assert by_provider["local-a"]["latency_ms"] is not None
    assert by_provider["local-b"]["outcome"] == "not_selected"
    assert by_provider["local-b"]["accepted"] is None


@pytest.mark.asyncio
async def test_adaptive_contract_reports_exact_selection_without_faking_coverage(
    healthy_cluster, tmp_path
):
    table = {
        ("local-a",): 0.99,
        ("local-b",): 0.93,
        ("local-c",): 0.91,
        ("local-a", "local-b"): 0.995,
        ("local-a", "local-c"): 0.994,
        ("local-b", "local-c"): 0.98,
        ("local-a", "local-b", "local-c"): 0.999,
    }
    app = create_app(
        inventory=local_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        estimator=ControlledTableEstimator(table, label="mvp-contract-test"),
    )
    response = await post(
        app,
        {
            "did": DID,
            "policy": "adaptive-min-set",
            "target_slo_probability": 0.95,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["selection"]["selected_providers"] == ["local-a"]
    assert body["selection"]["selected_count"] == 1
    assert body["selection"]["estimated_success"] == 0.99
    assert body["selection"]["target_success"] == 0.95
    assert body["selection"]["selection_mode"] == "exact"
    assert body["adaptive_plan"]["exact"] is True
    assert body["adaptive_plan"]["estimated_subset_count"] == 7
    assert body["cost"]["calls_saved_vs_all_race"] == 2


@pytest.mark.asyncio
async def test_normalized_result_keeps_a_bare_did_document(tmp_path):
    inventory = ProviderInventory(
        inventory_version="service-test",
        providers=[
            entry(
                "bare-document-provider",
                "https://provider.example",
                adapter="did-document-only-v1",
            )
        ],
    )
    app = create_app(
        inventory=inventory,
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="bare-document-provider",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/did+json"},
            json={"@context": ["https://www.w3.org/ns/did/v1"], "id": DID},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as outbound:
        response = await post(
            app,
            {"did": DID, "policy": "single-static"},
            outbound=outbound,
        )
    body = response.json()

    assert response.status_code == 200
    assert body["did_document"]["id"] == DID
    assert body["result"]["didDocument"]["id"] == DID
    assert body["result"]["didResolutionMetadata"] is None
    assert body["result"]["didDocumentMetadata"] is None


def test_controlled_inventory_cannot_be_labeled_real(healthy_cluster, tmp_path):
    with pytest.raises(ValueError, match="cannot be labeled real"):
        create_app(
            inventory=local_inventory(healthy_cluster),
            sink=TelemetrySink(tmp_path / "telemetry"),
            evidence_mode="real",
        )


@pytest.mark.asyncio
async def test_audit_boundary_receives_hashes_not_raw_telemetry(healthy_cluster, tmp_path):
    class CapturingRecorder:
        commitment = None

        async def record(self, commitment):
            self.commitment = commitment
            return AuditReceipt(True, "recorded", "test:commitment:1")

    recorder = CapturingRecorder()
    app = create_app(
        inventory=local_inventory(healthy_cluster),
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="local-a",
        audit_recorder=recorder,
    )
    body = (await post(app, {"did": DID, "policy": "single-static"})).json()

    assert body["audit"] == {
        "recorded": True,
        "status": "recorded",
        "reference": "test:commitment:1",
    }
    assert recorder.commitment.did_hash.startswith("sha256:")
    assert recorder.commitment.result_hash.startswith("sha256:")
    assert recorder.commitment.selected_providers == ("local-a",)
    assert not hasattr(recorder.commitment, "did")
    assert not hasattr(recorder.commitment, "attempts")
