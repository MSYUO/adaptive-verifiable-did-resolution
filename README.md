# Adaptive Verifiable DID Resolution

Local engineering prototype of a DID resolution router that returns the first
**acceptable** response rather than merely the first response.

> **Scope so far:** multi-resolver baseline routing + controlled fault
> injection → measurement-apparatus qualification → real DID resolver
> compatibility qualification. There is deliberately no machine learning, no
> adaptive fan-out sizing (`adaptive-k`), no racing *in the routing path*, and
> no blockchain. Those belong to later phases.
>
> Real public DID resolvers **are** contacted, and there is now a user-facing
> routing service over them with three baseline policies. `all-race` does
> contact every candidate at once — it is the maximum-fan-out **control
> baseline**, not the proposed adaptive algorithm.

---

## Scientific disclaimer — read before quoting any number

**Local resolver instances share host infrastructure. Artificial
delay/failure conditions are controlled engineering test conditions and must
not be interpreted as measurements of real DID resolver behaviour.**

Concretely:

- All four services run on **one Docker host** and share CPU, kernel, network
  stack and storage. They are **not** independent DID gateways, and no
  statistical independence may be claimed from this deployment.
- Every delay, error, timeout and invalid document in this repository is a
  **[CONTROLLED INJECTION]** knob that we set. `+200 ms` is an injected test
  value, not a measured DID latency.
- The DID documents served are **synthetic**. Nothing is resolved from a
  ledger, registry or network. Responses carry `"synthetic": true` in their
  metadata.
- This phase answers an engineering question only: *can the router route
  across multiple resolvers under controlled conditions while preserving
  complete telemetry?* It answers none of the project's research questions.

---

## Architecture

```
                              +--> resolver-a  (:8001)
  client --> router (:8000) --+--> resolver-b  (:8002)
                              +--> resolver-c  (:8003)

  router internals:
      routing policy  ->  selects resolver order      (policies.py, no I/O)
      transport       ->  one attempt, instrumented   (transport.py)
      acceptance      ->  structural DID checks       (acceptance.py)
      telemetry       ->  JSONL, two record levels    (telemetry.py)
```

| Path | Role |
| --- | --- |
| `src/avdr/config.py` | Resolver registry loader (YAML + `${VAR:-default}`) |
| `src/avdr/models.py` | Telemetry models; logical request vs resolver attempt |
| `src/avdr/acceptance.py` | Minimum structural DID-document checks |
| `src/avdr/telemetry.py` | Append-only JSONL sink, four separate files |
| `src/avdr/provenance.py` | Canonical hashing, git binding, run provenance |
| `src/avdr/scenarios.py` | Scenario loader + applier, injection hashing |
| `src/avdr/shadow.py` | Shadow characterization harness (measurement only) |
| `src/avdr/analysis.py` | Derived analysis + dataset integrity audit |
| `src/avdr/resolver/settings.py` | Injected-behaviour configuration |
| `src/avdr/resolver/app.py` | Mock resolver service + admin surface |
| `src/avdr/router/policies.py` | Policy abstraction and three baselines |
| `src/avdr/router/transport.py` | One instrumented resolver attempt |
| `src/avdr/router/app.py` | Router service, sequential execution loop |
| `config/resolvers.yaml` | Resolver registry and router settings |
| `config/scenarios.yaml` | Reproducible fault-injection scenario definitions |
| `scripts/e2e_smoke.py` | E2E qualification against docker compose |
| `src/avdr/inventory.py` | Provider inventory + DID fixture manifest loaders |
| `src/avdr/adapters.py` | Real provider adapters -> one normalized model |
| `src/avdr/profiles.py` | Versioned acceptance profiles (`w3c-basic-v1`) |
| `src/avdr/real_shadow.py` | Multi-provider real shadow harness (measurement only) |
| `config/providers.yaml` | Real provider compatibility matrix |
| `config/fixtures.yaml` | Real DID fixtures with documented provenance |
| `scripts/measurement_qualification.py` | Measurement-apparatus qualification |
| `src/avdr/probe.py` | One instrumented provider request (shared call path) |
| `src/avdr/budget.py` | Rolling-window per-provider request budgets |
| `src/avdr/candidates.py` | Capability-aware candidate selection |
| `src/avdr/real_router/policies.py` | Real baseline policies (no I/O) |
| `src/avdr/real_router/executor.py` | Sequential + concurrent execution |
| `src/avdr/real_router/app.py` | **Real routing service API** |
| `config/providers.local.yaml` | Local controlled provider inventory |
| `scripts/real_did_qualification.py` | Real DID resolver compatibility qualification |
| `scripts/real_routing_demo.py` | Controlled local routing demo (no public calls) |
| `src/avdr/adaptive/estimator.py` | Layer A — subset estimates `q_hat(S\|x)` |
| `src/avdr/adaptive/optimizer.py` | Layer B — minimum-set optimizer + cost models |
| `src/avdr/real_router/adaptive_policy.py` | `adaptive-min-set` policy (wiring only) |
| `scripts/real_routing_smoke.py` | Minimal real public smoke (1 request) |
| `scripts/adaptive_qualification.py` | Controlled adaptive decision-layer qualification |
| `src/avdr/learning/environment.py` | Controlled stochastic episode environment |
| `src/avdr/learning/features.py` | Pre-request feature contract |
| `src/avdr/learning/dataset.py` | Trial execution, subset targets, manifests |
| `src/avdr/learning/estimators.py` | Baselines B0/B1/B2 + learned M1/M2 + freeze |
| `src/avdr/learning/metrics.py` | Brier / log loss / calibration / decision metrics |
| `scripts/estimator_pipeline.py` | Generate, train, validate, select, freeze |
| `scripts/prospective_holdout.py` | Final prospective holdout (run once) |

