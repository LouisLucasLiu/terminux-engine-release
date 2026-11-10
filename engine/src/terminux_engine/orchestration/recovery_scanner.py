# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Daemon 啟動時的一次性 orchestration recovery scanner。"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from terminux_engine.orchestration.daemon_service import OrchestrationService
from terminux_engine.orchestration.ledger import OrchestrationLedger, RunRecord
from terminux_engine.orchestration.notifications import (
    build_pending_review_notification,
    build_pending_review_payload,
)
from terminux_engine.orchestration.process_group import (
    ProcessGroupSignalResult,
    signal_owned_process_group,
)
from terminux_engine.package_runtime.services import NotifyService


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    """一次 recovery scan 的結果。"""

    re_attached: list[str]
    marked_pending_review: list[str]


class RecoveryScanner:
    """在 daemon endpoint 開始接受請求前處理遺留 runs。"""

    def __init__(
        self,
        ledger: OrchestrationLedger,
        orchestration_service: OrchestrationService,
        *,
        stale_threshold_seconds: float = 30.0,
        notify_service: NotifyService | None = None,
    ) -> None:
        self._ledger = ledger
        self._service = orchestration_service
        self._stale_threshold_seconds = stale_threshold_seconds
        self._notify_service = notify_service

    def scan_once(self) -> RecoveryReport:
        """執行一次 recovery，回傳 re-attach 與待人工檢視清單。"""
        now = time.time()
        re_attached: list[str] = []
        marked_pending_review: list[str] = []
        for run in self._ledger.list_running_runs():
            if run.child_pid is None:
                if self._mark_abandoned(
                    run.run_id,
                    child_pid=None,
                    heartbeat_not_after=None,
                    terminal_reason="no_child_pid",
                ):
                    marked_pending_review.append(run.run_id)
                continue
            try:
                os.kill(run.child_pid, 0)
            except ProcessLookupError:
                if self._mark_abandoned(
                    run.run_id,
                    child_pid=run.child_pid,
                    heartbeat_not_after=None,
                    terminal_reason="child_not_found",
                ):
                    marked_pending_review.append(run.run_id)
                continue
            except PermissionError:
                _LOG.warning("Cannot inspect recovery child: pid=%s", run.child_pid)
                continue

            if (
                run.last_heartbeat_at is not None
                and now - run.last_heartbeat_at < self._stale_threshold_seconds
            ):
                self._service.register_existing_run(run.run_id, run.child_pid)
                re_attached.append(run.run_id)
                continue

            if self._mark_abandoned(
                run.run_id,
                child_pid=run.child_pid,
                heartbeat_not_after=now - self._stale_threshold_seconds,
                terminal_reason="child_zombie_at_recovery",
            ):
                marked_pending_review.append(run.run_id)
        return RecoveryReport(re_attached, marked_pending_review)

    def _mark_abandoned(
        self,
        run_id: str,
        *,
        child_pid: int | None,
        heartbeat_not_after: float | None,
        terminal_reason: str,
    ) -> bool:
        """僅由 conditional winner 標 abandoned，之後才處理仍存活的 group。"""

        result = self._ledger.mark_abandoned_if_running_and_no_cancel(
            run_id,
            expected_child_pid=child_pid,
            heartbeat_not_after=heartbeat_not_after,
            terminal_reason=terminal_reason,  # type: ignore[arg-type]
        )
        if not result.applied:
            self._ledger.get_run(run_id)
            return False
        if child_pid is None or heartbeat_not_after is None:
            self._service.unregister_run(run_id, expected_pid=child_pid)
            self._notify_pending_review(result.run)
            return True
        signal_result = signal_owned_process_group(child_pid)
        if signal_result is ProcessGroupSignalResult.SENT:
            self._service.wait_for_registered_child(run_id, expected_pid=child_pid)
        elif signal_result is ProcessGroupSignalResult.NOT_OWNER:
            _LOG.error(
                "Abandoned recovery zombie has non-owned process group: run_id=%s pid=%s",
                run_id,
                child_pid,
            )
        self._service.unregister_run(run_id, expected_pid=child_pid)
        self._notify_pending_review(result.run)
        return True

    def _notify_pending_review(self, run: RunRecord) -> None:
        """conditional winner 已 commit 後 best-effort 投遞 pending-review 通知。

        `notify_service=None` 時完全不 build／emit；build 或 emit 拋出的任何
        exception 只 warning，不 rollback durable abandoned state、不改變
        `scan_once()` 回傳結果、不 retry。
        """
        if self._notify_service is None:
            return
        try:
            payload = build_pending_review_payload(run)
            notification = build_pending_review_notification(payload)
            self._notify_service.emit(notification)
        except Exception as exc:
            _LOG.warning(
                "recovery scanner pending-review notification failed: run_id=%s: %s",
                run.run_id,
                exc,
            )
