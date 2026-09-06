# Adaptive Verifiable DID Resolution
## 블록체인·AI 융합 해커톤 실행 계획 + 후속 논문 확장 계획

> 작성일: 2026-09-06  
> 목표: **해커톤에서는 실제 동작하는 서비스 프로토타입을 완성하고, 그 과정에서 얻은 discovery를 바탕으로 후속 연구 질문을 동결(freeze)한 뒤 별도의 confirmatory experiment로 논문 확장 가능성을 검증한다.**

---

# 0. 프로젝트 한 줄 정의

**DID 요청 시 각 resolver/gateway의 현재 상태와 과거 성능을 기반으로 “deadline 안에 검증 가능한 결과를 반환할 가능성”을 예측하고, 필요한 최소 resolver 집합만 동적으로 선택하여 race/verification하는 적응형 DID resolution gateway.**

핵심은 단순히 **가장 빠른 gateway를 선택하는 것**이 아니다.

\[
\text{Fastest response}
\]

가 아니라

\[
\boxed{\text{Fastest acceptable / verifiable DID resolution}}
\]

을 목표로 한다.

---

# 1. 해커톤용 서비스 포지셔닝

## 1.1 사용자 관점 문제

기존 DID 서비스가 단일 resolver 또는 정적 failover에 의존하면 다음 문제가 발생할 수 있다.

- 특정 resolver의 순간적인 지연
- timeout 이후에야 failover되는 느린 복구
- stale / inconsistent / invalid response 가능성
- 여러 resolver를 항상 동시에 호출할 경우 불필요한 요청 비용 증가

해커톤에서는 이를 다음 서비스 문제로 단순화한다.

> **“DID 인증/조회 요청이 들어왔을 때, 현재 상태에 따라 1개 또는 복수 resolver를 선택하고, 가장 빠른 ‘허용 가능한’ 응답을 반환한다.”**

---

## 1.2 대회 트랙 적합성

공식 안내문 기준 주요 적합 트랙:

- **트랙1: AI + 블록체인 융합 서비스**
- 필요 시 **트랙4: 산업 및 비즈니스 혁신 솔루션**도 고려 가능

프로젝트 기획서에서 반드시 보여줘야 할 것:

1. 문제 정의
2. 기존 서비스 대비 차별성
3. 서비스/솔루션 개요
4. 사용자 시나리오
5. 핵심 기술 스택
6. 블록체인 기술 적용
7. 블록체인 + AI 융합 적용
8. 개발 계획
9. 개발 증빙 / GitHub / 데모

---

# 2. 선행 서비스 대비 포지셔닝

## 2.1 이미 존재하는 기능

다음 수준은 **novelty로 주장하지 않는다.**

- 여러 resolver 사용
- health-aware routing
- timeout 기반 failover
- latency 기반 endpoint 선택
- fixed hedging
- request racing
- blockchain RPC smart routing
- 단순 fastest-wins

즉 다음 문장은 금지한다.

> “우리는 여러 DID gateway 중 가장 빠른 곳을 자동 선택하는 최초 시스템이다.”

[금지 이유] 유사한 resolver/router 및 Web3 RPC routing 서비스가 이미 존재한다.

---

## 2.2 차별화 후보

본 프로젝트의 차별성은 다음 세 요소의 조합으로 잡는다.

### A. DID-aware acceptance

단순 HTTP 성공이 아니라 DID 응답이 **허용 가능한지**를 판단한다.

\[
A_i =
\begin{cases}
1 & \text{response is acceptable under method-specific rules}\\
0 & \text{otherwise}
\end{cases}
\]

가능한 acceptance 요소:

- DID document 형식 검증
- DID method-specific verification
- freshness/version 조건
- cross-resolver consistency
- cryptographic proof가 가능한 경우 proof 검증

---

### B. Adaptive minimum resolver set

resolver 수 \(k\)를 고정하지 않는다.

후보 집합:

\[
\mathcal G = \{g_1,g_2,\dots,g_M\}
\]

현재 상태 \(x\)에서:

\[
S^*(x)
=
\arg\min_{S\subseteq\mathcal G} C(S)
\]

subject to

