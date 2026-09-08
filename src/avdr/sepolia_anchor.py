"""Privacy-minimized Ethereum Sepolia anchoring for AVDR audit receipts.

The module deliberately uses ordinary zero-value EIP-1559 transactions.  The
calldata is canonical JSON containing only an AVDR receipt commitment and the
minimum provenance needed to interpret it.  No DID, DID document, normalized
result, resolver response, telemetry, or disclosure nonce is accepted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx

from .audit import AUDIT_SCHEMA_VERSION, AnchorResult, NullAnchor, canonical_json_bytes

SEPOLIA_CHAIN_ID = 11155111
SEPOLIA_NETWORK = "ethereum-sepolia"
ANCHOR_PROTOCOL_VERSION = "avdr-anchor-v1"
ANCHOR_PAYLOAD_FIELDS = frozenset(
    {
        "protocol",
        "receipt_hash",
        "receipt_schema",
        "policy_identity_hash",
        "receipt_timestamp",
    }
)
DEFAULT_MAX_GAS = 100_000
DEFAULT_MAX_FEE_PER_GAS_WEI = 100_000_000_000  # 100 gwei, testnet gas only.
DEFAULT_CONFIRMATION_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 3.0
SEPOLIA_EXPLORER_BASE_URL = "https://sepolia.etherscan.io/tx/"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class AnchorError(RuntimeError):
    """A typed, presenter-safe anchoring error that contains no secret data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class JsonRpcClient(Protocol):
    async def call(self, method: str, params: list[Any]) -> Any:
        """Call one Ethereum JSON-RPC method."""


class TransactionSigner(Protocol):
    @property
    def address(self) -> str:
        """Return the public sender address."""

    def sign_transaction(self, transaction: dict[str, Any]) -> bytes:
        """Sign an EIP-1559 transaction without exposing key material."""


class HttpJsonRpcClient:
    """Small async JSON-RPC client that never exposes its credential-bearing URL."""

    def __init__(self, url: str, *, timeout_seconds: float = 20.0) -> None:
        if not isinstance(url, str) or not url.strip():
            raise AnchorError("RPC_NOT_CONFIGURED")
        self._url = url.strip()
        self._timeout_seconds = timeout_seconds
        self._request_id = 0

    def __repr__(self) -> str:
        return "HttpJsonRpcClient(url=<redacted>)"

    async def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise AnchorError("RPC_TRANSPORT_ERROR") from exc
        if response.status_code != 200:
            raise AnchorError("RPC_HTTP_ERROR")
        try:
            body = response.json()
        except ValueError as exc:
            raise AnchorError("RPC_INVALID_JSON") from exc
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            raise AnchorError("RPC_INVALID_RESPONSE")
        if body.get("error") is not None:
            raise AnchorError("RPC_METHOD_ERROR")
        if "result" not in body:
            raise AnchorError("RPC_MISSING_RESULT")
        return body["result"]


class EthAccountSigner:
    """Local signer backed by eth-account; its repr and errors reveal no key."""

    def __init__(self, private_key: str) -> None:
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover - dependency install guidance
            raise AnchorError("ETH_ACCOUNT_NOT_INSTALLED") from exc
        try:
            self._account = Account.from_key(private_key)
        except (TypeError, ValueError) as exc:
            raise AnchorError("INVALID_PRIVATE_KEY") from exc

    @property
    def address(self) -> str:
        return str(self._account.address)

    def sign_transaction(self, transaction: dict[str, Any]) -> bytes:
        try:
            signed = self._account.sign_transaction(transaction)
            return bytes(signed.raw_transaction)
        except Exception as exc:  # eth-account raises several validation types
            raise AnchorError("SIGNING_FAILED") from exc

    def __repr__(self) -> str:
        return f"EthAccountSigner(address={self.address!r}, private_key=<redacted>)"


