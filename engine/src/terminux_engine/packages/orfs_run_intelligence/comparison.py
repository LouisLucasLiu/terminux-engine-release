# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS run comparability policy。

**只有這裡**知道兩個 ORFS run 在什麼條件下可以互相比較。Common Context
code 完全不參與這個判斷——它只保存決策結果。

Policy 的所有維度都取自已持久化的 identity（receipt／manifest／DQE），
不看 directory mtime、不看 FLOW_VARIANT 名稱是否相似。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_ID = "orfs_run_comparison"
POLICY_VERSION = "v1"

STATUS_COMPARABLE = "comparable"
STATUS_CONDITIONAL = "conditionally_comparable"
STATUS_NOT_COMPARABLE = "not_comparable"

#: 這些維度不同即 hard mismatch——兩次 run 量的不是同一件事，任何
#: 「改善／惡化」都沒有意義。
HARD_DIMENSIONS: tuple[str, ...] = (
    "design",
    "platform",
    "rtl_content",
)

#: 這些維度不同時，結論仍可能成立，但必須降級並明說理由。
#: ORFS 腳本的身分判定用**內容 digest**，不是 git commit。
#:
#: 驗收真正要問的是「兩次 run 跑的是不是同一份腳本」。commit 只是那件事的
#: 代理：working tree 可以髒、可以不在 git 下，commit 相同也不保證內容相同。
#: 因此判定欄位改為已驗證、且涵蓋實際執行腳本的內容 digest；commit 保留為
#: 來源資訊（provenance），兩者是不同欄位，永遠不互填。
#:
#: 涵蓋範圍不足（沒有 digest、或 digest 沒涵蓋任何檔案）時一律維持 unknown，
#: 不得因為改了欄位就讓 `identity_unknown` 自己消失。
SOFT_DIMENSIONS: tuple[str, ...] = (
    "tool_image",
    "orfs_scripts_content",
    "dqe_policy_package",
    "manifest_schema_version",
    "flow_completion",
    "manifest_capture_completeness",
    "artifact_role_availability",
)

#: 允許被宣告為 controlled change 的維度。宣告一個 controlled change 不會
#: 讓它從 mismatch 消失——它只是換一個更誠實的標籤。
CONTROLLED_DIMENSIONS: tuple[str, ...] = ("design_config",)

#: 固定 comparison policy → 該 policy 允許被標成 controlled 的維度。
#: **只有已知的固定 id 會放寬**，未知值一律退回預設集合，所以這不是一個
#: 「宣告什麼都能過」的開關。放寬的只有標籤，hash 與 refs 照實保留。
POLICY_CONTROLLED_DIMENSIONS: dict[str, tuple[str, ...]] = {
    # 候選的唯一 source 差異是一份已驗證的 RTL patch。
    "gcd.same_requirements_prefix_borrow.v1": ("design_config", "rtl_content"),
}

#: 被標成 controlled 的差異在 projection 裡的關係名稱。
RELATION_VERIFIED_DECLARED_CHANGE = "VERIFIED_DECLARED_CHANGE"


def controlled_dimensions_for_policy(policy_id: str | None) -> tuple[str, ...]:
    """依固定 policy 取允許集合。未知或缺值 → 預設集合。"""
    if not policy_id:
        return CONTROLLED_DIMENSIONS
    return POLICY_CONTROLLED_DIMENSIONS.get(str(policy_id), CONTROLLED_DIMENSIONS)


