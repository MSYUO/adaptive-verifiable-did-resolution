"""Audit/provenance commitment boundary for the service layer.

No blockchain client is implemented in this milestone.  The interface keeps
future recorders away from raw request telemetry: they receive hashes and the
selected provider identifiers only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AuditCommitment:
    request_id: str
    did_hash: str
    selected_providers: tuple[str, ...]
    policy: str
    policy_version_hash: str
    result_hash: str | None
    timestamp: str


@dataclass(frozen=True)
class AuditReceipt:
    recorded: bool
    status: str
    reference: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditRecorder(Protocol):
    async def record(self, commitment: AuditCommitment) -> AuditReceipt:
        """Record a privacy-minimized commitment and return its receipt."""


class NullAuditRecorder:
    """Default recorder: explicitly reports that no audit sink is configured."""

    async def record(self, commitment: AuditCommitment) -> AuditReceipt:
        return AuditReceipt(recorded=False, status="not_configured")


def build_commitment(
    *,
    request_id: str,
    did: str,
    selected_providers: list[str],
    policy: str,
    policy_metadata: dict[str, Any],
    result_hash: str | None,
    timestamp: str,
) -> AuditCommitment:
    """Build the narrow event a future audit implementation may persist."""
    return AuditCommitment(
        request_id=request_id,
        did_hash=_sha256_text(did),
        selected_providers=tuple(selected_providers),
        policy=policy,
        policy_version_hash=_sha256_json(
            {"policy": policy, "metadata": policy_metadata}
        ),
        result_hash=result_hash,
        timestamp=timestamp,
    )


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
