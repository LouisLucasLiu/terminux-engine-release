# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""explain_run single-run Context → Common Context Snapshot envelope。

刻意做成**單向 adapter**，而不是改寫 explain_run：

- `context_builder.build_context()` 一行未改，explain_run 的 `explain_run` response
  schema、`context_id`、`insight_id` 與既有 records 全部不變。
- 這裡只是把同一份內容重新封裝成共同 envelope，證明兩個案例確實共用同一
  組欄位。
- explain_run 自己的 `context_id` 以 `legacy_context_id` 保留在 domain payload 與
  provenance 裡，任何既有引用都還查得到。
"""

from __future__ import annotations

from terminux_engine.context import (
    ContextBudget,
    ContextRequest,
    ContextSnapshot,
    ContextSource,
    ContextSourceRequest,
    ContextSubject,
    DeterministicFact,
    EvidenceRef,
    MissingContext,
    assemble_context,
)
from terminux_engine.packages.orfs_run_intelligence.context_builder import (
    ContextSnapshot as SingleRunSnapshot,
)

PURPOSE_EXPLAIN_RUN = "orfs.explain_run"


def single_run_context_envelope(snapshot: SingleRunSnapshot) -> ContextSnapshot:
    """把 explain_run 的 single-run snapshot 包成 Common envelope。

    Subject role 是 `current`——單一 run 沒有 baseline／candidate 之分。
    """
    manifest = snapshot.artifact_manifest
    subject = ContextSubject(
        role="current",
        subject_id=str(snapshot.producer.get("run_id")),
        run_identity={
            "run_id": snapshot.producer.get("run_id"),
            "step_id": snapshot.producer.get("step_id"),
            "attempt": snapshot.producer.get("attempt"),
            "execution_lifecycle_status": snapshot.execution.get("lifecycle_status"),
        },
        manifest_identity={
            "manifest_id": manifest.get("manifest_id"),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "capture_status": manifest.get("capture_status"),
        },
    )

    by_role: dict[str, list[EvidenceRef]] = {}
    hashes: dict[str, str | None] = {}
    instances: dict[str, str] = {}
    for ref in snapshot.evidence_refs:
        by_role.setdefault(ref.adapter_role, []).append(EvidenceRef(
            evidence_ref_id=ref.evidence_ref_id,
            source_id=f"current:{ref.adapter_role}",
            subject_role="current",
            locator=ref.section,
            line_start=ref.line_start,
            line_end=ref.line_end,
            excerpt_sha256=ref.excerpt_hash,
            excerpt_text=ref.excerpt_text,
        ))
        hashes.setdefault(ref.adapter_role, ref.content_sha256)
        instances.setdefault(ref.adapter_role, ref.artifact_instance_id)

    requests: list[ContextSourceRequest] = []
    sources: list[ContextSource] = []
    for adapter_role, refs in by_role.items():
        source_id = f"current:{adapter_role}"
        requests.append(ContextSourceRequest(
            source_id=source_id, subject_role="current", kind="verified_artifact_text",
            locator={"adapter_role": adapter_role,
                     "manifest_id": manifest.get("manifest_id")},
        ))
        sources.append(ContextSource(
            source_id=source_id, subject_role="current", kind="verified_artifact_text",
            identity={"adapter_role": adapter_role,
                      "manifest_id": manifest.get("manifest_id"),
                      "artifact_instance_id": instances.get(adapter_role)},
            content_sha256=hashes.get(adapter_role),
            status="available", evidence_refs=tuple(refs),
        ))

    rejected = (snapshot.source_verification or {}).get("rejected_roles") or {}
    missing: list[MissingContext] = []
    for adapter_role, reason in sorted(rejected.items()):
        source_id = f"current:{adapter_role}"
        requests.append(ContextSourceRequest(
            source_id=source_id, subject_role="current", kind="verified_artifact_text",
            locator={"adapter_role": adapter_role,
                     "manifest_id": manifest.get("manifest_id")},
        ))
        sources.append(ContextSource(
            source_id=source_id, subject_role="current", kind="verified_artifact_text",
            identity={"adapter_role": adapter_role,
                      "manifest_id": manifest.get("manifest_id")},
            content_sha256=None,
            status="changed" if reason == "content_hash_drift" else "missing",
            status_reason=reason,
        ))

    known_ref_ids = {r.evidence_ref_id for s in sources for r in s.evidence_refs}
    facts = [
        DeterministicFact(
            fact_id=f.id, subject_role="current", category=f.category,
            statement=f.statement, value=f.value, unit=f.unit,
            evidence_ref_ids=tuple(i for i in f.evidence_ref_ids if i in known_ref_ids),
        )
        for f in snapshot.facts
    ]

    request = ContextRequest(
        purpose=PURPOSE_EXPLAIN_RUN,
        subjects=(subject,),
        source_requests=tuple(requests),
        budget=ContextBudget(
            total_bytes=int(snapshot.budget.get("max_total_bytes", 0)),
            consumed_bytes=int(snapshot.budget.get("included_bytes", 0)),
            truncated=bool(snapshot.budget.get("truncated", False)),
        ),
        selector_policy_ref={
            "policy_id": "orfs_run_selector",
            "policy_version": "v1",
            "resolved_by": snapshot.selector.get("resolved_by"),
        },
        question=snapshot.question,
    )
    return assemble_context(
        request, sources,
        deterministic_facts=facts,
        domain_payload={
            "legacy_context_id": snapshot.context_id,
            "dqe": dict(snapshot.dqe),
            "producer": dict(snapshot.producer),
            "source_verification": dict(snapshot.source_verification),
            "unknowns": list(snapshot.unknowns),
        },
        missing_context=missing,
        now=snapshot.created_at,
    )


__all__ = ["PURPOSE_EXPLAIN_RUN", "single_run_context_envelope"]
