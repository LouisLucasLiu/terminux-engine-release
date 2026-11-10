# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""唯一、thread-safe 的 canonical session context registry。

Process 內只允許存在一份這個 registry 實例（由 ``main.py`` bootstrap
建立，注入 ``IPCServer`` 與 lineage ``serve_forever``）。``IPCServer`` 的
``session.init``/``session.end`` 是唯一的 create/revoke 入口；lineage
JSON-RPC server 只透過 ``resolve()`` 這個 public read-only API 取得
context，不直接讀 ``IPCServer._sessions``，也不建立第二套 session model
。

Thread safety：本 registry 同時被 asyncio event loop（``IPCServer``）與
lineage 的 per-connection OS threads（``jsonrpc_server.py``）呼叫，所有
共享狀態操作都在單一 ``threading.Lock`` 底下完成，且鎖定期間不做任何
socket I/O、LLM 呼叫或慢速檔案操作。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from terminux_engine.ipc.request_context import (
    PROJECT_SOURCE_SESSION,
    PROJECT_SOURCE_UNSET,
    UNDECLARED_PROJECT_ID,
    CanonicalRequestContext,
)

# ── 穩定 reason code───────────────────────────────────────
REASON_PEER_IDENTITY_UNAVAILABLE = "PEER_IDENTITY_UNAVAILABLE"
REASON_MEMBER_ASSERTION_MISMATCH = "MEMBER_ASSERTION_MISMATCH"
REASON_INVALID_PROJECT_CONTEXT = "INVALID_PROJECT_CONTEXT"
REASON_INVALID_SESSION_CONTEXT = "INVALID_SESSION_CONTEXT"
REASON_SESSION_PEER_MISMATCH = "SESSION_PEER_MISMATCH"


class SessionRegistryError(RuntimeError):
    """所有 registry error 的共同基底；``reason`` 是穩定的對外字串。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PeerIdentityUnavailableError(SessionRegistryError):
    def __init__(self, message: str = "peer identity unavailable") -> None:
        super().__init__(REASON_PEER_IDENTITY_UNAVAILABLE, message)


class MemberAssertionMismatchError(SessionRegistryError):
    def __init__(self, message: str = "member assertion mismatch") -> None:
        super().__init__(REASON_MEMBER_ASSERTION_MISMATCH, message)


class InvalidProjectContextError(SessionRegistryError):
    def __init__(self, message: str = "invalid project context") -> None:
        super().__init__(REASON_INVALID_PROJECT_CONTEXT, message)


class InvalidSessionContextError(SessionRegistryError):
    """unknown／revoked session 與 wrong-peer session 共用同一個 error 類別。

    「wrong-peer 與 unknown session 對外不可區分」：兩種
    情境在 public 層必須回傳完全相同的 code/message，因此本類別同時代表
    這兩種內部原因（呼叫端可用 ``message`` 參數記錄不同 internal log，但
    ``reason`` 與 exception 類別本身一律相同）。
    """

    def __init__(self, message: str = "invalid session context") -> None:
        super().__init__(REASON_INVALID_SESSION_CONTEXT, message)


@dataclass
class _SessionRecord:
    context: CanonicalRequestContext
    peer_uid: int


class CanonicalSessionContextRegistry:
    """Process-wide 唯一的 canonical session authority。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionRecord] = {}
        self._revoke_listeners: list[Callable[[str, int], None]] = []

    def add_revoke_listener(self, listener: Callable[[str, int], None]) -> None:
        """登記 session 撤銷時的通知（例如清除該 session 的 Run 綁定）；重複登記只保留一份。

        listener 在鎖外呼叫，且其例外不影響撤銷本身。
        """
        with self._lock:
            if listener not in self._revoke_listeners:
                self._revoke_listeners.append(listener)

    # ── Create ───────────────────────────────────────────────────────────

    def create(
        self,
        *,
        peer_uid: int,
        member_id: str,
        project_id: str | None,
    ) -> CanonicalRequestContext:
        """建立一筆新 session。失敗（member/project 驗證失敗）不留下任何
        partial record。

        呼叫端負責先完成 peer credential 解析與 member assertion 比對
        （見 ``peer_identity.py``）；本方法只再做一次防禦性 non-empty 檢查，
        不重複做 peer UID 解析。

        ``project_id=None`` 代表 client **沒有宣告**
        project ⇒ context 的 ``project_id`` 為 ``UNDECLARED_PROJECT_ID``、
        ``project_source="unset"``。給了字串就是明確宣告（``session``），
        即使字串恰好是 "default"。空字串仍是錯誤（不是「未宣告」）。
        """
        if not member_id:
            raise MemberAssertionMismatchError("member_id must be a non-empty string")
        if project_id is None:
            effective_project = UNDECLARED_PROJECT_ID
            project_source = PROJECT_SOURCE_UNSET
        else:
            if not isinstance(project_id, str) or not project_id:
                raise InvalidProjectContextError("project_id must be a non-empty string")
            effective_project = project_id
            project_source = PROJECT_SOURCE_SESSION

        with self._lock:
            session_id = str(uuid.uuid4())
            while session_id in self._sessions:  # pragma: no cover - UUID4 碰撞防禦
                session_id = str(uuid.uuid4())
            context = CanonicalRequestContext(
                session_id=session_id,
                member_id=member_id,
                project_id=effective_project,
                project_source=project_source,
            )
            self._sessions[session_id] = _SessionRecord(context=context, peer_uid=peer_uid)
        return context

    # ── Resolve ──────────────────────────────────────────────────────────

    def resolve(self, *, session_id: str, peer_uid: int) -> CanonicalRequestContext:
        """以 session_id + 目前連線 peer UID 解析 immutable context。

        unknown session、revoked session、wrong-peer 一律拋出同一個
        ``InvalidSessionContextError``（見上方類別 docstring）。
        """
        if not session_id:
            raise InvalidSessionContextError("unknown session")
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                raise InvalidSessionContextError("unknown session")
            if record.peer_uid != peer_uid:
                raise InvalidSessionContextError("unknown session")
            return record.context

    # ── Revoke / lifecycle ───────────────────────────────────────────────

    def revoke(self, *, session_id: str, peer_uid: int) -> None:
        """撤銷一筆 session。只能撤銷同 peer 建立的 session；

        unknown session 或 peer 不符時 no-op（不拋出——``session.end``／
        disconnect cleanup 本身允許重複呼叫或 race）。
        """
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return
            if record.peer_uid != peer_uid:
                return
            del self._sessions[session_id]
            listeners = list(self._revoke_listeners)
        for listener in listeners:
            try:
                listener(session_id, peer_uid)
            except Exception:  # noqa: BLE001 - 撤銷通知失敗不能讓 session 殘留
                continue

    def clear(self) -> None:
        """daemon shutdown 時清空全部 session（測試也用於重設狀態）。"""
        with self._lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()
            listeners = list(self._revoke_listeners)
        for session_id, record in sessions:
            for listener in listeners:
                try:
                    listener(session_id, record.peer_uid)
                except Exception:  # noqa: BLE001
                    continue

    # ── Introspection（測試／診斷用，非 public consumer API）────────────────

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
