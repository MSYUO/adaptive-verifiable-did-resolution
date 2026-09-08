"""Canonical, privacy-minimized audit receipts for AVDR service requests.

The local recorder is useful without a blockchain: it stores canonical receipt
bytes in a process-local hash chain and can verify them later. The anchoring
interface receives only a receipt hash and minimal metadata, never a raw DID,
DID document, resolver response, or telemetry.

A valid receipt proves that disclosed receipt fields match the committed local
record. It does not prove DID truth, resolver trust, consensus, finality, W3C
conformance, or cryptographic correctness.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

AUDIT_SCHEMA_VERSION = "avdr-audit-v1"
RECEIPT_DOMAIN = "AVDR:AUDIT:RECEIPT:v1"
DID_DOMAIN = "AVDR:DID:v1"
RESULT_DOMAIN = "AVDR:RESULT:v1"
POLICY_DOMAIN = "AVDR:POLICY:v1"
ESTIMATOR_DOMAIN = "AVDR:ESTIMATOR:v1"


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the canonical JSON contract used by every audit hash."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _domain_hash(domain: str, payload: bytes) -> str:
    # NUL is an unambiguous boundary between the fixed ASCII domain and bytes.
    digest = hashlib.sha256(domain.encode("ascii") + b"\x00" + payload).hexdigest()
    return f"sha256:{digest}"


def did_commitment(did: str, request_nonce: str) -> str:
    """Commit to the exact UTF-8 request DID plus a public 128-bit nonce."""
    return _domain_hash(
        DID_DOMAIN,
        canonical_json_bytes({"did": did, "request_nonce": request_nonce}),
    )


def result_commitment(normalized_result: dict[str, Any]) -> str:
    """Commit to the exact normalized service result for this request."""
    return _domain_hash(RESULT_DOMAIN, canonical_json_bytes(normalized_result))


def receipt_hash(receipt_payload: dict[str, Any]) -> str:
    return _domain_hash(RECEIPT_DOMAIN, canonical_json_bytes(receipt_payload))


def _identity(
    domain: str, values: dict[str, Any], hash_field: str = "identity_hash"
) -> dict[str, Any]:
    payload = {key: value for key, value in values.items() if value is not None}
    return {
        **payload,
        hash_field: _domain_hash(domain, canonical_json_bytes(payload)),
    }


def build_policy_identity(policy: object, plan: object) -> dict[str, Any]:
    """Derive policy identity from the actual policy and request plan."""
    metadata = getattr(plan, "metadata", {})
    return _identity(
        POLICY_DOMAIN,
        {
            "name": getattr(plan, "policy"),
            "implementation": (
                f"{type(policy).__module__}.{type(policy).__qualname__}"
            ),
            "execution": getattr(plan, "execution"),
            "optimizer_version": metadata.get("optimizer_version"),
            "cost_model_id": metadata.get("cost_model_id"),
        },
    )


def build_estimator_identity(
    policy: object,
    plan: object,
    runtime_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Derive estimator identity from the same live objects used to plan."""
    estimator = getattr(policy, "estimator", None)
    if estimator is None:
        return None
    metadata = getattr(plan, "metadata", {})
    runtime_metadata = runtime_metadata or {}
    description = estimator.describe() if hasattr(estimator, "describe") else {}
    return _identity(
        ESTIMATOR_DOMAIN,
        {
            "estimator_id": (
                runtime_metadata.get("estimator_id")
                or metadata.get("estimator_id")
                or description.get("estimator_id")
            ),
            "estimator_version": (
                runtime_metadata.get("estimator_version")
                or metadata.get("estimator_version")
                or description.get("estimator_version")
            ),
            "estimator_class": (
                runtime_metadata.get("estimator_class")
                or f"{type(estimator).__module__}.{type(estimator).__qualname__}"
            ),
            "estimator_config_hash": (
                runtime_metadata.get("estimator_config_hash")
                or metadata.get("estimator_config_hash")
                or description.get("estimator_config_hash")
            ),
            "packaged_spec_sha256": runtime_metadata.get("packaged_spec_sha256"),
            "source_pickle_sha256": runtime_metadata.get("source_pickle_sha256"),
        },
    )


