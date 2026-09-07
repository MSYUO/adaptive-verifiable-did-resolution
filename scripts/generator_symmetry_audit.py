"""Generator symmetry audit + zero-cost V2 static-subset audit.

Two things, neither of which generates new trial data against the providers.

A. V2 STATIC-SUBSET AUDIT [POST-HOC DIAGNOSTIC]
   Recovers whatever static-subset baselines the persisted V2 holdout report
   supports. It does NOT support all seven: like V1, the V2 holdout persisted
   aggregate metrics only, so subsets that were not evaluated at run time
   ({B}, {C}, {A,C}, {B,C}) cannot be recovered. That gap is reported rather
   than filled by re-running and calling it the same trials.

B. GENERATOR SYMMETRY AUDIT [CALCULATED from injection parameters]
   Structural analysis of the controlled generator: which providers each state
   degrades, and the resulting per-provider and per-subset success structure.
   Outcomes are derived directly from the injected parameters (delay > tau,
   forced error, forced invalid) rather than by probing, so this costs nothing
   and isolates the GENERATOR's properties from transport noise.

Neither part revises any V1 or V2 conclusion.
"""

from __future__ import annotations

import json
import random
import sys
from itertools import combinations
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.learning.dataset import DEADLINE_TAU_MS  # noqa: E402
from avdr.learning.environment import (  # noqa: E402
    PROVIDERS,
    STATE_TABLE,
    STATES,
    build_episode_plan,
    sample_behaviors,
)

# Transport overhead observed on this host for a warmed connection; used only
# to keep the structural derivation comparable to measured trials.
ASSUMED_OVERHEAD_MS = 8.0
AUDIT_SAMPLES = 40_000
AUDIT_SEED = 424242


def succeeds(behavior: dict, tau_ms: float) -> bool:
    """Would this injected behaviour yield an acceptable response by tau?"""
    if behavior["force_error"] or behavior["force_invalid"]:
        return False
    return behavior["artificial_delay_ms"] + ASSUMED_OVERHEAD_MS <= tau_ms


def v2_static_audit() -> dict:
    path = REPO_ROOT / "artifacts" / "learning_v2" / "v2_holdout_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    baselines = report["baselines"]
    n = baselines["all-race"]["n"]

    recovered = {}
    mapping = {
        "{A}": "single-static(local-a)",
        "{A,B}": "fixed-k2(a,b)",
        "{A,B,C}": "all-race",
    }
    for label, key in mapping.items():
        d = baselines[key]
        successes = d["committed_success_count"]
        recovered[label] = {
            "successes": successes,
            "trials": n,
            "success_rate": round(successes / n, 6),
            "oracle_feasible_captured": successes,
            "misses_where_another_would_succeed": d["avoidable_miss_count"],
            "intrinsic_misses": d["intrinsic_miss_count"],
            "calls_per_request": d["mean_total_calls"],
            "total_calls": d["total_provider_calls"],
        }
    return {
        "recoverable": recovered,
        "not_recoverable": ["{B}", "{C}", "{A,C}", "{B,C}"],
        "reason": (
            "the V2 holdout report persisted aggregate metrics for only the "
            "four policies evaluated at run time; per-trial ground truth was "
            "not written, so the remaining static subsets cannot be scored on "
            "those same 220 trials"
        ),
        "oracle_feasible_count": baselines["all-race"]["oracle_feasible_count"],
        "trials": n,
    }


def structural_symmetry() -> dict:
    """Which providers does each state degrade, and how symmetric is that?"""
    default = STATE_TABLE["NORMAL"]["local-a"]
    per_state = {}
    degraded_counts = {p: 0 for p in PROVIDERS}

    for state in STATES:
        table = STATE_TABLE[state]
        degraded = [
            p for p in PROVIDERS
            if (
                table[p].delay_lo_ms != default.delay_lo_ms
                or table[p].delay_hi_ms != default.delay_hi_ms
                or table[p].error_probability != default.error_probability
                or table[p].invalid_probability != default.invalid_probability
            )
        ]
        per_state[state] = degraded
        for p in degraded:
            degraded_counts[p] += 1

    single_provider_states = {
        p: [s for s, d in per_state.items() if d == [p]] for p in PROVIDERS
    }
    symmetric = len({len(v) for v in single_provider_states.values()}) == 1
    return {
        "degraded_providers_per_state": per_state,
        "states_degrading_each_provider": degraded_counts,
        "provider_specific_states": single_provider_states,
        "provider_specific_state_counts": {
            p: len(v) for p, v in single_provider_states.items()
        },
        "symmetric": symmetric,
    }


