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

from .models import LogicalRequestRecord, ResolverAttempt

REQUESTS_FILENAME = "requests.jsonl"
ATTEMPTS_FILENAME = "attempts.jsonl"


class TelemetrySink:
    """Append-only JSONL sink with a bounded in-memory index for querying."""

    def __init__(self, directory: str | Path, memory_limit: int = 2000) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._requests: deque[dict] = deque(maxlen=memory_limit)
        self._attempts: dict[str, list[dict]] = {}
        self._attempt_order: deque[str] = deque(maxlen=memory_limit)

    @property
    def requests_path(self) -> Path:
        return self.directory / REQUESTS_FILENAME

    @property
    def attempts_path(self) -> Path:
        return self.directory / ATTEMPTS_FILENAME

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

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "logical_requests_in_memory": len(self._requests),
                "attempts_in_memory": sum(len(v) for v in self._attempts.values()),
            }
