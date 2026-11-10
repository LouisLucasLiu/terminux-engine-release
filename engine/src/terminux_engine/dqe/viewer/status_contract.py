# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer status / severity contract helpers。

本模組是純 mapping contract：
- 不讀 DB
- 不呼叫 isvn
- 不做 IPC
- 不執行任何 business workflow
"""

from __future__ import annotations

READ_UI_STATES = frozenset({
    "loading",
    "ready",
    "empty",
    "partial",
    "warning",
    "error",
})

ACTION_UI_STATES = frozenset({
    "action_success",
    "action_failed",
    "success_degraded",
    "refused",
    "destructive_confirm_required",
})

SEVERITY_LEVELS = frozenset({"neutral", "info", "warning", "error"})

SEMANTIC_TO_SEVERITY = {
    "loading": "neutral",
    "ready": "neutral",
    "empty": "neutral",
    "expired": "warning",
    "warning": "warning",
    "partial": "warning",
    "success_degraded": "warning",
    "refused": "neutral",
    "destructive_confirm_required": "warning",
    "action_success": "info",
    "action_failed": "error",
    "error": "error",
}

CHECKIN_RESULTS = frozenset({
    "not_run",
    "success_new",
    "success_update",
    "failed",
    "unknown",
})

PURGE_RESULTS = frozenset({"success", "failed", "refused", "unknown"})

SCHEDULED_CLEAN_RESULTS = frozenset({
    "success",
    "success_degraded",
    "failed",
    "invalid_args",
    "unknown",
})


def severity_for_state(state: str) -> str:
    """回傳 semantic state 的 severity；未知 state 採保守 warning。"""

    return SEMANTIC_TO_SEVERITY.get(state, "warning")


def classify_list_result(*, rpc_ok: bool, row_count: int) -> str:
    """將 list RPC 結果分類為 ready / empty / error。"""

    if not rpc_ok:
        return "error"
    return "ready" if row_count > 0 else "empty"


def classify_checkin_result(last_result: str, *, partial: bool = False) -> str:
    """分類 checkin domain result。"""

    if last_result in {"success_new", "success_update"}:
        return "action_success"
    if last_result == "failed":
        return "partial" if partial else "action_failed"
    return "warning"


def classify_purge_result(purge_result: str) -> str:
    """分類 purge domain result。"""

    if purge_result == "success":
        return "action_success"
    if purge_result == "refused":
        return "refused"
    if purge_result == "failed":
        return "action_failed"
    return "warning"


def classify_scheduled_clean_result(status: str, *, policy_degraded: bool = False) -> str:
    """分類 scheduled clean result。"""

    if status == "success_degraded" or policy_degraded:
        return "success_degraded"
    if status == "success":
        return "action_success"
    if status == "failed":
        return "action_failed"
    if status == "invalid_args":
        return "error"
    return "warning"


__all__ = [
    "ACTION_UI_STATES",
    "CHECKIN_RESULTS",
    "PURGE_RESULTS",
    "READ_UI_STATES",
    "SCHEDULED_CLEAN_RESULTS",
    "SEMANTIC_TO_SEVERITY",
    "SEVERITY_LEVELS",
    "classify_checkin_result",
    "classify_list_result",
    "classify_purge_result",
    "classify_scheduled_clean_result",
    "severity_for_state",
]
