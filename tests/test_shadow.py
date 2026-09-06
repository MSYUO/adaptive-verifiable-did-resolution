"""Integration tests for the shadow characterization harness.

Runs against the same real mock resolver processes used by the routing tests.
All injected conditions are CONTROLLED INJECTION; the instances share one
host and are not independent gateways.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.analysis import audit_dataset, derive_trial
from avdr.provenance import build_provenance, new_experiment_id
from avdr.router.policies import POLICY_TYPES
from avdr.shadow import PARALLEL, SEQUENTIAL, ShadowProbe
from avdr.telemetry import TelemetrySink

from conftest import INJECTED_DELAY_MS, TIMEOUT_SLEEP_MS

DID = "did:example:shadow-subject"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def provenance(router_config):
    return build_provenance(
        experiment_id=new_experiment_id("test"),
        scenario_id="unit",
        phase="shadow-test",
        repo_root=REPO_ROOT,
        router_config_payload=json.loads(router_config.model_dump_json()),
        injection_config_payload={"scenario_id": "unit", "behaviors": {}},
        seed=42,
    )


async def run_one(router_config, provenance, mode=PARALLEL, did=DID):
    probe = ShadowProbe(router_config)
    async with httpx.AsyncClient() as client:
        return await probe.run_trial(
            client=client, did=did, provenance=provenance, mode=mode
        )


# --------------------------------------------------------------------------
# The harness must never become a routing policy
# --------------------------------------------------------------------------


def test_shadow_is_not_registered_as_a_routing_policy():
    assert "shadow" not in POLICY_TYPES
    assert not any("shadow" in name for name in POLICY_TYPES)


# --------------------------------------------------------------------------
# One observation per resolver, bound by trial id
# --------------------------------------------------------------------------


async def test_shadow_trial_observes_every_resolver_exactly_once(
    router_config, provenance
):
    trial = await run_one(router_config, provenance)

    assert trial.record.expected_observations == 3
    assert trial.record.actual_observations == 3
    assert trial.complete is True
    assert trial.record.incomplete_reason is None

    observed = sorted(o.resolver_id for o in trial.observations)
    assert observed == ["resolver-a", "resolver-b", "resolver-c"]
    assert len(set(observed)) == 3


async def test_trial_id_binds_all_observations(router_config, provenance):
    trial = await run_one(router_config, provenance)
    trial_ids = {o.trial_id for o in trial.observations}
    assert trial_ids == {trial.record.trial_id}
    assert all(o.experiment_id == trial.record.experiment_id for o in trial.observations)
    assert all(o.scenario_id == trial.record.scenario_id for o in trial.observations)


async def test_distinct_trials_get_distinct_ids(router_config, provenance):
    first = await run_one(router_config, provenance)
    second = await run_one(router_config, provenance)
    assert first.record.trial_id != second.record.trial_id
    assert first.record.experiment_id == second.record.experiment_id


async def test_no_duplicate_trial_resolver_pairs_across_trials(
    router_config, provenance
):
    trials = [await run_one(router_config, provenance) for _ in range(3)]
    pairs = [
        (o.experiment_id, o.trial_id, o.resolver_id)
        for t in trials
        for o in t.observations
    ]
    assert len(pairs) == 9
    assert len(set(pairs)) == 9


# --------------------------------------------------------------------------
# Provenance survives end to end
# --------------------------------------------------------------------------


async def test_provenance_fields_survive_to_observations(router_config, provenance):
    trial = await run_one(router_config, provenance)
    for observation in trial.observations:
        assert observation.experiment_id == provenance.experiment_id
        assert observation.scenario_id == provenance.scenario_id
        assert observation.phase == provenance.phase
        assert observation.seed == provenance.seed
        assert observation.config_hash == provenance.config_hash
        assert observation.injection_config_hash == provenance.injection_config_hash
        assert observation.git_commit == provenance.git_commit


async def test_provenance_survives_a_jsonl_round_trip(
    router_config, provenance, tmp_path
):
    trial = await run_one(router_config, provenance)
    sink = TelemetrySink(tmp_path / "shadow")
    for observation in trial.observations:
        sink.record_shadow_observation(observation)
    sink.record_shadow_trial(trial.record)

    trials = [
        json.loads(line)
        for line in sink.shadow_trials_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    observations = [
        json.loads(line)
        for line in sink.shadow_observations_path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]

    assert len(trials) == 1
    assert len(observations) == 3
    assert trials[0]["config_hash"] == provenance.config_hash
    assert all(o["injection_config_hash"] == provenance.injection_config_hash for o in observations)

    audit = audit_dataset(trials, observations)
    assert audit["passed"], audit["violations"]


# --------------------------------------------------------------------------
# Parallel measurement path
# --------------------------------------------------------------------------


async def test_parallel_mode_records_launch_skew(router_config, provenance):
    trial = await run_one(router_config, provenance, mode=PARALLEL)
    assert trial.record.mode == "parallel"
    # Skew is measured and reported, never assumed to be zero.
    assert trial.record.launch_skew_ms is not None
    assert trial.record.launch_skew_ms >= 0
    offsets = [o.launch_offset_ms for o in trial.observations]
    assert all(o >= 0 for o in offsets)
    assert trial.record.launch_skew_ms == pytest.approx(
        max(offsets) - min(offsets), abs=1e-3
    )


async def test_parallel_mode_overlaps_a_delayed_resolver(
    router_config, provenance, healthy_cluster
):
    """Concurrency evidence: the trial must not take the sum of the delays."""
    healthy_cluster["resolver-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)
    healthy_cluster["resolver-c"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)

    trial = await run_one(router_config, provenance, mode=PARALLEL)
    assert trial.complete

    delayed = [
        o for o in trial.observations if o.resolver_id in ("resolver-b", "resolver-c")
    ]
    assert all(o.latency_ms >= INJECTED_DELAY_MS * 0.85 for o in delayed)
    # Sequential execution would need ~2x the injected delay.
    assert trial.record.trial_duration_ms < INJECTED_DELAY_MS * 1.8


async def test_sequential_mode_is_available_and_complete(router_config, provenance):
    trial = await run_one(router_config, provenance, mode=SEQUENTIAL)
    assert trial.record.mode == "sequential"
    assert trial.complete
    assert trial.record.actual_observations == 3


async def test_unknown_mode_is_rejected(router_config, provenance):
    probe = ShadowProbe(router_config)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="unknown shadow mode"):
            await probe.run_trial(
                client=client, did=DID, provenance=provenance, mode="race"
            )


# --------------------------------------------------------------------------
# Observation content under controlled injection
# --------------------------------------------------------------------------


async def test_shadow_observes_invalid_and_valid_in_one_trial(
    router_config, provenance, healthy_cluster
):
    """The counterfactual the routing policies cannot produce.

    Both acceptable resolvers are delayed so the ground truth is unambiguous:
    with only resolver-b delayed, resolver-a and resolver-c would race at
    ~equal latency and the expected winner would not be deterministic.
    """
    healthy_cluster["resolver-a"].set_behavior(force_invalid=True)
    healthy_cluster["resolver-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)
    healthy_cluster["resolver-c"].set_behavior(
        artificial_delay_ms=INJECTED_DELAY_MS * 2
    )

    trial = await run_one(router_config, provenance, mode=PARALLEL)
    assert trial.complete

    by_id = {o.resolver_id: o for o in trial.observations}
    assert by_id["resolver-a"].http_status == 200
    assert by_id["resolver-a"].accepted is False
    assert by_id["resolver-a"].document_valid is False
    assert by_id["resolver-b"].accepted is True
    assert by_id["resolver-c"].accepted is True

    # Every resolver was observed, so the comparison is now well defined.
    derivation = derive_trial(
        trial.record.model_dump(mode="json"),
        [o.model_dump(mode="json") for o in trial.observations],
    )
    assert derivation.analyzable
    assert derivation.fastest_responding_resolver == "resolver-a"
    assert derivation.fastest_accepted_resolver == "resolver-b"
    assert derivation.fastest_matches_fastest_accepted is False


async def test_shadow_records_timeout_as_censored(
    router_config, provenance, healthy_cluster
):
    healthy_cluster["resolver-a"].set_behavior(
        force_timeout=True, timeout_sleep_ms=TIMEOUT_SLEEP_MS
    )
    trial = await run_one(router_config, provenance, mode=PARALLEL)
    assert trial.complete
    assert trial.record.actual_observations == 3

    by_id = {o.resolver_id: o for o in trial.observations}
    assert by_id["resolver-a"].timeout is True
    assert by_id["resolver-a"].http_status is None
    assert by_id["resolver-a"].responded is False

    derivation = derive_trial(
        trial.record.model_dump(mode="json"),
        [o.model_dump(mode="json") for o in trial.observations],
    )
    assert derivation.censored_count == 1
    assert derivation.censored_resolvers == ["resolver-a"]
    assert derivation.fastest_responding_resolver != "resolver-a"


async def test_all_resolvers_unacceptable_is_not_analyzable(
    router_config, provenance, healthy_cluster
):
    for resolver in healthy_cluster.values():
        resolver.set_behavior(force_error=True)
    trial = await run_one(router_config, provenance, mode=PARALLEL)

    assert trial.complete  # every resolver was still observed
    derivation = derive_trial(
        trial.record.model_dump(mode="json"),
        [o.model_dump(mode="json") for o in trial.observations],
    )
    assert derivation.analyzable is False
    assert derivation.accepted_count == 0
    assert derivation.fastest_matches_fastest_accepted is None
