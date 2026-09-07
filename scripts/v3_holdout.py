"""V3 FINAL HOLDOUT: adaptive value GO/NO-GO. Run ONCE, clean tree.

Evaluates, on the SAME unseen V3 trials:
  * adaptive strict-slo and service-best-effort (executed closed-loop, with
    cold-start warmup and the fixed exploration schedule, both charged)
  * ALL seven static subsets, including every fixed pair
  * BEST_FIXED_K2, frozen from TRAIN+VALIDATION before this run

Primary comparison (§13): ADAPTIVE BEST-EFFORT vs BEST_FIXED_K2, success over
ALL logical requests against mean total provider calls per request. Per-trial
records ARE persisted this time -- V1 and V2 both stored aggregates only,
which made their static-subset audits unrecoverable.

GO/NO-GO rule, frozen before this run: GO if adaptive is not Pareto-dominated
by BEST_FIXED_K2 and shows a material cost-success advantage; otherwise NO-GO.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from itertools import combinations
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.inventory import ProviderEntry  # noqa: E402
from avdr.learning.closedloop import (  # noqa: E402
    COLD_START_TRIALS,
    EXPLORATION_INTERVAL_R,
    SERVICE_BEST_EFFORT,
    STRICT_SLO,
)
from avdr.learning.dataset import ADMIN_URLS, all_subsets  # noqa: E402
from avdr.learning.environment import PROVIDERS  # noqa: E402
from avdr.learning.estimators import load_frozen  # noqa: E402
from avdr.learning.metrics import evaluate_estimator  # noqa: E402
from avdr.learning.oracle import ClosedLoopOutcome, decompose  # noqa: E402
from avdr.learning.runner_v2 import oracle_for, run_closed_loop  # noqa: E402
from avdr.learning.v3 import injection_config_hash_v3, plan_episodes_v3  # noqa: E402
from avdr.provenance import resolve_git_commit, resolve_git_dirty  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from v3_pipeline import collect, provider_entries  # noqa: E402

# [DESIGN CHOICE] unseen V3 holdout seeds; disjoint from V1/V2 and V3 train/val.
HOLDOUT_EPISODES, HOLDOUT_SEED = 12, 1_300_000
MATERIAL_SUCCESS_DELTA = 0.02   # frozen GO/NO-GO thresholds
MATERIAL_COST_DELTA = 0.20


def static_outcomes(episodes, tau_ms, subset):
    out = []
    for episode_id, records in episodes.items():
        for record in sorted(records, key=lambda r: r.trial_index):
            if not record.complete:
                continue
            oracle = oracle_for(record, tau_ms)
            out.append(
                ClosedLoopOutcome(
                    episode_id=episode_id, trial_index=record.trial_index,
                    committed=True, selected_subset=tuple(sorted(subset)),
                    satisfied=oracle.subset_succeeds(subset), status="SELECTED",
                    oracle_feasible=oracle.oracle_feasible,
                    execution_calls=len(subset),
                )
            )
    return out


def pareto(points: dict[str, tuple[float, float]]) -> dict:
    """points: name -> (success, cost). A dominates B if >= success and <= cost."""
    dominated = {}
    for a, (sa, ca) in points.items():
        for b, (sb, cb) in points.items():
            if a == b:
                continue
            if sa >= sb and ca <= cb and (sa > sb or ca < cb):
                dominated.setdefault(b, []).append(a)
    frontier = [n for n in points if n not in dominated]
    return {
        "frontier": sorted(frontier, key=lambda n: points[n][1]),
        "dominated": {k: sorted(v) for k, v in dominated.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", default=str(REPO_ROOT / "frozen" / "frozen_estimator_v3.pkl"))
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "learning_v3" / "v3_holdout_report.json"))
    args = parser.parse_args()

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)
    policy = json.loads((REPO_ROOT / "frozen" / "v3_policy.json").read_text(encoding="utf-8"))
    estimator, metadata, digest = load_frozen(Path(args.artifact))
    tau = metadata["deadline_tau_ms"]
    target = policy["target_slo_probability"]
    best_k2 = tuple(policy["BEST_FIXED_K2"].split(","))

    print("V3 FINAL HOLDOUT -- ADAPTIVE VALUE GO/NO-GO")
    print(f"  git_commit={commit} git_dirty={dirty}")
    print(f"  frozen estimator = {metadata['estimator_id']} calibration={metadata['calibration']}")
    print(f"  artifact_sha256 verified = {digest}")
    print(f"  BEST_FIXED_K2 (frozen pre-holdout) = {best_k2}")
    print(f"  tau={tau} target={target} R={EXPLORATION_INTERVAL_R} "
          f"cold_start={COLD_START_TRIALS}\n")

    entries = provider_entries()
    with httpx.Client() as admin:
        for url in ADMIN_URLS.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

    print(f"== collecting holdout ground truth ({HOLDOUT_EPISODES} episodes) ==")
    plans, episodes = asyncio.run(
        collect("v3holdout", HOLDOUT_EPISODES, HOLDOUT_SEED, entries)
    )

    # ---- persist per-trial ground truth (V1/V2 failed to do this) ----
    per_trial = [
        {
            "episode_id": ep, "trial_index": r.trial_index,
            "hidden_state_audit_only": r.hidden_state,
            "within_deadline": {p: r.within_deadline(p, tau) for p in PROVIDERS},
            "complete": r.complete,
        }
        for ep, recs in episodes.items() for r in sorted(recs, key=lambda x: x.trial_index)
    ]

    results = {}
    for mode in (STRICT_SLO, SERVICE_BEST_EFFORT):
        loop = run_closed_loop(
            estimator, target, tau, episodes, mode=mode,
            exploration_interval=EXPLORATION_INTERVAL_R,
            cold_start_trials=COLD_START_TRIALS,
            collect_traces=2 if mode == SERVICE_BEST_EFFORT else 0,
        )
        m = evaluate_estimator(metadata["estimator_id"], loop.y_true, loop.y_prob)
        results[mode] = {
            "estimator_metrics": {**m.to_dict(), "reliability": m.reliability_table},
            "decision": decompose(loop.outcomes),
            "traces": loop.traces,
        }

    subsets = all_subsets()
    static = {
        ",".join(s): decompose(static_outcomes(episodes, tau, s)) for s in subsets
    }

    print("== estimator quality ==")
    em = results[STRICT_SLO]["estimator_metrics"]
    print(f"  n={em['n']} brier={em['brier']} logloss={em['log_loss']} ece={em['ece']}")

    print("\n== ALL static subsets + adaptive (denominator = ALL trials) ==")
    print(f"  {'policy':34s} {'success':>9s} {'calls/req':>10s}")
    points = {}
    for name, d in static.items():
        points[f"fixed{{{name}}}"] = (d["success_over_all_trials"], d["mean_total_calls"])
        print(f"  {'fixed{' + name + '}':34s} {d['success_over_all_trials']:>9} "
              f"{d['mean_total_calls']:>10}")
    for mode in (STRICT_SLO, SERVICE_BEST_EFFORT):
        d = results[mode]["decision"]
        points[f"adaptive[{mode}]"] = (
            d["success_over_all_trials"], d["mean_total_calls"]
        )
        print(f"  {'adaptive[' + mode + ']':34s} {d['success_over_all_trials']:>9} "
              f"{d['mean_total_calls']:>10}")

    frontier = pareto(points)
    print("\n== Pareto frontier (success vs calls/request) ==")
    print(f"  frontier : {frontier['frontier']}")
    for name, by in frontier["dominated"].items():
        print(f"  DOMINATED: {name}  <- by {by}")

    # ---- GO / NO-GO, rule frozen before this run ----
    adaptive_key = f"adaptive[{SERVICE_BEST_EFFORT}]"
    k2_key = f"fixed{{{','.join(best_k2)}}}"
    a_succ, a_cost = points[adaptive_key]
    k_succ, k_cost = points[k2_key]
    dominated_by_k2 = (
        k_succ >= a_succ and k_cost <= a_cost and (k_succ > a_succ or k_cost < a_cost)
    )
    material = (
        (a_succ - k_succ) >= MATERIAL_SUCCESS_DELTA and a_cost <= k_cost + 1e-9
    ) or ((k_cost - a_cost) >= MATERIAL_COST_DELTA and a_succ >= k_succ - 1e-9)
    verdict = "GO" if (not dominated_by_k2 and material) else "NO-GO"

    print(f"\n== PRIMARY COMPARISON (§13) ==")
    print(f"  adaptive best-effort : success={a_succ}  calls/req={a_cost}")
    print(f"  BEST_FIXED_K2 {str(best_k2):18s}: success={k_succ}  calls/req={k_cost}")
    print(f"  delta success = {a_succ - k_succ:+.6f}   delta cost = {a_cost - k_cost:+.6f}")
    print(f"  Pareto-dominated by BEST_FIXED_K2: {dominated_by_k2}")
    print(f"\n  ADAPTIVE VALUE VERDICT = {verdict}")

    report = {
        "protocol": "symmetric-generator-v3",
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "git_commit": commit, "git_dirty": dirty,
        "frozen_artifact": metadata, "artifact_sha256_verified": digest,
        "frozen_policy": policy,
        "injection_config_hash_v3": injection_config_hash_v3(),
        "holdout_episodes": [p.episode_id for p in plans],
        "holdout_seeds": [p.seed for p in plans],
        "role_permutations_audit_only": {p.episode_id: p.permutation for p in plans},
        "modes": results,
        "static_subsets": static,
        "pareto": {"points": {k: list(v) for k, v in points.items()}, **frontier},
        "primary_comparison": {
            "adaptive_best_effort": {"success": a_succ, "calls_per_request": a_cost},
            "best_fixed_k2": {"subset": list(best_k2), "success": k_succ,
                              "calls_per_request": k_cost},
            "delta_success": a_succ - k_succ,
            "delta_calls": a_cost - k_cost,
            "pareto_dominated_by_best_fixed_k2": dominated_by_k2,
            "material_thresholds": {
                "success_delta": MATERIAL_SUCCESS_DELTA,
                "cost_delta": MATERIAL_COST_DELTA,
            },
        },
        "ADAPTIVE_VALUE_VERDICT": verdict,
        "per_trial_ground_truth": per_trial,
        "disclaimer": (
            "Controlled injection on one shared host, synthetic documents. No "
            "claim about real DID reliability, latency, independence or "
            "production SLOs."
        ),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
