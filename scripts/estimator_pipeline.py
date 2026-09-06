"""Generate controlled data, train/validate estimators, select and freeze.

CONTROLLED LOCAL QUALIFICATION. Local mock providers only; zero public-network
calls. Every injected distribution is chosen by us and describes no real DID
resolver.

Order enforced by this script:

    generate (TRAIN / VALIDATION episodes, disjoint seeds)
        -> fit on TRAIN
        -> select on VALIDATION using a rule fixed in advance
        -> freeze artifact
    (FINAL HOLDOUT is a separate script and is never touched here)

MODEL SELECTION RULE, fixed before any validation number was seen
[DESIGN CHOICE]:
    rank candidates by VALIDATION Brier score; then choose the SIMPLEST
    candidate whose Brier is within 0.005 absolute of the best.
    Simplicity order: b0 < b0s < b1 < b2 < m1 < m2.
Selection never looks at fan-out, and never at holdout.

CALIBRATION RULE, also fixed in advance [DESIGN CHOICE]:
    apply post-hoc sigmoid calibration (fit on TRAIN only) if and only if the
    selected model's uncalibrated VALIDATION ECE exceeds 0.05.

Usage:
    docker compose up -d
    python scripts/estimator_pipeline.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.inventory import ProviderEntry  # noqa: E402
from avdr.learning.dataset import (  # noqa: E402
    ADMIN_URLS,
    DEADLINE_TAU_MS,
    Dataset,
    all_subsets,
    dataset_manifest,
    generate_episode,
    save_dataset,
)
from avdr.learning.environment import injection_config_hash, plan_episodes  # noqa: E402
from avdr.learning.estimators import (  # noqa: E402
    CONTEXT_KEY,
    HISTORY_KEY,
    EwmaEstimator,
    GlobalRateEstimator,
    RollingEmpiricalEstimator,
    SigmoidCalibratedEstimator,
    SubsetRateEstimator,
    build_gradient_boosting,
    build_logistic,
    freeze_estimator,
)
from avdr.learning.features import build_context, build_row  # noqa: E402
from avdr.learning.metrics import evaluate_estimator  # noqa: E402
from avdr.profiles import PROFILE_W3C_BASIC_V1  # noqa: E402
from avdr.provenance import config_hash, resolve_git_commit, resolve_git_dirty  # noqa: E402

# [DESIGN CHOICE] fixed before generation.
TRIALS_PER_EPISODE = 22
TRAIN_EPISODES, TRAIN_SEED = 22, 100_000
VAL_EPISODES, VAL_SEED = 9, 300_000
SIMPLICITY_ORDER = [
    "b0-global-rate", "b0s-subset-rate", "b1-rolling-empirical",
    "b2-ewma", "m1-logistic", "m2-hist-gradient-boosting",
]
BRIER_TOLERANCE = 0.005
ECE_CALIBRATION_THRESHOLD = 0.05


def dependency_lock_hash() -> str | None:
    try:
        payload = {
            n: (REPO_ROOT / n).read_text(encoding="utf-8")
            for n in ("requirements.txt", "requirements-dev.txt")
        }
    except OSError:
        return None
    return config_hash(payload)


def provider_entries() -> dict[str, ProviderEntry]:
    return {
        pid: ProviderEntry(
            id=pid, endpoint=url, adapter="universal-resolver-v1",
            supported_did_methods=["example"], implementation_id="avdr-mock-resolver",
        )
        for pid, url in ADMIN_URLS.items()
    }


def generate(split: str, count: int, seed: int, entries, admin) -> tuple[list, list, list]:
    plans = plan_episodes(split, count, seed, TRIALS_PER_EPISODE)
    trials, rows = [], []
    for i, plan in enumerate(plans, 1):
        episode_trials, episode_rows = generate_episode(plan, entries, admin)
        trials.extend(episode_trials)
        rows.extend(episode_rows)
        print(f"    {plan.episode_id} ({i}/{len(plans)}): "
              f"{len(episode_rows)} rows", flush=True)
    return plans, trials, rows


def replay_history_predictions(estimator, trials, tau_ms, needs_history: bool):
    """Prospective replay: predict each trial from history < t, then reveal.

    Used for the history-dependent baselines (B1/B2) and for any estimator, so
    every candidate is scored under exactly the same prospective protocol.
    """
    y_true, y_prob = [], []
    by_episode: dict[str, list] = {}
    for record in trials:
        by_episode.setdefault(record.episode_id, []).append(record)

    for episode_id, records in by_episode.items():
        records = sorted(records, key=lambda r: r.trial_index)
        history: list = []
        subset_history: dict[tuple[str, ...], list[int]] = {}
        for record in records:
            if not record.complete:
                history.append(record)
                continue
            context = build_context(history, tau_ms, episode_id, record.trial_index)
            payload = {CONTEXT_KEY: context, HISTORY_KEY: subset_history}
            for subset in all_subsets():
                q = estimator.estimate(subset, payload)
                if q is None:
                    continue
                target = int(any(record.within_deadline(p, tau_ms) for p in subset))
                y_true.append(target)
                y_prob.append(q)
            # Reveal only after predicting.
            for subset in all_subsets():
                target = int(any(record.within_deadline(p, tau_ms) for p in subset))
                subset_history.setdefault(subset, []).append(target)
            history.append(record)
    return y_true, y_prob


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "artifacts" / "learning"))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    commit, _ = resolve_git_commit(REPO_ROOT)
    dirty, _ = resolve_git_dirty(REPO_ROOT)
    injection_hash = injection_config_hash()
    lock_hash = dependency_lock_hash()

    print("CONTROLLED LOCAL QUALIFICATION -- estimator pipeline")
    print(f"  tau = {DEADLINE_TAU_MS} ms [DESIGN CHOICE, fixed before generation]")
    print(f"  git_commit={commit} git_dirty={dirty}")
    print(f"  injection_config_hash={injection_hash[:26]}...\n")

    entries = provider_entries()
    with httpx.Client() as admin:
        for url in ADMIN_URLS.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()

        print(f"== generating TRAIN ({TRAIN_EPISODES} episodes) ==")
        train_plans, train_trials, train_rows = generate(
            "train", TRAIN_EPISODES, TRAIN_SEED, entries, admin
        )
        print(f"== generating VALIDATION ({VAL_EPISODES} episodes) ==")
        val_plans, val_trials, val_rows = generate(
            "validation", VAL_EPISODES, VAL_SEED, entries, admin
        )

    train_incomplete = sum(1 for t in train_trials if not t.complete)
    val_incomplete = sum(1 for t in val_trials if not t.complete)

    train_manifest = dataset_manifest(
        "train-v1", train_plans, DEADLINE_TAU_MS, injection_hash, commit, dirty,
        lock_hash, PROFILE_W3C_BASIC_V1, len(train_rows), train_incomplete,
    )
    val_manifest = dataset_manifest(
        "validation-v1", val_plans, DEADLINE_TAU_MS, injection_hash, commit, dirty,
        lock_hash, PROFILE_W3C_BASIC_V1, len(val_rows), val_incomplete,
    )
    save_dataset(out_dir / "train.jsonl", Dataset(rows=train_rows), train_manifest)
    save_dataset(out_dir / "validation.jsonl", Dataset(rows=val_rows), val_manifest)

    train_eps = {p.episode_id for p in train_plans}
    val_eps = {p.episode_id for p in val_plans}
    train_seeds = {p.seed for p in train_plans}
    val_seeds = {p.seed for p in val_plans}
    print(f"\n  train rows={len(train_rows)} episodes={len(train_eps)} "
          f"incomplete_trials={train_incomplete}")
    print(f"  val   rows={len(val_rows)} episodes={len(val_eps)} "
          f"incomplete_trials={val_incomplete}")
    print(f"  episode overlap={len(train_eps & val_eps)} "
          f"seed overlap={len(train_seeds & val_seeds)}")
    base_rate = sum(r.target for r in train_rows) / max(1, len(train_rows))
    print(f"  train base rate (Y=1) = {base_rate:.4f}")

    # ---------------- fit candidates on TRAIN ----------------
    print("\n== fitting candidates on TRAIN ==")
    candidates = {
        "b0-global-rate": GlobalRateEstimator().fit(train_rows),
        "b0s-subset-rate": SubsetRateEstimator().fit(train_rows),
        "b1-rolling-empirical": RollingEmpiricalEstimator().fit(train_rows),
        "b2-ewma": EwmaEstimator().fit(train_rows),
        "m1-logistic": build_logistic().fit(train_rows),
        "m2-hist-gradient-boosting": build_gradient_boosting().fit(train_rows),
    }

    # ---------------- evaluate on VALIDATION ----------------
    print("== validation (prospective replay) ==")
    validation_metrics = {}
    for name, estimator in candidates.items():
        y_true, y_prob = replay_history_predictions(
            estimator, val_trials, DEADLINE_TAU_MS, needs_history=True
        )
        metrics = evaluate_estimator(name, y_true, y_prob)
        validation_metrics[name] = metrics
        print(f"  {name:28s} n={metrics.n:5d} brier={metrics.brier:.5f} "
              f"logloss={metrics.log_loss:.5f} ece={metrics.ece:.5f}")

    # ---------------- selection (rule fixed in advance) ----------------
    best_name = min(validation_metrics, key=lambda k: validation_metrics[k].brier)
    best_brier = validation_metrics[best_name].brier
    eligible = [
        name for name in SIMPLICITY_ORDER
        if name in validation_metrics
        and validation_metrics[name].brier <= best_brier + BRIER_TOLERANCE
    ]
    selected_name = eligible[0]
    selected = candidates[selected_name]
    print(f"\n  best by Brier      : {best_name} ({best_brier:.5f})")
    print(f"  within tolerance   : {eligible}")
    print(f"  SELECTED (simplest): {selected_name}")

    selected_ece = validation_metrics[selected_name].ece
    calibration = None
    calibrated_metrics = None
    if selected_ece > ECE_CALIBRATION_THRESHOLD:
        print(f"  calibration        : APPLYING ({selected_ece:.4f} > "
              f"{ECE_CALIBRATION_THRESHOLD})")
        # Fit Platt scaling on TRAIN predictions only.
        train_q, train_y = [], []
        y_t, y_p = replay_history_predictions(
            selected, train_trials, DEADLINE_TAU_MS, needs_history=True
        )
        train_y, train_q = y_t, y_p
        selected = SigmoidCalibratedEstimator(selected).fit_from_predictions(
            train_q, train_y
        )
        calibration = "platt-sigmoid-on-train"
        cy, cp = replay_history_predictions(
            selected, val_trials, DEADLINE_TAU_MS, needs_history=True
        )
        calibrated_metrics = evaluate_estimator(selected.estimator_id, cy, cp)
        print(f"    post-calibration validation: brier="
              f"{calibrated_metrics.brier:.5f} logloss="
              f"{calibrated_metrics.log_loss:.5f} ece={calibrated_metrics.ece:.5f}")
        print(f"    calibrated estimator id: {selected.estimator_id}")
    else:
        print(f"  calibration        : not applied ({selected_ece:.4f} <= "
              f"{ECE_CALIBRATION_THRESHOLD})")

    # ---------------- freeze ----------------
    # Metadata lives in a TRACKED directory so the frozen identity can be
    # committed before the holdout; the binary itself stays untracked.
    frozen_dir = REPO_ROOT / "frozen"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = frozen_dir / "frozen_estimator.pkl"
    hyperparameters = (
        {k: str(v) for k, v in selected.model.get_params().items()}
        if hasattr(selected, "model")
        else selected.config_hash()
    )
    artifact = freeze_estimator(
        selected, artifact_path,
        model_family=selected_name,
        hyperparameters=hyperparameters if isinstance(hyperparameters, dict) else {},
        deadline_tau_ms=DEADLINE_TAU_MS,
        train_dataset_id="train-v1",
        validation_dataset_id="validation-v1",
        calibration=calibration,
    )
    print(f"\n  frozen -> {artifact_path.name}")
    print(f"  artifact_sha256 = {artifact.artifact_sha256}")

    report = {
        "label": "CONTROLLED LOCAL QUALIFICATION",
        "git_commit": commit,
        "git_dirty": dirty,
        "deadline_tau_ms": DEADLINE_TAU_MS,
        "injection_config_hash": injection_hash,
        "dependency_lock_hash": lock_hash,
        "splits": {
            "train": {
                "episodes": sorted(train_eps), "rows": len(train_rows),
                "seeds": sorted(train_seeds), "incomplete_trials": train_incomplete,
            },
            "validation": {
                "episodes": sorted(val_eps), "rows": len(val_rows),
                "seeds": sorted(val_seeds), "incomplete_trials": val_incomplete,
            },
        },
        "episode_overlap": sorted(train_eps & val_eps),
        "seed_overlap": sorted(train_seeds & val_seeds),
        "train_base_rate": base_rate,
        "selection_rule": (
            "lowest VALIDATION Brier, then simplest candidate within "
            f"{BRIER_TOLERANCE} absolute Brier of the best; simplicity order "
            f"{SIMPLICITY_ORDER}"
        ),
        "validation_metrics": {
            k: {**v.to_dict(), "reliability": v.reliability_table}
            for k, v in validation_metrics.items()
        },
        "best_by_brier": best_name,
        "within_tolerance": eligible,
        "selected_estimator": selected_name,
        "calibration_rule": (
            f"apply sigmoid calibration iff validation ECE > "
            f"{ECE_CALIBRATION_THRESHOLD}"
        ),
        "calibration_applied": calibration,
        "post_calibration_validation_metrics": (
            {**calibrated_metrics.to_dict(),
             "reliability": calibrated_metrics.reliability_table}
            if calibrated_metrics
            else None
        ),
        "frozen_estimator_id": selected.estimator_id,
        "frozen_artifact": artifact.metadata(),
        "disclaimer": (
            "Controlled injection on one shared host. No claim about real DID "
            "resolver reliability, latency, independence or production SLOs."
        ),
    }
    (out_dir / "pipeline_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(f"  report -> {out_dir / 'pipeline_report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
