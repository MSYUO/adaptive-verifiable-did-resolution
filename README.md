# Adaptive Verifiable DID Resolution

Local engineering prototype of a DID resolution router that returns the first
**acceptable** response rather than merely the first response.

> **Scope of this milestone: multi-resolver baseline routing + controlled fault
> injection.** There is deliberately no machine learning, no adaptive fan-out
> sizing (`adaptive-k`), no parallel racing, no blockchain and no connection to
> any real DID provider. Those belong to later phases.

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
| `src/avdr/telemetry.py` | Append-only JSONL sink, two separate files |
| `src/avdr/resolver/settings.py` | Injected-behaviour configuration |
| `src/avdr/resolver/app.py` | Mock resolver service + admin surface |
| `src/avdr/router/policies.py` | Policy abstraction and three baselines |
| `src/avdr/router/transport.py` | One instrumented resolver attempt |
| `src/avdr/router/app.py` | Router service, sequential execution loop |
| `config/resolvers.yaml` | Resolver registry and router settings |
| `config/scenarios.yaml` | Reproducible fault-injection scenario definitions |
| `scripts/e2e_smoke.py` | E2E qualification against docker compose |

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
passes all six. This is what separates "fastest response" from "fastest
acceptable response" in the telemetry.

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

## How to run tests

```bash
# Unit + integration suite (starts real mock resolvers on loopback sockets)
./.venv/Scripts/python.exe -m pytest -q          # Windows
python -m pytest -q                              # POSIX

# End-to-end qualification against docker compose (stack must be up)
docker compose up -d --build
python scripts/e2e_smoke.py
```

`scripts/e2e_smoke.py` exercises scenarios N, D, F, I, T and X, checks router
behaviour and telemetry for each, prints a PASS/FAIL line per gate, writes
`artifacts/e2e_report.json`, and exits non-zero if any gate fails.

Gates: `PASS_LOCAL_MULTI_RESOLVER_E2E`, `PASS_ROUND_ROBIN`,
`PASS_SEQUENTIAL_FAILOVER`, `PASS_ATTEMPT_LEVEL_TELEMETRY`,
`PASS_CONTROLLED_DELAY_INJECTION`, `PASS_CONTROLLED_FAILURE_INJECTION`.

---

## Reproducibility

- Dependencies pinned in `requirements.txt` / `requirements-dev.txt`
- Python 3.12 (container: `python:3.12-slim`)
- Scenarios declared in `config/scenarios.yaml`
- Telemetry is raw JSONL and is **not** committed; it is regenerated by
  running the system, so it is never mistaken for curated evidence

---

## Not implemented (by design, this milestone)

Machine learning / predictive routing · adaptive-k subset sizing · parallel
racing or hedging · BFT or quorum agreement · cross-resolver consistency
checks · cryptographic proof verification · freshness/version checks ·
blockchain or smart-contract logging · cloud deployment · real DID providers.
