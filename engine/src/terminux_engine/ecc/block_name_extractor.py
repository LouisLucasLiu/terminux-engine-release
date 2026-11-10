# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""block_name 三層提取鏈 — Layer 1（deterministic runfile parser）+ Layer 2（lineage ledger 回溯）。

Layer 3（AI fallback）在 ai_companion.infer_block_name() 實作。
Layer 4（沿用上次值）在 jsonrpc_server 層處理。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Layer 1：deterministic runfile 掃描 ─────────────────────────────────────

# 支援的 Tcl set variable 名稱（匹配時不分大小寫）
_BLOCK_NAME_VARS = frozenset({
    "DESIGN",
    "TOP_MODULE",
    "BLOCK_NAME",
    "TOP_DESIGN",
    "DESIGN_NAME",
})

# 匹配 Tcl set 語句：set <VAR> <VALUE>
# VALUE 可能被雙引號 " 或大括號 { } 包圍，或裸字
_SET_RE = re.compile(
    r'^\s*set\s+(\w+)\s+'          # set <VAR>
    r'(?:"([^"]+)"|{([^}]+)}|(\S+))',  # "<val>" | {<val>} | <val>
)

_MAX_SCAN_LINES = 50


def extract_block_name_from_runfile(runfile_path: str | None) -> str | None:
    """Layer 1：掃描 runfile 前 50 行，尋找 block name 設定。

    支援格式：
    - set DESIGN <name>
    - set TOP_MODULE <name>
    - set BLOCK_NAME <name>
    - set TOP_DESIGN <name>
    - set DESIGN_NAME <name>

    找到第一個匹配即回傳。runfile 不存在或無匹配時回傳 None。
    不拋出 exception，失敗靜默記錄 warning。
    """
    if runfile_path is None:
        return None

    path = Path(runfile_path)
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _MAX_SCAN_LINES:
                    break
                m = _SET_RE.match(line)
                if m is None:
                    continue
                var_name = m.group(1).upper()
                if var_name in _BLOCK_NAME_VARS:
                    # value：三組 capture group 中取非 None 的
                    value = m.group(2) or m.group(3) or m.group(4)
                    if value:
                        return value.strip()
    except Exception as exc:
        logger.warning("block_name runfile scan failed (%s): %s", runfile_path, exc)

    return None


# ─── Layer 2：lineage ledger 回溯 ────────────────────────────────────────────

def extract_block_name_from_lineage(ledger_path: str | None) -> str | None:
    """Layer 2：從 lineage ledger 尾端搜尋最近一筆有 block_name 的 JobCommit。

    回傳最新的 block_name，或 None。
    不拋出 exception，失敗靜默記錄 warning。
    """
    if ledger_path is None:
        return None

    try:
        from terminux_engine.lineage.ledger import load_job_commits
        commits = load_job_commits(ledger_path)
        # 從尾端搜尋（最新優先）
        for commit in reversed(commits):
            if commit.block_name is not None:
                return commit.block_name
    except Exception as exc:
        logger.warning("block_name lineage lookup failed (%s): %s", ledger_path, exc)

    return None
