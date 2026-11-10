# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Protocol

from terminux_engine.core.ecc_manager import EccManager
from terminux_engine.ecc.models import ShellEvent
from terminux_engine.layer2.context import EccQueryApi, ReadyPlugin
from terminux_engine.layer2.llm import (
    ExecCommandAction,
    LlmActionResult,
    NotifyInlineAction,
    NotifyPanelAction,
    NotifyStatusbarAction,
    SuggestCommandAction,
)

logger = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────────────

class PluginCallError(Exception):
    """plugin_manager.run_perception_py / run_action_py 呼叫失敗時拋出。"""
    pass


# ── LLM Query Result ───────────────────────────────────────────────────────────

@dataclass
class LlmQueryResult:
    content: str

    @property
    def detail(self) -> str:
        """相容 Plugin 範例程式碼中 diagnosis.detail 的存取方式，回傳 content。"""
        return self.content


# ── Protocol Definitions ──────────────────────────────────────────────────────

class LlmApi(Protocol):
    """Plugin action.py 可見的 LLM 呼叫介面型別宣告。"""
    def query(self, prompt: str, data: dict) -> LlmQueryResult: ...


class NotifyApi(Protocol):
    """Layer 3 Notification API，供 Plugin action.py 使用。"""
    def inline(self, content: str) -> None: ...
    def statusbar(self, content: str) -> None: ...
    def sidebar(self, title: str, content: str) -> None: ...


class ExecutorApi(Protocol):
    """Layer 3 Executor API，供 Plugin action.py 使用。"""
    def suggest(self, command: str, reason: str = "") -> None: ...
    def execute(self, command: str, reason: str = "") -> None: ...


class Notifier(Protocol):
    """Layer 3 Notifier，由 layer2.parser 呼叫。"""
    def dispatch(
        self, actions: list, *, session_id: str, event: ShellEvent
    ) -> None: ...

    def create_notify_api(
        self, session_id: str, event: ShellEvent
    ) -> NotifyApi: ...


class Executor(Protocol):
    """Layer 3 Executor，由 layer2.parser 呼叫。"""
    def dispatch(
        self, actions: list, *, session_id: str, event: ShellEvent, plugin_id: str
    ) -> None: ...

    def create_executor_api(
        self, session_id: str, event: ShellEvent, plugin_id: str
    ) -> ExecutorApi: ...


# ── Context Data Structures ───────────────────────────────────────────────────

@dataclass
class PerceptionContext:
    """perception.py 的 evaluate(event_dict, context) 收到的 context 物件。"""
    query_result: list[dict]
    ecc:          EccQueryApi


@dataclass
class ActionContext:
    """action.py 的 execute(event_dict, context) 收到的 context 物件。"""
    query_result: list[dict]
    ecc:          EccQueryApi
    llm:          LlmApi
    notify:       NotifyApi
    executor:     ExecutorApi


# ── LlmApiImpl ────────────────────────────────────────────────────────────────

class LlmApiImpl:
    """
    Plugin action.py 可見的 LLM 呼叫介面實作。
    對應 ActionContext.llm（型別：LlmApi Protocol）。
    query() 為同步方法，在 asyncio.to_thread 執行環境中使用 asyncio.run()。
    """

    def __init__(self, llm_client: Any) -> None:
        self._llm_client = llm_client

    def query(self, prompt: str, data: dict) -> LlmQueryResult:
        """
        同步介面（action.py 為同步函式）。
        使用 asyncio.run() 呼叫 llm_client.complete()（已在 to_thread 中執行，
        此時無 running event loop）。
        任何 exception 均捕獲，失敗時回傳 LlmQueryResult(content="")。
        """
        system = "You are a helpful assistant for IC design engineers."
        user = (
            f"{prompt}\n\nData:\n"
            f"{json.dumps(data, ensure_ascii=False, default=str)}"
        )
        try:
            text = asyncio.run(
                self._llm_client.complete(system=system, user=user)
            )
        except RuntimeError as e:
            logger.error(
                "asyncio.run() failed in LlmApiImpl.query "
                "(event loop conflict?): %s", e,
            )
            return LlmQueryResult(content="")
        except Exception as e:
            logger.warning("LlmApiImpl.query failed: %s", e)
            return LlmQueryResult(content="")
        return LlmQueryResult(content=text)


# ── Helper Functions ──────────────────────────────────────────────────────────

def _to_event_dict(event: ShellEvent) -> dict:
    """把 ShellEvent 轉成 dict（給 Plugin 使用）。
    以 dataclasses.asdict() 轉換，timestamp 轉成 ISO 8601、CommandType 取 .value。
    """
    d = dataclasses.asdict(event)
    d["timestamp"] = event.timestamp.isoformat()
    d["command_type"] = event.command_type.value
    return d


# ── ActionRouter ──────────────────────────────────────────────────────────────

