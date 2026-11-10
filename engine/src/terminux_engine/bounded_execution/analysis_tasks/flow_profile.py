# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""server 固定的 flow execution profile。

一份由 **daemon 管理者**放置的唯讀 JSON，界定「這個 member 在這個 project
可以對哪一份已封存的來源、用哪一個 pinned image、執行哪一個固定參數比較」。

與 `access_policy.py`（analysis task 的靜態歷史 run 存取政策）**並存且互不 fallback**：
前者管「可以讀哪兩筆既有 run」，這裡管「可以執行哪一個固定 flow」。

邊界（不可放寬）：

- 只能由啟動環境的 `TERMINUX_FLOW_TASK_PROFILE_FILE` 指定**絕對路徑**，
  只載入一份。沒有寬鬆預設。
- **拒絕**未知欄位、重複 JSON key，以及任何 shell／command／工具路徑／
  Docker options 欄位——profile 的角色是**限制**可做的工作，不是描述怎麼做。
- profile 不能代替使用者的 `/task-start`：action 仍需 live task grant。
- 不 hot reload：啟動時載入一次，之後每個授權點只確認檔案仍在且 digest
  與啟動時一致。變更要重啟 daemon，舊 grant 不接續。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks.canonical import sha256_payload
from terminux_engine.bounded_execution.proposal_intake import (
    ProposalIntake, ProposalIntakeError, build_proposal_intake,
)

#: 唯一的設定來源。只讀 daemon 啟動環境。
PROFILE_ENV = "TERMINUX_FLOW_TASK_PROFILE_FILE"

SCHEMA_VERSION = 1
#: 新增的 schema。v1 完全保留——舊 profile、舊草案、歷史讀取都不變。
SCHEMA_VERSION_BOUNDED_SEARCH = 2
#: setup repair。v1／v2 的字彙、digest 材料、上限與結果讀取不變。
SCHEMA_VERSION_SETUP_REPAIR = 3
#: 只有 v4 的新 task 才有「解說被拒不阻擋已授權工程」的行為。
SCHEMA_VERSION_SETUP_REPAIR_V2 = 4
#: 速度導向重合成。ABC_AREA 1→0，候選沿用 baseline 的實體邊界。
#: v1～v4 的字彙、digest 材料、上限與結果讀取完全不變。
SCHEMA_VERSION_SPEED_RESYNTHESIS = 5
#: 固定 RTL 算術結構修改（prefix-borrow）。**沒有** v0.7 規劃的
#: schema 6——那條 script recipe 未完成，保留為歷史，不移用它的修改權。
SCHEMA_VERSION_PREFIX_BORROW = 7
#: 同一份凍結的 RTL patch，但 mapped pair 的 proof 改用
#: canonical transition miter（`yosys_transition_miter.v1`）。
#: **沒有** schema 8／9——那兩條（SEC、EQY）都未通過 qualification，保留為歷史。
SCHEMA_VERSION_TRANSITION_MITER = 10
#: 受控的 `SETUP_SLACK_MARGIN` 修復。**沒有** RTL patch、沒有合成策略切換；
#: 兩側唯一差異是一個 ORFS 既有、documented 且由 pinned source 證明會被
#: `repair_timing` 消費的 timing 參數。proof 沿用 v0.12 的 canonical
#: transition miter，但 envelope 必須是 schema v2（帶工具身分）。
SCHEMA_VERSION_SETUP_MARGIN = 11
# 候選不屬於 Profile，只能由 Package-validated proposal 帶入 task。
SCHEMA_VERSION_CORE_UTILIZATION = 12
SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (
    SCHEMA_VERSION, SCHEMA_VERSION_BOUNDED_SEARCH, SCHEMA_VERSION_SETUP_REPAIR,
    SCHEMA_VERSION_SETUP_REPAIR_V2, SCHEMA_VERSION_SPEED_RESYNTHESIS,
    SCHEMA_VERSION_PREFIX_BORROW, SCHEMA_VERSION_TRANSITION_MITER,
    SCHEMA_VERSION_SETUP_MARGIN, SCHEMA_VERSION_CORE_UTILIZATION)

#: flow task 的固定 flow 範本（schema v1）。
FIXED_TEMPLATE_ID = "orfs.run_parameter_compare.v1"
#: bounded utilization search 的固定 flow 範本（schema v2）：一個 baseline ＋ 兩個候選 ＋ 固定選擇。
BOUNDED_SEARCH_TEMPLATE_ID = "orfs.bounded_utilization_search.v1"
#: setup repair 的固定 flow 範本：baseline ＋ 一次 ECO candidate ＋ 修復判定。
SETUP_REPAIR_TEMPLATE_ID = "orfs.setup_repair.v1"
SETUP_REPAIR_V2_TEMPLATE_ID = "orfs.setup_repair.v2"
#: setup repair 的範本：baseline(ABC_AREA=1) ＋ candidate(ABC_AREA=0) ＋ 修復判定。
SETUP_REPAIR_V3_TEMPLATE_ID = "orfs.setup_repair.v3"
#: setup repair 的範本：baseline(原 RTL) ＋ candidate(已核准 RTL patch) ＋ 修復判定。
SETUP_REPAIR_V5_TEMPLATE_ID = "orfs.setup_repair.v5"
#: setup repair 的範本：與 v5 同一份 RTL patch，proof 改用 transition miter。
SETUP_REPAIR_V8_TEMPLATE_ID = "orfs.setup_repair.v8"
#: parametric setup repair 的固定範本。
SETUP_REPAIR_V9_TEMPLATE_ID = "orfs.setup_repair.v9"
CORE_UTILIZATION_TEMPLATE_ID = "orfs.eap.core_utilization.v1"
TEMPLATE_BY_SCHEMA: dict[int, str] = {
    SCHEMA_VERSION: FIXED_TEMPLATE_ID,
    SCHEMA_VERSION_BOUNDED_SEARCH: BOUNDED_SEARCH_TEMPLATE_ID,
    SCHEMA_VERSION_SETUP_REPAIR: SETUP_REPAIR_TEMPLATE_ID,
    SCHEMA_VERSION_SETUP_REPAIR_V2: SETUP_REPAIR_V2_TEMPLATE_ID,
    SCHEMA_VERSION_SPEED_RESYNTHESIS: SETUP_REPAIR_V3_TEMPLATE_ID,
    SCHEMA_VERSION_PREFIX_BORROW: SETUP_REPAIR_V5_TEMPLATE_ID,
    SCHEMA_VERSION_TRANSITION_MITER: SETUP_REPAIR_V8_TEMPLATE_ID,
    SCHEMA_VERSION_SETUP_MARGIN: SETUP_REPAIR_V9_TEMPLATE_ID,
    SCHEMA_VERSION_CORE_UTILIZATION: CORE_UTILIZATION_TEMPLATE_ID,
}
SETUP_REPAIR_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_TEMPLATE_ID, SETUP_REPAIR_V2_TEMPLATE_ID,
     SETUP_REPAIR_V3_TEMPLATE_ID, SETUP_REPAIR_V5_TEMPLATE_ID,
     SETUP_REPAIR_V8_TEMPLATE_ID, SETUP_REPAIR_V9_TEMPLATE_ID})
#: 只有 v3 走「合成策略切換 ＋ 沿用 baseline geometry」這條路。
RESYNTHESIS_TEMPLATES: frozenset[str] = frozenset({SETUP_REPAIR_V3_TEMPLATE_ID})
#: 只有 v0.8 的範本可以修改隔離候選的 RTL。舊 template 一律取不到這個權限。
PREFIX_BORROW_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V5_TEMPLATE_ID, SETUP_REPAIR_V8_TEMPLATE_ID})
#: 只有 v0.12 的範本用 canonical transition miter 當 mapped pair 的 proof。
TRANSITION_MITER_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V8_TEMPLATE_ID, SETUP_REPAIR_V9_TEMPLATE_ID})
#: 參數式 setup 修復。
SETUP_MARGIN_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V9_TEMPLATE_ID})
#: 沿用 baseline 實體邊界（exact core/die）的 template 全集。
EXACT_GEOMETRY_TEMPLATES: frozenset[str] = frozenset(
    {SETUP_REPAIR_V3_TEMPLATE_ID, SETUP_REPAIR_V5_TEMPLATE_ID,
     SETUP_REPAIR_V8_TEMPLATE_ID})

#: 嚴格枚舉：不是 client／provider 可傳的任意 policy 或 continue-on-error 清單。
FIXED_NARRATIVE_FAILURE_POLICY_ID = "r5.reference_rejection_nonblocking.v1"

#: setup repair 固定的 recipe 與驗收 policy 身分。profile 不能改成別的。
FIXED_REPAIR_RECIPE_ID = "gcd.delay_restructure.v1"
FIXED_REPAIR_POLICY_ID = "gcd.setup_repair_acceptance.v1"
#: 速度導向重合成的固定身分。全部是嚴格枚舉值。
FIXED_RESYNTHESIS_RECIPE_ID = "gcd.speed_resynthesis.v1"
FIXED_RESYNTHESIS_POLICY_ID = "gcd.setup_resynthesis_acceptance.v1"
FIXED_GEOMETRY_POLICY_ID = "gcd.exact_baseline_bounds.v1"
FIXED_RESYNTHESIS_COMPARISON_POLICY_ID = "gcd.same_requirements_resynthesis.v1"
#: 固定 RTL patch 的身分。全部是嚴格枚舉值，client／模型不得提供。
FIXED_PREFIX_BORROW_RECIPE_ID = "gcd.prefix_borrow.v1"
FIXED_PREFIX_BORROW_POLICY_ID = "gcd.prefix_borrow_acceptance.v1"
FIXED_PREFIX_BORROW_COMPARISON_POLICY_ID = "gcd.same_requirements_prefix_borrow.v1"
#: setup repair 的固定身分（Transition miter 與 proof policy 的不可變值）。
FIXED_MITER_RECIPE_ID = "gcd.prefix_borrow_transition_miter.v1"
FIXED_MITER_PROOF_POLICY_ID = "yosys_transition_miter.v1"
#: acceptance 沿 v0.8 的每一條工程條件，只多要求 proof 出自上面那個 policy。
FIXED_MITER_POLICY_ID = "gcd.prefix_borrow_miter_acceptance.v1"
#: 本版唯一允許被 patch 的 source 路徑與兩個 module。
FIXED_RTL_PATCH_PATH = "flow/designs/src/gcd/gcd.v"
FIXED_RTL_PATCH_MODULES: tuple[str, ...] = (
    "LtComparator_0x422b1f52edd46a85", "Subtractor_0x422b1f52edd46a85")
#: v0.8 兩側都用原 area mapping，所以策略變數兩端相同。
PREFIX_BORROW_PARAMETER_NAME = "ABC_AREA"

#: parametric setup repair 的固定身分。全部嚴格枚舉，client／模型都不得提供。
FIXED_SETUP_MARGIN_RECIPE_ID = "aes.setup_slack_margin.v1"
FIXED_SETUP_MARGIN_POLICY_ID = "aes.setup_slack_margin_acceptance.v1"
FIXED_SETUP_MARGIN_COMPARISON_POLICY_ID = "aes.same_requirements_setup_margin.v1"
#: 唯一允許被改動的 ORFS 變數。**不是**可由 profile 任意指定的名稱。
FIXED_SETUP_MARGIN_VARIABLE = "SETUP_SLACK_MARGIN"
#: baseline 必須是 ORFS 的 documented 預設值（0＝不 overfix）。
FIXED_SETUP_MARGIN_BASELINE_VALUE = "0"
#: pinned source 內證明會消費這個變數的階段（`scripts/variables.yaml` 的
#: `stages` 與 `scripts/cts.tcl`／`scripts/global_route.tcl` 的呼叫點）。
FIXED_SETUP_MARGIN_STAGES: tuple[str, ...] = ("cts", "floorplan", "grt")
PREFIX_BORROW_BASELINE_VALUE = "1"
PREFIX_BORROW_CANDIDATE_VALUE = "1"
#: v5 切換的合成策略變數與兩端固定值。
RESYNTHESIS_PARAMETER_NAME = "ABC_AREA"
RESYNTHESIS_BASELINE_VALUE = "1"
RESYNTHESIS_CANDIDATE_VALUE = "0"

#: bounded utilization search 固定的 selection policy 身分。profile 不能改成別的 policy。
FIXED_SELECTION_POLICY_ID = "gcd.setup_area_bounded.v1"

#: 本批固定的參數名稱。profile 不能改成別的參數。
FIXED_PARAMETER_NAME = "CORE_UTILIZATION"

