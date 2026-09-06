"""Real DID resolver compatibility qualification.

Answers ONE question: can the existing apparatus call real DID resolver
implementations, normalize their standards-level results, apply a minimal
method-aware acceptance profile, and preserve provenance -- without confusing
provider/API differences with DID semantics?

NOT a characterization experiment. N is deliberately tiny, no statistics are
computed, nothing is tuned, and NO performance claim may be derived from the
numbers it prints. Latency values are labelled [MEASURED-QUALIFICATION] and
are confounded by launch skew, geography, Cloudflare fronting, and the fact
that one provider runs on this very host.

Rate-limit discipline (public endpoints are testing instances):
  * tiny fixed trial counts
  * conservative pacing between trials
  * HTTP 429 / Retry-After aborts the run rather than being retried around

Usage:
    python scripts/real_did_qualification.py [--pacing 3.0]
Exit 0 = all gates PASS, 1 = any gate FAIL or PARTIAL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.inventory import (  # noqa: E402
    load_fixture_manifest,
    load_provider_inventory,
)
from avdr.profiles import CHECK_ORDER, PROFILE_W3C_BASIC_V1  # noqa: E402
from avdr.provenance import (  # noqa: E402
    build_provenance,
    config_hash,
    new_experiment_id,
)
from avdr.real_shadow import (  # noqa: E402
    CONNECTION_MODES,
    NEW_CLIENT,
    REUSED_CLIENT,
    RealProviderProbe,
    compare_documents,
)
from avdr.telemetry import TelemetrySink  # noqa: E402

PHASE_QUALIFICATION = "real-did-qualification"
PHASE_WARMUP = "warmup"

# [DESIGN CHOICE] Engineering warm-up, to let the process establish
# DNS/TLS/connection state and absorb the first-trial launch-skew outlier seen
# in the previous local milestone. Warm-up trials are stored with
# phase="warmup" and excluded from every qualification summary by this rule,
# which was fixed before any outcome was inspected.
#
# Originally set to 2. Reduced to 1 after discovering that the public endpoint
# enforces 10 requests per 1800 s: the reduction is forced by an external
# request budget, NOT tuned against results.
WARMUP_TRIALS = 1

# [DESIGN CHOICE] Trials per connection mode. Tiny on purpose.
TRIALS_PER_MODE = 2

# [DESIGN CHOICE] Seconds between trials. No rate limit is published for the
# DIF public instances, so pacing is deliberately conservative.
DEFAULT_PACING_S = 3.0

LAUNCH_ORDER_SEED = 20260907


class RequestBudget:
    """Hard cap on requests sent to each external provider.

    The public endpoint discloses a limit of 10 requests per 1800 s. The run
    refuses to exceed it rather than discovering the limit by hitting it.
    """

    def __init__(self, inventory) -> None:
        self.limits: dict[str, int] = {}
        self.spent: dict[str, int] = {}
        for provider in inventory.available_providers():
            if provider.is_external:
                self.limits[provider.id] = provider.rate_limit_requests or 10
                self.spent[provider.id] = 0

    def check(self, providers, label: str) -> None:
        for provider in providers:
            if provider.id not in self.limits:
                continue
            if self.spent[provider.id] + 1 > self.limits[provider.id]:
                raise SystemExit(
                    f"request budget exhausted for {provider.id} "
                    f"({self.spent[provider.id]}/{self.limits[provider.id]}) "
                    f"before {label}; aborting rather than exceeding the "
                    f"provider's documented limit"
                )

    def charge(self, providers) -> None:
        for provider in providers:
            if provider.id in self.spent:
                self.spent[provider.id] += 1

    def report(self) -> str:
        return ", ".join(
            f"{pid}={self.spent[pid]}/{self.limits[pid]}" for pid in sorted(self.limits)
        )


class Gates:
    def __init__(self) -> None:
        self.results: dict[str, str] = {}
        self.notes: dict[str, str] = {}

    def record(self, gate: str, status: str, note: str = "") -> None:
        # Never upgrade a gate that already failed.
        order = {"PASS": 2, "PARTIAL": 1, "FAIL": 0}
        current = self.results.get(gate)
        if current is None or order[status] < order[current]:
            self.results[gate] = status
        if note:
            self.notes[gate] = note
        print(f"  [{status}] {gate}: {note}")

    def all_passed(self) -> bool:
        return bool(self.results) and all(v == "PASS" for v in self.results.values())


def dependency_lock_hash() -> tuple[str | None, str | None]:
    try:
        payload = {
            name: (REPO_ROOT / name).read_text(encoding="utf-8")
            for name in ("requirements.txt", "requirements-dev.txt")
        }
    except OSError as exc:
        return None, f"could not read requirements files: {exc}"
    return config_hash(payload), None


async def run_trial(client, probe, fixture, providers, provenance, mode, index):
    return await probe.run_trial(
        client=client,
        did=fixture.did,
        fixture_id=fixture.fixture_id,
        did_method=fixture.did_method,
        providers=providers,
        provenance=provenance,
        connection_mode=mode,
        launch_order_seed=LAUNCH_ORDER_SEED,
        trial_index=index,
    )


def persist(sink, trial):
    for observation in trial.observations:
        sink.record_real_observation(observation)
        sink.record_raw_response(
            experiment_id=observation.experiment_id,
            trial_id=observation.trial_id,
            provider_id=observation.provider_id,
            raw_response_hash=observation.raw_response_hash,
            body=trial.raw_bodies.get(observation.provider_id),
        )
    sink.record_real_trial(trial.record)


async def execute(args, inventory, fixtures, sink, provenance_for, budget):
    """Run warm-up then qualification trials. Returns (trials, warmups)."""
    probe = RealProviderProbe(timeout_ms=args.timeout_ms)
    trials, warmups = [], []
    rate_limited = []

    key_fixture = fixtures.get("key-ed25519-1")
    key_providers = inventory.for_method("key")

    async def paced(label):
        print(f"    ... pacing {args.pacing}s before {label}")
        await asyncio.sleep(args.pacing)

    # ---------------- warm-up (excluded from all summaries) ----------------
    print(f"\n== warm-up ({WARMUP_TRIALS} trials, phase={PHASE_WARMUP}) ==")
    warm_provenance = provenance_for(PHASE_WARMUP, "warmup")
    async with httpx.AsyncClient() as client:
        for index in range(WARMUP_TRIALS):
            budget.check(key_providers, "warm-up trial")
            budget.charge(key_providers)
            trial = await run_trial(
                client, probe, key_fixture, key_providers, warm_provenance,
                REUSED_CLIENT, index,
            )
            persist(sink, trial)
            warmups.append(trial)
            print(
                f"  warmup {index}: complete={trial.complete} "
                f"skew={trial.record.launch_skew_ms} ms "
                f"order={trial.record.launch_order}"
            )
            rate_limited += trial.record.rate_limited_providers
            await paced("next warm-up trial")

    if rate_limited:
        raise SystemExit(f"rate limited during warm-up by {rate_limited}; aborting")

    # ---------------- Case A/B/D: did:key, both connection modes ----------
    print("\n== did:key across providers, both connection modes ==")
    provenance = provenance_for(PHASE_QUALIFICATION, "real-key")

    for index in range(TRIALS_PER_MODE):
        budget.check(key_providers, "new-client trial")
        budget.charge(key_providers)
        # new-client: a fresh client (and pool) per trial.
        async with httpx.AsyncClient() as client:
            trial = await run_trial(
                client, probe, key_fixture, key_providers, provenance,
                NEW_CLIENT, index,
            )
        persist(sink, trial)
        trials.append(trial)
        print(
            f"  {NEW_CLIENT} #{index}: complete={trial.complete} "
            f"skew={trial.record.launch_skew_ms} ms order={trial.record.launch_order}"
        )
        rate_limited += trial.record.rate_limited_providers
        await paced("next trial")

    async with httpx.AsyncClient() as client:
        for index in range(TRIALS_PER_MODE):
            budget.check(key_providers, "reused-client trial")
            budget.charge(key_providers)
            trial = await run_trial(
                client, probe, key_fixture, key_providers, provenance,
                REUSED_CLIENT, index,
            )
            persist(sink, trial)
            trials.append(trial)
            print(
                f"  {REUSED_CLIENT} #{index}: complete={trial.complete} "
                f"skew={trial.record.launch_skew_ms} ms "
                f"order={trial.record.launch_order}"
            )
            rate_limited += trial.record.rate_limited_providers
            await paced("next trial")

    # ---------------- method ladder: did:web, did:ethr --------------------
    print("\n== method ladder (did:web, did:ethr): one trial each ==")
    async with httpx.AsyncClient() as client:
        for fixture_id in ("web-danubetech", "ethr-default-doc"):
            fixture = fixtures.get(fixture_id)
            providers = inventory.for_method(fixture.did_method)
            if not providers:
                print(f"  {fixture_id}: no qualified provider, skipped")
                continue
            budget.check(providers, f"{fixture_id} trial")
            budget.charge(providers)
            trial = await run_trial(
                client, probe, fixture,
                providers,
                provenance_for(PHASE_QUALIFICATION, f"real-{fixture.did_method}"),
                REUSED_CLIENT, 0,
            )
            persist(sink, trial)
            trials.append(trial)
            accepted = [o.provider_id for o in trial.observations if o.accepted]
            print(f"  {fixture_id}: accepted by {accepted}")
            rate_limited += trial.record.rate_limited_providers
            await paced("next trial")

        # ---------------- Case C: error normalization --------------------
        print("\n== controlled unresolvable DIDs (real-provider error paths) ==")
        for fixture in fixtures.unresolvable():
            providers = inventory.for_method(fixture.did_method) or inventory.for_method("key")
            # An unsupported method has no qualified provider by definition;
            # use the full-resolution-result providers to observe the error.
            providers = [p for p in providers if p.full_resolution_result]
            budget.check(providers, f"{fixture.fixture_id} trial")
            budget.charge(providers)
            trial = await run_trial(
                client, probe, fixture, providers,
                provenance_for(PHASE_QUALIFICATION, f"real-error-{fixture.fixture_id}"),
                REUSED_CLIENT, 0,
            )
            persist(sink, trial)
            trials.append(trial)
            families = {o.provider_id: o.resolution_error_family for o in trial.observations}
            print(f"  {fixture.fixture_id}: error families {families}")
            rate_limited += trial.record.rate_limited_providers
            await paced("next trial")

    if rate_limited:
        print(f"\n  WARNING: rate limited by {sorted(set(rate_limited))}")
    return trials, warmups, sorted(set(rate_limited))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pacing", type=float, default=DEFAULT_PACING_S)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--telemetry-dir", default=str(REPO_ROOT / "telemetry" / "real")
    )
    parser.add_argument(
        "--out", default=str(REPO_ROOT / "artifacts" / "real_did_qualification.json")
    )
    parser.add_argument("--skip-regression", action="store_true")
    args = parser.parse_args()

    gates = Gates()
    inventory = load_provider_inventory()
    fixtures = load_fixture_manifest()
    sink = TelemetrySink(args.telemetry_dir)
    experiment_id = new_experiment_id("realqual")

    lock_hash, lock_reason = dependency_lock_hash()
    inventory_hash = inventory.inventory_hash()
    manifest_hash = fixtures.manifest_hash()

    def provenance_for(phase: str, scenario_id: str):
        provenance = build_provenance(
            experiment_id=experiment_id,
            scenario_id=scenario_id,
            phase=phase,
            repo_root=REPO_ROOT,
            router_config_payload=inventory.payload(),
            injection_config_payload={"note": "no fault injection in real-provider qualification"},
            seed=LAUNCH_ORDER_SEED,
        )
        provenance.provider_inventory_hash = inventory_hash
        provenance.fixture_manifest_hash = manifest_hash
        provenance.acceptance_profile = PROFILE_W3C_BASIC_V1
        provenance.dependency_lock_hash = lock_hash
        if lock_hash is None and lock_reason:
            provenance.unresolved["dependency_lock_hash"] = lock_reason
        return provenance

    print(f"experiment_id      = {experiment_id}")
    print(f"inventory_version  = {inventory.inventory_version} ({inventory_hash[:20]}...)")
    print(f"fixture_manifest   = {fixtures.manifest_version} ({manifest_hash[:20]}...)")
    print(f"acceptance_profile = {PROFILE_W3C_BASIC_V1}")
    print(f"pacing             = {args.pacing}s  warmup = {WARMUP_TRIALS} trials")
    available = inventory.available_providers()
    print(f"providers available: {[p.id for p in available]}")
    for provider in inventory.providers:
        if not provider.available:
            print(f"  excluded {provider.id}: {provider.unavailable_reason.strip()[:110]}")

    # ---------------- existing measurement regression ----------------
    print("\n== existing measurement qualification regression ==")
    if args.skip_regression:
        gates.record("PASS_EXISTING_MEASUREMENT_REGRESSION", "FAIL", "skipped by flag")
    else:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "measurement_qualification.py")],
            capture_output=True, text=True,
        )
        gates.record(
            "PASS_EXISTING_MEASUREMENT_REGRESSION",
            "PASS" if result.returncode == 0 else "FAIL",
            f"measurement_qualification.py exit={result.returncode}",
        )

    budget = RequestBudget(inventory)
    print(f"external request budget: {budget.report()}")
    trials, warmups, rate_limited = asyncio.run(
        execute(args, inventory, fixtures, sink, provenance_for, budget)
    )
    print(f"\nexternal requests spent: {budget.report()}")

    # ================= gates =================
    print("\n== gates ==")

    qualification_trials = [t for t in trials if t.record.phase == PHASE_QUALIFICATION]
    all_obs = [o for t in qualification_trials for o in t.observations]
    accepted_obs = [o for o in all_obs if o.accepted]

    # --- adapters ---
    adapters_used = {
        inventory.get(o.provider_id).adapter for o in all_obs
    }
    driver_obs = [
        o for o in accepted_obs
        if inventory.get(o.provider_id).adapter == "did-document-only-v1"
    ]
    full_obs = [
        o for o in accepted_obs
        if inventory.get(o.provider_id).adapter == "universal-resolver-v1"
    ]
    metadata_absent_ok = all(o.resolution_metadata is None for o in driver_obs)
    gates.record(
        "PASS_REAL_RESOLVER_ADAPTER",
        "PASS" if len(adapters_used) >= 2 and full_obs and driver_obs and metadata_absent_ok else "PARTIAL",
        f"{len(adapters_used)} adapters exercised {sorted(adapters_used)}; "
        f"full-result accepted={len(full_obs)}, document-only accepted={len(driver_obs)}; "
        f"absent metadata recorded as null (not invented)={metadata_absent_ok}",
    )

    # --- real resolution E2E + Case A (>=2 providers, same method) ---
    key_obs = [o for o in accepted_obs if o.did_method == "key"]
    key_providers_ok = {o.provider_id for o in key_obs}
    key_implementations = {
        inventory.get(pid).implementation_id for pid in key_providers_ok
    }
    methods_resolved = {o.did_method for o in accepted_obs}
    gates.record(
        "PASS_REAL_DID_RESOLUTION_E2E",
        "PASS" if len(methods_resolved) >= 1 and accepted_obs else "FAIL",
        f"real methods resolved and structurally accepted: {sorted(methods_resolved)}; "
        f"{len(accepted_obs)} accepted observations",
    )

    case_a_status = "PASS" if len(key_providers_ok) >= 2 else "FAIL"
    gates.record(
        "PASS_MULTI_PROVIDER_SAME_DID_SHADOW",
        case_a_status,
        f"did:key accepted by {len(key_providers_ok)} providers "
        f"{sorted(key_providers_ok)} spanning {len(key_implementations)} "
        f"implementation(s) {sorted(i for i in key_implementations if i)}",
    )

    # --- Case B: same DID, same trial, multi-provider, complete ---
    same_trial = [
        t for t in qualification_trials
        if t.record.fixture_id == "key-ed25519-1" and t.complete
        and t.record.actual_observations >= 2
    ]
    single_did_per_trial = all(
        len({o.requested_did for o in t.observations}) == 1 for t in same_trial
    )
    gates.record(
        "PASS_MULTI_PROVIDER_SAME_DID_SHADOW",
        "PASS" if same_trial and single_did_per_trial else "FAIL",
        f"{len(same_trial)} complete same-DID multi-provider trials; "
        f"one DID per trial={single_did_per_trial}",
    )

    # --- acceptance profile ---
    checks_complete = all(
        set(o.acceptance_checks) == set(CHECK_ORDER) for o in all_obs
    )
    derivable = all(
        o.accepted == all(v is not False for v in o.acceptance_checks.values())
        for o in all_obs
    )
    no_crypto_wording = all(
        "cryptographically verified" not in (o.acceptance_reason or "")
        for o in all_obs
    )
    gates.record(
        "PASS_W3C_BASIC_ACCEPTANCE",
        "PASS" if checks_complete and derivable and no_crypto_wording else "FAIL",
        f"all {len(CHECK_ORDER)} checks recorded per observation={checks_complete}; "
        f"accepted derivable from checks={derivable}; no crypto-verification "
        f"wording={no_crypto_wording}",
    )

    # --- error normalization ---
    error_trials = [
        t for t in qualification_trials if t.record.scenario_id.startswith("real-error-")
    ]
    error_obs = [o for t in error_trials for o in t.observations]
    expected_families = {
        f.fixture_id: f.expected_error_family for f in fixtures.unresolvable()
    }
    matched = [
        o for o in error_obs
        if o.resolution_error_family == expected_families.get(o.fixture_id)
    ]
    none_accepted = all(not o.accepted for o in error_obs)
    gates.record(
        "PASS_REAL_ERROR_NORMALIZATION",
        "PASS" if error_obs and none_accepted and len(matched) == len(error_obs) else "PARTIAL",
        f"{len(error_obs)} error observations, {len(matched)} matched the expected "
        f"family, none accepted={none_accepted}",
    )

    # --- connection modes ---
    modes_seen = {t.record.connection_mode for t in qualification_trials}
    modes_tagged = all(
        o.connection_mode in CONNECTION_MODES and o.client_reuse_policy
        for o in all_obs
    )
    gates.record(
        "PASS_CONNECTION_MODE_PROVENANCE",
        "PASS" if modes_seen == {NEW_CLIENT, REUSED_CLIENT} and modes_tagged else "FAIL",
        f"modes executed={sorted(modes_seen)}; every observation tagged with mode "
        f"and reuse policy={modes_tagged}",
    )

    # --- provider provenance ---
    required = (
        "provider_inventory_hash", "fixture_manifest_hash", "acceptance_profile",
        "git_commit", "config_hash", "provider_id", "resolver_endpoint_id",
        "fixture_id", "launch_order_seed", "raw_response_hash",
    )
    def stamped(observation) -> bool:
        data = observation.model_dump()
        # raw_response_hash is legitimately null when transport failed.
        return all(
            data.get(name) is not None
            for name in required
            if not (name == "raw_response_hash" and not observation.http_status)
        )
    fully_stamped = [o for o in all_obs if stamped(o)]
    gates.record(
        "PASS_REAL_PROVIDER_PROVENANCE",
        "PASS" if all_obs and len(fully_stamped) == len(all_obs) else "FAIL",
        f"{len(fully_stamped)}/{len(all_obs)} observations carry full provider "
        f"provenance; dependency_lock_hash="
        f"{'present' if lock_hash else 'null (' + str(lock_reason) + ')'}",
    )

    # ================= evidence =================
    print("\n== gate summary ==")
    for gate in sorted(gates.results):
        print(f"  {gate} = {gates.results[gate]}")

    comparisons = []
    for trial in same_trial:
        comparison = compare_documents(trial.observations)
        comparison["trial_id"] = trial.record.trial_id
        comparison["connection_mode"] = trial.record.connection_mode
        comparisons.append(comparison)

    difference_flags = {c["flag"] for c in comparisons if c["flag"]}
    print("\n== provider result comparison (same DID, same trial) ==")
    for comparison in comparisons[:2]:
        print(f"  trial {comparison['trial_id'][:8]} mode={comparison['connection_mode']}")
        print(f"    exact_subject_match={comparison['exact_subject_match']}")
        print(f"    distinct_document_hashes={comparison['distinct_document_hashes']}")
        print(f"    flag={comparison['flag']}")
    if difference_flags:
        print(
            "  NOTE: differing documents are recorded as a representational\n"
            "  difference only. No provider is labelled stale, invalid or\n"
            "  incorrect: that would require method-specific ground truth."
        )

    skews = [
        t.record.launch_skew_ms for t in qualification_trials
        if t.record.launch_skew_ms is not None
    ]
    warm_skews = [
        t.record.launch_skew_ms for t in warmups if t.record.launch_skew_ms is not None
    ]
    print("\n== launch skew [MEASURED-QUALIFICATION] ==")
    if skews:
        print(f"  qualification trials: {min(skews):.3f} - {max(skews):.3f} ms")
    if warm_skews:
        print(f"  warm-up trials (excluded): {min(warm_skews):.3f} - {max(warm_skews):.3f} ms")
    print(
        "  Launch skew is NOT negligible relative to inter-provider latency\n"
        "  differences. No provider ranking may be derived from this run."
    )

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "experiment_id": experiment_id,
                "phase": PHASE_QUALIFICATION,
                "label": "REAL DID COMPATIBILITY QUALIFICATION",
                "disclaimer": (
                    "Functional/measurement qualification only. NO performance "
                    "claim, NO provider ranking, NO heterogeneity claim and NO "
                    "justification for adaptive routing may be derived from "
                    "this run. Latency values are confounded by launch skew, "
                    "geography, CDN fronting and one provider running on the "
                    "measurement host itself."
                ),
                "provider_inventory_version": inventory.inventory_version,
                "provider_inventory_hash": inventory_hash,
                "fixture_manifest_version": fixtures.manifest_version,
                "fixture_manifest_hash": manifest_hash,
                "acceptance_profile": PROFILE_W3C_BASIC_V1,
                "dependency_lock_hash": lock_hash,
                "warmup_trials_excluded": len(warmups),
                "launch_order_seed": LAUNCH_ORDER_SEED,
                "pacing_seconds": args.pacing,
                "rate_limited_providers": rate_limited,
                "gates": gates.results,
                "notes": gates.notes,
                "document_comparisons": comparisons,
                "launch_skew_ms": {
                    "qualification_min": min(skews) if skews else None,
                    "qualification_max": max(skews) if skews else None,
                    "warmup_min": min(warm_skews) if warm_skews else None,
                    "warmup_max": max(warm_skews) if warm_skews else None,
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
