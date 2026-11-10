# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""把 ORFS run pair 組成 Common Context Snapshot。

這裡是 ORFS 這一側的 adapter：它知道 artifact role、report section、哪些
identity 欄位存在於哪個持久化來源；`terminux_engine.context` 完全不知道
這些，只負責 identity／hash／budget／missing 的共同封裝。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from terminux_engine.packages.orfs_run_intelligence import context_builder, evidence as ev
from terminux_engine.packages.orfs_run_intelligence.comparison import (
    ComparisonDecision,
    RunIdentity,
    controlled_dimensions_for_policy,
    validate_run_pair,
)
from terminux_engine.packages.orfs_run_intelligence.comparison_facts import (
    MetricComparison,
    compare_metrics,
    summarize,
)
from terminux_engine.packages.orfs_run_intelligence.dqe_reader import DQE_PACKAGE_ID, DqeRun

PURPOSE_COMPARE_RUNS = "orfs.compare_runs"

#: Comparison context 的 byte budget。比 single-run 大一點，因為要放兩側的
#: 節錄；仍然是硬上限，超過即截斷並登記。
MAX_TOTAL_CONTEXT_BYTES = 32000
MAX_PER_SOURCE_BYTES = 12000
MAX_PER_EXCERPT_BYTES = 4000

#: 這些 role 才會被讀成文字證據（與 single-run 一致）。
TEXT_ROLES = context_builder.TEXT_ROLES


def _boundary_coordinate(manifest: Any) -> dict[str, str | None]:
    """從 manifest 自己記錄的 output boundary 取出 platform／design／variant。

    這是 adapter **當時寫進 manifest** 的欄位，不是掃 filesystem 猜的；
    但它終究是路徑衍生資訊，所以 provenance 明確標示出來，讓下游知道它
    的可信度低於 receipt 裡的 identity。
    """
    for raw in getattr(manifest, "output_boundaries", None) or []:
        parts = Path(str(raw)).parts
        if len(parts) >= 4:
            return {"platform": parts[-3], "design": parts[-2], "flow_variant": parts[-1],
                    "provenance": "derived_from_manifest_output_boundaries"}
    return {"platform": None, "design": None, "flow_variant": None,
            "provenance": "unavailable"}


def _usable_verified_inputs(candidate: Any, run_id: str) -> Any:
    """只接受**這一筆 run 的**、形狀正確的 host 驗證結果。

    刻意用結構檢查而不是 isinstance：Package 不 import host 模組。但也刻意
    **不**接受任意 dict——必須帶 `run_id` 與 `files`，且 `run_id` 相符。
    """
    if candidate is None:
        return None
    run_attr = getattr(candidate, "run_id", None)
    files = getattr(candidate, "files", None)
    if run_attr != run_id or not isinstance(files, dict):
        return None
    return candidate


def _input_identity(inputs: Any, role: str) -> str | None:
    if inputs is None:
        return None
    entry = getattr(inputs, "files", {}).get(role)
    return getattr(entry, "sha256", None) if entry is not None else None


