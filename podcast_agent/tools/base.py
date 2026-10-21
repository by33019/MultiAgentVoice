"""工具协议基类 —— 模板方法模式。

设计意图（对应任务书第四节的考查点）：

    新增一种协议的成本，应该只是「两个翻译函数」，而不是再写一遍鉴权、超时、重试、脱敏。

    call()  ← 模板方法，固定流程，子类不要覆盖
      ├─ _build_request()    子类实现：业务参数 → 一次 HTTP 请求
      ├─ _send()             基类实现：注入鉴权 / 超时 / 状态码 / 脱敏
      └─ _parse_response()   子类实现：HTTP 响应 → ToolResult（可能带 Artifact）

把「协议差异」收敛到两个纯函数里，是这套工具层最核心的取舍。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..artifacts import Artifact
from ..config import AppConfig, ToolConfig
from ..observability import Tracer
from ..util import clip, one_line, sanitize


class ToolError(RuntimeError):
    """工具层**可预期**的失败：参数不合法、上游返回错误、超出配额等。

    与"代码 bug"区分开：这类错误会以可读文案回给模型/用户，不打印堆栈。
    """


@dataclass
class RequestSpec:
    """一次 HTTP 请求的描述。由子类构造，基类负责发送。"""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


@dataclass
class ToolResult:
    ok: bool
    text: str = ""  # 给模型/用户看的文本结果
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0


class ToolClient(ABC):
    """所有工具协议的公共父类。"""

    protocol: str = ""

    def __init__(
        self,
        name: str,
        cfg: ToolConfig,
        app_cfg: AppConfig,
        tracer: Tracer | None = None,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self.app = app_cfg
        self.tracer = tracer

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # ------------------------------------------------------------------ #
    # 模板方法：固定流程
    # ------------------------------------------------------------------ #
    async def call(self, **kwargs: Any) -> ToolResult:
        if not self.enabled:
            return ToolResult(ok=False, error=f"工具 {self.name} 未启用")

        started = time.perf_counter()
        span = f"tool:{self.name}"
        if self.tracer:
            self.tracer.log(span, "start", args=self._args_summary(kwargs))

        try:
            spec = self._build_request(**kwargs)
            async with httpx.AsyncClient(
                timeout=self.cfg.timeout_s, follow_redirects=True
            ) as client:
                response = await self._send(client, spec)
                result = await self._parse_response(response, **kwargs)
        except ToolError as exc:
            # 可预期失败：脱敏后回给调用方，不打堆栈
            message = sanitize(str(exc))
            result = ToolResult(ok=False, error=message, text=f"[{self.name}] 调用失败：{message}")
        except httpx.TimeoutException:
            result = ToolResult(
                ok=False,
                error=f"上游超时（>{self.cfg.timeout_s}s）",
                text=f"[{self.name}] 上游超时，请稍后重试或换一种方式取证。",
            )
        except Exception as exc:  # noqa: BLE001
            message = sanitize(f"{type(exc).__name__}: {exc}")
            result = ToolResult(ok=False, error=message, text=f"[{self.name}] 异常：{message}")

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        if self.tracer:
            self.tracer.log(
                span,
                "end" if result.ok else "error",
                duration_ms=result.latency_ms,
                ok=result.ok,
                artifacts=[a.kind for a in result.artifacts],
                summary=one_line(result.text, 160),
                error=result.error or None,
            )
        return result

    # ------------------------------------------------------------------ #
    # 子类必须实现的两个「翻译函数」
    # ------------------------------------------------------------------ #
    @abstractmethod
    def _build_request(self, **kwargs: Any) -> RequestSpec:
        """把业务参数翻译成一次 HTTP 请求。纯函数，不发网络。"""

    @abstractmethod
    async def _parse_response(self, response: httpx.Response, **kwargs: Any) -> ToolResult:
        """把 HTTP 响应翻译成 ToolResult。

        注意：允许有 IO（例如产物类工具需要把上游的临时链接下载落盘），
        但**不允许**再发鉴权请求 —— 鉴权、超时、状态码都由基类 `_send()` 负责。
        """

    # ------------------------------------------------------------------ #
    # 基类提供的能力
    # ------------------------------------------------------------------ #
    async def _send(self, client: httpx.AsyncClient, spec: RequestSpec) -> httpx.Response:
        """统一发送：注入鉴权、检查状态码、错误摘要脱敏。"""
        headers = dict(spec.headers)
        if self.cfg.api_key_env:
            key = self.cfg.api_key
            if not key:
                raise ToolError(f"缺少密钥：环境变量 {self.cfg.api_key_env} 未设置（请在 .env 中配置）")
            headers[self.cfg.auth_header] = f"{self.cfg.auth_scheme} {key}".strip()

        response = await client.request(
            spec.method, spec.url, headers=headers, json=spec.json, params=spec.params
        )
        if response.status_code >= 400:
            raise ToolError(
                f"上游 HTTP {response.status_code}：{clip(sanitize(response.text), 300)}"
            )
        return response

    async def _fetch_bytes(self, url: str, max_bytes: int) -> bytes:
        """流式下载二进制（用于音频/图片），带硬上限。

        为什么单独一个方法：产物链接通常是**带签名的临时地址**，
        既不需要也不应该再带 Authorization 头，所以走独立通道。
        """
        total = 0
        chunks: list[bytes] = []
        async with httpx.AsyncClient(timeout=self.cfg.timeout_s, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise ToolError(f"产物下载失败：HTTP {response.status_code}")
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ToolError(
                            f"产物超过上限 {max_bytes} 字节（已下载 {total} 字节），已中止"
                        )
                    chunks.append(chunk)
        if not chunks:
            raise ToolError("产物下载为空")
        return b"".join(chunks)

    def _args_summary(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """日志里只留关键参数，长文本截断（避免日志被正文淹没）。"""
        summary: dict[str, Any] = {}
        for key, value in kwargs.items():
            if isinstance(value, str):
                summary[key] = one_line(value, 100)
            elif isinstance(value, (int, float, bool)) or value is None:
                summary[key] = value
            else:
                summary[key] = one_line(str(value), 60)
        return summary
