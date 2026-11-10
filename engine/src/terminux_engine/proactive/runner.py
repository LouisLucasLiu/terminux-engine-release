# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Daemon-owned proactive runner（proactive intelligence activation, 2026-09-06）。

repo 裡沒有可重用的通用 periodic facility——`LogSweeper` 是專用的。因此這裡
新增一個**最小**的 daemon-owned runner，形狀刻意比照 `LogSweeper`：
stop event ＋ graceful join ＋ exception containment。

它刻意**不是** scheduler framework：
  - 只跑一個 coordinator，不接受 job 註冊、不排程多個任務
  - 單一 thread 依序呼叫 `run_once()`，因此 cycle **不會重疊**
  - 沒有 retry policy、沒有 backoff 曲線、沒有優先權
要擴張成通用 scheduler 必須另案設計，不從這裡長出來。

Runner 本身不認識 provider、budget 或 notification——那些全在 coordinator
裡；這一層只負責「每隔一段時間叫一次，並且能乾淨地停下來」。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: 預設 tick 間隔。刻意保守：proactive intelligence 是背景 read-only 分析，不需要低延遲。
DEFAULT_INTERVAL_SECONDS = 300.0

THREAD_NAME = "terminux-proactive"


class _Coordinator(Protocol):
    def run_once(self) -> dict[str, Any]: ...


class ProactiveRunner:
    """定期呼叫 `ProactiveCoordinator.run_once()` 的背景 thread。

    `start()` 冪等；`stop()` 會 set stop event 並 join，確保 daemon shutdown
    不會留下 thread。任何 cycle 內的例外都被吞掉並記 log——背景分析失敗
    絕不能拖垮 daemon。
    """

    def __init__(self, coordinator: _Coordinator,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._coordinator = coordinator
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        #: 只為了觀測；不參與任何控制流程。
        self.cycles_completed = 0
        self.cycles_failed = 0

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """啟動背景 thread（冪等）。daemon=True 避免阻擋 process exit。"""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=THREAD_NAME)
        self._thread.start()

    def signal_stop(self) -> None:
        """非阻塞發出停止信號。"""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """等待背景 thread 結束。"""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None
            else:
                logger.warning(
                    "proactive runner thread did not stop within %.1fs",
                    timeout if timeout is not None else -1.0,
                )

    @property
    def is_alive(self) -> bool:
        """背景 thread 是否仍在執行。"""
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float | None = 30.0) -> None:
        """停止背景 thread（blocking join）。可重複呼叫。"""
        self.signal_stop()
        self.join(timeout=timeout)

    # ── loop ─────────────────────────────────────────────────────────

    def run_once_now(self) -> dict[str, Any] | None:
        """同步跑一次；例外一律吞掉並記 log。供 loop 與測試共用。"""
        try:
            payload = self._coordinator.run_once()
            self.cycles_completed += 1
            return payload
        except Exception as exc:      # noqa: BLE001 - 背景分析不得拖垮 daemon
            self.cycles_failed += 1
            logger.warning("proactive cycle failed (non-fatal): %s", exc)
            return None

    def _loop(self) -> None:
        # 先等一個間隔再跑第一次：daemon 剛啟動時讓其他 subsystem 先就緒。
        while not self._stop_event.wait(self._interval):
            self.run_once_now()


__all__ = ["DEFAULT_INTERVAL_SECONDS", "THREAD_NAME", "ProactiveRunner"]
