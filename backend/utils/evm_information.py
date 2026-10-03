from typing import Any, List, Dict, TypedDict, Set, Tuple, Optional
import logging
import time
from web3 import Web3
import requests
from functools import lru_cache
from pathlib import Path
from concurrent.futures import Future
from threading import Lock

def compact_extracted_name(value: object) -> str:
    """Compact one on-chain name while preserving Uniswap's version word."""
    words = str(value).strip().strip("\x00").split() if value is not None else []
    if not words:
        return ""
    keep = 2 if words[0].casefold() == "uniswap" else 1
    return " ".join(words[:keep])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GETH_TRACE_START_BLOCK = 25676797
GETH_RPC_TIMEOUT_SECONDS = 600
GETH_TRACE_RETRIES = 2
GETH_RETRY_DELAY_SECONDS = 1.0
SIMPLE_ETH_TRANSFER_GAS = 21_000
TRANSFER_EVENT_TOPIC = Web3.to_hex(
    Web3.keccak(text="Transfer(address,address,uint256)")
).lower()

# Keep the tracer readable and independently editable. It filters inside Geth,
# before JSON serialization and network transfer.
GETH_SPARSE_TRACE_TRACER = (
    Path(__file__).resolve().parent.parent / "tracers" / "storage_flow.js"
).read_text(encoding="utf-8").strip()


class TraceFetchError(RuntimeError):
    """A raw transaction trace could not be fetched or validated."""


class NoOpcodeTraceError(TraceFetchError):
    """The transaction completed without executing EVM opcodes."""


class HistoricalStateUnavailableError(TraceFetchError):
    """The native node does not retain historical state for this block."""


