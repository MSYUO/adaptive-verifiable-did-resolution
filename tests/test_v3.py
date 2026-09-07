"""V3 symmetric generator + cold-start + Pareto tests. Offline only."""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from avdr.learning.closedloop import (
    COLD_START_TRIALS,
    ONLINE_SOURCES,
    WARMUP,
    DeploymentObservedHistory,
    reveal_subset,
)
from avdr.learning.environment import PROVIDERS, plan_episodes
from avdr.learning.features import ProviderObservation, TrialRecord
from avdr.learning.v3 import (
    ROLE_TABLE,
    ROLES,
    STATES_V3,
    build_episode_plan_v3,
    injection_config_hash_v3,
    plan_episodes_v3,
    sample_behaviors_v3,
)


def obs(p, accepted=True, completion=20.0):
    return ProviderObservation(
        provider_id=p, accepted=accepted, completion_offset_ms=completion,
        http_status=200, outcome="http_response", timed_out=False,
        errored=False, invalid=False, observed=True,
    )


def trial(i=0):
    return TrialRecord(
        episode_id="ep", trial_index=i, split="t", seed=1, hidden_state="NORMAL",
        did="did:example:x", observations={p: obs(p) for p in PROVIDERS},
        complete=True,
    )


# ---- symmetry by construction -------------------------------------------


def test_v3_states_are_defined_over_roles_not_providers():
    """No provider name may appear anywhere in the role table."""
    for state, table in ROLE_TABLE.items():
        assert set(table) == set(ROLES), state
        for provider in PROVIDERS:
            assert provider not in table


def test_single_provider_states_degrade_exactly_one_role():
    single_states = [s for s in STATES_V3 if s.startswith("SINGLE_PROVIDER")]
    assert len(single_states) == 3
    for state in single_states:
        degraded = [
            r for r in ROLES
            if ROLE_TABLE[state][r] is not ROLE_TABLE["NORMAL"][r]
        ]
        assert degraded == ["role_0"], state


def test_permutation_is_uniform_over_providers():
    plans = plan_episodes_v3("v3t", 3000, 77_000, 5)
    for role in ROLES:
        counts = Counter(p.permutation[role] for p in plans)
        for provider in PROVIDERS:
            share = counts[provider] / len(plans)
            assert 0.28 < share < 0.39, (role, provider, share)


def test_permutation_is_a_bijection_and_reproducible():
    a = build_episode_plan_v3("ep-1", 5, "t", 10)
    b = build_episode_plan_v3("ep-1", 5, "t", 10)
    assert a.permutation == b.permutation
    assert a.states == b.states
    assert sorted(a.permutation.values()) == sorted(PROVIDERS)


def test_provider_exposure_is_balanced_under_sampling():
    rng = random.Random(11)
    degraded = Counter()
    plans = plan_episodes_v3("v3s", 400, 88_000, 22)
    for plan in plans:
        for state in plan.states:
            behaviors = sample_behaviors_v3(state, plan.permutation, rng)
            for provider, b in behaviors.items():
                if (b["force_error"] or b["force_invalid"]
                        or b["artificial_delay_ms"] > 100):
                    degraded[provider] += 1
    total = sum(degraded.values())
    for provider in PROVIDERS:
        assert 0.30 < degraded[provider] / total < 0.37, (provider, degraded)


def test_v1_generator_asymmetry_is_real():
    """Documents the defect V3 exists to remove."""
    from avdr.learning.environment import STATE_TABLE

    default = STATE_TABLE["NORMAL"]["local-a"]
    specific = {p: 0 for p in PROVIDERS}
    for state, table in STATE_TABLE.items():
        degraded = [p for p in PROVIDERS if table[p] is not default]
        if len(degraded) == 1:
            specific[degraded[0]] += 1
    assert specific["local-a"] == 2
    assert specific["local-b"] == 1
    assert specific["local-c"] == 0, "local-c had no provider-specific state"


def test_v3_injection_hash_stable_and_distinct():
    from avdr.learning.environment import injection_config_hash

    assert injection_config_hash_v3() == injection_config_hash_v3()
    assert injection_config_hash_v3() != injection_config_hash()


