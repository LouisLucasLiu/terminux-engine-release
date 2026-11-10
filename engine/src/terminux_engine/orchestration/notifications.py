# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Pending-review notification payload 與 NotificationRequest adapter。

只把 conditional scanner winner 已 commit 的 `abandoned + pending_review`
`RunRecord` 轉成既有 public `NotificationRequest`；不讀 ledger 第二次、不讀
checkpoint／PID registry／workspace，也不修改 `package_runtime.services` 或
`NotificationRequest` 定義。
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict

from terminux_engine.orchestration.ledger import RunRecord
from terminux_engine.package_runtime.result import NotificationRequest

__all__ = [
    "PendingReviewPayload",
    "build_pending_review_payload",
    "build_pending_review_notification",
]


class PendingReviewPayload(TypedDict):
    """abandoned + pending_review run 的固定五欄通知內容。"""

    run_id: str
    workflow_type: str
    status: Literal["abandoned"]
    terminal_reason: str | None
    recovery_disposition: Literal["pending_review"]


def build_pending_review_payload(run: RunRecord) -> PendingReviewPayload:
    """只接受 `status='abandoned'` 且 `recovery_disposition='pending_review'`。

    其他任何 status／disposition（含 `dismissed`）皆 raise `ValueError`；不
    容錯改寫，不建立新 status 或 disposition。
    """
    if run.status != "abandoned":
        raise ValueError(
            f"build_pending_review_payload requires status='abandoned', "
            f"got status={run.status!r} (run_id={run.run_id!r})"
        )
    if run.recovery_disposition != "pending_review":
        raise ValueError(
            "build_pending_review_payload requires "
            f"recovery_disposition='pending_review', got "
            f"recovery_disposition={run.recovery_disposition!r} (run_id={run.run_id!r})"
        )
    return {
        "run_id": run.run_id,
        "workflow_type": run.workflow_type,
        "status": "abandoned",
        "terminal_reason": run.terminal_reason,
        "recovery_disposition": "pending_review",
    }


def build_pending_review_notification(
    payload: PendingReviewPayload,
) -> NotificationRequest:
    """固定產生 warning-level、deterministic compact JSON message 的 request。"""
    message = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return NotificationRequest(
        level="warning",
        title="Orchestration run requires review",
        message=message,
        actions=[],
    )
