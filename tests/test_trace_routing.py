import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from utils.evm_information import (
    GETH_TRACE_START_BLOCK,
    HistoricalStateUnavailableError,
    NoOpcodeTraceError,
    TRANSFER_EVENT_TOPIC,
    TraceFormatter,
)


def formatter_without_rpc() -> TraceFormatter:
    return TraceFormatter.__new__(TraceFormatter)


def test_old_block_reports_missing_historical_state_without_trace_request(monkeypatch):
    formatter = formatter_without_rpc()

    def unexpected_trace_request(_tx_hash):
        pytest.fail("old blocks must not issue a Geth trace request")

    monkeypatch.setattr(formatter, "_fetch_geth_trace", unexpected_trace_request)

    with pytest.raises(HistoricalStateUnavailableError, match="历史 state"):
        formatter._fetch_raw_trace("0x" + "11" * 32, GETH_TRACE_START_BLOCK - 1)


def test_21000_gas_transaction_skips_trace_request(monkeypatch):
    formatter = formatter_without_rpc()

    def unexpected_trace_request(_tx_hash):
        pytest.fail("a 21000-gas transfer must not issue a Geth trace request")

    monkeypatch.setattr(formatter, "_fetch_geth_trace", unexpected_trace_request)

    with pytest.raises(NoOpcodeTraceError, match="gasUsed=21000"):
        formatter._fetch_raw_trace(
            "0x" + "22" * 32,
            GETH_TRACE_START_BLOCK,
            gas_used=21_000,
        )


def test_other_transactions_still_use_native_geth(monkeypatch):
    formatter = formatter_without_rpc()
    expected = {"structLogs": [{"op": "STOP"}]}
    calls = []

    def geth_trace(tx_hash):
        calls.append(tx_hash)
        return expected

    monkeypatch.setattr(formatter, "_fetch_geth_trace", geth_trace)
    tx_hash = "0x" + "33" * 32

    assert formatter._fetch_raw_trace(
        tx_hash,
        GETH_TRACE_START_BLOCK,
        gas_used=21_001,
    ) is expected
    assert calls == [tx_hash]


def test_sparse_trace_is_fetched_once_and_adapted(monkeypatch):
    formatter = formatter_without_rpc()
    calls = []
    sparse = {
        "structLogs": [{
            "pc": 1,
            "op": "SHA3",
            "gas": 100,
            "gasCost": 30,
            "depth": 1,
            "stack": ["0x00", "0x40"],
            "memory": "0x" + "ab" * 64,
            "storage": {},
        }],
        "faults": [],
    }

    def trace_request(tx_hash, options, label):
        calls.append((tx_hash, options, label))
        return sparse

    monkeypatch.setattr(formatter, "_run_geth_trace_request", trace_request)
    result = formatter._fetch_geth_trace("0x" + "44" * 32)

    assert len(calls) == 1
    assert "tracer" in calls[0][1]
    assert result["structLogs"][0]["memory"] == ["0x" + "ab" * 32] * 2


def test_erc20_candidates_require_balance_or_transfer_evidence():
    formatter = formatter_without_rpc()
    storage_token = "0x" + "11" * 20
    event_token = "0x" + "22" * 20
    unrelated_contract = "0x" + "33" * 20
    slot = "0xabc"
    steps = [
        {"opcode": "SLOAD", "stack": [slot], "RW_address": storage_token},
        {"opcode": "SSTORE", "stack": ["0x01", "0xdef"], "RW_address": unrelated_contract},
    ]
    receipt = {
        "logs": [{
            "address": event_token,
            "topics": [TRANSFER_EVENT_TOPIC, "0x01", "0x02"],
        }]
    }

    assert formatter._erc20_candidates_from_evidence(
        steps,
        {slot: "0x" + "aa" * 20},
        receipt,
    ) == {storage_token, event_token}
