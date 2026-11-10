# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Loader for [Recent Work History] prompt section.

從 EventStore 撈 daily_digest + immediate period_digest events，
組成 RecentWorkHistoryPayload 供 AICompanion 消費。

截斷邏輯全部在此完成，model 內的 llm_summary 欄位已是截斷後的文字。
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from terminux_engine.memory.digest_models import (
    DailyDigestSummary,
    ImmediatePeriodDigestSummary,
    RecentWorkHistoryPayload,
)

if TYPE_CHECKING:
    from terminux_engine.store.event_store import EventStore

logger = logging.getLogger(__name__)


def _truncate(text: str, max_chars: int) -> str:
    """截斷文字。超過上限在末尾加 '…'（U+2026）。"""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "\u2026"


def load_recent_work_history(
    event_store: "EventStore",
    user_id: str,
    now_epoch: float,
    recent_history_days: int,
    max_digest_chars: int,
    max_today_immediate: int,
    max_immediate_chars: int,
) -> RecentWorkHistoryPayload:
    """從 event_store 組 [Recent Work History] payload。

    截斷在本 function 完成（model 內的 llm_summary 欄位已是截斷後的文字）。

    Args:
        event_store: daemon 層傳入的 EventStore instance
        user_id: 要查詢的 user
        now_epoch: 「現在」的 epoch（測試時可注入任意時間）
        recent_history_days: 過去 N 天（含今天）
        max_digest_chars: daily_digest 單則截斷長度
        max_today_immediate: 今天 immediate 最多保留幾則
        max_immediate_chars: immediate 單則截斷長度

    Returns:
        RecentWorkHistoryPayload。無資料時兩個 list 都為空（非 None）。
    """
    # ── Step 1：撈 daily_digest（過去 N 天，含今天）──────────────────────────
    today_local = datetime.datetime.fromtimestamp(now_epoch).date()
    start_date = today_local - datetime.timedelta(days=recent_history_days - 1)
    start_ts = datetime.datetime.combine(start_date, datetime.time.min).timestamp()
    end_ts = datetime.datetime.combine(today_local, datetime.time.max).timestamp()

    daily_events = event_store.query_events_in_range(
        user_id=user_id,
        start_ts=start_ts,
        end_ts=end_ts,
        event_types=["daily_digest"],
    )

    # ── Step 2：轉為 DailyDigestSummary，去重 + 排序 ────────────────────────
    # 同一 date 多個 daily_digest → 取 timestamp 最新的那個
    best_by_date: dict[str, object] = {}  # date → event (最新 timestamp)
    for ev in daily_events:
        date_val = ev.payload.get("date")
        if not date_val:
            logger.warning("daily_digest event missing 'date' in payload, skipping event_id=%s", ev.event_id)
            continue
        llm_summary = ev.payload.get("llm_summary", "")
        if not llm_summary:
            # 跳過空 summary
            continue
        existing = best_by_date.get(date_val)
        if existing is None or ev.timestamp > existing.timestamp:  # type: ignore[union-attr]
            best_by_date[date_val] = ev

    daily_digests: list[DailyDigestSummary] = []
    for date_val in sorted(best_by_date.keys(), reverse=True):
        ev = best_by_date[date_val]
        summary_text = _truncate(ev.payload["llm_summary"], max_digest_chars)  # type: ignore[union-attr]
        daily_digests.append(DailyDigestSummary(
            date=date_val,
            llm_summary=summary_text,
        ))

    # ── Step 3：撈今天的 immediate period_digest ────────────────────────────
    today_start_ts = datetime.datetime.combine(today_local, datetime.time.min).timestamp()

    period_events = event_store.query_events_in_range(
        user_id=user_id,
        start_ts=today_start_ts,
        end_ts=now_epoch,
        event_types=["period_digest"],
    )

    # ── Step 4：filter immediate + 轉為 ImmediatePeriodDigestSummary ───────
    immediates: list[ImmediatePeriodDigestSummary] = []
    for ev in period_events:
        if ev.payload.get("digest_type") != "immediate":
            continue
        llm_summary = ev.payload.get("llm_summary", "")
        if not llm_summary:
            continue
        # 安全提取 period_start / period_end
        try:
            p_start = float(ev.payload["period_start"])
            p_end = float(ev.payload["period_end"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "period_digest event missing period_start/period_end, skipping event_id=%s",
                ev.event_id,
            )
            continue
        summary_text = _truncate(llm_summary, max_immediate_chars)
        immediates.append(ImmediatePeriodDigestSummary(
            period_start=p_start,
            period_end=p_end,
            llm_summary=summary_text,
        ))

    # 按 period_start ASC 排序（最舊在前）
    immediates.sort(key=lambda x: x.period_start)

    # 超過 max_today_immediate 時保留最舊的
    if len(immediates) > max_today_immediate:
        immediates = immediates[:max_today_immediate]

    # ── Step 5：組 payload 回傳 ─────────────────────────────────────────────
    return RecentWorkHistoryPayload(
        daily_digests=daily_digests,
        today_immediates=immediates,
    )
