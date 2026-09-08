"""Offline Sepolia calldata-anchor safety, readback, and UI contract tests."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from avdr.audit import (
    AnchorResult,
    LocalAuditRecorder,
    NullAnchor,
    build_commitment,
    canonical_json_bytes,
)
from avdr.inventory import ProviderEntry, ProviderInventory
from avdr.real_router.app import create_app
from avdr.sepolia_anchor import (
    ANCHOR_PAYLOAD_FIELDS,
    ANCHOR_PROTOCOL_VERSION,
    SEPOLIA_CHAIN_ID,
    AnchorError,
    SepoliaAnchorConfig,
    SepoliaCalldataAnchor,
    build_audit_anchor_from_env,
    canonical_anchor_payload,
    decode_anchor_payload,
)
from avdr.telemetry import TelemetrySink

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIPT_HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "c" * 64
POLICY_HASH = "sha256:" + "b" * 64
ADDRESS = "0x" + "11" * 20
OTHER_ADDRESS = "0x" + "44" * 20
TX_HASH = "0x" + "22" * 32
BLOCK_HASH = "0x" + "33" * 32
TIMESTAMP = "2026-09-09T00:00:00+00:00"
METADATA = {
    "receipt_hash": RECEIPT_HASH,
    "schema_version": "avdr-audit-v1",
    "timestamp": TIMESTAMP,
    "policy_identity_hash": POLICY_HASH,
}


class FakeSigner:
    def __init__(self, address: str = ADDRESS) -> None:
        self._address = address
        self.transactions: list[dict] = []

    @property
    def address(self) -> str:
        return self._address

    def sign_transaction(self, transaction: dict) -> bytes:
        self.transactions.append(copy.deepcopy(transaction))
        return b"\x02signed-with-offline-fake"


class FakeRpc:
    def __init__(self, *, calldata: bytes | None = None) -> None:
        calldata = calldata or canonical_anchor_payload(RECEIPT_HASH, METADATA)
        self.calls: list[tuple[str, list]] = []
        self.sent_raw: str | None = None
        self.chain_id = SEPOLIA_CHAIN_ID
        self.balance = 10**18
        self.estimate = 30_000
        self.transaction: dict | None = {
            "hash": TX_HASH,
            "from": ADDRESS,
            "to": ADDRESS,
            "value": "0x0",
            "input": "0x" + calldata.hex(),
            "blockHash": BLOCK_HASH,
            "blockNumber": "0x10",
            "type": "0x2",
        }
        self.receipt: dict | None = {
            "transactionHash": TX_HASH,
            "blockHash": BLOCK_HASH,
            "blockNumber": "0x10",
            "status": "0x1",
            "type": "0x2",
        }
        self.finalized_number: int | None = 15
        self.safe_number: int | None = 16

    async def call(self, method: str, params: list):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_getTransactionCount":
            return "0x7"
        if method == "eth_getBlockByNumber":
            tag = params[0]
            if tag == "latest":
                return {"number": "0x12", "baseFeePerGas": hex(1_000_000_000)}
            if tag == "finalized":
                return (
                    None
                    if self.finalized_number is None
                    else {"number": hex(self.finalized_number)}
                )
            if tag == "safe":
                return (
                    None
                    if self.safe_number is None
                    else {"number": hex(self.safe_number)}
                )
        if method == "eth_maxPriorityFeePerGas":
            return hex(1_000_000_000)
        if method == "eth_estimateGas":
            return hex(self.estimate)
        if method == "eth_sendRawTransaction":
            self.sent_raw = params[0]
            return TX_HASH
        if method == "eth_getTransactionByHash":
            return copy.deepcopy(self.transaction)
        if method == "eth_getTransactionReceipt":
            return copy.deepcopy(self.receipt)
        if method == "eth_blockNumber":
            return "0x12"
        raise AssertionError(f"unexpected RPC method: {method}")


def anchor(rpc: FakeRpc, signer: FakeSigner | None = None) -> SepoliaCalldataAnchor:
    return SepoliaCalldataAnchor(
        rpc=rpc,
        signer=signer or FakeSigner(),
        config=SepoliaAnchorConfig(
            destination=ADDRESS,
            confirmation_timeout_seconds=1,
            poll_interval_seconds=0.1,
        ),
    )


def commitment():
    return build_commitment(
        request_id="p11-test-request",
        did="did:example:private-local-value",
        strategy="single-static",
        selected_resolver_ids=["local-a"],
        launch_order=["local-a"],
        candidate_count=1,
        policy_identity={"name": "single-static", "identity_hash": POLICY_HASH},
        acceptance_profile="w3c-basic-v1",
        returned_provider="local-a",
        normalized_result={
            "didDocument": {"id": "did:example:private-local-value"},
            "accepted": True,
        },
        calls_used=1,
        evidence_mode="controlled_demo",
        timestamp=TIMESTAMP,
        request_nonce="00112233445566778899aabbccddeeff",
    )


def test_null_anchor_is_default_and_configuration_fails_closed():
    configured = build_audit_anchor_from_env({})
    assert isinstance(configured, NullAnchor)

    with pytest.raises(AnchorError, match="NO_TESTNET_WALLET"):
        build_audit_anchor_from_env({"AVDR_AUDIT_ANCHOR": "sepolia"})
    with pytest.raises(AnchorError, match="RPC_NOT_CONFIGURED"):
        build_audit_anchor_from_env(
            {
                "AVDR_AUDIT_ANCHOR": "sepolia",
                "AVDR_SEPOLIA_PRIVATE_KEY": "configured-but-never-parsed",
            }
        )
    with pytest.raises(AnchorError, match="UNSUPPORTED_ANCHOR_MODE"):
        build_audit_anchor_from_env({"AVDR_AUDIT_ANCHOR": "mainnet"})
    with pytest.raises(AnchorError, match="DESTINATION_MUST_EQUAL_SENDER"):
        SepoliaCalldataAnchor(
            rpc=FakeRpc(),
            signer=FakeSigner(),
            config=SepoliaAnchorConfig(destination=OTHER_ADDRESS),
        )


@pytest.mark.asyncio
async def test_startup_network_validation_fails_closed_on_wrong_chain():
    rpc = FakeRpc()
    rpc.chain_id = 1

    with pytest.raises(AnchorError, match="WRONG_CHAIN"):
        await anchor(rpc).validate_network()

    assert [method for method, _ in rpc.calls] == ["eth_chainId"]


def test_anchor_payload_is_canonical_versioned_and_privacy_minimized():
    encoded = canonical_anchor_payload(RECEIPT_HASH, METADATA)
    payload = decode_anchor_payload(encoded)

    assert encoded == canonical_json_bytes(payload)
    assert set(payload) == ANCHOR_PAYLOAD_FIELDS
    assert payload["protocol"] == ANCHOR_PROTOCOL_VERSION
    assert payload["receipt_hash"] == RECEIPT_HASH
    assert b"did:example" not in encoded
    assert b"didDocument" not in encoded
    assert b"normalized" not in encoded
    assert b"request_nonce" not in encoded
    assert b"telemetry" not in encoded


def test_decoder_rejects_noncanonical_extra_fields_and_protocol_versions():
    payload = decode_anchor_payload(canonical_anchor_payload(RECEIPT_HASH, METADATA))
    with_extra = {**payload, "did": "did:example:must-not-appear"}
    with pytest.raises(AnchorError, match="MALFORMED_CALLDATA"):
        decode_anchor_payload(canonical_json_bytes(with_extra))

    wrong_protocol = {**payload, "protocol": "avdr-anchor-v2"}
    with pytest.raises(AnchorError, match="UNSUPPORTED_ANCHOR_PROTOCOL"):
        decode_anchor_payload(canonical_json_bytes(wrong_protocol))

    pretty = json.dumps(payload, sort_keys=True, indent=2).encode()
    with pytest.raises(AnchorError, match="NON_CANONICAL_CALLDATA"):
        decode_anchor_payload(pretty)


@pytest.mark.asyncio
async def test_submission_is_zero_value_eip1559_self_transaction_and_readback_matches():
    rpc = FakeRpc()
    signer = FakeSigner()
    result = await anchor(rpc, signer).anchor(RECEIPT_HASH, dict(METADATA))

    assert result.status == "mined"
    assert result.network == "ethereum-sepolia"
    assert result.chain_id == SEPOLIA_CHAIN_ID
    assert result.transaction_hash == TX_HASH
    assert result.transaction_receipt_status == 1
    assert result.onchain_receipt_hash == RECEIPT_HASH
    assert result.onchain_receipt_match is True
    assert result.verification == "onchain_readback"
    assert result.finality_status == "safe"
    assert result.confirmations == 3
    assert result.explorer_url.endswith(TX_HASH)

    assert len(signer.transactions) == 1
    transaction = signer.transactions[0]
    assert transaction["type"] == 2
    assert transaction["chainId"] == SEPOLIA_CHAIN_ID
    assert transaction["to"] == signer.address == ADDRESS
    assert transaction["value"] == 0
    assert transaction["gas"] <= 100_000
    assert transaction["maxFeePerGas"] <= 100_000_000_000
    assert decode_anchor_payload(transaction["data"])["receipt_hash"] == RECEIPT_HASH
    assert b"did:example" not in transaction["data"]
    assert b"didDocument" not in transaction["data"]

    estimate_call = next(params for method, params in rpc.calls if method == "eth_estimateGas")
    assert estimate_call[0]["from"] == ADDRESS
    assert estimate_call[0]["to"] == ADDRESS
    assert estimate_call[0]["value"] == "0x0"
    assert rpc.sent_raw == "0x" + b"\x02signed-with-offline-fake".hex()


@pytest.mark.asyncio
async def test_wrong_chain_fails_before_signing_or_submission():
    rpc = FakeRpc()
    rpc.chain_id = 1
    signer = FakeSigner()
    result = await anchor(rpc, signer).anchor(RECEIPT_HASH, dict(METADATA))

    assert result.status == "failed"
    assert result.error_code == "WRONG_CHAIN"
    assert signer.transactions == []
    assert not any(method == "eth_sendRawTransaction" for method, _ in rpc.calls)


@pytest.mark.asyncio
async def test_fee_gas_and_balance_bounds_fail_before_signing():
    gas_rpc = FakeRpc()
    gas_rpc.estimate = 100_001
    gas_result = await anchor(gas_rpc).anchor(RECEIPT_HASH, dict(METADATA))
    assert gas_result.error_code == "MAX_GAS_EXCEEDED"

    balance_rpc = FakeRpc()
    balance_rpc.balance = 1
    balance_result = await anchor(balance_rpc).anchor(RECEIPT_HASH, dict(METADATA))
    assert balance_result.error_code == "INSUFFICIENT_SEPOLIA_ETH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_hash", "error"),
    [
        ("wrong_hash", OTHER_HASH, "RECEIPT_HASH_MISMATCH"),
        ("wrong_chain", RECEIPT_HASH, "WRONG_CHAIN"),
        ("wrong_destination", RECEIPT_HASH, "WRONG_DESTINATION"),
        ("nonzero_value", RECEIPT_HASH, "NONZERO_TRANSACTION_VALUE"),
        ("wrong_transaction", RECEIPT_HASH, "WRONG_TRANSACTION"),
        ("wrong_type", RECEIPT_HASH, "WRONG_TRANSACTION_TYPE"),
        ("malformed_calldata", RECEIPT_HASH, "MALFORMED_CALLDATA"),
        ("unsupported_protocol", RECEIPT_HASH, "UNSUPPORTED_ANCHOR_PROTOCOL"),
        ("failed_receipt", RECEIPT_HASH, "FAILED_TRANSACTION_RECEIPT"),
    ],
)
async def test_readback_rejects_tampering_and_wrong_context(
    mutation: str, expected_hash: str, error: str
):
    rpc = FakeRpc()
    if mutation == "wrong_chain":
        rpc.chain_id = 1
    elif mutation == "wrong_destination":
        rpc.transaction["to"] = OTHER_ADDRESS
    elif mutation == "nonzero_value":
        rpc.transaction["value"] = "0x1"
    elif mutation == "wrong_transaction":
        rpc.transaction = None
    elif mutation == "wrong_type":
        rpc.transaction["type"] = "0x0"
    elif mutation == "malformed_calldata":
        rpc.transaction["input"] = "0xdeadbeef"
    elif mutation == "unsupported_protocol":
        payload = decode_anchor_payload(
            canonical_anchor_payload(RECEIPT_HASH, METADATA)
        )
        payload["protocol"] = "avdr-anchor-v2"
        rpc.transaction["input"] = "0x" + canonical_json_bytes(payload).hex()
    elif mutation == "failed_receipt":
        rpc.receipt["status"] = "0x0"

    verification = await anchor(rpc).verify_anchor(TX_HASH, expected_hash)
    assert verification.valid is False
    assert verification.status == "failed"
    assert verification.error_code == error


@pytest.mark.asyncio
async def test_anchor_failure_preserves_locally_verified_receipt():
    rpc = FakeRpc()
    rpc.chain_id = 1
    recorder = LocalAuditRecorder(anchor=anchor(rpc))

    created = await recorder.record(commitment())
    stored = await recorder.get(created.receipt_id)

    assert created.recorded is True
    assert created.status == "recorded_anchor_failed"
    assert created.anchor.status == "failed"
    assert created.anchor.error_code == "WRONG_CHAIN"
    assert stored["integrity_verified"] is True
    assert stored["anchor"]["status"] == "failed"
    assert stored["anchor"]["error_code"] == "WRONG_CHAIN"


@pytest.mark.asyncio
async def test_extended_anchor_result_is_additive_and_null_shape_is_unchanged():
    assert (await NullAnchor().anchor(RECEIPT_HASH, {})).to_dict() == {
        "status": "not_configured",
        "network": None,
        "transaction_id": None,
    }
    extended = AnchorResult(
        status="mined",
        network="ethereum-sepolia",
        transaction_id=TX_HASH,
        chain_id=SEPOLIA_CHAIN_ID,
        transaction_hash=TX_HASH,
        block_number=16,
        receipt_hash=RECEIPT_HASH,
        onchain_receipt_hash=RECEIPT_HASH,
        onchain_receipt_match=True,
        value_wei=0,
    ).to_dict()
    assert extended["status"] == "mined"
    assert extended["onchain_receipt_match"] is True
    assert extended["value_wei"] == 0
    assert "error_code" not in extended


@pytest.mark.asyncio
async def test_service_dto_exposes_verified_anchor_separately(
    healthy_cluster, tmp_path
):
    class VerifiedAnchor:
        async def anchor(self, receipt_hash_value, metadata):
            return AnchorResult(
                status="mined",
                network="ethereum-sepolia",
                transaction_id=TX_HASH,
                chain_id=SEPOLIA_CHAIN_ID,
                transaction_hash=TX_HASH,
                block_number=16,
                block_hash=BLOCK_HASH,
                sender=ADDRESS,
                destination=ADDRESS,
                value_wei=0,
                receipt_hash=receipt_hash_value,
                onchain_receipt_hash=receipt_hash_value,
                onchain_receipt_match=True,
                verification="onchain_readback",
                finality_status="not_verified",
                explorer_url=f"https://sepolia.etherscan.io/tx/{TX_HASH}",
                transaction_receipt_status=1,
            )

    inventory = ProviderInventory(
        inventory_version="p11-service-dto-test",
        providers=[
            ProviderEntry(
                id="local-a",
                endpoint=healthy_cluster["resolver-a"].url,
                adapter="universal-resolver-v1",
                supported_did_methods=["example"],
                available=True,
            )
        ],
    )
    recorder = LocalAuditRecorder(anchor=VerifiedAnchor())
    app = create_app(
        inventory=inventory,
        sink=TelemetrySink(tmp_path / "telemetry"),
        single_static_target="local-a",
        audit_recorder=recorder,
    )
    service = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://service"
    )
    outbound = httpx.AsyncClient()
    app.state.client = outbound
    async with service, outbound:
        response = await service.post(
            "/resolve",
            json={"did": "did:example:p11-service", "policy": "single-static"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["recorded"] is True
    assert body["audit"]["integrity_verified"] is True
    assert body["audit"]["anchor"]["status"] == "mined"
    assert body["audit"]["anchor"]["onchain_receipt_match"] is True
    assert body["audit"]["anchor"]["verification"] == "onchain_readback"
    assert body["audit"]["anchor"]["value_wei"] == 0
    assert "did:example:p11-service" not in json.dumps(body["audit"]["anchor"])


def test_dashboard_separates_local_integrity_from_public_anchor():
    html = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    javascript = (REPO_ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "Local integrity, not DID truth" in html
    assert "Public blockchain anchor" in html
    assert "On-chain receipt match" in html
    assert "does not make the DID document canonical" in html
    assert "auditAnchorNetwork" in javascript
    assert "Ethereum Sepolia" in javascript
    assert "onchain_receipt_match === true" in javascript
    assert "https://sepolia.etherscan.io/tx/" in javascript


def test_live_script_is_explicit_and_blocks_without_a_wallet():
    script = REPO_ROOT / "scripts" / "anchor_sepolia_receipt.py"
    environ = dict(os.environ)
    for name in (
        "AVDR_AUDIT_ANCHOR",
        "AVDR_SEPOLIA_RPC_URL",
        "AVDR_SEPOLIA_PRIVATE_KEY",
        "AVDR_SEPOLIA_ANCHOR_ADDRESS",
    ):
        environ.pop(name, None)

    disabled = subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=15,
    )
    blocked = subprocess.run(
        [sys.executable, str(script), "--execute-live"],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert disabled.returncode == 2
    assert json.loads(disabled.stdout)["block_reason"] == "EXPLICIT_OPT_IN_REQUIRED"
    assert blocked.returncode == 2
    blocked_report = json.loads(blocked.stdout)
    assert blocked_report["live_anchor_status"] == "BLOCKED"
    assert blocked_report["block_reason"] == "NO_TESTNET_WALLET"
