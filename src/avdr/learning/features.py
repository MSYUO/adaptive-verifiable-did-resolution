"""Pre-request feature contract.

THE BOUNDARY: a feature row for trial t may depend only on observations from
trials with index < t. Nothing about trial t's own outcome, nothing from any
later trial, and never the hidden injected state.

Enforcement is structural, not a convention: `build_context` is handed a
history list and cannot see anything else, and the runner computes the context
BEFORE executing the trial. Tests additionally perturb trial t's outcome and
assert the features are unchanged.

Explicitly FORBIDDEN as features (checked by test):
  current trial latency / acceptance / winner / error / completion,
  any future trial, the hidden injection state, and any scenario label that
  would reveal it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..provenance import config_hash
from .environment import PROVIDERS

# [DESIGN CHOICE] rolling window, fixed before data generation.
HISTORY_WINDOW = 10

# Sentinel for "no history yet". A distinct negative value is used rather than
# 0 so the model can tell "no observations" from "completed instantly".
NO_HISTORY = -1.0

PER_PROVIDER_FEATURES = (
    "obs_count",
    "accept_rate",
    "within_deadline_rate",
    "error_rate",
    "timeout_rate",
    "p50_completion_ms",
    "p95_completion_ms",
    "trials_since_failure",
)

GLOBAL_FEATURES = ("history_len", "any_within_deadline_rate")
SUBSET_FEATURES = ("in_local_a", "in_local_b", "in_local_c", "subset_size")

FEATURE_ORDER: tuple[str, ...] = (
    tuple(
        f"{provider.replace('-', '_')}__{name}"
        for provider in PROVIDERS
        for name in PER_PROVIDER_FEATURES
    )
    + GLOBAL_FEATURES
    + SUBSET_FEATURES
)

FEATURE_SCHEMA_VERSION = "prerequest-v1"


def feature_schema_hash() -> str:
    return config_hash(
        {
            "version": FEATURE_SCHEMA_VERSION,
            "order": list(FEATURE_ORDER),
            "history_window": HISTORY_WINDOW,
        }
    )


@dataclass
class ProviderObservation:
    """One provider's outcome in one completed trial."""

    provider_id: str
    accepted: bool
    completion_offset_ms: float | None  # launch_offset + latency
    http_status: int | None
    outcome: str
    timed_out: bool
    errored: bool
    invalid: bool
    observed: bool = True


@dataclass
class TrialRecord:
    """One fully observed trial. Written to the dataset and used as history."""

    episode_id: str
    trial_index: int
    split: str
    seed: int
    hidden_state: str  # audit only -- never a feature
    did: str
    observations: dict[str, ProviderObservation]
    complete: bool
    incomplete_reason: str | None = None

    def within_deadline(self, provider_id: str, tau_ms: float) -> bool:
        obs = self.observations.get(provider_id)
        if obs is None or not obs.observed or not obs.accepted:
            return False
        if obs.completion_offset_ms is None:
            return False
        return obs.completion_offset_ms <= tau_ms


@dataclass
class TrialContext:
    """Pre-request context for one trial. Derived only from history < t."""

    episode_id: str
    trial_index: int
    per_provider: dict[str, dict[str, float]] = field(default_factory=dict)
    history_len: float = 0.0
    any_within_deadline_rate: float = NO_HISTORY

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "trial_index": self.trial_index,
            "history_len": self.history_len,
            "any_within_deadline_rate": self.any_within_deadline_rate,
            "per_provider": self.per_provider,
        }


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return NO_HISTORY
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def build_context(
    history: Sequence[TrialRecord],
    tau_ms: float,
    episode_id: str,
    trial_index: int,
    window: int = HISTORY_WINDOW,
) -> TrialContext:
    """Summarise the last `window` trials. `history` must contain only t' < t.

    The caller is responsible for passing a truncated history; the runner does
    so by construction, and a test verifies every record has trial_index < t.
    """
    recent = list(history)[-window:]
    context = TrialContext(episode_id=episode_id, trial_index=trial_index)
    context.history_len = float(len(history))

    for provider in PROVIDERS:
        observations = [
            r.observations[provider]
            for r in recent
            if provider in r.observations and r.observations[provider].observed
        ]
        count = len(observations)
        if count == 0:
            context.per_provider[provider] = {
                "obs_count": 0.0,
                "accept_rate": NO_HISTORY,
                "within_deadline_rate": NO_HISTORY,
                "error_rate": NO_HISTORY,
                "timeout_rate": NO_HISTORY,
                "p50_completion_ms": NO_HISTORY,
                "p95_completion_ms": NO_HISTORY,
                "trials_since_failure": NO_HISTORY,
            }
            continue

        completions = [
            o.completion_offset_ms
            for o in observations
            if o.completion_offset_ms is not None
        ]
        within = [
            1.0
            for r in recent
            if provider in r.observations and r.within_deadline(provider, tau_ms)
        ]

        since_failure = NO_HISTORY
        for distance, observation in enumerate(reversed(observations)):
            if not observation.accepted:
                since_failure = float(distance)
                break
        else:
            since_failure = float(count)

        context.per_provider[provider] = {
            "obs_count": float(count),
            "accept_rate": sum(1 for o in observations if o.accepted) / count,
            "within_deadline_rate": len(within) / count,
            "error_rate": sum(1 for o in observations if o.errored) / count,
            "timeout_rate": sum(1 for o in observations if o.timed_out) / count,
            "p50_completion_ms": _percentile(completions, 0.50),
            "p95_completion_ms": _percentile(completions, 0.95),
            "trials_since_failure": since_failure,
        }

    if recent:
        hits = sum(
            1
            for r in recent
            if any(r.within_deadline(p, tau_ms) for p in PROVIDERS)
        )
        context.any_within_deadline_rate = hits / len(recent)
    return context


def build_row(context: TrialContext, subset: Iterable[str]) -> list[float]:
    """Feature vector for (context, subset), in FEATURE_ORDER.

    The subset is encoded as a membership mask plus its size, so ONE estimator
    serves all subsets. The mask generalises to larger M by extending the
    provider list rather than training a model per subset.
    """
    members = set(subset)
    row: list[float] = []
    for provider in PROVIDERS:
        stats = context.per_provider.get(provider, {})
        for name in PER_PROVIDER_FEATURES:
            row.append(float(stats.get(name, NO_HISTORY)))
    row.append(float(context.history_len))
    row.append(float(context.any_within_deadline_rate))
    for provider in PROVIDERS:
        row.append(1.0 if provider in members else 0.0)
    row.append(float(len(members)))
    assert len(row) == len(FEATURE_ORDER)
    return row
