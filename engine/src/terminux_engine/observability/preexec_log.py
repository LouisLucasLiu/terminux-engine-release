# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""
observability/preexec_log.py

負責 observability log file 寫入。

路徑解析優先順序：明確絕對路徑、明確 project root、使用者資料目錄。
預設絕不從安裝後的 __file__ 倒推可寫的開發 checkout。
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

from terminux_engine.observability.log_schema import ObservabilityEvent


def _resolve_project_root() -> Path | None:
    env_root = os.environ.get("TERMINUX_PROJECT_ROOT", "").strip()
    if not env_root:
        return None
    root = Path(env_root).expanduser()
    if not root.is_absolute():
        raise ValueError("TERMINUX_PROJECT_ROOT must be an absolute path")
    return root


def get_observability_log_path() -> Path:
    """回傳 observability log 的絕對路徑。

    優先順序:
    1. TERMINUX_LOG_PATH 環境變數
    2. TERMINUX_PROJECT_ROOT / artifacts / lineage_preexec.log
    3. ~/.terminux/lineage_preexec.log（使用者可寫資料）
    """
    # 明確路徑不允許 cwd-relative，以免安裝版從不同工作目錄寫到不同位置。
    explicit = os.environ.get("TERMINUX_LOG_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise ValueError("TERMINUX_LOG_PATH must be an absolute path")
        return path

    root = _resolve_project_root()
    if root is not None:
        candidate = root / "artifacts" / "lineage_preexec.log"
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            if os.access(candidate.parent, os.W_OK):
                return candidate
        except OSError:
            pass

    return Path.home() / ".terminux" / "lineage_preexec.log"


def append_observability_event(event: ObservabilityEvent) -> None:
    """
    將一筆 ObservabilityEvent 以 JSONL 格式追加到 log file。
    寫入失敗不得讓 caller 崩潰；最多輸出 stderr warning。
    """
    try:
        log_path = get_observability_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = dataclasses.asdict(event)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(
            f"WARNING: Failed to write observability log: {exc}",
            file=sys.stderr,
        )
