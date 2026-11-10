# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""解說失敗的 **host-owned typed metadata**。

由 Package 在**原錯誤產生點**依 `LlmOutputError` 物件建立，因此欄位來源是
受信任的 validator 程式，不是模型 JSON、不是 client 參數，也不是對錯誤訊息
做 substring 判斷。

它只是「這次模型輸出被拒絕的分類」；**能不能續跑由 host policy 決定**，
這個模組不做那個判斷。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.packages.orfs_run_intelligence.schemas import (
    CODE_EVIDENCE_REF_SCOPE,
    LlmOutputError,
)

#: 唯一可能被 host 判為可降級的分類。
FAILURE_MODEL_OUTPUT_EVIDENCE_REF_REJECTED = "MODEL_OUTPUT_EVIDENCE_REF_REJECTED"
FAILURE_STAGE_MODEL_OUTPUT_VALIDATION = "MODEL_OUTPUT_VALIDATION"

#: 本次模型呼叫已明確結束（不是 timeout／未知）。
OUTCOME_KNOWN_TERMINATED = "KNOWN_TERMINATED"

#: 產生這份 metadata 的程式身分。host 用它確認來源，不接受別處寫入同名欄位。
PRODUCER = "orfs_run_intelligence.analysis_failure.v1"


@dataclass(frozen=True)
class AnalysisFailure:
    """一次解說失敗的結構化分類。"""

    producer: str
    failure_kind: str
    failure_stage: str
    execution_outcome: str
    capability_id: str
    #: 本次模型呼叫的身分（prompt hash 等），由 Package 提供。
    invocation: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer, "failure_kind": self.failure_kind,
            "failure_stage": self.failure_stage,
            "execution_outcome": self.execution_outcome,
            "capability_id": self.capability_id,
            "invocation": dict(self.invocation), "details": dict(self.details),
        }


def classify(exc: Exception, *, capability_id: str,
             invocation: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """只在**確定**是 evidence-ref 拒絕時回傳分類；其餘一律 `None`。

    判準是 validator 自己設定的 `code`，不是訊息內容。
    """
    if not isinstance(exc, LlmOutputError):
        return None
    if getattr(exc, "code", None) != CODE_EVIDENCE_REF_SCOPE:
        return None
    details = dict(getattr(exc, "details", None) or {})
    if not details.get("rejected_evidence_refs"):
        # 沒有具體被拒的 ref 就不算已分類；不猜。
        return None
    return AnalysisFailure(
        producer=PRODUCER,
        failure_kind=FAILURE_MODEL_OUTPUT_EVIDENCE_REF_REJECTED,
        failure_stage=FAILURE_STAGE_MODEL_OUTPUT_VALIDATION,
        # 模型呼叫已回覆並被驗證後拒絕——所以本次呼叫確定已結束。
        execution_outcome=OUTCOME_KNOWN_TERMINATED,
        capability_id=capability_id,
        invocation=dict(invocation or {}),
        details=details,
    ).to_json_dict()


__all__ = [
    "FAILURE_MODEL_OUTPUT_EVIDENCE_REF_REJECTED",
    "FAILURE_STAGE_MODEL_OUTPUT_VALIDATION",
    "OUTCOME_KNOWN_TERMINATED", "PRODUCER",
    "AnalysisFailure", "classify",
]
