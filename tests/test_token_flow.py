import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from analyzer import spreadsheet_suffix, token_color
from token_flow import (
    enrich_flow_amounts,
    extract_balance_changes,
    format_raw_units,
    pair_token_changes,
    transaction_balance_changes,
)


def test_block_alias_suffixes_are_unbounded_and_stable():
    assert [spreadsheet_suffix(i) for i in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]


def test_token_colors_are_unique_for_large_block_palette():
    colors = [token_color(i) for i in range(200)]
    assert len(colors) == len(set(colors))


def test_storage_deltas_pair_into_one_transfer():
    token = "0x" + "11" * 20
    alice = "0x" + "aa" * 20
    bob = "0x" + "bb" * 20
    slot_a, slot_b = "0x01", "0x02"
    steps = [
        {"opcode":"SLOAD","stack":[slot_a],"address":token,"RW_address":token,"pc":"0x1"},
        {"opcode":"PUSH1","stack":["0x64"],"address":token,"RW_address":token,"pc":"0x2"},
        {"opcode":"SSTORE","stack":["0x5a",slot_a],"address":token,"RW_address":token,"pc":"0x3"},
        {"opcode":"SLOAD","stack":[slot_b],"address":token,"RW_address":token,"pc":"0x4"},
        {"opcode":"PUSH1","stack":["0x0"],"address":token,"RW_address":token,"pc":"0x5"},
        {"opcode":"SSTORE","stack":["0x0a",slot_b],"address":token,"RW_address":token,"pc":"0x6"},
    ]
    changes = extract_balance_changes(steps, {slot_a:alice,slot_b:bob}, {token:"TOK"})
    paired, pending = pair_token_changes((alice, token, 0), changes, {token:0})
    assert pending == []
    assert paired[0]["from"] == alice
    assert paired[0]["to"] == bob
    assert paired[0]["amount_raw"] == "10"


def test_amounts_are_formatted_exactly_for_token_decimals():
    assert format_raw_units("20165655946", 6) == "20165.655946"
    assert format_raw_units("20096643502479527", 18) == "0.020096643502479527"
    assert format_raw_units("-1200000", 6) == "-1.2"


def test_transaction_balance_changes_handle_transfer_mint_and_burn():
    alice, bob, token = "0xalice", "0xbob", "0xtoken"
    flows = [
        {"source": alice, "target": bob, "token_address": token, "token_symbol": "USD", "decimals": 6, "amount_raw": "2500000", "kind": "transfer"},
        {"source": token, "target": alice, "token_address": token, "token_symbol": "USD", "decimals": 6, "amount_raw": "500000", "kind": "mint"},
        {"source": bob, "target": token, "token_address": token, "token_symbol": "USD", "decimals": 6, "amount_raw": "1000000", "kind": "burn"},
    ]
    enrich_flow_amounts(flows)
    changes = transaction_balance_changes(flows)

    assert [flow["amount"] for flow in flows] == ["2.5", "0.5", "1"]
    assert changes[alice][0]["amount"] == "-2"
    assert changes[bob][0]["amount"] == "1.5"
