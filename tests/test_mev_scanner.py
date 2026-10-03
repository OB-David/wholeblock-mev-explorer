import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from mev_scanner import (
    MevScanner,
    MevStore,
    Swap,
    UNISWAP_V2_SWAP_TOPIC,
    UNISWAP_V3_SWAP_TOPIC,
    detect_arbitrages,
    detect_sandwiches,
    extract_event_swaps,
    extract_trace_swaps,
)


BOT = "0x" + "11" * 20
VICTIM = "0x" + "22" * 20
POOL_A = "0x" + "aa" * 20
POOL_B = "0x" + "bb" * 20
TOKEN_X = "0x" + "01" * 20
TOKEN_Y = "0x" + "02" * 20


def swap(tx, position, log_index, pool, sender, recipient, token_in, amount_in, token_out, amount_out):
    return Swap(tx, position, log_index, pool, sender, recipient, token_in, amount_in, token_out, amount_out, "uniswap_v2")


def test_mev_inspect_style_arbitrage_route():
    swaps = [
        swap("0xtx", 4, 1, POOL_A, BOT, BOT, TOKEN_X, 100, TOKEN_Y, 90),
        swap("0xtx", 4, 2, POOL_B, BOT, BOT, TOKEN_Y, 90, TOKEN_X, 112),
    ]

    result = detect_arbitrages(swaps)

    assert len(result) == 1
    assert result[0]["profit_amount"] == "12"
    assert len(result[0]["swaps"]) == 2


def test_mev_inspect_style_sandwich_requires_victim_and_profitable_backrun():
    swaps = [
        swap("0xfront", 1, 1, POOL_A, BOT, BOT, TOKEN_X, 100, TOKEN_Y, 90),
        swap("0xvictim", 2, 2, POOL_A, VICTIM, VICTIM, TOKEN_X, 50, TOKEN_Y, 40),
        swap("0xback", 3, 3, POOL_A, BOT, BOT, TOKEN_Y, 90, TOKEN_X, 111),
    ]

    result = detect_sandwiches(swaps)

    assert len(result) == 1
    assert result[0]["profit_amount"] == "11"
    assert [item["transaction_hash"] for item in result[0]["sandwiched_swaps"]] == ["0xvictim"]


def _topic_address(address):
    return "0x" + "0" * 24 + address.removeprefix("0x")


def _uint(value):
    return f"{value % (1 << 256):064x}"


def test_extracts_v2_and_v3_event_swaps():
    receipt = {
        "status": 1,
        "transactionHash": "0x1234",
        "transactionIndex": 7,
        "logs": [
            {
                "address": POOL_A,
                "topics": [UNISWAP_V2_SWAP_TOPIC, _topic_address(BOT), _topic_address(BOT)],
                "data": "0x" + "".join(map(_uint, [100, 0, 0, 90])),
                "logIndex": 10,
            },
            {
                "address": POOL_B,
                "topics": [UNISWAP_V3_SWAP_TOPIC, _topic_address(BOT), _topic_address(BOT)],
                "data": "0x" + _uint(-90) + _uint(112) + _uint(0) * 3,
                "logIndex": 11,
            },
        ],
    }

    result = extract_event_swaps(99, [receipt], {
        POOL_A: (TOKEN_X, TOKEN_Y),
        POOL_B: (TOKEN_X, TOKEN_Y),
    })

    assert [(item.token_in_address, item.token_out_address) for item in result] == [
        (TOKEN_X, TOKEN_Y), (TOKEN_Y, TOKEN_X),
    ]
    assert [(item.token_in_amount, item.token_out_amount) for item in result] == [(100, 90), (112, 90)]


