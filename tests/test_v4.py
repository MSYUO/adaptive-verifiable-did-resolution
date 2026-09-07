"""V4 confirmatory protocol tests. Offline only, no public network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from avdr.learning.closedloop import COLD_START_TRIALS, EXPLORATION_INTERVAL_R
from avdr.learning.dataset import DEADLINE_TAU_MS
from avdr.learning.environment import plan_episodes
from avdr.learning.v3 import injection_config_hash_v3, plan_episodes_v3
from v4_confirmatory import (
    ANALYSIS_SEED,
    BOOTSTRAP_RESAMPLES,
    MATERIALITY_SUCCESS,
    V4_EPISODES,
    V4_SEED,
    episode_bootstrap,
    verify_frozen,
)

POLICY = json.loads((REPO_ROOT / "frozen" / "v3_policy.json").read_text(encoding="utf-8"))
META = json.loads((REPO_ROOT / "frozen" / "frozen_estimator_v3.json").read_text(encoding="utf-8"))


def test_v4_seeds_do_not_overlap_any_earlier_split():
    v4 = {p.seed for p in plan_episodes_v3("v4conf", V4_EPISODES, V4_SEED, 22)}
    v1 = set()
    for name, seed, n in (("train", 100_000, 22), ("validation", 300_000, 9), ("holdout", 900_000, 10)):
        v1 |= {p.seed for p in plan_episodes(name, n, seed, 22)}
    v2 = set()
    for name, seed, n in (("v2train", 500_000, 22), ("v2val", 600_000, 9), ("v2holdout", 800_000, 10)):
        v2 |= {p.seed for p in plan_episodes(name, n, seed, 22)}
    v3 = set()
    for name, seed, n in (("v3train", 1_100_000, 24), ("v3val", 1_200_000, 10), ("v3holdout", 1_300_000, 12)):
        v3 |= {p.seed for p in plan_episodes_v3(name, n, seed, 22)}
    qual = {p.seed for p in plan_episodes_v3("v3qual", 6000, 9_900_000, 22)}
    for name, other in (("v1", v1), ("v2", v2), ("v3", v3), ("qual", qual)):
        assert not v4 & other, name


def test_frozen_identity_is_unchanged():
    assert POLICY["estimator_family"] == "b1-rolling-empirical"
    assert POLICY["calibration"] == "uncalibrated"
    assert META["estimator_id"] == "b1-rolling-empirical"
    assert META["calibration"] is None
    assert META["artifact_sha256"] == POLICY["artifact_sha256"]


def test_frozen_comparator_and_parameters():
    assert POLICY["BEST_FIXED_K2"] == "local-b,local-c"
    assert POLICY["target_slo_probability"] == 0.90
    assert POLICY["deadline_tau_ms"] == 250.0
    assert POLICY["exploration_interval_R"] == 8
    assert POLICY["cold_start_trials"] == 3
    assert DEADLINE_TAU_MS == 250.0
    assert EXPLORATION_INTERVAL_R == 8
    assert COLD_START_TRIALS == 3


def test_frozen_generator_hash_is_stable():
    assert injection_config_hash_v3() == injection_config_hash_v3()
    assert injection_config_hash_v3().startswith("sha256:")


def test_verify_frozen_accepts_current_freeze():
    assert verify_frozen(POLICY, META) == []


def test_verify_frozen_rejects_drift():
    bad = dict(POLICY, BEST_FIXED_K2="local-a,local-c")
    assert any("BEST_FIXED_K2" in p for p in verify_frozen(bad, META))
    bad2 = dict(POLICY, target_slo_probability=0.8)
    assert any("target_slo" in p for p in verify_frozen(bad2, META))


def test_paired_table_reconciles_to_n():
    n11, n10, n01, n00 = 40, 12, 5, 3
    assert n11 + n10 + n01 + n00 == 60
    assert (n10 - n01) / 60 == pytest.approx(7 / 60)


def test_bootstrap_resamples_episodes_not_rows():
    per_episode = {
        f"e{i}": {"trials": 10, "adaptive_success": 8, "fixed_success": 7,
                  "adaptive_calls": 15, "fixed_calls": 20}
        for i in range(20)
    }
    result = episode_bootstrap(per_episode, 200, 7)
    assert result["unit"] == "episode (cluster)"
    # Every episode is identical here, so every resample gives the same delta.
    lo, hi = result["delta_success_ci95"]
    assert lo == hi == pytest.approx(0.1)


def test_bootstrap_is_deterministic_under_fixed_seed():
    per_episode = {
        f"e{i}": {"trials": 10, "adaptive_success": i % 7, "fixed_success": i % 5,
                  "adaptive_calls": 10 + i, "fixed_calls": 20}
        for i in range(25)
    }
    a = episode_bootstrap(per_episode, 500, 99)
    b = episode_bootstrap(per_episode, 500, 99)
    c = episode_bootstrap(per_episode, 500, 100)
    assert a == b
    assert a["delta_success_ci95"] != c["delta_success_ci95"]


def test_predeclared_constants_are_frozen():
    assert V4_EPISODES == 120
    assert BOOTSTRAP_RESAMPLES == 10_000
    assert ANALYSIS_SEED == 20260909
    assert MATERIALITY_SUCCESS == 0.02


def test_cost_accounting_includes_every_adaptive_call():
    from avdr.learning.oracle import ClosedLoopOutcome

    o = ClosedLoopOutcome("e", 0, True, ("local-a",), True, "SELECTED", True,
                          execution_calls=1, exploration_calls=3)
    assert o.total_calls == 4


def test_no_public_network_constants():
    from avdr.learning.dataset import ADMIN_URLS
    assert all(u.startswith("http://127.0.0.1:") for u in ADMIN_URLS.values())
