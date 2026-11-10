# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""flow task 的資料模型與跨步 binding。

與 analysis task 的 `models.py` 並存：`AnalysisTask` 是固定兩步的純分析任務，
`FlowTask` 是固定五步、其中兩步會真的啟動 EDA 的任務。兩者共用同一個
service、同一個 grant／lock／history 機制，但事件命名與 snapshot 形狀分開
（`flow_task.*.v1` vs `analysis_task.*.v1`）。

跨步 binding 是本批的正式新增能力：`run_flow` 產生的 run id **在執行後**才
存在，由 host 驗證 Kernel reference 後才具體化後續步驟的 arguments。
模型層在這裡固定 slot 與解析規則，讓 LLM 沒有預填 run id 的位置。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from terminux_engine.bounded_execution.analysis_tasks.canonical import (
    now_ms, sha256_payload,
)
from terminux_engine.bounded_execution.analysis_tasks.models import (
    TaskScope, TaskStepSpec,
)

SCHEMA_VERSION = 1

FLOW_TEMPLATE_ID = "orfs.run_parameter_compare.v1"

#: 沒有可重用 baseline 時使用的正式單步 task。它只執行 s1，沒有
#: candidate slot、candidate materialization 或 provider analysis step。
BASELINE_ONLY_TEMPLATE_ID = "orfs.baseline_collection.v1"

#: flow task snapshot 的 schema 版本。v2 起**沒有**
#: 草案期限（不寫 `draft_expires_at_ms`、不據以拒絕核准）。v1 snapshot 仍可讀，
#: digest 材料與當年位元等價。deadline 仍由 profile 提供，且**從核准（start）
#: 時刻**起算，草案等待期間不消耗它。
TASK_SNAPSHOT_SCHEMA_VERSION = 2

SLOT_BASELINE = "baseline"
SLOT_CANDIDATE = "candidate"
#: bounded search 的兩個候選 slot。
SLOT_CANDIDATE_1 = "candidate_1"
SLOT_CANDIDATE_2 = "candidate_2"

#: 固定五步。順序與 capability 都不可變。
STEP_RUN_BASELINE = "s1"
STEP_EXPLAIN = "s2"
STEP_PREPARE_VARIANT = "s3"
STEP_RUN_CANDIDATE = "s4"
STEP_COMPARE = "s5"

FLOW_STEP_IDS: tuple[str, ...] = (
    STEP_RUN_BASELINE, STEP_EXPLAIN, STEP_PREPARE_VARIANT,
    STEP_RUN_CANDIDATE, STEP_COMPARE,
)

BASELINE_ONLY_STEP_IDS: tuple[str, ...] = (STEP_RUN_BASELINE,)
BASELINE_ONLY_BINDING_RULES: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "template_id": BASELINE_ONLY_TEMPLATE_ID,
    STEP_RUN_BASELINE: {"produces_run": SLOT_BASELINE},
}

#: bounded utilization search 固定八步（`orfs.bounded_utilization_search.v1`）。順序與 capability
#: 同樣不可變；s6 一定從**baseline 副本**出發，不沿用 s3 已改過的 config。
BOUNDED_SEARCH_TEMPLATE_ID = "orfs.bounded_utilization_search.v1"
STEP_PREPARE_CANDIDATE_1 = "s3"
STEP_RUN_CANDIDATE_1 = "s4"
STEP_COMPARE_CANDIDATE_1 = "s5"
STEP_PREPARE_CANDIDATE_2 = "s6"
STEP_RUN_CANDIDATE_2 = "s7"
STEP_COMPARE_CANDIDATE_2 = "s8"

BOUNDED_SEARCH_STEP_IDS: tuple[str, ...] = (
    STEP_RUN_BASELINE, STEP_EXPLAIN,
    STEP_PREPARE_CANDIDATE_1, STEP_RUN_CANDIDATE_1, STEP_COMPARE_CANDIDATE_1,
    STEP_PREPARE_CANDIDATE_2, STEP_RUN_CANDIDATE_2, STEP_COMPARE_CANDIDATE_2,
)

#: 每個 template 的固定 capability 序列。用來組草案與逐步重驗。
_RUN_FLOW = "orfs_run_intelligence.run_flow"
_EXPLAIN = "orfs_run_intelligence.explain_run"
_PREPARE = "orfs_run_intelligence.prepare_variant"
_COMPARE = "orfs_run_intelligence.compare_runs"

BOUNDED_SEARCH_CAPABILITIES: dict[str, str] = {
    STEP_RUN_BASELINE: _RUN_FLOW,
    STEP_EXPLAIN: _EXPLAIN,
    STEP_PREPARE_CANDIDATE_1: _PREPARE,
    STEP_RUN_CANDIDATE_1: _RUN_FLOW,
    STEP_COMPARE_CANDIDATE_1: _COMPARE,
    STEP_PREPARE_CANDIDATE_2: _PREPARE,
    STEP_RUN_CANDIDATE_2: _RUN_FLOW,
    STEP_COMPARE_CANDIDATE_2: _COMPARE,
}

#: bounded utilization search 的 binding 規則。納入 task digest，之後不可改。
BOUNDED_SEARCH_BINDING_RULES: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "template_id": BOUNDED_SEARCH_TEMPLATE_ID,
    STEP_RUN_BASELINE: {"produces_run": SLOT_BASELINE},
    STEP_EXPLAIN: {"run_id_from": SLOT_BASELINE},
    STEP_PREPARE_CANDIDATE_1: {"produces_input": SLOT_CANDIDATE_1,
                               "derived_from": SLOT_BASELINE},
    STEP_RUN_CANDIDATE_1: {"produces_run": SLOT_CANDIDATE_1,
                           "input_from": SLOT_CANDIDATE_1},
    STEP_COMPARE_CANDIDATE_1: {"baseline_run_id_from": SLOT_BASELINE,
                               "candidate_run_id_from": SLOT_CANDIDATE_1},
    # s6 一樣從 baseline 副本出發——**不**沿用 candidate_1 的已改 config。
    STEP_PREPARE_CANDIDATE_2: {"produces_input": SLOT_CANDIDATE_2,
                               "derived_from": SLOT_BASELINE},
    STEP_RUN_CANDIDATE_2: {"produces_run": SLOT_CANDIDATE_2,
                           "input_from": SLOT_CANDIDATE_2},
    STEP_COMPARE_CANDIDATE_2: {"baseline_run_id_from": SLOT_BASELINE,
                               "candidate_run_id_from": SLOT_CANDIDATE_2,
                               "runs_selection": True},
}

