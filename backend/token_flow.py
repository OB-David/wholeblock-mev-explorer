"""Token-only extraction copied from the transaction analyzer's storage-delta model."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def format_raw_units(raw_value: int | str, decimals: int) -> str:
    """Format base units exactly, without passing through a float."""

    value = int(raw_value)
    places = max(0, int(decimals))
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    if places == 0:
        return sign + digits
    digits = digits.zfill(places + 1)
    whole = digits[:-places]
    fraction = digits[-places:].rstrip("0")
    return sign + whole + (f".{fraction}" if fraction else "")


def enrich_flow_amounts(flows: list[dict[str, Any]]) -> None:
    """Add a precise, human-readable amount to every graph edge in place."""

    for flow in flows:
        flow["amount"] = format_raw_units(
            flow.get("amount_raw", "0"), int(flow.get("decimals", 18))
        )


def transaction_balance_changes(flows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return each address's final per-asset net change for one transaction."""

    totals: dict[tuple[str, str], int] = defaultdict(int)
    metadata: dict[str, tuple[str, int]] = {}
    for flow in flows:
        token_address = str(flow.get("token_address", ""))
        symbol = str(flow.get("token_symbol", "Unknown"))
        decimals = int(flow.get("decimals", 18))
        amount = abs(int(flow.get("amount_raw", 0)))
        source = str(flow.get("source", "")).lower()
        target = str(flow.get("target", "")).lower()
        kind = str(flow.get("kind", "transfer"))
        metadata[token_address] = (symbol, decimals)
        if kind != "mint" and source:
            totals[(source, token_address)] -= amount
        if kind != "burn" and target:
            totals[(target, token_address)] += amount

    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (address, token_address), raw in sorted(totals.items()):
        if raw == 0:
            continue
        symbol, decimals = metadata[token_address]
        result[address].append({
            "token_address": token_address,
            "token_symbol": symbol,
            "decimals": decimals,
            "amount_raw": str(raw),
            "amount": format_raw_units(raw, decimals),
        })
    return dict(result)


def _hex_int(value: Any) -> int:
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return 0


