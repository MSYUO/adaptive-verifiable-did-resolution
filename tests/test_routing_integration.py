"""Integration tests: router over three real mock resolver processes.

Every delay/error/invalidity here is a CONTROLLED INJECTION applied to a
resolver instance. None of it measures real DID resolver behaviour, and the
three instances share one host, so they are not independent gateways.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ATTEMPT_TIMEOUT_MS, INJECTED_DELAY_MS, TIMEOUT_SLEEP_MS

DID = "did:example:subject-1"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --------------------------------------------------------------------------
# Test 1 -- single-resolver functionality (the PASS_LOCAL_SINGLE_RESOLVER_E2E
# path), exercised through the single-static control policy.
# --------------------------------------------------------------------------


async def test_single_static_e2e_returns_valid_document(router_client, sink):
    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "single-static"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["returned_resolver"] == "resolver-a"
    assert body["attempted_sequence"] == ["resolver-a"]
    assert body["attempt_count"] == 1
    assert body["didDocument"]["id"] == DID
    assert body["logical_completion_latency_ms"] >= 0

    telemetry = sink.get_request(body["request_id"])
    assert telemetry["request"]["success"] is True
    assert telemetry["request"]["returned_resolver"] == "resolver-a"
    assert len(telemetry["attempts"]) == 1
    attempt = telemetry["attempts"][0]
    assert attempt["outcome"] == "accepted"
    assert attempt["accepted"] is True
    assert attempt["document_valid"] is True
    assert attempt["http_status"] == 200


async def test_single_static_has_no_failover(router_client, healthy_cluster):
    """The control baseline must fail rather than silently repair itself."""
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "single-static"}
    )
    assert response.status_code == 502
    body = response.json()
    assert body["attempted_sequence"] == ["resolver-a"]
    assert body["attempt_count"] == 1


async def test_health_and_policies_endpoints(router_client):
    health = (await router_client.get("/health")).json()
    assert health["status"] == "ok"
    assert [r["id"] for r in health["resolvers"]] == [
        "resolver-a",
        "resolver-b",
        "resolver-c",
    ]
    assert health["attempt_timeout_ms"] == ATTEMPT_TIMEOUT_MS

    policies = (await router_client.get("/policies")).json()
    assert policies["available"] == [
        "round-robin",
        "sequential-failover",
        "single-static",
    ]


async def test_unknown_policy_is_rejected(router_client):
    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "predictive-adaptive"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unknownPolicy"


# --------------------------------------------------------------------------
# Test 2 -- round-robin determinism, asserted on actual returned resolver ids
# --------------------------------------------------------------------------


async def test_round_robin_sequence_is_deterministic(router_client):
    observed = []
    for _ in range(6):
        response = await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "round-robin"}
        )
        assert response.status_code == 200
        observed.append(response.json()["returned_resolver"])

    assert observed == [
        "resolver-a",
        "resolver-b",
        "resolver-c",
        "resolver-a",
        "resolver-b",
        "resolver-c",
    ]


async def test_round_robin_documents_come_from_the_named_resolver(router_client):
    """Guard against a returned_resolver label that does not match reality."""
    for expected in ("resolver-a", "resolver-b", "resolver-c"):
        body = (
            await router_client.get(
                f"/1.0/identifiers/{DID}", params={"policy": "round-robin"}
            )
        ).json()
        assert body["returned_resolver"] == expected
        # The resolver stamps its own id into the response metadata.
        assert body["didResolutionMetadata"]["resolver_id"] == expected


# --------------------------------------------------------------------------
# Tests 3 and 4 -- sequential failover skips a failed resolver and records
# every attempt
# --------------------------------------------------------------------------


async def test_sequential_failover_skips_http_error(router_client, healthy_cluster):
    healthy_cluster["resolver-a"].set_behavior(force_error=True, force_error_status=503)

    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned_resolver"] == "resolver-b"
    assert body["attempted_sequence"] == ["resolver-a", "resolver-b"]
    assert body["attempt_count"] == 2


async def test_failover_records_failed_and_successful_attempts(
    router_client, healthy_cluster, sink
):
    healthy_cluster["resolver-a"].set_behavior(force_error=True, force_error_status=503)

    body = (
        await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
        )
    ).json()
    telemetry = sink.get_request(body["request_id"])
    attempts = telemetry["attempts"]

    assert len(attempts) == 2
    assert [a["attempt_index"] for a in attempts] == [0, 1]

    failed, succeeded = attempts
    assert failed["resolver_id"] == "resolver-a"
    assert failed["outcome"] == "http_error"
    assert failed["http_status"] == 503
    assert failed["accepted"] is False
    # No document was evaluated, so validity is unknown, not False.
    assert failed["document_valid"] is None

    assert succeeded["resolver_id"] == "resolver-b"
    assert succeeded["outcome"] == "accepted"
    assert succeeded["accepted"] is True
    assert succeeded["document_valid"] is True

    request_record = telemetry["request"]
    assert request_record["attempt_count"] == 2
    assert request_record["fanout_count"] == 2
    assert request_record["attempted_sequence"] == ["resolver-a", "resolver-b"]
    assert request_record["candidate_sequence"] == [
        "resolver-a",
        "resolver-b",
        "resolver-c",
    ]


async def test_failover_on_timeout(router_client, healthy_cluster, sink):
    """A hung resolver must be abandoned at the configured deadline."""
    healthy_cluster["resolver-a"].set_behavior(
        force_timeout=True, timeout_sleep_ms=TIMEOUT_SLEEP_MS
    )

    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned_resolver"] == "resolver-b"

    attempts = sink.get_request(body["request_id"])["attempts"]
    timed_out = attempts[0]
    assert timed_out["outcome"] == "timeout"
    assert timed_out["timeout"] is True
    assert timed_out["http_status"] is None
    # Abandoned at the deadline, not after the injected 5 s sleep.
    assert timed_out["latency_ms"] < TIMEOUT_SLEEP_MS / 2


async def test_failover_on_connection_error(router_client, router_config, sink):
    """A dead endpoint is a transport failure, not an HTTP error.

    An unresolvable host is used rather than a closed TCP port: on this
    Windows host a closed loopback port is not refused for roughly 2 s, so it
    would surface as a timeout instead of a connection error. DNS failure is
    fast and deterministic. The deadline is widened for this test so the
    transport error, not the timeout, is what terminates the attempt.
    """
    router_config.resolvers[0].url = "http://avdr-nonexistent-host.invalid:8001"
    router_config.attempt_timeout_ms = 3000

    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned_resolver"] == "resolver-b"

    attempts = sink.get_request(body["request_id"])["attempts"]
    assert attempts[0]["outcome"] == "connection_error"
    assert attempts[0]["http_status"] is None
    assert attempts[0]["error"] is not None


# --------------------------------------------------------------------------
# Test 5 -- injected delay is observable in attempt latency
# --------------------------------------------------------------------------


async def test_injected_delay_is_observable_in_attempt_latency(
    router_client, healthy_cluster, sink
):
    """Lower-bound assertion only. No exact latency is required."""
    healthy_cluster["resolver-b"].set_behavior(artificial_delay_ms=INJECTED_DELAY_MS)

    fast = (
        await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "single-static"}
        )
    ).json()
    fast_attempt = sink.get_request(fast["request_id"])["attempts"][0]
    assert fast_attempt["resolver_id"] == "resolver-a"

    # Route to resolver-b via round-robin: first call is a, second is b.
    await router_client.get(f"/1.0/identifiers/{DID}", params={"policy": "round-robin"})
    slow = (
        await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "round-robin"}
        )
    ).json()
    slow_attempt = sink.get_request(slow["request_id"])["attempts"][0]
    assert slow_attempt["resolver_id"] == "resolver-b"

    # Tolerance for timer granularity and scheduler noise; a sleep is a lower
    # bound, so 85% of the injected value is a safe floor.
    floor_ms = INJECTED_DELAY_MS * 0.85
    assert slow_attempt["latency_ms"] >= floor_ms
    assert slow_attempt["latency_ms"] > fast_attempt["latency_ms"]


# --------------------------------------------------------------------------
# Test 6 -- all resolvers fail: well-defined error plus complete telemetry
# --------------------------------------------------------------------------


async def test_all_resolvers_fail_produces_defined_error_and_telemetry(
    router_client, healthy_cluster, sink
):
    for resolver in healthy_cluster.values():
        resolver.set_behavior(force_error=True, force_error_status=503)

    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
    )
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "noAcceptableResponse"
    assert body["attempted_sequence"] == ["resolver-a", "resolver-b", "resolver-c"]
    assert body["attempt_count"] == 3
    assert len(body["attempt_outcomes"]) == 3

    telemetry = sink.get_request(body["request_id"])
    assert telemetry["request"]["success"] is False
    assert telemetry["request"]["returned_resolver"] is None
    assert telemetry["request"]["final_error"] is not None
    assert len(telemetry["attempts"]) == 3
    assert all(a["accepted"] is False for a in telemetry["attempts"])
    assert [a["attempt_index"] for a in telemetry["attempts"]] == [0, 1, 2]


# --------------------------------------------------------------------------
# Test 7 -- an invalid first response must not be returned as accepted
# --------------------------------------------------------------------------


async def test_invalid_first_response_is_not_accepted(
    router_client, healthy_cluster, sink
):
    healthy_cluster["resolver-a"].set_behavior(force_invalid=True)

    response = await router_client.get(
        f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["returned_resolver"] == "resolver-b"
    assert body["attempted_sequence"] == ["resolver-a", "resolver-b"]
    assert body["didDocument"]["id"] == DID

    attempts = sink.get_request(body["request_id"])["attempts"]
    rejected = attempts[0]
    # HTTP-successful but not acceptable: the distinction this project rests on.
    assert rejected["http_status"] == 200
    assert rejected["outcome"] == "rejected_invalid"
    assert rejected["document_valid"] is False
    assert rejected["accepted"] is False
    assert rejected["acceptance_reason"] is not None

    assert attempts[1]["accepted"] is True


# --------------------------------------------------------------------------
# Telemetry integrity
# --------------------------------------------------------------------------


async def test_telemetry_written_to_separate_jsonl_files(
    router_client, healthy_cluster, router_config
):
    healthy_cluster["resolver-a"].set_behavior(force_error=True)
    body = (
        await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
        )
    ).json()

    directory = Path(router_config.telemetry_dir)
    requests = _read_jsonl(directory / "requests.jsonl")
    attempts = _read_jsonl(directory / "attempts.jsonl")

    logical = [r for r in requests if r["request_id"] == body["request_id"]]
    linked = [a for a in attempts if a["request_id"] == body["request_id"]]

    # One logical request, two attempts: the counts must not be conflated.
    assert len(logical) == 1
    assert len(linked) == 2
    assert logical[0]["record_type"] == "logical_request"
    assert all(a["record_type"] == "attempt" for a in linked)
    assert logical[0]["attempt_count"] == len(linked)


async def test_logical_latency_covers_all_attempts(
    router_client, healthy_cluster, sink
):
    """Sanity check: the logical latency must not be shorter than its parts."""
    healthy_cluster["resolver-a"].set_behavior(
        artificial_delay_ms=INJECTED_DELAY_MS, force_error=True
    )
    body = (
        await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "sequential-failover"}
        )
    ).json()

    telemetry = sink.get_request(body["request_id"])
    attempt_total = sum(a["latency_ms"] for a in telemetry["attempts"])
    logical = telemetry["request"]["logical_completion_latency_ms"]

    assert logical >= attempt_total * 0.95
    assert all(a["latency_ms"] >= 0 for a in telemetry["attempts"])


@pytest.mark.parametrize("policy", ["single-static", "round-robin", "sequential-failover"])
async def test_every_policy_records_a_logical_request(router_client, sink, policy):
    body = (
        await router_client.get(f"/1.0/identifiers/{DID}", params={"policy": policy})
    ).json()
    record = sink.get_request(body["request_id"])["request"]
    assert record["routing_policy"] == policy
    assert record["did"] == DID
    assert record["did_method"] == "example"
    assert record["policy_version"] == "test-baseline-v1"
    assert record["attempt_timeout_ms"] == ATTEMPT_TIMEOUT_MS


async def test_deterministic_failure_mode(router_client, healthy_cluster, sink):
    """Every 2nd request to resolver-a fails, deterministically."""
    healthy_cluster["resolver-a"].set_behavior(deterministic_failure_every_n=2)

    outcomes = []
    for _ in range(4):
        response = await router_client.get(
            f"/1.0/identifiers/{DID}", params={"policy": "single-static"}
        )
        outcomes.append(response.status_code)

    # Requests 2 and 4 hit the injected failure; 1 and 3 succeed.
    assert outcomes == [200, 502, 200, 502]
