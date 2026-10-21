"""启动入口：python run.py

端口默认 8010（避开常见的 8000），可用 PORT 环境变量覆盖。
"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8010"))
    uvicorn.run("podcast_agent.api.app:app", host="127.0.0.1", port=port, reload=False)
