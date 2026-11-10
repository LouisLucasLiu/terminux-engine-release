# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`SituationContext v1`。

Common 只搬運 identity、source status、deterministic facts、evidence、budget
與 opaque `domain_payload`。它**不得**知道任何 metric 名稱、corner、
report 檔名或工具語意。

Budget 預設沿用**已實作的 compare_runs contract**（`comparison_context.py`）：
32000 / 12000 / 4000（total / per-source / per-excerpt）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SITUATION_CONTEXT_SCHEMA_VERSION = 1
ASSEMBLER_VERSION = "situation_context_v1"

SCOPE_RUN_PAIR = "run_pair"

#: Source 的解析結果。每一種都是一等狀態，不是錯誤——Package 必須能誠實
#: 表達「這份證據拿不到」。
SOURCE_STATUSES: frozenset[str] = frozenset(
    {"available", "missing", "unreadable", "truncated", "unverified"}
)

#: 與 compare_runs 已實作的 contract 一致，不另立一套數字。
DEFAULT_SOURCE_BUDGET = 32000
DEFAULT_PER_SOURCE_BUDGET = 12000
DEFAULT_PER_EXCERPT_BUDGET = 4000

_WHITESPACE = re.compile(r"\s+")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_question_hash(question: str | None) -> str:
    if not question:
        return sha256_text("")
    return sha256_text(_WHITESPACE.sub(" ", question.strip()).casefold())


@dataclass(frozen=True)
class EvidenceRef:
    """指向某個來源內一段可重算內容的引用。

    結構化來源（例如資料庫列）可用 `record_identity` ＋ `row_identity`
    表達，不必偽造 line range。
    """

    evidence_ref_id: str
    subject_id: str
    source_id: str
    source_type: str
    artifact_identity: str | None = None
    artifact_hash: str | None = None
    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    record_identity: str | None = None
    row_identity: str | None = None
    excerpt_hash: str | None = None
    excerpt_text: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref_id": self.evidence_ref_id,
            "subject_id": self.subject_id,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "artifact_identity": self.artifact_identity,
            "artifact_hash": self.artifact_hash,
            "section": self.section,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "record_identity": self.record_identity,
            "row_identity": self.row_identity,
            "excerpt_hash": self.excerpt_hash,
            "excerpt_text": self.excerpt_text,
        }

    def identity(self) -> dict[str, Any]:
        payload = self.to_json_dict()
        payload.pop("excerpt_text", None)
        return payload


@dataclass(frozen=True)
class SituationSubject:
    subject_id: str
    subject_type: str
    run_id: str
    manifest_id: str | None = None
    artifact_identity: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id, "subject_type": self.subject_type,
            "run_id": self.run_id, "manifest_id": self.manifest_id,
            "artifact_identity": dict(self.artifact_identity),
        }


@dataclass(frozen=True)
class SituationSource:
    source_id: str
    source_type: str
    subject_id: str
    status: str
    identity: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None
    excerpt_or_structured_data: Any = None
    status_reason: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "source_type": self.source_type,
            "subject_id": self.subject_id, "status": self.status,
            "identity": dict(self.identity), "content_hash": self.content_hash,
            "excerpt_or_structured_data": self.excerpt_or_structured_data,
            "status_reason": self.status_reason,
        }

    def identity_for_context_id(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "source_type": self.source_type,
                "subject_id": self.subject_id, "status": self.status,
                "identity": dict(self.identity), "content_hash": self.content_hash}


@dataclass(frozen=True)
class DeterministicFact:
    """由程式算出、LLM 不得改寫的事實。"""

    fact_id: str
    subject_id: str | None
    category: str
    text: str
    value: Any = None
    unit: str | None = None
    evidence_ref_ids: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id, "subject_id": self.subject_id,
            "category": self.category, "text": self.text, "value": self.value,
            "unit": self.unit, "evidence_ref_ids": list(self.evidence_ref_ids),
        }


@dataclass(frozen=True)
class MissingSource:
    requested_source_id: str
    subject_id: str | None
    reason: str
    impact: str

    def to_json_dict(self) -> dict[str, Any]:
        return {"requested_source_id": self.requested_source_id,
                "subject_id": self.subject_id, "reason": self.reason,
                "impact": self.impact}


@dataclass(frozen=True)
class ContextBudget:
    source_budget: int = DEFAULT_SOURCE_BUDGET
    per_source_budget: int = DEFAULT_PER_SOURCE_BUDGET
    per_excerpt_budget: int = DEFAULT_PER_EXCERPT_BUDGET
    consumed: int = 0
    truncated: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {"source_budget": self.source_budget,
                "per_source_budget": self.per_source_budget,
                "per_excerpt_budget": self.per_excerpt_budget,
                "consumed": self.consumed, "truncated": self.truncated}

    def settings(self) -> dict[str, Any]:
        return {"source_budget": self.source_budget,
                "per_source_budget": self.per_source_budget,
                "per_excerpt_budget": self.per_excerpt_budget}


@dataclass(frozen=True)
class SituationContext:
    schema_version: int
    context_id: str
    question: str | None
    scope: dict[str, Any]
    subjects: tuple[SituationSubject, ...]
    sources: tuple[SituationSource, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    deterministic_facts: tuple[DeterministicFact, ...]
    missing_sources: tuple[MissingSource, ...]
    limitations: tuple[str, ...]
    budget: ContextBudget
    domain_payload: dict[str, Any]
    provenance: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "question": self.question,
            "scope": dict(self.scope),
            "subjects": [s.to_json_dict() for s in self.subjects],
            "sources": [s.to_json_dict() for s in self.sources],
            "evidence_refs": [r.to_json_dict() for r in self.evidence_refs],
            "deterministic_facts": [f.to_json_dict() for f in self.deterministic_facts],
            "missing_sources": [m.to_json_dict() for m in self.missing_sources],
            "limitations": list(self.limitations),
            "budget": self.budget.to_json_dict(),
            "domain_payload": self.domain_payload,
            "provenance": dict(self.provenance),
        }


