"""一次完整的图执行。"""

from __future__ import annotations

from typing import Any, Callable

from ..config import AppConfig
from ..llm import LLM
from ..observability import Tracer
from ..tools import build_tools
from .build import build_graph
from .nodes import Nodes

Progress = Callable[[str, str], None]


class Pipeline:
    """装配 LLM / 工具 / 节点 / 图，并执行一次。"""

    def __init__(
        self,
        cfg: AppConfig,
        tracer: Tracer,
        progress: Progress | None = None,
    ) -> None:
        self.cfg = cfg
        self.tracer = tracer
        self.llm = LLM(cfg.llm, tracer)
        self.tools = build_tools(cfg, tracer)
        self.nodes = Nodes(cfg, self.llm, self.tools, tracer, progress)
        self.graph = build_graph(self.nodes)

    async def run(
        self,
        query: str,
        *,
        session_id: str,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        initial: dict[str, Any] = {
            "trace_id": self.tracer.trace_id,
            "session_id": session_id,
            "query": query,
            "history": history or [],
            "turns": 0,
            "audio_attempts": 0,
            "sources": [],
            "artifacts": [],
        }
        # recursion_limit 要覆盖 supervisor 的往返：max_turns=10 时约 2*10+2
        return await self.graph.ainvoke(initial, config={"recursion_limit": 60})
