"""Probability-quality and decision-quality metrics.

The optimizer consumes probabilities, so classification accuracy alone is
insufficient: a model can rank well and still be badly calibrated, which would
make the SLO constraint meaningless. Brier score, log loss and a reliability
summary are therefore the primary estimator metrics.

ECE binning convention, fixed in advance [DESIGN CHOICE]: 10 equal-width bins
over [0, 1], bin i = (i/10, (i+1)/10], with the first bin closed at 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

ECE_BINS = 10
LOG_LOSS_EPSILON = 1e-6


def brier_score(y_true: Sequence[int], y_prob: Sequence[float]) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_prob, dtype=float)
    return float(np.mean((p - y) ** 2))


def log_loss(y_true: Sequence[int], y_prob: Sequence[float]) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.clip(np.asarray(y_prob, dtype=float), LOG_LOSS_EPSILON, 1 - LOG_LOSS_EPSILON)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(y_true: Sequence[int], y_prob: Sequence[float], bins: int = ECE_BINS):
    """Equal-width reliability table plus expected calibration error."""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_prob, dtype=float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    table = []
    ece = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p > lo) & (p <= hi) if i > 0 else (p >= lo) & (p <= hi)
        count = int(mask.sum())
        if count == 0:
            table.append(
                {"bin": [round(lo, 2), round(hi, 2)], "count": 0,
                 "mean_predicted": None, "observed_rate": None}
            )
            continue
        mean_pred = float(p[mask].mean())
        observed = float(y[mask].mean())
        ece += (count / len(p)) * abs(mean_pred - observed)
        table.append(
            {
                "bin": [round(lo, 2), round(hi, 2)],
                "count": count,
                "mean_predicted": round(mean_pred, 4),
                "observed_rate": round(observed, 4),
            }
        )
    return {"bins": table, "ece": float(ece), "convention": "10 equal-width bins over [0,1]"}


@dataclass
class EstimatorMetrics:
    estimator_id: str
    n: int
    brier: float
    log_loss: float
    ece: float
    base_rate: float
    mean_prediction: float
    reliability_table: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "estimator_id": self.estimator_id,
            "n": self.n,
            "brier": round(self.brier, 6),
            "log_loss": round(self.log_loss, 6),
            "ece": round(self.ece, 6),
            "base_rate": round(self.base_rate, 6),
            "mean_prediction": round(self.mean_prediction, 6),
        }


def evaluate_estimator(
    estimator_id: str, y_true: Sequence[int], y_prob: Sequence[float]
) -> EstimatorMetrics:
    rel = reliability(y_true, y_prob)
    return EstimatorMetrics(
        estimator_id=estimator_id,
        n=len(y_true),
        brier=brier_score(y_true, y_prob),
        log_loss=log_loss(y_true, y_prob),
        ece=rel["ece"],
        base_rate=float(np.mean(y_true)) if len(y_true) else float("nan"),
        mean_prediction=float(np.mean(y_prob)) if len(y_prob) else float("nan"),
        reliability_table=rel["bins"],
    )


# ---------------------------------------------------------------------------
# Decision quality
# ---------------------------------------------------------------------------


@dataclass
class DecisionOutcome:
    """One policy's outcome on one trial."""

    policy: str
    subset: tuple[str, ...]
    fan_out: int
    satisfied: bool
    predicted_feasible: bool | None = None
    status: str | None = None


def summarize_decisions(outcomes: Sequence[DecisionOutcome]) -> dict:
    """Counts and means only. No significance testing in this milestone."""
    if not outcomes:
        return {"n": 0}
    planned = [o for o in outcomes if o.status in (None, "SELECTED")]
    unsatisfiable = [o for o in outcomes if o.status not in (None, "SELECTED")]
    satisfied = [o for o in planned if o.satisfied]
    false_feasible = [
        o for o in planned if o.predicted_feasible is True and not o.satisfied
    ]
    return {
        "n": len(outcomes),
        "planned": len(planned),
        "slo_satisfaction_rate": round(len(satisfied) / len(planned), 6) if planned else None,
        "mean_selected_subset_size": round(
            float(np.mean([o.fan_out for o in planned])), 4
        )
        if planned
        else None,
        "mean_attempted_providers": round(
            float(np.mean([o.fan_out for o in planned])), 4
        )
        if planned
        else None,
        "total_provider_calls": int(sum(o.fan_out for o in planned)),
        "unsatisfiable_estimate_count": len(unsatisfiable),
        "unsatisfiable_estimate_rate": round(len(unsatisfiable) / len(outcomes), 6),
        "false_feasible_count": len(false_feasible),
        "false_feasible_rate": round(len(false_feasible) / len(planned), 6)
        if planned
        else None,
    }


def sequential_failover_outcome(
    record, order: Sequence[str], tau_ms: float
) -> tuple[bool, int]:
    """[CALCULATED] counterfactual for strictly sequential failover.

    Reconstructed from the concurrently observed per-provider latencies: each
    attempt starts when the previous one finished, so cumulative time is used
    rather than a single provider's launch offset. This is a simulation from
    measured components, not a separately measured policy.
    """
    elapsed = 0.0
    attempts = 0
    for provider in order:
        obs = record.observations.get(provider)
        attempts += 1
        if obs is None or not obs.observed or obs.completion_offset_ms is None:
            # Unknown duration: treat as consuming the whole budget.
            return False, attempts
        # Per-attempt duration approximated by its own completion offset.
        elapsed += obs.completion_offset_ms
        if obs.accepted and elapsed <= tau_ms:
            return True, attempts
        if elapsed > tau_ms:
            return False, attempts
    return False, attempts
