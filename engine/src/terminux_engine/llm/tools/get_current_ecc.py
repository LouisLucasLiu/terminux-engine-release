# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""get_current_ecc tool。

取當前 session 的 ECC 完整狀態。透過 accumulator.get_current() 讀 ECCObject，
所有欄位 best-effort 取出（缺失 → null）；不呼叫會改變狀態的 method。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema

_log = logging.getLogger(__name__)


SCHEMA = ToolSchema(
    name="get_current_ecc",
    description=(
        "Return the current ECC session state: stage, tool, block, corner "
        "coverage, latest metrics per corner, gate results, and command count."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)


def create_tool(accumulator: Any) -> Callable[..., dict]:
    def executor(**kwargs: Any) -> dict:
        if accumulator is None:
            return {"active": False, "note": "ECC 未啟用：沒有可用的 ECC accumulator。"}

        try:
            ecc = accumulator.get_current()
        except Exception as e:
            _log.warning("get_current_ecc: get_current() failed: %s", e)
            return {"active": False, "note": f"Accumulator unavailable: {e}"}

        if ecc is None:
            return {"active": False, "note": "No active ECC session"}

        return {
            "active": True,
            "session_id": _safe(ecc, "session_id"),
            "stage": _safe(ecc, "current_stage_id"),
            "tool_name": _latest_tool_name(ecc),
            "block_name": _safe(accumulator, "current_block_name"),
            "work_directory": _safe(ecc, "work_directory") or "",
            "corners_done": _corners_done(ecc),
            "corners_pending": _corners_pending(ecc),
            "latest_metrics": _latest_metrics(ecc),
            "gate_results": _gate_results_by_corner(ecc),
            "iteration_count": None,
            "command_count": _command_count(ecc),
        }

    return executor


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe(obj: Any, attr: str) -> Any:
    """讀屬性，缺失或例外時回傳 None。"""
    try:
        v = getattr(obj, attr, None)
        return v
    except Exception:
        return None


def _latest_tool_name(ecc: Any) -> str | None:
    history = _safe(ecc, "command_history") or []
    if not history:
        return None
    last = history[-1]
    return _safe(last, "tool_name")


def _command_count(ecc: Any) -> int | None:
    history = _safe(ecc, "command_history")
    if history is None:
        return None
    try:
        return len(history)
    except Exception:
        return None


def _corners_done(ecc: Any) -> list[str]:
    cc = _safe(ecc, "corner_coverage")
    if cc is None:
        return []
    passed = list(_safe(cc, "completed_pass") or [])
    failed = list(_safe(cc, "completed_fail") or [])
    # 保留順序、去重
    seen: set[str] = set()
    out: list[str] = []
    for c in passed + failed:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _corners_pending(ecc: Any) -> list[str]:
    cc = _safe(ecc, "corner_coverage")
    if cc is None:
        return []
    required = list(_safe(cc, "required_corners") or [])
    done = set(_corners_done(ecc))
    return [c for c in required if c not in done]


def _latest_metrics(ecc: Any) -> dict[str, dict[str, float | None]]:
    """metric_records 是 append-only 時序 list；後面的覆蓋前面。"""
    records = _safe(ecc, "metric_records") or []
    out: dict[str, dict[str, float | None]] = {}
    for m in records:
        corner = _safe(m, "corner") or "no_corner"
        name = _safe(m, "metric_name")
        value = _safe(m, "value")
        if not name:
            continue
        out.setdefault(corner, {})[name] = value
    return out


def _gate_results_by_corner(ecc: Any) -> dict[str, dict[str, Any]]:
    """每個 corner 取最後一筆 gate result；後面覆蓋前面。"""
    results = _safe(ecc, "gate_results") or []
    out: dict[str, dict[str, Any]] = {}
    for g in results:
        corner = _safe(g, "corner") or "no_corner"
        passed = bool(_safe(g, "passed"))
        out[corner] = {
            "gate_id": _safe(g, "gate_id"),
            "metric_name": _safe(g, "metric_name"),
            "actual_value": _safe(g, "actual_value"),
            "threshold": _safe(g, "threshold"),
            "operator": _safe(g, "operator"),
            "status": "PASS" if passed else "FAIL",
        }
    return out
