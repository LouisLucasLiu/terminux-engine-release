# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Read-path summary models for [Recent Work History] prompt section.

These are lightweight view models consumed by AICompanion. They are
intentionally separate from memory/dreaming.py's write-path PeriodDigest /
DailyDigest models to keep read/write boundaries clean.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DailyDigestSummary(BaseModel):
    """單日的 daily_digest 摘要（已截斷）。"""
    date: str = Field(..., description="YYYY-MM-DD")
    llm_summary: str = Field(..., description="已截斷到 MAX_DIGEST_CHARS")


class ImmediatePeriodDigestSummary(BaseModel):
    """今天的 immediate period_digest 摘要（已截斷）。"""
    period_start: float = Field(..., description="epoch seconds")
    period_end: float = Field(..., description="epoch seconds")
    llm_summary: str = Field(..., description="已截斷到 MAX_IMMEDIATE_CHARS")


class RecentWorkHistoryPayload(BaseModel):
    """AICompanion 消費的 [Recent Work History] 資料 bundle。"""
    daily_digests: list[DailyDigestSummary] = Field(
        default_factory=list,
        description="按 date DESC（最近在前），最多 RECENT_HISTORY_DAYS 個",
    )
    today_immediates: list[ImmediatePeriodDigestSummary] = Field(
        default_factory=list,
        description="按 period_start ASC（最舊在前），最多 MAX_TODAY_IMMEDIATE 個",
    )
