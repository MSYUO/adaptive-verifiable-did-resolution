"""FINAL HOLDOUT -- prospective evaluation of the frozen estimator.

Run ONCE, after the model and configuration are frozen and committed, from a
clean tree. No source or config may be modified afterwards.

Per-trial order is strictly prospective:

    history through t-1
      -> pre-request features
      -> q_hat(S) for ALL 2^M - 1 subsets (frozen artifact)
      -> complete-coverage check
      -> optimizer -> S_t
      -> execute
      -> ONLY NOW reveal Y_t(S)

EXECUTION NOTE (stated plainly): every provider is probed in each holdout
trial, because Y_t(S) for every subset is needed to score the estimator and to
compare all policies on identical trials. The selected subset's outcome is
read from that same trial, so it reflects what an S_t-only execution would
have produced APART FROM any shared-host load caused by the extra probes. That
is a counterfactual evaluation, and is labelled as such rather than presented
as an isolated execution of S_t.

CONTROLLED LOCAL QUALIFICATION. No real DID resolver is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.adaptive.optimizer import (  # noqa: E402
    SELECTED,
    MinimumSetOptimizer,
)
from avdr.inventory import ProviderEntry  # noqa: E402
from avdr.learning.dataset import (  # noqa: E402
    ADMIN_URLS,
    DEADLINE_TAU_MS,
    all_subsets,
    apply_behaviors_async,
    dataset_manifest,
    observe_trial,
    subset_target,
    warm_connections,
)
from avdr.learning.environment import (  # noqa: E402
    PROVIDERS,
    injection_config_hash,
    iter_trials,
    plan_episodes,
    sample_behaviors,
)
from avdr.learning.estimators import CONTEXT_KEY, HISTORY_KEY, load_frozen  # noqa: E402
from avdr.learning.features import TrialRecord, build_context  # noqa: E402
from avdr.learning.metrics import (  # noqa: E402
    DecisionOutcome,
    evaluate_estimator,
    sequential_failover_outcome,
    summarize_decisions,
)
from avdr.profiles import PROFILE_W3C_BASIC_V1  # noqa: E402
from avdr.provenance import resolve_git_commit, resolve_git_dirty  # noqa: E402

# [DESIGN CHOICE] fixed in advance; seeds disjoint from train/validation.
HOLDOUT_EPISODES, HOLDOUT_SEED = 10, 900_000
TRIALS_PER_EPISODE = 22
TARGET_SLO = 0.90


def provider_entries() -> dict[str, ProviderEntry]:
    return {
        pid: ProviderEntry(
            id=pid, endpoint=url, adapter="universal-resolver-v1",
            supported_did_methods=["example"], implementation_id="avdr-mock-resolver",
        )
        for pid, url in ADMIN_URLS.items()
    }


async def run_holdout(estimator, optimizer, target, tau_ms, plans, entries):
    y_true: list[int] = []
    y_prob: list[float] = []
    adaptive: list[DecisionOutcome] = []
    policy_outcomes: dict[str, list[DecisionOutcome]] = {
        "single-static(local-a)": [], "sequential-failover": [],
        "fixed-k2(a,b)": [], "all-race": [],
    }
    traces: list[dict] = []
    trials_run = 0
    incomplete = 0

    async with httpx.AsyncClient() as admin, httpx.AsyncClient() as client:
        await warm_connections(client, entries, 3000)

        for plan in plans:
            history: list[TrialRecord] = []
            subset_history: dict[tuple[str, ...], list[int]] = {}

            for trial_index, hidden_state, rng in iter_trials(plan):
                # ---- 1. pre-request context from history < t --------------
                context = build_context(
                    history, tau_ms, plan.episode_id, trial_index
                )
                payload = {CONTEXT_KEY: context, HISTORY_KEY: subset_history}

                # ---- 2. predict every subset ------------------------------
                predictions = {
                    subset: estimator.estimate(subset, payload)
                    for subset in all_subsets()
                }

                # ---- 3. coverage check + 4. optimize ----------------------
                result = optimizer.select(
                    candidates=list(PROVIDERS), estimator=estimator,
                    target_probability=target, context=payload,
                )

                # ---- 5. execute (all probed; see module docstring) --------
                behaviors = sample_behaviors(hidden_state, rng)
                await apply_behaviors_async(admin, behaviors)
                did = f"did:example:{plan.episode_id}-{trial_index:04d}"
                observations = await observe_trial(client, entries, did, 3000)
                missing = [p for p, o in observations.items() if not o.observed]
                record = TrialRecord(
                    episode_id=plan.episode_id, trial_index=trial_index,
                    split="holdout", seed=plan.seed, hidden_state=hidden_state,
                    did=did, observations=observations, complete=not missing,
                    incomplete_reason=f"missing {sorted(missing)}" if missing else None,
                )
                trials_run += 1

                # ---- 6. outcome revealed ONLY NOW -------------------------
                if not record.complete:
                    incomplete += 1
                    history.append(record)
                    continue

                for subset in all_subsets():
                    target_value = subset_target(record, subset, tau_ms)
                    if predictions[subset] is not None:
                        y_true.append(target_value)
                        y_prob.append(predictions[subset])
                    subset_history.setdefault(subset, []).append(target_value)

                if result.status == SELECTED and result.selected_subset:
                    chosen = tuple(sorted(result.selected_subset))
                    satisfied = bool(subset_target(record, chosen, tau_ms))
                    adaptive.append(
                        DecisionOutcome(
                            policy="adaptive-min-set", subset=chosen,
                            fan_out=len(chosen), satisfied=satisfied,
                            predicted_feasible=True, status=SELECTED,
                        )
                    )
                    if len(traces) < 3:
                        traces.append(
                            {
                                "episode_id": plan.episode_id,
                                "trial_index": trial_index,
                                "history_len_before_trial": len(history),
                                "context_excerpt": {
                                    p: {
                                        k: round(v, 4) if isinstance(v, float) else v
                                        for k, v in context.per_provider[p].items()
                                    }
                                    for p in PROVIDERS
                                },
                                "predictions": {
                                    ",".join(k): round(v, 5)
                                    for k, v in predictions.items()
                                    if v is not None
                                },
                                "coverage_exact": result.exact,
                                "target_slo_probability": target,
                                "selected_subset": list(chosen),
                                "estimated_subset_success": result.estimated_subset_success,
                                "selection_reason": result.selection_reason,
                                "revealed_outcome": {
                                    p: {
                                        "accepted": observations[p].accepted,
                                        "completion_offset_ms": observations[
                                            p
                                        ].completion_offset_ms,
                                        "within_deadline": record.within_deadline(
                                            p, tau_ms
                                        ),
                                    }
                                    for p in PROVIDERS
                                },
                                "Y_selected": int(satisfied),
                                "hidden_state_audit_only": hidden_state,
                            }
                        )
                else:
                    adaptive.append(
                        DecisionOutcome(
                            policy="adaptive-min-set", subset=(), fan_out=0,
                            satisfied=False, predicted_feasible=False,
                            status=result.status,
                        )
                    )

                # ---- comparison policies on the SAME trial ---------------
                policy_outcomes["single-static(local-a)"].append(
                    DecisionOutcome(
                        "single-static(local-a)", ("local-a",), 1,
                        bool(subset_target(record, ("local-a",), tau_ms)),
                        status=SELECTED,
                    )
                )
                ok, attempts = sequential_failover_outcome(
                    record, ["local-a", "local-b", "local-c"], tau_ms
                )
                policy_outcomes["sequential-failover"].append(
                    DecisionOutcome(
                        "sequential-failover", ("local-a", "local-b", "local-c"),
                        attempts, ok, status=SELECTED,
                    )
                )
                policy_outcomes["fixed-k2(a,b)"].append(
                    DecisionOutcome(
                        "fixed-k2(a,b)", ("local-a", "local-b"), 2,
                        bool(subset_target(record, ("local-a", "local-b"), tau_ms)),
                        status=SELECTED,
                    )
                )
                policy_outcomes["all-race"].append(
                    DecisionOutcome(
                        "all-race", tuple(sorted(PROVIDERS)), 3,
                        bool(subset_target(record, tuple(PROVIDERS), tau_ms)),
                        status=SELECTED,
                    )
                )
                history.append(record)

    return y_true, y_prob, adaptive, policy_outcomes, traces, trials_run, incomplete


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", default=str(REPO_ROOT / "frozen" / "frozen_estimator.pkl"))
    parser.add_argument("--target", type=float, default=TARGET_SLO)
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "learning" / "holdout_report.json"))
    args = parser.parse_args()

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)

    estimator, metadata, digest = load_frozen(Path(args.artifact))
    print("FINAL HOLDOUT -- CONTROLLED LOCAL QUALIFICATION")
    print(f"  git_commit={commit}  git_dirty={dirty}")
    print(f"  frozen estimator = {metadata['estimator_id']} "
          f"({metadata['model_family']})")
    print(f"  artifact_sha256  = {digest}")
    print(f"  hash verified against committed metadata: OK")
    print(f"  tau = {metadata['deadline_tau_ms']} ms   target SLO = {args.target}")
    print("  NOTE: all providers are probed each trial; decisions are evaluated")
    print("        counterfactually on that record. See module docstring.\n")

    plans = plan_episodes("holdout", HOLDOUT_EPISODES, HOLDOUT_SEED, TRIALS_PER_EPISODE)
    entries = provider_entries()
    optimizer = MinimumSetOptimizer()

    (y_true, y_prob, adaptive, policy_outcomes, traces, trials_run,
     incomplete) = asyncio.run(
        run_holdout(estimator, optimizer, args.target,
                    metadata["deadline_tau_ms"], plans, entries)
    )

    est_metrics = evaluate_estimator(metadata["estimator_id"], y_true, y_prob)
    print("== FINAL HOLDOUT estimator quality ==")
    print(f"  n={est_metrics.n} brier={est_metrics.brier:.5f} "
          f"logloss={est_metrics.log_loss:.5f} ece={est_metrics.ece:.5f}")
    print(f"  base_rate={est_metrics.base_rate:.4f} "
          f"mean_prediction={est_metrics.mean_prediction:.4f}")

    print("\n== FINAL HOLDOUT decision quality ==")
    adaptive_summary = summarize_decisions(adaptive)
    rows = {"adaptive-min-set": adaptive_summary}
    for name, outcomes in policy_outcomes.items():
        rows[name] = summarize_decisions(outcomes)
    print(f"  {'policy':26s} {'SLO sat':>8s} {'mean fan-out':>13s} {'calls':>7s} "
          f"{'unsat':>6s} {'false-feas':>11s}")
    for name, summary in rows.items():
        print(f"  {name:26s} {summary.get('slo_satisfaction_rate'):>8} "
              f"{summary.get('mean_selected_subset_size'):>13} "
              f"{summary.get('total_provider_calls'):>7} "
              f"{summary.get('unsatisfiable_estimate_count'):>6} "
              f"{str(summary.get('false_feasible_rate')):>11}")

    report = {
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "git_commit": commit,
        "git_dirty": dirty,
        "frozen_artifact": metadata,
        "artifact_sha256_verified": digest,
        "target_slo_probability": args.target,
        "deadline_tau_ms": metadata["deadline_tau_ms"],
        "holdout_episodes": [p.episode_id for p in plans],
        "holdout_seeds": [p.seed for p in plans],
        "trials_run": trials_run,
        "incomplete_trials_excluded": incomplete,
        "estimator_metrics": {
            **est_metrics.to_dict(),
            "reliability": est_metrics.reliability_table,
        },
        "decision_metrics": rows,
        "prospective_traces": traces,
        "execution_note": (
            "All providers probed per trial so every subset has a target; "
            "policy outcomes are counterfactual reads of that record."
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
