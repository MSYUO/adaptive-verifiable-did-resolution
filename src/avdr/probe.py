"""One instrumented request to one real provider.

Shared by the measurement harness (`real_shadow.py`) and the real routing
service (`real_router/`), so a routing attempt and a shadow observation are
produced by exactly the same call path and are therefore comparable.

Cancellation honesty: the probe records whether the HTTP request had already
been dispatched when it was cancelled. Once a request is on the wire the
provider may already have done the work, so cancellation is never reported as
if it had prevented that.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .adapters import NormalizedResult, get_adapter
from .inventory import ProviderEntry

# HTTP statuses treated as "back off now". 429 is the standard signal; 403 is
# included because a public instance was observed returning it transiently.
THROTTLE_STATUSES = frozenset({429, 403})

# Cancellation outcomes. Deliberately distinguishes what we know from what we
# cannot know.
COMPLETED = "completed"
CANCELED_BEFORE_DISPATCH = "canceled_before_dispatch"
CANCELED_AFTER_DISPATCH = "canceled_after_dispatch_provider_side_unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DispatchState:
    """Tracks how far a probe got, so cancellation can be described exactly.

    Launch timing is captured here as well as in the returned outcome, because
    a cancelled probe never returns an outcome. Invariant: once `dispatched`
    is true, `launch_offset_ms` has already been recorded, so a cancelled
    dispatched attempt can still report when it was launched.
    """

    dispatched: bool = False
    launch_offset_ms: float | None = None
    start_ts: str | None = None
    telemetry_error: str | None = None


@dataclass
class ProbeOutcome:
    provider_id: str
    implementation_id: str | None
    resolver_endpoint_id: str
    launch_offset_ms: float
    start_ts: str
    end_ts: str
    latency_ms: float
    http_status: int | None
    content_type: str | None
    transport_outcome: str
    retry_after: str | None
    throttled: bool
    normalized: NormalizedResult
    dispatched: bool = True
    raw_body: object = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return self.normalized.accepted


async def probe_provider(
    client: httpx.AsyncClient,
    provider: ProviderEntry,
    did: str,
    timeout_ms: int,
    launch_offset_ms: float,
    dispatch_state: DispatchState | None = None,
) -> ProbeOutcome:
    """Issue one request to one provider and normalize the response."""
    adapter = get_adapter(provider.adapter)
    url = provider.endpoint.rstrip("/") + adapter.resolve_path(did)
    state = dispatch_state or DispatchState()

    start_ts = utc_now_iso()
    started = time.perf_counter()
    # Recorded before dispatch so the value survives cancellation.
    state.launch_offset_ms = round(launch_offset_ms, 3)
    state.start_ts = start_ts

    http_status = content_type = None
    raw_bytes = body = parse_error = retry_after = None
    transport_ok = False
    transport_outcome = "unknown"
    throttled = False

    headers = {}
    accept = adapter.accept_header()
    if accept:
        headers["Accept"] = accept

    try:
        # Marked immediately before the request leaves: after this point a
        # cancellation cannot be claimed to have spared the provider.
        state.dispatched = True
        response = await client.get(
            url, timeout=timeout_ms / 1000.0, headers=headers
        )
        http_status = response.status_code
        content_type = response.headers.get("content-type")
        raw_bytes = response.content
        retry_after = response.headers.get("retry-after")
        transport_ok = True
        transport_outcome = "http_response"
        throttled = http_status in THROTTLE_STATUSES
        try:
            body = response.json()
        except ValueError as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    except httpx.TimeoutException:
        transport_outcome = "timeout"
    except httpx.TransportError as exc:
        transport_outcome = "connection_error"
        parse_error = f"{type(exc).__name__}: {exc}"

    latency_ms = (time.perf_counter() - started) * 1000.0

    normalized = adapter.normalize(
        requested_did=did,
        http_status=http_status,
        content_type=content_type,
        raw_bytes=raw_bytes,
        transport_outcome=transport_outcome,
        transport_ok=transport_ok,
        parse_error=parse_error,
        body=body,
    )

    return ProbeOutcome(
        provider_id=provider.id,
        implementation_id=provider.implementation_id,
        resolver_endpoint_id=provider.endpoint,
        launch_offset_ms=round(launch_offset_ms, 3),
        start_ts=start_ts,
        end_ts=utc_now_iso(),
        latency_ms=round(latency_ms, 3),
        http_status=http_status,
        content_type=content_type,
        transport_outcome=transport_outcome,
        retry_after=retry_after,
        throttled=throttled,
        normalized=normalized,
        dispatched=state.dispatched,
        raw_body=normalized.raw_body,
    )
