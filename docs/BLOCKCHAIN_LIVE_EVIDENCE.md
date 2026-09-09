# Existing Ethereum Sepolia anchor evidence

This document packages read-only evidence for the one already-broadcast AVDR
anchor transaction. It does not authorize or require another transaction.

## Evidence record

| Field | Value |
| --- | --- |
| Network | Ethereum Sepolia |
| Chain ID | `11155111` |
| Transaction | `0xfd32a66a9227d5f724f2eb85b91457ed9e7e3437e30551edacc42c82e189b583` |
| Receipt status | `1` — mined successfully |
| Block number | `11664768` |
| Block hash | `0xdc69ca43f06fe03bb862a4ecda90fba0c3c917b769e7b09a758c3b6a4b674338` |
| Transaction type | `2` — EIP-1559 |
| Sender/destination | Same dedicated Sepolia test address |
| Transaction value | `0 wei` |
| Evidence mode | `controlled_demo` |
| Anchor protocol | `avdr-anchor-v1` |
| Local receipt hash | `sha256:8cfa52927e8f15fa289d7c3dbe1e504966a77a5d3421413a2c32dd56dede5959` |
| On-chain receipt hash | `sha256:8cfa52927e8f15fa289d7c3dbe1e504966a77a5d3421413a2c32dd56dede5959` |
| Receipt-hash match | **YES** |
| Finality | **Mined only; finality not evaluated** |

[Open the existing transaction in Sepolia Etherscan](https://sepolia.etherscan.io/tx/0xfd32a66a9227d5f724f2eb85b91457ed9e7e3437e30551edacc42c82e189b583).

## Independent readback

The frozen evidence was reconciled before this package and read again during
packaging using only:

1. `eth_chainId`;
2. `eth_getTransactionByHash`;
3. `eth_getTransactionReceipt`.

No signing, nonce lookup for submission, transaction construction, or
`eth_sendRawTransaction` occurred. The transaction and receipt hashes and
block hashes agreed. The RPC-reported chain ID was `11155111`, value was zero,
and receipt status was `1`.

The transaction input decoded as canonical JSON with exactly these fields:

```text
policy_identity_hash
protocol
receipt_hash
receipt_schema
receipt_timestamp
```

Its protocol was `avdr-anchor-v1`, its receipt schema was `avdr-audit-v1`, and
its receipt hash matched the known local receipt hash exactly.

## Actual calldata privacy evidence

These checks were performed against the input returned by
`eth_getTransactionByHash`, not against a mocked or pre-signing value.

| Check | Result |
| --- | --- |
| `RAW_DID_ONCHAIN` | **NO** |
| `RAW_DID_DOCUMENT_ONCHAIN` | **NO** |
| `RAW_RESULT_ONCHAIN` | **NO** |
| `ATTEMPT_TELEMETRY_ONCHAIN` | **NO** |

Only privacy-minimized receipt provenance metadata is anchored. This is data
minimization, not anonymity: a public commitment may still be correlated with
information available elsewhere.

## Trust and claim boundary

> Ethereum stores/verifies the AVDR receipt commitment. It does not establish
> the truth, correctness, freshness, or canonical status of the DID document.

The evidence supports the statement that AVDR anchored a receipt commitment to
Ethereum Sepolia and independently read it back from the public chain. It does
not support “Ethereum verified the DID,” “blockchain verified the resolver,” or
any finality claim.