\[
P(
T_{\text{accepted},S}\le\tau
\mid x
)
\ge 1-\epsilon
\]

따라서:

- 정상 상태: \(k^*=1\)
- 불안정 상태: \(k^*=2\)
- 높은 불확실성: \(k^*=3\) 또는 그 이상

이 가능하다.

---

### C. Prospective characterization

해커톤의 AI/optimizer는 target outcome을 본 뒤 fitting하지 않는다.

구조:

```text
characterization
    ↓
model / policy freeze
    ↓
unseen request
    ↓
pre-request prediction
    ↓
resolver-set selection
    ↓
actual execution
    ↓
actual outcome reveal
```

이 구조는 기존 Cluster Computing 연구에서 사용한 **characterization → freeze → prospective evaluation** 방법론을 DID domain에 새로 적용하는 것이다.

> 기존 cluster 파라미터나 수치를 DID에 재사용하지 않는다.

---

# 3. 프로젝트 핵심 연구/서비스 질문

## RQ0 — 필요성 확인

> 동일한 nominal DID resolver/gateway라도 성능이 실제로 interchangeable한가?

\[
H_0:
T_{G_1}
\simeq
T_{G_2}
\simeq
\dots
\simeq
T_{G_M}
\]

만약 실제 차이가 없다면 adaptive routing의 필요성이 약해진다.

---

## RQ1 — latency heterogeneity

> resolver 간 latency distribution과 tail behavior가 충분히 다른가?

측정:

- p50
- p95
- p99
- timeout rate
- error rate

---

## RQ2 — fastest vs acceptable

> 가장 빠른 resolver가 항상 가장 먼저 허용 가능한 응답을 주는가?

검사:

\[
\arg\min_i T_i
\overset{?}{=}
\arg\min_i T_i^{accepted}
\]

이 차이가 거의 없다면 DID-aware verification의 실효성이 약해질 수 있다.

---

## RQ3 — fixed-k vs adaptive-k

> 항상 1개, 2개, 3개를 호출하는 정책보다 state-dependent \(k^*(x)\)가 더 효율적인가?

평가:

- SLO success
- request fan-out
- cost
- accepted-response latency

---

## RQ4 — correlation

> resolver success/failure/latency가 독립적이라고 볼 수 있는가?

검사:

\[
P(A_i\cap A_j)
\overset{?}{=}
P(A_i)P(A_j)
\]

독립성이 확인되지 않으면:

\[
1-\prod_i(1-q_i)
\]

같은 독립식은 최종 모델에 사용하지 않는다.

대신 subset의 empirical joint outcome을 직접 측정한다.

---

## RQ5 — service value

> adaptive minimum-set이 all-race 수준의 reliability/SLO를 유지하면서 평균 fan-out을 줄이는가?

이것이 해커톤 데모와 후속 논문 모두에서 가장 강한 headline 후보다.

---

# 4. 시스템 아키텍처

```text
              ┌───────────────────┐
              │ Client / dApp     │
              └─────────┬─────────┘
                        │ DID Request
                        ▼
              ┌───────────────────┐
              │ Adaptive Router   │
              │ + Policy Engine   │
              └─────────┬─────────┘
                        │
        ┌───────────────┼────────────────┐
        │               │                │
        ▼               ▼                ▼
   Resolver A       Resolver B       Resolver C
        │               │                │
        └───────────────┼────────────────┘
                        │
                        ▼
              ┌───────────────────┐
              │ DID Validation    │
              │ / Agreement Layer │
              └─────────┬─────────┘
                        │
                        ▼
              ┌───────────────────┐
              │ Accepted Response │
              └───────────────────┘

Telemetry
   ↓
Feature Store / Metrics
   ↓
Predictor
   ↓
P(accepted response before deadline)

Optional blockchain audit layer
   ↓
- policy version hash
- request commitment
- selected resolver set
- result commitment
```

---

# 5. 컴포넌트 구성

## 5.1 Router

역할:

- 요청 수신
- DID method 식별
- predictor 호출
- resolver subset 결정
- parallel / staged dispatch
- first acceptable response 반환
- 나머지 request cancel
- telemetry 기록

예상 기술:

- Python + FastAPI
- asyncio / httpx

