"""End-to-end qualification against the docker compose deployment.

Drives the running stack over real HTTP, applies controlled fault-injection
scenarios through each resolver's admin endpoint, and verifies the resulting
router behaviour and telemetry.

CONTROLLED INJECTION: every delay/error/invalidity applied here is a test knob.
None of it measures real DID resolver behaviour. All services share one Docker
host, so the resolver instances are not independent gateways.

Usage:
    python scripts/e2e_smoke.py [--router http://127.0.0.1:8000]
Exit code 0 = all gates pass, 1 = at least one gate failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DID = "did:example:e2e-subject"

RESOLVER_ADMIN = {
    "resolver-a": "http://127.0.0.1:8001",
    "resolver-b": "http://127.0.0.1:8002",
    "resolver-c": "http://127.0.0.1:8003",
}

HEALTHY = {
    "artificial_delay_ms": 0,
    "force_error": False,
    "force_error_status": 503,
    "force_invalid": False,
    "force_timeout": False,
    "timeout_sleep_ms": 30000,
    "deterministic_failure_every_n": 0,
}

INJECTED_DELAY_MS = 200


class Gates:
    """Records pass/fail per named gate; never rationalises a failure."""

    def __init__(self) -> None:
        self.results: dict[str, bool] = {}
        self.notes: dict[str, str] = {}

    def record(self, gate: str, passed: bool, note: str = "") -> bool:
        # A gate that is checked twice must never be downgraded silently.
        self.results[gate] = self.results.get(gate, True) and passed
        if note:
            self.notes[gate] = note
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {gate}: {note}")
        return passed

    def all_passed(self) -> bool:
        return bool(self.results) and all(self.results.values())


def wait_for_health(client: httpx.Client, url: str, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    last_error = None
    while time.time() < deadline:
        try:
            response = client.get(f"{url}/health", timeout=3.0)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as exc:  # service not up yet
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"{url} did not become healthy: {last_error}")


def set_behavior(client: httpx.Client, resolver_id: str, **overrides) -> None:
    behavior = dict(HEALTHY)
    behavior.update(overrides)
    response = client.post(
        f"{RESOLVER_ADMIN[resolver_id]}/admin/behavior", json=behavior, timeout=5.0
    )
    response.raise_for_status()


def reset_all(client: httpx.Client) -> None:
    for resolver_id in RESOLVER_ADMIN:
        client.post(f"{RESOLVER_ADMIN[resolver_id]}/admin/reset", timeout=5.0)


def reset_policies(client: httpx.Client, router: str) -> None:
    client.post(f"{router}/admin/reset-policies", timeout=5.0).raise_for_status()


def resolve(client: httpx.Client, router: str, policy: str) -> httpx.Response:
    return client.get(
        f"{router}/1.0/identifiers/{DID}", params={"policy": policy}, timeout=15.0
    )


def telemetry(client: httpx.Client, router: str, request_id: str) -> dict:
    response = client.get(f"{router}/telemetry/requests/{request_id}", timeout=5.0)
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--router", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "e2e_report.json"))
    args = parser.parse_args()
    router = args.router.rstrip("/")

    gates = Gates()
    evidence: dict[str, object] = {}

    with httpx.Client() as client:
        print("== service health ==")
        router_health = wait_for_health(client, router)
        resolver_health = {
            rid: wait_for_health(client, url) for rid, url in RESOLVER_ADMIN.items()
        }
        evidence["router_health"] = router_health
        evidence["resolver_health"] = resolver_health
        gates.record(
            "PASS_LOCAL_MULTI_RESOLVER_E2E",
            len(resolver_health) == 3
            and all(h["status"] == "ok" for h in resolver_health.values())
            and router_health["status"] == "ok",
            f"router ok, resolvers up: {sorted(resolver_health)}",
        )

        # ---------------- Scenario N: normal ----------------
        print("\n== Scenario N (normal): single-static ==")
        reset_all(client)
        reset_policies(client, router)
        response = resolve(client, router, "single-static")
        body = response.json()
        evidence["scenario_N_single_static"] = body
        gates.record(
            "PASS_LOCAL_MULTI_RESOLVER_E2E",
            response.status_code == 200
            and body["returned_resolver"] == "resolver-a"
            and body["didDocument"]["id"] == DID,
            f"single-static -> {body.get('returned_resolver')} "
            f"({response.status_code})",
        )

        # ---------------- Round robin ----------------
        print("\n== Scenario N (normal): round-robin ==")
        reset_policies(client, router)
        observed = []
        rr_records = []
        for _ in range(6):
            resp = resolve(client, router, "round-robin")
            payload = resp.json()
            observed.append(payload.get("returned_resolver"))
            rr_records.append(
                {
                    "request_id": payload.get("request_id"),
                    "returned_resolver": payload.get("returned_resolver"),
                    "stamped_by": payload.get("didResolutionMetadata", {}).get(
                        "resolver_id"
                    ),
                    "attempted_sequence": payload.get("attempted_sequence"),
                }
            )
        expected = ["resolver-a", "resolver-b", "resolver-c"] * 2
        evidence["round_robin"] = rr_records
        stamped_ok = all(r["stamped_by"] == r["returned_resolver"] for r in rr_records)
        gates.record(
            "PASS_ROUND_ROBIN",
            observed == expected and stamped_ok,
            f"observed={observed} (resolver self-stamp consistent={stamped_ok})",
        )

        # ---------------- Scenario D: delay ----------------
        print("\n== Scenario D (delay): +200 ms injected on resolver-b ==")
        reset_all(client)
        reset_policies(client, router)
        set_behavior(client, "resolver-b", artificial_delay_ms=INJECTED_DELAY_MS)

        baseline = resolve(client, router, "single-static").json()
        baseline_attempt = telemetry(client, router, baseline["request_id"])["attempts"][0]

        resolve(client, router, "round-robin")  # rotation position 0 -> a
        delayed = resolve(client, router, "round-robin").json()  # position 1 -> b
        delayed_attempt = telemetry(client, router, delayed["request_id"])["attempts"][0]

        evidence["scenario_D"] = {
            "injected_delay_ms": INJECTED_DELAY_MS,
            "resolver_a_attempt": baseline_attempt,
            "resolver_b_attempt": delayed_attempt,
        }
        gates.record(
            "PASS_CONTROLLED_DELAY_INJECTION",
            delayed_attempt["resolver_id"] == "resolver-b"
            and delayed_attempt["latency_ms"] >= INJECTED_DELAY_MS * 0.85
            and delayed_attempt["latency_ms"] > baseline_attempt["latency_ms"],
            f"resolver-a {baseline_attempt['latency_ms']} ms vs "
            f"resolver-b {delayed_attempt['latency_ms']} ms "
            f"(injected +{INJECTED_DELAY_MS} ms)",
        )

        # ---------------- Scenario F: failure ----------------
        print("\n== Scenario F (failure): resolver-a forced HTTP 503 ==")
        reset_all(client)
        reset_policies(client, router)
        set_behavior(client, "resolver-a", force_error=True, force_error_status=503)

        response = resolve(client, router, "sequential-failover")
        body = response.json()
        trace = telemetry(client, router, body["request_id"])
        evidence["scenario_F"] = {"response": body, "telemetry": trace}

        failover_ok = (
            response.status_code == 200
            and body["returned_resolver"] == "resolver-b"
            and body["attempted_sequence"] == ["resolver-a", "resolver-b"]
        )
        gates.record(
            "PASS_SEQUENTIAL_FAILOVER",
            failover_ok,
            f"attempted={body.get('attempted_sequence')} "
            f"returned={body.get('returned_resolver')}",
        )
        gates.record(
            "PASS_CONTROLLED_FAILURE_INJECTION",
            trace["attempts"][0]["outcome"] == "http_error"
            and trace["attempts"][0]["http_status"] == 503,
            f"resolver-a outcome={trace['attempts'][0]['outcome']} "
            f"status={trace['attempts'][0]['http_status']}",
        )

        attempts = trace["attempts"]
        telemetry_ok = (
            len(attempts) == 2
            and [a["attempt_index"] for a in attempts] == [0, 1]
            and attempts[0]["accepted"] is False
            and attempts[1]["accepted"] is True
            and trace["request"]["attempt_count"] == 2
            and trace["request"]["success"] is True
            and all(a["latency_ms"] >= 0 for a in attempts)
        )
        gates.record(
            "PASS_ATTEMPT_LEVEL_TELEMETRY",
            telemetry_ok,
            f"1 logical request, {len(attempts)} attempts recorded "
            f"({[a['resolver_id'] + ':' + a['outcome'] for a in attempts]})",
        )

        # ---------------- Scenario I: invalid ----------------
        print("\n== Scenario I (invalid): resolver-a returns 200 + bad document ==")
        reset_all(client)
        reset_policies(client, router)
        set_behavior(client, "resolver-a", force_invalid=True)

        response = resolve(client, router, "sequential-failover")
        body = response.json()
        trace = telemetry(client, router, body["request_id"])
        evidence["scenario_I"] = {"response": body, "telemetry": trace}
        rejected = trace["attempts"][0]
        gates.record(
            "PASS_SEQUENTIAL_FAILOVER",
            response.status_code == 200
            and body["returned_resolver"] == "resolver-b"
            and rejected["http_status"] == 200
            and rejected["document_valid"] is False
            and rejected["accepted"] is False,
            f"resolver-a HTTP {rejected['http_status']} but "
            f"document_valid={rejected['document_valid']} -> returned "
            f"{body.get('returned_resolver')}",
        )

        # ---------------- Scenario T: timeout ----------------
        print("\n== Scenario T (timeout): resolver-a holds past the deadline ==")
        reset_all(client)
        reset_policies(client, router)
        set_behavior(client, "resolver-a", force_timeout=True, timeout_sleep_ms=30000)

        response = resolve(client, router, "sequential-failover")
        body = response.json()
        trace = telemetry(client, router, body["request_id"])
        evidence["scenario_T"] = {"response": body, "telemetry": trace}
        timed_out = trace["attempts"][0]
        gates.record(
            "PASS_CONTROLLED_FAILURE_INJECTION",
            response.status_code == 200
            and body["returned_resolver"] == "resolver-b"
            and timed_out["outcome"] == "timeout"
            and timed_out["timeout"] is True,
            f"resolver-a timed out at {timed_out['latency_ms']} ms "
            f"(configured {trace['request']['attempt_timeout_ms']} ms) -> "
            f"returned {body.get('returned_resolver')}",
        )

        # ---------------- Scenario X: all fail ----------------
        print("\n== Scenario X (all fail) ==")
        reset_all(client)
        reset_policies(client, router)
        for resolver_id in RESOLVER_ADMIN:
            set_behavior(client, resolver_id, force_error=True)

        response = resolve(client, router, "sequential-failover")
        body = response.json()
        trace = telemetry(client, router, body["request_id"])
        evidence["scenario_X"] = {"response": body, "telemetry": trace}
        gates.record(
            "PASS_ATTEMPT_LEVEL_TELEMETRY",
            response.status_code == 502
            and body["error"] == "noAcceptableResponse"
            and body["attempt_count"] == 3
            and len(trace["attempts"]) == 3
            and trace["request"]["success"] is False
            and trace["request"]["returned_resolver"] is None,
            f"all three attempted, HTTP {response.status_code}, "
            f"{len(trace['attempts'])} attempts recorded",
        )

        reset_all(client)
        reset_policies(client, router)

    print("\n== gate summary ==")
    for gate in sorted(gates.results):
        print(f"  {gate} = {'PASS' if gates.results[gate] else 'FAIL'}")

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "did": DID,
                "injected_delay_ms": INJECTED_DELAY_MS,
                "disclaimer": (
                    "CONTROLLED INJECTION. Local single-host deployment. "
                    "Resolver instances are not independent gateways and these "
                    "values do not describe real DID infrastructure."
                ),
                "gates": gates.results,
                "notes": gates.notes,
                "evidence": evidence,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nreport written to {output}")

    return 0 if gates.all_passed() else 1


if __name__ == "__main__":
    sys.exit(main())
