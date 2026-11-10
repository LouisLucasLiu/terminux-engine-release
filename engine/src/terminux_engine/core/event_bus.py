# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Coroutine

from terminux_engine.ecc.models import ShellEvent

logger = logging.getLogger(__name__)

# ── 合法訂閱者白名單 ──────────────────────────────────────────
ALLOWED_SUBSCRIBERS: frozenset[str] = frozenset({
    "Layer1Pipeline",
})

# ── 合法事件類型（初版）────────────────────────────────────────────────────────
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    "shell_event",
})

# Handler 型別別名
ShellEventHandler = Callable[[ShellEvent], Coroutine]


# ── 錯誤型別 ───────────────────────────────────────────────────────────────────

class SubscriberNotAllowedError(Exception):
    """subscriber_id 不在 ALLOWED_SUBSCRIBERS 白名單內時拋出。"""
    pass


# ── EventBus ───────────────────────────────────────────────────────────────────

class EventBus:
    """
    非同步 pub/sub 事件總線。
    對應 Qt IDEEventBus 的概念，以 asyncio.Task 取代 Qt signal/slot。
    線程安全：僅在 asyncio event loop 所在 thread 呼叫，不做跨 thread 保護。
    """

    def __init__(self) -> None:
        # _subscribers[event_type][subscriber_id] = handler
        self._subscribers: dict[str, dict[str, ShellEventHandler]] = defaultdict(dict)

    def subscribe(
        self,
        event_type: str,
        handler: ShellEventHandler,
        subscriber_id: str,
    ) -> None:
        """
        登記 handler 至 event_type 的訂閱者列表。
        subscriber_id 不在白名單時拋出 SubscriberNotAllowedError。
        重複訂閱時以新 handler 覆蓋舊 handler，並記錄 warning。
        """
        if subscriber_id not in ALLOWED_SUBSCRIBERS:
            raise SubscriberNotAllowedError(
                f"Subscriber '{subscriber_id}' is not in the allowed list."
            )

        if subscriber_id in self._subscribers[event_type]:
            logger.warning(
                "Subscriber '%s' re-registered for '%s', overwriting.",
                subscriber_id, event_type,
            )

        self._subscribers[event_type][subscriber_id] = handler

        if event_type not in KNOWN_EVENT_TYPES:
            logger.warning(
                "event_type '%s' is not a known type, registered anyway.",
                event_type,
            )

    def unsubscribe(self, event_type: str, subscriber_id: str) -> None:
        """
        移除 event_type 的指定訂閱者。
        event_type 或 subscriber_id 不存在時靜默忽略。
        訂閱者移除後若 event_type 無任何訂閱者，清理該 key。
        """
        if event_type not in self._subscribers:
            return
        if subscriber_id not in self._subscribers[event_type]:
            return
        del self._subscribers[event_type][subscriber_id]
        if not self._subscribers[event_type]:
            del self._subscribers[event_type]

    async def publish(self, event_type: str, event: ShellEvent) -> None:
        """
        廣播 event 給所有已訂閱 event_type 的 handler。
        為每個訂閱者建立獨立 asyncio.Task（fire-and-forget），立即返回。
        無訂閱者時記錄 debug log 並返回，不報錯。
        """
        snapshot = self._subscribers.get(event_type, {}).copy()
        if not snapshot:
            logger.debug(
                "No subscribers for event_type '%s', event discarded.", event_type
            )
            return

        for subscriber_id, handler in snapshot.items():
            asyncio.create_task(
                self._safe_call(subscriber_id, handler, event),
                name=f"event_bus.{event_type}.{subscriber_id}",
            )

    async def _safe_call(
        self,
        subscriber_id: str,
        handler: ShellEventHandler,
        event: ShellEvent,
    ) -> None:
        """捕獲 handler 執行期間的所有 exception，記錄 error log，不 re-raise。"""
        try:
            await handler(event)
        except Exception as e:
            logger.error(
                "Handler '%s' raised exception: %s: %s",
                subscriber_id, type(e).__name__, e,
            )

    def subscriber_count(self, event_type: str) -> int:
        """回傳指定 event_type 目前的訂閱者數量。供測試與診斷使用。"""
        return len(self._subscribers.get(event_type, {}))
