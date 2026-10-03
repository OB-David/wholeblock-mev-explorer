from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel

from analyzer import analyzer_from_env, load_graph_file
from mev_scanner import MevScanner, scanner_from_env


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
load_dotenv(Path(__file__).resolve().parent / ".env")
logger = logging.getLogger(__name__)
mev_scanner: MevScanner | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global mev_scanner
    try:
        mev_scanner = scanner_from_env(DATA)
        mev_scanner.start()
    except Exception:
        logger.exception("MEV 区块扫描器启动失败；TFG 接口仍可使用")
        mev_scanner = None
    yield
    if mev_scanner is not None:
        mev_scanner.stop()


cors_origins = [
    value.strip()
    for value in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:9020,http://127.0.0.1:9020",
    ).split(",")
    if value.strip()
]

app = FastAPI(title="Whole Block TFG", version="1.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="block-job")
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


class AnalyzeRequest(BaseModel):
    block: int | str = "latest"
    force: bool = False


@app.get("/api/explorer")
def explorer(limit: int = 180, before: int | None = None) -> dict[str, Any]:
    if limit < 1 or limit > 240:
        raise HTTPException(422, "limit 必须在 1 到 240 之间")
    if before is not None and before < 0:
        raise HTTPException(422, "before 不能为负数")
    if mev_scanner is None:
        raise HTTPException(503, "MEV 区块扫描器不可用，请检查 GETH_API")
    try:
        return mev_scanner.explorer(limit=limit, before=before)
    except Exception as exc:
        logger.exception("读取区块探索数据失败")
        raise HTTPException(502, f"读取链上区块失败: {exc}") from exc


@app.get("/api/latest")
def latest() -> dict[str, int]:
    analyzer = analyzer_from_env(DATA)
    number = analyzer.resolve_block("latest")
    block = analyzer.web3.eth.get_block(number, full_transactions=False)
    return {"number": number, "transaction_count": len(block.transactions)}


@app.post("/api/analyze", status_code=202)
def start_analysis(request: AnalyzeRequest) -> dict[str, str]:
    value = str(request.block).strip().lower()
    if value != "latest" and (not value.isdigit() or int(value) < 0):
        raise HTTPException(422, "请输入非负区块号或 latest")
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "status": "queued", "completed": 0, "total": 0, "message": "等待执行"}
    executor.submit(run_job, job_id, value, request.force)
    return {"job_id": job_id}


def run_job(job_id: str, block: str, force: bool = False) -> None:
    def progress(completed: int, total: int, message: str) -> None:
        with jobs_lock:
            jobs[job_id].update(status="running", completed=completed, total=total, message=message)
    try:
        analyzer = analyzer_from_env(DATA)
        graph = analyzer.analyze(block, progress, force=force)
        with jobs_lock:
            jobs[job_id].update(status="complete", completed=len(graph["transactions"]), total=len(graph["transactions"]), block=graph["block"]["number"], message="完成")
    except Exception as exc:
        with jobs_lock:
            jobs[job_id].update(status="error", message=f"{type(exc).__name__}: {exc}")


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return dict(job)


@app.get("/api/blocks/{block_number}")
def graph(block_number: int) -> FileResponse:
    path = DATA / str(block_number) / "graph.json"
    if load_graph_file(path, block_number) is None:
        raise HTTPException(404, "该区块尚未分析")
    return FileResponse(path, media_type="application/json")


DIST = ROOT / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        return FileResponse(DIST / "index.html")