@dataclass(frozen=True)
class SepoliaAnchorConfig:
    destination: str
    max_gas: int = DEFAULT_MAX_GAS
    max_fee_per_gas_wei: int = DEFAULT_MAX_FEE_PER_GAS_WEI
    confirmation_timeout_seconds: float = DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        _validate_address(self.destination)
        if not 21_000 <= self.max_gas <= 500_000:
            raise AnchorError("INVALID_MAX_GAS")
        if not 1 <= self.max_fee_per_gas_wei <= 1_000_000_000_000:
            raise AnchorError("INVALID_MAX_FEE")
        if not 1 <= self.confirmation_timeout_seconds <= 600:
            raise AnchorError("INVALID_CONFIRMATION_TIMEOUT")
        if not 0.1 <= self.poll_interval_seconds <= 30:
            raise AnchorError("INVALID_POLL_INTERVAL")


@dataclass(frozen=True)
class AnchorVerification:
    valid: bool
    status: str
    transaction_hash: str
    expected_receipt_hash: str
    chain_id: int | None = None
    onchain_receipt_hash: str | None = None
    destination: str | None = None
    sender: str | None = None
    value_wei: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    transaction_receipt_status: int | None = None
    confirmations: int | None = None
    finality_status: str = "not_verified"
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if value is not None
        }


@dataclass(frozen=True)
class _Preflight:
    transaction: dict[str, Any]
    wallet_balance_wei: int
    estimated_gas: int
    gas_limit: int
    max_fee_per_gas_wei: int
    max_priority_fee_per_gas_wei: int
    maximum_gas_cost_wei: int


def _validate_hash(value: Any, *, code: str = "INVALID_RECEIPT_HASH") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise AnchorError(code)
    return value


def _validate_address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise AnchorError("INVALID_DESTINATION")
    return value


def _same_address(left: str, right: str) -> bool:
    return left.lower() == right.lower()


def _quantity(value: int) -> str:
    if not isinstance(value, int) or value < 0:
        raise AnchorError("INVALID_QUANTITY")
    return hex(value)


def _parse_quantity(value: Any, *, code: str = "INVALID_RPC_QUANTITY") -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise AnchorError(code)
    return int(value, 16)


def canonical_anchor_payload(
    receipt_hash_value: str,
    metadata: Mapping[str, Any],
) -> bytes:
    """Encode the complete, fixed privacy boundary as canonical UTF-8 JSON."""
    digest = _validate_hash(receipt_hash_value)
    if metadata.get("receipt_hash") != digest:
        raise AnchorError("RECEIPT_HASH_METADATA_MISMATCH")
    if metadata.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise AnchorError("UNSUPPORTED_RECEIPT_SCHEMA")
    policy_hash = _validate_hash(
        metadata.get("policy_identity_hash"), code="INVALID_POLICY_IDENTITY_HASH"
    )
    timestamp = metadata.get("timestamp")
    if not isinstance(timestamp, str) or not 1 <= len(timestamp.encode("utf-8")) <= 64:
        raise AnchorError("INVALID_RECEIPT_TIMESTAMP")
    payload = {
        "protocol": ANCHOR_PROTOCOL_VERSION,
        "receipt_hash": digest,
        "receipt_schema": AUDIT_SCHEMA_VERSION,
        "policy_identity_hash": policy_hash,
        "receipt_timestamp": timestamp,
    }
    encoded = canonical_json_bytes(payload)
    if len(encoded) > 512:
        raise AnchorError("ANCHOR_PAYLOAD_TOO_LARGE")
    return encoded


def decode_anchor_payload(calldata: str | bytes) -> dict[str, Any]:
    """Decode and strictly validate on-chain AVDR anchor calldata."""
    if isinstance(calldata, str):
        if not calldata.startswith("0x") or len(calldata) % 2:
            raise AnchorError("MALFORMED_CALLDATA")
        try:
            raw = bytes.fromhex(calldata[2:])
        except ValueError as exc:
            raise AnchorError("MALFORMED_CALLDATA") from exc
    elif isinstance(calldata, bytes):
        raw = calldata
    else:
        raise AnchorError("MALFORMED_CALLDATA")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnchorError("MALFORMED_CALLDATA") from exc
    if not isinstance(payload, dict) or set(payload) != ANCHOR_PAYLOAD_FIELDS:
        raise AnchorError("MALFORMED_CALLDATA")
    if payload.get("protocol") != ANCHOR_PROTOCOL_VERSION:
        raise AnchorError("UNSUPPORTED_ANCHOR_PROTOCOL")
    _validate_hash(payload.get("receipt_hash"))
    _validate_hash(
        payload.get("policy_identity_hash"), code="INVALID_POLICY_IDENTITY_HASH"
    )
    if payload.get("receipt_schema") != AUDIT_SCHEMA_VERSION:
        raise AnchorError("UNSUPPORTED_RECEIPT_SCHEMA")
    timestamp = payload.get("receipt_timestamp")
    if not isinstance(timestamp, str) or not 1 <= len(timestamp.encode("utf-8")) <= 64:
        raise AnchorError("INVALID_RECEIPT_TIMESTAMP")
    if canonical_json_bytes(payload) != raw:
        raise AnchorError("NON_CANONICAL_CALLDATA")
    return payload


