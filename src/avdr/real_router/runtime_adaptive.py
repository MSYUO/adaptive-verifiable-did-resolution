"""Frozen adaptive estimator integration for the serving runtime.

This module is a composition boundary, not a new estimator or policy.  It
loads the frozen V3 rolling empirical estimator, supplies it with a coherent
snapshot of deployment-observed subset history, and updates that history only
from provider outcomes the real executor actually observed.

History is intentionally process-local for the hackathon MVP.  A new app
instance or process restart starts with empty runtime history.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..adaptive.estimator import SubsetEstimator, SubsetKey, subset_key
from ..adaptive.optimizer import (
    SELECTED,
    SLO_ESTIMATE_UNSATISFIABLE,
    MinimumSetOptimizer,
)
from ..candidates import CandidateSet
from ..config import REPO_ROOT
from ..learning.closedloop import update_subset_history
from ..learning.estimators import (
    HISTORY_KEY,
    RollingEmpiricalEstimator,
)
from ..models import RealRoutingAttempt
from .policies import RealRoutingPlan

DEFAULT_SPEC_PATH = REPO_ROOT / "service_assets" / "frozen_estimator_v3.spec.json"
LEGACY_ESTIMATOR_PATH = REPO_ROOT / "frozen" / "frozen_estimator_v3.pkl"
EXPECTED_PACKAGED_SPEC_SHA256 = (
    "sha256:f91a2786f93cfe7b2608f86f4215f1273c59b5f395ef98d19a0cc2b51eca776b"
)
EXPECTED_SOURCE_PICKLE_SHA256 = (
    "sha256:532cc2a5269bb39a8b9e6d1656fbdf08967b4115a6fb22784a5e6d07c0f60545"
)
PROCESS_MEMORY = "process_memory"
REAL = "real"
CONTROLLED_DEMO = "controlled_demo"
EVIDENCE_MODES = frozenset({REAL, CONTROLLED_DEMO})


class PackagedEstimatorParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window: int = Field(gt=0, strict=True)
    prior: float = Field(ge=0.0, le=1.0, strict=True)
    prior_weight: float = Field(gt=0.0, strict=True)


class PackagedEstimatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    class_name: Literal[
        "avdr.learning.estimators.RollingEmpiricalEstimator"
    ]
    estimator_id: Literal["b1-rolling-empirical"]
    estimator_version: Literal["v1"]
    parameters: PackagedEstimatorParameters
    estimator_config_hash: str


class PackagedServingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    class_name: Literal[
        "avdr.real_router.adaptive_policy.RealAdaptiveMinSet"
    ]
    optimizer_class: Literal["avdr.adaptive.optimizer.MinimumSetOptimizer"]
    target_slo_probability: float = Field(gt=0.0, le=1.0, strict=True)
    deadline_tau_ms: float = Field(gt=0.0, strict=True)


class PackagedSourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_pickle_sha256: str
    frozen_estimator_policy_commit: str
    ack_evidence_commit: str


class PackagedAdaptiveSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["avdr-serving-estimator-spec-v1"]
    artifact_name: Literal["frozen-estimator-v3"]
    packaging_method: Literal["typed_spec"]
    estimator: PackagedEstimatorIdentity
    serving_policy: PackagedServingPolicy
    source_provenance: PackagedSourceProvenance


@dataclass(frozen=True)
class RuntimeAttemptObservation:
    provider_id: str
    dispatched: bool
    observed: bool
    accepted: bool
    transport_outcome: str
    latency_ms: float | None
    completion_offset_ms: float | None
    within_deadline: bool | None
    normalized_document_hash: str | None


@dataclass(frozen=True)
class RuntimeHistoryRecord:
    request_id: str
    evidence_mode: str
    strategy: str
    candidate_providers: tuple[str, ...]
    selected_providers: tuple[str, ...]
    dispatched_providers: tuple[str, ...]
    observed_providers: tuple[str, ...]
    attempts: tuple[RuntimeAttemptObservation, ...]
    subset_updates: tuple[tuple[SubsetKey, int], ...]
    decision_history_version: int | None
    committed_history_version: int


@dataclass(frozen=True)
class RuntimeHistorySnapshot:
    version: int
    observed_request_count: int
    subset_history: dict[SubsetKey, tuple[int, ...]]

    def estimator_context(self) -> dict[str, Any]:
        """Return an isolated mutable shape expected by the frozen estimator."""
        return {
            HISTORY_KEY: {
                key: list(outcomes) for key, outcomes in self.subset_history.items()
            }
        }


class RuntimeObservedHistory:
    """Thread-safe, process-local history derived from executor attempts.

    Snapshot and commit are short critical sections.  Provider network I/O is
    never performed while this lock is held, and overlapping requests may
    legitimately plan against the same immutable snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._version = 0
        self._subset_history: dict[SubsetKey, list[int]] = {}
        self._records: list[RuntimeHistoryRecord] = []

    def snapshot(self) -> RuntimeHistorySnapshot:
        with self._lock:
            return RuntimeHistorySnapshot(
                version=self._version,
                observed_request_count=len(self._records),
                subset_history={
                    key: tuple(values) for key, values in self._subset_history.items()
                },
            )

    def records(self) -> list[RuntimeHistoryRecord]:
        with self._lock:
            return list(self._records)

    def reset(self) -> None:
        """Explicit test/local reset; a process restart has the same effect."""
        with self._lock:
            self._version = 0
            self._subset_history.clear()
            self._records.clear()

    def describe(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {
            "storage": PROCESS_MEMORY,
            "durable": False,
            "reset_semantics": "empty on application start; reset on process restart",
            "history_version": snapshot.version,
            "observed_request_count": snapshot.observed_request_count,
            "known_subset_count": len(snapshot.subset_history),
            "known_outcome_count": sum(
                len(values) for values in snapshot.subset_history.values()
            ),
        }

    def update_from_execution(
        self,
        *,
        request_id: str,
        plan: RealRoutingPlan,
        candidate_set: CandidateSet,
        attempts: list[RealRoutingAttempt],
        deadline_tau_ms: float,
        decision_history_version: int | None = None,
        evidence_mode: str = REAL,
    ) -> RuntimeHistoryRecord:
        """Commit only outcomes observable from this completed execution."""
        attempt_observations = tuple(
            _observation(attempt, deadline_tau_ms) for attempt in attempts
        )
        observed = {
            row.provider_id for row in attempt_observations if row.observed
        }
        within = {
            row.provider_id: bool(row.within_deadline)
            for row in attempt_observations
            if row.observed
        }

        # Reuse the existing deployment partial-feedback rule.  The temporary
        # mapping contains exactly one new outcome for every subset whose
        # value is knowable from the providers observed in this request.
        update_batch: dict[SubsetKey, list[int]] = {}
        update_subset_history(
            update_batch,
            observed,
            within,
            _nonempty_subsets(candidate_set.candidate_ids),
        )
        flattened_updates = tuple(
            (key, values[0]) for key, values in sorted(update_batch.items())
        )

        with self._lock:
            for key, outcome in flattened_updates:
                self._subset_history.setdefault(key, []).append(outcome)
            self._version += 1
            record = RuntimeHistoryRecord(
                request_id=request_id,
                evidence_mode=evidence_mode,
                strategy=plan.policy,
                candidate_providers=tuple(candidate_set.candidate_ids),
                selected_providers=tuple(plan.attempt_order()),
                dispatched_providers=tuple(
                    row.provider_id for row in attempt_observations if row.dispatched
                ),
                observed_providers=tuple(sorted(observed)),
                attempts=attempt_observations,
                subset_updates=flattened_updates,
                decision_history_version=decision_history_version,
                committed_history_version=self._version,
            )
            self._records.append(record)
            return record


@dataclass
class FrozenAdaptiveRuntime:
    estimator: SubsetEstimator
    target_slo_probability: float
    deadline_tau_ms: float
    artifact_sha256: str
    packaged_spec_sha256: str
    estimator_class: str
    policy_class: str
    optimizer_class: str
    estimator_metadata: dict[str, Any]
    policy_metadata: dict[str, Any]
    # `history` remains the real deployment namespace for compatibility.
    history: RuntimeObservedHistory
    controlled_demo_history: RuntimeObservedHistory

    def history_for(self, evidence_mode: str) -> RuntimeObservedHistory:
        mode = _validate_evidence_mode(evidence_mode)
        return self.history if mode == REAL else self.controlled_demo_history

    def snapshot(self, evidence_mode: str = REAL) -> RuntimeHistorySnapshot:
        return self.history_for(evidence_mode).snapshot()

    def observe(
        self,
        *,
        request_id: str,
        plan: RealRoutingPlan,
        candidate_set: CandidateSet,
        attempts: list[RealRoutingAttempt],
        decision_history_version: int | None = None,
        evidence_mode: str = REAL,
    ) -> RuntimeHistoryRecord:
        return self.history_for(evidence_mode).update_from_execution(
            request_id=request_id,
            plan=plan,
            candidate_set=candidate_set,
            attempts=attempts,
            deadline_tau_ms=self.deadline_tau_ms,
            decision_history_version=decision_history_version,
            evidence_mode=evidence_mode,
        )

    def assess_readiness(
        self,
        *,
        candidate_providers: Iterable[str],
        optimizer: MinimumSetOptimizer,
        evidence_mode: str = REAL,
        target_slo_probability: float | None = None,
        allow_best_effort: bool = False,
    ) -> dict[str, Any]:
        """Ask the existing optimizer whether this history permits a plan."""
        mode = _validate_evidence_mode(evidence_mode)
        snapshot = self.snapshot(mode)
        target = (
            self.target_slo_probability
            if target_slo_probability is None
            else target_slo_probability
        )
        result = optimizer.select(
            candidates=list(candidate_providers),
            estimator=self.estimator,
            target_probability=target,
            context=snapshot.estimator_context(),
            allow_best_effort=allow_best_effort,
        )
        ready = result.status == SELECTED and result.satisfied
        known_outcomes = sum(len(rows) for rows in snapshot.subset_history.values())
        if ready:
            reason = "ready"
        elif (
            result.status == SLO_ESTIMATE_UNSATISFIABLE
            and known_outcomes == 0
        ):
            reason = "insufficient_observed_history"
        elif result.status == SELECTED and result.best_effort:
            reason = "best_effort_does_not_meet_target"
        else:
            reason = result.status.lower()
        return {
            "adaptive_available": True,
            "adaptive_ready": ready,
            "reason": reason,
            "planning_status": result.status,
            "target_slo_probability": target,
            "candidate_providers": result.candidate_providers,
            "selected_subset": result.selected_subset,
            "estimated_subset_success": result.estimated_subset_success,
            "exact": result.exact,
            "evidence_mode": mode,
            "history_version": snapshot.version,
            "logical_requests_observed": snapshot.observed_request_count,
            "known_outcome_count": known_outcomes,
        }

    def describe(self, evidence_mode: str = REAL) -> dict[str, Any]:
        mode = _validate_evidence_mode(evidence_mode)
        return {
            "available": True,
            "adaptive_available": True,
            "packaging_method": "typed_spec",
            "estimator_class": self.estimator_class,
            "estimator_id": self.estimator.estimator_id,
            "estimator_version": self.estimator.estimator_version,
            "estimator_config_hash": self.estimator.config_hash(),
            "source_pickle_sha256": self.artifact_sha256,
            "packaged_spec_sha256": self.packaged_spec_sha256,
            "policy_class": self.policy_class,
            "optimizer_class": self.optimizer_class,
            "target_slo_probability": self.target_slo_probability,
            "deadline_tau_ms": self.deadline_tau_ms,
            "cold_start_behavior": "none_in_serving_policy",
            "cold_start_source": "controlled_experiment_only",
            "empty_history_behavior": "existing typed adaptive planning result",
            "history_mode": "isolated_by_evidence_mode",
            "active_evidence_mode": mode,
            "history": self.history_for(mode).describe(),
            "history_namespaces": {
                REAL: self.history.describe(),
                CONTROLLED_DEMO: self.controlled_demo_history.describe(),
            },
        }


def load_frozen_adaptive_runtime(
    spec_path: str | Path = DEFAULT_SPEC_PATH,
) -> FrozenAdaptiveRuntime:
    """Reconstruct the frozen estimator from the pinned typed service spec."""
    payload = Path(spec_path).read_bytes()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_PACKAGED_SPEC_SHA256:
        raise ValueError(
            "packaged estimator specification hash mismatch: "
            f"expected={EXPECTED_PACKAGED_SPEC_SHA256!r}, actual={digest!r}"
        )
    spec = PackagedAdaptiveSpec.model_validate_json(payload)
    if spec.source_provenance.source_pickle_sha256 != EXPECTED_SOURCE_PICKLE_SHA256:
        raise ValueError("packaged spec does not bind the frozen pickle identity")

    params = spec.estimator.parameters
    estimator = RollingEmpiricalEstimator(
        window=params.window,
        prior=params.prior,
        prior_weight=params.prior_weight,
    )
    if estimator.config_hash() != spec.estimator.estimator_config_hash:
        raise ValueError("reconstructed estimator configuration hash mismatch")
    if estimator.estimator_id != spec.estimator.estimator_id:
        raise ValueError("reconstructed estimator id mismatch")
    if estimator.estimator_version != spec.estimator.estimator_version:
        raise ValueError("reconstructed estimator version mismatch")

    return FrozenAdaptiveRuntime(
        estimator=estimator,
        target_slo_probability=spec.serving_policy.target_slo_probability,
        deadline_tau_ms=spec.serving_policy.deadline_tau_ms,
        artifact_sha256=spec.source_provenance.source_pickle_sha256,
        packaged_spec_sha256=digest,
        estimator_class=spec.estimator.class_name,
        policy_class=spec.serving_policy.class_name,
        optimizer_class=spec.serving_policy.optimizer_class,
        estimator_metadata=spec.estimator.model_dump(mode="json"),
        policy_metadata=spec.serving_policy.model_dump(mode="json"),
        history=RuntimeObservedHistory(),
        controlled_demo_history=RuntimeObservedHistory(),
    )


def try_load_frozen_adaptive_runtime() -> FrozenAdaptiveRuntime | None:
    """Return unavailable only when the packaged typed spec is absent.

    Integrity or compatibility failures remain fatal. The serving path never
    reads or unpickles the legacy developer-local artifact.
    """
    if not DEFAULT_SPEC_PATH.is_file():
        return None
    return load_frozen_adaptive_runtime()


def _observation(
    attempt: RealRoutingAttempt, deadline_tau_ms: float
) -> RuntimeAttemptObservation:
    # Canceled or timing-less attempts reveal no terminal provider result.
    observed = bool(
        attempt.dispatched
        and not attempt.canceled
        and attempt.latency_ms is not None
    )
    completion_offset_ms = (
        attempt.launch_offset_ms + attempt.latency_ms
        if observed
        and attempt.launch_offset_ms is not None
        and attempt.latency_ms is not None
        else None
    )
    within_deadline = (
        bool(attempt.accepted and completion_offset_ms <= deadline_tau_ms)
        if completion_offset_ms is not None
        else None
    )
    return RuntimeAttemptObservation(
        provider_id=attempt.provider_id,
        dispatched=attempt.dispatched,
        observed=observed,
        accepted=attempt.accepted,
        transport_outcome=attempt.transport_outcome,
        latency_ms=attempt.latency_ms,
        completion_offset_ms=completion_offset_ms,
        within_deadline=within_deadline,
        normalized_document_hash=attempt.normalized_document_hash,
    )


def _nonempty_subsets(providers: Iterable[str]) -> list[SubsetKey]:
    ordered = sorted(set(providers))
    return [
        subset_key(combo)
        for size in range(1, len(ordered) + 1)
        for combo in combinations(ordered, size)
    ]


def _validate_evidence_mode(evidence_mode: str) -> str:
    if evidence_mode not in EVIDENCE_MODES:
        raise ValueError(
            f"evidence_mode must be one of {sorted(EVIDENCE_MODES)}, "
            f"got {evidence_mode!r}"
        )
    return evidence_mode
