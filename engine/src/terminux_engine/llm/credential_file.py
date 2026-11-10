# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""provider 憑證的 daemon 專屬設定檔來源（0600，不進 Git）。

為什麼要改
----------

憑證原本只從環境變數讀。環境變數會出現在任何列出環境的地方——`env`、
`/proc/<pid>/environ`、被繼承的 child、以及任何不小心印出環境的診斷輸出
（曾有診斷輸出因此把 key 的明文印出來）。

這裡加上一個**優先於環境變數**的來源：一個只有本使用者能讀寫的檔案。

邊界
----

* 檔案必須是普通檔、非 symlink、屬於本 uid、權限**恰好** 0600。任何一項
  不成立就當作沒有這個檔案並記一筆 warning——**不**降級去讀比較寬鬆的檔案。
* 環境變數仍然可用（既有安裝不會突然壞掉），但檔案優先。值來自環境時會記
  一筆 warning，提醒把它移進檔案。
* 這個模組**永遠不記錄、不回傳、不比較**任何憑證的值、前綴或長度。可外露的
  只有「哪些名字有值」。
* 它不宣稱能抵抗同一個 Linux 帳號的持有者。它擋的是「憑證出現在環境與輸出裡」。
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

CREDENTIAL_FILE_ENV = "TERMINUX_CREDENTIALS_FILE"
DEFAULT_CREDENTIAL_FILE = "~/.terminux/credentials.json"

#: 只有這些名字會從檔案裡被採用。未知的 key 一律忽略（不讓設定檔變成
#: 任意環境注入的入口）。
SUPPORTED_NAMES: frozenset[str] = frozenset({
    "TERMINUX_GEMINI_API_KEY",
    "TERMINUX_OPENAI_API_KEY",
    "TERMINUX_OPENAI_BASE_URL",
})

REQUIRED_MODE = 0o600

#: 明確關閉憑證來源的開關。設成 "1" 時 `load()`／`secret()` 一律回空——
#: **只會收窄，不會放寬**（它不能讓任何人取得憑證，只能讓程序完全拿不到）。
#: 隔離 harness 預設打開它，這樣測試與展示 child 從機制上就不可能帶著憑證
#: 去呼叫真實 provider。
DISABLE_ENV = "TERMINUX_DISABLE_CREDENTIAL_FILE"

_cache: dict[str, str] | None = None
_cache_key: tuple[str, int, int] | None = None


def configured_path() -> Path:
    raw = os.environ.get(CREDENTIAL_FILE_ENV, "").strip() or DEFAULT_CREDENTIAL_FILE
    return Path(raw).expanduser()


def _secure(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _log.warning("TERM_CREDENTIAL_FILE_REJECTED reason=not_a_regular_file")
        return False
    if info.st_uid != os.getuid():
        _log.warning("TERM_CREDENTIAL_FILE_REJECTED reason=wrong_owner")
        return False
    if stat.S_IMODE(info.st_mode) != REQUIRED_MODE:
        _log.warning("TERM_CREDENTIAL_FILE_REJECTED reason=mode_must_be_0600")
        return False
    return True


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() == "1"


def load() -> dict[str, str]:
    """讀出檔案裡的憑證。檔案缺席或不合格一律回空 dict，不拋。

    回傳值含明文，只給呼叫端立即使用；**不要**把它印出來或寫進任何紀錄。
    """
    global _cache, _cache_key  # noqa: PLW0603
    if disabled():
        return {}
    path = configured_path()
    try:
        info = path.stat()
    except OSError:
        _cache, _cache_key = {}, None
        return {}
    key = (str(path), info.st_mtime_ns, info.st_size)
    if _cache is not None and _cache_key == key:
        return dict(_cache)
    if not _secure(path):
        _cache, _cache_key = {}, key
        return {}
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.warning("TERM_CREDENTIAL_FILE_REJECTED reason=unreadable_or_invalid_json")
        _cache, _cache_key = {}, key
        return {}
    if not isinstance(document, dict):
        _log.warning("TERM_CREDENTIAL_FILE_REJECTED reason=not_an_object")
        _cache, _cache_key = {}, key
        return {}
    values = {
        name: value for name, value in document.items()
        if name in SUPPORTED_NAMES and isinstance(value, str) and value.strip()
    }
    _cache, _cache_key = dict(values), key
    _log.info("TERM_CREDENTIAL_FILE_LOADED names=%s", sorted(values))
    return dict(values)


def secret(name: str) -> str | None:
    """取一個憑證：**檔案優先**，環境變數為相容性回退。

    值來自環境時記一筆 warning——那正是我們要遷移離開的位置。
    """
    if disabled():
        return None
    from_file = load().get(name)
    if from_file:
        return from_file
    from_env = os.environ.get(name)
    if from_env and from_env.strip():
        _log.warning(
            "TERM_CREDENTIAL_FROM_ENVIRONMENT name=%s; move it into %s "
            "(mode 0600) so it does not appear in the process environment",
            name, configured_path())
        return from_env
    return None


def presence_report() -> dict[str, str]:
    """給 preflight／診斷用：只回「有沒有」與「來自哪裡」，**永遠不回值**。"""
    if disabled():
        return {name: "disabled_by_environment" for name in sorted(SUPPORTED_NAMES)}
    from_file = load()
    report: dict[str, str] = {}
    for name in sorted(SUPPORTED_NAMES):
        if from_file.get(name):
            report[name] = "present_in_credential_file"
        elif (os.environ.get(name) or "").strip():
            report[name] = "present_in_environment"
        else:
            report[name] = "not_present"
    return report


def reset_cache() -> None:
    global _cache, _cache_key  # noqa: PLW0603
    _cache, _cache_key = None, None


__all__ = [
    "CREDENTIAL_FILE_ENV",
    "DISABLE_ENV",
    "DEFAULT_CREDENTIAL_FILE",
    "REQUIRED_MODE",
    "SUPPORTED_NAMES",
    "configured_path",
    "disabled",
    "load",
    "presence_report",
    "reset_cache",
    "secret",
]