SLOT_BASELINE = "baseline"
SLOT_CANDIDATE = "candidate"
#: bounded utilization search 的兩個候選 slot。它們只能由 bounded utilization search 的 live binding 派生——v1 profile 不會
#: 因為 action enum 變大就取得第三次 run。
SLOT_CANDIDATE_1 = "candidate_1"
SLOT_CANDIDATE_2 = "candidate_2"
SLOTS: tuple[str, ...] = (SLOT_BASELINE, SLOT_CANDIDATE)
BOUNDED_SEARCH_SLOTS: tuple[str, ...] = (
    SLOT_BASELINE, SLOT_CANDIDATE_1, SLOT_CANDIDATE_2)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Docker image 身分：`sha256:<64 hex>`。**不接受可漂移的 tag**。
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "profile_id", "template_id", "member_id", "project_id",
    "source_root", "input_snapshot_root", "input_manifest_path",
    "input_manifest_sha256", "design_config", "design_id", "platform_id",
    "docker_image_id", "execution_root", "dqe_db_path", "artifact_root",
    "parameter", "limits",
})
_CORE_UTILIZATION_POLICY_FIELDS = frozenset({
    "method_identity", "global_minimum", "global_maximum", "max_decrement",
    "max_candidates",
})
_PARAMETER_FIELDS = frozenset({"name", "baseline", "candidate"})
#: v2 用固定候選序列取代單一 candidate。
_PARAMETER_FIELDS_V2 = frozenset({"name", "baseline", "candidates"})
_SELECTION_POLICY_FIELDS = frozenset({"policy_id", "area_ratio_limit"})
#: v3 的 recipe：固定演算法與範圍，不接受任意 Tcl／shell／檔案路徑。
_RECIPE_FIELDS = frozenset({
    "recipe_id", "algorithm", "target", "slack_threshold", "depth_threshold",
    "invocations", "helper_dir", "helpers", "config_suffix_sha256",
    "candidate_config_suffix_sha256", "acceptance_policy_id",
})
_HELPER_FIELDS = frozenset({"relative_path", "sha256"})
#: parametric setup repair 的參數值：純十進位 token（允許小數），不接受運算式、單位或符號。
_DECIMAL_TOKEN_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
#: parametric setup repair 的 recipe 欄位。沒有 restructure 的演算法欄位，也沒有 rtl_patch。
_SETUP_MARGIN_RECIPE_FIELDS = frozenset({
    "recipe_id", "method", "helper_dir", "helpers", "config_suffix_sha256",
    "candidate_config_suffix_sha256", "acceptance_policy_id",
    "comparison_policy_id", "proof_policy_id", "qualified_tool_identity_path",
})
#: RC-4：可選的 qualified tool identity 檔案 bytes hash。給了就綁進 digest 並在
#: 選用、work input 與 proof reference 建立時逐一核對；不給時舊 Profile 的
#: canonical 材料與 digest 完全不變。
_SETUP_MARGIN_RECIPE_OPTIONAL_FIELDS = frozenset({
    "qualified_tool_identity_sha256", "target_slack_ns",
})
#: `method` 的固定欄位集合。
_SETUP_MARGIN_METHOD_FIELDS = frozenset({
    "variable", "baseline_value", "candidate_value", "consumption_stages",
    "consumption_evidence",
})
#: v5 的 recipe：切換合成策略，沒有 restructure 的演算法／門檻欄位。
_RESYNTHESIS_RECIPE_FIELDS = frozenset({
    "recipe_id", "strategy_variable", "baseline_value", "candidate_value",
    "helper_dir", "helpers", "config_suffix_sha256", "acceptance_policy_id",
    "geometry_policy_id", "comparison_policy_id",
})
#: v0.8 的 recipe：只多一個 `rtl_patch`，沒有任何合成策略／演算法選項。
_PREFIX_RECIPE_FIELDS = frozenset({
    "recipe_id", "helper_dir", "helpers", "config_suffix_sha256",
    "acceptance_policy_id", "geometry_policy_id", "comparison_policy_id",
    "rtl_patch",
})
#: v0.12 的 recipe：v0.8 的欄位再加一個固定的 `proof_policy_id`。
_MITER_RECIPE_FIELDS = frozenset({
    "recipe_id", "helper_dir", "helpers", "config_suffix_sha256",
    "acceptance_policy_id", "geometry_policy_id", "comparison_policy_id",
    "rtl_patch", "proof_policy_id",
    # qualification 的工具身分檔（host 提供、唯讀）。**可選**：
    # 省略時 `to_json_dict()` 不輸出這個 key，因此既有 v0.12 profile 的
    # digest 完全不變（不重寫 setup repair 既有的歷史身分）。
    "qualified_tool_identity_path",
})
#: `rtl_patch` 的固定欄位。多一個或少一個都拒絕。
_RTL_PATCH_FIELDS = frozenset({
    "path", "before_sha256", "after_sha256", "patch_sha256", "modules",
})
#: v3 額外的 limits。v1/v2 的欄位集合不變。
_LIMIT_FIELDS_V3 = frozenset({
    "eda_launches", "candidate_writes", "body_llm_dispatches",
    "runtime_retries", "task_deadline_sec", "run_timeout_sec",
    "restructure_invocations", "equivalence_timeout_sec",
})
_LIMIT_FIELDS = frozenset({
    "eda_launches", "candidate_writes", "body_llm_dispatches",
    "runtime_retries", "task_deadline_sec", "run_timeout_sec",
})

#: 這些名稱一律拒絕——profile 不得攜帶任何可執行內容。
#:
#: 用**精確名稱**而不是子字串：`execution_root` 這種合法欄位含有 "exec"，
#: 子字串比對會把它誤擋。頂層未知欄位本來就會被 `_TOP_LEVEL_FIELDS` 擋下，
#: 這個掃描是針對**巢狀**內容的縱深防禦。
_FORBIDDEN_FIELD_NAMES = frozenset({
    "command", "commands", "cmd", "shell", "argv", "args", "entrypoint",
    "docker_options", "docker_args", "docker_flags", "env", "environment",
    "script", "scripts", "exec", "mount", "mounts", "volume", "volumes",
    "network", "network_mode", "privileged", "user", "cap_add",
})

#: 已知的合法 schema 欄位——不參與上面的禁用檢查。
_KNOWN_FIELD_NAMES = frozenset()  # 於模組尾端填入（見下方賦值）

#: 本批固定的絕對路徑欄位。全部必須是絕對路徑。
_ABSOLUTE_PATH_FIELDS = (
    "source_root", "input_snapshot_root", "input_manifest_path",
    "execution_root", "dqe_db_path", "artifact_root",
)

#: user 可在 Profile 內宣告**怎麼執行**。這是唯一允許出現執行內容
#: 的位置（`_reject_executable_fields` 只豁免頂層的這一棵子樹）。內容會完整
#: 展開到 DRAFT、寫進 lineage；Terminux 不因內容拒絕執行——OS 權限才是邊界。
_EXECUTION_FIELDS = frozenset({
    "runner", "docker_executable", "docker_args", "host_env_script",
    "host_make_executable", "host_tool_env", "proof_tool", "user_declared",
    "fixture_corpus", "artifact_role_mapping", "durable_baseline_source",
    "native_tool_binding",
})
#: host-native 工具綁定（append-only）。宣告了才進 execution 子樹 ⇒ 進
#: profile digest；既有 Profile 沒有這個鍵，digest 一個位元都不變。
NATIVE_TOOL_BINDING_SCHEMA = "terminux.flow_task.native_tool_binding.v1"
_NATIVE_BINDING_FIELDS = frozenset({"schema", "tools", "env_script"})
#: flow 必須釘住的工具；`FORMAL_YOSYS` 只有需要相依 formal proof 時才宣告。
NATIVE_REQUIRED_TOOLS = frozenset({"OPENROAD_EXE", "YOSYS_EXE", "MAKE"})
NATIVE_OPTIONAL_TOOLS = frozenset({"FORMAL_YOSYS"})
#: 跨 daemon instance 重用既有 baseline 時，Profile 釘住的來源身分。宣告了就
#: 進 execution plan ⇒ 進 task digest，因此是授權前固定的 operator 宣告，而
#: 不是 RPC 參數。內容由 `durable_baseline` 驗證。
_DURABLE_BASELINE_SOURCE_FIELDS = frozenset({
    "source_task_id", "source_task_digest", "source_profile_id",
    "source_profile_digest", "reference_digest", "event_store_path",
    # 重用既有 run 時它的 DQE 仍屬於來源；位置只能由 Profile 釘住，
    # 宣告進 profile digest 再進 task digest，RPC 不得提供任意路徑。
    "artifact_root", "reference_path", "dqe_db_path"})
#: Package 的 artifact role allowlist 釘住宣告（version ＋ canonical digest）。
#: 宣告了就會進 execution plan ⇒ 進 task digest；派送前與 Package 現行 mapping
#: 核對，不符即拒絕（不降級沿用預設）。
_ARTIFACT_ROLE_MAPPING_FIELDS = frozenset({"version", "digest"})
#: qualification 專用：由 Profile 宣告的 fixture corpus 身分。宣告了就代表
#: 這個 Profile 的執行是 **fixture replay**，不是真實 EDA run。
_FIXTURE_CORPUS_FIELDS = frozenset({"purpose", "adapter_schema", "manifest_path",
                                    "manifest_sha256"})
#: fixture corpus 的唯一合法用途。宣告它就等於宣告「這不是 production 執行」。
QUALIFICATION_ONLY = "QUALIFICATION_ONLY"
#: `host_native`：以 `native_tool_binding` 釘住站點工具（realpath＋SHA-256）的
#: host-native 執行身分；`docker`／`host` 是舊執行身分，只供歷史 Profile 讀取與
#: 比對，新的 run 一律在執行邊界拒絕。
NATIVE_RUNNER = "host_native"
_EXECUTION_RUNNERS = frozenset({"docker", "host", NATIVE_RUNNER})
#: host runner 可覆寫的 ORFS 工具路徑變數（ORFS `Makefile`／`env.sh` 認得的名稱）。
_HOST_TOOL_ENV_KEYS = frozenset({"OPENROAD_EXE", "YOSYS_EXE", "KLAYOUT_CMD", "STA_EXE"})
_PROOF_TOOL_FIELDS = frozenset({"executable_realpath", "executable_sha256", "version_sha256"})
#: user 自述的 qualification／標籤。**永遠**是 `user_declared`，不會被提升成 system_verified。
_USER_DECLARED_FIELDS = frozenset({"qualification", "labels", "notes"})
#: ORFS 變數命名（`parameter.name`／`method.variable`）。
_VARIABLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: 拒絕碼：profile 來源（path／inline）本身不合法。
PROFILE_SOURCE_INVALID = "FLOW_TASK_PROFILE_SOURCE_INVALID"

# ── 拒絕碼 ────────────────────────────────────────────────────────────

PROFILE_NOT_CONFIGURED = "FLOW_TASK_PROFILE_NOT_CONFIGURED"
PROFILE_UNREADABLE = "FLOW_TASK_PROFILE_UNREADABLE"
PROFILE_INVALID = "FLOW_TASK_PROFILE_INVALID"
PROFILE_CHANGED = "FLOW_TASK_PROFILE_CHANGED"


