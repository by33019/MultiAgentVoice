"""LLM 调用封装（OpenAI 兼容）。

只做三件事：调一次 chat、把用量与耗时记进 trace、失败转成可读错误。
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from .config import LLMConfig
from .observability import Tracer
from .util import one_line, sanitize


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: LLMConfig, tracer: Tracer | None = None) -> None:
        self.cfg = cfg
        self.tracer = tracer
        if not cfg.api_key:
            raise LLMError(f"缺少大模型密钥：环境变量 {cfg.api_key_env} 未设置")
        self._client = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
        )

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        span: str = "llm",
        max_tokens: int | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if self.tracer:
            self.tracer.log(
                span, "llm_call",
                model=self.cfg.model,
                system_preview=one_line(system, 80),
                user_chars=len(user),
            )
        try:
            response = await self._client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                temperature=self.cfg.temperature if temperature is None else temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(sanitize(f"{type(exc).__name__}: {exc}")) from exc

        content = (response.choices[0].message.content or "").strip()
        usage = getattr(response, "usage", None)
        if self.tracer:
            self.tracer.log(
                span, "llm_result",
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                out_chars=len(content),
                preview=one_line(content, 120),
            )
        if not content:
            raise LLMError("模型返回了空内容")
        return content
