# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS 固定分析範本、run access 檢查與結構化摘要。

這**不是**新的 package action：它沒有 manifest entry、不註冊成 tool、也不
能被 provider 呼叫。production composition 明確把這裡的三個函式注入
`AnalysisTaskService`（Common 不認識 ORFS，見 analysis task）。

三個責任：

1. `build_task_steps()`：固定兩步的 capability 與 arguments。
2. `validate_task_runs()`：run access 檢查。
3. `summarize_task_action()`：把 action 的完整結果轉成 Common 能用的
   `business_class` 與顯示欄位。

Common 不解析 WNS／TNS，也不判 comparability——那些只出現在這個檔案與
既有的 `comparison.py`／`schemas.py` 裡。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.packages.orfs_run_intelligence import dqe_reader, evidence as ev
from terminux_engine.packages.orfs_run_intelligence import context_builder
from terminux_engine.packages.orfs_run_intelligence.planning_metadata import (
    COMPARE_RUNS,
    EXPLAIN_RUN,
)
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    SUPPORT_INSUFFICIENT,
)

TEMPLATE_ID = "orfs.explain_compare.v1"

STEP_EXPLAIN = "s1"
STEP_COMPARE = "s2"

#: Package 認定為「工具層失敗」的 status。DQE fail／support insufficient
#: **不在**這裡——分析可以有效完成，而其工程結論是 fail。
_TOOL_FAILURE_STATUSES = frozenset({"failed", "error", "fatal", "invalid_arguments"})

#: 分析有效但證據不足以支撐結論時的 support status。
_INDETERMINATE_SUPPORT = frozenset({SUPPORT_INSUFFICIENT})

RunAccessDecision = Literal["ALLOWED", "DENIED", "UNVERIFIABLE"]


# ── 固定範本 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TemplateStep:
    """範本產生的一個步驟。arguments 在建立 task 時就完全確定。"""

    step_id: str
    capability_id: str
    arguments: dict[str, Any]


def build_task_steps(
    *, current_run_id: str, baseline_run_id: str, question: str,
) -> tuple[TemplateStep, ...]:
    """固定兩步。

    s2 的執行順序條件是「s1 已確定完成且結果有效」，但它**不消費 s1 的
    文字，也不由 s1 的結果重建自己的參數**。`controlled_changes`
    不在 create 參數中，因此不填——host 與 LLM 都不猜值。
    """
    return (
        TemplateStep(
            step_id=STEP_EXPLAIN,
            capability_id=EXPLAIN_RUN,
            arguments={"run_id": current_run_id, "question": question},
        ),
        TemplateStep(
            step_id=STEP_COMPARE,
            capability_id=COMPARE_RUNS,
            arguments={
                "baseline_run_id": baseline_run_id,
                "candidate_run_id": current_run_id,
                "question": question,
            },
        ),
    )


# ── Run access ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RunAccessResult:
    decision: RunAccessDecision
    reason_code: str
    message: str
    #: 每個 run 的結構化判定，含來源 reference。
    runs: dict[str, dict[str, Any]]
    #: 明確列出「無法證明」的維度，供上層如實顯示，不得被解讀成已證明。
    #: 這只是診斷欄位：必要存取條件未成立時一律不會是 ALLOWED。
    unproven: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "message": self.message,
            "runs": {k: dict(v) for k, v in self.runs.items()},
            "unproven": list(self.unproven),
        }


#: **無法**由 run 資料證明的維度。這是工程可比性的診斷資訊，與
#: 「能不能執行這筆 task」的 project access 判定分開。
_UNPROVEN_DIMENSIONS: tuple[str, ...] = (
    "design_identity: no design/platform/variant is recorded in any queryable "
    "store, so two runs cannot be proven to belong to the same design.",
    "execution_inputs: manifests capture outputs only; the SDC/config.mk that "
    "produced a run is not captured, so input equivalence is not provable.",
)

PROJECT_ACCESS_UNVERIFIABLE = "project_access_unverifiable"


