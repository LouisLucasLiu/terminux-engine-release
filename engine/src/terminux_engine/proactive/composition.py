# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Production composition。

本模組是 production 端唯一的 proactive 組裝點。它刻意只做兩件事：

1. 用 `production_proactive_gate()` 建 coordinator；
2. 提供 `run_proactive_cycle_once()`，讓 daemon runner 呼叫。

2026-09-06：runtime validation 全數通過後 gate 改為 enabled，daemon 由
`jsonrpc_server._init_proactive_runner()` 註冊**恰好一個**
`ProactiveRunner`（單 thread、cycle 不重疊、graceful stop/join）。

production 現在會注入 per-cycle provider budget。`budget_limits`
仍是參數而非在此推導——推導需要 package 的 output budget 常數，Common
不得認識任何 package。呼叫端負責用 `budget.derive_cycle_limits()` 推導。

刻意仍然不讀 `os.environ`／設定檔：一旦讀了，任何能設環境變數的人就能
改變 proactive 的啟用狀態。要改必須改 `activation.py` 並經 review。
"""

from __future__ import annotations

from typing import Any

from terminux_engine.proactive.activation import (
    ProactiveActivationGate,
    production_proactive_gate,
)
from terminux_engine.proactive.coordinator import (
    ProactiveCoordinator,
    ProactiveDeps,
    ProactiveProvider,
)

#: 一個 cycle 最多處理幾個 candidate。單一來源——per-cycle budget 的推導
#: 必須用同一個值，否則預算與實際結構上限會對不起來。
PRODUCTION_MAX_CANDIDATES = 5


def build_production_coordinator(
    *,
    provider: ProactiveProvider,
    record_store: Any,
    notifier: Any,
    activation_gate: ProactiveActivationGate | None = None,
    budget_limits: Any | None = None,
    max_candidates: int = PRODUCTION_MAX_CANDIDATES,
) -> ProactiveCoordinator:
    """組出 production coordinator。預設使用 production gate。

    `activation_gate` 只在 validation harness 需要顯式 gate 時由呼叫端注入；
    production 呼叫端不傳，一律沿用 `production_proactive_gate()`。

    `budget_limits` 為 None 時行為與舊版完全相同（不設限）。
    production 呼叫端一律傳入——見 `budget.derive_cycle_limits()` 對於
    「為何是天花板而非節流閥」的說明。
    """

    return ProactiveCoordinator(
        ProactiveDeps(
            provider=provider,
            activation_gate=activation_gate or production_proactive_gate(),
            record_store=record_store,
            notifier=notifier,
            budget_limits=budget_limits,
        ),
        max_candidates=max_candidates,
    )


def run_proactive_cycle_once(coordinator: ProactiveCoordinator) -> dict[str, Any]:
    """daemon runner 的唯一進入點。"""

    return coordinator.run_once()
