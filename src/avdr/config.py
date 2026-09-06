"""Centralised router / resolver-registry configuration.

The resolver registry lives in a YAML file so that the router never contains
hard-coded resolver identities or endpoints. Values support
``${VAR:-default}`` expansion so the same file works for a host-local run and
for docker compose, where only the URLs differ.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# Repository root, derived from this file's location (src/avdr/config.py).
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "resolvers.yaml"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str, environ: dict[str, str] | None = None) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` references in a string."""
    env = os.environ if environ is None else environ

    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        value = env.get(name)
        if value is None or value == "":
            return default if default is not None else ""
        return value

    return _ENV_REF.sub(_sub, text)


class ResolverEndpoint(BaseModel):
    id: str
    url: str


class RouterConfig(BaseModel):
    """Everything the router needs to route. No experiment behaviour here."""

    resolvers: list[ResolverEndpoint] = Field(min_length=1)
    default_policy: str = "sequential-failover"
    single_static_target: str | None = None

    # Explicit, never a library default. Applied per resolver attempt.
    attempt_timeout_ms: int = 2000

    policy_version: str = "baseline-v1"
    telemetry_dir: str = "telemetry"

    def resolver_ids(self) -> list[str]:
        return [r.id for r in self.resolvers]

    def endpoint(self, resolver_id: str) -> ResolverEndpoint:
        for resolver in self.resolvers:
            if resolver.id == resolver_id:
                return resolver
        raise KeyError(f"unknown resolver_id: {resolver_id!r}")


def load_router_config(path: str | Path | None = None) -> RouterConfig:
    """Load the resolver registry from YAML, then apply env overrides."""
    config_path = Path(path or os.environ.get("AVDR_CONFIG", DEFAULT_CONFIG_PATH))
    raw = expand_env(config_path.read_text(encoding="utf-8"))
    data = yaml.safe_load(raw) or {}

    router_section = data.get("router", {}) or {}
    config = RouterConfig(
        resolvers=data.get("resolvers", []),
        default_policy=router_section.get("default_policy", "sequential-failover"),
        single_static_target=router_section.get("single_static_target"),
        attempt_timeout_ms=int(router_section.get("attempt_timeout_ms", 2000)),
        policy_version=router_section.get("policy_version", "baseline-v1"),
        telemetry_dir=router_section.get("telemetry_dir", "telemetry"),
    )

    if config.single_static_target is None:
        config.single_static_target = config.resolvers[0].id
    if config.single_static_target not in config.resolver_ids():
        raise ValueError(
            f"single_static_target {config.single_static_target!r} is not a configured resolver"
        )
    return config
