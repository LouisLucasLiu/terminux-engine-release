# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Production activation gate（approved package action）。

**預設 disabled，且沒有任何外部輸入可以打開它。**

沒有 environment variable、沒有 CLI flag、沒有 request field、沒有 Package
metadata、沒有 LLM 輸出可以把它變成 enabled。唯一能建立 enabled gate 的方式
是在 composition 時以 constructor 明確注入——那是 safety validation 用 test composition 的
權利，production composition 一律使用 `production_activation_gate()`。

要在 production 打開，必須：安全 validation 完成 → 設計者再次核准
→ 修改這個檔案的 production 預設並經 code review。
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVATION_REASON_VALIDATION_DEFERRED = "validation_deferred_to_AI-V"

#: 2026-09-06：安全 validation 完成、設計者明確核准後啟用。驗證內容：兩個獨立
#: session 皆建立恰 1 筆 proposal（其中一個是真實 daemon socket 上的 end-to-end），
#: approve／一次性 execute／replay 拒絕／negative matrix 全數通過。
#: 這個值是持久化的 reason code，不得改寫。
ACTIVATION_REASON_QUALIFIED = "qualified_by_AIV-030_and_AIV-031_2026-09-06"


@dataclass(frozen=True)
class ActivationGate:
    """Bounded execution 的啟用狀態。

    `enabled` 只能在建構時決定；frozen dataclass 讓它之後不可變。
    """

    enabled: bool
    reason: str

    def blocked_response(self) -> dict[str, object]:
        from terminux_engine.bounded_execution.errors import (
            BOUNDED_EXECUTION_VALIDATION_REQUIRED,
        )

        return {
            "status": "blocked",
            "error_code": BOUNDED_EXECUTION_VALIDATION_REQUIRED,
            "activation_reason": self.reason,
            "action_execution_count": 0,
            "proposal_claimed": False,
            "message": (
                "Bounded execution is implemented but disabled pending safety "
                "validation. No action was executed and the proposal was not consumed."
            ),
        }


def production_activation_gate() -> ActivationGate:
    """Production composition 的**唯一**來源。

    2026-09-06 起為 enabled——這個決定寫在版本控制的 source 裡，經 code review，
    不是任何 runtime 輸入的結果。

    刻意仍然不讀 `os.environ`：一旦讀了，任何能設環境變數的人就能改變 gate；
    要改就必須改這個檔案並經 review。啟用之後，執行權限依然完全受既有的
    proposal → explicit approval → one-shot claim → allowlist 約束
    （`eligibility.ALLOWED_EXECUTION_CAPABILITIES` 只有 explain_run 與
    compare_runs），gate 本身不放寬其中任何一項。
    """
    return ActivationGate(enabled=True, reason=ACTIVATION_REASON_QUALIFIED)


__all__ = [
    "ACTIVATION_REASON_QUALIFIED",
    "ACTIVATION_REASON_VALIDATION_DEFERRED",
    "ActivationGate",
    "production_activation_gate",
]
