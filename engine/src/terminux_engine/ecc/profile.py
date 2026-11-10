# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""EnrichedUserProfile — 從 event store 聚合進階行為統計。

取代舊有的計數器式 UserProfile + ProfileAccumulator。
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from terminux_engine.store.event_store import EventStore

logger = logging.getLogger(__name__)


# ─── 子 Models ───────────────────────────────────────────────────────────────

class BottleneckCorner(BaseModel):
    """gate_eval 失敗次數最多的 corner。"""
    corner: str
    fail_count: int


class CornerFirstWns(BaseModel):
    """某 corner 第一次跑出的 WNS。"""
    corner: str
    first_wns_ns: float
    session_id: str


class ToolUsage(BaseModel):
    """工具使用統計。"""
    tool_name: str
    run_count: int


# ─── EnrichedUserProfile ─────────────────────────────────────────────────────

class EnrichedUserProfile(BaseModel):
    """從 event store 聚合的進階行為統計。"""

    user_id: str
    computed_at: float
    event_window_days: int
    total_sessions: int
    total_commands: int

    typical_work_hours: list[int]
    """最活躍的 top-3 小時（0–23），按活躍度降序"""

    avg_session_duration_min: float
    """平均 session 時長（分鐘），排除 < 60 秒的 session"""

    bottleneck_corners: list[BottleneckCorner]
    """fail 次數最多的 top-5 corners"""

    corner_first_run_wns: list[CornerFirstWns]
    """每個 corner 第一次跑的 WNS"""

    most_used_tools: list[ToolUsage]
    """按 run_count 降序，最多 10 個"""


# ─── 聚合函數 ────────────────────────────────────────────────────────────────

def build_enriched_profile(
    event_store: EventStore,
    user_id: str,
    days: int = 30,
) -> EnrichedUserProfile:
    """從 event_store 聚合最近 N 天的 events，建構 EnrichedUserProfile。"""
    cutoff = time.time() - days * 86400

    # 取得所有 events（時間窗口內）
    all_events = event_store.query(user_id=user_id, after=cutoff)

    # ── total_sessions ────────────────────────────────────────────────────
    session_ids = {e.session_id for e in all_events}
    total_sessions = len(session_ids)

    # ── total_commands + typical_work_hours + most_used_tools ─────────────
    command_events = [e for e in all_events if e.event_type == "command"]
    total_commands = len(command_events)

    hour_counter: Counter[int] = Counter()
    tool_counter: Counter[str] = Counter()
    for ce in command_events:
        hour_counter[datetime.fromtimestamp(ce.timestamp).hour] += 1
        tn = ce.payload.get("tool_name")
        if tn:
            tool_counter[tn] += 1

    typical_work_hours = [h for h, _ in hour_counter.most_common(3)]

    most_used_tools = [
        ToolUsage(tool_name=t, run_count=c)
        for t, c in tool_counter.most_common(10)
    ]

    # ── avg_session_duration_min ──────────────────────────────────────────
    session_times: dict[str, tuple[float, float]] = {}  # sid -> (min_ts, max_ts)
    for e in all_events:
        sid = e.session_id
        if sid not in session_times:
            session_times[sid] = (e.timestamp, e.timestamp)
        else:
            mn, mx = session_times[sid]
            session_times[sid] = (min(mn, e.timestamp), max(mx, e.timestamp))

    durations = [
        (mx - mn) for mn, mx in session_times.values()
        if (mx - mn) >= 60
    ]
    avg_session_duration_min = (
        (sum(durations) / len(durations) / 60.0) if durations else 0.0
    )

    # ── bottleneck_corners ────────────────────────────────────────────────
    gate_events = [e for e in all_events if e.event_type == "gate_eval"]
    corner_fail_counter: Counter[str] = Counter()
    for ge in gate_events:
        if ge.payload.get("passed") is False:
            corner = ge.payload.get("corner")
            if corner:
                corner_fail_counter[corner] += 1

    bottleneck_corners = [
        BottleneckCorner(corner=c, fail_count=n)
        for c, n in corner_fail_counter.most_common(5)
    ]

    # ── corner_first_run_wns ─────────────────────────────────────────────
    metric_events = [e for e in all_events if e.event_type == "metric"]
    wns_events = [
        e for e in metric_events
        if e.payload.get("metric_name") == "wns_ns"
    ]
    # 按 corner 分組，取每個 corner 的 MIN(timestamp) 那筆
    corner_first: dict[str, tuple[float, float, str]] = {}  # corner -> (ts, value, session_id)
    for we in wns_events:
        corner = we.payload.get("corner")
        if not corner:
            continue
        value = we.payload.get("value")
        if value is None:
            continue
        if corner not in corner_first or we.timestamp < corner_first[corner][0]:
            corner_first[corner] = (we.timestamp, float(value), we.session_id)

    corner_first_run_wns = [
        CornerFirstWns(corner=c, first_wns_ns=v, session_id=sid)
        for c, (_, v, sid) in sorted(corner_first.items())
    ]

    return EnrichedUserProfile(
        user_id=user_id,
        computed_at=time.time(),
        event_window_days=days,
        total_sessions=total_sessions,
        total_commands=total_commands,
        typical_work_hours=typical_work_hours,
        avg_session_duration_min=round(avg_session_duration_min, 1),
        bottleneck_corners=bottleneck_corners,
        corner_first_run_wns=corner_first_run_wns,
        most_used_tools=most_used_tools,
    )
