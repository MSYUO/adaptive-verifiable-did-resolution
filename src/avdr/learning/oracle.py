"""Oracle decomposition of adaptive routing errors.

Separates the two questions a service operator actually cares about:

    Was a good answer AVAILABLE?      oracle_feasible_t
    Did the policy FIND it?           committed / satisfied

An `oracle_feasible` trial is one where at least one candidate provider
actually produced an acceptable response within tau. It is computed from the
EVALUATOR ground truth and is never visible to the estimator or optimizer.

Error classes:

    UNNECESSARY_ABSTENTION  policy said unsatisfiable, but a provider would
                            have delivered.  (Cost: an avoidable refusal.)
    CORRECT_ABSTENTION      policy said unsatisfiable, and nothing would have
                            delivered.  (Refusing was right.)
    AVOIDABLE_MISS          policy executed a subset that failed, while some
                            OTHER available provider would have succeeded.
                            (Cost: a wrong choice.)
    INTRINSIC_MISS          policy executed and failed, and nothing could have
                            succeeded.  (Not the policy's fault.)

Keeping these apart matters because a high failure rate caused by an
impossible environment and one caused by bad selection demand opposite fixes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

UNNECESSARY_ABSTENTION = "unnecessary_abstention"
CORRECT_ABSTENTION = "correct_abstention"
AVOIDABLE_MISS = "avoidable_miss"
INTRINSIC_MISS = "intrinsic_miss"
COMMITTED_SUCCESS = "committed_success"


@dataclass
class TrialOracle:
    """Per-trial evaluator view. Never reaches the model."""

    episode_id: str
    trial_index: int
    # provider -> did it deliver an acceptable response within tau?
    provider_within_deadline: dict[str, bool] = field(default_factory=dict)

    @property
    def oracle_feasible(self) -> bool:
        return any(self.provider_within_deadline.values())

    def subset_succeeds(self, subset: Iterable[str]) -> bool:
        return any(self.provider_within_deadline.get(p, False) for p in subset)


@dataclass
class ClosedLoopOutcome:
    """What the deployed policy did on one trial, plus its oracle context."""

    episode_id: str
    trial_index: int
    committed: bool
    selected_subset: tuple[str, ...]
    satisfied: bool
    status: str
    oracle_feasible: bool
    predicted_feasible: bool | None = None
    degradation_mode: str | None = None
    execution_calls: int = 0
    exploration_calls: int = 0

    @property
    def total_calls(self) -> int:
        return self.execution_calls + self.exploration_calls

    def classify(self) -> str:
        if not self.committed:
            return (
                UNNECESSARY_ABSTENTION if self.oracle_feasible else CORRECT_ABSTENTION
            )
        if self.satisfied:
            return COMMITTED_SUCCESS
        return AVOIDABLE_MISS if self.oracle_feasible else INTRINSIC_MISS


def decompose(outcomes: Sequence[ClosedLoopOutcome]) -> dict:
    """Full decomposition over ALL logical trials.

    Every rate here uses ALL trials as the denominator. `conditional_success
    _given_commit` is reported separately and only as a diagnostic, because a
    denominator of "trials the policy chose to answer" flatters any policy
    that abstains on the hard ones.
    """
    n = len(outcomes)
    if n == 0:
        return {"n": 0}

    classes = [o.classify() for o in outcomes]
    committed = [o for o in outcomes if o.committed]
    abstained = [o for o in outcomes if not o.committed]
    oracle_feasible = [o for o in outcomes if o.oracle_feasible]
    satisfied = [o for o in outcomes if o.satisfied]

    def rate(count: int, denominator: int) -> float | None:
        return round(count / denominator, 6) if denominator else None

    counts = {name: classes.count(name) for name in (
        COMMITTED_SUCCESS, AVOIDABLE_MISS, INTRINSIC_MISS,
        UNNECESSARY_ABSTENTION, CORRECT_ABSTENTION,
    )}
    false_feasible = [
        o for o in committed if o.predicted_feasible is True and not o.satisfied
    ]

    return {
        "n": n,
        "oracle_feasible_count": len(oracle_feasible),
        "oracle_infeasible_count": n - len(oracle_feasible),
        "oracle_feasible_rate": rate(len(oracle_feasible), n),
        "commit_count": len(committed),
        "commit_rate": rate(len(committed), n),
        "abstain_count": len(abstained),
        # PRIMARY service metric: successes over ALL logical requests.
        "success_over_all_trials": rate(len(satisfied), n),
        # Diagnostic only -- never the headline.
        "conditional_success_given_commit": rate(len(satisfied), len(committed)),
        "committed_and_oracle_feasible": sum(
            1 for o in committed if o.oracle_feasible
        ),
        "committed_and_oracle_infeasible": sum(
            1 for o in committed if not o.oracle_feasible
        ),
        "abstained_and_oracle_feasible": sum(
            1 for o in abstained if o.oracle_feasible
        ),
        "abstained_and_oracle_infeasible": sum(
            1 for o in abstained if not o.oracle_feasible
        ),
        "committed_success_count": counts[COMMITTED_SUCCESS],
        "avoidable_miss_count": counts[AVOIDABLE_MISS],
        "intrinsic_miss_count": counts[INTRINSIC_MISS],
        "unnecessary_abstention_count": counts[UNNECESSARY_ABSTENTION],
        "correct_abstention_count": counts[CORRECT_ABSTENTION],
        "avoidable_miss_rate": rate(counts[AVOIDABLE_MISS], n),
        "intrinsic_miss_rate": rate(counts[INTRINSIC_MISS], n),
        "unnecessary_abstention_rate": rate(counts[UNNECESSARY_ABSTENTION], n),
        "correct_abstention_rate": rate(counts[CORRECT_ABSTENTION], n),
        "false_feasible_count": len(false_feasible),
        "false_feasible_rate": rate(len(false_feasible), len(committed)),
        "mean_selected_k": round(
            sum(len(o.selected_subset) for o in committed) / len(committed), 4
        )
        if committed
        else None,
        "mean_execution_calls": round(sum(o.execution_calls for o in outcomes) / n, 4),
        "mean_exploration_calls": round(
            sum(o.exploration_calls for o in outcomes) / n, 4
        ),
        "mean_total_calls": round(sum(o.total_calls for o in outcomes) / n, 4),
        "total_execution_calls": sum(o.execution_calls for o in outcomes),
        "total_exploration_calls": sum(o.exploration_calls for o in outcomes),
        "total_provider_calls": sum(o.total_calls for o in outcomes),
    }


def bound_v1_decomposition(
    total: int, committed: int, committed_success: int, oracle_feasible: int
) -> dict:
    """Exact interval bounds when only marginals were persisted.

    Used for the V1 audit: that run stored aggregates, not per-trial records,
    so the joint distribution is not recoverable. A committed success implies
    oracle feasibility, which pins one side of each interval.
    """
    committed_fail = committed - committed_success
    abstained = total - committed
    cf_lo = committed_success
    cf_hi = min(committed, oracle_feasible)
    return {
        "recoverable": False,
        "reason": (
            "the V1 holdout report persisted aggregate metrics only; per-trial "
            "records were not written, so the joint distribution of "
            "(committed, oracle_feasible) cannot be recovered exactly"
        ),
        "total_trials": total,
        "oracle_feasible": oracle_feasible,
        "oracle_infeasible": total - oracle_feasible,
        "committed": committed,
        "committed_success": committed_success,
        "committed_failure": committed_fail,
        "abstained": abstained,
        "committed_and_oracle_feasible_bounds": [cf_lo, cf_hi],
        "committed_and_oracle_infeasible_bounds": [
            max(0, committed - cf_hi), committed - cf_lo
        ],
        "unnecessary_abstention_bounds": [
            max(0, oracle_feasible - cf_hi), oracle_feasible - cf_lo
        ],
        "correct_abstention_bounds": [
            abstained - (oracle_feasible - cf_lo), abstained - max(0, oracle_feasible - cf_hi)
        ],
        "avoidable_miss_bounds": [max(0, cf_lo - committed_success), committed_fail],
        "intrinsic_miss_bounds": [0, committed_fail],
    }
