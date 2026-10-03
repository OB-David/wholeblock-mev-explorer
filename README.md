# Whole Block TFG

Whole Block TFG is an Ethereum value-flow visualizer and MEV explorer. The home page shows recent blocks with fast arbitrage and sandwich labels. Opening a block builds an interactive, transaction-level Token Flow Graph (TFG) with transfer paths, cycles, balance changes, and MEV findings.

## Features

- Shows the live chain head and recent Ethereum blocks.
- Decodes Uniswap V2/V3 `Swap` events from receipts and supplements them with `callTracer`-inferred flows.
- Detects candidate arbitrage routes and sandwich transactions.
- Builds full-block Token Flow Graphs on demand.
- Supports block, transaction, edge-timeline, cycle, and MEV-focused views.
- Caches scan labels, traces, and generated graphs locally.
- Provides paginated access to the 1,800-block startup history window.

## Synchronization model

Every backend start anchors a new tracking session at the current chain head:

1. The head worker scans the startup head first, then continuously follows new blocks.
2. After the initial head scan succeeds, the history worker scans the preceding 1,799 blocks in descending order.
3. Blocks already present in the local scan database are skipped.
4. The explorer reads block headers directly from the node, so the newest blocks are visible immediately while MEV labels are still being computed.

The 1,800-block limit applies to backward filling. Blocks produced after startup continue to be tracked normally.
Quick-scan rows older than the active startup window are pruned on restart. Full TFG artifacts under `data/<block>/` are never removed by this retention policy.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js `^20.19.0` or `>=22.12.0`
- A Geth node with HTTP JSON-RPC and the `debug` API enabled
- RPC support for `eth_getBlockReceipts` and `debug_traceBlockByNumber`

The node must retain historical state for every block that you want to trace.

## Configuration

Copy the example environment file:

```bash
cp backend/.env.example backend/.env
```

Then edit `backend/.env`:

| Variable | Default | Description |
| --- | --- | --- |
| `GETH_API` | `http://127.0.0.1:8545` | Geth HTTP JSON-RPC endpoint |
| `TRACE_WORKERS` | `2` | Full-TFG trace concurrency, clamped to 1–8 |
| `MEV_POLL_SECONDS` | `3` | Chain-head polling interval in seconds |
| `MEV_HISTORY_BLOCKS` | `1800` | Startup tracking window, including the startup head |
| `CORS_ORIGINS` | Local frontend URLs | Comma-separated allowed browser origins |

`backend/.env` is ignored by Git. Never commit an authenticated RPC URL or other credentials.

## Local development

Start the backend:

```bash
cd backend
uv sync
uv run uvicorn server:app --host 0.0.0.0 --port 9021
```

In another terminal, start the frontend:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://localhost:9020>. The Vite development server sends API requests to `http://127.0.0.1:9021`.

## Production build

```bash
cd frontend
npm ci
npm run build

cd ../backend
uv sync
uv run uvicorn server:app --host 0.0.0.0 --port 9021
```

When `frontend/dist/` exists, FastAPI serves the built frontend together with the API. Open <http://localhost:9021>.

## Command-line analysis

Analyze a block without opening the web interface:

```bash
cd backend
uv run python cli.py latest
uv run python cli.py 25976348 --force
```

Generated traces and graph files are written under the repository's `data/` directory.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/explorer?limit=180&before=...` | Read one page of block headers and fast MEV labels |
| `GET` | `/api/latest` | Read the current chain head |
| `POST` | `/api/analyze` | Submit a full-TFG analysis job |
| `GET` | `/api/jobs/{job_id}` | Read analysis progress |
| `GET` | `/api/blocks/{block_number}` | Read a generated block graph |

## Tests

From the repository root:

```bash
uv run --project backend --with pytest python -m pytest -q
```

## Project layout

```text
backend/       FastAPI API, trace analysis, and MEV scanner
frontend/      Vue 3, TypeScript, and Vite frontend
tests/         Python unit tests
data/          Generated local data; excluded from Git
```
