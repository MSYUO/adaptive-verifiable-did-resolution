"""Unit tests for canonical serialisation, hashing and provenance."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.provenance import (
    Provenance,
    build_provenance,
    canonical_json,
    config_hash,
    new_experiment_id,
    new_trial_id,
    resolve_git_commit,
)
from avdr.scenarios import load_scenarios

RESOLVER_IDS = ["resolver-a", "resolver-b", "resolver-c"]


def test_canonical_json_is_key_order_independent():
    left = {"b": 1, "a": {"y": 2, "x": 3}}
    right = {"a": {"x": 3, "y": 2}, "b": 1}
    assert canonical_json(left) == canonical_json(right)


def test_canonical_json_preserves_list_order():
    """Resolver order is meaningful and must change the hash."""
    assert canonical_json({"r": ["a", "b"]}) != canonical_json({"r": ["b", "a"]})


def test_config_hash_is_deterministic_and_prefixed():
    payload = {"resolvers": ["a", "b"], "timeout_ms": 2000}
    first = config_hash(payload)
    second = config_hash({"timeout_ms": 2000, "resolvers": ["a", "b"]})
    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_config_hash_changes_with_content():
    base = config_hash({"timeout_ms": 2000})
    assert base != config_hash({"timeout_ms": 2001})


def test_scenario_injection_hash_is_deterministic_and_distinct():
    scenarios = load_scenarios()
    hashes = {
        scenario_id: scenario.injection_config_hash(RESOLVER_IDS)
        for scenario_id, scenario in scenarios.items()
    }
    # Stable across repeated computation.
    for scenario_id, scenario in scenarios.items():
        assert scenario.injection_config_hash(RESOLVER_IDS) == hashes[scenario_id]
    # Distinct scenarios must not collide.
    assert len(set(hashes.values())) == len(hashes)


def test_scenario_expands_unmentioned_resolvers_to_defaults():
    """An omitted resolver and an explicitly healthy one must hash alike."""
    scenarios = load_scenarios()
    resolved = scenarios["F"].resolved_behaviors(RESOLVER_IDS)
    assert set(resolved) == set(RESOLVER_IDS)
    assert resolved["resolver-b"]["force_error"] is False
    assert resolved["resolver-a"]["force_error"] is True


def test_scenario_rejects_unknown_behaviour_field():
    import pytest

    from avdr.scenarios import ScenarioDefinition

    scenario = ScenarioDefinition(
        id="Z", name="bad", behaviors={"resolver-a": {"not_a_field": 1}}
    )
    with pytest.raises(ValueError, match="unknown behaviour field"):
        scenario.resolved_behaviors(RESOLVER_IDS)


def test_git_commit_resolves_in_repository():
    """A real SHA, or an explicit reason. Never a fabricated value."""
    repo_root = Path(__file__).resolve().parent.parent
    sha, reason = resolve_git_commit(repo_root)
    if sha is None:
        assert reason, "an unresolved commit must carry a reason"
    else:
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)
        assert reason is None


def test_git_commit_unresolved_outside_repository(tmp_path):
    sha, reason = resolve_git_commit(tmp_path)
    assert sha is None
    assert reason is not None


def test_build_provenance_records_reason_for_missing_hash(tmp_path):
    provenance = build_provenance(
        experiment_id="exp-test",
        scenario_id="N",
        phase="unit-test",
        repo_root=tmp_path,
        router_config_payload=None,
        injection_config_payload=None,
    )
    assert provenance.config_hash is None
    assert provenance.injection_config_hash is None
    assert provenance.git_commit is None
    # Every null must be explained, not silently absent.
    assert "config_hash" in provenance.unresolved
    assert "injection_config_hash" in provenance.unresolved
    assert "git_commit" in provenance.unresolved


def test_provenance_for_trial_changes_only_trial_id():
    base = Provenance(
        experiment_id="exp-1",
        trial_id="",
        scenario_id="N",
        phase="unit-test",
        seed=7,
        git_commit="0" * 40,
        config_hash="sha256:abc",
        injection_config_hash="sha256:def",
    )
    derived = base.for_trial("trial-9")
    assert derived.trial_id == "trial-9"
    assert derived.experiment_id == base.experiment_id
    assert derived.config_hash == base.config_hash
    assert derived.seed == base.seed
    assert base.trial_id == ""  # original untouched


def test_identifiers_are_unique():
    assert new_trial_id() != new_trial_id()
    assert new_experiment_id() != new_experiment_id()
    assert new_experiment_id("qual").startswith("qual-")
