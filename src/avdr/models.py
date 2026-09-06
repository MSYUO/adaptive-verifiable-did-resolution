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


class ShadowObservation(BaseModel):
    """One resolver's outcome within a shadow characterization trial.

    A shadow trial probes EVERY candidate resolver for the same logical
    context, so unlike a routing attempt these observations are complete
    across the topology and can support counterfactual comparison.

    Measurement-only. Shadow probing is not a routing policy and never serves
    a client request.
    """

    record_type: str = "shadow_observation"

    # Provenance -- identical across every observation of one trial.
    experiment_id: str
    trial_id: str
    scenario_id: str
    phase: str
    seed: int | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    config_hash: str | None = None
    injection_config_hash: str | None = None

    did: str
    did_method: str | None = None

    resolver_id: str
    resolver_url: str

    # Monotonic offset from trial start to the moment this probe was issued.
    # Differences across observations give the launch skew.
    launch_offset_ms: float

    start_ts: str
    end_ts: str
    latency_ms: float

    http_status: int | None = None
    outcome: AttemptOutcome
    timeout: bool = False
    error: str | None = None

    document_valid: bool | None = None
    acceptance_reason: str | None = None
    accepted: bool = False

    @property
    def responded(self) -> bool:
        """True when a complete HTTP response was received.

        Timeouts and connection errors are right-censored: the resolver may
        have been about to answer. Their latency is a lower bound, not a
        comparable completion time, so they must not enter a min() over
        response times.
        """
        return self.http_status is not None


class ShadowTrialRecord(BaseModel):
    """Trial-level summary binding a set of shadow observations."""

    record_type: str = "shadow_trial"

    experiment_id: str
    trial_id: str
    scenario_id: str
    phase: str
    seed: int | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    config_hash: str | None = None
    injection_config_hash: str | None = None

    timestamp: str
    did: str
    did_method: str | None = None

    # "parallel" probes all resolvers concurrently; "sequential" probes them
    # one after another. Neither is a routing policy.
    mode: str

    expected_observations: int
    actual_observations: int
    complete: bool
    incomplete_reason: str | None = None

    # max(launch_offset) - min(launch_offset). Null when fewer than two
    # observations exist. Concurrent start is approximate, never perfect.
    launch_skew_ms: float | None = None
    trial_duration_ms: float


class RealProviderObservation(BaseModel):
    """One real resolver provider's outcome within a multi-provider trial.

    Provider/API differences (media type, metadata presence, route metadata)
    are recorded as provider properties. They are deliberately kept distinct
    from DID semantics so the two can never be conflated downstream.
    """

    record_type: str = "real_provider_observation"

    # Provenance -- identical across every observation of one trial.
    experiment_id: str
    trial_id: str
    scenario_id: str
    phase: str
    seed: int | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    config_hash: str | None = None
    injection_config_hash: str | None = None
    provider_inventory_hash: str | None = None
    fixture_manifest_hash: str | None = None
    acceptance_profile: str | None = None

    # Connection discipline.
    connection_mode: str
    client_reuse_policy: str
    launch_order_seed: int | None = None

    fixture_id: str
    requested_did: str
    did_method: str

    provider_id: str
    implementation_id: str | None = None
    resolver_endpoint_id: str

    launch_position: int
    launch_offset_ms: float
    start_ts: str
    end_ts: str
    latency_ms: float

    http_status: int | None = None
    content_type: str | None = None
    transport_outcome: str

    # Audit trail for the exact bytes received.
    raw_response_hash: str | None = None
    raw_response_bytes: int | None = None

    resolution_metadata: dict | None = None
    normalized_did_document: dict | None = None
    did_document_metadata: dict | None = None
    provider_route_metadata: dict | None = None
    normalized_document_hash: str | None = None

    resolution_error_family: str | None = None
    resolution_error_detail: str | None = None
    subject_id: str | None = None

    # Individual profile checks; `accepted` is derived from these.
    acceptance_checks: dict[str, bool | None] = Field(default_factory=dict)
    accepted: bool = False
    acceptance_reason: str | None = None

    retry_after: str | None = None


class RealProviderTrialRecord(BaseModel):
    """Trial-level record binding a set of real-provider observations."""

    record_type: str = "real_provider_trial"

    experiment_id: str
    trial_id: str
    scenario_id: str
    phase: str
    seed: int | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    config_hash: str | None = None
    injection_config_hash: str | None = None
    provider_inventory_hash: str | None = None
    fixture_manifest_hash: str | None = None
    acceptance_profile: str | None = None

    timestamp: str
    fixture_id: str
    requested_did: str
    did_method: str

    connection_mode: str
    client_reuse_policy: str
    launch_order_seed: int | None = None
    launch_order: list[str] = Field(default_factory=list)

    expected_observations: int
    actual_observations: int
    complete: bool
    incomplete_reason: str | None = None

    launch_skew_ms: float | None = None
    trial_duration_ms: float

    rate_limited_providers: list[str] = Field(default_factory=list)
