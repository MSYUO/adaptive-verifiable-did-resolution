"""V2 FINAL HOLDOUT: closed-loop partial feedback. Run ONCE, clean tree.

Evaluates the frozen V2 estimator under deployment observability in both
service modes, plus matched-trial baselines on the SAME trials.

OBSERVATION FAIRNESS (§16). Baselines are reconstructed counterfactually from
the same ground-truth trials; they are stateless with respect to history, so
they neither gain nor lose from an observation stream. The adaptive policy is
executed closed-loop and sees ONLY its own calls plus the fixed exploration
schedule. That asymmetry is stated rather than hidden: adaptive is charged for
exploration, baselines have none to charge.

CONTROLLED LOCAL QUALIFICATION. No public network, no real DID provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.inventory import ProviderEntry  # noqa: E402
from avdr.learning.closedloop import (  # noqa: E402
    EXPLORATION_INTERVAL_R,
    SERVICE_BEST_EFFORT,
    STRICT_SLO,
)
from avdr.learning.dataset import ADMIN_URLS, all_subsets, generate_episode  # noqa: E402
from avdr.learning.environment import PROVIDERS, plan_episodes  # noqa: E402
from avdr.learning.estimators import load_frozen  # noqa: E402
from avdr.learning.metrics import evaluate_estimator, sequential_failover_outcome  # noqa: E402
from avdr.learning.oracle import ClosedLoopOutcome, decompose  # noqa: E402
from avdr.learning.runner_v2 import oracle_for, run_closed_loop  # noqa: E402
from avdr.provenance import resolve_git_commit, resolve_git_dirty  # noqa: E402

# [DESIGN CHOICE] unseen V2 holdout seeds; disjoint from V1 (900k) and V2
# train/validation (500k/600k).
HOLDOUT_EPISODES, HOLDOUT_SEED = 10, 800_000
TRIALS_PER_EPISODE = 22
TARGET_SLO = 0.90


def provider_entries():
    return {
        pid: ProviderEntry(
            id=pid, endpoint=url, adapter="universal-resolver-v1",
            supported_did_methods=["example"], implementation_id="avdr-mock-resolver",
        )
        for pid, url in ADMIN_URLS.items()
    }


def baseline_outcomes(episodes, tau_ms, name, subset=None, sequential=False):
    outcomes = []
    for episode_id, records in episodes.items():
        for record in sorted(records, key=lambda r: r.trial_index):
            if not record.complete:
                continue
            oracle = oracle_for(record, tau_ms)
            if sequential:
                ok, attempts = sequential_failover_outcome(
                    record, list(PROVIDERS), tau_ms
                )
                chosen, calls, satisfied = tuple(PROVIDERS), attempts, ok
            else:
                chosen = tuple(sorted(subset))
                calls = len(chosen)
                satisfied = oracle.subset_succeeds(chosen)
            outcomes.append(
                ClosedLoopOutcome(
                    episode_id=episode_id, trial_index=record.trial_index,
                    committed=True, selected_subset=chosen, satisfied=satisfied,
                    status="SELECTED", oracle_feasible=oracle.oracle_feasible,
                    predicted_feasible=None, execution_calls=calls,
                )
            )
    return name, outcomes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", default=str(REPO_ROOT / "frozen" / "frozen_estimator_v2.pkl"))
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "learning_v2" / "v2_holdout_report.json"))
    args = parser.parse_args()

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)
    estimator, metadata, digest = load_frozen(Path(args.artifact))
    tau = metadata["deadline_tau_ms"]

    print("V2 FINAL HOLDOUT -- CLOSED-LOOP PARTIAL FEEDBACK")
    print(f"  git_commit={commit}  git_dirty={dirty}")
    print(f"  frozen = {metadata['estimator_id']} ({metadata['model_family']}) "
          f"calibration={metadata['calibration']}")
    print(f"  artifact_sha256 verified = {digest}")
    print(f"  tau={tau} ms target={TARGET_SLO} R={EXPLORATION_INTERVAL_R}\n")

    entries = provider_entries()
    with httpx.Client() as admin:
        for url in ADMIN_URLS.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

    plans = plan_episodes("v2holdout", HOLDOUT_EPISODES, HOLDOUT_SEED, TRIALS_PER_EPISODE)
    print(f"== collecting holdout ground truth ({HOLDOUT_EPISODES} episodes) ==")
    episodes = {}
    for i, plan in enumerate(plans, 1):
        trials, _ = generate_episode(plan, entries)
        episodes[plan.episode_id] = trials
        print(f"    {plan.episode_id} ({i}/{len(plans)})", flush=True)

    results = {}
    for mode in (STRICT_SLO, SERVICE_BEST_EFFORT):
        loop = run_closed_loop(
            estimator, TARGET_SLO, tau, episodes, mode=mode,
            exploration_interval=EXPLORATION_INTERVAL_R,
            collect_traces=3 if mode == STRICT_SLO else 0,
        )
        metrics = evaluate_estimator(metadata["estimator_id"], loop.y_true, loop.y_prob)
        results[mode] = {
            "estimator_metrics": {**metrics.to_dict(),
                                  "reliability": metrics.reliability_table},
            "decision": decompose(loop.outcomes),
            "traces": loop.traces,
            "known_subset_updates": loop.known_subset_updates,
            "unknown_subset_updates": loop.unknown_subset_updates,
        }

    print("== estimator quality (identical predictions in both modes) ==")
    em = results[STRICT_SLO]["estimator_metrics"]
    print(f"  n={em['n']} brier={em['brier']} logloss={em['log_loss']} ece={em['ece']}")
    print(f"  base_rate={em['base_rate']} mean_prediction={em['mean_prediction']}")

    baselines = dict(
        [
            baseline_outcomes(episodes, tau, "single-static(local-a)", ("local-a",)),
            baseline_outcomes(episodes, tau, "sequential-failover", sequential=True),
            baseline_outcomes(episodes, tau, "fixed-k2(a,b)", ("local-a", "local-b")),
            baseline_outcomes(episodes, tau, "all-race", tuple(PROVIDERS)),
        ]
    )
    baseline_metrics = {name: decompose(o) for name, o in baselines.items()}

    print("\n== matched-trial decision metrics (denominator = ALL trials) ==")
    header = f"  {'policy':34s} {'succ/all':>9s} {'commit':>7s} {'k':>5s} {'calls/req':>10s}"
    print(header)
    for mode in (STRICT_SLO, SERVICE_BEST_EFFORT):
        d = results[mode]["decision"]
        print(f"  {'adaptive [' + mode + ']':34s} {d['success_over_all_trials']:>9} "
              f"{d['commit_rate']:>7} {str(d['mean_selected_k']):>5} "
              f"{d['mean_total_calls']:>10}")
    for name, d in baseline_metrics.items():
        print(f"  {name:34s} {d['success_over_all_trials']:>9} "
              f"{d['commit_rate']:>7} {str(d['mean_selected_k']):>5} "
              f"{d['mean_total_calls']:>10}")

    print("\n== oracle decomposition (strict mode) ==")
    d = results[STRICT_SLO]["decision"]
    for key in ("oracle_feasible_count", "oracle_infeasible_count",
                "committed_and_oracle_feasible", "committed_and_oracle_infeasible",
                "abstained_and_oracle_feasible", "abstained_and_oracle_infeasible",
                "unnecessary_abstention_count", "correct_abstention_count",
                "avoidable_miss_count", "intrinsic_miss_count",
                "false_feasible_count"):
        print(f"  {key:38s} {d[key]}")

    report = {
        "protocol": "closed-loop-partial-feedback-v2",
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "git_commit": commit, "git_dirty": dirty,
        "frozen_artifact": metadata, "artifact_sha256_verified": digest,
        "target_slo_probability": TARGET_SLO, "deadline_tau_ms": tau,
        "exploration_interval_R": EXPLORATION_INTERVAL_R,
        "holdout_episodes": [p.episode_id for p in plans],
        "holdout_seeds": [p.seed for p in plans],
        "modes": results,
        "baselines": baseline_metrics,
        "observation_fairness_note": (
            "Adaptive runs closed-loop and sees only its own calls plus the "
            "fixed exploration schedule, and is charged for both. Baselines "
            "are stateless counterfactual reconstructions on the same trials "
            "and have no observation stream to charge."
        ),
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