---

## 5.2 Resolver adapter

각 resolver가 다른 인터페이스를 가져도 통일된 내부 형태로 래핑.

공통 반환 형식 예:

```json
{
  "resolver_id": "r1",
  "did_method": "did:ethr",
  "latency_ms": 83.4,
  "http_ok": true,
  "did_document_valid": true,
  "freshness_ok": true,
  "verification_ok": true,
  "accepted": true
}
```

---

## 5.3 Predictor

초기에는 복잡한 딥러닝을 쓰지 않는다.

입력 후보:

- resolver_id
- DID method
- request type
- recent latency
- rolling p95
- recent error rate
- timeout rate
- current inflight requests
- queue depth
- time since last failure

출력 후보:

\[
\hat p_i =
P(T_i\le\tau \land A_i=1\mid x)
\]

초기 비교 모델:

- Logistic Regression
- Random Forest
- Gradient Boosting

가장 단순한 모델이 충분하면 복잡한 AI로 바꾸지 않는다.

---

## 5.4 Optimizer

### 기본 목적

\[
S^*(x)
=
\arg\min_{S} C(S)
\]

subject to

\[
\hat P(
T_{\text{accepted},S}\le\tau
)
\ge1-\epsilon
\]

### 비용 함수 예

\[
C(S)
=
\sum_{i\in S} c_i
\]

초기 해커톤 버전에서는 \(c_i=1\)로 두어:

\[
C(S)=|S|
\]

즉 **최소 fan-out**을 우선한다.

---

# 6. Baseline

논문/해커톤 모두 같은 baseline 체계를 유지한다.

## B0 — Single Static

항상 하나의 resolver만 사용.

## B1 — Round Robin

요청마다 resolver를 순환.

## B2 — Best Historical

최근 일정 구간에서 평균 또는 p95가 가장 낮은 resolver 사용.

## B3 — Health-aware Sequential Failover

하나 호출 → timeout/error 발생 시 다음 resolver.

기존 DID routing 서비스와 가장 유사한 baseline.

## B4 — Fixed Hedging

항상 \(k=2\) 또는 일정 delay 이후 두 번째 resolver 추가.

## B5 — All Race

모든 resolver를 동시에 호출하고 first acceptable response 사용.

## Proposed — Adaptive Minimum Set

상태에 따라 \(k\)와 subset을 동적으로 결정.

---

# 7. 핵심 평가 지표

## 성능

- p50 latency
- p95 latency
- p99 latency
- deadline success rate

\[
P(T_{\text{accepted}}\le\tau)
\]

## correctness / validity

- valid response rate
- freshness failure rate
- disagreement rate
- verification success rate

## 비용 / burden

- mean resolver fan-out

\[
E[|S|]
\]

- requests per logical resolution
- canceled hedge requests
- optional cloud/API cost

## prediction

- Brier score
- calibration error
- precision/recall if binary threshold 사용
- probability calibration plot

---

# 8. 실험 단계

# Phase 0 — Local E2E Smoke

## 목표

> 기능적으로 요청이 끝까지 관통하는가?

검증 경로:

```text
client
→ router
→ resolver
→ DID resolution
→ validation
→ accepted response
→ metrics
```

PASS 조건:

- request 성공
- telemetry 저장
- selected resolver set 기록
- actual latency 기록
- failure injection 가능

이 단계에서 AI는 필요 없다.

---

# Phase 1 — Local Multi-Resolver

resolver 3~4개를 로컬에서 구성.

테스트:

- normal
- artificial delay
- CPU limitation
- network delay
- forced timeout
- stale/invalid mocked response

주의:

로컬 resolver들은 동일 host를 공유하므로 **독립 resolver라고 주장하지 않는다.**

---

# Phase 2 — Preliminary Characterization

질문:

> adaptive routing을 만들 가치가 있는가?

측정:

- resolver별 latency
- load별 latency
- tail
- timeout/error
- accepted response rate

### Stop condition A

resolver들이 사실상 동일:

\[
G_1 \simeq G_2 \simeq G_3
\]

이면 adaptive policy 필요성 재검토.

### Stop condition B

fastest와 accepted-fastest 차이가 거의 없음:

