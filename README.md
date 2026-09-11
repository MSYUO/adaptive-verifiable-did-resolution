# Adaptive Verifiable DID Resolution

> 신뢰 가능한 DID Resolution을 위한 적응형 멀티 리졸버 라우팅 시스템

AVDR은 하나의 Resolver에 의존하거나 모든 Resolver를 항상 호출하는 대신,
요청 시점의 context를 바탕으로 목표를 만족할 것으로 추정되는 최소 Resolver subset을 선택합니다.
선택된 후보는 동시에 실행하며, 단순히 가장 빠른 응답이 아니라 구조적으로 허용 가능한 첫 응답을 반환합니다.

## 30초 요약

| 질문 | 답변 |
| --- | --- |
| 문제 | single resolver는 장애·지연에 취약하고, all-race는 매 요청마다 최대 fan-out을 사용합니다. |
| 기존  방식 | `single-static`, `sequential-failover`, `all-race`를 비교 기준인 baseline으로 구현했습니다. |
| 제안  방식 | `adaptive-min-set`이 `q_hat(S \| x)`를 이용해 목표를 만족하는 최소 비용 subset을 선택합니다. |
| 실행  의미 | 선택된 Resolver를 concurrent하게 호출하고 **First Acceptable Response**를 반환합니다. |
| 핵심  목표 | 허용 가능한 DID Resolution 결과를 유지하면서 불필요한 request fan-out을 줄이는 것입니다. |
| 구현  범위 | FastAPI 서비스, dashboard, adaptive runtime, telemetry, audit receipt, controlled qualification, bounded public DID compatibility를 포함합니다. |

```text
single resolver        장애 또는 지연이 전체 요청에 직접 영향
all-race               모든 qualified Resolver 호출 = maximum fan-out baseline
adaptive-min-set       context → estimate → minimum acceptable subset → first acceptable
```

## 문제 정의

DID Resolution에는 서로 다른 capability, 가용성, 응답 시간과 결과 형식을 가진 경로가 존재할 수 있습니다.

- **single resolver**: 한 경로에 의존하므로 해당 경로의 실패나 지연에 취약합니다.
- **sequential failover**: 복구 경로를 제공하지만 앞선 시도가 끝난 뒤 다음 시도를 시작하므로 latency trade-off가 있습니다.
- **all-race**: 모든 qualified candidate를 동시에 호출해 빠르게 허용 가능한 응답을 찾지만, 매 요청의 fan-out과 provider burden이 최대가 됩니다.

AVDR은 “항상 하나”와 “항상 전부” 사이에서 요청별로 필요한 subset을 선택하는 문제를 다룹니다.

## 핵심 아이디어

```text
Request Context x
       │
       ▼
Estimator ── q_hat(S | x)
       │
       ▼
Optimizer ── 목표를 만족하는 minimum-cost subset S*
       │
       ▼
Executor ── 선택된 subset을 concurrent 실행
       │
       ▼
First Acceptable Response + Telemetry + Audit Receipt
```

```text
S*(x) = argmin C(S)
        subject to q_hat(S | x) >= target

C(S) = |S|
```

현재 `C(S) = |S|`는 request burden을 나타내는 **design choice**입니다.
검증된 monetary/economic cost model이 아니며, optimizer는 교체 가능한 `CostModel` interface를 사용합니다.

## Routing Policies

| Policy | 실행 | 역할 |
| --- | --- | --- |
| `single-static` | 선택된 한 provider만 호출 | hidden failover가 없는 control baseline |
| `sequential-failover` | 순서대로 호출하고 허용 가능한 응답에서 중단 | latency/failover baseline |
| `all-race` | 모든 qualified candidate를 concurrent 호출 | **maximum-fan-out control baseline** |
| `adaptive-min-set` | 추정 목표를 만족하는 최소 비용 subset을 concurrent 호출 | **proposed routing logic** |

`all-race`는 제안 알고리즘이 아닙니다. 두 개 이상의 candidate를 요구하며 모든 후보를 호출하는 비교 기준입니다.
두 concurrent policy 모두 가장 먼저 끝난 응답이 아니라 `w3c-basic-v1`을 통과한 **First Acceptable Response**를 선택합니다.

## 대표 Validation 결과

`artifacts/learning/pipeline_report.json`의 `CONTROLLED LOCAL QUALIFICATION` validation 결과입니다.

| Estimator | Validation Brier ↓ |
| --- | ---: |
| `b2-ewma` | **0.1131** |
| `m2-hist-gradient-boosting` | 0.1384 |
| `m1-logistic` | 0.1573 |