def build_run_identity(
    run: DqeRun,
    verified: ev.VerifiedManifest,
    *,
    receipt: dict[str, Any] | None = None,
    verified_inputs: Any = None,
) -> RunIdentity:
    """組出 comparability identity。

    `receipt` 是可選的：execution receipt 的 hashed content schema 不帶
    design config／RTL／SDC hash，所以只有 receipt 時這些欄位是 `None`——
    policy 會把它列進 unknowns，絕不默認兩側相同。

    `verified_inputs` 是 **host 端已驗證** 的 `VerifiedRunInputs`
    （由正式 `RunInputEvidenceReader` 從 ledger／task 事件／input manifest
    重建）。它**不是** provider 或 action arguments 可以填的參數，也不是
    「收到任意 JSON 就算 verified」——沒有 `run_id`／`files` 這些屬性的物件
    一律忽略，且 `run_id` 必須就是這一筆 run。有它才使用實際捕捉到的 RTL／
    design_config／SDC／scripts 身分；沒有就維持原本的 `None`。

    共用 receipt 的 schema、序列化與 content hash 完全沒有改動。
    """
    inputs = _usable_verified_inputs(verified_inputs, run.run_id)
    coord = _boundary_coordinate(verified.manifest)
    meta = (receipt or {}).get("adapter_metadata") or {}
    tve = (receipt or {}).get("tool_version_evidence") or {}
    return RunIdentity(
        run_id=run.run_id,
        step_id=run.step_id,
        attempt=run.attempt,
        design=meta.get("design") or coord["design"],
        platform=meta.get("platform") or coord["platform"],
        flow_variant=meta.get("variant") or coord["flow_variant"],
        design_config_relpath=meta.get("design_config_relpath"),
        design_config_sha256=(_input_identity(inputs, "design_config")
                              or (receipt or {}).get("design_config_sha256")),
        rtl_sha256=(_input_identity(inputs, "rtl")
                    or (receipt or {}).get("rtl_sha256")),
        sdc_sha256=(_input_identity(inputs, "sdc")
                    or (receipt or {}).get("sdc_sha256")),
        # commit 是來源資訊；判定用的是內容 digest。兩者分別取得，不互填。
        orfs_scripts_commit=(getattr(inputs, "source_revision", None)
                             or tve.get("orfs_scripts_commit")),
        orfs_scripts_digest=(getattr(inputs, "scripts_digest", None)
                             or tve.get("orfs_scripts_digest")),
        orfs_scripts_file_count=(getattr(inputs, "scripts_file_count", None)
                                 or tve.get("orfs_scripts_file_count")),
        docker_image_id=(getattr(inputs, "docker_image_id", None)
                         or tve.get("docker_image_id")),
        native_tool_binding_sha256=(getattr(inputs, "native_tool_binding_sha256", None)
                                    or tve.get("native_tool_binding_sha256")),
        execution_lifecycle_status=(
            getattr(inputs, "execution_lifecycle_status", None)
            or (receipt or {}).get("execution_lifecycle_status")),
        process_exit_code=(getattr(inputs, "process_exit_code", None)
                           if inputs is not None
                           else (receipt or {}).get("process_exit_code")),
        manifest_id=verified.manifest_id,
        manifest_sha256=verified.manifest_sha256,
        manifest_capture_status=verified.capture_status,
        manifest_schema_version=getattr(verified.manifest, "schema_version", None),
        dqe_package_id=DQE_PACKAGE_ID,
        dqe_row_count=len(run.rows),
        artifact_roles=tuple(sorted(verified.artifacts)),
    )


def _subject(role: str, identity: RunIdentity, verified: ev.VerifiedManifest) -> ContextSubject:
    return ContextSubject(
        role=role,
        subject_id=identity.run_id,
        run_identity={
            "run_id": identity.run_id, "step_id": identity.step_id,
            "attempt": identity.attempt,
            "execution_lifecycle_status": identity.execution_lifecycle_status,
        },
        manifest_identity={
            "manifest_id": verified.manifest_id,
            "manifest_sha256": verified.manifest_sha256,
            "capture_status": verified.capture_status,
        },
        coordinate={"design": identity.design, "platform": identity.platform,
                    "flow_variant": identity.flow_variant},
    )


def _sources_for(
    role: str, verified: ev.VerifiedManifest, sections: list[tuple[str, str, str]]
) -> tuple[list[ContextSourceRequest], list[ContextSource], list[MissingContext]]:
    """`sections` 是 (ref_id_suffix, adapter_role, section title)。"""
    requests: list[ContextSourceRequest] = []
    resolved: list[ContextSource] = []
    missing: list[MissingContext] = []
    by_role: dict[str, list[tuple[str, str]]] = {}
    for suffix, adapter_role, section in sections:
        by_role.setdefault(adapter_role, []).append((suffix, section))

    for adapter_role, wanted in by_role.items():
        source_id = f"{role}:{adapter_role}"
        requests.append(ContextSourceRequest(
            source_id=source_id, subject_role=role, kind="verified_artifact_text",
            locator={"adapter_role": adapter_role, "manifest_id": verified.manifest_id},
        ))
        artifact = verified.artifacts.get(adapter_role)
        if artifact is None:
            reason = verified.unreadable_roles.get(adapter_role, "not_in_manifest")
            resolved.append(ContextSource(
                source_id=source_id, subject_role=role, kind="verified_artifact_text",
                identity={"adapter_role": adapter_role, "manifest_id": verified.manifest_id},
                content_sha256=None,
                status="changed" if reason == "content_hash_drift" else "missing",
                status_reason=reason,
            ))
            missing.append(MissingContext(
                requested_source_id=source_id, subject_role=role, reason=reason,
                impact=f"no {adapter_role} evidence is available for the {role} run",
            ))
            continue
        refs: list[EvidenceRef] = []
        for suffix, section in wanted:
            single = context_builder._section_ref(
                f"ev_{role}_{suffix}", artifact, verified.manifest_id, section)
            refs.append(EvidenceRef(
                evidence_ref_id=single.evidence_ref_id, source_id=source_id,
                subject_role=role, locator=section,
                line_start=single.line_start, line_end=single.line_end,
                excerpt_sha256=single.excerpt_hash, excerpt_text=single.excerpt_text,
            ))
        resolved.append(ContextSource(
            source_id=source_id, subject_role=role, kind="verified_artifact_text",
            identity={"adapter_role": adapter_role, "manifest_id": verified.manifest_id,
                      "artifact_instance_id": artifact.artifact_instance_id},
            content_sha256=artifact.content_sha256,
            status="available", evidence_refs=tuple(refs),
        ))
    return requests, resolved, missing


