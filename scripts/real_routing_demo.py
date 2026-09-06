"""Controlled E2E demonstration of the real routing service.

Uses ONLY local deterministic services (the docker-compose mock resolvers), so
it makes zero public-network calls and spends no third-party request budget.

Every delay and failure below is [CONTROLLED INJECTION] on a single shared
host. None of it measures real DID resolver behaviour, and no provider
comparison or ranking may be derived from it.

Scenarios:
  A  all valid, A fastest              -> all-race returns A
  B  A invalid+fastest, B/C valid      -> all-race must NOT return A
  C  A fails, B valid                  -> sequential-failover returns B

Usage:
    docker compose up -d
    python scripts/real_routing_demo.py
Exit 0 = all scenarios behaved as required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.inventory import load_provider_inventory  # noqa: E402
from avdr.real_router.app import create_app  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

DID = "did:example:demo-subject"
ADMIN = {
    "local-a": "http://127.0.0.1:8001",
    "local-b": "http://127.0.0.1:8002",
    "local-c": "http://127.0.0.1:8003",
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
# [CONTROLLED INJECTION] test knobs, not measurements.
SLOW_MS = 300


def apply(client: httpx.Client, provider_id: str, **overrides) -> None:
    behavior = dict(HEALTHY)
    behavior.update(overrides)
    client.post(f"{ADMIN[provider_id]}/admin/behavior", json=behavior, timeout=5).raise_for_status()


def reset_all(client: httpx.Client) -> None:
    for url in ADMIN.values():
        client.post(f"{url}/admin/reset", timeout=5).raise_for_status()


async def call(app, policy: str) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            response = await c.post("/resolve", json={"did": DID, "policy": policy})
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telemetry-dir", default=str(REPO_ROOT / "telemetry" / "demo"))
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "routing_demo.json"))
    args = parser.parse_args()

    inventory = load_provider_inventory(REPO_ROOT / "config" / "providers.local.yaml")
    sink = TelemetrySink(args.telemetry_dir)
    app = create_app(
        inventory=inventory,
        sink=sink,
        timeout_ms=5000,
        single_static_target="local-a",
        launch_order_seed=None,
    )

    print("CONTROLLED LOCAL DEMONSTRATION -- all timings are injected test")
    print("knobs on one shared host, not real DID measurements.\n")
    print(f"providers: {[p.id for p in inventory.available_providers()]}\n")

    results = {}
    ok = True

    with httpx.Client() as admin:
        for url in ADMIN.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

        # -------- Scenario A --------
        print("== Scenario A: all valid, A fastest -> all-race returns A ==")
        reset_all(admin)
        apply(admin, "local-b", artificial_delay_ms=SLOW_MS)
        apply(admin, "local-c", artificial_delay_ms=SLOW_MS)
        body = asyncio.run(call(app, "all-race"))
        results["A"] = body
        passed = body.get("returned_provider") == "local-a" and body.get("accepted")
        ok &= passed
        print(f"  returned={body.get('returned_provider')} accepted={body.get('accepted')} "
              f"attempts={body.get('attempt_count')} canceled={body.get('canceled_count')}")
        print(f"  [{'PASS' if passed else 'FAIL'}] expected local-a\n")

        # -------- Scenario B --------
        print("== Scenario B: A invalid+fastest -> must NOT win ==")
        reset_all(admin)
        apply(admin, "local-a", force_invalid=True)
        apply(admin, "local-b", artificial_delay_ms=SLOW_MS)
        apply(admin, "local-c", artificial_delay_ms=SLOW_MS * 2)
        body = asyncio.run(call(app, "all-race"))
        results["B"] = body
        trace = sink.get_routing_request(body["request_id"])
        by_provider = {a["provider_id"]: a for a in trace["attempts"]}
        a_attempt = by_provider.get("local-a", {})
        passed = (
            body.get("returned_provider") == "local-b"
            and a_attempt.get("http_status") == 200
            and a_attempt.get("accepted") is False
        )
        ok &= passed
        print(f"  local-a: HTTP {a_attempt.get('http_status')} "
              f"latency={a_attempt.get('latency_ms')} ms accepted={a_attempt.get('accepted')}")
        print(f"  local-b: latency={by_provider.get('local-b',{}).get('latency_ms')} ms "
              f"accepted={by_provider.get('local-b',{}).get('accepted')}")
        print(f"  returned={body.get('returned_provider')}")
        print(f"  [{'PASS' if passed else 'FAIL'}] fastest-but-unacceptable did not win\n")

        # -------- Scenario C --------
        print("== Scenario C: A fails -> sequential-failover returns B ==")
        reset_all(admin)
        apply(admin, "local-a", force_error=True)
        body = asyncio.run(call(app, "sequential-failover"))
        results["C"] = body
        passed = (
            body.get("returned_provider") == "local-b"
            and body.get("attempted_providers") == ["local-a", "local-b"]
        )
        ok &= passed
        print(f"  attempted={body.get('attempted_providers')} "
              f"returned={body.get('returned_provider')}")
        print(f"  [{'PASS' if passed else 'FAIL'}] failover to local-b\n")

        reset_all(admin)

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "label": "CONTROLLED LOCAL DEMONSTRATION",
                "disclaimer": (
                    "All delays and failures are injected test knobs on one "
                    "shared host. Not real DID measurements. No provider "
                    "comparison or ranking may be derived."
                ),
                "injected_delay_ms": SLOW_MS,
                "scenarios": results,
                "all_scenarios_passed": ok,
            },
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"report written to {output}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
