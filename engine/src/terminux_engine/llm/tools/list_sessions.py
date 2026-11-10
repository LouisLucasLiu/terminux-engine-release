# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""list_sessions tool。

列最近 N session 摘要。走 EventStore.get_session_ids（DESC by MAX timestamp）
+ 逐個 query(session_id=...) 聚合 start/end/stage/block/commands/corners。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


_ACTIVE_THRESHOLD_SEC = 600.0  # session 最後一筆 event 在 10 分鐘內視為 active


SCHEMA = ToolSchema(
    name="list_sessions",
    description=(
        "List the most recent sessions with summary info: start/end time, "
        "stage, block, command count, and completed corners."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Number of sessions to return (default 5, max 20)",
            },
            "user_id": {
                "type": "string",
                "description": "Filter by user; omit to use the current user",
            },
        },
    },
)


def create_tool(event_store: Any, default_user_id: str = "") -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        if event_store is None:
            return {"sessions": [], "error": "EventStore not available"}

        try:
            n = int(kwargs.get("n", 5))
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(20, n))

        user_id = (kwargs.get("user_id") or default_user_id or "").strip() or None

        try:
            session_ids = event_store.get_session_ids(user_id=user_id, n=n)
        except Exception as e:
            _log.warning("list_sessions: get_session_ids failed: %s", e)
            return {"sessions": [], "error": f"EventStore query failed: {e}"}

        sessions: list[dict] = []
        now = time.time()
        for sid in session_ids:
            try:
                events = event_store.query(session_id=sid, order="asc")
            except Exception as e:
                _log.warning("list_sessions: query session %s failed: %s", sid, e)
                continue
            sessions.append(_summarize_session(sid, events, now))

        return {"sessions": sessions, "count": len(sessions)}

    return executor


def _summarize_session(sid: str, events: list[Any], now: float) -> dict:
    """聚合單一 session 的 events → 摘要 dict。events 已 ASC 排序。"""
    if not events:
        return {
            "session_id": sid,
            "start_time": None,
            "end_time": None,
            "active": False,
            "stage": None,
            "block_name": None,
            "corners_done": [],
            "command_count": 0,
            "duration_min": None,
        }

    start_ts = float(getattr(events[0], "timestamp", 0.0) or 0.0)
    end_ts = float(getattr(events[-1], "timestamp", 0.0) or 0.0)
    active = (now - end_ts) < _ACTIVE_THRESHOLD_SEC

    stage = None
    block_name = None
    command_count = 0
    corners_done: list[str] = []
    seen_corners: set[str] = set()

    for ev in events:
        et = getattr(ev, "event_type", "")
        payload = getattr(ev, "payload", None) or {}
        if et == "stage_change":
            to_stage = payload.get("to_stage")
            if to_stage:
                stage = to_stage
        elif et == "command":
            command_count += 1
        elif et == "corner_update":
            corner = payload.get("corner") or ""
            status = payload.get("status", "")
            if corner and corner not in seen_corners and status in ("pass", "fail"):
                seen_corners.add(corner)
                corners_done.append(corner)
        if block_name is None:
            bn = getattr(ev, "block_name", None)
            if bn:
                block_name = bn

    duration_sec = max(0.0, end_ts - start_ts)

    return {
        "session_id": sid,
        "start_time": _iso(start_ts),
        "end_time": None if active else _iso(end_ts),
        "active": active,
        "stage": stage,
        "block_name": block_name,
        "corners_done": corners_done,
        "command_count": command_count,
        "duration_min": round(duration_sec / 60.0, 1) if duration_sec > 0 else 0.0,
    }


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    try:
        return _dt.datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""
