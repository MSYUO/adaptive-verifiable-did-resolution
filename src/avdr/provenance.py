"""Experiment provenance: bind a dataset to the exact run configuration.

Every measurement record carries enough provenance to answer "which code,
which config, which injected conditions produced this row?".

Two rules are load-bearing here:

  * Nothing is fabricated. If a git commit or a hash cannot be resolved, the
    field is null and the reason is recorded in ``unresolved`` so the gap is
    visible in the dataset rather than silently filled with a plausible value.
  * Hashing uses a deterministic canonical serialisation, so the same logical
    config always produces the same hash regardless of key insertion order.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

HASH_PREFIX = "sha256:"


def canonical_json(payload: Any) -> str:
    """Deterministic serialisation used as hash input.

    Dict keys are sorted recursively (``sort_keys`` applies at every level),
    whitespace is stripped, and non-ASCII characters are preserved rather than
    escaped so the encoding does not depend on the ``ensure_ascii`` default.
    List order is preserved: it is meaningful (resolver order matters).
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def config_hash(payload: Any) -> str:
    """SHA-256 over the canonical serialisation, prefixed with the algorithm."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def resolve_git_commit(repo_root: Path) -> tuple[str | None, str | None]:
    """Return (commit_sha, reason_if_unresolved).

    Never invents a SHA. In a container built without the .git directory this
    correctly returns (None, reason).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "git executable not found on PATH"
    except subprocess.SubprocessError as exc:
        return None, f"git invocation failed: {type(exc).__name__}"

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[0] if detail else f"git exited {result.returncode}"
        return None, f"git rev-parse failed: {reason}"

    sha = result.stdout.strip()
    if not sha:
        return None, "git rev-parse returned empty output"
    return sha, None


def resolve_git_dirty(repo_root: Path) -> tuple[bool | None, str | None]:
    """Return (working_tree_is_dirty, reason_if_unresolved).

    A dirty tree means the recorded commit does not fully describe the code
    that produced the data, which a later reader must be able to see.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "git executable not found on PATH"
    except subprocess.SubprocessError as exc:
        return None, f"git invocation failed: {type(exc).__name__}"

    if result.returncode != 0:
        return None, f"git status failed with exit {result.returncode}"
    return bool(result.stdout.strip()), None


class Provenance(BaseModel):
    """Run-identifying fields stamped onto every measurement record."""

    experiment_id: str
    trial_id: str
    scenario_id: str
    phase: str
    seed: int | None = None

    git_commit: str | None = None
    git_dirty: bool | None = None
    config_hash: str | None = None
    injection_config_hash: str | None = None

    # Real-provider qualification provenance.
    provider_inventory_hash: str | None = None
    fixture_manifest_hash: str | None = None
    acceptance_profile: str | None = None
    dependency_lock_hash: str | None = None

    # field name -> why it could not be resolved. Empty when all resolved.
    unresolved: dict[str, str] = Field(default_factory=dict)

    def for_trial(self, trial_id: str) -> "Provenance":
        """Copy carrying a new trial_id; all other provenance is shared."""
        return self.model_copy(update={"trial_id": trial_id})

    def binding_key(self) -> tuple:
        """The tuple every record of one trial must agree on."""
        return (
            self.experiment_id,
            self.trial_id,
            self.scenario_id,
            self.phase,
            self.git_commit,
            self.config_hash,
            self.injection_config_hash,
            self.provider_inventory_hash,
            self.fixture_manifest_hash,
        )


def new_experiment_id(prefix: str = "exp") -> str:
    """Time-ordered, collision-resistant experiment identifier."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def new_trial_id() -> str:
    return str(uuid.uuid4())


def build_provenance(
    *,
    experiment_id: str,
    scenario_id: str,
    phase: str,
    repo_root: Path,
    router_config_payload: Any,
    injection_config_payload: Any,
    seed: int | None = None,
    trial_id: str = "",
) -> Provenance:
    """Assemble provenance, recording the reason for anything unresolved."""
    unresolved: dict[str, str] = {}

    commit, commit_reason = resolve_git_commit(repo_root)
    if commit is None:
        unresolved["git_commit"] = commit_reason or "unknown"

    dirty, dirty_reason = resolve_git_dirty(repo_root)
    if dirty is None:
        unresolved["git_dirty"] = dirty_reason or "unknown"

    if router_config_payload is None:
        hashed_config = None
        unresolved["config_hash"] = "router config payload was not supplied"
    else:
        hashed_config = config_hash(router_config_payload)

    if injection_config_payload is None:
        hashed_injection = None
        unresolved["injection_config_hash"] = (
            "injection config payload was not supplied"
        )
    else:
        hashed_injection = config_hash(injection_config_payload)

    return Provenance(
        experiment_id=experiment_id,
        trial_id=trial_id,
        scenario_id=scenario_id,
        phase=phase,
        seed=seed,
        git_commit=commit,
        git_dirty=dirty,
        config_hash=hashed_config,
        injection_config_hash=hashed_injection,
        unresolved=unresolved,
    )