def monte_carlo_audit(tau_ms: float) -> dict:
    """Per-provider and per-subset success structure over sampled episodes."""
    rng = random.Random(AUDIT_SEED)
    subsets = [
        tuple(c) for size in range(1, 4) for c in combinations(sorted(PROVIDERS), size)
    ]
    provider_success = {p: 0 for p in PROVIDERS}
    subset_success = {s: 0 for s in subsets}
    unique_wins = {p: 0 for p in PROVIDERS}
    oracle_feasible = 0
    state_counts = {s: 0 for s in STATES}

    # Sample states the way episodes actually do, so the marginal mix matches.
    plans = [
        build_episode_plan(f"audit-{i:04d}", AUDIT_SEED + i, "audit", 22)
        for i in range(AUDIT_SAMPLES // 22)
    ]
    total = 0
    for plan in plans:
        for state in plan.states:
            state_counts[state] += 1
            total += 1
            behaviors = sample_behaviors(state, rng)
            wins = {p: succeeds(behaviors[p], tau_ms) for p in PROVIDERS}
            for p, ok in wins.items():
                provider_success[p] += ok
            winners = [p for p, ok in wins.items() if ok]
            if winners:
                oracle_feasible += 1
            if len(winners) == 1:
                unique_wins[winners[0]] += 1
            for subset in subsets:
                if any(wins[p] for p in subset):
                    subset_success[subset] += 1

    rate = lambda c: round(c / total, 6)  # noqa: E731
    marginals = {}
    for pair in combinations(sorted(PROVIDERS), 2):
        full = subset_success[tuple(sorted(PROVIDERS))]
        marginals[f"add third to {{{','.join(p[-1].upper() for p in pair)}}}"] = round(
            (full - subset_success[pair]) / total, 6
        )

    return {
        "samples": total,
        "assumed_overhead_ms": ASSUMED_OVERHEAD_MS,
        "state_mix": {s: rate(c) for s, c in state_counts.items()},
        "provider_success_rate": {p: rate(c) for p, c in provider_success.items()},
        "subset_success_rate": {
            ",".join(s): rate(c) for s, c in subset_success.items()
        },
        "provider_unique_win_count": unique_wins,
        "provider_unique_win_rate": {p: rate(c) for p, c in unique_wins.items()},
        "oracle_feasible_rate": rate(oracle_feasible),
        "marginal_value_of_third_provider": marginals,
        "best_singleton": max(provider_success, key=provider_success.get),
        "best_pair": ",".join(
            max(
                (s for s in subsets if len(s) == 2),
                key=lambda s: subset_success[s],
            )
        ),
    }


def main() -> int:
    print("GENERATOR SYMMETRY AUDIT + ZERO-COST V2 STATIC AUDIT")
    print("  [POST-HOC DIAGNOSTIC] -- revises no V1 or V2 conclusion\n")

    v2 = v2_static_audit()
    print("== A. V2 static-subset audit (recoverable subsets only) ==")
    print(f"  trials={v2['trials']}  oracle-feasible={v2['oracle_feasible_count']}")
    print(f"  {'subset':10s} {'succ':>5s} {'rate':>8s} {'avoid.miss':>11s} {'calls/req':>10s}")
    for label, row in v2["recoverable"].items():
        print(f"  {label:10s} {row['successes']:>5d} {row['success_rate']:>8} "
              f"{row['misses_where_another_would_succeed']:>11d} "
              f"{row['calls_per_request']:>10}")
    print(f"  NOT RECOVERABLE: {v2['not_recoverable']}")
    print(f"  reason: {v2['reason']}\n")

    sym = structural_symmetry()
    print("== B. Generator structural symmetry ==")
    for state, degraded in sym["degraded_providers_per_state"].items():
        print(f"  {state:24s} degrades {degraded if degraded else '(none)'}")
    print(f"\n  provider-specific states per provider: "
          f"{sym['provider_specific_state_counts']}")
    verdict = "PASS" if sym["symmetric"] else "FAIL"
    print(f"  GENERATOR_PROVIDER_SYMMETRY = {verdict}")

    mc = monte_carlo_audit(DEADLINE_TAU_MS)
    print(f"\n== C. Monte-Carlo structure ({mc['samples']} sampled trials) ==")
    print(f"  provider success rate : {mc['provider_success_rate']}")
    print(f"  provider unique wins  : {mc['provider_unique_win_count']}")
    print(f"  oracle feasible rate  : {mc['oracle_feasible_rate']}")
    print("  subset success rate:")
    for s, r in sorted(mc["subset_success_rate"].items(), key=lambda kv: (len(kv[0]), kv[0])):
        print(f"     {s:34s} {r}")
    print(f"  marginal value of 3rd provider: {mc['marginal_value_of_third_provider']}")
    print(f"  best singleton = {mc['best_singleton']}   best pair = {mc['best_pair']}")

    out = REPO_ROOT / "artifacts" / "learning_v2" / "generator_symmetry_audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "diagnostic_type": "POST-HOC DIAGNOSTIC",
                "revises_no_prior_conclusion": True,
                "v2_static_subset_audit": v2,
                "structural_symmetry": sym,
                "GENERATOR_PROVIDER_SYMMETRY": verdict,
                "monte_carlo": mc,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