def _memory_words(value: str) -> List[str]:
    """Convert a contiguous hex memory snapshot into Geth-sized words."""
    body = value.lower().removeprefix("0x")
    if not body:
        return []
    if any(character not in "0123456789abcdef" for character in body):
        raise TraceFetchError("Geth trace 返回了非十六进制 memory")
    if len(body) % 64:
        body = body.ljust(((len(body) + 63) // 64) * 64, "0")
    return ["0x" + body[index:index + 64] for index in range(0, len(body), 64)]

# 标准化数据结构定义
class StandardizedStep(TypedDict):
    address: str  # 0x开头的十六进制字符串
    pc: str       # 0x开头的十六进制字符串
    opcode: str   # 操作码名称
    gascost: int  # gas消耗
    stack: List[str]  # 0x开头的十六进制字符串
    memory: List[str]
    storage: Dict[str, str]

class StandardizedTrace(TypedDict):
    tx_hash: str
    steps: List[StandardizedStep]

class ContractBytecode(TypedDict):
    address: str
    bytecode: str

# ERC20核心ABI片段（仅包含必要的检查方法和名称/符号获取方法）
ERC20_ABI_FRAGMENT = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

class TraceFormatter:
    def __init__(self, provider_url: str):
        self.provider_url = provider_url
        self.web3 = Web3(Web3.HTTPProvider(provider_url))
        # One TraceFormatter is created per block. Futures provide a small
        # single-flight cache: the first transaction probes an address while
        # concurrent transactions wait for and reuse that same result.
        self._erc20_probe_lock = Lock()
        self._erc20_probe_futures: Dict[
            str, Future[Optional[Tuple[str, int]]]
        ] = {}
        self._erc20_probe_count = 0
        self._erc20_cache_hit_count = 0
        if not self.web3.is_connected():
            raise ConnectionError("无法连接到以太坊节点，请检查provider URL是否正确")

    # 地址标准化（增加补0逻辑）
    def _normalize_address(self, address: str) -> str:
        """
        标准化以太坊地址格式，确保在0x后、数字前补0以满足42字符长度
        返回: 标准42字符地址(0x+40字符)或空字符串
        """
        if not address:
            return ""
        try:
            address_str = str(address).strip().lower().replace("0x0x", "0x")
            if address_str.startswith("0x"):
                prefix = "0x"
                body = address_str[2:]
            else:
                prefix = "0x"
                body = address_str

            # 处理32字节地址（64字符）转20字节（40字符）
            if len(body) > 40:
                body = body[-40:]

            if len(body) < 40:
                padding = "0" * (40 - len(body))
                body = padding + body

            full_address = f"{prefix}{body}"

            if len(full_address) != 42:
                raise ValueError(f"地址长度异常: {len(full_address)}字符（预期42）")

            checksum_addr = Web3.to_checksum_address(full_address)
            return checksum_addr.lower()

        except Exception as e:
            logger.debug(f"地址标准化失败: {address} - {str(e)}")
            return ""

    # PC标准化
    def _normalize_pc(self, pc: int) -> str:
        return self.web3.to_hex(pc)

    # 栈数据标准化
    def _normalize_stack(self, raw: List[str]) -> List[str]:
        normalized = []
        for item in raw or []:
            if not item:
                normalized.append("0x")
                continue
            str_item = str(item)
            if str_item.startswith("0x"):
                normalized.append(str_item)
            else:
                normalized.append(f"0x{str_item}")
        return normalized

    # 获取当前交易所属区块的矿工地址
    def get_miner_by_tx_hash(self, tx_hash: str) -> Optional[str]:
            tx = self.web3.eth.get_transaction(tx_hash)
            block_number = tx.blockNumber

            # 3. 根据区块号获取区块详情，提取miner地址
            block = self.web3.eth.get_block(block_number)
            miner_address = self.web3.to_checksum_address(block.miner)
            return miner_address

    # 获取交易发起者用户地址和根合约合约地址
    def _get_tx_from_to(self, tx_hash: str) -> str:
        try:
            tx = self.web3.eth.get_transaction(tx_hash)
            from_addr = tx.get("from", "")
            to_addr = tx.get("to", "")
            return self._normalize_address(from_addr), self._normalize_address(to_addr)
        except Exception as e:
            logger.error(f"获取交易发起者地址失败: {e}")
            return ""

    # 缓存 get_code 查询，减少 RPC 调用（基于地址）
    @lru_cache(maxsize=1024)
    def _get_code_cached(self, addr_checksum: str) -> bytes:
        try:
            return self.web3.eth.get_code(Web3.to_checksum_address(addr_checksum))
        except Exception as e:
            logger.debug(f"获取字节码 RPC 失败: {addr_checksum} - {e}")
            return b""

    @staticmethod
    def _normalize_token_text(value: object) -> str:
        """兼容现代 string 和早期 bytes32 形式的 ERC20 元数据。"""
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", errors="replace")
        # Some proxy logic contracts return a string made only of NUL padding.
        # Python's str.strip() does not remove NULs, so such a value otherwise
        # looks non-empty and becomes invisible text in legend/call-tree JSON.
        return str(value).strip().strip("\x00").strip()

    def _read_token_text(self, checksum_addr: str, function_name: str) -> str:
        for output_type in ("string", "bytes32"):
            abi = [{
                "constant": True,
                "inputs": [],
                "name": function_name,
                "outputs": [{"name": "", "type": output_type}],
                "stateMutability": "view",
                "type": "function",
            }]
            try:
                contract = self.web3.eth.contract(address=checksum_addr, abi=abi)
                value = getattr(contract.functions, function_name)().call()
                text = self._normalize_token_text(value)
                if text:
                    return text
            except Exception:
                continue
        return ""

    def _read_token_label(self, checksum_addr: str) -> str:
        """Prefer the ERC-20 symbol for display, falling back to its name."""
        return compact_extracted_name(
            self._read_token_text(checksum_addr, "symbol")
            or self._read_token_text(checksum_addr, "name")
        )


    def _probe_erc20_metadata(self, norm_addr: str) -> Optional[Tuple[str, int]]:
        """Probe one unique contract address for display metadata."""
        # 标准 ERC20 最小 ABI
        ERC20_MINI_ABI = [
            {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
        ]

        # 【通用】所有 AMM DEX 池一定会有的方法（普通代币绝对没有）
        AMM_POOL_DETECT_ABI = [
            {"constant":True,"inputs":[],"name":"factory","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
            {"constant":True,"inputs":[],"name":"token0","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
        ]

        try:
            bytecode = self._get_code_cached(norm_addr)
            if not bytecode or len(bytecode) < 20:
                return None

            checksum_addr = Web3.to_checksum_address(norm_addr)
            token_contract = self.web3.eth.contract(
                address=checksum_addr,
                abi=ERC20_MINI_ABI,
            )
            token_name = self._read_token_label(checksum_addr)
            decimals = token_contract.functions.decimals().call()
            if not isinstance(decimals, int) or not (0 <= decimals <= 18):
                return None
            if not token_name:
                logger.debug("[%s] 代币 symbol/name 为空，跳过", norm_addr)
                return None

            pool_contract = self.web3.eth.contract(
                address=checksum_addr,
                abi=AMM_POOL_DETECT_ABI,
            )
            is_amm_pool = False
            try:
                pool_contract.functions.factory().call()
                is_amm_pool = True
            except Exception:
                pass
            if not is_amm_pool:
                try:
                    pool_contract.functions.token0().call()
                    is_amm_pool = True
                except Exception:
                    pass
            if is_amm_pool:
                logger.debug("[%s] 识别为 AMM 流动性池，排除", norm_addr)
                return None
            logger.info("[%s] 识别为 ERC20 代币: %s", norm_addr, token_name)
            return token_name, int(decimals)
        except Exception as exc:
            logger.debug("[%s] 不是标准 ERC20: %s", norm_addr, exc)
            return None

    def _cached_erc20_metadata(self, norm_addr: str) -> Optional[Tuple[str, int]]:
        """Resolve metadata once per block, including negative results."""

        with self._erc20_probe_lock:
            future = self._erc20_probe_futures.get(norm_addr)
            owns_probe = future is None
            if owns_probe:
                future = Future()
                self._erc20_probe_futures[norm_addr] = future
                self._erc20_probe_count += 1
            else:
                self._erc20_cache_hit_count += 1

        if owns_probe:
            try:
                future.set_result(self._probe_erc20_metadata(norm_addr))
            except BaseException as exc:
                future.set_exception(exc)
                raise
        return future.result()

    def erc20_probe_stats(self) -> Tuple[int, int]:
        with self._erc20_probe_lock:
            return self._erc20_probe_count, self._erc20_cache_hit_count

    # 识别ERC20 token合约，包括逻辑合约识别
    def identify_erc20_contracts(
        self,
        initial_contracts: Set[str],
        steps: List[StandardizedStep],
    ) -> Tuple[Dict[str, str], Dict[str, int], Set[str]]:
        erc20_token_map: Dict[str, str] = {}
        erc20_decimals_map: Dict[str, int] = {}
        all_contracts = set(initial_contracts)

        for contract_addr in list(all_contracts):
            norm_addr = self._normalize_address(contract_addr)
            if not norm_addr:
                continue
            metadata = self._cached_erc20_metadata(norm_addr)
            if metadata is None:
                continue
            token_name, decimals = metadata
            erc20_token_map[norm_addr] = token_name
            erc20_decimals_map[norm_addr] = decimals

            # Logic contracts remain transaction-specific because the proxy's
            # DELEGATECALL target can vary by block or transaction.
            for step in steps:
                if (
                    step["opcode"] == "DELEGATECALL"
                    and step["address"] == norm_addr
                    and len(step["stack"]) >= 7
                ):
                    logic_addr = self._normalize_address(step["stack"][-2])
                    if logic_addr and logic_addr not in erc20_token_map:
                        erc20_token_map[logic_addr] = f"{token_name}_logic"
                        erc20_decimals_map[logic_addr] = decimals
                        all_contracts.add(logic_addr)

        return erc20_token_map, erc20_decimals_map, all_contracts

    def _erc20_candidates_from_evidence(
        self,
        steps: List[StandardizedStep],
        slot_map: Dict[str, str],
        receipt: Any | None,
    ) -> Set[str]:
        """Limit metadata probes to contracts with balance or Transfer evidence."""
        candidates: Set[str] = set()
        balance_slots = {str(slot).lower() for slot in slot_map}
        for step in steps:
            if str(step.get("opcode", "")).upper() not in {"SLOAD", "SSTORE"}:
                continue
            stack = step.get("stack") or []
            if not stack or str(stack[-1]).lower() not in balance_slots:
                continue
            address = self._normalize_address(step.get("RW_address", ""))
            if address:
                candidates.add(address)

        if receipt is not None:
            for log in receipt.get("logs", []) or []:
                topics = log.get("topics", []) or []
                if not topics:
                    continue
                try:
                    first_topic = (
                        topics[0].lower()
                        if isinstance(topics[0], str)
                        else Web3.to_hex(topics[0]).lower()
                    )
                except (TypeError, ValueError):
                    continue
                if first_topic != TRANSFER_EVENT_TOPIC:
                    continue
                address = self._normalize_address(log.get("address", ""))
                if address:
                    candidates.add(address)

        return candidates

    # 获取代币精度
    @lru_cache(maxsize=1024)
    def get_token_decimals(self, token_address: str) -> int:
        """
        获取代币精度：
        - ERC20代币：返回实际decimals，失败返回18
        - NFT（ERC721/ERC1155）：返回1（NFT无精度概念，兜底值）
        - 其他合约/无效地址：ERC20逻辑失败后返回18
        """
        try:
            # 步骤1：地址格式化校验
            norm_addr = self._normalize_address(token_address)
            if not norm_addr:
                logger.debug(f"代币地址 {token_address} 格式无效，ERC20兜底返回18")
                return 18
            checksum_addr = Web3.to_checksum_address(norm_addr)

            # 步骤2：先判断是否是NFT合约（核心逻辑）
            is_nft, nft_type = self._is_nft_contract(checksum_addr)
            if is_nft:
                logger.debug(f"{token_address} 是{nft_type} NFT，返回精度1")
                return 0

            # 步骤3：非NFT，按ERC20逻辑查询decimals
            DECIMALS_ABI = [{"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
            contract = self.web3.eth.contract(address=checksum_addr, abi=DECIMALS_ABI)

            decimals = contract.functions.decimals().call()
            decimals_int = int(decimals)
            # 额外校验：ERC20精度应在1-18之间，避免异常值
            if 1 <= decimals_int <= 18:
                logger.debug(f"获取 {token_address} ERC20精度成功: {decimals_int}")
                return decimals_int
            else:
                logger.warning(f"{token_address} ERC20精度异常({decimals_int})，返回默认18")
                return 18

        except Exception as e:
            # 异常分支：非NFT+ERC20查询失败 → 返回18；NFT判断失败仍走ERC20兜底
            logger.debug(f"获取 {token_address} 精度失败: {e}，非NFT则返回ERC20默认18")
            return 18

    def _is_nft_contract(self, checksum_addr: str) -> tuple[bool, str]:
        """
        内部辅助函数：判断是否是NFT合约（ERC721/ERC1155）
        返回：(是否是NFT, NFT类型/空字符串)
        """
        try:
            # 先检查是否是合约地址（非合约直接排除）
            code = self.web3.eth.get_code(checksum_addr)
            if len(code) == 0:
                return (False, "")

            # 定义supportsInterface ABI（NFT判断核心）
            INTERFACE_ABI = [
                {
                    "constant": True,
                    "inputs": [{"name": "interfaceId", "type": "bytes4"}],
                    "name": "supportsInterface",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]
            contract = self.web3.eth.contract(address=checksum_addr, abi=INTERFACE_ABI)

            # 检查ERC721接口（标准NFT）
            ERC721_INTERFACE_ID = "0x80ac58cd"
            if contract.functions.supportsInterface(ERC721_INTERFACE_ID).call():
                return (True, "ERC721")

            # 检查ERC1155接口（多类型NFT）
            ERC1155_INTERFACE_ID = "0xd9b67a26"
            if contract.functions.supportsInterface(ERC1155_INTERFACE_ID).call():
                return (True, "ERC1155")

            # 兼容非标NFT：检查ERC721核心方法ownerOf
            ERC721_OWNEROF_ABI = [
                {"constant":True,"inputs":[{"name":"tokenId","type":"uint256"}],
                "name":"ownerOf","outputs":[{"name":"","type":"address"}],"type":"function"}
            ]
            erc721_contract = self.web3.eth.contract(address=checksum_addr, abi=ERC721_OWNEROF_ABI)
            try:
                # 仅测试方法是否存在，传入任意tokenId（0）
                erc721_contract.functions.ownerOf(0).call()
                return (True, "ERC721（非标）")
            except:
                pass

            # 非NFT合约
            return (False, "")

        except Exception as e:
            logger.debug(f"判断 {checksum_addr} 是否为NFT失败: {e}，按ERC20处理")
            return (False, "")

    def _strip_0x(self, s: str) -> str:
        '''
        去掉字符串前的 0x 或 0X 前缀
        '''
        if not s:
            return ""
        s2 = str(s)
        if s2.startswith("0x") or s2.startswith("0X"):
            return s2[2:]
        return s2

    def _significant_hex_length(self, raw: str) -> int:
        """
        计算去掉 0x 前缀并去除前导零后的十六进制字符长度
        """
        if not raw:
            return 0
        s = self._strip_0x(raw).lower()
        # 去除前导零
        s = s.lstrip("0")
        return len(s)

    @staticmethod
    def _call_has_nonzero_value(opcode: str, raw_stack: List[str]) -> bool:
        """Return whether a CALL instruction has a non-zero ETH value operand."""
        if opcode != "CALL" or len(raw_stack) < 3:
            return False
        try:
            return int(str(raw_stack[-3]), 16) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _validate_raw_trace(raw_trace: object, source: str) -> Dict:
        if not isinstance(raw_trace, dict):
            raise TraceFetchError(f"{source} trace 返回值不是对象")
        struct_logs = raw_trace.get("structLogs")
        if not isinstance(struct_logs, list):
            raise TraceFetchError(f"{source} trace 缺少有效 structLogs")
        if not struct_logs:
            raise NoOpcodeTraceError(f"{source} trace 不包含 opcode")
        return raw_trace

    def _run_geth_rpc_request(
        self,
        method: str,
        params: List[Any],
        label: str,
        subject: str,
    ) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        # GETH_API normally points to a LAN node. A dedicated direct session
        # prevents machine-wide HTTP proxies from intercepting the request and
        # removes the runtime dependency on Foundry's `cast` executable.
        with requests.Session() as session:
            session.trust_env = False
            for attempt in range(GETH_TRACE_RETRIES + 1):
                try:
                    response = session.post(
                        self.provider_url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                        timeout=GETH_RPC_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict):
                        raise TraceFetchError(f"Geth {label}响应不是对象")
                    if body.get("error") is not None:
                        error = body["error"]
                        detail = (
                            error.get("message") or str(error)
                            if isinstance(error, dict)
                            else str(error)
                        )
                        raise TraceFetchError(f"Geth {label} RPC 错误: {detail}")
                    if "result" not in body:
                        raise TraceFetchError(f"Geth {label}响应缺少 result")
                    return body["result"]
                except (requests.RequestException, ValueError, TraceFetchError) as exc:
                    detail = str(exc)
                    normalized_detail = detail.lower()
                    historical_state_missing = any(
                        marker in normalized_detail
                        for marker in (
                            "historical state is not available",
                            "historical state unavailable",
                            "missing trie node",
                        )
                    )
                    if historical_state_missing or attempt >= GETH_TRACE_RETRIES:
                        raise TraceFetchError(
                            f"Geth {label}请求失败（尝试 {attempt + 1}/"
                            f"{GETH_TRACE_RETRIES + 1}）: {detail}"
                        ) from exc

                    wait_seconds = min(
                        GETH_RETRY_DELAY_SECONDS * (attempt + 1),
                        3.0,
                    )
                    logger.warning(
                        "Geth %s临时失败，%s 秒后重试 (%d/%d): target=%s error=%s",
                        label,
                        f"{wait_seconds:g}",
                        attempt + 1,
                        GETH_TRACE_RETRIES,
                        subject,
                        detail,
                    )
                    time.sleep(wait_seconds)

        raise AssertionError("unreachable Geth request retry state")

    def _run_geth_trace_request(
        self,
        tx_hash: str,
        trace_options: Dict,
        label: str,
    ) -> Dict:
        result = self._run_geth_rpc_request(
            "debug_traceTransaction",
            [tx_hash, trace_options],
            label,
            tx_hash,
        )
        if not isinstance(result, dict):
            raise TraceFetchError(f"Geth {label}返回值不是对象")
        return result

    @staticmethod
    def _adapt_sparse_trace(raw_trace: Dict) -> Dict:
        """Validate and convert only the sparse memory snapshots to word lists."""
        struct_logs = raw_trace.get("structLogs")
        if not isinstance(struct_logs, list):
            raise TraceFetchError("Geth 稀疏 trace 缺少 structLogs")

        adapted_logs: List[Dict] = []
        required = {"pc", "op", "gas", "gasCost", "depth", "stack", "memory", "storage"}
        for index, raw_step in enumerate(struct_logs):
            if not isinstance(raw_step, dict):
                raise TraceFetchError(f"Geth 稀疏 trace step {index} 不是对象")
            missing = required.difference(raw_step)
            if missing:
                raise TraceFetchError(
                    f"Geth 稀疏 trace step {index} 缺少字段: {', '.join(sorted(missing))}"
                )
            if not isinstance(raw_step["stack"], list):
                raise TraceFetchError(f"Geth 稀疏 trace step {index} stack 无效")
            if not isinstance(raw_step["storage"], dict):
                raise TraceFetchError(f"Geth 稀疏 trace step {index} storage 无效")

            memory = raw_step["memory"]
            if isinstance(memory, str):
                memory = _memory_words(memory)
            elif not isinstance(memory, list):
                raise TraceFetchError(f"Geth 稀疏 trace step {index} memory 无效")

            step = dict(raw_step)
            step["memory"] = memory
            adapted_logs.append(step)

        adapted = dict(raw_trace)
        adapted["structLogs"] = adapted_logs
        return adapted

    def _fetch_geth_trace(self, tx_hash: str) -> Dict:
        sparse_trace = self._run_geth_trace_request(
            tx_hash,
            {
                "tracer": GETH_SPARSE_TRACE_TRACER,
                "timeout": f"{GETH_RPC_TIMEOUT_SECONDS}s",
            },
            "稀疏 trace",
        )
        adapted = self._adapt_sparse_trace(sparse_trace)
        return self._validate_raw_trace(adapted, "Geth")

    def fetch_block_traces(self, block_number: int) -> Dict[str, Dict]:
        """Trace a block once and return filtered traces keyed by transaction hash."""

        trace_options = {
            "tracer": GETH_SPARSE_TRACE_TRACER,
            "timeout": f"{GETH_RPC_TIMEOUT_SECONDS}s",
        }
        result = self._run_geth_rpc_request(
            "debug_traceBlockByNumber",
            [hex(block_number), trace_options],
            "整块稀疏 trace",
            str(block_number),
        )
        if not isinstance(result, list):
            raise TraceFetchError("Geth 整块稀疏 trace 返回值不是数组")

        traces: Dict[str, Dict] = {}
        for index, item in enumerate(result):
            if not isinstance(item, dict):
                raise TraceFetchError(f"Geth 整块 trace 第 {index} 项不是对象")
            tx_hash = str(item.get("txHash") or "").lower()
            raw_trace = item.get("result")
            if not tx_hash or not isinstance(raw_trace, dict):
                logger.warning(
                    "整块 trace 第 %d 项无可用结果，将单独抓取: tx=%s error=%s",
                    index,
                    tx_hash or "unknown",
                    item.get("error"),
                )
                continue
            traces[tx_hash] = self._adapt_sparse_trace(raw_trace)
        return traces

    def _fetch_raw_trace(
        self,
        tx_hash: str,
        block_number: int,
        gas_used: Optional[int] = None,
        preloaded_trace: Optional[Dict] = None,
    ) -> Dict:
        if block_number < GETH_TRACE_START_BLOCK:
            raise HistoricalStateUnavailableError(
                f"区块 {block_number} 早于原生节点历史 state 分界区块 "
                f"{GETH_TRACE_START_BLOCK}，无法获取 trace"
            )
        if gas_used == SIMPLE_ETH_TRANSFER_GAS:
            raise NoOpcodeTraceError(
                f"交易 gasUsed={SIMPLE_ETH_TRANSFER_GAS}，"
                "判定为无 opcode 的简单 ETH 转账"
            )

        if preloaded_trace is None:
            raw_trace = self._fetch_geth_trace(tx_hash)
        else:
            raw_trace = self._validate_raw_trace(preloaded_trace, "Geth 整块")
        logger.info("成功从 Geth 获取 trace: %s", tx_hash)
        return raw_trace

    # 获取并标准化trace,计算contract address，并在遍历 CALL 时分类 addresses
    # 改用 foundry 的 cast 方法
    def get_standardized_trace(
        self,
        tx_hash: str,
        *,
        transaction: Any | None = None,
        receipt: Any | None = None,
        preloaded_trace: Dict | None = None,
    ) -> Dict:
        """
        返回一个 dict，包含至少以下字段：
        - tx_hash
        - steps: 标准化的 steps 列表（保持原来格式）
        - contracts_addresses: list（在遍历 CALL 时识别到的合约地址）
        - erc20_token_map: dict（ERC20合约地址 -> token名称，包含代理和逻辑合约）
        - slot_map: slot -> normalized address 映射（通过 steps 计算）
        - users_addresses: 最终用户地址集合（由 addresses_from_slots 与中间的 users_addresses_from_CALL 合并去重并减去contracts_addresses）
        - tx_sender_address: 交易发起者（from）地址
        说明：
        - users_addresses_from_CALL 仍在函数内部作为中间结果计算，但不会写入返回值
        """
        try:
            # Whole-block callers pass these objects directly to avoid repeated
            # eth_getTransactionByHash/eth_getBlockByNumber calls per transaction.
            if transaction is None:
                transaction = self.web3.eth.get_transaction(tx_hash)
            tx_sender_address = self._normalize_address(transaction.get("from", ""))
            initial_address = self._normalize_address(transaction.get("to", ""))
            logger.info(f"交易 {tx_hash} 的发起者地址: {tx_sender_address}")

            block_number = transaction.get("blockNumber")
            if block_number is None:
                raise TraceFetchError("交易尚未打包，无法获取 trace")
            gas_used: Optional[int] = None
            try:
                if receipt is None:
                    receipt = self.web3.eth.get_transaction_receipt(tx_hash)
                gas_used = int(receipt.get("gasUsed", 0))
            except Exception as exc:
                logger.warning("无法获取 gasUsed，将继续请求原生节点 trace: %s", exc)
            raw_trace = self._fetch_raw_trace(
                tx_hash,
                int(block_number),
                gas_used,
                preloaded_trace,
            )

            struct_logs = raw_trace.get("structLogs", [])
            steps: List[StandardizedStep] = []

            # 记录执行的代码所属合约地址
            current_address = initial_address
            next_address = initial_address
            call_stack = [initial_address] if initial_address else []

            # 记录当前上下文的读写地址
            RW_address = initial_address
            next_RW_address = initial_address
            RW_stack = [initial_address] if initial_address else []

            # 在遍历时收集 contracts_addresses 和 users_addresses_from_CALL
            contracts_addresses: Set[str] = set()

            contracts_addresses.add(initial_address)

            users_addresses_from_CALL: Set[str] = set()

            for i, step in enumerate(struct_logs):
                pc = step.get("pc", 0)
                opcode = step.get("op", "").upper()
                raw_stack = step.get("stack", [])
                raw_memory = step.get("memory",[])
                raw_storage = step.get("storage", {})
                depth = step.get("depth",[])
                # 单独处理CALL合约时的gascost计算
                # 执行CALL时会向合约预支付一笔gas，在trace中记录为CALL的gasCost
                # CALL本身的gascost是预支付的gasCost减去CALL下一步剩下的gasleft。
                if "enteredChildCall" in step:
                    entered_child_call = bool(step.get("enteredChildCall"))
                else:
                    # Backward compatibility for traces written before opcode
                    # filtering moved into the custom Geth tracer.
                    entered_child_call = (
                        opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}
                        and i + 1 < len(struct_logs)
                        and int(struct_logs[i + 1].get("depth", depth)) > int(depth)
                    )
                if entered_child_call:
                    next_gasleft = step.get("nextGas")
                    if next_gasleft is None and i + 1 < len(struct_logs):
                        next_gasleft = struct_logs[i + 1].get("gas", 0)
                    next_gasleft = next_gasleft or 0
                    gasCost = step.get("gasCost", 0)
                    gascost = gasCost - next_gasleft
                else:
                    if i < len(struct_logs) - 1:
                        gascost = step.get("gasCost", 0)
                    else:
                        gascost = 0  # 最后一步一定是终止指令，gascost固定是0

                # CALL 类指令,增加地址分类逻辑
                if opcode in {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}:
                    if len(raw_stack) >= 2:
                        # 1. 从 raw_stack[-2] 解析出地址（保持原变量名/索引）
                        to_address_raw = raw_stack[-2]

                        # 先判断 hex 位数是否大于 2（按去 0x 并去前导 0 的长度）
                        hex_len = self._significant_hex_length(to_address_raw)

                        # 默认不认为是有效地址，只有经过标准化才认为有效（is_valid_address 用于上下文切换）
                        to_address = ""
                        is_valid_address = False

                        # 预先判断下一步 pc 是否为 0x0（用于新的合约/用户分类）
                        next_step_pc = step.get("nextPC")
                        has_next_step = next_step_pc is not None
                        if next_step_pc is None and i < len(struct_logs) - 1:
                            next_step_pc = struct_logs[i + 1].get("pc", 0)
                            has_next_step = True
                        if next_step_pc is not None:
                            next_step_pc = self._normalize_pc(next_step_pc)
                        is_next_pc_zero = has_next_step and next_step_pc == "0x0"

                        # 只有当 hex_len > 2 时才进行标准化与分类（不再通过 bytecode 查询判断）
                        if hex_len > 2:
                            # 先标准化
                            norm_addr = self._normalize_address(to_address_raw)
                            if norm_addr:
                                to_address = norm_addr
                                is_valid_address = True

                                # 如果下一步 pc 是 0x0，则视为合约地址。
                                # 只有携带非零 ETH 的 CALL 目标才作为用户候选；
                                # 零 value CALL 以及不传 ETH 的其他 CALL 类指令不应污染资产图 legend。
                                if is_next_pc_zero:
                                    contracts_addresses.add(to_address)
                                else:
                                    if 10 <= hex_len <= 40 and self._call_has_nonzero_value(opcode, raw_stack):
                                        users_addresses_from_CALL.add(to_address)
                            else:
                                # 标准化失败，保持 to_address 为空，is_valid_address=False
                                pass
                        else:
                            # hex_len <= 2：被视为预编译合约或特殊地址，忽略（不标准化、不分类）
                            pass

                        # 每个实际进入的调用帧都同时保存 code/storage 父上下文。
                        # DELEGATECALL/CALLCODE 虽然继承父 storage 地址，仍然必须在
                        # RW_stack 中占一帧；否则子帧 RETURN 会误弹外层 CALL 保存的地址。
                        if is_valid_address and entered_child_call:
                            call_stack.append(current_address)
                            RW_stack.append(RW_address)
                            next_address = to_address
                            if opcode in {"CALL", "STATICCALL"}:
                                next_RW_address = to_address
                            else:  # DELEGATECALL / CALLCODE
                                next_RW_address = RW_address
                        else:
                            next_address = current_address
                            next_RW_address = RW_address
                    else:
                        next_address = current_address
                        next_RW_address = RW_address

                # CREATE 类指令
                elif opcode in ["CREATE", "CREATE2"]:
                    new_address = ""
                    if new_address:
                        new_address = self._normalize_address(new_address)
                        has_next_step = i < len(struct_logs) - 1
                        if has_next_step:
                            next_step_pc = self._normalize_pc(struct_logs[i + 1].get("pc", 0))
                            if next_step_pc == "0x0" and new_address:
                                call_stack.append(current_address)
                                next_address = new_address
                                RW_stack.append(RW_address)
                                next_RW_address = new_address
                            else:
                                next_address = current_address
                                next_RW_address = RW_address
                        else:
                            next_address = current_address
                            next_RW_address = RW_address
                    else:
                        next_address = current_address
                        next_RW_address = RW_address

                # 终止指令
                elif opcode in {"STOP", "RETURN", "REVERT", "INVALID", "SELFDESTRUCT"}:
                    if len(call_stack) > 1:
                        next_address = call_stack.pop()
                    else:
                        next_address = current_address
                    if len(RW_stack) > 1:
                        next_RW_address = RW_stack.pop()
                    else:
                        next_RW_address = RW_address

                # 记录当前步骤（保持原来格式）
                embedded_address = self._normalize_address(step.get("address", ""))
                embedded_rw_address = self._normalize_address(step.get("storageAddress", ""))
                result = step.get("result")
                normalized_result = (
                    self._normalize_stack([result])[0] if result is not None else None
                )
                standardized_storage = raw_storage if isinstance(raw_storage, dict) else {}
                if opcode == "SLOAD" and raw_stack and normalized_result is not None:
                    standardized_storage = dict(standardized_storage)
                    standardized_storage[str(raw_stack[-1])] = normalized_result

                steps.append({
                    "address": embedded_address or current_address,
                    "RW_address": embedded_rw_address or RW_address,
                    "depth": depth,
                    "pc": self._normalize_pc(pc),
                    "opcode": opcode,
                    "gascost": gascost,
                    "stack": self._normalize_stack(raw_stack),
                    "memory": raw_memory,
                    "memory_offset": step.get("memoryOffset"),
                    "memory_size": step.get("memorySize"),
                    "result": normalized_result,
                    # Per-step SLOAD/SSTORE delta from the incremental tracer.
                    "storage": standardized_storage,
                })

                current_address = next_address
                RW_address = next_RW_address

            # 中间过程 users_addresses_from_CALL 已收集完毕（但不返回）
            print(f"通过 CALL 类指令识别到合约地址数量: {len(contracts_addresses)}，用户地址数量: {len(users_addresses_from_CALL)}")

            # 余额 mapping 与 allowance 二级 mapping 分开保留。后者不参与真实
            # 余额配对，但会作为可选的 TFG 虚拟授权边。
            slot_map, allowance_slot_map = self.extract_storage_slot_maps({"steps": steps})
            erc20_candidates = self._erc20_candidates_from_evidence(
                steps,
                slot_map,
                receipt,
            )
            erc20_token_map, erc20_decimals_map, token_contracts = (
                self.identify_erc20_contracts(erc20_candidates, steps)
            )
            contracts_addresses.update(token_contracts)
            print(
                f"从 {len(erc20_candidates)} 个余额/Transfer 候选中识别出 "
                f"{len(erc20_token_map)} 个 ERC20 代理或逻辑合约"
            )

            addresses_from_slots: Set[str] = set(slot_map.values())
            addresses_from_allowances: Set[str] = {
                address
                for item in allowance_slot_map.values()
                for address in (item["owner_address"], item["spender_address"])
            }
            print(f"通过 slot_map 识别到地址数量: {len(addresses_from_slots)}")
            final_users_addresses_set: Set[str] = (
                addresses_from_slots
                .union(addresses_from_allowances)
                .union(users_addresses_from_CALL)
            ) - contracts_addresses

            # ========== 新增：将交易发起者加入用户地址集合 ==========
            if tx_sender_address and tx_sender_address not in contracts_addresses:
                final_users_addresses_set.add(tx_sender_address)
                logger.info(f"已将交易发起者 {tx_sender_address} 加入用户地址集合")

            # 返回时新增 erc20_token_map 和 tx_sender_address 字段
            return {
                "tx_hash": tx_hash,
                "steps": steps,
                "opcode_count": int(raw_trace.get("opcodeCount", len(steps))),
                "filtered_step_count": len(steps),
                "contracts_addresses": sorted(list(contracts_addresses)),
                "erc20_token_map": erc20_token_map,  # 新增：ERC20地址->名称映射（包含代理和逻辑合约）
                "erc20_decimals_map": erc20_decimals_map,
                "slot_map": slot_map,
                "allowance_slot_map": allowance_slot_map,
                "users_addresses": sorted(list(final_users_addresses_set)),
                "tx_sender_address": tx_sender_address  # 新增：交易发起者地址
            }

        except NoOpcodeTraceError:
            # A plain ETH transfer or a precompile-only execution has no
            # relevant opcode records and is handled by BlockAnalyzer.
            raise
        except Exception as e:
            logger.error(f"处理trace失败: {e}")
            raise

    # 提取合约地址（保留原有简单实现）
    def extract_contracts_from_trace(self, standardized_trace: StandardizedTrace) -> Set[str]:
        return {step["address"] for step in standardized_trace["steps"] if step["address"]}

    # 从 KECCAK 的 memory 输入中区分余额 slot 与 allowance 二级 mapping slot。
    def extract_storage_slot_maps(
        self,
        standardized_trace: Dict,
    ) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        """
        按 trace 顺序处理 KECCAK：
        - stack[-1] 是 memory offset，stack[-2] 是 memory size
        - 64 字节的标准 mapping 输入由 address word 和 base slot word 组成
        - 下一 step 的 stack[-1] 是 KECCAK 结果，即 storage slot
        - 一级 mapping 作为普通地址 slot
        - base slot 若是先前的一级 mapping hash，则当前结果单独作为 allowance slot
        - 最后只返回实际被 SLOAD/SSTORE 使用过的 slot
        """
        steps = standardized_trace.get("steps", []) if isinstance(standardized_trace, dict) else standardized_trace["steps"]

        def normalize_word(value: str) -> Optional[str]:
            """标准化为 0x + 64 位小写 hex，供 slot 比较使用。"""
            if value is None:
                return None
            body = self._strip_0x(str(value).strip()).lower() or "0"
            if len(body) > 64 or any(ch not in "0123456789abcdef" for ch in body):
                return None
            return f"0x{body.zfill(64)}"

        def word_to_int(value: str) -> Optional[int]:
            word = normalize_word(value)
            return int(word, 16) if word is not None else None

        def flatten_memory(memory: List[str]) -> Optional[str]:
            """将 memory 的 32 字节分段拼成连续、不含 0x 的 hex。"""
            words = []
            for raw_word in memory:
                word = normalize_word(raw_word)
                if word is None:
                    return None
                words.append(word[2:])
            return "".join(words)

        # canonical slot -> SLOAD/SSTORE 栈中实际出现的字符串格式
        used_slots: Dict[str, Set[str]] = {}
        # canonical hash -> mapping 计算来源。未被直接读写的一级 owner hash
        # 也必须保留，因为它会成为二级 allowance mapping 的 base。
        mapping_hashes: Dict[str, Dict[str, Any]] = {}

        for index, step in enumerate(steps):
            opcode = step.get("opcode", "").upper()
            stack = step.get("stack", []) or []

            if opcode in {"SLOAD", "SSTORE"} and stack:
                canonical_slot = normalize_word(stack[-1])
                if canonical_slot is not None:
                    used_slots.setdefault(canonical_slot, set()).add(str(stack[-1]).lower())

            if opcode not in {"SHA3", "KECCAK256", "KECCAK"}:
                continue
            if len(stack) < 2:
                continue

            memory_offset = word_to_int(stack[-1])
            memory_size = word_to_int(stack[-2])
            if memory_offset is None or memory_size != 64:
                continue

            memory = step.get("memory", []) or []
            if not isinstance(memory, list):
                continue
            memory_hex = flatten_memory(memory)
            if memory_hex is None:
                continue

            captured_offset = step.get("memory_offset")
            if captured_offset is not None:
                try:
                    if int(captured_offset) != memory_offset:
                        continue
                except (TypeError, ValueError):
                    continue
                # Filtered traces contain only the requested memory slice.
                start = 0
            else:
                # Older cached traces contain the complete memory snapshot.
                start = memory_offset * 2
            end = start + memory_size * 2
            if end > len(memory_hex):
                continue
            keccak_input = memory_hex[start:end]
            address_word = keccak_input[:64]
            base_slot = f"0x{keccak_input[64:128]}"

            # mapping key 必须是左侧补 12 字节 0 的标准 address word。
            if address_word[:24] != "0" * 24:
                continue
            significant_address_length = len(address_word.lstrip("0"))
            if not 20 <= significant_address_length <= 40:
                continue

            result = step.get("result")
            if result is None and index + 1 < len(steps):
                next_stack = steps[index + 1].get("stack", []) or []
                result = next_stack[-1] if next_stack else None
            slot = normalize_word(result)
            if slot is None:
                continue

            address = self._normalize_address(f"0x{address_word[-40:]}")
            if address:
                parent = mapping_hashes.get(base_slot)
                mapping_hashes[slot] = {
                    "key_address": address,
                    "base_slot": base_slot,
                    "depth": int(parent["depth"]) + 1 if parent else 1,
                    "parent_hash": base_slot if parent else None,
                    "hash_step": index,
                    "hash_pc": step.get("pc"),
                }

        # 对外保留 trace 栈中的 slot 格式，兼容下游的直接字符串查表。
        balance_slots = {
            raw_slot: item["key_address"]
            for slot, item in mapping_hashes.items()
            if item["depth"] == 1
            for raw_slot in used_slots.get(slot, set())
        }
        allowance_slots = {
            raw_slot: {
                "owner_address": mapping_hashes[item["parent_hash"]]["key_address"],
                "spender_address": item["key_address"],
                "owner_mapping_hash": item["parent_hash"],
                "owner_mapping_base_slot": mapping_hashes[item["parent_hash"]]["base_slot"],
                "owner_hash_step": mapping_hashes[item["parent_hash"]]["hash_step"],
                "spender_hash_step": item["hash_step"],
            }
            for slot, item in mapping_hashes.items()
            if item["depth"] == 2 and item["parent_hash"] in mapping_hashes
            for raw_slot in used_slots.get(slot, set())
        }
        return balance_slots, allowance_slots

    def extract_slot_address_map(self, standardized_trace: Dict) -> Dict[str, str]:
        """Backward-compatible balance-slot-only view."""
        balance_slots, _ = self.extract_storage_slot_maps(standardized_trace)
        return balance_slots

    # 获取单个合约字节码（使用缓存）
    def get_contract_bytecode(self, contract_address: str) -> ContractBytecode:
        normalized_addr = self._normalize_address(contract_address)
        if not normalized_addr or not self.web3.is_address(normalized_addr):
            raise ValueError(f"无效地址（需0x开头的十六进制）: {contract_address}")

        try:
            bytecode = self._get_code_cached(normalized_addr)
            return {
                "address": normalized_addr,
                "bytecode": self.web3.to_hex(bytecode)
            }
        except Exception as e:
            logger.error(f"获取合约字节码失败: {e}")
            raise

    # 获取所有涉及的合约字节码
    def get_all_contracts_bytecode(self, all_contracts) -> List[ContractBytecode]:
        return [self.get_contract_bytecode(addr) for addr in all_contracts if addr]
