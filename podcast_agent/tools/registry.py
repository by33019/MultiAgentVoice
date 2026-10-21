"""协议注册表：协议名 → client 类。

新增协议只需两步：
1. 写一个 `ToolClient` 子类，打上 `@register`
2. 在 config.yaml 里把某个工具的 `protocol` 填成这个协议名

业务代码（graph/nodes）完全不感知具体协议，只按工具名调用。
"""

from __future__ import annotations

from typing import Iterable

from ..config import AppConfig
from ..observability import Tracer
from .base import RequestSpec, ToolClient, ToolError, ToolResult

# 协议名 → 实现类
_REGISTRY: dict[str, type[ToolClient]] = {}


def register(cls: type[ToolClient]) -> type[ToolClient]:
    if not cls.protocol:
        raise ValueError(f"{cls.__name__} 缺少 protocol 属性")
    _REGISTRY[cls.protocol] = cls
    return cls


def available_protocols() -> list[str]:
    return sorted(_REGISTRY)


def build_tool(
    name: str,
    app_cfg: AppConfig,
    tracer: Tracer | None = None,
) -> ToolClient:
    cfg = app_cfg.tools.get(name)
    if cfg is None:
        raise KeyError(f"config.yaml 里没有工具 {name}")
    cls = _REGISTRY.get(cfg.protocol)
    if cls is None:
        raise KeyError(
            f"工具 {name} 声明了未知协议 {cfg.protocol}；"
            f"已注册协议：{available_protocols()}"
        )
    return cls(name, cfg, app_cfg, tracer)


def build_tools(
    app_cfg: AppConfig,
    tracer: Tracer | None = None,
    names: Iterable[str] | None = None,
) -> dict[str, ToolClient]:
    targets = list(names) if names is not None else list(app_cfg.tools)
    tools: dict[str, ToolClient] = {}
    for name in targets:
        try:
            tools[name] = build_tool(name, app_cfg, tracer)
        except KeyError:
            continue  # 配置里没声明的工具直接跳过
    return tools


__all__ = [
    "RequestSpec",
    "ToolClient",
    "ToolError",
    "ToolResult",
    "register",
    "build_tool",
    "build_tools",
    "available_protocols",
]
