# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""把真實 run pair 組成 Common `SituationContext`。

ORFS 這一側的 adapter：它知道 artifact role、report section、DQE check 語意
與 metric 方向；`terminux_engine.situation` 一律不知道。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from terminux_engine.situation.context import (
    ContextBudget,
    DeterministicFact,
    EvidenceRef,
    MissingSource,
    SCOPE_RUN_PAIR,
    SituationContext,
    SituationSource,
    SituationSubject,
    assemble_situation_context,
)
from terminux_engine.packages.orfs_run_intelligence import (
    comparison_facts as cf,
    context_builder,
    evidence as ev,
    situation_policy as policy,
)
from terminux_engine.packages.orfs_run_intelligence.comparison import (
    ComparisonDecision,
    validate_run_pair,
)
from terminux_engine.packages.orfs_run_intelligence.comparison_context import (
    build_run_identity,
)
from terminux_engine.packages.orfs_run_intelligence.dqe_reader import DQE_PACKAGE_ID, DqeRun

#: Receipt 目前不是 package 可 durable 查詢的來源（G-1）。缺它會限制
#: **full comparability**，但不影響 readiness——readiness 由 DQE 與 manifest
#: 支持的 deterministic blocker 決定。
RECEIPT_EVIDENCE_TYPE = "execution_receipt"
RECEIPT_REASON_NOT_QUERYABLE = "receipt_not_durably_queryable"
RECEIPT_REASON_UNVERIFIABLE = "receipt_identity_unverifiable"


def _receipt_evidence(role: str, run_id: str, receipt: dict[str, Any] | None
                      ) -> dict[str, Any] | None:
    """回傳一筆結構化 missing evidence；`None` 代表 receipt 已可用且可驗證。

    `exists` 不等於 `verified`：receipt 有值但 producer run id 對不上時，
    狀態是 `unverifiable`，不是 present/verified。
    """
    if receipt is None:
        return {"evidence_type": RECEIPT_EVIDENCE_TYPE, "subject_id": run_id,
                "status": "missing", "required_for_readiness": False,
                "required_for_full_comparability": True,
                "reason_code": RECEIPT_REASON_NOT_QUERYABLE,
                "detail": ("execution receipts are not durably queryable by a package "
                           "at run time (G-1)")}
    if receipt.get("run_id") not in (None, run_id):
        return {"evidence_type": RECEIPT_EVIDENCE_TYPE, "subject_id": run_id,
                "status": "unverifiable", "required_for_readiness": False,
                "required_for_full_comparability": True,
                "reason_code": RECEIPT_REASON_UNVERIFIABLE,
                "detail": ("the supplied receipt does not identify this run; it is not "
                           "treated as verified")}
    return None

SUBJECT_CURRENT = "current"
SUBJECT_BASELINE = "baseline"

TEXT_ROLES = context_builder.TEXT_ROLES


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SituationAssembly:
    context: SituationContext
    decision: ComparisonDecision
    readiness: policy.ReadinessDecision
    comparisons: list[cf.MetricComparison]
    #: Comparability 在**考慮 receipt 可用性之後**的最終分級。
    effective_comparability: str = "comparable"
    #: 結構化 missing evidence（每個 subject 各一筆，不合併）。
    missing_evidence: tuple[dict[str, Any], ...] = ()


def _dqe_source(role: str, run: DqeRun) -> SituationSource:
    rows = [{"check_id": r["DQE_ID"], "value": r.get("VALUE"), "unit": r.get("UNIT"),
             "semantic_status": r["semantic_status"],
             "semantic_status_source": r["semantic_status_source"]}
            for r in run.rows]
    return SituationSource(
        source_id=f"{role}:dqe", source_type="dqe_records", subject_id=role,
        status="available" if rows else "missing",
        identity={"package_id": DQE_PACKAGE_ID, "orchestration_run_id": run.run_id,
                  "row_count": len(rows)},
        content_hash=_sha(str(sorted((r["check_id"], r["value"], r["semantic_status"])
                                     for r in rows))) if rows else None,
        excerpt_or_structured_data=rows,
    )


def _artifact_source(role: str, verified: ev.VerifiedManifest) -> SituationSource:
    return SituationSource(
        source_id=f"{role}:artifact_manifest", source_type="artifact_manifest",
        subject_id=role, status="available",
        identity={"manifest_id": verified.manifest_id,
                  "capture_status": verified.capture_status,
                  "verified_roles": sorted(verified.artifacts),
                  "rejected_roles": dict(sorted(verified.unreadable_roles.items()))},
        content_hash=verified.manifest_sha256,
        excerpt_or_structured_data={
            "current_content_verified": {
                r: a.current_content_verified for r, a in verified.artifacts.items()},
        },
    )