class FlowProfileError(RuntimeError):
    """profile 缺席、不可讀或不合法。一律 fail-closed，不降級。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── 模型 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FlowParameter:
    """本批唯一允許改變的工程參數。"""

    name: str
    baseline: str
    candidate: str | None

    #: v2 的固定候選序列（依序執行）。v1 是單一候選，這裡就是 `(candidate,)`。
    candidates: tuple[str, ...] = ()

    def value_for(self, slot: str) -> str:
        if slot == SLOT_BASELINE:
            return self.baseline
        if slot == SLOT_CANDIDATE:
            if self.candidate is None:
                raise FlowProfileError(PROFILE_INVALID, "this Profile has no predeclared candidate")
            return self.candidate
        # candidate_1 / candidate_2 → 依序取候選值。
        for index, name in enumerate(
                (SLOT_CANDIDATE_1, SLOT_CANDIDATE_2)):
            if slot == name:
                if index >= len(self.candidates):
                    raise FlowProfileError(
                        PROFILE_INVALID,
                        f"this profile declares no value for slot {slot!r}")
                return self.candidates[index]
        raise FlowProfileError(PROFILE_INVALID, f"unknown slot: {slot!r}")

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "baseline": self.baseline}
        if self.candidate is not None:
            payload["candidate"] = self.candidate
        if self.candidates and tuple(self.candidates) != (self.candidate,):
            payload["candidates"] = list(self.candidates)
        return payload


@dataclass(frozen=True)
class SelectionPolicy:
    """固定的 deterministic 選擇規則身分與門檻。

    完整內容進 profile digest，因此也進 task digest——換門檻就是換一個 task。
    """

    policy_id: str
    area_ratio_limit: float

    def to_json_dict(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id,
                "area_ratio_limit": self.area_ratio_limit}


@dataclass(frozen=True)
class RepairHelperRef:
    """一個已核准的固定 helper 檔案身分。bytes 由 server 準備並固定。"""

    relative_path: str
    sha256: str

    def to_json_dict(self) -> dict[str, Any]:
        return {"relative_path": self.relative_path, "sha256": self.sha256}


@dataclass(frozen=True)
class RepairRecipe:
    """唯一被核准的工程修法。

    整份內容進 profile digest，因此也進 task digest——換演算法、換門檻、
    換任何一個 helper 的 bytes，就是換一個 task。
    """

    recipe_id: str
    algorithm: str
    target: str
    slack_threshold: float
    depth_threshold: int
    invocations: int
    helper_dir: str
    helpers: tuple[RepairHelperRef, ...]
    #: baseline 與 candidate **共同**追加到 design config 的內容。
    config_suffix_sha256: str
    #: candidate **額外**追加的那一段（唯一的工程處理差異）。
    candidate_config_suffix_sha256: str
    acceptance_policy_id: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id, "algorithm": self.algorithm,
            "target": self.target, "slack_threshold": self.slack_threshold,
            "depth_threshold": self.depth_threshold,
            "invocations": self.invocations, "helper_dir": self.helper_dir,
            "helpers": [h.to_json_dict() for h in self.helpers],
            "config_suffix_sha256": self.config_suffix_sha256,
            "candidate_config_suffix_sha256": self.candidate_config_suffix_sha256,
            "acceptance_policy_id": self.acceptance_policy_id,
        }


@dataclass(frozen=True)
class ResynthesisRecipe:
    """唯一被核准的速度導向重合成。

    與 `RepairRecipe` 分開是刻意的——這個修法沒有 restructure 的演算法與
    門檻，共同點只有「整份內容進 profile digest」。混用同一個結構會讓
    v1～v4 的 digest 材料被動搖。
    """

    recipe_id: str
    #: 被切換的合成策略變數；本批固定 `ABC_AREA`。
    strategy_variable: str
    baseline_value: str
    candidate_value: str
    helper_dir: str
    helpers: tuple[RepairHelperRef, ...]
    #: baseline 與 candidate **共同**追加的證據／LEC 段。
    config_suffix_sha256: str
    acceptance_policy_id: str
    #: candidate 沿用 baseline 實體邊界的固定規則。
    geometry_policy_id: str
    #: 本版判定「同要求」的固定 policy。
    comparison_policy_id: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "strategy_variable": self.strategy_variable,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "helper_dir": self.helper_dir,
            "helpers": [h.to_json_dict() for h in self.helpers],
            "config_suffix_sha256": self.config_suffix_sha256,
            "acceptance_policy_id": self.acceptance_policy_id,
            "geometry_policy_id": self.geometry_policy_id,
            "comparison_policy_id": self.comparison_policy_id,
        }


@dataclass(frozen=True)
class RtlPatch:
    """setup repair 的唯一固定 RTL 修改。bytes 由 host 事先凍結，hash 綁進 digest。

    `modules` 只是宣告修改範圍，供比較與判定使用；它**不是**授權 CLI 去改
    這些 module 以外的任何東西。patch 本體與前後 hash 一致才允許套用。
    """

    path: str
    before_sha256: str
    after_sha256: str
    patch_sha256: str
    modules: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "patch_sha256": self.patch_sha256,
            "modules": list(self.modules),
        }


@dataclass(frozen=True)
class TransitionMiterRecipe:
    """setup repair 的 recipe：與 v0.8 完全同一份 RTL patch，只換 mapped proof。

    `proof_policy_id` 是嚴格枚舉值（`yosys_transition_miter.v1`）；client 與
    模型都不能選 proof 方法，舊 recipe 也不會自動切換。
    """

    recipe_id: str
    helper_dir: str
    helpers: tuple[RepairHelperRef, ...]
    config_suffix_sha256: str
    acceptance_policy_id: str
    geometry_policy_id: str
    comparison_policy_id: str
    rtl_patch: RtlPatch
    proof_policy_id: str
    #: qualification 工具身分檔的 host 路徑。可選；None 時完全不出現
    #: 在序列化結果裡，既有 profile 的 digest 因此不受影響。
    qualified_tool_identity_path: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "recipe_id": self.recipe_id,
            "helper_dir": self.helper_dir,
            "helpers": [h.to_json_dict() for h in self.helpers],
            "config_suffix_sha256": self.config_suffix_sha256,
            "acceptance_policy_id": self.acceptance_policy_id,
            "geometry_policy_id": self.geometry_policy_id,
            "comparison_policy_id": self.comparison_policy_id,
            "rtl_patch": self.rtl_patch.to_json_dict(),
            "proof_policy_id": self.proof_policy_id,
        }
        if self.qualified_tool_identity_path is not None:
            payload["qualified_tool_identity_path"] = \
                self.qualified_tool_identity_path
        return payload


@dataclass(frozen=True)
class SetupMarginMethod:
    """parametric setup repair 的固定方法：一個 ORFS timing 參數，兩側只差它的值。"""

    variable: str
    baseline_value: str
    candidate_value: str
    #: pinned source 證明會消費這個變數的階段。
    consumption_stages: tuple[str, ...]
    #: pinned source 內的消費點（檔案:行號 → 說明）。純證據，不參與判定。
    consumption_evidence: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "consumption_stages": list(self.consumption_stages),
            "consumption_evidence": list(self.consumption_evidence),
        }


@dataclass(frozen=True)
class SetupMarginRecipe:
    """parametric setup repair 的 recipe：受控的 `SETUP_SLACK_MARGIN` 修復。

    沒有 RTL patch、沒有合成策略切換、沒有 ECO script——兩側唯一的差異就是
    `method` 描述的那一個參數值，而且它必須是固定枚舉值。
    """

    recipe_id: str
    method: SetupMarginMethod
    helper_dir: str
    helpers: tuple[RepairHelperRef, ...]
    config_suffix_sha256: str
    candidate_config_suffix_sha256: str
    acceptance_policy_id: str
    comparison_policy_id: str
    proof_policy_id: str
    #: qualification 工具身分檔（host 提供、唯讀）。parametric setup repair **必填**。
    qualified_tool_identity_path: str
    #: RC-4：工具身分檔 bytes 的 SHA-256。可選；給了就必須逐次相符。
    qualified_tool_identity_sha256: str | None = None
    #: 可選的目標 setup 餘量（ns）。未指定時維持預設零違規驗收。
    target_slack_ns: float | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "method": self.method.to_json_dict(),
            "helper_dir": self.helper_dir,
            "helpers": [h.to_json_dict() for h in self.helpers],
            "config_suffix_sha256": self.config_suffix_sha256,
            "candidate_config_suffix_sha256": self.candidate_config_suffix_sha256,
            "acceptance_policy_id": self.acceptance_policy_id,
            "comparison_policy_id": self.comparison_policy_id,
            "proof_policy_id": self.proof_policy_id,
            "qualified_tool_identity_path": self.qualified_tool_identity_path,
            **({"qualified_tool_identity_sha256": self.qualified_tool_identity_sha256}
               if self.qualified_tool_identity_sha256 is not None else {}),
            **({"target_slack_ns": self.target_slack_ns}
               if self.target_slack_ns is not None else {}),
        }


@dataclass(frozen=True)
class PrefixBorrowRecipe:
    """setup repair 的 recipe：一個固定 RTL patch，沒有任何合成策略選項。

    兩側都用原 area mapping，所以這裡刻意**沒有** `strategy_variable`——
    「候選有沒有換 script」不是這個 recipe 的判準。
    """

    recipe_id: str
    helper_dir: str
    helpers: tuple[RepairHelperRef, ...]
    config_suffix_sha256: str
    acceptance_policy_id: str
    geometry_policy_id: str
    comparison_policy_id: str
    rtl_patch: RtlPatch

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "helper_dir": self.helper_dir,
            "helpers": [h.to_json_dict() for h in self.helpers],
            "config_suffix_sha256": self.config_suffix_sha256,
            "acceptance_policy_id": self.acceptance_policy_id,
            "geometry_policy_id": self.geometry_policy_id,
            "comparison_policy_id": self.comparison_policy_id,
            "rtl_patch": self.rtl_patch.to_json_dict(),
        }


@dataclass(frozen=True)
class FlowLimits:
    """固定上限。這些是**上限**，不是預估耗時。"""

    eda_launches: int
    candidate_writes: int
    body_llm_dispatches: int
    runtime_retries: int
    task_deadline_sec: int
    run_timeout_sec: int
    #: v3 才有。v1／v2 為 `None`，因此它們的 digest 材料完全不變。
    restructure_invocations: int | None = None
    equivalence_timeout_sec: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "eda_launches": self.eda_launches,
            "candidate_writes": self.candidate_writes,
            "body_llm_dispatches": self.body_llm_dispatches,
            "runtime_retries": self.runtime_retries,
            "task_deadline_sec": self.task_deadline_sec,
            "run_timeout_sec": self.run_timeout_sec,
        }
        if self.restructure_invocations is not None:
            payload["restructure_invocations"] = self.restructure_invocations
        if self.equivalence_timeout_sec is not None:
            payload["equivalence_timeout_sec"] = self.equivalence_timeout_sec
        return payload


@dataclass(frozen=True)
class ProofToolDeclaration:
    """user 宣告的 proof 工具身分。只是 metadata；實際身分由 run 觀測。"""

    executable_realpath: str
    executable_sha256: str | None = None
    version_sha256: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "executable_realpath": self.executable_realpath,
            **({"executable_sha256": self.executable_sha256}
               if self.executable_sha256 else {}),
            **({"version_sha256": self.version_sha256}
               if self.version_sha256 else {}),
        }


@dataclass(frozen=True)
class UserDeclared:
    """user 自述的 qualification／標籤／備註。`system_verified` 永遠為 False。"""

    qualification: str | None = None
    labels: tuple[str, ...] = ()
    notes: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            **({"qualification": self.qualification} if self.qualification else {}),
            **({"labels": list(self.labels)} if self.labels else {}),
            **({"notes": self.notes} if self.notes else {}),
            "system_verified": False,
        }


@dataclass(frozen=True)
class FixtureCorpusDeclaration:
    """Profile 宣告的 fixture corpus 身分（adapter schema ＋ manifest digest）。

    宣告即代表本 Profile 的執行由 corpus replay executable 提供，**不是**真實
    EDA run；身分隨 execution plan 固定進 task digest。
    """

    purpose: str
    adapter_schema: str
    manifest_path: str
    manifest_sha256: str

    def to_json_dict(self) -> dict[str, Any]:
        return {"purpose": self.purpose,
                "adapter_schema": self.adapter_schema,
                "manifest_path": self.manifest_path,
                "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True)
class ArtifactRoleMappingDeclaration:
    """Profile 釘住的 artifact role allowlist 身分（由 Package 提供內容）。

    `version` 是 Package 的 mapping 版本，`digest` 是該 mapping 文件的 canonical
    SHA-256。內容不在 Profile 內重複宣告——Profile 只釘住「是哪一份」。
    """

    version: str
    digest: str

    def to_json_dict(self) -> dict[str, Any]:
        return {"version": self.version, "digest": self.digest}


@dataclass(frozen=True)
class NativeToolBinding:
    """站點工具的授權 pin：每個工具與 env script 都是 realpath ＋ SHA-256。

    版本字串只是描述，不在這裡；身分以內容雜湊為準。
    """

    tools: dict[str, Any]
    env_script: Any

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema": NATIVE_TOOL_BINDING_SCHEMA,
            "tools": {name: self.tools[name].to_json_dict()
                      for name in sorted(self.tools)},
            "env_script": self.env_script.to_json_dict(),
        }

    @property
    def digest(self) -> str:
        return sha256_payload(self.to_json_dict())


@dataclass(frozen=True)
class ExecutionSpec:
    """Profile 內的執行宣告。全部會展開到 DRAFT 並寫進 lineage。"""

    runner: str = "docker"
    docker_executable: str | None = None
    docker_args: tuple[str, ...] = ()
    host_env_script: str | None = None
    host_make_executable: str | None = None
    host_tool_env: dict[str, str] = field(default_factory=dict)
    proof_tool: ProofToolDeclaration | None = None
    user_declared: UserDeclared | None = None
    fixture_corpus: FixtureCorpusDeclaration | None = None
    artifact_role_mapping: ArtifactRoleMappingDeclaration | None = None
    durable_baseline_source: dict[str, str] | None = None
    native_tool_binding: NativeToolBinding | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            **({"docker_executable": self.docker_executable}
               if self.docker_executable else {}),
            **({"docker_args": list(self.docker_args)} if self.docker_args else {}),
            **({"host_env_script": self.host_env_script}
               if self.host_env_script else {}),
            **({"host_make_executable": self.host_make_executable}
               if self.host_make_executable else {}),
            **({"host_tool_env": dict(self.host_tool_env)}
               if self.host_tool_env else {}),
            **({"proof_tool": self.proof_tool.to_json_dict()}
               if self.proof_tool is not None else {}),
            **({"user_declared": self.user_declared.to_json_dict()}
               if self.user_declared is not None else {}),
            **({"fixture_corpus": self.fixture_corpus.to_json_dict()}
               if self.fixture_corpus is not None else {}),
            **({"artifact_role_mapping": self.artifact_role_mapping.to_json_dict()}
               if self.artifact_role_mapping is not None else {}),
            **({"durable_baseline_source": dict(self.durable_baseline_source)}
               if self.durable_baseline_source is not None else {}),
            **({"native_tool_binding": self.native_tool_binding.to_json_dict()}
               if self.native_tool_binding is not None else {}),
        }


@dataclass(frozen=True)
class ProfileSource:
    """task 建立當下 Profile 的來源身分。

    `logical_path` 是 user 提供的路徑，`resolved_path` 是實際解析結果；合法
    symlink 不拒絕但如實記錄。inline 來源沒有路徑，只有 canonical digest。
    """

    kind: str
    logical_path: str | None
    resolved_path: str | None
    is_symlink: bool
    raw_sha256: str
    size_bytes: int
    profile_digest: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "logical_path": self.logical_path,
            "resolved_path": self.resolved_path,
            "is_symlink": self.is_symlink,
            "raw_sha256": self.raw_sha256,
            "size_bytes": self.size_bytes,
            "profile_digest": self.profile_digest,
        }


@dataclass(frozen=True)
class FlowTaskProfile:
    """不可變的已載入 profile。"""

    schema_version: int
    profile_id: str
    template_id: str
    member_id: str
    project_id: str
    source_root: str
    input_snapshot_root: str
    input_manifest_path: str
    input_manifest_sha256: str
    design_config: str
    design_id: str
    platform_id: str
    #: 舊 Profile 的 container image 身分。host-native Profile（宣告了
    #: `execution.native_tool_binding`）一律為 `None`，工具身分改由 binding 提供。
    docker_image_id: str | None
    execution_root: str
    dqe_db_path: str
    artifact_root: str
    parameter: FlowParameter
    limits: FlowLimits
    profile_digest: str
    #: 只有 v2（bounded search）有；v1／v3 一律 `None`。
    selection_policy: SelectionPolicy | None = None
    #: 只有 v3／v4（setup repair）有。
    repair_recipe: RepairRecipe | None = None
    #: 只有 v4 有。v1～v3 一律 `None`——缺欄位不得默認享有新續跑權。
    narrative_failure_policy_id: str | None = None
    #: 可選的執行宣告。既有 profile 沒有這個鍵，digest 不變。
    execution: ExecutionSpec | None = None
    #: Profile 自己宣告的 method 身分（`core_utilization_policy.method_identity`
    #: 或 `repair_recipe.method`）。proposal 尚未存在的 baseline 收集用它，
    #: 不從 design、參數名或候選值推論。
    declared_method_identity: str | None = None
    #: Phase A：可選的 manual proposal intake 宣告。缺席 ⇒ 這個 Profile
    #: **不接受**手動 proposal。宣告了才進 digest／execution plan（既有
    #: Profile 沒有這個鍵，digest 一個位元都不變）。
    proposal_intake: ProposalIntake | None = None

    @property
    def is_setup_repair(self) -> bool:
        return self.template_id in SETUP_REPAIR_TEMPLATES

    @property
    def is_bounded_search(self) -> bool:
        return self.template_id == BOUNDED_SEARCH_TEMPLATE_ID

    @property
    def is_qualification_fixture(self) -> bool:
        """本 Profile 的執行是 fixture corpus replay（QUALIFICATION_ONLY）。"""
        execution = getattr(self, "execution", None)
        return getattr(execution, "fixture_corpus", None) is not None

    @property
    def is_core_utilization(self) -> bool:
        return self.template_id == CORE_UTILIZATION_TEMPLATE_ID

    @property
    def native_tool_binding(self) -> NativeToolBinding | None:
        execution = getattr(self, "execution", None)
        return getattr(execution, "native_tool_binding", None)

    @property
    def is_native(self) -> bool:
        """本 Profile 以站點工具在主機上執行（不使用 container image）。"""
        return self.native_tool_binding is not None

    @property
    def tool_identity_ref(self) -> str:
        """比對用的工具身分代號：舊 Profile 是 image id，native 是 binding digest。"""
        binding = self.native_tool_binding
        if binding is not None:
            return f"native-binding:sha256:{binding.digest}"
        return str(self.docker_image_id)

    @property
    def slots(self) -> tuple[str, ...]:
        return BOUNDED_SEARCH_SLOTS if self.is_bounded_search else SLOTS

    def matches_scope(self, member_id: str, project_id: str) -> bool:
        """member 與 session project 都必須**精確**相符。"""
        return self.member_id == member_id and self.project_id == project_id

    def with_prepared_input(
        self, *, profile_id: str, source_root: str, input_snapshot_root: str,
        input_manifest_path: str, input_manifest_sha256: str,
    ) -> "FlowTaskProfile":
        """建立 work 專用 input binding；不改原 Profile 或其 candidate 設定。"""
        prepared = replace(
            self, profile_id=profile_id, source_root=source_root,
            input_snapshot_root=input_snapshot_root,
            input_manifest_path=input_manifest_path,
            input_manifest_sha256=input_manifest_sha256, profile_digest="")
        return replace(
            prepared,
            profile_digest=sha256_payload(prepared._digest_material()))

    def slot_output_root(self, task_id: str, slot: str) -> Path:
        """本 slot 的專屬可寫輸出目錄。**永遠不覆寫 source_root。**"""
        return Path(self.execution_root) / task_id / slot

    def candidate_source_root(self, task_id: str,
                              slot: str = SLOT_CANDIDATE) -> Path:
        """候選 input snapshot 的根目錄（prepare 步驟才 materialize）。

        每個候選 slot 各有獨立目錄，兩個候選之間沒有共用的可寫路徑。
        v1 的路徑（`candidate_source`）維持不變。
        """
        if slot == SLOT_CANDIDATE:
            return Path(self.execution_root) / task_id / "candidate_source"
        return Path(self.execution_root) / task_id / f"{slot}_source"

    def design_config_path(self, source_root: str | Path) -> Path:
        """在指定 source tree 內的 design config 絕對路徑。"""
        return Path(source_root) / self.design_config

    def _digest_material(self) -> dict[str, Any]:
        """digest 材料**不含**檔案位置——profile 搬位置不改變它的身分。"""
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "template_id": self.template_id,
            "member_id": self.member_id,
            "project_id": self.project_id,
            "source_root": self.source_root,
            "input_snapshot_root": self.input_snapshot_root,
            "input_manifest_path": self.input_manifest_path,
            "input_manifest_sha256": self.input_manifest_sha256,
            "design_config": self.design_config,
            "design_id": self.design_id,
            "platform_id": self.platform_id,
            # host-native Profile 沒有 image；舊 Profile 一律帶這個鍵（位元不變）。
            **({"docker_image_id": self.docker_image_id}
               if self.docker_image_id is not None else {}),
            "execution_root": self.execution_root,
            "dqe_db_path": self.dqe_db_path,
            "artifact_root": self.artifact_root,
            "parameter": self.parameter.to_json_dict(),
            "limits": self.limits.to_json_dict(),
            # v1 沒有這個鍵——`None` 不寫進 digest 材料，v1 的 digest 因此
            # 與 flow task 完全相同（既有 profile 的身分不變）。
            **({"selection_policy": self.selection_policy.to_json_dict()}
               if self.selection_policy is not None else {}),
            **({"repair_recipe": self.repair_recipe.to_json_dict()}
               if self.repair_recipe is not None else {}),
            **({"narrative_failure_policy_id": self.narrative_failure_policy_id}
               if self.narrative_failure_policy_id else {}),
            **({"declared_method_identity": self.declared_method_identity}
               if self.declared_method_identity else {}),
            # 只有宣告了 execution 才進 digest 材料，既有 profile 的
            # digest 一個位元都不變。
            **({"execution": self.execution.to_json_dict()}
               if self.execution is not None else {}),
            # Phase A：同樣只有宣告了才進材料。
            **({"proposal_intake": self.proposal_intake.to_json_dict()}
               if self.proposal_intake is not None else {}),
        }

    def to_json_dict(self) -> dict[str, Any]:
        payload = self._digest_material()
        payload["profile_digest"] = self.profile_digest
        return payload

    def execution_plan(self) -> dict[str, Any]:
        """DRAFT 揭露用：這個 profile 會怎麼被執行、會寫到哪裡。

        全部由已驗證的欄位展開，不呼叫任何工具；docker argv 的固定部分由
        adapter 決定，這裡只列出 user 宣告會被接進去的部分。
        """
        execution = self.execution or ExecutionSpec()
        recipe = self.repair_recipe
        method: dict[str, Any] = {
            "parameter": self.parameter.to_json_dict(),
            **({"recipe_id": getattr(recipe, "recipe_id", None)} if recipe else {}),
            **({"method": recipe.method.to_json_dict()}
               if recipe is not None and hasattr(recipe, "method") else {}),
        }
        proof: dict[str, Any] = {
            **({"proof_policy_id": getattr(recipe, "proof_policy_id", None)}
               if getattr(recipe, "proof_policy_id", None) else {}),
            **({"qualified_tool_identity_path":
                getattr(recipe, "qualified_tool_identity_path", None)}
               if getattr(recipe, "qualified_tool_identity_path", None) else {}),
            **({"qualified_tool_identity_sha256":
                getattr(recipe, "qualified_tool_identity_sha256", None)}
               if getattr(recipe, "qualified_tool_identity_sha256", None) else {}),
            **({"user_declared_proof_tool": execution.proof_tool.to_json_dict()}
               if execution.proof_tool is not None else {}),
        }
        if execution.native_tool_binding is not None:
            runner_fields: dict[str, Any] = {
                "runner": execution.runner,
                "execution_mode": "host_native",
                "native_tool_binding": execution.native_tool_binding.to_json_dict(),
                "native_tool_binding_sha256": execution.native_tool_binding.digest,
            }
        else:
            runner_fields = {
                "runner": execution.runner,
                "docker_image_id": self.docker_image_id,
                "docker_executable": execution.docker_executable or "docker",
                "user_docker_args": list(execution.docker_args),
            }
        return {
            **runner_fields,
            "host_env_script": execution.host_env_script,
            "host_make_executable": execution.host_make_executable,
            "host_tool_env": dict(execution.host_tool_env),
            "design_config": self.design_config,
            "input_snapshot_root": self.input_snapshot_root,
            "method": method,
            "proof": proof,
            "budget": self.limits.to_json_dict(),
            "writable_roots": [self.execution_root],
            "read_only_inputs": [self.input_snapshot_root],
            "evidence_sinks": {"dqe_db_path": self.dqe_db_path,
                               "artifact_root": self.artifact_root},
            "side_effects": [
                f"launch up to {self.limits.eda_launches} EDA run(s) via "
                f"{execution.runner}",
                f"materialize up to {self.limits.candidate_writes} candidate "
                f"source copy(ies) under {self.execution_root}",
                f"write DQE rows to {self.dqe_db_path}",
                f"write artifact manifests under {self.artifact_root}",
                f"dispatch up to {self.limits.body_llm_dispatches} provider call(s)",
            ],
            "user_declared": (execution.user_declared.to_json_dict()
                              if execution.user_declared is not None else None),
            # qualification 專用：corpus adapter schema 與 manifest digest 也要
            # 進 DRAFT 與 task digest——「哪一份 corpus」是被核准的一部分。
            **({"fixture_corpus": execution.fixture_corpus.to_json_dict()}
               if execution.fixture_corpus is not None else {}),
            # production 專用：artifact role allowlist 的身分也要進 DRAFT 與
            # task digest——「用哪一份 allowlist 解析 artifact」是被核准的一部分。
            **({"artifact_role_mapping":
                execution.artifact_role_mapping.to_json_dict()}
               if execution.artifact_role_mapping is not None else {}),
            # 跨 daemon 重用既有 baseline：「重用的是哪一份」必須出現在 DRAFT
            # 並進 task digest，否則核准的人看不到它，也無法事後核對。
            **({"durable_baseline_source":
                dict(execution.durable_baseline_source)}
               if execution.durable_baseline_source is not None else {}),
            # Phase A：手動 proposal intake 也是「被核准的內容」的一部分，
            # 因此進 execution plan ⇒ 進 task digest。
            **({"proposal_intake": self.proposal_intake.to_json_dict()}
               if self.proposal_intake is not None else {}),
        }

    def public_summary(self) -> dict[str, Any]:
        """`list_task_profiles` 可外露的內容。

        **不含**任何路徑、DB 位置或 image 內部細節——只有 caller 需要知道
        的「這個 profile 會做什麼」。
        """
        return {
            "profile_id": self.profile_id,
            "template_id": self.template_id,
            "design_id": self.design_id,
            "platform_id": self.platform_id,
            "parameter": self.parameter.to_json_dict(),
            "limits": self.limits.to_json_dict(),
            "profile_digest": self.profile_digest,
            **({"selection_policy": self.selection_policy.to_json_dict()}
               if self.selection_policy is not None else {}),
            **({"repair_recipe": self.repair_recipe.to_json_dict()}
               if self.repair_recipe is not None else {}),
            **({"narrative_failure_policy_id": self.narrative_failure_policy_id}
               if self.narrative_failure_policy_id else {}),
            **({"declared_method_identity": self.declared_method_identity}
               if self.declared_method_identity else {}),
            **({"execution": self.execution.to_json_dict()}
               if self.execution is not None else {}),
            **({"proposal_intake": self.proposal_intake.to_json_dict()}
               if self.proposal_intake is not None else {}),
        }


# ── 載入與驗證 ───────────────────────────────────────────────────────


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise FlowProfileError(
                PROFILE_INVALID, f"duplicate JSON key in profile: {key!r}")
        seen.add(key)
    return dict(pairs)


def _require_str(document: dict[str, Any], field: str) -> str:
    value = document[field]
    if not isinstance(value, str) or not value.strip():
        raise FlowProfileError(
            PROFILE_INVALID, f"profile field {field!r} must be a non-empty string")
    return value


def _require_positive_int(container: dict[str, Any], field: str) -> int:
    value = container[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FlowProfileError(
            PROFILE_INVALID, f"limit {field!r} must be a non-negative integer")
    return value


def load_profile(path: str | Path) -> FlowTaskProfile:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise FlowProfileError(
            PROFILE_INVALID, f"{PROFILE_ENV} must be an absolute path: {path!r}")
    try:
        raw = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise FlowProfileError(
            PROFILE_UNREADABLE, f"the flow profile could not be read: {exc}") from exc
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except FlowProfileError:
        raise
    except ValueError as exc:
        raise FlowProfileError(
            PROFILE_INVALID, f"the flow profile is not valid JSON: {exc}") from exc
    return build_profile(document)


def build_profile(document: Any) -> FlowTaskProfile:
    """驗證一份已解析的 profile document。未知欄位一律拒絕。"""
    if not isinstance(document, dict):
        raise FlowProfileError(PROFILE_INVALID, "the flow profile must be a JSON object")

    schema_version = document.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"unsupported profile schema_version: {schema_version!r}")
    bounded = schema_version == SCHEMA_VERSION_BOUNDED_SEARCH
    core_utilization = schema_version == SCHEMA_VERSION_CORE_UTILIZATION
    repair = schema_version in (SCHEMA_VERSION_SETUP_REPAIR,
                                SCHEMA_VERSION_SETUP_REPAIR_V2)
    repair_v2 = schema_version == SCHEMA_VERSION_SETUP_REPAIR_V2
    resynth = schema_version == SCHEMA_VERSION_SPEED_RESYNTHESIS
    prefix = schema_version == SCHEMA_VERSION_PREFIX_BORROW
    miter = schema_version == SCHEMA_VERSION_TRANSITION_MITER
    setup_margin = schema_version == SCHEMA_VERSION_SETUP_MARGIN
    if miter:
        # v0.12 沿用 v0.8 的每一條 parameter／limits 規則。
        prefix = True
    if setup_margin:
        # parametric setup repair 沿用同一組 parameter／limits／narrative 規則，但 recipe 完全不同：
        # 沒有 RTL patch，只有一個已證明被消費的 timing 參數。
        repair = True
        repair_v2 = True
    if resynth or prefix:
        # v5／v0.8 也帶固定的解說失敗政策（引用被拒的處理只是被顯式納入，豁免集合不變）。
        repair = True
        repair_v2 = True
    if bounded:
        allowed = _TOP_LEVEL_FIELDS | {"selection_policy"}
    elif core_utilization:
        allowed = _TOP_LEVEL_FIELDS | {"core_utilization_policy"}
    elif repair_v2:
        allowed = _TOP_LEVEL_FIELDS | {"repair_recipe",
                                       "narrative_failure_policy_id"}
    elif repair:
        allowed = _TOP_LEVEL_FIELDS | {"repair_recipe"}
    else:
        allowed = _TOP_LEVEL_FIELDS

    # `execution` 在每一個 schema 版本都是**可選**的。
    # Phase A：`proposal_intake` 同樣在每一個版本都可選；缺席 ⇒ 不接受手動
    # proposal（fail closed），且既有 Profile 的 digest 不變。
    optional = {"execution", "proposal_intake"}
    # host-native Profile 以 `execution.native_tool_binding` 釘住站點工具；它
    # 沒有 container image，因此 `docker_image_id` 不適用、也不得出現。
    raw_execution = document.get("execution")
    native = (isinstance(raw_execution, dict)
              and (raw_execution.get("native_tool_binding") is not None
                   or raw_execution.get("runner") == NATIVE_RUNNER))
    if native:
        if "docker_image_id" in document:
            raise FlowProfileError(
                PROFILE_INVALID,
                "a host-native profile must not declare docker_image_id")
        allowed = allowed - {"docker_image_id"}
    unknown = sorted(set(document) - allowed - optional)
    if unknown:
        raise FlowProfileError(PROFILE_INVALID, f"unknown profile field(s): {unknown}")
    missing = sorted(allowed - set(document))
    if missing:
        raise FlowProfileError(PROFILE_INVALID, f"missing profile field(s): {missing}")
    execution = (_build_execution(document["execution"])
                 if "execution" in document else None)
    try:
        proposal_intake = (
            build_proposal_intake(document["proposal_intake"],
                                  where="profile.proposal_intake")
            if "proposal_intake" in document else None)
    except ProposalIntakeError as exc:
        raise FlowProfileError(PROFILE_INVALID, exc.message) from exc

    # profile 不得攜帶任何可執行內容。這個檢查掃全部巢狀 key。
    _reject_executable_fields(document)

    for field in ("profile_id", "template_id", "member_id", "project_id",
                  "design_config", "design_id", "platform_id"):
        _require_str(document, field)

    expected_template = TEMPLATE_BY_SCHEMA[schema_version]
    if document["template_id"] != expected_template:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"profile template_id must be {expected_template!r} for schema "
            f"version {schema_version}, got {document['template_id']!r}")

    for field in _ABSOLUTE_PATH_FIELDS:
        value = _require_str(document, field)
        if not Path(value).is_absolute():
            raise FlowProfileError(
                PROFILE_INVALID, f"profile field {field!r} must be an absolute path")

    # design_config 是 source tree 內的**相對**路徑，且不得逃出去。
    design_config = document["design_config"]
    config_path = Path(design_config)
    if config_path.is_absolute() or ".." in config_path.parts:
        raise FlowProfileError(
            PROFILE_INVALID,
            "design_config must be a relative path inside the source tree")

    # execution_root 不得覆寫 source_root 或 snapshot root。
    exec_root = Path(document["execution_root"]).resolve()
    for guarded in ("source_root", "input_snapshot_root"):
        other = Path(document[guarded]).resolve()
        if exec_root == other or _is_within(exec_root, other) or _is_within(other, exec_root):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"execution_root must not overlap {guarded}")

    manifest_sha = _require_str(document, "input_manifest_sha256")
    if not _SHA256_RE.match(manifest_sha):
        raise FlowProfileError(
            PROFILE_INVALID,
            "input_manifest_sha256 must be 64 lowercase hex characters")

    image_id: str | None = None
    if not native:
        image_id = _require_str(document, "docker_image_id")
        if not _IMAGE_ID_RE.match(image_id):
            raise FlowProfileError(
                PROFILE_INVALID,
                "docker_image_id must be an immutable 'sha256:<64 hex>' identity, "
                "not a mutable tag")

    parameter = _build_parameter(document["parameter"], bounded=bounded,
                                 repair=repair, resynth=resynth, prefix=prefix,
                                 setup_margin=setup_margin,
                                 core_utilization=core_utilization)
    limits = _build_limits(document["limits"], repair=repair,
                           resynth=resynth or prefix,
                           setup_margin=setup_margin)
    selection_policy = (_build_selection_policy(document["selection_policy"])
                        if bounded else None)
    repair_recipe = (
        _build_setup_margin_recipe(document["repair_recipe"]) if setup_margin
        else _build_miter_recipe(document["repair_recipe"]) if miter
        else _build_prefix_recipe(document["repair_recipe"]) if prefix
        else _build_resynthesis_recipe(document["repair_recipe"]) if resynth
        else _build_repair_recipe(document["repair_recipe"]) if repair
        else None)
    narrative_policy = None
    if repair_v2:
        narrative_policy = document["narrative_failure_policy_id"]
        if narrative_policy != FIXED_NARRATIVE_FAILURE_POLICY_ID:
            raise FlowProfileError(
                PROFILE_INVALID,
                "narrative_failure_policy_id must be "
                f"{FIXED_NARRATIVE_FAILURE_POLICY_ID!r}")
    if repair:
        if limits.eda_launches != 2:
            raise FlowProfileError(
                PROFILE_INVALID,
                "the setup repair template runs exactly two ORFS launches")
        expected_restructure = 0 if (resynth or prefix or setup_margin) else 1
        if (limits.candidate_writes != 1
                or limits.restructure_invocations != expected_restructure):
            raise FlowProfileError(
                PROFILE_INVALID,
                "this template allows exactly one materialization and "
                f"{expected_restructure} restructure invocation(s)")
        if (not resynth and not prefix and not setup_margin
                and repair_recipe.invocations != limits.restructure_invocations):
            raise FlowProfileError(
                PROFILE_INVALID,
                "the recipe invocation count must match the runtime limit")
    if bounded and limits.candidate_writes < len(parameter.candidates):
        raise FlowProfileError(
            PROFILE_INVALID,
            "candidate_writes must cover every declared candidate")
    if bounded and limits.eda_launches < len(parameter.candidates) + 1:
        raise FlowProfileError(
            PROFILE_INVALID,
            "eda_launches must cover the baseline and every declared candidate")
    if setup_margin:
        # 方法變數由 user 決定，但 parameter 與 recipe 必須指同一個變數，
        # 且兩側的值要一致——否則 DRAFT 揭露的與實際消費的不是同一件事。
        method = repair_recipe.method
        if (parameter.name != method.variable
                or parameter.baseline != method.baseline_value
                or parameter.candidate != method.candidate_value):
            raise FlowProfileError(
                PROFILE_INVALID,
                "parameter and repair_recipe.method must declare the same "
                "variable, baseline value and candidate value")
    declared_method_identity = None
    if core_utilization:
        _validate_core_utilization_policy(document["core_utilization_policy"], parameter)
        declared_method_identity = document["core_utilization_policy"]["method_identity"]
    elif repair_recipe is not None and getattr(repair_recipe, "method", None) is not None:
        declared_method_identity = getattr(repair_recipe.method, "method_identity", None)

    profile = FlowTaskProfile(
        schema_version=schema_version,
        profile_id=document["profile_id"],
        template_id=document["template_id"],
        member_id=document["member_id"],
        project_id=document["project_id"],
        source_root=document["source_root"],
        input_snapshot_root=document["input_snapshot_root"],
        input_manifest_path=document["input_manifest_path"],
        input_manifest_sha256=manifest_sha,
        design_config=design_config,
        design_id=document["design_id"],
        platform_id=document["platform_id"],
        docker_image_id=image_id,
        execution_root=document["execution_root"],
        dqe_db_path=document["dqe_db_path"],
        artifact_root=document["artifact_root"],
        parameter=parameter, limits=limits, profile_digest="",
        declared_method_identity=declared_method_identity,
        selection_policy=selection_policy, repair_recipe=repair_recipe,
        narrative_failure_policy_id=narrative_policy, execution=execution,
        proposal_intake=proposal_intake,
    )
    return replace(profile, profile_digest=sha256_payload(profile._digest_material()))


def _require_abs_path_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FlowProfileError(
            PROFILE_INVALID, f"execution.{field} must be a non-empty string")
    if not Path(value).is_absolute():
        raise FlowProfileError(
            PROFILE_INVALID, f"execution.{field} must be an absolute path")
    return value


def _build_execution(node: Any) -> ExecutionSpec:
    """逐欄驗證 user 宣告的執行方式。

    只驗**形狀**（型別、絕對路徑、token 不含控制字元）；不驗「內容合不合理」
    ——那是 user 的工程選擇。每一個值都會原樣展開到 DRAFT 讓人核准。
    """
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "execution must be an object")
    unknown = sorted(set(node) - _EXECUTION_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unsupported execution field(s): {unknown}")
    runner = node.get("runner", "docker")
    if runner not in _EXECUTION_RUNNERS:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"execution.runner must be one of {sorted(_EXECUTION_RUNNERS)}")

    docker_executable = None
    if node.get("docker_executable") is not None:
        value = node["docker_executable"]
        if not isinstance(value, str) or not value.strip() or "/" in value.strip("/") and not Path(value).is_absolute():
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.docker_executable must be a bare command name or an absolute path")
        docker_executable = value

    raw_args = node.get("docker_args", [])
    if not isinstance(raw_args, list):
        raise FlowProfileError(PROFILE_INVALID, "execution.docker_args must be a list")
    docker_args: list[str] = []
    for token in raw_args:
        if not isinstance(token, str) or not token or any(ch in token for ch in "\0\n\r"):
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.docker_args entries must be non-empty single-line strings")
        docker_args.append(token)

    host_env_script = (_require_abs_path_str(node["host_env_script"], "host_env_script")
                       if node.get("host_env_script") is not None else None)
    host_make = (_require_abs_path_str(node["host_make_executable"], "host_make_executable")
                 if node.get("host_make_executable") is not None else None)
    tool_env_raw = node.get("host_tool_env", {})
    if not isinstance(tool_env_raw, dict):
        raise FlowProfileError(PROFILE_INVALID, "execution.host_tool_env must be an object")
    host_tool_env: dict[str, str] = {}
    for key, value in tool_env_raw.items():
        if key not in _HOST_TOOL_ENV_KEYS:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"execution.host_tool_env only accepts {sorted(_HOST_TOOL_ENV_KEYS)}")
        host_tool_env[key] = _require_abs_path_str(value, f"host_tool_env.{key}")
    if (runner == "host" and host_env_script is None
            and node.get("native_tool_binding") is None):
        raise FlowProfileError(
            PROFILE_INVALID,
            "execution.runner=host requires execution.host_env_script")

    proof_tool = None
    if node.get("proof_tool") is not None:
        raw = node["proof_tool"]
        if not isinstance(raw, dict) or "executable_realpath" not in raw:
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.proof_tool must be an object with executable_realpath")
        extra = sorted(set(raw) - _PROOF_TOOL_FIELDS)
        if extra:
            raise FlowProfileError(
                PROFILE_INVALID, f"unsupported execution.proof_tool field(s): {extra}")
        for digest_field in ("executable_sha256", "version_sha256"):
            value = raw.get(digest_field)
            if value is not None and (not isinstance(value, str)
                                      or not _SHA256_RE.match(value)):
                raise FlowProfileError(
                    PROFILE_INVALID,
                    f"execution.proof_tool.{digest_field} must be 64 lowercase hex")
        proof_tool = ProofToolDeclaration(
            executable_realpath=_require_abs_path_str(
                raw["executable_realpath"], "proof_tool.executable_realpath"),
            executable_sha256=raw.get("executable_sha256"),
            version_sha256=raw.get("version_sha256"))

    user_declared = None
    if node.get("user_declared") is not None:
        raw = node["user_declared"]
        if not isinstance(raw, dict):
            raise FlowProfileError(PROFILE_INVALID, "execution.user_declared must be an object")
        extra = sorted(set(raw) - _USER_DECLARED_FIELDS)
        if extra:
            raise FlowProfileError(
                PROFILE_INVALID, f"unsupported execution.user_declared field(s): {extra}")
        labels = raw.get("labels", [])
        if not isinstance(labels, list) or any(not isinstance(v, str) for v in labels):
            raise FlowProfileError(
                PROFILE_INVALID, "execution.user_declared.labels must be a list of strings")
        for text_field in ("qualification", "notes"):
            value = raw.get(text_field)
            if value is not None and not isinstance(value, str):
                raise FlowProfileError(
                    PROFILE_INVALID, f"execution.user_declared.{text_field} must be a string")
        user_declared = UserDeclared(
            qualification=raw.get("qualification"), labels=tuple(labels),
            notes=raw.get("notes"))

    fixture_corpus = None
    if node.get("fixture_corpus") is not None:
        raw = node["fixture_corpus"]
        if not isinstance(raw, dict) or set(raw) != _FIXTURE_CORPUS_FIELDS:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"execution.fixture_corpus requires exactly {sorted(_FIXTURE_CORPUS_FIELDS)}")
        if raw["purpose"] != QUALIFICATION_ONLY:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"execution.fixture_corpus.purpose must be {QUALIFICATION_ONLY!r}: "
                "a fixture corpus is never a production execution")
        if not isinstance(raw["adapter_schema"], str) or not raw["adapter_schema"].strip():
            raise FlowProfileError(
                PROFILE_INVALID, "execution.fixture_corpus.adapter_schema must be a string")
        if not isinstance(raw["manifest_sha256"], str) or not _SHA256_RE.match(
                raw["manifest_sha256"]):
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.fixture_corpus.manifest_sha256 must be 64 lowercase hex")
        fixture_corpus = FixtureCorpusDeclaration(
            purpose=raw["purpose"], adapter_schema=raw["adapter_schema"],
            manifest_path=_require_abs_path_str(
                raw["manifest_path"], "fixture_corpus.manifest_path"),
            manifest_sha256=raw["manifest_sha256"])

    artifact_role_mapping = None
    if node.get("artifact_role_mapping") is not None:
        raw = node["artifact_role_mapping"]
        if not isinstance(raw, dict) or set(raw) != _ARTIFACT_ROLE_MAPPING_FIELDS:
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.artifact_role_mapping requires exactly "
                f"{sorted(_ARTIFACT_ROLE_MAPPING_FIELDS)}")
        if not isinstance(raw["version"], str) or not raw["version"].strip():
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.artifact_role_mapping.version must be a string")
        if not isinstance(raw["digest"], str) or not _SHA256_RE.match(raw["digest"]):
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.artifact_role_mapping.digest must be 64 lowercase hex")
        artifact_role_mapping = ArtifactRoleMappingDeclaration(
            version=raw["version"], digest=raw["digest"])

    durable_baseline_source = None
    if node.get("durable_baseline_source") is not None:
        declared = node["durable_baseline_source"]
        if (not isinstance(declared, dict)
                or set(declared) != _DURABLE_BASELINE_SOURCE_FIELDS):
            raise FlowProfileError(
                PROFILE_INVALID,
                "execution.durable_baseline_source requires exactly "
                f"{sorted(_DURABLE_BASELINE_SOURCE_FIELDS)}")
        for name, value in declared.items():
            if not isinstance(value, str) or not value:
                raise FlowProfileError(
                    PROFILE_INVALID,
                    f"execution.durable_baseline_source.{name} must be a "
                    f"non-empty string")
        durable_baseline_source = {name: str(declared[name])
                                   for name in sorted(declared)}

    native_tool_binding = None
    if runner == NATIVE_RUNNER and node.get("native_tool_binding") is None:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"execution.runner={NATIVE_RUNNER} requires execution.native_tool_binding")
    if node.get("native_tool_binding") is not None:
        native_tool_binding = _build_native_tool_binding(node["native_tool_binding"])
        _validate_native_execution(
            native_tool_binding, runner=runner, docker_executable=docker_executable,
            docker_args=docker_args, host_env_script=host_env_script,
            host_make=host_make, host_tool_env=host_tool_env,
            fixture_corpus=fixture_corpus, proof_tool=proof_tool)

    return ExecutionSpec(
        runner=runner, docker_executable=docker_executable,
        docker_args=tuple(docker_args), host_env_script=host_env_script,
        host_make_executable=host_make, host_tool_env=host_tool_env,
        proof_tool=proof_tool, user_declared=user_declared,
        fixture_corpus=fixture_corpus,
        artifact_role_mapping=artifact_role_mapping,
        durable_baseline_source=durable_baseline_source,
        native_tool_binding=native_tool_binding)


def _build_native_tool_binding(node: Any) -> NativeToolBinding:
    from terminux_engine.orchestration.native_tool_identity import (
        NativeToolIdentityError,
        parse_pin,
    )

    if not isinstance(node, dict) or set(node) != _NATIVE_BINDING_FIELDS:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"execution.native_tool_binding requires exactly {sorted(_NATIVE_BINDING_FIELDS)}")
    if node["schema"] != NATIVE_TOOL_BINDING_SCHEMA:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"execution.native_tool_binding.schema must be {NATIVE_TOOL_BINDING_SCHEMA!r}")
    tools = node["tools"]
    if not isinstance(tools, dict):
        raise FlowProfileError(PROFILE_INVALID,
                               "execution.native_tool_binding.tools must be an object")
    missing = sorted(NATIVE_REQUIRED_TOOLS - set(tools))
    unknown = sorted(set(tools) - NATIVE_REQUIRED_TOOLS - NATIVE_OPTIONAL_TOOLS)
    if missing or unknown:
        raise FlowProfileError(
            PROFILE_INVALID,
            "execution.native_tool_binding.tools must pin "
            f"{sorted(NATIVE_REQUIRED_TOOLS)} (optional {sorted(NATIVE_OPTIONAL_TOOLS)}); "
            f"missing={missing} unknown={unknown}")
    try:
        pins = {name: parse_pin(tools[name], where=f"native_tool_binding.tools.{name}")
                for name in tools}
        env_script = parse_pin(node["env_script"], where="native_tool_binding.env_script")
    except NativeToolIdentityError as exc:
        raise FlowProfileError(PROFILE_INVALID, exc.message) from exc
    return NativeToolBinding(tools=pins, env_script=env_script)


def _validate_native_execution(
    binding: NativeToolBinding, *, runner: str, docker_executable: str | None,
    docker_args: list[str], host_env_script: str | None, host_make: str | None,
    host_tool_env: dict[str, str], fixture_corpus: Any, proof_tool: Any,
) -> None:
    """native binding 與其餘執行宣告必須描述**同一組**工具，不得留下第二個來源。

    runner 必須明確是 `host_native`：`runner=host`＋binding 會同時符合舊 host
    runner 與 native 兩種身分，版本不明確，一律拒絕。`host_*` 欄位可省略；
    有宣告時必須等於 binding。
    """
    if runner != NATIVE_RUNNER:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"execution.native_tool_binding requires execution.runner={NATIVE_RUNNER} "
            f"(got {runner!r}; the runner identity must not be ambiguous)")
    if docker_executable is not None or docker_args:
        raise FlowProfileError(
            PROFILE_INVALID,
            "a host-native profile must not declare docker_executable or docker_args")
    if fixture_corpus is not None:
        raise FlowProfileError(
            PROFILE_INVALID,
            "a fixture corpus replay is not a host-native execution")
    if proof_tool is not None:
        raise FlowProfileError(
            PROFILE_INVALID,
            "a host-native profile pins its proof tool as native_tool_binding.tools."
            "FORMAL_YOSYS, not execution.proof_tool")
    if host_env_script is not None and host_env_script != binding.env_script.realpath:
        raise FlowProfileError(
            PROFILE_INVALID,
            "execution.host_env_script must equal native_tool_binding.env_script.realpath")
    if host_make is not None and host_make != binding.tools["MAKE"].realpath:
        raise FlowProfileError(
            PROFILE_INVALID,
            "execution.host_make_executable must equal native_tool_binding.tools.MAKE.realpath")
    expected_env = {name: binding.tools[name].realpath
                    for name in ("OPENROAD_EXE", "YOSYS_EXE")}
    if host_tool_env and {name: host_tool_env.get(name)
                          for name in expected_env} != expected_env:
        raise FlowProfileError(
            PROFILE_INVALID,
            "execution.host_tool_env must set OPENROAD_EXE and YOSYS_EXE to the "
            "native_tool_binding realpaths")


# ── task-scoped profile 來源 ──────────────────────────


def load_profile_from_path(path: str | Path) -> tuple[FlowTaskProfile, ProfileSource]:
    """user 提供的 Profile 路徑 → (已驗證 profile, 來源身分)。

    合法 symlink 不拒絕，但 logical／resolved 兩個路徑都記下；原始 bytes 的
    sha256 與 canonical digest 一起保存，task 之後**不再**重讀這個檔案。
    """
    raw_path = str(path)
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise FlowProfileError(
            PROFILE_SOURCE_INVALID,
            f"profile_path must be an absolute path: {raw_path!r}")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        raw_bytes = resolved.read_bytes()
    except OSError as exc:
        raise FlowProfileError(
            PROFILE_UNREADABLE, f"the profile could not be read: {exc}") from exc
    try:
        document = json.loads(raw_bytes.decode("utf-8"),
                              object_pairs_hook=_reject_duplicate_keys)
    except FlowProfileError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise FlowProfileError(
            PROFILE_INVALID, f"the profile is not valid JSON: {exc}") from exc
    profile = build_profile(document)
    import hashlib
    import stat as _stat

    source = ProfileSource(
        kind="path", logical_path=raw_path, resolved_path=str(resolved),
        is_symlink=_stat.S_ISLNK(metadata.st_mode),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        size_bytes=len(raw_bytes), profile_digest=profile.profile_digest)
    return profile, source


def load_profile_document_from_path(
        path: str | Path) -> tuple[FlowTaskProfile, ProfileSource, dict[str, Any]]:
    """載入 Profile 並保留已接受的 JSON document 給 task-owned v2 snapshot。"""
    raw_path = str(path)
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise FlowProfileError(
            PROFILE_SOURCE_INVALID,
            f"profile_path must be an absolute path: {raw_path!r}")
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        raw_bytes = resolved.read_bytes()
    except OSError as exc:
        raise FlowProfileError(
            PROFILE_UNREADABLE, f"the profile could not be read: {exc}") from exc
    try:
        document = json.loads(raw_bytes.decode("utf-8"),
                              object_pairs_hook=_reject_duplicate_keys)
    except FlowProfileError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise FlowProfileError(
            PROFILE_INVALID, f"the profile is not valid JSON: {exc}") from exc
    profile = build_profile(document)
    import hashlib
    import stat as _stat

    source = ProfileSource(
        kind="path", logical_path=raw_path, resolved_path=str(resolved),
        is_symlink=_stat.S_ISLNK(metadata.st_mode),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        size_bytes=len(raw_bytes), profile_digest=profile.profile_digest)
    return profile, source, document


def build_profile_from_inline(document: Any) -> tuple[FlowTaskProfile, ProfileSource]:
    """GUI／CLI／上層系統直接送來的 inline JSON → (已驗證 profile, 來源身分)。"""
    if not isinstance(document, dict):
        raise FlowProfileError(
            PROFILE_SOURCE_INVALID, "profile_inline must be a JSON object")
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    profile = build_profile(document)
    import hashlib

    source = ProfileSource(
        kind="inline", logical_path=None, resolved_path=None, is_symlink=False,
        raw_sha256=hashlib.sha256(canonical).hexdigest(),
        size_bytes=len(canonical), profile_digest=profile.profile_digest)
    return profile, source


def build_profile_document_from_inline(
        document: Any) -> tuple[FlowTaskProfile, ProfileSource, dict[str, Any]]:
    """inline Profile 與 v2 snapshot 所需的已接受 document。"""
    profile, source = build_profile_from_inline(document)
    return profile, source, dict(document)


def _is_within(inner: Path, outer: Path) -> bool:
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


def _reject_executable_fields(node: Any, path: str = "") -> None:
    """遞迴拒絕任何看起來像「怎麼執行」的欄位。

    profile 只描述**允許的範圍**；命令、mount、環境與 Docker options 一律
    由正式 Adapter 決定，不接受設定檔注入。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if path == "" and lowered == "execution":
                # 唯一允許宣告執行方式的位置，由 `_build_execution`
                # 逐欄驗證；這裡不做字面禁用，否則 user 永遠無法表達
                # docker args／EDA 路徑。
                continue
            if lowered in _KNOWN_FIELD_NAMES:
                _reject_executable_fields(value, f"{path}{key}.")
                continue
            if lowered in _FORBIDDEN_FIELD_NAMES:
                raise FlowProfileError(
                    PROFILE_INVALID,
                    f"profile must not carry executable content: "
                    f"field {path + str(key)!r} is not permitted")
            _reject_executable_fields(value, f"{path}{key}.")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_executable_fields(value, f"{path}{index}.")


