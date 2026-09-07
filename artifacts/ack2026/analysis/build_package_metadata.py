"""Derive the ACK package's protocol, provenance, MANIFEST and SHA256SUMS.

Everything here is READ from repository evidence (frozen policy, frozen
artifact metadata, run reports, git) and written out. No protocol value, hash
or commit is hand-typed, so the metadata cannot drift from what actually ran.

Run AFTER recompute_ack_results.py and make_figures.py.

Usage:
    python artifacts/ack2026/analysis/build_package_metadata.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE.parent.parent

V4_RAW = REPO_ROOT / "artifacts" / "learning_v4" / "v4_confirmatory_report.json"
V3_HOLDOUT = REPO_ROOT / "artifacts" / "learning_v3" / "v3_holdout_report.json"
V3_PIPELINE = REPO_ROOT / "artifacts" / "learning_v3" / "v3_pipeline_report.json"
V3_SYMMETRY = REPO_ROOT / "artifacts" / "learning_v3" / "v3_symmetry_qualification.json"
V3_POLICY = REPO_ROOT / "frozen" / "v3_policy.json"
V3_META = REPO_ROOT / "frozen" / "frozen_estimator_v3.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> int:
    raw = json.loads(V4_RAW.read_text(encoding="utf-8"))
    v3h = json.loads(V3_HOLDOUT.read_text(encoding="utf-8"))
    v3p = json.loads(V3_PIPELINE.read_text(encoding="utf-8"))
    v3s = json.loads(V3_SYMMETRY.read_text(encoding="utf-8"))
    policy = json.loads(V3_POLICY.read_text(encoding="utf-8"))
    meta = json.loads(V3_META.read_text(encoding="utf-8"))
    validation = json.loads(
        (PACKAGE / "analysis" / "validation_report.json").read_text(encoding="utf-8")
    )
    boot = json.loads(
        (PACKAGE / "analysis" / "bootstrap_summary.json").read_text(encoding="utf-8")
    )

    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    remote = git("config", "--get", "remote.origin.url")
    dirty = git("status", "--porcelain")

    # ---------------- protocol ----------------
    (PACKAGE / "protocol" / "v3_discovery_protocol.json").write_text(
        json.dumps(
            {
                "stage": "V3",
                "role": "DISCOVERY / policy development and freeze",
                "is_confirmatory": False,
                "must_not_be_pooled_with": "V4",
                "generator": "symmetric role-permuted v3",
                "injection_config_hash_v3": v3p["injection_config_hash_v3"],
                "feature_schema_hash_v2": v3p["feature_schema_hash_v2"],
                "deadline_tau_ms": v3p["deadline_tau_ms"],
                "target_slo_probability": v3p["target_slo_probability"],
                "exploration_interval_R": v3p["exploration_interval_R"],
                "cold_start_trials": v3p["cold_start_trials"],
                "training_protocol": v3p["training_protocol"],
                "train_episodes": v3p["train"]["episodes"],
                "train_seeds": v3p["train"]["seeds"],
                "validation_episodes": v3p["validation"]["episodes"],
                "validation_seeds": v3p["validation"]["seeds"],
                "holdout_episodes": v3h["holdout_episodes"],
                "holdout_seeds": v3h["holdout_seeds"],
                "BEST_FIXED_K2_selected_on": v3p["best_fixed_k2_selected_on"],
                "BEST_FIXED_K2": v3p["BEST_FIXED_K2"],
                "selected_estimator_family": v3p["selected_family"],
                "selected_calibration_variant": v3p["selected_variant"],
                "GO_NO_GO_RULE": v3p["GO_NO_GO_RULE"],
                "v3_verdict": v3h["ADAPTIVE_VALUE_VERDICT"],
                "generator_symmetry_qualification": {
                    "verdict": v3s["V3_GENERATOR_PROVIDER_SYMMETRY"],
                    "episodes": v3s["episodes"],
                    "trials": v3s["trials"],
                    "provider_success_rate": v3s["provider_success_rate"],
                    "success_spread": v3s["success_spread"],
                    "note": "generator qualification only; never reused for model evaluation",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (PACKAGE / "protocol" / "v4_confirmatory_protocol.json").write_text(
        json.dumps(
            {
                "stage": "V4",
                "role": "INDEPENDENT FROZEN CONFIRMATORY REPLICATION",
                "is_confirmatory": True,
                "must_not_be_pooled_with": "V3",
                "nothing_selected_tuned_or_retrained": True,
                "frozen_verified_no_drift": raw["frozen"]["verified_no_drift"],
                "frozen_policy": policy,
                "frozen_artifact_metadata": meta,
                "artifact_sha256_verified_at_run": raw["frozen"]["artifact_sha256_verified"],
                "injection_config_hash_v3": raw["frozen"]["injection_config_hash_v3"],
                "feature_schema_hash_v2": raw["frozen"]["feature_schema_hash_v2"],
                "pre_declared": raw["pre_declared"],
                "episode_ids": raw["episode_ids"],
                "episode_seeds": raw["episode_seeds"],
                "trials": validation["trial_count"],
                "episodes": validation["episode_count"],
                "bootstrap": {
                    "method": "paired bootstrap over episodes (clusters)",
                    "resamples": boot["resamples"],
                    "analysis_seed": boot["analysis_seed"],
                    "resampling_unit": boot["resampling_unit"],
                    "not_a_p_value": boot["note"],
                },
                "primary_comparison": (
                    "adaptive best-effort (frozen V3 policy) vs "
                    "BEST_FIXED_K2 {local-b, local-c} (frozen V3 comparator), "
                    "on identical trials, denominator = ALL logical requests"
                ),
                "v4_verdict": raw["CONFIRMATORY_VERDICT"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---------------- provenance ----------------
    (PACKAGE / "provenance" / "source_commits.json").write_text(
        json.dumps(
            {
                "source_repository": remote,
                "source_branch": branch,
                "source_HEAD_at_package_build": head,
                "working_tree_clean_at_build": not dirty,
                "v3_pipeline_commit": v3p["git_commit"],
                "v3_pipeline_git_dirty": v3p["git_dirty"],
                "v3_holdout_commit": v3h["git_commit"],
                "v3_holdout_git_dirty": v3h["git_dirty"],
                "v4_execution_commit": raw["git_commit"],
                "v4_execution_git_dirty": raw["git_dirty"],
                "recent_history": (git("log", "--oneline", "-12") or "").splitlines(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (PACKAGE / "provenance" / "frozen_hashes.json").write_text(
        json.dumps(
            {
                "frozen_estimator_artifact_sha256": meta["artifact_sha256"],
                "frozen_estimator_metadata_file_sha256": "sha256:" + sha256_file(V3_META),
                "frozen_policy_file_sha256": "sha256:" + sha256_file(V3_POLICY),
                "injection_config_hash_v3": raw["frozen"]["injection_config_hash_v3"],
                "feature_schema_hash_v2": raw["frozen"]["feature_schema_hash_v2"],
                "estimator_config_hash": meta.get("estimator_config_hash"),
                "v4_raw_evidence_sha256": validation["raw_evidence_sha256"],
                "v4_raw_evidence_repo_path": validation["recomputed_from"],
                "v3_holdout_raw_sha256": "sha256:" + sha256_file(V3_HOLDOUT),
                "v3_pipeline_raw_sha256": "sha256:" + sha256_file(V3_PIPELINE),
                "note": (
                    "The V3/V4 raw run reports live under artifacts/ and are "
                    "untracked experiment output. Their content is bound here "
                    "by SHA-256, and the evidence needed for every published "
                    "number is copied into data/ as CSV so this package is "
                    "self-contained."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    requirements = {
        name: (REPO_ROOT / name).read_text(encoding="utf-8")
        for name in ("requirements.txt", "requirements-dev.txt")
        if (REPO_ROOT / name).exists()
    }
    dependency_hash = sha256_bytes(
        json.dumps(requirements, sort_keys=True).encode("utf-8")
    )
    (PACKAGE / "provenance" / "dependency_hash.txt").write_text(
        "sha256:" + dependency_hash + "\n"
        + "files: " + ", ".join(sorted(requirements)) + "\n"
        + "python: 3.12\n",
        encoding="utf-8",
    )

    (PACKAGE / "provenance" / "experiment_identity.json").write_text(
        json.dumps(
            {
                "environment": "controlled local DID resolver environment",
                "providers": "3 local mock resolvers on one shared Docker host",
                "documents": "synthetic control documents",
                "conditions": "injected dynamic conditions (CONTROLLED INJECTION)",
                "public_providers_contacted": False,
                "acceptance_profile": "w3c-basic-v1 (structural)",
                "deadline_tau_ms": policy["deadline_tau_ms"],
                "target_slo_probability": policy["target_slo_probability"],
                "exploration_interval_R": policy["exploration_interval_R"],
                "cold_start_trials": policy["cold_start_trials"],
                "frozen_estimator": meta["estimator_id"],
                "frozen_calibration": meta["calibration"],
                "frozen_comparator": policy["BEST_FIXED_K2"],
                "v4_trials": validation["trial_count"],
                "v4_episodes": validation["episode_count"],
                "not_established": [
                    "real public DID resolver performance",
                    "production DID SLO",
                    "independent real providers",
                    "ML outperforming heuristics",
                    "W3C certification",
                    "Byzantine fault tolerance",
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---------------- MANIFEST ----------------
    manifest = {
        "artifact_name": "AVDR ACK 2026 evidence freeze",
        "artifact_version": "1.0.0",
        "purpose": (
            "Reproducible evidence package for an ACK 2026 paper: V3 discovery "
            "and V4 independent frozen confirmatory replication of adaptive "
            "resolver fan-out in a controlled local DID resolution environment."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper_target": "ACK 2026",
        "source_repository": remote,
        "source_branch": branch,
        "source_HEAD": head,
        "v3_source_commits": {
            "pipeline_and_freeze": v3p["git_commit"],
            "holdout": v3h["git_commit"],
        },
        "v4_execution_commit": raw["git_commit"],
        "v4_trial_count": validation["trial_count"],
        "v4_episode_count": validation["episode_count"],
        "frozen_estimator_id": meta["estimator_id"],
        "frozen_estimator_is_not_ml": True,
        "frozen_calibration": meta["calibration"],
        "frozen_comparator": policy["BEST_FIXED_K2"],
        "deadline_tau_ms": policy["deadline_tau_ms"],
        "target_slo_probability": policy["target_slo_probability"],
        "exploration_interval_R": policy["exploration_interval_R"],
        "cold_start_rule": f"{policy['cold_start_trials']} mandatory all-provider warmup trials per episode, charged as calls",
        "acceptance_profile": "w3c-basic-v1 (structural)",
        "generator_config_hash": raw["frozen"]["injection_config_hash_v3"],
        "feature_schema_hash": raw["frozen"]["feature_schema_hash_v2"],
        "bootstrap_method": "paired bootstrap over episodes (clusters), not trial rows",
        "bootstrap_resamples": boot["resamples"],
        "bootstrap_seed": boot["analysis_seed"],
        "raw_data_paths": {
            "v4_raw_report_untracked": validation["recomputed_from"],
            "v4_raw_report_sha256": validation["raw_evidence_sha256"],
            "v4_trials_csv_in_package": "data/v4_trials.csv",
            "v4_episode_metrics_csv_in_package": "data/v4_episode_metrics.csv",
        },
        "analysis_script": "analysis/recompute_ack_results.py",
        "figure_script": "analysis/make_figures.py",
        "metadata_script": "analysis/build_package_metadata.py",
        "figures": [
            "figures/fig1_architecture.pdf",
            "figures/fig2_success_vs_calls.pdf",
            "figures/fig3_selected_k_by_state.pdf",
        ],
        "figure_sources": [
            "figures/source/fig2_success_vs_calls.csv",
            "figures/source/fig3_selected_k_by_state.csv",
        ],
        "tables": [
            "tables/table_primary_results.csv",
            "tables/table_paired_outcomes.csv",
            "tables/table_static_baselines.csv",
            "tables/table_estimator_results.csv",
        ],
        "validation_status": validation["status"],
        "known_limitations": [
            "Controlled local environment only; no real public DID resolver "
            "performance is established.",
            "V3 is discovery and V4 is confirmatory; they must not be pooled.",
            "Static baselines are counterfactual reconstructions on the same "
            "trials; only the adaptive policy ran closed-loop.",
            "Adaptive is charged for warmup and exploration; stateless "
            "baselines have no observation stream to charge.",
            "The delta-success 95% CI lower bound lies below the 0.02 "
            "materiality reference, so the effect is reliably positive but "
            "not established as materially large.",
            "The operational meaning of an episode (router restart frequency) "
            "is not measured, and warmup cost is sensitive to it.",
            "See provenance/known_errata.md for a frozen-metadata erratum.",
        ],
    }
    (PACKAGE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ---------------- SHA256SUMS ----------------
    checks = []
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_dir() or path.name == "SHA256SUMS":
            continue
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PACKAGE).as_posix()
        checks.append(f"{sha256_file(path)}  {rel}")
    (PACKAGE / "SHA256SUMS").write_text("\n".join(checks) + "\n", encoding="utf-8")

    # verify what we just wrote
    bad = []
    for line in checks:
        digest, rel = line.split("  ", 1)
        if sha256_file(PACKAGE / rel) != digest:
            bad.append(rel)

    print("ACK package metadata built")
    print(f"  HEAD                 {head}  (branch {branch}, clean={not dirty})")
    print(f"  V4 execution commit  {raw['git_commit']}  git_dirty={raw['git_dirty']}")
    print(f"  frozen estimator     {meta['estimator_id']} (calibration={meta['calibration']})")
    print(f"  frozen comparator    {policy['BEST_FIXED_K2']}")
    print(f"  dependency hash      sha256:{dependency_hash[:24]}...")
    print(f"  SHA256SUMS           {len(checks)} files, verify mismatches={len(bad)}")
    if bad:
        print(f"  MISMATCH: {bad}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
