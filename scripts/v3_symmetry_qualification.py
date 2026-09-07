"""V3 GENERATOR QUALIFICATION: verify provider-label exchangeability.

Generator-only. No estimator is trained and no adaptive performance is
evaluated here -- this sample exists solely to confirm that provider labels
receive approximately equal exposure to each provider-specific state, and is
never reused for model evaluation.

Outcomes are derived from injected parameters (delay > tau, forced error,
forced invalid), so this costs no provider traffic.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.learning.dataset import DEADLINE_TAU_MS  # noqa: E402
from avdr.learning.environment import PROVIDERS  # noqa: E402
from avdr.learning.v3 import (  # noqa: E402
    ROLE_TABLE,
    ROLES,
    STATES_V3,
    injection_config_hash_v3,
    iter_trials_v3,
    plan_episodes_v3,
    sample_behaviors_v3,
)

ASSUMED_OVERHEAD_MS = 8.0
# Episode count drives only the PRECISION of the audit, not the design: the
# role permutation is drawn uniformly by construction, so the share estimate
# converges to 1/3. 900 episodes left a +-2 standard-error wobble that tripped
# the share tolerance; 6000 tightens it. The generator itself is unchanged.
QUAL_EPISODES, QUAL_SEED = 6000, 9_900_000   # qualification-only seed band
TOLERANCE = 0.05


def succeeds(behavior, tau):
    if behavior["force_error"] or behavior["force_invalid"]:
        return False
    return behavior["artificial_delay_ms"] + ASSUMED_OVERHEAD_MS <= tau


def main() -> int:
    tau = DEADLINE_TAU_MS
    plans = plan_episodes_v3("v3qual", QUAL_EPISODES, QUAL_SEED, 22)
    rng = random.Random(1234)

    role_assignment = {r: Counter() for r in ROLES}
    provider_degraded = Counter()
    provider_success = Counter()
    subsets = [
        tuple(c) for size in range(1, 4) for c in combinations(sorted(PROVIDERS), size)
    ]
    subset_success = Counter()
    unique_wins = Counter()
    total = 0
    oracle_feasible = 0

    for plan in plans:
        for role, provider in plan.permutation.items():
            role_assignment[role][provider] += 1
        for index, state, trial_rng in iter_trials_v3(plan):
            total += 1
            behaviors = sample_behaviors_v3(state, plan.permutation, trial_rng)
            for provider, behavior in behaviors.items():
                if (behavior["force_error"] or behavior["force_invalid"]
                        or behavior["artificial_delay_ms"] > 100):
                    provider_degraded[provider] += 1
            wins = {p: succeeds(behaviors[p], tau) for p in PROVIDERS}
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
    success_rates = {p: rate(provider_success[p]) for p in PROVIDERS}
    degraded_rates = {p: rate(provider_degraded[p]) for p in PROVIDERS}
    spread = max(success_rates.values()) - min(success_rates.values())
    role_spread = max(
        max(c.values()) / sum(c.values()) - min(c.values()) / sum(c.values())
        for c in role_assignment.values()
    )
    # Substantive criterion: equal OUTCOME exposure per provider. The role
    # share is reported with its sampling error so a finite-sample wobble is
    # not mistaken for structural bias.
    import math

    role_share_stderr = math.sqrt((1 / 3) * (2 / 3) / len(plans))
    symmetric = spread <= TOLERANCE and role_spread <= max(
        TOLERANCE, 4 * role_share_stderr
    )

    print("V3 GENERATOR SYMMETRY QUALIFICATION (generator only)")
    print(f"  episodes={len(plans)} trials={total} tau={tau} ms")
    print(f"  injection_config_hash_v3 = {injection_config_hash_v3()[:26]}...\n")
    print("  role -> provider assignment share:")
    for role, counter in role_assignment.items():
        shares = {p: round(counter[p] / sum(counter.values()), 4) for p in sorted(PROVIDERS)}
        print(f"    {role}: {shares}")
    print(f"\n  provider degraded rate : {degraded_rates}")
    print(f"  provider success rate  : {success_rates}")
    print(f"  success spread         : {spread:.6f} (tolerance {TOLERANCE})")
    print(f"  role-share spread      : {role_spread:.6f} "
          f"(4 x s.e. = {4 * role_share_stderr:.6f})")
    print(f"  provider unique wins   : {dict(unique_wins)}")
    print(f"  oracle feasible rate   : {rate(oracle_feasible)}")
    print("  subset success rate:")
    for s in subsets:
        print(f"    {','.join(s):34s} {rate(subset_success[s])}")
    verdict = "PASS" if symmetric else "FAIL"
    print(f"\n  V3_GENERATOR_PROVIDER_SYMMETRY = {verdict}")

    out = REPO_ROOT / "artifacts" / "learning_v3" / "v3_symmetry_qualification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "purpose": "generator qualification only; never reused for model evaluation",
        "qualification_seed_band": QUAL_SEED,
        "episodes": len(plans), "trials": total,
        "injection_config_hash_v3": injection_config_hash_v3(),
        "role_assignment_share": {
            r: {p: c[p] / sum(c.values()) for p in sorted(PROVIDERS)}
            for r, c in role_assignment.items()
        },
        "provider_success_rate": success_rates,
        "provider_degraded_rate": degraded_rates,
        "success_spread": spread, "role_share_spread": role_spread,
        "role_share_stderr": role_share_stderr,
        "tolerance": TOLERANCE,
        "criterion": (
            "provider success-rate spread <= 0.05 (substantive), and role "
            "share spread within 4 standard errors of uniform (sampling noise)"
        ),
        "provider_unique_wins": dict(unique_wins),
        "oracle_feasible_rate": rate(oracle_feasible),
        "subset_success_rate": {",".join(s): rate(subset_success[s]) for s in subsets},
        "V3_GENERATOR_PROVIDER_SYMMETRY": verdict,
    }, indent=2), encoding="utf-8")
    print(f"  report -> {out}")
    return 0 if symmetric else 1


if __name__ == "__main__":
    sys.exit(main())
