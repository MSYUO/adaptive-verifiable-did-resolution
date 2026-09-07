"""V3 pipeline: policy-faithful training, validation, BEST_FIXED_K2 freeze.

POLICY-FAITHFUL TRAINING (§9). V2 trained from a fully observed audit corpus
while deploying under partial observability. V3 does not. Training FEATURES
come from a DeploymentObservedHistory populated only by:

    warmup                 cold-start audit (first COLD_START_TRIALS trials)
    scheduled_exploration  the fixed R-schedule
    selected_execution     the behaviour policy's own choices

The evaluator's full trial outcome is used ONLY to form LABELS Y_t(S). It is
never appended to the deployment history, so it can never become observable
context. Behaviour policy during collection [DESIGN CHOICE]: round-robin over
single providers, which produces realistically sparse, stale histories.

Everything below was frozen before any V3 outcome was inspected:
  tau=250 ms, target=0.90, R=8, COLD_START_TRIALS=3,
  selection rule, BEST_FIXED_K2 chosen on TRAIN+VALIDATION only.
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
    SCHEDULED_EXPLORATION,
    SELECTED_EXECUTION,
    STRICT_SLO,
    WARMUP,
    DeploymentObservedHistory,
    build_partial_context,
    build_row_v2,
    feature_schema_hash_v2,
    reveal_subset,
    should_explore,
)
from avdr.learning.dataset import (  # noqa: E402
    ADMIN_URLS,
    DEADLINE_TAU_MS,
    DatasetRow,
    all_subsets,
    apply_behaviors_async,
    observe_trial,
    subset_target,
    warm_connections,
)
from avdr.learning.environment import PROVIDERS  # noqa: E402
from avdr.learning.estimators import (  # noqa: E402
    BackoffSubsetRateEstimator,
    GlobalRateEstimator,
    RollingEmpiricalEstimator,
    SigmoidCalibratedEstimator,
    build_gradient_boosting_v2,
    build_logistic_v2,
    freeze_estimator,
)
from avdr.learning.features import TrialRecord  # noqa: E402
from avdr.learning.metrics import evaluate_estimator  # noqa: E402
from avdr.learning.runner_v2 import oracle_for, run_closed_loop  # noqa: E402
from avdr.learning.v3 import (  # noqa: E402
    injection_config_hash_v3,
    iter_trials_v3,
    plan_episodes_v3,
    sample_behaviors_v3,
)
from avdr.provenance import resolve_git_commit, resolve_git_dirty  # noqa: E402

# [DESIGN CHOICE] V3 seeds, disjoint from V1 (100k/300k/900k), V2
# (500k/600k/800k) and the V3 qualification band (9.9M).
TRIALS_PER_EPISODE = 22
TRAIN_EPISODES, TRAIN_SEED = 24, 1_100_000
VAL_EPISODES, VAL_SEED = 10, 1_200_000
TARGET_SLO = 0.90
SIMPLICITY_ORDER = [
    "b0-global-rate", "b1-rolling-empirical", "b2-ewma-backoff",
    "m1-logistic-v2", "m2-hist-gradient-boosting-v2",
]
BRIER_TOLERANCE = 0.005


def provider_entries():
    return {
        pid: ProviderEntry(
            id=pid, endpoint=url, adapter="universal-resolver-v1",
            supported_did_methods=["example"], implementation_id="avdr-mock-resolver",
        )
        for pid, url in ADMIN_URLS.items()
    }


async def generate_v3_episode(plan, entries, admin, client, tau_ms, timeout_ms=3000):
    """Measure full ground truth for one V3 episode (labels come from here)."""
    records = []
    for index, state, rng in iter_trials_v3(plan):
        behaviors = sample_behaviors_v3(state, plan.permutation, rng)
        await apply_behaviors_async(admin, behaviors)
        did = f"did:example:{plan.episode_id}-{index:04d}"
        observations = await observe_trial(client, entries, did, timeout_ms)
        missing = [p for p, o in observations.items() if not o.observed]
        records.append(
            TrialRecord(
                episode_id=plan.episode_id, trial_index=index, split=plan.split,
                seed=plan.seed, hidden_state=state, did=did,
                observations=observations, complete=not missing,
                incomplete_reason=f"missing {sorted(missing)}" if missing else None,
            )
        )
    return records


async def collect(split, count, seed, entries):
    plans = plan_episodes_v3(split, count, seed, TRIALS_PER_EPISODE)
    episodes = {}
    async with httpx.AsyncClient() as admin, httpx.AsyncClient() as client:
        await warm_connections(client, entries, 3000)
        for i, plan in enumerate(plans, 1):
            episodes[plan.episode_id] = await generate_v3_episode(
                plan, entries, admin, client, DEADLINE_TAU_MS
            )
            print(f"    {plan.episode_id} ({i}/{len(plans)})", flush=True)
    return plans, episodes


def policy_faithful_rows(episodes, tau_ms) -> tuple[list[DatasetRow], dict]:
    """Build TRAIN rows whose FEATURES come from a partial deployment history.

    Labels are formed from the evaluator ground truth; the ground truth is
    never appended to the deployment history, so it cannot leak into features.
    """
    rows: list[DatasetRow] = []
    subsets = all_subsets()
    stats = {"warmup_calls": 0, "exploration_calls": 0, "execution_calls": 0}

    for episode_id, records in episodes.items():
        deployment = DeploymentObservedHistory()
        counter = 0
        for record in sorted(records, key=lambda r: r.trial_index):
            if not record.complete:
                continue
            counter += 1

            if counter <= COLD_START_TRIALS:
                deployment.append(reveal_subset(record, PROVIDERS, WARMUP))
                stats["warmup_calls"] += len(PROVIDERS)
                continue

            # FEATURES: partial deployment history only.
            context = build_partial_context(deployment, tau_ms, record.trial_index)
            for subset in subsets:
                rows.append(
                    DatasetRow(
                        episode_id=episode_id, trial_index=record.trial_index,
                        split="train", subset=subset,
                        features=build_row_v2(context, subset),
                        # LABEL from evaluator ground truth -- never a feature.
                        target=subset_target(record, subset, tau_ms),
                        hidden_state=record.hidden_state,
                    )
                )

            # Behaviour policy: round-robin single provider, plus the fixed
            # exploration schedule. Only these enter the deployment history.
            chosen = (PROVIDERS[counter % len(PROVIDERS)],)
            deployment.append(reveal_subset(record, chosen, SELECTED_EXECUTION))
            stats["execution_calls"] += len(chosen)
            if should_explore(counter, EXPLORATION_INTERVAL_R):
                deployment.append(
                    reveal_subset(record, PROVIDERS, SCHEDULED_EXPLORATION)
                )
                stats["exploration_calls"] += len(PROVIDERS)
    return rows, stats


def static_subset_success(episodes, tau_ms) -> dict:
    """Success rate of every static subset, for BEST_FIXED_K2 selection."""
    subsets = all_subsets()
    hits = {s: 0 for s in subsets}
    total = 0
    for records in episodes.values():
        for record in records:
            if not record.complete:
                continue
            total += 1
            oracle = oracle_for(record, tau_ms)
            for s in subsets:
                if oracle.subset_succeeds(s):
                    hits[s] += 1
    return {
        "trials": total,
        "success_rate": {",".join(s): round(hits[s] / total, 6) for s in subsets},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "artifacts" / "learning_v3"))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)
    print("V3 PIPELINE -- CONTROLLED LOCAL QUALIFICATION")
    print(f"  git_commit={commit} git_dirty={dirty}")
    print(f"  tau={DEADLINE_TAU_MS} target={TARGET_SLO} R={EXPLORATION_INTERVAL_R} "
          f"cold_start={COLD_START_TRIALS}")
    print(f"  injection_v3={injection_config_hash_v3()[:26]}...\n")

    entries = provider_entries()
    with httpx.Client() as admin:
        for url in ADMIN_URLS.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

    print(f"== collecting TRAIN ground truth ({TRAIN_EPISODES} episodes) ==")
    train_plans, train_eps = asyncio.run(
        collect("v3train", TRAIN_EPISODES, TRAIN_SEED, entries)
    )
    print(f"== collecting VALIDATION ground truth ({VAL_EPISODES} episodes) ==")
    val_plans, val_eps = asyncio.run(
        collect("v3val", VAL_EPISODES, VAL_SEED, entries)
    )

    rows, collection_stats = policy_faithful_rows(train_eps, DEADLINE_TAU_MS)
    print(f"\n  train rows={len(rows)} base_rate="
          f"{sum(r.target for r in rows)/len(rows):.4f}")
    print(f"  collection calls: {collection_stats}")

    # ---- BEST_FIXED_K2 from TRAIN + VALIDATION ONLY ----
    train_static = static_subset_success(train_eps, DEADLINE_TAU_MS)
    val_static = static_subset_success(val_eps, DEADLINE_TAU_MS)
    combined = {
        k: (train_static["success_rate"][k] * train_static["trials"]
            + val_static["success_rate"][k] * val_static["trials"])
        / (train_static["trials"] + val_static["trials"])
        for k in train_static["success_rate"]
    }
    pairs = {k: v for k, v in combined.items() if len(k.split(",")) == 2}
    best_pair = max(sorted(pairs), key=lambda k: pairs[k])
    print(f"\n== BEST_FIXED_K2 selection (TRAIN+VALIDATION only) ==")
    for k in sorted(pairs):
        print(f"    {k:34s} {pairs[k]:.6f}")
    print(f"  BEST_FIXED_K2 = {best_pair}  (frozen before holdout)")

    # ---- estimator candidates ----
    candidates = {
        "b0-global-rate": GlobalRateEstimator().fit(rows),
        "b1-rolling-empirical": RollingEmpiricalEstimator().fit(rows),
        "b2-ewma-backoff": BackoffSubsetRateEstimator().fit(rows),
        "m1-logistic-v2": build_logistic_v2().fit(rows),
        "m2-hist-gradient-boosting-v2": build_gradient_boosting_v2().fit(rows),
    }
    print("\n== VALIDATION under deployment protocol (partial feedback) ==")
    validation = {}
    for name, est in candidates.items():
        loop = run_closed_loop(
            est, TARGET_SLO, DEADLINE_TAU_MS, val_eps, mode=STRICT_SLO,
            exploration_interval=EXPLORATION_INTERVAL_R,
            cold_start_trials=COLD_START_TRIALS,
        )
        m = evaluate_estimator(name, loop.y_true, loop.y_prob)
        planned = [o for o in loop.outcomes if o.status != "WARMUP"]
        commit_rate = sum(1 for o in planned if o.committed) / max(1, len(planned))
        validation[name] = {"metrics": m, "commit_rate": commit_rate}
        print(f"  {name:30s} brier={m.brier:.5f} logloss={m.log_loss:.5f} "
              f"ece={m.ece:.5f} commit={commit_rate:.3f}")

    best = min(validation, key=lambda k: validation[k]["metrics"].brier)
    best_brier = validation[best]["metrics"].brier
    eligible = [
        n for n in SIMPLICITY_ORDER
        if n in validation
        and validation[n]["metrics"].brier <= best_brier + BRIER_TOLERANCE
    ]
    family = eligible[0]
    print(f"\n  best by Brier    : {best} ({best_brier:.5f})")
    print(f"  within tolerance : {eligible}")
    print(f"  SELECTED family  : {family}")

    base = candidates[family]
    train_loop = run_closed_loop(
        base, TARGET_SLO, DEADLINE_TAU_MS, train_eps, mode=STRICT_SLO,
        exploration_interval=EXPLORATION_INTERVAL_R,
        cold_start_trials=COLD_START_TRIALS,
    )
    platt = SigmoidCalibratedEstimator(base).fit_from_predictions(
        train_loop.y_prob, train_loop.y_true
    )
    variants = {"uncalibrated": base, "platt": platt}
    variant_metrics = {}
    print("\n== calibration variant selection (validation only) ==")
    for label, est in variants.items():
        loop = run_closed_loop(
            est, TARGET_SLO, DEADLINE_TAU_MS, val_eps, mode=STRICT_SLO,
            exploration_interval=EXPLORATION_INTERVAL_R,
            cold_start_trials=COLD_START_TRIALS,
        )
        m = evaluate_estimator(f"{family}/{label}", loop.y_true, loop.y_prob)
        variant_metrics[label] = m
        print(f"  {label:14s} brier={m.brier:.5f} ece={m.ece:.5f}")
    best_vb = min(m.brier for m in variant_metrics.values())
    tied = [k for k, m in variant_metrics.items() if m.brier <= best_vb + BRIER_TOLERANCE]
    chosen = "uncalibrated" if "uncalibrated" in tied else min(
        tied, key=lambda k: variant_metrics[k].ece
    )
    selected = variants[chosen]
    print(f"  SELECTED variant : {chosen}")

    frozen_dir = REPO_ROOT / "frozen"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    path = frozen_dir / "frozen_estimator_v3.pkl"
    artifact = freeze_estimator(
        selected, path, model_family=family,
        hyperparameters=(
            {k: str(v) for k, v in selected.model.get_params().items()}
            if hasattr(selected, "model") else {}
        ),
        deadline_tau_ms=DEADLINE_TAU_MS,
        train_dataset_id="v3train-v1", validation_dataset_id="v3val-v1",
        calibration=None if chosen == "uncalibrated" else "platt-sigmoid-on-train",
    )
    print(f"\n  frozen -> {path.name}  sha={artifact.artifact_sha256[:26]}...")

    policy = {
        "protocol": "symmetric-generator-v3",
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "git_commit": commit, "git_dirty": dirty,
        "deadline_tau_ms": DEADLINE_TAU_MS, "target_slo_probability": TARGET_SLO,
        "exploration_interval_R": EXPLORATION_INTERVAL_R,
        "cold_start_trials": COLD_START_TRIALS,
        "injection_config_hash_v3": injection_config_hash_v3(),
        "feature_schema_hash_v2": feature_schema_hash_v2(),
        "train": {"episodes": [p.episode_id for p in train_plans],
                  "seeds": [p.seed for p in train_plans], "rows": len(rows)},
        "validation": {"episodes": [p.episode_id for p in val_plans],
                       "seeds": [p.seed for p in val_plans]},
        "training_protocol": "policy-faithful partial-observation features; "
                             "labels from evaluator ground truth only",
        "collection_calls": collection_stats,
        "static_subset_success_train": train_static,
        "static_subset_success_validation": val_static,
        "BEST_FIXED_K2": best_pair,
        "best_fixed_k2_selected_on": "TRAIN+VALIDATION only",
        "validation_metrics": {
            k: {**v["metrics"].to_dict(), "commit_rate": round(v["commit_rate"], 4)}
            for k, v in validation.items()
        },
        "selected_family": family,
        "calibration_variants": {k: m.to_dict() for k, m in variant_metrics.items()},
        "selected_variant": chosen,
        "frozen_artifact": artifact.metadata(),
        "GO_NO_GO_RULE": (
            "GO if adaptive is not Pareto-dominated by BEST_FIXED_K2 and shows "
            "a material cost-success advantage; NO-GO if BEST_FIXED_K2 "
            "dominates or the benefit is negligible after warmup+exploration "
            "cost. Frozen before the holdout."
        ),
    }
    (out_dir / "v3_pipeline_report.json").write_text(
        json.dumps(policy, indent=2), encoding="utf-8"
    )
    (REPO_ROOT / "frozen" / "v3_policy.json").write_text(
        json.dumps(
            {
                "BEST_FIXED_K2": best_pair,
                "target_slo_probability": TARGET_SLO,
                "deadline_tau_ms": DEADLINE_TAU_MS,
                "exploration_interval_R": EXPLORATION_INTERVAL_R,
                "cold_start_trials": COLD_START_TRIALS,
                "estimator_family": family,
                "calibration": chosen,
                "artifact_sha256": artifact.artifact_sha256,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  policy -> frozen/v3_policy.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
