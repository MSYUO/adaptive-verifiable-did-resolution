"""Unit tests for routing policies (no I/O)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.config import ResolverEndpoint, RouterConfig
from avdr.router.policies import (
    RoundRobinPolicy,
    SequentialFailoverPolicy,
    SingleStaticPolicy,
    build_policies,
)

DID = "did:example:subject-1"


@pytest.fixture
def config() -> RouterConfig:
    return RouterConfig(
        resolvers=[
            ResolverEndpoint(id="resolver-a", url="http://a"),
            ResolverEndpoint(id="resolver-b", url="http://b"),
            ResolverEndpoint(id="resolver-c", url="http://c"),
        ],
        single_static_target="resolver-a",
    )


def test_single_static_always_targets_one_resolver(config):
    policy = SingleStaticPolicy(config)
    for _ in range(5):
        plan = policy.plan(DID)
        assert plan.attempt_order() == ["resolver-a"]
        assert plan.max_attempts == 1


def test_single_static_rejects_unknown_target(config):
    with pytest.raises(KeyError):
        SingleStaticPolicy(config, target="resolver-z")


def test_round_robin_rotates_per_logical_request(config):
    policy = RoundRobinPolicy(config)
    observed = [policy.plan(DID).target for _ in range(7)]
    assert observed == [
        "resolver-a",
        "resolver-b",
        "resolver-c",
        "resolver-a",
        "resolver-b",
        "resolver-c",
        "resolver-a",
    ]


def test_round_robin_does_not_failover(config):
    """One attempt per logical request keeps the sequence deterministic."""
    policy = RoundRobinPolicy(config)
    plan = policy.plan(DID)
    assert plan.max_attempts == 1
    assert len(plan.attempt_order()) == 1


def test_round_robin_reset(config):
    policy = RoundRobinPolicy(config)
    policy.plan(DID)
    policy.plan(DID)
    policy.reset()
    assert policy.plan(DID).target == "resolver-a"


def test_sequential_failover_offers_full_ordered_list(config):
    policy = SequentialFailoverPolicy(config)
    plan = policy.plan(DID)
    assert plan.candidate_sequence == ["resolver-a", "resolver-b", "resolver-c"]
    assert plan.max_attempts == 3
    assert plan.attempt_order() == ["resolver-a", "resolver-b", "resolver-c"]


def test_build_policies_returns_all_three(config):
    policies = build_policies(config)
    assert sorted(policies) == ["round-robin", "sequential-failover", "single-static"]
