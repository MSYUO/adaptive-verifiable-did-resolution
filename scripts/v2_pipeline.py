"""V2 pipeline: closed-loop partial-feedback estimator development.

CONTROLLED LOCAL QUALIFICATION. Local mock providers only, zero public network.

Protocol
--------
TRAIN corpus is collected with FULL observation (observation_source =
audit_collection): every provider is probed, which is how an operator would
bootstrap before any policy exists. VALIDATION is scored under the DEPLOYMENT
protocol -- each candidate estimator drives its own closed loop and sees only
what it selected -- so model selection happens under deployment-like
conditions rather than full information.

The resulting train/deploy feature-distribution shift (training features are
never stale; deployment features often are) is a known property of this setup
and is reported, not hidden.

SELECTION RULE, fixed before any V2 validation number was seen [DESIGN CHOICE]
    rank by validation Brier; among variants within 0.005 absolute Brier of the
    best prefer, in order: (1) lower complexity, (2) lower ECE, (3) the
    UNCALIBRATED variant. Simplicity order:
    b0 < b1 < b2-backoff < m1 < m2.

CALIBRATION RULE, fixed at the same time [DESIGN CHOICE]
    For the selected family, evaluate {uncalibrated, platt} on VALIDATION and
    apply the same tie-break above. Calibration may validly be NONE -- V1
    showed that forcing it on an ECE threshold can degrade quality, so no
    threshold rule is used.

Usage:
    docker compose up -d
    python scripts/v2_pipeline.py
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
    STRICT_SLO,
    DeploymentObservedHistory,
    build_partial_context,
    build_row_v2,
    feature_schema_hash_v2,
    reveal_subset,
    AUDIT_COLLECTION,
)
from avdr.learning.dataset import (  # noqa: E402
    ADMIN_URLS,
    DEADLINE_TAU_MS,
    DatasetRow,
    all_subsets,
    generate_episode,
    subset_target,
)
from avdr.learning.environment import injection_config_hash, plan_episodes  # noqa: E402
from avdr.learning.estimators import (  # noqa: E402
    BackoffSubsetRateEstimator,
    GlobalRateEstimator,
    RollingEmpiricalEstimator,
    SigmoidCalibratedEstimator,
    build_gradient_boosting_v2,
    build_logistic_v2,
    freeze_estimator,
)
from avdr.learning.metrics import evaluate_estimator  # noqa: E402
from avdr.learning.runner_v2 import run_closed_loop  # noqa: E402
from avdr.profiles import PROFILE_W3C_BASIC_V1  # noqa: E402
from avdr.provenance import config_hash, resolve_git_commit, resolve_git_dirty  # noqa: E402

# [DESIGN CHOICE] V2 seeds, disjoint from every V1 seed (100k / 300k / 900k).
TRIALS_PER_EPISODE = 22
TRAIN_EPISODES, TRAIN_SEED = 22, 500_000
VAL_EPISODES, VAL_SEED = 9, 600_000
TARGET_SLO = 0.90
SIMPLICITY_ORDER = [
    "b0-global-rate", "b1-rolling-empirical", "b2-ewma-backoff",
    "m1-logistic-v2", "m2-hist-gradient-boosting-v2",
]
BRIER_TOLERANCE = 0.005


def provider_entries() -> dict[str, ProviderEntry]:
    return {
        pid: ProviderEntry(
            id=pid, endpoint=url, adapter="universal-resolver-v1",
            supported_did_methods=["example"], implementation_id="avdr-mock-resolver",
        )
        for pid, url in ADMIN_URLS.items()
    }


def dependency_lock_hash() -> str | None:
    try:
        return config_hash(
            {
                n: (REPO_ROOT / n).read_text(encoding="utf-8")
                for n in ("requirements.txt", "requirements-dev.txt")
            }
        )
    except OSError:
        return None


def collect(split: str, count: int, seed: int, entries) -> tuple[list, dict]:
    """Ground-truth collection: all providers probed (audit_collection)."""
    plans = plan_episodes(split, count, seed, TRIALS_PER_EPISODE)
    episodes: dict[str, list] = {}
    for i, plan in enumerate(plans, 1):
        trials, _ = generate_episode(plan, entries)
        episodes[plan.episode_id] = trials
        print(f"    {plan.episode_id} ({i}/{len(plans)})", flush=True)
    return plans, episodes


def training_rows(episodes: dict[str, list], tau_ms: float) -> list[DatasetRow]:
    """Build V2 feature rows from a fully observed audit corpus.

    Even here the features are built through the DeploymentObservedHistory
    type, so training and deployment share one code path; the corpus simply
    happens to be complete.
    """
    rows: list[DatasetRow] = []
    subsets = all_subsets()
    for episode_id, records in episodes.items():
        deployment = DeploymentObservedHistory()
        for record in sorted(records, key=lambda r: r.trial_index):
            if not record.complete:
                continue
            context = build_partial_context(deployment, tau_ms, record.trial_index)
            for subset in subsets:
                rows.append(
                    DatasetRow(
                        episode_id=episode_id, trial_index=record.trial_index,
                        split="train", subset=subset,
                        features=build_row_v2(context, subset),
                        target=subset_target(record, subset, tau_ms),
                        hidden_state=record.hidden_state,
                    )
                )
            deployment.append(
                reveal_subset(record, list(record.observations), AUDIT_COLLECTION)
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "artifacts" / "learning_v2"))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)

    print("V2 CLOSED-LOOP PIPELINE -- CONTROLLED LOCAL QUALIFICATION")
    print(f"  git_commit={commit} git_dirty={dirty}")
    print(f"  tau={DEADLINE_TAU_MS} ms  target={TARGET_SLO}  R={EXPLORATION_INTERVAL_R}")
    print(f"  feature schema v2 = {feature_schema_hash_v2()[:26]}...\n")

    entries = provider_entries()
    with httpx.Client() as admin:
        for url in ADMIN_URLS.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

    print(f"== collecting TRAIN corpus ({TRAIN_EPISODES} episodes, full observation) ==")
    train_plans, train_episodes = collect("v2train", TRAIN_EPISODES, TRAIN_SEED, entries)
    print(f"== collecting VALIDATION ground truth ({VAL_EPISODES} episodes) ==")
    val_plans, val_episodes = collect("v2val", VAL_EPISODES, VAL_SEED, entries)

    rows = training_rows(train_episodes, DEADLINE_TAU_MS)
    print(f"\n  train rows={len(rows)}  base rate="
          f"{sum(r.target for r in rows)/len(rows):.4f}")

    candidates = {
        "b0-global-rate": GlobalRateEstimator().fit(rows),
        "b1-rolling-empirical": RollingEmpiricalEstimator().fit(rows),
        "b2-ewma-backoff": BackoffSubsetRateEstimator().fit(rows),
        "m1-logistic-v2": build_logistic_v2().fit(rows),
        "m2-hist-gradient-boosting-v2": build_gradient_boosting_v2().fit(rows),
    }

    print("\n== VALIDATION under the DEPLOYMENT protocol (partial feedback) ==")
    print("   each candidate drives its OWN closed loop and sees only its own calls")
    validation = {}
    for name, estimator in candidates.items():
        loop = run_closed_loop(
            estimator, TARGET_SLO, DEADLINE_TAU_MS, val_episodes,
            mode=STRICT_SLO, exploration_interval=EXPLORATION_INTERVAL_R,
        )
        metrics = evaluate_estimator(name, loop.y_true, loop.y_prob)
        commit_rate = sum(1 for o in loop.outcomes if o.committed) / len(loop.outcomes)
        validation[name] = {"metrics": metrics, "commit_rate": commit_rate}
        print(f"  {name:30s} brier={metrics.brier:.5f} logloss={metrics.log_loss:.5f} "
              f"ece={metrics.ece:.5f} commit={commit_rate:.3f}")

    best = min(validation, key=lambda k: validation[k]["metrics"].brier)
    best_brier = validation[best]["metrics"].brier
    eligible = [
        n for n in SIMPLICITY_ORDER
        if n in validation
        and validation[n]["metrics"].brier <= best_brier + BRIER_TOLERANCE
    ]
    family = eligible[0]
    print(f"\n  best by Brier       : {best} ({best_brier:.5f})")
    print(f"  within tolerance    : {eligible}")
    print(f"  SELECTED family     : {family}")

    # ---- calibration variant selection, VALIDATION only ----
    print("\n== calibration variant selection (validation only) ==")
    base = candidates[family]
    train_loop = run_closed_loop(
        base, TARGET_SLO, DEADLINE_TAU_MS, train_episodes,
        mode=STRICT_SLO, exploration_interval=EXPLORATION_INTERVAL_R,
    )
    platt = SigmoidCalibratedEstimator(base).fit_from_predictions(
        train_loop.y_prob, train_loop.y_true
    )
    variants = {"uncalibrated": base, "platt": platt}
    variant_metrics = {}
    for label, estimator in variants.items():
        loop = run_closed_loop(
            estimator, TARGET_SLO, DEADLINE_TAU_MS, val_episodes,
            mode=STRICT_SLO, exploration_interval=EXPLORATION_INTERVAL_R,
        )
        m = evaluate_estimator(f"{family}/{label}", loop.y_true, loop.y_prob)
        variant_metrics[label] = m
        print(f"  {label:14s} brier={m.brier:.5f} logloss={m.log_loss:.5f} "
              f"ece={m.ece:.5f}")

    best_variant_brier = min(m.brier for m in variant_metrics.values())
    tied = [
        k for k, m in variant_metrics.items()
        if m.brier <= best_variant_brier + BRIER_TOLERANCE
    ]
    # Tie-break: prefer uncalibrated, else lower ECE.
    if "uncalibrated" in tied:
        chosen_variant = "uncalibrated"
    else:
        chosen_variant = min(tied, key=lambda k: variant_metrics[k].ece)
    selected = variants[chosen_variant]
    print(f"  variants within tolerance: {tied}")
    print(f"  SELECTED variant   : {chosen_variant}")

    frozen_dir = REPO_ROOT / "frozen"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = frozen_dir / "frozen_estimator_v2.pkl"
    artifact = freeze_estimator(
        selected, artifact_path,
        model_family=family,
        hyperparameters=(
            {k: str(v) for k, v in selected.model.get_params().items()}
            if hasattr(selected, "model") else {}
        ),
        deadline_tau_ms=DEADLINE_TAU_MS,
        train_dataset_id="v2train-v1",
        validation_dataset_id="v2val-v1",
        calibration=None if chosen_variant == "uncalibrated" else "platt-sigmoid-on-train",
    )
    print(f"\n  frozen -> {artifact_path.name}")
    print(f"  artifact_sha256 = {artifact.artifact_sha256}")

    report = {
        "protocol": "closed-loop-partial-feedback-v2",
        "supersedes_evaluation_protocol_of": "full-information-shadow-v1",
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "git_commit": commit, "git_dirty": dirty,
        "deadline_tau_ms": DEADLINE_TAU_MS,
        "target_slo_probability": TARGET_SLO,
        "exploration_interval_R": EXPLORATION_INTERVAL_R,
        "feature_schema_hash_v2": feature_schema_hash_v2(),
        "injection_config_hash": injection_config_hash(),
        "dependency_lock_hash": dependency_lock_hash(),
        "acceptance_profile": PROFILE_W3C_BASIC_V1,
        "train": {
            "episodes": [p.episode_id for p in train_plans],
            "seeds": [p.seed for p in train_plans], "rows": len(rows),
            "collection": "full observation (audit_collection)",
        },
        "validation": {
            "episodes": [p.episode_id for p in val_plans],
            "seeds": [p.seed for p in val_plans],
            "protocol": "closed-loop partial feedback, per-candidate history",
        },
        "selection_rule": (
            f"lowest validation Brier; within {BRIER_TOLERANCE} prefer lower "
            f"complexity, then lower ECE, then uncalibrated. Order "
            f"{SIMPLICITY_ORDER}"
        ),
        "validation_metrics": {
            k: {**v["metrics"].to_dict(), "commit_rate": round(v["commit_rate"], 4)}
            for k, v in validation.items()
        },
        "selected_family": family,
        "calibration_variants": {k: m.to_dict() for k, m in variant_metrics.items()},
        "selected_variant": chosen_variant,
        "frozen_artifact": artifact.metadata(),
        "known_shift": (
            "TRAIN features come from a fully observed audit corpus; deployment "
            "features are partial and often stale. The V2 holdout measures the "
            "effect of that shift."
        ),
        "disclaimer": (
            "Controlled injection on one shared host. No claim about real DID "
            "reliability, latency, independence or production SLOs."
        ),
    }
    (out_dir / "v2_pipeline_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(f"  report -> {out_dir / 'v2_pipeline_report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
