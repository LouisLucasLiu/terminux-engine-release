# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Common Context Assembler。

職責邊界很窄，刻意如此：

- **會做**：驗證 subject／source identity 完整、套用 byte budget、決定
  truncation、登記 missing context、以 canonical content 算出可重現的
  `context_id`。
- **不會做**：找 run、判斷兩個 run 是否可比較、理解任何 metric／report
  section／artifact 檔名、判定改善方向、scan 檔案系統。

Package 必須把已經解析好的 subjects 與 sources 交進來；Assembler 只是把
它們封裝成共同格式（hard ownership rule）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from terminux_engine.context.contract import (
    CONTEXT_SCHEMA_VERSION,
    ContextBudget,
    ContextRequest,
    ContextSnapshot,
    ContextSource,
    DeterministicFact,
    EvidenceRef,
    MissingContext,
    SOURCE_STATUSES,
    SUBJECT_ROLES,
    canonical_json,
    normalized_question_hash,
    sha256_text,
)


class ContextAssemblyError(ValueError):
    """Context 無法以可重現的方式組成——fail closed，不產生半份 context。"""


def _excerpt_bytes(ref: EvidenceRef) -> int:
    return len((ref.excerpt_text or "").encode("utf-8"))


def assemble_context(
    request: ContextRequest,
    resolved_sources: list[ContextSource],
    *,
    deterministic_facts: list[DeterministicFact] | None = None,
    domain_payload: dict | None = None,
    missing_context: list[MissingContext] | None = None,
    now: str | None = None,
) -> ContextSnapshot:
    """把 Package 已解析的來源組成一份可重現的 Context Snapshot。

    `resolved_sources` 的順序即優先序：budget 用完之後的 evidence 會被
    截斷並記錄，Common 不重新排序（它不知道哪一段比較重要）。
    """
    subjects = tuple(request.subjects)
    if not subjects:
        raise ContextAssemblyError("a context needs at least one subject")

    seen_roles: set[str] = set()
    for subject in subjects:
        if subject.role not in SUBJECT_ROLES:
            raise ContextAssemblyError(f"unknown subject role: {subject.role!r}")
        if subject.role in seen_roles:
            raise ContextAssemblyError(f"duplicate subject role: {subject.role!r}")
        if not subject.subject_id:
            raise ContextAssemblyError(f"subject {subject.role!r} has no subject_id")
        if not subject.run_identity:
            raise ContextAssemblyError(f"subject {subject.role!r} has no run_identity")
        seen_roles.add(subject.role)

    requested = {r.source_id: r for r in request.source_requests}
    resolved_by_id: dict[str, ContextSource] = {}
    for source in resolved_sources:
        if source.source_id in resolved_by_id:
            raise ContextAssemblyError(f"duplicate source_id: {source.source_id!r}")
        if source.source_id not in requested:
            raise ContextAssemblyError(
                f"source {source.source_id!r} was never requested; the assembler "
                "never adds sources on its own"
            )
        if source.status not in SOURCE_STATUSES:
            raise ContextAssemblyError(
                f"source {source.source_id!r} has unknown status {source.status!r}"
            )
        if source.subject_role not in seen_roles:
            raise ContextAssemblyError(
                f"source {source.source_id!r} points at unknown subject role "
                f"{source.subject_role!r}"
            )
        if source.status == "available" and not source.content_sha256:
            raise ContextAssemblyError(
                f"source {source.source_id!r} is available but carries no content hash; "
                "an unverifiable source may not be presented as available"
            )
        resolved_by_id[source.source_id] = source

    budget = request.budget
    consumed = 0
    truncated = False
    kept: list[ContextSource] = []
    dropped: list[MissingContext] = []

    for source in resolved_sources:
        if source.status != "available":
            kept.append(source)
            continue
        per_source = 0
        refs: list[EvidenceRef] = []
        source_truncated = False
        for ref in source.evidence_refs:
            size = _excerpt_bytes(ref)
            if budget.per_excerpt_bytes is not None and size > budget.per_excerpt_bytes:
                source_truncated = True
                continue
            if budget.per_source_bytes is not None and per_source + size > budget.per_source_bytes:
                source_truncated = True
                continue
            if consumed + size > budget.total_bytes:
                source_truncated = True
                continue
            consumed += size
            per_source += size
            refs.append(ref)
        if source_truncated:
            truncated = True
        if refs or not source.evidence_refs:
            kept.append(
                ContextSource(
                    source_id=source.source_id, subject_role=source.subject_role,
                    kind=source.kind, identity=source.identity,
                    content_sha256=source.content_sha256,
                    status="truncated" if source_truncated else source.status,
                    status_reason=("context budget reached" if source_truncated
                                   else source.status_reason),
                    evidence_refs=tuple(refs),
                )
            )
        else:
            # 一段都放不進去 → 這份來源實質上不在 context 裡，據實登記。
            dropped.append(MissingContext(
                requested_source_id=source.source_id, subject_role=source.subject_role,
                reason="context budget reached before any excerpt could be included",
                impact="no evidence from this source is available to the interpreter",
            ))

    missing = list(missing_context or [])
    for source_id, req in requested.items():
        if source_id not in resolved_by_id:
            missing.append(MissingContext(
                requested_source_id=source_id, subject_role=req.subject_role,
                reason="requested source was not resolved",
                impact="any claim that depends on this source is unsupported",
            ))
    for source in kept:
        if source.status in ("missing", "changed", "unsupported"):
            missing.append(MissingContext(
                requested_source_id=source.source_id, subject_role=source.subject_role,
                reason=source.status_reason or source.status,
                impact="any claim that depends on this source is unsupported",
            ))
    missing.extend(dropped)

    final_budget = ContextBudget(
        total_bytes=budget.total_bytes,
        per_source_bytes=budget.per_source_bytes,
        per_excerpt_bytes=budget.per_excerpt_bytes,
        consumed_bytes=consumed,
        truncated=truncated,
    )

    facts = tuple(deterministic_facts or ())
    known_ref_ids = {r.evidence_ref_id for s in kept for r in s.evidence_refs}
    for fact in facts:
        unknown = [i for i in fact.evidence_ref_ids if i not in known_ref_ids]
        if unknown:
            raise ContextAssemblyError(
                f"fact {fact.fact_id!r} references evidence not in this context: {unknown}"
            )

    payload = dict(domain_payload or {})
    identity = canonical_json({
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "purpose": request.purpose,
        "normalized_question_hash": normalized_question_hash(request.question),
        "subjects": [s.identity() for s in subjects],
        "sources": [s.identity_for_context_id() for s in kept],
        "selector_policy_ref": dict(request.selector_policy_ref),
        "budget": final_budget.settings(),
        "domain_payload_sha256": sha256_text(canonical_json(payload)),
    })

    return ContextSnapshot(
        schema_version=CONTEXT_SCHEMA_VERSION,
        context_id=f"ctx_{sha256_text(identity)[:32]}",
        purpose=request.purpose,
        question=request.question,
        normalized_question_hash=normalized_question_hash(request.question),
        subjects=subjects,
        sources=tuple(kept),
        deterministic_facts=facts,
        domain_payload=payload,
        missing_context=tuple(missing),
        budget=final_budget,
        provenance={
            "assembler": "terminux_engine.context.assembler.assemble_context",
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "selector_policy_ref": dict(request.selector_policy_ref),
            "identity_inputs": [
                "schema_version", "purpose", "normalized_question_hash",
                "subject identities", "source identities and content hashes",
                "selector_policy_ref", "budget settings", "domain_payload_sha256",
            ],
            "identity_excludes": ["created_at", "absolute host paths", "excerpt text"],
        },
        created_at=now or datetime.now(timezone.utc).isoformat(),
    )


__all__ = ["ContextAssemblyError", "assemble_context"]
