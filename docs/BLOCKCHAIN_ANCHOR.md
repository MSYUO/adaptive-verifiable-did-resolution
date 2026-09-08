# Ethereum Sepolia receipt anchoring

AVDR can optionally place one privacy-minimized audit receipt commitment in
the calldata of a zero-value Ethereum Sepolia transaction. The blockchain
commits to the AVDR receipt hash; it does not validate the DID document itself.

The default remains `NullAnchor`. A clean checkout performs no blockchain
request, needs no wallet, and reports `anchor.status: not_configured`.

## What is anchored

The `avdr-audit-v1` receipt is constructed and verified locally first. Its
existing `receipt_hash` and minimal interpretation metadata are then encoded
as UTF-8 canonical JSON with sorted keys, fixed separators, and rejected
NaN/Infinity values:

```json
{
  "policy_identity_hash": "sha256:...",
  "protocol": "avdr-anchor-v1",
  "receipt_hash": "sha256:...",
  "receipt_schema": "avdr-audit-v1",
  "receipt_timestamp": "..."
}
```

The exact five-field set is enforced during both encoding and readback. The
calldata does not contain the raw DID, disclosure nonce, DID document,
normalized result, resolver response, attempt telemetry, or IP address. The
entire receipt is never written to the chain.

## Transaction safety contract

- Network: Ethereum Sepolia only, chain ID `11155111`.
- Transaction: EIP-1559 dynamic-fee transaction.
- Sender and destination: the same dedicated Sepolia test wallet.
- Value: exactly `0` wei.
- Default maximum gas: `100000`.
- Default maximum fee per gas: `100000000000` wei (100 gwei).
- No smart contract is deployed or called.

Before signing, the adapter reads `eth_chainId`, balance, pending nonce,
latest base fee, priority fee, and a gas estimate. A wrong chain, non-self
destination, unreasonable gas/fee, or insufficient Sepolia test ETH fails
closed before signing and broadcasting.

Only Sepolia faucet/test ETH may be used for gas. Do not purchase ETH, bridge
assets, or configure a production wallet for this feature.

## Configuration

Install the pinned dependencies and supply secrets only through the process
environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:AVDR_AUDIT_ANCHOR = "sepolia"
$env:AVDR_SEPOLIA_RPC_URL = "https://your-sepolia-rpc.example/..."
$env:AVDR_SEPOLIA_PRIVATE_KEY = "<dedicated-testnet-private-key>"
```

`AVDR_SEPOLIA_ANCHOR_ADDRESS` is optional. If omitted, the address derived
from the key is used. If supplied, it must equal the derived sender; arbitrary
third-party destinations are rejected.

Optional safety controls are:

```text
AVDR_SEPOLIA_MAX_GAS
AVDR_SEPOLIA_MAX_FEE_PER_GAS_WEI
AVDR_SEPOLIA_CONFIRMATION_TIMEOUT_SECONDS
AVDR_SEPOLIA_POLL_INTERVAL_SECONDS
```

Do not put keys or credential-bearing RPC URLs in tracked files, command-line
arguments, screenshots, logs, or issue reports. `.env` and `.env.*` are
ignored, but environment injection from a secret manager is preferred. The
implementation redacts the RPC URL and never returns the private key or full
signed raw transaction in service DTOs.

## Explicit live smoke

No normal test or service import submits a transaction. The one-transaction
smoke additionally requires an explicit flag:

```powershell
.\.venv\Scripts\python.exe scripts\anchor_sepolia_receipt.py --execute-live
```

The script creates one controlled, in-process service resolution and anchors
that newly created receipt. Its evidence remains `controlled_demo`; blockchain
anchoring does not convert it into real-provider evidence. Missing credentials
produce a structured `BLOCKED` result and never generate a wallet.

## Independent on-chain readback

After submission, `verify_anchor(transaction_hash, expected_receipt_hash)`
re-reads chain state through JSON-RPC rather than trusting submission memory:

1. verify the connected RPC still reports Sepolia chain ID `11155111`;
2. fetch the transaction by hash;
3. verify transaction hash, sender, self-destination, and zero value;
4. decode canonical calldata and require `avdr-anchor-v1`;
5. compare the on-chain receipt hash with the local expected hash;
6. fetch the transaction receipt and require mined status `1`;
7. verify the transaction and receipt block hashes agree;
8. query `safe` and `finalized` heads where supported.

The UI exposes an explorer link only after mined-success readback and receipt
hash equality have passed. `mined`, `safe`, and `finalized` remain distinct.
If safe/finalized block tags are unsupported, finality is `not_verified`.

## Trust boundary and failure behavior

Local verification establishes internal receipt/hash-chain consistency.
Sepolia anchoring separately establishes that matching commitment data is
present in public chain history at the referenced transaction. Neither proves
DID truth, canonical DID state, resolver correctness, resolver trust,
cryptographic correctness of a DID document, consensus about the DID, or
production availability.

Anchoring is optional and post-resolution. An anchor failure preserves the
locally verifiable receipt and returns `anchor.status: failed` with a typed,
non-secret error code. It does not turn a successful DID resolution into a
resolution failure, and it never reports a failed or unverified transaction as
confirmed.
