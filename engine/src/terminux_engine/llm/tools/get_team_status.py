# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""get_team_status tool。

讀已建好的 TeamSnapshot（ecc.team.TeamSnapshot），轉成 LLM 可消化的 dict。

note：spec 的範例輸出有 'stage' 欄位，但 TeamMemberSnapshot 沒有對應欄位 →
本實作回傳 stage=None。member.block_names 是 list（spec 寫單一字串），保留
為 list 並用 'block_name in member.block_names' 做過濾。
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


_TEAM_LIMIT = 20


SCHEMA = ToolSchema(
    name="get_team_status",
    description=(
        "Return a snapshot of other engineers' progress on the same project: "
        "block, last activity, corners passed/failed, latest WNS per corner."
    ),
    parameters={
        "type": "object",
        "properties": {
            "block_name": {
                "type": "string",
                "description": "Filter to members touching this block; omit for all",
            },
        },
    },
)


def create_tool(team_snapshot: Any) -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        if team_snapshot is None:
            return {"available": False, "note": "Team snapshot not available"}

        block_filter = (kwargs.get("block_name") or "").strip() or None

        try:
            members = list(getattr(team_snapshot, "members", []) or [])
        except Exception as e:
            _log.warning("get_team_status: accessing members failed: %s", e)
            return {"available": False, "note": f"TeamSnapshot read failed: {e}"}

        if block_filter:
            members = [
                m for m in members
                if block_filter in (getattr(m, "block_names", None) or [])
            ]

        members = members[:_TEAM_LIMIT]

        team_list = [
            {
                "user_id": getattr(m, "user_id", ""),
                "block_names": list(getattr(m, "block_names", None) or []),
                "stage": None,
                "last_active": _epoch_to_iso(getattr(m, "last_active", 0.0)),
                "last_session_id": getattr(m, "last_session_id", ""),
                "recent_commands": getattr(m, "recent_commands", 0),
                "corners_done": list(getattr(m, "corners_passed", None) or []),
                "corners_failed": list(getattr(m, "corners_failed", None) or []),
                "corners_total": len(getattr(m, "corners_run", None) or []),
                "latest_wns": dict(getattr(m, "latest_wns", None) or {}),
            }
            for m in members
        ]

        return {
            "available": True,
            "project_id": getattr(team_snapshot, "project_id", ""),
            "snapshot_time": _epoch_to_iso(getattr(team_snapshot, "computed_at", 0.0)),
            "team": team_list,
        }

    return executor


def _epoch_to_iso(epoch: float) -> str:
    try:
        if not epoch:
            return ""
        return _dt.datetime.fromtimestamp(float(epoch)).isoformat(timespec="seconds")
    except Exception:
        return ""
