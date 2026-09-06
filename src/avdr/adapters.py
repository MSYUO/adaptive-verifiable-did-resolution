"""Provider adapters: map real resolver responses into one internal model.

Provider-specific knowledge lives HERE and nowhere else. Policies, the shadow
harness and the analysis layer see only the normalized observation, so an API
difference between providers can never be mistaken for a DID-semantics
difference downstream.

Two adapters are needed for the qualified providers:

  universal-resolver-v1   full DID Resolution Result
                          {didResolutionMetadata, didDocument,
                           didDocumentMetadata}

  did-document-only-v1    a bare driver response carrying only {didDocument}.
                          Absent metadata is recorded as null -- explicitly
                          "this provider did not supply it" -- and is never
                          synthesised to make providers look alike.

Normalization is limited to what the standard justifies: locating the
document and the resolution metadata, and canonicalising key order for
hashing. JSON key order, optional metadata, route metadata and
implementation-specific fields are NOT normalized away, and documents are
never compared byte-for-byte across providers.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .profiles import ProfileResult, error_family, evaluate_w3c_basic_v1


def canonical_hash(payload: Any) -> str | None:
    """Stable hash of a JSON value, key-order independent."""
    if payload is None:
        return None
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def raw_hash(raw_bytes: bytes | None) -> str | None:
    """Hash of the exact bytes received, before any parsing."""
    if raw_bytes is None:
        return None
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


@dataclass
class NormalizedResult:
    """Provider-neutral view of one real resolver response."""

    adapter: str
    http_status: int | None
    content_type: str | None
    transport_outcome: str
    transport_ok: bool

    raw_response_hash: str | None = None
    raw_response_bytes: int | None = None

    resolution_metadata: dict | None = None
    normalized_did_document: dict | None = None
    did_document_metadata: dict | None = None
    provider_route_metadata: dict | None = None

    resolution_error: Any = None
    resolution_error_family: str | None = None

    normalized_document_hash: str | None = None
    parse_error: str | None = None

    acceptance: ProfileResult | None = None
    # Kept beside the normalized object, not inside it.
    raw_body: Any = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return bool(self.acceptance and self.acceptance.accepted)

    @property
    def subject_id(self) -> str | None:
        if isinstance(self.normalized_did_document, dict):
            value = self.normalized_did_document.get("id")
            return value if isinstance(value, str) else None
        return None


class ResolverAdapter(ABC):
    """Maps one provider's response shape into NormalizedResult."""

    name: str = "abstract"

    @abstractmethod
    def resolve_path(self, did: str) -> str:
        """Path component for resolving `did` on this provider."""

    @abstractmethod
    def extract(self, body: Any) -> tuple[Any, Any, Any, Any]:
        """Return (resolution_metadata, did_document, document_metadata, route_metadata)."""

    def accept_header(self) -> str | None:
        return None

    def normalize(
        self,
        *,
        requested_did: str,
        http_status: int | None,
        content_type: str | None,
        raw_bytes: bytes | None,
        transport_outcome: str,
        transport_ok: bool,
        parse_error: str | None = None,
        body: Any = None,
    ) -> NormalizedResult:
        result = NormalizedResult(
            adapter=self.name,
            http_status=http_status,
            content_type=content_type,
            transport_outcome=transport_outcome,
            transport_ok=transport_ok,
            raw_response_hash=raw_hash(raw_bytes),
            raw_response_bytes=len(raw_bytes) if raw_bytes is not None else None,
            parse_error=parse_error,
            raw_body=body,
        )

        if transport_ok and body is not None:
            resolution_metadata, document, document_metadata, route = self.extract(body)
            result.resolution_metadata = (
                resolution_metadata if isinstance(resolution_metadata, dict) else None
            )
            result.normalized_did_document = document if isinstance(document, dict) else None
            result.did_document_metadata = (
                document_metadata if isinstance(document_metadata, dict) else None
            )
            result.provider_route_metadata = route if isinstance(route, dict) else None
            result.normalized_document_hash = canonical_hash(result.normalized_did_document)

            if isinstance(resolution_metadata, dict):
                result.resolution_error = resolution_metadata.get("error")
            result.resolution_error_family = error_family(
                result.resolution_error, http_status
            )

        result.acceptance = evaluate_w3c_basic_v1(
            requested_did=requested_did,
            transport_ok=transport_ok,
            http_status=http_status,
            content_type=content_type,
            parsed_body=body,
            resolution_error=result.resolution_error,
            did_document=result.normalized_did_document,
        )
        return result


class UniversalResolverAdapter(ResolverAdapter):
    """Full DID Resolution Result, as served by Universal Resolver front ends."""

    name = "universal-resolver-v1"

    def resolve_path(self, did: str) -> str:
        return f"/1.0/identifiers/{did}"

    def extract(self, body: Any) -> tuple[Any, Any, Any, Any]:
        if not isinstance(body, dict):
            return None, None, None, None
        resolution_metadata = body.get("didResolutionMetadata")

        # Route/driver information is implementation-specific. It is kept as
        # provider_route_metadata rather than being treated as DID semantics.
        route = None
        if isinstance(resolution_metadata, dict):
            route = {
                key: resolution_metadata[key]
                for key in ("driverUrl", "pattern", "driverDuration", "duration")
                if key in resolution_metadata
            } or None

        return (
            resolution_metadata,
            body.get("didDocument"),
            body.get("didDocumentMetadata"),
            route,
        )


class DidDocumentOnlyAdapter(ResolverAdapter):
    """Bare driver response carrying only a didDocument.

    Absent resolution/document metadata is reported as null. It is NOT
    fabricated to match the richer providers -- the absence is a real,
    recordable property of this provider.
    """

    name = "did-document-only-v1"

    def resolve_path(self, did: str) -> str:
        return f"/1.0/identifiers/{did}"

    def extract(self, body: Any) -> tuple[Any, Any, Any, Any]:
        if not isinstance(body, dict):
            return None, None, None, None
        # Some drivers return the document at the top level instead of nested.
        document = body.get("didDocument")
        if document is None and "id" in body and "@context" in body:
            document = body
        return None, document, None, None


ADAPTERS: dict[str, ResolverAdapter] = {
    UniversalResolverAdapter.name: UniversalResolverAdapter(),
    DidDocumentOnlyAdapter.name: DidDocumentOnlyAdapter(),
}


def get_adapter(name: str) -> ResolverAdapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[name]
