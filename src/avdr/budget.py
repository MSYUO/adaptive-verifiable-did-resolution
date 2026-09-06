"""Per-provider request budgets.

A public resolver endpoint disclosed a limit of 10 requests per 1800 seconds.
Budgets are therefore part of ROUTING eligibility, not an afterthought: a
provider whose budget would be exceeded is skipped before it is called, rather
than discovering the limit by being refused.

A skipped provider is NOT a failed provider. Exhaustion is a policy exclusion
with its own reason code and is never mixed into failure telemetry.

The window is rolling: each charge records a timestamp, and only charges
inside the window count. Local providers (loopback) are unlimited, because no
third party's budget is being spent.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .inventory import ProviderEntry, ProviderInventory

# Reason code surfaced when a provider is skipped for budget reasons.
RATE_BUDGET_EXHAUSTED = "rate_budget_exhausted"


@dataclass
class BudgetState:
    provider_id: str
    limit: int | None
    window_seconds: int | None
    spent_in_window: int
    remaining: int | None
    exhausted: bool

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "limit": self.limit,
            "window_seconds": self.window_seconds,
            "spent_in_window": self.spent_in_window,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
        }


class ProviderBudget:
    """Rolling-window request counter for a single provider."""

    def __init__(self, provider_id: str, limit: int | None, window_seconds: int | None):
        self.provider_id = provider_id
        self.limit = limit
        self.window_seconds = window_seconds
        self._charges: deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def unlimited(self) -> bool:
        return self.limit is None or self.window_seconds is None

    def _prune(self, now: float) -> None:
        if self.window_seconds is None:
            return
        cutoff = now - self.window_seconds
        while self._charges and self._charges[0] < cutoff:
            self._charges.popleft()

    def state(self, now: float | None = None) -> BudgetState:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            spent = len(self._charges)
        if self.unlimited:
            return BudgetState(self.provider_id, None, None, spent, None, False)
        remaining = max(0, self.limit - spent)
        return BudgetState(
            provider_id=self.provider_id,
            limit=self.limit,
            window_seconds=self.window_seconds,
            spent_in_window=spent,
            remaining=remaining,
            exhausted=remaining <= 0,
        )

    def would_exceed(self, now: float | None = None) -> bool:
        return self.state(now).exhausted

    def charge(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            self._charges.append(now)

    def reset(self) -> None:
        with self._lock:
            self._charges.clear()


class BudgetRegistry:
    """Budgets for every provider in an inventory."""

    def __init__(self, inventory: ProviderInventory) -> None:
        self.budgets: dict[str, ProviderBudget] = {}
        for provider in inventory.providers:
            self.budgets[provider.id] = self._build(provider)

    @staticmethod
    def _build(provider: ProviderEntry) -> ProviderBudget:
        if not provider.is_external:
            # Loopback: no third-party budget is consumed.
            return ProviderBudget(provider.id, None, None)
        return ProviderBudget(
            provider.id,
            provider.rate_limit_requests,
            provider.rate_limit_window_seconds,
        )

    def get(self, provider_id: str) -> ProviderBudget:
        if provider_id not in self.budgets:
            # Unknown providers are treated as unlimited rather than blocked;
            # eligibility filtering already rejects unknown providers.
            self.budgets[provider_id] = ProviderBudget(provider_id, None, None)
        return self.budgets[provider_id]

    def would_exceed(self, provider_id: str) -> bool:
        return self.get(provider_id).would_exceed()

    def charge(self, provider_id: str) -> None:
        self.get(provider_id).charge()

    def snapshot(self) -> dict[str, dict]:
        return {pid: b.state().to_dict() for pid, b in self.budgets.items()}

    def reset(self) -> None:
        for budget in self.budgets.values():
            budget.reset()
