# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""安裝後的唯讀資源與使用者設定路徑。

套件資源不依賴工作目錄或開發 checkout；可寫設定與外掛則留在使用者
`~/.terminux`，不會寫回 wheel 內的範本。
"""

from __future__ import annotations

import os
from pathlib import Path


_PACKAGE_ROOT = Path(__file__).resolve().parent
_RESOURCE_ROOT = _PACKAGE_ROOT / "resources"


def _absolute_env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def bundled_knowledge_path(filename: str) -> Path:
    """取得 wheel 內的唯讀 knowledge 檔案。"""
    if not filename or Path(filename).name != filename:
        raise ValueError("knowledge filename must be a basename")
    return _RESOURCE_ROOT / "knowledge" / filename


def llm_config_path() -> Path:
    """依明確覆寫、使用者設定、發行範本的順序選擇舊 Layer 2 設定。"""
    explicit = _absolute_env_path("TERMINUX_LLM_CONFIG_PATH")
    if explicit is not None:
        return explicit
    user_config = Path.home() / ".terminux" / "config" / "llm_config.yaml"
    if user_config.is_file():
        return user_config
    return _RESOURCE_ROOT / "config" / "llm_config.yaml"


def plugin_dir() -> Path:
    """使用者可安裝 CAD／通知外掛的位置；不修改套件內建資源。"""
    return _absolute_env_path("TERMINUX_PLUGIN_DIR") or Path.home() / ".terminux" / "plugins"