Policy logic is kept free of HTTP concerns, and the router holds no
scenario/fault knowledge: injected behaviour belongs to the resolvers.

---

## How to run

### Docker compose (primary path)

```bash
docker compose up --build          # starts router + resolver-a/b/c
docker compose ps                  # all four should report healthy
docker compose down                # stop
```

Published ports: router `8000`, resolvers `8001` / `8002` / `8003`.
Telemetry is written to `./telemetry` via a bind mount.

### Host-local (no Docker)

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# start each resolver on its own port, then the router
RESOLVER_ID=resolver-a RESOLVER_PORT=8001 python -m uvicorn avdr.resolver.app:app --port 8001
python -m uvicorn avdr.router.app:app --port 8000
```

`PYTHONPATH` must include `src/`.

---

## Routing policies

Select per request with `?policy=`, or set the default in `config/resolvers.yaml`.

| Policy | Behaviour | Failover |
| --- | --- | --- |
| `single-static` | Always the configured target (`resolver-a`) | No — one attempt |
| `round-robin` | Rotates a, b, c, a, ... per **logical request** | No — one attempt |
| `sequential-failover` | Configured order until one is accepted | Yes — up to 3 attempts |

`round-robin` and `single-static` deliberately do **not** fail over: they are
control baselines, and a baseline that silently repairs itself is not a
baseline. `sequential-failover` is strictly sequential — attempt *i+1* is
issued only after attempt *i* terminates. No requests are raced.

The round-robin counter is process-local, so the router runs with a **single
worker**; multiple workers would break sequence determinism.

Failover is triggered by: timeout, connection failure, HTTP error, and
structurally unacceptable document.

### Acceptance rules (`structural-v1`)

Structural only — **no cryptographic verification, no signature checking, no
freshness/version checks, no cross-resolver agreement.**

1. Response body is a JSON object
2. Body contains a `didDocument` object
3. `didDocument['@context']` includes `https://www.w3.org/ns/did/v1`
4. `didDocument.id` is a non-empty string
5. `didDocument.id` equals the requested DID
6. `didDocument.verificationMethod` is a non-empty list

A response is `accepted` only if the HTTP call succeeded **and** the document
passes all six.

**What this does not yet establish.** Acceptance makes "responded" and
"acceptable" distinguishable *per attempt*, but the routing policies cannot
compare `argmin latency` against `argmin latency among accepted`: they stop at
the first acceptable response and therefore never observe the resolvers they
did not attempt. A sequential-failover trace showing resolver-a rejected and
resolver-b accepted supports only the statement *"the first attempted resolver
returned an unacceptable response and the next one returned an acceptable
response"* — it is **not** evidence that the fastest response differed from the
fastest acceptable response. Answering that requires observing every candidate
resolver for the same request, which is what the shadow harness
(`src/avdr/shadow.py`) exists to do.

---

## Resolver configuration

Registry lives in `config/resolvers.yaml`; the router never hard-codes
resolver identities or URLs.

