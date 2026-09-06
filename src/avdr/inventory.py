"""Loaders for the real-provider inventory and the DID fixture manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .config import REPO_ROOT, expand_env
from .provenance import config_hash

DEFAULT_PROVIDERS_PATH = REPO_ROOT / "config" / "providers.yaml"
DEFAULT_FIXTURES_PATH = REPO_ROOT / "config" / "fixtures.yaml"


class ProviderEntry(BaseModel):
    id: str
    implementation_id: str | None = None
    implementation_notes: str | None = None
    operator: str | None = None
    endpoint: str
    adapter: str
    auth_required: bool = False
    auth_scheme: str | None = None
    credentials_available: bool | None = None
    supported_did_methods: list[str] = Field(default_factory=list)
    response_media_type: str | None = None
    full_resolution_result: bool | None = None
    full_resolution_result_notes: str | None = None
    container_image: str | None = None
    container_image_digest: str | None = None
    rate_limit_documented: bool | None = None
    rate_limit_requests: int | None = None
    rate_limit_window_seconds: int | None = None
    rate_limit_notes: str | None = None
    terms_notes: str | None = None
    source: str | None = None
    available: bool = True
    unavailable_reason: str | None = None
    independence_notes: str | None = None

    def supports(self, did_method: str) -> bool:
        return did_method in self.supported_did_methods

    @property
    def is_external(self) -> bool:
        """True when requests leave this host and consume someone else's budget."""
        endpoint = self.endpoint.lower()
        return not (
            endpoint.startswith("http://127.0.0.1")
            or endpoint.startswith("http://localhost")
        )


class ProviderInventory(BaseModel):
    inventory_version: str
    providers: list[ProviderEntry]

    def available_providers(self) -> list[ProviderEntry]:
        return [p for p in self.providers if p.available]

    def for_method(self, did_method: str) -> list[ProviderEntry]:
        return [p for p in self.available_providers() if p.supports(did_method)]

    def get(self, provider_id: str) -> ProviderEntry:
        for provider in self.providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(f"unknown provider {provider_id!r}")

    def payload(self) -> dict[str, Any]:
        """Canonical payload for hashing the inventory."""
        return {
            "inventory_version": self.inventory_version,
            "providers": [p.model_dump(mode="json") for p in self.providers],
        }

    def inventory_hash(self) -> str:
        return config_hash(self.payload())


class FixtureEntry(BaseModel):
    fixture_id: str
    did: str
    did_method: str
    expected_subject: str | None = None
    source: str
    project_controlled: bool
    externally_hosted: bool
    persistence_assumption: str
    project_control_note: str | None = None
    expected_outcome: str = "resolvable"
    expected_error_family: str | None = None


class FixtureManifest(BaseModel):
    manifest_version: str
    fixtures: list[FixtureEntry]

    def get(self, fixture_id: str) -> FixtureEntry:
        for fixture in self.fixtures:
            if fixture.fixture_id == fixture_id:
                return fixture
        raise KeyError(f"unknown fixture {fixture_id!r}")

    def resolvable(self) -> list[FixtureEntry]:
        return [f for f in self.fixtures if f.expected_outcome == "resolvable"]

    def unresolvable(self) -> list[FixtureEntry]:
        return [f for f in self.fixtures if f.expected_outcome == "unresolvable"]

    def payload(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "fixtures": [f.model_dump(mode="json") for f in self.fixtures],
        }

    def manifest_hash(self) -> str:
        return config_hash(self.payload())


def load_provider_inventory(path: str | Path | None = None) -> ProviderInventory:
    # ${VAR:-default} expansion, so one inventory file works for a host-local
    # run and for docker service names. Without this the placeholder would be
    # used verbatim as a URL.
    raw = expand_env(
        Path(path or DEFAULT_PROVIDERS_PATH).read_text(encoding="utf-8")
    )
    data = yaml.safe_load(raw)
    inventory = ProviderInventory(**data)
    seen = set()
    for provider in inventory.providers:
        if provider.id in seen:
            raise ValueError(f"duplicate provider id {provider.id!r}")
        seen.add(provider.id)
        # Catch an unexpanded placeholder or a typo before it becomes a
        # mysterious connection error at request time.
        if not provider.endpoint.startswith(("http://", "https://")):
            raise ValueError(
                f"provider {provider.id!r} has a non-URL endpoint "
                f"{provider.endpoint!r} (unexpanded ${{VAR}} placeholder?)"
            )
        if provider.auth_required and provider.available:
            # An authenticated provider cannot be marked available unless
            # credentials were explicitly confirmed present.
            if not provider.credentials_available:
                raise ValueError(
                    f"provider {provider.id!r} requires auth but has no "
                    f"credentials and is marked available"
                )
    return inventory


def load_fixture_manifest(path: str | Path | None = None) -> FixtureManifest:
    data = yaml.safe_load(
        Path(path or DEFAULT_FIXTURES_PATH).read_text(encoding="utf-8")
    )
    manifest = FixtureManifest(**data)
    seen = set()
    for fixture in manifest.fixtures:
        if fixture.fixture_id in seen:
            raise ValueError(f"duplicate fixture id {fixture.fixture_id!r}")
        seen.add(fixture.fixture_id)
    return manifest
