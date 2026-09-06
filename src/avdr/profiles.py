"""Versioned acceptance profiles for real DID resolution results.

`w3c-basic-v1` is a STRUCTURAL profile. It checks that a response is a
processable DID Resolution Result for the DID that was requested. It performs

  NO signature verification
  NO proof verification
  NO key-material validation
  NO freshness / version checking
  NO cross-provider agreement

so a passing result must be described as

    "structurally acceptable under w3c-basic-v1"

and never as "verified", "valid", or "cryptographically verified". Nothing in
this module establishes any cryptographic property.

Each check is recorded individually and `accepted` is derived from them, so a
rejection can always be attributed to a specific rule rather than to an
opaque boolean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROFILE_W3C_BASIC_V1 = "w3c-basic-v1"

# Media types that carry a DID Resolution Result or a DID document. Compared
# on the type/subtype only: charset and profile parameters legitimately vary
# between conforming implementations.
ACCEPTABLE_MEDIA_TYPES = (
    "application/did-resolution",
    "application/ld+json",
    "application/did+ld+json",
    "application/did+json",
    "application/did",
    "application/json",
)

CHECK_ORDER = (
    "transport_success",
    "media_type_processable",
    "body_parseable",
    "no_resolution_error",
    "did_document_present",
    "did_document_id_matches_request",
    "structurally_processable",
)


def media_type_of(content_type: str | None) -> str | None:
    """Strip parameters: 'application/did-resolution;charset=utf-8' -> type."""
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


@dataclass
class ProfileResult:
    profile: str
    checks: dict[str, bool | None] = field(default_factory=dict)
    accepted: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "checks": dict(self.checks),
            "accepted": self.accepted,
            "reason": self.reason,
        }


def evaluate_w3c_basic_v1(
    *,
    requested_did: str,
    transport_ok: bool,
    http_status: int | None,
    content_type: str | None,
    parsed_body: Any,
    resolution_error: Any,
    did_document: Any,
) -> ProfileResult:
    """Apply the structural profile. Checks not reachable are recorded None.

    A check recorded as None means "could not be evaluated" (an earlier stage
    already failed), which is deliberately distinct from False.
    """
    result = ProfileResult(profile=PROFILE_W3C_BASIC_V1)
    checks: dict[str, bool | None] = {name: None for name in CHECK_ORDER}

    checks["transport_success"] = bool(transport_ok)
    if not transport_ok:
        result.checks = checks
        result.reason = "transport did not complete"
        return result

    media_type = media_type_of(content_type)
    checks["media_type_processable"] = media_type in ACCEPTABLE_MEDIA_TYPES

    checks["body_parseable"] = parsed_body is not None
    if parsed_body is None:
        result.checks = checks
        result.reason = "response body could not be parsed as JSON"
        return result

    # A DID Resolution error means resolution failed, regardless of status.
    checks["no_resolution_error"] = resolution_error is None

    checks["did_document_present"] = isinstance(did_document, dict)
    if not isinstance(did_document, dict):
        result.checks = checks
        result.reason = (
            "no didDocument in response"
            if resolution_error is None
            else f"resolution error: {_error_label(resolution_error)}"
        )
        return result

    document_id = did_document.get("id")
    checks["did_document_id_matches_request"] = document_id == requested_did

    # Structurally processable: a JSON object carrying an id string.
    checks["structurally_processable"] = isinstance(document_id, str) and bool(
        document_id
    )

    result.checks = checks
    failed = [name for name, value in checks.items() if value is False]
    result.accepted = not failed
    if result.accepted:
        result.reason = f"structurally acceptable under {PROFILE_W3C_BASIC_V1}"
    elif "did_document_id_matches_request" in failed:
        result.reason = (
            f"didDocument.id {document_id!r} does not match requested DID "
            f"{requested_did!r}"
        )
    else:
        result.reason = f"failed {PROFILE_W3C_BASIC_V1} checks: {', '.join(failed)}"
    return result


def _error_label(resolution_error: Any) -> str:
    """DID Resolution errors appear both as strings and as objects."""
    if isinstance(resolution_error, dict):
        return str(
            resolution_error.get("type")
            or resolution_error.get("title")
            or resolution_error
        )
    return str(resolution_error)


def error_family(resolution_error: Any, http_status: int | None) -> str | None:
    """Coarse, provider-neutral label for a DID Resolution error.

    Maps the observed shapes onto the error names used by the DID Resolution
    specification. Anything unrecognised is passed through as `other` rather
    than being forced into a familiar bucket.
    """
    if resolution_error is None:
        return None
    label = _error_label(resolution_error).strip().lower().replace("_", "")
    if "methodnotsupported" in label:
        return "methodNotSupported"
    if "notfound" in label:
        return "notFound"
    if "invaliddid" in label or "invalidpublickey" in label:
        return "invalidDid"
    if "representationnotsupported" in label:
        return "representationNotSupported"
    if "internalerror" in label or "resolvererror" in label:
        return "internalError"
    return "other"
