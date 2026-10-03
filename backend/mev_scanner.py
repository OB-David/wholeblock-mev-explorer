"""Lightweight, receipt-based MEV labels for the block explorer.

The route matching below is a small, dependency-free port of mev-inspect-py's
arbitrage and sandwich rules.  Unlike the full TFG analyzer, the scanner reads
DEX Swap events and block receipts only, so it can follow the chain head.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from web3 import Web3

from sandwiches import extract_exchange_occurrences
from utils.evm_information import GETH_TRACE_START_BLOCK


logger = logging.getLogger(__name__)
UNISWAP_V2_SWAP_TOPIC = Web3.to_hex(Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)"))
UNISWAP_V3_SWAP_TOPIC = Web3.to_hex(Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)"))
ROUTERS = {
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
    "0xe592427a0aece92de3edee1f18e0157c05861564",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
}
DETECTOR_VERSION = "2"


def _hex(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return Web3.to_hex(value).lower()
    return str(value or "").lower()


def _address(value: Any) -> str:
    value = _hex(value)
    return f"0x{value[-40:]}" if len(value) >= 40 else value


def _integer(value: Any) -> int:
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def _words(data: Any) -> list[str]:
    raw = _hex(data).removeprefix("0x")
    return [raw[index:index + 64] for index in range(0, len(raw), 64) if len(raw[index:index + 64]) == 64]


def _signed(word: str) -> int:
    value = int(word, 16)
    return value - (1 << 256) if value >= (1 << 255) else value


@dataclass(frozen=True)
class Swap:
    transaction_hash: str
    transaction_position: int
    log_index: int
    contract_address: str
    from_address: str
    to_address: str
    token_in_address: str
    token_in_amount: int
    token_out_address: str
    token_out_amount: int
    protocol: str


def extract_trace_transfers(trace_entries: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Decode native and ERC-20 transfers from a Geth callTracer block result."""

    edges: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    contracts: set[str] = set()
    for tx_position, entry in enumerate(trace_entries):
        tx_hash = _hex(entry.get("txHash"))
        transactions.append({"index": tx_position, "hash": tx_hash})
        order = 0

        def add(source: str, target: str, token: str, amount: int) -> None:
            nonlocal order
            if amount <= 0 or not source or not target or source == target:
                return
            edges.append({
                "id": f"{tx_position}:{order}:quick-transfer",
                "tx_hash": tx_hash,
                "tx_index": tx_position,
                "order": order,
                "source": source,
                "target": target,
                "token_address": token,
                "amount_raw": str(amount),
                "kind": "transfer",
            })
            order += 1

        def walk(frame: dict[str, Any], root: bool = False) -> None:
            if frame.get("error"):
                return
            call_type = str(frame.get("type", "CALL")).upper()
            source, target = _address(frame.get("from")), _address(frame.get("to"))
            if target:
                contracts.add(target)
            if call_type in {"CALL", "CALLCODE", "CREATE", "CREATE2"}:
                add(source, target, "eth", _integer(frame.get("value", 0)))

            raw_input = _hex(frame.get("input", "0x")).removeprefix("0x")
            if call_type == "CALL" and target and raw_input.startswith("a9059cbb") and len(raw_input) >= 136:
                recipient = _address("0x" + raw_input[8:72])
                amount = int(raw_input[72:136], 16)
                add(source, recipient, target, amount)
            elif call_type == "CALL" and target and raw_input.startswith("23b872dd") and len(raw_input) >= 200:
                sender = _address("0x" + raw_input[8:72])
                recipient = _address("0x" + raw_input[72:136])
                amount = int(raw_input[136:200], 16)
                add(sender, recipient, target, amount)

            for child in frame.get("calls", []):
                walk(child)

        result = entry.get("result") or {}
        root_sender = _address(result.get("from"))
        walk(result, root=True)
        contracts.discard(root_sender)
    return edges, transactions, contracts


