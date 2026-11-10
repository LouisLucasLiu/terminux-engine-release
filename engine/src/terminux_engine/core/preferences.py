# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Preferred-language 偏好設定（單一真實來源，daemon 端持有）。

GUI 透過 `preferences.set_language` RPC 設定，本模組負責讀寫
`~/.terminux/preferences.json`。未設定 → 預設 zh-Hant（行為與舊版一致）。

優先序（load）：preferences.json > TERMINUX_PREFERRED_LANGUAGE env > 預設。
—— 檔案優先，讓 GUI 下拉的選擇一定生效；env 僅作 headless / 首次未選前的預設。

所有讀寫失敗都靜默降級為 DEFAULT_LANGUAGE，絕不拋出 exception（daemon 不可因
偏好設定崩潰）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

# 支援語言（= i18n 字串表已翻譯的集合）。
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh-Hant", "en", "zh-Hans")
DEFAULT_LANGUAGE = "zh-Hant"

_NATIVE_NAME = {
    "zh-Hant": "繁體中文",
    "en": "English",
    "zh-Hans": "简体中文",
}

_ENV_VAR = "TERMINUX_PREFERRED_LANGUAGE"


def _prefs_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".terminux" / "preferences.json"


def normalize_language(lang: str | None) -> str:
    """合法 → 原值；非法 / None → DEFAULT_LANGUAGE。"""
    if lang and lang in SUPPORTED_LANGUAGES:
        return lang
    return DEFAULT_LANGUAGE


def language_native_name(lang: str | None) -> str:
    """語言代碼 → 母語名（繁體中文 / English / 简体中文）。"""
    return _NATIVE_NAME[normalize_language(lang)]


def load_preferred_language(home: Path | None = None) -> str:
    """讀偏好語言。檔案優先 > env > 預設；任何失敗靜默降級為 DEFAULT_LANGUAGE。"""
    path = _prefs_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            val = data.get("preferred_language")
            if val:
                return normalize_language(val)
    except (OSError, ValueError, TypeError):
        pass

    env = os.environ.get(_ENV_VAR)
    if env:
        return normalize_language(env.strip())
    return DEFAULT_LANGUAGE


def set_preferred_language(lang: str, home: Path | None = None) -> str:
    """寫偏好語言到 preferences.json，回傳實際寫入（normalize 後）的語言。

    非法值 → 寫入 DEFAULT_LANGUAGE。保留檔內既有其他 key。寫檔失敗 → 記
    warning 但仍回傳 normalize 後的語言（不拋）。
    """
    normalized = normalize_language(lang)
    path = _prefs_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError, TypeError):
                existing = {}
        existing["preferred_language"] = normalized
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        _log.warning("set_preferred_language write failed: %s", exc)
    return normalized
