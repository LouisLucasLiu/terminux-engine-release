# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""解說失敗的 host policy evaluator。

**唯一**被允許不阻擋已授權工程的失敗，是「本次 Package 輸出 validator 因
模型引用不存在／不屬於本次允許集合的 evidence ref 而拒絕解說」。其他任何
驗證錯誤、provider 缺席、timeout、未確認的背景工作、grant／budget／policy
問題都不適用——它們沿既有失敗政策。

s2 與 s5 共用**這一個** evaluator；不在每個入口各寫一次例外。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POLICY_ID = "r5.reference_rejection_nonblocking.v1"

DISPOSITION_CONTINUE = "CONTINUE_WITHOUT_NARRATIVE"
DISPOSITION_STOP = "STOP"

#: 只有這一組值才可能續跑。全部由受信任的 Package 程式產生。
_REQUIRED_PRODUCER = "orfs_run_intelligence.analysis_failure.v1"
_REQUIRED_KIND = "MODEL_OUTPUT_EVIDENCE_REF_REJECTED"
_REQUIRED_STAGE = "MODEL_OUTPUT_VALIDATION"
_REQUIRED_OUTCOME = "KNOWN_TERMINATED"

NARRATIVE_COMPLETE = "COMPLETE"
NARRATIVE_PARTIAL = "PARTIAL"
NARRATIVE_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class NarrativeDecision:
    disposition: str
    reason: str
    failure_kind: str | None = None
    classified: dict[str, Any] = field(default_factory=dict)

    @property
    def may_continue(self) -> bool:
        return self.disposition == DISPOSITION_CONTINUE

    def to_json_dict(self) -> dict[str, Any]:
        return {"policy_id": POLICY_ID, "disposition": self.disposition,
                "reason": self.reason, "failure_kind": self.failure_kind,
                "classified": dict(self.classified)}


def evaluate(
    *,
    policy_id: str | None,
    capability_id: str,
    full_result: Any,
    dispatch_timed_out: bool,
    dispatch_denial_code: str | None,
    worker_confirmed: bool,
    engineering_ready: bool,
    engineering_reason: str | None = None,
) -> NarrativeDecision:
    """判定這次解說失敗可不可以在不阻擋已授權工程的前提下繼續。

    每一個條件都必須成立；任何一個不成立就 `STOP`，且理由寫清楚。
    """
    def stop(reason: str, kind: str | None = None) -> NarrativeDecision:
        return NarrativeDecision(DISPOSITION_STOP, reason, kind)

    # 1. 只有新 template 的 profile 才帶這個 policy。缺欄位不得默認享有。
    if policy_id != POLICY_ID:
        return stop("this task does not carry the narrative failure policy")

    # 2. 本次模型呼叫必須明確結束；timeout／未確認一律不適用。
    if dispatch_timed_out or not worker_confirmed:
        return stop("the model invocation outcome is not confirmed")
    if dispatch_denial_code:
        return stop(f"the dispatch was denied: {dispatch_denial_code}")

    # 3. 分類必須來自受信任 producer，且欄位完全相符。
    if not isinstance(full_result, dict):
        return stop("the action result is not readable")
    classified = full_result.get("analysis_failure")
    if not isinstance(classified, dict):
        return stop("the action reported no typed analysis failure")
    if classified.get("producer") != _REQUIRED_PRODUCER:
        return stop("the analysis failure metadata has no trusted producer")
    kind = classified.get("failure_kind")
    if kind != _REQUIRED_KIND:
        return stop(f"failure kind {kind!r} is not eligible", kind)
    if classified.get("failure_stage") != _REQUIRED_STAGE:
        return stop("the rejection did not happen at model output validation", kind)
    if classified.get("execution_outcome") != _REQUIRED_OUTCOME:
        return stop("the model invocation is not known to have terminated", kind)
    if classified.get("capability_id") != capability_id:
        return stop("the analysis failure belongs to a different capability", kind)
    details = classified.get("details")
    if not isinstance(details, dict) or not details.get("rejected_evidence_refs"):
        return stop("no rejected evidence reference was recorded", kind)
    if not details.get("allowed_evidence_ref_digest"):
        return stop("the allowed evidence reference set is not identified", kind)

    # 4. 工程前提必須仍然可信。原始證據有問題就不是「解說降級」。
    if not engineering_ready:
        return stop(engineering_reason
                    or "the engineering evidence for this step is not trustworthy",
                    kind)

    return NarrativeDecision(
        DISPOSITION_CONTINUE,
        "the rejection is confined to model output evidence references; the "
        "verified engineering evidence and the approved fixed plan are unchanged",
        kind, dict(classified))


def narrative_status(*, attempted: int, rejected: int) -> str:
    """`COMPLETE`／`PARTIAL`／`UNAVAILABLE`——兩段皆有效／一段遭拒／皆遭拒。"""
    if attempted <= 0 or rejected <= 0:
        return NARRATIVE_COMPLETE
    if rejected >= attempted:
        return NARRATIVE_UNAVAILABLE
    return NARRATIVE_PARTIAL


__all__ = [
    "DISPOSITION_CONTINUE", "DISPOSITION_STOP",
    "NARRATIVE_COMPLETE", "NARRATIVE_PARTIAL", "NARRATIVE_UNAVAILABLE",
    "POLICY_ID", "NarrativeDecision", "evaluate", "narrative_status",
]
