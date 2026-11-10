# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""派送 flow step 期間的 host 注入 context。

與 analysis task 的 `TaskBodyBudget` 同一個模式：contextvar 讓 host 在派送**這一步**
時把 binding、執行 facade 與候選寫入器交給 package services，步驟結束即
reset。非 flow 呼叫拿到 `None`，行為完全不變。

`ToolRegistry.execute()` 已用 `contextvars.copy_context()`，因此它會正確
傳進 tool worker thread。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FlowBindingContext:
    binding: Any
    execution: Any
    variant_writer: Any
    #: 已核准 ECO hook 設定的寫入 helper。未注入時為 `None`。
    repair_writer: Any = None
    #: 僅由 host 從 task-owned Profile 解析的 evidence scope。這不是 action
    #: arguments，因此模型或 RPC caller 不能藉此改寫 DQE 來源。
    evidence_facts: dict[str, Any] = field(default_factory=dict)
    #: Per-run DQE DB 覆寫：`{run_id: source_dqe_db_path}`。imported baseline
    #: run 的 DQE 仍屬來源，位置由 Profile 釘選的 ``durable_baseline_source``
    #: 決定。action 層的 ``resolve_dqe_db_manager`` 透過此 mapping 判定某個
    #: run_id 該讀哪個 DB，而不是一律用 ``evidence_facts["dqe_db_path"]``。
    imported_run_dqe_overrides: dict[str, str] = field(default_factory=dict)


_current: "contextvars.ContextVar[FlowBindingContext | None]" = (
    contextvars.ContextVar("terminux_flow_binding_context", default=None)
)


def set_current_flow_context(
    value: FlowBindingContext | None,
) -> "contextvars.Token[FlowBindingContext | None]":
    return _current.set(value)


def reset_current_flow_context(
    token: "contextvars.Token[FlowBindingContext | None]",
) -> None:
    _current.reset(token)


def get_current_flow_context() -> FlowBindingContext | None:
    return _current.get()


__all__ = [
    "FlowBindingContext",
    "get_current_flow_context",
    "reset_current_flow_context",
    "set_current_flow_context",
]
