# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Proactive activation gate（proactive intelligence）。

**預設 disabled，且沒有任何外部輸入可以打開它。** 不讀 environment
variable、不讀 config、不接受 request 欄位。唯一能建立 enabled gate 的方式是
constructor injection——那是 safety validation 用 test composition 的權利。

Disabled 時**不得**消耗 candidate、移動 cursor 或寫任何 receipt。
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVATION_REASON = "validation_deferred_to_AI-V"

#: 2026-09-06：adapter 補上 per-request output limit 與 usage 回收之後全數通過
#: （within-budget 鏈路、output/call budget 的 pre-call 拒絕、usage 累計、dedup、
#: 零 execution counters），因此標為 REAL_RUNTIME_VALIDATED。
#: 這個值是持久化的 reason code，不得改寫。
ACTIVATION_REASON_QUALIFIED = "qualified_by_AIV-034_2026-09-06"
BLOCKED_CODE = "PROACTIVE_INTELLIGENCE_VALIDATION_REQUIRED"


@dataclass(frozen=True)
class ProactiveActivationGate:
    enabled: bool
    reason: str

    def blocked_response(self) -> dict[str, object]:
        return {
            "status": "blocked",
            "error_code": BLOCKED_CODE,
            "activation_reason": self.reason,
            "candidates_examined": 0,
            "claims_written": 0,
            "llm_calls": 0,
            "snapshots_written": 0,
            "notifications_sent": 0,
            "cursor_moved": False,
            "message": ("Proactive intelligence is implemented but disabled pending "
                        "safety validation. Nothing was observed, claimed, "
                        "analysed or notified."),
        }


def production_proactive_gate() -> ProactiveActivationGate:
    """Production composition 的**唯一**來源。

    2026-09-06 起為 enabled——這個決定寫在版本控制的 source 裡並經 review，
    不讀 environment variable、不讀 config、不接受 request 欄位。

    啟用之後 proactive intelligence 依然只做 read-only observation／analysis／durable snapshot／
    notification：依賴裡沒有 ActionRegistry、ProposalRegistry、Kernel、
    SubprocessExecutor 或任何 action executor，gate 本身也不放寬這一點。
    """
    return ProactiveActivationGate(enabled=True, reason=ACTIVATION_REASON_QUALIFIED)


__all__ = ["ACTIVATION_REASON", "ACTIVATION_REASON_QUALIFIED", "BLOCKED_CODE",
           "ProactiveActivationGate", "production_proactive_gate"]
