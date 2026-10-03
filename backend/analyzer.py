from __future__ import annotations

import gzip
import json
import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from web3 import Web3

from cycles import extract_transaction_cycles
from sandwiches import detect_sandwiches
from token_flow import (
    enrich_flow_amounts,
    extract_balance_changes,
    flows_from_pairs,
    pair_token_changes,
    transaction_balance_changes,
)
from utils.evm_information import (
    GETH_TRACE_START_BLOCK,
    HistoricalStateUnavailableError,
    NoOpcodeTraceError,
    TraceFormatter,
)


Progress = Callable[[int, int, str], None]
TraceWrite = Callable[[Path, dict[str, Any]], None]
logger = logging.getLogger(__name__)
GRAPH_SCHEMA_VERSION = 8


def enrich_graph_schema(graph: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an existing graph from its saved flows, without fetching traces."""

    edges = graph.get("edges", [])
    transactions = graph.get("transactions", [])
    enrich_flow_amounts(edges)
    contract_addresses = {
        str(node.get("id", "")).lower()
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("kind") == "contract"
    }
    edges_by_tx: dict[int, list[dict[str, Any]]] = {}
    for edge in edges:
        try:
            index = int(edge.get("tx_index"))
        except (TypeError, ValueError):
            continue
        edges_by_tx.setdefault(index, []).append(edge)
    for position, transaction in enumerate(transactions):
        try:
            index = int(transaction.get("index", position))
        except (TypeError, ValueError):
            index = position
        transaction["index"] = index
        tx_edges = sorted(edges_by_tx.get(index, []), key=lambda edge: int(edge.get("order", 0)))
        transaction["edge_ids"] = [edge["id"] for edge in tx_edges]
        transaction["edge_count"] = len(tx_edges)
        transaction["balance_changes"] = transaction_balance_changes(tx_edges)
        address_cycles, token_cycles = extract_transaction_cycles(tx_edges, contract_addresses)
        transaction["address_cycles"] = address_cycles
        transaction["token_cycles"] = token_cycles
    graph["sandwiches"] = detect_sandwiches(edges, transactions, contract_addresses)
    graph["schema_version"] = GRAPH_SCHEMA_VERSION
    return graph


def write_graph(path: Path, graph: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_graph_file(path: Path, block_number: int) -> dict[str, Any] | None:
    """Load, validate, and transparently migrate one cached graph file."""

    if not path.is_file():
        return None
    try:
        original = path.read_text(encoding="utf-8")
        graph = json.loads(original)
        if not isinstance(graph, dict):
            raise ValueError("根节点不是对象")
        block = graph.get("block")
        if not isinstance(block, dict) or int(block.get("number", -1)) != block_number:
            raise ValueError("区块号不匹配")
        for key in ("nodes", "edges", "tokens", "transactions"):
            if not isinstance(graph.get(key), list):
                raise ValueError(f"字段 {key} 不是数组")
        needs_upgrade = (
            graph.get("schema_version") != GRAPH_SCHEMA_VERSION
            or any("amount" not in edge for edge in graph["edges"])
            or any(
                any(key not in transaction for key in ("balance_changes", "address_cycles", "token_cycles"))
                for transaction in graph["transactions"]
            )
        )
        if needs_upgrade:
            enrich_graph_schema(graph)
            write_graph(path, graph)
        logger.info("复用已有区块分析结果: %s", path)
        return graph
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("缓存 %s 无效，将重新分析: %s", path, exc)
        return None


def spreadsheet_suffix(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def token_color(index: int) -> str:
    hue = (index * 137.508 + 205) % 360
    return f"hsl({hue:.1f} 72% 52%)"


@dataclass
class TxResult:
    index: int
    tx_hash: str
    sender: str
    target: str | None
    status: str
    flows: list[dict[str, Any]]
    contracts: set[str]
    users: set[str]
    token_meta: dict[str, dict[str, Any]]
    step_count: int
    error: str | None = None


class BlockAnalyzer:
    def __init__(self, rpc_url: str, data_dir: Path, workers: int = 2):
        self.rpc_url = rpc_url.strip()
        self.data_dir = data_dir
        self.workers = max(1, min(workers, 8))
        self.web3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 60}))
        if not self.web3.is_connected():
            raise ConnectionError("无法连接 GETH_API")

    def resolve_block(self, value: str | int) -> int:
        if str(value).lower() == "latest":
            return int(self.web3.eth.block_number)
        number = int(value)
        if number < 0:
            raise ValueError("区块号不能为负数")
        return number

    def analyze(
        self,
        value: str | int,
        progress: Progress | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        block_number = self.resolve_block(value)
        block_dir = self.data_dir / str(block_number)
        destination = block_dir / "graph.json"
        if not force:
            cached = self._load_cached_graph(destination, block_number)
            if cached is not None:
                total = len(cached["transactions"])
                if progress:
                    progress(total, total, f"复用区块 {block_number} 的已有分析结果")
                return cached

        if block_number < GETH_TRACE_START_BLOCK:
            raise HistoricalStateUnavailableError(
                f"区块 {block_number} 早于原生节点历史 state 分界区块 "
                f"{GETH_TRACE_START_BLOCK}，无法分析"
            )
        block = self.web3.eth.get_block(block_number, full_transactions=True)
        trace_dir = block_dir / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        transactions = list(block.transactions)
        receipts_by_hash: dict[str, Any] = {}
        try:
            receipts = self.web3.eth.get_block_receipts(block_number)
            receipts_by_hash = {
                Web3.to_hex(receipt["transactionHash"]): receipt
                for receipt in receipts
            }
        except Exception as exc:
            logger.warning(
                "批量获取区块 %d receipts 失败，将按交易查询: %s",
                block_number,
                exc,
            )
        formatter = TraceFormatter(self.rpc_url)
        total = len(transactions)
        results: list[TxResult] = []
        lock = threading.Lock()
        completed = 0

        def report(message: str) -> None:
            if progress:
                progress(completed, total, message)

        report("开始抓取整块 opcode trace")
        try:
            block_traces = formatter.fetch_block_traces(block_number)
            if len(block_traces) != total:
                logger.warning(
                    "区块 %d 共 %d 笔交易，整块 trace 返回 %d 笔；缺失项将单独抓取",
                    block_number,
                    total,
                    len(block_traces),
                )
            report("整块 opcode trace 已抓取，开始提取 TFG")
        except Exception as exc:
            logger.warning(
                "整块 trace 失败，将回退为逐交易抓取: block=%d error=%s",
                block_number,
                exc,
            )
            block_traces = {}
        write_futures = []
        write_lock = threading.Lock()
        write_slots = threading.Semaphore(max(4, self.workers * 2))
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="trace-write") as write_pool:
            def schedule_trace_write(path: Path, trace: dict[str, Any]) -> None:
                # Backpressure bounds the number of full trace objects waiting
                # for compression while keeping gzip work off trace workers.
                write_slots.acquire()
                future = write_pool.submit(self._write_trace, path, trace)
                future.add_done_callback(lambda _: write_slots.release())
                with write_lock:
                    write_futures.append(future)

            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="trace") as pool:
                futures = {
                    pool.submit(
                        self._analyze_transaction,
                        tx,
                        index,
                        trace_dir,
                        formatter,
                        receipts_by_hash.get(Web3.to_hex(tx["hash"])),
                        block_traces.get(Web3.to_hex(tx["hash"]).lower()),
                        schedule_trace_write,
                    ): index
                    for index, tx in enumerate(transactions)
                }
                for future in as_completed(futures):
                    result = future.result()
                    with lock:
                        results.append(result)
                        completed += 1
                        report(f"已完成交易 {completed}/{total}")

            # Surface disk/compression errors before publishing graph.json.
            for future in write_futures:
                future.result()

        metadata_probes, metadata_cache_hits = formatter.erc20_probe_stats()
        logger.info(
            "区块 %d ERC20 元数据解析: 唯一地址=%d 复用=%d",
            block_number,
            metadata_probes,
            metadata_cache_hits,
        )
        results.sort(key=lambda item: item.index)
        graph = self._assemble_graph(block_number, block, results)
        block_dir.mkdir(parents=True, exist_ok=True)
        write_graph(destination, graph)
        report("整块 TFG 已生成")
        return graph

    @staticmethod
    def _load_cached_graph(path: Path, block_number: int) -> dict[str, Any] | None:
        return load_graph_file(path, block_number)

    def _analyze_transaction(
        self,
        tx: Any,
        index: int,
        trace_dir: Path,
        formatter: TraceFormatter,
        receipt: Any | None,
        preloaded_trace: dict[str, Any] | None,
        schedule_trace_write: TraceWrite,
    ) -> TxResult:
        tx_hash = Web3.to_hex(tx["hash"])
        sender = str(tx["from"]).lower()
        target = str(tx["to"]).lower() if tx.get("to") else None
        trace_path = trace_dir / f"{index:04d}_{tx_hash.removeprefix('0x')}.json.gz"
        try:
            if target is None:
                gas_used = int(receipt.get("gasUsed", 0)) if receipt is not None else None
                raw = formatter._fetch_raw_trace(
                    tx_hash,
                    int(tx["blockNumber"]),
                    gas_used,
                    preloaded_trace,
                )
                schedule_trace_write(trace_path, {"tx_hash": tx_hash, "raw_trace": raw})
                return TxResult(
                    index, tx_hash, sender, None, "contract_creation", [], set(),
                    {sender}, {}, int(raw.get("opcodeCount", len(raw.get("structLogs", []))))
                )
            standardized = formatter.get_standardized_trace(
                tx_hash,
                transaction=tx,
                receipt=receipt,
                preloaded_trace=preloaded_trace,
            )
            steps = standardized.get("steps", [])
            token_map = {str(key).lower(): value for key, value in standardized.get("erc20_token_map", {}).items()}
            identified_decimals = standardized.get("erc20_decimals_map", {})
            decimals = {
                address: int(identified_decimals.get(address, 18))
                for address in token_map
            }
            changes = extract_balance_changes(steps, standardized.get("slot_map", {}), token_map)
            paired, pending = pair_token_changes((sender, target, int(tx.get("value", 0))), changes, decimals)
            flows = flows_from_pairs(tx_hash, index, paired, pending)
            contracts = {str(value).lower() for value in standardized.get("contracts_addresses", []) if value}
            contracts.update(token_map)
            users = {str(value).lower() for value in standardized.get("users_addresses", []) if value}
            users.add(sender)
            metadata = {
                address: {"symbol": str(symbol), "decimals": decimals[address]}
                for address, symbol in token_map.items()
            }
            metadata["ETH"] = {"symbol": "ETH", "decimals": 18}
            schedule_trace_write(trace_path, standardized)
            return TxResult(
                index, tx_hash, sender, target, "ok", flows, contracts, users,
                metadata, int(standardized.get("opcode_count", len(steps)))
            )
        except NoOpcodeTraceError:
            schedule_trace_write(trace_path, {"tx_hash": tx_hash, "steps": [], "note": "EOA transfer: no opcode trace"})
            flow = []
            if target and int(tx.get("value", 0)):
                flow = flows_from_pairs(tx_hash, index, [{
                    "order": 0, "from": sender, "to": target,
                    "amount_raw": str(int(tx["value"])), "token": "ETH",
                    "token_addr": "ETH", "decimals": 18,
                }], [])
            return TxResult(index, tx_hash, sender, target, "no_opcodes", flow, set(), {sender, *(set([target]) if target else set())}, {"ETH": {"symbol": "ETH", "decimals": 18}}, 0)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            schedule_trace_write(trace_path, {"tx_hash": tx_hash, "error": error})
            return TxResult(index, tx_hash, sender, target, "error", [], set(), {sender}, {}, 0, error)

    @staticmethod
    def _write_trace(path: Path, trace: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=5) as handle:
            json.dump(trace, handle, ensure_ascii=False, separators=(",", ":"))

    def _assemble_graph(self, block_number: int, block: Any, results: list[TxResult]) -> dict[str, Any]:
        flows = [flow for result in results for flow in result.flows]
        contracts = set().union(*(result.contracts for result in results)) if results else set()
        users = set().union(*(result.users for result in results)) if results else set()
        first_seen: list[str] = []
        seen: set[str] = set()
        for result in results:
            for address in [a for flow in result.flows for a in (flow["source"], flow["target"])]:
                if address and address != "ETH" and address not in seen:
                    seen.add(address)
                    first_seen.append(address)
        users -= contracts
        aliases: dict[str, str] = {}
        user_index = contract_index = 0
        for address in first_seen:
            if address in contracts:
                aliases[address] = f"Contract{spreadsheet_suffix(contract_index)}"
                contract_index += 1
            else:
                aliases[address] = f"User{spreadsheet_suffix(user_index)}"
                users.add(address)
                user_index += 1

        token_meta: dict[str, dict[str, Any]] = {}
        for result in results:
            token_meta.update(result.token_meta)
        used_tokens = []
        for flow in flows:
            token = flow["token_address"]
            if token not in used_tokens:
                used_tokens.append(token)
        tokens = []
        for index, address in enumerate(used_tokens):
            meta = token_meta.get(address, {"symbol": flow_symbol(flows, address), "decimals": 18})
            color = token_color(index)
            tokens.append({"address": address, "symbol": meta["symbol"], "decimals": meta["decimals"], "color": color})
        colors = {item["address"]: item["color"] for item in tokens}
        for flow in flows:
            flow["color"] = colors[flow["token_address"]]

        nodes = [{
            "id": address,
            "alias": aliases[address],
            "kind": "contract" if address in contracts else "user",
            "address": address,
        } for address in first_seen if address in aliases]
        tx_items = [{
            "index": result.index,
            "hash": result.tx_hash,
            "from": result.sender,
            "to": result.target,
            "status": result.status,
            "step_count": result.step_count,
            "edge_count": len(result.flows),
            "edge_ids": [item["id"] for item in result.flows],
            "error": result.error,
        } for result in results]
        return enrich_graph_schema({
            "schema_version": GRAPH_SCHEMA_VERSION,
            "block": {
                "number": block_number,
                "hash": Web3.to_hex(block["hash"]),
                "timestamp": int(block["timestamp"]),
                "transaction_count": len(results),
                "successful_traces": sum(item.status != "error" for item in results),
            },
            "nodes": nodes,
            "edges": flows,
            "tokens": tokens,
            "transactions": tx_items,
        })


def flow_symbol(flows: list[dict[str, Any]], token: str) -> str:
    return next((item["token_symbol"] for item in flows if item["token_address"] == token), "Unknown")


def analyzer_from_env(data_dir: Path) -> BlockAnalyzer:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    rpc_url = os.environ.get("GETH_API", "").strip()
    if not rpc_url:
        raise RuntimeError("请在 backend/.env 中配置 GETH_API")
    return BlockAnalyzer(rpc_url, data_dir, int(os.environ.get("TRACE_WORKERS", "2")))
