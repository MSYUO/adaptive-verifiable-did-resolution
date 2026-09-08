"""Typed estimator packaging, readiness, and evidence-history isolation."""

from __future__ import annotations

import hashlib
from itertools import combinations
from pathlib import Path

import httpx
import pytest

from avdr.adaptive.estimator import subset_key
from avdr.adaptive.optimizer import MinimumSetOptimizer
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.learning.estimators import HISTORY_KEY, load_frozen
from avdr.real_router.adaptive_policy import (
    AdaptivePlanningError,
    RealAdaptiveMinSet,
)
from avdr.real_router.app import create_app
from avdr.real_router.runtime_adaptive import (
    DEFAULT_SPEC_PATH,
    EXPECTED_PACKAGED_SPEC_SHA256,
    EXPECTED_SOURCE_PICKLE_SHA256,
    LEGACY_ESTIMATOR_PATH,
    load_frozen_adaptive_runtime,
)
from avdr.telemetry import TelemetrySink

DID = "did:example:packaged-runtime"
PROVIDER_IDS = ("local-a", "local-b", "local-c")
SUBSETS = tuple(
    subset_key(combo)
    for size in range(1, len(PROVIDER_IDS) + 1)
    for combo in combinations(PROVIDER_IDS, size)
)


def _entry(provider_id: str, endpoint: str) -> ProviderEntry:
    return ProviderEntry(
        id=provider_id,
        implementation_id="avdr-test-resolver",
        operator="controlled test transport",
        endpoint=endpoint,
        adapter="universal-resolver-v1",
        supported_did_methods=["example"],
        available=True,
    )


def _inventory(healthy_cluster, *, evidence_mode: str) -> ProviderInventory:
    version = (
        "local-packaging-test"
        if evidence_mode == "controlled_demo"
        else "real-mode-loopback-test"
    )
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
        transport=httpx.ASGITransport(app=app),
        base_url="http://service",
    )
    outbound = httpx.AsyncClient()
    app.state.client = outbound
    return service, outbound


def _legacy_estimator():
    payload = LEGACY_ESTIMATOR_PATH.read_bytes()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert digest == EXPECTED_SOURCE_PICKLE_SHA256
    estimator, metadata, loaded_digest = load_frozen(LEGACY_ESTIMATOR_PATH)
    assert loaded_digest == EXPECTED_SOURCE_PICKLE_SHA256
    assert metadata["estimator_config_hash"] == estimator.config_hash()
    return estimator


def _histories() -> dict[str, dict]:
    return {
        "empty": {},
        "one_observation": {("local-a",): [1]},
        "multiple_observations": {("local-a",): [1, 0, 1, 1]},
        "window_boundary": {key: [index % 2 for index in range(10)] for key in SUBSETS},
        "beyond_window": {key: [index % 2 for index in range(13)] for key in SUBSETS},
        "success_only": {key: [1] * 6 for key in SUBSETS},
        "failure_only": {key: [0] * 6 for key in SUBSETS},
        "mixed": {key: [1, 0, 1, 0, 1, 1] for key in SUBSETS},
        # Missing exact-subset histories are UNKNOWN observations, but the
        # frozen rolling estimator still has its explicit training prior.
        "partial_unknown": {("local-a",): [1, 0, 1]},
        "coverage_complete": {key: [1, 1, 0] for key in SUBSETS},
    }


def _policy_outcome(policy, candidates, context):
    try:
        plan = policy.plan(candidates, DID, context=context)
    except AdaptivePlanningError as exc:
        return "error", type(exc).__name__, exc.result.summary()
    return "plan", plan.attempt_order(), plan.metadata


def test_packaged_spec_identity_and_typed_reconstruction():
    runtime = load_frozen_adaptive_runtime()
    digest = "sha256:" + hashlib.sha256(DEFAULT_SPEC_PATH.read_bytes()).hexdigest()

    assert digest == EXPECTED_PACKAGED_SPEC_SHA256
    assert runtime.packaged_spec_sha256 == EXPECTED_PACKAGED_SPEC_SHA256
    assert runtime.artifact_sha256 == EXPECTED_SOURCE_PICKLE_SHA256
    assert runtime.estimator_class.endswith(".RollingEmpiricalEstimator")
    assert vars(runtime.estimator) == {
        "window": 10,
        "prior": 0.6879699248120301,
        "prior_weight": 2.0,
    }


def test_tampered_packaged_spec_fails_closed(tmp_path):
    tampered = tmp_path / DEFAULT_SPEC_PATH.name
    payload = DEFAULT_SPEC_PATH.read_bytes().replace(b'"window": 10', b'"window": 11')
    assert payload != DEFAULT_SPEC_PATH.read_bytes()
    tampered.write_bytes(payload)

    with pytest.raises(ValueError, match="specification hash mismatch"):
        load_frozen_adaptive_runtime(tampered)


def test_serving_loader_does_not_read_legacy_pickle(monkeypatch):
    original_read_bytes = Path.read_bytes
    legacy = LEGACY_ESTIMATOR_PATH.resolve()

    def guarded_read_bytes(path):
        if path.resolve() == legacy:
            raise AssertionError("serving loader attempted to read legacy pickle")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    runtime = load_frozen_adaptive_runtime()
    assert runtime.estimator.config_hash().startswith("sha256:")


