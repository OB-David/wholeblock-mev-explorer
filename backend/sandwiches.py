"""Strict, TFG-only sandwich detection over one complete block.

The detector intentionally favors precision over recall.  It does not rely on
contract names, ABIs, selectors, or events.  A contract is treated as an
exchange candidate in one transaction only when its transfer-edge net flow has
exactly one positive token balance and one negative token balance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ExchangeOccurrence:
    tx_index: int
    tx_hash: str
    address: str
    token_in: str
    token_out: str
    amount_in_raw: int
    amount_out_raw: int
    input_edge_ids: tuple[str, ...]
    output_edge_ids: tuple[str, ...]
    payer_addresses: tuple[str, ...]
    recipient_addresses: tuple[str, ...]
    direct_participant: str | None

    @property
    def edge_ids(self) -> tuple[str, ...]:
        return (*self.input_edge_ids, *self.output_edge_ids)


def extract_exchange_occurrences(
    edges: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    contract_addresses: set[str],
) -> list[ExchangeOccurrence]:
    """Extract strict two-asset net exchanges at contract addresses."""

    tx_hashes: dict[int, str] = {}
    for position, transaction in enumerate(transactions):
        index = _integer(transaction.get("index"))
        if index is None:
            index = position
        tx_hashes[index] = _normalized(transaction.get("hash"))

    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        if edge.get("kind") != "transfer":
            continue
        tx_index = _integer(edge.get("tx_index"))
        amount = _integer(edge.get("amount_raw"))
        source = _normalized(edge.get("source"))
        target = _normalized(edge.get("target"))
        token = _normalized(edge.get("token_address"))
        edge_id = str(edge.get("id") or "")
        if (
            tx_index is None
            or amount is None
            or amount <= 0
            or not source
            or not target
            or source == target
            or not token
            or not edge_id
        ):
            continue
        normalized_edge = {
            "id": edge_id,
            "order": _integer(edge.get("order")) or 0,
            "source": source,
            "target": target,
            "token": token,
            "amount": amount,
        }
        if source in contract_addresses:
            grouped[(tx_index, source)].append(normalized_edge)
        if target in contract_addresses:
            grouped[(tx_index, target)].append(normalized_edge)

    occurrences: list[ExchangeOccurrence] = []
    for (tx_index, address), incident_edges in grouped.items():
        deltas: dict[str, int] = defaultdict(int)
        for edge in incident_edges:
            if edge["target"] == address:
                deltas[edge["token"]] += edge["amount"]
            if edge["source"] == address:
                deltas[edge["token"]] -= edge["amount"]

        positive = [(token, delta) for token, delta in deltas.items() if delta > 0]
        negative = [(token, delta) for token, delta in deltas.items() if delta < 0]
        if len(positive) != 1 or len(negative) != 1:
            continue
        token_in, amount_in = positive[0]
        token_out, amount_out_delta = negative[0]
        if token_in == token_out:
            continue

        input_edges = sorted(
            (
                edge for edge in incident_edges
                if edge["target"] == address and edge["token"] == token_in
            ),
            key=lambda edge: (edge["order"], edge["id"]),
        )
        output_edges = sorted(
            (
                edge for edge in incident_edges
                if edge["source"] == address and edge["token"] == token_out
            ),
            key=lambda edge: (edge["order"], edge["id"]),
        )
        if not input_edges or not output_edges:
            continue

        payers = tuple(sorted({edge["source"] for edge in input_edges}))
        recipients = tuple(sorted({edge["target"] for edge in output_edges}))
        shared = sorted(set(payers) & set(recipients))
        direct_participant = shared[0] if len(shared) == 1 else None
        occurrences.append(ExchangeOccurrence(
            tx_index=tx_index,
            tx_hash=tx_hashes.get(tx_index, ""),
            address=address,
            token_in=token_in,
            token_out=token_out,
            amount_in_raw=amount_in,
            amount_out_raw=-amount_out_delta,
            input_edge_ids=tuple(edge["id"] for edge in input_edges),
            output_edge_ids=tuple(edge["id"] for edge in output_edges),
            payer_addresses=payers,
            recipient_addresses=recipients,
            direct_participant=direct_participant,
        ))

    return sorted(occurrences, key=lambda item: (
        item.tx_index, item.address, item.token_in, item.token_out, item.edge_ids,
    ))


def detect_sandwiches(
    edges: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    contract_addresses: set[str],
) -> list[dict[str, Any]]:
    """Detect strict front/victim/back motifs from TFG transfer edges only."""

    occurrences = extract_exchange_occurrences(edges, transactions, contract_addresses)
    by_market: dict[tuple[str, tuple[str, str]], list[ExchangeOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        pair = tuple(sorted((occurrence.token_in, occurrence.token_out)))
        by_market[(occurrence.address, pair)].append(occurrence)

    found: list[dict[str, Any]] = []
    for (pool_address, _), market in sorted(by_market.items()):
        distinct_transactions = {item.tx_index for item in market}
        directions = {(item.token_in, item.token_out) for item in market}
        if len(distinct_transactions) < 3 or len(directions) < 2:
            continue

        market.sort(key=lambda item: (item.tx_index, item.edge_ids))
        for front_index, front in enumerate(market):
            attacker = front.direct_participant
            if not attacker:
                continue
            victims: list[ExchangeOccurrence] = []
            for other in market[front_index + 1:]:
                if other.tx_index == front.tx_index:
                    continue
                same_direction = (
                    other.token_in == front.token_in
                    and other.token_out == front.token_out
                )
                reverse_direction = (
                    other.token_in == front.token_out
                    and other.token_out == front.token_in
                )
                if same_direction and attacker not in other.payer_addresses:
                    victims.append(other)
                    continue
                if not reverse_direction or other.direct_participant != attacker or not victims:
                    continue

                victims_before_back = [
                    victim for victim in victims if victim.tx_index < other.tx_index
                ]
                if not victims_before_back:
                    continue
                profit = other.amount_out_raw - front.amount_in_raw
                if profit <= 0:
                    continue
                found.append({
                    "pool_address": pool_address,
                    "attacker_address": attacker,
                    "token_in_address": front.token_in,
                    "token_out_address": front.token_out,
                    "profit_token_address": front.token_in,
                    "gross_profit_amount_raw": str(profit),
                    "front_tx_index": front.tx_index,
                    "front_tx_hash": front.tx_hash,
                    "front_edge_ids": list(front.edge_ids),
                    "victim_tx_indexes": [victim.tx_index for victim in victims_before_back],
                    "victim_tx_hashes": [victim.tx_hash for victim in victims_before_back],
                    "victim_edge_ids": [
                        edge_id
                        for victim in victims_before_back
                        for edge_id in victim.edge_ids
                    ],
                    "back_tx_index": other.tx_index,
                    "back_tx_hash": other.tx_hash,
                    "back_edge_ids": list(other.edge_ids),
                })
                # Match mev-inspect's behavior: the first qualifying backrun
                # closes this frontrun candidate.
                break

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in found:
        key = (
            item["pool_address"], item["attacker_address"], item["front_tx_index"],
            tuple(item["victim_tx_indexes"]), item["back_tx_index"],
            item["token_in_address"], item["token_out_address"],
        )
        unique.setdefault(key, item)
    result = sorted(unique.values(), key=lambda item: (
        item["front_tx_index"], item["back_tx_index"], item["pool_address"],
        item["token_in_address"], item["token_out_address"],
    ))
    for index, item in enumerate(result, 1):
        item["sandwich_id"] = f"sandwich-{index}"
    return result
