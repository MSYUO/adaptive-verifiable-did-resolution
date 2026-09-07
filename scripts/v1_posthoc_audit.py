"""POST-HOC DIAGNOSTIC of the V1 (full-information-shadow-v1) holdout.

Not a pre-registered V1 result and it does NOT revise any V1 claim. V1 stands
as reported: controlled-local, full-information shadow evaluation, EWMA
selected over ML, calibration degraded validation quality, 72/220
commitments, 148/220 estimated-unsatisfiable, 10/72 false-feasible.

LIMITATION discovered while writing this: the V1 holdout report persisted
aggregate metrics only -- no per-trial records -- so the JOINT distribution of
(committed, oracle_feasible) is not recoverable. What is recoverable:

  * oracle_feasible == the all-race success count, because all-race succeeds
    exactly when some provider delivered an acceptable response within tau
  * exact interval bounds on every joint cell, since a committed success
    implies oracle feasibility

V2 persists per-trial records, so its decomposition is exact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.learning.oracle import bound_v1_decomposition  # noqa: E402

V1_REPORT = REPO_ROOT / "artifacts" / "learning" / "holdout_report.json"


def main() -> int:
    report = json.loads(V1_REPORT.read_text(encoding="utf-8"))
    dm = report["decision_metrics"]
    adaptive, all_race = dm["adaptive-min-set"], dm["all-race"]

    total = adaptive["n"]
    committed = adaptive["planned"]
    committed_success = round(adaptive["slo_satisfaction_rate"] * committed)
    oracle_feasible = round(all_race["slo_satisfaction_rate"] * all_race["planned"])

    bounds = bound_v1_decomposition(
        total=total, committed=committed,
        committed_success=committed_success, oracle_feasible=oracle_feasible,
    )

    print("V1 POST-HOC ORACLE AUDIT  [POST-HOC DIAGNOSTIC]")
    print("  protocol: full-information-shadow-v1 (preserved, unmodified)")
    print(f"  source: {V1_REPORT.name}\n")
    print(f"  total trials            {total}")
    print(f"  oracle-feasible         {oracle_feasible}   "
          f"(= all-race successes; all-race succeeds iff some provider delivered)")
    print(f"  oracle-infeasible       {total - oracle_feasible}")
    print(f"  committed               {committed}")
    print(f"    committed success     {committed_success}")
    print(f"    committed failure     {bounds['committed_failure']}")
    print(f"  abstained               {bounds['abstained']}\n")
    print("  JOINT CELLS -- interval bounds only (per-trial records not persisted):")
    for key in ("committed_and_oracle_feasible_bounds",
                "committed_and_oracle_infeasible_bounds",
                "unnecessary_abstention_bounds", "correct_abstention_bounds",
                "avoidable_miss_bounds", "intrinsic_miss_bounds"):
        lo, hi = bounds[key]
        print(f"    {key.replace('_bounds',''):38s} [{lo}, {hi}]")
    print(f"\n  reason: {bounds['reason']}")

    out = REPO_ROOT / "artifacts" / "learning" / "v1_posthoc_oracle_audit.json"
    out.write_text(
        json.dumps(
            {
                "diagnostic_type": "POST-HOC DIAGNOSTIC",
                "protocol": "full-information-shadow-v1",
                "does_not_revise_v1_claims": True,
                **bounds,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