@dataclass(frozen=True)
class ProjectAccessVerdict:
    """一次 project 存取檢查的結果。

    `decision` 只有三個值，與 `RunAccessDecision` 相同：

    - ``ALLOWED``：有可信依據證明 caller 的 member/project 可讀這筆 run。
    - ``DENIED``：有可信依據證明 caller **無權**讀取。
    - ``UNVERIFIABLE``：依據不存在或不足。

    `evidence` 必須指出這個判定是由哪一段既有存取規則得出的；空的
    evidence 不能是 ALLOWED。
    """

    decision: RunAccessDecision
    reason_code: str
    message: str
    evidence: tuple[str, ...] = ()
    #: policy 宣告的 manifest **原始檔案 bytes** SHA-256。
    #: `ALLOWED` 時必須提供——它讓 run/manifest 段能確認拿到的是同一份
    #: 原始證據，與 manifest 內部驗證分開執行。
    expected_manifest_sha256: str | None = None


class ProjectAccessResolver(Protocol):
    """把 (member_id, project_id, run_id) 對應到可信的存取判定。

    analysis task起，production 的實作是
    `analysis_tasks.access_policy` 載入的 server policy：由 daemon 管理者
    明確允許指定 member 在指定 project 分析固定兩筆 run，並綁定 DQE
    source、artifact root 與 manifest hash。

    沒有配置 policy 時 composition 注入 `None`，`validate_task_runs()`
    一律回 UNVERIFIABLE——**沒有寬鬆預設**。

    `dqe_db_path`／`artifact_root` 是 server 端**實際解析出來**的來源，由
    `validate_task_runs()` 傳入；resolver 必須拿它們與 policy 比對，
    caller 不能提供。
    """

    def __call__(
        self, *, member_id: str, project_id: str, run_id: str,
        dqe_db_path: str | None = None, artifact_root: str | None = None,
    ) -> ProjectAccessVerdict: ...


def _no_project_authority(
    *, member_id: str, project_id: str, run_id: str,
    dqe_db_path: str | None = None, artifact_root: str | None = None,
) -> ProjectAccessVerdict:
    """production 預設：沒有可信的 project 存取連結，因此不可證明。

    這裡刻意**不**退回「同一 member 的 daemon 讀得到就算數」：相同 member
    可以另開 session 自報任意 project_id，只比較字串標籤不能證明其有權使用
    該 project。
    """
    return ProjectAccessVerdict(
        decision="UNVERIFIABLE",
        reason_code=PROJECT_ACCESS_UNVERIFIABLE,
        message=(
            "no analysis access policy is configured on this daemon, so no trusted "
            "rule links this member/project to the run data source"
        ),
    )


