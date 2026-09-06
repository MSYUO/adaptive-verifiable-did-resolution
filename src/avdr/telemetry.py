"""JSONL telemetry sink.

Logical requests and resolver attempts are written to two SEPARATE files so
that the two record levels cannot be accidentally aggregated together. They
are joined on ``request_id``.

Files are opened lazily on first write, so importing the router module has no
filesystem side effects.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path

from .models import (
    LogicalRequestRecord,
    RealProviderObservation,
    RealProviderTrialRecord,
    RealRoutingAttempt,
    RealRoutingRequestRecord,
    ResolverAttempt,
    ShadowObservation,
    ShadowTrialRecord,
)

REQUESTS_FILENAME = "requests.jsonl"
ATTEMPTS_FILENAME = "attempts.jsonl"
# Shadow measurement records are kept in their own files: they come from
# the characterization harness, not from serving client traffic, and the
# two must never be pooled into one dataset.
SHADOW_TRIALS_FILENAME = "shadow_trials.jsonl"
SHADOW_OBSERVATIONS_FILENAME = "shadow_observations.jsonl"
# Real-provider qualification records, again kept apart: these come from live
# third-party endpoints, not from controlled local resolvers.
REAL_TRIALS_FILENAME = "real_provider_trials.jsonl"
REAL_OBSERVATIONS_FILENAME = "real_provider_observations.jsonl"
# Exact response bodies, preserved outside the normalized records for audit.
RAW_RESPONSES_FILENAME = "raw_responses.jsonl"
# Real routing service records: user-facing traffic, kept apart from both the
# mock router's traffic and the measurement harness's observations.
REAL_ROUTING_REQUESTS_FILENAME = "real_routing_requests.jsonl"
REAL_ROUTING_ATTEMPTS_FILENAME = "real_routing_attempts.jsonl"


class TelemetrySink:
    """Append-only JSONL sink with a bounded in-memory index for querying."""

    def __init__(self, directory: str | Path, memory_limit: int = 2000) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._requests: deque[dict] = deque(maxlen=memory_limit)
        self._attempts: dict[str, list[dict]] = {}
        self._attempt_order: deque[str] = deque(maxlen=memory_limit)
        # Separate in-memory index for the real routing service.
        self._routing_requests: deque[dict] = deque(maxlen=memory_limit)
        self._routing_attempts: dict[str, list[dict]] = {}

    @property
    def requests_path(self) -> Path:
        return self.directory / REQUESTS_FILENAME

    @property
    def attempts_path(self) -> Path:
        return self.directory / ATTEMPTS_FILENAME

    @property
    def shadow_trials_path(self) -> Path:
        return self.directory / SHADOW_TRIALS_FILENAME

    @property
    def shadow_observations_path(self) -> Path:
        return self.directory / SHADOW_OBSERVATIONS_FILENAME

    @property
    def real_trials_path(self) -> Path:
        return self.directory / REAL_TRIALS_FILENAME

    @property
    def real_observations_path(self) -> Path:
        return self.directory / REAL_OBSERVATIONS_FILENAME

    @property
    def raw_responses_path(self) -> Path:
        return self.directory / RAW_RESPONSES_FILENAME

    @property
    def routing_requests_path(self) -> Path:
        return self.directory / REAL_ROUTING_REQUESTS_FILENAME

    @property
    def routing_attempts_path(self) -> Path:
        return self.directory / REAL_ROUTING_ATTEMPTS_FILENAME

    def _append(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def record_attempt(self, attempt: ResolverAttempt) -> None:
        payload = attempt.model_dump(mode="json")
        with self._lock:
            self._append(self.attempts_path, payload)
            bucket = self._attempts.setdefault(attempt.request_id, [])
            if not bucket:
                self._attempt_order.append(attempt.request_id)
                self._evict_locked()
            bucket.append(payload)

    def record_request(self, record: LogicalRequestRecord) -> None:
        payload = record.model_dump(mode="json")
        with self._lock:
            self._append(self.requests_path, payload)
            self._requests.append(payload)

    def _evict_locked(self) -> None:
        """Drop attempt buckets whose request_id has aged out of the index."""
        live = set(self._attempt_order)
        for request_id in list(self._attempts):
            if request_id not in live:
                del self._attempts[request_id]

    def get_request(self, request_id: str) -> dict | None:
        with self._lock:
            record = next(
                (r for r in reversed(self._requests) if r["request_id"] == request_id),
                None,
            )
            attempts = list(self._attempts.get(request_id, []))
        if record is None and not attempts:
            return None
        return {"request": record, "attempts": attempts}

    def recent_requests(self, limit: int = 20) -> list[dict]:
        with self._lock:
            records = list(self._requests)[-limit:]
            return [
                {"request": r, "attempts": list(self._attempts.get(r["request_id"], []))}
                for r in records
            ]

    def record_shadow_observation(self, observation: ShadowObservation) -> None:
        with self._lock:
            self._append(
                self.shadow_observations_path, observation.model_dump(mode="json")
            )

    def record_shadow_trial(self, trial: ShadowTrialRecord) -> None:
        with self._lock:
            self._append(self.shadow_trials_path, trial.model_dump(mode="json"))

    def record_real_observation(self, observation: RealProviderObservation) -> None:
        with self._lock:
            self._append(
                self.real_observations_path, observation.model_dump(mode="json")
            )

    def record_real_trial(self, trial: RealProviderTrialRecord) -> None:
        with self._lock:
            self._append(self.real_trials_path, trial.model_dump(mode="json"))

    def record_raw_response(
        self,
        *,
        experiment_id: str,
        trial_id: str,
        provider_id: str,
        raw_response_hash: str | None,
        body: object,
    ) -> None:
        """Preserve the exact parsed body for later audit.

        Kept out of the normalized observation so the trace stays small, but
        never discarded: a later reviewer must be able to re-derive any
        normalization decision from what the provider actually returned.
        """
        with self._lock:
            self._append(
                self.raw_responses_path,
                {
                    "record_type": "raw_response",
                    "experiment_id": experiment_id,
                    "trial_id": trial_id,
                    "provider_id": provider_id,
                    "raw_response_hash": raw_response_hash,
                    "body": body,
                },
            )

    def record_routing_attempt(self, attempt: RealRoutingAttempt) -> None:
        payload = attempt.model_dump(mode="json")
        with self._lock:
            self._append(self.routing_attempts_path, payload)
            self._routing_attempts.setdefault(attempt.request_id, []).append(payload)

    def record_routing_request(self, record: RealRoutingRequestRecord) -> None:
        payload = record.model_dump(mode="json")
        with self._lock:
            self._append(self.routing_requests_path, payload)
            self._routing_requests.append(payload)
            # Bound the attempt index to the requests still retained.
            live = {r["request_id"] for r in self._routing_requests}
            for request_id in list(self._routing_attempts):
                if request_id not in live:
                    del self._routing_attempts[request_id]

    def get_routing_request(self, request_id: str) -> dict | None:
        with self._lock:
            record = next(
                (
                    r
                    for r in reversed(self._routing_requests)
                    if r["request_id"] == request_id
                ),
                None,
            )
            attempts = list(self._routing_attempts.get(request_id, []))
        if record is None and not attempts:
            return None
        return {"request": record, "attempts": attempts}

    def recent_routing_requests(self, limit: int = 20) -> list[dict]:
        with self._lock:
            records = list(self._routing_requests)[-limit:]
            return [
                {
                    "request": r,
                    "attempts": list(self._routing_attempts.get(r["request_id"], [])),
                }
                for r in records
            ]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "logical_requests_in_memory": len(self._requests),
                "attempts_in_memory": sum(len(v) for v in self._attempts.values()),
            }