@dataclass(frozen=True)
class RunIdentity:
    """一次 ORFS run 的可比較性 identity。全部來自持久化欄位。"""

    run_id: str
    step_id: str | None
    attempt: int | None
    design: str | None
    platform: str | None
    flow_variant: str | None
    design_config_relpath: str | None
    design_config_sha256: str | None
    rtl_sha256: str | None
    sdc_sha256: str | None
    #: 來源資訊（provenance）；不參與身分判定。
    orfs_scripts_commit: str | None
    docker_image_id: str | None
    execution_lifecycle_status: str | None
    process_exit_code: int | None
    manifest_id: str | None
    manifest_sha256: str | None
    manifest_capture_status: str | None
    manifest_schema_version: int | None
    dqe_package_id: str | None
    dqe_row_count: int = 0
    artifact_roles: tuple[str, ...] = ()
    #: 判定欄位：已驗證腳本內容的 digest。與 commit 是不同欄位，不互填。
    orfs_scripts_digest: str | None = None
    #: 該 digest 實際涵蓋幾個檔案；0 或 None 代表涵蓋不足，維持 unknown。
    orfs_scripts_file_count: int | None = None
    #: host-native run 的工具身分（Profile 授權的 binding digest）。
    native_tool_binding_sha256: str | None = None

    @property
    def _tool_identity(self) -> str | None:
        """工具身分：舊 run 是 image id；host-native run 是 binding digest。"""
        if self.native_tool_binding_sha256:
            return f"native-binding:sha256:{self.native_tool_binding_sha256}"
        return self.docker_image_id

    @property
    def _orfs_scripts_identity(self) -> str | None:
        """腳本內容身分；涵蓋不足時回 `None`，由比較端記成 unknown。

        只有「已驗證且確實涵蓋到檔案」的 digest 才算身分。沒有 digest、或
        digest 宣稱涵蓋 0 個檔案，都代表我們並不知道兩次 run 是不是跑同一份
        腳本——那種情形必須維持 unknown，不得因為換了欄位就當成已建立。
        """
        digest = self.orfs_scripts_digest
        if not isinstance(digest, str) or not digest:
            return None
        count = self.orfs_scripts_file_count
        if not isinstance(count, int) or count <= 0:
            return None
        return digest

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "step_id": self.step_id, "attempt": self.attempt,
            "design": self.design, "platform": self.platform,
            "flow_variant": self.flow_variant,
            "design_config_relpath": self.design_config_relpath,
            "design_config_sha256": self.design_config_sha256,
            "rtl_sha256": self.rtl_sha256, "sdc_sha256": self.sdc_sha256,
            "orfs_scripts_commit": self.orfs_scripts_commit,
            "orfs_scripts_digest": self.orfs_scripts_digest,
            "orfs_scripts_file_count": self.orfs_scripts_file_count,
            "docker_image_id": self.docker_image_id,
            "execution_lifecycle_status": self.execution_lifecycle_status,
            "process_exit_code": self.process_exit_code,
            "manifest_id": self.manifest_id, "manifest_sha256": self.manifest_sha256,
            "manifest_capture_status": self.manifest_capture_status,
            "manifest_schema_version": self.manifest_schema_version,
            "dqe_package_id": self.dqe_package_id,
            "dqe_row_count": self.dqe_row_count,
            "artifact_roles": list(self.artifact_roles),
            **({"native_tool_binding_sha256": self.native_tool_binding_sha256}
               if self.native_tool_binding_sha256 else {}),
        }