class SepoliaCalldataAnchor:
    """AuditAnchor implementation using one zero-value Sepolia transaction."""

    def __init__(
        self,
        *,
        rpc: JsonRpcClient,
        signer: TransactionSigner,
        config: SepoliaAnchorConfig,
    ) -> None:
        _validate_address(signer.address)
        if not _same_address(config.destination, signer.address):
            raise AnchorError("DESTINATION_MUST_EQUAL_SENDER")
        self._rpc = rpc
        self._signer = signer
        self._config = config

    def describe(self) -> dict[str, Any]:
        return {
            "implementation": type(self).__name__,
            "network": SEPOLIA_NETWORK,
            "chain_id": SEPOLIA_CHAIN_ID,
            "destination": self._config.destination,
            "value_wei": 0,
            "protocol": ANCHOR_PROTOCOL_VERSION,
        }

    async def validate_network(self) -> None:
        """Validate the configured RPC chain without signing or broadcasting."""
        chain_id = _parse_quantity(
            await self._rpc.call("eth_chainId", []), code="INVALID_CHAIN_ID"
        )
        if chain_id != SEPOLIA_CHAIN_ID:
            raise AnchorError("WRONG_CHAIN")

    async def anchor(
        self,
        receipt_hash_value: str,
        metadata: dict[str, Any],
    ) -> AnchorResult:
        preflight: _Preflight | None = None
        transaction_hash: str | None = None
        try:
            calldata = canonical_anchor_payload(receipt_hash_value, metadata)
            preflight = await self._preflight(calldata)
            raw_transaction = self._signer.sign_transaction(preflight.transaction)
            if not isinstance(raw_transaction, bytes) or not raw_transaction:
                raise AnchorError("SIGNING_FAILED")
            submitted = await self._rpc.call(
                "eth_sendRawTransaction", ["0x" + raw_transaction.hex()]
            )
            if not isinstance(submitted, str) or not _TX_HASH_RE.fullmatch(submitted):
                raise AnchorError("INVALID_TRANSACTION_HASH")
            transaction_hash = submitted
            mined = await self._wait_for_receipt(transaction_hash)
            if mined is None:
                return self._result(
                    status="submitted",
                    receipt_hash_value=receipt_hash_value,
                    transaction_hash=transaction_hash,
                    preflight=preflight,
                    finality_status="not_verified",
                )
            verification = await self.verify_anchor(
                transaction_hash, receipt_hash_value
            )
            if not verification.valid:
                return self._result(
                    status="failed",
                    receipt_hash_value=receipt_hash_value,
                    transaction_hash=transaction_hash,
                    preflight=preflight,
                    verification=verification,
                    error_code=verification.error_code,
                )
            return self._result(
                status="mined",
                receipt_hash_value=receipt_hash_value,
                transaction_hash=transaction_hash,
                preflight=preflight,
                verification=verification,
            )
        except AnchorError as exc:
            return self._result(
                status="failed",
                receipt_hash_value=receipt_hash_value,
                transaction_hash=transaction_hash,
                preflight=preflight,
                error_code=exc.code,
            )
        except Exception:
            return self._result(
                status="failed",
                receipt_hash_value=receipt_hash_value,
                transaction_hash=transaction_hash,
                preflight=preflight,
                error_code="ANCHOR_INTERNAL_ERROR",
            )

    async def _preflight(self, calldata: bytes) -> _Preflight:
        await self.validate_network()

        sender = self._signer.address
        destination = self._config.destination
        if not _same_address(sender, destination):
            raise AnchorError("DESTINATION_MUST_EQUAL_SENDER")

        balance = _parse_quantity(
            await self._rpc.call("eth_getBalance", [sender, "latest"]),
            code="INVALID_BALANCE",
        )
        nonce = _parse_quantity(
            await self._rpc.call("eth_getTransactionCount", [sender, "pending"]),
            code="INVALID_NONCE",
        )
        latest = await self._rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict) or latest.get("baseFeePerGas") is None:
            raise AnchorError("EIP1559_NOT_SUPPORTED")
        base_fee = _parse_quantity(latest["baseFeePerGas"], code="INVALID_BASE_FEE")
        priority_fee = _parse_quantity(
            await self._rpc.call("eth_maxPriorityFeePerGas", []),
            code="INVALID_PRIORITY_FEE",
        )
        max_fee = base_fee * 2 + priority_fee
        if max_fee > self._config.max_fee_per_gas_wei:
            raise AnchorError("MAX_FEE_EXCEEDED")

        call = {
            "from": sender,
            "to": destination,
            "value": "0x0",
            "data": "0x" + calldata.hex(),
        }
        estimate = _parse_quantity(
            await self._rpc.call("eth_estimateGas", [call]),
            code="INVALID_GAS_ESTIMATE",
        )
        if estimate > self._config.max_gas:
            raise AnchorError("MAX_GAS_EXCEEDED")
        gas_limit = min(
            self._config.max_gas,
            estimate + max(5_000, estimate // 5),
        )
        maximum_cost = gas_limit * max_fee
        if balance < maximum_cost:
            raise AnchorError("INSUFFICIENT_SEPOLIA_ETH")

        transaction = {
            "type": 2,
            "chainId": SEPOLIA_CHAIN_ID,
            "nonce": nonce,
            "maxPriorityFeePerGas": priority_fee,
            "maxFeePerGas": max_fee,
            "gas": gas_limit,
            "to": destination,
            "value": 0,
            "data": calldata,
        }
        return _Preflight(
            transaction=transaction,
            wallet_balance_wei=balance,
            estimated_gas=estimate,
            gas_limit=gas_limit,
            max_fee_per_gas_wei=max_fee,
            max_priority_fee_per_gas_wei=priority_fee,
            maximum_gas_cost_wei=maximum_cost,
        )

    async def _wait_for_receipt(self, transaction_hash: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + self._config.confirmation_timeout_seconds
        while True:
            receipt = await self._rpc.call(
                "eth_getTransactionReceipt", [transaction_hash]
            )
            if receipt is not None:
                if not isinstance(receipt, dict):
                    raise AnchorError("INVALID_TRANSACTION_RECEIPT")
                return receipt
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._config.poll_interval_seconds)

    async def verify_anchor(
        self,
        transaction_hash: str,
        expected_receipt_hash: str,
    ) -> AnchorVerification:
        """Independently re-read transaction calldata and its mined receipt."""
        try:
            if not isinstance(transaction_hash, str) or not _TX_HASH_RE.fullmatch(
                transaction_hash
            ):
                raise AnchorError("INVALID_TRANSACTION_HASH")
            expected = _validate_hash(expected_receipt_hash)
            chain_id = _parse_quantity(
                await self._rpc.call("eth_chainId", []), code="INVALID_CHAIN_ID"
            )
            if chain_id != SEPOLIA_CHAIN_ID:
                raise AnchorError("WRONG_CHAIN")

            transaction = await self._rpc.call(
                "eth_getTransactionByHash", [transaction_hash]
            )
            if not isinstance(transaction, dict):
                raise AnchorError("WRONG_TRANSACTION")
            returned_hash = transaction.get("hash")
            if (
                not isinstance(returned_hash, str)
                or returned_hash.lower() != transaction_hash.lower()
            ):
                raise AnchorError("WRONG_TRANSACTION")
            transaction_type = _parse_quantity(
                transaction.get("type"), code="INVALID_TRANSACTION_TYPE"
            )
            if transaction_type != 2:
                raise AnchorError("WRONG_TRANSACTION_TYPE")
            destination = transaction.get("to")
            sender = transaction.get("from")
            if not isinstance(destination, str) or not _same_address(
                destination, self._config.destination
            ):
                raise AnchorError("WRONG_DESTINATION")
            if not isinstance(sender, str) or not _same_address(
                sender, self._signer.address
            ):
                raise AnchorError("WRONG_SENDER")
            value_wei = _parse_quantity(
                transaction.get("value"), code="INVALID_TRANSACTION_VALUE"
            )
            if value_wei != 0:
                raise AnchorError("NONZERO_TRANSACTION_VALUE")
            payload = decode_anchor_payload(transaction.get("input"))
            onchain_hash = payload["receipt_hash"]
            if onchain_hash != expected:
                raise AnchorError("RECEIPT_HASH_MISMATCH")

            receipt = await self._rpc.call(
                "eth_getTransactionReceipt", [transaction_hash]
            )
            if not isinstance(receipt, dict):
                raise AnchorError("TRANSACTION_NOT_MINED")
            receipt_status = _parse_quantity(
                receipt.get("status"), code="INVALID_TRANSACTION_RECEIPT"
            )
            if receipt_status != 1:
                raise AnchorError("FAILED_TRANSACTION_RECEIPT")
            receipt_transaction_hash = receipt.get("transactionHash")
            if (
                not isinstance(receipt_transaction_hash, str)
                or receipt_transaction_hash.lower() != transaction_hash.lower()
            ):
                raise AnchorError("WRONG_TRANSACTION_RECEIPT")
            block_number = _parse_quantity(
                receipt.get("blockNumber"), code="INVALID_BLOCK_NUMBER"
            )
            block_hash = receipt.get("blockHash")
            if not isinstance(block_hash, str) or not _TX_HASH_RE.fullmatch(block_hash):
                raise AnchorError("INVALID_BLOCK_HASH")
            transaction_block_hash = transaction.get("blockHash")
            if (
                not isinstance(transaction_block_hash, str)
                or transaction_block_hash.lower() != block_hash.lower()
            ):
                raise AnchorError("BLOCK_HASH_MISMATCH")

            finality = await self._finality_status(block_number)
            confirmations = await self._confirmation_count(block_number)
            return AnchorVerification(
                valid=True,
                status="verified",
                transaction_hash=transaction_hash,
                expected_receipt_hash=expected,
                chain_id=chain_id,
                onchain_receipt_hash=onchain_hash,
                destination=destination,
                sender=sender,
                value_wei=value_wei,
                block_number=block_number,
                block_hash=block_hash,
                transaction_receipt_status=receipt_status,
                confirmations=confirmations,
                finality_status=finality,
            )
        except AnchorError as exc:
            return AnchorVerification(
                valid=False,
                status="failed",
                transaction_hash=transaction_hash,
                expected_receipt_hash=expected_receipt_hash,
                error_code=exc.code,
            )
        except Exception:
            return AnchorVerification(
                valid=False,
                status="failed",
                transaction_hash=transaction_hash,
                expected_receipt_hash=expected_receipt_hash,
                error_code="ANCHOR_READBACK_ERROR",
            )

    async def _finality_status(self, block_number: int) -> str:
        supported = False
        for tag in ("finalized", "safe"):
            try:
                block = await self._rpc.call("eth_getBlockByNumber", [tag, False])
                if not isinstance(block, dict) or block.get("number") is None:
                    continue
                supported = True
                if _parse_quantity(block["number"]) >= block_number:
                    return tag
            except AnchorError:
                continue
        return "mined" if supported else "not_verified"

    async def _confirmation_count(self, block_number: int) -> int | None:
        try:
            latest = await self._rpc.call("eth_blockNumber", [])
            latest_number = _parse_quantity(latest, code="INVALID_BLOCK_NUMBER")
            return max(0, latest_number - block_number + 1)
        except AnchorError:
            return None

    def _result(
        self,
        *,
        status: str,
        receipt_hash_value: str,
        transaction_hash: str | None,
        preflight: _Preflight | None,
        verification: AnchorVerification | None = None,
        finality_status: str | None = None,
        error_code: str | None = None,
    ) -> AnchorResult:
        verified = verification if verification and verification.valid else None
        return AnchorResult(
            status=status,
            network=SEPOLIA_NETWORK,
            transaction_id=transaction_hash,
            chain_id=SEPOLIA_CHAIN_ID,
            transaction_hash=transaction_hash,
            block_number=verified.block_number if verified else None,
            block_hash=verified.block_hash if verified else None,
            sender=self._signer.address,
            destination=self._config.destination,
            value_wei=0,
            receipt_hash=receipt_hash_value,
            onchain_receipt_hash=(
                verified.onchain_receipt_hash if verified else None
            ),
            onchain_receipt_match=(True if verified else None),
            verification=("onchain_readback" if verified else None),
            confirmations=verified.confirmations if verified else None,
            finality_status=(
                verified.finality_status
                if verified
                else finality_status or "not_verified"
            ),
            explorer_url=(
                SEPOLIA_EXPLORER_BASE_URL + transaction_hash
                if verified and transaction_hash
                else None
            ),
            estimated_gas=preflight.estimated_gas if preflight else None,
            gas_limit=preflight.gas_limit if preflight else None,
            max_fee_per_gas_wei=(
                preflight.max_fee_per_gas_wei if preflight else None
            ),
            maximum_gas_cost_wei=(
                preflight.maximum_gas_cost_wei if preflight else None
            ),
            wallet_balance_before_wei=(
                preflight.wallet_balance_wei if preflight else None
            ),
            transaction_receipt_status=(
                verified.transaction_receipt_status if verified else None
            ),
            error_code=error_code,
        )


def build_audit_anchor_from_env(
    environ: Mapping[str, str] | None = None,
) -> NullAnchor | SepoliaCalldataAnchor:
    """Build the configured anchor without ever returning or logging secrets."""
    values = os.environ if environ is None else environ
    mode = values.get("AVDR_AUDIT_ANCHOR", "null").strip().lower()
    if mode in {"", "null", "none", "off"}:
        return NullAnchor()
    if mode != "sepolia":
        raise AnchorError("UNSUPPORTED_ANCHOR_MODE")

    private_key = values.get("AVDR_SEPOLIA_PRIVATE_KEY", "").strip()
    if not private_key:
        raise AnchorError("NO_TESTNET_WALLET")
    rpc_url = values.get("AVDR_SEPOLIA_RPC_URL", "").strip()
    if not rpc_url:
        raise AnchorError("RPC_NOT_CONFIGURED")
    signer = EthAccountSigner(private_key)
    destination = values.get("AVDR_SEPOLIA_ANCHOR_ADDRESS", "").strip()
    if not destination:
        destination = signer.address

    config = SepoliaAnchorConfig(
        destination=destination,
        max_gas=_environment_int(values, "AVDR_SEPOLIA_MAX_GAS", DEFAULT_MAX_GAS),
        max_fee_per_gas_wei=_environment_int(
            values,
            "AVDR_SEPOLIA_MAX_FEE_PER_GAS_WEI",
            DEFAULT_MAX_FEE_PER_GAS_WEI,
        ),
        confirmation_timeout_seconds=_environment_float(
            values,
            "AVDR_SEPOLIA_CONFIRMATION_TIMEOUT_SECONDS",
            DEFAULT_CONFIRMATION_TIMEOUT_SECONDS,
        ),
        poll_interval_seconds=_environment_float(
            values,
            "AVDR_SEPOLIA_POLL_INTERVAL_SECONDS",
            DEFAULT_POLL_INTERVAL_SECONDS,
        ),
    )
    return SepoliaCalldataAnchor(
        rpc=HttpJsonRpcClient(rpc_url), signer=signer, config=config
    )


def _environment_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AnchorError(f"INVALID_{name}") from exc


def _environment_float(
    values: Mapping[str, str], name: str, default: float
) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise AnchorError(f"INVALID_{name}") from exc
