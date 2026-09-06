"""Resolver behaviour configuration.

ALL experimental behaviour (delay, error, invalidity, timeout) belongs here,
on the resolver side. The router must never contain scenario-specific logic.

Every value below is a CONTROLLED INJECTION knob. Nothing measured through
these knobs describes real-world DID resolver behaviour.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


class ResolverBehavior(BaseModel):
    """Injected behaviour of a single mock resolver instance."""

    # Sleep before responding. CONTROLLED INJECTION.
    artificial_delay_ms: int = Field(default=0, ge=0)

    # Respond with an HTTP error status instead of a document.
    force_error: bool = False
    force_error_status: int = Field(default=503, ge=400, le=599)

    # Respond 200 with a structurally unacceptable document.
    force_invalid: bool = False

    # Sleep far longer than any sane router timeout, to force a client-side
    # timeout without closing the connection.
    force_timeout: bool = False
    timeout_sleep_ms: int = Field(default=30000, ge=0)

    # Deterministic failure mode: fail every Nth request (0 disables).
    # Deterministic given a fixed request ordering, unlike a random rate.
    deterministic_failure_every_n: int = Field(default=0, ge=0)


class ResolverSettings(BaseModel):
    resolver_id: str
    port: int = 8001
    behavior: ResolverBehavior


def load_resolver_settings() -> ResolverSettings:
    """Build resolver settings from environment variables."""
    return ResolverSettings(
        resolver_id=os.environ.get("RESOLVER_ID", "resolver-local"),
        port=_env_int("RESOLVER_PORT", 8001),
        behavior=ResolverBehavior(
            artificial_delay_ms=_env_int("ARTIFICIAL_DELAY_MS", 0),
            force_error=_env_bool("FORCE_ERROR", False),
            force_error_status=_env_int("FORCE_ERROR_STATUS", 503),
            force_invalid=_env_bool("FORCE_INVALID", False),
            force_timeout=_env_bool("FORCE_TIMEOUT", False),
            timeout_sleep_ms=_env_int("TIMEOUT_SLEEP_MS", 30000),
            deterministic_failure_every_n=_env_int("DETERMINISTIC_FAILURE_EVERY_N", 0),
        ),
    )
