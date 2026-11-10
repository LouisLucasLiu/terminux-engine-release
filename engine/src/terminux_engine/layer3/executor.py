# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from terminux_engine.core.plugin_manager import (
    PERMISSION_ORDER,
    PluginPermission,
)
from terminux_engine.ecc.models import ShellEvent
from terminux_engine.layer2.llm import (
    ExecCommandAction,
    NotifyInlineAction,
    SuggestCommandAction,
)
from terminux_engine.layer3.notification import build_notify_rpc_message

if TYPE_CHECKING:
    from terminux_engine.ipc.server import IPCServer

logger = logging.getLogger(__name__)


# ── Protocol ──────────────────────────────────────────────────────────────────

class ExecutorApi(Protocol):
    """Plugin action.py 可見的命令執行介面型別宣告。"""
    def suggest(self, command: str, reason: str) -> None: ...
    def exec(self, command: str, reason: str) -> None: ...


# ── ExecutorApiImpl ───────────────────────────────────────────────────────────

@dataclass
class ExecutorApiImpl:
    """
    Plugin action.py 可見的 ExecutorApi 具體實作。
    由 Executor.create_executor_api() 產生，綁定 session_id、event、plugin_id。
    suggest/exec 立即經 IPC 送出。
    在 asyncio.to_thread 内執行，使用 asyncio.run() 橋接 async dispatch。
    """
    _executor:   "Executor"
    _session_id: str
    _event:      ShellEvent
    _plugin_id:  str

    def suggest(self, command: str, reason: str) -> None:
        """
        送出 suggest_command 通知。
        授權檢查由 dispatch 內部進行。
        在 asyncio.to_thread 内執行，使用 asyncio.run() 橋接 async dispatch。
        """
        action = SuggestCommandAction(command=command, reason=reason)
        asyncio.run(
            self._executor.dispatch(
                actions=[action],
                session_id=self._session_id,
                event=self._event,
                plugin_id=self._plugin_id,
            )
        )

    def exec(self, command: str, reason: str) -> None:
        """
        送出 exec_command 通知。
        授權檢查由 dispatch 內部進行。
        在 asyncio.to_thread 内執行，使用 asyncio.run() 橋接 async dispatch。
        """
        action = ExecCommandAction(command=command, reason=reason)
        asyncio.run(
            self._executor.dispatch(
                actions=[action],
                session_id=self._session_id,
                event=self._event,
                plugin_id=self._plugin_id,
            )
        )


# ── Executor ──────────────────────────────────────────────────────────────────

class Executor:
    """
    Layer 3 命令執行分派器。
    驗證 Plugin 授權並將 SuggestCommandAction / ExecCommandAction 透過 IPC 送出。
    dispatch() 為 async；由 layer2.parser 以 await 呼叫。
    """

    def __init__(
        self,
        ipc_server:     "IPCServer",
        plugin_manager: Any,
    ) -> None:
        self._ipc_server     = ipc_server
        self._plugin_manager = plugin_manager

    async def dispatch(
        self,
        actions: list,
        *,
        session_id: str,
        event:      ShellEvent,
        plugin_id:  str,
    ) -> None:
        """
        取得 Plugin 授權等級，遍歷 actions，對通過授權的型別送出 IPC。
        不拋出 exception；各 action 的失敗獨立處理。
        """
        # Step 1: 取得 permission
        permission = PluginPermission.READ_ONLY
        try:
            permission = self._plugin_manager.get_plugin_permission(plugin_id)
        except Exception as e:
            logger.error(
                "get_plugin_permission failed for plugin %s: %s",
                plugin_id, e,
            )
            # 視為 READ_ONLY

        # Step 2: 逐一處理 actions
        for action in actions:
            try:
                if isinstance(action, SuggestCommandAction):
                    if not self._check_permission(
                        permission, PluginPermission.SUGGEST
                    ):
                        logger.warning(
                            "dispatch: plugin %s (permission=%s) lacks "
                            "SUGGEST permission, skipping suggest_command",
                            plugin_id, permission.value,
                        )
                        continue
                    if not action.command:
                        logger.warning(
                            "dispatch: empty command in SuggestCommandAction, skipping"
                        )
                        continue
                    if not action.reason:
                        logger.warning(
                            "dispatch: empty reason in SuggestCommandAction, skipping"
                        )
                        continue
                    await self._send_ipc(
                        session_id=session_id,
                        notify_type="suggest_command",
                        title=action.reason,
                        content=action.command,
                        event=event,
                    )

                elif isinstance(action, ExecCommandAction):
                    if not self._check_permission(
                        permission, PluginPermission.AUTO_EXEC
                    ):
                        logger.warning(
                            "dispatch: plugin %s (permission=%s) lacks "
                            "AUTO_EXEC permission, skipping exec_command",
                            plugin_id, permission.value,
                        )
                        continue
                    if not action.command:
                        logger.warning(
                            "dispatch: empty command in ExecCommandAction, skipping"
                        )
                        continue
                    if not action.reason:
                        logger.warning(
                            "dispatch: empty reason in ExecCommandAction, skipping"
                        )
                        continue
                    await self._send_ipc(
                        session_id=session_id,
                        notify_type="exec_command",
                        title=action.reason,
                        content=action.command,
                        event=event,
                    )

                else:
                    logger.debug(
                        "action type %r is not handled by layer3.executor, skipping",
                        type(action).__name__,
                    )

            except Exception as e:
                logger.error(
                    "dispatch: error processing action %r: %s",
                    type(action).__name__, e, exc_info=True,
                )

    def create_executor_api(
        self,
        session_id: str,
        event:      ShellEvent,
        plugin_id:  str,
    ) -> ExecutorApiImpl:
        """回傳已綁定 session_id、event、plugin_id 的 ExecutorApiImpl instance。"""
        return ExecutorApiImpl(
            _executor=self,
            _session_id=session_id,
            _event=event,
            _plugin_id=plugin_id,
        )

    def _check_permission(
        self,
        permission: PluginPermission,
        required:   PluginPermission,
    ) -> bool:
        """permission 的 index >= required 的 index 時回傳 True。"""
        return PERMISSION_ORDER[permission] >= PERMISSION_ORDER[required]

    async def _send_ipc(
        self,
        session_id:  str,
        notify_type: str,
        title:       str,
        content:     str,
        event:       ShellEvent,
    ) -> None:
        """
        透過 build_notify_rpc_message 建構完整 JSON-RPC notification，呼叫 ipc_server.send_notification。
        回傳 False 時記錄 warning；exception 時記錄 error。
        """
        notification = build_notify_rpc_message(
            session_id=session_id,
            notify_type=notify_type,
            title=title,
            content=content,
            detail="",
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