```yaml
resolvers:
  - id: resolver-a
    url: "${RESOLVER_A_URL:-http://127.0.0.1:8001}"
router:
  default_policy: "sequential-failover"
  attempt_timeout_ms: "${AVDR_ATTEMPT_TIMEOUT_MS:-2000}"
```

| Router env var | Default | Meaning |
| --- | --- | --- |
| `RESOLVER_A_URL` / `_B_` / `_C_` | loopback ports | Resolver endpoints |
| `AVDR_DEFAULT_POLICY` | `sequential-failover` | Policy when unspecified |
| `AVDR_SINGLE_STATIC_TARGET` | `resolver-a` | Control-baseline target |
| `AVDR_ATTEMPT_TIMEOUT_MS` | `2000` | **Explicit per-attempt timeout** |
| `AVDR_TELEMETRY_DIR` | `telemetry` | JSONL output directory |

**Timeouts are explicit and never inherited from library defaults.** The
configured value is recorded on every logical request as
`attempt_timeout_ms`, so a trace can always be read against the deadline that
produced it. The deployed default is 2000 ms; the automated tests use 400 ms
to stay fast.

---

## Controlled fault injection

**[CONTROLLED INJECTION] — test knobs, not measurements.** Behaviour belongs
to each resolver instance, set by environment variable at startup or through
the resolver's own admin endpoint at runtime.

| Env var | Default | Effect |
| --- | --- | --- |
| `RESOLVER_ID` | `resolver-local` | Identity reported in responses |
| `ARTIFICIAL_DELAY_MS` | `0` | Sleep before responding |
| `FORCE_ERROR` | `false` | Respond with `FORCE_ERROR_STATUS` |
| `FORCE_ERROR_STATUS` | `503` | Status used by `FORCE_ERROR` |
| `FORCE_INVALID` | `false` | HTTP 200 with an unacceptable document |
| `FORCE_TIMEOUT` | `false` | Hold past the router deadline |
| `TIMEOUT_SLEEP_MS` | `30000` | Duration of the hold |
| `DETERMINISTIC_FAILURE_EVERY_N` | `0` | Fail every Nth request (0 = off) |

Runtime control (no rebuild needed):

```bash
curl -X POST http://127.0.0.1:8001/admin/behavior \
  -H 'Content-Type: application/json' \
  -d '{"artificial_delay_ms":200}'

curl -X POST http://127.0.0.1:8001/admin/reset      # back to healthy
```

Scenarios are defined in `config/scenarios.yaml`:

| ID | Name | Condition |
| --- | --- | --- |
| N | normal | All resolvers healthy (control) |
| D | delay | resolver-b `+200 ms` (injected test value) |
| F | failure | resolver-a forced HTTP 503 |
| I | invalid | resolver-a HTTP 200 with unacceptable document |
| T | timeout | resolver-a holds past the deadline |
| X | all-fail | every resolver forced to 503 |

---

## Example requests

```bash
# Resolve with the control baseline
curl "http://127.0.0.1:8000/1.0/identifiers/did:example:alice?policy=single-static"

# Round-robin: run three times, watch returned_resolver rotate a -> b -> c
curl "http://127.0.0.1:8000/1.0/identifiers/did:example:alice?policy=round-robin"

# Sequential failover
curl "http://127.0.0.1:8000/1.0/identifiers/did:example:alice?policy=sequential-failover"

# Inspect the full trace for one logical request
curl "http://127.0.0.1:8000/telemetry/requests/<request_id>"
curl "http://127.0.0.1:8000/telemetry/recent?limit=5"

# Service surface
curl "http://127.0.0.1:8000/health"
curl "http://127.0.0.1:8000/policies"
```

Successful response (abridged, actual output):

```json
{
  "request_id": "0a6cc9d3-9348-4086-831b-7aa5df17fa4f",
  "did": "did:example:alice",
  "routing_policy": "single-static",
  "returned_resolver": "resolver-a",
  "attempted_sequence": ["resolver-a"],
  "attempt_count": 1,
  "logical_completion_latency_ms": 6.144,
  "didDocument": { "id": "did:example:alice", "...": "..." },
  "didResolutionMetadata": { "resolver_id": "resolver-a", "synthetic": true }
}
```

When no resolver returns an acceptable response the router replies **HTTP
502** with `error: "noAcceptableResponse"` and a per-attempt outcome list.

---

## Telemetry

