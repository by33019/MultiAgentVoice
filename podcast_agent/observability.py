"""可观测性：trace_id + JSON Lines 日志。

任务书要求「用 trace_id 在 logs/agent.log 里把 run→node→llm→tool 调用链走一遍」。
所以这里做的不是"打个日志"，而是：

- 每条记录都带 `trace_id` / `session_id` / `span` / `event` / `duration_ms`
- `event` 统一为 start / end / error / tool_call / llm_call
- 写入 **JSON Lines**（一行一条 JSON），方便 grep、也方便直接贴进录屏
- 所有文本都过一遍脱敏，避免 Key 落盘

前端拿到的 trace_id 与这里写的完全一致，录屏时可以照着对照。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from .util import now_iso, sanitize

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "agent.log"


def _clean(node: Any) -> Any:
    """递归脱敏。"""
    if isinstance(node, str):
        return sanitize(node)
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


class Tracer:
    """一次运行的追踪器：内存里留一份给接口回查，同时落 JSONL。"""

    def __init__(self, trace_id: str, session_id: str, log_path: Path | None = None) -> None:
        self.trace_id = trace_id
        self.session_id = session_id
        self.events: list[dict] = []
        self._log_path = Path(log_path) if log_path else LOG_PATH
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def log(self, span: str, event: str, duration_ms: int | None = None, **detail: Any) -> None:
        record = {
            "ts": now_iso(),
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "span": span,
            "event": event,
            "duration_ms": duration_ms,
            "detail": _clean({k: v for k, v in detail.items() if v is not None}),
        }
        with self._lock:
            self.events.append(record)
            try:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass  # 日志写失败不能影响主流程

    # ------------------------------------------------------------------ #
    @contextmanager
    def span(self, name: str, **detail: Any):
        started = time.perf_counter()
        self.log(name, "start", **detail)
        failed = False
        try:
            yield self
        except Exception as exc:  # noqa: BLE001
            failed = True
            self.log(
                name, "error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if not failed:
                self.log(name, "end", duration_ms=int((time.perf_counter() - started) * 1000))

    @asynccontextmanager
    async def aspan(self, name: str, **detail: Any):
        started = time.perf_counter()
        self.log(name, "start", **detail)
        failed = False
        try:
            yield self
        except Exception as exc:  # noqa: BLE001
            failed = True
            self.log(
                name, "error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if not failed:
                self.log(name, "end", duration_ms=int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ #
    def recent(self, limit: int = 200) -> list[dict]:
        return self.events[-limit:]


def tail_log(limit: int = 40, trace_id: str | None = None) -> list[dict]:
    """从日志文件尾部读回若干条（给 /logs 接口用，录屏时可对照）。"""
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    rows: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if trace_id and row.get("trace_id") != trace_id:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return list(reversed(rows))