DID-aware validation이 실용적 차별성을 만들지 못할 수 있음.

### Continue condition

- latency ranking이 상태에 따라 바뀜
- failure / stale / disagreement가 관찰됨
- fixed policy가 특정 조건에서 약함

---

# Phase 3 — Hackathon MVP

필수 구현:

- multi-resolver routing
- telemetry
- health dashboard
- fixed baselines
- simple predictor
- adaptive \(k\)
- controlled fault injection
- live demo

선택 구현:

- smart contract audit
- model version hash
- request/result commitment
- DID method-specific proof verification

---

# Phase 4 — AWS Qualification

해커톤 본선 진출 후 수행 권장.

목표:

> local-only prototype을 실제 분리된 cloud deployment로 옮겼을 때 E2E가 유지되는가?

초기 구성:

- Router / Client
- Resolver A
- Resolver B
- Resolver C

가능하면 서로 다른 AZ 사용.

주의:

**다른 AZ = 완전한 통계적 독립성**은 아니다.

---

# Phase 5 — AWS Discovery Run

목표:

- 실제 latency heterogeneity 탐색
- failure correlation 탐색
- adaptive fan-out 필요성 확인
- feature 후보 선정
- baseline behavior 확인

이 단계 결과는 **discovery**다.

최종 논문 performance claim에 그대로 사용하지 않는다.

---

# Phase 6 — Policy Freeze

동결 대상:

- feature set
- predictor type
- hyperparameters
- \(\tau\)
- \(\epsilon\)
- acceptance rules
- baselines
- optimizer
- evaluation metrics
- random seeds
- workload generation rule

동결 이후 target outcome을 보고 수정하지 않는다.

---

# Phase 7 — Prospective Confirmatory Run

새로운:

- workload sequence
- time window
- cloud instances
- possible region/provider
- request seeds

에서 별도 실행.

순서:

```text
request context x_t
      ↓
predict
      ↓
select S_t
      ↓
dispatch
      ↓
acceptance check
      ↓
actual outcome
      ↓
evaluation
```

---

# 9. 해커톤 데모 시나리오

## Demo A — Normal

상태:

- Resolver A/B/C 모두 정상

결과 기대:

\[
k^*=1
\]

메시지:

> “정상 상황에서는 불필요한 replication을 만들지 않습니다.”

---

## Demo B — Slow Resolver

상태:

- A에 artificial delay

기존 sequential failover:

```text
A
→ wait
→ timeout
→ B
```

Proposed:

```text
risk ↑
→ {A,B}
→ race
→ first acceptable
```

---

## Demo C — Invalid / stale first response

```text
A → 60 ms, stale
B → 90 ms, valid
C → 120 ms, valid
```

fastest-only:

> A 선택

Proposed:

> A reject → B 반환

메시지:

> “가장 빠른 응답이 아니라 가장 빠른 검증 가능한 응답을 사용합니다.”

---

## Demo D — High uncertainty

상태:

- recent failure 증가
- latency variance 증가

결과:

\[
k^*:1\rightarrow2\rightarrow3
\]

dashboard에서 fan-out 변화 시각화.

---

# 10. 블록체인 적용 범위

블록체인을 억지로 모든 데이터 저장소로 쓰지 않는다.

온체인 저장 후보:

- policy version hash
- resolver registry
- request commitment
- selected resolver-set commitment
- accepted response commitment
- audit timestamp

오프체인:

- raw telemetry
- latency traces
- model features
- logs

핵심 메시지:

> **AI는 routing decision을 만들고, blockchain은 policy/result provenance를 검증 가능하게 남긴다.**

---

# 11. AI가 반드시 필요한지 검증

AI 사용은 사전에 정당화하지 않는다.

## 비교

### Heuristic

- recent-lowest-latency
- EWMA
- threshold-based health score

### ML

- Logistic Regression
- Random Forest
- Gradient Boosting

### Stop-check

ML이 heuristic 대비 실질적 개선이 없다면:

> AI 모델을 억지로 유지하지 않는다.

다만 해커톤 트랙상 AI 요소가 필요하므로, 그 경우에는 AI를 **probability calibration / anomaly-risk prediction** 역할로 제한한다.