def validate_task_runs(
    context: PackageContext,
    current_run_id: str,
    baseline_run_id: str,
    *,
    project_access: ProjectAccessResolver | None = None,
) -> RunAccessResult:
    """判定這兩個 run 是否可由目前 caller 讀取並分析。

    兩段必要檢查，缺一不可：

    1. **project 存取**（`project_access`）。沒有可信依據就是
       ``UNVERIFIABLE``，不授權、不派送。揭露 `unproven` 不能代替這段檢查。
    2. **run 與 manifest 完整性**。只用既有、server 已選定的資料來源：
       DQE DB 由 `resolve_dqe_db_manager(ctx)` 的既有五層政策解析，caller
       不能指定 DB 或路徑；artifact manifest 由 run 自己的 DQE row 帶出並以
       `load_verified_manifest(expected_run_id=...)` 驗 producer run id。

    刻意不使用 `Reporter`、檔案 owner、`Block` 或使用者輸入的 run id 本身
    當授權證據——它們都不是授權依據。

    第 1 段先做：project 存取不成立時不回傳任何 run 內容，避免越界資料。
    """
    member_id, project_id = _caller_scope(context)
    resolver = project_access or _no_project_authority
    dqe_db_path, artifact_root = _resolved_sources(context)

    access: dict[str, dict[str, Any]] = {}
    verdicts: list[ProjectAccessVerdict] = []
    for role, run_id in (("current", current_run_id), ("baseline", baseline_run_id)):
        verdict = resolver(member_id=member_id, project_id=project_id, run_id=run_id,
                           dqe_db_path=dqe_db_path, artifact_root=artifact_root)
        if verdict.decision == "ALLOWED" and not verdict.evidence:
            # 沒有指出依據的 ALLOWED 不算依據。
            verdict = ProjectAccessVerdict(
                decision="UNVERIFIABLE",
                reason_code=PROJECT_ACCESS_UNVERIFIABLE,
                message="project access resolver returned ALLOWED without evidence",
            )
        verdicts.append(verdict)
        access[run_id] = {
            "run_id": run_id, "role": role, "decision": verdict.decision,
            "reason_code": verdict.reason_code, "message": verdict.message,
            "references": list(verdict.evidence),
        }

    decisions = [v.decision for v in verdicts]
    if "DENIED" in decisions:
        return RunAccessResult(
            decision="DENIED", reason_code="PROJECT_ACCESS_DENIED",
            message=("the caller is not permitted to read at least one of the "
                     "requested runs"),
            runs=access, unproven=_UNPROVEN_DIMENSIONS,
        )
    if "UNVERIFIABLE" in decisions:
        return RunAccessResult(
            decision="UNVERIFIABLE", reason_code=PROJECT_ACCESS_UNVERIFIABLE,
            message=("the required project access evidence is missing or "
                     "insufficient, so this task cannot be authorized"),
            runs=access, unproven=_UNPROVEN_DIMENSIONS,
        )

    expected = {v_run: v.expected_manifest_sha256
                for v_run, v in zip((current_run_id, baseline_run_id), verdicts)}

    runs: dict[str, dict[str, Any]] = {}
    store_decisions: list[RunAccessDecision] = []
    for role, run_id in (("current", current_run_id), ("baseline", baseline_run_id)):
        detail = _validate_one(context, run_id,
                               expected_manifest_sha256=expected.get(run_id))
        detail["role"] = role
        detail["project_access"] = dict(access[run_id])
        runs[run_id] = detail
        store_decisions.append(detail["decision"])

    if "DENIED" in store_decisions:
        return RunAccessResult(
            decision="DENIED",
            reason_code="RUN_NOT_IN_SCOPED_STORE",
            message=("at least one run id is not recorded in the DQE store this "
                     "daemon is allowed to read"),
            runs=runs, unproven=_UNPROVEN_DIMENSIONS,
        )
    if "UNVERIFIABLE" in store_decisions:
        return RunAccessResult(
            decision="UNVERIFIABLE",
            reason_code="RUN_EVIDENCE_UNVERIFIABLE",
            message=("at least one run exists but its artifact evidence could not "
                     "be verified"),
            runs=runs, unproven=_UNPROVEN_DIMENSIONS,
        )
    return RunAccessResult(
        decision="ALLOWED",
        reason_code="RUN_ACCESS_VERIFIED",
        message=("project access is verified for both runs, and both runs are "
                 "recorded in the scoped DQE store with verifying manifests"),
        runs=runs, unproven=_UNPROVEN_DIMENSIONS,
    )


def _resolved_sources(context: PackageContext) -> tuple[str | None, str | None]:
    """server **實際**選定的兩個資料來源。解析不出來就回 None。

    DQE DB 走既有 `resolve_dqe_db_manager()` 的五層政策（caller 不能指定
    DB 或路徑）；artifact root 走既有 manifest store 的 root。這裡不接受
    任何 caller 提供的路徑。
    """
    dqe_path: str | None = None
    artifact_root: str | None = None
    try:
        from terminux_engine.dqe.db_policy import resolve_dqe_db_manager

        dqe_path = str(resolve_dqe_db_manager(context).db_path)
    except Exception:  # noqa: BLE001 - 解析不出來就是不可證明，不猜
        dqe_path = None
    try:
        from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

        artifact_root = str(ArtifactManifestStore.DEFAULT_ROOT)
    except Exception:  # noqa: BLE001
        artifact_root = None
    return dqe_path, artifact_root


