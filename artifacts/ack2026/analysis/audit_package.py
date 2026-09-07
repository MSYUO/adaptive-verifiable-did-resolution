"""Independent data-quality audit of the ACK package.

Deliberately re-derives everything from the PACKAGE's own CSVs rather than the
raw run report, so it checks the published artifact rather than the pipeline
that produced it. Exit code 1 means the package must not be committed.

Tolerance note: tables/ and figures/source/ store values rounded to six
decimal places, so comparisons against them use a 5e-7 tolerance (the half-ulp
of that rounding). Counts are compared exactly.

Usage:
    cd artifacts/ack2026 && python analysis/audit_package.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import sys
from collections import Counter

PACKAGE = pathlib.Path(__file__).resolve().parent.parent
ROUND_TOL = 5e-7          # half-ulp of 6-decimal rounding
COUNT_EXACT = 0


def read(rel: str) -> list[dict]:
    with (PACKAGE / rel).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    trials = read("data/v4_trials.csv")
    episodes = read("data/v4_episode_metrics.csv")
    paired = {r["cell"]: int(r["count"]) for r in read("data/v4_paired_outcomes.csv")}
    table1 = read("tables/table_primary_results.csv")
    table2 = {r["quantity"]: r["value"] for r in read("tables/table_paired_outcomes.csv")}
    static = read("tables/table_static_baselines.csv")
    fig2 = read("figures/source/fig2_success_vs_calls.csv")
    fig3 = read("figures/source/fig3_selected_k_by_state.csv")
    validation = json.loads(
        (PACKAGE / "analysis" / "validation_report.json").read_text(encoding="utf-8")
    )

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    n = len(trials)
    check("V4 trial rows = 2640", n == 2640, str(n))
    keys = {(r["episode_id"], r["trial_index"]) for r in trials}
    check("duplicate primary keys = 0", n - len(keys) == COUNT_EXACT, f"{n - len(keys)}")
    check("episode ids present and = 120", len({r["episode_id"] for r in trials}) == 120)
    check("trial indices in range", all(0 <= int(r["trial_index"]) < 22 for r in trials))
    check(
        "no missing required outcome fields",
        all(r["adaptive_success"] and r["fixed_success"] and r["adaptive_total_calls"]
            for r in trials),
    )

    n11 = n10 = n01 = n00 = 0
    a_s = f_s = a_c = f_c = execution = exploration = 0
    for r in trials:
        a, f = int(r["adaptive_success"]), int(r["fixed_success"])
        a_s += a
        f_s += f
        n11 += a and f
        n10 += a and not f
        n01 += (not a) and f
        n00 += (not a) and (not f)
        a_c += int(r["adaptive_total_calls"])
        f_c += int(r["fixed_calls"])
        execution += int(r["adaptive_execution_calls"])
        exploration += int(r["adaptive_exploration_calls"])

    check("paired cells sum to N", n11 + n10 + n01 + n00 == n)
    check(
        "paired cells match data/v4_paired_outcomes.csv",
        (n11, n10, n01, n00, n)
        == (paired["n11"], paired["n10"], paired["n01"], paired["n00"], paired["N"]),
    )
    check("adaptive marginal reconciles", n11 + n10 == a_s, f"{n11 + n10} vs {a_s}")
    check("fixed marginal reconciles", n11 + n01 == f_s, f"{n11 + n01} vs {f_s}")
    check(
        "all call counts non-negative",
        all(
            int(r[k]) >= 0
            for r in trials
            for k in ("adaptive_execution_calls", "adaptive_exploration_calls",
                      "adaptive_total_calls", "fixed_calls")
        ),
    )
    check(
        "adaptive cost reconciles (execution + exploration = total)",
        execution + exploration == a_c, f"{execution}+{exploration}={a_c}",
    )
    check("fixed calls = N x |{b,c}|", f_c == 2 * n)

    per_episode = Counter(r["episode_id"] for r in trials)
    check(
        "episode metrics reconcile with trials",
        len(episodes) == 120
        and all(int(e["trials"]) == per_episode[e["episode_id"]] for e in episodes),
    )
    check(
        "bootstrap episode set == persisted episode set",
        {e["episode_id"] for e in episodes} == {r["episode_id"] for r in trials},
    )

    adaptive_row = next(r for r in table1 if r["policy"].startswith("adaptive"))
    fixed_row = next(r for r in table1 if r["policy"].startswith("BEST_FIXED_K2"))
    check(
        "Table 1 adaptive success matches trials",
        abs(float(adaptive_row["success_rate"]) - a_s / n) < ROUND_TOL,
        f"{adaptive_row['success_rate']} vs {a_s / n:.9f}",
    )
    check(
        "Table 1 adaptive calls/request matches trials",
        abs(float(adaptive_row["mean_calls_per_request"]) - a_c / n) < ROUND_TOL,
    )
    check(
        "Table 1 fixed success matches trials",
        abs(float(fixed_row["success_rate"]) - f_s / n) < ROUND_TOL,
        f"{fixed_row['success_rate']} vs {f_s / n:.9f}",
    )
    check(
        "Table 2 net difference = (n10-n01)/N",
        abs(float(table2["net_success_difference"]) - (n10 - n01) / n) < ROUND_TOL,
    )
    check("Table 2 raw additional successes", int(table2["raw_additional_successes"]) == n10 - n01)
    check("Table 2 raw calls saved", int(table2["raw_calls_saved"]) == f_c - a_c)
    check("Table 2 bootstrap unit is episode", table2["bootstrap_unit"] == "episode")

    ground_truth = Counter()
    for r in trials:
        gt = {"a": int(r["gt_local_a"]), "b": int(r["gt_local_b"]), "c": int(r["gt_local_c"])}
        for row in static:
            if any(gt[s.split("-")[1]] for s in row["subset"].split(",")):
                ground_truth[row["subset"]] += 1
    check(
        "static baseline table matches raw ground truth",
        all(int(row["success_count"]) == ground_truth[row["subset"]] for row in static),
    )

    fig2_points = {
        r["policy"]: (float(r["success_rate"]), float(r["calls_per_request"])) for r in fig2
    }
    table1_points = {
        r["policy"]: (float(r["success_rate"]), float(r["mean_calls_per_request"]))
        for r in table1
    }
    check(
        "fig2 source matches Table 1",
        all(
            abs(fig2_points[k][0] - v[0]) < ROUND_TOL
            and abs(fig2_points[k][1] - v[1]) < ROUND_TOL
            for k, v in table1_points.items()
            if k in fig2_points
        ),
    )
    k_counts = Counter(
        (r["hidden_state_controlled_injection"], r["adaptive_selected_k"]) for r in trials
    )
    check(
        "fig3 source matches trial-level k counts",
        all(
            int(row[f"k{k}"]) == k_counts[(row["controlled_injection_state"], str(k))]
            for row in fig3
            for k in (1, 2, 3)
        ),
    )
    check("fig3 trials column sums to N", sum(int(r["trials"]) for r in fig3) == n)

    check("validation_report status is PASS", validation["status"] == "PASS")
    check(
        "all cross-checks against the V4 run agree",
        all(v["agree"] for v in validation["crosscheck_vs_v4_run"].values()),
    )

    sums = [
        line.split("  ", 1)
        for line in (PACKAGE / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    bad = [
        rel for digest, rel in sums
        if hashlib.sha256((PACKAGE / rel).read_bytes()).hexdigest() != digest
    ]
    check(f"SHA256SUMS verify ({len(sums)} files)", not bad, str(bad))

    print("\nACK_EVIDENCE_AUDIT: " + ("PASS" if not failures else f"FAIL {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