def _build_parameter(node: Any, *, bounded: bool = False,
                     repair: bool = False,
                     resynth: bool = False,
                     prefix: bool = False,
                     setup_margin: bool = False,
                     core_utilization: bool = False) -> FlowParameter:
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "parameter must be an object")
    fields = (frozenset({"name", "baseline"}) if core_utilization else
              _PARAMETER_FIELDS_V2 if (bounded or repair) else _PARAMETER_FIELDS)
    unknown = sorted(set(node) - fields)
    if unknown:
        raise FlowProfileError(PROFILE_INVALID, f"unknown parameter field(s): {unknown}")
    missing = sorted(fields - set(node))
    if missing:
        raise FlowProfileError(PROFILE_INVALID, f"missing parameter field(s): {missing}")
    name = node["name"]
    if core_utilization:
        if name != FIXED_PARAMETER_NAME or node.get("baseline") != "55":
            raise FlowProfileError(
                PROFILE_INVALID,
                "CORE_UTILIZATION Profile fixes parameter=CORE_UTILIZATION and baseline=55")
        return FlowParameter(name=name, baseline="55", candidate=None, candidates=())
    generic = setup_margin or not (bounded or repair or resynth or prefix)
    if generic:
        # 參數名稱由 user 決定（ORFS 變數命名規則）；AES／gcd 固定
        # 案例的名稱不再是產品 gate。沒有消費點規則的變數，其消費證據會如實
        # 標成 unverifiable，而不是被拒絕執行。
        if not isinstance(name, str) or not _VARIABLE_NAME_RE.match(name):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"parameter.name must be an ORFS variable name "
                f"([A-Z][A-Z0-9_]*), got {name!r}")
    else:
        # v2／v3／v5／v0.8／v0.12 是歷史固定 recipe（gcd），名稱維持釘住。
        expected_name = (RESYNTHESIS_PARAMETER_NAME if resynth
                         else PREFIX_BORROW_PARAMETER_NAME if prefix
                         else FIXED_PARAMETER_NAME)
        if name != expected_name:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"parameter.name must be {expected_name!r} in this batch, "
                f"got {name!r}")
    baseline = node["baseline"]
    if bounded or repair:
        raw = node["candidates"]
        if not isinstance(raw, list) or not raw:
            raise FlowProfileError(
                PROFILE_INVALID, "parameter.candidates must be a non-empty list")
        if len(raw) > len(BOUNDED_SEARCH_SLOTS) - 1:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"this template runs at most {len(BOUNDED_SEARCH_SLOTS) - 1} "
                "candidates")
        candidates = tuple(raw)
    else:
        candidates = (node["candidate"],)

    for label, value in [("baseline", baseline)] + [
            (f"candidates[{i}]", v) for i, v in enumerate(candidates)]:
        if not isinstance(value, str) or not value.strip():
            raise FlowProfileError(
                PROFILE_INVALID, f"parameter.{label} must be a non-empty string")
        if setup_margin or generic:
            # 值只接受十進位 token（整數或小數）：不接受運算式、單位或其他
            # 字元——它會被原樣寫進 config 與 argv。數值本身由 user 決定。
            if not _DECIMAL_TOKEN_RE.match(value.strip()):
                raise FlowProfileError(
                    PROFILE_INVALID,
                    f"parameter.{label} must be a plain decimal token, "
                    f"got {value!r}")
            continue
        if not value.strip().isdigit():
            raise FlowProfileError(
                PROFILE_INVALID,
                f"parameter.{label} must be a plain integer token, got {value!r}")
    if setup_margin:
        # baseline／candidate 的數值由 user 決定；唯一的結構要求是
        # 兩側只差這一個值，且 candidate 只有一個。
        if len(candidates) != 1:
            raise FlowProfileError(
                PROFILE_INVALID,
                "this template runs exactly one candidate value")
        if candidates[0] == baseline:
            raise FlowProfileError(
                PROFILE_INVALID,
                "parameter.candidates[0] must differ from the baseline")
        return FlowParameter(name=name, baseline=baseline,
                             candidate=candidates[0], candidates=candidates)
    if prefix:
        # v0.8：兩側都用原 area mapping，所以 ABC_AREA **兩端相同**。
        # 唯一差異是已核准的 RTL patch，不是合成策略。
        if (baseline, candidates) != (PREFIX_BORROW_BASELINE_VALUE,
                                      (PREFIX_BORROW_CANDIDATE_VALUE,)):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"the prefix-borrow template fixes "
                f"{PREFIX_BORROW_PARAMETER_NAME}="
                f"{PREFIX_BORROW_BASELINE_VALUE} on both slots")
        return FlowParameter(name=name, baseline=baseline,
                             candidate=candidates[0], candidates=candidates)
    if resynth:
        # v5：兩個 slot 的合成策略**必須不同**（1 → 0），這正是唯一的差異。
        if (baseline, candidates) != (RESYNTHESIS_BASELINE_VALUE,
                                      (RESYNTHESIS_CANDIDATE_VALUE,)):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"the resynthesis template fixes {RESYNTHESIS_PARAMETER_NAME} "
                f"{RESYNTHESIS_BASELINE_VALUE} → {RESYNTHESIS_CANDIDATE_VALUE}")
        return FlowParameter(name=name, baseline=baseline,
                             candidate=candidates[0], candidates=candidates)
    if repair:
        # 兩個 slot 的參數值**相同**（都是 45）——唯一差異是已核准
        # 的 ECO hook，不是參數。
        if candidates != (baseline,):
            raise FlowProfileError(
                PROFILE_INVALID,
                "the setup repair template runs both slots at the same "
                "parameter value")
        return FlowParameter(name=name, baseline=baseline, candidate=baseline,
                             candidates=candidates)
    if baseline in candidates:
        raise FlowProfileError(
            PROFILE_INVALID, "every candidate must differ from the baseline")
    if len(set(candidates)) != len(candidates):
        raise FlowProfileError(
            PROFILE_INVALID, "the candidate sequence must not repeat a value")
    return FlowParameter(name=name, baseline=baseline, candidate=candidates[0],
                         candidates=candidates)