@dataclass(frozen=True)
class AuditCommitment:
    """Post-execution facts accepted by an audit recorder."""

    request_id: str
    timestamp: str
    evidence_mode: str
    request_nonce: str
    requested_did_commitment: str
    strategy: str
    selected_resolver_ids: tuple[str, ...]
    launch_order: tuple[str, ...]
    candidate_count: int
    selected_count: int
    policy_identity: dict[str, Any]
    estimator_identity: dict[str, Any] | None
    estimator_config_hash: str | None
    acceptance_profile: str
    returned_provider: str | None
    result_commitment: str
    calls_used: int
    selection_mode: str | None = None
    target_success: float | None = None
    estimated_success: float | None = None
    schema_version: str = AUDIT_SCHEMA_VERSION

    # Compatibility aliases for the P6 recorder boundary.
    @property
    def did_hash(self) -> str:
        return self.requested_did_commitment

    @property
    def selected_providers(self) -> tuple[str, ...]:
        return self.selected_resolver_ids

    @property
    def policy(self) -> str:
        return self.strategy

    @property
    def policy_version_hash(self) -> str:
        return self.policy_identity["identity_hash"]

    @property
    def result_hash(self) -> str:
        return self.result_commitment

    def receipt_payload(self, previous_receipt_hash: str | None) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "evidence_mode": self.evidence_mode,
            "request_nonce": self.request_nonce,
            "requested_did_commitment": self.requested_did_commitment,
            "strategy": self.strategy,
            "selected_resolver_ids": list(self.selected_resolver_ids),
            "launch_order": list(self.launch_order),
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "policy_identity": copy.deepcopy(self.policy_identity),
            "estimator_identity": copy.deepcopy(self.estimator_identity),
            "estimator_config_hash": self.estimator_config_hash,
            "acceptance_profile": self.acceptance_profile,
            "returned_provider": self.returned_provider,
            "result_commitment": self.result_commitment,
            "calls_used": self.calls_used,
            "selection_mode": self.selection_mode,
            "target_success": self.target_success,
            "estimated_success": self.estimated_success,
            "previous_receipt_hash": previous_receipt_hash,
        }


@dataclass(frozen=True)
class AnchorResult:
    status: str
    network: str | None = None
    transaction_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditAnchor(Protocol):
    async def anchor(
        self, receipt_hash_value: str, metadata: dict[str, Any]
    ) -> AnchorResult:
        """Anchor only the receipt hash and minimal non-sensitive metadata."""


class NullAnchor:
    """Explicitly configured absence of any external blockchain."""

    async def anchor(
        self, receipt_hash_value: str, metadata: dict[str, Any]
    ) -> AnchorResult:
        return AnchorResult(status="not_configured")


@dataclass(frozen=True)
class AuditReceipt:
    """Additive service-facing audit status DTO."""

    recorded: bool
    status: str
    reference: str | None = None
    receipt_id: str | None = None
    receipt_hash: str | None = None
    schema_version: str | None = None
    verification: str | None = None
    integrity_verified: bool | None = None
    anchor: AnchorResult | dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "recorded": self.recorded,
            "status": self.status,
            "reference": self.reference,
        }
        optional = {
            "receipt_id": self.receipt_id,
            "receipt_hash": self.receipt_hash,
            "schema_version": self.schema_version,
            "verification": self.verification,
            "integrity_verified": self.integrity_verified,
            "anchor": (
                self.anchor.to_dict()
                if isinstance(self.anchor, AnchorResult)
                else self.anchor
            ),
        }
        result.update({key: value for key, value in optional.items() if value is not None})
        return result


class AuditRecorder(Protocol):
    async def record(self, commitment: AuditCommitment) -> AuditReceipt:
        """Record a privacy-minimized commitment and return its receipt."""


class NullAuditRecorder:
    """Recorder used when local audit is deliberately disabled."""

    async def record(self, commitment: AuditCommitment) -> AuditReceipt:
        return AuditReceipt(recorded=False, status="not_configured")

    def describe(self) -> dict[str, Any]:
        return {
            "recorder": "null",
            "storage": None,
            "hash_chain_enabled": False,
            "anchor": "NullAnchor",
        }


@dataclass(frozen=True)
class _StoredReceipt:
    receipt_id: str
    receipt_hash: str
    canonical_bytes: bytes
    anchor: AnchorResult

    def payload(self) -> dict[str, Any]:
        return json.loads(self.canonical_bytes.decode("utf-8"))


