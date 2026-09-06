"""Minimum deterministic DID-document acceptance checks.

Scope note: this is structural validation only. There is deliberately NO
cryptographic proof verification, no signature check, no freshness/version
check and no cross-resolver agreement in this milestone. Those belong to
later phases and must not be implied by this module.

The checks here exist so that a resolver returning HTTP 200 with a
structurally unacceptable body can be distinguished from a resolver
returning a usable result -- the minimum needed for fault-injection
Scenario I.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DID_CONTEXT_V1 = "https://www.w3.org/ns/did/v1"

# Ordered list of the structural rules applied, for documentation/report use.
ACCEPTANCE_RULES = (
    "response body is a JSON object",
    "body contains a didDocument object",
    "didDocument['@context'] includes " + DID_CONTEXT_V1,
    "didDocument.id is a non-empty string",
    "didDocument.id equals the requested DID",
    "didDocument.verificationMethod is a non-empty list",
)

ACCEPTANCE_RULESET_VERSION = "structural-v1"


@dataclass(frozen=True)
class AcceptanceResult:
    valid: bool
    reason: str


def _ok() -> AcceptanceResult:
    return AcceptanceResult(True, "structural checks passed")


def _bad(reason: str) -> AcceptanceResult:
    return AcceptanceResult(False, reason)


def check_did_document(requested_did: str, payload: Any) -> AcceptanceResult:
    """Apply the structural ruleset to a resolver response body."""
    if not isinstance(payload, dict):
        return _bad("response body is not a JSON object")

    document = payload.get("didDocument")
    if not isinstance(document, dict):
        return _bad("response body has no didDocument object")

    context = document.get("@context")
    if isinstance(context, str):
        context = [context]
    if not isinstance(context, list) or DID_CONTEXT_V1 not in context:
        return _bad(f"didDocument['@context'] does not include {DID_CONTEXT_V1}")

    document_id = document.get("id")
    if not isinstance(document_id, str) or not document_id:
        return _bad("didDocument.id is missing or not a non-empty string")
    if document_id != requested_did:
        return _bad(
            f"didDocument.id {document_id!r} does not match requested DID {requested_did!r}"
        )

    verification_method = document.get("verificationMethod")
    if not isinstance(verification_method, list) or not verification_method:
        return _bad("didDocument.verificationMethod is missing or empty")

    return _ok()


def parse_did_method(did: str) -> str | None:
    """Return the method segment of a DID, or None if the DID is malformed."""
    parts = did.split(":")
    if len(parts) < 3 or parts[0] != "did" or not parts[1] or not parts[2]:
        return None
    return parts[1]