class ContextAssemblyError(ValueError):
    """Context 無法以可重現的方式組成——fail closed，不產生半份 context。"""


def assemble_situation_context(
    *,
    question: str | None,
    scope_kind: str,
    subjects: list[SituationSubject],
    sources: list[SituationSource],
    evidence_refs: list[EvidenceRef],
    deterministic_facts: list[DeterministicFact],
    missing_sources: list[MissingSource],
    limitations: list[str],
    domain_payload: dict[str, Any],
    budget: ContextBudget | None = None,
    created_at: str | None = None,
) -> SituationContext:
    """把 Package 已解析好的內容封裝成可重現的 context。

    Common **不**去找 source、不判斷哪一個 subject 是 baseline、也不理解
    `domain_payload` 的內容。Budget 依 evidence excerpt 的位元組數
    deterministic 截斷，超限即標記，絕不靜默丟棄。
    """
    if not subjects:
        raise ContextAssemblyError("a situation context needs at least one subject")
    subject_ids = {s.subject_id for s in subjects}
    if len(subject_ids) != len(subjects):
        raise ContextAssemblyError("duplicate subject_id")

    for source in sources:
        if source.status not in SOURCE_STATUSES:
            raise ContextAssemblyError(f"unknown source status: {source.status!r}")
        if source.subject_id not in subject_ids:
            raise ContextAssemblyError(
                f"source {source.source_id!r} points at unknown subject "
                f"{source.subject_id!r}")
        if source.status == "available" and not source.content_hash:
            raise ContextAssemblyError(
                f"source {source.source_id!r} is available but has no content hash")

    b = budget or ContextBudget()
    kept: list[EvidenceRef] = []
    per_source: dict[str, int] = {}
    consumed = 0
    truncated = False
    dropped: list[MissingSource] = []
    for ref in evidence_refs:
        size = len((ref.excerpt_text or "").encode("utf-8"))
        if size > b.per_excerpt_budget or \
                per_source.get(ref.source_id, 0) + size > b.per_source_budget or \
                consumed + size > b.source_budget:
            truncated = True
            dropped.append(MissingSource(
                requested_source_id=ref.source_id, subject_id=ref.subject_id,
                reason="context budget reached",
                impact="this excerpt is not available to the interpreter"))
            continue
        consumed += size
        per_source[ref.source_id] = per_source.get(ref.source_id, 0) + size
        kept.append(ref)

    known_ref_ids = {r.evidence_ref_id for r in kept}
    for fact in deterministic_facts:
        unknown = [i for i in fact.evidence_ref_ids if i not in known_ref_ids]
        if unknown:
            raise ContextAssemblyError(
                f"fact {fact.fact_id!r} references evidence not in this context: {unknown}")

    final_budget = ContextBudget(
        source_budget=b.source_budget, per_source_budget=b.per_source_budget,
        per_excerpt_budget=b.per_excerpt_budget, consumed=consumed, truncated=truncated)
    all_missing = list(missing_sources) + dropped

    identity = canonical_json({
        "schema_version": SITUATION_CONTEXT_SCHEMA_VERSION,
        "scope": {"kind": scope_kind, "subject_ids": sorted(subject_ids)},
        "normalized_question_hash": normalized_question_hash(question),
        "subjects": [s.to_json_dict() for s in subjects],
        "sources": [s.identity_for_context_id() for s in sources],
        "evidence_refs": [r.identity() for r in kept],
        "facts": [f.to_json_dict() for f in deterministic_facts],
        "missing_sources": [m.to_json_dict() for m in all_missing],
        "budget": final_budget.settings(),
        "domain_payload_sha256": sha256_text(canonical_json(domain_payload)),
    })

    return SituationContext(
        schema_version=SITUATION_CONTEXT_SCHEMA_VERSION,
        context_id=f"sitctx_{sha256_text(identity)[:32]}",
        question=question,
        scope={"kind": scope_kind, "subject_ids": [s.subject_id for s in subjects]},
        subjects=tuple(subjects), sources=tuple(sources),
        evidence_refs=tuple(kept), deterministic_facts=tuple(deterministic_facts),
        missing_sources=tuple(all_missing), limitations=tuple(limitations),
        budget=final_budget, domain_payload=dict(domain_payload),
        provenance={
            "assembler_version": ASSEMBLER_VERSION,
            "created_at": created_at,
            "identity_inputs": [
                "schema_version", "scope", "normalized_question_hash",
                "subject identities", "source identities and content hashes",
                "evidence ref identities", "deterministic facts", "missing sources",
                "budget settings", "domain_payload_sha256",
            ],
            "identity_excludes": ["created_at", "excerpt text bytes",
                                  "dict iteration order"],
        },
    )


__all__ = [
    "ASSEMBLER_VERSION",
    "ContextAssemblyError",
    "ContextBudget",
    "DEFAULT_PER_EXCERPT_BUDGET",
    "DEFAULT_PER_SOURCE_BUDGET",
    "DEFAULT_SOURCE_BUDGET",
    "DeterministicFact",
    "EvidenceRef",
    "MissingSource",
    "SCOPE_RUN_PAIR",
    "SITUATION_CONTEXT_SCHEMA_VERSION",
    "SOURCE_STATUSES",
    "SituationContext",
    "SituationSource",
    "SituationSubject",
    "assemble_situation_context",
    "canonical_json",
    "normalized_question_hash",
    "sha256_text",
]
