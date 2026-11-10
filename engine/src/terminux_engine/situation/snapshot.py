# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`SituationSnapshot v1`。

刻意**沒有** `confidence: 0.85` 這種未校準的自評分數。可信程度只用兩件事
表達：每個 claim 的 `support_status`，以及明列的 `missing_evidence`。

`readiness` 永遠帶 `authoritative: false` 與
`formal_signoff_gate: not_evaluated`——situation assessment 不是 signoff authority。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.situation.context import canonical_json, sha256_text

#: v2：新增 `analysis_input` 區塊，把「分析輸入是否備妥」與
#: 「execution/dispatch/Barrier lifecycle 是否被觀察到」明確分開。
#: 舊 v1 snapshot 只存在於 development
#: evidence，沒有 production／public consumer。
SITUATION_SNAPSHOT_SCHEMA_VERSION = 2

#: 沒有 durable receipt 時的預設 lifecycle 陳述。situation assessment action path 與 proactive intelligence
#: proactive path 都拿不到 receipt（G-1），所以兩者的預設相同。
UNOBSERVED_LIFECYCLE: dict[str, Any] = {
    "execution_completion_status": "unverified",
    "dispatch_status": "not_observed",
    "barrier_evidence_status": "not_observed",
    "lifecycle_evidence_basis": {
        "execution_receipt": "unavailable",
        "dispatch_receipt": "unavailable",
        "barrier_receipt": "unavailable",
    },
}
SNAPSHOT_RECORD_TYPE = "situation_snapshot"

READINESS_NOT_READY = "not_ready"
READINESS_INSUFFICIENT = "insufficient_evidence"
READINESS_CONDITIONAL = "conditionally_ready"
READINESS_STATUSES: tuple[str, ...] = (
    READINESS_NOT_READY, READINESS_INSUFFICIENT, READINESS_CONDITIONAL)

SUPPORT_SUPPORTED = "supported"
SUPPORT_PARTIAL = "partially_supported"
SUPPORT_INSUFFICIENT = "insufficient_evidence"
SUPPORT_STATUSES: tuple[str, ...] = (
    SUPPORT_SUPPORTED, SUPPORT_PARTIAL, SUPPORT_INSUFFICIENT)
GLOBAL_SUPPORT_STATUSES = SUPPORT_STATUSES


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    evidence_refs: tuple[str, ...] = ()
    support_status: str | None = None
    fact_ids: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "claim_id": self.claim_id, "text": self.text,
            "evidence_refs": list(self.evidence_refs),
            "fact_ids": list(self.fact_ids),
        }
        if self.support_status is not None:
            payload["support_status"] = self.support_status
        return payload


@dataclass(frozen=True)
class Hypothesis:
    """可能原因，**尚未證實**。必須說明還缺什麼才能確認。"""

    claim_id: str
    text: str
    supporting_evidence_refs: tuple[str, ...] = ()
    missing_confirmation: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id, "text": self.text,
            "supporting_evidence_refs": list(self.supporting_evidence_refs),
            "missing_confirmation": list(self.missing_confirmation),
            "fact_ids": list(self.fact_ids),
        }


@dataclass(frozen=True)
class RecommendedCheck:
    check_id: str
    description: str
    purpose: str
    required_evidence: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {"check_id": self.check_id, "description": self.description,
                "purpose": self.purpose,
                "required_evidence": list(self.required_evidence)}


@dataclass(frozen=True)
class Readiness:
    status: str
    basis: tuple[str, ...] = ()

    #: 常數。situation assessment 不是 signoff authority。
    authoritative: bool = False
    formal_signoff_gate: str = "not_evaluated"

    def to_json_dict(self) -> dict[str, Any]:
        return {"status": self.status, "authoritative": False,
                "basis": list(self.basis),
                "formal_signoff_gate": "not_evaluated"}