@pytest.mark.parametrize("case_name,history", _histories().items())
def test_typed_spec_matches_pickle_q_hat_optimizer_and_policy(case_name, history):
    original = _legacy_estimator()
    reconstructed = load_frozen_adaptive_runtime().estimator
    context = {HISTORY_KEY: history}

    for subset in SUBSETS:
        assert reconstructed.estimate(subset, context) == original.estimate(
            subset, context
        ), case_name

    optimizer = MinimumSetOptimizer()
    original_result = optimizer.select(PROVIDER_IDS, original, 0.9, context)
    reconstructed_result = optimizer.select(
        PROVIDER_IDS,
        reconstructed,
        0.9,
        context,
    )
    assert reconstructed_result.summary() == original_result.summary(), case_name

    candidates = [_entry(provider, "http://127.0.0.1:9") for provider in PROVIDER_IDS]
    assert _policy_outcome(
        RealAdaptiveMinSet(reconstructed, default_target=0.9),
        candidates,
        context,
    ) == _policy_outcome(
        RealAdaptiveMinSet(original, default_target=0.9),
        candidates,
        context,
    )


def test_typed_spec_matches_pickle_error_type():
    context = {HISTORY_KEY: {("local-a",): ["not-a-binary-outcome"]}}
    original = _legacy_estimator()
    reconstructed = load_frozen_adaptive_runtime().estimator

    with pytest.raises(Exception) as original_error:
        original.estimate(("local-a",), context)
    with pytest.raises(type(original_error.value)):
        reconstructed.estimate(("local-a",), context)


def test_partial_history_keeps_identical_complete_prior_coverage():
    context = {HISTORY_KEY: _histories()["partial_unknown"]}
    original = MinimumSetOptimizer().select(PROVIDER_IDS, _legacy_estimator(), 0.9, context)
    packaged = MinimumSetOptimizer().select(
        PROVIDER_IDS,
        load_frozen_adaptive_runtime().estimator,
        0.9,
        context,
    )

    assert packaged.summary() == original.summary()
    assert packaged.estimated_subset_count == 7
    assert packaged.unestimated_subset_count == 0
    assert packaged.exact is True


@pytest.mark.asyncio
async def test_fresh_runtime_is_available_not_ready_and_has_no_fake_warmup(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    app = create_app(
        inventory=_inventory(healthy_cluster, evidence_mode="real"),
        sink=TelemetrySink(tmp_path / "telemetry"),
        adaptive_runtime=runtime,
        single_static_target="local-a",
    )
    service, outbound = await _clients(app)
    before = {
        name: resolver.app.state.counter.value
        for name, resolver in healthy_cluster.items()
    }
    async with service, outbound:
        health = (await service.get("/health")).json()
        policies = (await service.get("/policies")).json()
        abstention = await service.post(
            "/resolve",
            json={"did": DID, "strategy": "adaptive"},
        )
    after = {
        name: resolver.app.state.counter.value
        for name, resolver in healthy_cluster.items()
    }

    status = health["adaptive_runtime"]
    assert status["adaptive_available"] is True
    assert status["adaptive_ready"] is False
    assert status["reason"] == "insufficient_observed_history"
    assert status["by_did_method"]["example"]["adaptive_ready"] is False
    assert policies["adaptive"]["runtime"]["adaptive_ready"] is False
    assert abstention.status_code == 409
    assert before == after
    assert runtime.history.snapshot().version == 0


@pytest.mark.asyncio
async def test_evidence_modes_are_isolated_and_real_observations_drive_readiness(
    healthy_cluster, tmp_path
):
    runtime = load_frozen_adaptive_runtime()
    controlled_app = create_app(
        inventory=_inventory(healthy_cluster, evidence_mode="controlled_demo"),
        sink=TelemetrySink(tmp_path / "controlled-telemetry"),
        adaptive_runtime=runtime,
        single_static_target="local-a",
    )
    controlled, controlled_outbound = await _clients(controlled_app)
    async with controlled, controlled_outbound:
        response = await controlled.post(
            "/resolve",
            json={"did": DID, "strategy": "single"},
        )
    assert response.status_code == 200
    assert runtime.history.snapshot().version == 0
    assert runtime.controlled_demo_history.snapshot().version == 1
    assert runtime.controlled_demo_history.records()[0].evidence_mode == "controlled_demo"

    real_app = create_app(
        inventory=_inventory(healthy_cluster, evidence_mode="real"),
        sink=TelemetrySink(tmp_path / "real-telemetry"),
        adaptive_runtime=runtime,
        single_static_target="local-a",
    )
    real, real_outbound = await _clients(real_app)
    observed_requests = 0
    async with real, real_outbound:
        while True:
            status = (await real.get("/health")).json()["adaptive_runtime"]
            if status["adaptive_ready"]:
                break
            observed = await real.post(
                "/resolve",
                json={
                    "did": f"did:example:real-observation-{observed_requests}",
                    "strategy": "single",
                },
            )
            assert observed.status_code == 200
            observed_requests += 1
            assert observed_requests < 10

        adaptive = await real.post(
            "/resolve",
            json={"did": DID, "strategy": "adaptive"},
        )

    assert observed_requests > 0
    assert status["ready_did_methods"] == ["example"]
    assert status["by_did_method"]["example"]["planning_status"] == "SELECTED"
    assert runtime.history.snapshot().version == observed_requests + 1
    assert runtime.history.records()[0].evidence_mode == "real"
    assert runtime.controlled_demo_history.snapshot().version == 1
    assert adaptive.status_code == 200
    body = adaptive.json()
    assert body["selection"]["selection_mode"] == "exact"
    assert body["attempts"]
    assert body["cost"]["calls_used"] == 1
    assert body["evidence"] == {"mode": "real"}
    assert body["audit"]["recorded"] is False