def test_store_anchors_each_session_at_current_head(tmp_path):
    store = MevStore(tmp_path)

    assert store.begin_session(100, 91) == (100, 99)
    assert store.metadata("session_start_block") == "100"
    assert store.metadata("history_start_block") == "91"
    assert store.metadata("backfill_next") == "99"
    store.set_metadata("last_scanned", 102)
    restarted = MevStore(tmp_path)
    assert restarted.begin_session(110, 101) == (110, 109)
    assert restarted.metadata("session_start_block") == "110"
    assert restarted.metadata("history_start_block") == "101"
    assert restarted.metadata("last_scanned") == "109"
    assert restarted.metadata("backfill_next") == "109"
    assert restarted.metadata("start_block") == "100"
    assert (tmp_path / "mev" / "arbitrages.sqlite3").is_file()
    assert (tmp_path / "mev" / "sandwiches.sqlite3").is_file()


def test_store_prunes_only_quick_scan_history(tmp_path):
    store = MevStore(tmp_path)
    graph = tmp_path / "90" / "graph.json"
    trace = tmp_path / "90" / "traces" / "0000.json.gz"
    graph.parent.mkdir(parents=True)
    trace.parent.mkdir()
    graph.write_text("{}")
    trace.write_bytes(b"trace")

    with store._connect(store.arbitrage_path) as db:
        db.executemany(
            "INSERT INTO blocks(block_number,scan_status) VALUES (?,'scanned')",
            [(90,), (91,), (92,)],
        )
        db.executemany(
            "INSERT INTO arbitrages(block_number,tx_hash,tx_position,account_address,profit_token_address,"
            "start_amount,end_amount,profit_amount,swap_count,route_json) VALUES (?,'0xtx',0,'0xa','0xt','1','2','1',1,'[]')",
            [(90,), (92,)],
        )
        db.execute(
            "INSERT INTO pools(pool_address,token0,token1,updated_block) VALUES ('0xp','0x0','0x1',90)",
        )
    with store._connect(store.sandwich_path) as db:
        db.executemany(
            "INSERT INTO sandwiches(block_number,pool_address,attacker_address,profit_token_address,profit_amount,"
            "front_tx_hash,front_tx_position,back_tx_hash,back_tx_position,victims_json,swaps_json) "
            "VALUES (?,'0xp','0xa','0xt','1','0xf',0,'0xb',1,'[]','{}')",
            [(90,), (92,)],
        )

    removed = store.prune_before(92)

    with store._connect(store.arbitrage_path) as db:
        assert [row[0] for row in db.execute("SELECT block_number FROM blocks")] == [92]
        assert [row[0] for row in db.execute("SELECT block_number FROM arbitrages")] == [92]
        assert db.execute("SELECT COUNT(*) FROM pools").fetchone()[0] == 1
    with store._connect(store.sandwich_path) as db:
        assert [row[0] for row in db.execute("SELECT block_number FROM sandwiches")] == [92]
    assert removed == {"blocks": 2, "arbitrages": 1, "sandwiches": 1}
    assert store.metadata("start_block") == "92"
    assert graph.is_file() and trace.is_file()


class _WorkerStore:
    def __init__(self, scanned=()):
        self.scanned = set(scanned)
        self.metadata_values = {}

    def block_is_scanned(self, number):
        return number in self.scanned

    def set_metadata(self, key, value):
        self.metadata_values[key] = value

    def save_error(self, number, message):
        raise AssertionError(f"unexpected scan error for {number}: {message}")


def _worker_scanner(store):
    scanner = object.__new__(MevScanner)
    scanner.store = store
    scanner.stop_event = threading.Event()
    scanner.head_ready_event = threading.Event()
    scanner.poll_seconds = 0
    scanner.history_start_block = 0
    scanner.forward_block = None
    scanner.backfill_block = None
    scanner.forward_error = None
    scanner.backfill_error = None
    scanner._web3_client = lambda: SimpleNamespace(eth=SimpleNamespace(block_number=100))
    scanner._log_scan = lambda *args: None
    return scanner


def test_head_worker_scans_session_head_first():
    scanner = _worker_scanner(_WorkerStore())
    scanned = []

    def scan_block(number):
        scanned.append(number)
        scanner.stop_event.set()
        return {"swaps": 0, "arbitrages": 0, "sandwiches": 0, "elapsed_ms": 1}

    scanner.scan_block = scan_block
    scanner._watch_head(100)

    assert scanned == [100]
    assert scanner.store.metadata_values["last_scanned"] == 100


