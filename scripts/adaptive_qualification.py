"""Controlled qualification of the adaptive minimum-set decision engine.

Uses ONLY local deterministic providers (docker-compose mock resolvers). Zero
public-network calls, zero third-party request budget consumed.

Every q_hat value here is [CONTROLLED TEST INPUT] chosen so the optimizer's
correct answer is known in advance. K1/K2/K3/KU are optimizer fixtures. They
are NOT findings about DID infrastructure, and nothing in this run is evidence
that the estimates themselves are correct -- only that, given estimates, the
decision layer selects and executes correctly.

Clean-tree rule: this qualification must run from a committed, clean working
tree. A dirty tree fails PASS_CLEAN_TREE_QUALIFICATION.

Usage:
    docker compose up -d
    python scripts/adaptive_qualification.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.adaptive.estimator import ControlledTableEstimator  # noqa: E402
from avdr.adaptive.optimizer import (  # noqa: E402
    OPTIMIZER_VERSION,
    SELECTED,
    SLO_ESTIMATE_UNSATISFIABLE,
    TIE_BREAK_RULE,
)
from avdr.inventory import load_provider_inventory  # noqa: E402
from avdr.probe import CANCELED_AFTER_DISPATCH  # noqa: E402
from avdr.provenance import resolve_git_commit, resolve_git_dirty  # noqa: E402
from avdr.real_router.app import create_app  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

DID = "did:example:adaptive-qualification"
ADMIN = {
    "local-a": "http://127.0.0.1:8001",
    "local-b": "http://127.0.0.1:8002",
    "local-c": "http://127.0.0.1:8003",
}
HEALTHY = {
    "artificial_delay_ms": 0, "force_error": False, "force_error_status": 503,
    "force_invalid": False, "force_timeout": False, "timeout_sleep_ms": 30000,
    "deterministic_failure_every_n": 0,
}
SLOW_MS = 400  # [CONTROLLED INJECTION]

# [CONTROLLED TEST INPUT] -- not measurements, not predictions.
Q_TABLE = {
    ("local-a",): 0.99,
    ("local-b",): 0.96,
    ("local-c",): 0.90,
    ("local-a", "local-b"): 0.995,
    ("local-a", "local-c"): 0.993,
    ("local-b", "local-c"): 0.991,
    ("local-a", "local-b", "local-c"): 0.9995,
}


class Gates:
    def __init__(self) -> None:
        self.results: dict[str, bool] = {}
        self.notes: dict[str, str] = {}

    def record(self, gate: str, passed: bool, note: str = "") -> None:
        self.results[gate] = self.results.get(gate, True) and passed
        if note:
            self.notes[gate] = note
        print(f"  [{'PASS' if passed else 'FAIL'}] {gate}: {note}")

    def all_passed(self) -> bool:
        return bool(self.results) and all(self.results.values())


def apply(client, provider_id, **overrides):
    behavior = dict(HEALTHY)
    behavior.update(overrides)
    client.post(f"{ADMIN[provider_id]}/admin/behavior", json=behavior, timeout=5).raise_for_status()


def reset_all(client):
    for url in ADMIN.values():
        client.post(f"{url}/admin/reset", timeout=5).raise_for_status()


async def call(app, target=None, policy="adaptive-min-set"):
    payload = {"did": DID, "policy": policy}
    if target is not None:
        payload["target_slo_probability"] = target
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            r = await c.post("/resolve", json=payload)
    return r.status_code, r.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telemetry-dir", default=str(REPO_ROOT / "telemetry" / "adaptive"))
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "adaptive_qualification.json"))
    parser.add_argument("--skip-regression", action="store_true")
    args = parser.parse_args()

    gates = Gates()
    inventory = load_provider_inventory(REPO_ROOT / "config" / "providers.local.yaml")
    sink = TelemetrySink(args.telemetry_dir)
    estimator = ControlledTableEstimator(Q_TABLE, label="adaptive-qualification")
    app = create_app(
        inventory=inventory, sink=sink, timeout_ms=5000,
        launch_order_seed=None, estimator=estimator, default_target_slo=0.99,
    )

    commit, commit_reason = resolve_git_commit(REPO_ROOT)
    dirty, dirty_reason = resolve_git_dirty(REPO_ROOT)

    print("CONTROLLED ADAPTIVE QUALIFICATION")
    print("  All q_hat values are CONTROLLED TEST INPUT. This run qualifies the")
    print("  DECISION LAYER only; it is not evidence that the estimates are correct.")
    print(f"  git_commit = {commit}   git_dirty = {dirty}")
    print(f"  estimator  = {estimator.estimator_id}/{estimator.estimator_version}")
    print(f"  optimizer  = {OPTIMIZER_VERSION}")
    print(f"  tie-break  = {TIE_BREAK_RULE}\n")

    results = {}
    with httpx.Client() as admin:
        for url in ADMIN.values():
            admin.get(f"{url}/health", timeout=10).raise_for_status()
        reset_all(admin)

        # ---------------- K1 / K2 / K3 ----------------
        for name, target, expected in (
            ("K1", 0.95, ["local-a"]),
            ("K2", 0.992, ["local-a", "local-b"]),
            ("K3", 0.999, ["local-a", "local-b", "local-c"]),
        ):
            reset_all(admin)
            status, body = asyncio.run(call(app, target))
            plan = body.get("adaptive_plan", {})
            results[name] = body
            ok = (
                status == 200
                and plan.get("selected_subset") == expected
                and plan.get("selected_subset_size") == len(expected)
                and body.get("attempted_providers") == expected
                and plan.get("evaluated_subset_count") == 7
            )
            gates.record(
                f"PASS_{name}_SELECTION", ok,
                f"target={target} -> k*={plan.get('selected_subset_size')} "
                f"{plan.get('selected_subset')} q_hat={plan.get('estimated_subset_success')} "
                f"attempted={body.get('attempted_providers')}",
            )
            gates.record(
                "PASS_EXACT_MINIMUM_SET_OPTIMIZER",
                plan.get("evaluated_subset_count") == 7 and plan.get("optimizer_version") == OPTIMIZER_VERSION,
                f"evaluated {plan.get('evaluated_subset_count')} subsets "
                f"(2^3-1=7), optimizer={plan.get('optimizer_version')}",
            )
            gates.record(
                "PASS_SUBSET_ESTIMATOR_INTERFACE",
                plan.get("estimator_id") == "controlled-table"
                and plan.get("estimator_config_hash", "").startswith("sha256:")
                and plan.get("unestimated_subset_count") == 0,
                f"estimator={plan.get('estimator_id')}/{plan.get('estimator_version')} "
                f"unestimated={plan.get('unestimated_subset_count')}",
            )

        # ---------------- KU ----------------
        reset_all(admin)
        status, body = asyncio.run(call(app, 0.99999))
        results["KU"] = body
        gates.record(
            "PASS_UNSATISFIABLE_TARGET_HANDLING",
            status == 409
            and body.get("error") == SLO_ESTIMATE_UNSATISFIABLE
            and body.get("selected_subset") is None
            and body.get("best_subset") == ["local-a", "local-b", "local-c"]
            and body.get("best_probability") == 0.9995,
            f"HTTP {status} {body.get('error')}; best={body.get('best_subset')} "
            f"@ {body.get('best_probability')} < target {body.get('target_slo_probability')}; "
            f"no silent all-call",
        )

        # ---------------- deterministic tie-break ----------------
        tie_estimator = ControlledTableEstimator(
            {("local-a",): 0.99, ("local-b",): 0.99, ("local-c",): 0.99},
            label="tie",
        )
        tie_app = create_app(
            inventory=inventory, sink=sink, timeout_ms=5000,
            launch_order_seed=None, estimator=tie_estimator, default_target_slo=0.95,
        )
        picks = []
        for _ in range(5):
            reset_all(admin)
            _, tie_body = asyncio.run(call(tie_app, 0.95))
            picks.append(tuple(tie_body["adaptive_plan"]["selected_subset"]))
        results["TIE"] = picks[0]
        gates.record(
            "PASS_DETERMINISTIC_TIE_BREAK",
            len(set(picks)) == 1 and picks[0] == ("local-a",),
            f"5 identical inputs -> {len(set(picks))} distinct selection(s): "
            f"{picks[0]} (lexicographic)",
        )

        # ---------------- first-acceptable under adaptive ----------------
        reset_all(admin)
        apply(admin, "local-a", force_invalid=True)
        apply(admin, "local-b", artificial_delay_ms=SLOW_MS)
        status, body = asyncio.run(call(app, 0.992))
        results["FIRST_ACCEPTABLE"] = body
        trace = sink.get_routing_request(body["request_id"])
        by_provider = {a["provider_id"]: a for a in trace["attempts"]}
        a_att = by_provider.get("local-a", {})
        gates.record(
            "PASS_ADAPTIVE_FIRST_ACCEPTABLE_EXECUTION",
            status == 200
            and body.get("returned_provider") == "local-b"
            and a_att.get("http_status") == 200
            and a_att.get("accepted") is False
            and set(body.get("attempted_providers", [])) <= set(body["adaptive_plan"]["selected_subset"]),
            f"local-a HTTP 200 in {a_att.get('latency_ms')} ms but unacceptable; "
            f"returned {body.get('returned_provider')} "
            f"({by_provider.get('local-b',{}).get('latency_ms')} ms)",
        )

        # ---------------- canceled-dispatched launch timing ----------------
        reset_all(admin)
        apply(admin, "local-b", artificial_delay_ms=SLOW_MS * 3)
        apply(admin, "local-c", artificial_delay_ms=SLOW_MS * 3)
        status, body = asyncio.run(call(app, 0.999))   # k*=3, a wins fast
        results["CANCEL"] = body
        trace = sink.get_routing_request(body["request_id"])
        canceled = [a for a in trace["attempts"] if a["canceled"]]
        dispatched_canceled = [a for a in canceled if a["dispatched"]]
        invariant_ok = bool(dispatched_canceled) and all(
            a["launch_offset_ms"] is not None
            and a["latency_ms"] is None
            and a["cancellation_outcome"] == CANCELED_AFTER_DISPATCH
            for a in dispatched_canceled
        )
        never_dispatched_ok = all(
            a["launch_offset_ms"] is None
            for a in canceled
            if not a["dispatched"]
        )
        gates.record(
            "PASS_DISPATCH_LAUNCH_TELEMETRY_INVARIANT",
            invariant_ok and never_dispatched_ok,
            f"{len(dispatched_canceled)} dispatched+canceled attempts all keep "
            f"launch_offset_ms with null latency_ms; never-dispatched keep null offset",
        )

        reset_all(admin)

    # ---------------- telemetry + provenance reconciliation ----------------
    def read(path):
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    requests = read(sink.routing_requests_path)
    attempts = read(sink.routing_attempts_path)
    adaptive_rows = [r for r in requests if r["routing_policy"] == "adaptive-min-set"]
    by_request = {}
    for a in attempts:
        by_request.setdefault(a["request_id"], []).append(a)

    reconciles = all(
        r["attempt_count"] == len(by_request.get(r["request_id"], []))
        and set(r["attempted_providers"]) <= set(r["selected_subset"] or [])
        for r in adaptive_rows
    )
    gates.record(
        "PASS_ADAPTIVE_TELEMETRY",
        bool(adaptive_rows) and reconciles,
        f"{len(adaptive_rows)} adaptive logical requests; attempt counts reconcile "
        f"and attempted ⊆ selected for all",
    )

    provenance_fields = (
        "git_commit", "provider_inventory_hash", "acceptance_profile",
        "routing_policy", "estimator_id", "estimator_version",
        "estimator_config_hash", "target_slo_probability", "optimizer_version",
        "selection_status", "cost_model_id",
    )
    stamped = [
        r for r in adaptive_rows
        if all(r.get(f) is not None for f in provenance_fields)
    ]
    gates.record(
        "PASS_ADAPTIVE_PROVENANCE",
        len(stamped) == len(adaptive_rows) and bool(adaptive_rows),
        f"{len(stamped)}/{len(adaptive_rows)} adaptive records carry all "
        f"{len(provenance_fields)} provenance fields",
    )

    # ---------------- clean tree ----------------
    gates.record(
        "PASS_CLEAN_TREE_QUALIFICATION",
        dirty is False,
        f"git_dirty={dirty} commit={commit}"
        + (f" ({dirty_reason})" if dirty_reason else ""),
    )

    # ---------------- previous regression ----------------
    if args.skip_regression:
        gates.record("PASS_PREVIOUS_REGRESSION", False, "skipped by flag")
    else:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        gates.record(
            "PASS_PREVIOUS_REGRESSION", proc.returncode == 0,
            f"pytest exit={proc.returncode}: {tail}",
        )

    print("\n== gate summary ==")
    for gate in sorted(gates.results):
        print(f"  {gate} = {'PASS' if gates.results[gate] else 'FAIL'}")

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "label": "CONTROLLED ADAPTIVE QUALIFICATION",
                "disclaimer": (
                    "All q_hat values are CONTROLLED TEST INPUT chosen to give "
                    "the optimizer a known-correct answer. K1/K2/K3/KU are "
                    "optimizer fixtures, NOT findings about DID infrastructure. "
                    "This run qualifies the decision layer only and is not "
                    "evidence that the estimates are correct."
                ),
                "git_commit": commit,
                "git_dirty": dirty,
                "estimator": estimator.describe(),
                "optimizer_version": OPTIMIZER_VERSION,
                "tie_break_rule": TIE_BREAK_RULE,
                "q_table": {",".join(k): v for k, v in Q_TABLE.items()},
                "injected_delay_ms": SLOW_MS,
                "gates": gates.results,
                "notes": gates.notes,
                "scenarios": {
                    k: (v if not isinstance(v, tuple) else list(v))
                    for k, v in results.items()
                },
            },
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nreport written to {output}")
    return 0 if gates.all_passed() else 1


if __name__ == "__main__":
    sys.exit(main())