def extract_trace_swaps(trace_entries: Iterable[dict[str, Any]]) -> list[Swap]:
    """Build generic pool swaps using mev-inspect's transfer-in/out model."""

    edges, transactions, contracts = extract_trace_transfers(trace_entries)
    occurrences = extract_exchange_occurrences(edges, transactions, contracts)
    swaps: list[Swap] = []
    for occurrence in occurrences:
        if len(occurrence.payer_addresses) != 1 or len(occurrence.recipient_addresses) != 1:
            continue
        swaps.append(Swap(
            transaction_hash=occurrence.tx_hash,
            transaction_position=occurrence.tx_index,
            log_index=min(
                (int(edge_id.split(":", 2)[1]) for edge_id in occurrence.edge_ids),
                default=0,
            ),
            contract_address=occurrence.address,
            from_address=occurrence.payer_addresses[0],
            to_address=occurrence.recipient_addresses[0],
            token_in_address=occurrence.token_in,
            token_in_amount=occurrence.amount_in_raw,
            token_out_address=occurrence.token_out,
            token_out_amount=occurrence.amount_out_raw,
            protocol="transfer_inference",
        ))
    return swaps


def extract_event_swaps(
    block_number: int,
    receipts: Iterable[Any],
    pool_tokens: dict[str, tuple[str, str]],
) -> list[Swap]:
    """Convert Uniswap V2/V3-compatible Swap events to mev-inspect swaps."""

    swaps: list[Swap] = []
    for fallback_position, receipt in enumerate(receipts):
        if _integer(receipt.get("status", 1)) == 0:
            continue
        tx_hash = _hex(receipt.get("transactionHash"))
        tx_position = _integer(receipt.get("transactionIndex", fallback_position))
        for fallback_log_index, log in enumerate(receipt.get("logs", [])):
            topics = [_hex(topic) for topic in log.get("topics", [])]
            if len(topics) < 3:
                continue
            topic = topics[0]
            if topic not in {UNISWAP_V2_SWAP_TOPIC, UNISWAP_V3_SWAP_TOPIC}:
                continue
            pool = _address(log.get("address"))
            pair = pool_tokens.get(pool)
            if pair is None:
                continue
            token0, token1 = pair
            values = _words(log.get("data", "0x"))
            sender = _address(topics[1])
            recipient = _address(topics[2])
            protocol = "uniswap_v2"
            if topic == UNISWAP_V2_SWAP_TOPIC:
                if len(values) < 4:
                    continue
                amount0_in, amount1_in, amount0_out, amount1_out = (int(word, 16) for word in values[:4])
                if amount0_in > 0 and amount1_out > 0 and amount1_in == 0 and amount0_out == 0:
                    token_in, amount_in, token_out, amount_out = token0, amount0_in, token1, amount1_out
                elif amount1_in > 0 and amount0_out > 0 and amount0_in == 0 and amount1_out == 0:
                    token_in, amount_in, token_out, amount_out = token1, amount1_in, token0, amount0_out
                else:
                    continue
            else:
                if len(values) < 2:
                    continue
                protocol = "uniswap_v3"
                amount0, amount1 = _signed(values[0]), _signed(values[1])
                if amount0 > 0 and amount1 < 0:
                    token_in, amount_in, token_out, amount_out = token0, amount0, token1, -amount1
                elif amount1 > 0 and amount0 < 0:
                    token_in, amount_in, token_out, amount_out = token1, amount1, token0, -amount0
                else:
                    continue
            swaps.append(Swap(
                transaction_hash=tx_hash,
                transaction_position=tx_position,
                log_index=_integer(log.get("logIndex", fallback_log_index)),
                contract_address=pool,
                from_address=sender,
                to_address=recipient,
                token_in_address=token_in,
                token_in_amount=amount_in,
                token_out_address=token_out,
                token_out_amount=amount_out,
                protocol=protocol,
            ))
    return sorted(swaps, key=lambda item: (item.transaction_position, item.log_index))


def _amounts_match(left: int, right: int, tolerance: float = 0.01) -> bool:
    if left == right:
        return True
    return max(left, right) > 0 and abs(left - right) / max(left, right) <= tolerance


def _swap_out_matches_input(left: Swap, right: Swap) -> bool:
    return (
        left.token_out_address == right.token_in_address
        and (
            left.contract_address == right.from_address
            or left.to_address == right.contract_address
            or left.to_address == right.from_address
        )
        and _amounts_match(left.token_out_amount, right.token_in_amount)
    )


def _shortest_route(start: Swap, ends: list[Swap], all_swaps: list[Swap]) -> list[Swap] | None:
    for end in ends:
        if _swap_out_matches_input(start, end):
            return [start, end]
    other = [swap for swap in all_swaps if swap is not start and swap not in ends]
    best: list[Swap] | None = None
    for candidate in other:
        if not _swap_out_matches_input(start, candidate):
            continue
        tail = _shortest_route(candidate, ends, other)
        if tail is not None and (best is None or len(tail) < len(best)):
            best = tail
    return None if best is None else [start, *best]