Two **separate** JSONL files, joined on `request_id`, so the two record levels
can never be accidentally aggregated together:

- `telemetry/requests.jsonl` — one line per **logical request**
- `telemetry/attempts.jsonl` — one line per **resolver attempt**

The measurement harness writes two further files, kept apart from serving
traffic (see *Measurement harness* below):

- `shadow_trials.jsonl` — one line per **shadow trial**
- `shadow_observations.jsonl` — one line per **resolver observation**

Real-provider qualification adds three more, again kept separate because they
come from live third-party endpoints rather than controlled local resolvers:

- `real_provider_trials.jsonl` — one line per **real multi-provider trial**
- `real_provider_observations.jsonl` — one line per **provider observation**
- `raw_responses.jsonl` — the exact bodies, preserved for audit

The real routing service writes two more, again separated by record level:

- `real_routing_requests.jsonl` — one line per **logical routing request**
- `real_routing_attempts.jsonl` — one line per **provider attempt**

One logical request may contain several attempts under failover; conflating
the two would corrupt any later fan-out or burden analysis.

| Logical request | Resolver attempt |
| --- | --- |
| `request_id`, `timestamp`, `did`, `did_method` | `request_id`, `attempt_index` |
| `routing_policy`, `policy_version`, `policy_target` | `resolver_id`, `resolver_url` |
| `candidate_sequence`, `attempted_sequence` | `start_ts`, `end_ts`, `latency_ms` |
| `returned_resolver`, `success`, `final_error` | `http_status`, `outcome`, `timeout` |
| `logical_completion_latency_ms` | `document_valid`, `acceptance_reason`, `accepted` |
| `attempt_count`, `fanout_count`, `canceled_count` | `canceled`, `error` |
| `attempt_timeout_ms` | |

Conventions:

- Durations come from a **monotonic clock** (`time.perf_counter`); `start_ts` /
  `end_ts` are UTC wall clock for ordering only. Durations are never derived
  from wall-clock differences.
- Unavailable values are **`null`, never fabricated**. `document_valid: null`
  means "no document was evaluated" (e.g. an HTTP error); `false` means
  "evaluated and found unacceptable". The distinction matters.
- Latency is recorded even for timeouts and connection errors, because time
  was genuinely spent.

---

## Measurement harness (shadow characterization)

**`src/avdr/shadow.py` is measurement-only and is NOT a routing policy.** It is
deliberately absent from `router.policies.POLICY_TYPES`, unreachable from the
router's request path, and never serves a client. Probing every resolver on
every request is precisely the all-race behaviour this project exists to
avoid in production; here it is an instrument, not a service.

It exists because the routing policies cannot answer counterfactual questions.
Sequential failover stops at the first acceptable response, so the resolvers it
did not attempt have no observation at all. A **shadow trial** probes every
candidate resolver for the same logical context, producing one observation per
resolver:

```
                    +--> resolver-a --> observation
  trial (one DID) --+--> resolver-b --> observation
                    +--> resolver-c --> observation
```

Modes: `parallel` (all probes launched from one trial start) and `sequential`.
Parallel start is **approximate, never perfect** — the per-probe
`launch_offset_ms` is recorded and the trial's `launch_skew_ms` is measured, not
assumed to be zero.

Output goes to two further JSONL files: `shadow_trials.jsonl` and
`shadow_observations.jsonl`, joined on `(experiment_id, trial_id)`. These are
kept separate from the router's serving telemetry so measurement data and
production traces can never be pooled into one dataset.

### Provenance

Every measurement record is bound to the run that produced it:

| Field | Meaning |
| --- | --- |
| `experiment_id` | One qualification/characterization run |
| `trial_id` | One trial; shared by all its observations |
| `scenario_id` | Injected condition applied |
| `phase` | Project phase that produced the row |
| `seed` | RNG seed for workload generation |
| `git_commit` / `git_dirty` | Code identity; `git_dirty` flags uncommitted changes |
| `config_hash` | SHA-256 over the canonicalised router config |
| `injection_config_hash` | SHA-256 over the **fully expanded** injected state |
| `unresolved` | Why any of the above is null |

Hashing uses a deterministic canonical serialisation (recursively sorted keys,
list order preserved). **Nothing is fabricated:** an unresolvable git SHA or
hash is recorded as `null` with its reason in `unresolved`, never filled with a
plausible value. Scenario expansion fills defaults for resolvers a scenario does
not mention, so an omission and an explicit healthy setting hash identically.