#: 選擇結果事件。它是**結果紀錄**，沒有新的 execution authority。
EVENT_SELECTION = "flow_task.selection.v1"
#: 修復判定結果事件。同樣只是結果紀錄。
EVENT_REPAIR = "flow_task.repair.v1"

#: host 對解說失敗的處置紀錄。**只是結果紀錄，不帶執行授權。**
EVENT_ANALYSIS_DISPOSITION = "flow_task.analysis_disposition.v1"

#: setup repair 固定五步（`orfs.setup_repair.v1`／`.v2`）。
SETUP_REPAIR_TEMPLATE_ID = "orfs.setup_repair.v1"
SETUP_REPAIR_V2_TEMPLATE_ID = "orfs.setup_repair.v2"
#: 速度導向重合成（ABC_AREA 1→0）。步驟序列與 v1／v2 相同。
SETUP_REPAIR_V3_TEMPLATE_ID = "orfs.setup_repair.v3"
#: 固定 RTL patch（prefix-borrow）。步驟序列與 v1～v3 相同。
SETUP_REPAIR_V5_TEMPLATE_ID = "orfs.setup_repair.v5"
#: 同一份凍結 RTL patch，mapped pair 的 proof 改用 transition miter。
SETUP_REPAIR_V8_TEMPLATE_ID = "orfs.setup_repair.v8"
#: aes 的 SETUP_SLACK_MARGIN 受控修復。沒有 RTL patch、沒有合成策略切換，
#: 唯一差異是一個 ORFS 既有、documented、且由 pinned source 證明會被
#: `repair_timing` 消費的 timing 參數。
SETUP_REPAIR_V9_TEMPLATE_ID = "orfs.setup_repair.v9"
#: 由 Package 驗證過的動態方案轉成 task-owned 候選；仍沿用 parametric setup repair 的
#: repair acceptance，唯有 s3 改走既有的 host-bound `prepare_variant`。
EAP_SETUP_MARGIN_TEMPLATE_ID = "orfs.eap.setup_margin.v1"
# Package-owned CORE_UTILIZATION；候選由 task-owned source materialize，
# 不是 bounded utilization search 的預排 bounded-search slots。
EAP_CORE_UTILIZATION_TEMPLATE_ID = "orfs.eap.core_utilization.v1"
PACKAGE_SINGLE_TOOL_TEMPLATE_ID = "package.approved_local_tool.v1"

SETUP_REPAIR_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_TEMPLATE_ID, SETUP_REPAIR_V2_TEMPLATE_ID,
     SETUP_REPAIR_V3_TEMPLATE_ID, SETUP_REPAIR_V5_TEMPLATE_ID,
     SETUP_REPAIR_V8_TEMPLATE_ID, SETUP_REPAIR_V9_TEMPLATE_ID,
     EAP_SETUP_MARGIN_TEMPLATE_ID})
#: 只有 v3 用「合成策略切換」而不是 ECO hook。
RESYNTHESIS_TEMPLATES: frozenset[str] = frozenset({SETUP_REPAIR_V3_TEMPLATE_ID})
#: 只有 v0.8 的 template 可以修改隔離候選的 RTL。
PREFIX_BORROW_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V5_TEMPLATE_ID, SETUP_REPAIR_V8_TEMPLATE_ID})
#: 只有 v0.12 的範本用 canonical transition miter 當 mapped pair 的 proof。
TRANSITION_MITER_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V8_TEMPLATE_ID, SETUP_REPAIR_V9_TEMPLATE_ID})
#: parametric setup repair 專屬：參數式 setup 修復（兩側只差一個已證明被消費的 timing 參數）。
SETUP_MARGIN_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V9_TEMPLATE_ID, EAP_SETUP_MARGIN_TEMPLATE_ID})
#: 沿用 baseline exact core/die 的 template 全集（v3 與 v0.8）。
EXACT_GEOMETRY_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V3_TEMPLATE_ID, SETUP_REPAIR_V5_TEMPLATE_ID,
     SETUP_REPAIR_V8_TEMPLATE_ID})
_PREPARE_REPAIR = "orfs_run_intelligence.prepare_timing_repair"

SETUP_REPAIR_STEP_IDS: tuple[str, ...] = ("s1", "s2", "s3", "s4", "s5")
SETUP_REPAIR_CAPABILITIES: dict[str, str] = {
    "s1": _RUN_FLOW,
    "s2": _EXPLAIN,
    "s3": _PREPARE_REPAIR,
    "s4": _RUN_FLOW,
    "s5": _COMPARE,
}
#: EAP proposal 的候選值不是 profile 內的固定 recipe 值；必須在已授權 task
#: 的 host binding 下 materialize，不能讓 Domain 提供 path 或 binding。
EAP_SETUP_MARGIN_CAPABILITIES: dict[str, str] = {
    STEP_RUN_BASELINE: _RUN_FLOW,
    STEP_EXPLAIN: _EXPLAIN,
    STEP_PREPARE_VARIANT: _PREPARE,
    STEP_RUN_CANDIDATE: _RUN_FLOW,
    STEP_COMPARE: _COMPARE,
}
EAP_CORE_UTILIZATION_CAPABILITIES: dict[str, str] = dict(EAP_SETUP_MARGIN_CAPABILITIES)
SETUP_REPAIR_BINDING_RULES: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "template_id": SETUP_REPAIR_TEMPLATE_ID,
    "s1": {"produces_run": SLOT_BASELINE,
           "produces_verification_reference": SLOT_BASELINE},
    "s2": {"run_id_from": SLOT_BASELINE},
    # candidate 從**同一份已核准的 baseline source** 出發，只加已核准的
    # ECO hook 設定；參數值不變。
    "s3": {"produces_input": SLOT_CANDIDATE, "derived_from": SLOT_BASELINE},
    "s4": {"produces_run": SLOT_CANDIDATE, "input_from": SLOT_CANDIDATE,
           "verification_reference_from": SLOT_BASELINE},
    "s5": {"baseline_run_id_from": SLOT_BASELINE,
           "candidate_run_id_from": SLOT_CANDIDATE,
           "runs_repair_acceptance": True},
}
#: 與 setup repair 相同的 baseline/candidate relation，但 s3 的 capability 是
#: `prepare_variant`；template identity 必須與被核准的 EAP task 完全一致。
EAP_SETUP_MARGIN_BINDING_RULES: dict[str, Any] = {
    **SETUP_REPAIR_BINDING_RULES,
    "template_id": EAP_SETUP_MARGIN_TEMPLATE_ID,
}
EAP_CORE_UTILIZATION_BINDING_RULES: dict[str, Any] = {
    **SETUP_REPAIR_BINDING_RULES,
    "template_id": EAP_CORE_UTILIZATION_TEMPLATE_ID,
}