# ---- cold start ----------------------------------------------------------


def test_warmup_is_an_online_source_and_enters_history():
    assert WARMUP in ONLINE_SOURCES
    history = DeploymentObservedHistory()
    history.append(reveal_subset(trial(0), PROVIDERS, WARMUP))
    assert history.records()[0].observation_source == WARMUP
    assert history.trials_since_observed("local-c") == 0.0


def test_cold_start_is_charged_not_free():
    from avdr.adaptive.estimator import ControlledTableEstimator
    from avdr.learning.dataset import all_subsets
    from avdr.learning.oracle import decompose
    from avdr.learning.runner_v2 import run_closed_loop

    est = ControlledTableEstimator({s: 0.99 for s in all_subsets()})
    loop = run_closed_loop(
        est, 0.90, 250.0, {"ep": [trial(i) for i in range(8)]},
        cold_start_trials=COLD_START_TRIALS,
    )
    summary = decompose(loop.outcomes)
    assert summary["total_exploration_calls"] >= COLD_START_TRIALS * len(PROVIDERS)
    warmups = [o for o in loop.outcomes if o.status == "WARMUP"]
    assert len(warmups) == COLD_START_TRIALS
    assert all(o.exploration_calls == len(PROVIDERS) for o in warmups)


def test_cold_start_populates_history_for_later_trials():
    from avdr.adaptive.estimator import ControlledTableEstimator
    from avdr.learning.dataset import all_subsets
    from avdr.learning.runner_v2 import run_closed_loop

    est = ControlledTableEstimator({s: 0.99 for s in all_subsets()})
    loop = run_closed_loop(
        est, 0.90, 250.0, {"ep": [trial(i) for i in range(8)]},
        cold_start_trials=COLD_START_TRIALS, collect_traces=1,
    )
    trace = loop.traces[0]
    for provider in PROVIDERS:
        assert trace["observed_history_per_provider"][provider]["observation_count"] >= 1


# ---- Pareto --------------------------------------------------------------


def test_pareto_dominance_detection():
    from v3_holdout import pareto

    points = {
        "cheap_bad": (0.50, 1.0),
        "mid": (0.80, 2.0),
        "expensive_same": (0.80, 3.0),
        "best": (0.85, 2.0),
    }
    result = pareto(points)
    assert "expensive_same" in result["dominated"]
    assert "mid" in result["dominated"]
    assert "best" in result["frontier"]
    assert "cheap_bad" in result["frontier"]


def test_pareto_requires_strict_improvement():
    from v3_holdout import pareto

    assert pareto({"a": (0.8, 2.0), "b": (0.8, 2.0)})["dominated"] == {}


# ---- split isolation -----------------------------------------------------


def test_v3_seeds_disjoint_from_v1_v2_and_qualification():
    v1 = {p.seed for p in plan_episodes("holdout", 10, 900_000, 22)}
    v2 = (
        {p.seed for p in plan_episodes("v2train", 22, 500_000, 22)}
        | {p.seed for p in plan_episodes("v2val", 9, 600_000, 22)}
        | {p.seed for p in plan_episodes("v2holdout", 10, 800_000, 22)}
    )
    qual = {p.seed for p in plan_episodes_v3("v3qual", 6000, 9_900_000, 22)}
    train = {p.seed for p in plan_episodes_v3("v3train", 24, 1_100_000, 22)}
    val = {p.seed for p in plan_episodes_v3("v3val", 10, 1_200_000, 22)}
    holdout = {p.seed for p in plan_episodes_v3("v3holdout", 12, 1_300_000, 22)}

    for name, group in (("train", train), ("val", val), ("holdout", holdout)):
        assert not group & v1, name
        assert not group & v2, name
        assert not group & qual, name
    assert not train & val and not train & holdout and not val & holdout


def test_role_permutation_is_not_a_feature():
    from avdr.learning.closedloop import FEATURE_ORDER_V2

    joined = " ".join(FEATURE_ORDER_V2).lower()
    for token in ("role", "permut", "hidden", "state", "scenario"):
        assert token not in joined
