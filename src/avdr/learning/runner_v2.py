"""Closed-loop V2 runner: partial feedback, strict vs best-effort modes.

Per trial, strictly in this order:

    deployment history through t-1
      -> pre-request features (deployment history ONLY)
      -> q_hat(S | x_t) for every subset
      -> complete-coverage check
      -> optimizer -> S_t
      -> execute / reveal ONLY S_t to the deployment policy
      -> update deployment history from attempted providers only

The full trial outcome goes to the evaluator alone, for oracle scoring. The
policy never updates itself from a provider it did not call.

EXECUTION MODEL. Ground-truth trials are measured once against the local mock
providers, then each policy is replayed against those same recorded trials,
seeing only what it selected. This is what makes matched-trial comparison
possible (§14) and keeps partial feedback faithful: a policy still learns only
from its own choices. Provider-call accounting therefore reports what each
POLICY would have called, not the evaluator's collection cost, which is an
apparatus cost and is reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..adaptive.optimizer import SELECTED, MinimumSetOptimizer
from .closedloop import (
    SCHEDULED_EXPLORATION,
    SELECTED_EXECUTION,
    SERVICE_BEST_EFFORT,
    STRICT_SLO,
    DeploymentObservedHistory,
    build_partial_context,
    reveal_subset,
    should_explore,
    update_subset_history,
)
from .dataset import all_subsets, subset_target
from .environment import PROVIDERS
from .estimators import HISTORY_KEY
from .features import TrialRecord
from .oracle import ClosedLoopOutcome, TrialOracle

CONTEXT_KEY_V2 = "partial_context"


@dataclass
class ClosedLoopResult:
    outcomes: list[ClosedLoopOutcome] = field(default_factory=list)
    y_true: list[int] = field(default_factory=list)
    y_prob: list[float] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)
    unknown_subset_updates: int = 0
    known_subset_updates: int = 0


def oracle_for(record: TrialRecord, tau_ms: float) -> TrialOracle:
    return TrialOracle(
        episode_id=record.episode_id,
        trial_index=record.trial_index,
        provider_within_deadline={
            p: record.within_deadline(p, tau_ms) for p in PROVIDERS
        },
    )


def run_closed_loop(
    estimator,
    target: float,
    tau_ms: float,
    episodes: dict[str, list[TrialRecord]],
    mode: str = STRICT_SLO,
    exploration_interval: int | None = None,
    optimizer: MinimumSetOptimizer | None = None,
    collect_traces: int = 0,
) -> ClosedLoopResult:
    optimizer = optimizer or MinimumSetOptimizer()
    subsets = all_subsets()
    result = ClosedLoopResult()

    for episode_id, records in episodes.items():
        deployment = DeploymentObservedHistory()
        subset_history: dict[tuple[str, ...], list[int]] = {}
        counter = 0

        for record in sorted(records, key=lambda r: r.trial_index):
            if not record.complete:
                continue
            counter += 1

            # ---- 1. features from the deployment history only -------------
            context = build_partial_context(deployment, tau_ms, record.trial_index)
            payload = {CONTEXT_KEY_V2: context, HISTORY_KEY: subset_history}

            # ---- 2. predict every subset (scored offline by evaluator) ----
            predictions = {s: estimator.estimate(s, payload) for s in subsets}

            # ---- 3+4. coverage check and optimize -------------------------
            plan = optimizer.select(
                candidates=list(PROVIDERS),
                estimator=estimator,
                target_probability=target,
                context=payload,
            )

            committed = plan.status == SELECTED and bool(plan.selected_subset)
            degradation = None
            predicted_feasible: bool | None = None

            if committed:
                selected = tuple(sorted(plan.selected_subset))
                predicted_feasible = True
            elif mode == SERVICE_BEST_EFFORT:
                # Explicit fallback: highest-estimated subset. Marked as NOT
                # satisfying the target.
                fallback = plan.best_subset or list(PROVIDERS)
                selected = tuple(sorted(fallback))
                committed = True
                degradation = "best-effort"
                predicted_feasible = False
            else:
                selected = ()
                predicted_feasible = False

            # ---- 5. execute; reveal ONLY the selected providers -----------
            oracle = oracle_for(record, tau_ms)
            satisfied = bool(selected) and oracle.subset_succeeds(selected)
            execution_calls = len(selected)

            explore = should_explore(counter, exploration_interval)
            exploration_calls = len(PROVIDERS) if explore else 0

            result.outcomes.append(
                ClosedLoopOutcome(
                    episode_id=episode_id,
                    trial_index=record.trial_index,
                    committed=committed,
                    selected_subset=selected,
                    satisfied=satisfied,
                    status=plan.status,
                    oracle_feasible=oracle.oracle_feasible,
                    predicted_feasible=predicted_feasible,
                    degradation_mode=degradation,
                    execution_calls=execution_calls,
                    exploration_calls=exploration_calls,
                )
            )

            # ---- evaluator-only scoring of the estimator ------------------
            for subset in subsets:
                if predictions[subset] is not None:
                    result.y_true.append(subset_target(record, subset, tau_ms))
                    result.y_prob.append(predictions[subset])

            if len(result.traces) < collect_traces:
                result.traces.append(
                    _trace(
                        episode_id, record, deployment, context, predictions,
                        plan, selected, satisfied, oracle, tau_ms, explore, mode,
                    )
                )

            # ---- 6. update deployment history: attempted providers ONLY ---
            if selected:
                deployment.append(
                    reveal_subset(record, selected, SELECTED_EXECUTION)
                )
            if explore:
                # Exploration is a REAL request and is charged as such.
                deployment.append(
                    reveal_subset(record, PROVIDERS, SCHEDULED_EXPLORATION)
                )

            observed = set(selected) | (set(PROVIDERS) if explore else set())
            within = {p: oracle.provider_within_deadline[p] for p in observed}
            known = update_subset_history(subset_history, observed, within, subsets)
            result.known_subset_updates += known
            result.unknown_subset_updates += len(subsets) - known

    return result


def _trace(episode_id, record, deployment, context, predictions, plan,
           selected, satisfied, oracle, tau_ms, explore, mode) -> dict:
    return {
        "episode_id": episode_id,
        "trial_index": record.trial_index,
        "mode": mode,
        "deployment_history_len_before": len(deployment),
        "observed_history_per_provider": {
            p: {
                "trials_since_observed": context.per_provider[p]["trials_since_observed"],
                "observation_count": context.per_provider[p]["observation_count"],
                "recent_window_count": context.per_provider[p][
                    "recent_observation_window_count"
                ],
                "within_deadline_rate": context.per_provider[p]["within_deadline_rate"],
            }
            for p in PROVIDERS
        },
        "predictions": {
            ",".join(k): round(v, 5) for k, v in predictions.items() if v is not None
        },
        "coverage_exact": plan.exact,
        "optimizer_status": plan.status,
        "selected_subset": list(selected),
        "estimated_subset_success": plan.estimated_subset_success,
        "exploration_this_trial": explore,
        "executed_and_revealed_to_policy": list(selected)
        + (list(PROVIDERS) if explore else []),
        "withheld_from_policy": sorted(
            set(PROVIDERS) - set(selected) - (set(PROVIDERS) if explore else set())
        ),
        "oracle_full_outcome_evaluator_only": {
            p: oracle.provider_within_deadline[p] for p in PROVIDERS
        },
        "satisfied": satisfied,
        "oracle_feasible": oracle.oracle_feasible,
        "hidden_state_audit_only": record.hidden_state,
    }