class LocalAuditRecorder:
    """Process-local append-only audit log with hash linkage.

    Genesis uses ``previous_receipt_hash = null``. This is a local hash chain,
    not a blockchain; process restart discards all receipts.
    """

    def __init__(
        self,
        anchor: AuditAnchor | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._anchor = anchor or NullAnchor()
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        self._entries: list[_StoredReceipt] = []
        self._by_id: dict[str, _StoredReceipt] = {}
        self._lock = asyncio.Lock()

    def new_nonce(self) -> str:
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or len(nonce) < 32:
            raise ValueError("audit nonce must be at least 128 bits encoded as text")
        return nonce

    async def record(self, commitment: AuditCommitment) -> AuditReceipt:
        async with self._lock:
            previous = self._entries[-1].receipt_hash if self._entries else None
            payload = commitment.receipt_payload(previous)
            encoded = canonical_json_bytes(payload)
            digest = receipt_hash(payload)
            if not verify_receipt_payload(payload, digest)["valid"]:
                raise ValueError("locally constructed audit receipt did not verify")
            receipt_id = f"avdr-audit-v1-{digest.removeprefix('sha256:')}"
            anchor_metadata = future_anchor_payload(digest, payload)
            try:
                anchor = await self._anchor.anchor(digest, anchor_metadata)
                status = (
                    "recorded"
                    if anchor.status == "not_configured"
                    else "recorded_and_anchor_processed"
                )
            except Exception:  # local record remains valid when anchoring fails
                anchor = AnchorResult(status="anchoring_failed")
                status = "recorded_anchor_failed"
            entry = _StoredReceipt(receipt_id, digest, encoded, anchor)
            self._entries.append(entry)
            self._by_id[receipt_id] = entry
            return AuditReceipt(
                recorded=True,
                status=status,
                reference=receipt_id,
                receipt_id=receipt_id,
                receipt_hash=digest,
                schema_version=AUDIT_SCHEMA_VERSION,
                verification="local",
                integrity_verified=True,
                anchor=anchor,
            )

    async def get(self, receipt_id: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._by_id.get(receipt_id)
            if entry is None:
                return None
            verification = self._verify_chain_to(entry)
            return {
                "receipt_id": entry.receipt_id,
                "receipt_hash": entry.receipt_hash,
                "receipt": entry.payload(),
                "verification": "local",
                "integrity_verified": verification["valid"],
                "anchor": entry.anchor.to_dict(),
            }

    async def verify(
        self,
        receipt_id: str,
        *,
        disclosed_did: str | None = None,
        disclosed_result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._by_id.get(receipt_id)
            if entry is None:
                return None
            result = self._verify_chain_to(entry)
            disclosure = verify_receipt_payload(
                entry.payload(),
                entry.receipt_hash,
                disclosed_did=disclosed_did,
                disclosed_result=disclosed_result,
            )
            result.update(
                {
                    "did_commitment_valid": disclosure["did_commitment_valid"],
                    "result_commitment_valid": disclosure["result_commitment_valid"],
                    "errors": disclosure["errors"],
                }
            )
            result["valid"] = bool(result["valid"] and disclosure["valid"])
            return result

    def _verify_chain_to(self, target: _StoredReceipt) -> dict[str, Any]:
        previous = None
        for entry in self._entries:
            payload = entry.payload()
            hash_valid = receipt_hash(payload) == entry.receipt_hash
            link_valid = payload.get("previous_receipt_hash") == previous
            if not hash_valid or not link_valid:
                return {
                    "valid": False,
                    "receipt_hash_valid": hash_valid,
                    "chain_valid": False,
                    "did_commitment_valid": None,
                    "result_commitment_valid": None,
                    "errors": ["local_chain_mismatch"],
                }
            previous = entry.receipt_hash
            if entry.receipt_id == target.receipt_id:
                return {
                    "valid": True,
                    "receipt_hash_valid": True,
                    "chain_valid": True,
                    "did_commitment_valid": None,
                    "result_commitment_valid": None,
                    "errors": [],
                }
        return {
            "valid": False,
            "receipt_hash_valid": False,
            "chain_valid": False,
            "did_commitment_valid": None,
            "result_commitment_valid": None,
            "errors": ["unknown_receipt"],
        }

    def describe(self) -> dict[str, Any]:
        return {
            "recorder": "local_append_only_audit_chain",
            "storage": "process_memory",
            "durable": False,
            "hash_chain_enabled": True,
            "genesis_previous_receipt_hash": None,
            "receipt_count": len(self._entries),
            "anchor": type(self._anchor).__name__,
        }


def build_commitment(
    *,
    request_id: str,
    did: str,
    strategy: str | None = None,
    selected_resolver_ids: list[str] | tuple[str, ...] | None = None,
    launch_order: list[str] | tuple[str, ...] | None = None,
    candidate_count: int = 0,
    policy_identity: dict[str, Any] | None = None,
    estimator_identity: dict[str, Any] | None = None,
    estimator_config_hash: str | None = None,
    acceptance_profile: str = "w3c-basic-v1",
    returned_provider: str | None = None,
    normalized_result: dict[str, Any] | None = None,
    calls_used: int = 0,
    evidence_mode: str = "real",
    timestamp: str,
    selection_mode: str | None = None,
    target_success: float | None = None,
    estimated_success: float | None = None,
    request_nonce: str | None = None,
    nonce_factory: Callable[[], str] | None = None,
    # P6 keyword aliases remain accepted for external custom recorders.
    selected_providers: list[str] | None = None,
    policy: str | None = None,
    policy_metadata: dict[str, Any] | None = None,
    result_hash: str | None = None,
) -> AuditCommitment:
    """Build a privacy-minimized, post-execution audit commitment."""
    strategy = strategy or policy
    if strategy is None:
        raise ValueError("strategy is required")
    launch = tuple(launch_order or selected_resolver_ids or selected_providers or ())
    selected = tuple(sorted(set(selected_resolver_ids or selected_providers or launch)))
    if len(launch) != len(set(launch)):
        raise ValueError("launch_order must not contain duplicate resolver ids")
    if set(launch) != set(selected):
        raise ValueError("launch_order and selected_resolver_ids must identify the same set")
    nonce = request_nonce or (nonce_factory or (lambda: secrets.token_hex(16)))()
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise ValueError("audit nonce must be at least 128 bits encoded as text")
    if policy_identity is None:
        policy_identity = _identity(
            POLICY_DOMAIN,
            {"name": strategy, "metadata": policy_metadata or {}},
        )
    result_value = (
        result_commitment(normalized_result)
        if normalized_result is not None
        else result_hash
    )
    if result_value is None:
        result_value = result_commitment(
            {"accepted": False, "returned_by": returned_provider}
        )
    return AuditCommitment(
        request_id=request_id,
        timestamp=timestamp,
        evidence_mode=evidence_mode,
        request_nonce=nonce,
        requested_did_commitment=did_commitment(did, nonce),
        strategy=strategy,
        selected_resolver_ids=selected,
        launch_order=launch,
        candidate_count=candidate_count,
        selected_count=len(selected),
        policy_identity=copy.deepcopy(policy_identity),
        estimator_identity=copy.deepcopy(estimator_identity),
        estimator_config_hash=estimator_config_hash,
        acceptance_profile=acceptance_profile,
        returned_provider=returned_provider,
        result_commitment=result_value,
        calls_used=calls_used,
        selection_mode=selection_mode,
        target_success=target_success,
        estimated_success=estimated_success,
    )


def verify_receipt_payload(
    receipt_payload: dict[str, Any],
    expected_hash: str,
    *,
    disclosed_did: str | None = None,
    disclosed_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute receipt and optional disclosure commitments locally."""
    errors: list[str] = []
    try:
        hash_valid = receipt_hash(receipt_payload) == expected_hash
    except (TypeError, ValueError, OverflowError):
        hash_valid = False
    if not hash_valid:
        errors.append("receipt_hash_mismatch")

    did_valid = None
    if disclosed_did is not None:
        try:
            did_valid = did_commitment(
                disclosed_did, receipt_payload["request_nonce"]
            ) == receipt_payload["requested_did_commitment"]
        except (KeyError, TypeError, ValueError):
            did_valid = False
        if not did_valid:
            errors.append("did_commitment_mismatch")

    result_valid = None
    if disclosed_result is not None:
        try:
            result_valid = (
                result_commitment(disclosed_result)
                == receipt_payload["result_commitment"]
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            result_valid = False
        if not result_valid:
            errors.append("result_commitment_mismatch")

    return {
        "valid": hash_valid and did_valid is not False and result_valid is not False,
        "receipt_hash_valid": hash_valid,
        "chain_valid": None,
        "did_commitment_valid": did_valid,
        "result_commitment_valid": result_valid,
        "errors": errors,
    }


def future_anchor_payload(
    receipt_hash_value: str, receipt_payload: dict[str, Any]
) -> dict[str, Any]:
    """Return the complete minimal payload permitted at a future anchor."""
    return {
        "receipt_hash": receipt_hash_value,
        "schema_version": receipt_payload["schema_version"],
        "timestamp": receipt_payload["timestamp"],
        "policy_identity_hash": receipt_payload["policy_identity"]["identity_hash"],
    }
