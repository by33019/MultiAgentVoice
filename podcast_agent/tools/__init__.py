"""工具层。

导入顺序有讲究：先 `registry`（拿到注册表和基类），再导入各协议实现
——各实现模块里的 `@register` 装饰器才会把协议名登记进去。
"""

from .base import RequestSpec, ToolClient, ToolError, ToolResult
from .registry import available_protocols, build_tool, build_tools, register

# 以下导入的副作用就是「注册协议」
from .bocha import BochaSearchClient  # noqa: E402,F401
from .readability import ReadabilityClient  # noqa: E402,F401
from .qwen_tts import QwenTTSClient, plain_text  # noqa: E402,F401

__all__ = [
    "RequestSpec",
    "ToolClient",
    "ToolError",
    "ToolResult",
    "register",
    "build_tool",
    "build_tools",
    "available_protocols",
    "BochaSearchClient",
    "ReadabilityClient",
    "QwenTTSClient",
    "plain_text",
]