def _validate_core_utilization_policy(node: Any, parameter: FlowParameter) -> None:
    """Profile 只能宣告公開 bound，不能夾帶 candidate 或 runner 策略。"""
    required = {"method_identity": "gcd.core_utilization.v1", "global_minimum": 35,
                "global_maximum": 55, "max_decrement": 10, "max_candidates": 2}
    if not isinstance(node, dict) or set(node) != _CORE_UTILIZATION_POLICY_FIELDS:
        raise FlowProfileError(PROFILE_INVALID,
                               "core_utilization_policy has unknown or missing controls")
    if any(node.get(key) != value for key, value in required.items()):
        raise FlowProfileError(PROFILE_INVALID,
                               "core_utilization_policy differs from Package contract")
    if parameter.candidate is not None or parameter.baseline != "55":
        raise FlowProfileError(PROFILE_INVALID, "CORE_UTILIZATION Profile carries a candidate")


def _build_selection_policy(node: Any) -> SelectionPolicy:
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "selection_policy must be an object")
    unknown = sorted(set(node) - _SELECTION_POLICY_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown selection_policy field(s): {unknown}")
    missing = sorted(_SELECTION_POLICY_FIELDS - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing selection_policy field(s): {missing}")
    policy_id = node["policy_id"]
    if policy_id != FIXED_SELECTION_POLICY_ID:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"selection_policy.policy_id must be {FIXED_SELECTION_POLICY_ID!r}, "
            f"got {policy_id!r}")
    limit = node["area_ratio_limit"]
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        raise FlowProfileError(
            PROFILE_INVALID, "selection_policy.area_ratio_limit must be a number")
    if not 1.0 < float(limit) <= 10.0:
        raise FlowProfileError(
            PROFILE_INVALID,
            "selection_policy.area_ratio_limit must be greater than 1.0 and at "
            "most 10.0")
    return SelectionPolicy(policy_id=policy_id, area_ratio_limit=float(limit))


def _build_limits(node: Any, *, repair: bool = False,
                  resynth: bool = False,
                  setup_margin: bool = False) -> FlowLimits:
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "limits must be an object")
    fields = _LIMIT_FIELDS_V3 if repair else _LIMIT_FIELDS
    unknown = sorted(set(node) - fields)
    if unknown:
        raise FlowProfileError(PROFILE_INVALID, f"unknown limit field(s): {unknown}")
    missing = sorted(fields - set(node))
    if missing:
        raise FlowProfileError(PROFILE_INVALID, f"missing limit field(s): {missing}")
    values: dict[str, int] = {}
    for f in sorted(fields):
        if (resynth or setup_margin) and f == "restructure_invocations":
            # v5 與 parametric setup repair 都完全不做 restructure；這裡必須是 0，不是「至少 1」。
            raw = node.get(f)
            if raw != 0:
                raise FlowProfileError(
                    PROFILE_INVALID,
                    "restructure_invocations must be 0 for this template")
            values[f] = 0
            continue
        values[f] = _require_positive_int(node, f)
    limits = FlowLimits(**values)
    if limits.runtime_retries != 0:
        raise FlowProfileError(PROFILE_INVALID, "runtime_retries must be 0 in this batch")
    if limits.eda_launches < 1 or limits.candidate_writes < 1:
        raise FlowProfileError(
            PROFILE_INVALID, "eda_launches and candidate_writes must be at least 1")
    if limits.run_timeout_sec < 1 or limits.task_deadline_sec < limits.run_timeout_sec:
        raise FlowProfileError(
            PROFILE_INVALID,
            "task_deadline_sec must be at least run_timeout_sec")
    if (limits.equivalence_timeout_sec is not None
            and limits.equivalence_timeout_sec > limits.run_timeout_sec):
        # 等價 checker 的 timeout 包含在 run deadline 內。
        raise FlowProfileError(
            PROFILE_INVALID,
            "equivalence_timeout_sec must fit inside run_timeout_sec")
    return limits