def extract_balance_changes(
    steps: list[dict[str, Any]],
    slot_map: dict[str, str],
    token_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Rebuild ETH transfers and ERC-20 balance deltas from opcode evidence.

    This is the token-only portion of ``CFGConstructor.construct_cfg``.  It
    intentionally keeps the same SLOAD -> SSTORE pairing semantics without
    constructing bytecode blocks or a CFG.
    """

    changes: list[dict[str, Any]] = []
    traces: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"value": None, "pc": None, "step": None}
    )
    slots = {str(key).lower(): value.lower() for key, value in slot_map.items()}
    tokens = {str(key).lower(): value for key, value in token_map.items()}

    for index, step in enumerate(steps):
        opcode = str(step.get("opcode", "")).upper()
        stack = step.get("stack") or []
        code_address = str(step.get("address") or "").lower()
        storage_address = str(step.get("RW_address") or code_address).lower()

        if opcode == "CALL" and len(stack) >= 3:
            value = _hex_int(stack[-3])
            if value:
                target = "0x" + str(stack[-2]).lower().removeprefix("0x")[-40:].zfill(40)
                changes.append({
                    "type": "ETH_TRANSFER",
                    "codecontract_address": code_address,
                    "from_address": storage_address,
                    "to_address": target,
                    "eth_value": str(value),
                    "pc": step.get("pc"),
                    "step": index,
                })

        if opcode == "SLOAD" and stack:
            slot = str(stack[-1]).lower()
            holder = slots.get(slot)
            token_name = tokens.get(storage_address)
            if holder and token_name:
                result = step.get("result")
                if result is None and index + 1 < len(steps):
                    # Backward compatibility for old full-opcode traces.
                    next_stack = steps[index + 1].get("stack", []) or []
                    result = next_stack[-1] if next_stack else None
                value = _hex_int(result)
                traces[(storage_address, holder)] = {
                    "value": value,
                    "pc": step.get("pc"),
                    "step": index,
                }

        if opcode == "SSTORE" and len(stack) >= 2:
            slot = str(stack[-1]).lower()
            holder = slots.get(slot)
            token_name = tokens.get(storage_address)
            previous = traces[(storage_address, holder)] if holder else None
            if holder and token_name and previous and previous["value"] is not None:
                new_value = _hex_int(stack[-2])
                delta = new_value - int(previous["value"])
                if delta:
                    changes.append({
                        "type": "ERC20_BALANCE_CHANGE",
                        "codecontract_address": code_address,
                        "erc20_token_address": storage_address,
                        "token_name": token_name,
                        "user_address": holder,
                        "changed_balance": str(delta),
                        "SLOAD_pc": previous["pc"],
                        "SLOAD_step": previous["step"],
                        "SSTORE_pc": step.get("pc"),
                        "SSTORE_step": index,
                    })
                traces[(storage_address, holder)] = {"value": None, "pc": None, "step": None}

    return changes


def pair_token_changes(
    original_transfer: tuple[str, str | None, int],
    all_changes: list[dict[str, Any]],
    decimals_map: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair equal-and-opposite balance deltas using the analyzer's ordering model."""

    paired: list[dict[str, Any]] = []
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sender, receiver, original_value = original_transfer
    order = 0
    if original_value and receiver:
        paired.append(_transfer(order, sender, receiver, original_value, "ETH", "ETH", 18, "transfer"))

    def event_step(change: dict[str, Any]) -> int:
        key = "step" if change.get("type") == "ETH_TRANSFER" else "SSTORE_step"
        try:
            return int(change.get(key))
        except (TypeError, ValueError):
            return 10**30

    for change in sorted(enumerate(all_changes), key=lambda item: (event_step(item[1]), item[0])):
        item = change[1]
        order += 1
        if item["type"] == "ETH_TRANSFER":
            paired.append(_transfer(
                order,
                item["from_address"],
                item["to_address"],
                abs(int(item["eth_value"])),
                "ETH",
                "ETH",
                18,
                "transfer",
            ))
            continue
        if item["type"] != "ERC20_BALANCE_CHANGE":
            continue
        token = str(item["erc20_token_address"]).lower()
        current = {
            "order": order,
            "user": str(item["user_address"]).lower(),
            "value": int(item["changed_balance"]),
            "token": item["token_name"],
            "token_addr": token,
            "decimals": int(decimals_map.get(token, 18)),
        }
        queue = pending[token]
        match_index = next(
            (i for i in range(len(queue) - 1, -1, -1) if queue[i]["value"] + current["value"] == 0),
            None,
        )
        if match_index is None:
            queue.append(current)
            continue
        previous = queue.pop(match_index)
        negative, positive = (previous, current) if previous["value"] < 0 else (current, previous)
        if negative["user"] != positive["user"]:
            paired.append(_transfer(
                min(previous["order"], current["order"]),
                negative["user"],
                positive["user"],
                abs(current["value"]),
                current["token"],
                token,
                current["decimals"],
                "transfer",
            ))

    unpaired = [item for queue in pending.values() for item in queue if item["value"]]
    combined = [(item["order"], item, "paired") for item in paired]
    combined += [(item["order"], item, "pending") for item in unpaired]
    combined.sort(key=lambda entry: entry[0])
    next_order = 0 if original_value and receiver else 1
    for _, item, _ in combined:
        item["order"] = next_order
        next_order += 1
    return sorted(paired, key=lambda item: item["order"]), sorted(unpaired, key=lambda item: item["order"])


def _transfer(
    order: int,
    source: str,
    target: str,
    amount_raw: int,
    symbol: str,
    token_address: str,
    decimals: int,
    kind: str,
) -> dict[str, Any]:
    return {
        "order": order,
        "from": str(source).lower(),
        "to": str(target).lower(),
        "amount_raw": str(abs(int(amount_raw))),
        "token": symbol,
        "token_addr": token_address,
        "decimals": decimals,
        "kind": kind,
    }


def flows_from_pairs(
    tx_hash: str,
    tx_index: int,
    paired: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = []
    for item in paired:
        flows.append({
            "id": f"{tx_index}:{item['order']}:transfer",
            "tx_hash": tx_hash,
            "tx_index": tx_index,
            "order": item["order"],
            "source": item["from"],
            "target": item["to"],
            "token_address": item["token_addr"],
            "token_symbol": item["token"],
            "decimals": item["decimals"],
            "amount_raw": item["amount_raw"],
            "kind": "transfer",
        })
    for item in pending:
        positive = item["value"] > 0
        token = item["token_addr"]
        holder = item["user"]
        flows.append({
            "id": f"{tx_index}:{item['order']}:{'mint' if positive else 'burn'}",
            "tx_hash": tx_hash,
            "tx_index": tx_index,
            "order": item["order"],
            "source": token if positive else holder,
            "target": holder if positive else token,
            "token_address": token,
            "token_symbol": item["token"],
            "decimals": item["decimals"],
            "amount_raw": str(abs(item["value"])),
            "kind": "mint" if positive else "burn",
        })
    return sorted(flows, key=lambda item: item["order"])