### Derived analysis — qualification only

`src/avdr/analysis.py` computes, per complete trial:

- `fastest_responding_resolver` — argmin latency among resolvers that returned
  a complete HTTP response
- `fastest_accepted_resolver` — argmin latency among resolvers whose response
  passed structural acceptance
- `fastest_matches_fastest_accepted`

Two rules keep this honest:

- **Right-censoring.** A timed-out or connection-failed observation has no
  completion time; its latency is a lower bound set by our own deadline. Such
  observations are excluded from both minima and counted separately, never
  treated as very slow completions.
- **Completeness.** A trial missing any observation is marked incomplete and
  excluded from derivation. It is never partially analysed.

Results carry the label **`CONTROLLED LOCAL QUALIFICATION`**. They demonstrate
that the pipeline computes the quantity correctly against known injected ground
truth (scenario `Q`). They are **not** findings about real DID resolvers, and
proportions are deliberately not reported as evidence in this phase.

```bash
docker compose up -d
python scripts/measurement_qualification.py --trials 3 --seed 20260906
```

N is intentionally tiny — the script refuses more than 10 trials per scenario.
This phase validates the apparatus; it is not a characterization experiment.

---

## Real DID resolver compatibility

`src/avdr/real_shadow.py` extends the measurement-only discipline to real
resolver endpoints. It is **not** a routing policy either.

### Provider inventory (`config/providers.yaml`)

Records, per provider: endpoint, adapter, auth requirement, supported methods,
media type, whether a full DID Resolution Result is returned, rate-limit
information, terms, and an explicit `independence_notes` field.

**Distinct endpoints are not distinct implementations, and distinct
implementations are not independent providers.** Two hostnames that resolve to
one IP are one deployment. Two deployments of the same codebase are one
implementation. The inventory records what is actually known and excludes
non-distinct or unavailable entries with a stated reason rather than inflating
the provider count.

### Adapters

| Adapter | Shape |
| --- | --- |
| `universal-resolver-v1` | Full `{didResolutionMetadata, didDocument, didDocumentMetadata}` |
| `did-document-only-v1` | Bare driver response carrying only `didDocument` |

Provider-specific knowledge lives only in adapters. Absent metadata is recorded
as `null` — "this provider did not supply it" — and is **never synthesised** to
make providers look alike. Route/driver details are kept as
`provider_route_metadata`, separate from DID semantics.

### Acceptance profile `w3c-basic-v1`

Structural only. Each check is recorded separately and `accepted` is derived
from them:

`transport_success` · `media_type_processable` · `body_parseable` ·
`no_resolution_error` · `did_document_present` ·
`did_document_id_matches_request` · `structurally_processable`

A passing result is **"structurally acceptable under w3c-basic-v1"** — never
"verified" or "cryptographically verified". No signature, proof, key-material,
freshness or cross-provider agreement check is performed.

### Connection modes

| Mode | Meaning |
| --- | --- |
| `new-client` | Fresh `AsyncClient` and pool per trial. **Not "cold":** OS/DNS caches and platform TLS session reuse are not flushed. |
| `reused-client` | One client across trials; keep-alive and pooled TLS reused. |

The label deliberately avoids the word "cold" because true cold TCP/TLS
semantics cannot be guaranteed here. Modes are never mixed inside one derived
comparison.

### Launch order and skew

Provider launch order is rotated per trial from a recorded deterministic seed,
because launch skew is not zero and a fixed order would give one provider a
systematic head start. Every trial records `launch_order`, per-provider
`launch_offset_ms`, and `launch_skew_ms`.

### Response differences

When providers return different documents for the same DID, the run records
`normalized_document_hash` per provider, `exact_subject_match`, metadata
presence, and flags `PROVIDER_RESULT_DIFFERENCE_OBSERVED`. It does **not** call
any provider stale, invalid, incorrect or Byzantine: that would require
method-specific ground truth this project does not have.

### Rate limits and ethics

Public resolver endpoints are testing instances and are **not** load-tested.
The qualification script enforces a hard per-provider request budget, paces
requests, and treats HTTP 429 **and 403** as back-off signals that abort the
run rather than being retried around.

```bash
python scripts/real_did_qualification.py --pacing 5.0
```

---

## Real routing service