---

# 12. 일정 계획

공식 안내문 기준:

- 참가 신청 마감: **2026-09-14 18:00**
- 본선 10팀 발표: **2026-09-16**
- 사전 OT: **2026-09-19**
- 사전 멘토링: **7주**
- 본선: **2026-11-06 ~ 2026-11-07**

## 9/06

- 아이디어 고정
- novelty boundary 정리
- system architecture 초안
- project repo 생성

## 9/07

- single resolver E2E
- router skeleton
- metrics schema

## 9/08

- 3-resolver local setup
- round robin
- single static
- sequential failover

## 9/09

- delay / failure injection
- first characterization
- dashboard 초안

## 9/10

- predictor baseline
- adaptive subset selector prototype

## 9/11

- demo scenario A/B/C 구현
- preliminary plots

## 9/12

- 10-page proposal 초안
- system diagram
- user scenario
- technical differentiation

## 9/13

- GitHub 정리
- 2~3분 demo video optional 제작
- 제안서 내부 검증

## 9/14

- 최종 제출
- 18:00 이전 제출 완료

## 9/16 이후 — 본선 진출 시

### Week 1

- mentor feedback
- DID method 선정
- acceptance rule 확정

### Week 2

- AWS deployment
- cloud smoke

### Week 3

- discovery characterization

### Week 4

- predictor + optimizer 개선

### Week 5

- dashboard / smart contract audit

### Week 6

- freeze candidate
- confirmatory dry-run

### Week 7

- final cloud run
- demo/pitch stabilization

---

# 13. 10-page 제안서 권장 구성

## P1 — Problem

**DID resolution의 단일/정적 resolver 의존 문제**

## P2 — Existing Solutions & Gap

기존:

- sequential failover
- health-aware routing
- fixed hedging

Gap:

> latency만이 아니라 DID result acceptability까지 고려하면서 필요한 redundancy를 동적으로 결정하는 문제.

## P3 — Core Idea

```text
Predict
→ Select minimum set
→ Race
→ Verify
→ Return first acceptable
```

## P4 — Architecture

전체 시스템 diagram.

## P5 — AI

\[
\hat p_i =
P(T_i\le\tau \land A_i=1\mid x)
\]

## P6 — Blockchain / DID

- DID method
- validation
- audit provenance
- smart contract 역할

## P7 — Optimization

\[
S^*
=
\arg\min_S |S|
\]

subject to

\[
P(T_{\text{accepted},S}\le\tau)
\ge1-\epsilon
\]

## P8 — Prototype / Demo

- local architecture
- dashboard
- fault injection
- GitHub / demo evidence

## P9 — Evaluation

baseline 및 metrics.

## P10 — Development Plan / Team

- 7주 개발 계획
- 역할 분담
- 최종 목표

---

# 14. 논문 확장 경로

해커톤 결과를 그대로 논문 결과로 사용하지 않는다.

구조:

```text
Hackathon
   ↓
engineering + discovery
   ↓
research question selection
   ↓
freeze
   ↓
new independent experiment
   ↓
paper
```

---

# 15. 논문 후보 제목

## 국내 학술대회형

**DID 다중 Resolver 환경에서 적응형 요청 중복을 이용한 지연시간 및 신뢰성 최적화**

또는

**DID Resolution을 위한 상태 기반 적응형 Resolver 선택 기법**

## 후속 저널 후보형

**Adaptive Verifiable DID Resolution under Heterogeneous Resolver Performance**

또는

**Prospective Resolver-Set Selection for Deadline-Constrained Verifiable DID Resolution**

---

# 16. 논문용 연구 질문

## Paper RQ1

실제 resolver 간 latency / reliability heterogeneity는 얼마나 큰가?

## Paper RQ2

fastest response와 fastest accepted response 사이에 유의미한 차이가 존재하는가?

## Paper RQ3

resolver outcome correlation 때문에 independence-based redundancy sizing이 틀리는가?

## Paper RQ4

adaptive resolver-set selection이 fixed hedging / all-race 대비 동일 SLO에서 request burden을 줄이는가?

## Paper RQ5

discovery 환경에서 동결한 정책이 unseen environment에서도 유지되는가?

