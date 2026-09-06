"""Minimal REAL public smoke test of the routing service.

Sends the smallest number of real requests that can demonstrate:

    real DID -> AVDR router API -> real adapter -> w3c-basic-v1 acceptance
             -> response

The public endpoint enforces 10 requests / 1800 s. This script therefore makes
at most ONE public request by default, and if the provider signals throttling
(HTTP 403/429) it reports

    REAL_E2E_DEFERRED_RATE_BUDGET

and exits 0. A deferral is NOT a pass and is never reported as one; it is also
not a local engineering failure.

No performance comparison is attempted and none may be derived.

Usage:
    python scripts/real_routing_smoke.py
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

from avdr.budget import BudgetRegistry  # noqa: E402
from avdr.inventory import load_provider_inventory  # noqa: E402
from avdr.profiles import PROFILE_W3C_BASIC_V1  # noqa: E402
from avdr.real_router.app import create_app  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

DEFERRED = "REAL_E2E_DEFERRED_RATE_BUDGET"
# Deterministic did:key control fixture; no ledger or hosting dependency.
DID = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"


async def call(app, did: str, policy: str) -> tuple[int, dict]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            response = await c.post("/resolve", json={"did": did, "policy": policy})
    return response.status_code, response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default="sequential-failover")
    parser.add_argument("--did", default=DID)
    parser.add_argument("--telemetry-dir", default=str(REPO_ROOT / "telemetry" / "routing"))
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "real_routing_smoke.json"))
    args = parser.parse_args()

    inventory = load_provider_inventory()
    sink = TelemetrySink(args.telemetry_dir)
    budgets = BudgetRegistry(inventory)
    app = create_app(
        inventory=inventory,
        sink=sink,
        budgets=budgets,
        timeout_ms=30000,
        launch_order_seed=None,
    )

    print("REAL PUBLIC SMOKE -- minimum request count")
    print(f"  did    = {args.did}")
    print(f"  policy = {args.policy}")
    print(f"  providers available: {[p.id for p in inventory.available_providers()]}")
    print("  NOTE: no performance claim may be derived from this run.\n")

    status, body = asyncio.run(call(app, args.did, args.policy))

    throttled = [
        w for w in body.get("warnings", []) if w.get("code") == "PROVIDER_THROTTLED"
    ]
    outcome_statuses = {
        o.get("http_status") for o in body.get("attempt_outcomes", [])
    }
    hit_limit = bool(throttled) or bool(outcome_statuses & {403, 429})

    result = {"status_code": status, "response": body}

    if hit_limit and not body.get("accepted"):
        print(f"[{DEFERRED}]")
        print("  the public provider signalled throttling (HTTP 403/429).")
        print("  This is a DEFERRAL, not a PASS, and not a local failure.")
        verdict = DEFERRED
    elif status == 200 and body.get("accepted"):
        print("[PASS] real DID resolved through the routing service")
        print(f"  returned_provider   = {body['returned_provider']}")
        print(f"  attempted_providers = {body['attempted_providers']}")
        print(f"  acceptance_profile  = {body['acceptance_profile']}")
        print(f"  did_document.id     = {body['did_document'].get('id')}")
        print(f"  latency_ms          = {body['logical_completion_latency_ms']}"
              "   [MEASURED] single observation, no comparison implied")
        print(f"  acceptance_checks   = {json.dumps(body['acceptance_checks'])}")
        for warning in body.get("warnings", []):
            print(f"  warning: {warning['code']}")
        verdict = "PASS"
    else:
        print(f"[FAIL] HTTP {status}")
        print(json.dumps(body, indent=2)[:900])
        verdict = "FAIL"

    result["verdict"] = verdict
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "verdict": verdict,
                "acceptance_profile": PROFILE_W3C_BASIC_V1,
                "disclaimer": (
                    "Single real request for functional smoke only. No "
                    "performance, ranking or heterogeneity claim may be "
                    "derived."
                ),
                **result,
            },
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nreport written to {output}")
    # A deferral is an acceptable outcome for this milestone; a FAIL is not.
    return 0 if verdict in ("PASS", DEFERRED) else 1


if __name__ == "__main__":
    sys.exit(main())
