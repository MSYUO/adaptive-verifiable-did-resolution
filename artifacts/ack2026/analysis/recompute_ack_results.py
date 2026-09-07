"""Single source of truth for every number in the ACK 2026 evidence package.

Recomputes all paper quantities from the AUTHORITATIVE raw V4 per-trial
records, cross-checks them against the aggregates the V4 run itself reported,
and emits the data/, tables/ and figures/source/ CSVs.

Nothing is hand-typed. Every table cell and every figure point originates
here. If a recomputed value disagrees with the value stored by the V4 run,
the disagreement is recorded in validation_report.json and the RECOMPUTED
value is what the package publishes.

This script performs NO experiment: it never contacts a provider, never
retrains, never resamples trials from the generator. It reads frozen evidence
and derives tables.

Usage:
    python artifacts/ack2026/analysis/recompute_ack_results.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE.parent.parent

# Authoritative raw evidence (untracked experiment output; copied into the
# package as CSV so the committed artifact is self-contained).
V4_RAW = REPO_ROOT / "artifacts" / "learning_v4" / "v4_confirmatory_report.json"
V3_HOLDOUT = REPO_ROOT / "artifacts" / "learning_v3" / "v3_holdout_report.json"
V3_PIPELINE = REPO_ROOT / "artifacts" / "learning_v3" / "v3_pipeline_report.json"
V3_POLICY = REPO_ROOT / "frozen" / "v3_policy.json"
V3_ARTIFACT_META = REPO_ROOT / "frozen" / "frozen_estimator_v3.json"

PROVIDERS = ("local-a", "local-b", "local-c")

# Frozen analysis parameters. Identical to the V4 run; NOT re-chosen here.
BOOTSTRAP_RESAMPLES = 10_000
ANALYSIS_SEED = 20260909
MATERIALITY_SUCCESS = 0.02


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def episode_bootstrap(per_episode: dict, resamples: int, seed: int) -> dict:
    """Paired bootstrap over EPISODES (clusters). Never over trial rows.

    Trials inside an episode share a hidden-state sequence and a provider-role
    permutation, so they are correlated; resampling rows would understate
    uncertainty.
    """
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
        return [
            round(values[int(0.025 * len(values))], 6),
            round(values[int(0.975 * len(values)) - 1], 6),
        ]

    return {
        "resamples": resamples,
        "analysis_seed": seed,
        "resampling_unit": "episode (cluster)",
        "note": (
            "Tail proportions below are bootstrap resample fractions. They "
            "are NOT p-values and no null hypothesis test is implied."
        ),
        "delta_success_point": None,  # filled by caller
        "delta_success_ci95": ci(deltas_success),
        "delta_calls_ci95": ci(deltas_calls),
        "delta_success_bootstrap_mean": round(sum(deltas_success) / len(deltas_success), 6),
        "delta_calls_bootstrap_mean": round(sum(deltas_calls) / len(deltas_calls), 6),
        "resample_fraction_delta_success_le_0": round(
            sum(1 for d in deltas_success if d <= 0) / len(deltas_success), 6
        ),
        "resample_fraction_delta_calls_ge_0": round(
            sum(1 for d in deltas_calls if d >= 0) / len(deltas_calls), 6
        ),
    }


def main() -> int:
    if not V4_RAW.exists():
        print("ACK_EVIDENCE_FREEZE_BLOCKED: V4 raw per-trial evidence not found")
        return 2

    raw = json.loads(V4_RAW.read_text(encoding="utf-8"))
    records = raw["per_trial_records"]
    policy = json.loads(V3_POLICY.read_text(encoding="utf-8"))
    meta = json.loads(V3_ARTIFACT_META.read_text(encoding="utf-8"))
    fixed_subset = tuple(policy["BEST_FIXED_K2"].split(","))

    violations: list[str] = []

    # ================= recompute from raw records only =================
    N = len(records)
    keys = {(r["episode_id"], r["trial_index"]) for r in records}
    if len(keys) != N:
        violations.append(f"duplicate (episode_id, trial_index): {N - len(keys)}")

    n11 = n10 = n01 = n00 = 0
    adaptive_success = fixed_success = 0
    adaptive_calls = fixed_calls = 0
    exec_calls = explore_calls = 0
    warmup_calls = fallback_calls = 0
    per_episode = defaultdict(
        lambda: {"trials": 0, "adaptive_success": 0, "fixed_success": 0,
                 "adaptive_calls": 0, "fixed_calls": 0}
    )
    k_by_state = defaultdict(Counter)
    success_by_k = defaultdict(lambda: [0, 0])
    success_by_state = defaultdict(lambda: [0, 0])
    calls_by_state = Counter()
    trials_by_state = Counter()
    subsets = [
        tuple(c) for size in range(1, 4) for c in combinations(sorted(PROVIDERS), size)
    ]
    static_hits = Counter()
    oracle_feasible = 0

    for r in records:
        a, f = bool(r["adaptive_success"]), bool(r["fixed_success"])
        adaptive_success += a
        fixed_success += f
        if a and f:
            n11 += 1
        elif a and not f:
            n10 += 1
        elif f:
            n01 += 1
        else:
            n00 += 1

        ec, xc, tc = (
            r["adaptive_execution_calls"], r["adaptive_exploration_calls"],
            r["adaptive_total_calls"],
        )
        if min(ec, xc, tc, r["fixed_calls"]) < 0:
            violations.append(f"negative call count at {r['episode_id']}/{r['trial_index']}")
        if ec + xc != tc:
            violations.append(
                f"call reconciliation failed at {r['episode_id']}/{r['trial_index']}: "
                f"{ec}+{xc} != {tc}"
            )
        adaptive_calls += tc
        fixed_calls += r["fixed_calls"]
        exec_calls += ec
        explore_calls += xc
        if r["adaptive_status"] == "WARMUP":
            warmup_calls += xc
        if r["adaptive_degradation_mode"] == "best-effort":
            fallback_calls += ec

        e = per_episode[r["episode_id"]]
        e["trials"] += 1
        e["adaptive_success"] += int(a)
        e["fixed_success"] += int(f)
        e["adaptive_calls"] += tc
        e["fixed_calls"] += r["fixed_calls"]

        state = r["hidden_state_audit_only"]
        k = len(r["adaptive_selected_subset"])
        trials_by_state[state] += 1
        calls_by_state[state] += tc
        k_by_state[state][k] += 1
        success_by_k[k][0] += int(a)
        success_by_k[k][1] += 1
        success_by_state[state][0] += int(a)
        success_by_state[state][1] += 1

        gt = r["ground_truth_within_deadline"]
        for s in subsets:
            if any(gt[p] for p in s):
                static_hits[s] += 1
        if any(gt.values()):
            oracle_feasible += 1
        for field in ("episode_id", "trial_index", "adaptive_success",
                      "fixed_success", "adaptive_total_calls"):
            if r.get(field) is None:
                violations.append(f"missing field {field}")

    # ---- reconciliation ----
    if n11 + n10 + n01 + n00 != N:
        violations.append("paired table does not sum to N")
    if n11 + n10 != adaptive_success:
        violations.append("adaptive marginal does not reconcile with paired table")
    if n11 + n01 != fixed_success:
        violations.append("fixed marginal does not reconcile with paired table")
    if fixed_calls != N * len(fixed_subset):
        violations.append("fixed calls do not equal N * |fixed subset|")
    if exec_calls + explore_calls != adaptive_calls:
        violations.append("adaptive cost components do not sum to total")
    if set(per_episode) != {r["episode_id"] for r in records}:
        violations.append("bootstrap episode set differs from persisted episodes")

    adaptive_rate = adaptive_success / N
    fixed_rate = fixed_success / N
    adaptive_cpr = adaptive_calls / N
    fixed_cpr = fixed_calls / N
    delta_success = adaptive_rate - fixed_rate
    delta_calls = adaptive_cpr - fixed_cpr
    net_diff = (n10 - n01) / N

    boot = episode_bootstrap(per_episode, BOOTSTRAP_RESAMPLES, ANALYSIS_SEED)
    boot["delta_success_point"] = round(delta_success, 6)
    boot["delta_calls_point"] = round(delta_calls, 6)
    boot["materiality_reference_success"] = MATERIALITY_SUCCESS

    # ---- cross-check against what the V4 run itself recorded ----
    stored = raw["primary"]
    stored_table = raw["paired_table"]
    crosscheck = {}
    for label, mine, theirs in (
        ("N", N, stored_table["N"]),
        ("n11", n11, stored_table["n11"]), ("n10", n10, stored_table["n10"]),
        ("n01", n01, stored_table["n01"]), ("n00", n00, stored_table["n00"]),
        ("adaptive_success_rate", adaptive_rate, stored["adaptive_best_effort"]["success"]),
        ("adaptive_calls_per_request", adaptive_cpr,
         stored["adaptive_best_effort"]["calls_per_request"]),
        ("fixed_success_rate", fixed_rate, stored["best_fixed_k2"]["success"]),
        ("fixed_calls_per_request", fixed_cpr, stored["best_fixed_k2"]["calls_per_request"]),
        ("delta_success", delta_success, stored["delta_success"]),
        ("delta_calls", delta_calls, stored["delta_calls"]),
    ):
        agree = abs(float(mine) - float(theirs)) < 1e-9
        crosscheck[label] = {
            "recomputed": mine, "stored_by_v4_run": theirs, "agree": agree
        }
        if not agree:
            violations.append(f"crosscheck mismatch on {label}: {mine} vs {theirs}")

    # ================= emit data / tables / figure sources =================
    data, tables, figsrc = PACKAGE / "data", PACKAGE / "tables", PACKAGE / "figures" / "source"

    write_csv(
        data / "v4_trials.csv",
        ["episode_id", "trial_index", "seed", "role_permutation",
         "hidden_state_controlled_injection", "gt_local_a", "gt_local_b", "gt_local_c",
         "adaptive_selected_subset", "adaptive_selected_k", "adaptive_status",
         "adaptive_degradation_mode", "adaptive_execution_calls",
         "adaptive_exploration_calls", "adaptive_total_calls", "adaptive_success",
         "fixed_subset", "fixed_calls", "fixed_success", "oracle_feasible"],
        [
            {
                "episode_id": r["episode_id"], "trial_index": r["trial_index"],
                "seed": r["seed"],
                "role_permutation": ";".join(f"{k}={v}" for k, v in sorted(r["role_permutation"].items())),
                "hidden_state_controlled_injection": r["hidden_state_audit_only"],
                "gt_local_a": int(r["ground_truth_within_deadline"]["local-a"]),
                "gt_local_b": int(r["ground_truth_within_deadline"]["local-b"]),
                "gt_local_c": int(r["ground_truth_within_deadline"]["local-c"]),
                "adaptive_selected_subset": "|".join(r["adaptive_selected_subset"]),
                "adaptive_selected_k": len(r["adaptive_selected_subset"]),
                "adaptive_status": r["adaptive_status"],
                "adaptive_degradation_mode": r["adaptive_degradation_mode"] or "",
                "adaptive_execution_calls": r["adaptive_execution_calls"],
                "adaptive_exploration_calls": r["adaptive_exploration_calls"],
                "adaptive_total_calls": r["adaptive_total_calls"],
                "adaptive_success": int(r["adaptive_success"]),
                "fixed_subset": "|".join(r["fixed_subset"]),
                "fixed_calls": r["fixed_calls"],
                "fixed_success": int(r["fixed_success"]),
                "oracle_feasible": int(r["oracle_feasible"]),
            }
            for r in records
        ],
    )

    write_csv(
        data / "v4_episode_metrics.csv",
        ["episode_id", "trials", "adaptive_success", "fixed_success",
         "adaptive_calls", "fixed_calls", "episode_delta_success", "episode_delta_calls"],
        [
            {
                "episode_id": e, "trials": v["trials"],
                "adaptive_success": v["adaptive_success"],
                "fixed_success": v["fixed_success"],
                "adaptive_calls": v["adaptive_calls"], "fixed_calls": v["fixed_calls"],
                "episode_delta_success": round(
                    (v["adaptive_success"] - v["fixed_success"]) / v["trials"], 6
                ),
                "episode_delta_calls": round(
                    (v["adaptive_calls"] - v["fixed_calls"]) / v["trials"], 6
                ),
            }
            for e, v in sorted(per_episode.items())
        ],
    )

    write_csv(
        data / "v4_paired_outcomes.csv",
        ["cell", "meaning", "count"],
        [
            {"cell": "n11", "meaning": "adaptive success AND fixed success", "count": n11},
            {"cell": "n10", "meaning": "adaptive success AND fixed fail", "count": n10},
            {"cell": "n01", "meaning": "adaptive fail AND fixed success", "count": n01},
            {"cell": "n00", "meaning": "adaptive fail AND fixed fail", "count": n00},
            {"cell": "N", "meaning": "total paired logical trials", "count": N},
        ],
    )

    # ---- Table 1: primary results ----
    policies = [
        ("fixed{local-a}", static_hits[("local-a",)], 1.0),
        ("fixed{local-b}", static_hits[("local-b",)], 1.0),
        ("fixed{local-c}", static_hits[("local-c",)], 1.0),
        ("fixed{local-a,local-b}", static_hits[("local-a", "local-b")], 2.0),
        ("fixed{local-a,local-c}", static_hits[("local-a", "local-c")], 2.0),
        ("BEST_FIXED_K2{local-b,local-c}", static_hits[("local-b", "local-c")], 2.0),
        ("all-race{local-a,local-b,local-c}", static_hits[tuple(sorted(PROVIDERS))], 3.0),
    ]
    table1 = [
        {
            "policy": name, "success_count": hits, "success_rate": round(hits / N, 6),
            "mean_calls_per_request": cpr,
            "delta_success_vs_best_fixed_k2": round(hits / N - fixed_rate, 6),
            "delta_calls_vs_best_fixed_k2": round(cpr - fixed_cpr, 6),
        }
        for name, hits, cpr in policies
    ]
    table1.append(
        {
            "policy": "adaptive-best-effort (frozen V3 policy)",
            "success_count": adaptive_success, "success_rate": round(adaptive_rate, 6),
            "mean_calls_per_request": round(adaptive_cpr, 6),
            "delta_success_vs_best_fixed_k2": round(delta_success, 6),
            "delta_calls_vs_best_fixed_k2": round(delta_calls, 6),
        }
    )
    write_csv(
        tables / "table_primary_results.csv",
        ["policy", "success_count", "success_rate", "mean_calls_per_request",
         "delta_success_vs_best_fixed_k2", "delta_calls_vs_best_fixed_k2"],
        table1,
    )

    write_csv(
        tables / "table_paired_outcomes.csv",
        ["quantity", "value", "note"],
        [
            {"quantity": "n11", "value": n11, "note": "adaptive success, fixed success"},
            {"quantity": "n10", "value": n10, "note": "adaptive success, fixed fail"},
            {"quantity": "n01", "value": n01, "note": "adaptive fail, fixed success"},
            {"quantity": "n00", "value": n00, "note": "adaptive fail, fixed fail"},
            {"quantity": "N", "value": N, "note": "paired logical trials"},
            {"quantity": "net_success_difference", "value": round(net_diff, 6),
             "note": "(n10 - n01)/N"},
            {"quantity": "delta_success", "value": round(delta_success, 6),
             "note": "adaptive - BEST_FIXED_K2, all logical requests"},
            {"quantity": "delta_calls", "value": round(delta_calls, 6),
             "note": "adaptive - BEST_FIXED_K2, calls per request"},
            {"quantity": "delta_success_ci95_low", "value": boot["delta_success_ci95"][0],
             "note": "episode-cluster bootstrap"},
            {"quantity": "delta_success_ci95_high", "value": boot["delta_success_ci95"][1],
             "note": "episode-cluster bootstrap"},
            {"quantity": "delta_calls_ci95_low", "value": boot["delta_calls_ci95"][0],
             "note": "episode-cluster bootstrap"},
            {"quantity": "delta_calls_ci95_high", "value": boot["delta_calls_ci95"][1],
             "note": "episode-cluster bootstrap"},
            {"quantity": "bootstrap_unit", "value": "episode",
             "note": "clusters resampled, NOT individual trial rows"},
            {"quantity": "bootstrap_resamples", "value": BOOTSTRAP_RESAMPLES, "note": ""},
            {"quantity": "raw_additional_successes", "value": n10 - n01,
             "note": "adaptive minus fixed, absolute trials"},
            {"quantity": "raw_calls_saved", "value": fixed_calls - adaptive_calls,
             "note": "absolute provider calls"},
        ],
    )

    write_csv(
        tables / "table_static_baselines.csv",
        ["subset", "size", "success_count", "success_rate", "calls_per_request"],
        [
            {"subset": ",".join(s), "size": len(s), "success_count": static_hits[s],
             "success_rate": round(static_hits[s] / N, 6),
             "calls_per_request": float(len(s))}
            for s in subsets
        ],
    )

    # ---- Table 3: estimator selection (V3 development stage) ----
    v3_pipeline = json.loads(V3_PIPELINE.read_text(encoding="utf-8"))
    write_csv(
        tables / "table_estimator_results.csv",
        ["estimator", "validation_brier", "validation_log_loss", "validation_ece",
         "commit_rate", "frozen_for_v3_v4"],
        [
            {
                "estimator": name, "validation_brier": m["brier"],
                "validation_log_loss": m["log_loss"], "validation_ece": m["ece"],
                "commit_rate": m.get("commit_rate"),
                "frozen_for_v3_v4": name == v3_pipeline["selected_family"],
            }
            for name, m in v3_pipeline["validation_metrics"].items()
        ],
    )
    write_csv(
        data / "estimator_summary.csv",
        ["field", "value"],
        [
            {"field": "frozen_estimator", "value": meta["estimator_id"]},
            {"field": "calibration", "value": str(meta["calibration"])},
            # The V3 report does not carry a prose selection-rule string; the
            # rule lives in scripts/v3_pipeline.py. Rather than hand-typing it,
            # the observable consequence is recorded: which estimator had the
            # best validation Brier, and which was actually frozen.
            {"field": "selection_rule_source", "value": "scripts/v3_pipeline.py"},
            {"field": "best_by_validation_brier",
             "value": min(v3_pipeline["validation_metrics"],
                          key=lambda k: v3_pipeline["validation_metrics"][k]["brier"])},
            {"field": "selected_family", "value": v3_pipeline["selected_family"]},
            {"field": "selected_variant", "value": v3_pipeline["selected_variant"]},
            {"field": "v4_holdout_brier", "value": raw["estimator_metrics"]["brier"]},
            {"field": "v4_holdout_ece", "value": raw["estimator_metrics"]["ece"]},
        ],
    )

    # ---- V3 summary (discovery stage; NOT pooled with V4) ----
    v3 = json.loads(V3_HOLDOUT.read_text(encoding="utf-8"))
    v3p = v3["primary_comparison"]
    write_csv(
        data / "v3_summary.csv",
        ["field", "value"],
        [
            {"field": "role", "value": "discovery / policy development (NOT confirmatory)"},
            {"field": "trials", "value": v3["static_subsets"]["local-a"]["n"]},
            {"field": "adaptive_success", "value": v3p["adaptive_best_effort"]["success"]},
            {"field": "adaptive_calls_per_request",
             "value": v3p["adaptive_best_effort"]["calls_per_request"]},
            {"field": "best_fixed_k2", "value": ",".join(v3p["best_fixed_k2"]["subset"])},
            {"field": "best_fixed_k2_success", "value": v3p["best_fixed_k2"]["success"]},
            {"field": "best_fixed_k2_calls_per_request",
             "value": v3p["best_fixed_k2"]["calls_per_request"]},
            {"field": "delta_success", "value": v3p["delta_success"]},
            {"field": "delta_calls", "value": v3p["delta_calls"]},
            {"field": "verdict", "value": v3["ADAPTIVE_VALUE_VERDICT"]},
        ],
    )

    # ---- figure sources ----
    write_csv(
        figsrc / "fig2_success_vs_calls.csv",
        ["policy", "success_rate", "calls_per_request", "highlight"],
        [
            {"policy": name, "success_rate": round(hits / N, 6),
             "calls_per_request": cpr,
             "highlight": "frozen_comparator" if "BEST_FIXED_K2" in name else ""}
            for name, hits, cpr in policies
        ]
        + [
            {"policy": "adaptive-best-effort", "success_rate": round(adaptive_rate, 6),
             "calls_per_request": round(adaptive_cpr, 6), "highlight": "adaptive"}
        ],
    )

    states = sorted(k_by_state)
    write_csv(
        figsrc / "fig3_selected_k_by_state.csv",
        ["controlled_injection_state", "k1", "k2", "k3", "trials",
         "success_rate", "calls_per_request"],
        [
            {
                "controlled_injection_state": s,
                "k1": k_by_state[s][1], "k2": k_by_state[s][2], "k3": k_by_state[s][3],
                "trials": trials_by_state[s],
                "success_rate": round(success_by_state[s][0] / success_by_state[s][1], 6),
                "calls_per_request": round(calls_by_state[s] / trials_by_state[s], 6),
            }
            for s in states
        ],
    )

    # ---- bootstrap + validation reports ----
    (PACKAGE / "analysis" / "bootstrap_summary.json").write_text(
        json.dumps(boot, indent=2), encoding="utf-8"
    )

    validation = {
        "status": "PASS" if not violations else "FAIL",
        "recomputed_from": str(V4_RAW.relative_to(REPO_ROOT)).replace("\\", "/"),
        "raw_evidence_sha256": sha256_file(V4_RAW),
        "trial_count": N,
        "episode_count": len(per_episode),
        "unique_primary_keys": len(keys),
        "duplicate_primary_keys": N - len(keys),
        "paired_table_sums_to_N": n11 + n10 + n01 + n00 == N,
        "adaptive_marginal_reconciles": n11 + n10 == adaptive_success,
        "fixed_marginal_reconciles": n11 + n01 == fixed_success,
        "cost_components_reconcile": exec_calls + explore_calls == adaptive_calls,
        "cost_component_breakdown": {
            "execution_calls": exec_calls,
            "exploration_calls_including_warmup": explore_calls,
            "of_which_warmup_calls": warmup_calls,
            "of_which_scheduled_exploration_calls": explore_calls - warmup_calls,
            "fallback_calls_subset_of_execution": fallback_calls,
            "total": adaptive_calls,
            "semantics": (
                "total = execution + exploration. Warmup calls are recorded in "
                "the exploration channel; fallback (best-effort) calls are "
                "recorded in the execution channel, so they are components of "
                "those two totals rather than a third addend."
            ),
        },
        "fixed_calls_equal_N_times_subset_size": fixed_calls == N * len(fixed_subset),
        "bootstrap_episode_set_matches_records": set(per_episode)
        == {r["episode_id"] for r in records},
        "crosscheck_vs_v4_run": crosscheck,
        "violations": violations,
        "oracle_feasible_count": oracle_feasible,
    }
    (PACKAGE / "analysis" / "validation_report.json").write_text(
        json.dumps(validation, indent=2), encoding="utf-8"
    )

    # ================= console summary =================
    print("ACK 2026 EVIDENCE RECOMPUTATION  [recomputed from raw V4 records]")
    print(f"  raw evidence  : {V4_RAW.relative_to(REPO_ROOT)}")
    print(f"  sha256        : {validation['raw_evidence_sha256']}")
    print(f"  N             : {N} trials across {len(per_episode)} episodes\n")
    print(f"  adaptive best-effort : success={adaptive_rate:.6f} "
          f"({adaptive_success}/{N})  calls/req={adaptive_cpr:.6f}")
    print(f"  BEST_FIXED_K2 {','.join(fixed_subset):22s}: success={fixed_rate:.6f} "
          f"({fixed_success}/{N})  calls/req={fixed_cpr:.6f}")
    print(f"  delta success = {delta_success:+.6f}   delta calls = {delta_calls:+.6f}")
    print(f"  paired: n11={n11} n10={n10} n01={n01} n00={n00}  "
          f"net=(n10-n01)/N={net_diff:+.6f}")
    print(f"  raw additional successes = {n10 - n01:+d}   "
          f"raw calls saved = {fixed_calls - adaptive_calls:+d}")
    print(f"  bootstrap (episode unit, {BOOTSTRAP_RESAMPLES} resamples, seed {ANALYSIS_SEED}):")
    print(f"    delta success 95% CI = {boot['delta_success_ci95']}")
    print(f"    delta calls   95% CI = {boot['delta_calls_ci95']}")
    print(f"\n  crosscheck vs V4 run: "
          f"{sum(1 for v in crosscheck.values() if v['agree'])}/{len(crosscheck)} agree")
    print(f"  VALIDATION: {validation['status']}"
          + (f"  violations={violations}" if violations else ""))
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
