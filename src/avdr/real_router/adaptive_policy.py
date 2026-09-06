"""The `adaptive-min-set` policy: proposed service logic, not a baseline.

Orchestrates the three layers without absorbing any of them:

    candidates (capability + budget layer, already existing)
        -> estimator.estimate(subset)        [layer A]
        -> optimizer.select(...)             [layer B]
        -> RealRoutingPlan (concurrent)      [layer C executes it]

The optimizer's mathematics live in `avdr.adaptive.optimizer`; this module
only wires layers together and converts a planning failure into a typed
condition. The existing baselines are untouched -- this policy is added
alongside them, never in place of them.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..adaptive.estimator import SubsetEstimator
from ..adaptive.optimizer import (
    SELECTED,
    CostModel,
    MinimumSetOptimizer,
    OptimizerResult,
)
from ..inventory import ProviderEntry
from .policies import CONCURRENT, RealRoutingPlan, RealRoutingPolicy


class AdaptivePlanningError(Exception):
    """Planning could not produce an executable subset.

    Carries the full optimizer result so the caller can explain exactly what
    was evaluated and why nothing was chosen -- never just "failed".
    """

    def __init__(self, result: OptimizerResult) -> None:
        super().__init__(result.selection_reason or result.status)
        self.result = result
        self.status = result.status


class RealAdaptiveMinSet(RealRoutingPolicy):
    """Select the minimum-cost subset predicted to meet the target SLO."""

    name = "adaptive-min-set"
    execution = CONCURRENT
    # One provider is a legitimate adaptive answer (k*=1); the point of the
    # policy is that k is chosen, not fixed.
    min_providers = 1

    def __init__(
        self,
        estimator: SubsetEstimator,
        default_target: float = 0.99,
        cost_model: CostModel | None = None,
        optimizer: MinimumSetOptimizer | None = None,
        allow_best_effort: bool = False,
    ) -> None:
        self.estimator = estimator
        self.default_target = default_target
        self.optimizer = optimizer or MinimumSetOptimizer(cost_model=cost_model)
        self.allow_best_effort = allow_best_effort

    def plan(
        self,
        candidates: list[ProviderEntry],
        did: str,
        target_probability: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> RealRoutingPlan:
        target = self.default_target if target_probability is None else target_probability
        result = self.optimizer.select(
            candidates=[p.id for p in candidates],
            estimator=self.estimator,
            target_probability=target,
            context=context,
            allow_best_effort=self.allow_best_effort,
        )

        if result.status != SELECTED or not result.selected_subset:
            raise AdaptivePlanningError(result)

        # Execute ONLY the selected subset, concurrently, first-acceptable.
        return RealRoutingPlan(
            policy=self.name,
            provider_order=list(result.selected_subset),
            execution=CONCURRENT,
            max_attempts=len(result.selected_subset),
            min_providers=self.min_providers,
            target=None,
            metadata={
                **result.summary(),
                **self.estimator.describe(),
                "cost_model": self.optimizer.cost_model.describe(),
            },
        )