def detect_arbitrages(swaps: list[Swap]) -> list[dict[str, Any]]:
    """Port of mev-inspect-py get_arbitrages, grouped per transaction."""

    by_transaction: dict[str, list[Swap]] = {}
    for swap in swaps:
        by_transaction.setdefault(swap.transaction_hash, []).append(swap)
    result: list[dict[str, Any]] = []
    for tx_swaps in by_transaction.values():
        pool_addresses = {swap.contract_address for swap in tx_swaps}
        starts: list[tuple[Swap, list[Swap]]] = []
        for start in tx_swaps:
            ends = [
                end for end in tx_swaps
                if end is not start
                and start.token_in_address == end.token_out_address
                and start.contract_address != end.contract_address
                and start.from_address == end.to_address
                and start.from_address not in pool_addresses
            ]
            if ends:
                starts.append((start, ends))
        used: set[Swap] = set()
        transaction_results: list[dict[str, Any]] = []
        for start, ends in starts:
            if start in used:
                continue
            route = _shortest_route(start, [item for item in ends if item not in used], tx_swaps)
            if route is None:
                continue
            used.update(route)
            transaction_results.append({
                "block_number": 0,
                "transaction_hash": start.transaction_hash,
                "transaction_position": start.transaction_position,
                "account_address": start.from_address,
                "profit_token_address": start.token_in_address,
                "start_amount": str(route[0].token_in_amount),
                "end_amount": str(route[-1].token_out_amount),
                "profit_amount": str(route[-1].token_out_amount - route[0].token_in_amount),
                "swaps": [asdict(item) for item in route],
            })
        if len(transaction_results) == 1:
            result.extend(transaction_results)
        else:
            result.extend(item for item in transaction_results if item["swaps"][0]["log_index"] < item["swaps"][-1]["log_index"])
    return result


def detect_sandwiches(swaps: list[Swap]) -> list[dict[str, Any]]:
    """Port of mev-inspect-py get_sandwiches with positive-profit filtering."""

    ordered = sorted(swaps, key=lambda item: (item.transaction_position, item.log_index))
    result: list[dict[str, Any]] = []
    for index, front in enumerate(ordered):
        attacker = front.to_address
        if attacker in ROUTERS:
            continue
        victims: list[Swap] = []
        for other in ordered[index + 1:]:
            if other.transaction_hash == front.transaction_hash or other.contract_address != front.contract_address:
                continue
            if (
                other.token_in_address == front.token_in_address
                and other.token_out_address == front.token_out_address
                and other.from_address != attacker
            ):
                victims.append(other)
                continue
            if not (
                other.token_out_address == front.token_in_address
                and other.token_in_address == front.token_out_address
                and other.from_address == attacker
                and victims
            ):
                continue
            profit = other.token_out_amount - front.token_in_amount
            if profit > 0:
                result.append({
                    "block_number": 0,
                    "pool_address": front.contract_address,
                    "sandwicher_address": attacker,
                    "profit_token_address": front.token_in_address,
                    "profit_amount": str(profit),
                    "frontrun_swap": asdict(front),
                    "backrun_swap": asdict(other),
                    "sandwiched_swaps": [asdict(item) for item in victims],
                })
            break
    return result


