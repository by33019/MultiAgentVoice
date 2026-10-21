"""★ 本组新增工具协议：千问 TTS（qwen3-tts-flash）。

为什么需要"新增协议"而不是复用现成的：
千问 TTS **不是** OpenAI 兼容的 `/v1/audio/speech`：

    请求  POST /api/v1/services/aigc/multimodal-generation/generation
          {model, input: {text, voice, language_type}}
    响应  JSON，音频在 output.audio.url —— 一个 24 小时有效的 OSS 链接，
          **不是**二进制音频流

所以它和 OpenAI 的 TTS 协议在「怎么要音频」和「怎么拿音频」两处都不一样，
必须新增一个 client。

实现上严格照模板方法：只写 `_build_request` 与 `_parse_response`，
鉴权 / 超时 / 状态码 / 脱敏全部交给基类。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from ..artifacts import Artifact
from ..util import clip
from .base import RequestSpec, ToolClient, ToolError, ToolResult
from .registry import register

# 音频地址里有签名，日志里绝不能整条打出来
_SAFE_URL_HINT = "oss://…(已隐藏签名)"


def plain_text(markdown: str) -> str:
    """把口播稿的 Markdown 洗成适合朗读的纯文本。

    TTS 会把 `##`、`**`、`[1]` 这类标记念出来或者读得磕磕巴巴，
    所以合成前必须先清一遍。
    """
    text = markdown or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # 链接保留文字
    text = re.sub(r"\[\d+\]", "", text)                       # 引用编号
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)  # 标题井号
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)       # 列表符号
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)           # 引用
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)         # 行内代码
    text = text.replace("**", "").replace("__", "").replace("*", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@register
class QwenTTSClient(ToolClient):
    protocol = "qwen_tts"

    # ------------------------------------------------------------------ #
    # 翻译函数 1：业务参数 → HTTP 请求
    # ------------------------------------------------------------------ #
    def _build_request(
        self,
        text: str = "",
        voice: str | None = None,
        language_type: str | None = None,
        **_: object,
    ) -> RequestSpec:
        spoken = plain_text(text)
        if not spoken.strip():
            raise ToolError("待合成文本为空（口播稿清洗后没有可朗读内容）")

        max_chars = int(self.cfg.opt("max_text_chars", 1200))
        if len(spoken) > max_chars:
            # 截断到句号处，避免把一句话切一半
            spoken = spoken[:max_chars]
            cut = max(spoken.rfind("。"), spoken.rfind("！"), spoken.rfind("？"))
            if cut > max_chars * 0.6:
                spoken = spoken[: cut + 1]

        return RequestSpec(
            method="POST",
            url=self.cfg.base_url or "",
            headers={"Content-Type": "application/json"},
            json={
                "model": self.cfg.opt("model", "qwen3-tts-flash"),
                "input": {
                    "text": spoken,
                    "voice": voice or self.cfg.opt("voice", "Cherry"),
                    "language_type": language_type or self.cfg.opt("language_type", "Chinese"),
                },
            },
        )

    # ------------------------------------------------------------------ #
    # 翻译函数 2：HTTP 响应 → ToolResult(+Artifact)
    # ------------------------------------------------------------------ #
    async def _parse_response(
        self,
        response,
        text: str = "",
        voice: str | None = None,
        **_: object,
    ) -> ToolResult:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"响应不是合法 JSON：{exc}") from exc

        audio = ((payload.get("output") or {}).get("audio")) or {}
        audio_url = (audio.get("url") or "").strip()
        inline_data = audio.get("data") or ""

        # ① 拿二进制：优先 OSS 链接，其次响应内联 base64
        if audio_url:
            raw = await self._fetch_bytes(audio_url, self.app.app.max_audio_bytes)
            suffix = Path(urlparse(audio_url).path).suffix or ".wav"
            source = "oss_url"
        elif inline_data:
            try:
                raw = base64.b64decode(inline_data)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"内联音频 base64 解码失败：{exc}") from exc
            if len(raw) > self.app.app.max_audio_bytes:
                raise ToolError(
                    f"内联音频超过上限 {self.app.app.max_audio_bytes} 字节"
                )
            suffix = ".wav"
            source = "inline_base64"
        else:
            raise ToolError(
                "响应里既没有 output.audio.url 也没有 output.audio.data："
                f"{clip(str(payload), 300)}"
            )

        if not raw:
            raise ToolError("拿到的音频内容为空")

        # ② 落盘
        filename = f"tts-{uuid4().hex[:12]}{suffix}"
        dest = self.app.app.output_path() / filename
        dest.write_bytes(raw)

        chosen_voice = voice or self.cfg.opt("voice", "Cherry")
        artifact = Artifact(
            kind="audio",
            url=f"/files/{filename}",
            path=str(dest),
            meta={
                "filename": filename,
                "bytes": len(raw),
                "voice": chosen_voice,
                "model": self.cfg.opt("model", "qwen3-tts-flash"),
                # 原始链接 24 小时过期，只保留来源类型与过期时间，不保留带签名的 URL
                "source": source,
                "expires_at": audio.get("expires_at"),
                "text_chars": len(plain_text(text)),
            },
        )

        return ToolResult(
            ok=True,
            text=(
                f"语音已生成：{filename}（{len(raw) / 1024:.0f} KB，"
                f"音色 {chosen_voice}，来源 {_SAFE_URL_HINT}）"
            ),
            data={"filename": filename, "bytes": len(raw)},
            artifacts=[artifact],
        )
