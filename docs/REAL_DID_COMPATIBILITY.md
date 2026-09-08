# Real DID compatibility qualification

- Qualification time: 2026-09-08T06:29:00Z
- Provider inventory: `2026-09-07-a`
- Provider: `uniresolver-dif-dev` (`https://dev.uniresolver.io`)
- Adapter: `universal-resolver-v1`
- Policy: `single-static`
Acceptance profile: `w3c-basic-v1`

This is a small, opt-in interoperability qualification. It is not a benchmark,
research experiment, provider comparison, availability study, or Adaptive
policy evaluation. Each latency below is one observed request trace only.

## Measured qualification results

The script sent exactly one request per DID and performed no retries. All
three requests traversed AVDR's existing candidate selection, real provider
executor, Universal Resolver adapter, structural acceptance profile, normalized
service result, local audit recorder, receipt lookup, and disclosure
verification endpoints.

| DID method | Public test DID | External provider HTTP | AVDR resolution | W3C basic acceptance | Receipt | Single observed request latency | Scope |
| --- | --- | --- | --- | --- | --- | ---: | --- |
| `did:key` | `did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK` | `uniresolver-dif-dev`, HTTP 200 | PASS | PASS | VERIFIED | 1026.351 ms | compatibility only |
| `did:web` | `did:web:danubetech.com` | `uniresolver-dif-dev`, HTTP 200 | PASS | PASS | VERIFIED | 512.313 ms | compatibility only |
| `did:ethr` | `did:ethr:0xb9c5714089478a327f09197987f16f9e5d936e8a` | `uniresolver-dif-dev`, HTTP 200 | PASS | PASS | VERIFIED | 555.455 ms | compatibility only |

Every normalized result contained object-valued `didResolutionMetadata`,
`didDocument`, and `didDocumentMetadata`, and every `didDocument.id` exactly
matched the requested DID. The measurements passed the sanity checks
`latency >= 0`, `calls_used >= 1`, and `selected_count <= candidate_count`.

### Qualification receipt evidence

These hashes are ephemeral local values from the three-request scripted run.
They are not blockchain transaction identifiers.

| DID method | Receipt hash | Evidence mode | Result commitment disclosure | Anchor |
| --- | --- | --- | --- | --- |
| `did:key` | `sha256:6d96c319794f347fd54c3302775b4be0c49f4164954224721cc25398ff69c7cd` | `real` | PASS | `not_configured` |
| `did:web` | `sha256:395dde3ffb84fbdf1c935db81a2b5504d2fc8c9bbe3f85cc38c46f7924b6894e` | `real` | PASS | `not_configured` |
| `did:ethr` | `sha256:d271fdaeb57f0c28f86d317961512b1d1672c7467e91c0985b5c01d6eb82e2bb` | `real` | PASS | `not_configured` |

For each case, the stored receipt's resolver set and returned provider matched
the service response, its policy identity matched the running policy, the
disclosed DID and normalized result commitments verified, and the local hash
chain link was intact. This verifies local receipt integrity; it does not
establish DID truth, resolver trust, blockchain finality, or cryptographic
correctness of the resolved document.

## Public DID provenance

- `did:key` is the public Ed25519 example expanded in the
  [did:key method specification](https://w3c-ccg.github.io/did-key-spec/).
  Its resolution is deterministic from the identifier and does not depend on
  a ledger or document host.
- `did:web:danubetech.com` is a public third-party DID recorded in
  `config/fixtures.yaml`. It depends on Danube Tech's DNS, TLS, and hosted DID
  document and may change or disappear.
- The `did:ethr` value is the public no-event default-document example from the
  [ethr DID resolver project](https://github.com/decentralized-identity/ethr-did-resolver).
  No identity or transaction was created for this qualification. Resolution is
  RPC-backed by implementation design, but this run did not trace whether the
  resolver used a fresh RPC lookup or a cache.

None of these identifiers belongs to the AVDR presenter or was created for
this run.

## Endpoint audit

Before identifier resolution, each configured external hostname received one
DNS lookup and one HTTPS `HEAD` request. All four hostnames resolved and
returned HTTP 200 at their root on 2026-09-08. A root response does not prove
method compatibility.

| Configured endpoint | DNS / root HTTP | Qualification decision |
| --- | --- | --- |
| `dev.uniresolver.io` | `109.70.102.245`, HTTP 200 | Configured available; qualified for key/web/ethr |
| `resolver.identity.foundation` | Cloudflare addresses, HTTP 200 | Inventory remains unavailable after earlier identifier-path 502 observations; not silently substituted or requalified |
| `api.godiddy.com` | `109.70.102.245`, HTTP 200 | Authentication required and no credentials available; not used |
| `uniresolver.io` | `109.70.102.245`, HTTP 200 | Same observed address as the dev endpoint; excluded as non-distinct and not used |

The active provider returned HTTP 200 DID Resolution Results for all three
method paths. No throttling response was observed, and no request was retried.

## Presenter screenshots

The dashboard screenshots are separate one-request-per-method UI observations.
They visibly show real evidence mode, the requested DID, accepted result,
returned provider, structural acceptance, receipt hash, local verification,
and the unconfigured blockchain anchor.

- [`real_did_key.png`](real_compatibility_screenshots/real_did_key.png) — UI receipt `sha256:709d122af2e53f3b5ab1d4e9c527e696998b70510dc3160d4ea8af19a1633969`
- [`real_did_web.png`](real_compatibility_screenshots/real_did_web.png) — UI receipt `sha256:7c146f0b342d3747dae5f307fedb0ada336c9e8dcbef77150bea357b27733c49`
- [`real_did_ethr.png`](real_compatibility_screenshots/real_did_ethr.png) — UI receipt `sha256:7986a17e213b2487b9cd66295997bbc5ec28a7c63b99527c2b7d75f3cf857bc8`

The scripted qualification used three public DID resolution requests. The UI
capture used three more. Endpoint discovery used four root `HEAD` requests.
Total public HTTP requests for P9 were therefore 10, of which six were DID
resolution requests. This is a bounded compatibility check, not load traffic.

## Reproduce deliberately

Public traffic is disabled by default. The explicit flag permits exactly the
three fixed method requests; the script stops after any rate-limit response.

```powershell
.\.venv\Scripts\python.exe scripts\qualify_real_dids.py `
  --execute-live --timeout-seconds 30 --out <temporary-report.json>
```

Default `pytest` does not execute this command and retains its public-network
block. Do not repeatedly run the live command merely to warm Adaptive or
collect latency samples.

## What this qualification demonstrates

> These checks demonstrate interoperability between AVDR and the tested external DID resolution paths.

In the six real UI/script observations made here, the existing adapter and
acceptance path handled the tested `did:key`, `did:web`, and `did:ethr` results,
and AVDR produced verifiable local receipts in real evidence mode.

## What this qualification does not demonstrate

> These observations do not establish global performance, availability, reliability, or generalization across DID infrastructure.

They do not establish provider independence, production readiness, global
method coverage, canonical blockchain state, freshness, finality,
cryptographic correctness, resolver trust, or an Adaptive performance gain.
Caching, geography, provider load, CDN behavior, and network conditions may
all affect the individual latency traces above.
