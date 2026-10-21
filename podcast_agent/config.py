"""配置加载：config.yaml + .env → 强类型配置对象。

两条铁律（对应任务书的「配置纪律」计分点）：
1. 密钥只从环境变量读，配置文件里只能写**变量名**。
2. 加载时做一次密钥扫描——有人把 Key 粘进 config.yaml 就直接报错，不给"以后再说"的机会。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
# 一旦在配置里看到这种形态，基本就是把 Key 贴上去了
_LOOKS_LIKE_SECRET = re.compile(r"^(sk-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9]{40,})$")


# --------------------------------------------------------------------------- #
# .env 读取（不引入 python-dotenv，规则足够简单）
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path | None = None) -> None:
    """把 .env 里的变量灌进 os.environ（已存在的环境变量优先，不覆盖）。"""
    env_file = path or (ROOT / ".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
class ToolConfig(BaseModel):
    """单个工具的装配信息。extra="allow" 让各协议可以带自己的专有参数。"""

    model_config = ConfigDict(extra="allow")

    protocol: str
    enabled: bool = True
    base_url: str | None = None
    api_key_env: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    timeout_s: float = 20.0

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env or "", "").strip()

    def opt(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)


class AppMeta(BaseModel):
    name: str = "voice-researcher"
    output_dir: str = "./data/outputs"
    max_audio_bytes: int = 8 * 1024 * 1024
    run_timeout_s: int = 240

    def output_path(self) -> Path:
        p = Path(self.output_dir)
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p


class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.4
    timeout_s: float = 120.0

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()


class GraphConfig(BaseModel):
    mode: str = "supervisor"
    max_turns: int = 10
    llm_routing: bool = False


class AppConfig(BaseModel):
    app: AppMeta = AppMeta()
    llm: LLMConfig = LLMConfig()
    tools: dict[str, ToolConfig] = {}
    agents: dict[str, dict[str, Any]] = {}
    graph: GraphConfig = GraphConfig()

    def tool(self, name: str) -> ToolConfig | None:
        cfg = self.tools.get(name)
        if cfg is None or not cfg.enabled:
            return None
        return cfg

    def agent(self, name: str) -> dict[str, Any]:
        return self.agents.get(name, {})


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _interpolate(node: Any) -> Any:
    """把 ${VAR} / ${VAR:-默认值} 替换成环境变量的值。

    支持默认值是为了"开箱即跑"：没配 LLM_BASE_URL 时也能用 DeepSeek 官方端点，
    但 .env 里一写就会覆盖——换服务商依然只改环境变量。
    """

    def _sub(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        value = os.environ.get(name)
        if value:
            return value
        return default if default is not None else ""

    if isinstance(node, str):
        return _ENV_PATTERN.sub(_sub, node)
    if isinstance(node, dict):
        return {k: _interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v) for v in node]
    return node


def _assert_no_secrets(node: Any, trail: str = "") -> None:
    """扫描配置树，发现疑似密钥明文就报错。"""
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_no_secrets(value, f"{trail}.{key}" if trail else str(key))
        return
    if isinstance(node, list):
        for i, value in enumerate(node):
            _assert_no_secrets(value, f"{trail}[{i}]")
        return
    if not isinstance(node, str):
        return

    if _LOOKS_LIKE_SECRET.match(node.strip()):
        raise ValueError(
            f"配置项 {trail or '(root)'} 看起来是密钥明文。"
            "请把它移到 .env，并在 config.yaml 中改用 *_api_key_env 引用变量名。"
        )
    # 与真实环境变量逐字比对（防止把 Key 换个字段名藏起来）
    for env_name in ("DEEPSEEK_API_KEY", "BOCHA_API_KEY", "DASHSCOPE_API_KEY"):
        secret = os.environ.get(env_name, "")
        if secret and len(secret) >= 8 and secret == node.strip():
            raise ValueError(
                f"配置项 {trail or '(root)'} 的值与环境变量 {env_name} 完全相同，"
                "相当于把密钥写进了配置文件，请改为 *_api_key_env 引用。"
            )


@lru_cache(maxsize=1)
def _load_cached(config_path: str) -> AppConfig:
    load_dotenv()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _assert_no_secrets(raw)
    data = _interpolate(raw)
    return AppConfig(**data)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    return _load_cached(str(path))


def reload_config(config_path: str | Path | None = None) -> AppConfig:
    _load_cached.cache_clear()
    return load_config(config_path)
