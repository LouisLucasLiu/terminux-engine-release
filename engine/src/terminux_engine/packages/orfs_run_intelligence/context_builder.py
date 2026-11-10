# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Bounded Context Snapshot 建立。

這裡是**唯一**理解 ORFS report 結構的地方：哪個 section 對應
哪個 check、哪一段值得節錄。Common code 完全不知道這些。

Deterministic facts 全部由本模組從 receipt／DQE／manifest 產生，LLM 只能
引用、不能覆寫。

Budget 優先序：mandatory failed checks → warn checks →
relevant warnings → 其他背景；超限即截斷並記錄。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from terminux_engine.orchestration.evidence_bundle import caller_execution_evidence
from terminux_engine.packages.orfs_run_intelligence import evidence as ev
from terminux_engine.packages.orfs_run_intelligence.dqe_reader import DqeRun
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    CONTEXT_TYPE,
    SCHEMA_VERSION,
    EvidenceRef,
    Fact,
    canonical_json,
    sha256_text,
)

#: 本 package 會讀的文字 artifact role（ORFS-specific，只存在這裡）。
TEXT_ROLES: tuple[str, ...] = (
    "orfs.finish_report",
    "orfs.route_drc_report",
    "orfs.finish_log",
)

#: check_id → (role, section title, 供 LLM 判讀的說明)。ORFS domain 知識。
_CHECK_SECTIONS: dict[str, tuple[str, str]] = {
    "orfs_setup_wns": ("orfs.finish_report", "finish report_wns"),
    "orfs_setup_tns": ("orfs.finish_report", "finish report_tns"),
    "orfs_setup_violation_count": ("orfs.finish_report", "finish setup_violation_count"),
    "orfs_hold_violation_count": ("orfs.finish_report", "finish hold_violation_count"),
    "orfs_max_slew_violation_count": ("orfs.finish_report", "finish max_slew_violation_count"),
    "orfs_max_fanout_violation_count": ("orfs.finish_report", "finish max_fanout_violation_count"),
    "orfs_max_cap_violation_count": ("orfs.finish_report", "finish max_cap_violation_count"),
    "orfs_route_drc_violation_count": ("orfs.route_drc_report", "violation type: markers"),
    "orfs_log_error_markers": ("orfs.finish_log", "[ERROR] markers"),
    "orfs_log_warning_markers": ("orfs.finish_log", "[WARNING] markers"),
}

#: 除了 check 對應的 section 外，額外納入的背景 section（最低優先序）。
_BACKGROUND_SECTIONS: tuple[tuple[str, str], ...] = (
    ("orfs.finish_report", "finish report_checks -path_delay max"),
    ("orfs.finish_report", "finish report_clock_skew"),
)


@dataclass
class ContextSnapshot:
    context_id: str
    created_at: str
    question: str
    selector: dict[str, Any]
    producer: dict[str, Any]
    execution: dict[str, Any]
    artifact_manifest: dict[str, Any]
    dqe: dict[str, Any]
    facts: list[Fact]
    evidence_refs: list[EvidenceRef]
    unknowns: list[str]
    budget: dict[str, Any]
    source_verification: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "context_type": CONTEXT_TYPE,
            "context_id": self.context_id,
            "created_at": self.created_at,
            "question": self.question,
            "selector": dict(self.selector),
            "producer": dict(self.producer),
            "execution": dict(self.execution),
            "artifact_manifest": dict(self.artifact_manifest),
            "dqe": dict(self.dqe),
            "facts": [f.to_json_dict() for f in self.facts],
            "evidence_refs": [r.to_json_dict() for r in self.evidence_refs],
            "unknowns": list(self.unknowns),
            "budget": dict(self.budget),
            "source_verification": dict(self.source_verification),
        }


#: 沒有 host Evidence Resolver 時的 Terminal Outcome。DQE 與 manifest 的存在
#: 都不能證明執行完成或成功。
_UNVERIFIED = "EXECUTION_UNVERIFIED"
_SUCCEEDED = "SUCCEEDED"
_NOT_SUCCEEDED_TERMINAL = frozenset({"FAILED", "TIMED_OUT", "INTERRUPTED"})


