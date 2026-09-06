"""Subset-probability estimators: non-ML baselines and learned models.

All of them implement the existing `SubsetEstimator` interface, so the
already-qualified optimizer consumes them unchanged. None composes subset
probabilities from per-provider probabilities -- every estimator predicts
q_hat(S) for the subset directly.

Baselines exist to be beaten, or not:
    B0  global constant rate
    B1  rolling empirical rate for that exact subset
    B2  EWMA of that subset's recent outcomes

Learned:
    M1  logistic regression (linear, simplest learned option)
    M2  histogram gradient boosting (non-linear)

No deep learning, no LLM. If a baseline matches the learned models, that is a
result to report, not a problem to engineer around.
"""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..adaptive.estimator import SubsetEstimator, subset_key, validate_probability
from ..provenance import config_hash
from .features import FEATURE_ORDER, TrialContext, build_row, feature_schema_hash

CONTEXT_KEY = "trial_context"
HISTORY_KEY = "subset_history"

# [DESIGN CHOICE] clip predictions away from exactly 0/1 so log loss stays
# finite. Applied identically to every estimator, baselines included.
PROB_EPSILON = 1e-6


def _clip(value: float) -> float:
    return float(min(1.0 - PROB_EPSILON, max(PROB_EPSILON, value)))


class BaseSubsetEstimator(SubsetEstimator):
    """Common plumbing: context extraction and output validation."""

    def _context(self, context: Mapping[str, Any] | None) -> TrialContext | None:
        if context is None:
            return None
        return context.get(CONTEXT_KEY)

    def _finalize(self, value: float) -> float:
        return validate_probability(_clip(value), f"{self.estimator_id} q_hat")


# ---------------------------------------------------------------------------
# Non-ML baselines
# ---------------------------------------------------------------------------


class GlobalRateEstimator(BaseSubsetEstimator):
    """B0: one constant, the training-set success rate. No context at all."""

    estimator_id = "b0-global-rate"
    estimator_version = "v1"

    def __init__(self, rate: float = 0.5) -> None:
        self.rate = float(rate)

    def fit(self, rows) -> "GlobalRateEstimator":
        targets = [r.target for r in rows]
        self.rate = float(np.mean(targets)) if targets else 0.5
        return self

    def estimate(self, subset, context=None):
        return self._finalize(self.rate)

    def config_hash(self) -> str:
        return config_hash({"id": self.estimator_id, "rate": round(self.rate, 9)})


class SubsetRateEstimator(BaseSubsetEstimator):
    """B0b: per-subset training rate. Still no context, but subset-aware."""

    estimator_id = "b0s-subset-rate"
    estimator_version = "v1"

    def __init__(self) -> None:
        self.rates: dict[tuple[str, ...], float] = {}
        self.fallback = 0.5

    def fit(self, rows) -> "SubsetRateEstimator":
        buckets: dict[tuple[str, ...], list[int]] = {}
        for row in rows:
            buckets.setdefault(subset_key(row.subset), []).append(row.target)
        self.rates = {k: float(np.mean(v)) for k, v in buckets.items()}
        all_targets = [r.target for r in rows]
        self.fallback = float(np.mean(all_targets)) if all_targets else 0.5
        return self

    def estimate(self, subset, context=None):
        return self._finalize(self.rates.get(subset_key(subset), self.fallback))

    def config_hash(self) -> str:
        return config_hash(
            {
                "id": self.estimator_id,
                "rates": sorted(
                    [list(k), round(v, 9)] for k, v in self.rates.items()
                ),
                "fallback": round(self.fallback, 9),
            }
        )


class RollingEmpiricalEstimator(BaseSubsetEstimator):
    """B1: rolling success rate for this subset over its recent history.

    Reads the per-subset outcome history supplied in the context, which the
    caller assembles from trials strictly before t.
    """

    estimator_id = "b1-rolling-empirical"
    estimator_version = "v1"

    def __init__(self, window: int = 10, prior: float = 0.5, prior_weight: float = 2.0):
        self.window = window
        self.prior = prior
        self.prior_weight = prior_weight

    def fit(self, rows) -> "RollingEmpiricalEstimator":
        targets = [r.target for r in rows]
        if targets:
            self.prior = float(np.mean(targets))
        return self

    def estimate(self, subset, context=None):
        history = (context or {}).get(HISTORY_KEY, {})
        recent = list(history.get(subset_key(subset), []))[-self.window :]
        # Laplace-style smoothing toward the training prior keeps early
        # estimates from swinging to 0 or 1 on one observation.
        total = len(recent) + self.prior_weight
        hits = sum(recent) + self.prior * self.prior_weight
        return self._finalize(hits / total)

    def config_hash(self) -> str:
        return config_hash(
            {
                "id": self.estimator_id,
                "window": self.window,
                "prior": round(self.prior, 9),
                "prior_weight": self.prior_weight,
            }
        )


