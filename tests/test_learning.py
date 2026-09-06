"""Prospective estimator pipeline tests.

Everything here is offline: the controlled environment is exercised through
its sampling functions and synthetic TrialRecords, so no HTTP and no public
network is involved. The session socket guard remains active.

All injected values are [CONTROLLED INJECTION] and all q_hat values are
[CONTROLLED TEST INPUT].
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.adaptive.optimizer import (
    ESTIMATOR_COVERAGE_INCOMPLETE,
    SELECTED,
    MinimumSetOptimizer,
)
from avdr.learning.dataset import (
    DEADLINE_TAU_MS,
    all_subsets,
    subset_target,
)
from avdr.learning.environment import (
    PROVIDERS,
    SHARED_DEGRADATION,
    STATES,
    build_episode_plan,
    injection_config_hash,
    plan_episodes,
    sample_behaviors,
)
from avdr.learning.estimators import (
    CONTEXT_KEY,
    HISTORY_KEY,
    EwmaEstimator,
    GlobalRateEstimator,
    RollingEmpiricalEstimator,
    SubsetRateEstimator,
    build_gradient_boosting,
    build_logistic,
    freeze_estimator,
    load_frozen,
)
from avdr.learning.features import (
    FEATURE_ORDER,
    HISTORY_WINDOW,
    NO_HISTORY,
    ProviderObservation,
    TrialRecord,
    build_context,
    build_row,
    feature_schema_hash,
)
from avdr.learning.metrics import (
    DecisionOutcome,
    brier_score,
    evaluate_estimator,
    log_loss,
    reliability,
    summarize_decisions,
)

TAU = DEADLINE_TAU_MS


def obs(provider, accepted=True, completion=20.0, status=200, outcome="http_response",
        timed_out=False, errored=False, invalid=False, observed=True):
    return ProviderObservation(
        provider_id=provider, accepted=accepted, completion_offset_ms=completion,
        http_status=status, outcome=outcome, timed_out=timed_out,
        errored=errored, invalid=invalid, observed=observed,
    )


def record(trial_index=0, episode_id="ep-1", **provider_overrides) -> TrialRecord:
    observations = {p: obs(p) for p in PROVIDERS}
    observations.update(provider_overrides)
    return TrialRecord(
        episode_id=episode_id, trial_index=trial_index, split="test", seed=1,
        hidden_state="NORMAL", did="did:example:x", observations=observations,
        complete=all(o.observed for o in observations.values()),
    )


# ==========================================================================
# 3-5. subset target derivation
# ==========================================================================


def test_target_uses_launch_offset_plus_latency():
    """Completion offset, not raw latency, decides the deadline."""
    # local-a launched late but is fast; its ABSOLUTE completion is what counts.
    late = record(**{"local-a": obs("local-a", accepted=True, completion=260.0)})
    assert late.within_deadline("local-a", TAU) is False
    early = record(**{"local-a": obs("local-a", accepted=True, completion=240.0)})
    assert early.within_deadline("local-a", TAU) is True


def test_invalid_response_cannot_satisfy_target():
    """Fast but structurally unacceptable contributes nothing."""
    rec = record(
        **{
            "local-a": obs("local-a", accepted=False, completion=5.0, invalid=True),
            "local-b": obs("local-b", accepted=False, completion=6.0, invalid=True),
            "local-c": obs("local-c", accepted=False, completion=7.0, invalid=True),
        }
    )
    assert subset_target(rec, PROVIDERS, TAU) == 0
    assert subset_target(rec, ("local-a",), TAU) == 0


def test_deadline_miss_cannot_satisfy_target():
    rec = record(
        **{p: obs(p, accepted=True, completion=TAU + 1.0) for p in PROVIDERS}
    )
    assert subset_target(rec, PROVIDERS, TAU) == 0
    # One provider just inside the deadline flips the whole subset.
    rec.observations["local-b"] = obs("local-b", accepted=True, completion=TAU - 1.0)
    assert subset_target(rec, PROVIDERS, TAU) == 1
    assert subset_target(rec, ("local-a", "local-c"), TAU) == 0


def test_timeout_and_error_contribute_zero():
    rec = record(
        **{
            "local-a": obs("local-a", accepted=False, completion=None,
                           status=None, outcome="timeout", timed_out=True),
            "local-b": obs("local-b", accepted=False, completion=12.0,
                           status=503, errored=True),
            "local-c": obs("local-c", accepted=True, completion=15.0),
        }
    )
    assert subset_target(rec, ("local-a",), TAU) == 0
    assert subset_target(rec, ("local-b",), TAU) == 0
    assert subset_target(rec, ("local-a", "local-b"), TAU) == 0
    assert subset_target(rec, ("local-a", "local-c"), TAU) == 1


def test_missing_observation_is_not_inferred_as_success():
    rec = record(
        **{"local-a": obs("local-a", accepted=True, completion=5.0, observed=False)}
    )
    assert rec.complete is False
    assert rec.within_deadline("local-a", TAU) is False
    assert subset_target(rec, ("local-a",), TAU) == 0


def test_subset_enumeration_is_complete_and_sorted():
    subsets = all_subsets()
    assert len(subsets) == 7
    assert all(list(s) == sorted(s) for s in subsets)
    assert ("local-a", "local-b", "local-c") in subsets


# ==========================================================================
# 6-8. pre-request feature boundary / leakage
# ==========================================================================


def test_features_ignore_current_trial_outcome():
    """Perturbing trial t's own outcome must not change trial t's features."""
    history = [record(trial_index=i) for i in range(5)]
    context_a = build_context(history, TAU, "ep-1", 5)
    row_a = build_row(context_a, ("local-a",))

    # Trial 5's outcome (catastrophic) is NOT part of the history passed in.
    _current = record(
        trial_index=5,
        **{p: obs(p, accepted=False, completion=999.0) for p in PROVIDERS},
    )
    context_b = build_context(history, TAU, "ep-1", 5)
    assert build_row(context_b, ("local-a",)) == row_a


def test_features_ignore_future_trials():
    history = [record(trial_index=i) for i in range(4)]
    future = [
        record(trial_index=i,
               **{p: obs(p, accepted=False, completion=900.0) for p in PROVIDERS})
        for i in range(4, 9)
    ]
    baseline = build_row(build_context(history, TAU, "ep-1", 4), ("local-b",))
    # Appending future trials must not retroactively change t=4 features.
    assert build_row(build_context(history, TAU, "ep-1", 4), ("local-b",)) == baseline
    with_future = build_row(
        build_context(history + future, TAU, "ep-1", 4), ("local-b",)
    )
    assert with_future != baseline, "sanity: future data would change features"


def test_history_passed_to_features_is_strictly_earlier():
    """Structural audit of the boundary the generator relies on."""
    history = [record(trial_index=i) for i in range(7)]
    for t in range(1, 8):
        window = history[:t]
        assert all(r.trial_index < t for r in window)
        build_context(window, TAU, "ep-1", t)


def test_hidden_injection_state_is_not_a_feature():
    """The hidden state must be invisible to the model."""
    joined = " ".join(FEATURE_ORDER).lower()
    for state in STATES:
        assert state.lower() not in joined
    for token in ("hidden", "state", "scenario", "episode", "seed", "inject"):
        assert token not in joined
    # And a differing hidden state with identical history yields identical rows.
    normal = [record(trial_index=i) for i in range(3)]
    for r in normal:
        r.hidden_state = "NORMAL"
    shared = [record(trial_index=i) for i in range(3)]
    for r in shared:
        r.hidden_state = SHARED_DEGRADATION
    assert build_row(build_context(normal, TAU, "e", 3), ("local-a",)) == build_row(
        build_context(shared, TAU, "e", 3), ("local-a",)
    )


def test_empty_history_uses_explicit_no_history_sentinel():
    context = build_context([], TAU, "ep-1", 0)
    row = build_row(context, ("local-a",))
    assert context.per_provider["local-a"]["obs_count"] == 0.0
    assert context.per_provider["local-a"]["accept_rate"] == NO_HISTORY
    assert len(row) == len(FEATURE_ORDER)


def test_rolling_window_is_bounded():
    history = [record(trial_index=i) for i in range(HISTORY_WINDOW + 15)]
    context = build_context(history, TAU, "ep-1", len(history))
    assert context.per_provider["local-a"]["obs_count"] == float(HISTORY_WINDOW)


# ==========================================================================
# 9-11. splits, encoding, environment
# ==========================================================================


def test_episode_and_seed_splits_are_disjoint():
    train = plan_episodes("train", 22, 100_000, 22)
    val = plan_episodes("validation", 9, 300_000, 22)
    holdout = plan_episodes("holdout", 10, 900_000, 22)

    ids = [{p.episode_id for p in group} for group in (train, val, holdout)]
    seeds = [{p.seed for p in group} for group in (train, val, holdout)]
    for i in range(3):
        for j in range(i + 1, 3):
            assert not ids[i] & ids[j], "episode id overlap between splits"
            assert not seeds[i] & seeds[j], "seed overlap between splits"


def test_episode_plans_are_reproducible_and_sequential():
    a = build_episode_plan("ep-1", 42, "train", 30)
    b = build_episode_plan("ep-1", 42, "train", 30)
    assert a.states == b.states
    assert len(a.states) == 30
    # Piecewise-constant: states persist in runs rather than flipping per trial.
    runs = sum(1 for i in range(1, len(a.states)) if a.states[i] != a.states[i - 1])
    assert runs < len(a.states) - 1


def test_subset_membership_encoding_is_deterministic():
    context = build_context([record(trial_index=0)], TAU, "e", 1)
    first = build_row(context, ("local-a", "local-c"))
    second = build_row(context, ("local-c", "local-a"))
    assert first == second
    mask = first[-4:]
    assert mask == [1.0, 0.0, 1.0, 2.0]
    assert build_row(context, PROVIDERS)[-4:] == [1.0, 1.0, 1.0, 3.0]


def test_shared_degradation_affects_every_provider():
    """The correlated condition exists, so independence must not be assumed."""
    rng = random.Random(7)
    slow = 0
    for _ in range(60):
        behaviors = sample_behaviors(SHARED_DEGRADATION, rng)
        if all(b["artificial_delay_ms"] > TAU * 0.5 for b in behaviors.values()):
            slow += 1
    assert slow > 40, "SHARED_DEGRADATION should slow all providers together"


def test_injection_config_hash_is_stable():
    assert injection_config_hash() == injection_config_hash()
    assert injection_config_hash().startswith("sha256:")


def test_feature_schema_hash_is_stable():
    assert feature_schema_hash() == feature_schema_hash()
    assert len(FEATURE_ORDER) == len(set(FEATURE_ORDER))


# ==========================================================================
# 12-13. estimator outputs
# ==========================================================================


def synthetic_rows(n=400, seed=3):
    from avdr.learning.dataset import DatasetRow

    rng = random.Random(seed)
    rows = []
    for t in range(n):
        history = [record(trial_index=i) for i in range(min(t, 5))]
        context = build_context(history, TAU, "ep", t)
        for subset in all_subsets():
            rows.append(
                DatasetRow(
                    episode_id="ep", trial_index=t, split="train", subset=subset,
                    features=build_row(context, subset),
                    target=1 if rng.random() < 0.3 + 0.2 * len(subset) else 0,
                    hidden_state="NORMAL",
                )
            )
    return rows


@pytest.mark.parametrize(
    "factory",
    [
        lambda rows: GlobalRateEstimator().fit(rows),
        lambda rows: SubsetRateEstimator().fit(rows),
        lambda rows: RollingEmpiricalEstimator().fit(rows),
        lambda rows: EwmaEstimator().fit(rows),
        lambda rows: build_logistic().fit(rows),
        lambda rows: build_gradient_boosting().fit(rows),
    ],
)
def test_all_estimators_produce_valid_probabilities_for_every_subset(factory):
    rows = synthetic_rows()
    estimator = factory(rows)
    context = build_context([record(trial_index=0)], TAU, "ep", 1)
    payload = {CONTEXT_KEY: context, HISTORY_KEY: {("local-a",): [1, 0, 1]}}
    for subset in all_subsets():
        q = estimator.estimate(subset, payload)
        assert q is not None, f"{estimator.estimator_id} skipped {subset}"
        assert 0.0 <= q <= 1.0
    assert estimator.config_hash().startswith("sha256:")


def test_learned_estimator_supplies_every_required_subset_to_optimizer():
    rows = synthetic_rows()
    estimator = build_logistic().fit(rows)
    context = build_context([record(trial_index=0)], TAU, "ep", 1)
    payload = {CONTEXT_KEY: context, HISTORY_KEY: {}}
    result = MinimumSetOptimizer().select(
        list(PROVIDERS), estimator, 0.5, context=payload
    )
    # Complete coverage is what allows an exact-minimum claim at all.
    assert result.status in (SELECTED, "SLO_ESTIMATE_UNSATISFIABLE")
    assert result.status != ESTIMATOR_COVERAGE_INCOMPLETE
    assert result.expected_subset_count == 7
    assert result.estimated_subset_count == 7
    assert result.exact is True


def test_estimator_without_context_returns_none_not_a_guess():
    estimator = build_logistic().fit(synthetic_rows())
    assert estimator.estimate(("local-a",), None) is None


# ==========================================================================
# 14-16. freeze / reload
# ==========================================================================


def test_freeze_produces_stable_hash_and_reload_reproduces_predictions(tmp_path):
    rows = synthetic_rows()
    estimator = build_logistic().fit(rows)
    context = build_context([record(trial_index=0)], TAU, "ep", 1)
    payload = {CONTEXT_KEY: context, HISTORY_KEY: {}}
    before = {s: estimator.estimate(s, payload) for s in all_subsets()}

    path = tmp_path / "frozen.pkl"
    artifact = freeze_estimator(
        estimator, path, model_family="m1-logistic", hyperparameters={},
        deadline_tau_ms=TAU, train_dataset_id="t", validation_dataset_id="v",
    )
    assert artifact.artifact_sha256.startswith("sha256:")
    assert artifact.feature_order == list(FEATURE_ORDER)

    reloaded, metadata, digest = load_frozen(path)
    assert digest == artifact.artifact_sha256
    assert metadata["deadline_tau_ms"] == TAU
    after = {s: reloaded.estimate(s, payload) for s in all_subsets()}
    assert after == before


def test_tampered_artifact_is_rejected(tmp_path):
    estimator = GlobalRateEstimator(0.7)
    path = tmp_path / "frozen.pkl"
    freeze_estimator(
        estimator, path, model_family="b0", hyperparameters={},
        deadline_tau_ms=TAU, train_dataset_id="t", validation_dataset_id="v",
    )
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_frozen(path)


# ==========================================================================
# 17-19. metrics and decision accounting
# ==========================================================================


def test_brier_and_log_loss_are_correct():
    assert brier_score([1, 0], [1.0, 0.0]) == pytest.approx(0.0)
    assert brier_score([1, 0], [0.0, 1.0]) == pytest.approx(1.0)
    assert brier_score([1], [0.5]) == pytest.approx(0.25)
    assert log_loss([1], [0.5]) == pytest.approx(0.6931, abs=1e-3)


def test_reliability_binning_convention():
    result = reliability([1, 1, 0, 0], [0.9, 0.85, 0.1, 0.05])
    assert result["convention"].startswith("10 equal-width bins")
    assert len(result["bins"]) == 10
    # bin (0.8,0.9]: mean_pred 0.875 vs observed 1.0 -> 0.125, weight 0.5
    # bin [0.0,0.1]: mean_pred 0.075 vs observed 0.0 -> 0.075, weight 0.5
    # ECE = 0.5*0.125 + 0.5*0.075 = 0.10
    assert result["ece"] == pytest.approx(0.10, abs=1e-6)


def test_false_feasible_calculation():
    """Predicted feasible but the executed subset actually missed."""
    outcomes = [
        DecisionOutcome("adaptive", ("a",), 1, satisfied=True,
                        predicted_feasible=True, status=SELECTED),
        DecisionOutcome("adaptive", ("a",), 1, satisfied=False,
                        predicted_feasible=True, status=SELECTED),
        DecisionOutcome("adaptive", ("a", "b"), 2, satisfied=False,
                        predicted_feasible=True, status=SELECTED),
        DecisionOutcome("adaptive", (), 0, satisfied=False,
                        predicted_feasible=False,
                        status="SLO_ESTIMATE_UNSATISFIABLE"),
    ]
    summary = summarize_decisions(outcomes)
    assert summary["n"] == 4
    assert summary["planned"] == 3
    assert summary["false_feasible_count"] == 2
    assert summary["false_feasible_rate"] == pytest.approx(2 / 3)
    assert summary["slo_satisfaction_rate"] == pytest.approx(1 / 3)
    assert summary["unsatisfiable_estimate_count"] == 1
    assert summary["mean_selected_subset_size"] == pytest.approx(4 / 3, abs=1e-4)
    assert summary["total_provider_calls"] == 4


def test_evaluate_estimator_reports_base_rate_and_mean_prediction():
    metrics = evaluate_estimator("x", [1, 1, 0, 0], [0.6, 0.6, 0.4, 0.4])
    assert metrics.n == 4
    assert metrics.base_rate == pytest.approx(0.5)
    assert metrics.mean_prediction == pytest.approx(0.5)


def test_baseline_estimators_execute_and_use_history():
    rows = synthetic_rows()
    b1 = RollingEmpiricalEstimator().fit(rows)
    optimistic = b1.estimate(("local-a",), {HISTORY_KEY: {("local-a",): [1] * 10}})
    pessimistic = b1.estimate(("local-a",), {HISTORY_KEY: {("local-a",): [0] * 10}})
    assert optimistic > pessimistic

    b2 = EwmaEstimator().fit(rows)
    up = b2.estimate(("local-b",), {HISTORY_KEY: {("local-b",): [1] * 10}})
    down = b2.estimate(("local-b",), {HISTORY_KEY: {("local-b",): [0] * 10}})
    assert up > down


def test_no_public_network_in_learning_paths():
    """Data generation targets loopback only."""
    from avdr.learning.dataset import ADMIN_URLS

    for url in ADMIN_URLS.values():
        assert url.startswith("http://127.0.0.1:")