def execution_section(evidence: Any) -> dict[str, Any]:
    """Execution Outcome：只來自 Evidence Resolver，與 DQE（Engineering Result）分開。

    只採 Resolver 結果的 **caller 可見投影**（`caller_execution_evidence`）：其他 member 寫入
    同 run id 的 receipt 列的數量與由它派生的 reason 不進入 context、facts、prompt 或 action
    回應。Resolver 內部的完整判定（含 terminal outcome 對外人列的處理）不在這裡改變。
    """
    if evidence is None:
        return {
            "lifecycle_status": _UNVERIFIED,
            "terminal_outcome": _UNVERIFIED,
            "execution_verified": False,
            "process_exit_code": None,
            "timed_out": None,
            "lifecycle_source": "none",
            "outcome_reasons": ["execution_evidence_resolver_unavailable"],
            "evidence": None,
        }
    payload = caller_execution_evidence(evidence)
    receipt = payload.get("receipt") or {}
    outcome = payload["terminal_outcome"]
    return {
        # 欄位名沿用既有 `lifecycle_status`，值改為 Terminal Outcome，不再寫死。
        "lifecycle_status": outcome,
        "terminal_outcome": outcome,
        "execution_verified": outcome == _SUCCEEDED,
        "process_exit_code": receipt.get("process_exit_code"),
        "timed_out": receipt.get("timed_out"),
        "lifecycle_source": payload["lifecycle_source"],
        "outcome_reasons": list(payload["outcome_reasons"]),
        # 只放判定結果與身分；receipt／ledger 的原始 lifecycle 字串（例如只代表
        # dispatch 完成的 `completed`）不放進 context，避免被讀成執行成功。
        "evidence": {
            "axes": payload["axes"],
            "conflicts": list(payload["conflicts"]),
            "receipt_id": receipt.get("receipt_id"),
            "receipt_content_hash": receipt.get("content_hash"),
            "receipt_count": payload["receipt_count"],
            "untrusted_receipt_count": payload["untrusted_receipt_count"],
            "ledger_present": payload["ledger_status"] is not None,
        },
    }


def execution_statement(identity: str, execution: dict[str, Any]) -> str:
    """`fact_execution` 類事實的固定敘述。只有 SUCCEEDED 才寫成功。"""
    outcome = execution["terminal_outcome"]
    if outcome == _SUCCEEDED:
        return (f"Execution outcome of {identity} is SUCCEEDED: trusted terminal "
                "evidence records a finished step, process exit code 0 and no timeout.")
    reasons = ", ".join(execution["outcome_reasons"]) or "none recorded"
    if outcome in _NOT_SUCCEEDED_TERMINAL:
        # 已觀察到結束，但不是成功：DQE 只描述它留下的輸出。
        return (f"Execution outcome of {identity} is {outcome} (reasons: {reasons}). "
                "The execution did not succeed; recorded DQE rows describe its "
                "outputs only.")
    return (f"Execution outcome of {identity} is {outcome} (reasons: {reasons}). "
            "Recorded DQE rows and artifacts do not establish that this execution "
            "finished or that it was successful.")


