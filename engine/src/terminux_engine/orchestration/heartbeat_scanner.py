# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Daemon 側 child heartbeat 監看器。"""

from __future__ import annotations

import logging
import os
import threading
import time

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


class HeartbeatScanner:
    """週期偵測失聯或僵屍的 running child。"""

    def __init__(
        self,
        ledger: OrchestrationLedger,
        orchestration_service: OrchestrationService,
        *,
        scan_interval_seconds: float = 15.0,
        stale_threshold_seconds: float = 30.0,
        notify_service: NotifyService | None = None,
    ) -> None:
        self._ledger = ledger
        self._service = orchestration_service
        self._scan_interval_seconds = scan_interval_seconds
        self._stale_threshold_seconds = stale_threshold_seconds
        self._notify_service = notify_service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """啟動 scanner daemon thread。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="orchestration-heartbeat-scanner",
        )
        self._thread.start()
        _LOG.info("Orchestration heartbeat scanner started")

    def stop(self, timeout: float = 15.0) -> None:
        """停止 scanner，join 超時只記錄錯誤。"""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                _LOG.error("Orchestration heartbeat scanner stop timed out")

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._scan_once()
            self._stop_event.wait(self._scan_interval_seconds)

    def _scan_once(self) -> None:
        threshold = time.time() - self._stale_threshold_seconds
        for run in self._ledger.list_stale_running_runs(threshold):
            if run.child_pid is None:
                continue
            try:
                os.kill(run.child_pid, 0)
            except ProcessLookupError:
                self._mark_abandoned(
                    run.run_id,
                    child_pid=run.child_pid,
                    heartbeat_not_after=None,
                    terminal_reason="child_died",
                )
            except PermissionError:
                _LOG.warning("Cannot inspect orchestration child: pid=%s", run.child_pid)
            else:
                self._mark_abandoned(
                    run.run_id,
                    child_pid=run.child_pid,
                    heartbeat_not_after=threshold,
                    terminal_reason="child_zombie",
                )

    def _mark_abandoned(
        self,
        run_id: str,
        *,
        child_pid: int,
        heartbeat_not_after: float | None,
        terminal_reason: str,
    ) -> None:
        """只有 conditional ownership winner 可清理 process group。"""

        result = self._ledger.mark_abandoned_if_running_and_no_cancel(
            run_id,
            expected_child_pid=child_pid,
            heartbeat_not_after=heartbeat_not_after,
            terminal_reason=terminal_reason,  # type: ignore[arg-type]
        )
        if not result.applied:
            self._ledger.get_run(run_id)
            return
        if heartbeat_not_after is None:
            self._service.unregister_run(run_id, expected_pid=child_pid)
            self._notify_pending_review(result.run)
            return
        signal_result = signal_owned_process_group(child_pid)
        if signal_result is ProcessGroupSignalResult.SENT:
            self._service.wait_for_registered_child(run_id, expected_pid=child_pid)
        elif signal_result is ProcessGroupSignalResult.NOT_OWNER:
            _LOG.error(
                "Abandoned zombie has non-owned process group: run_id=%s pid=%s",
                run_id,
                child_pid,
            )
        self._service.unregister_run(run_id, expected_pid=child_pid)
        self._notify_pending_review(result.run)

    def _notify_pending_review(self, run: RunRecord) -> None:
        """conditional winner 已 commit 後 best-effort 投遞 pending-review 通知。

        `notify_service=None` 時完全不 build／emit；build 或 emit 拋出的任何
        exception 只 warning，不 rollback durable abandoned state、不終止
        scanner thread、不 retry。
        """
        if self._notify_service is None:
            return
        try:
            payload = build_pending_review_payload(run)
            notification = build_pending_review_notification(payload)
            self._notify_service.emit(notification)
        except Exception as exc:
            _LOG.warning(
                "heartbeat scanner pending-review notification failed: run_id=%s: %s",
                run.run_id,
                exc,
            )
