"""Measurement-apparatus qualification against the docker compose deployment.

Runs a SMALL number of shadow trials -- only enough to verify the schema,
concurrency behaviour, provenance binding, analysis correctness and
reproducibility. This is not a characterization experiment: N is deliberately
tiny, no statistics are computed, and nothing is tuned.

CONTROLLED LOCAL QUALIFICATION. All resolvers share one Docker host and every
condition is injected by us. No result here describes real DID resolvers.

Usage:
    python scripts/measurement_qualification.py [--trials 3] [--seed 20260906]
Exit code 0 = all gates pass, 1 = at least one gate failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.analysis import (  # noqa: E402
    QUALIFICATION_LABEL,
    audit_dataset,
    derive_all,
    load_shadow_dataset,
    summarize,
)
from avdr.config import load_router_config  # noqa: E402
from avdr.provenance import build_provenance, new_experiment_id  # noqa: E402
from avdr.scenarios import apply_scenario, load_scenarios, reset_resolvers  # noqa: E402
from avdr.shadow import PARALLEL, SEQUENTIAL, ShadowProbe  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

PHASE = "measurement-qualification"

# Host-side admin endpoints published by docker compose.
ADMIN_URLS = {
    "resolver-a": "http://127.0.0.1:8001",
    "resolver-b": "http://127.0.0.1:8002",
    "resolver-c": "http://127.0.0.1:8003",
}

# Scenarios exercised. Q has KNOWN ground truth and drives the analysis gate.
QUALIFICATION_SCENARIOS = ["N", "Q", "D", "T"]


class Gates:
    def __init__(self) -> None:
        self.results: dict[str, bool] = {}
        self.notes: dict[str, str] = {}

    def record(self, gate: str, passed: bool, note: str = "") -> bool:
        self.results[gate] = self.results.get(gate, True) and passed
        if note:
            self.notes[gate] = note
        print(f"  [{'PASS' if passed else 'FAIL'}] {gate}: {note}")
        return passed

    def all_passed(self) -> bool:
        return bool(self.results) and all(self.results.values())


def wait_for_health(client: httpx.Client, url: str, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    last_error = None
    while time.time() < deadline:
        try:
            response = client.get(f"{url}/health", timeout=3.0)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"{url} did not become healthy: {last_error}")


async def run_trials(config, sink, scenario, provenance_template, dids, mode):
    """Run one shadow trial per DID under an already-applied scenario."""
    probe = ShadowProbe(config)
    trials = []
    async with httpx.AsyncClient() as client:
        for did in dids:
            trial = await probe.run_trial(
                client=client, did=did, provenance=provenance_template, mode=mode
            )
            for observation in trial.observations:
                sink.record_shadow_observation(observation)
            sink.record_shadow_trial(trial.record)
            trials.append(trial)
    return trials


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3, help="trials per scenario")
    parser.add_argument("--seed", type=int, default=20260906)
    parser.add_argument(
        "--telemetry-dir", default=str(REPO_ROOT / "telemetry" / "qualification")
    )
    parser.add_argument(
        "--out", default=str(REPO_ROOT / "artifacts" / "measurement_qualification.json")
    )
    parser.add_argument("--skip-regression", action="store_true")
    args = parser.parse_args()

    if args.trials > 10:
        print("refusing: this is apparatus qualification, not an experiment.")
        print("N is intentionally small; use <= 10 trials per scenario.")
        return 1

    gates = Gates()
    config = load_router_config()
    scenarios = load_scenarios()
    sink = TelemetrySink(args.telemetry_dir)
    experiment_id = new_experiment_id("qual")
    rng = random.Random(args.seed)

    router_config_payload = json.loads(config.model_dump_json())

    print(f"experiment_id = {experiment_id}")
    print(f"label         = {QUALIFICATION_LABEL}")
    print(f"seed          = {args.seed}\n")

    # ---------------- existing baseline regression ----------------
    print("== existing baseline regression ==")
    if args.skip_regression:
        gates.record("PASS_EXISTING_BASELINE_REGRESSION", False, "skipped by flag")
    else:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "e2e_smoke.py")],
            capture_output=True,
            text=True,
        )
        tail = [l for l in result.stdout.splitlines() if "=" in l and "PASS" in l]
        gates.record(
            "PASS_EXISTING_BASELINE_REGRESSION",
            result.returncode == 0,
            f"e2e_smoke.py exit={result.returncode}, {len(tail)} gates reported",
        )

    with httpx.Client() as client:
        wait_for_health(client, "http://127.0.0.1:8000")
        for url in ADMIN_URLS.values():
            wait_for_health(client, url)

        all_trials = []
        derivations_by_scenario: dict[str, list] = {}

        for scenario_id in QUALIFICATION_SCENARIOS:
            scenario = scenarios[scenario_id]
            print(f"\n== scenario {scenario_id} ({scenario.name}) ==")
            reset_resolvers(client, ADMIN_URLS)
            acknowledged = apply_scenario(client, scenario, ADMIN_URLS)

            injection_payload = scenario.injection_config_payload(config.resolver_ids())
            provenance = build_provenance(
                experiment_id=experiment_id,
                scenario_id=scenario_id,
                phase=PHASE,
                repo_root=REPO_ROOT,
                router_config_payload=router_config_payload,
                injection_config_payload=injection_payload,
                seed=args.seed,
            )
            if provenance.unresolved:
                print(f"  provenance unresolved: {provenance.unresolved}")

            # Verify the deployment actually applied what we asked for.
            requested = scenario.resolved_behaviors(config.resolver_ids())
            drift = [r for r in requested if acknowledged.get(r) != requested[r]]
            if drift:
                print(f"  WARNING: resolver behaviour drift on {drift}")

            dids = [
                f"did:example:qual-{scenario_id}-{rng.randrange(10**8):08d}"
                for _ in range(args.trials)
            ]
            mode = SEQUENTIAL if scenario_id == "D" else PARALLEL
            trials = asyncio.run(
                run_trials(config, sink, scenario, provenance, dids, mode)
            )
            all_trials.extend(trials)

            skews = [
                t.record.launch_skew_ms
                for t in trials
                if t.record.launch_skew_ms is not None
            ]
            print(
                f"  {len(trials)} {mode} trials, "
                f"complete={sum(1 for t in trials if t.complete)}/{len(trials)}, "
                f"launch skew {min(skews):.3f}-{max(skews):.3f} ms"
                if skews
                else f"  {len(trials)} {mode} trials"
            )

            grouped_obs = [o.model_dump(mode="json") for t in trials for o in t.observations]
            grouped_trials = [t.record.model_dump(mode="json") for t in trials]
            derivations_by_scenario[scenario_id] = derive_all(grouped_trials, grouped_obs)

        reset_resolvers(client, ADMIN_URLS)

    # ---------------- gates ----------------
    print("\n== gates ==")

    # Provenance binding.
    trials_rows, observation_rows = load_shadow_dataset(args.telemetry_dir)
    mine_trials = [t for t in trials_rows if t["experiment_id"] == experiment_id]
    mine_obs = [o for o in observation_rows if o["experiment_id"] == experiment_id]
    provenance_complete = [
        t
        for t in mine_trials
        if t["git_commit"] and t["config_hash"] and t["injection_config_hash"]
    ]
    distinct_injection_hashes = {t["injection_config_hash"] for t in mine_trials}
    gates.record(
        "PASS_PROVENANCE_BINDING",
        len(provenance_complete) == len(mine_trials)
        and len(mine_trials) > 0
        and len(distinct_injection_hashes) == len(QUALIFICATION_SCENARIOS),
        f"{len(provenance_complete)}/{len(mine_trials)} trials fully stamped; "
        f"{len(distinct_injection_hashes)} distinct injection hashes for "
        f"{len(QUALIFICATION_SCENARIOS)} scenarios",
    )

    # All-resolver observation.
    expected_total = len(mine_trials) * len(config.resolvers)
    gates.record(
        "PASS_SHADOW_ALL_RESOLVER_OBSERVATION",
        len(mine_obs) == expected_total
        and all(t["actual_observations"] == t["expected_observations"] for t in mine_trials),
        f"{len(mine_obs)}/{expected_total} observations "
        f"({len(config.resolvers)} per trial x {len(mine_trials)} trials)",
    )

    # Parallel measurement path.
    parallel_trials = [t for t in mine_trials if t["mode"] == "parallel"]
    sequential_trials = [t for t in mine_trials if t["mode"] == "sequential"]
    parallel_skews = [
        t["launch_skew_ms"] for t in parallel_trials if t["launch_skew_ms"] is not None
    ]
    gates.record(
        "PASS_PARALLEL_MEASUREMENT_PATH",
        len(parallel_trials) > 0
        and len(sequential_trials) > 0
        and len(parallel_skews) == len(parallel_trials),
        f"{len(parallel_trials)} parallel + {len(sequential_trials)} sequential "
        f"trials; parallel launch skew "
        f"{min(parallel_skews):.3f}-{max(parallel_skews):.3f} ms (observed, "
        f"not assumed zero)",
    )

    # Trial completeness + integrity audit.
    audit = audit_dataset(mine_trials, mine_obs)
    gates.record(
        "PASS_TRIAL_COMPLETENESS_CHECK",
        audit["passed"]
        and all(t["complete"] for t in mine_trials),
        f"{len(audit['violations'])} integrity violations; "
        f"{sum(1 for t in mine_trials if t['complete'])}/{len(mine_trials)} "
        f"trials complete",
    )
    for violation in audit["violations"][:10]:
        print(f"      violation: {violation}")

    # Analysis correctness, judged against KNOWN injected ground truth.
    n_derivations = derivations_by_scenario.get("N", [])
    q_derivations = derivations_by_scenario.get("Q", [])
    n_ok = bool(n_derivations) and all(
        d.analyzable and d.fastest_matches_fastest_accepted is True
        for d in n_derivations
    )
    q_ok = bool(q_derivations) and all(
        d.analyzable
        and d.fastest_responding_resolver == "resolver-a"
        and d.fastest_accepted_resolver == "resolver-b"
        and d.fastest_matches_fastest_accepted is False
        for d in q_derivations
    )
    gates.record(
        "PASS_FASTEST_VS_FASTEST_ACCEPTED_ANALYSIS",
        n_ok and q_ok,
        f"scenario N (all valid) -> match on {len(n_derivations)}/"
        f"{len(n_derivations)} trials; scenario Q (injected fast-invalid) -> "
        f"fastest=resolver-a, fastest_accepted=resolver-b, mismatch on "
        f"{len(q_derivations)}/{len(q_derivations)} trials",
    )

    all_derivations = [d for ds in derivations_by_scenario.values() for d in ds]
    overall = summarize(all_derivations)

    print("\n== gate summary ==")
    for gate in sorted(gates.results):
        print(f"  {gate} = {'PASS' if gates.results[gate] else 'FAIL'}")

    print(f"\n== derived summary ({QUALIFICATION_LABEL}) ==")
    for key, value in overall.items():
        print(f"  {key}: {value}")
    print(
        "  NOTE: counts only. These are apparatus-qualification outputs under\n"
        "  controlled injection on one shared host, not findings about real\n"
        "  DID resolvers."
    )

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "experiment_id": experiment_id,
                "phase": PHASE,
                "label": QUALIFICATION_LABEL,
                "seed": args.seed,
                "trials_per_scenario": args.trials,
                "scenarios": QUALIFICATION_SCENARIOS,
                "disclaimer": (
                    "CONTROLLED INJECTION on a single shared Docker host. "
                    "Resolver instances are not independent gateways. Nothing "
                    "here measures real DID infrastructure. Apparatus "
                    "qualification only."
                ),
                "gates": gates.results,
                "notes": gates.notes,
                "audit": audit,
                "summary": overall,
                "derivations": {
                    scenario_id: [d.to_dict() for d in derivations]
                    for scenario_id, derivations in derivations_by_scenario.items()
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nreport written to {output}")

    return 0 if gates.all_passed() else 1


if __name__ == "__main__":
    sys.exit(main())
