# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Context Snapshot 共同格式。

抽象化原則：只有 explain_run single-run 與 compare_runs run-comparison **都**
實際需要的欄位才進 Common。只有一邊需要的（check 語意、metric 方向、節錄
選段、comparability 判定理由）一律留在 `domain_payload`。

Common 不得出現任何 vendor 名稱、metric 名稱、artifact 檔名或「哪個 run
比較新」的判斷。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

CONTEXT_SCHEMA_VERSION = 1

#: Subject 在一次 context 中扮演的角色。Common 只用它區分「哪個主體」，
#: 不賦予任何品質或時間順序含義——baseline 不代表比較舊，也不代表比較好。
SUBJECT_ROLES: frozenset[str] = frozenset(
    {"current", "baseline", "candidate", "reference"}
)

#: Source 的解析結果。`missing`／`changed`／`unsupported`／`truncated` 都是
#: 一等狀態，不是錯誤——Package 必須能誠實表達「這份證據拿不到」。
SOURCE_STATUSES: frozenset[str] = frozenset(
    {"available", "missing", "changed", "unsupported", "truncated"}
)

_WHITESPACE = re.compile(r"\s+")


def canonical_json(payload: Any) -> str:
    """穩定序列化：排序 key、無多餘空白、非 ASCII 不轉義。

    identity 一律建在這個函式之上，避免 dict 迭代順序造成同內容不同 id
   。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_question_hash(question: str | None) -> str:
    """問題的 normalized hash：去頭尾空白、壓縮內部空白、casefold。

    只做這三件事——不做翻譯、不做同義詞歸一、不做語言判斷。"""
    if not question:
        return sha256_text("")
    return sha256_text(_WHITESPACE.sub(" ", question.strip()).casefold())


@dataclass(frozen=True)
class EvidenceRef:
    """指向某個已驗證來源內某一段內容的可重算引用。

    與 explain_run 的 EvidenceRef 同形（相容）：任何一方都能重新讀出
    同一段內容並比對 `excerpt_sha256`。"""

    evidence_ref_id: str
    source_id: str
    subject_role: str
    locator: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    excerpt_sha256: str | None = None
    excerpt_text: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref_id": self.evidence_ref_id,
            "source_id": self.source_id,
            "subject_role": self.subject_role,
            "locator": self.locator,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "excerpt_sha256": self.excerpt_sha256,
            "excerpt_text": self.excerpt_text,
        }

    def identity(self) -> dict[str, Any]:
        """進 context_id 的部分——刻意不含 `excerpt_text` 本身，因為
        `excerpt_sha256` 已經唯一決定它。"""
        return {
            "evidence_ref_id": self.evidence_ref_id,
            "source_id": self.source_id,
            "subject_role": self.subject_role,
            "locator": self.locator,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "excerpt_sha256": self.excerpt_sha256,
        }


@dataclass(frozen=True)
class ContextSubject:
    """一次 context 裡的一個被分析主體。

    `run_identity` 與 `manifest_identity` 是 Package 已經解析好的 identity
    map；Common 只檢查它們存在且 canonical 化，不解讀欄位語意。"""

    role: str
    subject_id: str
    run_identity: dict[str, Any] = field(default_factory=dict)
    manifest_identity: dict[str, Any] | None = None
    coordinate: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "subject_id": self.subject_id,
            "run_identity": dict(self.run_identity),
            "manifest_identity": dict(self.manifest_identity) if self.manifest_identity else None,
            "coordinate": dict(self.coordinate) if self.coordinate else None,
        }

    def identity(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "subject_id": self.subject_id,
            "run_identity": dict(self.run_identity),
            "manifest_identity": dict(self.manifest_identity) if self.manifest_identity else None,
        }


@dataclass(frozen=True)
class ContextSourceRequest:
    """Package 明確指定要哪一份來源。

    Common **不會**自己去找來源：沒有出現在這裡的東西就不會進 context
    （不 scan cwd、不猜 latest run）。"""

    source_id: str
    subject_role: str
    kind: str
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSource:
    """Package 解析後的來源。Common 只驗證與記錄，不重新解析。"""

    source_id: str
    subject_role: str
    kind: str
    identity: dict[str, Any] = field(default_factory=dict)
    content_sha256: str | None = None
    status: str = "available"
    status_reason: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "subject_role": self.subject_role,
            "kind": self.kind,
            "identity": dict(self.identity),
            "content_sha256": self.content_sha256,
            "status": self.status,
            "status_reason": self.status_reason,
            "evidence_refs": [r.to_json_dict() for r in self.evidence_refs],
        }

    def identity_for_context_id(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "subject_role": self.subject_role,
            "kind": self.kind,
            "identity": dict(self.identity),
            "content_sha256": self.content_sha256,
            "status": self.status,
            "evidence_refs": [r.identity() for r in self.evidence_refs],
        }


@dataclass(frozen=True)
class ContextBudget:
    """Bounded context 的預算與實際消耗。

    Common 只數 bytes——它不知道哪一段比較重要。優先序由 Package 決定
    （它送進來的 source 順序就是優先序）。"""

    total_bytes: int
    per_source_bytes: int | None = None
    per_excerpt_bytes: int | None = None
    consumed_bytes: int = 0
    truncated: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "per_source_bytes": self.per_source_bytes,
            "per_excerpt_bytes": self.per_excerpt_bytes,
            "consumed_bytes": self.consumed_bytes,
            "truncated": self.truncated,
        }

    def settings(self) -> dict[str, Any]:
        """只有設定值進 context_id；consumed／truncated 是結果，會被
        settings 與 sources 共同決定，不需要再獨立參與 identity。"""
        return {
            "total_bytes": self.total_bytes,
            "per_source_bytes": self.per_source_bytes,
            "per_excerpt_bytes": self.per_excerpt_bytes,
        }


@dataclass(frozen=True)
class MissingContext:
    """明確登記「本來想要、但沒拿到」的來源。

    這是 Common 的一等公民，因為 explain_run 與 compare_runs 都必須讓下游誠實知道缺口
    （沒有 baseline evidence 就不能講「退步」）。"""

    requested_source_id: str
    subject_role: str
    reason: str
    impact: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "requested_source_id": self.requested_source_id,
            "subject_role": self.subject_role,
            "reason": self.reason,
            "impact": self.impact,
        }


@dataclass(frozen=True)
class DeterministicFact:
    """由程式（Package）算出、LLM 不得改寫的事實。"""

    fact_id: str
    subject_role: str | None
    category: str
    statement: str
    value: Any = None
    unit: str | None = None
    evidence_ref_ids: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject_role": self.subject_role,
            "category": self.category,
            "statement": self.statement,
            "value": self.value,
            "unit": self.unit,
            "evidence_ref_ids": list(self.evidence_ref_ids),
        }


@dataclass(frozen=True)
class ContextRequest:
    """Package 送給 Assembler 的完整請求。"""

    purpose: str
    subjects: tuple[ContextSubject, ...]
    source_requests: tuple[ContextSourceRequest, ...]
    budget: ContextBudget
    selector_policy_ref: dict[str, Any]
    question: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "normalized_question_hash": normalized_question_hash(self.question),
            "subjects": [s.to_json_dict() for s in self.subjects],
            "source_requests": [
                {"source_id": r.source_id, "subject_role": r.subject_role,
                 "kind": r.kind, "locator": dict(r.locator)}
                for r in self.source_requests
            ],
            "budget": self.budget.to_json_dict(),
            "selector_policy_ref": dict(self.selector_policy_ref),
        }


@dataclass(frozen=True)
class ContextSnapshot:
    """可重現的 Context Snapshot。

    `created_at` 保存但**不**參與 `context_id`。"""

    schema_version: int
    context_id: str
    purpose: str
    question: str | None
    normalized_question_hash: str
    subjects: tuple[ContextSubject, ...]
    sources: tuple[ContextSource, ...]
    deterministic_facts: tuple[DeterministicFact, ...]
    domain_payload: dict[str, Any]
    missing_context: tuple[MissingContext, ...]
    budget: ContextBudget
    provenance: dict[str, Any]
    created_at: str | None = None

    def evidence_refs(self) -> list[EvidenceRef]:
        return [ref for source in self.sources for ref in source.evidence_refs]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "purpose": self.purpose,
            "question": self.question,
            "normalized_question_hash": self.normalized_question_hash,
            "created_at": self.created_at,
            "subjects": [s.to_json_dict() for s in self.subjects],
            "sources": [s.to_json_dict() for s in self.sources],
            "deterministic_facts": [f.to_json_dict() for f in self.deterministic_facts],
            "domain_payload": self.domain_payload,
            "missing_context": [m.to_json_dict() for m in self.missing_context],
            "budget": self.budget.to_json_dict(),
            "provenance": dict(self.provenance),
        }


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "ContextBudget",
    "ContextRequest",
    "ContextSnapshot",
    "ContextSource",
    "ContextSourceRequest",
    "ContextSubject",
    "DeterministicFact",
    "EvidenceRef",
    "MissingContext",
    "SOURCE_STATUSES",
    "SUBJECT_ROLES",
    "canonical_json",
    "normalized_question_hash",
    "sha256_text",
]
