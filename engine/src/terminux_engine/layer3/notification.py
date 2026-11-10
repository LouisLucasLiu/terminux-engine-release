# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from terminux_engine.ecc.models import ShellEvent
from terminux_engine.layer2.llm import (
    NotifyInlineAction,
    NotifyPanelAction,
    NotifyStatusbarAction,
    SuggestCommandAction,
    ExecCommandAction,
)

if TYPE_CHECKING:
    from terminux_engine.ipc.server import IPCServer

logger = logging.getLogger(__name__)


# ── Protocol ──────────────────────────────────────────────────────────────────

class NotifyApi(Protocol):
    """Plugin action.py 可見的通知介面型別宣告。"""
    def inline(self, content: str) -> None: ...
    def sidebar(self, title: str, content: str) -> None: ...


# ── Internal Data Structure ───────────────────────────────────────────────────

@dataclass
class _NotifyParams:
    """IPC plugin.notify params dict 的內部表示（以 asdict() 轉成 dict）。"""
    session_id:  str
    notify_type: str   # "notify_inline" | "notify_statusbar" | "notify_panel"
    title:       str   # notify_inline / notify_statusbar 時為空字串 ""
    content:     str
    detail:      str   # 目前一律為 ""（reserved）
    timestamp:   str   # ISO 8601、UTC，取自 event.timestamp


def build_notify_rpc_message(
    session_id: str,
    notify_type: str,
    title: str,
    content: str,
    detail: str = "",
    *,
    event: ShellEvent,
) -> dict[str, Any]:
    """建構符合 IPC plugin.notify 的完整 JSON-RPC notification payload。"""
    ts = event.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    timestamp_str = ts.astimezone(timezone.utc).isoformat()

    params = _NotifyParams(
        session_id=session_id,
        notify_type=notify_type,
        title=title,
        content=content,
        detail=detail,
        timestamp=timestamp_str,
    )
    return {
        "jsonrpc": "2.0",
        "method": "plugin.notify",
        "params": dataclasses.asdict(params),
    }


# ── NotifyApiImpl ─────────────────────────────────────────────────────────────

@dataclass
class NotifyApiImpl:
    """
    Plugin action.py 可見的 NotifyApi 具體實作。
    由 Notifier.create_notify_api() 產生，綁定 session_id 與 event。
    inline() 立即經 IPC 送出。
    """
    _notifier:   "Notifier"
    _session_id: str
    _event:      ShellEvent

    def inline(self, content: str) -> None:
        """
        Plugin action.py 呼叫此方法時即時送出 IPC。
        content 為空字串時記錄 warning 並返回。
        在 asyncio.to_thread 內執行，使用 asyncio.run() 橋接 async dispatch。
        """
        if not content:
            logger.warning(
                "NotifyApiImpl.inline: empty content, skipping"
            )
            return
        import asyncio
        action = NotifyInlineAction(content=content)
        asyncio.run(
            self._notifier.dispatch(
                actions=[action],
                session_id=self._session_id,
                event=self._event,
            )
        )

    def sidebar(self, title: str, content: str) -> None:
        """notify_panel 未實作，記錄 warning 後返回。"""
        logger.warning(
            "notify_panel not implemented (Option B), sidebar() call ignored"
        )


# ── Notifier ──────────────────────────────────────────────────────────────────

class Notifier:
    """
    Layer 3 通知分派器。
    將 Action 物件轉換為 IPC 通知並送達對應 session 的 C++ terminal。
    dispatch() 為 async；由 layer2.parser 以 await 呼叫。
    """

    def __init__(self, ipc_server: "IPCServer") -> None:
        self._ipc_server = ipc_server

    async def dispatch(
        self,
        actions: list,
        *,
        session_id: str,
        event: ShellEvent,
    ) -> None:
        """
        遍歷 actions，對支援的型別送出 IPC 通知。
        不拋出 exception；各 action 的失敗獨立處理。
        """
        for action in actions:
            try:
                if isinstance(action, NotifyInlineAction):
                    if not action.content:
                        logger.warning(
                            "dispatch: empty content in NotifyInlineAction, skipping"
                        )
                        continue
                    await self._send_ipc(
                        session_id=session_id,
                        notify_type="notify_inline",
                        title="",
                        content=action.content,
                        detail="",
                        event=event,
                    )
                elif isinstance(action, NotifyStatusbarAction):
                    if not action.content:
                        logger.warning(
                            "dispatch: empty content in NotifyStatusbarAction, skipping"
                        )
                        continue
                    await self._send_ipc(
                        session_id=session_id,
                        notify_type="notify_statusbar",
                        title="",
                        content=action.content,
                        detail="",
                        event=event,
                    )
                elif isinstance(action, NotifyPanelAction):
                    logger.warning(
                        "notify_panel not implemented (Option B), skipping"
                    )
                elif isinstance(action, (SuggestCommandAction, ExecCommandAction)):
                    logger.debug(
                        "action type %r is not handled by layer3.notification, skipping",
                        type(action).__name__,
                    )
                else:
                    logger.debug(
                        "action type %r is not handled by layer3.notification, skipping",
                        type(action).__name__,
                    )
            except Exception as e:
                logger.error(
                    "dispatch: error processing action %r: %s",
                    type(action).__name__, e, exc_info=True,
                )

    def create_notify_api(
        self, session_id: str, event: ShellEvent
    ) -> NotifyApiImpl:
        """回傳已綁定 session_id 與 event 的 NotifyApiImpl instance。"""
        return NotifyApiImpl(
            _notifier=self,
            _session_id=session_id,
            _event=event,
        )

    async def _send_ipc(
        self,
        session_id: str,
        notify_type: str,
        title: str,
        content: str,
        detail: str,
        event: ShellEvent,
    ) -> None:
        """
        建構 _NotifyParams，呼叫 ipc_server.send_notification。
        send_notification 回傳 False 時記錄 warning；exception 時記錄 error。
        """
        notification = build_notify_rpc_message(
            session_id=session_id,
            notify_type=notify_type,
            title=title,
            content=content,
            detail=detail,
            event=event,
        )

        try:
            result = await self._ipc_server.send_notification(
                session_id=session_id,
                notification=notification,
            )
            if not result:
                logger.warning(
                    "send_notification returned False for session_id=%s",
                    session_id,
                )
        except Exception as e:
            logger.error(
                "send_notification raised exception for session_id=%s: %s",
                session_id, e, exc_info=True,
            )
