"""Unit tests for provider adapters and the w3c-basic-v1 acceptance profile.

These use recorded response shapes and a mocked HTTP transport. They never
touch the public Internet: real endpoint calls belong only in the explicit
qualification script.

The fixture bodies below are trimmed copies of shapes actually observed on
2026-09-07 from the DIF Universal Resolver and the self-hosted
universalresolver/driver-did-key container.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.adapters import (
    DidDocumentOnlyAdapter,
    UniversalResolverAdapter,
    canonical_hash,
    get_adapter,
    raw_hash,
)
from avdr.profiles import (
    PROFILE_W3C_BASIC_V1,
    error_family,
    evaluate_w3c_basic_v1,
    media_type_of,
)

DID_KEY = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"

# Observed shape: DIF Universal Resolver (driver-didkit).
UNIRESOLVER_BODY = {
    "didResolutionMetadata": {
        "driverDuration": 2,
        "contentType": "application/did",
        "pattern": "^did:(?:pkh:|key:(?:z6Mk|z6LS|zQ3s|z.{200,})).+$",
        "driverUrl": "http://driver-didkit:3000/identifiers/$1",
        "duration": 3,
        "did": {"didString": DID_KEY, "methodSpecificId": DID_KEY[8:], "method": "key"},
    },
    "didDocument": {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": DID_KEY,
        "verificationMethod": [
            {
                "id": f"{DID_KEY}#{DID_KEY[8:]}",
                "type": "Ed25519VerificationKey2018",
                "controller": DID_KEY,
                "publicKeyJwk": {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": "Lm_M42cB3HkUiODQsXRcweM6TByfzEHGO9ND274JcOY",
                },
            }
        ],
        "authentication": [f"{DID_KEY}#{DID_KEY[8:]}"],
    },
    "didDocumentMetadata": {},
}

# Observed shape: self-hosted universalresolver/driver-did-key.
DRIVER_BODY = {
    "didDocument": {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/jws-2020/v1",
        ],
        "id": DID_KEY,
        "verificationMethod": [
            {
                "id": f"{DID_KEY}#{DID_KEY[8:]}",
                "type": "JsonWebKey2020",
                "controller": DID_KEY,
                "publicKeyJwk": {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": "Lm_M42cB3HkUiODQsXRcweM6TByfzEHGO9ND274JcOY",
                },
            }
        ],
        "authentication": [f"{DID_KEY}#{DID_KEY[8:]}"],
        "capabilityInvocation": [f"{DID_KEY}#{DID_KEY[8:]}"],
    }
}

# Observed shape: unsupported DID method (HTTP 501).
UNSUPPORTED_BODY = {
    "didResolutionMetadata": {
        "error": {
            "type": "METHOD_NOT_SUPPORTED",
            "title": "The DID method is not supported.",
            "detail": "Method not supported: avdrnotamethod",
        }
    },
    "didDocument": None,
    "didDocumentMetadata": {},
}

NOT_FOUND_BODY = {
    "didResolutionMetadata": {
        "error": {
            "type": "NOT_FOUND",
            "title": "The DID or DID document was not found.",
            "detail": "resolver_error: DID must resolve to a valid https URL",
        }
    },
    "didDocument": None,
    "didDocumentMetadata": {},
}


def normalize(adapter, body, *, did=DID_KEY, status=200, ctype="application/did-resolution;charset=utf-8"):
    raw = json.dumps(body).encode()
    return adapter.normalize(
        requested_did=did,
        http_status=status,
        content_type=ctype,
        raw_bytes=raw,
        transport_outcome="http_response",
        transport_ok=True,
        body=body,
    )


# --------------------------------------------------------------------------
# 1. adapter normalization
# --------------------------------------------------------------------------


def test_universal_resolver_adapter_extracts_all_three_sections():
    result = normalize(UniversalResolverAdapter(), UNIRESOLVER_BODY)
    assert result.normalized_did_document["id"] == DID_KEY
    assert result.resolution_metadata is not None
    assert result.did_document_metadata == {}
    assert result.provider_route_metadata is not None
    assert result.provider_route_metadata["driverUrl"].startswith("http://driver-didkit")
    assert result.accepted is True


def test_driver_adapter_reports_absent_metadata_as_null_not_invented():
    """The absence of metadata is a real provider property, not a gap to fill."""
    result = normalize(
        DidDocumentOnlyAdapter(),
        DRIVER_BODY,
        ctype='application/ld+json;profile="https://w3id.org/did-resolution"',
    )
    assert result.normalized_did_document["id"] == DID_KEY
    assert result.resolution_metadata is None
    assert result.did_document_metadata is None
    assert result.provider_route_metadata is None
    assert result.accepted is True


def test_driver_adapter_accepts_top_level_document():
    body = dict(DRIVER_BODY["didDocument"])
    result = normalize(DidDocumentOnlyAdapter(), body)
    assert result.normalized_did_document["id"] == DID_KEY
    assert result.accepted is True


def test_two_implementations_produce_different_document_hashes():
    """Same DID, same key material, different conforming representations.

    This is the reason byte-for-byte cross-provider comparison is invalid.
    """
    a = normalize(UniversalResolverAdapter(), UNIRESOLVER_BODY)
    b = normalize(DidDocumentOnlyAdapter(), DRIVER_BODY)
    assert a.accepted and b.accepted
    assert a.subject_id == b.subject_id == DID_KEY
    assert a.normalized_document_hash != b.normalized_document_hash
    # ...but the key material is identical, so neither is "wrong".
    key_a = a.normalized_did_document["verificationMethod"][0]["publicKeyJwk"]["x"]
    key_b = b.normalized_did_document["verificationMethod"][0]["publicKeyJwk"]["x"]
    assert key_a == key_b


def test_resolve_paths_and_registry():
    assert UniversalResolverAdapter().resolve_path(DID_KEY).endswith(DID_KEY)
    assert get_adapter("universal-resolver-v1").name == "universal-resolver-v1"
    assert get_adapter("did-document-only-v1").name == "did-document-only-v1"
    with pytest.raises(KeyError):
        get_adapter("no-such-adapter")


# --------------------------------------------------------------------------
# 2. acceptance profile
# --------------------------------------------------------------------------


def test_profile_records_each_check_separately():
    result = normalize(UniversalResolverAdapter(), UNIRESOLVER_BODY)
    checks = result.acceptance.checks
    for name in (
        "transport_success",
        "media_type_processable",
        "body_parseable",
        "no_resolution_error",
        "did_document_present",
        "did_document_id_matches_request",
        "structurally_processable",
    ):
        assert name in checks
    assert all(v is True for v in checks.values())
    assert result.acceptance.accepted is True
    # Wording must not imply cryptographic verification.
    assert "structurally acceptable" in result.acceptance.reason
    assert "verified" not in result.acceptance.reason


def test_accepted_is_derivable_from_checks():
    result = normalize(UniversalResolverAdapter(), UNIRESOLVER_BODY)
    derived = all(v is not False for v in result.acceptance.checks.values())
    assert result.acceptance.accepted == derived


def test_transport_failure_short_circuits_profile():
    result = UniversalResolverAdapter().normalize(
        requested_did=DID_KEY,
        http_status=None,
        content_type=None,
        raw_bytes=None,
        transport_outcome="timeout",
        transport_ok=False,
        body=None,
    )
    assert result.accepted is False
    assert result.acceptance.checks["transport_success"] is False
    # Later checks were never reachable: None, not False.
    assert result.acceptance.checks["did_document_present"] is None


def test_media_type_parameters_are_ignored():
    assert media_type_of("application/did-resolution;charset=utf-8") == "application/did-resolution"
    assert media_type_of('application/ld+json;profile="x"') == "application/ld+json"
    assert media_type_of(None) is None


def test_profile_version_is_recorded():
    result = normalize(UniversalResolverAdapter(), UNIRESOLVER_BODY)
    assert result.acceptance.profile == PROFILE_W3C_BASIC_V1


# --------------------------------------------------------------------------
# 3. DID Resolution error handling
# --------------------------------------------------------------------------


def test_method_not_supported_is_not_accepted():
    result = normalize(
        UniversalResolverAdapter(), UNSUPPORTED_BODY,
        did="did:avdrnotamethod:qualification", status=501,
    )
    assert result.accepted is False
    assert result.resolution_error_family == "methodNotSupported"
    assert result.acceptance.checks["no_resolution_error"] is False
    assert result.normalized_did_document is None


def test_not_found_is_not_accepted():
    result = normalize(
        UniversalResolverAdapter(), NOT_FOUND_BODY,
        did="did:web:avdr-nonexistent-qualification.invalid", status=404,
    )
    assert result.accepted is False
    assert result.resolution_error_family == "notFound"


def test_error_family_mapping_and_passthrough():
    assert error_family({"type": "METHOD_NOT_SUPPORTED"}, 501) == "methodNotSupported"
    assert error_family({"type": "NOT_FOUND"}, 404) == "notFound"
    assert error_family("invalidDid", 400) == "invalidDid"
    assert error_family("representationNotSupported", 406) == "representationNotSupported"
    # Unrecognised errors are passed through, never forced into a known bucket.
    assert error_family({"type": "SOMETHING_NEW"}, 500) == "other"
    assert error_family(None, 200) is None


def test_http_200_with_resolution_error_is_still_rejected():
    """A 200 status does not override an explicit resolution error."""
    result = normalize(UniversalResolverAdapter(), UNSUPPORTED_BODY, status=200)
    assert result.accepted is False


# --------------------------------------------------------------------------
# 4. requested DID / didDocument.id mismatch
# --------------------------------------------------------------------------


def test_subject_mismatch_is_rejected():
    result = normalize(
        UniversalResolverAdapter(), UNIRESOLVER_BODY, did="did:key:zSomethingElse"
    )
    assert result.accepted is False
    assert result.acceptance.checks["did_document_id_matches_request"] is False
    assert "does not match requested DID" in result.acceptance.reason


def test_subject_mismatch_rejected_for_driver_adapter_too():
    result = normalize(DidDocumentOnlyAdapter(), DRIVER_BODY, did="did:key:zOther")
    assert result.accepted is False


def test_missing_document_is_rejected():
    result = normalize(UniversalResolverAdapter(), {"didResolutionMetadata": {}})
    assert result.accepted is False
    assert result.acceptance.checks["did_document_present"] is False


# --------------------------------------------------------------------------
# 5. raw response hash preservation
# --------------------------------------------------------------------------


def test_raw_response_hash_is_over_exact_bytes():
    body = UNIRESOLVER_BODY
    compact = json.dumps(body, separators=(",", ":")).encode()
    spaced = json.dumps(body, indent=2).encode()
    # Byte-level difference must change the raw hash...
    assert raw_hash(compact) != raw_hash(spaced)
    # ...while the canonical document hash is representation-independent.
    assert canonical_hash(json.loads(compact)) == canonical_hash(json.loads(spaced))
    assert raw_hash(None) is None
    assert canonical_hash(None) is None


def test_raw_hash_and_size_recorded_on_result():
    raw = json.dumps(UNIRESOLVER_BODY).encode()
    result = UniversalResolverAdapter().normalize(
        requested_did=DID_KEY,
        http_status=200,
        content_type="application/did-resolution",
        raw_bytes=raw,
        transport_outcome="http_response",
        transport_ok=True,
        body=UNIRESOLVER_BODY,
    )
    assert result.raw_response_hash == raw_hash(raw)
    assert result.raw_response_bytes == len(raw)
    # Raw body preserved beside the normalized object, not inside it.
    assert result.raw_body is UNIRESOLVER_BODY


def test_canonical_hash_is_key_order_independent():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    assert canonical_hash({"a": [1, 2]}) != canonical_hash({"a": [2, 1]})


# --------------------------------------------------------------------------
# adapter used over a mocked transport (still no real network)
# --------------------------------------------------------------------------


async def test_adapter_over_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/1.0/identifiers/{DID_KEY}"
        return httpx.Response(
            200,
            json=UNIRESOLVER_BODY,
            headers={"content-type": "application/did-resolution;charset=utf-8"},
        )

    adapter = UniversalResolverAdapter()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(
            "https://mock.invalid" + adapter.resolve_path(DID_KEY)
        )
    result = adapter.normalize(
        requested_did=DID_KEY,
        http_status=response.status_code,
        content_type=response.headers.get("content-type"),
        raw_bytes=response.content,
        transport_outcome="http_response",
        transport_ok=True,
        body=response.json(),
    )
    assert result.accepted is True
    assert result.raw_response_hash is not None
