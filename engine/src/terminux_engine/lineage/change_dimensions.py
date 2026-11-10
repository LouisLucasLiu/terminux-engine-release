# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""差異是**資訊**，不是執行禁令——machine-readable change dimensions。

輸入是既有 DLE 的兩筆 `JobCommit`（baseline／candidate，或任兩個 task 的
run），輸出是既有的 `CheckResult`／`Finding`／`LineageStatus`：

* `Finding.category` 沿用既有字彙 `ANCHOR_MISMATCH`（rtl／sdc／upf 這三個
  既有 anchor role），並**加性**擴充非 anchor 維度的 category（見下）。
  沒有另造一套同義 enum。
* `CheckResult.status` 沿用既有 `LineageStatus`：
  - `CONSISTENT`：兩個 run 在相同 basis（constraint／library／toolchain／proof
    basis 都沒變；方法差異是**預期**的受控變更）。
  - `MIXED_LINEAGE`：至少一個 basis 維度改變——執行結果保留，但後續結論只能
    以「新條件下的結果」表述，不得冒充相同條件下的 repair。
  - `INSUFFICIENT_EVIDENCE`：任一側 lineage 不完整，比較無法確認。

這些判定**不會**阻止 user 執行任何 Profile；它們只限制 acceptance 能宣稱到哪裡。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.lineage.models import CheckResult, Finding, JobCommit
from terminux_engine.lineage.types import ANCHOR_KEYS

# ── 既有 category ──────────────────────────────────────────────────
CATEGORY_ANCHOR_MISMATCH = "ANCHOR_MISMATCH"
CATEGORY_ALL_INPUTS_ALIGNED = "ALL_INPUTS_ALIGNED"
# ── 加性擴充（見 models.Finding.category 的已知值清單）────────────
CATEGORY_LIBRARY_OR_CORNER_CHANGED = "LIBRARY_OR_CORNER_CHANGED"
CATEGORY_METHOD_CHANGED = "METHOD_CHANGED"
CATEGORY_TOOLCHAIN_CHANGED = "TOOLCHAIN_CHANGED"
CATEGORY_PROOF_BASIS_CHANGED = "PROOF_BASIS_CHANGED"
CATEGORY_OUTPUT_ONLY_CHANGED = "OUTPUT_ONLY_CHANGED"
CATEGORY_UNKNOWN_OR_INCOMPLETE_LINEAGE = "UNKNOWN_OR_INCOMPLETE_LINEAGE"

#: role → category。anchor role 用既有字彙；其餘用加性 category。
ROLE_CATEGORY: dict[str, str] = {
    "rtl": CATEGORY_ANCHOR_MISMATCH,
    "sdc": CATEGORY_ANCHOR_MISMATCH,
    "upf": CATEGORY_ANCHOR_MISMATCH,
    "liberty": CATEGORY_LIBRARY_OR_CORNER_CHANGED,
    "platform": CATEGORY_LIBRARY_OR_CORNER_CHANGED,
    "design_config": CATEGORY_METHOD_CHANGED,
    "method": CATEGORY_METHOD_CHANGED,
    "proof_definition": CATEGORY_PROOF_BASIS_CHANGED,
    "profile": CATEGORY_METHOD_CHANGED,
}

#: 這些維度一改，comparison basis 就不再相同。
BASIS_BREAKING: frozenset[str] = frozenset({
    CATEGORY_ANCHOR_MISMATCH, CATEGORY_LIBRARY_OR_CORNER_CHANGED,
    CATEGORY_TOOLCHAIN_CHANGED, CATEGORY_PROOF_BASIS_CHANGED,
})

#: 允許的表述。
CLAIM_SAME_CONDITIONS_REPAIR = "same_conditions_repair"
CLAIM_NEW_CONDITIONS_RESULT = "new_conditions_result"
CLAIM_CROSS_TOOLCHAIN_EXPERIMENT = "cross_toolchain_experiment"
CLAIM_NEW_PROOF_BASIS_RESULT = "new_proof_basis_result"
CLAIM_NOT_COMPARABLE = "not_comparable"

