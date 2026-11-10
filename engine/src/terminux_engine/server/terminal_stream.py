# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Multi-stream 基礎設施：TerminalStream + StreamManager。

每個 terminal 連接有獨立的 ECCAccumulator + ConversationBuffer（per-stream），
但共享 EventStore / LLMRouter 等 shared layer。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from terminux_engine.ecc.accumulator import ECCAccumulator
    from terminux_engine.ecc.playbook import Playbook
    from terminux_engine.store.event_store import EventStore

logger = logging.getLogger(__name__)

# idle 判定閾值（秒）
_IDLE_THRESHOLD_SEC: float = 3600.0


# ── TerminalStream ────────────────────────────────────────────────────────────

class TerminalStream:
    """一個 terminal 連接的完整狀態。"""

    def __init__(
        self,
        terminal_id: str,
        accumulator: Any,  # ECCAccumulator
        conversation: Any,  # ConversationBuffer
    ) -> None:
        self.terminal_id = terminal_id
        self.accumulator = accumulator
        self.conversation = conversation
        self.lock = threading.Lock()
        self.last_activity: float = time.time()
        self.registered_at: float = time.time()
        self.period_start: float = time.time()

    @property
    def is_idle(self) -> bool:
        """距離上次 activity 是否超過 1 小時。"""
        return (time.time() - self.last_activity) > _IDLE_THRESHOLD_SEC

    def update_activity(self) -> None:
        """更新 last_activity 時間戳。pre_exec 和 post_exec 都要呼叫。"""
        self.last_activity = time.time()

    def reset_period(self) -> None:
        """開新工作時段。應由 jsonrpc_server gap 偵測後在 stream.lock 內呼叫。

        與 update_activity() 對稱：無參，內部 time.time()。
        只更新 period_start，不動 last_activity。
        """
        self.period_start = time.time()

    def to_summary_dict(self) -> dict:
        """產生此 stream 的狀態摘要。

        用於 prompt 的 [Other Active Terminals] 段落。
        使用 getattr 安全存取 accumulator 屬性，避免因版本差異報錯。
        """
        acc = self.accumulator
        ecc = getattr(acc, "get_current", lambda: None)()

        stage = None
        block_name = getattr(acc, "current_block_name", None)
        corner = None
        last_gate_status = None

        if ecc is not None:
            stage = getattr(ecc, "current_stage_id", None)
            # 從 corner_coverage 取最近 corner 狀態
            cc = getattr(ecc, "corner_coverage", None)
            if cc is not None:
                passed = getattr(cc, "completed_pass", [])
                failed = getattr(cc, "completed_fail", [])
                all_done = list(passed) + list(failed)
                if all_done:
                    corner = all_done[-1]
                    last_gate_status = "pass" if corner in passed else "fail"

        return {
            "terminal_id": self.terminal_id,
            "stage": stage,
            "corner": corner,
            "block_name": block_name,
            "last_gate_status": last_gate_status,
            "last_activity": self.last_activity,
            "is_idle": self.is_idle,
        }


# ── StreamManager ─────────────────────────────────────────────────────────────

class StreamManager:
    """管理多個 TerminalStream 的生命週期。

    Per-stream 元件（每個 terminal 獨立）：ECCAccumulator, ConversationBuffer。
    Shared 元件（所有 terminal 共用，由外部注入）：EventStore, PlaybookSpec。
    """

    def __init__(
        self,
        playbook: Any,           # Playbook | None
        event_store: Any = None,  # EventStore | None
        user_id: str = "",
        session_id: str = "",
        project_id: str | None = None,
        compactor: Any = None,   # ConversationCompactor | None
        flusher: Any = None,     # MemoryFlusher | None
    ) -> None:
        self._streams: dict[str, TerminalStream] = {}
        self._streams_lock = threading.Lock()
        self._playbook = playbook
        self._event_store = event_store
        self._user_id = user_id
        self._session_id = session_id
        self._project_id = project_id
        self._compactor = compactor
        self._flusher = flusher

    def register_terminal(self, terminal_id: str) -> TerminalStream:
        """註冊一個 terminal。若已存在，回傳既有 stream（重連場景）。"""
        with self._streams_lock:
            if terminal_id in self._streams:
                return self._streams[terminal_id]

            accumulator = self._create_accumulator(terminal_id)
            conversation = self._create_conversation_buffer(terminal_id)

            stream = TerminalStream(
                terminal_id=terminal_id,
                accumulator=accumulator,
                conversation=conversation,
            )
            self._streams[terminal_id] = stream
            logger.info("Registered terminal stream: %s", terminal_id)
            return stream

    def unregister_terminal(self, terminal_id: str) -> None:
        """移除 terminal。Terminal 斷開時呼叫。不存在時靜默。"""
        with self._streams_lock:
            removed = self._streams.pop(terminal_id, None)
        if removed is not None:
            logger.info("Unregistered terminal stream: %s", terminal_id)

    def get_stream(self, terminal_id: str) -> TerminalStream | None:
        """取得指定 terminal 的 stream。不存在回 None。"""
        with self._streams_lock:
            return self._streams.get(terminal_id)

    def get_all_streams(self) -> dict[str, TerminalStream]:
        """取得所有 stream 的 shallow copy。"""
        with self._streams_lock:
            return dict(self._streams)

    def get_other_streams_summary(self, exclude_terminal_id: str) -> list[dict]:
        """取得除了指定 terminal 以外的所有 stream 的狀態摘要。"""
        summaries = []
        with self._streams_lock:
            for tid, stream in self._streams.items():
                if tid == exclude_terminal_id:
                    continue
                summaries.append(stream.to_summary_dict())
        return summaries

    @property
    def terminal_count(self) -> int:
        with self._streams_lock:
            return len(self._streams)

    @property
    def terminal_ids(self) -> list[str]:
        with self._streams_lock:
            return list(self._streams.keys())

    def _create_accumulator(self, terminal_id: str) -> Any:
        """建立新的 ECCAccumulator instance。所有 stream 共用同一 event_store。"""
        from terminux_engine.ecc.accumulator import ECCAccumulator

        return ECCAccumulator(
            playbook=self._playbook,
            store=None,           # per-stream 不做 ECCStore 持久化
            session_id=None,      # 新 accumulator 會自動建立新 session
            event_store=self._event_store,
            user_id=self._user_id,
            project_id=self._project_id,
        )

    def _create_conversation_buffer(self, terminal_id: str = "default") -> Any:
        """建立新的 ConversationBuffer instance。

        若有 compactor/flusher，傳入以啟用 compaction 機制。
        """
        from terminux_engine.ecc.ai_companion import ConversationBuffer

        return ConversationBuffer(
            compactor=self._compactor,
            flusher=self._flusher,
            terminal_id=terminal_id,
            session_id=self._session_id,
            user_id=self._user_id,
        )
