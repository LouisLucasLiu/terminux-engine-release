# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Prompt phase LLM 回覆轉換為標準 PhaseResult。"""

from __future__ import annotations

import logging
import re
from typing import Literal

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.result import (
    NotificationRequest,
    PhaseResult,
    RecordRequest,
)

_log = logging.getLogger(__name__)
_JUDGMENT_RE = re.compile(r"^\s*\[STATUS:\s*(ok|warn|fail)\]\s*(.*)$", re.I | re.S)
_DIGEST_RE = re.compile(r"^\s*\[DIGEST\]\s*(.+)$", re.I | re.S)
_COMPARISON_RE = re.compile(
    r"^\s*\[COMPARISON:\s*(improved|degraded|unchanged)\]\s*(.*)$",
    re.I | re.S,
)


def convert_prompt_response(
    pattern: Literal["judgment", "digest", "comparison"],
    response: str,
    ctx: PackageContext,
) -> PhaseResult | None:
    """依 pattern 將 LLM 純文字回覆轉成 PhaseResult；無法解析時回 None。"""
    if pattern == "judgment":
        return _convert_judgment(response, ctx)
    if pattern == "digest":
        return _convert_digest(response, ctx)
    if pattern == "comparison":
        return _convert_comparison(response, ctx)
    _log.warning("Unknown prompt pattern: %s", pattern)
    return None


def _convert_judgment(response: str, ctx: PackageContext) -> PhaseResult | None:
    match = _JUDGMENT_RE.match(response)
    if match is not None:
        status = match.group(1).lower()
        reason = match.group(2).strip() or "未提供理由"
    else:
        status = _infer_judgment_status(response)
        reason = response.strip()
        if status is None or not reason:
            _log.warning("Prompt judgment response is not parseable: %s", response[:300])
            return None

    notifications: list[NotificationRequest] = []
    if status == "warn":
        notifications.append(NotificationRequest(level="warning", title="Package 判斷警告", message=reason))
    elif status == "fail":
        notifications.append(NotificationRequest(level="error", title="Package 判斷失敗", message=reason))
    return PhaseResult(status=status, message=reason, notifications=notifications)


def _convert_digest(response: str, ctx: PackageContext) -> PhaseResult | None:
    match = _DIGEST_RE.match(response)
    if match is None or not match.group(1).strip():
        _log.warning("Prompt digest response is not parseable: %s", response[:300])
        return None
    summary = match.group(1).strip()
    record = _record_for(ctx, "digest", {"summary": summary})
    return PhaseResult(status="ok", message=summary, records=[record])


def _convert_comparison(response: str, ctx: PackageContext) -> PhaseResult | None:
    match = _COMPARISON_RE.match(response)
    if match is not None:
        outcome = match.group(1).lower()
        reason = match.group(2).strip() or "未提供比較理由"
    else:
        outcome = _infer_comparison_outcome(response)
        reason = response.strip()
        if outcome is None or not reason:
            _log.warning("Prompt comparison response is not parseable: %s", response[:300])
            return None

    record = _record_for(ctx, "comparison", {"outcome": outcome, "summary": reason})
    if outcome == "degraded":
        notification = NotificationRequest(
            level="warning",
            title="Package 比較結果惡化",
            message=reason,
        )
        return PhaseResult(
            status="warn",
            message=reason,
            records=[record],
            notifications=[notification],
        )
    return PhaseResult(status="ok", message=reason, records=[record])


def _record_for(
    ctx: PackageContext,
    pattern: str,
    details: dict[str, str],
) -> RecordRequest:
    return RecordRequest(
        namespace=ctx.package_id,
        record_type=f"prompt_{pattern}",
        key=f"{ctx.package_id}:{pattern}:{ctx.event.event_id}",
        payload={
            "pattern": pattern,
            "source_event_id": ctx.event.event_id,
            "tool_name": ctx.event.tool_name,
            **details,
        },
        display_message=details.get("summary"),
        timestamp=ctx.event.timestamp,
    )


def _infer_judgment_status(response: str) -> str | None:
    text = response.lower()
    if "warn" in text or "警告" in response or "警示" in response:
        return "warn"
    if "fail" in text or "失敗" in response:
        return "fail"
    if "ok" in text or "通過" in response or "正常" in response:
        return "ok"
    return None


def _infer_comparison_outcome(response: str) -> str | None:
    text = response.lower()
    if "degraded" in text or "惡化" in response:
        return "degraded"
    if "improved" in text or "改善" in response or "變好" in response:
        return "improved"
    if "unchanged" in text or "持平" in response or "無變化" in response:
        return "unchanged"
    return None
