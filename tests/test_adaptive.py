"""Adaptive minimum-set decision engine tests.

All q_hat values below are [CONTROLLED TEST INPUT]: numbers chosen so the
optimizer has a known-correct answer. None of them is a measurement, a
prediction, or a claim about any DID resolver.

Layers are tested separately (estimator / optimizer) and then together
through the routing service against LOCAL loopback providers. Zero
public-network calls -- the session socket guard enforces it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.adaptive.estimator import (
    ControlledTableEstimator,
    EstimatorError,
    SubsetEstimator,
    subset_key,
    validate_probability,
)
from avdr.adaptive.optimizer import (
    ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED,
    NO_ELIGIBLE_CANDIDATES,
    OPTIMIZER_VERSION,
    SELECTED,
    SLO_ESTIMATE_UNSATISFIABLE,
    CardinalityCost,
    MinimumSetOptimizer,
    WeightedCost,
    validate_target,
)
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.models import RealRoutingAttempt
from avdr.probe import CANCELED_AFTER_DISPATCH, CANCELED_BEFORE_DISPATCH
from avdr.real_router.adaptive_policy import AdaptivePlanningError, RealAdaptiveMinSet
from avdr.real_router.app import create_app
from avdr.telemetry import TelemetrySink

from conftest import INJECTED_DELAY_MS

DID = "did:example:adaptive-subject"
ABC = ["local-a", "local-b", "local-c"]


# ==========================================================================
# 1-2. dispatched / launch-offset telemetry invariant
# ==========================================================================


def _attempt(**kw) -> RealRoutingAttempt:
    base = dict(
        request_id="r1",
        launch_position=0,
        provider_id="local-a",
        resolver_endpoint_id="http://127.0.0.1:8001",
        transport_outcome="http_response",
        acceptance_profile="w3c-basic-v1",
    )
    base.update(kw)
    return RealRoutingAttempt(**base)


def test_dispatched_completed_attempt_has_launch_offset_and_latency():
    attempt = _attempt(dispatched=True, launch_offset_ms=0.5, latency_ms=12.0)
    assert attempt.launch_offset_ms == 0.5
    assert attempt.latency_ms == 12.0
    assert attempt.telemetry_error is None


def test_dispatched_canceled_attempt_keeps_launch_offset_with_null_latency():
    """The value that matters: a canceled dispatched attempt was still launched."""
    attempt = _attempt(
        dispatched=True,
        canceled=True,
        cancellation_outcome=CANCELED_AFTER_DISPATCH,
        launch_offset_ms=1.25,
        latency_ms=None,
        transport_outcome="canceled",
    )
    assert attempt.launch_offset_ms == 1.25
    assert attempt.latency_ms is None


def test_never_dispatched_attempt_has_null_launch_offset_and_latency():
    attempt = _attempt(
        dispatched=False,
        canceled=True,
        cancellation_outcome=CANCELED_BEFORE_DISPATCH,
        launch_offset_ms=None,
        latency_ms=None,
        transport_outcome="canceled",
    )
    assert attempt.launch_offset_ms is None
    assert attempt.latency_ms is None


def test_dispatched_without_launch_offset_is_rejected():
    """The invariant is enforced, not merely documented."""
    with pytest.raises(ValueError, match="launch timing must be preserved"):
        _attempt(dispatched=True, launch_offset_ms=None)


def test_dispatched_without_launch_offset_allowed_only_with_explicit_error():
    attempt = _attempt(
        dispatched=True,
        launch_offset_ms=None,
        telemetry_error="clock failure during dispatch",
    )
    assert attempt.launch_offset_ms is None
    assert attempt.telemetry_error


# ==========================================================================
# 3-4. estimator interface; no independence formula
# ==========================================================================


def test_estimator_operates_on_subsets():
    est = ControlledTableEstimator({("a",): 0.9, ("a", "b"): 0.97})
    assert est.estimate(("a",)) == 0.9
    # Order-insensitive subset identity.
    assert est.estimate(("b", "a")) == 0.97
    assert est.estimate(subset_key(["a", "b"])) == 0.97


def test_uncovered_subset_returns_none_not_a_composed_value():
    """No independence composition may be substituted for a missing estimate."""
    est = ControlledTableEstimator({("a",): 0.9, ("b",): 0.8})
    assert est.estimate(("a", "b")) is None
    # If independence were applied we would see 1-(0.1*0.2)=0.98.
    assert est.estimate(("a", "b")) != pytest.approx(0.98)


def test_optimizer_does_not_synthesise_missing_subsets():
    est = ControlledTableEstimator({("a",): 0.5, ("b",): 0.5})
    result = MinimumSetOptimizer().select(["a", "b"], est, 0.9)
    assert result.status == SLO_ESTIMATE_UNSATISFIABLE
    # {a,b} was evaluated but had no estimate; it was NOT composed to 0.75.
    assert result.unestimated_subset_count == 1
    assert result.evaluated_subset_count == 3
    assert result.best_probability == 0.5


def test_independence_formula_absent_from_source():
    """Guard against an independence model creeping in later.

    A behavioural check would not catch a composition rule that happens to
    agree with the table, so this inspects the source for the machinery such a
    rule needs: a product accumulator over per-provider terms.
    """
    source = Path(__file__).resolve().parent.parent / "src" / "avdr" / "adaptive"
    files = list(source.glob("*.py"))
    assert files, "adaptive package source not found"

    banned = ("math.prod", "np.prod", "numpy.prod", "reduce(mul", "operator.mul")
    for path in files:
        text = path.read_text(encoding="utf-8")
        # Ignore the docstrings that explicitly forbid the formula.
        code = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        for token in banned:
            assert token not in code, f"{path.name} contains {token!r}"


def test_optimizer_uses_only_table_values_for_composite_subsets():
    """Behavioural proof: a pair's q_hat comes from the table, not from its parts.

    The table below is deliberately anti-independent -- the pair scores LOWER
    than either member. An independence composition could not produce this,
    so observing it proves the table is consulted directly.
    """
    est = ControlledTableEstimator(
        {("a",): 0.90, ("b",): 0.90, ("a", "b"): 0.40}
    )
    result = MinimumSetOptimizer().select(["a", "b"], est, 0.85)
    pair = next(e for e in result.evaluations if e.subset == ("a", "b"))
    assert pair.q_hat == 0.40
    assert pair.feasible is False
    # Independence would have given 1-(0.1*0.1)=0.99 and selected the pair.
    assert result.selected_subset == ["a"]


def test_estimator_describe_and_config_hash_are_stable():
    a = ControlledTableEstimator({("a",): 0.9, ("b",): 0.8}, label="x")
    b = ControlledTableEstimator({("b",): 0.8, ("a",): 0.9}, label="x")
    assert a.config_hash() == b.config_hash()
    c = ControlledTableEstimator({("a",): 0.91, ("b",): 0.8}, label="x")
    assert a.config_hash() != c.config_hash()
    described = a.describe()
    assert described["estimator_id"] == "controlled-table"
    assert described["estimator_version"] == "v1"
    assert "CONTROLLED TEST INPUT" in described["note"]


# ==========================================================================
# 5-8. K1 / K2 / K3 / KU controlled optimizer fixtures
# ==========================================================================

# [CONTROLLED TEST INPUT] -- chosen so the correct answer is known in advance.
K_TABLE = {
    ("local-a",): 0.99,
    ("local-b",): 0.96,
    ("local-c",): 0.90,
    ("local-a", "local-b"): 0.995,
    ("local-a", "local-c"): 0.993,
    ("local-b", "local-c"): 0.991,
    ("local-a", "local-b", "local-c"): 0.9995,
}


@pytest.fixture
def k_estimator() -> ControlledTableEstimator:
    return ControlledTableEstimator(K_TABLE, label="K-fixture")


def test_k1_selects_single_provider(k_estimator):
    """target 0.95; q({A})=0.99 -> k*=1."""
    result = MinimumSetOptimizer().select(ABC, k_estimator, 0.95)
    assert result.status == SELECTED
    assert result.selected_subset == ["local-a"]
    assert result.selected_subset_size == 1
    assert result.selected_cost == 1.0
    assert result.estimated_subset_success == 0.99
    assert result.best_effort is False


def test_k2_selects_two_providers(k_estimator):
    """target 0.99 with strict '>=' met by {A} exactly; use 0.992 to force k=2."""
    result = MinimumSetOptimizer().select(ABC, k_estimator, 0.992)
    assert result.status == SELECTED
    assert result.selected_subset_size == 2
    # All singles are below 0.992; the cheapest feasible pair wins.
    assert result.selected_subset == ["local-a", "local-b"]
    assert result.estimated_subset_success == 0.995


def test_k3_selects_three_providers(k_estimator):
    """target 0.999; only the full set reaches it -> k*=3."""
    result = MinimumSetOptimizer().select(ABC, k_estimator, 0.999)
    assert result.status == SELECTED
    assert result.selected_subset_size == 3
    assert result.selected_subset == ABC
    assert result.estimated_subset_success == 0.9995


def test_ku_unsatisfiable_target(k_estimator):
    """No subset reaches the target -> explicit planning state, not a silent all-call."""
    result = MinimumSetOptimizer().select(ABC, k_estimator, 0.99999)
    assert result.status == SLO_ESTIMATE_UNSATISFIABLE
    assert result.selected_subset is None
    assert result.best_subset == ABC
    assert result.best_probability == 0.9995
    assert result.candidate_count == 3
    assert "no subset reached the target" in result.selection_reason


def test_best_effort_degradation_is_tagged_and_does_not_claim_the_slo(k_estimator):
    result = MinimumSetOptimizer().select(
        ABC, k_estimator, 0.99999, allow_best_effort=True
    )
    assert result.status == SELECTED
    assert result.best_effort is True
    assert result.satisfied is False, "best-effort must never count as satisfied"
    assert result.selected_subset == ABC
    assert "does NOT satisfy the target" in result.selection_reason


# ==========================================================================
# 9. deterministic tie breaking
# ==========================================================================


def test_tie_break_prefers_higher_q_hat_at_equal_cost():
    est = ControlledTableEstimator({("a",): 0.97, ("b",): 0.99, ("c",): 0.98})
    result = MinimumSetOptimizer().select(["a", "b", "c"], est, 0.95)
    assert result.selected_subset == ["b"]


def test_tie_break_is_lexicographic_when_cost_and_q_hat_tie():
    est = ControlledTableEstimator({("a",): 0.99, ("b",): 0.99, ("c",): 0.99})
    result = MinimumSetOptimizer().select(["c", "b", "a"], est, 0.95)
    assert result.selected_subset == ["a"]
    assert "tied on cost and q_hat" in result.selection_reason


def test_selection_is_repeatable():
    est = ControlledTableEstimator({("a",): 0.99, ("b",): 0.99})
    optimizer = MinimumSetOptimizer()
    picks = {
        tuple(optimizer.select(["a", "b"], est, 0.9).selected_subset)
        for _ in range(25)
    }
    assert picks == {("a",)}


def test_cost_model_is_pluggable():
    est = ControlledTableEstimator({("a",): 0.99, ("b",): 0.99})
    weighted = MinimumSetOptimizer(cost_model=WeightedCost({"a": 5.0, "b": 1.0}))
    result = weighted.select(["a", "b"], est, 0.9)
    # Cardinality would pick "a" lexicographically; weights pick "b".
    assert result.selected_subset == ["b"]
    assert result.cost_model_id == "weighted-v1"


# ==========================================================================
# 10-11. validation
# ==========================================================================


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, float("nan"), float("inf"), -float("inf")])
def test_invalid_target_probability_is_rejected(bad):
    with pytest.raises((ValueError, EstimatorError)):
        validate_target(bad)


def test_valid_target_boundaries():
    assert validate_target(1.0) == 1.0
    assert validate_target(0.5) == 0.5


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf"), "0.9", None, True])
def test_invalid_estimator_probability_is_rejected(bad):
    with pytest.raises(EstimatorError):
        validate_probability(bad, "q_hat")


def test_estimator_rejects_invalid_table_values():
    with pytest.raises(EstimatorError):
        ControlledTableEstimator({("a",): 1.2})
    with pytest.raises(EstimatorError):
        ControlledTableEstimator({("a",): float("nan")})
    with pytest.raises(EstimatorError, match="empty subset"):
        ControlledTableEstimator({(): 0.5})


def test_impossible_values_are_never_clamped():
    """A 1.2 must raise, not silently become 1.0."""
    with pytest.raises(EstimatorError, match=r"outside \[0, 1\]"):
        validate_probability(1.2, "q_hat")


def test_optimizer_rejects_invalid_estimator_output():
    class Rogue(SubsetEstimator):
        estimator_id, estimator_version = "rogue", "v0"

        def estimate(self, subset, context=None):
            return 1.5

        def config_hash(self):
            return "sha256:rogue"

    with pytest.raises(EstimatorError):
        MinimumSetOptimizer().select(["a"], Rogue(), 0.9)


# ==========================================================================
# 12-13. enumeration count and safety bound
# ==========================================================================


@pytest.mark.parametrize("m", [1, 2, 3, 4, 5])
def test_evaluated_subset_count_is_exactly_two_pow_m_minus_one(m):
    providers = [f"p{i}" for i in range(m)]
    table = {}
    for size in range(1, m + 1):
        from itertools import combinations

        for combo in combinations(sorted(providers), size):
            table[combo] = 0.5
    est = ControlledTableEstimator(table)
    result = MinimumSetOptimizer().select(providers, est, 0.99)
    assert result.evaluated_subset_count == 2**m - 1
    assert result.evaluated_subset_count <= 2 ** result.candidate_count - 1


def test_candidate_limit_is_enforced_not_truncated():
    providers = [f"p{i}" for i in range(13)]
    est = ControlledTableEstimator({("p0",): 0.99})
    result = MinimumSetOptimizer(max_candidates=12).select(providers, est, 0.9)
    assert result.status == ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED
    assert result.selected_subset is None
    assert result.candidate_count == 13
    # The provider set is reported in full, not silently shortened.
    assert len(result.candidate_providers) == 13
    assert "refusing rather than" in result.selection_reason


def test_no_eligible_candidates_is_typed():
    est = ControlledTableEstimator({("a",): 0.99})
    result = MinimumSetOptimizer().select([], est, 0.9)
    assert result.status == NO_ELIGIBLE_CANDIDATES
    assert result.evaluated_subset_count == 0


def test_optimizer_version_recorded():
    est = ControlledTableEstimator({("a",): 0.99})
    assert MinimumSetOptimizer().select(["a"], est, 0.9).optimizer_version == OPTIMIZER_VERSION


# ==========================================================================
# 14-18. adaptive policy through the routing service (local providers only)
# ==========================================================================


def local_entry(pid, url):
    return ProviderEntry(
        id=pid,
        endpoint=url,
        adapter="universal-resolver-v1",
        supported_did_methods=["example"],
        implementation_id="avdr-mock-resolver",
    )


@pytest.fixture
def local_inventory(healthy_cluster) -> ProviderInventory:
    mapping = dict(zip(ABC, healthy_cluster.values()))
    return ProviderInventory(
        inventory_version="test-local",
        providers=[local_entry(pid, r.url) for pid, r in mapping.items()],
    )


@pytest.fixture
def adaptive_sink(tmp_path) -> TelemetrySink:
    return TelemetrySink(tmp_path / "adaptive")


def make_client(inventory, sink, table, target=0.95, best_effort=False):
    app = create_app(
        inventory=inventory,
        sink=sink,
        timeout_ms=3000,
        launch_order_seed=None,
        estimator=ControlledTableEstimator(table, label="test"),
        default_target_slo=target,
        allow_best_effort=best_effort,
    )
    return app


async def adaptive_resolve(app, target=None, did=DID):
    payload = {"did": did, "policy": "adaptive-min-set"}
    if target is not None:
        payload["target_slo_probability"] = target
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            response = await c.post("/resolve", json=payload)
    return response


async def test_adaptive_attempts_only_the_selected_subset(
    local_inventory, adaptive_sink, healthy_cluster
):
    """k*=1 -> exactly one provider is contacted; the others are never called."""
    before = {
        pid: r.app.state.counter.value
        for pid, r in zip(ABC, healthy_cluster.values())
    }
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    response = await adaptive_resolve(app, target=0.95)
    assert response.status_code == 200
    body = response.json()

    assert body["adaptive_plan"]["selected_subset"] == ["local-a"]
    assert body["attempted_providers"] == ["local-a"]
    assert body["candidate_providers"] == ABC

    after = {
        pid: r.app.state.counter.value
        for pid, r in zip(ABC, healthy_cluster.values())
    }
    assert after["local-b"] == before["local-b"], "non-selected provider was called"
    assert after["local-c"] == before["local-c"], "non-selected provider was called"


async def test_attempted_is_subset_of_selected(local_inventory, adaptive_sink):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    body = (await adaptive_resolve(app, target=0.999)).json()
    selected = set(body["adaptive_plan"]["selected_subset"])
    attempted = set(body["attempted_providers"])
    assert attempted <= selected
    assert attempted == selected  # normal completion


async def test_adaptive_k2_and_k3_execute_expected_subset_sizes(
    local_inventory, adaptive_sink
):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    k2 = (await adaptive_resolve(app, target=0.992)).json()
    assert k2["adaptive_plan"]["selected_subset_size"] == 2
    assert set(k2["attempted_providers"]) == {"local-a", "local-b"}

    k3 = (await adaptive_resolve(app, target=0.999)).json()
    assert k3["adaptive_plan"]["selected_subset_size"] == 3
    assert set(k3["attempted_providers"]) == set(ABC)


async def test_adaptive_unsatisfiable_returns_typed_condition(
    local_inventory, adaptive_sink
):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    response = await adaptive_resolve(app, target=0.99999)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == SLO_ESTIMATE_UNSATISFIABLE
    assert body["target_slo_probability"] == 0.99999
    assert body["best_subset"] == ABC
    assert body["best_probability"] == 0.9995
    assert body["candidate_count"] == 3
    assert body["selected_subset"] is None


async def test_adaptive_first_invalid_selected_provider_cannot_win(
    local_inventory, adaptive_sink, healthy_cluster
):
    """Adaptive changes WHICH providers are called, not acceptance semantics."""
    resolvers = dict(zip(ABC, healthy_cluster.values()))
    resolvers["local-a"].set_behavior(force_invalid=True)
    resolvers["local-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)

    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    body = (await adaptive_resolve(app, target=0.992)).json()

    assert body["adaptive_plan"]["selected_subset"] == ["local-a", "local-b"]
    # The fastest selected provider was unacceptable and did not win.
    assert body["returned_provider"] == "local-b"
    assert body["did_document"]["id"] == DID

    trace = adaptive_sink.get_routing_request(body["request_id"])
    by_provider = {a["provider_id"]: a for a in trace["attempts"]}
    assert by_provider["local-a"]["http_status"] == 200
    assert by_provider["local-a"]["accepted"] is False
    assert by_provider["local-a"]["latency_ms"] < by_provider["local-b"]["latency_ms"]


async def test_adaptive_selected_valid_provider_can_win(local_inventory, adaptive_sink):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    body = (await adaptive_resolve(app, target=0.95)).json()
    assert body["accepted"] is True
    assert body["returned_provider"] == "local-a"
    assert body["acceptance_profile"] == "w3c-basic-v1"


async def test_adaptive_all_selected_fail_returns_no_acceptable_result(
    local_inventory, adaptive_sink, healthy_cluster
):
    for resolver in healthy_cluster.values():
        resolver.set_behavior(force_error=True)
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    response = await adaptive_resolve(app, target=0.999)
    assert response.status_code == 502
    assert response.json()["error"] == "noAcceptableResult"


async def test_adaptive_telemetry_reconciles_and_records_decision(
    local_inventory, adaptive_sink
):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    body = (await adaptive_resolve(app, target=0.992)).json()
    trace = adaptive_sink.get_routing_request(body["request_id"])
    record, attempts = trace["request"], trace["attempts"]

    assert record["attempt_count"] == len(attempts)
    assert record["attempted_providers"] == [a["provider_id"] for a in attempts]
    assert set(record["attempted_providers"]) <= set(record["selected_subset"])

    assert record["routing_policy"] == "adaptive-min-set"
    assert record["target_slo_probability"] == 0.992
    assert record["estimator_id"] == "controlled-table"
    assert record["estimator_version"] == "v1"
    assert record["estimator_config_hash"].startswith("sha256:")
    assert record["evaluated_subset_count"] == 7
    assert record["selected_subset"] == ["local-a", "local-b"]
    assert record["selected_subset_size"] == 2
    assert record["estimated_subset_success"] == 0.995
    assert record["selection_cost"] == 2.0
    assert record["selection_status"] == SELECTED
    assert record["optimizer_version"] == OPTIMIZER_VERSION
    assert record["cost_model_id"] == "cardinality-v1"
    assert record["best_effort"] is False


async def test_adaptive_provenance_survives_jsonl(local_inventory, adaptive_sink):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    body = (await adaptive_resolve(app, target=0.95)).json()

    rows = [
        json.loads(line)
        for line in adaptive_sink.routing_requests_path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    row = next(r for r in rows if r["request_id"] == body["request_id"])
    for field in (
        "provider_inventory_hash", "acceptance_profile", "routing_policy",
        "estimator_id", "estimator_version", "estimator_config_hash",
        "target_slo_probability", "optimizer_version", "selection_status",
    ):
        assert row[field] is not None, field
    assert row["git_commit"] is None or len(row["git_commit"]) == 40
    assert isinstance(row["git_dirty"], bool) or row["git_dirty"] is None


async def test_adaptive_rejects_invalid_target_via_api(local_inventory, adaptive_sink):
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    for bad in (0, -0.5, 1.5):
        response = await adaptive_resolve(app, target=bad)
        assert response.status_code == 422, bad
        assert response.json()["error"] == "invalidTargetSloProbability"


async def test_baselines_remain_available_alongside_adaptive(
    local_inventory, adaptive_sink
):
    """Adaptive is added, never a replacement."""
    app = make_client(local_inventory, adaptive_sink, K_TABLE)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
        policies = (await c.get("/policies")).json()
    assert sorted(policies["available"]) == [
        "adaptive-min-set", "all-race", "sequential-failover", "single-static",
    ]
    assert policies["adaptive"]["estimator_id"] == "controlled-table"
    assert policies["adaptive"]["optimizer_version"] == OPTIMIZER_VERSION
    assert policies["adaptive"]["max_adaptive_candidates"] == 12


async def test_adaptive_policy_absent_without_configured_estimator(
    local_inventory, adaptive_sink
):
    app = create_app(inventory=local_inventory, sink=adaptive_sink, timeout_ms=2000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
        policies = (await c.get("/policies")).json()
        response = await c.post(
            "/resolve", json={"did": DID, "policy": "adaptive-min-set"}
        )
    assert "adaptive-min-set" not in policies["available"]
    assert policies["adaptive"] is None
    assert response.status_code == 400


def test_adaptive_policy_raises_typed_planning_error():
    est = ControlledTableEstimator({("local-a",): 0.5})
    policy = RealAdaptiveMinSet(estimator=est, default_target=0.99)
    with pytest.raises(AdaptivePlanningError) as excinfo:
        policy.plan([local_entry("local-a", "http://127.0.0.1:1")], DID)
    assert excinfo.value.status == SLO_ESTIMATE_UNSATISFIABLE
    assert excinfo.value.result.best_probability == 0.5
