"""产物（Artifact）：agent 生成的非纯文本结果。

任务书要求「最终产物中必须包含至少一种非纯文本内容（音频或图片，由真实服务生成）」。
这里把产物抽象成统一结构，前端按 `kind` 决定用 <audio> 还是 <img> 渲染，
而不是让模型在正文里拼一段路径让前端去猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Artifact:
    kind: str  # audio | image
    url: str  # 前端可直接访问的地址，如 /files/xxx.wav
    path: str  # 本地落盘绝对路径
    meta: dict = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return Path(self.path).name

    def to_dict(self) -> dict:
        # 注意：不外传本地绝对路径，也不外传带签名的源 URL
        return {"kind": self.kind, "url": self.url, "meta": self.meta}
