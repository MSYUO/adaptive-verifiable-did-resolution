"""Multi-provider shadow harness for REAL resolvers -- MEASUREMENT ONLY.

Kept separate from `shadow.py` so the already-qualified local harness is not
disturbed. Same discipline applies:

  THIS IS NOT A ROUTING POLICY.

It issues the same DID request to every provider qualified for that DID
method, within one trial, purely to characterise the apparatus.

Connection modes (deliberately explicit, because DNS/TCP/TLS reuse otherwise
silently confounds any real-network measurement):

  new-client     A fresh httpx.AsyncClient, and therefore a fresh connection
                 pool, is constructed for each trial. This is NOT a cold
                 TCP/TLS measurement: the OS and the recursive DNS resolver
                 keep their own caches, TLS session tickets may be reused by
                 the platform, and no attempt is made to flush either. It is
                 named "new-client" precisely so it is not read as "cold".

  reused-client  One client is shared across trials, so keep-alive and any
                 pooled TLS session are reused where the server permits.

Never mix modes inside one derived comparison.

Rate-limit discipline: public endpoints are testing instances. Trial counts
are tiny, pacing is enforced by the caller, and HTTP 429 / Retry-After is
surfaced rather than retried around.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone

import httpx

from .inventory import ProviderEntry
from .models import RealProviderObservation, RealProviderTrialRecord
from .probe import THROTTLE_STATUSES, probe_provider  # noqa: F401
from .provenance import Provenance, new_trial_id

NEW_CLIENT = "new-client"
REUSED_CLIENT = "reused-client"

# THROTTLE_STATUSES is re-exported from .probe so the measurement harness and
# the routing service share one definition of "back off now".

CONNECTION_MODES = {
    NEW_CLIENT: "a fresh AsyncClient and connection pool per trial; OS/DNS "
                "caches and platform TLS session reuse are NOT flushed",
    REUSED_CLIENT: "one AsyncClient shared across trials; keep-alive and "
                   "pooled TLS sessions reused where the server permits",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RateLimitSignal(Exception):
    """Raised when a provider asks us to back off. Never retried around."""

    def __init__(self, provider_id: str, retry_after: str | None) -> None:
        super().__init__(
            f"provider {provider_id} returned HTTP 429 (Retry-After={retry_after})"
        )
        self.provider_id = provider_id
        self.retry_after = retry_after


class RealProviderTrial:
    def __init__(
        self,
        record: RealProviderTrialRecord,
        observations: list[RealProviderObservation],
        raw_bodies: dict[str, object],
    ) -> None:
        self.record = record
        self.observations = observations
        # provider_id -> raw parsed body, kept outside the normalized record.
        self.raw_bodies = raw_bodies

    @property
    def complete(self) -> bool:
        return self.record.complete


class RealProviderProbe:
    """Probes every qualified provider for one DID, within one trial."""

    def __init__(self, timeout_ms: int = 30000) -> None:
        self.timeout_ms = timeout_ms

    def launch_order(
        self, providers: list[ProviderEntry], seed: int, trial_index: int
    ) -> list[ProviderEntry]:
        """Deterministic per-trial rotation of provider launch order.

        Rotating matters because launch skew is not zero: a provider that is
        always launched first would enjoy a systematic head start.
        """
        ordered = list(providers)
        # A string seed keeps the derivation explicit and reproducible.
        # (Tuple seeds are not accepted by random.Random on Python 3.12+.)
        random.Random(f"{seed}:{trial_index}").shuffle(ordered)
        return ordered

    async def run_trial(
        self,
        client: httpx.AsyncClient,
        did: str,
        fixture_id: str,
        did_method: str,
        providers: list[ProviderEntry],
        provenance: Provenance,
        connection_mode: str,
        launch_order_seed: int,
        trial_index: int = 0,
        trial_id: str | None = None,
    ) -> RealProviderTrial:
        if connection_mode not in CONNECTION_MODES:
            raise ValueError(f"unknown connection mode {connection_mode!r}")
        if not providers:
            raise ValueError("no providers supplied for trial")

        trial = provenance.for_trial(trial_id or new_trial_id())
        ordered = self.launch_order(providers, launch_order_seed, trial_index)
        expected = len(ordered)

        timestamp = _utc_now_iso()
        trial_started = time.perf_counter()
        rate_limited: list[str] = []

        async def probe(position: int, provider: ProviderEntry):
            launch_offset_ms = (time.perf_counter() - trial_started) * 1000.0
            # Shared call path with the routing service: a shadow observation
            # and a routing attempt are produced by identical code.
            outcome = await probe_provider(
                client, provider, did, self.timeout_ms, launch_offset_ms
            )
            if outcome.throttled:
                rate_limited.append(provider.id)
            normalized = outcome.normalized

            observation = RealProviderObservation(
                experiment_id=trial.experiment_id,
                trial_id=trial.trial_id,
                scenario_id=trial.scenario_id,
                phase=trial.phase,
                seed=trial.seed,
                git_commit=trial.git_commit,
                git_dirty=trial.git_dirty,
                config_hash=trial.config_hash,
                injection_config_hash=trial.injection_config_hash,
                provider_inventory_hash=trial.provider_inventory_hash,
                fixture_manifest_hash=trial.fixture_manifest_hash,
                acceptance_profile=normalized.acceptance.profile,
                connection_mode=connection_mode,
                client_reuse_policy=CONNECTION_MODES[connection_mode],
                launch_order_seed=launch_order_seed,
                fixture_id=fixture_id,
                requested_did=did,
                did_method=did_method,
                provider_id=provider.id,
                implementation_id=provider.implementation_id,
                resolver_endpoint_id=provider.endpoint,
                launch_position=position,
                launch_offset_ms=outcome.launch_offset_ms,
                start_ts=outcome.start_ts,
                end_ts=outcome.end_ts,
                latency_ms=outcome.latency_ms,
                http_status=outcome.http_status,
                content_type=outcome.content_type,
                transport_outcome=outcome.transport_outcome,
                raw_response_hash=normalized.raw_response_hash,
                raw_response_bytes=normalized.raw_response_bytes,
                resolution_metadata=normalized.resolution_metadata,
                normalized_did_document=normalized.normalized_did_document,
                did_document_metadata=normalized.did_document_metadata,
                provider_route_metadata=normalized.provider_route_metadata,
                normalized_document_hash=normalized.normalized_document_hash,
                resolution_error_family=normalized.resolution_error_family,
                resolution_error_detail=_error_detail(normalized.resolution_error),
                subject_id=normalized.subject_id,
                acceptance_checks=dict(normalized.acceptance.checks),
                accepted=normalized.accepted,
                acceptance_reason=normalized.acceptance.reason,
                retry_after=outcome.retry_after,
            )
            return observation, outcome.raw_body

        results = await asyncio.gather(
            *(probe(i, p) for i, p in enumerate(ordered)), return_exceptions=True
        )

        observations: list[RealProviderObservation] = []
        raw_bodies: dict[str, object] = {}
        failures: list[str] = []
        for provider, result in zip(ordered, results):
            if isinstance(result, BaseException):
                failures.append(
                    f"{provider.id}: probe raised {type(result).__name__}: {result}"
                )
            else:
                observation, raw_body = result
                observations.append(observation)
                raw_bodies[provider.id] = raw_body

        trial_duration_ms = (time.perf_counter() - trial_started) * 1000.0

        launch_skew_ms = None
        if len(observations) >= 2:
            offsets = [o.launch_offset_ms for o in observations]
            launch_skew_ms = round(max(offsets) - min(offsets), 3)

        complete = len(observations) == expected and not failures
        incomplete_reason = None
        if not complete:
            parts = []
            if len(observations) != expected:
                parts.append(
                    f"expected {expected} observations, recorded {len(observations)}"
                )
            parts.extend(failures)
            incomplete_reason = "; ".join(parts)

        record = RealProviderTrialRecord(
            experiment_id=trial.experiment_id,
            trial_id=trial.trial_id,
            scenario_id=trial.scenario_id,
            phase=trial.phase,
            seed=trial.seed,
            git_commit=trial.git_commit,
            git_dirty=trial.git_dirty,
            config_hash=trial.config_hash,
            injection_config_hash=trial.injection_config_hash,
            provider_inventory_hash=trial.provider_inventory_hash,
            fixture_manifest_hash=trial.fixture_manifest_hash,
            acceptance_profile=(
                observations[0].acceptance_profile if observations else None
            ),
            timestamp=timestamp,
            fixture_id=fixture_id,
            requested_did=did,
            did_method=did_method,
            connection_mode=connection_mode,
            client_reuse_policy=CONNECTION_MODES[connection_mode],
            launch_order_seed=launch_order_seed,
            launch_order=[p.id for p in ordered],
            expected_observations=expected,
            actual_observations=len(observations),
            complete=complete,
            incomplete_reason=incomplete_reason,
            launch_skew_ms=launch_skew_ms,
            trial_duration_ms=round(trial_duration_ms, 3),
            rate_limited_providers=rate_limited,
        )
        return RealProviderTrial(record, observations, raw_bodies)


def _error_detail(resolution_error) -> str | None:
    if resolution_error is None:
        return None
    if isinstance(resolution_error, dict):
        import json as _json

        return _json.dumps(resolution_error, ensure_ascii=False)[:500]
    return str(resolution_error)[:500]


def compare_documents(observations: list[RealProviderObservation]) -> dict:
    """Record how provider results differ. Judges nobody.

    A difference is reported as PROVIDER_RESULT_DIFFERENCE_OBSERVED. It is
    NOT called stale, invalid, incorrect or Byzantine: without method-specific
    ground truth, differing conforming representations of the same subject are
    simply different, and for did:key in particular both observed shapes carry
    identical key material.
    """
    accepted = [o for o in observations if o.accepted]
    hashes = {o.provider_id: o.normalized_document_hash for o in accepted}
    distinct = {h for h in hashes.values() if h is not None}
    subjects = {o.provider_id: o.subject_id for o in accepted}

    exact_subject_match = (
        len(accepted) > 0
        and len({s for s in subjects.values() if s is not None}) == 1
        and all(o.subject_id == o.requested_did for o in accepted)
    )

    metadata_presence = {
        o.provider_id: {
            "resolution_metadata": o.resolution_metadata is not None,
            "did_document_metadata": o.did_document_metadata is not None,
            "provider_route_metadata": o.provider_route_metadata is not None,
        }
        for o in observations
    }

    return {
        "accepted_provider_count": len(accepted),
        "exact_subject_match": exact_subject_match,
        "subjects": subjects,
        "normalized_document_hashes": hashes,
        "distinct_document_hashes": len(distinct),
        "difference_observed": len(distinct) > 1,
        "flag": "PROVIDER_RESULT_DIFFERENCE_OBSERVED" if len(distinct) > 1 else None,
        "metadata_presence": metadata_presence,
    }
