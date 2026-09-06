"""Adaptive minimum-set decision engine.

Three deliberately separated layers:

    estimator.py   q_hat(S | x)          -- what we believe
    optimizer.py   S* = argmin C(S)      -- what we choose
    (execution stays in real_router/)    -- what we do

Estimation never performs optimization, optimization never performs I/O, and
execution never re-derives either.

Nothing in this package is trained, fitted or measured. The only estimator is
a deterministic table supplied as CONTROLLED TEST INPUT so the optimizer's
correctness can be qualified.
"""

from .estimator import (  # noqa: F401
    ControlledTableEstimator,
    EstimatorError,
    SubsetEstimator,
    subset_key,
    validate_probability,
)
from .optimizer import (  # noqa: F401
    ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED,
    MAX_ADAPTIVE_CANDIDATES,
    NO_ELIGIBLE_CANDIDATES,
    OPTIMIZER_VERSION,
    SELECTED,
    SLO_ESTIMATE_UNSATISFIABLE,
    TIE_BREAK_RULE,
    CardinalityCost,
    CostModel,
    MinimumSetOptimizer,
    OptimizerResult,
    WeightedCost,
    validate_target,
)
