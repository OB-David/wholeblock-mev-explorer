"""Transaction-level address and token cycle extraction for graph edges."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
import heapq
from itertools import count
from typing import Any


WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
MAX_COMPOSITE_OPTIONS = 16
MAX_COMPOSITE_STATES_PER_SETTLEMENT = 100_000


def _token_identity(value: Any) -> str:
    token = str(value or "").strip().lower()
    return "eth" if token in {"eth", WETH} else token


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source: str
    target: str
    token_address: str
    token_identity: str
    amount_raw: int
    order: int


def _transfer_edges(edges: list[dict[str, Any]]) -> list[Edge]:
    result: list[Edge] = []
    for position, item in enumerate(edges):
        try:
            amount = int(item.get("amount_raw", 0))
            order = int(item.get("order", position))
        except (TypeError, ValueError):
            continue
        source = str(item.get("source", "")).lower()
        target = str(item.get("target", "")).lower()
        token = str(item.get("token_address", "")).lower()
        edge_id = str(item.get("id", ""))
        if (
            item.get("kind") != "transfer" or not edge_id or not source or not target
            or source == target or not token or amount <= 0
        ):
            continue
        result.append(Edge(edge_id, source, target, token, _token_identity(token), amount, order))
    return sorted(result, key=lambda edge: (edge.order, edge.edge_id))


def extract_address_cycles(
    edges: list[dict[str, Any]], contract_addresses: set[str]
) -> list[dict[str, Any]]:
    """Enumerate elementary directed contract-address cycles.

    Mint/burn edges and transfers incident to an EOA never participate.
    """

    contracts = {str(address).lower() for address in contract_addresses}
    transfers = [
        edge for edge in _transfer_edges(edges)
        if edge.source in contracts and edge.target in contracts
    ]
    adjacency: dict[str, list[Edge]] = defaultdict(list)
    for edge in transfers:
        adjacency[edge.source].append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda edge: (edge.target, edge.order, edge.edge_id))

    raw: list[tuple[list[str], list[Edge]]] = []
    for start in sorted({edge.source for edge in transfers}):
        visited = {start}

        def walk(node: str, path_nodes: list[str], path_edges: list[Edge]) -> None:
            for edge in adjacency.get(node, []):
                target = edge.target
                if target == start:
                    if path_edges:
                        raw.append(([*path_nodes, start], [*path_edges, edge]))
                    continue
                # Requiring start to be the smallest member removes rotations.
                if target < start or target in visited:
                    continue
                visited.add(target)
                walk(target, [*path_nodes, target], [*path_edges, edge])
                visited.remove(target)

        walk(start, [start], [])

    cycles: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for nodes, path in sorted(raw, key=lambda item: (len(item[1]), tuple(e.order for e in item[1]))):
        if len(path) == 2 and path[0].token_address == path[1].token_address:
            continue
        signature = tuple(edge.edge_id for edge in path)
        if signature in seen:
            continue
        seen.add(signature)
        cycles.append({
            "cycle_id": f"address-cycle-{len(cycles) + 1}",
            "nodes": nodes,
            "edge_ids": list(signature),
            "token_address_path": [edge.token_address for edge in path],
            "edge_count": len(path),
        })
    return cycles


def _anchor_options(cycles: list[dict[str, Any]], by_id: dict[str, Edge]) -> list[dict[str, Any]]:
    options = []
    for cycle in cycles:
        nodes = cycle["nodes"][:-1]
        path = [by_id[edge_id] for edge_id in cycle["edge_ids"]]
        for index, anchor in enumerate(nodes):
            rotation = list(range(index, len(nodes))) + list(range(index))
            options.append({
                "cycle_id": cycle["cycle_id"],
                "anchor": anchor,
                "token_in": path[index].token_address,
                "identity_in": path[index].token_identity,
                "amount_in": path[index].amount_raw,
                "token_out": path[index - 1].token_address,
                "identity_out": path[index - 1].token_identity,
                "amount_out": path[index - 1].amount_raw,
                "edge_ids": cycle["edge_ids"],
                "rotated_edge_ids": [path[position].edge_id for position in rotation],
            })
    return options


def _execution_order_gap(item: dict[str, Any], by_id: dict[str, Edge]) -> int:
    """Count missing transfer orders inside one candidate's execution span."""

    orders = sorted({by_id[edge_id].order for edge_id in item["edge_ids"]})
    return sum(
        max(0, current - previous - 1)
        for previous, current in zip(orders, orders[1:])
    )


