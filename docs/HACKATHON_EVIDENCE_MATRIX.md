# AVDR hackathon evidence and claim matrix

Frozen presenter baseline:

- commit `21796f9db1995a2ba37fc99b287e3ce5a382f418`;
- tag `hackathon-sepolia-live-v1`;
- existing Sepolia transaction
  `0xfd32a66a9227d5f724f2eb85b91457ed9e7e3437e30551edacc42c82e189b583`.

Labels mean: **[MEASURED]** directly observed, **[CALCULATED]** derived from
measured values, and **[INTERPRETATION]** a bounded explanation of evidence.
Claims from different sections must not be merged.

## A. Product capability

| Class | Claim | Evidence | Presenter-safe wording |
| --- | --- | --- | --- |
| [MEASURED] | An adaptive DID resolution gateway exists. | Service API, dashboard, policies, integration tests. | “AVDR routes DID resolution through qualified resolver candidates.” |
| [MEASURED] | A minimum-set adaptive policy exists. | Frozen `adaptive-min-set` runtime and exact-selection tests. | “The policy selects the smallest estimated subset meeting its target, or reports bounded best effort.” |
| [MEASURED] | First-acceptable-result execution exists. | Executor traces and acceptance-aware tests. | “AVDR returns the first response that passes `w3c-basic-v1`, not merely the first response.” |
| [MEASURED] | Versioned audit receipts exist. | `avdr-audit-v1`, local verification API, dashboard receipt panel. | “AVDR commits request, policy, selection, and returned-result provenance.” |

These are implementation capabilities, not claims of universal correctness or
production performance.

## B. Controlled adaptive evidence

| Class | Claim | Frozen evidence | Boundary |
| --- | --- | --- | --- |
| [MEASURED] | Normal scenario selects a small set. | `scenario_normal.png`: `k = 1`, one call, two saved. | Synthetic loopback providers only. |
| [MEASURED] | Slow/failure scenario increases redundancy and falls back. | `scenario_slow_failure.png`: `k = 2`, failed first path, accepted second path. | Degradation is intentionally injected. |
| [MEASURED] | Fast malformed/unacceptable data does not win. | `scenario_fast_unacceptable.png`: faster path rejected, later acceptable path returned. | Acceptance is structural under `w3c-basic-v1`. |
| [MEASURED] | Adaptive success is `0.867045`; fixed `{local-b, local-c}` success is `0.834470`. | Frozen ACK evidence package, 2,640 paired controlled trials. | **CONTROLLED EVALUATION — NOT REAL-WORLD PERFORMANCE.** |
| [CALCULATED] | Success difference is `+0.032576`. | Adaptive minus frozen fixed comparator. | No production generalization claim. |
| [MEASURED] | Adaptive calls/request is `1.905303`; fixed comparator is `2.000000`. | Frozen ACK evidence package. | Controlled workload only. |
| [CALCULATED] | Calls/request difference is `-0.094697`. | Adaptive minus frozen fixed comparator. | Not a public-provider cost forecast. |

## C. Real compatibility evidence

| Class | Tested path | Observation | Boundary |
| --- | --- | --- | --- |
| [MEASURED] | `did:key` | External Universal Resolver path returned HTTP 200, passed structural acceptance, and produced a verified local receipt with `evidence.mode=real`. | One request trace in the scripted qualification. |
| [MEASURED] | `did:web` | External Universal Resolver path returned HTTP 200, passed structural acceptance, and produced a verified local receipt with `evidence.mode=real`. | One request trace in the scripted qualification. |
| [MEASURED] | `did:ethr` | External Universal Resolver path returned HTTP 200, passed structural acceptance, and produced a verified local receipt with `evidence.mode=real`. | One request trace in the scripted qualification. |

All three observations used one qualified `dev.uniresolver.io` deployment.
They demonstrate tested-path interoperability, not provider independence,
global availability, or adaptive performance in production.

## D. Audit and provenance evidence

| Class | Claim | Evidence | Boundary |
| --- | --- | --- | --- |
| [MEASURED] | A receipt binds routing and result provenance. | Canonical `avdr-audit-v1` receipt and SHA-256 commitment. | It records AVDR execution; it does not prove DID truth. |
| [MEASURED] | Local receipt verification succeeds in controlled and qualified real traces. | `/audit/verify` results and screenshots. | Default storage is process memory and is not durable across restart/workers. |
| [MEASURED] | Raw DID is excluded from the receipt and replaced by a nonce-salted commitment. | Receipt schema and privacy tests. | Commitment privacy is not anonymity or encryption. |

## E. Public Sepolia anchor evidence

| Class | Claim | Evidence | Boundary |
| --- | --- | --- | --- |
| [MEASURED] | The existing Sepolia transaction was mined successfully. | Receipt status `1`, block `11664768`. | Mined does not mean finalized. |
| [MEASURED] | Transaction value is `0 wei`. | RPC transaction readback. | Gas was still paid in Sepolia test ETH. |
| [MEASURED] | Readback decoded `avdr-anchor-v1`. | `eth_getTransactionByHash` input. | No smart-contract assertion or DID validation. |
| [MEASURED] | On-chain receipt hash equals the known local receipt hash. | Both equal `sha256:8cfa52927e8f15fa289d7c3dbe1e504966a77a5d3421413a2c32dd56dede5959`. | Proves matching commitment data was included in the referenced transaction. |
| [MEASURED] | Raw DID, DID document, normalized result, and attempt telemetry are absent from actual calldata. | RPC-read 303-byte canonical payload with exactly five fields. | Data minimization is not a claim of anonymity. |

See [`BLOCKCHAIN_LIVE_EVIDENCE.md`](BLOCKCHAIN_LIVE_EVIDENCE.md) for the exact
transaction evidence.

## F. Explicit limitations

| Class | Limitation |
| --- | --- |
| [INTERPRETATION] | Controlled performance evidence does not establish real-world generalization. |
| [MEASURED] | Real compatibility qualified one Universal Resolver deployment; it did not establish provider independence. |
| [INTERPRETATION] | Ethereum commits the receipt metadata but does not verify DID truth, resolver correctness, freshness, or canonical DID state. |
| [MEASURED] | The recorded chain state is mined; finality was not evaluated. |
| [MEASURED] | The default local receipt recorder is process-memory-only. |
| [MEASURED] | `w3c-basic-v1` performs structural acceptance, not cryptographic DID-document verification. |
