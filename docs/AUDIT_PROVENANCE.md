# AVDR Audit Receipt and Provenance Boundary

AVDR produces a versioned, privacy-minimized receipt after resolver execution
and normalization. A valid receipt supports only this claim:

> A verifier can check that the recorded AVDR request metadata, routing
> decision, policy identity, and returned-result commitment match the
> committed audit record.

It does not establish DID truth, resolver trust, consensus, finality,
cryptographic correctness, or blockchain validation of W3C correctness. The
`w3c-basic-v1` profile remains structural acceptance only.

## Receipt format

The schema is `avdr-audit-v1`. Canonical bytes are UTF-8 JSON with sorted
object keys, fixed `,` and `:` separators, explicit nulls, UTF-8 text, and no
NaN or Infinity. Arrays retain order. `selected_resolver_ids` is a
lexicographically sorted semantic set; `launch_order` separately preserves
the policy's meaningful execution order.

Hashes are SHA-256 over a fixed ASCII domain separator, one NUL boundary byte,
and the canonical payload bytes:

- `AVDR:AUDIT:RECEIPT:v1` for the complete receipt;
- `AVDR:DID:v1` for the requested DID commitment;
- `AVDR:RESULT:v1` for the exact normalized service result;
- `AVDR:POLICY:v1` and `AVDR:ESTIMATOR:v1` for implementation identities.

The DID commitment is computed from canonical JSON containing the exact UTF-8
request DID and a public, randomly generated 128-bit request nonce. The raw DID
is not stored in the receipt. The nonce prevents reusable unsalted lookup
tables, but it does not hide a DID from someone who already has a likely DID
candidate and the receipt. Disclosure verification is therefore integrity
checking, not encryption or anonymity.

The result commitment covers the exact normalized `result` object returned by
AVDR for that request. It does not assert semantic equivalence with other JSON
representations or identify a globally canonical DID document. Executed
failures are supported: `returned_provider` is null and the canonical failure
result contains null normalized result fields.

## Local recorder and hash chain

The default presenter service uses `LocalAuditRecorder`. It retains canonical
receipt bytes only in process memory and links each entry through
`previous_receipt_hash`. Genesis is explicit JSON null. This is a local
append-only audit chain, not a blockchain. Receipts disappear on process
restart and are not durable across workers.

`NullAnchor` is the active anchor implementation. It reports
`status: not_configured`; no external chain request occurs. The future
`AuditAnchor` interface receives only:

```json
{
  "receipt_hash": "sha256:...",
  "schema_version": "avdr-audit-v1",
  "timestamp": "...",
  "policy_identity_hash": "sha256:..."
}
```

Raw DIDs, DID documents, resolver bodies, attempt telemetry, and IP addresses
are outside that boundary.

## Verification API

- `GET /audit/receipts/{receipt_id}` retrieves a safe receipt and its local
  anchor status.
- `POST /audit/verify` with `{"receipt_id":"..."}` recomputes stored receipt
  and hash-chain integrity.
- `POST /audit/verify` may instead accept `receipt` plus `receipt_hash` for a
  `standalone_payload` integrity check. This proves internal hash consistency,
  not that the payload is in the local recorded chain. Optional
  `disclosed_did` and `disclosed_result` fields explicitly check those
  commitments.

Audit recording is provenance, not resolution. If recording fails, the
resolution result remains available and `audit.status` reports
`recording_failed`. An anchor failure retains the locally verifiable receipt
and reports `recorded_anchor_failed`; it never reports an on-chain transaction.
