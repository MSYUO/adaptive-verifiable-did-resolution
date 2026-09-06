"""Real-provider routing service tests.

Two layers, both offline:
  * candidate selection / budget logic -- pure, no I/O
  * routing policies end-to-end against the LOCAL uvicorn mock resolvers on
    loopback, driven through the real adapter + acceptance path

Zero public-network calls: the session-wide socket guard in conftest blocks
any non-loopback connection, so the public resolver's 10 req / 1800 s budget
can never be spent by the test suite.

All delays/failures here are [CONTROLLED INJECTION] on one shared host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.budget import RATE_BUDGET_EXHAUSTED, BudgetRegistry, ProviderBudget
from avdr.candidates import (
    AUTH_REQUIRED_NO_CREDENTIALS,
    INSUFFICIENT_QUALIFIED_PROVIDERS,
    METHOD_NOT_SUPPORTED,
    PROVIDER_UNAVAILABLE,
    UNKNOWN_ADAPTER,
    select_candidates,
)
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.probe import CANCELED_AFTER_DISPATCH, CANCELED_BEFORE_DISPATCH
from avdr.profiles import PROFILE_W3C_BASIC_V1
from avdr.real_router.app import create_app
from avdr.real_router.policies import RealAllRace, RealSingleStatic
from avdr.telemetry import TelemetrySink

from conftest import INJECTED_DELAY_MS

DID = "did:example:routing-subject"


def entry(pid, **kw):
    base = dict(
        id=pid,
        endpoint=f"http://127.0.0.1:9/{pid}",
        adapter="universal-resolver-v1",
        supported_did_methods=["example"],
        implementation_id="test-impl",
    )
    base.update(kw)
    return ProviderEntry(**base)


def inventory_of(*providers) -> ProviderInventory:
    return ProviderInventory(inventory_version="test", providers=list(providers))


# ==========================================================================
# 1-4. capability-aware selection
# ==========================================================================


def test_capability_filtering_by_did_method():
    inv = inventory_of(
        entry("supports-example"),
        entry("supports-key", supported_did_methods=["key"]),
    )
    result = select_candidates(inv, "example")
    assert result.candidate_ids == ["supports-example"]
    assert result.qualified_provider_count == 1
    skipped = {s.provider_id: s.reason for s in result.skipped}
    assert skipped == {"supports-key": METHOD_NOT_SUPPORTED}


def test_unavailable_provider_is_excluded():
    inv = inventory_of(
        entry("ok"),
        entry("down", available=False, unavailable_reason="observed HTTP 502"),
    )
    result = select_candidates(inv, "example")
    assert result.candidate_ids == ["ok"]
    skipped = {s.provider_id: (s.reason, s.detail) for s in result.skipped}
    assert skipped["down"][0] == PROVIDER_UNAVAILABLE
    assert "502" in skipped["down"][1]


def test_auth_required_without_credentials_is_excluded():
    inv = inventory_of(
        entry("ok"),
        entry("needs-key", auth_required=True, credentials_available=False),
    )
    result = select_candidates(inv, "example")
    assert result.candidate_ids == ["ok"]
    assert {s.provider_id: s.reason for s in result.skipped} == {
        "needs-key": AUTH_REQUIRED_NO_CREDENTIALS
    }


def test_auth_required_with_credentials_is_eligible():
    inv = inventory_of(entry("has-key", auth_required=True, credentials_available=True))
    assert select_candidates(inv, "example").candidate_ids == ["has-key"]


def test_rate_budget_exhausted_provider_is_excluded():
    """Budget exhaustion is a policy exclusion, never a failure."""
    inv = inventory_of(
        entry("plenty", endpoint="https://plenty.invalid",
              rate_limit_requests=10, rate_limit_window_seconds=1800),
        entry("spent", endpoint="https://spent.invalid",
              rate_limit_requests=2, rate_limit_window_seconds=1800),
    )
    budgets = BudgetRegistry(inv)
    budgets.charge("spent")
    budgets.charge("spent")

    result = select_candidates(inv, "example", budgets=budgets)
    assert result.candidate_ids == ["plenty"]
    skipped = {s.provider_id: (s.reason, s.detail) for s in result.skipped}
    assert skipped["spent"][0] == RATE_BUDGET_EXHAUSTED
    assert "not called" in skipped["spent"][1]


def test_unknown_adapter_is_excluded():
    inv = inventory_of(entry("weird", adapter="no-such-adapter"))
    result = select_candidates(inv, "example", known_adapters={"universal-resolver-v1"})
    assert result.candidate_ids == []
    assert result.skipped[0].reason == UNKNOWN_ADAPTER


def test_local_providers_are_unlimited():
    inv = inventory_of(entry("local", endpoint="http://127.0.0.1:8001"))
    budget = BudgetRegistry(inv).get("local")
    assert budget.unlimited
    for _ in range(50):
        budget.charge()
    assert budget.would_exceed() is False


def test_budget_window_is_rolling():
    budget = ProviderBudget("p", limit=2, window_seconds=100)
    budget.charge(now=0.0)
    budget.charge(now=1.0)
    assert budget.would_exceed(now=2.0) is True
    # Charges older than the window fall out of the count.
    assert budget.would_exceed(now=200.0) is False
    assert budget.state(now=200.0).spent_in_window == 0


# ==========================================================================
# routing service over the local mock resolvers
# ==========================================================================


@pytest.fixture
def local_inventory(healthy_cluster) -> ProviderInventory:
    return inventory_of(
        *[
            entry(
                f"local-{rid[-1]}",
                endpoint=resolver.url,
                implementation_id="avdr-mock-resolver",
            )
            for rid, resolver in healthy_cluster.items()
        ]
    )


@pytest.fixture
def routing_sink(tmp_path) -> TelemetrySink:
    return TelemetrySink(tmp_path / "routing")


@pytest.fixture
async def routing_client(local_inventory, routing_sink):
    app = create_app(
        inventory=local_inventory,
        sink=routing_sink,
        timeout_ms=2000,
        single_static_target="local-a",
        launch_order_seed=None,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            yield client


async def resolve(client, policy, did=DID):
    return await client.post("/resolve", json={"did": did, "policy": policy})


# ------------------------- 5. single-static ------------------------------


async def test_single_static_makes_exactly_one_attempt(routing_client, routing_sink):
    response = await resolve(routing_client, "single-static")
    assert response.status_code == 200
    body = response.json()
    assert body["returned_provider"] == "local-a"
    assert body["attempted_providers"] == ["local-a"]
    assert body["attempt_count"] == 1
    assert body["accepted"] is True
    assert body["acceptance_profile"] == PROFILE_W3C_BASIC_V1
    assert body["did_document"]["id"] == DID


async def test_single_static_has_no_hidden_failover(routing_client, healthy_cluster):
    """The control baseline must fail rather than silently repair itself."""
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    response = await resolve(routing_client, "single-static")
    assert response.status_code == 502
    body = response.json()
    assert body["attempted_providers"] == ["local-a"]
    assert body["attempt_count"] == 1
    assert body["accepted"] is False


# ---------------------- 6-7. sequential failover -------------------------


async def test_sequential_failover_returns_next_acceptable_provider(
    routing_client, healthy_cluster
):
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    response = await resolve(routing_client, "sequential-failover")
    assert response.status_code == 200
    body = response.json()
    assert body["returned_provider"] == "local-b"
    assert body["attempted_providers"] == ["local-a", "local-b"]


async def test_sequential_failover_stops_after_success(
    routing_client, healthy_cluster, routing_sink
):
    """local-c must never be contacted once local-b succeeds."""
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    before = healthy_cluster["resolver-c"].app.state.counter.value

    body = (await resolve(routing_client, "sequential-failover")).json()
    after = healthy_cluster["resolver-c"].app.state.counter.value

    assert body["attempted_providers"] == ["local-a", "local-b"]
    assert "local-c" not in body["attempted_providers"]
    assert after == before, "provider after the winner was contacted"


async def test_sequential_failover_skips_structurally_unacceptable(
    routing_client, healthy_cluster, routing_sink
):
    """HTTP 200 with a bad document is not a win."""
    healthy_cluster["resolver-a"].set_behavior(force_invalid=True)
    body = (await resolve(routing_client, "sequential-failover")).json()

    assert body["returned_provider"] == "local-b"
    trace = routing_sink.get_routing_request(body["request_id"])
    rejected = trace["attempts"][0]
    assert rejected["http_status"] == 200
    assert rejected["accepted"] is False
    assert rejected["acceptance_checks"]["did_document_id_matches_request"] is False


async def test_all_providers_fail_returns_typed_error(routing_client, healthy_cluster):
    for resolver in healthy_cluster.values():
        resolver.set_behavior(force_error=True)
    response = await resolve(routing_client, "sequential-failover")
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "noAcceptableResult"
    assert body["attempt_count"] == 3
    assert len(body["attempt_outcomes"]) == 3


# --------------------------- 8-10. all-race ------------------------------


async def test_all_race_returns_first_acceptable_result(
    routing_client, healthy_cluster
):
    """Scenario A: A fast, B and C slower -> A wins. [CONTROLLED INJECTION]"""
    healthy_cluster["resolver-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)
    healthy_cluster["resolver-c"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)

    response = await resolve(routing_client, "all-race")
    assert response.status_code == 200
    body = response.json()
    assert body["returned_provider"] == "local-a"
    assert body["execution"] == "concurrent"
    assert body["accepted"] is True


async def test_all_race_rejects_faster_invalid_result(
    routing_client, healthy_cluster, routing_sink
):
    """Scenario B: A is fastest but structurally unacceptable -> must NOT win."""
    healthy_cluster["resolver-a"].set_behavior(force_invalid=True)
    healthy_cluster["resolver-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)
    healthy_cluster["resolver-c"].set_behavior(
        artificial_delay_ms=INJECTED_DELAY_MS * 2
    )

    body = (await resolve(routing_client, "all-race")).json()
    assert body["returned_provider"] == "local-b"
    assert body["did_document"]["id"] == DID

    trace = routing_sink.get_routing_request(body["request_id"])
    by_provider = {a["provider_id"]: a for a in trace["attempts"]}
    # The fastest completion was recorded, and lost on acceptance.
    assert by_provider["local-a"]["http_status"] == 200
    assert by_provider["local-a"]["accepted"] is False
    assert by_provider["local-a"]["latency_ms"] < by_provider["local-b"]["latency_ms"]
    assert by_provider["local-b"]["accepted"] is True


async def test_all_race_records_cancellation_state(
    routing_client, healthy_cluster, routing_sink
):
    healthy_cluster["resolver-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS * 4)
    healthy_cluster["resolver-c"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS * 4)

    body = (await resolve(routing_client, "all-race")).json()
    assert body["returned_provider"] == "local-a"
    assert body["canceled_count"] >= 1

    trace = routing_sink.get_routing_request(body["request_id"])
    canceled = [a for a in trace["attempts"] if a["canceled"]]
    assert canceled, "expected outstanding attempts to be canceled"
    for attempt in canceled:
        assert attempt["cancellation_outcome"] in (
            CANCELED_AFTER_DISPATCH,
            CANCELED_BEFORE_DISPATCH,
        )
        # Never claim a canceled request spared the provider if it was sent.
        if attempt["dispatched"]:
            assert attempt["cancellation_outcome"] == CANCELED_AFTER_DISPATCH
        # No fabricated timing for work that never finished.
        assert attempt["latency_ms"] is None
        assert attempt["accepted"] is False

    warnings = {w["code"] for w in body["warnings"]}
    if any(a["dispatched"] for a in canceled):
        assert "CANCELED_AFTER_DISPATCH" in warnings


async def test_all_race_every_provider_launched(routing_client, routing_sink):
    body = (await resolve(routing_client, "all-race")).json()
    trace = routing_sink.get_routing_request(body["request_id"])
    assert len(trace["attempts"]) == 3
    assert sorted(a["provider_id"] for a in trace["attempts"]) == [
        "local-a", "local-b", "local-c",
    ]
    assert sorted(a["launch_position"] for a in trace["attempts"]) == [0, 1, 2]


# ------------------- 11. insufficient providers --------------------------


async def test_all_race_requires_at_least_two_providers(
    healthy_cluster, routing_sink, tmp_path
):
    single = inventory_of(
        entry("only-one", endpoint=healthy_cluster["resolver-a"].url)
    )
    app = create_app(inventory=single, sink=routing_sink, timeout_ms=2000,
                     launch_order_seed=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            response = await client.post(
                "/resolve", json={"did": DID, "policy": "all-race"}
            )
            single_static = await client.post(
                "/resolve", json={"did": DID, "policy": "single-static"}
            )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == INSUFFICIENT_QUALIFIED_PROVIDERS
    assert body["qualified_provider_count"] == 1
    assert body["required_provider_count"] == 2
    # Redundancy is never fabricated, but a single-provider policy still runs.
    assert single_static.status_code == 200


async def test_no_qualified_provider_for_method(routing_client):
    response = await routing_client.post(
        "/resolve", json={"did": "did:key:zAbc", "policy": "single-static"}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == INSUFFICIENT_QUALIFIED_PROVIDERS
    assert body["qualified_provider_count"] == 0
    assert {s["reason"] for s in body["skipped_providers"]} == {METHOD_NOT_SUPPORTED}


async def test_single_provider_emits_warning(healthy_cluster, routing_sink):
    single = inventory_of(entry("only-one", endpoint=healthy_cluster["resolver-a"].url))
    app = create_app(inventory=single, sink=routing_sink, timeout_ms=2000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            body = (
                await client.post(
                    "/resolve", json={"did": DID, "policy": "sequential-failover"}
                )
            ).json()
    assert "SINGLE_QUALIFIED_PROVIDER" in {w["code"] for w in body["warnings"]}


async def test_malformed_did_and_unknown_policy(routing_client):
    bad_did = await routing_client.post("/resolve", json={"did": "nope", "policy": "single-static"})
    assert bad_did.status_code == 400
    assert bad_did.json()["error"] == "invalidDid"

    bad_policy = await resolve(routing_client, "adaptive-k")
    assert bad_policy.status_code == 400
    assert bad_policy.json()["error"] == "unknownPolicy"


# ------------------- 12. telemetry reconciliation ------------------------


async def test_logical_and_attempt_telemetry_reconcile(
    routing_client, healthy_cluster, routing_sink, tmp_path
):
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    body = (await resolve(routing_client, "sequential-failover")).json()

    trace = routing_sink.get_routing_request(body["request_id"])
    record, attempts = trace["request"], trace["attempts"]

    assert record["attempt_count"] == len(attempts)
    assert record["attempted_providers"] == [a["provider_id"] for a in attempts]
    assert record["returned_provider"] == "local-b"
    assert record["acceptance_profile"] == PROFILE_W3C_BASIC_V1
    assert record["qualified_provider_count"] == 3
    # Skipped providers were never attempted: the two sets must not overlap.
    skipped_ids = {s["provider_id"] for s in record["skipped_providers"]}
    assert skipped_ids.isdisjoint(set(record["attempted_providers"]))

    # Written to two separate JSONL files, joined on request_id.
    def read(path):
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    requests = read(routing_sink.routing_requests_path)
    attempt_rows = read(routing_sink.routing_attempts_path)
    mine = [r for r in requests if r["request_id"] == body["request_id"]]
    linked = [a for a in attempt_rows if a["request_id"] == body["request_id"]]
    assert len(mine) == 1
    assert len(linked) == len(attempts)
    assert mine[0]["record_type"] == "real_routing_request"
    assert all(a["record_type"] == "real_routing_attempt" for a in linked)


async def test_provenance_is_bound_to_routing_records(routing_client, routing_sink):
    body = (await resolve(routing_client, "single-static")).json()
    record = routing_sink.get_routing_request(body["request_id"])["request"]
    assert record["provider_inventory_hash"]
    assert record["config_hash"]
    assert record["phase"] == "real-routing-service"
    assert record["experiment_id"]
    # git_commit may be a real SHA or explicitly null; never fabricated.
    assert record["git_commit"] is None or len(record["git_commit"]) == 40


# ------------------- 13. provider status endpoint ------------------------


async def test_provider_status_endpoint_shape(routing_client):
    response = await routing_client.get("/providers")
    assert response.status_code == 200
    body = response.json()
    assert body["provider_inventory_hash"].startswith("sha256:")
    row = body["providers"][0]
    for field in (
        "provider_id", "implementation_id", "supported_did_methods",
        "configured", "available", "auth_required", "auth_available",
        "rate_budget", "exclusion_reason",
    ):
        assert field in row
    assert set(row["rate_budget"]) == {
        "limit", "window_seconds", "spent_in_window", "remaining", "exhausted",
    }


async def test_provider_status_exposes_no_secrets(routing_client):
    response = await routing_client.get("/providers")
    text = response.text.lower()
    for forbidden in ("authorization", "bearer", "api_key", "apikey", "token", "secret", "password"):
        assert forbidden not in text
    # auth availability is a boolean, never the credential.
    for row in response.json()["providers"]:
        assert isinstance(row["auth_available"], bool)
        assert "auth_scheme" not in row
        assert "credentials" not in row


async def test_health_and_policies_endpoints(routing_client):
    health = (await routing_client.get("/health")).json()
    assert health["status"] == "ok"
    assert health["acceptance_profile"] == PROFILE_W3C_BASIC_V1

    policies = (await routing_client.get("/policies")).json()
    assert sorted(policies["available"]) == ["all-race", "sequential-failover", "single-static"]
    assert policies["minimum_providers"]["all-race"] == 2
    assert policies["minimum_providers"]["single-static"] == 1


# ------------------- 14. network guard is real ---------------------------


def test_public_network_is_blocked_in_tests():
    """The suite must be structurally unable to spend the public budget."""
    from conftest import PublicNetworkBlocked

    with pytest.raises(PublicNetworkBlocked):
        httpx.get("https://dev.uniresolver.io/1.0/identifiers/did:key:zAbc", timeout=5)


def test_loopback_is_still_permitted(healthy_cluster):
    response = httpx.get(f"{healthy_cluster['resolver-a'].url}/health", timeout=5)
    assert response.status_code == 200


# ------------------- policy unit checks ----------------------------------


def test_single_static_rejects_unqualified_target():
    policy = RealSingleStatic(target="not-a-candidate")
    with pytest.raises(KeyError, match="not a qualified candidate"):
        policy.plan([entry("local-a")], DID)


def test_all_race_launch_order_rotation_is_seeded():
    policy = RealAllRace(launch_order_seed=42)
    providers = [entry("a"), entry("b"), entry("c")]
    orders = {tuple(policy.plan(providers, DID).provider_order) for _ in range(12)}
    assert len(orders) > 1

    policy.reset()
    first = policy.plan(providers, DID).provider_order
    policy.reset()
    assert policy.plan(providers, DID).provider_order == first


def test_policy_execution_modes():
    assert RealSingleStatic().plan([entry("a")], DID).execution == "sequential"
    assert RealAllRace().plan([entry("a"), entry("b")], DID).execution == "concurrent"
