"""Derived analysis over shadow trials -- CONTROLLED LOCAL QUALIFICATION.

Purpose of this module in the current phase: demonstrate that the pipeline can
compute

    fastest_responding_resolver   = argmin_i T_i  over resolvers that answered
    fastest_accepted_resolver     = argmin_i T_i  over resolvers whose answer
                                    passed structural acceptance

and decide whether they agree, for a trial in which every candidate resolver
was observed.

What these outputs are NOT: evidence about real DID resolvers. Every input is
produced by controlled injection on a single shared host. Counts are reported;
proportions are deliberately not framed as findings.

Two definitions carry weight:

  * Right-censoring. A timed-out or connection-failed observation has no
    completion time -- its latency is a lower bound set by our own deadline.
    Such observations are excluded from both minima and counted separately,
    rather than being treated as very slow completions.
  * Completeness. A trial missing any observation is marked incomplete and
    excluded from derivation. It is never partially analysed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .telemetry import SHADOW_OBSERVATIONS_FILENAME, SHADOW_TRIALS_FILENAME

QUALIFICATION_LABEL = "CONTROLLED LOCAL QUALIFICATION"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_shadow_dataset(directory: str | Path) -> tuple[list[dict], list[dict]]:
    """Return (trials, observations) as raw dict rows."""
    base = Path(directory)
    return (
        read_jsonl(base / SHADOW_TRIALS_FILENAME),
        read_jsonl(base / SHADOW_OBSERVATIONS_FILENAME),
    )


@dataclass
class TrialDerivation:
    """Derived quantities for one shadow trial."""

    experiment_id: str
    trial_id: str
    scenario_id: str
    analyzable: bool
    reason: str | None = None

    fastest_responding_resolver: str | None = None
    fastest_responding_latency_ms: float | None = None
    fastest_accepted_resolver: str | None = None
    fastest_accepted_latency_ms: float | None = None
    fastest_matches_fastest_accepted: bool | None = None

    responding_count: int = 0
    accepted_count: int = 0
    censored_count: int = 0
    censored_resolvers: list[str] = field(default_factory=list)

    # True when the winning latency is shared by more than one resolver, so
    # the argmin is order-dependent rather than uniquely determined.
    fastest_responding_tie: bool = False
    fastest_accepted_tie: bool = False

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "trial_id": self.trial_id,
            "scenario_id": self.scenario_id,
            "analyzable": self.analyzable,
            "reason": self.reason,
            "fastest_responding_resolver": self.fastest_responding_resolver,
            "fastest_responding_latency_ms": self.fastest_responding_latency_ms,
            "fastest_accepted_resolver": self.fastest_accepted_resolver,
            "fastest_accepted_latency_ms": self.fastest_accepted_latency_ms,
            "fastest_matches_fastest_accepted": self.fastest_matches_fastest_accepted,
            "responding_count": self.responding_count,
            "accepted_count": self.accepted_count,
            "censored_count": self.censored_count,
            "censored_resolvers": self.censored_resolvers,
            "fastest_responding_tie": self.fastest_responding_tie,
            "fastest_accepted_tie": self.fastest_accepted_tie,
        }


def _argmin(rows: list[dict]) -> tuple[str | None, float | None, bool]:
    """Return (resolver_id, latency, tied) for the minimum latency row."""
    if not rows:
        return None, None, False
    best = min(rows, key=lambda r: r["latency_ms"])
    tied = sum(1 for r in rows if r["latency_ms"] == best["latency_ms"]) > 1
    return best["resolver_id"], best["latency_ms"], tied


def derive_trial(trial: dict, observations: list[dict]) -> TrialDerivation:
    """Compute derived quantities for a single trial."""
    derivation = TrialDerivation(
        experiment_id=trial["experiment_id"],
        trial_id=trial["trial_id"],
        scenario_id=trial["scenario_id"],
        analyzable=False,
    )

    if not trial.get("complete", False):
        derivation.reason = (
            f"trial marked incomplete: {trial.get('incomplete_reason')}"
        )
        return derivation

    if len(observations) != trial["expected_observations"]:
        derivation.reason = (
            f"observation count {len(observations)} does not match expected "
            f"{trial['expected_observations']}"
        )
        return derivation

    responding = [o for o in observations if o.get("http_status") is not None]
    censored = [o for o in observations if o.get("http_status") is None]
    accepted = [o for o in responding if o.get("accepted") is True]

    derivation.responding_count = len(responding)
    derivation.accepted_count = len(accepted)
    derivation.censored_count = len(censored)
    derivation.censored_resolvers = sorted(o["resolver_id"] for o in censored)

    fastest_id, fastest_latency, fastest_tie = _argmin(responding)
    accepted_id, accepted_latency, accepted_tie = _argmin(accepted)

    derivation.fastest_responding_resolver = fastest_id
    derivation.fastest_responding_latency_ms = fastest_latency
    derivation.fastest_responding_tie = fastest_tie
    derivation.fastest_accepted_resolver = accepted_id
    derivation.fastest_accepted_latency_ms = accepted_latency
    derivation.fastest_accepted_tie = accepted_tie

    if fastest_id is None:
        derivation.reason = "no resolver produced a complete HTTP response"
        return derivation
    if accepted_id is None:
        derivation.reason = "no resolver produced an acceptable response"
        return derivation

    derivation.analyzable = True
    derivation.fastest_matches_fastest_accepted = fastest_id == accepted_id
    return derivation


def group_observations(observations: Iterable[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for observation in observations:
        key = (observation["experiment_id"], observation["trial_id"])
        grouped.setdefault(key, []).append(observation)
    return grouped


def derive_all(trials: list[dict], observations: list[dict]) -> list[TrialDerivation]:
    grouped = group_observations(observations)
    return [
        derive_trial(trial, grouped.get((trial["experiment_id"], trial["trial_id"]), []))
        for trial in trials
    ]


def summarize(derivations: list[TrialDerivation]) -> dict[str, Any]:
    """Counts only. Proportions are not reported as findings in this phase."""
    analyzable = [d for d in derivations if d.analyzable]
    return {
        "label": QUALIFICATION_LABEL,
        "total_trials": len(derivations),
        "analyzable_trials": len(analyzable),
        "excluded_trials": len(derivations) - len(analyzable),
        "fastest_equals_fastest_accepted": sum(
            1 for d in analyzable if d.fastest_matches_fastest_accepted is True
        ),
        "fastest_differs_from_fastest_accepted": sum(
            1 for d in analyzable if d.fastest_matches_fastest_accepted is False
        ),
        "trials_with_censored_observations": sum(
            1 for d in derivations if d.censored_count > 0
        ),
        "trials_with_ties": sum(
            1 for d in derivations if d.fastest_responding_tie or d.fastest_accepted_tie
        ),
    }


# ---------------------------------------------------------------------------
# Integrity audit
# ---------------------------------------------------------------------------

PROVENANCE_BINDING_FIELDS = (
    "experiment_id",
    "trial_id",
    "scenario_id",
    "phase",
    "git_commit",
    "config_hash",
    "injection_config_hash",
)


def audit_dataset(trials: list[dict], observations: list[dict]) -> dict[str, Any]:
    """Structural checks over a shadow dataset. Violations are listed, not
    summarised away."""
    violations: list[str] = []
    grouped = group_observations(observations)

    # Observation counts must match what each trial declares.
    for trial in trials:
        key = (trial["experiment_id"], trial["trial_id"])
        rows = grouped.get(key, [])
        if len(rows) != trial["actual_observations"]:
            violations.append(
                f"trial {trial['trial_id']}: declares "
                f"{trial['actual_observations']} observations, found {len(rows)}"
            )
        if trial["complete"] and len(rows) != trial["expected_observations"]:
            violations.append(
                f"trial {trial['trial_id']}: marked complete but has "
                f"{len(rows)}/{trial['expected_observations']} observations"
            )

    # No duplicate (experiment_id, trial_id, resolver_id).
    seen: set[tuple[str, str, str]] = set()
    for observation in observations:
        key = (
            observation["experiment_id"],
            observation["trial_id"],
            observation["resolver_id"],
        )
        if key in seen:
            violations.append(f"duplicate observation for {key}")
        seen.add(key)

    # Physical sanity.
    for observation in observations:
        if observation["latency_ms"] < 0:
            violations.append(
                f"negative latency for {observation['trial_id']}/"
                f"{observation['resolver_id']}"
            )
        if observation["end_ts"] < observation["start_ts"]:
            violations.append(
                f"end before start for {observation['trial_id']}/"
                f"{observation['resolver_id']}"
            )
        if observation["accepted"] and observation["document_valid"] is not True:
            violations.append(
                f"accepted without passing validation: {observation['trial_id']}/"
                f"{observation['resolver_id']}"
            )
        if observation["accepted"] and observation["http_status"] != 200:
            violations.append(
                f"accepted without HTTP 200: {observation['trial_id']}/"
                f"{observation['resolver_id']}"
            )

    # Every observation of a trial must share the trial's provenance.
    trials_by_key = {(t["experiment_id"], t["trial_id"]): t for t in trials}
    for key, rows in grouped.items():
        trial = trials_by_key.get(key)
        if trial is None:
            violations.append(f"observations with no parent trial: {key}")
            continue
        for observation in rows:
            for name in PROVENANCE_BINDING_FIELDS:
                if observation.get(name) != trial.get(name):
                    violations.append(
                        f"provenance mismatch on {name} for {key}/"
                        f"{observation['resolver_id']}: "
                        f"{observation.get(name)!r} != {trial.get(name)!r}"
                    )

    return {
        "trials": len(trials),
        "observations": len(observations),
        "violations": violations,
        "passed": not violations,
    }
