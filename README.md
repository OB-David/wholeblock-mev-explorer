# Whole Block TFG

Whole Block TFG 是一个面向 Ethereum 区块的价值流可视化与 MEV 探索工具。首页展示最新区块以及套利、Sandwich 快速标签；进入区块详情后，可查看交易级 Token Flow Graph、资金路径和检测结果。

## 功能

- 实时展示链头与最新 180 个区块。
- 从 receipt 中解析 Uniswap V2/V3 `Swap` Event，并结合 `callTracer` 推断其他资金流。
- 快速标记套利路径与 Sandwich 交易。
- 按需生成完整区块 Token Flow Graph，支持交易、边和周期维度查看。
- 扫描结果与图数据本地缓存，避免重复分析。

## 同步模型

后端每次启动都把当时链头作为本次同步锚点：

- 链头线程优先扫描启动时的最新区块，之后持续跟踪新块。
- 链头首次扫描成功后，历史线程从 `latest - 1` 向下回填，已扫描区块会自动跳过。
- 前端直接读取最新区块头，因此打开系统后可立即看到最新区块，MEV 标签在扫描完成后更新。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js `^20.19.0` 或 `>=22.12.0`
- 开启 HTTP JSON-RPC 和 `debug` API 的 Geth 节点
- 节点需支持 `eth_getBlockReceipts` 与 `debug_traceBlockByNumber`

> 历史回填能到达的最早高度取决于节点保留的历史 state。

## 配置

复制示例配置：

```bash
cp backend/.env.example backend/.env
```

然后编辑 `backend/.env`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GETH_API` | `http://127.0.0.1:8545` | Geth HTTP JSON-RPC 地址 |
| `TRACE_WORKERS` | `2` | 完整 TFG 分析的并发数，限制在 1–8 |
| `MEV_POLL_SECONDS` | `3` | 链头轮询间隔（秒） |
| `MEV_BACKFILL_START_BLOCK` | `25676797` | 历史回填下界 |
| `CORS_ORIGINS` | 本地前端地址 | 逗号分隔的允许来源 |

`backend/.env` 不会被 Git 跟踪。请不要提交带凭据的 RPC URL。

## 本地开发

启动后端：

```bash
cd backend
uv sync
uv run uvicorn server:app --host 0.0.0.0 --port 9021
```

在另一个终端启动前端：

```bash
cd frontend
npm ci
npm run dev
```

访问 <http://localhost:9020>。Vite 开发服务器会将 API 请求转发到 `http://127.0.0.1:9021`。

## 生产构建

```bash
cd frontend
npm ci
npm run build

cd ../backend
uv sync
uv run uvicorn server:app --host 0.0.0.0 --port 9021
```

前端构建到 `frontend/dist/` 后，FastAPI 会同时提供静态前端，此时访问 <http://localhost:9021>。

## 命令行分析

不启动 Web 界面也可以分析指定区块：

```bash
cd backend
uv run python cli.py latest
uv run python cli.py 25976348 --force
```

结果会写入项目根目录下的 `data/`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/explorer?limit=180` | 读取最新区块和快速 MEV 标签 |
| `GET` | `/api/latest` | 读取当前链头 |
| `POST` | `/api/analyze` | 提交完整 TFG 分析任务 |
| `GET` | `/api/jobs/{job_id}` | 查询分析进度 |
| `GET` | `/api/blocks/{block_number}` | 读取已生成的图数据 |

## 测试

```bash
uv run --with pytest pytest -q
```

## 目录结构

```text
backend/       FastAPI API、trace 分析和 MEV 扫描器
frontend/      Vue 3 + TypeScript + Vite 前端
tests/         Python 单元测试
data/          本地生成数据，不提交到 Git
```