통제된 local environment의 validation에서 non-ML `b2-ewma`가 세 후보 중 가장 낮은 Brier Score를 기록했습니다.
이는 실제 DID network에서의 우수성, 통계적 유의성, production 성능 또는 일반화를 입증하지 않습니다.

## 시스템 구조

```text
Client / Dashboard
        │
        ▼
FastAPI Real Router API
        │
        ▼
Capability + Budget-aware Candidate Selection
        │
        ▼
Routing Policy ── Estimator → Optimizer (adaptive-min-set)
        │
        ▼
Resolver Adapter → DID Resolver
        │
        ▼
Structural Acceptance → First Acceptable
        │
        ▼
Telemetry → Local Audit Receipt → Optional Sepolia Commitment
```

## 주요 구현 기능

- **FastAPI routing API와 dashboard**: 동일 process가 `/resolve`와 `/dashboard/`를 제공합니다.
- **Capability-aware candidate selection**: DID method, availability, credentials, adapter와 request budget을 호출 전에 검사합니다.
- **Resolver adapters**: provider별 응답을 공통 DID Resolution Result 형태로 정규화합니다.
- **First Acceptable semantics**: 빠르지만 구조적으로 허용되지 않는 응답은 winner가 아닙니다.
- **Request budget enforcement**: 제한을 초과할 provider를 dispatch 전에 제외합니다.
- **Cancellation telemetry**: dispatch 전·후 cancellation을 구분하고 provider-side 결과의 불확실성을 보존합니다.
- **Controlled fault injection**: delay, failure, timeout, invalid document scenario를 loopback Resolver에서 재현합니다.
- **Adaptive minimum-set optimizer**: subset estimate와 target으로 실행 subset을 결정합니다.
- **Frozen estimator serving**: typed specification을 검증하고 `RollingEmpiricalEstimator`를 runtime에 재구성합니다.
- **Prospective estimator pipeline**: episode 단위 split, validation, freeze, one-time holdout artifact를 제공합니다.
- **Provenance와 audit receipt**: canonical hash와 process-local hash chain으로 요청 결과의 무결성 경계를 기록합니다.
- **Reproducibility artifacts**: raw/derived evidence와 manifest를 분리해 재검산 경로를 제공합니다.

## Adaptive Minimum-Set

Estimator, optimizer, executor는 서로 분리되어 있습니다. Estimator는 I/O나 선택을 수행하지 않고,
optimizer는 HTTP를 호출하지 않으며, executor는 estimate를 다시 계산하지 않습니다.

- `q_hat(S | x)`는 개별 provider 확률을 독립이라고 가정해 합성하지 않고 **subset 자체**에 대해 정의됩니다.
- `q_hat(S) = 1 - Π(1 - p_i)`는 fallback으로 사용하지 않습니다.
- estimate가 없는 subset은 임의로 채우지 않고 unknown으로 보고합니다.
- 기본 coverage mode에서 모든 non-empty subset의 estimate가 없으면 exact-minimum claim을 차단합니다.
- 만족하는 subset이 없으면 `SLO_ESTIMATE_UNSATISFIABLE`을 반환하며, 전부 호출한 뒤 목표를 달성했다고 표시하지 않습니다.
- exact enumeration은 `2^M - 1`개 subset을 평가하므로 `MAX_ADAPTIVE_CANDIDATES = 12`로 제한합니다.
- tie-break는 lowest cost → highest `q_hat` → provider ID의 lexicographic order로 고정됩니다.

Serving 기본 사양은 `service_assets/frozen_estimator_v3.spec.json`에 있습니다.
Runtime history는 process memory에만 존재하며 재시작 시 초기화되고, real과 controlled-demo namespace는 분리됩니다.

## 실제 DID Resolver 연동

FastAPI real routing service는 qualified adapter를 통해 public resolution 경로를 호출할 수 있습니다.
동결된 compatibility evidence는 `did:key`, `did:web`, `did:ethr`에 대해 각각 bounded qualification을 수행했습니다.

이 evidence는 동일한 Universal Resolver deployment 경로에서 얻은 interoperability 관찰입니다.
독립 provider 비교, provider ranking, DID method별 성능 비교 또는 production reliability benchmark가 아닙니다.
자세한 범위와 측정 기록은 [`docs/REAL_DID_COMPATIBILITY.md`](docs/REAL_DID_COMPATIBILITY.md)를 참고하십시오.

`w3c-basic-v1` acceptance는 transport와 media type, parse 가능성, resolution error,
DID document 존재와 requested DID 일치, 구조적 처리 가능성을 확인합니다.
이는 signature, proof, freshness, key material 또는 canonical DID truth에 대한 cryptographic verification이 아닙니다.

