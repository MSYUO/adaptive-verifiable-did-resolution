"""Explicit one-transaction Sepolia smoke for an AVDR service receipt.

No network access or wallet construction occurs unless ``--execute-live`` is
present and the required environment variables are configured.  The script
never prints the private key, signed raw transaction, or RPC URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.audit import LocalAuditRecorder  # noqa: E402
from avdr.inventory import ProviderEntry, ProviderInventory  # noqa: E402
from avdr.real_router.app import create_app  # noqa: E402
from avdr.sepolia_anchor import AnchorError, build_audit_anchor_from_env  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

CONTROLLED_DID = "did:example:sepolia-anchor-smoke"
PROVIDER_ID = "local-sepolia-anchor-smoke"


def blocked_report(status: str) -> dict[str, Any]:
    return {
        "scope": "one controlled service receipt anchored to Ethereum Sepolia",
        "live_anchor_attempted": False,
        "live_anchor_status": "BLOCKED",
        "block_reason": status,
        "chain": "ethereum-sepolia",
        "chain_id": 11155111,
        "transaction_value_wei": 0,
    }


def _configured_anchor():
    if not os.environ.get("AVDR_SEPOLIA_PRIVATE_KEY", "").strip():
        raise AnchorError("NO_TESTNET_WALLET")
    if not os.environ.get("AVDR_SEPOLIA_RPC_URL", "").strip():
        raise AnchorError("RPC_NOT_CONFIGURED")
    values = dict(os.environ)
    values["AVDR_AUDIT_ANCHOR"] = "sepolia"
    return build_audit_anchor_from_env(values)


def _inventory() -> ProviderInventory:
    return ProviderInventory(
        inventory_version="local-sepolia-anchor-smoke-v1",
        providers=[
            ProviderEntry(
                id=PROVIDER_ID,
                implementation_id="in-process-anchor-smoke-resolver",
                operator="AVDR controlled smoke",
                endpoint="http://127.0.0.1:9",
                adapter="universal-resolver-v1",
                supported_did_methods=["example"],
                available=True,
            )
        ],
    )


def _provider_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/did-resolution+json"},
        json={
            "didResolutionMetadata": {"contentType": "application/did+ld+json"},
            "didDocument": {
                "@context": ["https://www.w3.org/ns/did/v1"],
                "id": CONTROLLED_DID,
            },
            "didDocumentMetadata": {},
        },
        request=request,
    )


async def execute() -> dict[str, Any]:
    try:
        anchor = _configured_anchor()
    except AnchorError as exc:
        return blocked_report(exc.code)

    recorder = LocalAuditRecorder(anchor=anchor)
    inventory = _inventory()
    with tempfile.TemporaryDirectory(prefix="avdr-p11-anchor-") as telemetry:
        app = create_app(
            inventory=inventory,
            sink=TelemetrySink(telemetry),
            single_static_target=PROVIDER_ID,
            launch_order_seed=None,
            evidence_mode="controlled_demo",
            audit_recorder=recorder,
        )
        provider = httpx.AsyncClient(transport=httpx.MockTransport(_provider_response))
        service = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://avdr-anchor-smoke",
        )
        app.state.client = provider
        async with provider, service:
            response = await service.post(
                "/resolve", json={"did": CONTROLLED_DID, "policy": "single-static"}
            )
            body = response.json()
            receipt_id = body.get("audit", {}).get("receipt_id")
            local_verification = await service.post(
                "/audit/verify",
                json={
                    "receipt_id": receipt_id,
                    "disclosed_did": CONTROLLED_DID,
                    "disclosed_result": body.get("result"),
                },
            )

    audit = body.get("audit", {})
    anchor_result = audit.get("anchor", {})
    local = local_verification.json()
    passed = bool(
        response.status_code == 200
        and body.get("success") is True
        and body.get("evidence", {}).get("mode") == "controlled_demo"
        and local_verification.status_code == 200
        and local.get("valid") is True
        and local.get("did_commitment_valid") is True
        and local.get("result_commitment_valid") is True
        and anchor_result.get("status") == "mined"
        and anchor_result.get("network") == "ethereum-sepolia"
        and anchor_result.get("chain_id") == 11155111
        and anchor_result.get("value_wei") == 0
        and anchor_result.get("onchain_receipt_match") is True
        and anchor_result.get("verification") == "onchain_readback"
    )
    return {
        "scope": "one controlled service receipt anchored to Ethereum Sepolia",
        "live_anchor_attempted": bool(anchor_result.get("transaction_hash")),
        "live_anchor_status": "PASS" if passed else "FAIL",
        "block_reason": None,
        "evidence_mode": body.get("evidence", {}).get("mode"),
        "local_receipt_verification": local.get("valid"),
        "chain": anchor_result.get("network"),
        "chain_id": anchor_result.get("chain_id"),
        "wallet_address": anchor_result.get("sender"),
        "wallet_balance_before_wei": anchor_result.get("wallet_balance_before_wei"),
        "estimated_gas": anchor_result.get("estimated_gas"),
        "transaction_value_wei": anchor_result.get("value_wei"),
        "transaction_hash": anchor_result.get("transaction_hash"),
        "block_number": anchor_result.get("block_number"),
        "block_hash": anchor_result.get("block_hash"),
        "local_receipt_hash": audit.get("receipt_hash"),
        "onchain_receipt_hash": anchor_result.get("onchain_receipt_hash"),
        "onchain_receipt_match": anchor_result.get("onchain_receipt_match"),
        "transaction_receipt_status": anchor_result.get(
            "transaction_receipt_status"
        ),
        "finality_status": anchor_result.get("finality_status"),
        "explorer_url": anchor_result.get("explorer_url"),
        "error_code": anchor_result.get("error_code"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="permit at most one zero-value Sepolia anchor transaction",
    )
    args = parser.parse_args()
    if not args.execute_live:
        report = blocked_report("EXPLICIT_OPT_IN_REQUIRED")
        print(json.dumps(report, indent=2))
        return 2
    report = asyncio.run(execute())
    print(json.dumps(report, indent=2))
    if report["live_anchor_status"] == "PASS":
        return 0
    return 2 if report["live_anchor_status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
