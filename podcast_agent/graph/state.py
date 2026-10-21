"""LangGraph 状态定义。

supervisor 模式的状态里有两类东西：
- **业务产物**：sources / notes / script / audio / answer_md
- **调度信号**：next_worker / turns

把"调度信号"也放进状态，是 supervisor 模式的关键——
调度决策因此可观测、可回放，而不是藏在某个 if 里。
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # 基础
    trace_id: str
    session_id: str
    query: str
    history: list[dict[str, Any]]

    # 调度
    next_worker: str
    turns: int
    stage: str

    # researcher 产物
    search_queries: list[str]
    sources: list[dict[str, Any]]
    notes: str

    # writer 产物
    script: str

    # voice 产物
    audio: dict[str, Any] | None
    audio_attempts: int
    artifacts: list[dict[str, Any]]

    # 汇总
    answer_md: str
    error: str
