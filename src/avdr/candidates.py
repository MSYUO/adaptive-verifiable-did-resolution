"""Capability-aware candidate provider selection.

A provider is never selected merely because it exists in the inventory. Every
provider is evaluated against the request and either becomes a candidate or is
skipped with an explicit, typed reason.

The distinction that matters: a SKIPPED provider is not a FAILED provider. It
was never called. Exclusion reasons and runtime failures live in separate
telemetry fields and must never be pooled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .budget import RATE_BUDGET_EXHAUSTED, BudgetRegistry
from .inventory import ProviderEntry, ProviderInventory

# Typed exclusion reasons.
PROVIDER_UNAVAILABLE = "provider_unavailable"
METHOD_NOT_SUPPORTED = "method_not_supported"
AUTH_REQUIRED_NO_CREDENTIALS = "auth_required_no_credentials"
UNKNOWN_ADAPTER = "unknown_adapter"

# Typed failure condition for the caller.
INSUFFICIENT_QUALIFIED_PROVIDERS = "INSUFFICIENT_QUALIFIED_PROVIDERS"


@dataclass
class SkippedProvider:
    provider_id: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class CandidateSet:
    did_method: str
    candidates: list[ProviderEntry] = field(default_factory=list)
    skipped: list[SkippedProvider] = field(default_factory=list)

    @property
    def qualified_provider_count(self) -> int:
        return len(self.candidates)

    @property
    def candidate_ids(self) -> list[str]:
        return [p.id for p in self.candidates]

    def skipped_dicts(self) -> list[dict]:
        return [s.to_dict() for s in self.skipped]


class InsufficientQualifiedProviders(Exception):
    """Raised when a policy cannot run with the candidates available.

    Carries the full selection record so the caller can explain exactly which
    providers were excluded and why -- never just "no providers".
    """

    def __init__(
        self, candidate_set: CandidateSet, required: int, policy: str
    ) -> None:
        super().__init__(
            f"policy {policy!r} requires at least {required} qualified "
            f"provider(s) for did:{candidate_set.did_method}, found "
            f"{candidate_set.qualified_provider_count}"
        )
        self.condition = INSUFFICIENT_QUALIFIED_PROVIDERS
        self.candidate_set = candidate_set
        self.required = required
        self.policy = policy


def select_candidates(
    inventory: ProviderInventory,
    did_method: str,
    budgets: BudgetRegistry | None = None,
    known_adapters: set[str] | None = None,
) -> CandidateSet:
    """Filter the inventory down to providers eligible for this request."""
    result = CandidateSet(did_method=did_method)

    for provider in inventory.providers:
        reason = _ineligibility_reason(provider, did_method, budgets, known_adapters)
        if reason is None:
            result.candidates.append(provider)
        else:
            code, detail = reason
            result.skipped.append(SkippedProvider(provider.id, code, detail))

    return result


def _ineligibility_reason(
    provider: ProviderEntry,
    did_method: str,
    budgets: BudgetRegistry | None,
    known_adapters: set[str] | None,
) -> tuple[str, str | None] | None:
    """Return (reason_code, detail) if the provider cannot be used, else None.

    Order matters: the most fundamental disqualification is reported, so a
    provider that is both unavailable and out of budget reads as unavailable.
    """
    if not provider.available:
        return PROVIDER_UNAVAILABLE, provider.unavailable_reason

    if not provider.supports(did_method):
        return (
            METHOD_NOT_SUPPORTED,
            f"supports {sorted(provider.supported_did_methods)}, not {did_method!r}",
        )

    if provider.auth_required and not provider.credentials_available:
        return (
            AUTH_REQUIRED_NO_CREDENTIALS,
            "provider requires authentication and no credentials are configured",
        )

    if known_adapters is not None and provider.adapter not in known_adapters:
        return UNKNOWN_ADAPTER, f"no adapter named {provider.adapter!r}"

    if budgets is not None:
        state = budgets.get(provider.id).state()
        if state.exhausted:
            return (
                RATE_BUDGET_EXHAUSTED,
                f"{state.spent_in_window}/{state.limit} requests used within "
                f"the last {state.window_seconds}s; not called",
            )

    return None
