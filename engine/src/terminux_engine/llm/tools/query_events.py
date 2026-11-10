# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""query_events tool。

查 EventStore 中的歷史事件。底層走 query_events_in_range（支援多 event_type
+ 時間窗），corner 過濾在 Python 層做（query_events_in_range 不支援）。

dependency injection：
    - event_store: EventStore
    - default_user_id: str  — 當 caller 未提供 user_id 時的 fallback
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


SCHEMA = ToolSchema(
    name="query_events",
    description=(
        "Query historical session events from EventStore. Supports filtering "
        "by user, event type list, corner, and time window."
    ),
    parameters={
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "Filter by user; omit to use the current user",
            },
            "event_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Filter by event type list, e.g. "
                    "['command', 'metric', 'gate_eval']"
                ),
            },
            "corner": {
                "type": "string",
                "description": "Filter by corner name (post-filter on payload)",
            },
            "hours_back": {
                "type": "number",
                "description": "Look back N hours (default 24)",
            },
            "limit": {
                "type": "integer",
                "description": "Max events to return (default 20, max 50)",
            },
        },
    },
)


def create_tool(event_store: Any, default_user_id: str = "") -> Callable[..., dict]:
    """Factory：注入 event_store + default_user_id，回傳 tool executor。"""

    def executor(**kwargs: Any) -> dict:
        user_id = (kwargs.get("user_id") or default_user_id or "").strip()
        if not user_id:
            return {
                "events": [],
                "total_found": 0,
                "truncated": False,
                "error": "user_id not provided and no default available",
            }

        event_types = kwargs.get("event_types") or None
        if event_types is not None and not isinstance(event_types, list):
            event_types = [str(event_types)]

        corner_filter = kwargs.get("corner")
        try:
            hours_back = float(kwargs.get("hours_back", 24))
        except (TypeError, ValueError):
            hours_back = 24.0
        if hours_back <= 0:
            hours_back = 24.0

        try:
            limit = int(kwargs.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(50, limit))

        now = time.time()
        start_ts = now - hours_back * 3600.0

        try:
            events = event_store.query_events_in_range(
                user_id=user_id,
                start_ts=start_ts,
                end_ts=now,
                event_types=event_types,
            )
        except Exception as e:
            _log.warning("query_events: event_store call failed: %s", e)
            return {
                "events": [],
                "total_found": 0,
                "truncated": False,
                "error": f"EventStore query failed: {e}",
            }

        if corner_filter:
            events = [
                e for e in events
                if isinstance(getattr(e, "payload", None), dict)
                and e.payload.get("corner") == corner_filter
            ]

        total_found = len(events)
        # query_events_in_range 回傳 ASC；保留最新 N 筆
        kept = events[-limit:] if total_found > limit else events
        truncated = total_found > limit

        summarized = []
        for e in kept:
            payload = getattr(e, "payload", None) or {}
            summary = json.dumps(payload, ensure_ascii=False, default=str)
            summarized.append(
                {
                    "timestamp": getattr(e, "timestamp", 0.0),
                    "event_type": getattr(e, "event_type", ""),
                    "session_id": getattr(e, "session_id", ""),
                    "corner": payload.get("corner"),
                    "payload_summary": summary[:200],
                }
            )

        return {
            "events": summarized,
            "total_found": total_found,
            "truncated": truncated,
        }

    return executor
