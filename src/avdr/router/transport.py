"""HTTP transport: one resolver attempt, fully instrumented.

Separated from policy logic. This module knows how to call a resolver and how
to classify what came back; it does not know which resolver to call or why.

Timing discipline:
  * duration uses time.perf_counter() (monotonic) -- never wall-clock deltas
  * start_ts/end_ts use timezone-aware UTC wall clock, for cross-process
    ordering only

Latency is recorded even for timeouts and connection errors, because time was
genuinely spent; it is never nulled or zeroed to paper over a failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..acceptance import check_did_document
from ..config import ResolverEndpoint
from ..models import AttemptOutcome, ResolverAttempt

RESOLVE_PATH = "/1.0/identifiers/{did}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AttemptResult:
    """An attempt's telemetry record plus, if accepted, the response body.

    The document is kept beside the telemetry record rather than inside it,
    so telemetry stays small and the payload never leaks into the JSONL trace.
    """

    attempt: ResolverAttempt
    payload: dict | None = None


async def dispatch_attempt(
    client: httpx.AsyncClient,
    endpoint: ResolverEndpoint,
    did: str,
    request_id: str,
    attempt_index: int,
    timeout_ms: int,
) -> AttemptResult:
    """Perform exactly one resolver call and classify the outcome."""
    url = endpoint.url.rstrip("/") + RESOLVE_PATH.format(did=did)
    timeout_s = timeout_ms / 1000.0

    start_ts = _utc_now_iso()
    started = time.perf_counter()

    def record(**kwargs) -> ResolverAttempt:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return ResolverAttempt(
            request_id=request_id,
            attempt_index=attempt_index,
            resolver_id=endpoint.id,
            resolver_url=endpoint.url,
            start_ts=start_ts,
            end_ts=_utc_now_iso(),
            latency_ms=round(elapsed_ms, 3),
            **kwargs,
        )

    try:
        response = await client.get(url, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        return AttemptResult(
            record(
                http_status=None,
                outcome=AttemptOutcome.TIMEOUT,
                timeout=True,
                error=f"timeout after {timeout_ms}ms: {type(exc).__name__}",
                document_valid=None,
                acceptance_reason=None,
                accepted=False,
            )
        )
    except httpx.TransportError as exc:
        return AttemptResult(
            record(
                http_status=None,
                outcome=AttemptOutcome.CONNECTION_ERROR,
                error=f"{type(exc).__name__}: {exc}",
                document_valid=None,
                acceptance_reason=None,
                accepted=False,
            )
        )

    if response.status_code >= 400:
        return AttemptResult(
            record(
                http_status=response.status_code,
                outcome=AttemptOutcome.HTTP_ERROR,
                error=f"HTTP {response.status_code}",
                # No document was evaluated: explicitly unknown, not False.
                document_valid=None,
                acceptance_reason=None,
                accepted=False,
            )
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return AttemptResult(
            record(
                http_status=response.status_code,
                outcome=AttemptOutcome.REJECTED_INVALID,
                error=f"response body is not JSON: {exc}",
                document_valid=False,
                acceptance_reason="response body is not JSON",
                accepted=False,
            )
        )

    verdict = check_did_document(did, payload)
    if not verdict.valid:
        return AttemptResult(
            record(
                http_status=response.status_code,
                outcome=AttemptOutcome.REJECTED_INVALID,
                error=None,
                document_valid=False,
                acceptance_reason=verdict.reason,
                accepted=False,
            )
        )

    return AttemptResult(
        record(
            http_status=response.status_code,
            outcome=AttemptOutcome.ACCEPTED,
            error=None,
            document_valid=True,
            acceptance_reason=verdict.reason,
            accepted=True,
        ),
        payload=payload,
    )
