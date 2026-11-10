# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Deterministic evidence support validator。

Snapshot 輸出前逐項檢查。任何一項不過即 invalid——invalid snapshot **不得**
被部分保存成正式 Situation Snapshot，也不得觸發任何額外查詢或 action。

這一層不理解任何 domain 語意：它只檢查引用是否存在、excerpt hash 是否相符、
分類是否被混用、以及 execution counters 是否全為 0。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from terminux_engine.situation.context import SituationContext
from terminux_engine.situation.snapshot import (
    READINESS_CONDITIONAL,
    READINESS_INSUFFICIENT,
    READINESS_NOT_READY,
    SUPPORT_STATUSES,
    SituationSnapshot,
)

#: 出現在 hypothesis 文字裡就代表它被寫成了確定的因果——必須拒絕。
_CAUSAL_ASSERTIONS: tuple[str, ...] = (
    "caused the", "is the root cause", "was the root cause", "root cause is",
    "確認是", "就是主因", "導致了",
)


@dataclass(frozen=True)
class SupportReview:
    valid: bool
    errors: tuple[dict[str, Any], ...] = ()
    checks: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": [dict(e) for e in self.errors],
                "checks": dict(self.checks)}


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def validate_support(
    snapshot: SituationSnapshot,
    context: SituationContext,
    *,
    deterministic_readiness: str,
) -> SupportReview:
    """回傳 deterministic 檢查結果。不修改 snapshot、不自動修補。"""
    errors: list[dict[str, Any]] = []

    fact_ids = {f.fact_id for f in context.deterministic_facts}
    refs = {r.evidence_ref_id: r for r in context.evidence_refs}
    subject_ids = {s.subject_id for s in context.subjects}

    # 1. facts 只能引用 deterministic fact catalog 內的 id
    for claim in snapshot.facts:
        unknown = [i for i in claim.fact_ids if i not in fact_ids]
        if unknown:
            errors.append(_err("SITUATION_FACT_UNKNOWN_ID",
                               f"fact {claim.claim_id} references unknown fact ids",
                               claim_id=claim.claim_id, unknown=unknown))

    # 2./3. 所有 evidence ref 必須 resolve；interpretation 至少要有一個
    def check_refs(claim_id: str, ref_ids, label: str, require_one: bool) -> None:
        unknown = [i for i in ref_ids if i not in refs]
        if unknown:
            errors.append(_err("SITUATION_EVIDENCE_UNRESOLVED",
                               f"{label} {claim_id} references unresolvable evidence",
                               claim_id=claim_id, unknown=unknown))
        if require_one and not ref_ids:
            errors.append(_err("SITUATION_INTERPRETATION_UNSUPPORTED",
                               f"{label} {claim_id} has no evidence reference",
                               claim_id=claim_id))

    for group, label, require in (
        (snapshot.facts, "fact", False),
        (snapshot.improvements, "improvement", False),
        (snapshot.regressions, "regression", False),
        (snapshot.blockers, "blocker", False),
        (snapshot.interpretations, "interpretation", True),
    ):
        for claim in group:
            check_refs(claim.claim_id, claim.evidence_refs, label, require)
    for h in snapshot.hypotheses:
        check_refs(h.claim_id, h.supporting_evidence_refs, "hypothesis", False)

    # 4. evidence subject 必須屬於這個 context 的 scope
    for claim in list(snapshot.facts) + list(snapshot.interpretations):
        for ref_id in claim.evidence_refs:
            ref = refs.get(ref_id)
            if ref is not None and ref.subject_id not in subject_ids:
                errors.append(_err("SITUATION_EVIDENCE_OUT_OF_SCOPE",
                                   f"evidence {ref_id} points outside this context scope",
                                   claim_id=claim.claim_id))

    # 5. excerpt hash 必須與內容一致
    for ref in context.evidence_refs:
        if ref.excerpt_hash is None or not ref.excerpt_text:
            continue
        actual = hashlib.sha256(ref.excerpt_text.encode("utf-8")).hexdigest()
        if actual != ref.excerpt_hash:
            errors.append(_err("SITUATION_EXCERPT_HASH_MISMATCH",
                               f"evidence {ref.evidence_ref_id} excerpt hash does not "
                               f"match its text", evidence_ref_id=ref.evidence_ref_id))

    # 6. hypothesis 不得出現在 facts；也不得被寫成確定的因果
    fact_texts = {c.text.strip().casefold() for c in snapshot.facts}
    for h in snapshot.hypotheses:
        if h.text.strip().casefold() in fact_texts:
            errors.append(_err("SITUATION_HYPOTHESIS_AS_FACT",
                               f"hypothesis {h.claim_id} also appears as a fact",
                               claim_id=h.claim_id))
        lowered = h.text.casefold()
        for marker in _CAUSAL_ASSERTIONS:
            if marker in lowered:
                errors.append(_err("SITUATION_HYPOTHESIS_ASSERTS_CAUSE",
                                   f"hypothesis {h.claim_id} states a confirmed cause",
                                   claim_id=h.claim_id, marker=marker))
        # 7. hypothesis 必須說明還缺什麼才能確認
        if not h.missing_confirmation:
            errors.append(_err("SITUATION_HYPOTHESIS_NO_MISSING_CONFIRMATION",
                               f"hypothesis {h.claim_id} does not say what would confirm it",
                               claim_id=h.claim_id))

    # 8. unknowns 不得被清空
    if not snapshot.unknowns:
        errors.append(_err("SITUATION_UNKNOWNS_REMOVED",
                           "unknowns must not be empty; at least the root-cause limitation "
                           "has to be acknowledged"))

    # 9. readiness 必須與 deterministic base status 一致，且非權威
    if snapshot.readiness.status != deterministic_readiness:
        errors.append(_err("SITUATION_READINESS_MISMATCH",
                           "readiness does not match the deterministic base status",
                           declared=snapshot.readiness.status,
                           deterministic=deterministic_readiness))
    if snapshot.readiness.status not in (READINESS_NOT_READY, READINESS_INSUFFICIENT,
                                         READINESS_CONDITIONAL):
        errors.append(_err("SITUATION_READINESS_INVALID",
                           f"unknown readiness status {snapshot.readiness.status!r}"))
    if snapshot.readiness.authoritative or \
            snapshot.readiness.formal_signoff_gate != "not_evaluated":
        errors.append(_err("SITUATION_READINESS_CLAIMS_AUTHORITY",
                           "situation readiness is never authoritative and never evaluates a "
                           "formal signoff gate"))

    # 10. execution counters 全部為 0
    execution = snapshot.execution or {}
    for key in ("action_calls", "tool_calls", "proposals_created"):
        if execution.get(key, 0) != 0:
            errors.append(_err("SITUATION_EXECUTION_COUNTER_NONZERO",
                               f"{key} must be 0 in a read-only situation snapshot",
                               key=key, value=execution.get(key)))
    if execution.get("execution_allowed", False):
        errors.append(_err("SITUATION_EXECUTION_ALLOWED",
                           "execution_allowed must be false"))

    if snapshot.global_support_status not in SUPPORT_STATUSES:
        errors.append(_err("SITUATION_SUPPORT_STATUS_INVALID",
                           f"unknown global support status "
                           f"{snapshot.global_support_status!r}"))
    for claim in snapshot.interpretations:
        if claim.support_status not in SUPPORT_STATUSES:
            errors.append(_err("SITUATION_SUPPORT_STATUS_INVALID",
                               f"interpretation {claim.claim_id} has an invalid support "
                               f"status", claim_id=claim.claim_id))

    return SupportReview(
        valid=not errors, errors=tuple(errors),
        checks={
            "fact_count": len(snapshot.facts),
            "interpretation_count": len(snapshot.interpretations),
            "hypothesis_count": len(snapshot.hypotheses),
            "unknown_count": len(snapshot.unknowns),
            "evidence_ref_count": len(context.evidence_refs),
            "unsupported_fact_count": sum(
                1 for e in errors if e["code"] == "SITUATION_FACT_UNKNOWN_ID"),
            "hypothesis_mislabeled_as_fact_count": sum(
                1 for e in errors if e["code"] == "SITUATION_HYPOTHESIS_AS_FACT"),
            "readiness_matches_deterministic": snapshot.readiness.status
            == deterministic_readiness,
        },
    )


__all__ = ["SupportReview", "validate_support"]