def test_backfill_worker_skips_scanned_blocks_and_descends():
    scanner = _worker_scanner(_WorkerStore(scanned={99}))
    scanner.head_ready_event.set()
    scanned = []

    def scan_block(number):
        scanned.append(number)
        scanner.stop_event.set()
        return {"swaps": 0, "arbitrages": 0, "sandwiches": 0, "elapsed_ms": 1}

    scanner.scan_block = scan_block
    scanner._backfill_history(99)

    assert scanned == [98]
    assert scanner.store.metadata_values["backfill_next"] == 97


def test_backfill_worker_stops_at_session_history_boundary():
    scanner = _worker_scanner(_WorkerStore())
    scanner.head_ready_event.set()
    scanner.history_start_block = 98
    scanned = []

    def scan_block(number):
        scanned.append(number)
        return {"swaps": 0, "arbitrages": 0, "sandwiches": 0, "elapsed_ms": 1}

    scanner.scan_block = scan_block
    scanner._backfill_history(99)

    assert scanned == [99, 98]
    assert scanner.store.metadata_values["backfill_next"] == 97


class _ExplorerStore:
    def __init__(self):
        self.values = {
            "session_start_block": "100",
            "history_start_block": "91",
            "last_scanned": "100",
        }

    def metadata(self, key):
        return self.values.get(key)

    def labels(self, first, last):
        return {}


def test_explorer_pages_stay_inside_session_history_window():
    scanner = object.__new__(MevScanner)
    scanner.web3 = SimpleNamespace(eth=SimpleNamespace(block_number=100))
    scanner.store = _ExplorerStore()
    scanner.history_blocks = 10
    scanner.forward_block = None
    scanner.backfill_block = None
    scanner.forward_error = None
    scanner.backfill_error = None

    def rpc_batch(calls):
        return [
            {
                "hash": hex(int(params[0], 16)), "timestamp": "0x1",
                "transactions": [], "gasUsed": "0x0", "gasLimit": "0x1", "baseFeePerGas": "0x0",
            }
            for _, params in calls
        ]

    scanner._rpc_batch = rpc_batch

    newest = scanner.explorer(limit=4)
    middle = scanner.explorer(limit=4, before=96)
    oldest = scanner.explorer(limit=4, before=92)

    assert [block["number"] for block in newest["blocks"]] == [100, 99, 98, 97]
    assert newest["has_newer"] is False and newest["has_older"] is True
    assert [block["number"] for block in middle["blocks"]] == [96, 95, 94, 93]
    assert middle["has_newer"] is True and middle["has_older"] is True
    assert [block["number"] for block in oldest["blocks"]] == [92, 91]
    assert oldest["start_block"] == 91
    assert oldest["has_newer"] is True and oldest["has_older"] is False


def _transfer_call(token, sender, recipient, amount):
    encoded_recipient = "0" * 24 + recipient.removeprefix("0x")
    return {
        "type": "CALL", "from": sender, "to": token, "value": "0x0",
        "input": "0xa9059cbb" + encoded_recipient + f"{amount:064x}",
    }


def test_builds_generic_swap_from_call_trace_transfers():
    trace = [{
        "txHash": "0xbeef",
        "result": {
            "type": "CALL", "from": BOT, "to": BOT, "value": "0x0", "input": "0x",
            "calls": [
                _transfer_call(TOKEN_X, BOT, POOL_A, 100),
                {
                    "type": "CALL", "from": BOT, "to": POOL_A, "value": "0x0", "input": "0x",
                    "calls": [_transfer_call(TOKEN_Y, POOL_A, BOT, 90)],
                },
            ],
        },
    }]

    result = extract_trace_swaps(trace)

    assert len(result) == 1
    assert result[0].contract_address == POOL_A
    assert (result[0].token_in_address, result[0].token_out_address) == (TOKEN_X, TOKEN_Y)
    assert (result[0].from_address, result[0].to_address) == (BOT, BOT)