A user-facing service over the qualified real adapters. The mock router
(`avdr.router.app`) is **untouched** and still serves the deterministic local
path; both share the same separations — policy holds no I/O, transport holds
no policy — and since this milestone both share one probe call path
(`src/avdr/probe.py`), so a routing attempt and a shadow observation are
produced by identical code.

```bash
python -m uvicorn avdr.real_router.app:app --port 8080     # PYTHONPATH=src
curl -X POST localhost:8080/resolve -H 'content-type: application/json'      -d '{"did":"did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
          "policy":"sequential-failover"}'
curl localhost:8080/providers
curl localhost:8080/policies
curl localhost:8080/telemetry/requests/<request_id>
```

### Baseline policies (all three are baselines — none is adaptive)

| Policy | Execution | Min providers | Behaviour |
| --- | --- | --- | --- |
| `single-static` | sequential | 1 | One explicitly selected provider. **No hidden failover.** |
| `sequential-failover` | sequential | 1 | In order until structurally acceptable; nothing contacted after success. |
| `all-race` | concurrent | **2** | All candidates at once; first *structurally acceptable* completion wins. |
| `adaptive-min-set` | concurrent | 1 | **Proposed logic**, not a baseline. Minimum-cost subset predicted to meet the target, then raced. |

`all-race` is the maximum-fan-out control this project exists to improve on,
not the proposed algorithm. It requires two providers — racing one provider is
not a race, and pretending otherwise would fabricate redundancy.

**First acceptable, not first response.** A faster completion that fails
`w3c-basic-v1` does not win; the race continues past it.

### Capability-aware candidate selection

No provider is used merely because it is configured. Each is evaluated and
either becomes a candidate or is skipped with a typed reason:

`provider_unavailable` · `method_not_supported` ·
`auth_required_no_credentials` · `unknown_adapter` · `rate_budget_exhausted`

**A skipped provider is not a failed provider** — it was never called.
Exclusions and runtime failures live in separate telemetry fields and are
never pooled.

When a policy needs more redundancy than exists, the service returns HTTP 409
`INSUFFICIENT_QUALIFIED_PROVIDERS` with `qualified_provider_count` and the full
skip list, rather than silently degrading. A single-provider request also
carries a `SINGLE_QUALIFIED_PROVIDER` warning.

### Request budgets

Budgets are part of *eligibility*, not an afterthought: a provider whose
rolling-window budget would be exceeded is skipped **before** it is called.
The public endpoint's disclosed limit (10 requests / 1800 s) is enforced this
way. Loopback providers are unlimited — no third party's budget is spent.

### Cancellation honesty

After a winner is found, outstanding requests are cancelled. Cancellation is
recorded as `canceled_before_dispatch` or
`canceled_after_dispatch_provider_side_unknown` — once a request is on the
wire the provider may already have done the work, and cancellation is never
reported as if it had prevented that.

**Launch-timing invariant:** `dispatched == true ⇒ launch_offset_ms is not
null`. A cancelled attempt that reached dispatch keeps the launch offset
measured before the request left, and carries `null` *latency* only (never a
fabricated `0`). The only permitted null offset on a dispatched attempt is one
accompanied by an explicit `telemetry_error`, and a pydantic validator
enforces this at construction rather than by convention.

### Adaptive minimum-set decision engine

Three layers, deliberately separated — estimation never optimizes, optimization
never performs I/O, execution never re-derives either:

```
request context
   -> Estimator   q_hat(S | x)            src/avdr/adaptive/estimator.py
   -> Optimizer   S* = argmin C(S)        src/avdr/adaptive/optimizer.py
   -> Executor    first acceptable        src/avdr/real_router/executor.py
```

**Contract**

```
S*(x) = argmin C(S)   subject to   q_hat(S | x) >= target
C(S)  = |S|                                        [DESIGN CHOICE]
```

Cardinality is a stand-in for request burden, **not** a validated economic
model; the `CostModel` interface lets later work substitute API/resource cost
without touching the optimizer.

**No independence assumption.** The estimator is defined over *subsets*, and
`q_hat(S) = 1 - Π(1 - p_i)` is **not** implemented and is **not** used as a
fallback. A subset with no estimate is reported as unknown
(`unestimated_subset_count`), never synthesised. A test asserts the source
contains no product accumulator, and another proves behaviourally that a
composite subset's value comes from the table by using an *anti-independent*
fixture where the pair scores lower than either member.

**Tie-break rule** (fixed before execution, never random):