def build_context(
    *,
    question: str,
    run: DqeRun,
    verified: ev.VerifiedManifest,
    requested_run_id: str | None,
    now: str | None = None,
    execution_evidence: Any = None,
) -> ContextSnapshot:
    """`execution_evidence` 是 host Evidence Resolver 的判定；沒有就是 EXECUTION_UNVERIFIED。"""
    manifest = verified.manifest
    producer = manifest.producer.to_json_dict()
    execution = execution_section(execution_evidence)

    facts: list[Fact] = []
    refs: list[EvidenceRef] = []
    unknowns: list[str] = []
    included_bytes = 0
    truncated = False
    seen_ranges: dict[tuple[str, int | None, int | None], str] = {}

    # ── 執行事實（來自 Evidence Resolver；LLM 不得改寫）──────────────
    facts.append(Fact(
        id="fact_execution",
        category="execution",
        statement=execution_statement(
            f"run {run.run_id} step {run.step_id} attempt {run.attempt}", execution),
        value=execution["terminal_outcome"],
    ))
    facts.append(Fact(
        id="fact_artifact_manifest",
        category="execution",
        statement=(f"The artifact manifest {verified.manifest_id} referenced by the "
                   f"DQE rows names run {run.run_id} as its producer."),
    ))
    facts.append(Fact(
        id="fact_capture",
        category="execution",
        statement=f"Artifact capture status is {verified.capture_status}.",
        value=verified.capture_status,
    ))

    overall = run.overall_status
    facts.append(Fact(
        id="fact_dqe_overall",
        category="dqe",
        statement=f"The recorded DQE overall status for this run is {overall}.",
        value=overall,
    ))

    # ── 依 budget 優先序排序 checks ────────────────────────────────────
    priority = {"fail": 0, "warn": 1, "unknown": 2, "legacy_ambiguous": 2, "pass": 3}
    ordered = sorted(run.rows, key=lambda r: (priority.get(r["semantic_status"], 4), r["DQE_ID"]))

    for index, row in enumerate(ordered):
        check_id = row["DQE_ID"]
        status = row["semantic_status"]
        raw_value = row.get("VALUE")
        value: float | int | str | None = raw_value if raw_value not in ("", None) else None
        if isinstance(value, str):
            try:
                value = float(value) if "." in value or "e" in value.lower() else int(value)
            except ValueError:
                pass

        if row["semantic_status_source"] == "legacy_ambiguous":
            unknowns.append(
                f"{check_id}: legacy DQE row cannot distinguish warn from unknown"
            )

        ref_ids: list[str] = []
        mapping = _CHECK_SECTIONS.get(check_id)
        if mapping is not None and not truncated:
            role, section = mapping
            artifact = verified.artifacts.get(role)
            if artifact is None:
                unknowns.append(
                    f"{check_id}: source artifact {role} unavailable "
                    f"({verified.unreadable_roles.get(role, 'not_in_manifest')})"
                )
            else:
                ref = _section_ref(f"ev_{index:02d}_{check_id}", artifact, verified.manifest_id, section)
                # 同一段內容（同 role、同 line range）只放進 context 一次；
                # 多個 check 共用同一個 evidence_ref_id，省 budget 也避免
                # 讓 LLM 看到重複片段。
                dedupe_key = (ref.adapter_role, ref.line_start, ref.line_end)
                existing = seen_ranges.get(dedupe_key)
                if existing is not None:
                    ref_ids.append(existing)
                else:
                    size = len(ref.excerpt_text.encode("utf-8"))
                    if included_bytes + size > ev.MAX_TOTAL_CONTEXT_BYTES:
                        truncated = True
                    else:
                        included_bytes += size
                        refs.append(ref)
                        ref_ids.append(ref.evidence_ref_id)
                        seen_ranges[dedupe_key] = ref.evidence_ref_id

        facts.append(Fact(
            id=f"fact_check_{check_id}",
            category="dqe_check",
            statement=(
                f"DQE check {check_id} is {status}"
                + (f" with observed value {raw_value}" if raw_value else " with no observed value")
                + (f" {row['UNIT']}" if row.get("UNIT") else "")
                + "."
            ),
            value=value,
            unit=row.get("UNIT") or None,
            evidence_ref_ids=ref_ids,
        ))

    # ── 背景 section（最低優先序）────────────────────────────────────
    for offset, (role, section) in enumerate(_BACKGROUND_SECTIONS):
        artifact = verified.artifacts.get(role)
        if artifact is None or truncated:
            continue
        ref = _section_ref(f"ev_bg_{offset:02d}", artifact, verified.manifest_id, section)
        dedupe_key = (ref.adapter_role, ref.line_start, ref.line_end)
        if dedupe_key in seen_ranges:
            continue
        size = len(ref.excerpt_text.encode("utf-8"))
        if included_bytes + size > ev.MAX_TOTAL_CONTEXT_BYTES:
            truncated = True
            continue
        included_bytes += size
        refs.append(ref)
        seen_ranges[dedupe_key] = ref.evidence_ref_id

    for role, reason in sorted(verified.unreadable_roles.items()):
        unknowns.append(f"artifact {role} could not be used: {reason}")
    if truncated:
        unknowns.append("context budget reached; some background evidence was omitted")

    # ── context id：由 canonical inputs 決定，created_at 不參與 ────────
    identity = canonical_json({
        "question": question.strip(),
        "run_id": run.run_id,
        "manifest_id": verified.manifest_id,
        "manifest_sha256": verified.manifest_sha256,
        "evidence_ref_ids": sorted(r.evidence_ref_id for r in refs),
        "fact_ids": sorted(f.id for f in facts),
        "terminal_outcome": execution["terminal_outcome"],
    })

    return ContextSnapshot(
        context_id=f"ctx_{sha256_text(identity)[:32]}",
        created_at=now or datetime.now(timezone.utc).isoformat(),
        question=question,
        selector={
            "requested_run_id": requested_run_id,
            "resolved_by": run.resolved_by,
        },
        producer=producer,
        execution=execution,
        artifact_manifest={
            "manifest_id": verified.manifest_id,
            "manifest_sha256": verified.manifest_sha256,
            "manifest_reference": verified.manifest_reference,
            "capture_status": verified.capture_status,
        },
        dqe={
            "package_id": "orfs_dqe",
            "policy_version": "v1",
            "overall_status": overall,
            "checks": [
                {
                    "check_id": r["DQE_ID"],
                    "status": r["semantic_status"],
                    "status_source": r["semantic_status_source"],
                    "legacy_status": r["STATUS"],
                    "value": r.get("VALUE"),
                    "unit": r.get("UNIT"),
                }
                for r in ordered
            ],
        },
        facts=facts,
        evidence_refs=refs,
        unknowns=unknowns,
        budget={
            "max_total_bytes": ev.MAX_TOTAL_CONTEXT_BYTES,
            "included_bytes": included_bytes,
            "truncated": truncated,
        },
        source_verification={
            "manifest_hash_matches": True,
            "artifact_hashes_match": bool(verified.artifacts)
            and all(a.current_content_verified for a in verified.artifacts.values()),
            "verified_roles": sorted(verified.artifacts),
            "rejected_roles": dict(sorted(verified.unreadable_roles.items())),
        },
    )