def _caller_scope(context: PackageContext) -> tuple[str, str]:
    """從既有 runtime services 取 caller scope。缺任一段就給空字串。

    空字串不會被當成合法 scope：`_no_project_authority` 無論如何都回
    UNVERIFIABLE，而注入的 resolver 有責任自己拒絕空 scope。
    """
    services = getattr(context, "services", None)
    store = getattr(services, "store", None) if services is not None else None
    member_id = getattr(store, "user_id", "") or ""
    project_id = getattr(store, "project_id", "") or ""
    return str(member_id), str(project_id)


def _validate_one(
    context: PackageContext, run_id: str, *,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "run_id": run_id, "decision": "DENIED", "reason_code": "RUN_ID_INVALID",
        "references": [],
    }
    try:
        dqe_reader.validate_run_id(run_id)
    except dqe_reader.RunSelectionError as exc:
        detail["message"] = str(exc)
        return detail

    try:
        run = dqe_reader.resolve_run(context, run_id)
    except dqe_reader.RunSelectionError as exc:
        detail["reason_code"] = "RUN_NOT_RECORDED"
        detail["message"] = str(exc)
        return detail
    except Exception as exc:  # noqa: BLE001 - 讀不到就是不可證明，不猜
        detail["decision"] = "UNVERIFIABLE"
        detail["reason_code"] = "DQE_STORE_UNREADABLE"
        detail["message"] = repr(exc)
        return detail

    detail["references"] = [f"dqe:orchestration_run_id={run.run_id}"]
    detail["dqe_row_count"] = len(run.rows)
    detail["dqe_overall_status"] = run.overall_status

    if not run.manifest_reference:
        detail["decision"] = "UNVERIFIABLE"
        detail["reason_code"] = "MANIFEST_REFERENCE_MISSING"
        detail["message"] = "the run has DQE rows but no artifact manifest reference"
        return detail

    try:
        verified = ev.load_verified_manifest(
            run.manifest_reference, expected_run_id=run.run_id,
            text_roles=context_builder.TEXT_ROLES,
        )
    except ev.ManifestVerificationError as exc:
        detail["decision"] = "UNVERIFIABLE"
        detail["reason_code"] = "MANIFEST_NOT_VERIFIABLE"
        detail["message"] = str(exc)
        return detail

    # analysis task-3：policy 宣告的 manifest 原始 bytes hash 必須相符。
    # 這與上面的 manifest 內部驗證是**兩件事**：改寫 manifest 之後，內部
    # 驗證可能仍過，但原始證據已經不是 policy 授權的那一份。
    if expected_manifest_sha256 is not None:
        try:
            from terminux_engine.lineage.artifact_manifest_store import manifest_sha256

            actual = manifest_sha256(Path(run.manifest_reference).read_bytes())
        except Exception as exc:  # noqa: BLE001
            detail["decision"] = "UNVERIFIABLE"
            detail["reason_code"] = "MANIFEST_NOT_VERIFIABLE"
            detail["message"] = f"the manifest bytes could not be hashed: {exc!r}"
            return detail
        detail["manifest_sha256"] = actual
        if actual != expected_manifest_sha256:
            detail["decision"] = "DENIED"
            detail["reason_code"] = "MANIFEST_HASH_NOT_IN_POLICY"
            detail["message"] = (
                "the manifest bytes do not match the hash authorized by the "
                "analysis access policy")
            return detail

    detail["references"].append(f"manifest:{run.manifest_reference}")
    detail["verified_artifact_roles"] = sorted(verified.artifacts)
    detail["decision"] = "ALLOWED"
    detail["reason_code"] = "RUN_RECORDED_AND_MANIFEST_VERIFIED"
    detail["message"] = "recorded in the scoped DQE store with a verified manifest"
    return detail