def _select_edge_disjoint_cycles(
    candidates: list[dict[str, Any]], by_id: dict[str, Edge]
) -> list[dict[str, Any]]:
    """Port of the native analyzer's exact set-packing selection.

    Across all candidate token closures, enforce globally edge-disjoint output.
    Prefer dense execution spans, boundary-coherent nested paths, broad edge
    coverage, and plausible positive profit ratios. Conflict-connected
    components are independent.
    """

    if not candidates:
        return []

    edge_sets = [frozenset(item["edge_ids"]) for item in candidates]
    gaps = [_execution_order_gap(item, by_id) for item in candidates]
    boundary_penalties = [int(item["_boundary_span_penalty"]) for item in candidates]
    profit_ratios = [
        Fraction(abs(int(item["amount_delta_raw"])), int(item["_amount_in_raw"]))
        for item in candidates
    ]
    conflicts: list[set[int]] = [set() for _ in candidates]
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if edge_sets[left] & edge_sets[right]:
                conflicts[left].add(right)
                conflicts[right].add(left)

    components: list[list[int]] = []
    unseen = set(range(len(candidates)))
    while unseen:
        start = min(unseen)
        stack = [start]
        component: list[int] = []
        unseen.remove(start)
        while stack:
            index = stack.pop()
            component.append(index)
            neighbours = conflicts[index] & unseen
            unseen.difference_update(neighbours)
            stack.extend(sorted(neighbours, reverse=True))
        components.append(sorted(component))

    def better(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
        left_covered = sum(len(edge_sets[index]) for index in left)
        right_covered = sum(len(edge_sets[index]) for index in right)
        left_gaps = sum(gaps[index] for index in left)
        right_gaps = sum(gaps[index] for index in right)
        left_profit_ratio = sum((profit_ratios[index] for index in left), Fraction())
        right_profit_ratio = sum((profit_ratios[index] for index in right), Fraction())
        left_boundary_penalty = sum(boundary_penalties[index] for index in left)
        right_boundary_penalty = sum(boundary_penalties[index] for index in right)
        left_score = (
            left_covered - left_gaps,
            -left_boundary_penalty,
            left_covered,
            -left_gaps,
            -left_profit_ratio,
            -len(left),
        )
        right_score = (
            right_covered - right_gaps,
            -right_boundary_penalty,
            right_covered,
            -right_gaps,
            -right_profit_ratio,
            -len(right),
        )
        if left_score != right_score:
            return left_score > right_score
        return left < right

    selected: list[int] = []
    for component in components:
        edge_universe = sorted({edge for index in component for edge in edge_sets[index]})
        edge_bits = {edge: bit for bit, edge in enumerate(edge_universe)}
        masks = {
            index: sum(1 << edge_bits[edge] for edge in edge_sets[index])
            for index in component
        }
        memo: dict[tuple[int, int], tuple[int, ...]] = {}

        def solve(position: int, used_mask: int) -> tuple[int, ...]:
            key = (position, used_mask)
            cached = memo.get(key)
            if cached is not None:
                return cached
            if position == len(component):
                return ()

            index = component[position]
            best = solve(position + 1, used_mask)
            if masks[index] & used_mask == 0:
                included = (index, *solve(position + 1, used_mask | masks[index]))
                if better(included, best):
                    best = included
            memo[key] = best
            return best

        selected.extend(solve(0, 0))

    return [candidates[index] for index in sorted(selected)]


def _branched_balance_candidates(
    options: list[dict[str, Any]], by_id: dict[str, Edge]
) -> list[dict[str, Any]]:
    """Find split/merge closures by conserving every intermediate asset.

    Each option is one address-cycle transformation at a shared anchor. A
    valid composite has exactly one non-zero token balance (the settlement
    asset); every intermediate token must sum to zero across all branches.
    """

    by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for option in options:
        if option["identity_in"] != option["identity_out"]:
            by_anchor[option["anchor"]].append(option)

    found: list[dict[str, Any]] = []
    for anchor, anchor_options in sorted(by_anchor.items()):
        anchor_options.sort(key=lambda item: (
            min(by_id[edge_id].order for edge_id in item["edge_ids"]),
            item["cycle_id"], item["identity_in"], item["identity_out"],
        ))
        consumers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        producers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for option in anchor_options:
            consumers[option["identity_in"]].append(option)
            producers[option["identity_out"]].append(option)

        settlement_tokens = sorted(set(consumers) & set(producers))
        for settlement in settlement_tokens:
            visited: set[tuple[frozenset[str], tuple[tuple[str, int], ...]]] = set()
            state_count = 0

            def walk(
                path: tuple[dict[str, Any], ...],
                used_cycles: frozenset[str],
                used_edges: frozenset[str],
                balances: dict[str, int],
            ) -> None:
                nonlocal state_count
                if state_count >= MAX_COMPOSITE_STATES_PER_SETTLEMENT:
                    return
                state_count += 1
                normalized_balances = tuple(sorted(
                    (token, amount) for token, amount in balances.items() if amount
                ))
                state_key = (used_cycles, normalized_balances)
                if state_key in visited:
                    return
                visited.add(state_key)

                internal = [
                    (token, amount) for token, amount in normalized_balances
                    if token != settlement
                ]
                if not internal:
                    settlement_delta = balances.get(settlement, 0)
                    has_input = any(item["identity_in"] == settlement for item in path)
                    has_output = any(item["identity_out"] == settlement for item in path)
                    identities = {
                        identity
                        for item in path
                        for identity in (item["identity_in"], item["identity_out"])
                    }
                    input_counts: dict[str, int] = defaultdict(int)
                    output_counts: dict[str, int] = defaultdict(int)
                    for item in path:
                        input_counts[item["identity_in"]] += 1
                        output_counts[item["identity_out"]] += 1
                    is_branched = any(
                        input_counts[token] > 1 or output_counts[token] > 1
                        for token in identities
                    )
                    if (
                        settlement_delta and has_input and has_output
                        and len(identities) >= 2 and is_branched
                    ):
                        ordered = sorted(path, key=lambda item: (
                            min(by_id[edge_id].order for edge_id in item["edge_ids"]),
                            item["cycle_id"],
                        ))
                        settlement_address = next(
                            item["token_in"] for item in ordered
                            if item["identity_in"] == settlement
                        )
                        other_addresses: list[str] = []
                        for item in ordered:
                            for address, identity in (
                                (item["token_in"], item["identity_in"]),
                                (item["token_out"], item["identity_out"]),
                            ):
                                if identity != settlement and address not in other_addresses:
                                    other_addresses.append(address)
                        edge_ids = [
                            edge_id for item in ordered
                            for edge_id in item["rotated_edge_ids"]
                        ]
                        found.append({
                            "anchor_address": anchor,
                            "token_address_path": [
                                settlement_address, *other_addresses, settlement_address,
                            ],
                            "token_branches": [{
                                "token_in_address": item["token_in"],
                                "token_out_address": item["token_out"],
                                "amount_in_raw": str(item["amount_in"]),
                                "amount_out_raw": str(item["amount_out"]),
                            } for item in ordered],
                            "is_branched": True,
                            "address_cycle_ids": [item["cycle_id"] for item in ordered],
                            "edge_ids": edge_ids,
                            "amount_delta_raw": str(settlement_delta),
                            "_amount_in_raw": str(sum(
                                item["amount_in"] for item in ordered
                                if item["identity_in"] == settlement
                            )),
                            "_boundary_span_penalty": 0,
                            "edge_count": len(used_edges),
                            "address_cycle_count": len(ordered),
                        })
                    return

                if len(path) >= MAX_COMPOSITE_OPTIONS:
                    return

                choices_by_token: list[tuple[int, str, list[dict[str, Any]]]] = []
                for token, amount in internal:
                    candidates = consumers[token] if amount > 0 else producers[token]
                    usable = []
                    for option in candidates:
                        edge_set = frozenset(option["edge_ids"])
                        if option["cycle_id"] in used_cycles or edge_set & used_edges:
                            continue
                        next_amount = amount + (
                            option["amount_out"] if option["identity_out"] == token else 0
                        ) - (
                            option["amount_in"] if option["identity_in"] == token else 0
                        )
                        if abs(next_amount) <= abs(amount):
                            usable.append(option)
                    choices_by_token.append((len(usable), token, usable))
                _, _, next_options = min(choices_by_token, key=lambda item: (item[0], item[1]))
                for option in next_options:
                    next_balances = dict(balances)
                    next_balances[option["identity_in"]] = (
                        next_balances.get(option["identity_in"], 0) - option["amount_in"]
                    )
                    next_balances[option["identity_out"]] = (
                        next_balances.get(option["identity_out"], 0) + option["amount_out"]
                    )
                    edge_set = frozenset(option["edge_ids"])
                    walk(
                        (*path, option),
                        used_cycles | {option["cycle_id"]},
                        used_edges | edge_set,
                        next_balances,
                    )

            for seed in consumers[settlement]:
                if seed["identity_out"] == settlement:
                    continue
                seed_edges = frozenset(seed["edge_ids"])
                walk(
                    (seed,), frozenset({seed["cycle_id"]}), seed_edges,
                    {
                        seed["identity_in"]: -seed["amount_in"],
                        seed["identity_out"]: seed["amount_out"],
                    },
                )
    return found


def extract_token_cycles(
    edges: list[dict[str, Any]], address_cycles: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Compose address cycles into edge-disjoint token closures at one address."""

    transfers = _transfer_edges(edges)
    by_id = {edge.edge_id: edge for edge in transfers}
    options = _anchor_options(address_cycles, by_id)
    choices: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    starts: dict[str, set[str]] = defaultdict(set)
    for option in options:
        choices[(option["anchor"], option["identity_in"])].append(option)
        starts[option["anchor"]].add(option["identity_in"])

    found: list[dict[str, Any]] = []
    serial = count()
    for anchor in sorted(starts):
        for start_token in sorted(starts[anchor]):
            queue: list[tuple[Any, ...]] = [(0, 0, next(serial), start_token, (), frozenset(), frozenset(), (start_token,))]
            while queue:
                edge_count, cycle_count, _, current, path, used_cycles, used_edges, token_path = heapq.heappop(queue)
                for option in choices.get((anchor, current), []):
                    edge_set = frozenset(option["edge_ids"])
                    if option["cycle_id"] in used_cycles or edge_set & used_edges:
                        continue
                    # At the anchor, an intermediate asset must flow through
                    # unchanged. Token-only matching can otherwise splice
                    # transfers from separate swaps that revisit the same
                    # contracts in one transaction.
                    if path and option["amount_in"] != path[-1]["amount_out"]:
                        continue
                    next_path = (*path, option)
                    next_token = option["identity_out"]
                    next_edges = used_edges | edge_set
                    if next_token == start_token:
                        # A token closure must contain an actual asset exchange.
                        # ETH and WETH share one identity, so wrapping/unwrapping
                        # alone is not classified as an arbitrage token cycle.
                        if len(set((*token_path, next_token))) < 2:
                            continue
                        delta = next_path[-1]["amount_out"] - next_path[0]["amount_in"]
                        if delta == 0:
                            continue
                        spans = [
                            (
                                min(by_id[edge_id].order for edge_id in item["edge_ids"]),
                                max(by_id[edge_id].order for edge_id in item["edge_ids"]),
                            )
                            for item in next_path
                        ]
                        boundary_low = min(spans[0][0], spans[-1][0])
                        boundary_high = max(spans[0][1], spans[-1][1])
                        overall_low = min(span[0] for span in spans)
                        overall_high = max(span[1] for span in spans)
                        found.append({
                            "anchor_address": anchor,
                            "token_address_path": [next_path[0]["token_in"], *[item["token_out"] for item in next_path]],
                            "token_branches": [{
                                "token_in_address": item["token_in"],
                                "token_out_address": item["token_out"],
                                "amount_in_raw": str(item["amount_in"]),
                                "amount_out_raw": str(item["amount_out"]),
                            } for item in next_path],
                            "is_branched": False,
                            "address_cycle_ids": [item["cycle_id"] for item in next_path],
                            "edge_ids": [edge_id for item in next_path for edge_id in item["rotated_edge_ids"]],
                            "amount_delta_raw": str(delta),
                            "_amount_in_raw": str(next_path[0]["amount_in"]),
                            "_boundary_span_penalty": (
                                boundary_low - overall_low + overall_high - boundary_high
                            ),
                            "edge_count": len(next_edges),
                            "address_cycle_count": cycle_count + 1,
                        })
                        continue
                    if next_token in token_path:
                        continue
                    heapq.heappush(queue, (
                        len(next_edges), cycle_count + 1, next(serial), next_token, next_path,
                        used_cycles | {option["cycle_id"]}, next_edges, (*token_path, next_token),
                    ))

    linear_edge_sets = [frozenset(item["edge_ids"]) for item in found]
    for candidate in _branched_balance_candidates(options, by_id):
        candidate_edges = frozenset(candidate["edge_ids"])
        # Do not merge an already closed simple lobe into a larger aggregate.
        # Composite balance closure is only needed when open branches must be
        # settled together.
        if any(edge_set < candidate_edges for edge_set in linear_edge_sets):
            continue
        found.append(candidate)

    unique: dict[tuple[str, frozenset[str]], dict[str, Any]] = {}
    for item in found:
        key = (item["anchor_address"], frozenset(item["edge_ids"]))
        old = unique.get(key)
        if old is None or (item["edge_count"], item["address_cycle_count"], item["edge_ids"]) < (
            old["edge_count"], old["address_cycle_count"], old["edge_ids"]
        ):
            unique[key] = item
    result = sorted(unique.values(), key=lambda item: (
        item["edge_count"], item["address_cycle_count"], item["anchor_address"], item["edge_ids"]
    ))
    result = _select_edge_disjoint_cycles(result, by_id)
    for index, item in enumerate(result, 1):
        item["cycle_id"] = f"token-cycle-{index}"
        item.pop("_amount_in_raw", None)
        item.pop("_boundary_span_penalty", None)
    return result


def extract_transaction_cycles(
    edges: list[dict[str, Any]], contract_addresses: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    address_cycles = extract_address_cycles(edges, contract_addresses)
    return address_cycles, extract_token_cycles(edges, address_cycles)