def build_situation_context(
    *,
    question: str | None,
    current_run: DqeRun,
    baseline_run: DqeRun,
    current_verified: ev.VerifiedManifest,
    baseline_verified: ev.VerifiedManifest,
    current_receipt: dict[str, Any] | None = None,
    baseline_receipt: dict[str, Any] | None = None,
    declared_controlled_changes: tuple[str, ...] = (),
    created_at: str | None = None,
) -> SituationAssembly:
    current_identity = build_run_identity(current_run, current_verified,
                                          receipt=current_receipt)
    baseline_identity = build_run_identity(baseline_run, baseline_verified,
                                           receipt=baseline_receipt)
    decision = validate_run_pair(
        baseline_identity, current_identity,
        declared_controlled_changes=declared_controlled_changes)
    comparisons = cf.compare_metrics(baseline_run.rows, current_run.rows, decision)
    readiness = policy.decide_readiness(
        current_rows=current_run.rows, required_sources_available=True)

    # ── receipt 可用性 → 影響 comparability，**不**影響 readiness ─────
    missing_evidence = tuple(
        e for e in (
            _receipt_evidence(SUBJECT_CURRENT, current_run.run_id, current_receipt),
            _receipt_evidence(SUBJECT_BASELINE, baseline_run.run_id, baseline_receipt),
        ) if e is not None)
    effective_comparability = decision.status
    if missing_evidence and effective_comparability == "comparable":
        # 沒有雙側可驗證的 receipt 就不得標成 comparable。
        effective_comparability = "conditionally_comparable"

    subjects = [
        SituationSubject(subject_id=SUBJECT_CURRENT, subject_type="run",
                         run_id=current_run.run_id,
                         manifest_id=current_verified.manifest_id,
                         artifact_identity={
                             "manifest_sha256": current_verified.manifest_sha256,
                             "capture_status": current_verified.capture_status,
                             "flow_variant": current_identity.flow_variant}),
        SituationSubject(subject_id=SUBJECT_BASELINE, subject_type="run",
                         run_id=baseline_run.run_id,
                         manifest_id=baseline_verified.manifest_id,
                         artifact_identity={
                             "manifest_sha256": baseline_verified.manifest_sha256,
                             "capture_status": baseline_verified.capture_status,
                             "flow_variant": baseline_identity.flow_variant}),
    ]
    sources = [
        _dqe_source(SUBJECT_CURRENT, current_run),
        _dqe_source(SUBJECT_BASELINE, baseline_run),
        _artifact_source(SUBJECT_CURRENT, current_verified),
        _artifact_source(SUBJECT_BASELINE, baseline_verified),
    ]

    # ── evidence：只為「有變化或是 blocker」的 check 取節錄，省 budget ──
    refs: list[EvidenceRef] = []
    missing: list[MissingSource] = []
    blocker_ids = {b["check_id"] for b in readiness.blockers}
    changed_ids = {c.metric_id for c in comparisons if c.comparison != cf.UNCHANGED}
    wanted = sorted(blocker_ids | changed_ids)

    for role, verified in ((SUBJECT_CURRENT, current_verified),
                           (SUBJECT_BASELINE, baseline_verified)):
        for index, check_id in enumerate(wanted):
            mapping = policy.SITUATION_SECTIONS.get(check_id)
            if mapping is None:
                continue
            adapter_role, section = mapping
            artifact = verified.artifacts.get(adapter_role)
            if artifact is None:
                reason = verified.unreadable_roles.get(adapter_role, "not_in_manifest")
                missing.append(MissingSource(
                    requested_source_id=f"{role}:{adapter_role}", subject_id=role,
                    reason=reason,
                    impact=f"no {adapter_role} evidence for the {role} run"))
                continue
            single = context_builder._section_ref(
                f"ev_{role}_{index:02d}_{check_id}", artifact, verified.manifest_id, section)
            refs.append(EvidenceRef(
                evidence_ref_id=single.evidence_ref_id, subject_id=role,
                source_id=f"{role}:{adapter_role}", source_type="verified_artifact_text",
                artifact_identity=single.artifact_instance_id,
                artifact_hash=single.content_sha256, section=section,
                line_start=single.line_start, line_end=single.line_end,
                excerpt_hash=single.excerpt_hash, excerpt_text=single.excerpt_text))

    # DQE 結構化 evidence：用 record identity ＋ row identity，不偽造 line range。
    for role, run in ((SUBJECT_CURRENT, current_run), (SUBJECT_BASELINE, baseline_run)):
        for check_id in wanted:
            row = next((r for r in run.rows if r["DQE_ID"] == check_id), None)
            if row is None:
                continue
            refs.append(EvidenceRef(
                evidence_ref_id=f"ev_{role}_dqe_{check_id}", subject_id=role,
                source_id=f"{role}:dqe", source_type="dqe_records",
                record_identity=f"{DQE_PACKAGE_ID}:{run.run_id}",
                row_identity=check_id, excerpt_hash=None, excerpt_text=""))

    ref_by = {(r.subject_id, r.source_type, r.section or r.row_identity): r
              for r in refs}

    def refs_for(check_id: str) -> tuple[str, ...]:
        out = [r.evidence_ref_id for r in refs
               if r.row_identity == check_id
               or (r.section and policy.SITUATION_SECTIONS.get(check_id, ("", ""))[1]
                   == r.section)]
        return tuple(dict.fromkeys(out))

    # ── deterministic facts ─────────────────────────────────────────
    facts: list[DeterministicFact] = [
        DeterministicFact(
            fact_id="fact_current_identity", subject_id=SUBJECT_CURRENT,
            category="identity",
            text=(f"The current run is {current_run.run_id} with artifact manifest "
                  f"{current_verified.manifest_id}.")),
        DeterministicFact(
            fact_id="fact_baseline_identity", subject_id=SUBJECT_BASELINE,
            category="identity",
            text=(f"The baseline run is {baseline_run.run_id} with artifact manifest "
                  f"{baseline_verified.manifest_id}.")),
        DeterministicFact(
            fact_id="fact_current_dqe_overall", subject_id=SUBJECT_CURRENT,
            category="dqe",
            text=f"The current run's recorded DQE overall status is "
                 f"{current_run.overall_status}.",
            value=current_run.overall_status),
        DeterministicFact(
            fact_id="fact_baseline_dqe_overall", subject_id=SUBJECT_BASELINE,
            category="dqe",
            text=f"The baseline run's recorded DQE overall status is "
                 f"{baseline_run.overall_status}.",
            value=baseline_run.overall_status),
        DeterministicFact(
            fact_id="fact_comparability", subject_id=None, category="comparability",
            text=(f"The run pair is {effective_comparability} under policy "
                  f"{decision.policy_id} {decision.policy_version} with "
                  f"{len(decision.controlled_changes)} declared controlled change(s)"
                  + (f"; {len(missing_evidence)} execution receipt(s) could not be "
                     f"verified, which limits full comparability"
                     if missing_evidence else "")
                  + "."),
            value=effective_comparability),
    ]
    for comparison in comparisons:
        if comparison.comparison == cf.UNCHANGED:
            continue
        facts.append(DeterministicFact(
            fact_id=f"fact_delta_{comparison.metric_id}", subject_id=None,
            category="metric_delta",
            text=(f"{comparison.metric_id}: baseline {comparison.baseline_value}"
                  + (f" {comparison.unit}" if comparison.unit else "")
                  + f", current {comparison.candidate_value}"
                  + (f" {comparison.unit}" if comparison.unit else "")
                  + f" -> {comparison.comparison}"
                  + (f" (status comparison {comparison.status_comparison})"
                     if comparison.status_comparison != cf.STATUS_COMPARABLE else "")
                  + "."),
            value=comparison.delta, unit=comparison.unit,
            evidence_ref_ids=refs_for(comparison.metric_id)))
    for blocker in readiness.blockers:
        facts.append(DeterministicFact(
            fact_id=f"fact_blocker_{blocker['check_id']}", subject_id=SUBJECT_CURRENT,
            category="blocker", text=policy.describe_blocker(blocker),
            value=blocker.get("value"), unit=blocker.get("unit"),
            evidence_ref_ids=refs_for(blocker["check_id"])))
    facts.append(DeterministicFact(
        fact_id="fact_readiness_base", subject_id=SUBJECT_CURRENT, category="readiness",
        text=(f"Based on the recorded DQE results the current run is "
              f"{readiness.status}; this is not a formal signoff decision."),
        value=readiness.status))

    limitations = [
        "This is a read-only assessment of two already-completed runs; nothing was "
        "executed or modified.",
        "Only the metrics recorded by the DQE package are compared.",
        "This covers one design and one run pair; it is not a block, multi-corner or "
        "milestone signoff assessment.",
        "Readiness here is derived from recorded DQE results and is not authoritative.",
    ]
    if decision.unknowns:
        limitations.append(
            "Some comparability dimensions could not be established: "
            + "; ".join(decision.unknowns))
    if missing_evidence:
        limitations.append(
            "Execution receipts could not be verified for "
            f"{len(missing_evidence)} of the two runs, so this pair is "
            f"{effective_comparability} rather than fully comparable. This limits "
            "cross-run claims; it does not change the recorded DQE blockers.")

    context = assemble_situation_context(
        question=question, scope_kind=SCOPE_RUN_PAIR, subjects=subjects,
        sources=sources, evidence_refs=refs, deterministic_facts=facts,
        missing_sources=missing, limitations=limitations,
        domain_payload={
            "comparison_decision": decision.to_json_dict(),
            "effective_comparability": effective_comparability,
            "missing_evidence": [dict(e) for e in missing_evidence],
            "metric_comparisons": [c.to_json_dict() for c in comparisons],
            "metric_comparison_summary": cf.summarize(comparisons),
            "readiness": readiness.to_json_dict(),
            "dqe": {"package_id": DQE_PACKAGE_ID,
                    "current_overall_status": current_run.overall_status,
                    "baseline_overall_status": baseline_run.overall_status},
        },
        budget=ContextBudget(), created_at=created_at,
    )
    return SituationAssembly(context=context, decision=decision, readiness=readiness,
                             comparisons=comparisons,
                             effective_comparability=effective_comparability,
                             missing_evidence=missing_evidence)


__all__ = ["RECEIPT_EVIDENCE_TYPE", "RECEIPT_REASON_NOT_QUERYABLE",
           "RECEIPT_REASON_UNVERIFIABLE", "SUBJECT_BASELINE", "SUBJECT_CURRENT",
           "SituationAssembly", "build_situation_context"]