#: 這些 role 在 baseline／candidate 缺席時，lineage 視為不完整。
REQUIRED_ROLES_FOR_COMPARISON: tuple[str, ...] = ("design_config", "sdc")


def _by_role(commit: JobCommit) -> dict[str, dict[str, str]]:
    """role → {path: checksum}。同一 role 可有多個檔案（RTL）。"""
    grouped: dict[str, dict[str, str]] = {}
    for snap in commit.input_snapshots:
        grouped.setdefault(snap.role, {})[snap.path] = snap.checksum_sha256
    return grouped


def _evidence_by_kind(commit: JobCommit, kind: str) -> dict[str, str] | None:
    for item in commit.evidence:
        if item.get("kind") == kind:
            return dict(item)
    return None


@dataclass
class ChangeDimensions:
    """給 acceptance／view 用的結構化摘要。"""

    status: str
    changed: list[str] = field(default_factory=list)
    allowed_claim: str = CLAIM_NOT_COMPARABLE
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    @property
    def same_basis(self) -> bool:
        return self.status == "CONSISTENT"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "same_basis": self.same_basis,
            "changed_dimensions": list(self.changed),
            "allowed_claim": self.allowed_claim,
            "summary": self.summary,
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


def _role_findings(base: dict[str, dict[str, str]], cand: dict[str, dict[str, str]],
                   *, expected_method_roles: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    for role in sorted(set(base) | set(cand)):
        category = ROLE_CATEGORY.get(role)
        if category is None:
            continue
        left, right = base.get(role, {}), cand.get(role, {})
        # 以 checksum 集合比：路徑不同（隔離副本）但內容相同不算改變。
        if set(left.values()) == set(right.values()):
            continue
        if role in expected_method_roles:
            severity, status, note = "info", "CONSISTENT", "expected controlled change"
        elif category in BASIS_BREAKING:
            severity, status, note = "high", "MIXED_LINEAGE", "comparison basis changed"
        else:
            severity, status, note = "medium", "MIXED_LINEAGE", "input changed"
        findings.append(Finding(
            severity=severity, status=status,
            message=f"{role} differs between baseline and candidate ({note})",
            role=role, category=category, kind="flow_change_dimension",
            context={
                "dimension": category,
                "baseline_paths": ", ".join(sorted(left)),
                "candidate_paths": ", ".join(sorted(right)),
                "baseline_checksums": ", ".join(sorted(left.values())),
                "candidate_checksums": ", ".join(sorted(right.values())),
            }))
    return findings


def _evidence_findings(baseline: JobCommit, candidate: JobCommit) -> list[Finding]:
    findings: list[Finding] = []
    for kind, category, keys in (
            ("toolchain", CATEGORY_TOOLCHAIN_CHANGED,
             ("docker_image_id", "runner", "docker_executable", "host_env_script",
              "host_make_executable", "native_tool_binding_sha256")),
            ("proof_tool", CATEGORY_PROOF_BASIS_CHANGED,
             ("proof_policy_id", "qualified_tool_identity_path",
              "user_declared_executable", "user_declared_version_sha256"))):
        left = _evidence_by_kind(baseline, kind) or {}
        right = _evidence_by_kind(candidate, kind) or {}
        if not left and not right:
            continue
        changed = [k for k in keys if left.get(k) != right.get(k)]
        if not changed:
            continue
        findings.append(Finding(
            severity="high", status="MIXED_LINEAGE",
            message=f"{kind} identity differs between baseline and candidate: {changed}",
            role=kind, category=category, kind="flow_change_dimension",
            context={"dimension": category, "changed_keys": ", ".join(changed),
                     **{f"baseline_{k}": left.get(k, "") for k in changed},
                     **{f"candidate_{k}": right.get(k, "") for k in changed}}))
    return findings


def compare_flow_commits(
    baseline: JobCommit, candidate: JobCommit, *,
    expected_method_roles: tuple[str, ...] = ("design_config", "method", "profile"),
) -> ChangeDimensions:
    """回傳 change dimensions。**不**判斷品質，也不阻止任何執行。"""
    base, cand = _by_role(baseline), _by_role(candidate)
    incomplete = [
        role for role in REQUIRED_ROLES_FOR_COMPARISON
        if role not in base or role not in cand]
    if baseline.status != "committed" or candidate.status != "committed":
        incomplete.append(f"status:{baseline.status}/{candidate.status}")

    findings = _role_findings(base, cand, expected_method_roles=frozenset(expected_method_roles))
    findings += _evidence_findings(baseline, candidate)
    changed = sorted({str(f.category) for f in findings if f.category})

    if incomplete:
        findings.append(Finding(
            severity="medium", status="INSUFFICIENT_EVIDENCE",
            message=f"lineage is incomplete for comparison: {incomplete}",
            category=CATEGORY_UNKNOWN_OR_INCOMPLETE_LINEAGE, kind="flow_change_dimension",
            context={"dimension": CATEGORY_UNKNOWN_OR_INCOMPLETE_LINEAGE,
                     "missing": ", ".join(incomplete)}))
        return ChangeDimensions(
            status="INSUFFICIENT_EVIDENCE", changed=changed + [CATEGORY_UNKNOWN_OR_INCOMPLETE_LINEAGE],
            allowed_claim=CLAIM_NOT_COMPARABLE, findings=findings,
            summary="lineage incomplete; the comparison cannot be confirmed")

    basis_broken = [c for c in changed if c in BASIS_BREAKING]
    if not basis_broken:
        if not changed:
            findings.append(Finding(
                severity="info", status="CONSISTENT",
                message="all inputs aligned; no change dimension detected",
                category=CATEGORY_ALL_INPUTS_ALIGNED, kind="flow_change_dimension"))
        return ChangeDimensions(
            status="CONSISTENT", changed=changed,
            allowed_claim=CLAIM_SAME_CONDITIONS_REPAIR, findings=findings,
            summary=("same comparison basis; only the declared method changed"
                     if changed else "same comparison basis; no input changed"))

    if CATEGORY_TOOLCHAIN_CHANGED in basis_broken:
        claim = CLAIM_CROSS_TOOLCHAIN_EXPERIMENT
    elif CATEGORY_PROOF_BASIS_CHANGED in basis_broken:
        claim = CLAIM_NEW_PROOF_BASIS_RESULT
    else:
        claim = CLAIM_NEW_CONDITIONS_RESULT
    return ChangeDimensions(
        status="MIXED_LINEAGE", changed=changed, allowed_claim=claim, findings=findings,
        summary=f"comparison basis changed: {', '.join(basis_broken)}")


def to_check_result(dimensions: ChangeDimensions, *, run_file: str,
                    checked_stage: str = "flow_comparison") -> CheckResult:
    """投影成既有 `CheckResult`（給既有 reader／explainer 用）。"""
    anchors: dict[str, str | None] = {key: None for key in ANCHOR_KEYS}
    return CheckResult(
        run_file=run_file, checked_stage=checked_stage,
        target_anchors=anchors, status=dimensions.status,  # type: ignore[arg-type]
        findings=list(dimensions.findings), summary=dimensions.summary)


__all__ = [
    "BASIS_BREAKING",
    "CATEGORY_ALL_INPUTS_ALIGNED",
    "CATEGORY_ANCHOR_MISMATCH",
    "CATEGORY_LIBRARY_OR_CORNER_CHANGED",
    "CATEGORY_METHOD_CHANGED",
    "CATEGORY_OUTPUT_ONLY_CHANGED",
    "CATEGORY_PROOF_BASIS_CHANGED",
    "CATEGORY_TOOLCHAIN_CHANGED",
    "CATEGORY_UNKNOWN_OR_INCOMPLETE_LINEAGE",
    "CLAIM_CROSS_TOOLCHAIN_EXPERIMENT",
    "CLAIM_NEW_CONDITIONS_RESULT",
    "CLAIM_NEW_PROOF_BASIS_RESULT",
    "CLAIM_NOT_COMPARABLE",
    "CLAIM_SAME_CONDITIONS_REPAIR",
    "REQUIRED_ROLES_FOR_COMPARISON",
    "ROLE_CATEGORY",
    "ChangeDimensions",
    "compare_flow_commits",
    "to_check_result",
]