def _build_resynthesis_recipe(node: Any) -> ResynthesisRecipe:
    """v5 的 recipe。每一個 id 都是嚴格枚舉，不接受任意字串。"""
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "repair_recipe must be an object")
    unknown = sorted(set(node) - _RESYNTHESIS_RECIPE_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown repair_recipe field(s): {unknown}")
    missing = sorted(_RESYNTHESIS_RECIPE_FIELDS - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing repair_recipe field(s): {missing}")
    fixed = (
        ("recipe_id", FIXED_RESYNTHESIS_RECIPE_ID),
        ("acceptance_policy_id", FIXED_RESYNTHESIS_POLICY_ID),
        ("geometry_policy_id", FIXED_GEOMETRY_POLICY_ID),
        ("comparison_policy_id", FIXED_RESYNTHESIS_COMPARISON_POLICY_ID),
        ("strategy_variable", RESYNTHESIS_PARAMETER_NAME),
        ("baseline_value", RESYNTHESIS_BASELINE_VALUE),
        ("candidate_value", RESYNTHESIS_CANDIDATE_VALUE),
    )
    for field, expected in fixed:
        if node[field] != expected:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"repair_recipe.{field} must be {expected!r}")
    helper_dir, helpers = _build_helper_refs(node)
    suffix = _require_str(node, "config_suffix_sha256")
    if not _SHA256_RE.match(suffix):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.config_suffix_sha256 must be 64 lowercase hex characters")
    return ResynthesisRecipe(
        recipe_id=node["recipe_id"],
        strategy_variable=node["strategy_variable"],
        baseline_value=node["baseline_value"],
        candidate_value=node["candidate_value"],
        helper_dir=helper_dir, helpers=helpers,
        config_suffix_sha256=suffix,
        acceptance_policy_id=node["acceptance_policy_id"],
        geometry_policy_id=node["geometry_policy_id"],
        comparison_policy_id=node["comparison_policy_id"])


def _build_rtl_patch(node: Any) -> RtlPatch:
    """`rtl_patch` 的驗證。path／modules／三個 hash 全部是固定值。"""
    if not isinstance(node, dict):
        raise FlowProfileError(
            PROFILE_INVALID, "repair_recipe.rtl_patch must be an object")
    unknown = sorted(set(node) - _RTL_PATCH_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown rtl_patch field(s): {unknown}")
    missing = sorted(_RTL_PATCH_FIELDS - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing rtl_patch field(s): {missing}")
    path = _require_str(node, "path")
    if path != FIXED_RTL_PATCH_PATH:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"rtl_patch.path must be {FIXED_RTL_PATCH_PATH!r}")
    for field in ("before_sha256", "after_sha256", "patch_sha256"):
        value = _require_str(node, field)
        if not _SHA256_RE.match(value):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"rtl_patch.{field} must be 64 lowercase hex characters")
    if node["before_sha256"] == node["after_sha256"]:
        raise FlowProfileError(
            PROFILE_INVALID,
            "rtl_patch before/after hashes are identical; that is not a change")
    modules = node["modules"]
    if (not isinstance(modules, list)
            or tuple(modules) != FIXED_RTL_PATCH_MODULES):
        raise FlowProfileError(
            PROFILE_INVALID,
            f"rtl_patch.modules must be {list(FIXED_RTL_PATCH_MODULES)}")
    return RtlPatch(
        path=path, before_sha256=node["before_sha256"],
        after_sha256=node["after_sha256"], patch_sha256=node["patch_sha256"],
        modules=tuple(modules))


def _optional_abs_path(node: dict[str, Any], field: str) -> str | None:
    """可選的絕對路徑欄位。給了就必須是非空絕對路徑字串。"""
    if field not in node or node[field] is None:
        return None
    value = node[field]
    if not isinstance(value, str) or not value.strip():
        raise FlowProfileError(
            PROFILE_INVALID, f"repair_recipe.{field} must be a non-empty string")
    if not value.startswith("/"):
        raise FlowProfileError(
            PROFILE_INVALID, f"repair_recipe.{field} must be an absolute path")
    return value


def _build_setup_margin_recipe(node: Any) -> SetupMarginRecipe:
    """parametric setup repair 的 recipe。每一個 id 與方法值都是嚴格枚舉，不接受任意字串。"""
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "repair_recipe must be an object")
    unknown = sorted(set(node) - _SETUP_MARGIN_RECIPE_FIELDS
                     - _SETUP_MARGIN_RECIPE_OPTIONAL_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown repair_recipe field(s): {unknown}")
    missing = sorted(_SETUP_MARGIN_RECIPE_FIELDS - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing repair_recipe field(s): {missing}")
    # id 由 user／project 決定，不再釘住 AES 的字串；身分原樣
    # 記錄並進 digest。proof_policy_id 仍必須是本 daemon 認得的 proof 實作，
    # 否則「兩組等價證明」沒有東西可以產生。
    for field in ("recipe_id", "acceptance_policy_id", "comparison_policy_id",
                  "proof_policy_id"):
        if not isinstance(node[field], str) or not node[field].strip():
            raise FlowProfileError(
                PROFILE_INVALID, f"repair_recipe.{field} must be a non-empty string")
    if node["proof_policy_id"] != FIXED_MITER_PROOF_POLICY_ID:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"repair_recipe.proof_policy_id must name an implemented proof "
            f"policy; this daemon implements {FIXED_MITER_PROOF_POLICY_ID!r}")

    method_node = node["method"]
    if not isinstance(method_node, dict):
        raise FlowProfileError(
            PROFILE_INVALID, "repair_recipe.method must be an object")
    if set(method_node) != _SETUP_MARGIN_METHOD_FIELDS:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"repair_recipe.method needs exactly "
            f"{sorted(_SETUP_MARGIN_METHOD_FIELDS)}")
    variable = method_node["variable"]
    if not isinstance(variable, str) or not _VARIABLE_NAME_RE.match(variable):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.method.variable must be an ORFS variable name "
            "([A-Z][A-Z0-9_]*)")
    baseline_value = str(method_node["baseline_value"])
    candidate_value = str(method_node["candidate_value"])
    for label, value in (("baseline_value", baseline_value),
                         ("candidate_value", candidate_value)):
        if not _DECIMAL_TOKEN_RE.match(value.strip()):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"repair_recipe.method.{label} must be a plain decimal token")
    if candidate_value == baseline_value:
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.method.candidate_value must differ from baseline_value")
    stages = method_node["consumption_stages"]
    if (not isinstance(stages, list) or not stages
            or any(not isinstance(s, str) or not re.match(r"^[a-z][a-z0-9_]*$", s)
                   for s in stages)):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.method.consumption_stages must be a non-empty list "
            "of ORFS stage names")
    evidence = method_node["consumption_evidence"]
    if not isinstance(evidence, list) or not evidence or not all(
            isinstance(e, str) and e.strip() for e in evidence):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.method.consumption_evidence must be a non-empty "
            "list of pinned-source references")

    helper_dir, helpers = _build_helper_refs(node)
    for field in ("config_suffix_sha256", "candidate_config_suffix_sha256"):
        if not _SHA256_RE.match(str(node[field])):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"repair_recipe.{field} must be 64 lowercase hex characters")
    identity_path = _optional_abs_path(node, "qualified_tool_identity_path")
    if identity_path is None:
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.qualified_tool_identity_path is required: the proof "
            "envelope must be checked against a qualified tool identity")
    identity_sha256 = node.get("qualified_tool_identity_sha256")
    if "qualified_tool_identity_sha256" in node and (
            not isinstance(identity_sha256, str)
            or not _SHA256_RE.match(identity_sha256)):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.qualified_tool_identity_sha256 must be 64 lowercase "
            "hex characters")
    target_slack_raw = node.get("target_slack_ns")
    target_slack_ns = None
    if target_slack_raw is not None:
        try:
            target_slack_ns = float(target_slack_raw)
        except (TypeError, ValueError):
            raise FlowProfileError(
                PROFILE_INVALID,
                "repair_recipe.target_slack_ns must be a float or decimal token")
    return SetupMarginRecipe(
        recipe_id=node["recipe_id"],
        method=SetupMarginMethod(
            variable=method_node["variable"],
            baseline_value=str(method_node["baseline_value"]),
            candidate_value=candidate_value,
            consumption_stages=tuple(str(s) for s in stages),
            consumption_evidence=tuple(str(e) for e in evidence)),
        helper_dir=helper_dir, helpers=helpers,
        config_suffix_sha256=str(node["config_suffix_sha256"]),
        candidate_config_suffix_sha256=str(node["candidate_config_suffix_sha256"]),
        acceptance_policy_id=node["acceptance_policy_id"],
        comparison_policy_id=node["comparison_policy_id"],
        proof_policy_id=node["proof_policy_id"],
        qualified_tool_identity_path=identity_path,
        qualified_tool_identity_sha256=identity_sha256,
        target_slack_ns=target_slack_ns)