---

# 17. 논문으로 확장할 가치가 생기는 조건

다음 중 최소 하나 이상의 강한 empirical finding이 필요하다.

### Finding A

\[
\text{fastest} \neq \text{fastest acceptable}
\]

가 반복적으로 관찰됨.

### Finding B

resolver success/failure가 유의미하게 correlated.

### Finding C

optimal \(k\)가 workload/state에 따라 실제로 바뀜.

### Finding D

adaptive policy가 all-race와 비슷한 SLO를 유지하면서 fan-out을 크게 절감.

### Finding E

기존 health-aware failover보다 tail latency / reliability / burden trade-off에서 의미 있는 개선.

---

# 18. 논문 확장을 중단해야 하는 조건

다음이면 해커톤 프로젝트로만 종료하는 것이 합리적이다.

- resolver 간 차이가 거의 없음
- fastest와 accepted-fastest가 거의 동일
- fixed \(k=2\)가 adaptive보다 거의 항상 충분
- simple heuristic이 ML/adaptive policy와 동일
- 실제 provider에서도 controlled injection 외에는 문제가 재현되지 않음
- novelty가 기존 router 서비스와 구분되지 않음

이 경우 억지로 SCIE를 노리지 않는다.

---

# 19. Cloud 실험 설계

## 해커톤

AWS multi-AZ로 충분.

목적:

- actual network
- actual VM separation
- cloud E2E
- realistic latency variability

## 논문

가능하면 더 강한 diversity를 사용.

예:

```text
AWS resolver
GCP resolver
Public provider resolver
Self-hosted resolver
```

목표는 **provider diversity**와 **backend diversity** 확보.

주의:

multi-cloud라고 해서 자동으로 독립성이 보장되는 것은 아니다.

---

# 20. 비용 원칙

AWS credit은 qualification / discovery / confirmatory를 분리해서 사용한다.

실험 전:

1. 예상 instance-hours 계산
2. storage
3. public IP
4. inter-AZ traffic
5. external RPC/API cost

확인.

실험 후 즉시:

- instance stop/delete
- unused volume 확인
- public IP 해제
- cost report 기록

---

# 21. 데이터 스키마

각 logical DID request에 대해 최소 기록:

```text
request_id
timestamp
did_method
request_type
context_features
selected_resolver_set
policy_version
prediction_per_resolver
actual_latency_per_resolver
accepted_flag_per_resolver
verification_result
returned_resolver
logical_completion_latency
fanout_count
canceled_count
error_code
```

---

# 22. Reproducibility

해커톤 단계부터 다음을 유지한다.

- Docker Compose
- pinned dependency versions
- fixed seeds
- experiment config YAML
- raw CSV/Parquet
- exact command logs
- policy/model version hash
- Git commit hash

논문 확장 시 그대로 재현성 패키지 기반으로 사용 가능.

---

# 23. IP / 연구윤리

## 사용할 수 있는 것

- 공개 DID standards
- 공개 resolver implementations
- 본인이 새로 작성한 code
- 본인이 새로 수집한 experiment data
- 기존 Cluster Computing 논문의 일반적 methodology

## 사용 전 확인 필요한 것

- 교수님과 진행 중인 국가과제의 비공개 code
- 비공개 gateway architecture
- 내부 dataset
- 미공개 algorithm
- 기관 소유 IP

## 해커톤 본선 진출 시 확인

윤리·저작권 서약서 수령 직후:

> “대회에서 생성한 코드·실험데이터·결과를 참가자가 후속 학술논문에 사용하는 데 제한이 있는가?”

서면 확인.

---

# 24. 실패 방지 Stop-Check

새 실험을 돌리기 전에 항상 아래를 답한다.

### Q1

이 실험이 답하는 질문은 무엇인가?

### Q2

결과가 A/B 어느 쪽으로 나오든 다음 행동이 정해지는가?

### Q3

더 싼 local experiment로 같은 답을 얻을 수 있는가?

### Q4

다른 환경에서 측정한 파라미터를 재사용하고 있지 않은가?

### Q5

target outcome을 보고 threshold/model을 수정하고 있지 않은가?

---

# 25. 숫자/결과 표기 규칙