1. lowest cost
2. highest `q_hat`
3. lexicographic provider-id order

Criterion "lowest predicted accepted latency" is defined in the ordering but
**skipped in this milestone** — no latency predictor exists.

**When nothing satisfies the target** the planner returns HTTP 409
`SLO_ESTIMATE_UNSATISFIABLE` with the target, the best subset found and its
estimate. It never silently calls everything and reports the SLO as met. An
optional degradation mode executes all eligible providers but is tagged
`best_effort: true`, and `OptimizerResult.satisfied` is `False` for it.

**Bounds.** Exact enumeration evaluates `2^M − 1` subsets, so
`MAX_ADAPTIVE_CANDIDATES = 12` [DESIGN CHOICE]. Exceeding it returns
`ADAPTIVE_CANDIDATE_LIMIT_EXCEEDED` rather than truncating the provider set or
switching to an unvalidated approximation.

```bash
curl -X POST localhost:8080/resolve -H 'content-type: application/json'      -d '{"did":"did:example:x","policy":"adaptive-min-set",
          "target_slo_probability":0.992}'
```

Clients supply **only the target**. The estimator and its `q_hat` table are
server-side configuration — a client can never submit its own probability
table. Without a configured estimator the policy is simply unavailable, and
the three baselines are untouched.

**The estimator in this milestone is a deterministic table supplied as
[CONTROLLED TEST INPUT].** No model is trained. Qualification proves the
decision layer selects and executes correctly *given* estimates; it is **not**
evidence that the estimates are correct.

### Prospective subset-probability estimator

**CONTROLLED LOCAL QUALIFICATION.** Every distribution is injected by us on
local mock providers. Nothing here measures real DID reliability, latency,
independence, or any production SLO.

```
controlled stochastic episodes -> fully observed trials -> subset targets
  -> pre-request features -> estimators -> freeze -> prospective holdout
  -> optimizer integration
```

**Target.** `Y_t(S) = 1` iff some provider in `S` is accepted AND
`launch_offset + latency <= tau`, with `tau = 250 ms` [DESIGN CHOICE] fixed
before generation. Raw per-attempt latency is never used on its own. Timeouts,
errors and unacceptable responses contribute 0; a missing observation excludes
the trial rather than being read as either success or failure.

**Correlation is real here.** `SHARED_DEGRADATION` slows every provider at
once — measured within-deadline rates a=0.11 / b=0.14 / c=0.04 versus 1.00 for
all three under `NORMAL`. Redundancy does not help in that state, which is
exactly why `q(S) = 1 - Π(1 - p_i)` is never used.

**Feature boundary.** 30 features built only from trials with index `< t`.
The hidden injected state is recorded for audit and excluded from features by
test; leakage tests perturb the current trial and append future trials to
prove neither changes a row.

**Splits** are episode-level with disjoint episode ids *and* seeds — adjacent
trials within an episode are correlated, so a random row split would leak.

**Selection rule, fixed in advance:** lowest validation Brier, then the
simplest candidate within 0.005 Brier of the best. **Calibration rule, fixed
in advance:** apply Platt scaling (fit on TRAIN only) iff validation ECE
> 0.05.

```bash
docker compose up -d
python scripts/estimator_pipeline.py      # generate, train, select, freeze
python scripts/prospective_holdout.py     # run ONCE, from a clean tree
```

**Headline result: the non-ML EWMA baseline won.** On validation Brier,
`b2-ewma` (0.1131) beat both `m2-hist-gradient-boosting` (0.1384) and
`m1-logistic` (0.1573). ML complexity was **not** justified in this controlled
environment. That is reported as the result, not engineered around.

### Demos

```bash
docker compose up -d
python scripts/real_routing_demo.py     # Scenarios A/B/C, zero public calls
python scripts/real_routing_smoke.py    # ONE real public request
python scripts/adaptive_qualification.py  # K1/K2/K3/KU, zero public calls
```

The smoke script reports `REAL_E2E_DEFERRED_RATE_BUDGET` if the provider
signals throttling. **A deferral is not a pass.**

---

## How to run tests

```bash
# Unit + integration suite (starts real mock resolvers on loopback sockets)
./.venv/Scripts/python.exe -m pytest -q          # Windows
python -m pytest -q                              # POSIX

# End-to-end qualification against docker compose (stack must be up)
docker compose up -d --build
python scripts/e2e_smoke.py

# Measurement-apparatus qualification (shadow harness + provenance + analysis)
python scripts/measurement_qualification.py

# Real DID resolver compatibility qualification (hits real public endpoints)
python scripts/real_did_qualification.py --pacing 5.0
```

