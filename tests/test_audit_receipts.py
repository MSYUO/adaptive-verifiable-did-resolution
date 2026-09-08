"""Canonical audit receipt, privacy, tamper, and hash-chain tests."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from avdr.audit import (
    AUDIT_SCHEMA_VERSION,
    DID_DOMAIN,
    RECEIPT_DOMAIN,
    RESULT_DOMAIN,
    AnchorResult,
    LocalAuditRecorder,
    build_commitment,
    canonical_json_bytes,
    future_anchor_payload,
    receipt_hash,
    verify_receipt_payload,
)

DID = "did:example:private-subject"
NONCE = "00112233445566778899aabbccddeeff"
RESULT = {
    "returned_by": "resolver-b",
    "acceptance_profile": "w3c-basic-v1",
    "accepted": True,
    "didResolutionMetadata": {"contentType": "application/did+ld+json"},
    "didDocument": {"id": DID, "service": [{"id": "#messages"}]},
    "didDocumentMetadata": {},
}
POLICY_IDENTITY = {
    "name": "adaptive-min-set",
    "implementation": "avdr.real_router.adaptive_policy.RealAdaptiveMinSet",
    "identity_hash": "sha256:" + "1" * 64,
}
ESTIMATOR_IDENTITY = {
    "estimator_id": "b1-rolling-empirical",
    "estimator_version": "v1",
    "estimator_config_hash": "sha256:" + "2" * 64,
    "identity_hash": "sha256:" + "3" * 64,
}


def commitment(**overrides):
    values = {
        "request_id": "request-1",
        "did": DID,
        "strategy": "adaptive-min-set",
        "selected_resolver_ids": ["resolver-b", "resolver-a"],
        "launch_order": ["resolver-b", "resolver-a"],
        "candidate_count": 3,
        "policy_identity": POLICY_IDENTITY,
        "estimator_identity": ESTIMATOR_IDENTITY,
        "estimator_config_hash": ESTIMATOR_IDENTITY["estimator_config_hash"],
        "acceptance_profile": "w3c-basic-v1",
        "returned_provider": "resolver-b",
        "normalized_result": RESULT,
        "calls_used": 2,
        "evidence_mode": "real",
        "timestamp": "2026-09-08T00:00:00Z",
        "selection_mode": "exact",
        "target_success": 0.9,
        "estimated_success": 0.95,
        "request_nonce": NONCE,
    }
    values.update(overrides)
    return build_commitment(**values)


def test_canonical_json_and_receipt_hash_are_reproducible():
    left = {"z": [3, 2, 1], "a": {"b": True, "a": None}}
    right = {"a": {"a": None, "b": True}, "z": [3, 2, 1]}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)

    payload = commitment().receipt_payload(None)
    reparsed = json.loads(json.dumps(payload, indent=4, ensure_ascii=False))
    assert canonical_json_bytes(payload) == canonical_json_bytes(reparsed)
    assert receipt_hash(payload) == receipt_hash(reparsed)
    assert payload["schema_version"] == AUDIT_SCHEMA_VERSION


def test_selected_set_is_sorted_but_launch_order_remains_semantic():
    first = commitment(
        selected_resolver_ids=["resolver-b", "resolver-a"],
        launch_order=["resolver-b", "resolver-a"],
    ).receipt_payload(None)
    same_set = commitment(
        selected_resolver_ids=["resolver-a", "resolver-b"],
        launch_order=["resolver-b", "resolver-a"],
    ).receipt_payload(None)
    changed_launch = commitment(
        selected_resolver_ids=["resolver-a", "resolver-b"],
        launch_order=["resolver-a", "resolver-b"],
    ).receipt_payload(None)

    assert first == same_set
    assert first["selected_resolver_ids"] == ["resolver-a", "resolver-b"]
    assert first["launch_order"] == ["resolver-b", "resolver-a"]
    assert receipt_hash(first) != receipt_hash(changed_launch)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("requested_did_commitment", "sha256:" + "a" * 64),
        ("strategy", "all-race"),
        ("selected_resolver_ids", ["resolver-a"]),
        ("returned_provider", "resolver-a"),
        ("result_commitment", "sha256:" + "b" * 64),
        ("evidence_mode", "controlled_demo"),
        ("timestamp", "2026-09-08T00:00:01Z"),
    ],
)
def test_receipt_field_tampering_fails(field, replacement):
    original = commitment().receipt_payload(None)
    expected = receipt_hash(original)
    tampered = copy.deepcopy(original)
    tampered[field] = replacement
    verification = verify_receipt_payload(tampered, expected)
    assert verification["valid"] is False
    assert verification["receipt_hash_valid"] is False


def test_policy_identity_tampering_fails():
    original = commitment().receipt_payload(None)
    tampered = copy.deepcopy(original)
    tampered["policy_identity"]["name"] = "single-static"
    assert not verify_receipt_payload(tampered, receipt_hash(original))["valid"]


def test_did_and_result_disclosure_verification_detects_content_tampering():
    payload = commitment().receipt_payload(None)
    digest = receipt_hash(payload)
    verified = verify_receipt_payload(
        payload,
        digest,
        disclosed_did=DID,
        disclosed_result=RESULT,
    )
    assert verified["valid"] is True
    assert verified["did_commitment_valid"] is True
    assert verified["result_commitment_valid"] is True

    wrong_result = copy.deepcopy(RESULT)
    wrong_result["didDocument"]["service"][0]["id"] = "#tampered"
    failed = verify_receipt_payload(
        payload,
        digest,
        disclosed_did="did:example:wrong",
        disclosed_result=wrong_result,
    )
    assert failed["valid"] is False
    assert failed["did_commitment_valid"] is False
    assert failed["result_commitment_valid"] is False


def test_default_receipt_bytes_exclude_raw_did_and_use_domain_separators():
    payload = commitment().receipt_payload(None)
    encoded = canonical_json_bytes(payload)
    assert DID.encode() not in encoded
    assert payload["request_nonce"] == NONCE
    assert payload["requested_did_commitment"].startswith("sha256:")
    assert payload["result_commitment"].startswith("sha256:")
    assert len({DID_DOMAIN, RESULT_DOMAIN, RECEIPT_DOMAIN}) == 3


@pytest.mark.asyncio
async def test_local_hash_chain_genesis_link_and_verification():
    recorder = LocalAuditRecorder()
    first = await recorder.record(commitment())
    second = await recorder.record(
        commitment(
            request_id="request-2",
            timestamp="2026-09-08T00:00:02Z",
            request_nonce="ffeeddccbbaa99887766554433221100",
        )
    )
    first_stored = await recorder.get(first.receipt_id)
    second_stored = await recorder.get(second.receipt_id)

    assert first_stored["receipt"]["previous_receipt_hash"] is None
    assert second_stored["receipt"]["previous_receipt_hash"] == first.receipt_hash
    assert (await recorder.verify(first.receipt_id))["valid"] is True
    assert (await recorder.verify(second.receipt_id))["chain_valid"] is True


@pytest.mark.asyncio
async def test_local_hash_chain_detects_tampered_earlier_entry():
    recorder = LocalAuditRecorder()
    first = await recorder.record(commitment())
    second = await recorder.record(
        commitment(
            request_id="request-2",
            timestamp="2026-09-08T00:00:02Z",
            request_nonce="ffeeddccbbaa99887766554433221100",
        )
    )
    original = recorder._entries[0]
    tampered_payload = original.payload()
    tampered_payload["strategy"] = "tampered"
    recorder._entries[0] = replace(
        original, canonical_bytes=canonical_json_bytes(tampered_payload)
    )

    verification = await recorder.verify(second.receipt_id)
    assert first.receipt_hash != receipt_hash(tampered_payload)
    assert verification["valid"] is False
    assert verification["chain_valid"] is False


@pytest.mark.asyncio
async def test_executed_failure_receipt_is_verifiable():
    failure = {
        "returned_by": None,
        "acceptance_profile": "w3c-basic-v1",
        "accepted": False,
        "didResolutionMetadata": None,
        "didDocument": None,
        "didDocumentMetadata": None,
    }
    recorder = LocalAuditRecorder()
    created = await recorder.record(
        commitment(
            returned_provider=None,
            normalized_result=failure,
            calls_used=2,
            estimator_identity=None,
            estimator_config_hash=None,
        )
    )
    stored = await recorder.get(created.receipt_id)
    verified = await recorder.verify(
        created.receipt_id, disclosed_did=DID, disclosed_result=failure
    )
    assert stored["receipt"]["returned_provider"] is None
    assert verified["valid"] is True
    assert verified["result_commitment_valid"] is True


@pytest.mark.asyncio
async def test_anchor_receives_only_minimum_payload():
    class CapturingAnchor:
        received = None

        async def anchor(self, digest, metadata):
            self.received = (digest, metadata)
            return AnchorResult(status="not_configured")

    anchor = CapturingAnchor()
    recorder = LocalAuditRecorder(anchor=anchor)
    created = await recorder.record(commitment())
    stored = await recorder.get(created.receipt_id)
    expected = future_anchor_payload(created.receipt_hash, stored["receipt"])

    assert anchor.received == (created.receipt_hash, expected)
    serialized = canonical_json_bytes(anchor.received[1])
    assert DID.encode() not in serialized
    assert b"didDocument" not in serialized
    assert set(anchor.received[1]) == {
        "receipt_hash",
        "schema_version",
        "timestamp",
        "policy_identity_hash",
    }
