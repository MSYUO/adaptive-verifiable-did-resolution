"""Routing policy abstraction.

A policy answers one question only: "which resolvers, in what order, and how
many of them may be attempted?" It performs no I/O. Execution and HTTP
transport live in ``executor.py`` / ``transport.py``.

This milestone contains baseline policies only. There is no prediction, no
adaptive fan-out sizing and no parallel racing here by design.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..config import RouterConfig


@dataclass
class RoutingPlan:
    """A policy's decision for one logical request."""

    policy: str
    # Ordered resolver ids the policy is willing to contact.
    candidate_sequence: list[str]
    # Upper bound on attempts. 1 means "no failover".
    max_attempts: int
    target: str | None = None
    metadata: dict = field(default_factory=dict)

    def attempt_order(self) -> list[str]:
        return self.candidate_sequence[: self.max_attempts]


class RoutingPolicy(ABC):
    name: str = "abstract"

    def __init__(self, config: RouterConfig) -> None:
        self.config = config

    @abstractmethod
    def plan(self, did: str) -> RoutingPlan:
        """Choose the resolver order for one logical request."""

    def reset(self) -> None:
        """Reset any internal state. Used by tests for determinism."""


class SingleStaticPolicy(RoutingPolicy):
    """Always route to one configured resolver. Control condition.

    No failover: if the target fails, the logical request fails. That is the
    point of the baseline.
    """

    name = "single-static"

    def __init__(self, config: RouterConfig, target: str | None = None) -> None:
        super().__init__(config)
        self.target = target or config.single_static_target
        if self.target not in config.resolver_ids():
            raise KeyError(f"unknown resolver_id: {self.target!r}")

    def plan(self, did: str) -> RoutingPlan:
        return RoutingPlan(
            policy=self.name,
            candidate_sequence=[self.target],
            max_attempts=1,
            target=self.target,
        )


class RoundRobinPolicy(RoutingPolicy):
    """Rotate one resolver per LOGICAL request: a, b, c, a, b, c, ...

    Rotation advances once per logical request, not once per attempt, and
    there is no failover (max_attempts=1) so the sequence stays deterministic
    regardless of resolver health.

    The counter is process-local, so the router must run as a single worker
    for the sequence to be deterministic. See README.
    """

    name = "round-robin"

    def __init__(self, config: RouterConfig) -> None:
        super().__init__(config)
        self._lock = threading.Lock()
        self._index = 0

    def plan(self, did: str) -> RoutingPlan:
        ids = self.config.resolver_ids()
        with self._lock:
            index = self._index
            self._index = (self._index + 1) % len(ids)
        chosen = ids[index]
        return RoutingPlan(
            policy=self.name,
            candidate_sequence=[chosen],
            max_attempts=1,
            target=chosen,
            metadata={"rotation_index": index},
        )

    def reset(self) -> None:
        with self._lock:
            self._index = 0


class SequentialFailoverPolicy(RoutingPolicy):
    """Try resolvers in configured order until one is accepted.

    Strictly sequential: the executor issues attempt i+1 only after attempt i
    has terminated. No requests are issued in parallel.
    """

    name = "sequential-failover"

    def plan(self, did: str) -> RoutingPlan:
        ids = self.config.resolver_ids()
        return RoutingPlan(
            policy=self.name,
            candidate_sequence=list(ids),
            max_attempts=len(ids),
            target=ids[0],
        )


POLICY_TYPES: dict[str, type[RoutingPolicy]] = {
    SingleStaticPolicy.name: SingleStaticPolicy,
    RoundRobinPolicy.name: RoundRobinPolicy,
    SequentialFailoverPolicy.name: SequentialFailoverPolicy,
}


def build_policies(config: RouterConfig) -> dict[str, RoutingPolicy]:
    """Instantiate one long-lived instance of each policy.

    Stateful policies (round-robin) must be singletons so their state spans
    logical requests.
    """
    return {name: cls(config) for name, cls in POLICY_TYPES.items()}
