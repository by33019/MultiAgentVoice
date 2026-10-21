"""HTTP 接口 + 静态页面。

接口约定（对应任务书第三节）：
    POST /chat              {message, session_id?} → {trace_id, session_id, status}
    GET  /chat/{trace_id}   轮询运行状态 / 最终回答 / 产物
    GET  /files/{name}      播放生成的音频
    GET  /logs              查看 JSONL 调用链（录屏时对照用）
    GET  /health            自检：密钥是否就绪、协议是否注册

为什么用「提交 + 轮询」而不是一次请求等到结果：
一次播报生成要 40-90 秒，长时间挂起既容易超时、也没法展示阶段进展。
先拿到 trace_id，再按 trace_id 轮询，界面就能实时显示 supervisor 的调度过程。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from ..config import ROOT, AppConfig, load_config
from ..graph import Pipeline
from ..observability import Tracer, tail_log
from ..tools import available_protocols
from ..util import now_iso, sanitize

WEB_DIR = ROOT / "web"
MAX_HISTORY_TURNS = 6

md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": True})


# --------------------------------------------------------------------------- #
# 运行记录（单进程内存态，够课堂演示；生产应落库）
# --------------------------------------------------------------------------- #
@dataclass
class RunRecord:
    trace_id: str
    session_id: str
    query: str
    status: str = "running"  # running | done | error
    stage: str = "queued"
    stage_message: str = ""
    answer_md: str = ""
    answer_html: str = ""
    artifacts: list[dict] = field(default_factory=list)
    error: str = ""
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "query": self.query,
            "status": self.status,
            "stage": self.stage,
            "stage_message": self.stage_message,
            "answer_md": self.answer_md,
            "answer_html": self.answer_html,
            "artifacts": self.artifacts,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "events": self.events,
        }


RUNS: dict[str, RunRecord] = {}
SESSIONS: dict[str, list[dict]] = {}


# --------------------------------------------------------------------------- #
# 请求/响应模型
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, description="用户需求/主题")
    session_id: str | None = Field(default=None, max_length=64, description="多轮会话标识")


class ChatResponse(BaseModel):
    trace_id: str
    session_id: str
    status: str


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
app = FastAPI(title="多 Agent 语音播报系统", version="1.0.0")

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def _cfg() -> AppConfig:
    try:
        return load_config()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"配置加载失败：{sanitize(str(exc))}") from exc


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # noqa: ANN001
    """兜底：任何未捕获异常都返回可读 JSON，避免前端白屏。"""
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务端异常：{sanitize(f'{type(exc).__name__}: {exc}')}"},
    )


@app.get("/health")
async def health() -> dict:
    cfg = _cfg()
    return {
        "status": "ok",
        "app": cfg.app.name,
        "graph_mode": cfg.graph.mode,
        "llm": {"model": cfg.llm.model, "base_url": cfg.llm.base_url,
                "key_ready": bool(cfg.llm.api_key)},
        "tools": {
            name: {
                "protocol": t.protocol,
                "enabled": t.enabled,
                "key_ready": bool(t.api_key) if t.api_key_env else True,
            }
            for name, t in cfg.tools.items()
        },
        "protocols": available_protocols(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, background: BackgroundTasks) -> ChatResponse:
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")

    cfg = _cfg()
    session_id = body.session_id or uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex[:16]

    history = SESSIONS.get(session_id, [])[-MAX_HISTORY_TURNS:]
    SESSIONS.setdefault(session_id, []).append({"role": "user", "content": message})

    record = RunRecord(trace_id=trace_id, session_id=session_id, query=message)
    RUNS[trace_id] = record

    background.add_task(_execute, record, cfg, history)
    return ChatResponse(trace_id=trace_id, session_id=session_id, status="running")


@app.get("/chat/{trace_id}")
async def get_run(trace_id: str) -> dict:
    record = RUNS.get(trace_id)
    if record is None:
        raise HTTPException(status_code=404, detail="找不到该 trace_id，可能服务已重启")
    return record.to_dict()


@app.get("/logs")
async def logs(trace_id: str | None = None, limit: int = 40) -> dict:
    return {"trace_id": trace_id, "lines": tail_log(limit=min(limit, 200), trace_id=trace_id)}


@app.get("/files/{filename}")
async def files(filename: str) -> FileResponse:
    cfg = _cfg()
    out_dir = cfg.app.output_path().resolve()
    target = (out_dir / filename).resolve()
    # 防目录穿越
    if not str(target).startswith(str(out_dir)) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(target))


@app.get("/")
async def index() -> FileResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="web/index.html 缺失")
    return FileResponse(str(page))


# --------------------------------------------------------------------------- #
# 后台执行
# --------------------------------------------------------------------------- #
async def _execute(record: RunRecord, cfg: AppConfig, history: list[dict]) -> None:
    tracer = Tracer(record.trace_id, record.session_id)

    def progress(stage: str, message: str = "") -> None:
        record.stage = stage
        record.stage_message = message

    tracer.log("run", "start", query=record.query, mode=cfg.graph.mode, session_id=record.session_id)
    started = time.perf_counter()
    try:
        pipeline = Pipeline(cfg, tracer, progress)
        final = await asyncio.wait_for(
            pipeline.run(record.query, session_id=record.session_id, history=history),
            timeout=cfg.app.run_timeout_s,
        )
        record.answer_md = final.get("answer_md") or ""
        record.answer_html = md.render(record.answer_md)
        record.artifacts = final.get("artifacts") or []
        record.status = "done"
        record.stage = "done"
        SESSIONS.setdefault(record.session_id, []).append(
            {"role": "assistant", "content": record.answer_md[:500]}
        )
    except asyncio.TimeoutError:
        record.status = "error"
        record.error = f"任务超过 {cfg.app.run_timeout_s}s 未完成"
        tracer.log("run", "error", error=record.error)
    except Exception as exc:  # noqa: BLE001
        record.status = "error"
        # 错误摘要必须脱敏（Key 可能出现在上游报错里）
        record.error = sanitize(f"{type(exc).__name__}: {exc}")
        tracer.log("run", "error", error=record.error)
    finally:
        record.finished_at = now_iso()
        record.events = tracer.recent(300)
        if record.status == "done":
            tracer.log(
                "run", "end",
                duration_ms=int((time.perf_counter() - started) * 1000),
                artifacts=[a.get("kind") for a in record.artifacts],
                answer_chars=len(record.answer_md),
            )
