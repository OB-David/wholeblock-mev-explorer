import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from cycles import extract_transaction_cycles


def edge(order, source, target, token, amount):
    return {
        "id": f"0:{order}:transfer", "order": order, "source": source, "target": target,
        "token_address": token, "amount_raw": str(amount), "kind": "transfer",
    }


def test_address_cycles_compose_into_token_cycle():
    edges = [
        edge(0, "a", "b", "x", 100), edge(1, "b", "a", "y", 100),
        edge(2, "a", "c", "y", 100), edge(3, "c", "a", "x", 110),
    ]

    address_cycles, token_cycles = extract_transaction_cycles(edges, {"a", "b", "c"})

    assert len(address_cycles) == 2
    assert token_cycles[0]["token_address_path"] == ["x", "y", "x"]
    assert token_cycles[0]["amount_delta_raw"] == "10"
    assert set(token_cycles[0]["edge_ids"]) == {item["id"] for item in edges}


def test_same_token_two_address_round_trip_is_not_a_cycle_candidate():
    cycles = extract_transaction_cycles([
        edge(0, "a", "b", "x", 10), edge(1, "b", "a", "x", 9),
    ], {"a", "b"})
    assert cycles == ([], [])


def test_address_cycle_search_excludes_eoa_nodes():
    edges = [
        edge(0, "contract-a", "user", "x", 10),
        edge(1, "user", "contract-b", "y", 11),
        edge(2, "contract-b", "contract-a", "x", 12),
    ]

    cycles = extract_transaction_cycles(edges, {"contract-a", "contract-b"})

    assert cycles == ([], [])


def test_token_cycle_requires_two_normalized_token_identities():
    edges = [
        edge(0, "a", "b", "ETH", 10),
        edge(1, "b", "c", "weth", 11),
        edge(2, "c", "a", "ETH", 12),
    ]

    address_cycles, token_cycles = extract_transaction_cycles(edges, {"a", "b", "c"})

    assert len(address_cycles) == 1
    assert token_cycles == []


def test_negative_token_closure_is_retained():
    edges = [
        edge(0, "a", "b", "x", 100),
        edge(1, "b", "a", "y", 90),
        edge(2, "a", "c", "y", 90),
        edge(3, "c", "a", "x", 90),
    ]

    address_cycles, token_cycles = extract_transaction_cycles(edges, {"a", "b", "c"})

    assert len(address_cycles) == 2
    assert len(token_cycles) == 1
    assert token_cycles[0]["token_address_path"] == ["x", "y", "x"]
    assert token_cycles[0]["amount_delta_raw"] == "-10"


def test_token_cycles_do_not_splice_unequal_intermediate_amounts():
    edges = [
        edge(0, "a", "b", "x", 100),
        edge(1, "b", "a", "y", 120),
        edge(2, "a", "c", "y", 100),
        edge(3, "c", "a", "x", 110),
    ]

    address_cycles, token_cycles = extract_transaction_cycles(edges, {"a", "b", "c"})

    assert len(address_cycles) == 2
    assert token_cycles == []


def test_simple_token_cycle_has_distinct_intermediate_tokens():
    edges = [
        edge(0, "a", "b", "x", 100), edge(1, "b", "a", "y", 100),
        edge(2, "a", "c", "y", 100), edge(3, "c", "a", "z", 100),
        edge(4, "a", "d", "z", 100), edge(5, "d", "a", "x", 110),
    ]

    _, token_cycles = extract_transaction_cycles(edges, {"a", "b", "c", "d"})

    assert len(token_cycles) == 1
    assert token_cycles[0]["token_address_path"] == ["x", "y", "z", "x"]
    assert len(set(token_cycles[0]["token_address_path"][:-1])) == 3


def test_figure_eight_and_multi_leaf_cycles_are_returned_as_edge_disjoint_lobes():
    edges = [
        edge(0, "a", "b", "x", 100), edge(1, "b", "a", "y", 100),
        edge(2, "a", "c", "y", 100), edge(3, "c", "a", "x", 110),
        edge(4, "a", "d", "x", 200), edge(5, "d", "a", "z", 200),
        edge(6, "a", "e", "z", 200), edge(7, "e", "a", "x", 190),
        edge(8, "a", "f", "x", 300), edge(9, "f", "a", "w", 300),
        edge(10, "a", "g", "w", 300), edge(11, "g", "a", "x", 305),
    ]

    _, token_cycles = extract_transaction_cycles(
        edges, {"a", "b", "c", "d", "e", "f", "g"}
    )

    assert len(token_cycles) == 3
    assert {tuple(cycle["token_address_path"]) for cycle in token_cycles} == {
        ("x", "y", "x"), ("x", "z", "x"), ("x", "w", "x"),
    }
    assert {cycle["amount_delta_raw"] for cycle in token_cycles} == {"10", "-10", "5"}
    edge_sets = [set(cycle["edge_ids"]) for cycle in token_cycles]
    assert all(not left & right for index, left in enumerate(edge_sets) for right in edge_sets[index + 1:])


def test_split_merge_leaves_close_by_aggregate_token_balance():
    edges = [
        edge(0, "a", "b", "x", 100), edge(1, "b", "a", "y", 100),
        edge(2, "a", "c", "y", 60), edge(3, "c", "a", "z", 60),
        edge(4, "a", "d", "y", 40), edge(5, "d", "a", "w", 40),
        edge(6, "a", "e", "z", 60), edge(7, "e", "a", "x", 65),
        edge(8, "a", "f", "w", 40), edge(9, "f", "a", "x", 40),
    ]

    _, token_cycles = extract_transaction_cycles(
        edges, {"a", "b", "c", "d", "e", "f"}
    )

    assert len(token_cycles) == 1
    cycle = token_cycles[0]
    assert cycle["is_branched"] is True
    assert cycle["amount_delta_raw"] == "5"
    assert set(cycle["edge_ids"]) == {item["id"] for item in edges}
    assert len(cycle["token_branches"]) == 5


def test_token_cycles_are_globally_edge_disjoint_like_native_analyzer():
    # Reduced topology from transaction
    # 0x4140c7a368d39a1d5926d90bece1b93846c450906df956c15a466f043e9d77b5.
    # Three candidate closures share edges 18 and 19. The native analyzer
    # retains the longer composed path with the smaller execution-order gap.
    edges = [
        edge(9, "a", "d", "usdt", 10),
        edge(10, "e", "h", "weth", 80),
        edge(11, "d", "e", "usdt", 10),
        edge(13, "a", "f", "usdt", 10),
        edge(14, "g", "h", "weth", 105),
        edge(15, "f", "g", "usdt", 10),
        edge(16, "a", "j", "usdt", 10),
        edge(17, "j", "h", "weth", 110),
        edge(18, "h", "c", "dai", 100),
        edge(19, "c", "a", "mog", 10),
        edge(20, "h", "k", "weth", 105),
        edge(21, "k", "h", "dai", 105),
    ]

    address_cycles, token_cycles = extract_transaction_cycles(
        edges, {"a", "c", "d", "e", "f", "g", "h", "j", "k"}
    )

    assert len(address_cycles) == 4
    assert len(token_cycles) == 1
    assert set(token_cycles[0]["edge_ids"]) == {
        "0:13:transfer", "0:14:transfer", "0:15:transfer",
        "0:18:transfer", "0:19:transfer", "0:20:transfer", "0:21:transfer",
    }
