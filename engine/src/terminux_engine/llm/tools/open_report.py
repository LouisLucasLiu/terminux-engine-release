# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""open_report tool：開啟已生成的報表 HTML（lineage graph）。

這是目前唯一「有副作用」的 agent tool，但副作用受嚴格限制：
- 只「請求開啟」白名單位置（~/.terminux/lineage_graph.html）下**既有**的報表檔。
- 不執行任何指令、不寫使用者檔案、不生成報表。
- 實際開啟由 C++ GUI 端（QDesktopServices）在使用者的 display 進行——本 tool
  透過注入的 on_open callback 把路徑交給 daemon 的 pending buffer，chat handler
  再附在回應的 open_path 欄位回 C++（daemon 不自行開瀏覽器，避免 SSH
  X-forward display 錯位）。

找不到報表 → 回 opened=False + 提示先生成（不報錯、不中斷 agent loop）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)

_VALID_TYPES = ("lineage_graph", "latest")

SCHEMA = ToolSchema(
    name="open_report",
    description=(
        "Open a previously generated report HTML in the user's browser: the "
        "lineage graph. Use this when the user asks to open / show / view a "
        "report that has already been generated. This does NOT generate "
        "reports — if none exists yet, it returns opened=false with a hint to "
        "generate one first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "report_type": {
                "type": "string",
                "enum": list(_VALID_TYPES),
                "description": (
                    "Which report to open: 'lineage_graph' or 'latest'. "
                    "Default: latest."
                ),
            },
        },
    },
)


def _find_lineage_graph(home: Path) -> Path | None:
    p = home / ".terminux" / "lineage_graph.html"
    return p if p.exists() else None


def create_tool(
    on_open: Callable[[str], None] | None,
    *,
    home: Path | None = None,
) -> Callable[..., dict]:
    """factory。on_open(path) 由 daemon 注入：把路徑放進 pending buffer 供
    chat handler 附到回應的 open_path。home 預設 Path.home()（測試可覆寫）。"""
    _home = home or Path.home()

    def executor(**kwargs: Any) -> dict:
        raw = (kwargs.get("report_type") or "latest").strip().lower()
        rtype = raw if raw in _VALID_TYPES else "latest"

        target: Path | None = None
        if rtype in ("lineage_graph", "latest"):
            target = _find_lineage_graph(_home)

        if target is None:
            return {
                "opened": False,
                "report_type": rtype,
                "note": (
                    "No matching report found. Generate one first — e.g. ask "
                    "for a 'lineage graph'."
                ),
            }

        path = str(target)
        if on_open is not None:
            try:
                on_open(path)
            except Exception as exc:  # callback 失敗不中斷 agent loop
                _log.warning("open_report on_open callback failed: %s", exc)
                return {
                    "opened": False,
                    "report_type": rtype,
                    "path": path,
                    "note": f"Open request could not be queued: {exc}",
                }

        return {
            "opened": True,
            "report_type": rtype,
            "path": path,
            "note": "Report will open in the user's browser.",
        }

    return executor