def _comparison_sections(comparisons: list[MetricComparison]) -> list[tuple[str, str, str]]:
    """只為「真的有差異或不可比較」的 metric 取節錄，省 budget。"""
    wanted: list[tuple[str, str, str]] = []
    for index, comparison in enumerate(comparisons):
        if comparison.comparison == "unchanged":
            continue
        mapping = context_builder._CHECK_SECTIONS.get(comparison.metric_id)
        if mapping is None:
            continue
        role, section = mapping
        wanted.append((f"{index:02d}_{comparison.metric_id}", role, section))
    return wanted


@dataclass(frozen=True)
class ComparisonContext:
    snapshot: ContextSnapshot
    decision: ComparisonDecision
    comparisons: list[MetricComparison]


def build_comparison_context(
    *,
    question: str | None,
    baseline_run: DqeRun,
    candidate_run: DqeRun,
    baseline_verified: ev.VerifiedManifest,
    candidate_verified: ev.VerifiedManifest,
    baseline_receipt: dict[str, Any] | None = None,
    candidate_receipt: dict[str, Any] | None = None,
    baseline_verified_inputs: Any = None,
    candidate_verified_inputs: Any = None,
    declared_controlled_changes: tuple[str, ...] = (),
    comparison_policy_id: str | None = None,
    now: str | None = None,
    baseline_execution_evidence: Any = None,
    candidate_execution_evidence: Any = None,
) -> ComparisonContext:
    """`*_execution_evidence` 是 host Evidence Resolver 的判定；沒有就是 EXECUTION_UNVERIFIED。

    Execution Outcome 只進入 facts 與 `domain_payload`，**不**改變 comparability、
    metric deltas 或 selection eligibility 的既有判定。
    """
    from terminux_engine.packages.orfs_run_intelligence.context_builder import (
        execution_section,
        execution_statement,
    )

    executions = {"baseline": execution_section(baseline_execution_evidence),
                  "candidate": execution_section(candidate_execution_evidence)}
    baseline_identity = build_run_identity(
        baseline_run, baseline_verified, receipt=baseline_receipt,
        verified_inputs=baseline_verified_inputs)
    candidate_identity = build_run_identity(
        candidate_run, candidate_verified, receipt=candidate_receipt,
        verified_inputs=candidate_verified_inputs)
    decision = validate_run_pair(
        baseline_identity, candidate_identity,
        declared_controlled_changes=declared_controlled_changes,
        # 固定 policy 決定哪些維度可被標成受控；未知值退回預設集合。
        allowed_controlled_dimensions=controlled_dimensions_for_policy(
            comparison_policy_id),
    )
    comparisons = compare_metrics(baseline_run.rows, candidate_run.rows, decision)
    sections = _comparison_sections(comparisons)

    requests: list[ContextSourceRequest] = []
    resolved: list[ContextSource] = []
    missing: list[MissingContext] = []
    for role, verified in (("baseline", baseline_verified), ("candidate", candidate_verified)):
        r, s, m = _sources_for(role, verified, sections)
        requests.extend(r)
        resolved.extend(s)
        missing.extend(m)

    ref_by_metric: dict[str, list[str]] = {}
    for source in resolved:
        for ref in source.evidence_refs:
            metric = (ref.evidence_ref_id.split("_", 3) + [""])[3]
            if metric:
                ref_by_metric.setdefault(metric, []).append(ref.evidence_ref_id)

    facts: list[DeterministicFact] = []
    for role, run in (("baseline", baseline_run), ("candidate", candidate_run)):
        facts.append(DeterministicFact(
            fact_id=f"fact_{role}_execution_outcome", subject_role=role,
            category="execution",
            statement=execution_statement(f"the {role} run {run.run_id}", executions[role]),
            value=executions[role]["terminal_outcome"],
        ))
        facts.append(DeterministicFact(
            fact_id=f"fact_{role}_dqe_overall", subject_role=role, category="dqe",
            statement=f"The recorded DQE overall status for the {role} run "
                      f"{run.run_id} is {run.overall_status}.",
            value=run.overall_status,
        ))
    facts.append(DeterministicFact(
        fact_id="fact_comparability", subject_role=None, category="comparability",
        statement=f"The run pair is {decision.status} under policy "
                  f"{decision.policy_id} {decision.policy_version}.",
        value=decision.status,
    ))
    for comparison in comparisons:
        refs = tuple(ref_by_metric.get(comparison.metric_id, ()))
        facts.append(DeterministicFact(
            fact_id=f"fact_delta_{comparison.metric_id}", subject_role=None,
            category="metric_delta",
            statement=(
                f"{comparison.metric_id}: baseline {comparison.baseline_value}"
                + (f" {comparison.unit}" if comparison.unit else "")
                + f", candidate {comparison.candidate_value}"
                + (f" {comparison.unit}" if comparison.unit else "")
                + f" -> {comparison.comparison}"
                + (f" ({comparison.reason})" if comparison.reason else "")
                + "."
            ),
            value=comparison.delta, unit=comparison.unit, evidence_ref_ids=refs,
        ))

    request = ContextRequest(
        purpose=PURPOSE_COMPARE_RUNS,
        subjects=(
            _subject("baseline", baseline_identity, baseline_verified),
            _subject("candidate", candidate_identity, candidate_verified),
        ),
        source_requests=tuple(requests),
        budget=ContextBudget(
            total_bytes=MAX_TOTAL_CONTEXT_BYTES,
            per_source_bytes=MAX_PER_SOURCE_BYTES,
            per_excerpt_bytes=MAX_PER_EXCERPT_BYTES,
        ),
        selector_policy_ref={
            "policy_id": decision.policy_id,
            "policy_version": decision.policy_version,
            "decision_status": decision.status,
            "decision_digest": decision.digest(),
        },
        question=question,
    )
    snapshot = assemble_context(
        request, resolved,
        deterministic_facts=facts,
        domain_payload={
            "comparison_decision": decision.to_json_dict(),
            "metric_comparisons": [c.to_json_dict() for c in comparisons],
            "metric_comparison_summary": summarize(comparisons),
            "dqe": {
                "package_id": DQE_PACKAGE_ID,
                "baseline_overall_status": baseline_run.overall_status,
                "candidate_overall_status": candidate_run.overall_status,
            },
            "execution_outcomes": {role: _outcome_summary(execution)
                                   for role, execution in executions.items()},
        },
        missing_context=missing,
        now=now,
    )
    return ComparisonContext(snapshot=snapshot, decision=decision, comparisons=comparisons)


def _outcome_summary(execution: dict[str, Any]) -> dict[str, Any]:
    """兩側 Execution Outcome 的固定摘要（不含路徑、不含 receipt 原始 lifecycle 字串）。"""
    return {
        "terminal_outcome": execution["terminal_outcome"],
        "execution_verified": execution["execution_verified"],
        "lifecycle_source": execution["lifecycle_source"],
        "outcome_reasons": list(execution["outcome_reasons"]),
        "process_exit_code": execution["process_exit_code"],
        "timed_out": execution["timed_out"],
    }


__all__ = [
    "MAX_PER_EXCERPT_BYTES",
    "MAX_PER_SOURCE_BYTES",
    "MAX_TOTAL_CONTEXT_BYTES",
    "PURPOSE_COMPARE_RUNS",
    "ComparisonContext",
    "build_comparison_context",
    "build_run_identity",
]
