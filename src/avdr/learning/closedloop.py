"""Closed-loop partial-feedback routing: two strictly separated histories.

THE BOUNDARY THIS MODULE EXISTS TO ENFORCE
------------------------------------------
A deployed adaptive router observes only what it actually called. If it sends
S_t = {B}, then B's outcome is observable and A's and C's are not. The V1
holdout gave the estimator every provider's outcome every trial, which is a
full-information advantage no deployment has.

Two histories therefore exist and never mix:

  DeploymentObservedHistory   ONLY outcomes the policy actually acquired.
                              The ONLY history online features may read.

  EvaluatorGroundTruthHistory ALL provider outcomes. Offline oracle scoring
                              only. It deliberately exposes no method the
                              feature code calls, and passing one to the
                              feature builder raises TypeError.

Every observed record carries `observation_source` so the reason it was
observable is auditable:

    selected_execution     the policy chose this provider and called it
    scheduled_exploration  a fixed audit schedule called it (charged as cost)
    audit_collection       offline corpus collection (training phase)
    warmup                 connection warm-up, never scored

STALENESS
---------
Under partial feedback a provider may not have been seen for many trials. Old
telemetry is never forward-filled to look current: rates are computed only
from real observations, absence uses an explicit sentinel, and the age of the
last observation is itself a feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..provenance import config_hash
from .environment import PROVIDERS
from .features import NO_HISTORY, ProviderObservation, TrialRecord

SELECTED_EXECUTION = "selected_execution"
SCHEDULED_EXPLORATION = "scheduled_exploration"
AUDIT_COLLECTION = "audit_collection"
WARMUP = "warmup"
EVALUATOR_ONLY = "evaluator_only"

# Sources that represent requests the policy ACTUALLY made, and may therefore
# enter the deployment history. `warmup` counts: it is a real cold-start
# request and its calls are charged like any other.
ONLINE_SOURCES = frozenset(
    {SELECTED_EXECUTION, SCHEDULED_EXPLORATION, AUDIT_COLLECTION, WARMUP}
)

# [DESIGN CHOICE] Cold start, fixed before the V3 holdout. V2 showed a
# self-reinforcing trap: with an empty history the backoff priors sit below
# target, the router abstains, nothing is observed, and the history stays
# empty. The first COLD_START_TRIALS trials of every episode are therefore a
# mandatory all-provider audit, tagged `warmup` and CHARGED as real calls.
COLD_START_TRIALS = 3

FEATURE_SCHEMA_VERSION_V2 = "prerequest-partial-v2"

# [DESIGN CHOICE] rolling window, unchanged from V1 so the only difference is
# observability, not window length.
HISTORY_WINDOW_V2 = 10


@dataclass
class ObservedRecord:
    """One trial as the DEPLOYED policy saw it: possibly partial."""

    trial_index: int
    # provider -> outcome, present ONLY for providers actually observed.
    observations: dict[str, ProviderObservation] = field(default_factory=dict)
    observation_source: str = SELECTED_EXECUTION

    def observed_providers(self) -> set[str]:
        return {p for p, o in self.observations.items() if o.observed}

    def within_deadline(self, provider_id: str, tau_ms: float) -> bool:
        obs = self.observations.get(provider_id)
        if obs is None or not obs.observed or not obs.accepted:
            return False
        if obs.completion_offset_ms is None:
            return False
        return obs.completion_offset_ms <= tau_ms


class EvaluatorGroundTruthHistory:
    """All outcomes, for offline scoring. NEVER reaches the model.

    Deliberately does NOT implement the interface the feature builder uses,
    so handing it to online feature code fails loudly instead of silently
    leaking full information.
    """

    observation_source = EVALUATOR_ONLY

    def __init__(self) -> None:
        self._records: list[TrialRecord] = []

    def append(self, record: TrialRecord) -> None:
        self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> list[TrialRecord]:
        return list(self._records)

    # Explicitly poisoned: nothing online may iterate this as a history.
    def __iter__(self):
        raise TypeError(
            "EvaluatorGroundTruthHistory must never be iterated by online "
            "feature code; use DeploymentObservedHistory"
        )


class DeploymentObservedHistory:
    """Only what the deployed policy actually acquired."""

    def __init__(self) -> None:
        self._records: list[ObservedRecord] = []

    def append(self, record: ObservedRecord) -> None:
        if record.observation_source not in ONLINE_SOURCES:
            raise ValueError(
                f"observation_source {record.observation_source!r} may not "
                f"enter the deployment history; online sources are "
                f"{sorted(ONLINE_SOURCES)}"
            )
        self._records.append(record)

    def records(self) -> list[ObservedRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def observation_count(self, provider_id: str) -> int:
        return sum(1 for r in self._records if provider_id in r.observed_providers())

    def trials_since_observed(self, provider_id: str) -> float:
        """Trials since this provider was last seen. NO_HISTORY if never."""
        for distance, record in enumerate(reversed(self._records)):
            if provider_id in record.observed_providers():
                return float(distance)
        return NO_HISTORY


# ---------------------------------------------------------------------------
# V2 feature contract
# ---------------------------------------------------------------------------

PER_PROVIDER_FEATURES_V2 = (
    "obs_count",
    "accept_rate",
    "within_deadline_rate",
    "error_rate",
    "timeout_rate",
    "p50_completion_ms",
    "p95_completion_ms",
    "trials_since_failure",
    # Partial-feedback additions.
    "trials_since_observed",
    "observation_count",
    "recent_observation_window_count",
)

GLOBAL_FEATURES_V2 = ("history_len", "any_within_deadline_rate")
SUBSET_FEATURES_V2 = ("in_local_a", "in_local_b", "in_local_c", "subset_size")

FEATURE_ORDER_V2: tuple[str, ...] = (
    tuple(
        f"{provider.replace('-', '_')}__{name}"
        for provider in PROVIDERS
        for name in PER_PROVIDER_FEATURES_V2
    )
    + GLOBAL_FEATURES_V2
    + SUBSET_FEATURES_V2
)


def feature_schema_hash_v2() -> str:
    return config_hash(
        {
            "version": FEATURE_SCHEMA_VERSION_V2,
            "order": list(FEATURE_ORDER_V2),
            "history_window": HISTORY_WINDOW_V2,
        }
    )


@dataclass
class PartialContext:
    trial_index: int
    per_provider: dict[str, dict[str, float]] = field(default_factory=dict)
    history_len: float = 0.0
    any_within_deadline_rate: float = NO_HISTORY


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return NO_HISTORY
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def build_partial_context(
    history: DeploymentObservedHistory,
    tau_ms: float,
    trial_index: int,
    window: int = HISTORY_WINDOW_V2,
) -> PartialContext:
    """Features from the DEPLOYMENT history only.

    Raises if handed the evaluator history, so full-information leakage is a
    hard error rather than a silent advantage.
    """
    if not isinstance(history, DeploymentObservedHistory):
        raise TypeError(
            "online features may only be built from DeploymentObservedHistory; "
            f"got {type(history).__name__}"
        )

    all_records = history.records()
    recent = all_records[-window:]
    context = PartialContext(trial_index=trial_index)
    context.history_len = float(len(all_records))

    for provider in PROVIDERS:
        observations = [
            r.observations[provider]
            for r in recent
            if provider in r.observed_providers()
        ]
        count = len(observations)
        stats = {
            "trials_since_observed": history.trials_since_observed(provider),
            "observation_count": float(history.observation_count(provider)),
            "recent_observation_window_count": float(count),
        }
        if count == 0:
            # No forward filling. Absence is stated, not imputed.
            stats.update(
                {
                    "obs_count": 0.0,
                    "accept_rate": NO_HISTORY,
                    "within_deadline_rate": NO_HISTORY,
                    "error_rate": NO_HISTORY,
                    "timeout_rate": NO_HISTORY,
                    "p50_completion_ms": NO_HISTORY,
                    "p95_completion_ms": NO_HISTORY,
                    "trials_since_failure": NO_HISTORY,
                }
            )
            context.per_provider[provider] = stats
            continue

        completions = [
            o.completion_offset_ms
            for o in observations
            if o.completion_offset_ms is not None
        ]
        within = sum(
            1
            for r in recent
            if provider in r.observed_providers()
            and r.within_deadline(provider, tau_ms)
        )
        since_failure = float(count)
        for distance, observation in enumerate(reversed(observations)):
            if not observation.accepted:
                since_failure = float(distance)
                break

        stats.update(
            {
                "obs_count": float(count),
                "accept_rate": sum(1 for o in observations if o.accepted) / count,
                "within_deadline_rate": within / count,
                "error_rate": sum(1 for o in observations if o.errored) / count,
                "timeout_rate": sum(1 for o in observations if o.timed_out) / count,
                "p50_completion_ms": _percentile(completions, 0.50),
                "p95_completion_ms": _percentile(completions, 0.95),
                "trials_since_failure": since_failure,
            }
        )
        context.per_provider[provider] = stats

    scored = [r for r in recent if r.observed_providers()]
    if scored:
        hits = sum(
            1
            for r in scored
            if any(r.within_deadline(p, tau_ms) for p in r.observed_providers())
        )
        context.any_within_deadline_rate = hits / len(scored)
    return context


def build_row_v2(context: PartialContext, subset: Iterable[str]) -> list[float]:
    members = set(subset)
    row: list[float] = []
    for provider in PROVIDERS:
        stats = context.per_provider.get(provider, {})
        for name in PER_PROVIDER_FEATURES_V2:
            row.append(float(stats.get(name, NO_HISTORY)))
    row.append(float(context.history_len))
    row.append(float(context.any_within_deadline_rate))
    for provider in PROVIDERS:
        row.append(1.0 if provider in members else 0.0)
    row.append(float(len(members)))
    assert len(row) == len(FEATURE_ORDER_V2)
    return row


# ---------------------------------------------------------------------------
# Exploration schedule and service modes
# ---------------------------------------------------------------------------

# [DESIGN CHOICE] E1 audits every R-th logical request, fixed before the V2
# holdout and never tuned against it.
EXPLORATION_INTERVAL_R = 8

STRICT_SLO = "strict-slo"
SERVICE_BEST_EFFORT = "service-best-effort"


def should_explore(trial_counter: int, interval: int | None) -> bool:
    """E0 = interval None (never). E1 = every `interval`-th request."""
    if not interval:
        return False
    return trial_counter > 0 and trial_counter % interval == 0


def reveal_subset(
    record: TrialRecord, subset: Iterable[str], source: str
) -> ObservedRecord:
    """Expose ONLY the named providers' outcomes to the deployment history."""
    members = set(subset)
    return ObservedRecord(
        trial_index=record.trial_index,
        observations={
            p: o for p, o in record.observations.items() if p in members
        },
        observation_source=source,
    )


def known_subset_outcome(
    observed: set[str], subset: Iterable[str], record_within: dict[str, bool]
) -> int | None:
    """Y(S) as far as the DEPLOYED policy can tell, or None if unknowable.

    Partial feedback makes this genuinely three-valued:

      * an observed member that met the deadline proves Y(S)=1
      * if every member was observed and none met it, Y(S)=0
      * otherwise an unobserved member might have succeeded, so Y(S) is
        UNKNOWN and must not be recorded as 0

    Recording unknown as failure would teach the policy that subsets it never
    tried do not work -- a self-fulfilling bias.
    """
    members = list(subset)
    if any(record_within.get(p, False) for p in members if p in observed):
        return 1
    if all(p in observed for p in members):
        return 0
    return None


def update_subset_history(
    subset_history: dict[tuple[str, ...], list[int]],
    observed: set[str],
    within: dict[str, bool],
    subsets: Sequence[tuple[str, ...]],
) -> int:
    """Append only the subset outcomes the policy can actually determine."""
    updated = 0
    for subset in subsets:
        outcome = known_subset_outcome(observed, subset, within)
        if outcome is not None:
            subset_history.setdefault(tuple(sorted(subset)), []).append(outcome)
            updated += 1
    return updated