@dataclass(frozen=True)
class SituationSnapshot:
    schema_version: int
    snapshot_id: str
    context_id: str
    scope: dict[str, Any]
    generated_at: str | None
    producer: dict[str, Any]
    subjects: tuple[dict[str, Any], ...]
    readiness: Readiness
    facts: tuple[Claim, ...]
    improvements: tuple[Claim, ...]
    regressions: tuple[Claim, ...]
    blockers: tuple[Claim, ...]
    interpretations: tuple[Claim, ...]
    hypotheses: tuple[Hypothesis, ...]
    unknowns: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    recommended_checks: tuple[RecommendedCheck, ...]
    global_support_status: str
    limitations: tuple[str, ...]
    provenance: dict[str, Any]

    #: 常數。situation assessment 是 read-only intelligence。
    execution: dict[str, Any] = field(default_factory=lambda: {
        "action_calls": 0, "tool_calls": 0, "proposals_created": 0,
        "execution_allowed": False})

    #: 分析輸入是否備妥，以及 lifecycle 三軸是否被觀察到。
    #: 預設是「輸入狀態未宣告、lifecycle 未觀察」——要升級必須有 receipt。
    analysis_input: dict[str, Any] = field(default_factory=lambda: {
        "analysis_input_status": "unverifiable",
        "analysis_input_basis": {},
        "analysis_input_provenance": [],
        **{k: (dict(v) if isinstance(v, dict) else v)
           for k, v in UNOBSERVED_LIFECYCLE.items()},
    })

    def content(self) -> dict[str, Any]:
        """進 `snapshot_sha256` 的內容。`generated_at` 與 `snapshot_id`
        本身不參與——identity 由內容決定。"""
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "scope": dict(self.scope),
            "producer": {k: v for k, v in self.producer.items() if k != "generated_at"},
            "subjects": [dict(s) for s in self.subjects],
            "readiness": self.readiness.to_json_dict(),
            "facts": [c.to_json_dict() for c in self.facts],
            "improvements": [c.to_json_dict() for c in self.improvements],
            "regressions": [c.to_json_dict() for c in self.regressions],
            "blockers": [c.to_json_dict() for c in self.blockers],
            "interpretations": [c.to_json_dict() for c in self.interpretations],
            "hypotheses": [h.to_json_dict() for h in self.hypotheses],
            "unknowns": list(self.unknowns),
            "missing_evidence": list(self.missing_evidence),
            "recommended_checks": [c.to_json_dict() for c in self.recommended_checks],
            "global_support_status": self.global_support_status,
            "limitations": list(self.limitations),
            "execution": {"action_calls": 0, "tool_calls": 0,
                          "proposals_created": 0, "execution_allowed": False},
            "analysis_input": dict(self.analysis_input),
        }

    def to_json_dict(self) -> dict[str, Any]:
        payload = self.content()
        payload["snapshot_id"] = self.snapshot_id
        payload["generated_at"] = self.generated_at
        payload["provenance"] = {**dict(self.provenance),
                                 "snapshot_sha256": compute_snapshot_sha256(self)}
        return payload


#: `to_json_dict()` 相對於 `content()` 多出來的欄位。它們由內容決定或是
#: 寫入時資訊，因此不參與 canonical hash。
_NON_CONTENT_KEYS = ("snapshot_id", "generated_at", "provenance")


def canonical_content_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """從已保存的 record payload 還原 canonical content。

    `None` 代表這筆紀錄無法 canonicalize——呼叫端必須拒絕，**不得猜測**。
    """
    if not isinstance(payload, dict):
        return None
    content = {k: v for k, v in payload.items() if k not in _NON_CONTENT_KEYS}
    # `content()` 會把 producer 裡的 generated_at 去掉；還原時比照處理，
    # 否則同一份 snapshot 會算出不同 hash。
    producer = content.get("producer")
    if isinstance(producer, dict):
        content["producer"] = {k: v for k, v in producer.items() if k != "generated_at"}
    required = {"schema_version", "context_id", "facts", "readiness", "execution",
                "analysis_input"}
    if not required.issubset(content):
        return None
    return content


def recompute_snapshot_sha256(payload: dict[str, Any]) -> str | None:
    """從**內容**重算 hash，而不是相信 payload 裡存的那個值。

    存下來的 `provenance.snapshot_sha256` 只是宣稱；內容被改而 hash 沒改的
    紀錄必須被偵測出來。
    """
    content = canonical_content_from_payload(payload)
    if content is None:
        return None
    return sha256_text(canonical_json(content))


def compute_snapshot_sha256(snapshot: SituationSnapshot) -> str:
    return sha256_text(canonical_json(snapshot.content()))


def compute_snapshot_id(snapshot: SituationSnapshot) -> str:
    return f"sit_{compute_snapshot_sha256(snapshot)[:32]}"


__all__ = [
    "Claim",
    "GLOBAL_SUPPORT_STATUSES",
    "Hypothesis",
    "READINESS_CONDITIONAL",
    "READINESS_INSUFFICIENT",
    "READINESS_NOT_READY",
    "READINESS_STATUSES",
    "Readiness",
    "RecommendedCheck",
    "SITUATION_SNAPSHOT_SCHEMA_VERSION",
    "SNAPSHOT_RECORD_TYPE",
    "SUPPORT_INSUFFICIENT",
    "SUPPORT_PARTIAL",
    "SUPPORT_STATUSES",
    "SUPPORT_SUPPORTED",
    "SituationSnapshot",
    "canonical_content_from_payload",
    "compute_snapshot_id",
    "compute_snapshot_sha256",
    "recompute_snapshot_sha256",
]