프로젝트 문서에서 항상 구분한다.

- **[측정]** 실제 출력에서 확인
- **[계산]** 측정값에서 계산
- **[해석]** 이론/분석
- **[가정]** 검증되지 않은 전제
- **[추측]** 근거 약한 예상

예:

> [측정] Resolver B의 p95 latency는 184 ms였다.

> [계산] Round Robin 대비 p95가 23.4% 감소했다.

> [가정] 서로 다른 AWS AZ의 resolver failure는 독립적이다.

마지막 문장은 검증 전에는 절대로 사실처럼 쓰지 않는다.

---

# 26. 현재 최종 프로젝트 범위

## 해커톤 MVP

반드시 구현:

- DID request routing
- multiple resolvers
- latency/health telemetry
- fixed baselines
- adaptive resolver-set
- acceptable-response validation
- dashboard
- demo scenario

가능하면 구현:

- simple ML predictor
- on-chain audit
- cloud deployment

## 해커톤에서 하지 않을 것

- BFT consensus 자체 구현
- 3f+1 강제 적용
- 완전한 Byzantine fault proof
- token economy
- NFT
- 불필요한 LLM
- 거대한 multi-chain platform
- SCIE급 claim

---

# 27. 프로젝트의 핵심 차별화 문장

> **기존 DID router가 resolver 상태 기반 routing/failover에 초점을 둔다면, 본 시스템은 DID 응답의 acceptability와 deadline을 동시에 고려하여 현재 상태에서 필요한 최소 resolver 집합을 동적으로 선택한다.**

---

# 28. 발표용 20초 설명

> “DID 서비스는 여러 resolver를 사용할 수 있지만, 항상 하나만 쓰면 장애와 지연에 취약하고 항상 여러 개를 호출하면 비용이 증가합니다. 저희 시스템은 AI가 각 resolver의 상태를 예측해 현재 요청에 필요한 최소 resolver 집합만 선택하고, 가장 빠른 응답이 아니라 가장 빠른 검증 가능한 DID 응답을 반환합니다.”

---

# 29. 최종 성공 기준

## 해커톤 성공

- 실제 E2E 데모
- adaptive \(k\)가 화면에서 동작
- fault injection 시 routing 변화
- valid/invalid distinction
- baseline 비교
- clear user value

## 연구 성공

다음 중 적어도 하나:

- 실제 DID resolver heterogeneity 발견
- fastest/acceptable mismatch 발견
- correlated failure 발견
- adaptive \(k\)의 명확한 burden–SLO 이득
- prospective unseen run에서도 개선 유지

---

# 30. 현재 권장 실행 순서

```text
[1] local single resolver
        ↓
[2] local multi-resolver
        ↓
[3] baselines
        ↓
[4] fault injection
        ↓
[5] preliminary characterization
        ↓
[6] adaptive subset selector
        ↓
[7] hackathon proposal
        ↓
[8] selected? → AWS qualification
        ↓
[9] discovery
        ↓
[10] freeze
        ↓
[11] confirmatory
        ↓
[12] paper decision
```

---

# 31. 현재 판단

## 해커톤 적합성

**높음.**

단, 발표의 중심은 연구가 아니라:

> **실제 사용 가능한 DID reliability / latency optimization service**

여야 한다.

## 국내 학술대회 확장성

**충분히 있음.**

특히 empirical evaluation과 adaptive policy 비교가 실제 결과로 나오면 ACK/KIPS 계열 2~3페이지 논문으로 적절하다.

## SCIE 확장성

**현재 상태에서는 미확정.**

다음과 같은 실제 empirical novelty가 발견되어야 한다.

- DID-specific correctness problem
- resolver correlation
- state-dependent optimal redundancy
- strong prospective improvement
- multi-provider generalization

그런 발견이 없다면 해커톤 + 국내학회 수준에서 종료하는 것이 합리적이다.

---

# 32. 다음 즉시 해야 할 작업

1. Git repository 생성
2. single resolver E2E
3. router skeleton
4. telemetry schema
5. 3-resolver local deployment
6. sequential failover baseline
7. fault injection
8. preliminary characterization
9. proposal draft
10. demo video optional 준비
