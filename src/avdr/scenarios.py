"""Controlled fault-injection scenario definitions.

Loads config/scenarios.yaml and applies a scenario to running resolvers
through their own admin endpoints.

CONTROLLED INJECTION. Every value a scenario carries is a test knob we chose.
Applying a scenario does not measure anything about real DID resolvers.

A scenario's YAML entry lists only the resolvers it perturbs. Before hashing,
the definition is expanded to the FULL behaviour of every resolver (defaults
filled in), so ``injection_config_hash`` describes the complete injected state
of the deployment rather than just the diff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field

from .config import REPO_ROOT
from .provenance import config_hash
from .resolver.settings import ResolverBehavior

DEFAULT_SCENARIOS_PATH = REPO_ROOT / "config" / "scenarios.yaml"


class ScenarioDefinition(BaseModel):
    id: str
    name: str
    description: str = ""
    # resolver_id -> partial behaviour overrides
    behaviors: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def resolved_behaviors(self, resolver_ids: list[str]) -> dict[str, dict]:
        """Expand to the complete behaviour of every resolver in the topology.

        Resolvers the scenario does not mention are explicitly healthy, not
        absent -- an omission and an explicit default must hash identically.
        """
        resolved: dict[str, dict] = {}
        for resolver_id in resolver_ids:
            overrides = self.behaviors.get(resolver_id) or {}
            unknown = set(overrides) - set(ResolverBehavior.model_fields)
            if unknown:
                raise ValueError(
                    f"scenario {self.id!r} sets unknown behaviour field(s) "
                    f"{sorted(unknown)} for {resolver_id!r}"
                )
            resolved[resolver_id] = ResolverBehavior(**overrides).model_dump()
        return resolved

    def injection_config_payload(self, resolver_ids: list[str]) -> dict:
        return {
            "scenario_id": self.id,
            "scenario_name": self.name,
            "behaviors": self.resolved_behaviors(resolver_ids),
        }

    def injection_config_hash(self, resolver_ids: list[str]) -> str:
        return config_hash(self.injection_config_payload(resolver_ids))


def load_scenarios(path: str | Path | None = None) -> dict[str, ScenarioDefinition]:
    """Load scenario definitions keyed by scenario id."""
    scenarios_path = Path(path or DEFAULT_SCENARIOS_PATH)
    data = yaml.safe_load(scenarios_path.read_text(encoding="utf-8")) or {}
    definitions = [ScenarioDefinition(**entry) for entry in data.get("scenarios", [])]

    by_id: dict[str, ScenarioDefinition] = {}
    for definition in definitions:
        if definition.id in by_id:
            raise ValueError(f"duplicate scenario id {definition.id!r}")
        by_id[definition.id] = definition
    return by_id


def apply_scenario(
    client: httpx.Client,
    scenario: ScenarioDefinition,
    admin_urls: dict[str, str],
    timeout_s: float = 5.0,
) -> dict[str, dict]:
    """Push the scenario's full behaviour to every resolver.

    Returns the behaviour actually acknowledged by each resolver, so the
    caller can verify that what was requested is what got applied.
    """
    resolved = scenario.resolved_behaviors(sorted(admin_urls))
    acknowledged: dict[str, dict] = {}
    for resolver_id, behavior in resolved.items():
        response = client.post(
            f"{admin_urls[resolver_id].rstrip('/')}/admin/behavior",
            json=behavior,
            timeout=timeout_s,
        )
        response.raise_for_status()
        acknowledged[resolver_id] = response.json()["behavior"]
    return acknowledged


def reset_resolvers(
    client: httpx.Client, admin_urls: dict[str, str], timeout_s: float = 5.0
) -> None:
    """Return every resolver to the healthy default."""
    for url in admin_urls.values():
        client.post(f"{url.rstrip('/')}/admin/reset", timeout=timeout_s).raise_for_status()