def step_ids_for_template(template_id: str) -> tuple[str, ...]:
    if template_id == PACKAGE_SINGLE_TOOL_TEMPLATE_ID:
        return ("s1",)
    if template_id == BASELINE_ONLY_TEMPLATE_ID:
        return BASELINE_ONLY_STEP_IDS
    if template_id == BOUNDED_SEARCH_TEMPLATE_ID:
        return BOUNDED_SEARCH_STEP_IDS
    if template_id in SETUP_REPAIR_TEMPLATES or template_id == EAP_CORE_UTILIZATION_TEMPLATE_ID:
        return SETUP_REPAIR_STEP_IDS
    return FLOW_STEP_IDS


def binding_rules_for_template(template_id: str) -> dict[str, Any]:
    if template_id == PACKAGE_SINGLE_TOOL_TEMPLATE_ID:
        return {"schema_version": SCHEMA_VERSION,
                "template_id": PACKAGE_SINGLE_TOOL_TEMPLATE_ID,
                "s1": {"produces_run": SLOT_CANDIDATE,
                       "input_from": "approved_package_materialization"}}
    if template_id == BASELINE_ONLY_TEMPLATE_ID:
        return BASELINE_ONLY_BINDING_RULES
    if template_id == BOUNDED_SEARCH_TEMPLATE_ID:
        return BOUNDED_SEARCH_BINDING_RULES
    if template_id == EAP_SETUP_MARGIN_TEMPLATE_ID:
        return EAP_SETUP_MARGIN_BINDING_RULES
    if template_id == EAP_CORE_UTILIZATION_TEMPLATE_ID:
        return EAP_CORE_UTILIZATION_BINDING_RULES
    if template_id in SETUP_REPAIR_TEMPLATES:
        return SETUP_REPAIR_BINDING_RULES
    return FLOW_BINDING_RULES


def candidate_slot_for_step(step_id: str) -> str | None:
    """s3/s4/s5 → candidate_1；s6/s7/s8 → candidate_2。"""
    return {
        STEP_PREPARE_CANDIDATE_1: SLOT_CANDIDATE_1,
        STEP_RUN_CANDIDATE_1: SLOT_CANDIDATE_1,
        STEP_COMPARE_CANDIDATE_1: SLOT_CANDIDATE_1,
        STEP_PREPARE_CANDIDATE_2: SLOT_CANDIDATE_2,
        STEP_RUN_CANDIDATE_2: SLOT_CANDIDATE_2,
        STEP_COMPARE_CANDIDATE_2: SLOT_CANDIDATE_2,
    }.get(step_id)

#: 事件型別。與 `analysis_task.*.v1` 分開，共用讀取器依 schema 解析。
EVENT_SNAPSHOT = "flow_task.snapshot.v1"
EVENT_GRANT = "flow_task.grant.v1"
EVENT_STARTED = "flow_task.started.v1"
EVENT_STEP_INTENT = "flow_task.step_intent.v1"
EVENT_STEP_RESULT = "flow_task.step_result.v1"
EVENT_STEP_RECEIPT = "flow_task.step_receipt.v1"
EVENT_CANCEL_REQUESTED = "flow_task.cancel_requested.v1"
EVENT_TERMINAL = "flow_task.terminal.v1"
#: 事實／引用型事件——**不是**第二個 EDA ledger。
EVENT_RUN_LINK = "flow_task.run_link.v1"
EVENT_INPUT_REFERENCE = "flow_task.input_reference.v1"
EVENT_MILESTONE = "flow_task.milestone.v1"
#: 已核准但等待名額。**只是狀態紀錄**，不帶新的執行授權。
EVENT_QUEUED = "flow_task.queued.v1"
#: task-owned immutable profile snapshot（原始／canonical 表示與來源身分）。
EVENT_PROFILE_SNAPSHOT = "flow_task.profile_snapshot.v1"
#: 可重建 Profile 的 v2 snapshot；v1 只保留唯讀相容，不能供 hydration 執行。
EVENT_PROFILE_SNAPSHOT_V2 = "flow_task.profile_snapshot.v2"
#: 既有 DLE ledger 內對應 commit 的引用（planned／executed 兩層）。
EVENT_LINEAGE = "flow_task.lineage.v1"
#: B1：method 宣告的 formal-proof 相依。CORE task 在 compare 之前落盤這筆
#: intent 並進入 `WAITING_DEPENDENCY`；相依滿足後只恢復 compare 那一步。
EVENT_DEPENDENCY_WAIT = "flow_task.dependency_wait.v1"
EVENT_DEPENDENCY_SATISFIED = "flow_task.dependency_satisfied.v1"
#: 相依在操作層無法完成（例如 proof task／grant／launch 失敗，或完成後仍沒有
#: 可供 controller 重新解析的 durable envelope）。proof 的 engineering
#: classification **不是**這個事件的判定依據；它交給 compare fail closed。
#: CORE 以 UNVERIFIABLE 收束——**不**永久停在等待。
EVENT_DEPENDENCY_FAILED = "flow_task.dependency_failed.v1"
#: 非終結狀態：task 已完成 s1–s4，正在等一個已授權的相依工作。
STATUS_WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
#: 相依的種類。刻意只有這一種——這不是通用的 workflow DAG。
DEPENDENCY_FORMAL_PAIR_PROOF = "formal_pair_proof"
#: 已執行 task 的 Domain result references 與 Global 下一步；不含 grant。
EVENT_EAP_FEEDBACK = "flow_task.eap_feedback.v1"
#: 這些事件不指向任何 step，reader 只投影、不驗 step 關聯。
INFORMATIONAL_EVENT_TYPES: tuple[str, ...] = (
    EVENT_QUEUED, EVENT_PROFILE_SNAPSHOT, EVENT_PROFILE_SNAPSHOT_V2, EVENT_LINEAGE, EVENT_EAP_FEEDBACK,
    EVENT_RUN_LINK, EVENT_INPUT_REFERENCE, EVENT_MILESTONE, EVENT_SELECTION,
    EVENT_DEPENDENCY_WAIT, EVENT_DEPENDENCY_SATISFIED, EVENT_DEPENDENCY_FAILED)