class MevStore:
    def __init__(self, data_dir: Path):
        self.directory = data_dir / "mev"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.arbitrage_path = self.directory / "arbitrages.sqlite3"
        self.sandwich_path = self.directory / "sandwiches.sqlite3"
        self._initialize()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with self._connect(self.arbitrage_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS blocks (
                    block_number INTEGER PRIMARY KEY, block_hash TEXT, timestamp INTEGER,
                    tx_count INTEGER, gas_used INTEGER, gas_limit INTEGER, base_fee TEXT,
                    scan_status TEXT NOT NULL, scan_ms REAL, swap_count INTEGER DEFAULT 0,
                    error TEXT, scanned_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS pools (
                    pool_address TEXT PRIMARY KEY, token0 TEXT NOT NULL, token1 TEXT NOT NULL,
                    updated_block INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arbitrages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, block_number INTEGER NOT NULL,
                    tx_hash TEXT NOT NULL, tx_position INTEGER NOT NULL, account_address TEXT NOT NULL,
                    profit_token_address TEXT NOT NULL, start_amount TEXT NOT NULL,
                    end_amount TEXT NOT NULL, profit_amount TEXT NOT NULL,
                    swap_count INTEGER NOT NULL, route_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS arbitrages_block_idx ON arbitrages(block_number);
            """)
        with self._connect(self.sandwich_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sandwiches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, block_number INTEGER NOT NULL,
                    pool_address TEXT NOT NULL, attacker_address TEXT NOT NULL,
                    profit_token_address TEXT NOT NULL, profit_amount TEXT NOT NULL,
                    front_tx_hash TEXT NOT NULL, front_tx_position INTEGER NOT NULL,
                    back_tx_hash TEXT NOT NULL, back_tx_position INTEGER NOT NULL,
                    victims_json TEXT NOT NULL, swaps_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sandwiches_block_idx ON sandwiches(block_number);
            """)

    def metadata(self, key: str) -> str | None:
        with self._connect(self.arbitrage_path) as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return None if row is None else str(row["value"])

    def set_metadata(self, key: str, value: str | int) -> None:
        with self._connect(self.arbitrage_path) as db:
            db.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def begin_session(self, latest: int) -> tuple[int, int]:
        """Anchor both workers at the head observed for this server session."""

        self.set_metadata("detector_version", DETECTOR_VERSION)
        self.set_metadata("session_start_block", latest)
        self.set_metadata("last_scanned", latest - 1)
        self.set_metadata("backfill_next", latest - 1)
        if self.metadata("start_block") is None:
            self.set_metadata("start_block", latest)
        return latest, latest - 1

    def block_is_scanned(self, number: int) -> bool:
        with self._connect(self.arbitrage_path) as db:
            row = db.execute(
                "SELECT 1 FROM blocks WHERE block_number = ? AND scan_status = 'scanned'",
                (number,),
            ).fetchone()
            return row is not None

    def pool_tokens(self, addresses: Iterable[str]) -> dict[str, tuple[str, str]]:
        addresses = list(dict.fromkeys(addresses))
        if not addresses:
            return {}
        placeholders = ",".join("?" for _ in addresses)
        with self._connect(self.arbitrage_path) as db:
            rows = db.execute(
                f"SELECT pool_address, token0, token1 FROM pools WHERE pool_address IN ({placeholders})",
                addresses,
            ).fetchall()
        return {row["pool_address"]: (row["token0"], row["token1"]) for row in rows}

    def save_pool_tokens(self, pairs: dict[str, tuple[str, str]], block_number: int) -> None:
        if not pairs:
            return
        with self._connect(self.arbitrage_path) as db:
            db.executemany(
                "INSERT INTO pools(pool_address,token0,token1,updated_block) VALUES (?,?,?,?) "
                "ON CONFLICT(pool_address) DO UPDATE SET token0=excluded.token0,token1=excluded.token1,updated_block=excluded.updated_block",
                [(pool, token0, token1, block_number) for pool, (token0, token1) in pairs.items()],
            )

    def save_block(
        self,
        header: dict[str, Any],
        swaps: list[Swap],
        arbitrages: list[dict[str, Any]],
        sandwiches: list[dict[str, Any]],
        elapsed_ms: float,
    ) -> None:
        number = _integer(header["number"])
        now = int(time.time())
        for item in arbitrages:
            item["block_number"] = number
        for item in sandwiches:
            item["block_number"] = number
        with self._connect(self.arbitrage_path) as db:
            db.execute("DELETE FROM arbitrages WHERE block_number = ?", (number,))
            db.execute(
                "INSERT INTO blocks(block_number,block_hash,timestamp,tx_count,gas_used,gas_limit,base_fee,scan_status,scan_ms,swap_count,error,scanned_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(block_number) DO UPDATE SET "
                "block_hash=excluded.block_hash,timestamp=excluded.timestamp,tx_count=excluded.tx_count,gas_used=excluded.gas_used,"
                "gas_limit=excluded.gas_limit,base_fee=excluded.base_fee,scan_status='scanned',scan_ms=excluded.scan_ms,"
                "swap_count=excluded.swap_count,error=NULL,scanned_at=excluded.scanned_at",
                (number, _hex(header["hash"]), _integer(header["timestamp"]), len(header.get("transactions", [])),
                 _integer(header["gasUsed"]), _integer(header["gasLimit"]), str(_integer(header.get("baseFeePerGas", 0))),
                 "scanned", elapsed_ms, len(swaps), now),
            )
            db.executemany(
                "INSERT INTO arbitrages(block_number,tx_hash,tx_position,account_address,profit_token_address,start_amount,end_amount,profit_amount,swap_count,route_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(number, item["transaction_hash"], item["transaction_position"], item["account_address"],
                  item["profit_token_address"], item["start_amount"], item["end_amount"], item["profit_amount"],
                  len(item["swaps"]), json.dumps(item["swaps"], separators=(",", ":"))) for item in arbitrages],
            )
            db.execute(
                "INSERT INTO metadata(key,value) VALUES ('start_block',?) ON CONFLICT(key) DO UPDATE SET "
                "value=CAST(MIN(CAST(metadata.value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT)",
                (str(number),),
            )
        with self._connect(self.sandwich_path) as db:
            db.execute("DELETE FROM sandwiches WHERE block_number = ?", (number,))
            db.executemany(
                "INSERT INTO sandwiches(block_number,pool_address,attacker_address,profit_token_address,profit_amount,front_tx_hash,front_tx_position,back_tx_hash,back_tx_position,victims_json,swaps_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(number, item["pool_address"], item["sandwicher_address"], item["profit_token_address"], item["profit_amount"],
                  item["frontrun_swap"]["transaction_hash"], item["frontrun_swap"]["transaction_position"],
                  item["backrun_swap"]["transaction_hash"], item["backrun_swap"]["transaction_position"],
                  json.dumps([swap["transaction_hash"] for swap in item["sandwiched_swaps"]], separators=(",", ":")),
                  json.dumps(item, separators=(",", ":"))) for item in sandwiches],
            )

    def save_error(self, number: int, message: str) -> None:
        with self._connect(self.arbitrage_path) as db:
            db.execute(
                "INSERT INTO blocks(block_number,scan_status,error,scanned_at) VALUES (?,'error',?,?) "
                "ON CONFLICT(block_number) DO UPDATE SET scan_status='error',error=excluded.error,scanned_at=excluded.scanned_at",
                (number, message[:1000], int(time.time())),
            )

    def labels(self, first: int, last: int) -> dict[int, dict[str, Any]]:
        with self._connect(self.arbitrage_path) as db:
            blocks = db.execute(
                "SELECT * FROM blocks WHERE block_number BETWEEN ? AND ?", (first, last),
            ).fetchall()
            arbs = dict(db.execute(
                "SELECT block_number,COUNT(*) FROM arbitrages WHERE block_number BETWEEN ? AND ? GROUP BY block_number",
                (first, last),
            ).fetchall())
        with self._connect(self.sandwich_path) as db:
            sandwiches = dict(db.execute(
                "SELECT block_number,COUNT(*) FROM sandwiches WHERE block_number BETWEEN ? AND ? GROUP BY block_number",
                (first, last),
            ).fetchall())
        result = {int(row["block_number"]): dict(row) for row in blocks}
        for number in range(first, last + 1):
            result.setdefault(number, {})
            result[number]["arbitrage_count"] = int(arbs.get(number, 0))
            result[number]["sandwich_count"] = int(sandwiches.get(number, 0))
        return result


