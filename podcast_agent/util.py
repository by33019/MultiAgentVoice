"""通用小工具：脱敏、时间、截断。"""

from __future__ import annotations

import os
import re
from datetime import datetime

# 常见的密钥形态
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}"),
]

# 需要参与脱敏的环境变量名
_SECRET_ENVS = (
    "DEEPSEEK_API_KEY",
    "BOCHA_API_KEY",
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
)


def sanitize(text: str) -> str:
    """把密钥从文本里抹掉。

    为什么必须有这一层：错误信息里最容易带出鉴权头/URL 签名，
    而这些内容一旦进日志、进前端报错框，就等于把 Key 公开了。
    任务书里「错误摘要不落密钥」说的就是这件事。
    """
    if not text:
        return ""
    out = str(text)
    for env_name in _SECRET_ENVS:
        value = os.environ.get(env_name, "")
        if value and len(value) >= 8:
            out = out.replace(value, "***")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("***", out)
    # OSS 预签名 URL 里的签名参数
    out = re.sub(r"(Signature=)[^&\"'\s]+", r"\1***", out)
    out = re.sub(r"(OSSAccessKeyId=)[^&\"'\s]+", r"\1***", out)
    return out


def now_iso() -> str:
    """带时区的本地时间（毫秒精度），日志里用它对齐录屏画面。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断，原长 {len(text)} 字）"


def one_line(text: str, limit: int = 120) -> str:
    """压成一行，用于日志摘要。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")