# ── 結構化摘要 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionSummary:
    action_status: str | None
    business_class: Literal["valid", "failed", "indeterminate"]
    display: dict[str, Any]
    record_refs: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "action_status": self.action_status,
            "business_class": self.business_class,
            "display": dict(self.display),
            "record_refs": list(self.record_refs),
            "limitations": list(self.limitations),
        }


def summarize_task_action(capability_id: str, full_result: Any) -> ActionSummary:
    """把一個 action 的**完整**結果轉成 Common 可用的摘要。

    分類規則：

    - 結果不是 dict、或缺 `status` ⇒ `indeterminate`（不猜）。
    - `status` 落在工具失敗集合 ⇒ `failed`。
    - 其餘 ⇒ `valid`。**DQE fail 是有效分析**，不是工具失敗；
      `support_status=insufficient` 則是 `indeterminate`——分析跑完了，
      但證據不足以支撐結論，因此不繼續派下一步。
    """
    if not isinstance(full_result, dict):
        return ActionSummary(
            action_status=None, business_class="indeterminate",
            display={"reason": "action result is not a JSON object"},
            record_refs=(), limitations=("The action result could not be interpreted.",),
        )

    status = full_result.get("status")
    action_status = status if isinstance(status, str) and status else None
    record_refs = tuple(str(r) for r in (full_result.get("record_refs") or [])
                        if isinstance(r, (str, int)))
    limitations = tuple(str(x) for x in (full_result.get("limitations") or []))

    if action_status is None:
        business_class: Literal["valid", "failed", "indeterminate"] = "indeterminate"
    elif action_status.lower() in _TOOL_FAILURE_STATUSES:
        business_class = "failed"
    elif str(full_result.get("support_status") or "") in _INDETERMINATE_SUPPORT:
        business_class = "indeterminate"
    else:
        business_class = "valid"

    return ActionSummary(
        action_status=action_status,
        business_class=business_class,
        display=_display_fields(capability_id, full_result),
        record_refs=record_refs,
        limitations=limitations,
    )


def _display_fields(capability_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """deterministic 顯示欄位。不呼叫 LLM、不重寫內容、不下工程結論。"""
    insight = result.get("insight") if isinstance(result.get("insight"), dict) else {}
    common = {
        "capability_id": capability_id,
        "support_status": result.get("support_status"),
        "message": result.get("message"),
        "record_status": (result.get("record_status") or {}).get("status")
        if isinstance(result.get("record_status"), dict) else None,
    }
    if capability_id == EXPLAIN_RUN:
        common.update({
            "run_id": result.get("run_id"),
            "insight_id": result.get("insight_id"),
            "context_id": result.get("context_id"),
            "execution_status": insight.get("execution_status"),
            "dqe_overall_status": insight.get("dqe_overall_status"),
            "fact_count": len(insight.get("facts") or []),
            "interpretation_count": len(insight.get("interpretations") or []),
            "hypothesis_count": len(insight.get("hypotheses") or []),
            "unknown_count": len(insight.get("unknowns") or []),
            "recommended_check_count": len(insight.get("recommended_checks") or []),
        })
    elif capability_id == COMPARE_RUNS:
        common.update({
            "context_id": result.get("context_id"),
            "insight_id": result.get("insight_id"),
            "comparison_status": result.get("comparison_status"),
            "comparability_statement": insight.get("comparability_statement"),
            "metric_comparison_summary": insight.get("metric_comparison_summary"),
            "metric_comparison_count": len(insight.get("metric_comparisons") or []),
            "hypothesis_count": len(insight.get("hypotheses") or []),
            "unknown_count": len(insight.get("unknowns") or []),
        })
    return common


__all__ = [
    "ActionSummary",
    "PROJECT_ACCESS_UNVERIFIABLE",
    "ProjectAccessResolver",
    "ProjectAccessVerdict",
    "RunAccessResult",
    "STEP_COMPARE",
    "STEP_EXPLAIN",
    "TEMPLATE_ID",
    "TemplateStep",
    "build_task_steps",
    "summarize_task_action",
    "validate_task_runs",
]
