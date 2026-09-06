"""Layer B -- OPTIMIZATION.

Selects the minimum-cost resolver subset predicted to satisfy an SLO:

    S*(x) = argmin_{S subseteq G(x)}  C(S)
    subject to                        q_hat(S | x) >= target

with the initial cost

    C(S) = |S|                                        [DESIGN CHOICE]

Cardinality is a stand-in for request burden. It has NOT been validated as an
economic model, so cost is expressed through a `CostModel` interface that
later work can replace with API cost, resource cost or a weighted mix without
touching this optimizer.

This module performs no I/O and knows nothing about HTTP, providers'
endpoints, or acceptance mechanics. It consumes estimates and returns a
decision.

Honesty rules enforced here:
  * If no subset meets the target, that is reported as
    SLO_ESTIMATE_UNSATISFIABLE. The optimizer never quietly falls back to
    "call everything" and then claims the SLO was met.
  * Subsets the estimator cannot score are reported, never imputed, and by
    default they BLOCK the exact-minimum claim entirely (see below).
  * Selection is fully deterministic; ties are broken by a fixed documented
    rule, never at random.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Mapping, Sequence

from .estimator import SubsetEstimator, SubsetKey, subset_key, validate_probability

OPTIMIZER_VERSION = "exact-enumeration-v1"

# [DESIGN CHOICE] Exact enumeration evaluates 2^M - 1 subsets, so the
# candidate count must stay small. Beyond this bound the optimizer refuses the
# request rather than silently truncating the provider set or switching to an
# unvalidated approximation.
MAX_ADAPTIVE_CANDIDATES = 12

# Planning statuses.
SELECTED = "SELECTED"
SLO_ESTIMATE_UNSATISFIABLE = "SLO_ESTIMATE_UNSATISFIABLE"
ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED = "ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED"
NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
ESTIMATOR_COVERAGE_INCOMPLETE = "ESTIMATOR_COVERAGE_INCOMPLETE"

# Coverage modes.
#
# REQUIRE_COMPLETE (default): every non-empty subset of the candidate set must
# have a valid estimate. Anything less cannot support an exact-minimum claim.
#
#   Counterexample that motivates this. target = 0.95,
#       q({A})   = unknown
#       q({B})   = 0.80
#       q({A,B}) = 0.99
#   Selecting {A,B} is NOT minimum: the unknown q({A}) could be >= 0.95, in
#   which case the true minimum is the cheaper {A}. Optimising over "the
#   subsets we happen to know" silently answers a different question.
#
# PARTIAL_BEST_KNOWN: opt-in, returns the cheapest subset among those that
# were estimated. Its result is explicitly NOT minimum, NOT optimal and NOT
# SLO-guaranteed, and the wording in `selection_reason` says so.
REQUIRE_COMPLETE = "require-complete"
PARTIAL_BEST_KNOWN = "partial-best-known"

# Tie-break rule, fixed before execution. Documented in README.
TIE_BREAK_RULE = (
    "1) lowest cost; 2) highest q_hat; "
    "3) lexicographic provider-id order. "
    "Criterion 'lowest predicted accepted latency' is defined in the ordering "
    "but SKIPPED in this milestone because no latency predictor exists."
)


class CostModel(ABC):
    cost_model_id: str = "abstract"

    @abstractmethod
    def cost(self, subset: SubsetKey) -> float:
        ...

    def describe(self) -> dict:
        return {"cost_model_id": self.cost_model_id}


class CardinalityCost(CostModel):
    """C(S) = |S|. [DESIGN CHOICE], not a validated economic model."""

    cost_model_id = "cardinality-v1"

    def cost(self, subset: SubsetKey) -> float:
        return float(len(subset))


class WeightedCost(CostModel):
    """C(S) = sum of per-provider weights.

    Present to demonstrate that the optimizer is cost-agnostic; no weights are
    claimed to reflect real API or resource cost.
    """

    cost_model_id = "weighted-v1"

    def __init__(self, weights: Mapping[str, float], default: float = 1.0) -> None:
        self.weights = dict(weights)
        self.default = default

    def cost(self, subset: SubsetKey) -> float:
        return float(sum(self.weights.get(p, self.default) for p in subset))

    def describe(self) -> dict:
        return {
            "cost_model_id": self.cost_model_id,
            "weights": self.weights,
            "default": self.default,
        }


@dataclass
class SubsetEvaluation:
    subset: SubsetKey
    q_hat: float | None
    cost: float | None
    feasible: bool

    def to_dict(self) -> dict:
        return {
            "subset": list(self.subset),
            "q_hat": self.q_hat,
            "cost": self.cost,
            "feasible": self.feasible,
        }


@dataclass
class OptimizerResult:
    status: str
    target_probability: float
    candidate_providers: list[str]
    candidate_count: int
    evaluated_subset_count: int
    unestimated_subset_count: int = 0

    coverage_mode: str = REQUIRE_COMPLETE
    expected_subset_count: int = 0
    estimated_subset_count: int = 0
    missing_subsets: list[list[str]] = field(default_factory=list)
    # True only when every non-empty subset had a valid estimate. An exact
    # minimum may be claimed only when this is True.
    exact: bool = False

    selected_subset: list[str] | None = None
    selected_subset_size: int | None = None
    selected_cost: float | None = None
    estimated_subset_success: float | None = None
    selection_reason: str = ""

    # Best subset found even when infeasible, so a caller can see how far off
    # the estimate was rather than only "unsatisfiable".
    best_subset: list[str] | None = None
    best_probability: float | None = None

    best_effort: bool = False
    optimizer_version: str = OPTIMIZER_VERSION
    cost_model_id: str = CardinalityCost.cost_model_id
    tie_break_rule: str = TIE_BREAK_RULE
    evaluations: list[SubsetEvaluation] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        """True only when a subset actually met the target estimate."""
        return self.status == SELECTED and not self.best_effort

    def summary(self) -> dict:
        return {
            "status": self.status,
            "target_slo_probability": self.target_probability,
            "candidate_providers": self.candidate_providers,
            "candidate_count": self.candidate_count,
            "evaluated_subset_count": self.evaluated_subset_count,
            "unestimated_subset_count": self.unestimated_subset_count,
            "coverage_mode": self.coverage_mode,
            "expected_subset_count": self.expected_subset_count,
            "estimated_subset_count": self.estimated_subset_count,
            "missing_subsets": self.missing_subsets,
            "exact": self.exact,
            "selected_subset": self.selected_subset,
            "selected_subset_size": self.selected_subset_size,
            "selection_cost": self.selected_cost,
            "estimated_subset_success": self.estimated_subset_success,
            "selection_reason": self.selection_reason,
            "best_subset": self.best_subset,
            "best_probability": self.best_probability,
            "best_effort": self.best_effort,
            "optimizer_version": self.optimizer_version,
            "cost_model_id": self.cost_model_id,
            "tie_break_rule": self.tie_break_rule,
        }


def validate_target(target: Any) -> float:
    """0 < target <= 1. Rejects NaN, infinity, zero and negatives."""
    value = validate_probability(target, "target_slo_probability")
    if value <= 0.0:
        raise ValueError("target_slo_probability must be greater than 0")
    return value


class MinimumSetOptimizer:
    """Exact enumeration over non-empty subsets of the candidate set."""

    version = OPTIMIZER_VERSION

    def __init__(
        self,
        cost_model: CostModel | None = None,
        max_candidates: int = MAX_ADAPTIVE_CANDIDATES,
        coverage_mode: str = REQUIRE_COMPLETE,
    ) -> None:
        self.cost_model = cost_model or CardinalityCost()
        self.max_candidates = max_candidates
        if coverage_mode not in (REQUIRE_COMPLETE, PARTIAL_BEST_KNOWN):
            raise ValueError(f"unknown coverage_mode {coverage_mode!r}")
        self.coverage_mode = coverage_mode

    def select(
        self,
        candidates: Sequence[str],
        estimator: SubsetEstimator,
        target_probability: float,
        context: Mapping[str, Any] | None = None,
        allow_best_effort: bool = False,
    ) -> OptimizerResult:
        target = validate_target(target_probability)
        providers = sorted(candidates)

        if not providers:
            return OptimizerResult(
                status=NO_ELIGIBLE_CANDIDATES,
                target_probability=target,
                candidate_providers=[],
                candidate_count=0,
                evaluated_subset_count=0,
                selection_reason="no eligible candidate providers",
                cost_model_id=self.cost_model.cost_model_id,
            )

        if len(providers) > self.max_candidates:
            # Refuse rather than truncate: dropping providers would silently
            # change the problem being solved.
            return OptimizerResult(
                status=ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED,
                target_probability=target,
                candidate_providers=providers,
                candidate_count=len(providers),
                evaluated_subset_count=0,
                selection_reason=(
                    f"{len(providers)} candidates exceeds the exact-enumeration "
                    f"limit of {self.max_candidates}; refusing rather than "
                    f"truncating the provider set or approximating"
                ),
                cost_model_id=self.cost_model.cost_model_id,
            )

        expected_subset_count = 2 ** len(providers) - 1
        evaluations: list[SubsetEvaluation] = []
        unestimated = 0
        missing: list[list[str]] = []

        for size in range(1, len(providers) + 1):
            for combo in combinations(providers, size):
                key: SubsetKey = subset_key(combo)
                raw = estimator.estimate(key, context)
                if raw is None:
                    # Unknown stays unknown. No composition rule is applied.
                    unestimated += 1
                    missing.append(list(key))
                    evaluations.append(SubsetEvaluation(key, None, None, False))
                    continue
                q_hat = validate_probability(raw, f"q_hat({','.join(key)})")
                cost = self.cost_model.cost(key)
                evaluations.append(
                    SubsetEvaluation(key, q_hat, cost, q_hat >= target)
                )

        # Sanity bound: non-empty subsets of M providers number 2^M - 1.
        assert len(evaluations) <= 2 ** len(providers) - 1

        scored = [e for e in evaluations if e.q_hat is not None]
        feasible = [e for e in scored if e.feasible]

        best = max(scored, key=lambda e: (e.q_hat, -e.cost)) if scored else None

        result = OptimizerResult(
            status=SELECTED,
            target_probability=target,
            candidate_providers=providers,
            candidate_count=len(providers),
            evaluated_subset_count=len(evaluations),
            unestimated_subset_count=unestimated,
            coverage_mode=self.coverage_mode,
            expected_subset_count=expected_subset_count,
            estimated_subset_count=len(scored),
            missing_subsets=missing,
            exact=not missing,
            best_subset=list(best.subset) if best else None,
            best_probability=best.q_hat if best else None,
            optimizer_version=self.version,
            cost_model_id=self.cost_model.cost_model_id,
            evaluations=evaluations,
        )

        # ---- coverage gate -------------------------------------------------
        # An exact minimum cannot be claimed while any subset is unscored: an
        # unknown cheaper subset might have satisfied the target.
        if missing and self.coverage_mode == REQUIRE_COMPLETE:
            result.status = ESTIMATOR_COVERAGE_INCOMPLETE
            result.selection_reason = (
                f"{len(missing)} of {expected_subset_count} non-empty subsets "
                f"have no estimate ({missing[:4]}"
                f"{'...' if len(missing) > 4 else ''}); refusing to claim an "
                f"exact minimum, because an unscored cheaper subset could "
                f"itself have satisfied the target"
            )
            return result

        if not feasible:
            result.status = SLO_ESTIMATE_UNSATISFIABLE
            result.selection_reason = (
                f"no subset reached the target estimate of {target}; best "
                f"estimated subset {result.best_subset} at "
                f"{result.best_probability}"
            )
            if allow_best_effort and best is not None:
                # Degradation is permitted but must never be presented as
                # meeting the target.
                result.status = SELECTED
                result.best_effort = True
                result.selected_subset = list(providers)
                result.selected_subset_size = len(providers)
                result.selected_cost = self.cost_model.cost(subset_key(providers))
                result.estimated_subset_success = next(
                    (e.q_hat for e in scored if e.subset == subset_key(providers)),
                    None,
                )
                result.selection_reason = (
                    "BEST-EFFORT degradation: target "
                    f"{target} is not reachable by any subset; executing all "
                    "eligible providers. This does NOT satisfy the target."
                )
            return result

        # Deterministic selection. See TIE_BREAK_RULE.
        chosen = min(feasible, key=lambda e: (e.cost, -e.q_hat, e.subset))
        partial = bool(missing) and self.coverage_mode == PARTIAL_BEST_KNOWN
        tied = [
            e for e in feasible if e.cost == chosen.cost and e.q_hat == chosen.q_hat
        ]

        result.selected_subset = list(chosen.subset)
        result.selected_subset_size = len(chosen.subset)
        result.selected_cost = chosen.cost
        result.estimated_subset_success = chosen.q_hat
        tie_note = (
            f"; {len(tied)} subsets tied on cost and q_hat, broken "
            f"lexicographically"
            if len(tied) > 1
            else ""
        )
        if partial:
            # Deliberately avoids the words minimum / optimal / guaranteed.
            result.selection_reason = (
                f"PARTIAL-COVERAGE BEST KNOWN subset meeting target {target}: "
                f"cost={chosen.cost}, q_hat={chosen.q_hat}. "
                f"{len(missing)} of {expected_subset_count} subsets were "
                f"unscored, so this is the cheapest subset AMONG THOSE "
                f"ESTIMATED. It is not established as the smallest satisfying "
                f"subset and carries no SLO assurance" + tie_note
            )
        else:
            result.selection_reason = (
                f"minimum-cost subset meeting target {target}: cost={chosen.cost}, "
                f"q_hat={chosen.q_hat}" + tie_note
            )
        return result
