# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""LLM Router 設定模型與載入邏輯。

支援兩種載入方式：
1. YAML 檔案（~/.terminux/llm_router.yaml）— 上線用
2. 環境變數 fallback（TERMINUX_LLM_BACKEND 等）— 開發用
"""

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

_log = logging.getLogger(__name__)

# YAML 預設路徑
_DEFAULT_YAML_PATH = Path.home() / ".terminux" / "llm_router.yaml"

# 環境變數展開 pattern：${VAR_NAME}
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ProviderConfig(BaseModel):
    """LLM completion provider 設定。"""
    backend: str                    # "llama_cpp" | "gemini" | "openai_compatible" | "none"
    model_path: str | None = None   # llama_cpp 用
    model_name: str | None = None   # gemini / openai_compatible 用
    api_key: str | None = None      # gemini / openai_compatible 用
    base_url: str | None = None     # openai_compatible 用（DeepSeek/OpenAI/Groq/vLLM…）


class EmbeddingProviderConfig(BaseModel):
    """Embedding provider 設定。"""
    backend: str                    # "gemini" | "local" | "noop"
    model_name: str | None = None   # gemini: "gemini-embedding-001"
    model_path: str | None = None   # local: GGUF path
    api_key: str | None = None      # gemini 用


class RouterConfig(BaseModel):
    """四個 task type slot 的完整設定。"""
    realtime: ProviderConfig
    background: ProviderConfig
    analysis: ProviderConfig
    embedding: EmbeddingProviderConfig


def _expand_env_vars(value: str) -> str:
    """展開字串中的 ${VAR_NAME} 為環境變數值。未設定的變數保留原樣。"""
    def _replace(match: re.Match) -> str:
        var = match.group(1)
        return os.environ.get(var, match.group(0))
    return _ENV_VAR_RE.sub(_replace, value)


def _expand_dict(data: dict) -> dict:
    """遞迴展開 dict 中所有字串值的環境變數。"""
    result = {}
    for k, v in data.items():
        if isinstance(v, dict):
            result[k] = _expand_dict(v)
        elif isinstance(v, str):
            result[k] = _expand_env_vars(v)
        else:
            result[k] = v
    return result


def _config_from_yaml(yaml_path: Path) -> RouterConfig:
    """從 YAML 檔案載入 RouterConfig。"""
    with open(yaml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid YAML format in {yaml_path}: expected mapping")
    expanded = _expand_dict(raw)
    return RouterConfig(**expanded)


#: Production 預設 Gemini model。
#:
#: 舊值 `gemini-2.5-pro` 已被 provider 下架，實際呼叫會回
#: `404 NOT_FOUND ... no longer available to new users`，因此任何依賴預設值
#: 的 production 路徑都會失敗。`gemini-3.1-pro-preview` 則回 `429
#: RESOURCE_EXHAUSTED`（free-tier quota 0）。實測唯一可成功呼叫的是
#: `gemini-3.6-flash`，故採用之。
#:
#: 這只是**預設值**：`TERMINUX_GEMINI_MODEL`、YAML config 或任何明確指定
#: 的 model 一律優先，且 provider 失敗時**不會**自動改試別的 model
#: （沒有 silent fallback loop）。
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


def _config_from_env() -> RouterConfig:
    """從既有環境變數自動構建 RouterConfig。

    **憑證**改由 `llm.credential_file.secret()` 取得——daemon 專屬、
    權限 0600、不進 Git 的設定檔優先，環境變數只作相容性回退。非憑證的
    設定（backend、model、embedding 選項）仍然來自環境變數。
    """
    from terminux_engine.llm.credential_file import secret as _secret

    backend = os.environ.get("TERMINUX_LLM_BACKEND", "")
    embedding_backend = os.environ.get("TERMINUX_EMBEDDING_BACKEND", "").strip().lower()

    if backend == "gemini":
        provider = ProviderConfig(
            backend="gemini",
            model_name=os.environ.get("TERMINUX_GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            api_key=_secret("TERMINUX_GEMINI_API_KEY"),
        )
        default_embedding = EmbeddingProviderConfig(
            backend="gemini",
            model_name="gemini-embedding-001",
            api_key=_secret("TERMINUX_GEMINI_API_KEY"),
        )
    elif backend == "llama_cpp":
        provider = ProviderConfig(
            backend="llama_cpp",
            model_path=os.environ.get("TERMINUX_LLM_MODEL_PATH"),
        )
        default_embedding = EmbeddingProviderConfig(backend="noop")
    elif backend == "openai_compatible":
        # 通用 OpenAI-compatible：DeepSeek / OpenAI / Groq / vLLM …
        provider = ProviderConfig(
            backend="openai_compatible",
            model_name=os.environ.get("TERMINUX_OPENAI_MODEL", "deepseek-chat"),
            api_key=_secret("TERMINUX_OPENAI_API_KEY"),
            base_url=(_secret("TERMINUX_OPENAI_BASE_URL")
                      or "https://api.deepseek.com"),
        )
        # Embedding 仍走 Gemini（若有設 GEMINI_API_KEY）；否則 noop
        gemini_key = _secret("TERMINUX_GEMINI_API_KEY")
        if gemini_key:
            default_embedding = EmbeddingProviderConfig(
                backend="gemini",
                model_name="gemini-embedding-001",
                api_key=gemini_key,
            )
        else:
            default_embedding = EmbeddingProviderConfig(backend="noop")
    else:
        # 無 LLM 配置 → 全部 none/noop
        provider = ProviderConfig(backend="none")
        default_embedding = EmbeddingProviderConfig(backend="noop")

    if embedding_backend == "local":
        embedding = EmbeddingProviderConfig(
            backend="local",
            model_path=os.environ.get("TERMINUX_EMBEDDING_MODEL_PATH"),
        )
    elif embedding_backend == "gemini":
        embedding = EmbeddingProviderConfig(
            backend="gemini",
            model_name=os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
            api_key=(
                os.environ.get("GEMINI_API_KEY")
                or _secret("TERMINUX_GEMINI_API_KEY")
            ),
        )
    elif embedding_backend in {"none", "noop"}:
        # smoke／離線 daemon 必須能明確覆寫 credential file 中存在的 Gemini
        # key；否則 EventStore 的背景 indexing 仍可能發出 embedding 請求。
        embedding = EmbeddingProviderConfig(backend="noop")
    else:
        embedding = default_embedding

    return RouterConfig(
        realtime=provider,
        background=provider,
        analysis=provider,
        embedding=embedding,
    )


def load_router_config(yaml_path: str | Path | None = None) -> RouterConfig:
    """載入 LLM Router 設定。

    優先級：
    1. 指定 yaml_path → 讀取該檔案
    2. ~/.terminux/llm_router.yaml 存在 → 讀取
    3. 環境變數 fallback
    """
    # 優先級 1：明確指定路徑
    if yaml_path is not None:
        p = Path(yaml_path)
        if p.exists():
            _log.info("Loading router config from %s", p)
            return _config_from_yaml(p)
        raise FileNotFoundError(f"Router config not found: {p}")

    # 優先級 2：預設 YAML 路徑
    if _DEFAULT_YAML_PATH.exists():
        _log.info("Loading router config from %s", _DEFAULT_YAML_PATH)
        return _config_from_yaml(_DEFAULT_YAML_PATH)

    # 優先級 3：環境變數 fallback
    _log.info("No YAML config found, falling back to environment variables")
    return _config_from_env()
