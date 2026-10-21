"""图里的各个节点（agent）。

分工：
- supervisor  只做调度，不产出内容
- researcher  检索 + 打开原文 + 整理要点
- writer      把要点改写成口播稿
- voice       调千问 TTS 生成音频
- finish      汇总成最终回复
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..config import AppConfig
from ..llm import LLM
from ..observability import Tracer
from ..tools.base import ToolClient
from .prompts import (
    FINAL_TEMPLATE,
    RESEARCHER_NOTES_SYSTEM,
    RESEARCHER_QUERIES_SYSTEM,
    WRITER_SYSTEM,
)
from .state import AgentState

Progress = Callable[[str, str], None]


def _extract_list(text: str) -> list[str]:
    """从模型输出里尽力抠出一个字符串数组。"""
    match = re.search(r"\[[\s\S]*?\]", text or "")
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                items = [str(x).strip() for x in data if str(x).strip()]
                if items:
                    return items
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[\n,，、;；]", text or "")
    return [p.strip(" -•*\"'") for p in parts if p.strip(" -•*\"'")][:5]


class Nodes:
    def __init__(
        self,
        cfg: AppConfig,
        llm: LLM,
        tools: dict[str, ToolClient],
        tracer: Tracer,
        progress: Progress | None = None,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.tools = tools
        self.tracer = tracer
        self._progress: Progress = progress or (lambda stage, message="": None)

    def _tool(self, name: str) -> ToolClient | None:
        return self.tools.get(name)

    def _agent(self, name: str) -> dict[str, Any]:
        return self.cfg.agent(name)

    # ------------------------------------------------------------------ #
    # 调度中心
    # ------------------------------------------------------------------ #
    async def supervisor(self, state: AgentState) -> dict:
        turns = int(state.get("turns") or 0) + 1
        max_turns = int(self._agent("supervisor").get("max_turns") or self.cfg.graph.max_turns)

        notes = (state.get("notes") or "").strip()
        script = (state.get("script") or "").strip()
        audio = state.get("audio")
        attempts = int(state.get("audio_attempts") or 0)

        if turns > max_turns:
            nxt, reason = "finish", f"达到最大轮数 {max_turns}"
        elif not notes:
            nxt, reason = "researcher", "还没有调研资料"
        elif not script:
            nxt, reason = "writer", "还没有口播稿"
        elif not audio and attempts < 2:
            nxt, reason = "voice", "还没有音频产物"
        elif audio:
            nxt, reason = "finish", "文字稿与音频都已就绪"
        else:
            nxt, reason = "finish", "配音连续失败，先交付文字稿"

        self.tracer.log(
            "supervisor", "decision",
            turn=turns, next=nxt, reason=reason,
            has_notes=bool(notes), has_script=bool(script), has_audio=bool(audio),
        )
        self._progress(nxt, reason)
        return {"turns": turns, "next_worker": nxt, "stage": nxt}

    # ------------------------------------------------------------------ #
    # 调研
    # ------------------------------------------------------------------ #
    async def researcher(self, state: AgentState) -> dict:
        query = state.get("query", "")
        conf = self._agent("researcher")
        max_searches = int(conf.get("max_searches") or 3)
        max_pages = int(conf.get("max_pages") or 2)

        async with self.tracer.aspan("node:researcher", query=query):
            raw = await self.llm.chat(
                RESEARCHER_QUERIES_SYSTEM,
                f"主题：{query}",
                span="llm:queries",
                temperature=0.3,
            )
            queries = _extract_list(raw)[:max_searches] or [query]
            self._progress("researcher", "检索计划：" + " / ".join(queries))

            # 搜索
            search = self._tool("web_search")
            collected: list[dict] = []
            if search is not None:
                for q in queries:
                    result = await search.call(query=q)
                    if result.ok:
                        collected.extend(result.data.get("results") or [])

            # 按 URL 去重，编号供撰稿时引用
            seen: set[str] = set()
            unique: list[dict] = []
            for item in collected:
                url = (item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                unique.append(item)

            sources = [
                {
                    "index": i,
                    "title": item.get("title") or item["url"],
                    "url": item["url"],
                    "snippet": (item.get("snippet") or "")[:200],
                }
                for i, item in enumerate(unique[:12], start=1)
            ]
            self._progress("researcher", f"已收集 {len(sources)} 条来源，正在打开原文")

            # 打开前几篇原文（摘要不够时补细节）
            fetch = self._tool("fetch_page")
            excerpts: list[str] = []
            if fetch is not None:
                for item in unique[:max_pages]:
                    page = await fetch.call(url=item["url"])
                    if page.ok:
                        title = page.data.get("title") or item.get("title") or ""
                        body = (page.data.get("text") or "")[:2500]
                        excerpts.append(f"《{title}》\n{body}")

            material = self._build_material(sources, excerpts)
            notes = await self.llm.chat(
                RESEARCHER_NOTES_SYSTEM,
                f"主题：{query}\n\n{material}",
                span="llm:notes",
                temperature=0.2,
            )
            self._progress("researcher", f"要点整理完成（{len(notes)} 字）")
            return {"search_queries": queries, "sources": sources, "notes": notes}

    @staticmethod
    def _build_material(sources: list[dict], excerpts: list[str]) -> str:
        parts = ["## 搜索结果"]
        for s in sources:
            parts.append(f"[{s['index']}] {s['title']}\n{s['url']}\n{s['snippet']}")
        if excerpts:
            parts.append("\n## 原文摘录")
            parts.extend(excerpts)
        material = "\n\n".join(parts)
        return material[:12000]

    # ------------------------------------------------------------------ #
    # 撰稿
    # ------------------------------------------------------------------ #
    async def writer(self, state: AgentState) -> dict:
        max_chars = int(self._agent("writer").get("max_chars") or 900)
        async with self.tracer.aspan("node:writer", notes_chars=len(state.get("notes") or "")):
            script = await self.llm.chat(
                WRITER_SYSTEM.replace("{max_chars}", str(max_chars)),
                f"主题：{state.get('query', '')}\n\n研究笔记：\n{state.get('notes', '')}",
                span="llm:script",
                temperature=0.5,
            )
            self._progress("writer", f"口播稿完成（{len(script)} 字）")
            return {"script": script}

    # ------------------------------------------------------------------ #
    # 配音
    # ------------------------------------------------------------------ #
    async def voice(self, state: AgentState) -> dict:
        attempts = int(state.get("audio_attempts") or 0) + 1
        async with self.tracer.aspan("node:voice", attempt=attempts):
            tts = self._tool("qwen_tts")
            if tts is None:
                self._progress("voice", "语音工具未启用")
                return {"audio_attempts": attempts, "error": "语音工具未启用"}

            script = state.get("script") or ""
            self._progress("voice", "正在合成语音…")
            result = await tts.call(text=script)
            if not result.ok or not result.artifacts:
                self._progress("voice", f"配音失败：{result.error or '无产物'}")
                return {"audio_attempts": attempts, "error": result.error or "配音失败"}

            artifact = result.artifacts[0].to_dict()
            meta = artifact.get("meta") or {}
            self._progress(
                "voice",
                f"音频已生成：{meta.get('filename')}（{meta.get('bytes', 0) / 1024:.0f} KB）",
            )
            return {"audio": artifact, "audio_attempts": attempts, "artifacts": [artifact]}

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    async def finish(self, state: AgentState) -> dict:
        async with self.tracer.aspan("node:finish"):
            sources = state.get("sources") or []
            source_lines = (
                "\n".join(f"[{s['index']}] {s['title']} —— {s['url']}" for s in sources)
                or "（本次未检索到来源）"
            )
            audio = state.get("audio") or {}
            meta = audio.get("meta") or {}
            audio_line = (
                f"已生成语音：`{meta.get('filename')}`"
                f"（{meta.get('bytes', 0) / 1024:.0f} KB，音色 {meta.get('voice')}，"
                f"模型 {meta.get('model')}）"
                if audio
                else "本次未生成音频。"
            )

            answer = FINAL_TEMPLATE.format(
                query=state.get("query", ""),
                notes=(state.get("notes") or "（无）").strip(),
                audio_line=audio_line,
                script=(state.get("script") or "（无）").strip(),
                sources=source_lines,
            )
            self._progress("finish", "已汇总最终回复")
            return {"answer_md": answer, "stage": "done"}