def _build_miter_recipe(node: Any) -> TransitionMiterRecipe:
    """v0.12 的 recipe。每一個 id 都是嚴格枚舉，不接受任意字串或路徑。"""
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "repair_recipe must be an object")
    unknown = sorted(set(node) - _MITER_RECIPE_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown repair_recipe field(s): {unknown}")
    # `qualified_tool_identity_path` 是可選的，不列入必填。
    missing = sorted(_MITER_RECIPE_FIELDS - {"qualified_tool_identity_path"}
                     - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing repair_recipe field(s): {missing}")
    fixed = (
        ("recipe_id", FIXED_MITER_RECIPE_ID),
        ("acceptance_policy_id", FIXED_MITER_POLICY_ID),
        ("geometry_policy_id", FIXED_GEOMETRY_POLICY_ID),
        ("comparison_policy_id", FIXED_PREFIX_BORROW_COMPARISON_POLICY_ID),
        ("proof_policy_id", FIXED_MITER_PROOF_POLICY_ID),
    )
    for field, expected in fixed:
        if node[field] != expected:
            raise FlowProfileError(
                PROFILE_INVALID, f"repair_recipe.{field} must be {expected!r}")
    helper_dir, helpers = _build_helper_refs(node)
    suffix = _require_str(node, "config_suffix_sha256")
    if not _SHA256_RE.match(suffix):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.config_suffix_sha256 must be 64 lowercase hex characters")
    return TransitionMiterRecipe(
        recipe_id=node["recipe_id"], helper_dir=helper_dir, helpers=helpers,
        config_suffix_sha256=suffix,
        acceptance_policy_id=node["acceptance_policy_id"],
        geometry_policy_id=node["geometry_policy_id"],
        comparison_policy_id=node["comparison_policy_id"],
        rtl_patch=_build_rtl_patch(node["rtl_patch"]),
        proof_policy_id=node["proof_policy_id"],
        qualified_tool_identity_path=_optional_abs_path(
            node, "qualified_tool_identity_path"))


def _build_prefix_recipe(node: Any) -> PrefixBorrowRecipe:
    """v0.8 的 recipe。每一個 id 都是嚴格枚舉，不接受任意字串或路徑。"""
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "repair_recipe must be an object")
    unknown = sorted(set(node) - _PREFIX_RECIPE_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown repair_recipe field(s): {unknown}")
    missing = sorted(_PREFIX_RECIPE_FIELDS - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing repair_recipe field(s): {missing}")
    fixed = (
        ("recipe_id", FIXED_PREFIX_BORROW_RECIPE_ID),
        ("acceptance_policy_id", FIXED_PREFIX_BORROW_POLICY_ID),
        ("geometry_policy_id", FIXED_GEOMETRY_POLICY_ID),
        ("comparison_policy_id", FIXED_PREFIX_BORROW_COMPARISON_POLICY_ID),
    )
    for field, expected in fixed:
        if node[field] != expected:
            raise FlowProfileError(
                PROFILE_INVALID, f"repair_recipe.{field} must be {expected!r}")
    helper_dir, helpers = _build_helper_refs(node)
    suffix = _require_str(node, "config_suffix_sha256")
    if not _SHA256_RE.match(suffix):
        raise FlowProfileError(
            PROFILE_INVALID,
            "repair_recipe.config_suffix_sha256 must be 64 lowercase hex characters")
    return PrefixBorrowRecipe(
        recipe_id=node["recipe_id"], helper_dir=helper_dir, helpers=helpers,
        config_suffix_sha256=suffix,
        acceptance_policy_id=node["acceptance_policy_id"],
        geometry_policy_id=node["geometry_policy_id"],
        comparison_policy_id=node["comparison_policy_id"],
        rtl_patch=_build_rtl_patch(node["rtl_patch"]))


def _build_helper_refs(node: Any) -> tuple[str, tuple[RepairHelperRef, ...]]:
    """`helper_dir` ＋ `helpers` 的共同驗證（v3～v5 都用同一套規則）。"""
    helper_dir = _require_str(node, "helper_dir")
    if Path(helper_dir).is_absolute() or ".." in Path(helper_dir).parts:
        raise FlowProfileError(
            PROFILE_INVALID, "repair_recipe.helper_dir must be a safe relative path")
    raw_helpers = node["helpers"]
    if not isinstance(raw_helpers, list) or not raw_helpers:
        raise FlowProfileError(
            PROFILE_INVALID, "repair_recipe.helpers must be a non-empty list")
    helpers: list[RepairHelperRef] = []
    for entry in raw_helpers:
        if not isinstance(entry, dict) or set(entry) != _HELPER_FIELDS:
            raise FlowProfileError(
                PROFILE_INVALID, "each repair_recipe.helpers entry needs "
                "exactly relative_path and sha256")
        rel = str(entry["relative_path"])
        if not rel.startswith(helper_dir.rstrip("/") + "/"):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"helper {rel!r} must live under {helper_dir!r}")
        sha = str(entry["sha256"])
        if not _SHA256_RE.match(sha):
            raise FlowProfileError(
                PROFILE_INVALID, f"helper {rel!r} needs a sha256 digest")
        helpers.append(RepairHelperRef(relative_path=rel, sha256=sha))
    return helper_dir, tuple(helpers)


def _build_repair_recipe(node: Any) -> RepairRecipe:
    if not isinstance(node, dict):
        raise FlowProfileError(PROFILE_INVALID, "repair_recipe must be an object")
    unknown = sorted(set(node) - _RECIPE_FIELDS)
    if unknown:
        raise FlowProfileError(
            PROFILE_INVALID, f"unknown repair_recipe field(s): {unknown}")
    missing = sorted(_RECIPE_FIELDS - set(node))
    if missing:
        raise FlowProfileError(
            PROFILE_INVALID, f"missing repair_recipe field(s): {missing}")
    if node["recipe_id"] != FIXED_REPAIR_RECIPE_ID:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"repair_recipe.recipe_id must be {FIXED_REPAIR_RECIPE_ID!r}")
    if node["acceptance_policy_id"] != FIXED_REPAIR_POLICY_ID:
        raise FlowProfileError(
            PROFILE_INVALID,
            f"repair_recipe.acceptance_policy_id must be {FIXED_REPAIR_POLICY_ID!r}")
    if node["algorithm"] != "restructure" or node["target"] != "delay":
        # `target` 在 profile 裡記的是**工程意圖**（delay）；實際 argv 的拼寫
        # 由 pinned 工具決定，並在 recipe 的 helper bytes 內固定。
        raise FlowProfileError(
            PROFILE_INVALID,
            "this round fixes algorithm=restructure and target=delay")
    for field in ("slack_threshold",):
        value = node[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FlowProfileError(
                PROFILE_INVALID, f"repair_recipe.{field} must be a number")
    for field in ("depth_threshold", "invocations"):
        value = node[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"repair_recipe.{field} must be a positive integer")
    if node["invocations"] != 1:
        raise FlowProfileError(
            PROFILE_INVALID, "this round allows exactly one restructure call")
    helper_dir = _require_str(node, "helper_dir")
    if Path(helper_dir).is_absolute() or ".." in Path(helper_dir).parts:
        raise FlowProfileError(
            PROFILE_INVALID, "repair_recipe.helper_dir must be a safe relative path")
    raw_helpers = node["helpers"]
    if not isinstance(raw_helpers, list) or not raw_helpers:
        raise FlowProfileError(
            PROFILE_INVALID, "repair_recipe.helpers must be a non-empty list")
    helpers: list[RepairHelperRef] = []
    for entry in raw_helpers:
        if not isinstance(entry, dict) or set(entry) != _HELPER_FIELDS:
            raise FlowProfileError(
                PROFILE_INVALID,
                f"each helper needs exactly {sorted(_HELPER_FIELDS)}")
        rel = str(entry["relative_path"])
        if not rel.startswith(helper_dir.rstrip("/") + "/"):
            raise FlowProfileError(
                PROFILE_INVALID,
                f"helper {rel!r} is outside the approved helper directory")
        if ".." in Path(rel).parts:
            raise FlowProfileError(PROFILE_INVALID, f"unsafe helper path: {rel!r}")
        digest = str(entry["sha256"])
        if not _SHA256_RE.match(digest):
            raise FlowProfileError(
                PROFILE_INVALID, f"helper {rel!r} needs a 64 hex sha256")
        helpers.append(RepairHelperRef(relative_path=rel, sha256=digest))
    for field in ("config_suffix_sha256", "candidate_config_suffix_sha256"):
        if not _SHA256_RE.match(str(node[field])):
            raise FlowProfileError(
                PROFILE_INVALID, f"repair_recipe.{field} must be 64 hex characters")
    return RepairRecipe(
        recipe_id=node["recipe_id"], algorithm=node["algorithm"],
        target=node["target"], slack_threshold=float(node["slack_threshold"]),
        depth_threshold=int(node["depth_threshold"]),
        invocations=int(node["invocations"]), helper_dir=helper_dir,
        helpers=tuple(helpers),
        config_suffix_sha256=str(node["config_suffix_sha256"]),
        candidate_config_suffix_sha256=str(node["candidate_config_suffix_sha256"]),
        acceptance_policy_id=node["acceptance_policy_id"])


def configured_profile_path() -> str | None:
    value = os.environ.get(PROFILE_ENV, "").strip()
    return value or None


# ── 啟動時載入一次，之後只驗「沒有變」（無 hot reload）───────────


class FlowProfileGuard:
    """持有啟動時載入的 profile，並在每個授權點確認它沒有變。

    與 `access_policy.PolicyGuard` 同一原則：檔案不見了、讀不到、解析失敗，
    或 digest 與啟動時不同，一律拋出——**不採用新內容**。
    """

    def __init__(self, profile: FlowTaskProfile, path: str) -> None:
        self._profile = profile
        self._path = path

    @property
    def startup_profile(self) -> FlowTaskProfile:
        return self._profile

    @property
    def path(self) -> str:
        return self._path

    def current(self) -> FlowTaskProfile:
        reloaded = load_profile(self._path)
        if reloaded.profile_digest != self._profile.profile_digest:
            raise FlowProfileError(
                PROFILE_CHANGED,
                "the flow task profile changed since daemon startup; restart the "
                "daemon to apply it (no task may use the new profile)")
        return self._profile


def load_guard_from_env() -> FlowProfileGuard | None:
    """**Deprecated。** production 路徑不再依賴 daemon-global
    profile；這個函式只保留給舊測試工具。daemon 啟動時若仍設了
    `TERMINUX_FLOW_TASK_PROFILE_FILE`，只記錄 deprecation warning、忽略它，
    **不**把它變成 process-global active profile，也不猜它與 request 來源的
    優先順序。要執行它，請用 `profile_path` 建立 task。"""
    path = configured_profile_path()
    if path is None:
        return None
    return FlowProfileGuard(load_profile(path), path)


def deprecated_env_profile_path() -> str | None:
    """只回報是否還設著舊的 daemon-global 變數，不載入它。"""
    return configured_profile_path()


_KNOWN_FIELD_NAMES = _TOP_LEVEL_FIELDS | _PARAMETER_FIELDS | _LIMIT_FIELDS

__all__ = [
    "FIXED_PARAMETER_NAME",
    "FIXED_TEMPLATE_ID",
    "CORE_UTILIZATION_TEMPLATE_ID",
    "PROFILE_CHANGED",
    "PROFILE_ENV",
    "PROFILE_INVALID",
    "PROFILE_NOT_CONFIGURED",
    "PROFILE_SOURCE_INVALID",
    "PROFILE_UNREADABLE",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_CORE_UTILIZATION",
    "SLOTS",
    "SLOT_BASELINE",
    "SLOT_CANDIDATE",
    "ExecutionSpec",
    "FixtureCorpusDeclaration",
    "QUALIFICATION_ONLY",
    "FlowLimits",
    "FlowParameter",
    "FlowProfileError",
    "FlowProfileGuard",
    "FlowTaskProfile",
    "ProfileSource",
    "ProofToolDeclaration",
    "ProposalIntake",
    "UserDeclared",
    "build_profile",
    "build_profile_document_from_inline",
    "build_profile_from_inline",
    "configured_profile_path",
    "deprecated_env_profile_path",
    "load_guard_from_env",
    "load_profile",
    "load_profile_document_from_path",
    "load_profile_from_path",
]