class ActionRouter:
    """layer2.parser 唯一公開的類別。消費 ReadyPlugin 串流，執行評估與 action。"""

    def __init__(
        self,
        llm_client:     Any,
        plugin_manager: Any,
        notifier:       Any,
        executor:       Any,
        ecc_manager:    EccManager,
    ) -> None:
        self._llm_client     = llm_client
        self._plugin_manager = plugin_manager
        self._notifier       = notifier
        self._executor       = executor
        self._ecc_manager    = ecc_manager

    async def process(self, ready_stream: AsyncIterator[ReadyPlugin]) -> None:
        """
        逐一消費 ready_stream，每個 ReadyPlugin 以 asyncio.create_task 並行執行。
        process() 結束前 await 所有已啟動的 task（確保清理）。
        不拋出 exception。
        """
        tasks: list[asyncio.Task] = []
        async for ready in ready_stream:
            task = asyncio.create_task(self._safe_run_plugin(ready))
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)

    async def _safe_run_plugin(self, ready: ReadyPlugin) -> None:
        """包裝 _run_plugin，捕獲所有 exception，記錄 error log，不 re-raise。"""
        try:
            await self._run_plugin(ready)
        except Exception as e:
            logger.error(
                "Plugin %s processing failed: %s",
                ready.plugin_id, e, exc_info=True,
            )

    async def _run_plugin(self, ready: ReadyPlugin) -> None:
        """
        單一 ReadyPlugin 的處理流程。
        步驟：(1) ai_condition 評估 → (2) advanced_perception 評估 → (3) action 執行。
        """
        # 步驟 1：評估 ai_condition
        ai_condition: str | None = None
        try:
            ai_condition = self._plugin_manager.get_plugin_ai_condition(
                ready.plugin_id
            )
        except Exception as e:
            logger.warning(
                "get_plugin_ai_condition failed for plugin %s: %s",
                ready.plugin_id, e,
            )
            # 視為 None（通過）

        if ai_condition is not None:
            passed = await self._llm_client.evaluate_condition(
                condition=ai_condition,
                event=ready.event,
                query_result=ready.query_result,
            )
            if not passed:
                logger.debug("ai_condition not met: %s", ready.plugin_id)
                return

        # 步驟 2：評估 advanced_perception
        advanced_perception: bool = False
        try:
            advanced_perception = (
                self._plugin_manager.get_plugin_advanced_perception(
                    ready.plugin_id
                )
            )
        except Exception as e:
            logger.warning(
                "get_plugin_advanced_perception failed for plugin %s: %s",
                ready.plugin_id, e,
            )
            # 視為 False

        if advanced_perception:
            perception_ctx = PerceptionContext(
                query_result=ready.query_result,
                ecc=EccQueryApi(self._ecc_manager, ready.event),
            )
            try:
                result = await asyncio.to_thread(
                    self._plugin_manager.run_perception_py,
                    ready.plugin_id,
                    _to_event_dict(ready.event),
                    perception_ctx,
                )
            except PluginCallError as e:
                logger.warning(
                    "run_perception_py PluginCallError for plugin %s: %s",
                    ready.plugin_id, e,
                )
                return

            if not result:
                logger.debug(
                    "advanced_perception rejected: %s", ready.plugin_id
                )
                return

        # 步驟 3：執行 action
        if ready.has_action_py:
            # 分支 A：action.py
            action_ctx = ActionContext(
                query_result=ready.query_result,
                ecc=EccQueryApi(self._ecc_manager, ready.event),
                llm=LlmApiImpl(self._llm_client),
                notify=self._notifier.create_notify_api(
                    ready.event.session_id, ready.event
                ),
                executor=self._executor.create_executor_api(
                    ready.event.session_id, ready.event, ready.plugin_id
                ),
            )
            try:
                await asyncio.to_thread(
                    self._plugin_manager.run_action_py,
                    ready.plugin_id,
                    _to_event_dict(ready.event),
                    action_ctx,
                )
            except PluginCallError as e:
                logger.warning(
                    "run_action_py PluginCallError for plugin %s: %s",
                    ready.plugin_id, e,
                )
                return

            logger.info("action.py executed: %s", ready.plugin_id)

        else:
            # 分支 B：ai_action
            ai_action: str | None = None
            try:
                ai_action = self._plugin_manager.get_plugin_ai_action(
                    ready.plugin_id
                )
            except Exception as e:
                logger.warning(
                    "get_plugin_ai_action failed for plugin %s: %s",
                    ready.plugin_id, e,
                )
                # 視為 None

            if ai_action is None:
                logger.warning(
                    "plugin %s has no action defined", ready.plugin_id
                )
                return

            action_result: LlmActionResult = (
                await self._llm_client.execute_ai_action(
                    ai_action=ai_action,
                    event=ready.event,
                    query_result=ready.query_result,
                )
            )

            notify_actions = [
                a for a in action_result.actions
                if isinstance(
                    a, (NotifyInlineAction, NotifyStatusbarAction, NotifyPanelAction)
                )
            ]
            exec_actions = [
                a for a in action_result.actions
                if isinstance(a, (SuggestCommandAction, ExecCommandAction))
            ]

            try:
                await self._notifier.dispatch(
                    notify_actions,
                    session_id=ready.event.session_id,
                    event=ready.event,
                )
            except Exception as e:
                logger.error(
                    "notifier.dispatch failed for plugin %s: %s",
                    ready.plugin_id, e,
                )

            try:
                await self._executor.dispatch(
                    exec_actions,
                    session_id=ready.event.session_id,
                    event=ready.event,
                    plugin_id=ready.plugin_id,
                )
            except Exception as e:
                logger.error(
                    "executor.dispatch failed for plugin %s: %s",
                    ready.plugin_id, e,
                )
