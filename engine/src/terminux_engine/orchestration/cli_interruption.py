# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""前景 CLI 執行的 SIGINT／SIGTERM 中斷控制。

signal handler 只記錄 signal 與當時所在的階段，不在任意位置拋例外；終止與確認由
CLI 專用的 executor（process group）與 adapter 的 `cleanup()`（container）負責。
例外只有一種：工具已結束、正在收尾（DQE dispatch、receipt 寫入）時，第二次
signal 或總期限到時，以 `FinalizationAborted` 中止收尾。

期限以第一次 signal 起算，使用 monotonic clock：

- graceful：對 process group 送 SIGTERM 後等待的時間；
- process group：確認 group 已不存在的時間；
- cleanup：確認 container 已不存在的時間。

第二次 signal 跳過剩餘的 graceful 時間（forced），但不延長總期限；之後的
signal 只記錄。
"""

from __future__ import annotations

import os
import signal
import sys
import time
from types import FrameType
from typing import Any, Callable, NoReturn

PHASE_PREPARING = "preparing"
PHASE_EXECUTING = "executing"
PHASE_INTERRUPTING = "interrupting"
PHASE_FINALIZING = "finalizing"
PHASE_PERSISTED = "persisted"

DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_PROCESS_GROUP_SECONDS = 5.0
DEFAULT_CLEANUP_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)

Timer = Callable[[float, Callable[[], None]], Callable[[], None]]


class FinalizationAborted(BaseException):
    """收尾期間的第二次 signal 或總期限到時，中止收尾。

    繼承 `BaseException`，不會被一般的 `except Exception` 吞掉。
    """


def _alarm_timer(seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
    """以 SIGALRM 在主 thread 觸發 `callback`；回傳取消函式。"""
    previous = signal.signal(signal.SIGALRM, lambda _s, _f: callback())
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))

    def cancel() -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return cancel


class CliInterruptController:
    """記錄 signal、維護階段與期限；同時作為 container 清理的 `CleanupBudget`。"""

    def __init__(
        self, *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        process_group_seconds: float = DEFAULT_PROCESS_GROUP_SECONDS,
        cleanup_seconds: float = DEFAULT_CLEANUP_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timer: Timer | None = None,
    ) -> None:
        self.grace_seconds = float(grace_seconds)
        self.process_group_seconds = float(process_group_seconds)
        self.cleanup_seconds = float(cleanup_seconds)
        self._poll_interval = float(poll_interval)
        self._clock = clock
        self._timer = timer or _alarm_timer
        self._phase = PHASE_PREPARING
        self._signal: int | None = None
        self._count = 0
        self._first_at: float | None = None
        self._phase_at_signal: str | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._previous: dict[int, Any] = {}
        self.process_exit_code: int | None = None

    # ── signal ───────────────────────────────────────────────────────

    @property
    def total_seconds(self) -> float:
        return self.grace_seconds + self.process_group_seconds + self.cleanup_seconds

    def install(self) -> None:
        for signo in HANDLED_SIGNALS:
            self._previous[signo] = signal.signal(signo, self.handle_signal)

    def restore(self) -> None:
        self._disarm()
        for signo, previous in self._previous.items():
            signal.signal(signo, previous)
        self._previous.clear()

    def handle_signal(self, signo: int, frame: FrameType | None = None) -> None:
        del frame
        self._count += 1
        if self._count == 1:
            self._signal = int(signo)
            self._first_at = self._clock()
            self._phase_at_signal = self._phase
            if self._phase == PHASE_FINALIZING:
                self._arm()
            return
        if self._phase == PHASE_FINALIZING:
            raise FinalizationAborted(f"signal {signo} received during finalization")

    @property
    def interrupted(self) -> bool:
        return self._signal is not None

    @property
    def signal_number(self) -> int | None:
        return self._signal

    @property
    def signal_count(self) -> int:
        return self._count

    @property
    def phase_at_signal(self) -> str | None:
        return self._phase_at_signal

    def forced(self) -> bool:
        return self._count >= 2

    # ── 階段 ─────────────────────────────────────────────────────────

    @property
    def phase(self) -> str:
        return self._phase

    def enter_phase(self, phase: str) -> None:
        self._phase = phase
        if phase == PHASE_FINALIZING and self.interrupted:
            self._arm()
        elif phase != PHASE_FINALIZING:
            self._disarm()

    def note_process_exit(self, exit_code: int) -> None:
        """工具已自行結束：記錄 exit code，進入收尾階段。"""
        self.process_exit_code = exit_code
        self.enter_phase(PHASE_FINALIZING)

    def on_finalization_deadline(self) -> None:
        if self._phase == PHASE_FINALIZING:
            raise FinalizationAborted("finalization deadline exceeded after a signal")

    def _arm(self) -> None:
        if self._cancel_timer is not None:
            return
        deadline = self.deadline()
        if deadline is None:
            return
        self._cancel_timer = self._timer(max(deadline - self._clock(), 0.0),
                                         self.on_finalization_deadline)

    def _disarm(self) -> None:
        if self._cancel_timer is not None:
            cancel, self._cancel_timer = self._cancel_timer, None
            cancel()

    # ── 期限 ─────────────────────────────────────────────────────────

    def deadline(self) -> float | None:
        if self._first_at is None:
            return None
        return self._first_at + self.total_seconds

    def grace_deadline(self) -> float | None:
        if self._first_at is None:
            return None
        return self._first_at + self.grace_seconds

    def process_group_deadline(self) -> float | None:
        if self._first_at is None:
            return None
        return self._first_at + self.grace_seconds + self.process_group_seconds

    # ── CleanupBudget ────────────────────────────────────────────────

    def cleanup_deadline(self, started_at: float) -> float:
        own = started_at + self.cleanup_seconds
        overall = self.deadline()
        return own if overall is None else min(own, overall)

    def stop_grace_seconds(self) -> float:
        grace = self.grace_deadline()
        if grace is None or self.forced():
            return 0.0
        return max(grace - self._clock(), 0.0)

    def poll_interval(self) -> float:
        return self._poll_interval


def exit_with_signal(signo: int) -> NoReturn:
    """清理完成後以原 signal 結束 process。

    shell 因此看到 128＋signal（130／143），Python `subprocess` 看到負的 signal
    編號（-2／-15）。結束前先 flush stdout／stderr。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    signal.signal(signo, signal.SIG_DFL)
    os.kill(os.getpid(), signo)
    # signal 被 mask 時仍以相同的 shell 語意結束。
    os._exit(128 + signo)


__all__ = [
    "DEFAULT_CLEANUP_SECONDS", "DEFAULT_GRACE_SECONDS", "DEFAULT_PROCESS_GROUP_SECONDS",
    "HANDLED_SIGNALS", "PHASE_EXECUTING", "PHASE_FINALIZING", "PHASE_INTERRUPTING",
    "PHASE_PERSISTED", "PHASE_PREPARING", "CliInterruptController", "FinalizationAborted",
    "exit_with_signal",
]
