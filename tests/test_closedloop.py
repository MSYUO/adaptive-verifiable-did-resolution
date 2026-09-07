"""Closed-loop partial-feedback tests.

Offline: synthetic TrialRecords, no HTTP, no public network. All injected
values are [CONTROLLED INJECTION]; all q_hat values [CONTROLLED TEST INPUT].
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.adaptive.estimator import ControlledTableEstimator
from avdr.adaptive.optimizer import ESTIMATOR_COVERAGE_INCOMPLETE, MinimumSetOptimizer
from avdr.learning.closedloop import (
    AUDIT_COLLECTION,
    EVALUATOR_ONLY,
    EXPLORATION_INTERVAL_R,
    FEATURE_ORDER_V2,
    SCHEDULED_EXPLORATION,
    SELECTED_EXECUTION,
    SERVICE_BEST_EFFORT,
    STRICT_SLO,
    DeploymentObservedHistory,
    EvaluatorGroundTruthHistory,
    ObservedRecord,
    build_partial_context,
    build_row_v2,
    feature_schema_hash_v2,
    known_subset_outcome,
    reveal_subset,
    should_explore,
    update_subset_history,
)
from avdr.learning.dataset import DEADLINE_TAU_MS, all_subsets
from avdr.learning.environment import PROVIDERS, plan_episodes
from avdr.learning.estimators import BackoffSubsetRateEstimator, GlobalRateEstimator
from avdr.learning.features import NO_HISTORY, ProviderObservation, TrialRecord
from avdr.learning.oracle import (
    AVOIDABLE_MISS,
    CORRECT_ABSTENTION,
    INTRINSIC_MISS,
    UNNECESSARY_ABSTENTION,
    ClosedLoopOutcome,
    TrialOracle,
    bound_v1_decomposition,
    decompose,
)
from avdr.learning.runner_v2 import CONTEXT_KEY_V2, run_closed_loop

TAU = DEADLINE_TAU_MS


def obs(provider, accepted=True, completion=20.0):
    return ProviderObservation(
        provider_id=provider, accepted=accepted, completion_offset_ms=completion,
        http_status=200 if accepted else 503, outcome="http_response",
        timed_out=False, errored=not accepted, invalid=False, observed=True,
    )


def trial(index=0, episode="ep", **overrides):
    observations = {p: obs(p) for p in PROVIDERS}
    observations.update(overrides)
    return TrialRecord(
        episode_id=episode, trial_index=index, split="test", seed=1,
        hidden_state="NORMAL", did="did:example:x", observations=observations,
        complete=True,
    )


# ==========================================================================
# 1. evaluator history is unreachable from online code
# ==========================================================================


def test_evaluator_history_cannot_build_features():
    evaluator = EvaluatorGroundTruthHistory()
    evaluator.append(trial(0))
    with pytest.raises(TypeError, match="DeploymentObservedHistory"):
        build_partial_context(evaluator, TAU, 1)


def test_evaluator_history_refuses_iteration():
    evaluator = EvaluatorGroundTruthHistory()
    evaluator.append(trial(0))
    with pytest.raises(TypeError, match="never be iterated"):
        list(evaluator)


def test_evaluator_only_records_cannot_enter_deployment_history():
    history = DeploymentObservedHistory()
    with pytest.raises(ValueError, match="may not"):
        history.append(
            ObservedRecord(trial_index=0, observations={}, observation_source=EVALUATOR_ONLY)
        )


# ==========================================================================
# 2-3. partial observation boundary
# ==========================================================================


def test_unselected_provider_outcome_does_not_enter_next_features():
    """The core V2 property: what you did not call, you did not learn."""
    record = trial(0, **{"local-b": obs("local-b", accepted=False, completion=900.0)})
    history = DeploymentObservedHistory()
    history.append(reveal_subset(record, ("local-a",), SELECTED_EXECUTION))

    context = build_partial_context(history, TAU, 1)
    # local-a was called: real statistics.
    assert context.per_provider["local-a"]["obs_count"] == 1.0
    assert context.per_provider["local-a"]["trials_since_observed"] == 0.0
    # local-b and local-c were NOT called: nothing is known about them.
    for provider in ("local-b", "local-c"):
        assert context.per_provider[provider]["obs_count"] == 0.0
        assert context.per_provider[provider]["accept_rate"] == NO_HISTORY
        assert context.per_provider[provider]["trials_since_observed"] == NO_HISTORY
        assert context.per_provider[provider]["observation_count"] == 0.0


def test_selected_provider_outcome_does_enter_next_features():
    record = trial(0, **{"local-b": obs("local-b", accepted=False, completion=900.0)})
    history = DeploymentObservedHistory()
    history.append(reveal_subset(record, ("local-a", "local-b"), SELECTED_EXECUTION))
    context = build_partial_context(history, TAU, 1)
    assert context.per_provider["local-b"]["obs_count"] == 1.0
    assert context.per_provider["local-b"]["accept_rate"] == 0.0
    assert context.per_provider["local-b"]["within_deadline_rate"] == 0.0
    assert context.per_provider["local-c"]["obs_count"] == 0.0


def test_reveal_subset_exposes_only_named_providers():
    record = trial(0)
    revealed = reveal_subset(record, ("local-c",), SELECTED_EXECUTION)
    assert revealed.observed_providers() == {"local-c"}
    assert "local-a" not in revealed.observations


# ==========================================================================
# 4. exploration tagging and charging
# ==========================================================================


def test_exploration_schedule_is_fixed_and_periodic():
    assert should_explore(0, EXPLORATION_INTERVAL_R) is False
    assert should_explore(EXPLORATION_INTERVAL_R, EXPLORATION_INTERVAL_R) is True
    assert should_explore(EXPLORATION_INTERVAL_R + 1, EXPLORATION_INTERVAL_R) is False
    # E0: no exploration at all.
    assert should_explore(50, None) is False
    assert should_explore(50, 0) is False


def test_exploration_observation_is_tagged_and_enters_history():
    record = trial(0)
    history = DeploymentObservedHistory()
    history.append(reveal_subset(record, PROVIDERS, SCHEDULED_EXPLORATION))
    assert history.records()[0].observation_source == SCHEDULED_EXPLORATION
    assert history.records()[0].observed_providers() == set(PROVIDERS)


def test_exploration_calls_are_charged_in_cost_accounting():
    outcomes = [
        ClosedLoopOutcome("ep", 0, True, ("local-a",), True, "SELECTED", True,
                          execution_calls=1, exploration_calls=0),
        ClosedLoopOutcome("ep", 1, True, ("local-a",), True, "SELECTED", True,
                          execution_calls=1, exploration_calls=3),
    ]
    summary = decompose(outcomes)
    assert summary["total_execution_calls"] == 2
    assert summary["total_exploration_calls"] == 3
    assert summary["total_provider_calls"] == 5
    assert summary["mean_total_calls"] == pytest.approx(2.5)
    # Exploration is never free.
    assert summary["mean_total_calls"] > summary["mean_execution_calls"]


# ==========================================================================
# 5-6. staleness
# ==========================================================================


def test_staleness_grows_while_provider_unobserved():
    history = DeploymentObservedHistory()
    for i in range(5):
        history.append(reveal_subset(trial(i), ("local-a",), SELECTED_EXECUTION))
    assert history.trials_since_observed("local-a") == 0.0
    assert history.trials_since_observed("local-b") == NO_HISTORY

    history.append(reveal_subset(trial(5), ("local-b",), SELECTED_EXECUTION))
    assert history.trials_since_observed("local-b") == 0.0
    assert history.trials_since_observed("local-a") == 1.0
    history.append(reveal_subset(trial(6), ("local-b",), SELECTED_EXECUTION))
    assert history.trials_since_observed("local-a") == 2.0


def test_staleness_resets_after_observation():
    history = DeploymentObservedHistory()
    for i in range(6):
        history.append(reveal_subset(trial(i), ("local-a",), SELECTED_EXECUTION))
    assert history.trials_since_observed("local-c") == NO_HISTORY
    history.append(reveal_subset(trial(6), PROVIDERS, SCHEDULED_EXPLORATION))
    assert history.trials_since_observed("local-c") == 0.0
    assert history.observation_count("local-c") == 1


def test_no_silent_forward_fill_of_stale_rates():
    """An old observation must not be presented as a current window rate."""
    history = DeploymentObservedHistory()
    history.append(reveal_subset(trial(0), ("local-c",), SELECTED_EXECUTION))
    for i in range(1, 15):
        history.append(reveal_subset(trial(i), ("local-a",), SELECTED_EXECUTION))
    context = build_partial_context(history, TAU, 15)
    # local-c has aged out of the 10-trial window: rates are sentinel, and the
    # staleness is reported explicitly rather than the old value persisting.
    assert context.per_provider["local-c"]["recent_observation_window_count"] == 0.0
    assert context.per_provider["local-c"]["accept_rate"] == NO_HISTORY
    assert context.per_provider["local-c"]["trials_since_observed"] == 14.0
    assert context.per_provider["local-c"]["observation_count"] == 1.0


def test_v2_schema_includes_staleness_features():
    joined = " ".join(FEATURE_ORDER_V2)
    for name in ("trials_since_observed", "observation_count",
                 "recent_observation_window_count"):
        assert name in joined
    assert len(FEATURE_ORDER_V2) == 39
    assert feature_schema_hash_v2().startswith("sha256:")
    # Hidden state still absent.
    assert "hidden" not in joined.lower() and "scenario" not in joined.lower()


# ==========================================================================
# 7. no current/future leakage
# ==========================================================================


def test_features_do_not_see_current_trial():
    history = DeploymentObservedHistory()
    for i in range(3):
        history.append(reveal_subset(trial(i), PROVIDERS, SCHEDULED_EXPLORATION))
    before = build_row_v2(build_partial_context(history, TAU, 3), ("local-a",))
    # Constructing (but not appending) trial 3 must change nothing.
    _current = trial(3, **{p: obs(p, accepted=False, completion=999.0) for p in PROVIDERS})
    after = build_row_v2(build_partial_context(history, TAU, 3), ("local-a",))
    assert before == after


def test_features_change_only_after_history_append():
    history = DeploymentObservedHistory()
    history.append(reveal_subset(trial(0), PROVIDERS, SCHEDULED_EXPLORATION))
    first = build_row_v2(build_partial_context(history, TAU, 1), ("local-a",))
    history.append(
        reveal_subset(
            trial(1, **{"local-a": obs("local-a", accepted=False, completion=800.0)}),
            PROVIDERS, SCHEDULED_EXPLORATION,
        )
    )
    second = build_row_v2(build_partial_context(history, TAU, 2), ("local-a",))
    assert first != second


# ==========================================================================
# 8. coverage under partial observability
# ==========================================================================


def test_backoff_estimator_gives_complete_coverage_with_empty_history():
    from avdr.learning.dataset import DatasetRow

    rows = [
        DatasetRow("ep", 0, "train", s, [0.0] * len(FEATURE_ORDER_V2), 1, "NORMAL")
        for s in all_subsets()
    ]
    estimator = BackoffSubsetRateEstimator().fit(rows)
    result = MinimumSetOptimizer().select(
        list(PROVIDERS), estimator, 0.5, context={"subset_history": {}}
    )
    assert result.status != ESTIMATOR_COVERAGE_INCOMPLETE
    assert result.exact is True
    assert result.estimated_subset_count == 7


def test_incomplete_coverage_still_blocks_under_partial_feedback():
    partial = ControlledTableEstimator({("local-a",): 0.99})
    result = MinimumSetOptimizer().select(list(PROVIDERS), partial, 0.5)
    assert result.status == ESTIMATOR_COVERAGE_INCOMPLETE
    assert result.exact is False


def test_backoff_chain_is_hierarchical_not_independence():
    from avdr.learning.dataset import DatasetRow

    rows = [
        DatasetRow("ep", 0, "train", ("local-a",), [0.0], 1, "N"),
        DatasetRow("ep", 0, "train", ("local-b",), [0.0], 0, "N"),
        DatasetRow("ep", 0, "train", ("local-a", "local-b"), [0.0], 0, "N"),
    ]
    estimator = BackoffSubsetRateEstimator().fit(rows)
    # size-2 training rate is 0.0; independence over the singletons would give
    # 1-(0*1)=1.0, so observing the size-rate proves no composition happened.
    assert estimator.estimate(("local-a", "local-b"), {"subset_history": {}}) < 0.5


def test_known_subset_outcome_is_three_valued():
    within = {"local-a": False, "local-b": True}
    assert known_subset_outcome({"local-b"}, ("local-a", "local-b"), within) == 1
    assert known_subset_outcome({"local-a"}, ("local-a", "local-b"), within) is None
    assert known_subset_outcome({"local-a", "local-b"}, ("local-a",), within) == 0


def test_unknown_subset_outcomes_are_not_recorded_as_failures():
    history: dict = {}
    within = {"local-a": False}
    updated = update_subset_history(history, {"local-a"}, within, all_subsets())
    # Only subsets fully determined by observing local-a alone are recorded.
    assert ("local-a",) in history and history[("local-a",)] == [0]
    assert ("local-a", "local-b") not in history
    assert updated < len(all_subsets())


# ==========================================================================
# 9-10. strict vs best-effort
# ==========================================================================


def episodes_fixture():
    records = [trial(i) for i in range(6)]
    return {"ep": records}


def pessimistic_estimator():
    return ControlledTableEstimator({s: 0.10 for s in all_subsets()})


def optimistic_estimator():
    return ControlledTableEstimator({s: 0.99 for s in all_subsets()})


def test_strict_mode_abstains_and_claims_nothing():
    loop = run_closed_loop(
        pessimistic_estimator(), 0.90, TAU, episodes_fixture(), mode=STRICT_SLO
    )
    assert all(not o.committed for o in loop.outcomes)
    assert all(o.selected_subset == () for o in loop.outcomes)
    assert all(o.execution_calls == 0 for o in loop.outcomes)
    assert all(not o.satisfied for o in loop.outcomes)
    summary = decompose(loop.outcomes)
    # An abstention is NOT a success under the primary metric.
    assert summary["success_over_all_trials"] == 0.0
    assert summary["commit_rate"] == 0.0


def test_best_effort_mode_executes_but_is_marked_unsatisfied():
    loop = run_closed_loop(
        pessimistic_estimator(), 0.90, TAU, episodes_fixture(),
        mode=SERVICE_BEST_EFFORT,
    )
    assert all(o.degradation_mode == "best-effort" for o in loop.outcomes)
    # Executed, so calls are charged...
    assert all(o.execution_calls > 0 for o in loop.outcomes)
    # ...but the target estimate is explicitly NOT claimed satisfied.
    assert all(o.predicted_feasible is False for o in loop.outcomes)
    summary = decompose(loop.outcomes)
    assert summary["false_feasible_count"] == 0


def test_strict_and_best_effort_metrics_are_not_mixed():
    strict = decompose(
        run_closed_loop(pessimistic_estimator(), 0.90, TAU, episodes_fixture(),
                        mode=STRICT_SLO).outcomes
    )
    best = decompose(
        run_closed_loop(pessimistic_estimator(), 0.90, TAU, episodes_fixture(),
                        mode=SERVICE_BEST_EFFORT).outcomes
    )
    assert strict["total_execution_calls"] == 0
    assert best["total_execution_calls"] > 0
    assert strict["success_over_all_trials"] != best["success_over_all_trials"]


def test_committed_policy_executes_only_selected_subset():
    loop = run_closed_loop(
        optimistic_estimator(), 0.90, TAU, episodes_fixture(), mode=STRICT_SLO
    )
    assert all(o.committed for o in loop.outcomes)
    # k*=1 with a uniform 0.99 table and cardinality cost.
    assert all(len(o.selected_subset) == 1 for o in loop.outcomes)
    assert all(o.execution_calls == 1 for o in loop.outcomes)


# ==========================================================================
# 12-16. oracle decomposition
# ==========================================================================


def test_oracle_feasible_definition():
    oracle = TrialOracle("ep", 0, {"local-a": False, "local-b": True, "local-c": False})
    assert oracle.oracle_feasible is True
    assert oracle.subset_succeeds(("local-a",)) is False
    assert oracle.subset_succeeds(("local-a", "local-b")) is True
    dead = TrialOracle("ep", 1, {p: False for p in PROVIDERS})
    assert dead.oracle_feasible is False


def test_error_classification():
    assert ClosedLoopOutcome("e", 0, False, (), False, "UNSAT", True).classify() == (
        UNNECESSARY_ABSTENTION
    )
    assert ClosedLoopOutcome("e", 1, False, (), False, "UNSAT", False).classify() == (
        CORRECT_ABSTENTION
    )
    assert ClosedLoopOutcome(
        "e", 2, True, ("local-a",), False, "SELECTED", True
    ).classify() == AVOIDABLE_MISS
    assert ClosedLoopOutcome(
        "e", 3, True, ("local-a",), False, "SELECTED", False
    ).classify() == INTRINSIC_MISS


def test_decomposition_counts_and_all_trials_denominator():
    outcomes = [
        ClosedLoopOutcome("e", 0, True, ("local-a",), True, "SELECTED", True,
                          predicted_feasible=True, execution_calls=1),
        ClosedLoopOutcome("e", 1, True, ("local-a",), False, "SELECTED", True,
                          predicted_feasible=True, execution_calls=1),
        ClosedLoopOutcome("e", 2, True, ("local-a",), False, "SELECTED", False,
                          predicted_feasible=True, execution_calls=1),
        ClosedLoopOutcome("e", 3, False, (), False, "UNSAT", True),
        ClosedLoopOutcome("e", 4, False, (), False, "UNSAT", False),
    ]
    d = decompose(outcomes)
    assert d["n"] == 5
    assert d["oracle_feasible_count"] == 3
    assert d["unnecessary_abstention_count"] == 1
    assert d["correct_abstention_count"] == 1
    assert d["avoidable_miss_count"] == 1
    assert d["intrinsic_miss_count"] == 1
    assert d["committed_success_count"] == 1
    # PRIMARY metric uses ALL trials, not just committed ones.
    assert d["success_over_all_trials"] == pytest.approx(1 / 5)
    assert d["conditional_success_given_commit"] == pytest.approx(1 / 3)
    assert d["false_feasible_count"] == 2


def test_v1_bounds_are_consistent_with_marginals():
    """V1 stored aggregates only; the joint must be reported as an interval."""
    bounds = bound_v1_decomposition(
        total=220, committed=72, committed_success=62, oracle_feasible=180
    )
    assert bounds["recoverable"] is False
    lo, hi = bounds["committed_and_oracle_feasible_bounds"]
    assert lo == 62 and hi == 72
    ulo, uhi = bounds["unnecessary_abstention_bounds"]
    assert ulo == 108 and uhi == 118
    assert bounds["committed_failure"] == 10


# ==========================================================================
# 17-21. splits, freeze, network
# ==========================================================================


def test_v1_and_v2_holdout_seeds_do_not_overlap():
    v1_holdout = {p.seed for p in plan_episodes("holdout", 10, 900_000, 22)}
    v2_train = {p.seed for p in plan_episodes("v2train", 22, 500_000, 22)}
    v2_val = {p.seed for p in plan_episodes("v2val", 9, 600_000, 22)}
    v2_holdout = {p.seed for p in plan_episodes("v2holdout", 10, 800_000, 22)}

    assert not v1_holdout & v2_holdout
    assert not v2_train & v2_val
    assert not v2_train & v2_holdout
    assert not v2_val & v2_holdout
    ids = {p.episode_id for p in plan_episodes("v2holdout", 10, 800_000, 22)}
    v1_ids = {p.episode_id for p in plan_episodes("holdout", 10, 900_000, 22)}
    assert not ids & v1_ids


def test_closed_loop_never_reveals_unselected_providers():
    """End-to-end audit of the runner's own history updates."""
    records = [
        trial(i, **{"local-c": obs("local-c", accepted=False, completion=999.0)})
        for i in range(5)
    ]
    loop = run_closed_loop(
        optimistic_estimator(), 0.90, TAU, {"ep": records},
        mode=STRICT_SLO, exploration_interval=None, collect_traces=5,
    )
    for tr in loop.traces:
        revealed = set(tr["executed_and_revealed_to_policy"])
        assert revealed == set(tr["selected_subset"])
        assert set(tr["withheld_from_policy"]) == set(PROVIDERS) - revealed
        # The evaluator still sees everything -- but only in the trace record.
        assert set(tr["oracle_full_outcome_evaluator_only"]) == set(PROVIDERS)


def test_no_public_network_constants():
    from avdr.learning.dataset import ADMIN_URLS

    assert all(u.startswith("http://127.0.0.1:") for u in ADMIN_URLS.values())
