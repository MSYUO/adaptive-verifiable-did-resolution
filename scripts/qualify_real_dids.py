"""Minimal opt-in qualification of real DID interoperability through AVDR.

This is a compatibility check, not a benchmark. It sends exactly one request
for each configured case, never retries, verifies the local audit receipt, and
emits structured JSON. Public traffic is disabled unless ``--execute-live`` is
present.

Usage:
    python scripts/qualify_real_dids.py --execute-live --out <report.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.audit import LocalAuditRecorder  # noqa: E402
from avdr.budget import BudgetRegistry  # noqa: E402
from avdr.inventory import (  # noqa: E402
    FixtureEntry,
    ProviderEntry,
    load_fixture_manifest,
    load_provider_inventory,
)
from avdr.profiles import PROFILE_W3C_BASIC_V1  # noqa: E402
from avdr.real_router.app import create_app  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

REPORT_SCHEMA = "p9-real-did-compatibility-v1"
PROVIDER_ID = "uniresolver-dif-dev"
FIXTURE_IDS = (
    "key-ed25519-1",
    "web-danubetech",
    "ethr-default-doc",
)
POLICY = "single-static"
MAX_PUBLIC_REQUESTS = len(FIXTURE_IDS)


@dataclass(frozen=True)
class QualificationCase:
    fixture_id: str
    method: str
    did: str
    source: str
    provider_id: str
    provider_endpoint: str
    provider_adapter: str


def qualification_cases() -> list[QualificationCase]:
    """Resolve the fixed public cases from the checked-in manifests."""
    inventory = load_provider_inventory()
    fixtures = load_fixture_manifest()
    provider = inventory.get(PROVIDER_ID)
    if not provider.available:
        raise ValueError(f"provider {PROVIDER_ID!r} is not marked available")
    if not provider.is_external:
        raise ValueError(f"provider {PROVIDER_ID!r} is not external")

    cases: list[QualificationCase] = []
    for fixture_id in FIXTURE_IDS:
        fixture = fixtures.get(fixture_id)
        if fixture.project_controlled:
            raise ValueError(f"fixture {fixture_id!r} is controlled, not real")
        if fixture.did_method not in provider.supported_did_methods:
            raise ValueError(
                f"provider {PROVIDER_ID!r} does not declare {fixture.did_method!r}"
            )
        cases.append(_case(provider, fixture))
    if len(cases) != MAX_PUBLIC_REQUESTS:
        raise ValueError("qualification request budget changed unexpectedly")
    return cases


def _case(provider: ProviderEntry, fixture: FixtureEntry) -> QualificationCase:
    return QualificationCase(
        fixture_id=fixture.fixture_id,
        method=fixture.did_method,
        did=fixture.did,
        source=fixture.source,
        provider_id=provider.id,
        provider_endpoint=provider.endpoint,
        provider_adapter=provider.adapter,
    )


def dns_observation(endpoint: str) -> dict[str, Any]:
    """Record DNS only; the qualification request establishes HTTP behavior."""
    hostname = urlparse(endpoint).hostname
    if not hostname:
        return {"hostname": None, "resolved": False, "addresses": [], "error": "invalid endpoint"}
    try:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        addresses = sorted({row[4][0] for row in answers})
        return {
            "hostname": hostname,
            "resolved": bool(addresses),
            "addresses": addresses,
            "error": None,
        }
    except OSError as exc:
        return {
            "hostname": hostname,
            "resolved": False,
            "addresses": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _failure_classification(body: dict[str, Any]) -> str | None:
    attempts = [row for row in body.get("attempts", []) if row.get("dispatched")]
    statuses = {row.get("http_status") for row in attempts}
    resolution_metadata = body.get("result", {}).get("didResolutionMetadata")
    if statuses & {403, 429}:
        return "rate_limited"
    if any(row.get("transport_outcome") not in (None, "http_response") for row in attempts):
        return "provider_unreachable"
    if any(isinstance(status, int) and status >= 400 for status in statuses):
        return "http_failure"
    if body.get("error") or (
        isinstance(resolution_metadata, dict) and resolution_metadata.get("error")
    ):
        return "resolution_error"
    if body.get("success") is not True:
        return "acceptance_failure"
    return None


async def qualify_case(
    service: httpx.AsyncClient,
    case: QualificationCase,
    previous_receipt_hash: str | None,
) -> dict[str, Any]:
    response = await service.post(
        "/resolve", json={"did": case.did, "policy": POLICY}
    )
    try:
        body = response.json()
    except ValueError:
        body = {"unparsed_service_body": response.text}

    audit = body.get("audit", {})
    receipt_id = audit.get("receipt_id")
    lookup_response = None
    verification_response = None
    lookup: dict[str, Any] = {}
    verification: dict[str, Any] = {}
    if receipt_id:
        lookup_response = await service.get(f"/audit/receipts/{receipt_id}")
        if lookup_response.headers.get("content-type", "").startswith("application/json"):
            lookup = lookup_response.json()
        verification_response = await service.post(
            "/audit/verify",
            json={
                "receipt_id": receipt_id,
                "disclosed_did": case.did,
                "disclosed_result": body.get("result"),
            },
        )
        if verification_response.headers.get("content-type", "").startswith("application/json"):
            verification = verification_response.json()

    result = body.get("result", {})
    selection = body.get("selection", {})
    cost = body.get("cost", {})
    receipt = lookup.get("receipt", {})
    attempts = [row for row in body.get("attempts", []) if row.get("dispatched")]
    provider_attempt = next(
        (row for row in attempts if row.get("provider") == case.provider_id), {}
    )
    document = result.get("didDocument")
    selected = selection.get("selected_providers") or []
    receipt_hash_value = audit.get("receipt_hash")

    checks = {
        "service_success": response.status_code == 200 and body.get("success") is True,
        "provider_dispatched": bool(provider_attempt),
        "provider_http_response": provider_attempt.get("transport_outcome") == "http_response",
        "w3c_basic_accepted": (
            body.get("acceptance_profile") == PROFILE_W3C_BASIC_V1
            and result.get("accepted") is True
        ),
        "normalized_document_present": isinstance(document, dict),
        "normalized_subject_matches": (
            isinstance(document, dict) and document.get("id") == case.did
        ),
        "evidence_mode_real": body.get("evidence", {}).get("mode") == "real",
        "returned_provider_matches": result.get("returned_by") == case.provider_id,
        "selected_provider_matches": selected == [case.provider_id],
        "receipt_recorded": audit.get("recorded") is True,
        "receipt_lookup_ok": bool(
            lookup_response is not None
            and lookup_response.status_code == 200
            and lookup.get("integrity_verified") is True
        ),
        "receipt_evidence_mode_real": receipt.get("evidence_mode") == "real",
        "receipt_selected_resolvers_match": (
            receipt.get("selected_resolver_ids") == sorted(selected)
        ),
        "receipt_returned_provider_matches": (
            receipt.get("returned_provider") == result.get("returned_by")
        ),
        "receipt_policy_matches": (
            receipt.get("policy_identity", {}).get("name") == body.get("strategy")
        ),
        "receipt_verification_ok": bool(
            verification_response is not None
            and verification_response.status_code == 200
            and verification.get("verification") == "local_record"
            and verification.get("valid") is True
            and verification.get("did_commitment_valid") is True
            and verification.get("result_commitment_valid") is True
        ),
        "null_anchor_not_configured": lookup.get("anchor", {}).get("status") == "not_configured",
        "hash_chain_link_matches": receipt.get("previous_receipt_hash") == previous_receipt_hash,
        "latency_nonnegative": (
            isinstance(body.get("completion_latency_ms"), (int, float))
            and body["completion_latency_ms"] >= 0
        ),
        "calls_used_sane": isinstance(cost.get("calls_used"), int) and cost["calls_used"] >= 1,
        "selection_count_sane": (
            isinstance(selection.get("selected_count"), int)
            and isinstance(selection.get("candidate_count"), int)
            and selection["selected_count"] <= selection["candidate_count"]
        ),
    }
    status = "PASS" if all(checks.values()) else "FAIL"
    summary: dict[str, Any] = {
        "case": case.fixture_id,
        "method": case.method,
        "did": case.did,
        "did_source": case.source,
        "provider": case.provider_id,
        "provider_endpoint": case.provider_endpoint,
        "provider_adapter": case.provider_adapter,
        "request_count": 1,
        "service_http_status": response.status_code,
        "provider_http_status": provider_attempt.get("http_status"),
        "provider_transport_outcome": provider_attempt.get("transport_outcome"),
        "avdr_status": status,
        "acceptance_profile": body.get("acceptance_profile"),
        "acceptance": "PASS" if checks["w3c_basic_accepted"] else "FAIL",
        "returned_provider": result.get("returned_by"),
        "normalized_result_structure": {
            "didResolutionMetadata": isinstance(result.get("didResolutionMetadata"), dict),
            "didDocument": isinstance(document, dict),
            "didDocumentMetadata": isinstance(result.get("didDocumentMetadata"), dict),
        },
        "receipt_id": receipt_id,
        "receipt_hash": receipt_hash_value,
        "receipt_verification": (
            "PASS" if checks["receipt_verification_ok"] else "FAIL"
        ),
        "evidence_mode": body.get("evidence", {}).get("mode"),
        "anchor_status": lookup.get("anchor", {}).get("status"),
        "single_observed_request_latency_ms": body.get("completion_latency_ms"),
        "checks": checks,
        "failure_classification": _failure_classification(body),
    }
    if status != "PASS":
        summary["raw_failure"] = body
    return summary


async def execute_live(timeout_seconds: float) -> dict[str, Any]:
    cases = qualification_cases()
    inventory = load_provider_inventory()
    provider = inventory.get(PROVIDER_ID)
    budget = BudgetRegistry(inventory)
    configured_limit = budget.get(PROVIDER_ID).limit
    if configured_limit is not None and MAX_PUBLIC_REQUESTS > configured_limit:
        raise ValueError("fixed qualification budget exceeds provider limit")

    recorder = LocalAuditRecorder()
    results: list[dict[str, Any]] = []
    previous_hash = None
    with tempfile.TemporaryDirectory(prefix="avdr-p9-telemetry-") as telemetry:
        app = create_app(
            inventory=inventory,
            sink=TelemetrySink(telemetry),
            budgets=budget,
            timeout_ms=int(timeout_seconds * 1000),
            single_static_target=PROVIDER_ID,
            launch_order_seed=None,
            evidence_mode="real",
            audit_recorder=recorder,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False
        ) as outbound, httpx.AsyncClient(
            transport=transport, base_url="http://avdr-p9"
        ) as service:
            app.state.client = outbound
            for case in cases:
                result = await qualify_case(service, case, previous_hash)
                results.append(result)
                if result.get("receipt_hash"):
                    previous_hash = result["receipt_hash"]
                if result.get("failure_classification") == "rate_limited":
                    break

    actual_requests = sum(row["request_count"] for row in results)
    return {
        "schema_version": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real DID interoperability qualification only",
        "disclaimer": (
            "These observations do not establish global performance, availability, "
            "reliability, or generalization across DID infrastructure. Latencies are "
            "single observed request traces only."
        ),
        "provider_inventory_version": inventory.inventory_version,
        "provider_inventory_hash": inventory.inventory_hash(),
        "provider": {
            "id": provider.id,
            "endpoint": provider.endpoint,
            "adapter": provider.adapter,
            "supported_methods": provider.supported_did_methods,
            "dns": dns_observation(provider.endpoint),
        },
        "request_budget": {
            "maximum": MAX_PUBLIC_REQUESTS,
            "actual": actual_requests,
            "retries": 0,
            "stopped_on_rate_limit": any(
                row.get("failure_classification") == "rate_limited"
                for row in results
            ),
        },
        "policy": POLICY,
        "acceptance_profile": PROFILE_W3C_BASIC_V1,
        "controlled_provider_used": False,
        "controlled_history_imported": False,
        "performance_generalization_claimed": False,
        "cases": results,
        "qualified_method_count": sum(row["avdr_status"] == "PASS" for row in results),
        "overall_status": (
            "PASS"
            if len(results) == len(cases)
            and all(row["avdr_status"] == "PASS" for row in results)
            else "PARTIAL_OR_FAIL"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="explicitly permit the three fixed public resolver requests",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.execute_live:
        print(
            "LIVE QUALIFICATION DISABLED: pass --execute-live to permit exactly "
            f"{MAX_PUBLIC_REQUESTS} public requests."
        )
        return 2
    if args.timeout_seconds <= 0 or args.timeout_seconds > 60:
        parser.error("--timeout-seconds must be in (0, 60]")

    report = asyncio.run(execute_live(args.timeout_seconds))
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    print(encoded)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