class EwmaEstimator(BaseSubsetEstimator):
    """B2: exponentially weighted recent success rate for this subset."""

    estimator_id = "b2-ewma"
    estimator_version = "v1"

    def __init__(self, alpha: float = 0.35, prior: float = 0.5) -> None:
        self.alpha = alpha
        self.prior = prior

    def fit(self, rows) -> "EwmaEstimator":
        targets = [r.target for r in rows]
        if targets:
            self.prior = float(np.mean(targets))
        return self

    def estimate(self, subset, context=None):
        history = (context or {}).get(HISTORY_KEY, {})
        value = self.prior
        for outcome in history.get(subset_key(subset), []):
            value = self.alpha * outcome + (1.0 - self.alpha) * value
        return self._finalize(value)

    def config_hash(self) -> str:
        return config_hash(
            {"id": self.estimator_id, "alpha": self.alpha, "prior": round(self.prior, 9)}
        )


# ---------------------------------------------------------------------------
# Learned estimators
# ---------------------------------------------------------------------------


class SklearnSubsetEstimator(BaseSubsetEstimator):
    """Wraps a scikit-learn classifier over (context features + subset mask).

    One model serves every subset: the subset is an input (membership mask +
    size), not a separate model. Extending M means extending the mask, not
    training 2^M - 1 models.
    """

    def __init__(self, model, estimator_id: str, version: str = "v1") -> None:
        self.model = model
        self.estimator_id = estimator_id
        self.estimator_version = version
        self.feature_order = list(FEATURE_ORDER)
        self._fitted = False

    def fit(self, rows) -> "SklearnSubsetEstimator":
        X = np.array([r.features for r in rows], dtype=float)
        y = np.array([r.target for r in rows], dtype=int)
        self.model.fit(X, y)
        self._fitted = True
        return self

    def predict_rows(self, rows) -> np.ndarray:
        X = np.array([r.features for r in rows], dtype=float)
        return self.model.predict_proba(X)[:, 1]

    def estimate(self, subset, context=None):
        trial_context = self._context(context)
        if trial_context is None:
            return None
        row = np.array([build_row(trial_context, subset)], dtype=float)
        return self._finalize(float(self.model.predict_proba(row)[0, 1]))

    def config_hash(self) -> str:
        return config_hash(
            {
                "id": self.estimator_id,
                "version": self.estimator_version,
                "feature_schema_hash": feature_schema_hash(),
                "params": {k: str(v) for k, v in sorted(self.model.get_params().items())},
            }
        )


def build_logistic(seed: int = 20260907) -> SklearnSubsetEstimator:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    pipeline = Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    max_iter=2000, C=1.0, random_state=seed, solver="lbfgs"
                ),
            ),
        ]
    )
    return SklearnSubsetEstimator(pipeline, "m1-logistic", "v1")


def build_gradient_boosting(seed: int = 20260907) -> SklearnSubsetEstimator:
    from sklearn.ensemble import HistGradientBoostingClassifier

    model = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.08, max_depth=4, random_state=seed
    )
    return SklearnSubsetEstimator(model, "m2-hist-gradient-boosting", "v1")


# ---------------------------------------------------------------------------
# Freeze / reload
# ---------------------------------------------------------------------------


@dataclass
class FrozenArtifact:
    estimator_id: str
    estimator_version: str
    model_family: str
    hyperparameters: dict
    feature_schema_version: str
    feature_schema_hash: str
    feature_order: list[str]
    deadline_tau_ms: float
    train_dataset_id: str
    validation_dataset_id: str
    calibration: str | None
    artifact_sha256: str = ""
    extra: dict = field(default_factory=dict)

    def metadata(self) -> dict:
        return {
            "estimator_id": self.estimator_id,
            "estimator_version": self.estimator_version,
            "model_family": self.model_family,
            "hyperparameters": self.hyperparameters,
            "feature_schema_version": self.feature_schema_version,
            "feature_schema_hash": self.feature_schema_hash,
            "feature_order": self.feature_order,
            "deadline_tau_ms": self.deadline_tau_ms,
            "train_dataset_id": self.train_dataset_id,
            "validation_dataset_id": self.validation_dataset_id,
            "calibration": self.calibration,
            "artifact_sha256": self.artifact_sha256,
            **self.extra,
        }


