# AVDR judge Q&A

## What problem does AVDR solve?

DID resolution can involve providers with different method coverage, latency,
availability, and output quality. AVDR selects a qualified subset, accepts only
structurally valid results, and records what it selected and returned.

## Why not query all resolvers every time?

All-race can improve redundancy, but it consumes more provider calls. AVDR
estimates whether a smaller qualified subset can satisfy the configured target
and selects the minimum qualifying set. When estimates cannot meet the target,
the frozen policy reports bounded best effort rather than inventing certainty.

## How is this different from simple hedged requests?

No. AVDR combines DID-method capability filtering, partial-feedback subset
estimation, minimum-set selection, acceptance-aware winner selection, and a
verifiable record of what was selected and returned. Concurrent execution can
be one mechanism inside that pipeline, but it is not the whole system.

## What happens if the fastest resolver returns malformed data?

Speed alone does not win. The response must pass the configured structural
acceptance profile. The controlled fast-but-unacceptable scenario shows a
faster response being rejected and a later acceptable result being returned.

## Why blockchain?

Local receipt integrity does not require blockchain. Sepolia provides an
external timestamped commitment outside the AVDR process and operator-local
memory. It is an optional provenance layer.

## Why not put the DID or DID document on-chain?

Privacy and data minimization. The transaction carries only the versioned
receipt commitment and minimal interpretation metadata. The actual chain-read
calldata contains no raw DID, DID document, normalized result, or attempt
telemetry.

## Does Ethereum prove the DID is correct?

No. Ethereum establishes inclusion of matching commitment data in the cited
transaction. It does not establish DID truth, document correctness, resolver
trust, freshness, consensus, or canonical DID state.

## Is the transaction finalized?

This evidence package claims only **mined**. Receipt status was `1`, but
finality was not separately evaluated.

## Was adaptive performance proven in production?

No. The quantitative adaptive comparison is a controlled evaluation. The real
qualification demonstrates interoperability on three tested DID method paths;
it is not a production performance study.

## How was real-world compatibility tested?

The frozen qualification made one bounded public request for each of `did:key`,
`did:web`, and `did:ethr` through `dev.uniresolver.io`. Each returned HTTP 200,
passed `w3c-basic-v1`, and produced a verified local receipt with
`evidence.mode=real`.

## How are user data and credentials protected?

DID resolution does not require a user's private key. AVDR keeps provider
credentials and the optional blockchain signing key outside source control, and
anchors only a privacy-minimized receipt commitment—not the raw DID, DID
document, result, or attempt telemetry. Selected resolvers still see the DID
being requested, and this MVP does not provide network-level anonymity.

## Why Universal Resolver?

It provides an actual external DID Resolution path across multiple DID methods
using AVDR's existing adapter. The current evidence qualified one deployment,
so it does not establish provider independence.

## What exactly did the real qualification cover?

One bounded request each for public `did:key`, `did:web`, and `did:ethr`
fixtures through `dev.uniresolver.io`. Each returned HTTP 200, passed
`w3c-basic-v1`, and produced a locally verified AVDR receipt labeled
`evidence.mode=real`.

## What does `w3c-basic-v1` prove?

It checks the expected DID Resolution result structure and requested subject
relationship. It does not perform method-specific cryptographic verification
or declare the returned document globally canonical.

## What is verifiable in an AVDR audit receipt?

A verifier can recompute the canonical receipt hash, local hash-chain link,
nonce-salted DID commitment when the DID is disclosed, and normalized-result
commitment when the result is disclosed. This verifies AVDR provenance and
integrity, not the external truth of the resolution.

## Is receipt storage durable?

Not in the current presenter service. `LocalAuditRecorder` is process-memory
storage and does not survive restart or coordinate across workers. The public
Sepolia commitment remains independently visible, but it does not reconstruct
the full local receipt by itself.

## Are the controlled and real observations combined?

No. Controlled demo history is isolated and labeled `controlled_demo`. Real
compatibility responses are labeled `real`. Blockchain anchoring of a
controlled receipt does not upgrade it into real-world evidence.

## What is the business/customer target?

The initial target is teams building wallets, identity gateways, and
credential-verification services that need resilient DID resolution plus an
auditable routing record. The MVP demonstrates the technical workflow; it does
not establish commercial demand or production readiness.

## What are the current limitations?

Performance evidence is controlled, real compatibility covers one Universal
Resolver deployment and three tested paths, acceptance is structural rather
than method-specific cryptographic verification, local receipts are
process-memory-only, and the Sepolia evidence is mined rather than separately
finality-qualified. The MVP also provides no provider independence or
network-level anonymity.
