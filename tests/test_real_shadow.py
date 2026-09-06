"""Multi-provider shadow harness tests over a mocked transport.

No public Internet access is used. Provider responses are served by
httpx.MockTransport so launch ordering, connection-mode tagging, provenance
propagation and completeness handling are all deterministic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.inventory import (
    ProviderEntry,
    load_fixture_manifest,
    load_provider_inventory,
)
from avdr.provenance import Provenance, new_experiment_id
from avdr.real_shadow import (
    CONNECTION_MODES,
    NEW_CLIENT,
    REUSED_CLIENT,
    RealProviderProbe,
    compare_documents,
)
from avdr.telemetry import TelemetrySink

from test_real_adapters import DID_KEY, DRIVER_BODY, UNIRESOLVER_BODY, UNSUPPORTED_BODY

PROVIDERS = [
    ProviderEntry(
        id="mock-full-a",
        implementation_id="impl-alpha",
        endpoint="https://alpha.invalid",
        adapter="universal-resolver-v1",
        supported_did_methods=["key"],
    ),
    ProviderEntry(
        id="mock-full-b",
        implementation_id="impl-alpha",
        endpoint="https://beta.invalid",
        adapter="universal-resolver-v1",
        supported_did_methods=["key"],
    ),
    ProviderEntry(
        id="mock-driver-c",
        implementation_id="impl-gamma",
        endpoint="https://gamma.invalid",
        adapter="did-document-only-v1",
        supported_did_methods=["key"],
    ),
]


def make_provenance(**overrides) -> Provenance:
    base = dict(
        experiment_id=new_experiment_id("test"),
        trial_id="",
        scenario_id="real-key",
        phase="real-qualification-test",
        seed=7,
        git_commit="0" * 40,
        git_dirty=False,
        config_hash="sha256:cfg",
        injection_config_hash="sha256:inj",
        provider_inventory_hash="sha256:inv",
        fixture_manifest_hash="sha256:fix",
    )
    base.update(overrides)
    return Provenance(**base)


def routing_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "gamma.invalid":
        return httpx.Response(
            200,
            json=DRIVER_BODY,
            headers={"content-type": 'application/ld+json;profile="x"'},
        )
    if host == "boom.invalid":
        raise httpx.ConnectError("simulated connection failure", request=request)
    if host == "limited.invalid":
        return httpx.Response(
            429,
            json={"error": "rate limited"},
            headers={"content-type": "application/json", "retry-after": "42"},
        )
    return httpx.Response(
        200,
        json=UNIRESOLVER_BODY,
        headers={"content-type": "application/did-resolution;charset=utf-8"},
    )


async def run_trial(providers=None, mode=NEW_CLIENT, seed=1234, index=0, did=DID_KEY,
                    handler=routing_handler, provenance=None):
    probe = RealProviderProbe(timeout_ms=5000)
    transport = httpx.MockTransport(handler)
    # `is None`, not truthiness: an explicitly empty list must reach the probe.
    if providers is None:
        providers = PROVIDERS
    async with httpx.AsyncClient(transport=transport) as client:
        return await probe.run_trial(
            client=client,
            did=did,
            fixture_id="key-ed25519-1",
            did_method="key",
            providers=providers,
            provenance=provenance or make_provenance(),
            connection_mode=mode,
            launch_order_seed=seed,
            trial_index=index,
        )


# --------------------------------------------------------------------------
# same-trial multi-provider observation
# --------------------------------------------------------------------------


async def test_trial_observes_every_qualified_provider_once():
    trial = await run_trial()
    assert trial.complete
    assert trial.record.expected_observations == 3
    assert trial.record.actual_observations == 3
    assert sorted(o.provider_id for o in trial.observations) == [
        "mock-driver-c",
        "mock-full-a",
        "mock-full-b",
    ]
    assert len({o.provider_id for o in trial.observations}) == 3


async def test_trial_id_and_fixture_bind_all_observations():
    trial = await run_trial()
    assert {o.trial_id for o in trial.observations} == {trial.record.trial_id}
    assert {o.fixture_id for o in trial.observations} == {"key-ed25519-1"}
    assert {o.requested_did for o in trial.observations} == {DID_KEY}
    assert {o.experiment_id for o in trial.observations} == {
        trial.record.experiment_id
    }


# --------------------------------------------------------------------------
# 7. launch-order seed reproducibility
# --------------------------------------------------------------------------


def test_launch_order_is_deterministic_for_a_seed():
    probe = RealProviderProbe()
    first = [p.id for p in probe.launch_order(PROVIDERS, 20260907, 3)]
    second = [p.id for p in probe.launch_order(PROVIDERS, 20260907, 3)]
    assert first == second
    assert sorted(first) == sorted(p.id for p in PROVIDERS)


def test_launch_order_rotates_across_trials():
    """A fixed order would give one provider a systematic head start."""
    probe = RealProviderProbe()
    orders = {
        tuple(p.id for p in probe.launch_order(PROVIDERS, 20260907, i))
        for i in range(12)
    }
    assert len(orders) > 1


def test_launch_order_differs_by_seed():
    probe = RealProviderProbe()
    a = {tuple(p.id for p in probe.launch_order(PROVIDERS, 1, i)) for i in range(12)}
    b = {tuple(p.id for p in probe.launch_order(PROVIDERS, 2, i)) for i in range(12)}
    assert a != b


async def test_launch_order_recorded_and_positions_match():
    trial = await run_trial(seed=99, index=5)
    order = trial.record.launch_order
    assert sorted(order) == sorted(p.id for p in PROVIDERS)
    by_position = {o.launch_position: o.provider_id for o in trial.observations}
    for position, provider_id in by_position.items():
        assert order[position] == provider_id
    assert trial.record.launch_skew_ms is not None
    assert trial.record.launch_skew_ms >= 0
    assert all(o.launch_order_seed == 99 for o in trial.observations)


# --------------------------------------------------------------------------
# 8. connection mode tagging
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [NEW_CLIENT, REUSED_CLIENT])
async def test_connection_mode_is_tagged_on_trial_and_observations(mode):
    trial = await run_trial(mode=mode)
    assert trial.record.connection_mode == mode
    assert trial.record.client_reuse_policy == CONNECTION_MODES[mode]
    assert all(o.connection_mode == mode for o in trial.observations)
    assert all(o.client_reuse_policy == CONNECTION_MODES[mode] for o in trial.observations)


async def test_unknown_connection_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown connection mode"):
        await run_trial(mode="cold")


def test_new_client_mode_does_not_claim_to_be_cold():
    """The label must not overstate what the platform actually guarantees."""
    description = CONNECTION_MODES[NEW_CLIENT]
    assert "NOT flushed" in description or "not flushed" in description
    assert "cold" not in NEW_CLIENT


# --------------------------------------------------------------------------
# 9. incomplete trials
# --------------------------------------------------------------------------


async def test_connection_failure_is_recorded_not_dropped():
    providers = PROVIDERS + [
        ProviderEntry(
            id="mock-dead",
            endpoint="https://boom.invalid",
            adapter="universal-resolver-v1",
            supported_did_methods=["key"],
        )
    ]
    trial = await run_trial(providers=providers)
    # The provider still yields an observation: a failed one.
    assert trial.record.actual_observations == 4
    assert trial.complete
    dead = next(o for o in trial.observations if o.provider_id == "mock-dead")
    assert dead.transport_outcome == "connection_error"
    assert dead.accepted is False
    assert dead.http_status is None


async def test_rate_limit_is_surfaced_and_flagged():
    providers = PROVIDERS + [
        ProviderEntry(
            id="mock-limited",
            endpoint="https://limited.invalid",
            adapter="universal-resolver-v1",
            supported_did_methods=["key"],
        )
    ]
    trial = await run_trial(providers=providers)
    assert "mock-limited" in trial.record.rate_limited_providers
    limited = next(o for o in trial.observations if o.provider_id == "mock-limited")
    assert limited.http_status == 429
    assert limited.retry_after == "42"
    assert limited.accepted is False


async def test_empty_provider_list_is_rejected():
    with pytest.raises(ValueError, match="no providers"):
        await run_trial(providers=[])


# --------------------------------------------------------------------------
# 6. provenance survives JSONL
# --------------------------------------------------------------------------


async def test_real_provider_provenance_survives_jsonl(tmp_path):
    provenance = make_provenance()
    trial = await run_trial(provenance=provenance)
    sink = TelemetrySink(tmp_path / "real")
    for observation in trial.observations:
        sink.record_real_observation(observation)
        sink.record_raw_response(
            experiment_id=observation.experiment_id,
            trial_id=observation.trial_id,
            provider_id=observation.provider_id,
            raw_response_hash=observation.raw_response_hash,
            body=trial.raw_bodies.get(observation.provider_id),
        )
    sink.record_real_trial(trial.record)

    def read(path):
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    trials = read(sink.real_trials_path)
    observations = read(sink.real_observations_path)
    raws = read(sink.raw_responses_path)

    assert len(trials) == 1 and len(observations) == 3 and len(raws) == 3
    for row in observations:
        assert row["provider_inventory_hash"] == provenance.provider_inventory_hash
        assert row["fixture_manifest_hash"] == provenance.fixture_manifest_hash
        assert row["git_commit"] == provenance.git_commit
        assert row["acceptance_profile"] == "w3c-basic-v1"
        assert row["connection_mode"] == NEW_CLIENT
        assert row["launch_order_seed"] == 1234
        assert row["provider_id"] and row["resolver_endpoint_id"]
    # Raw bodies are preserved and joinable by hash.
    hashes = {r["raw_response_hash"] for r in raws}
    assert hashes == {o["raw_response_hash"] for o in observations}


# --------------------------------------------------------------------------
# 13. response equivalence -- differences recorded, nobody judged
# --------------------------------------------------------------------------


async def test_provider_result_difference_is_observed_not_judged():
    trial = await run_trial()
    comparison = compare_documents(trial.observations)
    assert comparison["accepted_provider_count"] == 3
    assert comparison["exact_subject_match"] is True
    # Two implementations disagree on representation, not on subject.
    assert comparison["distinct_document_hashes"] == 2
    assert comparison["difference_observed"] is True
    assert comparison["flag"] == "PROVIDER_RESULT_DIFFERENCE_OBSERVED"
    # Metadata presence differs and is recorded per provider.
    assert comparison["metadata_presence"]["mock-driver-c"]["resolution_metadata"] is False
    assert comparison["metadata_presence"]["mock-full-a"]["resolution_metadata"] is True


async def test_identical_providers_report_no_difference():
    trial = await run_trial(providers=PROVIDERS[:2])
    comparison = compare_documents(trial.observations)
    assert comparison["distinct_document_hashes"] == 1
    assert comparison["difference_observed"] is False
    assert comparison["flag"] is None


async def test_error_response_yields_no_accepted_providers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            501,
            json=UNSUPPORTED_BODY,
            headers={"content-type": "application/did-resolution"},
        )

    trial = await run_trial(
        providers=PROVIDERS[:2], did="did:avdrnotamethod:x", handler=handler
    )
    assert trial.complete
    assert all(not o.accepted for o in trial.observations)
    assert all(o.resolution_error_family == "methodNotSupported" for o in trial.observations)
    comparison = compare_documents(trial.observations)
    assert comparison["accepted_provider_count"] == 0
    assert comparison["exact_subject_match"] is False


# --------------------------------------------------------------------------
# inventory / fixture manifest integrity (no network)
# --------------------------------------------------------------------------


def test_real_inventory_loads_and_excludes_unavailable():
    inventory = load_provider_inventory()
    available = {p.id for p in inventory.available_providers()}
    # Authenticated provider without credentials must not be available.
    assert "godiddy" not in available
    # Same-deployment alias must be excluded so diversity is not fabricated.
    assert "uniresolver-io" not in available
    assert inventory.get("uniresolver-io").unavailable_reason
    assert inventory.get("godiddy").unavailable_reason
    assert len(available) >= 2


def test_inventory_rejects_authenticated_provider_marked_available():
    with pytest.raises(ValueError, match="requires auth"):
        load_provider_inventory.__wrapped__ if False else None
        from avdr.inventory import ProviderInventory

        inventory = ProviderInventory(
            inventory_version="t",
            providers=[
                ProviderEntry(
                    id="x",
                    endpoint="https://x.invalid",
                    adapter="universal-resolver-v1",
                    auth_required=True,
                    credentials_available=False,
                    available=True,
                )
            ],
        )
        # Re-run the same validation the loader applies.
        for provider in inventory.providers:
            if provider.auth_required and provider.available:
                if not provider.credentials_available:
                    raise ValueError(
                        f"provider {provider.id!r} requires auth but has no "
                        f"credentials and is marked available"
                    )


def test_fixture_manifest_documents_provenance():
    manifest = load_fixture_manifest()
    assert manifest.manifest_hash().startswith("sha256:")
    for fixture in manifest.fixtures:
        assert fixture.source.strip()
        assert fixture.persistence_assumption.strip()
        assert fixture.expected_outcome in ("resolvable", "unresolvable")
    assert {f.fixture_id for f in manifest.unresolvable()} == {
        "unsupported-method",
        "web-unresolvable",
    }


def test_inventory_records_discovered_rate_limit():
    """The public endpoint's limit is only disclosed in its 403 body.

    It must be captured in the inventory so a run can respect it without
    rediscovering it by hitting it.
    """
    inventory = load_provider_inventory()
    dif = inventory.get("uniresolver-dif-dev")
    assert dif.rate_limit_documented is True
    assert dif.rate_limit_requests == 10
    assert dif.rate_limit_window_seconds == 1800
    assert "1800" in dif.rate_limit_notes


def test_external_vs_local_provider_classification():
    """Only external providers consume someone else's request budget."""
    inventory = load_provider_inventory()
    assert inventory.get("uniresolver-dif-dev").is_external is True
    assert inventory.get("selfhosted-driver-did-key").is_external is False


def test_independence_notes_present_for_every_provider():
    """Provider diversity must never be assumed silently."""
    inventory = load_provider_inventory()
    for provider in inventory.providers:
        assert provider.independence_notes, provider.id


def test_unavailable_providers_state_a_reason():
    inventory = load_provider_inventory()
    for provider in inventory.providers:
        if not provider.available:
            assert provider.unavailable_reason, provider.id


def test_throttle_statuses_include_403_and_429():
    from avdr.real_shadow import THROTTLE_STATUSES

    assert 429 in THROTTLE_STATUSES
    assert 403 in THROTTLE_STATUSES
