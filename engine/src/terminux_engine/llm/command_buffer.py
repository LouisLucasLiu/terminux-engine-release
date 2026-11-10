# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""CommandBuffer。

累積 pre/post-exec 觸發的 command，按時間窗 + EDA acceleration 觸發 flush，
把整批 commands 交給 caller（通常是 AgentTaskQueue.submit）做 async Agent
triage。

Lifecycle / 行為：
- append(cmd)：加入 buffer；若是 EDA tool 立即嘗試 flush（acceleration），
  否則啟動 window_sec timer
- flush 後進入 dedup_sec 冷卻：期間自動 flush（含 EDA acceleration）會 skip
- shutdown 後 append/flush 為 no-op；殘餘 buffer 在 shutdown 時 force-flush
- 所有 callback（on_flush / ecc_snapshot_fn / recent_events_fn /
  is_eda_tool_fn）的例外都 catch + log，不擴散到 caller thread
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable
from uuid import uuid4

from terminux_engine.llm.agent_models import BufferedCommand, CommandBatch

_log = logging.getLogger(__name__)


class CommandBuffer:
    """執行緒安全的 command 累積器。"""

    def __init__(
        self,
        *,
        window_sec: float = 30.0,
        dedup_sec: float = 15.0,
        on_flush: Callable[[CommandBatch], None] | None = None,
        ecc_snapshot_fn: Callable[[], dict | None] | None = None,
        recent_events_fn: Callable[[], list[dict] | None] | None = None,
        is_eda_tool_fn: Callable[[str], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        if dedup_sec < 0:
            raise ValueError("dedup_sec must be >= 0")
        self._window_sec = float(window_sec)
        self._dedup_sec = float(dedup_sec)
        self._on_flush = on_flush
        self._ecc_snapshot_fn = ecc_snapshot_fn
        self._recent_events_fn = recent_events_fn
        self._is_eda_tool_fn = is_eda_tool_fn
        self._clock = clock or time.monotonic

        self._buffer: list[BufferedCommand] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last_flush_mono: float | None = None
        self._shutdown_flag = False

    # ── Public API ──────────────────────────────────────────────────────────

    def append(self, command: str, cwd: str | None = None) -> None:
        """加入一個 command；非阻塞，shutdown 後 silent no-op。"""
        is_eda = False
        if self._is_eda_tool_fn is not None:
            try:
                is_eda = bool(self._is_eda_tool_fn(command))
            except Exception as e:
                _log.warning("is_eda_tool_fn raised (%s); defaulting to False", e)
                is_eda = False

        with self._lock:
            if self._shutdown_flag:
                return
            self._buffer.append(
                BufferedCommand(
                    command=command,
                    timestamp=time.time(),
                    is_eda_tool=is_eda,
                    cwd=cwd,
                )
            )

        if is_eda:
            # acceleration：嘗試立即 flush（dedup 期間會回 None）
            if self._do_flush() is None:
                # 被 dedup 擋住或 buffer 已被別處清空 → 確保 timer 仍排程
                self._ensure_timer()
        else:
            self._ensure_timer()

    def flush(self) -> CommandBatch | None:
        """手動 flush。空 buffer / shutdown / dedup 期間皆回 None。"""
        return self._do_flush()

    def shutdown(self) -> None:
        """停止 timer，flush 殘餘 buffer。冪等。"""
        with self._lock:
            if self._shutdown_flag:
                return
            self._shutdown_flag = True
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
        # force flush 一次把殘餘 commands 也丟出去
        self._do_flush(force=True)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown_flag

    # ── Internal ────────────────────────────────────────────────────────────

    def _ensure_timer(self) -> None:
        """若無 pending timer，啟動一個 window_sec 後 fire 的 daemon Timer。"""
        with self._lock:
            if self._shutdown_flag:
                return
            if self._timer is not None and self._timer.is_alive():
                return
            t = threading.Timer(self._window_sec, self._on_timer_fire)
            t.daemon = True
            self._timer = t
        # start 在 lock 外（避免 Timer 內部呼叫 callback 時與 lock 衝突）
        t.start()

    def _on_timer_fire(self) -> None:
        self._do_flush()

    def _do_flush(self, *, force: bool = False) -> CommandBatch | None:
        """嘗試 flush。force=True 跳過 shutdown / dedup 檢查（shutdown 內部使用）。"""
        with self._lock:
            if self._shutdown_flag and not force:
                return None
            now = self._clock()
            if (
                not force
                and self._last_flush_mono is not None
                and (now - self._last_flush_mono) < self._dedup_sec
            ):
                return None
            if not self._buffer:
                return None
            cmds = list(self._buffer)
            self._buffer.clear()
            self._last_flush_mono = now
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        # 以下都在 lock 外執行（避免 callback 內又 append 造成 deadlock）
        ecc_snapshot = _safe_call(self._ecc_snapshot_fn, "ecc_snapshot_fn")
        recent_events = _safe_call(self._recent_events_fn, "recent_events_fn")

        batch = CommandBatch(
            batch_id=uuid4().hex,
            commands=cmds,
            ecc_snapshot=ecc_snapshot if isinstance(ecc_snapshot, dict) else None,
            recent_events=recent_events if isinstance(recent_events, list) else None,
            flushed_at=time.time(),
        )

        _log.info(
            "CommandBuffer flush: batch_id=%s cmd_count=%d force=%s",
            batch.batch_id, len(cmds), force,
        )

        if self._on_flush is not None:
            try:
                self._on_flush(batch)
            except Exception as e:
                _log.warning("on_flush callback raised: %s", e)

        return batch


def _safe_call(fn, name: str):
    """呼叫 0-arg callable；例外時 log + 回 None，不拋出。"""
    if fn is None:
        return None
    try:
        return fn()
    except Exception as e:
        _log.warning("%s raised: %s", name, e)
        return None
