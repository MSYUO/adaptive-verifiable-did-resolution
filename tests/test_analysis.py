"""Unit tests for derived analysis over shadow trials.

These use synthetic rows with KNOWN ground truth so the derivation logic is
tested independently of any live deployment.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from avdr.analysis import audit_dataset, derive_all, derive_trial, summarize

PROVENANCE = {
    "experiment_id": "exp-1",
    "scenario_id": "Q",
    "phase": "unit-test",
    "git_commit": "0" * 40,
    "config_hash": "sha256:cfg",
    "injection_config_hash": "sha256:inj",
}


def make_trial(trial_id="t1", expected=3, actual=3, complete=True, reason=None):
    return {
        **PROVENANCE,
        "trial_id": trial_id,
        "record_type": "shadow_trial",
        "expected_observations": expected,
        "actual_observations": actual,
        "complete": complete,
        "incomplete_reason": reason,
        "mode": "parallel",
        "did": "did:example:x",
        "timestamp": "2026-09-06T00:00:00+00:00",
        "trial_duration_ms": 10.0,
        "launch_skew_ms": 0.5,
        "seed": 1,
        "git_dirty": False,
        "did_method": "example",
    }


def make_observation(
    resolver_id,
    latency_ms,
    accepted,
    trial_id="t1",
    http_status=200,
    document_valid=None,
    outcome=None,
):
    if document_valid is None:
        document_valid = accepted if http_status == 200 else None
    if outcome is None:
        outcome = "accepted" if accepted else "rejected_invalid"
    return {
        **PROVENANCE,
        "trial_id": trial_id,
        "record_type": "shadow_observation",
        "resolver_id": resolver_id,
        "resolver_url": f"http://{resolver_id}",
        "latency_ms": latency_ms,
        "accepted": accepted,
        "http_status": http_status,
        "document_valid": document_valid,
        "acceptance_reason": "synthetic",
        "outcome": outcome,
        "timeout": http_status is None,
        "error": None,
        "start_ts": "2026-09-06T00:00:00+00:00",
        "end_ts": "2026-09-06T00:00:01+00:00",
        "launch_offset_ms": 0.1,
        "did": "did:example:x",
        "did_method": "example",
        "seed": 1,
        "git_dirty": False,
    }


# --------------------------------------------------------------------------
# fastest / fastest-accepted derivation
# --------------------------------------------------------------------------


def test_fastest_resolver_is_the_minimum_latency_responder():
    trial = make_trial()
    observations = [
        make_observation("resolver-a", 50.0, True),
        make_observation("resolver-b", 10.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    result = derive_trial(trial, observations)
    assert result.analyzable
    assert result.fastest_responding_resolver == "resolver-b"
    assert result.fastest_responding_latency_ms == 10.0
    assert result.fastest_accepted_resolver == "resolver-b"
    assert result.fastest_matches_fastest_accepted is True


def test_fastest_accepted_rejects_invalid_faster_response():
    """The core capability: an unacceptable fast response must not win."""
    trial = make_trial()
    observations = [
        make_observation("resolver-a", 2.0, False),   # fastest, unacceptable
        make_observation("resolver-b", 200.0, True),
        make_observation("resolver-c", 400.0, True),
    ]
    result = derive_trial(trial, observations)
    assert result.analyzable
    assert result.fastest_responding_resolver == "resolver-a"
    assert result.fastest_accepted_resolver == "resolver-b"
    assert result.fastest_matches_fastest_accepted is False
    assert result.accepted_count == 2
    assert result.responding_count == 3


def test_http_error_is_not_accepted_even_if_fastest():
    trial = make_trial()
    observations = [
        make_observation(
            "resolver-a", 1.0, False, http_status=503,
            document_valid=None, outcome="http_error",
        ),
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    result = derive_trial(trial, observations)
    assert result.fastest_responding_resolver == "resolver-a"
    assert result.fastest_accepted_resolver == "resolver-b"
    assert result.fastest_matches_fastest_accepted is False


def test_censored_observations_are_excluded_from_minima():
    """A timeout is a lower bound, not a completion time."""
    trial = make_trial()
    observations = [
        make_observation(
            "resolver-a", 400.0, False, http_status=None,
            document_valid=None, outcome="timeout",
        ),
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    result = derive_trial(trial, observations)
    assert result.censored_count == 1
    assert result.censored_resolvers == ["resolver-a"]
    assert result.responding_count == 2
    assert result.fastest_responding_resolver == "resolver-b"
    assert result.fastest_matches_fastest_accepted is True


def test_tie_is_flagged():
    trial = make_trial()
    observations = [
        make_observation("resolver-a", 10.0, True),
        make_observation("resolver-b", 10.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    result = derive_trial(trial, observations)
    assert result.fastest_responding_tie is True
    assert result.fastest_accepted_tie is True


def test_no_acceptable_response_is_not_analyzable():
    trial = make_trial()
    observations = [
        make_observation("resolver-a", 10.0, False),
        make_observation("resolver-b", 20.0, False),
        make_observation("resolver-c", 30.0, False),
    ]
    result = derive_trial(trial, observations)
    assert result.analyzable is False
    assert result.fastest_matches_fastest_accepted is None
    assert "no resolver produced an acceptable response" in result.reason


# --------------------------------------------------------------------------
# incomplete trials must never be silently analysed
# --------------------------------------------------------------------------


def test_incomplete_trial_is_excluded_and_labelled():
    trial = make_trial(actual=2, complete=False, reason="resolver-c probe raised")
    observations = [
        make_observation("resolver-a", 2.0, False),
        make_observation("resolver-b", 20.0, True),
    ]
    result = derive_trial(trial, observations)
    assert result.analyzable is False
    assert result.fastest_matches_fastest_accepted is None
    assert "incomplete" in result.reason


def test_observation_count_mismatch_is_excluded():
    """Declared complete but short on rows: still refused."""
    trial = make_trial(expected=3, actual=3, complete=True)
    observations = [make_observation("resolver-a", 2.0, True)]
    result = derive_trial(trial, observations)
    assert result.analyzable is False
    assert "does not match expected" in result.reason


def test_summarize_counts_only_analyzable_trials():
    trials = [make_trial("t1"), make_trial("t2", actual=1, complete=False)]
    observations = [
        make_observation("resolver-a", 2.0, False, trial_id="t1"),
        make_observation("resolver-b", 20.0, True, trial_id="t1"),
        make_observation("resolver-c", 30.0, True, trial_id="t1"),
        make_observation("resolver-a", 2.0, True, trial_id="t2"),
    ]
    summary = summarize(derive_all(trials, observations))
    assert summary["total_trials"] == 2
    assert summary["analyzable_trials"] == 1
    assert summary["excluded_trials"] == 1
    assert summary["fastest_differs_from_fastest_accepted"] == 1
    assert summary["label"] == "CONTROLLED LOCAL QUALIFICATION"


# --------------------------------------------------------------------------
# integrity audit
# --------------------------------------------------------------------------


def test_audit_passes_on_a_clean_dataset():
    trials = [make_trial()]
    observations = [
        make_observation("resolver-a", 2.0, True),
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    audit = audit_dataset(trials, observations)
    assert audit["passed"], audit["violations"]


def test_audit_detects_duplicate_trial_resolver_pair():
    trials = [make_trial(actual=4, expected=4)]
    observations = [
        make_observation("resolver-a", 2.0, True),
        make_observation("resolver-a", 3.0, True),  # duplicate
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    audit = audit_dataset(trials, observations)
    assert not audit["passed"]
    assert any("duplicate observation" in v for v in audit["violations"])


def test_audit_detects_missing_observation():
    trials = [make_trial(expected=3, actual=3, complete=True)]
    observations = [
        make_observation("resolver-a", 2.0, True),
        make_observation("resolver-b", 20.0, True),
    ]
    audit = audit_dataset(trials, observations)
    assert not audit["passed"]
    assert any("declares 3 observations, found 2" in v for v in audit["violations"])


def test_audit_detects_negative_latency_and_bad_ordering():
    trials = [make_trial()]
    bad = make_observation("resolver-a", -1.0, True)
    bad["end_ts"] = "2025-01-01T00:00:00+00:00"
    observations = [
        bad,
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    audit = audit_dataset(trials, observations)
    assert not audit["passed"]
    assert any("negative latency" in v for v in audit["violations"])
    assert any("end before start" in v for v in audit["violations"])


def test_audit_detects_accepted_without_validation():
    trials = [make_trial()]
    bad = make_observation("resolver-a", 2.0, True)
    bad["document_valid"] = False
    observations = [
        bad,
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    audit = audit_dataset(trials, observations)
    assert not audit["passed"]
    assert any("accepted without passing validation" in v for v in audit["violations"])


def test_audit_detects_provenance_mismatch():
    trials = [make_trial()]
    rogue = make_observation("resolver-a", 2.0, True)
    rogue["injection_config_hash"] = "sha256:different"
    observations = [
        rogue,
        make_observation("resolver-b", 20.0, True),
        make_observation("resolver-c", 30.0, True),
    ]
    audit = audit_dataset(trials, observations)
    assert not audit["passed"]
    assert any("provenance mismatch" in v for v in audit["violations"])


def test_audit_detects_orphan_observations():
    observations = [make_observation("resolver-a", 2.0, True, trial_id="ghost")]
    audit = audit_dataset([], observations)
    assert not audit["passed"]
    assert any("no parent trial" in v for v in audit["violations"])
