"""网页正文抽取协议（本地解析，不需要鉴权）。

用 trafilatura 从 HTML 里抽正文——比正则去标签可靠得多，
也是"搜索摘要不够、需要看原文"时的取证手段。
"""

from __future__ import annotations

import trafilatura

from ..util import clip
from .base import RequestSpec, ToolError, ToolResult, ToolClient
from .registry import register


@register
class ReadabilityClient(ToolClient):
    protocol = "readability"

    def _build_request(self, url: str = "", **_: object) -> RequestSpec:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ToolError(f"链接不合法：{url}")
        return RequestSpec(
            method="GET",
            url=url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    async def _parse_response(self, response, **_: object) -> ToolResult:
        max_bytes = int(self.cfg.opt("max_bytes", 2_000_000))
        content = response.content[:max_bytes]
        html = content.decode(response.encoding or "utf-8", errors="ignore")

        meta = trafilatura.extract(
            html,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=True,
        )
        text, title = "", ""
        if meta:
            import json

            try:
                data = json.loads(meta) if isinstance(meta, str) else meta
                text = (data.get("text") or "").strip()
                title = (data.get("title") or "").strip()
            except Exception:  # noqa: BLE001
                text = str(meta).strip()

        if len(text) < 120:
            raise ToolError(f"正文过短（{len(text)} 字），可能是动态渲染或被反爬")

        return ToolResult(
            ok=True,
            text=f"《{title or '未命名页面'}》\n来源：{response.url}\n\n{clip(text, 4000)}",
            data={"title": title, "url": str(response.url), "text": text},
        )
