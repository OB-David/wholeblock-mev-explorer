import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from sandwiches import detect_sandwiches, extract_exchange_occurrences


def edge(tx_index, order, source, target, token, amount):
    return {
        "id": f"{tx_index}:{order}:transfer",
        "tx_hash": f"tx-{tx_index}",
        "tx_index": tx_index,
        "order": order,
        "source": source,
        "target": target,
        "token_address": token,
        "amount_raw": str(amount),
        "kind": "transfer",
    }


def transactions(count):
    return [{"index": index, "hash": f"tx-{index}"} for index in range(count)]


def test_detects_profitable_strict_tfg_sandwich():
    edges = [
        edge(0, 1, "attacker", "pool", "x", 100),
        edge(0, 2, "pool", "attacker", "y", 90),
        # Unrelated bot behavior must not affect the pool-local occurrence.
        edge(0, 3, "attacker", "tip", "z", 3),
        edge(1, 1, "victim", "pool", "x", 50),
        edge(1, 2, "pool", "next-pool", "y", 40),
        edge(2, 1, "attacker", "pool", "y", 90),
        edge(2, 2, "pool", "attacker", "x", 112),
    ]

    result = detect_sandwiches(edges, transactions(3), {"pool", "tip", "next-pool"})

    assert len(result) == 1
    assert result[0]["pool_address"] == "pool"
    assert result[0]["attacker_address"] == "attacker"
    assert result[0]["victim_tx_indexes"] == [1]
    assert result[0]["gross_profit_amount_raw"] == "12"
    assert result[0]["front_edge_ids"] == ["0:1:transfer", "0:2:transfer"]
    assert result[0]["victim_edge_ids"] == ["1:1:transfer", "1:2:transfer"]
    assert result[0]["back_edge_ids"] == ["2:1:transfer", "2:2:transfer"]


def test_zero_balance_router_is_not_an_exchange_occurrence():
    edges = [
        edge(0, 1, "user", "router", "x", 100),
        edge(0, 2, "router", "pool", "x", 100),
        edge(0, 3, "pool", "router", "y", 90),
        edge(0, 4, "router", "trader", "y", 90),
    ]

    occurrences = extract_exchange_occurrences(
        edges, transactions(1), {"router", "pool"},
    )

    assert [item.address for item in occurrences] == ["pool"]


def test_requires_positive_same_token_gross_profit():
    edges = [
        edge(0, 1, "attacker", "pool", "x", 100),
        edge(0, 2, "pool", "attacker", "y", 90),
        edge(1, 1, "victim", "pool", "x", 50),
        edge(1, 2, "pool", "victim", "y", 40),
        edge(2, 1, "attacker", "pool", "y", 90),
        edge(2, 2, "pool", "attacker", "x", 99),
    ]

    assert detect_sandwiches(edges, transactions(3), {"pool"}) == []