FLOW_TASK_EVENT_TYPES: tuple[str, ...] = (
    EVENT_SNAPSHOT, EVENT_GRANT, EVENT_STARTED, EVENT_STEP_INTENT,
    EVENT_STEP_RESULT, EVENT_STEP_RECEIPT, EVENT_CANCEL_REQUESTED,
    EVENT_TERMINAL, EVENT_RUN_LINK, EVENT_INPUT_REFERENCE, EVENT_MILESTONE,
    EVENT_SELECTION, EVENT_REPAIR, EVENT_ANALYSIS_DISPOSITION,
    EVENT_QUEUED, EVENT_PROFILE_SNAPSHOT, EVENT_PROFILE_SNAPSHOT_V2, EVENT_LINEAGE, EVENT_EAP_FEEDBACK,
    EVENT_DEPENDENCY_WAIT, EVENT_DEPENDENCY_SATISFIED, EVENT_DEPENDENCY_FAILED,
)

#: reader 的字彙介面用這個名稱（與 analysis task 的 `TASK_EVENT_TYPES` 對齊），
#: 讓同一個 `TaskHistoryReader` 能以注入的方式讀兩種 task 的歷史。
TASK_EVENT_TYPES: tuple[str, ...] = FLOW_TASK_EVENT_TYPES

#: `view.current_operation`：等待 Kernel 時 task 仍 RUNNING。
OPERATION_IDLE = "idle"
OPERATION_VERIFYING_INPUTS = "verifying_inputs"
OPERATION_WAITING_EXECUTION = "waiting_execution"
OPERATION_WAITING_EVIDENCE = "waiting_evidence"
OPERATION_PREPARING_VARIANT = "preparing_variant"
OPERATION_ANALYZING = "analyzing"
#: 等待 EDA／provider 平行度名額。
OPERATION_WAITING_SLOT = "waiting_slot"

#: Kernel 端 task-owned run 的固定 recovery policy。
RECOVERY_POLICY_OBSERVE_ONLY = "observe_only"


def new_flow_task_id() -> str:
    return f"ftask_{uuid.uuid4().hex}"


# ── 執行綁定（host-only）────────────────────────────────────────────


