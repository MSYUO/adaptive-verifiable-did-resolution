"""Layer A -- ESTIMATION.

Provides q_hat(S | x): the estimated probability that resolver SUBSET S
returns a structurally acceptable result before the configured deadline.

Two rules define this module.

1. SUBSETS, NOT PROVIDERS. The interface is defined over subsets because
   subset success is not generally a function of per-provider probabilities.
   There is deliberately NO composition rule here -- in particular

       q_hat(S) = 1 - prod_i (1 - p_i)

   is NOT implemented, is NOT used as a fallback, and must not be added until
   a future milestone establishes and validates an independence model. A
   subset with no estimate is reported as unknown, never synthesised.

2. NOTHING HERE IS A MEASUREMENT. The only estimator in this milestone is a
   deterministic table supplied as [CONTROLLED TEST INPUT]. It exists solely
   to prove that the optimizer selects correctly. No model is trained, and no
   value here describes real DID infrastructure.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping

from ..provenance import config_hash

# A subset is identified by its sorted provider ids, so {a,b} and {b,a} are
# the same key.
SubsetKey = tuple[str, ...]


def subset_key(providers: Iterable[str]) -> SubsetKey:
    return tuple(sorted(providers))


class EstimatorError(Exception):
    """Raised when an estimator produces something that cannot be a probability."""


def validate_probability(value: Any, label: str) -> float:
    """Reject anything that is not a real probability. Never clamps.

    Silently clamping an impossible value would hide an estimator defect
    behind a plausible-looking number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EstimatorError(f"{label} is not a number: {value!r}")
    value = float(value)
    if math.isnan(value):
        raise EstimatorError(f"{label} is NaN")
    if math.isinf(value):
        raise EstimatorError(f"{label} is infinite")
    if value < 0.0 or value > 1.0:
        raise EstimatorError(f"{label} is outside [0, 1]: {value}")
    return value


class SubsetEstimator(ABC):
    """Estimates q_hat(S | x) for a resolver subset S."""

    estimator_id: str = "abstract"
    estimator_version: str = "0"

    @abstractmethod
    def estimate(self, subset: SubsetKey, context: Mapping[str, Any] | None = None):
        """Return q_hat in [0, 1], or None if this subset has no estimate.

        Returning None is the honest answer for an uncovered subset. It must
        never be replaced by a composed or interpolated value.
        """

    @abstractmethod
    def config_hash(self) -> str:
        """Hash binding a dataset to the exact estimator configuration used."""

    def describe(self) -> dict:
        return {
            "estimator_id": self.estimator_id,
            "estimator_version": self.estimator_version,
            "estimator_config_hash": self.config_hash(),
        }


class ControlledTableEstimator(SubsetEstimator):
    """Deterministic lookup table. [CONTROLLED TEST INPUT], not a prediction.

    Every entry is a value we chose in order to give the optimizer a known
    correct answer. The table is exhaustive by construction for the subsets it
    covers; anything absent returns None rather than being derived.
    """

    estimator_id = "controlled-table"
    estimator_version = "v1"

    def __init__(
        self,
        table: Mapping[Iterable[str], float],
        label: str = "controlled",
    ) -> None:
        self.label = label
        self._table: dict[SubsetKey, float] = {}
        for providers, value in table.items():
            key = subset_key(providers)
            if not key:
                raise EstimatorError("the empty subset has no q_hat")
            if key in self._table:
                raise EstimatorError(f"duplicate subset entry for {key}")
            self._table[key] = validate_probability(value, f"q_hat({','.join(key)})")

    def estimate(self, subset: SubsetKey, context: Mapping[str, Any] | None = None):
        return self._table.get(subset_key(subset))

    def coverage(self) -> set[SubsetKey]:
        return set(self._table)

    def config_hash(self) -> str:
        return config_hash(
            {
                "estimator_id": self.estimator_id,
                "estimator_version": self.estimator_version,
                "label": self.label,
                # Sorted for a stable hash regardless of insertion order.
                "table": sorted(
                    ([list(k), v] for k, v in self._table.items()),
                    key=lambda row: row[0],
                ),
            }
        )

    def describe(self) -> dict:
        return {
            **super().describe(),
            "label": self.label,
            "covered_subset_count": len(self._table),
            "note": (
                "CONTROLLED TEST INPUT. Deterministic table used to qualify "
                "the optimizer. Not a measurement and not a prediction about "
                "any real DID resolver."
            ),
        }