class MevScanner:
    def __init__(
        self,
        rpc_url: str,
        data_dir: Path,
        poll_seconds: float = 3.0,
        backfill_start_block: int = GETH_TRACE_START_BLOCK,
    ):
        self.rpc_url = rpc_url
        self.web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if not self.web3.is_connected():
            raise ConnectionError("无法连接 GETH_API")
        self.store = MevStore(data_dir)
        self.poll_seconds = poll_seconds
        self.backfill_start_block = max(0, backfill_start_block)
        self.stop_event = threading.Event()
        self.head_ready_event = threading.Event()
        self.head_thread: threading.Thread | None = None
        self.backfill_thread: threading.Thread | None = None
        self.forward_block: int | None = None
        self.backfill_block: int | None = None
        self.forward_error: str | None = None
        self.backfill_error: str | None = None
        self._thread_local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def start(self) -> None:
        latest = int(self.web3.eth.block_number)
        self.store.begin_session(latest)
        self.stop_event.clear()
        self.head_ready_event.clear()
        if self.head_thread is None or not self.head_thread.is_alive():
            self.head_thread = threading.Thread(
                target=self._watch_head,
                args=(latest,),
                name="mev-head-scanner",
                daemon=True,
            )
            self.head_thread.start()
        if self.backfill_thread is None or not self.backfill_thread.is_alive():
            self.backfill_thread = threading.Thread(
                target=self._backfill_history,
                args=(latest - 1,),
                name="mev-history-backfill",
                daemon=True,
            )
            self.backfill_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for thread in (self.head_thread, self.backfill_thread):
            if thread is not None:
                thread.join(timeout=5)
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def _http_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _web3_client(self) -> Web3:
        client = getattr(self._thread_local, "web3", None)
        if client is None:
            client = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 30}))
            self._thread_local.web3 = client
        return client

    def _rpc_batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls)
        ]
        response = self._http_session().post(self.rpc_url, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            body = [body]
        by_id = {item.get("id"): item for item in body}
        result = []
        for index in range(len(calls)):
            item = by_id.get(index, {})
            if "error" in item:
                raise RuntimeError(str(item["error"]))
            result.append(item.get("result"))
        return result

    def _pool_pairs(self, pools: set[str], block_number: int) -> dict[str, tuple[str, str]]:
        pairs = self.store.pool_tokens(pools)
        missing = sorted(pools - pairs.keys())
        calls = []
        for pool in missing:
            calls.extend([
                ("eth_call", [{"to": pool, "data": "0x0dfe1681"}, hex(block_number)]),
                ("eth_call", [{"to": pool, "data": "0xd21220a7"}, hex(block_number)]),
            ])
        if calls:
            try:
                values = self._rpc_batch(calls)
                discovered: dict[str, tuple[str, str]] = {}
                for index, pool in enumerate(missing):
                    token0, token1 = values[index * 2:index * 2 + 2]
                    if token0 and token1 and len(token0) >= 42 and len(token1) >= 42:
                        discovered[pool] = (_address(token0), _address(token1))
                self.store.save_pool_tokens(discovered, block_number)
                pairs.update(discovered)
            except Exception as exc:
                logger.warning("批量读取区块 %d 的池 token0/token1 失败: %s", block_number, exc)
        return pairs

    def scan_block(self, number: int) -> dict[str, int | float]:
        started = time.perf_counter()
        client = self._web3_client()
        header = client.eth.get_block(number, full_transactions=False)
        receipts = list(client.eth.get_block_receipts(number))
        call_traces = self._rpc_batch([(
            "debug_traceBlockByNumber",
            [hex(number), {"tracer": "callTracer", "timeout": "60s", "tracerConfig": {"onlyTopCall": False, "withLog": False}}],
        )])[0] or []
        pools = {
            _address(log.get("address"))
            for receipt in receipts for log in receipt.get("logs", [])
            if log.get("topics") and _hex(log["topics"][0]) in {UNISWAP_V2_SWAP_TOPIC, UNISWAP_V3_SWAP_TOPIC}
        }
        pairs = self._pool_pairs(pools, number)
        event_swaps = extract_event_swaps(number, receipts, pairs)
        all_inferred = extract_trace_swaps(call_traces)
        inferred_by_market = {
            (item.transaction_hash, item.contract_address): item
            for item in all_inferred
        }
        # Event amounts are exact; transfer boundaries identify the actual
        # payer/recipient used by mev-inspect more reliably than event
        # sender fields (notably when a callback intermediary is present).
        event_swaps = [
            replace(
                item,
                from_address=inferred.from_address,
                to_address=inferred.to_address,
            ) if (inferred := inferred_by_market.get((item.transaction_hash, item.contract_address))) else item
            for item in event_swaps
        ]
        exact_markets = {(item.transaction_hash, item.contract_address) for item in event_swaps}
        inferred_swaps = [
            item for item in all_inferred
            if (item.transaction_hash, item.contract_address) not in exact_markets
        ]
        swaps = sorted(
            [*event_swaps, *inferred_swaps],
            key=lambda item: (item.transaction_position, item.log_index),
        )
        arbitrages = detect_arbitrages(swaps)
        sandwiches = detect_sandwiches(swaps)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.store.save_block(header, swaps, arbitrages, sandwiches, elapsed_ms)
        return {
            "swaps": len(swaps), "arbitrages": len(arbitrages),
            "sandwiches": len(sandwiches), "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _log_scan(direction: str, number: int, stats: dict[str, int | float]) -> None:
        logger.info(
            "MEV %s block=%d swaps=%d arbitrages=%d sandwiches=%d %.0fms",
            direction, number, stats["swaps"], stats["arbitrages"], stats["sandwiches"], stats["elapsed_ms"],
        )

    def _watch_head(self, first_block: int) -> None:
        number = first_block
        while not self.stop_event.is_set():
            try:
                tip = int(self._web3_client().eth.block_number)
                if number > tip:
                    self.stop_event.wait(self.poll_seconds)
                    continue
                self.forward_block = number
                stats = self.scan_block(number)
                self.store.set_metadata("last_scanned", number)
                self.forward_error = None
                self._log_scan("新区块扫描", number, stats)
                self.head_ready_event.set()
                number += 1
            except Exception as exc:
                self.forward_error = f"{type(exc).__name__}: {exc}"
                self.store.save_error(number, self.forward_error)
                logger.exception("MEV 新区块扫描失败")
                self.stop_event.wait(self.poll_seconds)
            finally:
                self.forward_block = None

    def _backfill_history(self, first_block: int) -> None:
        while not self.head_ready_event.is_set() and not self.stop_event.is_set():
            self.head_ready_event.wait(self.poll_seconds)
        number = first_block
        while number >= self.backfill_start_block and not self.stop_event.is_set():
            try:
                if self.store.block_is_scanned(number):
                    number -= 1
                    self.store.set_metadata("backfill_next", number)
                    continue
                self.backfill_block = number
                stats = self.scan_block(number)
                self.backfill_error = None
                self._log_scan("历史回填", number, stats)
                number -= 1
                self.store.set_metadata("backfill_next", number)
            except Exception as exc:
                self.backfill_error = f"{type(exc).__name__}: {exc}"
                self.store.save_error(number, self.backfill_error)
                logger.exception("MEV 历史区块回填失败 block=%d", number)
                self.stop_event.wait(self.poll_seconds)
            finally:
                self.backfill_block = None

    def explorer(self, limit: int = 180, before: int | None = None) -> dict[str, Any]:
        latest = int(self.web3.eth.block_number)
        newest = min(latest, before if before is not None else latest)
        oldest = max(0, newest - limit + 1)
        calls = [("eth_getBlockByNumber", [hex(number), False]) for number in range(newest, oldest - 1, -1)]
        headers = self._rpc_batch(calls)
        labels = self.store.labels(oldest, newest)
        blocks = []
        for number, header in zip(range(newest, oldest - 1, -1), headers):
            if header is None:
                continue
            saved = labels.get(number, {})
            blocks.append({
                "number": number,
                "hash": header.get("hash"),
                "timestamp": _integer(header.get("timestamp", 0)),
                "transaction_count": len(header.get("transactions", [])),
                "gas_used": _integer(header.get("gasUsed", 0)),
                "gas_limit": _integer(header.get("gasLimit", 0)),
                "base_fee": str(_integer(header.get("baseFeePerGas", 0))),
                "scan_status": saved.get("scan_status", "untracked"),
                "scan_ms": saved.get("scan_ms"),
                "swap_count": saved.get("swap_count", 0),
                "arbitrage_count": saved.get("arbitrage_count", 0),
                "sandwich_count": saved.get("sandwich_count", 0),
            })
        start = self.store.metadata("start_block")
        session_start = self.store.metadata("session_start_block")
        last = self.store.metadata("last_scanned")
        return {
            "latest": latest,
            "start_block": None if start is None else int(start),
            "session_start_block": None if session_start is None else int(session_start),
            "last_scanned": None if last is None else int(last),
            "scanning_block": self.forward_block,
            "backfill_block": self.backfill_block,
            "scanner_error": self.forward_error,
            "backfill_error": self.backfill_error,
            "blocks": blocks,
        }


def scanner_from_env(data_dir: Path) -> MevScanner:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    rpc_url = os.environ.get("GETH_API", "").strip()
    if not rpc_url:
        raise RuntimeError("请在 backend/.env 中配置 GETH_API")
    return MevScanner(
        rpc_url,
        data_dir,
        float(os.environ.get("MEV_POLL_SECONDS", "3")),
        int(os.environ.get("MEV_BACKFILL_START_BLOCK", str(GETH_TRACE_START_BLOCK))),
    )
