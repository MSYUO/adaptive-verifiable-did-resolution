"""Unit tests for the structural acceptance ruleset."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.acceptance import DID_CONTEXT_V1, check_did_document, parse_did_method
from avdr.resolver.app import build_did_document, build_invalid_did_document

DID = "did:example:subject-1"


def test_synthetic_document_is_accepted():
    payload = {"didDocument": build_did_document(DID, "resolver-a")}
    result = check_did_document(DID, payload)
    assert result.valid, result.reason


def test_injected_invalid_document_is_rejected():
    payload = {"didDocument": build_invalid_did_document(DID)}
    result = check_did_document(DID, payload)
    assert not result.valid
    assert "does not match requested DID" in result.reason


def test_subject_mismatch_is_rejected():
    document = build_did_document("did:example:other", "resolver-a")
    result = check_did_document(DID, {"didDocument": document})
    assert not result.valid


def test_missing_verification_method_is_rejected():
    document = build_did_document(DID, "resolver-a")
    document.pop("verificationMethod")
    result = check_did_document(DID, {"didDocument": document})
    assert not result.valid
    assert "verificationMethod" in result.reason


def test_missing_context_is_rejected():
    document = build_did_document(DID, "resolver-a")
    document["@context"] = ["https://example.org/other"]
    result = check_did_document(DID, {"didDocument": document})
    assert not result.valid
    assert DID_CONTEXT_V1 in result.reason


def test_non_object_body_is_rejected():
    assert not check_did_document(DID, ["not", "an", "object"]).valid
    assert not check_did_document(DID, None).valid


def test_parse_did_method():
    assert parse_did_method("did:example:abc") == "example"
    assert parse_did_method("did:ethr:0x123") == "ethr"
    assert parse_did_method("not-a-did") is None
    assert parse_did_method("did:example") is None
    assert parse_did_method("did::abc") is None
