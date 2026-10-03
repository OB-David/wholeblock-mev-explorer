import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from analyzer import BlockAnalyzer


def test_analyze_reuses_valid_graph_before_rpc(tmp_path):
    block_number = 25_944_323
    graph = {
        "schema_version": 1,
        "block": {"number": block_number},
        "nodes": [],
        "edges": [],
        "tokens": [],
        "transactions": [{"hash": "0x01"}],
    }
    block_dir = tmp_path / str(block_number)
    block_dir.mkdir()
    (block_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    analyzer = BlockAnalyzer.__new__(BlockAnalyzer)
    analyzer.data_dir = tmp_path
    progress = []

    result = analyzer.analyze(
        block_number,
        lambda completed, total, message: progress.append((completed, total, message)),
    )

    assert result["schema_version"] == 8
    assert result["sandwiches"] == []
    assert result["transactions"][0]["balance_changes"] == {}
    assert result["transactions"][0]["address_cycles"] == []
    assert result["transactions"][0]["token_cycles"] == []
    assert json.loads((block_dir / "graph.json").read_text(encoding="utf-8")) == result
    assert progress == [(1, 1, f"复用区块 {block_number} 的已有分析结果")]


def test_invalid_graph_cache_is_rejected(tmp_path):
    block_number = 25_944_323
    block_dir = tmp_path / str(block_number)
    block_dir.mkdir()
    path = block_dir / "graph.json"
    path.write_text('{"block":{"number":1}}', encoding="utf-8")

    assert BlockAnalyzer._load_cached_graph(path, block_number) is None
