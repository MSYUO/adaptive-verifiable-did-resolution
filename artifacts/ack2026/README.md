# AVDR — ACK 2026 evidence freeze

Reproducible evidence package for an ACK 2026 paper on **adaptive resolver
fan-out for W3C DID Resolution-oriented routing**, evaluated in a **controlled
local DID resolver environment**.

This directory freezes evidence, tables, figures and provenance. It is not the
manuscript.

---

## What this artifact supports

- A **V3 discovery** stage that developed and froze a routing policy, and
- an **independent V4 frozen confirmatory replication** on new data, showing
  that in this controlled environment adaptive fan-out **improved the
  cost-success frontier** against a fixed two-resolver comparator that was
  itself frozen before the confirmatory run.

## What this artifact does NOT support

No claim about real public DID resolver performance, production SLOs,
independent real providers, W3C certification, Byzantine fault tolerance, or
any 3f+1 rule — and **no claim that machine learning beat heuristics**.
Read [`protocol/claim_boundaries.md`](protocol/claim_boundaries.md) before
citing anything here.

## V3 versus V4 — do not pool them

| Stage | Role | Data |
|---|---|---|
| **V3** | **Discovery**: generator design, estimator selection, policy and comparator freeze | own train / validation / holdout episodes |
| **V4** | **Confirmatory**: nothing selected, tuned or retrained; every component verified against the V3 freeze before generation | 120 new episodes, seeds disjoint from V1/V2/V3 |

V3 is **not** confirmatory evidence, and the two stages are **never pooled**
into one larger sample. V4 is the result a paper should lead with.

## Headline result (V4, recomputed from raw records)

| | success (all requests) | calls / request |
|---|---|---|
| adaptive best-effort (frozen policy) | **0.867045** | **1.9053** |
| BEST_FIXED_K2 `{local-b, local-c}` (frozen comparator) | 0.834470 | 2.0000 |

Δ success **+0.032576**, Δ calls **−0.094697**, on **2 640 paired trials**
across **120 episodes**. Adaptive cost is fully loaded: execution + cold-start
warmup + scheduled exploration + best-effort fallback.

Episode-clustered paired bootstrap (10 000 resamples, seed 20260909):
Δ success 95% CI **[+0.012121, +0.054924]**, Δ calls 95% CI
**[−0.172348, −0.015530]**.

The CI lower bound for Δ success sits **below** the 0.02 materiality reference
carried from V3, so the improvement is reliably positive but is **not**
established as materially large. State that limit when citing the number.

## Estimator result — the honest version

The frozen estimator is **`b1-rolling-empirical`**, a rolling empirical
success-rate estimator with hierarchical backoff, **uncalibrated**. It is not
a machine-learning model.

At every development stage the simple rolling/EWMA estimators beat logistic
regression and histogram gradient boosting on validation probability quality
(see [`tables/table_estimator_results.csv`](tables/table_estimator_results.csv)).
The contribution is the adaptive fan-out mechanism; the permissible secondary
observation is that *more complex learned estimators were not required in the
controlled setting*.

---

## How to recompute everything

Numbers flow one way, so no value is ever hand-typed twice:

```
raw V4 per-trial evidence
        │
        ▼
analysis/recompute_ack_results.py   ──►  data/*.csv, tables/*.csv,
                                         figures/source/*.csv,
                                         analysis/bootstrap_summary.json,
                                         analysis/validation_report.json
        │
        ▼
analysis/make_figures.py            ──►  figures/*.pdf
        │
        ▼
analysis/build_package_metadata.py  ──►  protocol/*.json, provenance/*.json,
                                         MANIFEST.json, SHA256SUMS
```

```bash
python artifacts/ack2026/analysis/recompute_ack_results.py
python artifacts/ack2026/analysis/make_figures.py
python artifacts/ack2026/analysis/build_package_metadata.py
sha256sum -c artifacts/ack2026/SHA256SUMS      # from inside artifacts/ack2026
```

`recompute_ack_results.py` re-derives every published quantity from the raw V4
records **and cross-checks it against the aggregates the V4 run itself wrote**.
All 11 cross-checks agree; the outcome is recorded in
`analysis/validation_report.json`. If they ever disagree, the recomputed value
is what the package publishes and the disagreement is recorded.

The figures read only `figures/source/*.csv` — no numeric constant is embedded
in the plotting code.

## Which commit produced V4

- **V4 execution commit:** `8b2aa822167ffab68959161f222fb40b6544f592`, run with
  a clean working tree (`git_dirty: false`).
- **V3 policy/estimator freeze:** `f7737bb20bb9aed6f6204296881f51eb0011c4a9`.
- Full binding: [`provenance/source_commits.json`](provenance/source_commits.json).

The raw V3/V4 run reports live under `artifacts/learning_v3/` and
`artifacts/learning_v4/`, which are **gitignored experiment output**. They are
bound to this package by SHA-256 in
[`provenance/frozen_hashes.json`](provenance/frozen_hashes.json), and all
evidence needed for the published numbers is copied here as CSV, so this
directory is self-contained.

## Controlled-environment limitation

Three local mock resolvers on one shared Docker host, synthetic control
documents, and injected dynamic conditions (delays, errors, structurally
unacceptable responses, correlated degradation). The resolvers are **not**
independent providers. Hidden generator states are audit-only and never
estimator inputs. Nothing here measures real DID infrastructure.

## Known errata

See [`provenance/known_errata.md`](provenance/known_errata.md). In short: the
frozen estimator metadata records the V1 feature-schema hash rather than the V2
one. It is a labelling issue only — the frozen estimator reads outcome history
and never the feature vector, so no V4 result is affected. The historical
artifact was deliberately **not** rewritten.

---

## Directory contents

```
README.md                     this file
MANIFEST.json                 machine-readable binding of the whole package
SHA256SUMS                    checksums for every file here (excludes itself)

protocol/
  v3_discovery_protocol.json  V3 stage parameters, seeds, freeze decisions
  v4_confirmatory_protocol.json  V4 frozen inputs, pre-declared design choices
  claim_boundaries.md         what may and may not be claimed

data/
  v3_summary.csv              V3 discovery headline (kept separate, not pooled)
  v4_trials.csv               all 2 640 V4 logical trials, one row each
  v4_episode_metrics.csv      per-episode aggregates (bootstrap clusters)
  v4_paired_outcomes.csv      the 2x2 paired table
  estimator_summary.csv       frozen estimator identity and selection outcome

tables/
  table_primary_results.csv   Table 1 — policies, success, calls/request
  table_paired_outcomes.csv   Table 2 — paired cells, deltas, bootstrap CIs
  table_static_baselines.csv  all seven static subsets
  table_estimator_results.csv Table 3 — estimator comparison (secondary)

figures/
  fig1_architecture.pdf       estimator / optimizer / executor pipeline
  fig2_success_vs_calls.pdf   success vs cost, adaptive and comparator marked
  fig3_selected_k_by_state.pdf  selected fan-out by controlled injection state
  source/                     the CSVs those figures are drawn from

analysis/
  recompute_ack_results.py    single source of truth for every number
  make_figures.py             figure rendering from source CSVs
  build_package_metadata.py   protocol/provenance/manifest/checksum generation
  bootstrap_summary.json      episode-clustered bootstrap output
  validation_report.json      reconciliation audit and cross-check results

provenance/
  source_commits.json         repo, branch, HEAD, per-stage run commits
  frozen_hashes.json          artifact, policy, generator and raw-data hashes
  dependency_hash.txt         pinned dependency set hash
  experiment_identity.json    environment description and non-claims
  known_errata.md             documented errata
```
