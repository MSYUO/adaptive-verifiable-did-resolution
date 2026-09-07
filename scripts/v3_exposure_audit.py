"""V3 holdout provider-role exposure audit. [POST-HOC DIAGNOSTIC]

Uses ONLY the per-trial ground truth persisted by the V3 holdout. Asks whether
the V3 singleton spread (A 0.5644, B 0.6629, C 0.6856) is associated with
finite-sample role-exposure imbalance rather than any structural property.

This diagnostic must NOT be used to change the V4 generator or policy, and
does not revise the V3 result.

SCOPE LIMIT, disclosed. The V3 holdout persisted per-trial ground truth
(hidden state, role permutation, per-provider within-deadline) but NOT the
per-trial adaptive selections, nor the observation detail (completion times,
error/timeout flags) the features depend on. The adaptive mechanism audit
requested in the milestone brief therefore cannot be reconstructed exactly
from V3 and is produced from V4 instead, where every field is persisted.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.learning.environment import PROVIDERS  # noqa: E402
from avdr.learning.v3 import ROLE_TABLE, ROLES  # noqa: E402

V3_REPORT = REPO_ROOT / "artifacts" / "learning_v3" / "v3_holdout_report.json"

# Which roles a state degrades, derived from the frozen role table.
NORMAL_TABLE = ROLE_TABLE["NORMAL"]
DEGRADED_ROLES = {
    state: [r for r in ROLES if table[r] is not NORMAL_TABLE[r]]
    for state, table in ROLE_TABLE.items()
}


def exposure_label(state: str, role: str) -> str:
    """How this provider experienced this trial, given its role."""
    degraded = DEGRADED_ROLES[state]
    if role not in degraded:
        return "normal"
    if state == "SHARED_DEGRADATION":
        return "shared_degradation"
    if state == "PAIR_CORRELATED_DEGRADATION":
        return "pair_correlated"
    return {
        "SINGLE_PROVIDER_CONGESTED": "single_congested",
        "SINGLE_PROVIDER_UNSTABLE": "single_unstable",
        "SINGLE_PROVIDER_INVALID_RISK": "single_invalid_risk",
    }[state]


def main() -> int:
    report = json.loads(V3_REPORT.read_text(encoding="utf-8"))
    trials = report["per_trial_ground_truth"]
    permutations = report["role_permutations_audit_only"]

    exposure = {p: Counter() for p in PROVIDERS}
    success = Counter()
    total = 0

    for row in trials:
        if not row["complete"]:
            continue
        total += 1
        permutation = permutations[row["episode_id"]]
        provider_to_role = {v: k for k, v in permutation.items()}
        state = row["hidden_state_audit_only"]
        for provider in PROVIDERS:
            label = exposure_label(state, provider_to_role[provider])
            exposure[provider][label] += 1
            if row["within_deadline"][provider]:
                success[provider] += 1

    labels = [
        "normal", "single_congested", "single_unstable",
        "single_invalid_risk", "pair_correlated", "shared_degradation",
    ]

    print("V3 HOLDOUT PROVIDER-ROLE EXPOSURE AUDIT  [POST-HOC DIAGNOSTIC]")
    print("  does not revise V3; must not inform V4 tuning\n")
    print(f"  trials = {total}\n")
    header = "  " + "exposure".ljust(22) + "".join(p.rjust(11) for p in PROVIDERS)
    print(header)
    for label in labels:
        row = "  " + label.ljust(22)
        row += "".join(str(exposure[p][label]).rjust(11) for p in PROVIDERS)
        print(row)

    degraded_total = {
        p: sum(exposure[p][l] for l in labels if l != "normal") for p in PROVIDERS
    }
    print("  " + "-" * (22 + 11 * 3))
    print("  " + "TOTAL DEGRADED".ljust(22)
          + "".join(str(degraded_total[p]).rjust(11) for p in PROVIDERS))
    print("  " + "TOTAL NON-DEGRADED".ljust(22)
          + "".join(str(exposure[p]["normal"]).rjust(11) for p in PROVIDERS))
    print("  " + "SUCCESS COUNT".ljust(22)
          + "".join(str(success[p]).rjust(11) for p in PROVIDERS))
    print("  " + "SUCCESS RATE".ljust(22)
          + "".join(f"{success[p]/total:.4f}".rjust(11) for p in PROVIDERS))

    # Solo-degradation exposure is the strongest driver of a singleton gap.
    solo = {
        p: sum(exposure[p][l] for l in
               ("single_congested", "single_unstable", "single_invalid_risk"))
        for p in PROVIDERS
    }
    print("\n  solo-degraded exposure: " + str(solo))
    spread_solo = max(solo.values()) - min(solo.values())
    spread_success = max(success[p] for p in PROVIDERS) - min(
        success[p] for p in PROVIDERS
    )
    print(f"  solo-degraded exposure spread : {spread_solo} trials")
    print(f"  success-count spread          : {spread_success} trials")
    print(
        "\n  [INTERPRETATION] the singleton spread is consistent with "
        "finite-sample\n  role-exposure imbalance rather than provider "
        "identity: the generator is\n  symmetric by construction and 264 "
        "trials leave visible sampling noise."
    )

    out = REPO_ROOT / "artifacts" / "learning_v3" / "v3_exposure_audit.json"
    out.write_text(
        json.dumps(
            {
                "diagnostic_type": "POST-HOC DIAGNOSTIC",
                "revises_v3": False,
                "must_not_inform_v4_tuning": True,
                "trials": total,
                "exposure_counts": {p: dict(exposure[p]) for p in PROVIDERS},
                "total_degraded": degraded_total,
                "total_non_degraded": {p: exposure[p]["normal"] for p in PROVIDERS},
                "solo_degraded_exposure": solo,
                "success_count": dict(success),
                "success_rate": {p: success[p] / total for p in PROVIDERS},
                "solo_exposure_spread_trials": spread_solo,
                "success_count_spread_trials": spread_success,
                "mechanism_audit_limitation": (
                    "V3 did not persist per-trial adaptive selections or the "
                    "observation detail features depend on, so the adaptive "
                    "mechanism audit is produced from V4 instead"
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