@dataclass(frozen=True)
class ComparisonDecision:
    status: str
    policy_id: str
    policy_version: str
    matched_dimensions: tuple[str, ...]
    mismatches: tuple[dict[str, Any], ...]
    controlled_changes: tuple[dict[str, Any], ...]
    unknowns: tuple[str, ...]
    baseline_identity: dict[str, Any]
    candidate_identity: dict[str, Any]

    @property
    def allows_directional_conclusions(self) -> bool:
        """`not_comparable` 時任何有方向的 delta 結論都不得產生。"""
        return self.status in (STATUS_COMPARABLE, STATUS_CONDITIONAL)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "matched_dimensions": list(self.matched_dimensions),
            "mismatches": [dict(m) for m in self.mismatches],
            "controlled_changes": [dict(c) for c in self.controlled_changes],
            "unknowns": list(self.unknowns),
            "baseline_identity": dict(self.baseline_identity),
            "candidate_identity": dict(self.candidate_identity),
        }

    def digest(self) -> str:
        canonical = json.dumps(self.to_json_dict(), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_DIMENSION_FIELDS: dict[str, str] = {
    "design": "design",
    "platform": "platform",
    "rtl_content": "rtl_sha256",
    "design_config": "design_config_sha256",
    "tool_image": "_tool_identity",
    "orfs_scripts_content": "_orfs_scripts_identity",
    "dqe_policy_package": "dqe_package_id",
    "manifest_schema_version": "manifest_schema_version",
    "flow_completion": "execution_lifecycle_status",
    "manifest_capture_completeness": "manifest_capture_status",
}


def validate_run_pair(
    baseline: RunIdentity,
    candidate: RunIdentity,
    *,
    declared_controlled_changes: tuple[str, ...] = (),
    allowed_controlled_dimensions: tuple[str, ...] | None = None,
) -> ComparisonDecision:
    """判定兩次 ORFS run 是否可比較。

    `declared_controlled_changes` 讓呼叫端宣告「這個維度的差異是刻意的」。
    宣告只影響標籤與降級程度，**不會**讓 mismatch 從報告裡消失，也不會讓
    它變成 root cause。
    """
    matched: list[str] = []
    mismatches: list[dict[str, Any]] = []
    controlled: list[dict[str, Any]] = []
    unknowns: list[str] = []
    allowed = (CONTROLLED_DIMENSIONS if allowed_controlled_dimensions is None
               else tuple(allowed_controlled_dimensions))

    if baseline.run_id == candidate.run_id:
        return ComparisonDecision(
            status=STATUS_NOT_COMPARABLE, policy_id=POLICY_ID, policy_version=POLICY_VERSION,
            matched_dimensions=(), mismatches=({"dimension": "run_identity",
                                                "reason": "baseline and candidate are the same run"},),
            controlled_changes=(), unknowns=(),
            baseline_identity=baseline.to_json_dict(),
            candidate_identity=candidate.to_json_dict(),
        )

    for dimension, attr in _DIMENSION_FIELDS.items():
        left, right = getattr(baseline, attr), getattr(candidate, attr)
        if left is None or right is None:
            # Missing identity 一律不得默認相同。
            unknowns.append(
                f"{dimension}: identity is missing on "
                f"{'baseline' if left is None else 'candidate'}; not assumed equal"
            )
            continue
        if left == right:
            matched.append(dimension)
            continue
        entry = {"dimension": dimension, "baseline": left, "candidate": right}
        if dimension in declared_controlled_changes and dimension in allowed:
            # 標籤換成「已驗證的受控差異」，但兩側 hash 原樣留在 entry 裡。
            entry["input_relation"] = RELATION_VERIFIED_DECLARED_CHANGE
            controlled.append(entry)
        else:
            mismatches.append(entry)

    # SDC 是 constraint identity；它不在 _DIMENSION_FIELDS 是因為要和
    # design_config 一起被視為 constraint 面向，但仍必須逐項檢查。
    if baseline.sdc_sha256 is None or candidate.sdc_sha256 is None:
        unknowns.append("constraint_content: SDC identity is missing; not assumed equal")
    elif baseline.sdc_sha256 == candidate.sdc_sha256:
        matched.append("constraint_content")
    else:
        entry = {"dimension": "constraint_content",
                 "baseline": baseline.sdc_sha256, "candidate": candidate.sdc_sha256}
        if "constraint_content" in declared_controlled_changes:
            entry["input_relation"] = RELATION_VERIFIED_DECLARED_CHANGE
            controlled.append(entry)
        else:
            mismatches.append(entry)

    missing_roles = sorted(set(baseline.artifact_roles) ^ set(candidate.artifact_roles))
    if missing_roles:
        mismatches.append({"dimension": "artifact_role_availability",
                           "baseline": sorted(baseline.artifact_roles),
                           "candidate": sorted(candidate.artifact_roles),
                           "asymmetric_roles": missing_roles})
    else:
        matched.append("artifact_role_availability")

    hard = [m for m in mismatches if m["dimension"] in HARD_DIMENSIONS]
    if hard:
        status = STATUS_NOT_COMPARABLE
    elif mismatches or unknowns:
        status = STATUS_CONDITIONAL
    elif controlled:
        # 只有已宣告的 controlled change 不同：仍可比較，但這正是要解釋的差異。
        status = STATUS_COMPARABLE
    else:
        status = STATUS_COMPARABLE

    return ComparisonDecision(
        status=status, policy_id=POLICY_ID, policy_version=POLICY_VERSION,
        matched_dimensions=tuple(sorted(matched)),
        mismatches=tuple(mismatches), controlled_changes=tuple(controlled),
        unknowns=tuple(unknowns),
        baseline_identity=baseline.to_json_dict(),
        candidate_identity=candidate.to_json_dict(),
    )


# ── 由 comparability 派生的「可否進入自動選擇」判定 ──
#
# 這是**派生**判定：它不修改、不隱藏 `ComparisonDecision` 的任何欄位。
# comparability 的字串不是唯一 gate——因為已證實且已宣告的 controlled
# design_config change 而保留 conditional 標籤是可以接受的；因必要身分
# **缺失或矛盾**而 conditional 則不可自動選優。

SELECTION_OK = "ok"
SELECTION_NOT_COMPARABLE = "not_comparable"
SELECTION_IDENTITY_UNKNOWN = "identity_unknown"
SELECTION_UNEXPECTED_MISMATCH = "unexpected_mismatch"
SELECTION_UNDECLARED_CONTROLLED = "undeclared_controlled_change"
SELECTION_PROOF_NOT_ESTABLISHED = "formal_proof_not_established"

#: method 宣告需要 formal proof 時，只有這一組結果算「已成立」。缺失、無效、
#: TOOL_ERROR、TIMEOUT、UNPROVEN 與 NOT_EQUIVALENT 全部不算，且一律不得選用
#: candidate。`UNPROVEN` 特別列出：沒證出來就是沒證出來，不得因為「也沒證出
#: 不等價」而放行。
PROOF_ESTABLISHED_STATUS = "VERIFIED"
PROOF_ESTABLISHED_CLASSIFICATION = "EQUIVALENT"


@dataclass(frozen=True)
class SelectionEligibility:
    """候選是否具備「可被自動選擇」的證據品質。"""

    eligible: bool
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {"selection_eligible": self.eligible,
                "reason_codes": list(self.reason_codes),
                "reasons": list(self.reasons)}


def _proof_refusal(proof: Any) -> str | None:
    """method 宣告需要 formal proof 時，回傳不得選用的理由；成立則回 `None`。

    `proof` 只接受 host 由 durable envelope 推導後注入的事實，不接受呼叫端
    宣告。沒有宣告需要 proof 時這一項完全不參與判定，既有 template 的行為
    一個位元都不變。

    成立 = `status == VERIFIED` 且 `classification == EQUIVALENT`。其餘一律
    不得選用：缺失、無效、`TOOL_ERROR`、`TIMEOUT`、`UNPROVEN`、
    `NOT_EQUIVALENT` 都一樣。
    成立本身也只是**必要條件**——其他工程判定仍照常適用。
    """
    if not isinstance(proof, dict) or not proof.get("required"):
        return None
    status = str(proof.get("status") or "")
    classification = str(proof.get("classification") or "")
    if status == PROOF_ESTABLISHED_STATUS \
            and classification == PROOF_ESTABLISHED_CLASSIFICATION:
        return None
    detail = proof.get("reason")
    if status or classification:
        return (f"the method requires a formal equivalence proof and it is not "
                f"established: status={status or 'MISSING'}, "
                f"classification={classification or 'MISSING'}")
    return ("the method requires a formal equivalence proof and none is "
            f"established{f': {detail}' if detail else ''}")


def evaluate_selection_eligibility(
    decision: ComparisonDecision,
    *,
    allowed_controlled_dimensions: tuple[str, ...] = CONTROLLED_DIMENSIONS,
    proof: dict[str, Any] | None = None,
) -> SelectionEligibility:
    """只有「證據完整」且「差異都是已批准的那一個」才 eligible。

    method 宣告需要 formal proof 時，proof 未成立一律不得選用 candidate；
    proof 成立也只是必要條件，下面的 comparability 判定仍要全部通過。

    刻意不看 LLM 敘述，也不看 `status` 字串本身以外的任何啟發式。
    """
    codes: list[str] = []
    reasons: list[str] = []

    refusal = _proof_refusal(proof)
    if refusal is not None:
        codes.append(SELECTION_PROOF_NOT_ESTABLISHED)
        reasons.append(refusal)

    if decision.status == STATUS_NOT_COMPARABLE:
        codes.append(SELECTION_NOT_COMPARABLE)
        reasons.append("the two runs are not comparable under the policy")

    if decision.unknowns:
        codes.append(SELECTION_IDENTITY_UNKNOWN)
        reasons.extend(f"identity is not established: {u}" for u in decision.unknowns)

    for entry in decision.mismatches:
        codes.append(SELECTION_UNEXPECTED_MISMATCH)
        reasons.append(
            f"undeclared difference in {entry.get('dimension')!r}")

    for entry in decision.controlled_changes:
        dimension = entry.get("dimension")
        if dimension not in allowed_controlled_dimensions:
            codes.append(SELECTION_UNDECLARED_CONTROLLED)
            reasons.append(
                f"{dimension!r} is not an approved controlled change for this task")

    if codes:
        # 去重但保持順序。
        seen: set[str] = set()
        ordered = [c for c in codes if not (c in seen or seen.add(c))]
        return SelectionEligibility(False, tuple(ordered), tuple(reasons))
    return SelectionEligibility(
        True, (SELECTION_OK,),
        ("every comparability dimension is established and the only difference is "
         "the approved controlled change",))


__all__ = [
    "CONTROLLED_DIMENSIONS", "POLICY_CONTROLLED_DIMENSIONS",
    "SELECTION_PROOF_NOT_ESTABLISHED",
    "RELATION_VERIFIED_DECLARED_CHANGE", "controlled_dimensions_for_policy",
    "SELECTION_IDENTITY_UNKNOWN",
    "SELECTION_NOT_COMPARABLE",
    "SELECTION_OK",
    "SELECTION_UNDECLARED_CONTROLLED",
    "SELECTION_UNEXPECTED_MISMATCH",
    "SelectionEligibility",
    "evaluate_selection_eligibility",
    "ComparisonDecision",
    "HARD_DIMENSIONS",
    "POLICY_ID",
    "POLICY_VERSION",
    "RunIdentity",
    "SOFT_DIMENSIONS",
    "STATUS_COMPARABLE",
    "STATUS_CONDITIONAL",
    "STATUS_NOT_COMPARABLE",
    "validate_run_pair",
]