def _section_ref(
    ref_id: str, artifact: ev.VerifiedArtifact, manifest_id: str, section: str
) -> EvidenceRef:
    """找出 ORFS section 在檔案中的 1-based line range。

    `6_finish.rpt` 的結構是 `====` / title / `----` / body（見 `orfs_dqe`
    的 parser）。找不到 section 或檔案為空時，回傳沒有 line range 的 ref
    ——不虛構位置。
    """
    lines = artifact.lines
    if not lines:
        return ev.make_evidence_ref(
            ref_id, artifact, manifest_id, section=section, line_start=None, line_end=None
        )

    for index, line in enumerate(lines):
        if line.strip() != section:
            continue
        # 0-based：title 在 index，`----` 在 index+1，body 從 index+2 起。
        body_start = index + 2
        body_end = body_start
        while body_end < len(lines) and not lines[body_end].strip().startswith("===="):
            body_end += 1
        # 去掉 body 尾端的空行，讓 excerpt 精準對到內容。
        while body_end > body_start and not lines[body_end - 1].strip():
            body_end -= 1
        return ev.make_evidence_ref(
            ref_id, artifact, manifest_id,
            section=section, line_start=body_start + 1, line_end=body_end,
        )

    # 非 section 型（marker 計數類）：節錄檔頭作為可追溯位置。
    return ev.make_evidence_ref(
        ref_id, artifact, manifest_id,
        section=section, line_start=1, line_end=min(len(lines), ev.MAX_EXCERPT_LINES),
    )


__all__ = ["ContextSnapshot", "TEXT_ROLES", "build_context", "execution_section",
           "execution_statement"]
