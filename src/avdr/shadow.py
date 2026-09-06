"""Shadow characterization harness -- MEASUREMENT ONLY.

Why this exists: the routing policies observe only the resolvers they attempt.
Sequential failover stops at the first acceptable response, so its traces can
never compare

    argmin_i T_i        against        argmin_{i : accepted_i} T_i

because the resolvers that were not attempted have no observation at all. To
compare those quantities every candidate resolver must be observed for the
SAME logical context. That is what a shadow trial does.

  THIS IS NOT A ROUTING POLICY.

It is deliberately not registered in ``router.policies.POLICY_TYPES``, is never
reachable from the router's request path, and never serves a client. Probing
every resolver on every request is exactly the all-race behaviour the project
is trying to avoid in production; here it is an instrument, not a service.

Local-deployment caveat: all resolvers share one host (CPU, kernel, network
stack, Docker engine). Latency differences observed here are dominated by
CONTROLLED INJECTION and shared-host effects, and must not be read as DID
infrastructure heterogeneity. This phase qualifies the apparatus only.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from .acceptance import parse_did_method
from .config import RouterConfig
from .models import ShadowObservation, ShadowTrialRecord
from .provenance import Provenance, new_trial_id
from .router.transport import dispatch_attempt

PARALLEL = "parallel"
SEQUENTIAL = "sequential"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ShadowTrial:
    """One trial's results: the trial record plus its observations."""

    def __init__(
        self, record: ShadowTrialRecord, observations: list[ShadowObservation]
    ) -> None:
        self.record = record
        self.observations = observations

    @property
    def complete(self) -> bool:
        return self.record.complete


class ShadowProbe:
    """Probes every candidate resolver for one logical trial context."""

    def __init__(self, config: RouterConfig, timeout_ms: int | None = None) -> None:
        self.config = config
        # Defaults to the router's configured deadline so shadow observations
        # are censored at the same boundary the router would apply.
        self.timeout_ms = timeout_ms or config.attempt_timeout_ms

    async def run_trial(
        self,
        client: httpx.AsyncClient,
        did: str,
        provenance: Provenance,
        mode: str = PARALLEL,
        trial_id: str | None = None,
    ) -> ShadowTrial:
        """Observe every configured resolver for one DID.

        In parallel mode all probes are launched from the same trial start;
        the per-probe launch offset is recorded so the actual skew is visible
        rather than assumed to be zero.
        """
        if mode not in (PARALLEL, SEQUENTIAL):
            raise ValueError(f"unknown shadow mode {mode!r}")

        trial = provenance.for_trial(trial_id or new_trial_id())
        endpoints = list(self.config.resolvers)
        expected = len(endpoints)

        timestamp = _utc_now_iso()
        trial_started = time.perf_counter()

        async def probe(index: int, endpoint) -> ShadowObservation:
            # Recorded at the moment this probe actually begins running, not
            # when it was scheduled.
            launch_offset_ms = (time.perf_counter() - trial_started) * 1000.0
            result = await dispatch_attempt(
                client=client,
                endpoint=endpoint,
                did=did,
                request_id=trial.trial_id,
                attempt_index=index,
                timeout_ms=self.timeout_ms,
            )
            attempt = result.attempt
            return ShadowObservation(
                experiment_id=trial.experiment_id,
                trial_id=trial.trial_id,
                scenario_id=trial.scenario_id,
                phase=trial.phase,
                seed=trial.seed,
                git_commit=trial.git_commit,
                git_dirty=trial.git_dirty,
                config_hash=trial.config_hash,
                injection_config_hash=trial.injection_config_hash,
                did=did,
                did_method=parse_did_method(did),
                resolver_id=endpoint.id,
                resolver_url=endpoint.url,
                launch_offset_ms=round(launch_offset_ms, 3),
                start_ts=attempt.start_ts,
                end_ts=attempt.end_ts,
                latency_ms=attempt.latency_ms,
                http_status=attempt.http_status,
                outcome=attempt.outcome,
                timeout=attempt.timeout,
                error=attempt.error,
                document_valid=attempt.document_valid,
                acceptance_reason=attempt.acceptance_reason,
                accepted=attempt.accepted,
            )

        observations: list[ShadowObservation] = []
        failures: list[str] = []

        if mode == PARALLEL:
            results = await asyncio.gather(
                *(probe(i, ep) for i, ep in enumerate(endpoints)),
                return_exceptions=True,
            )
            for endpoint, result in zip(endpoints, results):
                if isinstance(result, BaseException):
                    # The probe itself broke. Record the gap; never synthesise
                    # an observation to make the trial look complete.
                    failures.append(
                        f"{endpoint.id}: probe raised {type(result).__name__}: {result}"
                    )
                else:
                    observations.append(result)
        else:
            for index, endpoint in enumerate(endpoints):
                try:
                    observations.append(await probe(index, endpoint))
                except Exception as exc:  # noqa: BLE001 - recorded, not hidden
                    failures.append(
                        f"{endpoint.id}: probe raised {type(exc).__name__}: {exc}"
                    )

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

        record = ShadowTrialRecord(
            experiment_id=trial.experiment_id,
            trial_id=trial.trial_id,
            scenario_id=trial.scenario_id,
            phase=trial.phase,
            seed=trial.seed,
            git_commit=trial.git_commit,
            git_dirty=trial.git_dirty,
            config_hash=trial.config_hash,
            injection_config_hash=trial.injection_config_hash,
            timestamp=timestamp,
            did=did,
            did_method=parse_did_method(did),
            mode=mode,
            expected_observations=expected,
            actual_observations=len(observations),
            complete=complete,
            incomplete_reason=incomplete_reason,
            launch_skew_ms=launch_skew_ms,
            trial_duration_ms=round(trial_duration_ms, 3),
        )
        return ShadowTrial(record, observations)
