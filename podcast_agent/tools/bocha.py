"""博查联网搜索协议。

协议特征：POST + JSON，Authorization: Bearer <key>，结果为
`data.webPages.value[]`（含 name / url / snippet / summary）。
"""

from __future__ import annotations

from ..util import clip
from .base import RequestSpec, ToolError, ToolResult, ToolClient
from .registry import register


@register
class BochaSearchClient(ToolClient):
    protocol = "bocha"

    def _build_request(self, query: str = "", count: int | None = None, **_: object) -> RequestSpec:
        query = (query or "").strip()
        if not query:
            raise ToolError("query 不能为空")
        return RequestSpec(
            method="POST",
            url=self.cfg.base_url or "",
            headers={"Content-Type": "application/json"},
            json={
                "query": query,
                "count": int(count or self.cfg.opt("count", 8)),
                "summary": True,
            },
        )

    async def _parse_response(self, response, **_: object) -> ToolResult:
        payload = response.json()
        pages = (((payload.get("data") or {}).get("webPages") or {}).get("value")) or []
        if not pages:
            return ToolResult(ok=False, error="搜索无结果", text="[web_search] 没有搜到相关网页。")

        results: list[dict] = []
        for item in pages[:8]:
            results.append(
                {
                    "title": (item.get("name") or "").strip(),
                    "url": (item.get("url") or "").strip(),
                    "snippet": (item.get("summary") or item.get("snippet") or "").strip(),
                }
            )

        lines = []
        for i, r in enumerate(results, start=1):
            lines.append(f"[{i}] {r['title']}\n    {r['url']}\n    {clip(r['snippet'], 220)}")
        return ToolResult(
            ok=True,
            text=f"搜索「{_.get('query', '')}」得到 {len(results)} 条结果：\n" + "\n".join(lines),
            data={"results": results},
        )