## Optional Sepolia Commitment Anchoring

Audit receipt의 최소 commitment를 Ethereum Sepolia transaction calldata에 기록하고 다시 읽어 검증하는 선택적 경로가 있습니다.
기본값은 `not_configured`이며 routing algorithm과 분리되어 있습니다. raw DID, DID document와 telemetry는 anchor interface로 전달하지 않습니다.

기존 동결 transaction의 성공적인 readback은 local receipt commitment와 on-chain commitment가 일치함을 뜻할 뿐,
DID 또는 Resolver의 진실성·정확성을 검증하지 않습니다. 자세한 경계는
[`docs/BLOCKCHAIN_ANCHOR.md`](docs/BLOCKCHAIN_ANCHOR.md)와
[`docs/BLOCKCHAIN_LIVE_EVIDENCE.md`](docs/BLOCKCHAIN_LIVE_EVIDENCE.md)에 정리되어 있습니다.

## Quick Start

### Presenter dashboard — Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\scripts\start_demo.ps1
```

`AVDR_DEMO_STATUS=READY`가 출력되면 <http://127.0.0.1:8080/dashboard/>를 엽니다.
이 경로는 세 개의 controlled loopback Resolver와 real router UI를 실행하며 public endpoint를 호출하지 않습니다.

```powershell
.\scripts\stop_demo.ps1
```

발표 순서와 복구 절차는 [`docs/HACKATHON_DEMO_RUNBOOK.md`](docs/HACKATHON_DEMO_RUNBOOK.md)에 있습니다.

### Controlled baseline stack — Docker Compose

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
docker compose down
```

### Real Router API — 명시적 실행

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m uvicorn avdr.real_router.app:app --host 127.0.0.1 --port 8080
```

```bash
curl -X POST http://127.0.0.1:8080/resolve \
  -H "content-type: application/json" \
  -d '{"did":"did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK","policy":"sequential-failover"}'
```

Public qualification과 smoke script는 외부 endpoint에 실제 요청을 보냅니다.
rate limit과 evidence scope를 확인하지 않은 채 반복 실행하지 마십시오.

## Tech Stack

Python 3.12 · FastAPI · Uvicorn · HTTPX · Pydantic · PyYAML · NumPy · scikit-learn · Docker Compose · Ethereum Sepolia

## Repository Structure

| Path | 역할 |
| --- | --- |
| `src/avdr/real_router/` | real routing API, baseline/adaptive policy, concurrent executor |
| `src/avdr/adaptive/` | subset estimator contract와 minimum-set optimizer |
| `src/avdr/learning/` | controlled estimator dataset, feature, metric과 training pipeline |
| `src/avdr/resolver/` | controlled local Resolver와 fault-injection surface |
| `config/` | Resolver/provider inventory, fixtures와 scenario configuration |
| `service_assets/` | 검증 가능한 non-executable frozen serving specification |
| `web/` | dependency-free presenter dashboard asset |
| `scripts/` | demo, qualification, estimator와 evidence 실행 entry point |
| `tests/` | unit/integration 및 public-network 차단 test suite |
| `artifacts/` | reproducibility report, frozen evidence와 재검산 자료 |
| `docs/` | audit, real compatibility, Sepolia와 presenter runbook |

## 검증 범위 및 한계

- controlled local Resolver는 동일 Docker host의 CPU, kernel, network stack과 storage를 공유합니다. 독립 gateway가 아닙니다.
- controlled delay, failure, timeout과 invalid document는 주입한 engineering condition이며 실제 Resolver latency/reliability 측정값이 아닙니다.
- controlled qualification은 synthetic DID document를 사용하며 public DID qualification과 분리됩니다.
- public evidence는 bounded compatibility/smoke 범위이며 load test, provider benchmark 또는 ranking이 아닙니다.
- structural acceptance는 cryptographic proof verification, freshness, cross-resolver agreement 또는 method-specific truth 검증을 포함하지 않습니다.
- validation과 holdout 결과는 controlled local distribution에 한정되며 production 성능이나 일반화를 주장하지 않습니다.
- cardinality cost는 request burden proxy일 뿐 검증된 경제 비용이 아닙니다.
- runtime observed history는 durable storage가 아닌 process-local memory입니다.
- optional Sepolia anchor는 receipt commitment provenance를 보조하며 on-chain DID resolution이나 전체 결과의 blockchain logging이 아닙니다.

## English Technical Documentation

개편 전 947줄의 영문 기술 문서는 [`README_EN.md`](README_EN.md)에 원문 그대로 보존되어 있습니다.
구현별 상세 contract와 evidence 해석은 해당 문서 및 [`docs/`](docs/)를 참고하십시오.
