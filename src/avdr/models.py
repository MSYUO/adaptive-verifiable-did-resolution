"""Telemetry and API data models.

The separation between a LOGICAL REQUEST (one client-facing DID resolution)
and a RESOLVER ATTEMPT (one HTTP call to one resolver) is load-bearing. A
single logical request may contain several attempts under failover, so the
two must never be counted as the same thing.

Fields whose value is genuinely unavailable are typed Optional and recorded
as null. They are never back-filled with a plausible-looking default.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AttemptOutcome(str, Enum):
    """Terminal classification of a single resolver attempt."""

    ACCEPTED = "accepted"
    REJECTED_INVALID = "rejected_invalid"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    CANCELED = "canceled"


class ResolverAttempt(BaseModel):
    """One HTTP call from the router to one resolver."""

    record_type: str = "attempt"
    request_id: str
    attempt_index: int
    resolver_id: str
    resolver_url: str

    # Wall-clock boundaries (ISO-8601 UTC), for ordering across processes.
    start_ts: str
    end_ts: str
    # Duration from a monotonic clock; never derived from the wall clock.
    latency_ms: float

    http_status: int | None = None
    outcome: AttemptOutcome
    timeout: bool = False
    canceled: bool = False
    error: str | None = None

    # None means "not evaluated" (no response body was obtained), which is
    # distinct from False ("evaluated and found invalid").
    document_valid: bool | None = None
    acceptance_reason: str | None = None
    accepted: bool = False


class LogicalRequestRecord(BaseModel):
    """One client-facing DID resolution, spanning one or more attempts."""

    record_type: str = "logical_request"
    request_id: str
    timestamp: str
    did: str
    did_method: str | None = None
    routing_policy: str
    policy_version: str
    policy_target: str | None = None

    # What the policy planned versus what was actually contacted.
    candidate_sequence: list[str] = Field(default_factory=list)
    attempted_sequence: list[str] = Field(default_factory=list)

    returned_resolver: str | None = None
    logical_completion_latency_ms: float
    success: bool
    final_error: str | None = None

    attempt_count: int = 0
    # Distinct resolvers contacted. Equal to attempt_count for the sequential
    # policies in this milestone; kept separate because racing policies (not
    # implemented here) would make them diverge.
    fanout_count: int = 0
    canceled_count: int = 0

    attempt_timeout_ms: int


class ResolveSuccess(BaseModel):
    """Client-facing success payload."""

    request_id: str
    did: str
    routing_policy: str
    returned_resolver: str
    attempted_sequence: list[str]
    attempt_count: int
    logical_completion_latency_ms: float
    didDocument: dict


class ResolveFailure(BaseModel):
    """Client-facing failure payload. All attempts were exhausted."""

    request_id: str
    did: str
    routing_policy: str
    error: str
    attempted_sequence: list[str]
    attempt_count: int
    logical_completion_latency_ms: float
    attempt_outcomes: list[dict]