def freeze_estimator(
    estimator: SubsetEstimator,
    path: Path,
    *,
    model_family: str,
    hyperparameters: dict,
    deadline_tau_ms: float,
    train_dataset_id: str,
    validation_dataset_id: str,
    calibration: str | None = None,
) -> FrozenArtifact:
    """Pickle the estimator and record a hash over the exact bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = pickle.dumps(estimator, protocol=pickle.HIGHEST_PROTOCOL)
    path.write_bytes(payload)
    artifact = FrozenArtifact(
        estimator_id=estimator.estimator_id,
        estimator_version=estimator.estimator_version,
        model_family=model_family,
        hyperparameters=hyperparameters,
        feature_schema_version="prerequest-v1",
        feature_schema_hash=feature_schema_hash(),
        feature_order=list(FEATURE_ORDER),
        deadline_tau_ms=deadline_tau_ms,
        train_dataset_id=train_dataset_id,
        validation_dataset_id=validation_dataset_id,
        calibration=calibration,
        artifact_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        extra={"estimator_config_hash": estimator.config_hash()},
    )
    path.with_suffix(".json").write_text(
        __import__("json").dumps(artifact.metadata(), indent=2), encoding="utf-8"
    )
    return artifact


def load_frozen(path: Path) -> tuple[SubsetEstimator, dict, str]:
    """Reload a frozen estimator and verify the artifact hash."""
    payload = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    estimator = pickle.loads(payload)
    metadata = __import__("json").loads(
        path.with_suffix(".json").read_text(encoding="utf-8")
    )
    if metadata.get("artifact_sha256") != digest:
        raise ValueError(
            f"frozen artifact hash mismatch: metadata says "
            f"{metadata.get('artifact_sha256')}, file hashes to {digest}"
        )
    return estimator, metadata, digest


class SigmoidCalibratedEstimator(BaseSubsetEstimator):
    """Platt scaling on top of another estimator's probabilities.

    Fitted on TRAIN predictions ONLY -- never on validation and never on the
    final holdout. The wrapper keeps the base estimator intact so the
    uncalibrated behaviour remains inspectable.

    Applied only when the pre-declared rule fires (validation ECE above the
    documented threshold), so calibration is a rule-driven step rather than a
    post-hoc reaction to results.
    """

    def __init__(self, base: SubsetEstimator, coef: float = 1.0, intercept: float = 0.0):
        self.base = base
        self.coef = float(coef)
        self.intercept = float(intercept)
        self.estimator_id = f"{base.estimator_id}+sigmoid"
        self.estimator_version = base.estimator_version

    @staticmethod
    def _logit(p: float) -> float:
        p = _clip(p)
        return float(np.log(p / (1.0 - p)))

    def fit_from_predictions(self, q_raw: Sequence[float], y: Sequence[int]):
        from sklearn.linear_model import LogisticRegression

        X = np.array([[self._logit(q)] for q in q_raw], dtype=float)
        target = np.asarray(y, dtype=int)
        if len(set(target.tolist())) < 2:
            # Degenerate target: leave the identity mapping rather than fit
            # something meaningless.
            self.coef, self.intercept = 1.0, 0.0
            return self
        model = LogisticRegression(max_iter=1000).fit(X, target)
        self.coef = float(model.coef_[0][0])
        self.intercept = float(model.intercept_[0])
        return self

    def estimate(self, subset, context=None):
        raw = self.base.estimate(subset, context)
        if raw is None:
            return None
        z = self.coef * self._logit(float(raw)) + self.intercept
        return self._finalize(float(1.0 / (1.0 + np.exp(-z))))

    def config_hash(self) -> str:
        return config_hash(
            {
                "id": self.estimator_id,
                "base": self.base.config_hash(),
                "coef": round(self.coef, 9),
                "intercept": round(self.intercept, 9),
            }
        )

    def describe(self) -> dict:
        return {
            **super().describe(),
            "base_estimator_id": self.base.estimator_id,
            "calibration": "platt-sigmoid-on-train",
            "coef": self.coef,
            "intercept": self.intercept,
        }
