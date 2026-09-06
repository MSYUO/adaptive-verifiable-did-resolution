"""Real-provider baseline routing policies.

Mirrors the abstraction used by the mock router (`avdr.router.policies`): a
policy performs NO I/O and only answers "which providers, in what order, how
many, and executed how?". Execution lives in `executor.py`.

These are BASELINES. None of them is the proposed adaptive algorithm: there is
no prediction, no adaptive fan-out sizing and no cost model here.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..inventory import ProviderEntry

SEQUENTIAL = "sequential"
CONCURRENT = "concurrent"


@dataclass
class RealRoutingPlan:
    policy: str
    provider_order: list[str]
    execution: str
    max_attempts: int
    min_providers: int
    target: str | None = None
    metadata: dict = field(default_factory=dict)

    def attempt_order(self) -> list[str]:
        return self.provider_order[: self.max_attempts]


class RealRoutingPolicy(ABC):
    name: str = "abstract"
    execution: str = SEQUENTIAL
    # Minimum qualified providers required for the policy to be meaningful.
    min_providers: int = 1

    @abstractmethod
    def plan(self, candidates: list[ProviderEntry], did: str) -> RealRoutingPlan:
        ...

    def reset(self) -> None:
        """Reset internal state, if any."""


class RealSingleStatic(RealRoutingPolicy):
    """Always one explicitly selected provider. No hidden failover.

    Control baseline: if the chosen provider fails, the request fails. A
    baseline that silently repairs itself is not a baseline.
    """

    name = "single-static"
    execution = SEQUENTIAL
    min_providers = 1

    def __init__(self, target: str | None = None) -> None:
        self.target = target

    def plan(self, candidates: list[ProviderEntry], did: str) -> RealRoutingPlan:
        chosen = None
        if self.target:
            chosen = next((p for p in candidates if p.id == self.target), None)
            if chosen is None:
                raise KeyError(
                    f"single-static target {self.target!r} is not a qualified "
                    f"candidate; qualified: {[p.id for p in candidates]}"
                )
        else:
            chosen = candidates[0]
        return RealRoutingPlan(
            policy=self.name,
            provider_order=[chosen.id],
            execution=SEQUENTIAL,
            max_attempts=1,
            min_providers=self.min_providers,
            target=chosen.id,
        )


class RealSequentialFailover(RealRoutingPolicy):
    """Try providers in order until one is structurally acceptable.

    Strictly sequential: provider i+1 is contacted only after provider i has
    terminated, and no provider is contacted after a success.
    """

    name = "sequential-failover"
    execution = SEQUENTIAL
    min_providers = 1

    def plan(self, candidates: list[ProviderEntry], did: str) -> RealRoutingPlan:
        order = [p.id for p in candidates]
        return RealRoutingPlan(
            policy=self.name,
            provider_order=order,
            execution=SEQUENTIAL,
            max_attempts=len(order),
            min_providers=self.min_providers,
            target=order[0] if order else None,
        )


class RealAllRace(RealRoutingPolicy):
    """Contact every qualified candidate concurrently. BASELINE ONLY.

    This is the maximum-fan-out control the project exists to improve on, not
    the proposed algorithm. The winner is the first STRUCTURALLY ACCEPTABLE
    completion: a faster response that fails acceptance does not win.

    Requires at least two providers -- racing a single provider is not a race,
    and pretending otherwise would fabricate redundancy.
    """

    name = "all-race"
    execution = CONCURRENT
    min_providers = 2

    def __init__(self, launch_order_seed: int | None = None) -> None:
        self.launch_order_seed = launch_order_seed
        self._lock = threading.Lock()
        self._counter = 0

    def plan(self, candidates: list[ProviderEntry], did: str) -> RealRoutingPlan:
        order = [p.id for p in candidates]
        metadata: dict = {}
        if self.launch_order_seed is not None:
            import random

            with self._lock:
                index = self._counter
                self._counter += 1
            shuffled = list(order)
            random.Random(f"{self.launch_order_seed}:{index}").shuffle(shuffled)
            order = shuffled
            metadata = {
                "launch_order_seed": self.launch_order_seed,
                "rotation_index": index,
            }
        return RealRoutingPlan(
            policy=self.name,
            provider_order=order,
            execution=CONCURRENT,
            max_attempts=len(order),
            min_providers=self.min_providers,
            target=None,
            metadata=metadata,
        )

    def reset(self) -> None:
        with self._lock:
            self._counter = 0


POLICY_TYPES: dict[str, type[RealRoutingPolicy]] = {
    RealSingleStatic.name: RealSingleStatic,
    RealSequentialFailover.name: RealSequentialFailover,
    RealAllRace.name: RealAllRace,
}


def build_policies(
    single_static_target: str | None = None,
    launch_order_seed: int | None = None,
) -> dict[str, RealRoutingPolicy]:
    return {
        RealSingleStatic.name: RealSingleStatic(single_static_target),
        RealSequentialFailover.name: RealSequentialFailover(),
        RealAllRace.name: RealAllRace(launch_order_seed),
    }
