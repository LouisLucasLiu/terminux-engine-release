# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""query_metric_trend tool。

查特定 corner + metric 的歷史趨勢（跨 session）。

策略：用 EventStore.query() 撈 event_type='metric' + corner + user_id 的所有
事件（DESC by timestamp），Python 層 post-filter metric_name；按 session_id
分組，每個 session 取最新一筆。

note：metric event payload 沒有 iteration 欄位，輸出的 iteration 欄位固定 None。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


SCHEMA = ToolSchema(
    name="query_metric_trend",
    description=(
        "Query historical trend of a specific (corner, metric) pair across "
        "the most recent sessions. Returns one data point per session."
    ),
    parameters={
        "type": "object",
        "properties": {
            "corner": {
                "type": "string",
                "description": "Corner name, e.g. 'ss_0.72v_125c'",
            },
            "metric": {
                "type": "string",
                "description": "Metric name, e.g. 'wns_ns', 'tns_ns', 'nvp'",
            },
            "n_sessions": {
                "type": "integer",
                "description": "How many recent sessions to look back (default 5, max 20)",
            },
        },
        "required": ["corner", "metric"],
    },
)


def create_tool(event_store: Any, default_user_id: str = "") -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        corner = (kwargs.get("corner") or "").strip()
        metric = (kwargs.get("metric") or "").strip()
        if not corner or not metric:
            return {
                "trend": [],
                "corner": corner,
                "metric": metric,
                "error": "Both 'corner' and 'metric' are required",
            }

        try:
            n_sessions = int(kwargs.get("n_sessions", 5))
        except (TypeError, ValueError):
            n_sessions = 5
        n_sessions = max(1, min(20, n_sessions))

        user_id = (kwargs.get("user_id") or default_user_id or "").strip() or None

        try:
            events = event_store.query(
                user_id=user_id,
                event_type="metric",
                corner=corner,
                order="desc",
            )
        except Exception as e:
            _log.warning("query_metric_trend: event_store query failed: %s", e)
            return {
                "trend": [],
                "corner": corner,
                "metric": metric,
                "error": f"EventStore query failed: {e}",
            }

        # post-filter by metric_name；events 已 DESC，按 session 取第一筆即「最新」
        by_session: dict[str, Any] = {}
        for e in events:
            payload = getattr(e, "payload", None) or {}
            if payload.get("metric_name") != metric:
                continue
            sid = getattr(e, "session_id", "")
            if sid and sid not in by_session:
                by_session[sid] = e

        if not by_session:
            return {
                "trend": [],
                "corner": corner,
                "metric": metric,
                "note": "No data found",
            }

        trend_events = sorted(
            by_session.values(),
            key=lambda ev: getattr(ev, "timestamp", 0.0),
            reverse=True,
        )[:n_sessions]

        trend = [
            {
                "session_id": getattr(e, "session_id", ""),
                "timestamp": getattr(e, "timestamp", 0.0),
                "value": (getattr(e, "payload", None) or {}).get("value"),
                "iteration": None,
            }
            for e in trend_events
        ]

        return {
            "trend": trend,
            "corner": corner,
            "metric": metric,
        }

    return executor
