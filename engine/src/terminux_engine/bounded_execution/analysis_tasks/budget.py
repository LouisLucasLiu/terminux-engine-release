# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""task body 的 application-level LLM dispatch 預算。

一個根授權下的整個 task body 共用**最多三次** application-level LLM
dispatch：explain 的 initial 與 repair、compare 全部計入。失敗、timeout
與沒有 usage metadata 的 dispatch 一樣消耗一次——「有沒有拿到答案」不是
計費依據，「有沒有真的送出一次 application dispatch」才是。沒有額度就
**不呼叫 router**。

這三次**不是** provider HTTP request 數，也不是金額上限：SDK 內部 retry
是否可觀測由 provider 決定，本模組不宣稱能數它。

budget 透過 contextvar 傳遞：task worker 在自己的 thread 設定，
`ToolRegistry.execute()` 已用 `contextvars.copy_context()` 把它帶進 tool
worker thread。既有非 task 的呼叫拿到 `None`，行為完全不變。
"""

from __future__ import annotations

import contextvars
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

#: 沒有額度時的固定原因碼。
BUDGET_EXHAUSTED = "task_budget_exhausted"
#: task 已停止（cancel／deadline）時的固定原因碼。
BUDGET_STOPPED = "task_stopped"

_LOG = logging.getLogger(__name__)
#: 即時帳本無法先行入帳時的固定原因碼（fail closed，不送出 dispatch）。
BUDGET_LEDGER_UNAVAILABLE = "task_budget_ledger_unavailable"


@dataclass(frozen=True)
class BudgetSnapshot:
    limit: int
    used: int
    #: planning 階段的呼叫次數；不可觀測時是 None，**不填零**。
    planning_dispatches: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "body_llm_dispatch_limit": self.limit,
            "body_llm_dispatches_used": self.used,
            "planning_dispatches": self.planning_dispatches,
        }


class TaskBodyBudget:
    """thread-safe 的 dispatch 額度。

    `should_stop` 由 task service 注入，讓每次 call 前同時檢查 grant
    deadline 與 cancel/stop。已取得 permit 的在途呼叫**計入消耗**並允許
    自然返回——不撤銷、不宣稱能取消已送出的 provider 請求。
    """

    def __init__(self, limit: int, *, should_stop: Callable[[], str | None] | None = None,
                 on_consume: Callable[[int, int], None] | None = None) -> None:
        self._limit = int(limit)
        self._used = 0
        self._lock = threading.Lock()
        self._should_stop = should_stop
        #: 取得額度前呼叫 `(used_after, limit)`，用於先行寫入即時帳本。
        self._on_consume = on_consume

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def try_consume(self) -> tuple[bool, str | None]:
        """取得一次 dispatch 額度。回傳 `(allowed, reason)`。

        扣額與檢查在同一個 lock 內完成：並行的兩次呼叫不可能同時看到
        「還剩一次」。
        """
        stop_reason = self._should_stop() if self._should_stop is not None else None
        if stop_reason is not None:
            return False, stop_reason
        with self._lock:
            if self._used >= self._limit:
                return False, BUDGET_EXHAUSTED
            if self._on_consume is not None:
                try:
                    self._on_consume(self._used + 1, self._limit)
                except Exception:  # noqa: BLE001 - 未入帳就不得派送
                    _LOG.exception("task body dispatch ledger entry failed")
                    return False, BUDGET_LEDGER_UNAVAILABLE
            self._used += 1
            return True, None

    def snapshot(self, *, planning_dispatches: int | None = None) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(limit=self._limit, used=self._used,
                                  planning_dispatches=planning_dispatches)


_current_task_budget: "contextvars.ContextVar[TaskBodyBudget | None]" = (
    contextvars.ContextVar("terminux_task_body_budget", default=None)
)


def set_current_task_budget(
    budget: TaskBodyBudget | None,
) -> "contextvars.Token[TaskBodyBudget | None]":
    return _current_task_budget.set(budget)


def reset_current_task_budget(
    token: "contextvars.Token[TaskBodyBudget | None]",
) -> None:
    """完成或失敗一律 reset，不讓下一個 task 沿用前一個的額度。"""
    _current_task_budget.reset(token)


def get_current_task_budget() -> TaskBodyBudget | None:
    """目前 task body 的預算；非 task 呼叫恆為 None。"""
    return _current_task_budget.get()


__all__ = [
    "BUDGET_EXHAUSTED",
    "BUDGET_LEDGER_UNAVAILABLE",
    "BUDGET_STOPPED",
    "BudgetSnapshot",
    "TaskBodyBudget",
    "get_current_task_budget",
    "reset_current_task_budget",
    "set_current_task_budget",
]