**The test suite makes zero public-network calls, and this is enforced rather
than assumed:** a session-wide autouse fixture in `tests/conftest.py` blocks
every outbound socket connection to a non-loopback address, so a new test
cannot quietly start spending the public resolver's 10 req / 1800 s budget.
Real provider shapes are exercised through `httpx.MockTransport` and local
loopback services. Only the explicit qualification/smoke scripts above make
real endpoint calls.

`scripts/e2e_smoke.py` exercises scenarios N, D, F, I, T and X, checks router
behaviour and telemetry for each, prints a PASS/FAIL line per gate, writes
`artifacts/e2e_report.json`, and exits non-zero if any gate fails.

Routing gates: `PASS_LOCAL_MULTI_RESOLVER_E2E`, `PASS_ROUND_ROBIN`,
`PASS_SEQUENTIAL_FAILOVER`, `PASS_ATTEMPT_LEVEL_TELEMETRY`,
`PASS_CONTROLLED_DELAY_INJECTION`, `PASS_CONTROLLED_FAILURE_INJECTION`.

`scripts/measurement_qualification.py` re-runs the routing gates as a
regression check, then adds: `PASS_PROVENANCE_BINDING`,
`PASS_SHADOW_ALL_RESOLVER_OBSERVATION`, `PASS_PARALLEL_MEASUREMENT_PATH`,
`PASS_TRIAL_COMPLETENESS_CHECK`, `PASS_FASTEST_VS_FASTEST_ACCEPTED_ANALYSIS`,
`PASS_EXISTING_BASELINE_REGRESSION`.

`scripts/real_did_qualification.py` adds: `PASS_REAL_RESOLVER_ADAPTER`,
`PASS_REAL_DID_RESOLUTION_E2E`, `PASS_MULTI_PROVIDER_SAME_DID_SHADOW`,
`PASS_W3C_BASIC_ACCEPTANCE`, `PASS_REAL_ERROR_NORMALIZATION`,
`PASS_CONNECTION_MODE_PROVENANCE`, `PASS_REAL_PROVIDER_PROVENANCE`,
`PASS_EXISTING_MEASUREMENT_REGRESSION`.

`scripts/real_routing_demo.py` and `scripts/real_routing_smoke.py` cover the
routing gates: `PASS_REAL_ROUTER_API`, `PASS_CAPABILITY_AWARE_SELECTION`,
`PASS_REAL_SINGLE_STATIC_BASELINE`, `PASS_REAL_SEQUENTIAL_FAILOVER_BASELINE`,
`PASS_REAL_ALL_RACE_BASELINE`, `PASS_FIRST_ACCEPTABLE_SEMANTICS`,
`PASS_PROVIDER_BUDGET_ENFORCEMENT`, `PASS_REAL_ROUTING_TELEMETRY`,
`PASS_PROVIDER_STATUS_ENDPOINT`.

---

## Reproducibility

- Dependencies pinned in `requirements.txt` / `requirements-dev.txt`
- Python 3.12 (container: `python:3.12-slim`)
- Scenarios declared in `config/scenarios.yaml`, applied from that file at run
  time and hashed into `injection_config_hash`
- Every measurement row carries `git_commit`, `config_hash` and
  `injection_config_hash`, so a dataset can be bound to the exact run
- Telemetry is raw JSONL and is **not** committed; it is regenerated by
  running the system, so it is never mistaken for curated evidence

---

## Not implemented (by design, this milestone)

Machine learning / predictive routing · adaptive-k subset sizing · parallel
racing or hedging **as a routing policy** · BFT or quorum agreement ·
cross-resolver consistency checks · cryptographic proof verification ·
freshness/version checks · blockchain or smart-contract logging · cloud
deployment · statistical characterization claims.

Real DID providers ARE now contacted, but only by the compatibility
qualification script, and only to prove the adapter/normalization/provenance
path works. **No performance claim, provider ranking, DID-method comparison,
resolver-heterogeneity claim, or justification for adaptive routing may be
derived from any run in this repository.**

The shadow harness probes all resolvers concurrently, but as a *measurement
instrument only*. It is not a routing policy and does not serve requests.
