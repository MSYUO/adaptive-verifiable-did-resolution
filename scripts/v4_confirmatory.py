"""V4 CONFIRMATORY replication of the V3 adaptive-value result. Run ONCE.

Nothing is selected, tuned or retrained here. Every component is reused from
the V3 freeze and verified against `frozen/v3_policy.json` before generation;
any mismatch aborts the run.

Frozen and reused without modification:
    generator      symmetric role-permuted V3
    tau            250 ms          target      0.90
    cold start     3 trials        exploration R = 8
    estimator      b1-rolling-empirical, uncalibrated
    comparator     BEST_FIXED_K2 = {local-b, local-c}
    cost model     execution + warmup + exploration + fallback

PRE-DECLARED BEFORE ANY V4 OUTCOME [DESIGN CHOICE]
    episodes            120 (2 640 trials), seeds 2 000 000..2 000 119
    uncertainty         10 000 episode-level paired bootstrap resamples,
                        analysis seed 20260909
    materiality         0.02 absolute success difference (carried from V3)
    verdict rule        ROBUST_GO / INCONCLUSIVE / NO_GO, coded below

Sample-size rationale, using pre-V4 information only: V3 observed
delta_success = +0.0341 from 12 episodes. Resolving a 0.02 effect needs a
cluster-aware standard error near 0.01. Per-episode paired deltas are bounded
in [-1, 1] over 22 trials, so a per-episode standard deviation around 0.10 is
a reasonable prior; 0.10 / sqrt(120) ~ 0.009. 120 episodes is 10x V3.

Trials within an episode are correlated, so the EPISODE is the unit of
resampling. Individual subset rows are never bootstrapped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from avdr.learning.closedloop import (  # noqa: E402
    COLD_START_TRIALS,
    EXPLORATION_INTERVAL_R,
    SERVICE_BEST_EFFORT,
    feature_schema_hash_v2,
)
from avdr.learning.dataset import ADMIN_URLS, DEADLINE_TAU_MS, all_subsets  # noqa: E402
from avdr.learning.environment import PROVIDERS  # noqa: E402
from avdr.learning.estimators import load_frozen  # noqa: E402
from avdr.learning.metrics import evaluate_estimator  # noqa: E402
from avdr.learning.oracle import ClosedLoopOutcome, decompose  # noqa: E402
from avdr.learning.runner_v2 import oracle_for, run_closed_loop  # noqa: E402
from avdr.learning.v3 import (  # noqa: E402
    ROLE_TABLE,
    ROLES,
    injection_config_hash_v3,
    plan_episodes_v3,
)
from avdr.provenance import resolve_git_commit, resolve_git_dirty  # noqa: E402
from v3_pipeline import collect, provider_entries  # noqa: E402

# ---- PRE-DECLARED, frozen before generation ----
V4_EPISODES, V4_SEED = 120, 2_000_000
TRIALS_PER_EPISODE = 22
BOOTSTRAP_RESAMPLES = 10_000
ANALYSIS_SEED = 20260909
MATERIALITY_SUCCESS = 0.02

NORMAL_TABLE = ROLE_TABLE["NORMAL"]
DEGRADED_ROLES = {
    s: [r for r in ROLES if t[r] is not NORMAL_TABLE[r]] for s, t in ROLE_TABLE.items()
}


def verify_frozen(policy: dict, metadata: dict) -> list[str]:
    """Abort the run on ANY drift from the V3 freeze."""
    problems = []
    expected = {
        "BEST_FIXED_K2": "local-b,local-c",
        "target_slo_probability": 0.90,
        "deadline_tau_ms": 250.0,
        "exploration_interval_R": 8,
        "cold_start_trials": 3,
        "estimator_family": "b1-rolling-empirical",
        "calibration": "uncalibrated",
    }
    for key, want in expected.items():
        if policy.get(key) != want:
            problems.append(f"policy.{key} = {policy.get(key)!r}, expected {want!r}")
    if metadata["artifact_sha256"] != policy["artifact_sha256"]:
        problems.append("frozen artifact hash does not match the frozen policy")
    if metadata["estimator_id"] != "b1-rolling-empirical":
        problems.append(f"estimator_id = {metadata['estimator_id']!r}")
    if metadata["calibration"] is not None:
        problems.append(f"calibration = {metadata['calibration']!r}, expected null")
    if DEADLINE_TAU_MS != 250.0:
        problems.append(f"code DEADLINE_TAU_MS = {DEADLINE_TAU_MS}")
    if EXPLORATION_INTERVAL_R != 8:
        problems.append(f"code EXPLORATION_INTERVAL_R = {EXPLORATION_INTERVAL_R}")
    if COLD_START_TRIALS != 3:
        problems.append(f"code COLD_START_TRIALS = {COLD_START_TRIALS}")
    return problems


def episode_bootstrap(per_episode, resamples, seed):
    """Paired bootstrap over EPISODES (clusters), not individual trials."""
    rng = random.Random(seed)
    episodes = list(per_episode)
    n = len(episodes)
    deltas_success, deltas_calls = [], []
    for _ in range(resamples):
        picked = [episodes[rng.randrange(n)] for _ in range(n)]
        a_s = sum(per_episode[e]["adaptive_success"] for e in picked)
        f_s = sum(per_episode[e]["fixed_success"] for e in picked)
        a_c = sum(per_episode[e]["adaptive_calls"] for e in picked)
        f_c = sum(per_episode[e]["fixed_calls"] for e in picked)
        t = sum(per_episode[e]["trials"] for e in picked)
        deltas_success.append((a_s - f_s) / t)
        deltas_calls.append((a_c - f_c) / t)
    deltas_success.sort()
    deltas_calls.sort()

    def ci(values):
        lo = values[int(0.025 * len(values))]
        hi = values[int(0.975 * len(values)) - 1]
        return [round(lo, 6), round(hi, 6)]

    return {
        "resamples": resamples,
        "analysis_seed": seed,
        "unit": "episode (cluster)",
        "delta_success_ci95": ci(deltas_success),
        "delta_calls_ci95": ci(deltas_calls),
        "delta_success_bootstrap_mean": round(sum(deltas_success) / len(deltas_success), 6),
        "delta_calls_bootstrap_mean": round(sum(deltas_calls) / len(deltas_calls), 6),
        "p_delta_success_le_0": round(
            sum(1 for d in deltas_success if d <= 0) / len(deltas_success), 6
        ),
        "p_delta_calls_ge_0": round(
            sum(1 for d in deltas_calls if d >= 0) / len(deltas_calls), 6
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "learning_v4" / "v4_confirmatory_report.json"))
    args = parser.parse_args()

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)
    policy = json.loads((REPO_ROOT / "frozen" / "v3_policy.json").read_text(encoding="utf-8"))
    estimator, metadata, digest = load_frozen(REPO_ROOT / "frozen" / "frozen_estimator_v3.pkl")

    problems = verify_frozen(policy, metadata)
    print("V4 CONFIRMATORY REPLICATION -- CONTROLLED LOCAL QUALIFICATION")
    print(f"  git_commit={commit} git_dirty={dirty}")
    print(f"  frozen estimator = {metadata['estimator_id']} calibration={metadata['calibration']}")
    print(f"  artifact_sha256 verified = {digest}")
    print(f"  BEST_FIXED_K2 = {policy['BEST_FIXED_K2']}")
    print(f"  injection_config_hash_v3 = {injection_config_hash_v3()}")
    print(f"  feature_schema_hash_v2   = {feature_schema_hash_v2()}")
    if problems:
        print("\n  FROZEN-CONFIG DISCREPANCIES -- ABORTING:")
        for p in problems:
            print(f"    - {p}")
        return 2
    print("  frozen configuration verified: no drift\n")

    target = policy["target_slo_probability"]
    tau = policy["deadline_tau_ms"]
    fixed_subset = tuple(policy["BEST_FIXED_K2"].split(","))

    entries = provider_entries()
    with httpx.Client() as admin:
        for url in ADMIN_URLS.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

    print(f"== generating V4 ({V4_EPISODES} episodes x {TRIALS_PER_EPISODE} trials) ==")
    plans, episodes = asyncio.run(
        collect("v4conf", V4_EPISODES, V4_SEED, entries)
    )
    permutations = {p.episode_id: p.permutation for p in plans}

    # ---- adaptive, frozen policy, best-effort ----
    loop = run_closed_loop(
        estimator, target, tau, episodes, mode=SERVICE_BEST_EFFORT,
        exploration_interval=EXPLORATION_INTERVAL_R,
        cold_start_trials=COLD_START_TRIALS,
    )
    adaptive_by_trial = {(o.episode_id, o.trial_index): o for o in loop.outcomes}

    # ---- paired per-trial table + full persistence ----
    n11 = n10 = n01 = n00 = 0
    per_episode = defaultdict(
        lambda: {"trials": 0, "adaptive_success": 0, "fixed_success": 0,
                 "adaptive_calls": 0, "fixed_calls": 0}
    )
    records = []
    exposure = {p: Counter() for p in PROVIDERS}
    provider_success = Counter()
    k_by_state = defaultdict(Counter)
    success_by_k = defaultdict(lambda: [0, 0])
    success_by_state = defaultdict(lambda: [0, 0])
    calls_by_state = defaultdict(int)
    trials_by_state = Counter()
    fallback_count = exploration_count = warmup_count = 0

    for episode_id, recs in episodes.items():
        permutation = permutations[episode_id]
        provider_to_role = {v: k for k, v in permutation.items()}
        for record in sorted(recs, key=lambda r: r.trial_index):
            if not record.complete:
                continue
            key = (episode_id, record.trial_index)
            outcome = adaptive_by_trial[key]
            oracle = oracle_for(record, tau)
            fixed_success = oracle.subset_succeeds(fixed_subset)
            adaptive_success = outcome.satisfied
            state = record.hidden_state

            if adaptive_success and fixed_success:
                n11 += 1
            elif adaptive_success and not fixed_success:
                n10 += 1
            elif not adaptive_success and fixed_success:
                n01 += 1
            else:
                n00 += 1

            e = per_episode[episode_id]
            e["trials"] += 1
            e["adaptive_success"] += int(adaptive_success)
            e["fixed_success"] += int(fixed_success)
            e["adaptive_calls"] += outcome.total_calls
            e["fixed_calls"] += len(fixed_subset)

            trials_by_state[state] += 1
            calls_by_state[state] += outcome.total_calls
            k = len(outcome.selected_subset)
            k_by_state[state][k] += 1
            success_by_k[k][0] += int(adaptive_success)
            success_by_k[k][1] += 1
            success_by_state[state][0] += int(adaptive_success)
            success_by_state[state][1] += 1
            if outcome.degradation_mode == "best-effort":
                fallback_count += 1
            if outcome.status == "WARMUP":
                warmup_count += 1
            elif outcome.exploration_calls:
                exploration_count += 1

            for provider in PROVIDERS:
                role = provider_to_role[provider]
                degraded = DEGRADED_ROLES[state]
                label = "normal" if role not in degraded else state
                exposure[provider][label] += 1
                if record.within_deadline(provider, tau):
                    provider_success[provider] += 1

            records.append(
                {
                    "episode_id": episode_id, "trial_index": record.trial_index,
                    "seed": record.seed, "role_permutation": permutation,
                    "hidden_state_audit_only": state,
                    "ground_truth_within_deadline": {
                        p: record.within_deadline(p, tau) for p in PROVIDERS
                    },
                    "adaptive_selected_subset": list(outcome.selected_subset),
                    "adaptive_status": outcome.status,
                    "adaptive_degradation_mode": outcome.degradation_mode,
                    "adaptive_execution_calls": outcome.execution_calls,
                    "adaptive_exploration_calls": outcome.exploration_calls,
                    "adaptive_total_calls": outcome.total_calls,
                    "adaptive_success": adaptive_success,
                    "fixed_subset": list(fixed_subset),
                    "fixed_success": fixed_success,
                    "fixed_calls": len(fixed_subset),
                    "oracle_feasible": oracle.oracle_feasible,
                }
            )

    N = n11 + n10 + n01 + n00
    adaptive_success_rate = sum(e["adaptive_success"] for e in per_episode.values()) / N
    fixed_success_rate = sum(e["fixed_success"] for e in per_episode.values()) / N
    adaptive_calls = sum(e["adaptive_calls"] for e in per_episode.values()) / N
    fixed_calls = sum(e["fixed_calls"] for e in per_episode.values()) / N
    net_diff = (n10 - n01) / N

    boot = episode_bootstrap(per_episode, BOOTSTRAP_RESAMPLES, ANALYSIS_SEED)

    delta_success = adaptive_success_rate - fixed_success_rate
    delta_calls = adaptive_calls - fixed_calls
    lo_s, hi_s = boot["delta_success_ci95"]
    lo_c, hi_c = boot["delta_calls_ci95"]

    dominated = (
        fixed_success_rate >= adaptive_success_rate
        and fixed_calls <= adaptive_calls
        and (fixed_success_rate > adaptive_success_rate or fixed_calls < adaptive_calls)
    )
    favourable = delta_success >= 0 and delta_calls < 0
    stable = lo_s > -MATERIALITY_SUCCESS and hi_c < 0
    if dominated or (delta_success < -MATERIALITY_SUCCESS):
        verdict = "NO_GO"
    elif favourable and stable:
        verdict = "ROBUST_GO"
    else:
        verdict = "INCONCLUSIVE"

    print("\n== PAIRED OUTCOME TABLE (same trials) ==")
    print(f"  {'':22s}{'FIXED success':>15s}{'FIXED fail':>13s}")
    print(f"  {'ADAPTIVE success':22s}{n11:>15d}{n10:>13d}")
    print(f"  {'ADAPTIVE fail':22s}{n01:>15d}{n00:>13d}")
    print(f"  N = {N}   net success difference = (n10 - n01)/N = {net_diff:+.6f}")

    print("\n== PRIMARY ENDPOINTS (all logical requests) ==")
    print(f"  adaptive best-effort : success={adaptive_success_rate:.6f} calls/req={adaptive_calls:.4f}")
    print(f"  BEST_FIXED_K2 {str(fixed_subset):20s}: success={fixed_success_rate:.6f} calls/req={fixed_calls:.4f}")
    print(f"  delta success = {delta_success:+.6f}   delta calls = {delta_calls:+.6f}")
    print(f"  raw additional successes = {n10 - n01:+d}")
    print(f"  raw calls saved          = {int(round((fixed_calls - adaptive_calls) * N)):+d}")

    print("\n== EPISODE-CLUSTERED BOOTSTRAP (10k resamples of episodes) ==")
    print(f"  delta success 95% CI : [{lo_s:+.6f}, {hi_s:+.6f}]")
    print(f"  delta calls   95% CI : [{lo_c:+.6f}, {hi_c:+.6f}]")
    print(f"  P(delta success <= 0) = {boot['p_delta_success_le_0']}")
    print(f"  P(delta calls   >= 0) = {boot['p_delta_calls_ge_0']}")

    print("\n== provider-role exposure sanity ==")
    for provider in PROVIDERS:
        total_deg = sum(v for k, v in exposure[provider].items() if k != "normal")
        print(f"  {provider}: degraded={total_deg:5d} normal={exposure[provider]['normal']:5d} "
              f"success_rate={provider_success[provider]/N:.4f}")

    print("\n== adaptive mechanism (V4) ==")
    print(f"  selected-k distribution by hidden state:")
    for state in sorted(k_by_state):
        dist = {k: k_by_state[state][k] for k in sorted(k_by_state[state])}
        print(f"    {state:32s} {dist}  n={trials_by_state[state]}")
    print(f"  success by selected k: "
          f"{ {k: round(v[0]/v[1], 4) for k, v in sorted(success_by_k.items())} }")
    print(f"  success by hidden state: "
          f"{ {s: round(v[0]/v[1], 4) for s, v in sorted(success_by_state.items())} }")
    print(f"  calls/request by hidden state: "
          f"{ {s: round(calls_by_state[s]/trials_by_state[s], 3) for s in sorted(trials_by_state)} }")
    print(f"  fallback trials={fallback_count} warmup trials={warmup_count} "
          f"exploration trials={exploration_count}")

    # ---- secondary static diagnostics ----
    secondary = {}
    for subset in all_subsets():
        hits = sum(
            1 for r in records
            if any(r["ground_truth_within_deadline"][p] for p in subset)
        )
        secondary[",".join(subset)] = {
            "success": round(hits / N, 6), "calls_per_request": float(len(subset))
        }
    print("\n== secondary static diagnostics ==")
    for name, row in secondary.items():
        print(f"  fixed{{{name}}}: success={row['success']} calls={row['calls_per_request']}")

    print(f"\n  CONFIRMATORY VERDICT = {verdict}")

    report = {
        "protocol": "v4-confirmatory",
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "preserves": "V3 remains the discovery experiment; not overwritten",
        "git_commit": commit, "git_dirty": dirty,
        "frozen": {
            "policy": policy, "artifact_metadata": metadata,
            "artifact_sha256_verified": digest,
            "injection_config_hash_v3": injection_config_hash_v3(),
            "feature_schema_hash_v2": feature_schema_hash_v2(),
            "verified_no_drift": True,
        },
        "pre_declared": {
            "episodes": V4_EPISODES, "trials_per_episode": TRIALS_PER_EPISODE,
            "seed_base": V4_SEED,
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "analysis_seed": ANALYSIS_SEED,
            "materiality_success": MATERIALITY_SUCCESS,
        },
        "episode_seeds": [p.seed for p in plans],
        "episode_ids": [p.episode_id for p in plans],
        "role_permutations_audit_only": permutations,
        "paired_table": {"n11": n11, "n10": n10, "n01": n01, "n00": n00, "N": N},
        "net_success_difference": net_diff,
        "primary": {
            "adaptive_best_effort": {
                "success": adaptive_success_rate, "calls_per_request": adaptive_calls
            },
            "best_fixed_k2": {
                "subset": list(fixed_subset), "success": fixed_success_rate,
                "calls_per_request": fixed_calls
            },
            "delta_success": delta_success, "delta_calls": delta_calls,
            "raw_additional_successes": n10 - n01,
            "raw_calls_saved": int(round((fixed_calls - adaptive_calls) * N)),
        },
        "bootstrap": boot,
        "provider_exposure": {p: dict(exposure[p]) for p in PROVIDERS},
        "provider_success_rate": {p: provider_success[p] / N for p in PROVIDERS},
        "mechanism": {
            "k_by_hidden_state": {s: dict(c) for s, c in k_by_state.items()},
            "success_by_k": {k: v[0] / v[1] for k, v in success_by_k.items()},
            "success_by_hidden_state": {s: v[0] / v[1] for s, v in success_by_state.items()},
            "calls_by_hidden_state": {
                s: calls_by_state[s] / trials_by_state[s] for s in trials_by_state
            },
            "fallback_trials": fallback_count, "warmup_trials": warmup_count,
            "exploration_trials": exploration_count,
        },
        "secondary_static": secondary,
        "adaptive_decomposition": decompose(loop.outcomes),
        "estimator_metrics": evaluate_estimator(
            metadata["estimator_id"], loop.y_true, loop.y_prob
        ).to_dict(),
        "CONFIRMATORY_VERDICT": verdict,
        "per_trial_records": records,
        "disclaimer": (
            "Controlled injection on one shared host, synthetic documents. No "
            "claim about real DID reliability, latency, independence or "
            "production SLOs."
        ),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}  ({len(records)} per-trial records persisted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
