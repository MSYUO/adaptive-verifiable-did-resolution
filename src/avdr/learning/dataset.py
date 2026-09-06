"""Trial execution, subset-target derivation and dataset assembly.

TARGET DEFINITION
-----------------
For trial t and subset S:

    Y_t(S) = 1  iff  exists i in S with
                     accepted_i = true
                     AND absolute_completion_offset_i <= tau

where

    absolute_completion_offset_i = launch_offset_i + latency_i

both measured against the SAME logical trial start. Raw per-attempt latency
alone is never used: providers are launched at slightly different moments, and
comparing latencies while ignoring launch offsets would credit a late-launched
provider with time it never had.

Explicit semantics for non-completions -- none of these may be read as success:

    timeout            accepted = false          -> contributes 0
    HTTP/resolution
      error            accepted = false          -> contributes 0
    structurally
      unacceptable     accepted = false          -> contributes 0
    missing
      observation      observed = false, and the trial is marked incomplete
                       and EXCLUDED from training and evaluation rather than
                       being counted as a failure or inferred as a success

All timings are [CONTROLLED INJECTION] on one shared host.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Iterable, Sequence

import httpx

from ..inventory import ProviderEntry
from ..probe import probe_provider
from ..provenance import config_hash
from .environment import PROVIDERS, EpisodePlan, iter_trials, sample_behaviors
from .features import (
    FEATURE_ORDER,
    FEATURE_SCHEMA_VERSION,
    ProviderObservation,
    TrialContext,
    TrialRecord,
    build_context,
    build_row,
    feature_schema_hash,
)

# [DESIGN CHOICE] Deadline, fixed BEFORE final dataset generation and never
# tuned against holdout results. Chosen against the injected time scale:
# healthy responses land around 5-40 ms and degraded states around 210-620 ms,
# so 250 ms sits between them and makes the decision non-trivial.
DEADLINE_TAU_MS = 250.0

GENERATOR_VERSION = "controlled-stochastic-v1"

ADMIN_URLS = {
    "local-a": "http://127.0.0.1:8001",
    "local-b": "http://127.0.0.1:8002",
    "local-c": "http://127.0.0.1:8003",
}

BEHAVIOR_DEFAULTS = {
    "force_error_status": 503,
    "force_timeout": False,
    "timeout_sleep_ms": 30000,
    "deterministic_failure_every_n": 0,
}


def all_subsets(providers: Sequence[str] = PROVIDERS) -> list[tuple[str, ...]]:
    ordered = sorted(providers)
    return [
        tuple(combo)
        for size in range(1, len(ordered) + 1)
        for combo in combinations(ordered, size)
    ]


def subset_target(record: TrialRecord, subset: Iterable[str], tau_ms: float) -> int:
    """Y_t(S): 1 iff some member was accepted AND completed within tau."""
    return int(any(record.within_deadline(p, tau_ms) for p in subset))


@dataclass
class DatasetRow:
    episode_id: str
    trial_index: int
    split: str
    subset: tuple[str, ...]
    features: list[float]
    target: int
    hidden_state: str  # audit only; never fed to the model

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "trial_index": self.trial_index,
            "split": self.split,
            "subset": list(self.subset),
            "features": self.features,
            "target": self.target,
            "hidden_state": self.hidden_state,
        }


@dataclass
class Dataset:
    rows: list[DatasetRow] = field(default_factory=list)
    trials: list[TrialRecord] = field(default_factory=list)
    incomplete_trials: int = 0

    def split_rows(self, split: str) -> list[DatasetRow]:
        return [r for r in self.rows if r.split == split]

    def episodes(self, split: str) -> set[str]:
        return {r.episode_id for r in self.rows if r.split == split}


def apply_behaviors(client: httpx.Client, behaviors: dict[str, dict]) -> None:
    for provider, sampled in behaviors.items():
        payload = {**BEHAVIOR_DEFAULTS, **sampled}
        client.post(
            f"{ADMIN_URLS[provider]}/admin/behavior", json=payload, timeout=5
        ).raise_for_status()


async def apply_behaviors_async(
    client: httpx.AsyncClient, behaviors: dict[str, dict]
) -> None:
    """Apply injected behaviour before a trial. Admin traffic is never
    measured and never enters the dataset."""
    for provider, sampled in behaviors.items():
        response = await client.post(
            f"{ADMIN_URLS[provider]}/admin/behavior",
            json={**BEHAVIOR_DEFAULTS, **sampled},
            timeout=5,
        )
        response.raise_for_status()


async def observe_trial(
    client: httpx.AsyncClient,
    entries: dict[str, ProviderEntry],
    did: str,
    timeout_ms: int,
) -> dict[str, ProviderObservation]:
    """Probe every provider concurrently from one common trial start.

    CONNECTION MODE: reused-client. One AsyncClient is shared for the whole
    episode and connections are warmed before the first recorded trial.

    This is load-bearing. With a fresh client per trial, connection setup on
    this host cost roughly 275 ms -- larger than the deadline itself -- which
    would have buried the injected signal under transport overhead and made
    the dataset measure connection setup rather than resolver behaviour.
    """
    started = time.perf_counter()

    async def one(provider: ProviderEntry):
        # Offset from the COMMON trial start, so completion offsets are
        # comparable across providers.
        offset_ms = (time.perf_counter() - started) * 1000.0
        return await probe_provider(client, provider, did, timeout_ms, offset_ms)

    outcomes = await asyncio.gather(
        *(one(entry) for entry in entries.values()), return_exceptions=True
    )

    observations: dict[str, ProviderObservation] = {}
    for provider_id, outcome in zip(entries, outcomes):
        if isinstance(outcome, BaseException):
            # Never inferred as success, never counted as a plain failure.
            observations[provider_id] = ProviderObservation(
                provider_id=provider_id,
                accepted=False,
                completion_offset_ms=None,
                http_status=None,
                outcome=f"probe_error:{type(outcome).__name__}",
                timed_out=False,
                errored=True,
                invalid=False,
                observed=False,
            )
            continue
        normalized = outcome.normalized
        observations[provider_id] = ProviderObservation(
            provider_id=provider_id,
            accepted=outcome.accepted,
            completion_offset_ms=round(
                outcome.launch_offset_ms + outcome.latency_ms, 3
            ),
            http_status=outcome.http_status,
            outcome=outcome.transport_outcome,
            timed_out=outcome.transport_outcome == "timeout",
            errored=(outcome.http_status is not None and outcome.http_status >= 400),
            invalid=(
                normalized.acceptance is not None
                and normalized.acceptance.checks.get(
                    "did_document_id_matches_request"
                )
                is False
            ),
            observed=True,
        )
    return observations


async def warm_connections(
    client: httpx.AsyncClient, entries: dict[str, ProviderEntry], timeout_ms: int
) -> None:
    """Establish connections before the first recorded trial.

    [DESIGN CHOICE] Warm-up requests are discarded and never enter the
    dataset; their only purpose is to keep transport setup out of the
    measured completion offsets.
    """
    await asyncio.gather(
        *(
            probe_provider(client, entry, "did:example:warmup", timeout_ms, 0.0)
            for entry in entries.values()
        ),
        return_exceptions=True,
    )


async def generate_episode_async(
    plan: EpisodePlan,
    entries: dict[str, ProviderEntry],
    admin: httpx.AsyncClient,
    tau_ms: float = DEADLINE_TAU_MS,
    timeout_ms: int = 3000,
) -> tuple[list[TrialRecord], list[DatasetRow]]:
    """Run one episode; return its trial records and dataset rows.

    Features for trial t are built from history[:t] BEFORE trial t executes.
    """
    history: list[TrialRecord] = []
    rows: list[DatasetRow] = []

    async with httpx.AsyncClient() as client:
        await warm_connections(client, entries, timeout_ms)

        for trial_index, hidden_state, rng in iter_trials(plan):
            # ---- pre-request: context from strictly earlier trials only ----
            context = build_context(history, tau_ms, plan.episode_id, trial_index)

            behaviors = sample_behaviors(hidden_state, rng)
            await apply_behaviors_async(admin, behaviors)
            did = f"did:example:{plan.episode_id}-{trial_index:04d}"

            observations = await observe_trial(client, entries, did, timeout_ms)
            missing = [p for p, o in observations.items() if not o.observed]
            record = TrialRecord(
                episode_id=plan.episode_id,
                trial_index=trial_index,
                split=plan.split,
                seed=plan.seed,
                hidden_state=hidden_state,
                did=did,
                observations=observations,
                complete=not missing,
                incomplete_reason=(
                    f"missing observations for {sorted(missing)}"
                    if missing
                    else None
                ),
            )

            # ---- outcome revealed only now --------------------------------
            if record.complete:
                for subset in all_subsets():
                    rows.append(
                        DatasetRow(
                            episode_id=plan.episode_id,
                            trial_index=trial_index,
                            split=plan.split,
                            subset=subset,
                            features=build_row(context, subset),
                            target=subset_target(record, subset, tau_ms),
                            hidden_state=hidden_state,
                        )
                    )
            history.append(record)

    return history, rows


def generate_episode(
    plan: EpisodePlan,
    entries: dict[str, ProviderEntry],
    admin: httpx.Client | None = None,
    tau_ms: float = DEADLINE_TAU_MS,
    timeout_ms: int = 3000,
) -> tuple[list[TrialRecord], list[DatasetRow]]:
    """Synchronous convenience wrapper around generate_episode_async."""

    async def runner():
        async with httpx.AsyncClient() as admin_client:
            return await generate_episode_async(
                plan, entries, admin_client, tau_ms, timeout_ms
            )

    return asyncio.run(runner())


def dataset_manifest(
    dataset_id: str,
    plans: Sequence[EpisodePlan],
    tau_ms: float,
    injection_hash: str,
    git_commit: str | None,
    git_dirty: bool | None,
    dependency_lock_hash: str | None,
    acceptance_profile: str,
    row_count: int,
    incomplete_trials: int,
) -> dict:
    return {
        "dataset_id": dataset_id,
        "generator_version": GENERATOR_VERSION,
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "injection_config_hash": injection_hash,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": feature_schema_hash(),
        "feature_order": list(FEATURE_ORDER),
        "acceptance_profile": acceptance_profile,
        "deadline_tau_ms": tau_ms,
        "dependency_lock_hash": dependency_lock_hash,
        "seed_manifest": {p.episode_id: p.seed for p in plans},
        "episode_manifest": [p.manifest() for p in plans],
        "row_count": row_count,
        "incomplete_trials_excluded": incomplete_trials,
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "disclaimer": (
            "Controlled injection on one shared host with synthetic documents. "
            "Not a measurement of real DID resolver behaviour."
        ),
    }


def save_dataset(path: Path, dataset: Dataset, manifest: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"record_type": "manifest", **manifest}) + "\n")
        for row in dataset.rows:
            handle.write(json.dumps({"record_type": "row", **row.to_dict()}) + "\n")
    return config_hash(manifest)


def load_dataset(path: Path) -> tuple[dict, list[DatasetRow]]:
    manifest: dict = {}
    rows: list[DatasetRow] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("record_type") == "manifest":
                manifest = payload
            else:
                rows.append(
                    DatasetRow(
                        episode_id=payload["episode_id"],
                        trial_index=payload["trial_index"],
                        split=payload["split"],
                        subset=tuple(payload["subset"]),
                        features=payload["features"],
                        target=payload["target"],
                        hidden_state=payload["hidden_state"],
                    )
                )
    return manifest, rows