@dataclass(frozen=True)
class TaskExecutionBinding:
    """host 交給 `FlowExecutionService` 的 live binding。

    **只有 host 能建立**：Package action 拿到的是這個物件，不是可序列化的
    dict；provider／caller 無法自己組一份出來。
    """

    task_id: str
    task_digest: str
    grant_id: str
    slot: str
    member_id: str
    project_id: str
    session_id: str
    profile_id: str
    profile_digest: str
    #: 本 slot 實際要讀的 input snapshot 根目錄（唯讀）。
    input_root: str
    input_manifest_path: str
    input_manifest_sha256: str
    input_tree_hash: str
    #: 本 slot 專屬的可寫輸出根目錄。
    output_root: str
    #: 舊 Profile 的 container image 身分；host-native Profile 一律為 `None`，
    #: 工具身分改看 `native_tool_binding_sha256`。
    docker_image_id: str | None
    design_id: str
    platform_id: str
    variant: str
    design_config_relpath: str
    run_timeout_sec: int
    step_id: str
    #: baseline final netlist 的唯讀 reference 目錄。只有 candidate
    #: 的 EDA step 會有；由 host 從 s1 的產物派生並在派送前驗 hash。
    #: 它**不是** client／provider 可指定的路徑。
    verification_reference_dir: str | None = None
    verification_reference_sha256: str | None = None
    #: profile snapshot 內的 `execution` 宣告（dict）。由 host 從
    #: task-owned snapshot 展開；`None` ⇒ 預設 docker runner、無額外 args。
    execution: dict[str, Any] | None = None
    #: task-owned Profile 固定的證據 sinks。這兩個值只能由 host 從已保存的
    #: Profile snapshot 帶入；child 不得改以自己的 HOME 預設路徑取代。
    dqe_db_path: str | None = None
    artifact_root: str | None = None
    #: 受信任 Package 編譯並固定在 FlowTask digest 的本機工具計畫。
    #: None 維持既有 ORFS 執行位元相容；model 不得建立 host binding。
    package_tool: dict[str, Any] | None = None

    @property
    def native_tool_binding_sha256(self) -> str | None:
        """host-native 工具綁定的 digest（由 binding 帶的執行宣告推得）。"""
        binding = (self.execution or {}).get("native_tool_binding")
        return sha256_payload(binding) if isinstance(binding, dict) else None

    def to_json_dict(self) -> dict[str, Any]:
        native = self.native_tool_binding_sha256
        return {
            "task_id": self.task_id, "task_digest": self.task_digest,
            "grant_id": self.grant_id, "slot": self.slot,
            "member_id": self.member_id, "project_id": self.project_id,
            "profile_id": self.profile_id, "profile_digest": self.profile_digest,
            "input_root": self.input_root,
            "input_manifest_sha256": self.input_manifest_sha256,
            "input_tree_hash": self.input_tree_hash,
            "output_root": self.output_root,
            "docker_image_id": self.docker_image_id,
            **({"native_tool_binding_sha256": native} if native else {}),
            "design_id": self.design_id, "platform_id": self.platform_id,
            "variant": self.variant, "step_id": self.step_id,
            **({"package_tool_sha256": sha256_payload(self.package_tool)}
               if self.package_tool is not None else {}),
        }

    def agent_task_metadata(self) -> dict[str, Any]:
        """寫進 ledger `metadata.agent_task` 的子樹。

        **禁止 caller 覆寫**——它由 host 在 `start_task_run()` 內組出，
        並在 run 建立時一次寫入（ledger metadata 建立後不可變）。
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "slot": self.slot,
            "grant_id": self.grant_id,
            "task_digest": self.task_digest,
            "profile_digest": self.profile_digest,
            "input_manifest_hash": self.input_manifest_sha256,
            "recovery_policy": RECOVERY_POLICY_OBSERVE_ONLY,
            **({"verification_reference_sha256": self.verification_reference_sha256}
               if self.verification_reference_sha256 else {}),
            # 執行宣告跟著 grant 綁定的 metadata 走，child 從 ledger 讀回，
            # 公開 `orchestration.start` 沒有這棵子樹，因此無法注入。
            **({"execution": dict(self.execution)} if self.execution else {}),
            **({"dqe_db_path": self.dqe_db_path} if self.dqe_db_path else {}),
            **({"artifact_root": self.artifact_root} if self.artifact_root else {}),
            **({"package_tool_sha256": sha256_payload(self.package_tool)}
               if self.package_tool is not None else {}),
        }


@dataclass(frozen=True)
class RunRef:
    """Kernel 產生的 run 身分。**執行後**才存在。"""

    run_id: str
    slot: str
    task_id: str
    workflow_type: str
    created_at_ms: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "slot": self.slot, "task_id": self.task_id,
            "workflow_type": self.workflow_type,
            "created_at_ms": self.created_at_ms,
        }


@dataclass(frozen=True)
class VariantInputRef:
    """`prepare_variant` 的回傳：候選 input snapshot 的身分。"""

    task_id: str
    candidate_root: str
    manifest_path: str
    manifest_sha256: str
    tree_hash: str
    config_sha256: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "candidate_root": self.candidate_root,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "tree_hash": self.tree_hash, "config_sha256": self.config_sha256,
        }


@dataclass(frozen=True)
class RunAccessBinding:
    """host-only 的新 run 讀取權。

    只有**同一個 live task 產生、已記入 ledger 與 task history、且證據驗證
    通過**的 run 才能成為後續分析輸入。這不是可序列化後由 caller 提供的
    憑證——它由 host 在每次分析步驟前重新推導。
    """

    member_id: str
    project_id: str
    task_id: str
    slot: str
    run_id: str
    grant_id: str
    profile_digest: str
    input_manifest_sha256: str
    receipt_reference: str | None = None
    output_manifest_reference: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id, "project_id": self.project_id,
            "task_id": self.task_id, "slot": self.slot, "run_id": self.run_id,
            "grant_id": self.grant_id, "profile_digest": self.profile_digest,
            "input_manifest_sha256": self.input_manifest_sha256,
            "receipt_reference": self.receipt_reference,
            "output_manifest_reference": self.output_manifest_reference,
        }


# ── Flow task snapshot ───────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class FlowTask:
    """Immutable flow task snapshot。

    `task_digest` 涵蓋下列全部：scope、daemon instance、profile hash、
    baseline input snapshot hash、**預期** candidate hash、固定五步與 binding
    規則、capability metadata、時間與 limits。

    `draft_expires_at_ms`：v1 snapshot 的歷史欄位。**只供讀取舊資料**：有值就
    原樣保留並照舊 digest 規則計算；v2 起為 `None`、不進 digest、任何程式碼
    都不再用它判草案是否過期。
    """

    schema_version: int
    task_id: str
    daemon_instance_id: str
    template_id: str
    scope: TaskScope
    profile_id: str
    profile_digest: str
    question: str
    design_id: str
    platform_id: str
    parameter_name: str
    baseline_value: str
    candidate_value: str | None
    baseline_input_manifest_sha256: str
    baseline_input_tree_hash: str
    expected_candidate_tree_hash: str | None
    expected_candidate_config_sha256: str | None
    created_at_ms: int
    draft_expires_at_ms: int | None = None
    task_deadline_sec: int
    run_timeout_sec: int
    limits: dict[str, Any]
    steps: tuple[TaskStepSpec, ...]
    task_digest: str
    #: 每個候選 slot 的固定參數值與**預期** input hash。v1 留空，因此
    #: v1 的 digest 材料與 flow task 完全相同（既有 task 身分不變）。
    candidate_plan: tuple[dict[str, Any], ...] = ()
    #: 固定 selection policy（policy_id ＋ 門檻）。v1 為 `None`。
    selection_policy: dict[str, Any] | None = None
    #: 固定 repair recipe（演算法／門檻／helper hashes）。v1／v2 為 `None`。
    repair_recipe: dict[str, Any] | None = None
    #: profile 來源身分（path／inline、resolved path、raw sha256）。
    #: 舊版的 task 沒有這兩個欄位；缺席時不進 digest 材料，舊 digest 不變。
    profile_source: dict[str, Any] | None = None
    #: 展開後的執行計畫（runner／docker args／method／proof／budget／
    #: writable roots／side effects）。人核准的就是這一份。
    execution_plan: dict[str, Any] | None = None
    #: 被使用者核准的 task 必須固定其來源 proposal，不能只保存候選值。
    eap_proposal: dict[str, Any] | None = None
    #: v2 Profile snapshot 的 immutable identity；缺席代表歷史 v1 task。
    profile_snapshot: dict[str, Any] | None = None

    @property
    def is_bounded_search(self) -> bool:
        return self.template_id == BOUNDED_SEARCH_TEMPLATE_ID

    def candidate_entry(self, slot: str) -> dict[str, Any] | None:
        return next((c for c in self.candidate_plan if c.get("slot") == slot), None)

    def value_for_slot(self, slot: str) -> str:
        if slot == SLOT_BASELINE:
            return self.baseline_value
        entry = self.candidate_entry(slot)
        if entry is not None:
            return str(entry["value"])
        if self.candidate_value is None:
            raise ValueError("this task has no candidate slot")
        return self.candidate_value

    def step(self, step_id: str) -> TaskStepSpec | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def _digest_material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "daemon_instance_id": self.daemon_instance_id,
            "template_id": self.template_id,
            "scope": self.scope.to_json_dict(),
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "question": self.question,
            "design_id": self.design_id,
            "platform_id": self.platform_id,
            "parameter_name": self.parameter_name,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "baseline_input_manifest_sha256": self.baseline_input_manifest_sha256,
            "baseline_input_tree_hash": self.baseline_input_tree_hash,
            "expected_candidate_tree_hash": self.expected_candidate_tree_hash,
            "expected_candidate_config_sha256": self.expected_candidate_config_sha256,
            "created_at_ms": self.created_at_ms,
            # v1 snapshot 帶 `draft_expires_at_ms` ⇒ 照原位置、原鍵序
            # 進 digest（舊 digest 不變）；v2 沒有這一欄 ⇒ 不出現在材料裡。
            **({"draft_expires_at_ms": self.draft_expires_at_ms}
               if self.draft_expires_at_ms is not None else {}),
            "task_deadline_sec": self.task_deadline_sec,
            "run_timeout_sec": self.run_timeout_sec,
            "limits": dict(self.limits),
            "steps": [s.to_json_dict() for s in self.steps],
            # binding 規則本身也綁進 digest：哪一步用哪個 slot、後續步驟的
            # 參數由哪個 run 解析出來，都不可事後改。
            "binding_rules": binding_rules_for_template(self.template_id),
            # bounded utilization search 的兩個新欄位只在 bounded search 時出現——v1 的 digest 材料
            # 一個位元都沒變。
            **({"candidate_plan": [dict(c) for c in self.candidate_plan]}
               if self.candidate_plan else {}),
            **({"selection_policy": dict(self.selection_policy)}
               if self.selection_policy is not None else {}),
            **({"repair_recipe": dict(self.repair_recipe)}
               if self.repair_recipe is not None else {}),
            **({"profile_source": dict(self.profile_source)}
               if self.profile_source is not None else {}),
            **({"execution_plan": dict(self.execution_plan)}
               if self.execution_plan is not None else {}),
            **({"eap_proposal": dict(self.eap_proposal)}
               if self.eap_proposal is not None else {}),
            **({"profile_snapshot": dict(self.profile_snapshot)}
               if self.profile_snapshot is not None else {}),
        }

    def recompute_digest(self) -> str:
        return sha256_payload(self._digest_material())

    def digest_matches(self) -> bool:
        return self.recompute_digest() == self.task_digest

    def to_json_dict(self) -> dict[str, Any]:
        payload = self._digest_material()
        payload["task_digest"] = self.task_digest
        return payload

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "FlowTask":
        return cls(
            schema_version=int(payload["schema_version"]),
            task_id=str(payload["task_id"]),
            daemon_instance_id=str(payload["daemon_instance_id"]),
            template_id=str(payload["template_id"]),
            scope=TaskScope.from_json_dict(payload["scope"]),
            profile_id=str(payload["profile_id"]),
            profile_digest=str(payload["profile_digest"]),
            question=str(payload["question"]),
            design_id=str(payload["design_id"]),
            platform_id=str(payload["platform_id"]),
            parameter_name=str(payload["parameter_name"]),
            baseline_value=str(payload["baseline_value"]),
            candidate_value=(str(payload["candidate_value"])
                             if payload.get("candidate_value") is not None else None),
            baseline_input_manifest_sha256=str(payload["baseline_input_manifest_sha256"]),
            baseline_input_tree_hash=str(payload["baseline_input_tree_hash"]),
            expected_candidate_tree_hash=(
                str(payload["expected_candidate_tree_hash"])
                if payload.get("expected_candidate_tree_hash") is not None else None),
            expected_candidate_config_sha256=(
                str(payload["expected_candidate_config_sha256"])
                if payload.get("expected_candidate_config_sha256") is not None else None),
            created_at_ms=int(payload["created_at_ms"]),
            draft_expires_at_ms=(int(payload["draft_expires_at_ms"])
                                 if payload.get("draft_expires_at_ms") is not None
                                 else None),
            task_deadline_sec=int(payload["task_deadline_sec"]),
            run_timeout_sec=int(payload["run_timeout_sec"]),
            limits=dict(payload["limits"]),
            steps=tuple(TaskStepSpec.from_json_dict(s) for s in payload["steps"]),
            task_digest=str(payload["task_digest"]),
            candidate_plan=tuple(dict(c) for c in payload.get("candidate_plan", ())),
            selection_policy=(dict(payload["selection_policy"])
                              if payload.get("selection_policy") is not None
                              else None),
            repair_recipe=(dict(payload["repair_recipe"])
                           if payload.get("repair_recipe") is not None else None),
            profile_source=(dict(payload["profile_source"])
                            if payload.get("profile_source") is not None else None),
            execution_plan=(dict(payload["execution_plan"])
                            if payload.get("execution_plan") is not None else None),
            eap_proposal=(dict(payload["eap_proposal"])
                          if payload.get("eap_proposal") is not None else None),
            profile_snapshot=(dict(payload["profile_snapshot"])
                              if payload.get("profile_snapshot") is not None else None),
        )


#: 固定的 run-ref 解析規則。納入 task digest，之後不可改。
FLOW_BINDING_RULES: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    STEP_RUN_BASELINE: {"produces_run": SLOT_BASELINE},
    STEP_EXPLAIN: {"run_id_from": SLOT_BASELINE},
    STEP_PREPARE_VARIANT: {"produces_input": SLOT_CANDIDATE},
    STEP_RUN_CANDIDATE: {"produces_run": SLOT_CANDIDATE,
                         "input_from": SLOT_CANDIDATE},
    STEP_COMPARE: {"baseline_run_id_from": SLOT_BASELINE,
                   "candidate_run_id_from": SLOT_CANDIDATE},
}


def build_flow_task_snapshot(
    *,
    task_id: str,
    daemon_instance_id: str,
    scope: TaskScope,
    profile_id: str,
    profile_digest: str,
    question: str,
    design_id: str,
    platform_id: str,
    parameter_name: str,
    baseline_value: str,
    candidate_value: str | None,
    baseline_input_manifest_sha256: str,
    baseline_input_tree_hash: str,
    expected_candidate_tree_hash: str | None,
    expected_candidate_config_sha256: str | None,
    task_deadline_sec: int,
    run_timeout_sec: int,
    limits: dict[str, Any],
    steps: tuple[TaskStepSpec, ...],
    created_at_ms: int | None = None,
    template_id: str = FLOW_TEMPLATE_ID,
    candidate_plan: tuple[dict[str, Any], ...] = (),
    selection_policy: dict[str, Any] | None = None,
    repair_recipe: dict[str, Any] | None = None,
    profile_source: dict[str, Any] | None = None,
    execution_plan: dict[str, Any] | None = None,
    eap_proposal: dict[str, Any] | None = None,
) -> FlowTask:
    created = now_ms() if created_at_ms is None else int(created_at_ms)
    partial = FlowTask(
        schema_version=TASK_SNAPSHOT_SCHEMA_VERSION, task_id=task_id,
        daemon_instance_id=daemon_instance_id, template_id=template_id,
        scope=scope, profile_id=profile_id, profile_digest=profile_digest,
        question=question, design_id=design_id, platform_id=platform_id,
        parameter_name=parameter_name, baseline_value=baseline_value,
        candidate_value=candidate_value,
        baseline_input_manifest_sha256=baseline_input_manifest_sha256,
        baseline_input_tree_hash=baseline_input_tree_hash,
        expected_candidate_tree_hash=expected_candidate_tree_hash,
        expected_candidate_config_sha256=expected_candidate_config_sha256,
        created_at_ms=created,
        # 新草案沒有期限（None ⇒ 不寫入、不進 digest）。
        draft_expires_at_ms=None,
        task_deadline_sec=task_deadline_sec, run_timeout_sec=run_timeout_sec,
        limits=dict(limits), steps=tuple(steps), task_digest="",
        candidate_plan=tuple(dict(c) for c in candidate_plan),
        selection_policy=(dict(selection_policy)
                          if selection_policy is not None else None),
        repair_recipe=(dict(repair_recipe) if repair_recipe is not None else None),
        profile_source=(dict(profile_source) if profile_source is not None else None),
        execution_plan=(dict(execution_plan) if execution_plan is not None else None),
        eap_proposal=(dict(eap_proposal) if eap_proposal is not None else None),
    )
    return replace(partial, task_digest=partial.recompute_digest())


# ── 對外 view ────────────────────────────────────────────────────────


@dataclass
class FlowTaskView:
    """`agent.flow_task.*` 的對外 view。

    `current_operation` 明示等待原因——launch 回覆**不會**被標成 step
    SUCCEEDED。
    """

    task_id: str
    task_digest: str
    status: str
    template_id: str
    profile_id: str
    question: str
    design_id: str
    platform_id: str
    parameter: dict[str, Any]
    created_at_ms: int
    #: 歷史欄位：只有 v1 snapshot 有值；新草案為 None（沒有核准期限）。
    draft_expires_at_ms: int | None = None
    #: task 固定的有效 project 與來源（session／profile）。
    project_id: str | None = None
    project_source: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    current_operation: str = OPERATION_IDLE
    daemon_instance_id: str | None = None
    is_current_instance: bool = False
    started_at_ms: int | None = None
    deadline_at_ms: int | None = None
    #: 這一次執行授權的 identity。它是 lineage 的一部分：呼叫端（例如 Engineering Work
    #: 的 attempt）要能把 task、run、receipt 與 grant 串成同一條鏈，而不是只
    #: 看得到 deadline。grant 的其他內容（限額、writable roots）不在這裡揭露。
    grant_id: str | None = None
    terminal_reason: str | None = None
    persistence_status: str = "durable"
    budget: dict[str, Any] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    evidence_status: str = "ok"
    #: 固定候選序列與 deterministic 選擇結果。v1 為空／`None`。
    candidate_plan: list[dict[str, Any]] = field(default_factory=list)
    selection: dict[str, Any] | None = None
    repair: dict[str, Any] | None = None
    repair_recipe: dict[str, Any] | None = None
    #: COMPLETE／PARTIAL／UNAVAILABLE，以及每一段被拒的原因。
    narrative_status: str | None = None
    narrative_rejections: list[dict[str, Any]] = field(default_factory=list)
    #: 跑完但**沒有產生任何解說**的步（與引用被拒的步分開列）。
    narrative_unavailable: list[dict[str, Any]] = field(default_factory=list)
    #: 本次 task 用的 profile 來源、執行計畫、queue 狀態、DLE 差異
    #: 維度與 execution／engineering 分列結果。
    profile_source: dict[str, Any] | None = None
    execution_plan: dict[str, Any] | None = None
    queue: dict[str, Any] | None = None
    lineage: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    eap_proposal: dict[str, Any] | None = None
    eap_feedback: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "task_kind": "flow_task",
            "task_id": self.task_id,
            **({"profile_source": dict(self.profile_source)}
               if self.profile_source is not None else {}),
            **({"execution_plan": dict(self.execution_plan)}
               if self.execution_plan is not None else {}),
            **({"queue": dict(self.queue)} if self.queue is not None else {}),
            **({"lineage": dict(self.lineage)} if self.lineage is not None else {}),
            **({"comparison": dict(self.comparison)}
               if self.comparison is not None else {}),
            **({"execution_result": dict(self.execution_result)}
               if self.execution_result is not None else {}),
            **({"eap_proposal": dict(self.eap_proposal)}
               if self.eap_proposal is not None else {}),
            **({"eap_feedback": dict(self.eap_feedback)}
               if self.eap_feedback is not None else {}),
            **({"candidate_plan": [dict(c) for c in self.candidate_plan]}
               if self.candidate_plan else {}),
            **({"selection": dict(self.selection)}
               if self.selection is not None else {}),
            **({"repair": dict(self.repair)}
               if self.repair is not None else {}),
            **({"repair_recipe": dict(self.repair_recipe)}
               if self.repair_recipe is not None else {}),
            **({"narrative_status": self.narrative_status}
               if self.narrative_status is not None else {}),
            **({"narrative_rejections":
                [dict(r) for r in self.narrative_rejections]}
               if self.narrative_rejections else {}),
            **({"narrative_unavailable":
                [dict(r) for r in self.narrative_unavailable]}
               if self.narrative_unavailable else {}),
            "task_digest": self.task_digest,
            "status": self.status,
            "template_id": self.template_id,
            "profile_id": self.profile_id,
            "question": self.question,
            "design_id": self.design_id,
            "platform_id": self.platform_id,
            "parameter": dict(self.parameter),
            "created_at_ms": self.created_at_ms,
            "draft_expires_at_ms": self.draft_expires_at_ms,
            "project_id": self.project_id,
            "project_source": self.project_source,
            "steps": [dict(s) for s in self.steps],
            "current_operation": self.current_operation,
            "daemon_instance_id": self.daemon_instance_id,
            "is_current_instance": self.is_current_instance,
            "started_at_ms": self.started_at_ms,
            "deadline_at_ms": self.deadline_at_ms,
            **({"grant_id": self.grant_id} if self.grant_id else {}),
            "terminal_reason": self.terminal_reason,
            "persistence_status": self.persistence_status,
            "budget": dict(self.budget),
            "runs": [dict(r) for r in self.runs],
            "limitations": list(self.limitations),
            "evidence_status": self.evidence_status,
        }



#: reader 的字彙介面：這個 vocabulary 的 snapshot 由哪個型別解讀。
TASK_SNAPSHOT_TYPE = FlowTask
#: flow task 的 snapshot 放在 body 的這個欄位（見 service 的寫入端）。
TASK_SNAPSHOT_BODY_KEY = "task"

__all__ = [
    "EVENT_LINEAGE", "EVENT_PROFILE_SNAPSHOT", "EVENT_PROFILE_SNAPSHOT_V2", "EVENT_QUEUED",
    "INFORMATIONAL_EVENT_TYPES",
    "EVENT_CANCEL_REQUESTED", "EVENT_GRANT", "EVENT_INPUT_REFERENCE",
    "EVENT_MILESTONE", "EVENT_RUN_LINK", "EVENT_SNAPSHOT", "EVENT_STARTED",
    "EVENT_STEP_INTENT", "EVENT_STEP_RECEIPT", "EVENT_STEP_RESULT",
    "EVENT_SELECTION", "EVENT_REPAIR", "EVENT_ANALYSIS_DISPOSITION",
    "SETUP_REPAIR_TEMPLATES", "SETUP_REPAIR_V2_TEMPLATE_ID",
    "SETUP_REPAIR_V3_TEMPLATE_ID", "RESYNTHESIS_TEMPLATES",
    "SETUP_REPAIR_V5_TEMPLATE_ID", "PREFIX_BORROW_TEMPLATES",
    "SETUP_REPAIR_V8_TEMPLATE_ID", "TRANSITION_MITER_TEMPLATES",
    "SETUP_REPAIR_V9_TEMPLATE_ID", "EAP_SETUP_MARGIN_TEMPLATE_ID", "EAP_CORE_UTILIZATION_TEMPLATE_ID",
    "SETUP_MARGIN_TEMPLATES",
    "EXACT_GEOMETRY_TEMPLATES",
    "TASK_EVENT_TYPES", "TASK_SNAPSHOT_TYPE", "TASK_SNAPSHOT_BODY_KEY",
    "SETUP_REPAIR_BINDING_RULES", "SETUP_REPAIR_CAPABILITIES",
    "EAP_SETUP_MARGIN_CAPABILITIES", "EAP_SETUP_MARGIN_BINDING_RULES",
    "SETUP_REPAIR_STEP_IDS", "SETUP_REPAIR_TEMPLATE_ID",
    "BOUNDED_SEARCH_BINDING_RULES", "BOUNDED_SEARCH_CAPABILITIES",
    "BOUNDED_SEARCH_STEP_IDS", "BOUNDED_SEARCH_TEMPLATE_ID",
    "SLOT_CANDIDATE_1", "SLOT_CANDIDATE_2",
    "STEP_COMPARE_CANDIDATE_1", "STEP_COMPARE_CANDIDATE_2",
    "STEP_PREPARE_CANDIDATE_1", "STEP_PREPARE_CANDIDATE_2",
    "STEP_RUN_CANDIDATE_1", "STEP_RUN_CANDIDATE_2",
    "binding_rules_for_template", "candidate_slot_for_step",
    "step_ids_for_template",
    "EVENT_TERMINAL", "EVENT_EAP_FEEDBACK", "FLOW_BINDING_RULES",
    "FLOW_STEP_IDS", "FLOW_TASK_EVENT_TYPES", "FLOW_TEMPLATE_ID",
    "OPERATION_ANALYZING", "OPERATION_IDLE", "OPERATION_PREPARING_VARIANT",
    "OPERATION_WAITING_SLOT",
    "OPERATION_VERIFYING_INPUTS", "OPERATION_WAITING_EVIDENCE",
    "OPERATION_WAITING_EXECUTION", "RECOVERY_POLICY_OBSERVE_ONLY",
    "SCHEMA_VERSION", "TASK_SNAPSHOT_SCHEMA_VERSION", "SLOT_BASELINE", "SLOT_CANDIDATE", "STEP_COMPARE",
    "STEP_EXPLAIN", "STEP_PREPARE_VARIANT", "STEP_RUN_BASELINE",
    "STEP_RUN_CANDIDATE", "FlowTask", "FlowTaskView", "RunAccessBinding",
    "RunRef", "TaskExecutionBinding", "VariantInputRef",
    "build_flow_task_snapshot", "new_flow_task_id",
]
