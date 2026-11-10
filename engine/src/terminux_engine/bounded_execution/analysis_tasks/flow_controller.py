# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""固定五步 flow task 的 controller。

它是 `AnalysisTaskService` 的 `flow` 依賴：service 管 lifecycle／鎖／名額，
controller 管「這個 template 的每一步該做什麼」。

固定五步（順序與 capability 都不可變）：

| step | capability | 產出 |
| --- | --- | --- |
| s1 | `run_flow(baseline)` | baseline RunRef ＋ 完成證據 |
| s2 | `explain_run` | baseline 的分析結果 |
| s3 | `prepare_variant` | 候選 input snapshot（唯一 diff） |
| s4 | `run_flow(candidate)` | candidate RunRef ＋ 完成證據 |
| s5 | `compare_runs` | 兩個 run 的工程比較 |

前段＝s1＋s2，後段＝s3～s5。前段的完成條件全部成立才自動進 s3；條件不足就停止
依賴它的修改／派送，保留前段已完成的事實。
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable

from terminux_engine.bounded_execution.analysis_tasks import errors as E
from terminux_engine.bounded_execution.analysis_tasks import flow_models as F
from terminux_engine.bounded_execution.analysis_tasks import flow_worker as W
from terminux_engine.bounded_execution.analysis_tasks import models as M
from terminux_engine.bounded_execution.analysis_tasks import repair_evidence as RE_HOST
from terminux_engine.bounded_execution.analysis_tasks import run_evidence as RE
from terminux_engine.bounded_execution.analysis_tasks.canonical import monotonic_s

_LOG = logging.getLogger(__name__)

CAP_RUN_FLOW = "orfs_run_intelligence.run_flow"
CAP_PREPARE_VARIANT = "orfs_run_intelligence.prepare_variant"
CAP_EXPLAIN_RUN = "orfs_run_intelligence.explain_run"
CAP_COMPARE_RUNS = "orfs_run_intelligence.compare_runs"

#: 每一步的固定 capability。與 `FLOW_STEP_IDS` 一一對應。
#: selector 需要的 setup 指標（由 Package policy 定義，這裡只引用）。
SEL_SETUP_METRICS: tuple[str, ...] = (
    "orfs_setup_wns", "orfs_setup_tns", "orfs_setup_violation_count")

STEP_CAPABILITIES: dict[str, str] = {
    F.STEP_RUN_BASELINE: CAP_RUN_FLOW,
    F.STEP_EXPLAIN: CAP_EXPLAIN_RUN,
    F.STEP_PREPARE_VARIANT: CAP_PREPARE_VARIANT,
    F.STEP_RUN_CANDIDATE: CAP_RUN_FLOW,
    F.STEP_COMPARE: CAP_COMPARE_RUNS,
}


#: ECO 前置步驟的 capability。
CAP_PREPARE_REPAIR = "orfs_run_intelligence.prepare_timing_repair"


def _capabilities_for(template_id: str) -> dict[str, str]:
    """每個 template 有自己的固定 capability 序列。"""
    if template_id == F.BASELINE_ONLY_TEMPLATE_ID:
        return {F.STEP_RUN_BASELINE: CAP_RUN_FLOW}
    if template_id == F.BOUNDED_SEARCH_TEMPLATE_ID:
        return F.BOUNDED_SEARCH_CAPABILITIES
    if template_id == F.EAP_SETUP_MARGIN_TEMPLATE_ID:
        return F.EAP_SETUP_MARGIN_CAPABILITIES
    if template_id == F.EAP_CORE_UTILIZATION_TEMPLATE_ID:
        return F.EAP_CORE_UTILIZATION_CAPABILITIES
    if template_id in F.SETUP_REPAIR_TEMPLATES:
        return F.SETUP_REPAIR_CAPABILITIES
    return STEP_CAPABILITIES


def _bind_eap_candidate_snapshot(
    repair_recipe: dict[str, Any] | None,
    execution_plan: dict[str, Any],
    candidate: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """將 Package 核准候選固定到 task snapshot 的所有方法欄位。

    Profile 仍是原始環境定義，不能被動態 proposal 改寫；因此只複製 DRAFT
    即將保存的 recipe／execution plan。helper hash 保留 Profile 已驗證的原始
    身分，實際候選 config hash則由 ``candidate_plan`` 另外固定。
    """
    recipe = copy.deepcopy(repair_recipe)
    plan = copy.deepcopy(execution_plan)
    if recipe is not None:
        method = recipe.get("method")
        if isinstance(method, dict):
            method["candidate_value"] = candidate
    plan_method = plan.get("method")
    if isinstance(plan_method, dict):
        parameter = plan_method.get("parameter")
        if isinstance(parameter, dict):
            if "candidate" in parameter:
                parameter["candidate"] = candidate
            if "candidates" in parameter:
                parameter["candidates"] = [candidate]
        method = plan_method.get("method")
        if isinstance(method, dict):
            method["candidate_value"] = candidate
    return recipe, plan


def _clear_candidate_from_baseline_snapshot(
    repair_recipe: dict[str, Any] | None,
    execution_plan: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """baseline-only digest 不攜帶 Profile 的 legacy candidate seed。"""
    recipe = copy.deepcopy(repair_recipe)
    plan = copy.deepcopy(execution_plan)
    if isinstance(recipe, dict) and isinstance(recipe.get("method"), dict):
        recipe["method"]["candidate_value"] = None
    method = plan.get("method")
    if isinstance(method, dict):
        parameter = method.get("parameter")
        if isinstance(parameter, dict):
            parameter["candidate"] = None
            if "candidates" in parameter:
                parameter["candidates"] = []
        nested = method.get("method")
        if isinstance(nested, dict):
            nested["candidate_value"] = None
    return recipe, plan


#: 這些 step 會啟動一次 EDA。
def _is_eda_step(template_id: str, step_id: str) -> bool:
    return _capabilities_for(template_id).get(step_id) == CAP_RUN_FLOW


def _is_prepare_step(template_id: str, step_id: str) -> bool:
    return _capabilities_for(template_id).get(step_id) in (
        CAP_PREPARE_VARIANT, CAP_PREPARE_REPAIR)


def _slot_for_step(task: Any, step_id: str) -> str:
    """這一步操作哪一個 slot。"""
    if step_id == F.STEP_RUN_BASELINE:
        return F.SLOT_BASELINE
    if (getattr(task, "template_id", "") in F.SETUP_REPAIR_TEMPLATES
            or getattr(task, "template_id", "") == F.EAP_CORE_UTILIZATION_TEMPLATE_ID):
        return F.SLOT_CANDIDATE
    if getattr(task, "is_bounded_search", False):
        slot = F.candidate_slot_for_step(step_id)
        if slot is not None:
            return slot
        return F.SLOT_BASELINE
    return F.SLOT_CANDIDATE


class FlowDraftError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FlowDraft:
    task: Any
    profile: Any


@dataclass(frozen=True)
class FlowControllerDeps:
    """controller 的依賴。由 daemon composition 注入。"""

    #: `(task_id) -> FlowTaskProfile`——task-owned immutable snapshot。
    profile_for_task: Callable[[str], Any]
    step_deps: W.FlowStepDeps
    #: `(profile) -> InputManifest`，讀回並驗 manifest 原始 bytes。
    load_manifest: Callable[[Any], Any]
    #: `(manifest, name, baseline, candidate) -> (config_bytes, tree_hash)`
    derive_candidate: Callable[..., Any]
    #: `(run_ref) -> dict`，請 Kernel 取消一個已綁定的 run。
    cancel_run: Callable[[Any], dict[str, Any]]
    #: `(binding) -> VariantInputRef`，Package 的受控寫入 helper。
    variant_writer: Callable[[Any], Any]
    #: `() -> (ready, reason)`，Docker/image/來源條件的派工前核對。
    preflight_check: Callable[[Any], tuple[bool, str | None]]
    #: 候選 config 追加內容的預期身分。只有 v3 profile 會用到。
    derive_repair_candidate: Callable[..., Any] | None = None
    #: Package 編譯的單工具計畫仍由同一個 host-only task run 入口派送。
    start_package_run: Callable[..., Any] | None = None


def _all_child_runs_terminal(ledger: Any, live: Any) -> tuple[bool, str]:
    """判定 live flow task 的所有 child run 是否均已終結。

    三種情況：
    1. child 仍在執行：ledger 狀態為非 terminal（例如 running／pending），名額應保留。
    2. child 狀態未知：無 ledger、無法讀取 ledger 或找不到 run，保守保留名額。
       若無 child runs 紀錄，但曾有 dispatch（或非明確未啟動狀態），亦屬狀態未知，保留名額。
    3. child 已結束（或從未啟動）：
       - 從未啟動：無 child runs 且明確無 dispatch（`not live.dispatched`）。
       - 已結束：所有 child run 皆為 terminal 狀態。
       名額應釋放或由 reconciliation 收回。
    """
    runs = list(getattr(live, "runs", {}).values())
    dispatched = getattr(live, "dispatched", None)
    had_dispatch = bool(dispatched)

    if not runs:
        # 無 child run 紀錄時，必須有可靠的「尚未啟動」依據才能釋放名額
        if not had_dispatch:
            return True, "never_dispatched_no_child_runs"
        # 若已 dispatch 過步驟卻無 child runs 記錄，屬於情況 2（狀態未知），保守保留名額走對帳
        return False, "dispatched_but_no_runs_unknown"
    if ledger is None:
        return False, "ledger_unavailable"

    for run_ref in runs:
        try:
            record = ledger.get_run(run_ref.run_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"ledger_error:{exc!r}"
        if record is None:
            return False, f"run_not_in_ledger:{run_ref.run_id}"
        status = getattr(record, "status", None)
        if status is None and isinstance(record, dict):
            status = record.get("status")
        if status not in RE.TERMINAL_RUN_STATES:
            return False, f"child_still_running:{run_ref.run_id}:{status}"
    return True, "all_child_runs_terminal"


class FlowController:
    """固定五步的執行控制。"""

    def __init__(self, deps: FlowControllerDeps) -> None:
        self._deps = deps

    # ── create ──────────────────────────────────────────────────────

    def build_draft(
        self, *, profile: Any, profile_source: Any, question: str, context: Any,
        daemon_instance_id: str,
    ) -> FlowDraft:
        if getattr(profile, "is_core_utilization", False):
            raise FlowDraftError(
                E.TASK_INPUT_INVALID,
                "CORE_UTILIZATION requires a Package-validated EAP proposal")
        return self._build_draft(
            profile=profile, profile_source=profile_source, question=question,
            context=context, daemon_instance_id=daemon_instance_id)

    def build_eap_draft(
        self, *, profile: Any, profile_source: Any, question: str, context: Any,
        daemon_instance_id: str, proposal: dict[str, Any],
    ) -> FlowDraft:
        """將已驗證 proposal 固定為獨立 task 草案，不改寫 Profile。"""
        package = proposal.get("package")
        changes = package.get("changes") if isinstance(package, dict) else None
        core_utilization = (isinstance(changes, dict)
                            and changes.get("kind") == "core_utilization"
                            and changes.get("parameter") == "CORE_UTILIZATION")
        if (not isinstance(changes, dict)
                or (not isinstance(changes.get("candidate"), str)
                    and not (core_utilization and isinstance(changes.get("candidate"), int)
                             and not isinstance(changes.get("candidate"), bool)))):
            raise FlowDraftError(E.TASK_INPUT_INVALID,
                                 "the validated EAP proposal has no candidate")
        if package.get("parameter_name") != profile.parameter.name:
            raise FlowDraftError(
                E.TASK_INPUT_INVALID,
                "the EAP proposal parameter does not match the selected Profile")
        return self._build_draft(
            profile=profile, profile_source=profile_source, question=question,
            context=context, daemon_instance_id=daemon_instance_id,
            template_id=(F.EAP_CORE_UTILIZATION_TEMPLATE_ID if core_utilization
                         else F.EAP_SETUP_MARGIN_TEMPLATE_ID),
            # Flow config 是 Make token，Package 驗證後才做無損整數序列化；
            # canonical proposal 保留 typed int 作稽核權威。
            candidate_override=str(changes["candidate"]), eap_proposal=proposal)

    def build_package_draft(
        self, *, profile: Any, profile_source: Any, question: str, context: Any,
        daemon_instance_id: str, proposal: dict[str, Any],
        create_identity: dict[str, Any] | None = None,
    ) -> FlowDraft:
        """由通用 Package tool contract 建單步 FlowTask；不解讀領域改法。"""
        from terminux_engine.bounded_execution.analysis_tasks.package_flow import (
            build_package_draft,
        )

        return build_package_draft(
            profile=profile, profile_source=profile_source, question=question,
            context=context, daemon_instance_id=daemon_instance_id,
            proposal=proposal, create_identity=create_identity)

    def build_baseline_draft(
        self, *, profile: Any, profile_source: Any, question: str, context: Any,
        daemon_instance_id: str, request: dict[str, Any],
    ) -> FlowDraft:
        """建立只有 baseline s1 的正式 task；不推導或建立 candidate。"""
        return self._build_draft(
            profile=profile, profile_source=profile_source, question=question,
            context=context, daemon_instance_id=daemon_instance_id,
            template_id=F.BASELINE_ONLY_TEMPLATE_ID,
            eap_proposal={"kind": "BASELINE_ONLY", "source": dict(request)})

    def _build_draft(
        self, *, profile: Any, profile_source: Any, question: str, context: Any,
        daemon_instance_id: str, template_id: str | None = None,
        candidate_override: str | None = None,
        eap_proposal: dict[str, Any] | None = None,
    ) -> FlowDraft:
        """建立草案。輕量驗證，不重算 3.2 GB、不 materialize candidate。

        `profile` 是 service 依 user 提供的來源（path／inline／
        session 選擇）載入並驗證過的 task-scoped snapshot；`profile_source`
        是它的來源身分。兩者都綁進 task digest；之後不再重讀 live 檔案。
        """
        from terminux_engine.bounded_execution.binding import (
            compute_arguments_sha256, compute_capability_contract_sha256,
        )

        if not profile.matches_scope(context.member_id, context.project_id):
            raise FlowDraftError(
                E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                "the execution profile does not cover this member/project")

        # manifest 原始 bytes/hash ＋ inventory 引用一致性 ＋ 固定 config bytes。
        manifest = self._deps.load_manifest(profile)
        bounded = profile.is_bounded_search
        repair = (getattr(profile, "is_setup_repair", False)
                  and template_id != F.EAP_SETUP_MARGIN_TEMPLATE_ID)
        # template 身分直接取自已驗證的 profile：schema version 與
        # template_id 在 `build_profile()` 已互相綁定，因此 v4 profile 一定
        # 得到 `orfs.setup_repair.v2`，不會退回舊 template。
        template_id = template_id or profile.template_id
        if (template_id not in F.SETUP_REPAIR_TEMPLATES
                and template_id != F.EAP_CORE_UTILIZATION_TEMPLATE_ID
                and template_id not in (F.BOUNDED_SEARCH_TEMPLATE_ID,
                                        F.FLOW_TEMPLATE_ID,
                                        F.BASELINE_ONLY_TEMPLATE_ID)):
            raise E.TaskError(
                E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                f"unsupported flow template: {template_id!r}")
        step_ids = F.step_ids_for_template(template_id)
        capability_map = _capabilities_for(template_id)

        # 每個候選各自推導預期 config／tree hash，全部綁進 task digest。
        candidate_plan: list[dict[str, Any]] = []
        baseline_only = template_id == F.BASELINE_ONLY_TEMPLATE_ID
        if baseline_only:
            # baseline-only 是獨立 template：candidate 身分明確為空，不能拿
            # Profile 的預設 candidate 假裝已經有一份方案。
            expected_tree_hash = None
            candidate_config_sha = None
        elif bounded:
            for index, slot in enumerate(
                    (F.SLOT_CANDIDATE_1, F.SLOT_CANDIDATE_2)):
                if index >= len(profile.parameter.candidates):
                    break
                value = profile.parameter.candidates[index]
                _bytes, tree_hash, config_sha = self._deps.derive_candidate(
                    profile, manifest, candidate_value=value)
                candidate_plan.append({
                    "slot": slot, "order_index": index, "value": value,
                    "expected_tree_hash": tree_hash,
                    "expected_config_sha256": config_sha,
                })
            expected_tree_hash = candidate_plan[0]["expected_tree_hash"]
            candidate_config_sha = candidate_plan[0]["expected_config_sha256"]
        elif eap_proposal is not None:
            if candidate_override is None:
                raise FlowDraftError(E.TASK_INPUT_INVALID,
                                     "the EAP proposal candidate is missing")
            _bytes, expected_tree_hash, candidate_config_sha = (
                self._deps.derive_candidate(
                    profile, manifest, candidate_value=candidate_override))
            candidate_plan.append({
                "slot": F.SLOT_CANDIDATE, "order_index": 0,
                "value": candidate_override,
                "expected_tree_hash": expected_tree_hash,
                "expected_config_sha256": candidate_config_sha,
            })
        elif repair:
            # 唯一差異是已核准的 config 追加內容，參數值兩邊相同。
            # 差異是 `ABC_AREA` 的 token ＋ s1 之後才存在的 geometry
            # 段，因此這裡綁的是**規則**的 digest，實際 tree hash 由 s3 產生。
            _bytes, expected_tree_hash, candidate_config_sha = (
                self._deps.derive_repair_candidate(profile, manifest))
            candidate_plan.append({
                "slot": F.SLOT_CANDIDATE, "order_index": 0,
                "value": profile.parameter.candidates[0],
                "expected_tree_hash": expected_tree_hash,
                "expected_config_sha256": candidate_config_sha,
            })
        else:
            _bytes, expected_tree_hash, candidate_config_sha = (
                self._deps.derive_candidate(profile, manifest))

        snapshot_id, capabilities = self._deps.step_deps.catalog_provider()
        steps: list[M.TaskStepSpec] = []
        for step_id in step_ids:
            capability_id = capability_map[step_id]
            capability = capabilities.get(capability_id)
            if capability is None:
                raise FlowDraftError(
                    E.TASK_REVALIDATION_FAILED,
                    f"capability {capability_id!r} is not registered")
            # snapshot 只記**綁定規則**下可先確定的參數；run id 由 host 在
            # 執行當下具體化（LLM 沒有預填 run id 的位置）。
            arguments = self._planned_arguments(step_id, question,
                                                template_id=template_id)
            steps.append(M.TaskStepSpec(
                step_id=step_id, capability_id=capability_id, arguments=arguments,
                arguments_sha256=compute_arguments_sha256(arguments),
                capability_contract_sha256=compute_capability_contract_sha256(capability),
                catalog_snapshot_id=snapshot_id,
                risk=str(capability.get("risk")),
                approval_required=bool(capability.get("approval_required")),
                manifest_timeout_sec=capability.get("timeout_sec"),
            ))

        # `context` 是 service 算好的**有效** context——session 明確宣告
        # 就是 session 的 project；未宣告就是 Profile 的 project（project_source=
        # "profile"）。scope 一經固定，後續 start／dispatch／get／cancel 都不再重推。
        scope = M.TaskScope(
            member_id=context.member_id, project_id=context.project_id,
            origin_session_id=context.session_id,
            workspace=getattr(context, "workspace", "") or "",
            project_source=getattr(context, "project_source", "session"))
        repair_recipe = (profile.repair_recipe.to_json_dict()
                         if getattr(profile, "repair_recipe", None) is not None
                         else None)
        execution_plan = profile.execution_plan()
        limits = profile.limits.to_json_dict()
        if baseline_only:
            repair_recipe, execution_plan = _clear_candidate_from_baseline_snapshot(
                repair_recipe, execution_plan)
            execution_plan = {
                **execution_plan,
                "template_id": F.BASELINE_ONLY_TEMPLATE_ID,
                "steps": [{"step_id": F.STEP_RUN_BASELINE,
                           "capability_id": CAP_RUN_FLOW,
                           "slot": F.SLOT_BASELINE}],
                "budget": {**limits, "eda_launches": 1,
                           "candidate_writes": 0, "body_llm_dispatches": 0},
                "side_effects": [
                    f"launch exactly one baseline EDA run via {execution_plan['runner']}",
                    f"write DQE rows to {profile.dqe_db_path}",
                    f"write artifact manifests under {profile.artifact_root}",
                ],
            }
            limits = dict(execution_plan["budget"])
        elif eap_proposal is not None:
            repair_recipe, execution_plan = _bind_eap_candidate_snapshot(
                repair_recipe, execution_plan, str(candidate_override))
            baseline_plan = eap_proposal.get("baseline_plan")
            if (isinstance(baseline_plan, dict)
                    and baseline_plan.get("mode") == "REUSE_VALIDATED_EAP3_BASELINE"):
                # baseline-only child 已產生並驗證工程證據。candidate
                # DRAFT 將該 host-validated import 完整固定進 digest，並只
                # 授權一次 candidate launch；不能再偷跑 baseline。
                limits = {**limits, "eda_launches": 1,
                          "candidate_writes": 1}
                execution_plan = {
                    **execution_plan,
                    "baseline_plan": copy.deepcopy(baseline_plan),
                    "budget": dict(limits),
                    "side_effects": [
                        "reuse one host-validated baseline from the bound Engineering Work child task",
                        "materialize exactly one candidate under the approved writable root",
                        f"launch exactly one candidate EDA run via {execution_plan['runner']}",
                        f"write DQE rows to {profile.dqe_db_path}",
                        f"write artifact manifests under {profile.artifact_root}",
                    ],
                }
        task = F.build_flow_task_snapshot(
            task_id=F.new_flow_task_id(), daemon_instance_id=daemon_instance_id,
            scope=scope, profile_id=profile.profile_id,
            profile_digest=profile.profile_digest, question=question,
            design_id=profile.design_id, platform_id=profile.platform_id,
            parameter_name=profile.parameter.name,
            baseline_value=profile.parameter.baseline,
            candidate_value=(None if baseline_only else
                             candidate_override if candidate_override is not None
                             else (profile.parameter.candidates[0]
                                   if (bounded or repair)
                                   else profile.parameter.candidate)),
            baseline_input_manifest_sha256=profile.input_manifest_sha256,
            baseline_input_tree_hash=manifest.tree_hash,
            expected_candidate_tree_hash=expected_tree_hash,
            expected_candidate_config_sha256=candidate_config_sha,
            task_deadline_sec=profile.limits.task_deadline_sec,
            run_timeout_sec=profile.limits.run_timeout_sec,
            limits=limits, steps=tuple(steps),
            template_id=template_id,
            candidate_plan=tuple(candidate_plan),
            selection_policy=(profile.selection_policy.to_json_dict()
                              if profile.selection_policy is not None else None),
            repair_recipe=repair_recipe,
            # 來源身分與展開後的執行計畫都進 digest——人核准的就是
            # 這一份；live 檔案之後改了也換不掉。
            profile_source=(profile_source.to_json_dict()
                            if profile_source is not None else None),
            execution_plan=execution_plan,
            eap_proposal=eap_proposal,
        )
        return FlowDraft(task=task, profile=profile)

    @staticmethod
    def _planned_arguments(step_id: str, question: str, *,
                           template_id: str = F.FLOW_TEMPLATE_ID) -> dict[str, Any]:
        """snapshot 階段可先確定的參數。

        `s2`／`s5` 的 run id 尚不存在，因此這裡**不放**任何 run id 佔位值——
        它們在執行當下由 host 從已驗證的 Kernel reference 具體化。
        """
        bounded = template_id == F.BOUNDED_SEARCH_TEMPLATE_ID
        capability = _capabilities_for(template_id).get(step_id)
        if capability == CAP_RUN_FLOW:
            if step_id == F.STEP_RUN_BASELINE:
                return {"slot": F.SLOT_BASELINE}
            return {"slot": (F.candidate_slot_for_step(step_id) if bounded
                             else F.SLOT_CANDIDATE)}
        if capability == CAP_PREPARE_REPAIR:
            # 唯一允許的 slot，寫死在草案裡。
            return {"slot": F.SLOT_CANDIDATE}
        if capability == CAP_PREPARE_VARIANT:
            # v1 的 prepare 不帶 slot（既有草案 hash 不變）；bounded utilization search 明確帶 slot。
            return ({"slot": F.candidate_slot_for_step(step_id)} if bounded
                    else {})
        return {"question": question}

    def preflight(self, task: Any) -> tuple[bool, str | None]:
        """派工前的來源／image／profile 條件核對。不呼叫 LLM。"""
        if task.template_id == F.PACKAGE_SINGLE_TOOL_TEMPLATE_ID:
            from terminux_engine.bounded_execution.analysis_tasks.package_flow import (
                package_preflight,
            )

            return package_preflight(task, self._deps.profile_for_task(task.task_id))
        return self._deps.preflight_check(task)

    def cancel_run(self, run_ref: Any, *, output_root: str | None = None,
                   docker_executable: str | None = None,
                   native: bool = False) -> dict[str, Any]:
        """ownership-first cancel：先請 Kernel 取消 run，再依本 slot 的 cidfile
        精確停止 container（不用全機器 `docker ps`／模糊 `pgrep`）。

        host-native run 沒有 container：改依本 slot 的 pgid 紀錄與 run 標記
        停止並確認 process group 已不存在。"""
        result = dict(self._deps.cancel_run(run_ref))
        if native:
            from terminux_engine.orchestration.native_tool_identity import (
                release_native_process_group,
            )

            if output_root:
                result["native_process_group"] = release_native_process_group(
                    output_root, str(run_ref.run_id))
            return result
        if output_root:
            from terminux_engine.orchestration.orfs_flow_task_adapter import (
                cancel_container_by_cidfile,
            )

            result["container"] = cancel_container_by_cidfile(
                output_root, docker_executable=docker_executable or "docker")
        return result

    # ── worker ──────────────────────────────────────────────────────

    def run(self, service: Any, live: Any) -> None:
        """五步 worker。由 service 在背景 thread 呼叫。"""
        from terminux_engine.bounded_execution.analysis_tasks.budget import (
            BUDGET_STOPPED, TaskBodyBudget, reset_current_task_budget,
            set_current_task_budget,
        )
        from terminux_engine.ipc.request_context import (
            CanonicalRequestContext, reset_current_request_context,
            set_current_request_context,
        )

        task = live.task
        grant = live.grant
        context = CanonicalRequestContext(
            session_id=task.scope.origin_session_id,
            member_id=task.scope.member_id, project_id=task.scope.project_id,
            project_source=task.scope.project_source)

        def _should_stop() -> str | None:
            if live.stop:
                return BUDGET_STOPPED
            if grant is not None and monotonic_s() >= grant.deadline_at_monotonic:
                return BUDGET_STOPPED
            return None

        from terminux_engine.expert_ai import batch_ledger as BL

        ledger = BL.configured_ledger()

        def _record_narration(used: int, limit: int) -> None:
            if ledger is not None:
                ledger.record_narration(
                    task_id=task.task_id, session_id=task.scope.origin_session_id,
                    identity=BL.task_identity(task), used=used, limit=limit)

        live.budget = TaskBodyBudget(
            int(task.limits.get("body_llm_dispatches", 3)), should_stop=_should_stop,
            on_consume=_record_narration if ledger is not None else None)
        ctx_token = set_current_request_context(context)
        budget_token = set_current_task_budget(live.budget)
        worker_confirmed = True
        try:
            if task.template_id == F.PACKAGE_SINGLE_TOOL_TEMPLATE_ID:
                from terminux_engine.bounded_execution.analysis_tasks.package_flow import (
                    run_package_steps,
                )

                worker_confirmed = run_package_steps(self, service, live)
            else:
                worker_confirmed = self._run_steps(service, live)
        except Exception as exc:  # noqa: BLE001 - 不吞掉、不假裝成功
            _LOG.warning("flow task worker failed: %r", exc)
            with live.lock:
                service._force_flow_terminal_locked(
                    live, "OUTCOME_UNCONFIRMED", repr(exc))
            worker_confirmed = False
        finally:
            reset_current_task_budget(budget_token)
            reset_current_request_context(ctx_token)
            if (worker_confirmed
                    and task.template_id != F.BASELINE_ONLY_TEMPLATE_ID):
                # FlowTask 只消費既有 s2/s5 的 receipt 與工程判定，形成可追溯的
                # Global 下一步；它不以完成事件自動建立或授權下一份 task。
                service.record_eap_execution_feedback(live)

            step_deps_ledger = getattr(self._deps.step_deps, "ledger", None)
            ledger_for_check = step_deps_ledger if step_deps_ledger is not None else ledger
            all_terminal, terminal_reason = _all_child_runs_terminal(ledger_for_check, live)

            if worker_confirmed:
                service._release_slot_flow(live, True)
            elif all_terminal:
                # 情況 3：
                # 雖然結果未確認，但所有 child run 均已終結（或從未啟動）。
                # 名額應釋放，避免卡死同一個 Work 的後續 attempt 或 queued task。
                # 保持 unconfirmed_work 標記以如實記錄歷史與證據。
                live.unconfirmed_work = True
                service._release_slot_flow(live, True)
                _LOG.info("flow task %s outcome unconfirmed but all child runs terminal (%s); slot released",
                          task.task_id, terminal_reason)
            else:
                # 情況 1 或 2：child 仍在執行或狀態未知，保守地不釋放名額。
                live.unconfirmed_work = True
                service.mark_worker_unconfirmed(live)
                _LOG.warning("flow task %s left the worker slot unavailable (%s)",
                             task.task_id, terminal_reason)

    def _run_steps(self, service: Any, live: Any) -> bool:
        deps = self._deps.step_deps
        task = live.task
        grant = live.grant
        worker_confirmed = True
        stop_reason: str | None = None

        for step_id in F.step_ids_for_template(live.task.template_id):
            if (step_id == F.STEP_RUN_BASELINE
                    and getattr(live, "baseline_import", None) is not None):
                # create 時已重驗 import，且 s1 只能指向前一份
                # baseline-only task 的已完成 RunRef。這裡不 claim capability，
                # 因此不會再產生 EDA launch。
                state = live.steps.get(step_id) or {}
                # functional reference 是否必須存在，由 import 時已固定的宣告
                # 決定（`baseline_functional_reference`）：宣告本方法不產生、
                # 且來源是 QUALIFICATION_ONLY fixture 時才可以沒有。
                reference_required = (
                    (getattr(live, "baseline_import", None) or {}).get(
                        "baseline_functional_reference")
                    != "NOT_PRODUCED_QUALIFICATION_ONLY")
                if (state.get("status") != "SUCCEEDED"
                        or live.runs.get(F.SLOT_BASELINE) is None
                        or (reference_required
                            and live.verification_reference is None)):
                    stop_reason = "the imported baseline live state is incomplete"
                    self._mark_step(live, step_id, "FAILED", reason=stop_reason)
                    break
                continue
            if live.stop:
                stop_reason = "cancelled"
                break
            if (_is_prepare_step(live.task.template_id, step_id)
                    or (_is_eda_step(live.task.template_id, step_id)
                        and step_id != F.STEP_RUN_BASELINE)):
                try:
                    self._validate_formal_dependency_prerequisites(live)
                except W.FlowStopped as stopped:
                    stop_reason = stopped.reason
                    self._mark_step(live, step_id, "SKIPPED", reason=stopped.reason)
                    break
            if step_id == F.STEP_COMPARE:
                try:
                    intent = self._formal_dependency_intent(live)
                except W.FlowStopped as stopped:
                    # 必要的 formal proof 相依無法建立：停在這裡，不得讓
                    # compare 照跑而把缺少的必要條件靜默略過。
                    stop_reason = stopped.reason
                    self._mark_step(live, step_id, "SKIPPED", reason=stopped.reason)
                    break
                if intent is not None:
                    # s1–s4 的結果已經落盤；這裡不終結 task，只把它停在
                    # 「等一個已授權的相依工作」。恢復時只跑 compare。
                    self._record_dependency_wait(service, live, intent)
                    return worker_confirmed
            try:
                outcome = self._run_one_step(service, live, step_id)
            except W.FlowStopped as stopped:
                stop_reason = stopped.reason
                self._mark_step(live, step_id, "SKIPPED", reason=stopped.reason)
                break
            self._apply_step_outcome(live, step_id, outcome)
            worker_confirmed = worker_confirmed and outcome.worker_confirmed
            if not outcome.can_continue:
                stop_reason = outcome.reason or outcome.error_code
                break

        with live.lock:
            for state in live.steps.values():
                if state.get("status") == "PENDING":
                    state["status"] = "SKIPPED"
                    state.setdefault("reason",
                                     "the task stopped before this step ran")
            self._finalize_locked(service, live, stop_reason)
        return worker_confirmed

    def resume_after_dependency(self, service: Any, live: Any) -> dict[str, Any]:
        """相依滿足後恢復：**只**跑 compare，然後終結。

        不重跑 s1–s4，也不重新 materialize candidate——那些步驟的結果早就
        落盤了。重覆呼叫不會產生第二次 compare：只有仍在
        `WAITING_DEPENDENCY` 的 task 會被恢復。
        """
        with live.lock:
            if live.status != F.STATUS_WAITING_DEPENDENCY:
                return {"status": "not_waiting", "task_status": live.status}
            intent = dict(getattr(live, "dependency_wait", None) or {})

        try:
            still_waiting = self._formal_dependency_intent(live)
        except W.FlowStopped as stopped:
            return {"status": "prerequisite_failed", "reason": stopped.reason}
        if still_waiting is not None:
            return {"status": "still_waiting", "dependency": still_waiting}

        with live.lock:
            # cancel 與 proof completion 的競爭以這個 task lock 線性化：若
            # cancel 已先落盤／設 stop，絕不可寫 satisfied 或啟動 s5。
            if live.status != F.STATUS_WAITING_DEPENDENCY or live.stop:
                return {"status": "cancelled", "task_status": live.status}
            W.persist_fact_event(
                self._deps.step_deps, live, live.task,
                event_type=F.EVENT_DEPENDENCY_SATISFIED,
                body={"body": dict(intent)})
            live.status = "RUNNING"
            live.dependency_wait = None

        stop_reason: str | None = None
        try:
            with live.lock:
                cancelled_before_compare = live.stop
            if cancelled_before_compare:
                raise W.FlowStopped("cancelled before the dependency compare")
            outcome = self._run_one_step(service, live, F.STEP_COMPARE)
        except W.FlowStopped as stopped:
            stop_reason = stopped.reason
            self._mark_step(live, F.STEP_COMPARE, "SKIPPED", reason=stopped.reason)
        else:
            self._apply_step_outcome(live, F.STEP_COMPARE, outcome)
            if not outcome.can_continue:
                stop_reason = outcome.reason or outcome.error_code
        with live.lock:
            for state in live.steps.values():
                if state.get("status") == "PENDING":
                    state["status"] = "SKIPPED"
                    state.setdefault("reason",
                                     "the task stopped before this step ran")
            self._finalize_locked(service, live, stop_reason)
        return {"status": "resumed", "task_status": live.status}

    def _requires_formal_proof(self, task: Any, profile: Any) -> Any | None:
        """若此 task 的已釘住 method extension 宣告需要 formal equivalence，回傳 extension；否則回傳 None。"""
        if getattr(profile, "is_qualification_fixture", False):
            return None
        from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME
        try:
            dispatch = _resolve_method_extension(task, profile)
        except ME.MethodExtensionError:
            return None
        if dispatch is None or ME.host_evidence_set(dispatch.extension) != "core_utilization":
            return None
        extension = dispatch.extension
        if "formal_equivalence" not in (extension.required_proof_kinds or ()):
            return None
        if ME.formal_proof_module(extension) is None:
            return None
        return extension

    def _validate_formal_dependency_prerequisites(self, live: Any) -> None:
        """在昂貴動作（candidate materialization／launch）前驗證所有已知相依條件。

        gate-level 等價性證明的必要條件中，有許多在執行前就已經固定（可靜態判定）：
        - Profile 宣告的 execution.docker_executable
        - formal_pair proof descriptor
        - platform 的 standard cell library
        - attempt authorization 的 writable_roots
        - Work / attempt 身分
        - baseline final netlist 身分
        - formal_proof_launches 額度

        缺少任一條件即直接拒絕（fail closed），在任何 candidate 寫入或 ORFS launch
        之前停止 task，確保 candidate materializations=0 且 ORFS launch=0。
        """
        task = live.task
        profile = self._deps.profile_for_task(task.task_id)
        extension = self._requires_formal_proof(task, profile)
        if extension is None:
            return

        from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

        FP = ME.formal_proof_module(extension)
        descriptor_key = ("formal_pair_native_proof"
                          if getattr(extension, "formal_pair_native_proof", None)
                          else "formal_pair_proof")
        plan = dict(task.execution_plan or {})
        proposal = dict(getattr(task, "eap_proposal", None) or {})

        # 1. 驗證 Profile 宣告的工具邊界，且執行模式與釘住的 proof 契約一致
        _check_formal_tool_boundary(profile, extension)

        # 2. 驗證 Work / attempt 身分
        try:
            work_id, attempt_id = _work_attempt_identity(proposal)
        except WorkIdentityConflict as exc:
            raise W.FlowStopped(
                f"the CORE task carries conflicting Work identity: {exc}") from exc
        if not work_id or not attempt_id:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the CORE task "
                "carries no Work/attempt identity")

        # 3. 驗證 proof descriptor 與 task/plan 釘選的 digest 相符
        try:
            expected_desc = FP.proof_descriptor()
            expected_digest = FP.descriptor_digest()
            if not isinstance(expected_digest, str) or not expected_digest:
                raise ValueError("empty proof descriptor digest")
        except Exception as exc:
            raise W.FlowStopped(
                f"the method requires a dependent formal proof but the proof "
                f"descriptor is invalid: {exc}")

        pinned_fp = None
        pinned_digest = None

        proposal_ext = proposal.get("method_extension") if isinstance(proposal, dict) else None
        if isinstance(proposal_ext, dict):
            pinned_desc = proposal_ext.get("descriptor")
            if isinstance(pinned_desc, dict) and descriptor_key in pinned_desc:
                pinned_fp = pinned_desc[descriptor_key]
                pinned_digest = FP.digest_json(pinned_fp) if isinstance(pinned_fp, dict) else None

        if pinned_digest is None and isinstance(plan, dict):
            plan_ext = plan.get("method_extension") or plan.get("flow_task_proposal", {}).get("method_extension")
            if isinstance(plan_ext, dict):
                pinned_desc = plan_ext.get("descriptor")
                if isinstance(pinned_desc, dict) and descriptor_key in pinned_desc:
                    pinned_fp = pinned_desc[descriptor_key]
                    pinned_digest = FP.digest_json(pinned_fp) if isinstance(pinned_fp, dict) else None

        if pinned_digest is None:
            if isinstance(proposal, dict) and proposal.get("proof_descriptor_sha256"):
                pinned_digest = str(proposal["proof_descriptor_sha256"])
            elif isinstance(plan, dict) and plan.get("proof_descriptor_sha256"):
                pinned_digest = str(plan["proof_descriptor_sha256"])
            elif isinstance(proposal, dict) and isinstance(proposal.get("package_tool"), dict):
                pt_formal = proposal["package_tool"].get("formal_pair")
                if isinstance(pt_formal, dict) and pt_formal.get("proof_descriptor_sha256"):
                    pinned_digest = str(pt_formal["proof_descriptor_sha256"])

        if not pinned_digest:
            raise W.FlowStopped(
                "the method requires a dependent formal proof but task/plan "
                "does not pin the formal proof descriptor digest")

        if pinned_digest != expected_digest:
            raise W.FlowStopped(
                f"the method requires a dependent formal proof but the pinned proof "
                f"descriptor digest ({pinned_digest}) does not match the approved "
                f"proof descriptor ({expected_digest})")

        if pinned_fp is not None and pinned_fp != expected_desc:
            raise W.FlowStopped(
                "the method requires a dependent formal proof but the pinned proof "
                "descriptor content does not match the approved proof descriptor")

        # 4. 驗證 platform cell libraries
        cell_libraries = _platform_cell_libraries(profile)
        if not cell_libraries:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the platform "
                "declares no resolvable standard cell library for it")
        for cl in cell_libraries:
            cl_path = cl.get("path")
            if not cl_path or not Path(cl_path).is_file():
                raise W.FlowStopped(
                    f"the method requires a formal equivalence proof but standard "
                    f"cell library path does not exist: {cl_path}")

        # 5. 驗證 attempt authorization 與 writable_roots
        authorization = dict(plan.get("authorization") or {})
        writable_roots = [str(item) for item in (
            authorization.get("writable_roots") or plan.get("writable_roots") or [])
            if isinstance(item, str) and item]
        if not writable_roots:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the attempt "
                "authorization declares no writable root for it")
        for wr in writable_roots:
            p = Path(wr)
            if not p.exists() and not p.parent.exists():
                raise W.FlowStopped(
                    f"the method requires a formal equivalence proof but writable root "
                    f"is unreachable: {wr}")

        # 6. 驗證 baseline netlist 身分
        with live.lock:
            reference = getattr(live, "verification_reference", None)
        baseline_netlist = _reference_role_sha256(reference, "final_netlist")
        if not baseline_netlist:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the baseline "
                "final netlist identity is unavailable")

        # 7. 驗證用途額度 (purpose quota / budget) 與 Work 帳本預留
        if "formal_proof_launches" in plan:
            if int(plan.get("formal_proof_launches", 0)) < 1:
                raise W.FlowStopped(
                    "the method requires a formal equivalence proof but the execution "
                    "plan declares 0 formal_proof_launches")

        from terminux_engine.expert_ai import batch_ledger as BL
        ledger = BL.configured_ledger()
        if ledger is None or not Path(ledger._path).is_file():
            raise W.FlowStopped(
                f"the formal Work path ({work_id}) requires a configured batch ledger, "
                f"but none is available; cannot skip quota check")
        try:
            rows = BL.read_rows(ledger._path)
        except Exception as exc:
            raise W.FlowStopped(
                f"the batch ledger for Work ({work_id}) cannot be read: {exc}") from exc

        has_work_reservations = any(
            r.get("work_id") == work_id and r.get("source") == "work_runtime"
            for r in rows
        )
        if not has_work_reservations:
            raise W.FlowStopped(
                f"the formal Work path ({work_id}) requires work_runtime reservations "
                f"in the batch ledger, but none was found")

        totals = BL.totals(rows, work_id)
        formal_reserved = totals.get("formal_proof_launches", {}).get("reserved", 0)
        formal_consumed = totals.get("formal_proof_launches", {}).get("consumed", 0)
        if (formal_reserved + formal_consumed) < 1:
            raise W.FlowStopped(
                f"the method requires a formal equivalence proof but the Work "
                f"ledger records no reserved or consumed formal_proof_launches quota for {work_id}")

    def _formal_dependency_intent(self, live: Any) -> dict[str, Any] | None:
        """compare 之前是否要等一個 method 宣告的 formal proof。

        只處理**已釘住的 method extension 宣告需要 formal equivalence** 這一種
        相依——這不是通用的 workflow DAG，也不接受任意 task 相依。沒有宣告、
        是 qualification fixture、或 envelope 已經可解析時，一律回 `None`，
        既有 template（含 setup-margin 與 legacy）行為一個位元都不變。

        但一旦 method **宣告了**必須有 formal proof，缺少 netlist 身分或
        Work／attempt 身分就不是「不需要相依」，而是「無法建立必要的相依」。
        那種情形一律 `W.FlowStopped`，不得回 `None` 讓 compare 照跑——否則
        必要條件會被靜默跳過，比較結果看起來仍然成立。
        """
        from terminux_engine.packages.orfs_run_intelligence import (
            core_proof_resolution as PR,
        )
        from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

        task = live.task
        profile = self._deps.profile_for_task(task.task_id)
        extension = self._requires_formal_proof(task, profile)
        if extension is None:
            return None
        FP = ME.formal_proof_module(extension)

        with live.lock:
            run_facts = dict(getattr(live, "run_evidence", None) or {})
            reference = getattr(live, "verification_reference", None)
        baseline_netlist = _reference_role_sha256(reference, "final_netlist")
        candidate_facts = dict(run_facts.get(F.SLOT_CANDIDATE) or {})
        by_role = _artifact_manifest_by_role(
            profile, candidate_facts.get("artifact_manifest_id"))
        candidate_netlist = str((by_role.get("orfs.final_netlist") or {}).get(
            "sha256") or "")
        if not baseline_netlist:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the baseline "
                "final netlist identity is unavailable")
        if not candidate_netlist:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but this run's "
                "artifact manifest does not attest a candidate final netlist")

        cell_libraries = _platform_cell_libraries(profile)
        if not cell_libraries:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the platform "
                "declares no resolvable standard cell library for it")

        proposal = dict(getattr(task, "eap_proposal", None) or {})
        try:
            work_id, attempt_id = _work_attempt_identity(proposal)
        except WorkIdentityConflict as exc:
            raise W.FlowStopped(
                f"the CORE task carries conflicting Work identity: {exc}") from exc
        if not work_id or not attempt_id:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the CORE task "
                "carries no Work/attempt identity")

        writer = getattr(self._deps.step_deps, "writer", None)
        store = getattr(writer, "event_store", None)
        if store is not None:
            context = self._deps.step_deps.request_context()
            try:
                PR.resolve_pair_proof(
                    PR.step_result_payloads(store, member_id=context.member_id,
                                            event_type=F.EVENT_STEP_RESULT),
                    work_id=work_id, attempt_id=attempt_id,
                    descriptor_sha256=FP.descriptor_digest(),
                    baseline_netlist_sha256=baseline_netlist,
                    candidate_netlist_sha256=candidate_netlist)
                return None  # 相依已滿足：直接跑 compare
            except PR.PairProofResolutionError:
                pass

        from terminux_engine.packages.orfs_run_intelligence import (
            core_utilization_evidence as CU,
        )

        baseline_artifact = ((_production_reference(reference) or {}).get(
            "artifacts") or {}).get("final_netlist") or {}
        candidate_artifact = by_role.get("orfs.final_netlist") or {}
        plan = dict(task.execution_plan or {})
        authorization = dict(plan.get("authorization") or {})
        # proof task 會在同一組已授權的 writable root 底下建立它自己的 task
        # root；沒有帶過去的話 draft 會被 "task root is outside Profile" 擋下。
        writable_roots = [str(item) for item in (
            authorization.get("writable_roots") or plan.get("writable_roots") or [])
            if isinstance(item, str) and item]
        if not writable_roots:
            raise W.FlowStopped(
                "the method requires a formal equivalence proof but the attempt "
                "authorization declares no writable root for it")
        authorization["writable_roots"] = writable_roots
        grant = getattr(live, "grant", None)
        return {
            "schema_version": "flow_task.formal_dependency_intent.v1",
            "kind": F.DEPENDENCY_FORMAL_PAIR_PROOF,
            # 誰在等
            "work_id": work_id, "attempt_id": attempt_id,
            "core_task_id": task.task_id, "core_task_digest": task.task_digest,
            # 等什麼（方法與工具身分，授權前就固定）
            "method_identity": extension.method_identity,
            "method_extension": extension.pinned(),
            "proof_descriptor_sha256": FP.descriptor_digest(),
            # 證哪一對（角色 ＋ hash ＋ 來源身分）
            "baseline": {"role": CU.PROOF_ROLE_BASELINE,
                         "sha256": baseline_netlist,
                         "path": baseline_artifact.get("reference_path")
                         or baseline_artifact.get("resolved_path")},
            "candidate": {"role": CU.PROOF_ROLE_CANDIDATE,
                          "sha256": candidate_netlist,
                          "path": candidate_artifact.get("path")},
            "source_identity": {
                "candidate_run_id": candidate_facts.get("run_id"),
                "candidate_manifest_id": candidate_facts.get(
                    "artifact_manifest_id"),
                "candidate_manifest_digest": candidate_facts.get(
                    "manifest_digest"),
                "candidate_receipt_digest": candidate_facts.get("receipt_digest"),
                "baseline_reference_digest": (
                    _production_reference(reference) or {}).get(
                        "reference_digest"),
            },
            "input_manifest": {
                "manifest_sha256": task.baseline_input_manifest_sha256,
                "tree_hash": task.baseline_input_tree_hash},
            # 元件庫內容與版本隨 intent 一起釘住，最後進 proof_plan_digest。
            "cell_libraries": cell_libraries,
            # 相依工作的輸入身分沿用本 task 已釘住的那一份；proof proposal 的
            # `input` 由它組出，不另外挑一份 snapshot。
            "input": {
                "snapshot_root": str(profile.input_snapshot_root),
                "manifest_path": str(profile.input_manifest_path),
                "manifest_digest": task.baseline_input_manifest_sha256,
                "tree_hash": task.baseline_input_tree_hash},
            "authorization": {
                "authorization_id": authorization.get("authorization_id"),
                "grant_id": getattr(grant, "grant_id", None),
                # 相依工作在**同一組**已授權 writable root 下建立自己的 task
                # root；這是把既有授權帶過去，不是新授權。
                "writable_roots": writable_roots},
            "resume_step_id": F.STEP_COMPARE,
        }

    def _record_dependency_wait(self, service: Any, live: Any,
                                intent: dict[str, Any]) -> None:
        """先落盤 wait intent，再把 task 停在非終結的等待狀態。

        順序重要：intent 必須先持久化，之後才會有人依它建立 proof task。
        重啟時由這筆事件重建「在等什麼」，不靠記憶體。
        """
        # intent 自己帶 `schema_version`，與事件外層的保留欄位同名；整份攤平
        # 會被 `append()` 以 "event body may not override reserved fields" 拒絕。
        # 巢狀放在 `body` 之下——`waiting_dependencies()` 本來就是這樣讀的。
        W.persist_fact_event(
            self._deps.step_deps, live, live.task,
            event_type=F.EVENT_DEPENDENCY_WAIT, body={"body": dict(intent)})
        with live.lock:
            live.status = F.STATUS_WAITING_DEPENDENCY
            live.terminal_reason = None
            live.current_operation = F.OPERATION_IDLE
            live.dependency_wait = dict(intent)

    def _finalize_locked(self, service: Any, live: Any, stop_reason: str | None) -> None:
        statuses = [s.get("status") for s in live.steps.values()]
        if any(s == "OUTCOME_UNCONFIRMED" for s in statuses):
            status, reason = "OUTCOME_UNCONFIRMED", "a step outcome is unconfirmed"
        elif live.stop:
            status, reason = "CANCELLED", stop_reason or "cancelled"
        elif all(s == "SUCCEEDED" for s in statuses):
            status, reason = "COMPLETED", None
        elif any(s in ("FAILED", "DENIED") for s in statuses):
            status, reason = "FAILED", stop_reason
        else:
            status, reason = "BLOCKED", stop_reason
        service._force_flow_terminal_locked(live, status, reason)

    @staticmethod
    def _mark_step(live: Any, step_id: str, status: str, **fields: Any) -> None:
        with live.lock:
            state = live.steps.setdefault(step_id, {"step_id": step_id})
            state["status"] = status
            state.update({k: v for k, v in fields.items() if v is not None})

    def _apply_step_outcome(self, live: Any, step_id: str, outcome: Any) -> None:
        self._mark_step(
            live, step_id, outcome.step_status,
            reason=outcome.reason, error_code=outcome.error_code,
            business_class=outcome.business_class,
            result_event_id=outcome.result_event_id,
            result_sha256=outcome.result_sha256,
            persistence_status=outcome.persistence_status,
            **outcome.details)


__all__ = [
    "CAP_COMPARE_RUNS", "CAP_EXPLAIN_RUN", "CAP_PREPARE_VARIANT", "CAP_RUN_FLOW",
    "STEP_CAPABILITIES", "FlowController", "FlowControllerDeps", "FlowDraft",
    "FlowDraftError",
]


# ── 每一步的實際執行（附加在 FlowController 上）─────────────────────


def _resolved_arguments(controller: "FlowController", live: Any, step_id: str) -> dict[str, Any]:
    """執行當下具體化本步參數。

    `s2`／`s5` 的 run id 來自**已驗證的 Kernel reference**；沒有已驗證的
    reference 就直接停，不用「最新 DQE」或其他 run 替代。
    """
    task = live.task
    capability = _capabilities_for(task.template_id).get(step_id)
    if capability == CAP_RUN_FLOW:
        return {"task_id": task.task_id, "slot": _slot_for_step(task, step_id)}
    if capability == CAP_PREPARE_REPAIR:
        return {"task_id": task.task_id, "slot": F.SLOT_CANDIDATE}
    if capability == CAP_PREPARE_VARIANT:
        # v1 的 prepare 不帶 slot（既有 arguments hash 不變）。
        if not task.is_bounded_search:
            return {"task_id": task.task_id}
        return {"task_id": task.task_id,
                "slot": F.candidate_slot_for_step(step_id)}
    if capability == CAP_EXPLAIN_RUN:
        baseline = live.runs.get(F.SLOT_BASELINE)
        if baseline is None:
            raise W.FlowStopped("no verified baseline run reference is available")
        return {"run_id": baseline.run_id, "question": task.question}
    if capability == CAP_COMPARE_RUNS:
        baseline = live.runs.get(F.SLOT_BASELINE)
        slot = _slot_for_step(task, step_id)
        candidate = live.runs.get(slot)
        if baseline is None or candidate is None:
            raise W.FlowStopped("both verified run references are required")
        return {
            "baseline_run_id": baseline.run_id,
            "candidate_run_id": candidate.run_id,
            "question": task.question,
            # 固定指明 `design_config` 這一個維度，**編碼沿既有 manifest
            # schema**——既有 action 收的是維度名稱字串陣列，dict 會被靜默
            # 丟棄。它只標註已驗證的允許差異，不把一次比較升為因果證明。
            **_controlled_change_args(controller, task),
        }
    raise W.FlowStopped(f"unknown step: {step_id}")


def _controlled_change_args(controller: "FlowController",
                            task: Any) -> dict[str, Any]:
    """host 決定哪些差異是「已宣告且已驗證」的。**client／模型不參與。**

    v0.8 的候選多一份已核准 RTL patch，因此 `rtl_content` 也要被標成受控——
    否則它是 hard mismatch，整個比較會變成 not_comparable。標籤放寬的同時
    兩側 hash 照實保留（`input_relation=VERIFIED_DECLARED_CHANGE`）。
    """
    try:
        profile = controller._deps.profile_for_task(task.task_id)
    except Exception:  # noqa: BLE001 - 讀不到就用預設集合，不放寬
        profile = None
    recipe = getattr(profile, "repair_recipe", None)
    if (task.template_id in F.PREFIX_BORROW_TEMPLATES
            and getattr(recipe, "rtl_patch", None) is not None):
        return {"controlled_changes": ["design_config", "rtl_content"],
                "comparison_policy_id": recipe.comparison_policy_id}
    return {"controlled_changes": ["design_config"]}


def _variant_ref_for(live: Any, slot: str) -> Any:
    """取這個 slot 已建立的候選 input 身分。"""
    refs = getattr(live, "variant_refs", None)
    if isinstance(refs, dict) and slot in refs:
        return refs[slot]
    if slot == F.SLOT_CANDIDATE:
        return live.variant_ref
    return None


def _build_binding(controller: "FlowController", live: Any, step_id: str) -> Any:
    """為需要 host binding 的步驟組出 `TaskExecutionBinding`。"""
    task = live.task
    profile = controller._deps.profile_for_task(live.task.task_id)
    capability = _capabilities_for(task.template_id).get(step_id)
    slot = _slot_for_step(task, step_id)
    if capability == CAP_RUN_FLOW and slot == F.SLOT_BASELINE:
        input_root = profile.input_snapshot_root
        manifest_path = profile.input_manifest_path
        manifest_sha = profile.input_manifest_sha256
        tree_hash = task.baseline_input_tree_hash
    elif capability == CAP_RUN_FLOW:
        ref = _variant_ref_for(live, slot)
        if ref is None:
            raise W.FlowStopped(
                f"the input snapshot for {slot} has not been created")
        input_root = ref.candidate_root
        manifest_path = ref.manifest_path
        manifest_sha = ref.manifest_sha256
        tree_hash = ref.tree_hash
    elif capability in (CAP_PREPARE_VARIANT, CAP_PREPARE_REPAIR):
        # prepare 從**baseline 副本**出發。
        input_root = profile.input_snapshot_root
        manifest_path = profile.input_manifest_path
        manifest_sha = profile.input_manifest_sha256
        tree_hash = task.baseline_input_tree_hash
    else:
        return None

    grant = live.grant
    context = controller._deps.step_deps.request_context()
    reference_dir = reference_sha = None
    if (task.template_id in F.SETUP_REPAIR_TEMPLATES
            and capability == CAP_RUN_FLOW and slot == F.SLOT_CANDIDATE):
        reference = getattr(live, "verification_reference", None)
        if not reference:
            raise W.FlowStopped(
                "the baseline functional reference has not been produced")
        # 派送前重驗 bytes/hash——不是只信 s1 當時記下的值。
        import hashlib

        path = Path(reference["reference_path"])
        if not path.is_file():
            raise W.FlowStopped("the baseline functional reference is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
            raise W.FlowStopped(
                "the baseline functional reference no longer matches its hash")
        reference_dir = reference["reference_dir"]
        reference_sha = reference["sha256"]
    return F.TaskExecutionBinding(
        task_id=task.task_id, task_digest=task.task_digest,
        grant_id=grant.grant_id, slot=slot,
        member_id=task.scope.member_id, project_id=task.scope.project_id,
        session_id=context.session_id, profile_id=profile.profile_id,
        profile_digest=profile.profile_digest, input_root=str(input_root),
        input_manifest_path=str(manifest_path), input_manifest_sha256=manifest_sha,
        input_tree_hash=tree_hash,
        output_root=str(profile.slot_output_root(task.task_id, slot)),
        docker_image_id=profile.docker_image_id, design_id=profile.design_id,
        platform_id=profile.platform_id,
        variant=f"terminux_{task.task_id[-8:]}_{slot}",
        design_config_relpath=str(
            Path(profile.design_config).relative_to("flow")
            if str(profile.design_config).startswith("flow/")
            else profile.design_config),
        run_timeout_sec=task.run_timeout_sec, step_id=step_id,
        verification_reference_dir=reference_dir,
        verification_reference_sha256=reference_sha,
        # user 宣告的執行方式跟著 grant 綁定的 binding 走。
        # method extension 與 fixture corpus 身分由 host 由已核准 task／Profile
        # 組出並先驗過 digest；caller 無法注入，Kernel 只原樣傳遞。
        execution=_binding_execution(profile, task),
        dqe_db_path=profile.dqe_db_path,
        artifact_root=profile.artifact_root,
    )


def _binding_execution(profile: Any, task: Any) -> dict[str, Any] | None:
    """binding 帶的執行宣告：Profile 的 execution ＋ host 驗過的 fixture／method 身分。

    - `fixture_corpus`：Profile 宣告的 corpus adapter schema 與 manifest digest，
      **在派送前**重新核對 manifest bytes；不符即 `FlowStopped`，不啟動 child。
    - `method_extension`：本 task snapshot 固定的 pin（identity／version／
      descriptor digest）。executable 依它選 corpus，不依 design 或候選值。
    """
    execution = (profile.execution.to_json_dict()
                 if getattr(profile, "execution", None) is not None else None)
    pinned_roles = (execution or {}).get("artifact_role_mapping")
    if pinned_roles is not None:
        # Profile 釘住的 artifact role allowlist：派送前就核對，不符不啟動 child。
        from terminux_engine.packages.orfs_run_intelligence import (
            baseline_reference as BR,
        )

        try:
            BR.resolve_role_mapping(pinned_roles)
        except BR.BaselineReferenceError as exc:
            raise W.FlowStopped(
                f"the pinned artifact role mapping is not usable "
                f"({exc.code}): {exc}") from None
    fixture = (execution or {}).get("fixture_corpus")
    if not fixture:
        return execution
    from terminux_engine.packages.orfs_run_intelligence import corpus_replay as CR
    from terminux_engine.packages.orfs_run_intelligence import method_extensions as ME

    proposal = getattr(task, "eap_proposal", None)
    pinned = proposal.get("method_extension") if isinstance(proposal, dict) else None
    try:
        if pinned is not None:
            extension = ME.resolve_pinned(pinned)
            descriptor_digest = pinned["descriptor_sha256"]
        else:
            # proposal 尚未存在的 baseline 收集：身分只能來自已核准 Profile 的
            # 宣告，不從 design、參數名或候選值推論。
            extension = ME.resolve_declared_identity(
                getattr(profile, "declared_method_identity", None))
            descriptor_digest = ME.descriptor_sha256(extension.descriptor())
    except ME.MethodExtensionError as exc:
        raise W.FlowStopped(
            f"fixture corpus replay requires a resolvable method identity: {exc.reason}")
    try:
        digest = CR.manifest_sha256(fixture["manifest_path"])
    except OSError as exc:
        raise W.FlowStopped(f"the fixture corpus manifest is unreadable: {exc}")
    if digest != fixture["manifest_sha256"]:
        raise W.FlowStopped(
            "the fixture corpus manifest no longer matches its approved digest")
    if fixture["adapter_schema"] not in CR.SUPPORTED_SCHEMAS:
        raise W.FlowStopped("the fixture corpus adapter schema is unsupported")
    return {**execution,
            "method_extension": {"method_identity": extension.method_identity,
                                 "extension_version": extension.extension_version,
                                 "descriptor_sha256": descriptor_digest}}


def _lineage_identity(live: Any, binding: Any, run_id: str) -> Any:
    from terminux_engine.lineage.flow_lineage import FlowRunIdentity

    task = live.task
    grant = live.grant
    return FlowRunIdentity(
        task_id=task.task_id, task_digest=task.task_digest,
        grant_id=grant.grant_id if grant is not None else "",
        slot=binding.slot, run_id=run_id, profile_id=binding.profile_id,
        profile_digest=binding.profile_digest, design_id=binding.design_id,
        platform_id=binding.platform_id, docker_image_id=binding.docker_image_id,
        native_tool_binding_sha256=getattr(binding, "native_tool_binding_sha256", None),
        member_id=task.scope.member_id, project_id=task.scope.project_id,
        command="host-built orfs run (see adapter_metadata in receipt)")


def _record_lineage_intent(self: "FlowController", live: Any, binding: Any) -> None:
    """EDA launch **之前**把 planned／approved 記進既有 DLE。
    寫不進去就在 side effect 前停止。`lineage_ledger_path` 未注入時跳過（測試）。"""
    from terminux_engine.lineage import flow_lineage as FL

    ledger = self._deps.step_deps.lineage_ledger_path
    if ledger is None:
        return
    profile = self._deps.profile_for_task(live.task.task_id)
    try:
        commit = FL.build_intent_commit(
            _lineage_identity(live, binding, run_id=""), profile,
            input_root=binding.input_root, profile_source=live.task.profile_source,
            parameter_value=live.task.value_for_slot(binding.slot))
        FL.append_flow_commit(ledger, commit)
    except Exception as exc:  # noqa: BLE001 - 任何失敗都是「launch 前停止」
        raise W.FlowStopped(
            f"lineage intent could not be recorded before launch; no EDA was "
            f"started: {exc!r}")
    reference = FL.commit_reference(commit)
    with live.lock:
        live.lineage.setdefault(binding.slot, {})["intent"] = reference
    W.persist_fact_event(
        self._deps.step_deps, live, live.task, event_type=F.EVENT_LINEAGE,
        body={"slot": binding.slot, "layer": "approved", "commit": reference,
              "ledger_path": str(ledger)})


def _record_lineage_commit(self: "FlowController", live: Any, binding: Any, *,
                           run_id: str, effective_values: dict[str, str] | None,
                           consumption: dict[str, Any] | None, ready: bool,
                           started_at: float) -> None:
    """run 完成後把 executed 層記進既有 DLE。launch 後失敗只標記，不刪、不重跑。"""
    from terminux_engine.lineage import flow_lineage as FL

    ledger = self._deps.step_deps.lineage_ledger_path
    if ledger is None:
        return
    profile = self._deps.profile_for_task(live.task.task_id)
    intents = FL.find_flow_commits(ledger, task_id=live.task.task_id,
                                   slot=binding.slot, status=FL.STATUS_INTENT)
    reference: dict[str, Any]
    try:
        if not intents:
            raise FL.FlowLineageError("no lineage intent was recorded for this slot")
        output_root = Path(binding.output_root)
        results = (output_root / "results" / binding.platform_id
                   / binding.design_id / binding.variant)
        reports = (output_root / "reports" / binding.platform_id
                   / binding.design_id / binding.variant)
        outputs = sorted(results.glob("6_final*")) + sorted(reports.glob("terminux_*.json"))
        commit = FL.build_committed_commit(
            intents[-1], _lineage_identity(live, binding, run_id=run_id), profile,
            input_root=binding.input_root, profile_source=live.task.profile_source,
            effective_values=effective_values, output_paths=outputs,
            parameter_value=live.task.value_for_slot(binding.slot),
            consumption=consumption, started_at=started_at,
            status=FL.STATUS_COMMITTED if ready else FL.STATUS_FAILED)
        FL.append_flow_commit(ledger, commit)
        reference = FL.commit_reference(commit)
        state = "recorded"
    except Exception as exc:  # noqa: BLE001 - 保留 output 與 receipt，標記不可確認
        _LOG.warning("lineage commit not recorded for %s/%s: %r",
                     live.task.task_id, binding.slot, exc)
        reference = {"status": "unrecorded", "reason": repr(exc)}
        state = "unrecorded"
    with live.lock:
        live.lineage.setdefault(binding.slot, {})["executed"] = reference
        live.lineage[binding.slot]["state"] = state
    W.persist_fact_event(
        self._deps.step_deps, live, live.task, event_type=F.EVENT_LINEAGE,
        body={"slot": binding.slot, "layer": "executed", "commit": reference,
              "state": state, "ledger_path": str(ledger)})


def _run_one_step(self: "FlowController", service: Any, live: Any, step_id: str) -> W.FlowStepOutcome:
    deps = self._deps.step_deps
    task = live.task
    grant = live.grant
    capability_id = _capabilities_for(task.template_id)[step_id]
    is_eda = _is_eda_step(task.template_id, step_id)

    # ── 昂貴動作前前置防護：驗證已知相依條件 ──
    if (_is_prepare_step(task.template_id, step_id)
            or (is_eda and step_id != F.STEP_RUN_BASELINE)):
        self._validate_formal_dependency_prerequisites(live)

    # ── 慢速工作在鎖外：完整逐檔 input 驗證 ──
    binding = _build_binding(self, live, step_id)
    if is_eda:
        with live.lock:
            live.current_operation = F.OPERATION_VERIFYING_INPUTS
        verify = deps.verify_inputs(
            manifest_path=binding.input_manifest_path,
            expected_sha256=binding.input_manifest_sha256,
            root=binding.input_root)
        if not verify.ok:
            raise W.FlowStopped(f"input inventory drift before dispatch: {verify.summary()}")
        # 額度：本 task 的 EDA launch 上限。
        with live.lock:
            limit = int(task.limits.get("eda_launches", 2))
            if live.eda_launches_used >= limit:
                raise W.FlowStopped("the task EDA launch budget is exhausted")
    if _is_prepare_step(task.template_id, step_id):
        with live.lock:
            limit = int(task.limits.get("candidate_writes", 1))
            if live.candidate_writes_used >= limit:
                raise W.FlowStopped("the candidate materialization budget is exhausted")

    # ── 短臨界區：重驗 → intent 落盤 → claim ──
    with live.lock:
        arguments = _resolved_arguments(self, live, step_id)
        snapshot_id, capability, arguments_sha = W.revalidate_step_locked(
            deps, live, task, grant, step_id,
            resolved_arguments=arguments, capability_id=capability_id)
        if is_eda:
            live.eda_launches_used += 1
        if _is_prepare_step(task.template_id, step_id):
            live.candidate_writes_used += 1
        proposal_id, proposal_digest, claimed = W.claim_step_locked(
            deps, live, task, grant, step_id=step_id, capability_id=capability_id,
            resolved_arguments=arguments, snapshot_id=snapshot_id,
            capability=capability, arguments_sha=arguments_sha)
        live.steps[step_id]["status"] = "CLAIMED"
        live.current_operation = (
            F.OPERATION_WAITING_EXECUTION if is_eda
            else F.OPERATION_PREPARING_VARIANT
            if _is_prepare_step(task.template_id, step_id)
            else F.OPERATION_ANALYZING)

    # ── EDA／provider 平行度（鎖外 acquire；步驟結束後 release）──
    slots = None
    if is_eda:
        slots = deps.eda_slots
    elif capability_id in (CAP_EXPLAIN_RUN, CAP_COMPARE_RUNS):
        slots = deps.provider_slots
    if slots is not None:
        with live.lock:
            live.current_operation = F.OPERATION_WAITING_SLOT
        slots.acquire()
    try:
        if is_eda:
            # launch 前先寫 lineage intent；失敗即停止，沒有 side effect。
            _record_lineage_intent(self, live, binding)
        return _dispatch_and_finish(
            self, service, live, step_id, capability_id, arguments_sha,
            proposal_id, proposal_digest, claimed, binding, is_eda)
    finally:
        if slots is not None:
            slots.release()


def _source_dqe_db_path(self: "FlowController", live: Any) -> str | None:
    """取得 Profile 釘選的來源 DQE DB 路徑。沒有就回 None。"""
    profile = self._deps.step_deps.profile_for_task(live.task.task_id)
    execution = getattr(profile, "execution", None) if profile is not None else None
    source_decl = (getattr(execution, "durable_baseline_source", None)
                   if execution is not None else None)
    if source_decl is None and isinstance(execution, dict):
        source_decl = execution.get("durable_baseline_source")
    if hasattr(source_decl, "to_json_dict"):
        source_decl = source_decl.to_json_dict()
    if isinstance(source_decl, dict):
        src_dqe = source_decl.get("dqe_db_path")
        if isinstance(src_dqe, str) and src_dqe:
            return src_dqe
    return None


def _imported_run_dqe_overrides(
    self: "FlowController", live: Any,
) -> dict[str, str] | None:
    """由 live state 的 imported baseline 建出 per-run DQE DB 覆寫。

    imported baseline run 的 DQE 仍屬來源 DB（Profile 釘選的
    ``durable_baseline_source.dqe_db_path``）。mapping 是
    ``{run_id: source_dqe_db_path}``，只有在 import 存在時才非空。
    """
    baseline_import = getattr(live, "baseline_import", None)
    if baseline_import is None:
        return None
    baseline_ref = live.runs.get(F.SLOT_BASELINE)
    if baseline_ref is None:
        return None
    src_dqe = _source_dqe_db_path(self, live)
    if not src_dqe:
        return None
    return {baseline_ref.run_id: src_dqe}


def _dispatch_and_finish(self: "FlowController", service: Any, live: Any,
                         step_id: str, capability_id: str, arguments_sha: str,
                         proposal_id: str, proposal_digest: str, claimed: Any,
                         binding: Any, is_eda: bool) -> W.FlowStepOutcome:
    deps = self._deps.step_deps
    task = live.task

    # ── per-run DQE 覆寫：imported baseline run → 來源 DB ──
    overrides = _imported_run_dqe_overrides(self, live)

    # ── 鎖外派送 ──
    def _on_run_created(run_ref: Any) -> None:
        """child 啟動**前**先持久化 RunRef。

        寫入失敗就拋出——`start_task_run()` 會因此不啟動 child，並留下可查的
        未啟動記錄。
        """
        event_id = W.persist_fact_event(
            deps, live, task, event_type=F.EVENT_RUN_LINK,
            body={"step_id": step_id, "slot": run_ref.slot,
                  "run": run_ref.to_json_dict()})
        if event_id is None:
            raise RuntimeError("the run link could not be persisted")
        with live.lock:
            live.runs[run_ref.slot] = run_ref

    task_body_llm = task.limits.get("body_llm_dispatches") if hasattr(task, "limits") else None
    allow_llm = False if task_body_llm == 0 else None

    # compare 是唯一需要 formal proof 事實的步驟；其餘步驟不注入。
    formal_proof = (_formal_proof_evidence_fact(self, live)
                    if step_id == F.STEP_COMPARE else None)

    dispatch = W.dispatch_step(
        deps, proposal_id=proposal_id, proposal_digest=proposal_digest,
        claimed=claimed, binding=binding, task_id=task.task_id,
        on_run_created=_on_run_created if is_eda else None,
        imported_run_dqe_overrides=overrides,
        allow_llm=allow_llm, formal_proof=formal_proof)

    if is_eda:
        return _finish_eda_step(self, service, live, step_id, capability_id,
                                arguments_sha, proposal_id, dispatch, binding)
    return _finish_action_step(self, live, step_id, capability_id, arguments_sha,
                               proposal_id, dispatch)


FlowController._run_one_step = _run_one_step


def _finish_eda_step(
    self: "FlowController", service: Any, live: Any, step_id: str,
    capability_id: str, arguments_sha: str, proposal_id: str, dispatch: Any,
    binding: Any,
) -> W.FlowStepOutcome:
    """EDA step 的收尾。

    **launch accepted ≠ step SUCCEEDED**：這裡等真實的完成證據，才寫
    可繼續的 receipt。result envelope 同時保存 `launch_result` 與
    `completed_run_evidence`，receipt 引用這份 envelope；不把 launch result
    改寫成「工具已完成」，也不第二次 finalize。
    """
    deps = self._deps.step_deps
    task = live.task
    grant = live.grant
    slot = binding.slot
    full = dispatch.full_result if isinstance(dispatch.full_result, dict) else {}
    launch_ok = bool(full.get("launch_accepted")) and dispatch.succeeded

    if not launch_ok:
        outcome = {"execution_status": "failed", "timed_out": dispatch.timed_out,
                   "worker_completion": "confirmed" if not dispatch.timed_out else "unknown",
                   "launch_accepted": False}
        event_id, sha, persistence = W.persist_step_result(
            deps, live, task, grant, step_id=step_id, capability_id=capability_id,
            arguments_sha=arguments_sha, proposal_id=proposal_id,
            execution_status="failed", action_status=full.get("status"),
            business_class=None,
            full_result={"launch_result": full, "completed_run_evidence": None})
        W.persist_step_receipt(
            deps, live, task, step_id=step_id, step_status="FAILED",
            outcome=outcome, persistence_status=persistence,
            result_event_id=event_id, result_sha256=sha, can_continue=False,
            error_code=full.get("error_code"))
        return W.FlowStepOutcome(
            step_status="FAILED", can_continue=False,
            error_code=full.get("error_code") or "FLOW_LAUNCH_NOT_ACCEPTED",
            reason=full.get("message"), result_event_id=event_id,
            result_sha256=sha, persistence_status=persistence,
            worker_confirmed=not dispatch.timed_out)

    # RunRef 由 `on_run_created` 在 child 啟動前寫入 live state。
    run_ref = live.runs.get(slot)
    if run_ref is None:
        raise W.FlowStopped("the run reference was not persisted before launch")

    with live.lock:
        live.current_operation = F.OPERATION_WAITING_EVIDENCE
    profile = self._deps.profile_for_task(live.task.task_id)
    # 每個候選 slot 有自己的固定值（`value_for_slot` 對 v1 回原本的
    # `candidate_value`，行為不變）。
    expected_value = task.value_for_slot(slot)

    # 有效設定與參數消費證據都由本 slot 專屬輸出區取得（因此天然綁定同一
    # run／attempt），並且**交給判定器一起判**——`ready` 不能只憑
    # ledger／receipt／DQE 成立。
    def _settings_and_consumption() -> tuple[Any, Any]:
        return (
            deps.read_effective_settings(
                binding.output_root, platform=binding.platform_id,
                design=binding.design_id, variant=binding.variant),
            deps.consumption_evidence(
                binding.output_root, platform=binding.platform_id,
                design=binding.design_id, variant=binding.variant,
                expected_value=expected_value,
                parameter_name=task.parameter_name),
        )

    import time as _time
    run_started_at = _time.time()
    evidence = W.wait_for_run_evidence(
        deps, live, run_id=run_ref.run_id, slot=slot, task_id=task.task_id,
        dqe_db_path=profile.dqe_db_path,
        deadline_monotonic=grant.deadline_at_monotonic,
        parameter_name=task.parameter_name,
        expected_parameter_value=expected_value,
        settings_provider=_settings_and_consumption)

    settings, consumption = _settings_and_consumption()
    effective_value = settings.get(task.parameter_name)
    # RC-4：實際 EDA binary 身分與有效設定同時收進 durable step result。
    from terminux_engine.packages.orfs_run_intelligence import flow_evidence as _FE
    tool_binaries = _FE.read_tool_binaries(binding.output_root)
    ready = evidence.state == RE.READY
    evidence_reason = evidence.reason
    # executed 層進既有 DLE。失敗只標記，不影響 receipt。
    _record_lineage_commit(
        self, live, binding, run_id=run_ref.run_id,
        effective_values=dict(settings.values), consumption=consumption.to_json_dict(),
        ready=ready, started_at=run_started_at)

    step_status = ("SUCCEEDED" if ready
                   else "OUTCOME_UNCONFIRMED" if evidence.state == RE.UNCONFIRMED
                   else "FAILED")
    outcome = {
        "execution_status": "succeeded" if ready else (
            "unconfirmed" if evidence.state == RE.UNCONFIRMED else "failed"),
        "timed_out": bool(evidence.timed_out),
        "worker_completion": ("confirmed" if evidence.state in (RE.READY, RE.FAILED)
                              else "unknown"),
        "launch_accepted": True,
        "run_id": run_ref.run_id, "slot": slot,
        "evidence_state": evidence.state, "evidence_reason": evidence_reason,
    }
    event_id, sha, persistence = W.persist_step_result(
        deps, live, task, grant, step_id=step_id, capability_id=capability_id,
        arguments_sha=arguments_sha, proposal_id=proposal_id,
        execution_status=outcome["execution_status"],
        action_status=full.get("status"),
        business_class="valid" if ready else None,
        full_result={
            "launch_result": full,
            "completed_run_evidence": evidence.to_json_dict(),
            "effective_settings": settings.to_json_dict(),
            "parameter_consumption": consumption.to_json_dict(),
            "expected_parameter_value": expected_value,
            "input_manifest_sha256": binding.input_manifest_sha256,
            "input_tree_hash": binding.input_tree_hash,
            "output_root": binding.output_root,
            "docker_image_id": binding.docker_image_id,
            **({"native_tool_binding_sha256": native_binding}
               if (native_binding := getattr(
                   binding, "native_tool_binding_sha256", None)) else {}),
            "tool_binaries": tool_binaries,
        })
    W.persist_step_receipt(
        deps, live, task, step_id=step_id, step_status=step_status,
        outcome=outcome, persistence_status=persistence,
        result_event_id=event_id, result_sha256=sha,
        can_continue=ready and persistence == "durable",
        error_code=None if ready else evidence_reason)
    # 本 slot 這一次 run 的執行身分。judgement 時由它補上 receipt／manifest／
    # DQE／result event，證據檔本身不帶（也不得帶）這些欄位。
    with live.lock:
        if not hasattr(live, "run_evidence") or live.run_evidence is None:
            live.run_evidence = {}
        live.run_evidence[slot] = RE_HOST.host_run_facts(
            evidence.to_json_dict(), task_id=task.task_id, slot=slot,
            result_event_id=event_id)

    # ── baseline 完成後，host 從它的產物派生 candidate checker 的
    # gold reference。路徑與 hash 都由 host 決定並在 s4 派送前重驗。 ──
    profile_uses_setup_margin = (
        getattr(getattr(getattr(profile, "repair_recipe", None), "method", None),
                "variable", None) == "SETUP_SLACK_MARGIN")
    produces_reference = _method_produces_baseline_reference(profile)
    if (ready and slot == F.SLOT_BASELINE
            and (task.template_id in F.SETUP_REPAIR_TEMPLATES
                 or (task.template_id == F.BASELINE_ONLY_TEMPLATE_ID
                     and (profile_uses_setup_margin or produces_reference)))):
        try:
            reference = (_derive_verification_reference(self, live, binding)
                         if (task.template_id in F.SETUP_REPAIR_TEMPLATES
                             or profile_uses_setup_margin)
                         else None)
        except Exception as exc:  # noqa: BLE001 - 缺就是缺，不補造
            _LOG.warning("verification reference not derived: %r", exc)
            reference = None
        if produces_reference:
            # Package 擁有的 method-neutral production reference。缺任何一項
            # 宣告要求的輸入就不產生——不補值，也不從別的 run 搬。
            try:
                reference = _build_production_reference(
                    self, live, binding, evidence=evidence, result_event_id=event_id,
                    result_sha256=sha, existing=reference)
            except Exception as exc:  # noqa: BLE001 - 如實記錄，不補造
                _record_reference_stop(self, live, binding, exc)
        if reference is not None:
            with live.lock:
                live.verification_reference = reference
            W.persist_fact_event(
                deps, live, task, event_type=F.EVENT_INPUT_REFERENCE,
                body={"slot": F.SLOT_BASELINE, "step_id": step_id,
                      "verification_reference": reference})
        if task_uses_exact_geometry(task):
            # v5／v0.8：candidate 的 floorplan 邊界必須來自本 task 的 s1 量測。
            try:
                layout = _derive_layout_binding(self, live, binding)
            except Exception as exc:  # noqa: BLE001 - 缺就是缺，不補造
                _LOG.warning("layout binding not derived: %r", exc)
                layout = None
            if layout is not None:
                with live.lock:
                    live.layout_binding = layout
                W.persist_fact_event(
                    deps, live, task, event_type=F.EVENT_INPUT_REFERENCE,
                    body={"slot": F.SLOT_BASELINE, "step_id": step_id,
                          "layout_binding": layout.to_json_dict()})

    return W.FlowStepOutcome(
        step_status=step_status, can_continue=ready and persistence == "durable",
        error_code=None if ready else evidence.reason, reason=evidence_reason,
        result_event_id=event_id, result_sha256=sha,
        business_class="valid" if ready else None,
        worker_confirmed=evidence.state in (RE.READY, RE.FAILED),
        persistence_status=persistence,
        details={"run_id": run_ref.run_id, "slot": slot,
                 "effective_value": effective_value,
                 # selector 用的 deterministic 事實，來源是本 slot 自己的
                 # floorplan 消費證據（不是顯示字串）。
                 "core_area_um2": (consumption.to_json_dict().get("observed") or {})
                 .get("core_area_um2")})


def _finish_action_step(
    self: "FlowController", live: Any, step_id: str, capability_id: str,
    arguments_sha: str, proposal_id: str, dispatch: Any,
) -> W.FlowStepOutcome:
    """非 EDA step（explain／prepare_variant／compare）的收尾。"""
    deps = self._deps.step_deps
    task = live.task
    grant = live.grant
    summary = deps.summarize(capability_id, dispatch.full_result)
    business_class = getattr(summary, "business_class", None)

    if dispatch.timed_out:
        execution_status, worker_completion = "unconfirmed", "unknown"
    elif dispatch.succeeded:
        execution_status, worker_completion = "succeeded", "confirmed"
    elif dispatch.denial_code is not None:
        execution_status, worker_completion = "denied", "confirmed"
    else:
        execution_status, worker_completion = "failed", "confirmed"

    # prepare_variant 成功時把候選身分記進 live state 與事實事件。
    extra: dict[str, Any] = {}
    if (_is_prepare_step(task.template_id, step_id)
            and execution_status == "succeeded"
            and isinstance(dispatch.full_result, dict)):
        ref_payload = dispatch.full_result.get("variant_input_ref")
        if isinstance(ref_payload, dict):
            slot = _slot_for_step(task, step_id)
            entry = task.candidate_entry(slot)
            expected = (str(entry["expected_tree_hash"]) if entry
                        else task.expected_candidate_tree_hash)
            if task_uses_exact_geometry(task):
                # v5／v0.8：create 時綁的是**規則** digest（geometry 要等 s1 才
                # 存在），所以這裡驗的是「候選確實由已驗證的 layout binding
                # 產生」，實際 tree hash 由這一步產生並往下綁到 s4。
                with live.lock:
                    layout = getattr(live, "layout_binding", None)
                if layout is None:
                    raise W.FlowStopped(
                        "the candidate was materialized without a verified "
                        "baseline layout binding")
            elif ref_payload.get("tree_hash") != expected:
                raise W.FlowStopped(
                    "the materialized candidate tree hash does not match the value "
                    "bound into the task digest")
            with live.lock:
                live.variant_ref = F.VariantInputRef(
                    task_id=task.task_id,
                    candidate_root=str(ref_payload["candidate_root"]),
                    manifest_path=str(ref_payload["manifest_path"]),
                    manifest_sha256=str(ref_payload["manifest_sha256"]),
                    tree_hash=str(ref_payload["tree_hash"]),
                    config_sha256=str(ref_payload["config_sha256"]))
                if not isinstance(getattr(live, "variant_refs", None), dict):
                    live.variant_refs = {}
                live.variant_refs[slot] = live.variant_ref
            W.persist_fact_event(
                deps, live, task, event_type=F.EVENT_INPUT_REFERENCE,
                body={"slot": slot, "step_id": step_id,
                      "input_reference": ref_payload})
            extra["candidate_tree_hash"] = ref_payload["tree_hash"]

    event_id, sha, persistence = W.persist_step_result(
        deps, live, task, grant, step_id=step_id, capability_id=capability_id,
        arguments_sha=arguments_sha, proposal_id=proposal_id,
        execution_status=execution_status,
        action_status=getattr(summary, "action_status", None),
        business_class=business_class, full_result=dispatch.full_result,
        package_record_refs=tuple(getattr(summary, "record_refs", ()) or ()))

    step_status = ("OUTCOME_UNCONFIRMED" if execution_status == "unconfirmed"
                   else "SUCCEEDED" if execution_status == "succeeded"
                   and persistence == "durable"
                   else "DENIED" if execution_status == "denied" else "FAILED")
    # prepare_variant 沒有 business_class 概念（它不是分析）。
    can_continue = (step_status == "SUCCEEDED" and persistence == "durable"
                    and (_is_prepare_step(task.template_id, step_id)
                         or business_class == "valid"))

    # ── 解說被拒時，唯一被允許的「不阻擋已授權工程」分支 ──
    # 只有新 template、只有 evidence-ref 拒絕、只有工程前提仍可信才適用。
    # 這個判定必須在下面的通用失敗收尾**之前**做完——不能先把 task 終結
    # 再復活。
    if (not can_continue
            and _capabilities_for(task.template_id).get(step_id)
            in (CAP_EXPLAIN_RUN, CAP_COMPARE_RUNS)):
        decision = _evaluate_narrative_failure(
            self, live, step_id, capability_id, dispatch)
        _record_narrative_rejection(live, step_id, capability_id, decision)
        if decision.may_continue:
            event_ok = W.persist_fact_event(
                deps, live, task, event_type=F.EVENT_ANALYSIS_DISPOSITION,
                body=_disposition_body(live, step_id, capability_id, decision,
                                       event_id, sha))
            if event_ok:
                # 事件在下一步 intent 之前落盤；寫入失敗就不續跑。
                can_continue = (step_status in ("SUCCEEDED", "FAILED")
                                and persistence == "durable")
                extra["narrative_disposition"] = decision.to_json_dict()
            else:
                _LOG.warning(
                    "analysis disposition not persisted; not continuing")
    W.persist_step_receipt(
        deps, live, task, step_id=step_id, step_status=step_status,
        outcome={"execution_status": execution_status,
                 "timed_out": dispatch.timed_out,
                 "worker_completion": worker_completion},
        persistence_status=persistence, result_event_id=event_id,
        result_sha256=sha, can_continue=can_continue,
        error_code=dispatch.denial_code)
    # compare 的 deterministic 產物留在 step details，供 selector 使用。
    if (_capabilities_for(task.template_id).get(step_id) == CAP_COMPARE_RUNS
            and isinstance(dispatch.full_result, dict)):
        full = dispatch.full_result
        insight = full.get("insight") or {}
        extra["metric_comparisons"] = (full.get("metric_comparisons")
                                       or insight.get("metric_comparisons") or [])
        extra["selection_eligibility"] = full.get("selection_eligibility")
        extra["comparison_status"] = full.get("comparison_status")

    # ── 最後一個 compare 之後由 host 呼叫 Package 的 deterministic
    # selector。它不是第九個 action，也沒有 LLM turn。 ──
    # 解說被拒後續跑時 s5 是 FAILED 但 `can_continue`：工程 gate 照樣要判定並
    # 保存——解說降級不會讓已授權的工程判定消失。
    # 走 method extension 的 template 一樣要在 compare 步驟做固定 gate；
    # CORE 的判定由 registry 解析出的 extension 負責，不是這裡的分支。
    if ((task.template_id in F.SETUP_REPAIR_TEMPLATES
         or task.template_id == F.EAP_CORE_UTILIZATION_TEMPLATE_ID)
            and _capabilities_for(task.template_id).get(step_id) == CAP_COMPARE_RUNS
            and (step_status == "SUCCEEDED" or can_continue)):
        try:
            verdict = _run_repair_acceptance(self, live, extra)
        except Exception as exc:  # noqa: BLE001 - 判定失敗如實回報
            _LOG.warning("flow task repair acceptance failed: %r", exc)
            verdict = {"repair_status": "UNVERIFIABLE", "error": repr(exc)}
        with live.lock:
            live.repair = verdict
        W.persist_fact_event(deps, live, task, event_type=F.EVENT_REPAIR,
                             body={"step_id": step_id, "repair": verdict})
        extra["repair"] = verdict

    if (task.is_bounded_search and step_id == F.STEP_COMPARE_CANDIDATE_2
            and step_status == "SUCCEEDED"):
        try:
            selection = _run_selection(
                self, live, current_slot=_slot_for_step(task, step_id),
                current_facts=extra)
        except Exception as exc:  # noqa: BLE001 - 選擇失敗如實回報，不猜結果
            _LOG.warning("flow task selection failed: %r", exc)
            selection = {"error": repr(exc)}
        with live.lock:
            live.selection = selection
        W.persist_fact_event(deps, live, task, event_type=F.EVENT_SELECTION,
                             body={"step_id": step_id, "selection": selection})
        extra["selection"] = selection

    return W.FlowStepOutcome(
        step_status=step_status, can_continue=can_continue,
        error_code=dispatch.denial_code, reason=None, result_event_id=event_id,
        result_sha256=sha, business_class=business_class,
        worker_confirmed=worker_completion == "confirmed",
        persistence_status=persistence, details=extra)


def _evaluate_narrative_failure(controller: "FlowController", live: Any,
                                step_id: str, capability_id: str,
                                dispatch: Any) -> Any:
    """s2 與 s5 共用的**同一個** host policy evaluator。"""
    from terminux_engine.bounded_execution.analysis_tasks import (
        narrative_policy as NP,
    )

    # 讀不到 profile 就等於證明不了這個 task 帶有該 policy——那就是 STOP，
    # 不是默認享有。
    try:
        profile = controller._deps.profile_for_task(live.task.task_id)
    except Exception:  # noqa: BLE001 - 讀不到即視為沒有 policy
        profile = None
    policy_id = getattr(profile, "narrative_failure_policy_id", None)
    # 工程前提：這一步依賴的已驗證證據仍然可信。只有這個 task 真的帶著固定
    # policy 時才需要算——否則第一個條件就已經是 STOP。
    ready, reason = (_narrative_engineering_ready(controller, live, step_id)
                     if policy_id == NP.POLICY_ID else (False, None))
    return NP.evaluate(
        policy_id=policy_id,
        capability_id=capability_id,
        full_result=dispatch.full_result,
        dispatch_timed_out=bool(dispatch.timed_out),
        dispatch_denial_code=dispatch.denial_code,
        worker_confirmed=not dispatch.timed_out,
        engineering_ready=ready, engineering_reason=reason)


def _narrative_engineering_ready(controller: "FlowController", live: Any,
                                 step_id: str) -> tuple[bool, str | None]:
    """重新核對這一步依賴的工程前提（s2 要重驗 s1 readiness）。"""
    task = live.task
    with live.lock:
        states = {k: dict(v) for k, v in live.steps.items()}
    for other, capability in _capabilities_for(task.template_id).items():
        if capability != CAP_RUN_FLOW:
            continue
        state = states.get(other) or {}
        if state.get("status") not in (None, "PENDING", "SUCCEEDED"):
            return False, (f"the engineering step {other} is "
                           f"{state.get('status')}, not trustworthy")
    baseline = live.runs.get(F.SLOT_BASELINE)
    if baseline is None:
        return False, "no verified baseline run reference is available"
    return True, None


def _record_narrative_rejection(live: Any, step_id: str, capability_id: str,
                                decision: Any) -> None:
    """只記「這一步真的是模型引用被拒」的判定。

    evaluator 會被每一個未通過的解說步呼叫，其中大多數與引用被拒無關（例如
    business class 不是 valid）。把那些也記成「解說被拒」會讓產品標示的
    解說狀態失真，所以這裡以分類結果為準，不以「有沒有走到 evaluator」為準。
    """
    from terminux_engine.bounded_execution.analysis_tasks import (
        narrative_policy as NP,
    )

    if decision.failure_kind is None and not decision.may_continue:
        _LOG.info("narrative failure not eligible for %s: %s",
                  step_id, decision.reason)
        return
    with live.lock:
        if not isinstance(getattr(live, "narrative_rejections", None), list):
            live.narrative_rejections = []
        live.narrative_rejections.append({
            "step_id": step_id, "capability_id": capability_id,
            "disposition": decision.disposition, "reason": decision.reason,
            "failure_kind": decision.failure_kind,
            "policy_id": NP.POLICY_ID,
        })


def _disposition_body(live: Any, step_id: str, capability_id: str,
                      decision: Any, result_event_id: str | None,
                      result_sha256: str | None) -> dict[str, Any]:
    """`flow_task.analysis_disposition.v1` 的內容。**不帶執行授權。**"""
    task = live.task
    return {
        "step_id": step_id, "capability_id": capability_id,
        "policy_id": decision.to_json_dict()["policy_id"],
        "template_id": task.template_id,
        # `task_digest` 是 event envelope 的共同欄位，body 不得重複覆寫。
        "profile_digest": task.profile_digest,
        "failed_result_event_id": result_event_id,
        "failed_result_sha256": result_sha256,
        "failure_kind": decision.failure_kind,
        "disposition": decision.disposition,
        "reason": decision.reason,
        "classified": decision.to_json_dict()["classified"],
        "verified_engineering_references": {
            slot: ref.run_id for slot, ref in live.runs.items()},
    }


def _read_json_evidence(path: Path) -> dict[str, Any] | None:
    """讀回 flow 在正式 stage 內寫出的證據檔。讀不到就是 `None`，不補造。"""
    import json

    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _slot_reports_dir(profile: Any, task: Any, slot: str, variant: str) -> Path:
    return (Path(profile.slot_output_root(task.task_id, slot)) / "reports"
            / profile.platform_id / profile.design_id / variant)


def _read_synthesis_target_evidence(profile: Any, task: Any) -> dict[str, Any]:
    """兩側合成 delay target 的實收與**實際消費**事實。

    `expected_ps` 由已驗證的 SDC 週期與時間單位精確換算，不是寫死的常數；
    script 的 `{D}` 佔位符由 snapshot 內那支**實際被 source 的檔案**檢查。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        synthesis_target as ST,
    )

    variant = f"terminux_{task.task_id[-8:]}"
    scripts = Path(profile.input_snapshot_root) / "flow" / "scripts"
    expected = ST.expected_ps_from_sdc_period(
        _sdc_clock_period(profile) or "",
        unit=_sta_time_unit(profile, task) or "")
    out: dict[str, Any] = {"expected_ps": expected}
    for slot in (F.SLOT_BASELINE, F.SLOT_CANDIDATE):
        try:
            evidence = ST.read_synthesis_target_evidence(
                profile.slot_output_root(task.task_id, slot),
                platform=profile.platform_id, design=profile.design_id,
                variant=f"{variant}_{slot}", slot=slot, expected_ps=expected,
                script_root=scripts)
        except Exception as exc:  # noqa: BLE001 - 讀不到就是 unknown
            out[slot] = {"slot": slot, "status": ST.UNKNOWN,
                         "reason": f"evidence unreadable: {exc!r}",
                         "consumed": False}
            continue
        out[slot] = evidence.to_json_dict()
    return out


def _read_rtl_source_evidence(profile: Any, task: Any) -> dict[str, Any]:
    """兩側「合成實際讀取核准 RTL」的三層事實。

    baseline 應該讀到 `before_sha256`，candidate 應該讀到 `after_sha256`。
    兩者都由該 slot **自己的** source 樹核對，不用 profile snapshot 頂替
    candidate——那正是這一項要防的事。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        rtl_source_evidence as RSE,
    )

    recipe = getattr(profile, "repair_recipe", None)
    patch = getattr(recipe, "rtl_patch", None)
    if patch is None:
        return {}
    variant = f"terminux_{task.task_id[-8:]}"
    expected = {F.SLOT_BASELINE: patch.before_sha256,
                F.SLOT_CANDIDATE: patch.after_sha256}
    roots = {F.SLOT_BASELINE: Path(profile.input_snapshot_root)}
    candidate_root = getattr(profile, "candidate_source_root", None)
    if callable(candidate_root):
        roots[F.SLOT_CANDIDATE] = Path(
            candidate_root(task.task_id, F.SLOT_CANDIDATE))
    out: dict[str, Any] = {"rtl_patch": patch.to_json_dict()}
    for slot in (F.SLOT_BASELINE, F.SLOT_CANDIDATE):
        try:
            evidence = RSE.read_rtl_source_evidence(
                profile.slot_output_root(task.task_id, slot),
                platform=profile.platform_id, design=profile.design_id,
                variant=f"{variant}_{slot}", slot=slot,
                rtl_relative_path=patch.path,
                expected_sha256=expected[slot],
                modules=patch.modules,
                source_root=roots.get(slot))
        except Exception as exc:  # noqa: BLE001 - 讀不到就是 unknown
            out[slot] = {"slot": slot, "status": RSE.UNKNOWN,
                         "reason": f"evidence unreadable: {exc!r}",
                         "established": False}
            continue
        out[slot] = evidence.to_json_dict()
    return out


def _sdc_clock_period(profile: Any) -> str | None:
    """從**已驗證的 snapshot** 讀 SDC 的 `clk_period`。不開發通用 parser。"""
    import re

    sdc = (Path(profile.input_snapshot_root) / "flow" / "designs"
           / profile.platform_id / profile.design_id / "constraint.sdc")
    if not sdc.is_file():
        return None
    match = re.search(r"^\s*set\s+clk_period\s+(\S+)\s*$",
                      sdc.read_text(errors="replace"), re.MULTILINE)
    return match.group(1) if match else None


def _sta_time_unit(profile: Any, task: Any) -> str | None:
    """本 task 自己保存的 STA 時間單位。

    來源是 setup 證據裡的 `time_unit`——那是由 `sta::unit_scale_abbreviation`／
    `sta::unit_suffix` 取得的**工具自報單位**，不是我們假設的。讀不到就回
    `None`，讓上層判成 unknown；**不預設 ns**。
    """
    import json

    variant = f"terminux_{task.task_id[-8:]}"
    for slot in (F.SLOT_BASELINE, F.SLOT_CANDIDATE):
        path = (Path(profile.slot_output_root(task.task_id, slot)) / "reports"
                / profile.platform_id / profile.design_id / f"{variant}_{slot}"
                / f"terminux_r5_final_{slot}_setup_endpoints.json")
        if not path.is_file():
            continue
        try:
            unit = json.loads(path.read_text(encoding="utf-8")).get("time_unit")
        except (OSError, ValueError):
            continue
        if isinstance(unit, str) and unit.strip():
            return unit.strip()
    return None


def _read_strategy_evidence(profile: Any, task: Any, live: Any) -> dict[str, Any]:
    """兩個 slot 實際消費的 ABC 策略與合成時脈 target。

    來源是**正式 synth log 自己印出來的行**（`synth_preamble.tcl` 的
    「Using ABC area/speed script.」與「Setting clock period to …」），不是
    `make print-ABC_AREA` 之類的設定字串——設定值不等於被消費。
    """
    import re

    out: dict[str, Any] = {}
    variant = f"terminux_{task.task_id[-8:]}"
    for slot in (F.SLOT_BASELINE, F.SLOT_CANDIDATE):
        logs = (Path(profile.slot_output_root(task.task_id, slot)) / "logs"
                / profile.platform_id / profile.design_id / f"{variant}_{slot}")
        text = ""
        for name in ("1_1_yosys_canonicalize.log", "1_synth.log", "1_2_yosys.log"):
            path = logs / name
            if path.is_file():
                text += path.read_text(errors="replace")
        if not text:
            continue
        script = None
        if "Using ABC speed script." in text:
            script = "abc_speed.script"
        elif "Using ABC area script." in text:
            script = "abc_area.script"
        out[f"{slot}_abc_script"] = script
        match = re.search(r"Setting clock period to (\S+)", text)
        if match:
            out[f"{slot}_abc_clock_period_value"] = match.group(1)
    # 單位如實記錄：pinned 的變數叫 `ABC_CLOCK_PERIOD_IN_PS`，但它取的是
    # SDC 的原始數值（本設計是 0.25 ns）。這個不一致在這裡如實列出，
    # 不在這裡「修正」成 250——改流程不在授權範圍內。
    out["sdc_clock_period_ns"] = "0.25"
    out["expected_ps_if_unit_were_applied"] = "250"
    value = out.get("baseline_abc_clock_period_value")
    out["abc_target_unit_consistent"] = (value == "250")
    out["abc_target_note"] = (
        "pinned variables.mk 直接把 SDC 的數值當作 _IN_PS 傳給 yosys-abc 的 "
        "-D；兩個 slot 完全相同，因此是受控的共同條件，但單位並不一致。")
    return out


def _run_repair_acceptance(controller: "FlowController", live: Any,
                           compare_facts: dict[str, Any]) -> dict[str, Any]:
    """setup repair 固定 gate ＋ comparison basis。

    gate 的輸入與判定完全不變；這裡只**加上** DLE 的 change dimensions：
    basis 相同 ⇒ `comparison_status=SAME_BASIS`，gate 結論照舊；basis 改變
    ⇒ `comparison_status=NOT_COMPARABLE`，`repair_status` 改為
    `NOT_COMPARABLE`（原 gate 結論保留在 `gate_repair_status`），並給出
    允許的表述範圍。lineage 不完整 ⇒ `UNVERIFIABLE`。這些都不阻止執行。
    """
    payload = _run_repair_acceptance_gate(controller, live, compare_facts)
    payload["comparison_basis"] = None
    payload["comparison_status"] = "UNKNOWN"
    ledger = controller._deps.step_deps.lineage_ledger_path
    if ledger is None:
        return payload
    try:
        from terminux_engine.lineage import change_dimensions as CD
        from terminux_engine.lineage import flow_lineage as FL

        task = live.task
        baseline_source_task_id = ((getattr(live, "baseline_import", None) or {}).get(
            "source_task_id") or task.task_id)
        base = FL.find_flow_commits(ledger, task_id=baseline_source_task_id,
                                    slot=F.SLOT_BASELINE, status=FL.STATUS_COMMITTED)
        cand = FL.find_flow_commits(ledger, task_id=task.task_id,
                                    slot=F.SLOT_CANDIDATE, status=FL.STATUS_COMMITTED)
        if not base or not cand:
            payload["comparison_status"] = "UNVERIFIABLE"
            payload["claim_scope"] = CD.CLAIM_NOT_COMPARABLE
            payload["comparison_reason"] = "lineage commits are missing for one slot"
            return payload
        dims = CD.compare_flow_commits(base[-1], cand[-1])
        payload["comparison_basis"] = dims.to_json_dict()
        payload["claim_scope"] = dims.allowed_claim
        if dims.status == "CONSISTENT":
            payload["comparison_status"] = "SAME_BASIS"
        elif dims.status == "INSUFFICIENT_EVIDENCE":
            payload["comparison_status"] = "UNVERIFIABLE"
            payload["gate_repair_status"] = payload.get("repair_status")
            payload["repair_status"] = "UNVERIFIABLE"
        else:
            payload["comparison_status"] = "NOT_COMPARABLE"
            payload["gate_repair_status"] = payload.get("repair_status")
            payload["repair_status"] = "NOT_COMPARABLE"
    except Exception as exc:  # noqa: BLE001 - 比較本身失敗就是無法確認
        payload["comparison_status"] = "UNVERIFIABLE"
        payload["comparison_reason"] = repr(exc)
    return payload


def _run_repair_acceptance_gate(controller: "FlowController", live: Any,
                                compare_facts: dict[str, Any]) -> dict[str, Any]:
    """固定 gate。輸入只有正式 flow 寫出的證據與既有 DQE 比較。"""
    from terminux_engine.packages.orfs_run_intelligence import (
        repair_acceptance as RA,
    )

    task = live.task
    profile = controller._deps.profile_for_task(live.task.task_id)
    variant = f"terminux_{task.task_id[-8:]}"
    baseline_import = getattr(live, "baseline_import", None) or {}
    baseline_task_id = baseline_import.get("source_task_id") or task.task_id
    baseline_variant = (baseline_import.get("variant")
                        or f"{variant}_{F.SLOT_BASELINE}")
    baseline_output_root = (baseline_import.get("output_root")
                            or str(profile.slot_output_root(
                                task.task_id, F.SLOT_BASELINE)))
    base_dir = (Path(baseline_output_root) / "reports" / profile.platform_id
                / profile.design_id / baseline_variant)
    cand_dir = _slot_reports_dir(profile, task, F.SLOT_CANDIDATE,
                                 f"{variant}_{F.SLOT_CANDIDATE}")

    with live.lock:
        states = {k: dict(v) for k, v in live.steps.items()}
        runs = dict(live.runs)
        reference = getattr(live, "verification_reference", None)
        layout = getattr(live, "layout_binding", None)
    resynthesis = task_uses_resynthesis(task)
    prefix_borrow = task_uses_prefix_borrow(task)
    from terminux_engine.packages.orfs_run_intelligence import (
        method_extensions as ME,
    )

    # method extension 只由 task digest 內保存的 pin 決定；解析失敗即 fail closed。
    try:
        dispatch = _resolve_method_extension(task, profile)
    except ME.MethodExtensionError as exc:
        return _extension_rejected_payload(controller, live, runs, reference, exc)
    setup_margin = (dispatch is not None
                    and ME.host_evidence_set(dispatch.extension) == "setup_margin")
    run_facts = dict(getattr(live, "run_evidence", None) or {})
    #: v5 與 v0.8 共用同一組 post-resize 證據與 geometry 規則。
    exact_geometry = task_uses_exact_geometry(task)
    execution_ok = all(
        state.get("status") == "SUCCEEDED" for step_id, state in states.items()
        if _capabilities_for(task.template_id).get(step_id) == CAP_RUN_FLOW)

    # 允許差異：candidate 的 input 必須就是本 task 已核准的那一份。
    variant_ref = _variant_ref_for(live, F.SLOT_CANDIDATE)
    entry = task.candidate_entry(F.SLOT_CANDIDATE)
    expected_tree = (str(entry["expected_tree_hash"]) if entry
                     else task.expected_candidate_tree_hash)
    if exact_geometry:
        # v5／v0.8 的 tree hash 在 create 時還不存在（geometry 要等 s1），因此改驗：
        # 候選確實被 materialize 過、geometry 綁定來自本 task 的 s1、且
        # baseline reference 已備妥。expected_tree 在這裡是規則 digest。
        inputs_approved = bool(
            variant_ref is not None and reference and layout is not None
            and getattr(layout, "run_id", None)
            == (runs.get(F.SLOT_BASELINE).run_id if runs.get(F.SLOT_BASELINE)
                else None))
        inputs_reason = None
        if not inputs_approved:
            inputs_reason = ("the candidate input, the baseline functional "
                             "reference or the verified layout binding is missing")
    else:
        # 是否需要 baseline 功能驗證 reference 由 method extension descriptor
        # 宣告（不是看 template 目前會不會產生它）。宣告需要而缺席時：
        # qualification fixture 只標記 production readiness 未建立，其他一律
        # 由 Package adapter fail closed。
        # qualification fixture 不可能有 production reference（它被禁止產生），
        # 因此不以它判 inputs；production readiness 另外標成未建立。
        qualification_fixture = bool(
            getattr(profile, "is_qualification_fixture", False))
        needs_reference = (
            dispatch.extension.requires_baseline_functional_reference
            and dispatch.extension.produces_baseline_functional_reference
            and not qualification_fixture
            if dispatch is not None else task.template_id in F.SETUP_REPAIR_TEMPLATES)
        inputs_approved = bool(
            variant_ref is not None and variant_ref.tree_hash == expected_tree
            and (reference or not needs_reference))
        inputs_reason = None
        if not inputs_approved:
            inputs_reason = ("the candidate input tree hash or the baseline "
                             "functional reference does not match the approved "
                             "value")

    # CORE production 路徑：兩側 slot 證據在這裡才一次組出來——它需要相依
    # proof task 的 durable envelope，而那份 envelope 在 candidate run 完成時
    # 還不存在。prerequisite 不成立就不放行比較（fail closed）。
    core_slot_files: dict[str, dict[str, str]] = {}
    core_production = (
        dispatch is not None
        and ME.host_evidence_set(dispatch.extension) == "core_utilization"
        and not getattr(profile, "is_qualification_fixture", False))
    core_evidence_location: dict[str, tuple[str, str]] = {}
    if core_production:
        assembled, reason = _core_production_slot_evidence(
            controller, live, task=task, profile=profile, dispatch=dispatch,
            reference=reference, run_facts=run_facts, variant=variant)
        if assembled is None:
            return _extension_rejected_payload(
                controller, live, runs, reference,
                ME.MethodExtensionError(CORE_PROOF_PREREQUISITE_FAILED,
                                        reason or "the CORE pair proof "
                                        "prerequisite is not satisfied"))
        core_evidence_location = {
            slot: core_slot_evidence_location(profile, task, slot, variant=variant)
            for slot in assembled}
        for slot, payload in assembled.items():
            slot_root, slot_variant = core_evidence_location[slot]
            core_slot_files[slot] = _write_core_slot_evidence(
                profile, task, slot=slot, variant=slot_variant,
                payload=payload, output_root=slot_root)

    # 證據身分由 host 的 `VerifiedRepairEvidence` 驗；內容由 Package 解讀。
    from terminux_engine.bounded_execution.analysis_tasks import (
        repair_evidence as RE,
    )

    context = controller._deps.step_deps.request_context()
    verified: dict[str, Any] = {}
    for slot in (F.SLOT_BASELINE, F.SLOT_CANDIDATE):
        if dispatch is not None and ME.host_evidence_set(dispatch.extension) is None:
            # 此 extension 沒有 host repair evidence 檔案集合；不借用其他方法的。
            continue
        run_ref = runs.get(slot)
        source_task_id = baseline_task_id if slot == F.SLOT_BASELINE else task.task_id
        if core_production:
            output_root, evidence_variant = core_evidence_location[slot]
        else:
            output_root = (baseline_output_root if slot == F.SLOT_BASELINE
                           else str(profile.slot_output_root(task.task_id, slot)))
            evidence_variant = (baseline_variant if slot == F.SLOT_BASELINE
                                else f"{variant}_{slot}")
        verified[slot] = RE.read_repair_evidence(
            context=context, task_id=source_task_id, slot=slot,
            run_id=run_ref.run_id if run_ref is not None else "",
            output_root=output_root,
            platform_id=profile.platform_id, design_id=profile.design_id,
            variant=evidence_variant,
            # 證據集合以「有沒有 post-resize pair」為準，v5 與 v0.8 相同。
            resynthesis=exact_geometry,
            # parametric setup repair 的中間比較點是 post-CTS（方法在 CTS 被消費），檔名與階段
            # 標籤都如實反映，不沿用 post_resize 這個會誤導的名字。
            setup_margin=setup_margin,
            # extension 管轄的 task：證據集合由 Package registry 宣告。
            evidence_set=(ME.host_evidence_set(dispatch.extension)
                          if dispatch is not None else None))

    if dispatch is not None:
        return _run_extension_acceptance(
            controller, live, dispatch, verified=verified, runs=runs,
            reference=reference, variant=variant, compare_facts=compare_facts,
            execution_ok=execution_ok, inputs_approved=inputs_approved,
            inputs_reason=inputs_reason, host_run_facts=run_facts,
            core_slot_files=core_slot_files)

    base_ev, cand_ev = verified[F.SLOT_BASELINE], verified[F.SLOT_CANDIDATE]
    if prefix_borrow:
        # 方法 gate 按**明確 recipe** 分派。這一條不依賴 mapper
        # delay target，但也不是「遇到 false 就自動 NOT_APPLICABLE」——是
        # template／recipe 決定的固定分支。
        if task_uses_transition_miter(task):
            # 同一組工程條件，但 mapped pair 的 proof 必須出自固定的
            # `yosys_transition_miter.v1`。方法身分不成立即 UNVERIFIABLE。
            from terminux_engine.packages.orfs_run_intelligence import (
                prefix_borrow_miter_acceptance as PBMA,
            )

            evaluate = PBMA.evaluate_prefix_borrow_miter
            repaired_label = PBMA.REPAIRED
        else:
            from terminux_engine.packages.orfs_run_intelligence import (
                prefix_borrow_acceptance as PBA,
            )

            evaluate = PBA.evaluate_prefix_borrow
            repaired_label = PBA.REPAIRED

        verdict = evaluate(
            baseline_setup=base_ev.payload("setup_endpoints"),
            candidate_setup=cand_ev.payload("setup_endpoints"),
            baseline_core=base_ev.payload("core"),
            candidate_core=cand_ev.payload("core"),
            baseline_post_resize_topology=base_ev.payload("post_resize_topology"),
            candidate_post_resize_topology=cand_ev.payload("post_resize_topology"),
            post_resize_pair_proof=cand_ev.payload("post_resize_pair_proof"),
            final_pair_proof=cand_ev.payload("final_pair_proof"),
            metric_comparisons=compare_facts.get("metric_comparisons") or (),
            rtl_evidence=_read_rtl_source_evidence(profile, task),
            #: 原 target 未被使用的觀察照實保留，但不作為本 recipe 的門檻。
            target_evidence=_read_synthesis_target_evidence(profile, task),
            execution_ok=execution_ok,
            inputs_approved=inputs_approved, inputs_reason=inputs_reason)
        payload = verdict.to_json_dict()
        payload["evidence"] = {slot: ev.to_json_dict()
                               for slot, ev in verified.items()}
        payload["runs"] = {slot: ref.run_id for slot, ref in runs.items()}
        payload["verification_reference"] = reference
        payload["layout_binding"] = (
            layout.to_json_dict() if layout is not None else None)
        payload["retained"] = _retained_references(controller, live, "")
        payload["selected_slot"] = (
            F.SLOT_CANDIDATE if verdict.repair_status == repaired_label
            else F.SLOT_BASELINE)
        payload["retained"]["selected_slot"] = payload["selected_slot"]
        return payload

    if resynthesis:
        from terminux_engine.packages.orfs_run_intelligence import (
            resynthesis_acceptance as RSA,
        )

        verdict = RSA.evaluate_resynthesis(
            baseline_setup=base_ev.payload("setup_endpoints"),
            candidate_setup=cand_ev.payload("setup_endpoints"),
            baseline_core=base_ev.payload("core"),
            candidate_core=cand_ev.payload("core"),
            baseline_post_resize_topology=base_ev.payload("post_resize_topology"),
            candidate_post_resize_topology=cand_ev.payload("post_resize_topology"),
            post_resize_pair_proof=cand_ev.payload("post_resize_pair_proof"),
            final_pair_proof=cand_ev.payload("final_pair_proof"),
            metric_comparisons=compare_facts.get("metric_comparisons") or (),
            strategy_evidence=_read_strategy_evidence(profile, task, live),
            target_evidence=_read_synthesis_target_evidence(profile, task),
            execution_ok=execution_ok,
            inputs_approved=inputs_approved, inputs_reason=inputs_reason)
        repaired_status = RSA.REPAIRED if hasattr(RSA, "REPAIRED") else "REPAIRED"
        payload = verdict.to_json_dict()
        payload["evidence"] = {slot: ev.to_json_dict()
                               for slot, ev in verified.items()}
        payload["runs"] = {slot: ref.run_id for slot, ref in runs.items()}
        payload["verification_reference"] = reference
        payload["layout_binding"] = (
            layout.to_json_dict() if layout is not None else None)
        payload["retained"] = _retained_references(controller, live, "")
        payload["selected_slot"] = (
            F.SLOT_CANDIDATE if verdict.repair_status == repaired_status
            else F.SLOT_BASELINE)
        payload["retained"]["selected_slot"] = payload["selected_slot"]
        return payload

    verdict = RA.evaluate_repair(
        baseline_setup=base_ev.payload("setup_endpoints"),
        candidate_setup=cand_ev.payload("setup_endpoints"),
        baseline_core=base_ev.payload("core"),
        candidate_core=cand_ev.payload("core"),
        eco_diff=cand_ev.payload("eco_diff"),
        eco_proof=cand_ev.payload("eco_proof"),
        final_pair_proof=cand_ev.payload("final_pair_proof"),
        metric_comparisons=compare_facts.get("metric_comparisons") or (),
        execution_ok=execution_ok,
        inputs_approved=inputs_approved, inputs_reason=inputs_reason,
        target_slack_ns=getattr(getattr(profile, "repair_recipe", None), "target_slack_ns", None))

    payload = verdict.to_json_dict()
    payload["evidence"] = {slot: ev.to_json_dict() for slot, ev in verified.items()}
    payload["runs"] = {slot: ref.run_id for slot, ref in runs.items()}
    payload["verification_reference"] = reference
    payload["retained"] = _retained_references(controller, live, "")
    payload["selected_slot"] = (
        F.SLOT_CANDIDATE if verdict.repair_status == RA.REPAIRED
        else F.SLOT_BASELINE)
    payload["retained"]["selected_slot"] = payload["selected_slot"]
    return payload


def task_uses_resynthesis(task: Any) -> bool:
    """本 task 是否走 v5 的速度導向重合成 template。"""
    return getattr(task, "template_id", "") in F.RESYNTHESIS_TEMPLATES


def task_uses_prefix_borrow(task: Any) -> bool:
    """本 task 是否走固定 RTL patch template（v0.8 與 v0.12 都是）。"""
    return getattr(task, "template_id", "") in F.PREFIX_BORROW_TEMPLATES


def task_uses_transition_miter(task: Any) -> bool:
    """本 task 的 mapped pair proof 是否由 canonical transition miter 產生。"""
    return getattr(task, "template_id", "") in F.TRANSITION_MITER_TEMPLATES


@dataclass(frozen=True)
class _ExtensionDispatch:
    """本 task 由保存的 pin 解析出的 method extension。"""

    extension: Any
    #: 只有歷史 setup-margin（無 pin）才是 True。
    legacy: bool


def _resolve_method_extension(task: Any, profile: Any) -> _ExtensionDispatch | None:
    """gate 用：Profile method lineage 取自本 task 的 task-owned Profile snapshot。"""
    recipe = getattr(profile, "repair_recipe", None)
    return resolve_task_method_extension(
        task, method_variable=getattr(getattr(recipe, "method", None), "variable", None))


def resolve_task_method_extension(task: Any, *,
                                  method_variable: str | None) -> _ExtensionDispatch | None:
    """只讀 `task.eap_proposal.method_extension`；不回頭讀 Work plan 補值。

    FlowController gate 與 closed Work reader 共用本規則：

    - 有 pin：經 Package registry 核對 identity／version／descriptor／digest，
      且 descriptor 的 parameter 必須就是本 task 已核准的 parameter。pin 欄位
      存在但不合法時不退回 legacy。
    - 無 pin 的 CORE_UTILIZATION template：拒絕（沒有 legacy fallback）。
    - 無 pin 的 setup-margin template：Profile method、Package schema 與
      parameter 都確認是 SETUP_SLACK_MARGIN 才走受限 legacy adapter；完整歷史
      evidence shape 由 adapter 再核對。
    - 其他 template 無 pin：不屬於 extension dispatch，回 `None`。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        method_extensions as ME,
    )
    from terminux_engine.packages.orfs_run_intelligence.proposal_validator import (
        PROPOSAL_SCHEMA_VERSION,
    )

    template_id = getattr(task, "template_id", "")
    governed = (template_id in F.SETUP_MARGIN_TEMPLATES
                or template_id == F.EAP_CORE_UTILIZATION_TEMPLATE_ID)
    proposal = getattr(task, "eap_proposal", None)
    package = proposal.get("package") if isinstance(proposal, dict) else None
    if isinstance(proposal, dict) and "method_extension" in proposal:
        extension = ME.resolve_pinned(proposal["method_extension"])
        if (not governed or task.parameter_name != extension.parameter
                or not isinstance(package, dict)
                or package.get("parameter_name") != extension.parameter):
            raise ME.MethodExtensionError(
                ME.UNVERIFIABLE,
                "the pinned method extension does not match this task")
        return _ExtensionDispatch(extension=extension, legacy=False)
    if not governed:
        return None
    if template_id == F.EAP_CORE_UTILIZATION_TEMPLATE_ID:
        raise ME.MethodExtensionError(
            ME.UNVERIFIABLE,
            "CORE_UTILIZATION task has no pinned method extension")
    lineage_ok = method_variable == ME.SETUP_MARGIN.parameter
    if proposal is not None:
        lineage_ok = lineage_ok and (
            isinstance(package, dict)
            and package.get("package_schema_version") == PROPOSAL_SCHEMA_VERSION
            and package.get("parameter_name") == ME.SETUP_MARGIN.parameter)
    if not lineage_ok:
        raise ME.MethodExtensionError(
            ME.UNVERIFIABLE, "historical task lineage is not setup-margin")
    return _ExtensionDispatch(extension=ME.SETUP_MARGIN, legacy=True)


def _read_extension_consumption(dispatch: _ExtensionDispatch, profile: Any,
                                task: Any, slot: str, variant: str) -> Any:
    """由 resolved extension 的 host reader 產生權威的方法消費證據。

    setup-margin：hook 只看得到 env 值，`repair_timing` 實際 argv 要等 CTS log
    寫完才讀得到，因此由 host 在判定時算。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        method_extensions as ME,
    )

    try:
        return ME.read_consumption_evidence(
            dispatch.extension, str(profile.slot_output_root(task.task_id, slot)),
            platform=profile.platform_id, design=profile.design_id,
            variant=f"{variant}_{slot}",
            expected_value=task.value_for_slot(slot))
    except Exception as exc:  # noqa: BLE001 - 讀不到就是沒有證據
        _LOG.warning("method extension consumption evidence unavailable: %r", exc)
        return None


def _run_extension_acceptance(
    controller: "FlowController", live: Any, dispatch: _ExtensionDispatch, *,
    verified: dict[str, Any], runs: dict[str, Any], reference: Any, variant: str,
    compare_facts: dict[str, Any], execution_ok: bool, inputs_approved: bool,
    inputs_reason: str | None, host_run_facts: dict[str, Any] | None = None,
    core_slot_files: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """acceptance 位置：呼叫 resolved extension 的 proof／comparison／acceptance。"""
    from terminux_engine.packages.orfs_run_intelligence import (
        method_extensions as ME,
    )

    task = live.task
    profile = controller._deps.profile_for_task(task.task_id)
    candidate_value = task.value_for_slot(F.SLOT_CANDIDATE)
    recipe = getattr(profile, "repair_recipe", None)
    method = getattr(recipe, "method", None)
    # EAP 的 candidate 來自已核准 task snapshot，不可回頭取 Profile 的固定值。
    method_json = (method.to_json_dict() if method is not None
                   else {"variable": task.parameter_name})
    method_json["candidate_value"] = candidate_value
    slots = {slot: ({name: verified[slot].payload(name) for name in verified[slot].refs}
                    if slot in verified else {})
             for slot in (F.SLOT_BASELINE, F.SLOT_CANDIDATE)}
    try:
        native_proof = _native_setup_margin_context(profile, dispatch.extension)
        result = ME.evaluate_acceptance(
            dispatch.extension, baseline=slots[F.SLOT_BASELINE],
            candidate=slots[F.SLOT_CANDIDATE],
            host_consumption=_read_extension_consumption(
                dispatch, profile, task, F.SLOT_CANDIDATE, variant),
            method=method_json, candidate_value=candidate_value,
            metric_comparisons=compare_facts.get("metric_comparisons") or (),
            execution_ok=execution_ok, inputs_approved=inputs_approved,
            inputs_reason=inputs_reason, legacy=dispatch.legacy,
            host_run_facts=host_run_facts or {},
            baseline_reference_available=bool(reference),
            qualification_fixture=bool(
                getattr(profile, "is_qualification_fixture", False)),
            target_slack_ns=getattr(recipe, "target_slack_ns", None),
            native_proof=native_proof)
    except ME.MethodExtensionError as exc:
        return _extension_rejected_payload(controller, live, runs, reference, exc)
    payload = dict(result.payload)
    payload["method"] = method_json
    if core_slot_files:
        # 兩份 slot 證據的 durable 位置與 hash。fresh reader 由此定位，
        # 不接受任何外部傳入的路徑。
        payload["core_slot_evidence"] = {slot: dict(row)
                                         for slot, row in core_slot_files.items()}
    payload["method_extension"] = {
        "method_identity": dispatch.extension.method_identity,
        "extension_version": dispatch.extension.extension_version,
        "resolution": "LEGACY_SETUP_MARGIN" if dispatch.legacy else "PINNED"}
    payload["baseline_method_consumption"] = slots[F.SLOT_BASELINE].get(
        "method_consumption")
    payload["evidence"] = {slot: ev.to_json_dict() for slot, ev in verified.items()}
    payload["runs"] = {slot: ref.run_id for slot, ref in runs.items()}
    payload["verification_reference"] = reference
    payload["retained"] = _retained_references(controller, live, "")
    payload["selected_slot"] = (F.SLOT_CANDIDATE if result.candidate_selected
                                else F.SLOT_BASELINE)
    payload["retained"]["selected_slot"] = payload["selected_slot"]
    return payload


def _native_setup_margin_context(profile: Any, extension: Any) -> dict[str, Any] | None:
    """host-native setup-margin 驗收的 host 端身分；其他 extension 回 `None`。

    proof 契約與 Profile 執行模式必須一致：container 契約不在 host-native Profile
    下判定，native 契約也不在 image Profile 下判定。qualified identity 檔在判定時
    重新核對 bytes 與 pin；binary 是否仍符合 pin 由驗收本身記錄。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        method_extensions as ME,
        setup_margin_native_proof as SMN,
    )

    if not ME.is_setup_margin(extension):
        return None
    native_contract = extension is ME.SETUP_MARGIN_NATIVE_PROOF
    native_profile = bool(getattr(profile, "is_native", False))
    if native_contract != native_profile:
        raise ME.MethodExtensionError(
            ME.UNVERIFIABLE,
            "the setup-margin proof contract does not match the Profile execution mode "
            f"(contract={'host_native' if native_contract else 'container'}, "
            f"profile={'host_native' if native_profile else 'container'}); a proof "
            "identity is never moved to another execution mode")
    if not native_contract:
        return None
    pin = _native_formal_tool(profile)
    recipe = getattr(profile, "repair_recipe", None)
    if pin is None or recipe is None:
        raise ME.MethodExtensionError(
            ME.UNVERIFIABLE,
            "the host-native Profile does not authorize a FORMAL_YOSYS binary for the "
            "setup-margin proof")
    try:
        qualified = SMN.verify_qualified_identity_file(
            recipe.qualified_tool_identity_path,
            declared_sha256=getattr(recipe, "qualified_tool_identity_sha256", None),
            pin=pin, check_host=False)
    except SMN.NativeProofIdentityError as exc:
        raise ME.MethodExtensionError(exc.code, exc.message) from None
    return {"tool": pin, "qualified_digest": qualified["digest"],
            "helper_sha256": sorted({helper.sha256 for helper in recipe.helpers})}


def _extension_rejected_payload(controller: "FlowController", live: Any,
                                runs: dict[str, Any], reference: Any,
                                exc: Any) -> dict[str, Any]:
    """extension 解析或證據歸屬不成立：一律 UNVERIFIABLE，保留 baseline。"""
    from terminux_engine.packages.orfs_run_intelligence import (
        repair_acceptance as RA,
    )

    payload = RA.RepairVerdict(
        policy_id="orfs.method_extension.dispatch.v1", policy_version="v1",
        repair_status=RA.UNVERIFIABLE, reason_codes=(exc.code,),
        reasons=(exc.reason,)).to_json_dict()
    payload["method_extension"] = {"resolution": "REJECTED", "reason_code": exc.code}
    payload["runs"] = {slot: ref.run_id for slot, ref in runs.items()}
    payload["verification_reference"] = reference
    payload["retained"] = _retained_references(controller, live, F.SLOT_BASELINE)
    payload["selected_slot"] = F.SLOT_BASELINE
    return payload


def task_uses_setup_margin(task: Any) -> bool:
    """本 task 是否走 parametric setup repair 的受控 `SETUP_SLACK_MARGIN` 修復 template。"""
    return getattr(task, "template_id", "") in F.SETUP_MARGIN_TEMPLATES


def task_uses_exact_geometry(task: Any) -> bool:
    """本 task 是否沿用 baseline 的 exact core/die（v3 與 v0.8 都是）。"""
    return getattr(task, "template_id", "") in F.EXACT_GEOMETRY_TEMPLATES


def _derive_layout_binding(controller: "FlowController", live: Any,
                           binding: Any) -> Any:
    """由 s1 的 core 證據派生 candidate 的實體邊界綁定。"""
    import hashlib
    import json

    from terminux_engine.bounded_execution.analysis_tasks import (
        layout_binding as LB,
    )

    evidence = (Path(binding.output_root) / "reports" / binding.platform_id
                / binding.design_id / binding.variant
                / f"terminux_r5_final_{F.SLOT_BASELINE}_core.json")
    if not evidence.is_file():
        raise FileNotFoundError(f"baseline core evidence not found: {evidence}")
    raw = evidence.read_bytes()
    return LB.build_layout_binding(
        payload=json.loads(raw.decode("utf-8")),
        run_id=live.runs[F.SLOT_BASELINE].run_id, slot=F.SLOT_BASELINE,
        evidence_path=str(evidence),
        evidence_sha256=hashlib.sha256(raw).hexdigest())


def _method_produces_baseline_reference(profile: Any) -> bool:
    """本 Profile 的 method 是否宣告「自己產生」baseline functional reference。"""
    identity = getattr(profile, "declared_method_identity", None)
    if not identity or getattr(profile, "is_qualification_fixture", False):
        # qualification fixture 永遠不產生 production reference。
        return False
    try:
        from terminux_engine.packages.orfs_run_intelligence import (
            method_extensions as ME,
        )

        extension = ME.resolve_declared_identity(identity)
    except Exception:  # noqa: BLE001 - 認不得的身分不產生 reference
        return False
    return bool(extension.produces_baseline_functional_reference)


def _artifact_manifest_entries(profile: Any, manifest_id: str | None) -> dict[str, str]:
    """讀回本次 run 已持久化的 artifact manifest：`resolved_path → sha256`。

    來源是 Kernel 在 Package dispatch 前寫下的 immutable manifest，不重新掃描
    檔案系統。讀不到就回空 dict——reference 會因此 fail closed。
    """
    if not manifest_id:
        return {}
    from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

    root = getattr(profile, "artifact_root", None)
    store = ArtifactManifestStore(root) if root else ArtifactManifestStore()
    manifest = store.load(manifest_id)
    entries: dict[str, str] = {}
    for observation in manifest.observations:
        identity = observation.content_identity
        path = observation.resolved_path
        if path and identity is not None and identity.algorithm == "sha256":
            entries[str(path)] = identity.digest
    return entries


#: CORE production 比較的 prerequisite 失敗原因（都會擋下比較）。
CORE_PROOF_PREREQUISITE_FAILED = "core_pair_proof_prerequisite_not_satisfied"


class WorkIdentityConflict(RuntimeError):
    """FlowTask 的兩個身分來源互相矛盾；不得自行挑一個。"""


def _production_reference(reference: Any) -> dict[str, Any] | None:
    """由 verification reference 取出 Package 的 production reference 記錄。

    `live.verification_reference` 是一層**外包裝**（`kind`／`reference_dir`／
    `reference_path`／`sha256`／`production_reference`），而 `reference_digest`
    與 `artifacts` 都在 `production_reference` 裡面。本地產生
    （`_build_production_reference`）與 durable import（`service.py` 匯入時）
    存的都是這個形狀，因此讀取一律先解開一層；傳進來的若已經是記錄本身
    （帶 `reference_digest`）則原樣接受。
    """
    if not isinstance(reference, dict):
        return None
    record = reference.get("production_reference")
    if isinstance(record, dict):
        return record
    if reference.get("reference_digest"):
        return reference
    return None


def _reference_role_sha256(reference: Any, role: str) -> str:
    """由 production reference 取某個 artifact 角色的 sha256；缺就回空字串。"""
    record = _production_reference(reference)
    artifact = ((record or {}).get("artifacts") or {}).get(role) or {}
    return str(artifact.get("sha256") or "")


def _work_attempt_identity(proposal: Any) -> tuple[str, str]:
    """由 FlowTask 的 `eap_proposal` 取 Work／attempt 身分。

    與 `batch_ledger.task_identity()` 用同一組來源與同一優先序：
    `package_tool.formal_pair` → `work_source` → `source`。任兩個來源都帶了
    身分但彼此不同時一律 `WorkIdentityConflict`，不自行選一個——選錯會把證據
    掛到別的 attempt 上。
    """
    if not isinstance(proposal, dict):
        return "", ""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for name in ("formal_pair", "work_source", "source"):
        block = (proposal.get("package_tool") or {}).get(name) \
            if name == "formal_pair" else proposal.get(name)
        if isinstance(block, dict):
            candidates.append((name, block))

    def _pick(field: str) -> str:
        seen: dict[str, str] = {}
        for name, block in candidates:
            value = block.get(field)
            if isinstance(value, str) and value:
                seen[name] = value
        distinct = set(seen.values())
        if len(distinct) > 1:
            raise WorkIdentityConflict(
                f"conflicting {field} across proposal identity sources: "
                + ", ".join(f"{n}={v}" for n, v in sorted(seen.items())))
        return next(iter(distinct), "")

    return _pick("work_id"), _pick("attempt_id")


def _native_formal_tool(profile: Any) -> dict[str, str] | None:
    """host-native Profile 授權的 formal Yosys pin（`{realpath, sha256}`）。"""
    binding = getattr(profile, "native_tool_binding", None)
    pin = (getattr(binding, "tools", None) or {}).get("FORMAL_YOSYS")
    return pin.to_json_dict() if pin is not None else None


def _check_formal_tool_boundary(profile: Any, extension: Any) -> None:
    """formal proof 的工具邊界只能來自已核准 Profile，且執行模式必須與契約一致。

    container 契約只在 image Profile 下執行；host-native 契約只在 host-native
    Profile 下、以 Profile 釘住且此刻仍相符的 `FORMAL_YOSYS` 執行。
    """
    native_contract = bool(getattr(extension, "formal_pair_native_proof", None))
    native_profile = bool(getattr(profile, "is_native", False))
    if native_contract != native_profile:
        raise W.FlowStopped(
            "the pinned formal proof contract does not match the Profile execution mode "
            f"(contract={'host_native' if native_contract else 'container'}, "
            f"profile={'host_native' if native_profile else 'container'}); "
            "a proof is never moved to another execution mode")
    if not native_contract:
        # container 契約的 proof 不再啟動：在任何 candidate 寫入或 ORFS launch
        # 之前停下，不改走 host-native、也不以 Docker 代跑。
        from terminux_engine.orchestration import legacy_docker_execution as LDE

        raise W.FlowStopped(LDE.disabled_message(
            LDE.PATH_FORMAL_PAIR, "the pinned formal proof contract is the container one"))
    from terminux_engine.orchestration.native_tool_identity import (
        NativeToolIdentityError,
        parse_pin,
        verify_pin,
    )

    pin = _native_formal_tool(profile)
    if pin is None:
        raise W.FlowStopped(
            "the host-native Profile does not authorize a FORMAL_YOSYS binary for the "
            "dependent proof")
    try:
        verify_pin(parse_pin(pin, where="FORMAL_YOSYS"), where="FORMAL_YOSYS")
    except NativeToolIdentityError as exc:
        raise W.FlowStopped(
            f"the authorized FORMAL_YOSYS binary does not match this host: {exc}") from None


def _expected_formal_tool(extension: Any, descriptor: dict[str, Any],
                          profile: Any) -> dict[str, Any]:
    """envelope 內工具身分必須等於的值：container 契約取 descriptor，native 取 Profile。"""
    if getattr(extension, "formal_pair_native_proof", None):
        from terminux_engine.packages.orfs_run_intelligence import formal_pair_native as FPN

        pin = _native_formal_tool(profile)
        if pin is None:
            raise W.FlowStopped(
                "the host-native Profile does not authorize a FORMAL_YOSYS binary")
        return FPN.tool_identity(pin)
    tool = dict(descriptor.get("tool") or {})
    return {name: tool[name] for name in ("image_id", "executable_sha256", "version_sha256")
            if name in tool}


def _formal_proof_evidence_fact(controller: "FlowController", live: Any
                                ) -> dict[str, Any] | None:
    """compare 要用的 formal proof 事實；method 沒宣告需要時回 `None`。

    狀態一律由 **durable envelope** 推導：resolve 不到就是 `MISSING`，
    envelope 驗不過就是 `UNVERIFIABLE`，驗得過才帶出它自己的 classification
    （`EQUIVALENT` / `NOT_EQUIVALENT` / `UNPROVEN` / `TOOL_ERROR` / `TIMEOUT`）。
    只有 `EQUIVALENT` 能讓 candidate eligible；`UNPROVEN` 是**明確的拒絕依據**，
    不是缺證據。

    這個事實由 host 注入，呼叫端不得提供：selection eligibility 用它決定能不能
    選用 candidate。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        core_proof_resolution as PR,
        core_utilization_evidence as CU,
        method_extensions as ME,
    )

    task = live.task
    profile = controller._deps.profile_for_task(task.task_id)
    if getattr(profile, "is_qualification_fixture", False):
        return None
    try:
        dispatch = _resolve_method_extension(task, profile)
    except ME.MethodExtensionError:
        return None
    if dispatch is None or ME.host_evidence_set(dispatch.extension) != (
            "core_utilization"):
        return None
    extension = dispatch.extension
    if "formal_equivalence" not in (extension.required_proof_kinds or ()):
        return None
    descriptor = ME.formal_proof_descriptor(extension)
    if not descriptor:
        return None
    FP = ME.formal_proof_module(extension)

    with live.lock:
        run_facts = dict(getattr(live, "run_evidence", None) or {})
        reference = getattr(live, "verification_reference", None)
    baseline_netlist = _reference_role_sha256(reference, "final_netlist")
    candidate_facts = dict(run_facts.get(F.SLOT_CANDIDATE) or {})
    by_role = _artifact_manifest_by_role(
        profile, candidate_facts.get("artifact_manifest_id"))
    candidate_netlist = str((by_role.get("orfs.final_netlist") or {}).get(
        "sha256") or "")
    if not baseline_netlist or not candidate_netlist:
        return {"required": True, "status": "MISSING",
                "reason": "the netlist identity of both sides is not established"}
    try:
        work_id, attempt_id = _work_attempt_identity(
            dict(getattr(task, "eap_proposal", None) or {}))
    except WorkIdentityConflict as exc:
        return {"required": True, "status": "MISSING", "reason": str(exc)}
    if not work_id or not attempt_id:
        return {"required": True, "status": "MISSING",
                "reason": "the CORE task carries no Work/attempt identity"}

    writer = getattr(controller._deps.step_deps, "writer", None)
    store = getattr(writer, "event_store", None)
    if store is None:
        return {"required": True, "status": "MISSING",
                "reason": "no durable event store is available to resolve the proof"}
    context = controller._deps.step_deps.request_context()
    try:
        envelope, qualification_only = PR.resolve_pair_proof(
            PR.step_result_payloads(store, member_id=context.member_id,
                                    event_type=F.EVENT_STEP_RESULT),
            work_id=work_id, attempt_id=attempt_id,
            descriptor_sha256=FP.descriptor_digest(),
            baseline_netlist_sha256=baseline_netlist,
            candidate_netlist_sha256=candidate_netlist)
    except PR.PairProofResolutionError as exc:
        return {"required": True, "status": "MISSING", "reason": exc.reason}

    try:
        expected_tool = _expected_formal_tool(extension, descriptor, profile)
    except W.FlowStopped as exc:
        return {"required": True, "status": "UNVERIFIABLE", "reason": str(exc)}
    try:
        CU.verify_pair_proof_envelope(
            envelope, work_id=work_id, attempt_id=attempt_id,
            expected_descriptor_sha256=FP.descriptor_digest(),
            baseline_netlist_sha256=baseline_netlist,
            candidate_netlist_sha256=candidate_netlist,
            expected_tool=expected_tool, qualification_only=qualification_only)
    except CU.CoreEvidenceUnverifiable as exc:
        return {"required": True, "status": "UNVERIFIABLE", "reason": exc.reason}
    return {"required": True, "status": "VERIFIED",
            "classification": str(envelope.get("classification") or ""),
            "durable_envelope_sha256": envelope.get("durable_envelope_sha256")}


def _core_production_slot_evidence(
    controller: "FlowController", live: Any, *, task: Any, profile: Any,
    dispatch: Any, reference: Any, run_facts: dict[str, Any], variant: str,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """由**durable 紀錄**組出兩側 CORE slot 證據；任一環節不成立就不放行比較。

    固定順序（每一步都 fail-closed）：

        durable proof identity → 解析 pair-proof envelope → 驗 envelope 與
        lineage → baseline slot 證據（唯讀重用 reference）→ candidate slot
        證據（這次 run）→ 兩側角色與身分核對 → proof prerequisite

    這裡不接受任何由 harness 或 user 提供的 envelope 路徑：envelope 由本次
    Work／attempt 與兩側 netlist hash 從 EventStore 找回，因此 daemon 重啟後
    仍然是同一份。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        core_proof_resolution as PR,
        core_utilization_evidence as CU,
        method_extensions as ME,
    )

    extension = getattr(dispatch, "extension", None)
    descriptor = ME.formal_proof_descriptor(extension)
    if not descriptor:
        return None, "the CORE method extension pins no formal pair proof"
    FP = ME.formal_proof_module(extension)

    # ── baseline 側的權威來源：durable reference ────────────────────
    record = _production_reference(reference)
    if record is None or not record.get("reference_digest"):
        return None, "the durable baseline functional reference is unavailable"
    baseline_artifact = ((record.get("artifacts") or {}).get("final_netlist")
                         or {})
    baseline_netlist = str(baseline_artifact.get("sha256") or "")
    if not baseline_netlist:
        return None, "the baseline reference carries no final netlist identity"

    # ── candidate 側的權威來源：這次 run 的 artifact manifest ───────
    candidate_facts = dict(run_facts.get(F.SLOT_CANDIDATE) or {})
    by_role = _artifact_manifest_by_role(
        profile, candidate_facts.get("artifact_manifest_id"))
    candidate_artifact = by_role.get("orfs.final_netlist") or {}
    candidate_netlist = str(candidate_artifact.get("sha256") or "")
    if not candidate_netlist:
        return None, ("this run's artifact manifest does not attest a final "
                      "netlist for the candidate")

    proposal = dict(getattr(task, "eap_proposal", None) or {})
    try:
        work_id, attempt_id = _work_attempt_identity(proposal)
    except WorkIdentityConflict as exc:
        return None, f"the CORE task carries conflicting Work identity: {exc}"
    if not work_id or not attempt_id:
        return None, "the CORE task carries no Work/attempt identity"

    # ── 1-2. durable identity → 解析 envelope ──────────────────────
    writer = getattr(controller._deps.step_deps, "writer", None)
    store = getattr(writer, "event_store", None)
    if store is None:
        return None, "no durable event store is available to resolve the proof"
    context = controller._deps.step_deps.request_context()
    try:
        envelope, qualification_only = PR.resolve_pair_proof(
            PR.step_result_payloads(store, member_id=context.member_id,
                                    event_type=F.EVENT_STEP_RESULT),
            work_id=work_id, attempt_id=attempt_id,
            descriptor_sha256=FP.descriptor_digest(),
            baseline_netlist_sha256=baseline_netlist,
            candidate_netlist_sha256=candidate_netlist)
    except PR.PairProofResolutionError as exc:
        return None, exc.reason

    # ── 3. 驗 envelope 與 lineage ──────────────────────────────────
    try:
        expected_tool = _expected_formal_tool(extension, descriptor, profile)
    except W.FlowStopped as exc:
        return None, str(exc)
    try:
        binding = CU.verify_pair_proof_envelope(
            envelope, work_id=work_id, attempt_id=attempt_id,
            expected_descriptor_sha256=FP.descriptor_digest(),
            baseline_netlist_sha256=baseline_netlist,
            candidate_netlist_sha256=candidate_netlist,
            expected_tool=expected_tool, qualification_only=qualification_only)
    except CU.CoreEvidenceUnverifiable as exc:
        return None, exc.reason

    # ── 4-5. 兩側 slot 證據 ───────────────────────────────────────
    parameter = getattr(profile, "parameter", None)
    baseline_value = str(getattr(parameter, "baseline", "") or "")
    candidate_value = str(task.value_for_slot(F.SLOT_CANDIDATE))
    output_root = str(profile.slot_output_root(task.task_id, F.SLOT_CANDIDATE))
    variant_ref = _variant_ref_for(live, F.SLOT_CANDIDATE)
    candidate_root = getattr(variant_ref, "candidate_root", None)
    materialized = (Path(candidate_root) / profile.design_config
                    if candidate_root else None)
    if materialized is None or not Path(materialized).is_file():
        return None, "the materialized candidate config is not readable"
    provenance = CU.OrfsRunProvenance(
        task_id=str(candidate_facts.get("task_id") or ""),
        run_id=str(candidate_facts.get("run_id") or ""),
        grant_id=str(getattr(getattr(live, "grant", None), "grant_id", "") or ""),
        slot=F.SLOT_CANDIDATE,
        receipt_digest=str(candidate_facts.get("receipt_digest") or ""),
        manifest_digest=str(candidate_facts.get("manifest_digest") or ""),
        dqe_digest=str(candidate_facts.get("dqe_digest") or ""),
        result_event_id=str(candidate_facts.get("result_event_id") or ""),
        manifest_entries={row["path"]: row["sha256"] for row in by_role.values()})
    tools = dict(record.get("tools") or {})
    common = {
        "scripts_digest": (record.get("scripts") or {}).get("digest"),
        "image_digest": (record.get("image") or {}).get("docker_image_id"),
        "tool_digest": (tools.get("yosys") or {}).get("executable_sha256"),
        "rtl_digest": ((record.get("input") or {}).get("rtl") or {}).get("sha256"),
        "sdc_digest": ((record.get("input") or {}).get("sdc") or {}).get("sha256"),
        "corner": (record.get("baseline") or {}).get("corner") or "",
        "library": (record.get("baseline") or {}).get("library") or "",
    }
    try:
        baseline_evidence = CU.build_reused_baseline_slot_evidence(
            reference=record, extension_version=extension.extension_version,
            method_identity=extension.method_identity,
            expected_core_utilization=baseline_value, proof=binding)
        candidate_evidence = CU.build_orfs_slot_evidence(
            provenance=provenance, extension_version=extension.extension_version,
            method_identity=extension.method_identity,
            output_root=output_root, platform=profile.platform_id,
            design=profile.design_id, variant=f"{variant}_{F.SLOT_CANDIDATE}",
            materialized_config=materialized,
            expected_core_utilization=candidate_value, proof=binding, **common)
    except CU.CoreEvidenceUnverifiable as exc:
        return None, exc.reason

    # ── 6. 兩側角色與身分核對 ─────────────────────────────────────
    pair = (baseline_evidence["pair_proof"], candidate_evidence["pair_proof"])
    if pair[0]["role"] == pair[1]["role"]:
        return None, "both slots claim the same proof role"
    if pair[0]["durable_envelope_sha256"] != pair[1]["durable_envelope_sha256"]:
        return None, "the two slots reference different proof envelopes"
    for row in pair:
        if row["work_id"] != work_id or row["attempt_id"] != attempt_id:
            return None, "a slot proof reference belongs to another Work/attempt"
    for name in CU.COMMON_CONDITION_FIELDS:
        if baseline_evidence.get(name) != candidate_evidence.get(name):
            return None, (f"the two slots differ on the common condition "
                          f"{name!r}; they are not comparable")
    return {F.SLOT_BASELINE: baseline_evidence,
            F.SLOT_CANDIDATE: candidate_evidence}, None


def core_slot_evidence_root(profile: Any, task: Any, slot: str) -> Path:
    """host 衍生 CORE slot 證據（`terminux_core_utilization_{slot}.json`）的
    專屬輸出根目錄。

    刻意與 `profile.slot_output_root()`（ORFS docker 掛載給工具寫入的目錄）
    分開：candidate 側那份目錄的 `reports/`／`logs/`／`results/`／`objects/`
    由 container 以 root 身分建立（image 未做 UID 對應），daemon 進程沒有
    寫入權限，寫入會拋 `PermissionError`（真實執行曾發現這個缺陷）。

    host 唯一需要寫入的衍生證據改放在同一 `execution_root` 底下、container
    從未掛載也從未建立過的子目錄，讀寫雙方一律使用這個位置——不依賴「這次
    剛好沒被 docker 碰過」的巧合（重用 baseline 先前之所以能寫成功，正是
    巧合：本 task 沒有為 baseline 跑過真實 ORFS，`baseline/` 從未被 container
    建立過；本包把同一個「host 自己的目錄，不共用 container 掛載點」原則
    套用到兩側 slot，不再依賴這個巧合，也不再造第二套儲存架構）。
    """
    return Path(profile.execution_root) / task.task_id / "core_evidence" / slot


def core_slot_evidence_location(
    profile: Any, task: Any, slot: str, *, variant: str,
) -> tuple[str, str]:
    """CORE slot 證據的（輸出根目錄, variant）；兩側 slot、重用或這次收集
    的 baseline 皆同一規則，見 `core_slot_evidence_root()`。"""
    return str(core_slot_evidence_root(profile, task, slot)), f"{variant}_{slot}"


def _write_core_slot_evidence(profile: Any, task: Any, *, variant: str,
                              slot: str, payload: dict[str, Any],
                              output_root: str) -> dict[str, str]:
    """把 slot 證據寫進**本 slot 自己的**輸出區，位置與 host reader 一致。"""
    from terminux_engine.packages.orfs_run_intelligence import (
        core_utilization_evidence as CU,
    )

    reports = (Path(output_root) / "reports" / profile.platform_id
               / profile.design_id / variant)
    reports.mkdir(parents=True, exist_ok=True)
    target = reports / CU.EVIDENCE_FILENAME_TEMPLATE.format(slot=slot)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    # 回傳檔案身分，讓它隨 acceptance 一起進 durable step result：重啟後的
    # fresh reader 就能由 durable 紀錄找到這兩份檔案，而不是由呼叫端給路徑。
    return CU.file_identity(target)


def _artifact_manifest_by_role(profile: Any, manifest_id: str | None
                               ) -> dict[str, dict[str, str]]:
    """本次 run 的 artifact manifest：`adapter_role -> {path, sha256}`。

    只回報 `present` 的觀察。缺席的 role 不補值——CORE 的 candidate netlist
    必須真的在 manifest 裡，否則比較不放行。
    """
    if not manifest_id:
        return {}
    from terminux_engine.lineage.artifact_manifest_store import ArtifactManifestStore

    root = getattr(profile, "artifact_root", None)
    store = ArtifactManifestStore(root) if root else ArtifactManifestStore()
    by_role: dict[str, dict[str, str]] = {}
    for observation in store.load(manifest_id).observations:
        identity = observation.content_identity
        path = observation.resolved_path
        if path and identity is not None and identity.algorithm == "sha256":
            by_role[str(observation.adapter_role)] = {
                "path": str(path), "sha256": identity.digest}
    return by_role


def _build_production_reference(controller: "FlowController", live: Any, binding: Any,
                                *, evidence: Any, result_event_id: str | None,
                                result_sha256: str | None,
                                existing: dict[str, Any] | None) -> dict[str, Any]:
    """由本次 baseline run 的真實輸出組出 Package 的 production reference。"""
    from terminux_engine.packages.orfs_run_intelligence import (
        baseline_reference as BR,
    )
    from terminux_engine.packages.orfs_run_intelligence import (
        method_extensions as ME,
    )

    task = live.task
    profile = controller._deps.profile_for_task(task.task_id)
    extension = ME.resolve_declared_identity(
        profile.declared_method_identity,
        host_native=bool(getattr(profile, "is_native", False)))
    run_evidence = evidence.to_json_dict() if hasattr(evidence, "to_json_dict") else {}
    reference_dir = (Path(profile.execution_root) / binding.task_id
                     / "verification_reference")
    design_config = str(profile.design_config)
    record = BR.build_reference(BR.ReferenceInputs(
        method_identity=extension.method_identity,
        extension_version=extension.extension_version,
        task_id=task.task_id, task_digest=task.task_digest,
        run_id=run_evidence.get("run_id") or "", slot=F.SLOT_BASELINE,
        profile_id=profile.profile_id, profile_digest=profile.profile_digest,
        input_snapshot_root=profile.input_snapshot_root,
        input_manifest_sha256=profile.input_manifest_sha256,
        input_tree_hash=task.baseline_input_tree_hash,
        design_config_relpath=design_config,
        rtl_relpath=_design_source_relpath(profile, "rtl"),
        sdc_relpath=_design_source_relpath(profile, "sdc"),
        output_root=binding.output_root, platform_id=binding.platform_id,
        design_id=binding.design_id, variant=binding.variant,
        docker_image_id=binding.docker_image_id,
        native_tool_binding_sha256=getattr(binding, "native_tool_binding_sha256", None),
        reference_dir=str(reference_dir),
        artifact_manifest_entries=_artifact_manifest_entries(
            profile, run_evidence.get("artifact_manifest_id")),
        required_proof_kinds=tuple(extension.required_proof_kinds),
        artifact_role_mapping=(
            profile.execution.artifact_role_mapping.to_json_dict()
            if getattr(getattr(profile, "execution", None),
                       "artifact_role_mapping", None) is not None else None),
        run_identity={**run_evidence,
                      "dqe_dispatch_key": run_evidence.get("dqe_dispatch_key"),
                      "result_event_id": result_event_id,
                      "result_sha256": result_sha256},
        qualification_fixture=bool(
            getattr(profile, "is_qualification_fixture", False))))
    merged = dict(existing or {})
    final = record["artifacts"]["final_netlist"]
    merged.update({
        "kind": BR.REFERENCE_KIND,
        "reference_dir": str(reference_dir),
        "reference_path": final["reference_path"],
        "sha256": final["sha256"],
        "production_reference": record,
        "production_reference_path": record["record_path"],
    })
    return merged


def _record_reference_stop(controller: "FlowController", live: Any, binding: Any,
                           exc: BaseException) -> None:
    """reference 沒成立時，把停止碼如實留在 task-owned reference 區與 log。

    只記錄，不補造、也不改寫 run 的事實：run 仍然是它本來的樣子，reference
    單純沒有成立。
    """
    import json

    code = getattr(exc, "code", None) or type(exc).__name__
    _LOG.warning("baseline functional reference not produced (%s): %r", code, exc)
    try:
        profile = controller._deps.profile_for_task(live.task.task_id)
        target = (Path(profile.execution_root) / binding.task_id
                  / "verification_reference")
        target.mkdir(parents=True, exist_ok=True)
        (target / "terminux_baseline_reference_stop.json").write_text(
            json.dumps({"code": code, "detail": str(exc),
                        "task_id": binding.task_id, "slot": binding.slot},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 診斷檔寫不出來不得影響 run 的事實
        _LOG.warning("the baseline reference stop reason could not be recorded")


def _platform_cell_libraries(profile: Any) -> list[dict[str, str]]:
    """由 platform config 自己宣告的 `LIB_FILES` 取出標準元件庫。

    gate-level netlist 的等價性證明需要元件的功能模型；檔名不由 host 猜，
    一律讀 platform config 的宣告，逐檔算 sha256 後釘進 proof 身分。
    解析不出任何一份就回空清單，由呼叫端 fail closed——不得改用空 module、
    blackbox 或忽略未解析元件的方式讓 proof 通過。
    """
    import re

    root = Path(profile.input_snapshot_root)
    platform_dir = f"flow/platforms/{profile.platform_id}"
    config = root / platform_dir / "config.mk"
    if not config.is_file():
        return []
    text = config.read_text(errors="replace")
    match = re.search(r"^\s*export\s+LIB_FILES\s*\??=\s*(.+?)(?<!\\)$",
                      text, re.MULTILINE | re.DOTALL)
    if match is None:
        return []
    declared = match.group(1).replace("\\\n", " ").split()
    found: list[dict[str, str]] = []
    for raw in declared:
        value = raw.replace("$(PLATFORM_DIR)", platform_dir)
        if "$" in value:
            continue
        path = (root / value).resolve()
        if not path.is_file():
            continue
        found.append({"role": "cell_library", "path": str(path),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return found


def _design_source_relpath(profile: Any, kind: str) -> str:
    """由已核准 design config 讀出 RTL／SDC 的 snapshot 相對路徑。

    值來自 config 自己的 `VERILOG_FILES`／`SDC_FILE` 賦值，不由 host 猜檔名。
    """
    import re

    root = Path(profile.input_snapshot_root)
    config = root / str(profile.design_config)
    text = config.read_text(errors="replace")
    name = "VERILOG_FILES" if kind == "rtl" else "SDC_FILE"
    match = re.search(rf"^\s*export\s+{name}\s*\??=\s*(.+)$", text, re.MULTILINE)
    if match is None:
        raise FileNotFoundError(f"{name} is not declared in {config}")
    value = match.group(1).strip().split()[0]
    value = value.replace("$(DESIGN_HOME)", "flow/designs")
    value = value.replace("$(PLATFORM)", str(profile.platform_id))
    value = value.replace("$(DESIGN_NAME)", str(profile.design_id))
    value = value.replace("./", "")
    if "$" in value:
        raise FileNotFoundError(f"{name} is not a resolvable snapshot path: {value}")
    return value


def _derive_verification_reference(controller: "FlowController", live: Any,
                                   binding: Any) -> dict[str, Any] | None:
    """把 baseline 的 final LEC netlist 複製到本 task 的唯讀 reference 區。

    來源是 baseline **自己的** 輸出區；host 記下 bytes/hash，s4 派送前再驗
    一次。它是從 s1 派生的驗證輸入，**不是** client／provider 可指定的路徑，
    也不會被混進 candidate 的設計輸入清單。
    """
    import hashlib
    import shutil

    source = (Path(binding.output_root) / "results" / binding.platform_id
              / binding.design_id / binding.variant / "6_final_lec.v")
    if not source.is_file():
        raise FileNotFoundError(f"baseline final LEC netlist not found: {source}")
    profile = controller._deps.profile_for_task(live.task.task_id)
    reference_dir = (Path(profile.execution_root) / binding.task_id
                     / "verification_reference")
    reference_dir.mkdir(parents=True, exist_ok=True)
    target = reference_dir / "baseline_final_lec.v"
    shutil.copyfile(source, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    extra: dict[str, Any] = {}
    profile_uses_setup_margin = (
        getattr(getattr(getattr(profile, "repair_recipe", None), "method", None),
                "variable", None) == "SETUP_SLACK_MARGIN")
    if task_uses_setup_margin(live.task) or profile_uses_setup_margin:
        # parametric setup repair 的等價一比的是兩側 **post-CTS** netlist（方法在 CTS 被消費），
        # 所以 baseline 這一份也要進同一個唯讀 reference 區。
        #
        # 取的是**本 task 自己的 POST_CTS hook 寫出來的那一份**，不是 ORFS 的
        # `4_after_rsz_lec.v`：後者寫在 `repair_timing_helper` 的正下方，而
        # hook 跑在 CTS 尾端，兩者之間工具還會再動 netlist。用不同時點的兩份
        # 去比，flop 名稱集合會不一致，miter 會如實 fail closed 成
        # `state_boundary_rejected`（attempt5 就是這樣）。兩側取同一個 hook 點
        # 才是對稱的比較。
        cts_source = source.with_name("terminux_r8_post_cts_lec.v")
        if not cts_source.is_file():
            raise FileNotFoundError(
                f"baseline post-CTS LEC netlist not found: {cts_source}")
        cts_target = reference_dir / "baseline_post_cts_lec.v"
        shutil.copyfile(cts_source, cts_target)
        extra = {
            "post_cts_source_path": str(cts_source),
            "post_cts_reference_path": str(cts_target),
            "post_cts_sha256":
                hashlib.sha256(cts_target.read_bytes()).hexdigest(),
            "post_cts_bytes": cts_target.stat().st_size,
        }
    if task_uses_exact_geometry(live.task):
        # v5／v0.8 的等價一比的是兩側 post-resize netlist，所以 baseline 這一份
        # 也要進同一個唯讀 reference 區。缺就是缺，不用 final 頂替。
        pr_source = source.with_name("terminux_r5_post_resize_lec.v")
        if not pr_source.is_file():
            raise FileNotFoundError(
                f"baseline post-resize LEC netlist not found: {pr_source}")
        pr_target = reference_dir / "baseline_post_resize_lec.v"
        shutil.copyfile(pr_source, pr_target)
        extra = {
            "post_resize_source_path": str(pr_source),
            "post_resize_reference_path": str(pr_target),
            "post_resize_sha256":
                hashlib.sha256(pr_target.read_bytes()).hexdigest(),
            "post_resize_bytes": pr_target.stat().st_size,
        }
    # qualification 的工具身分檔也放進同一個唯讀 reference 區。
    # 它是 host 提供的 proof input（由 profile 的固定路徑指定），不是
    # client／provider 可指定的值，也不是容器自己產生的——否則就變成自我認證。
    recipe = getattr(live.task, "repair_recipe", None) or {}
    identity_source = recipe.get("qualified_tool_identity_path")
    if identity_source:
        identity_path = Path(identity_source)
        if not identity_path.is_file():
            raise FileNotFoundError(
                f"qualified tool identity file not found: {identity_path}")
        identity_target = reference_dir / "qualified_tool_identity.json"
        shutil.copyfile(identity_path, identity_target)
        declared_identity = recipe.get("qualified_tool_identity_sha256")
        if declared_identity is not None and hashlib.sha256(
                identity_target.read_bytes()).hexdigest() != declared_identity:
            # RC-4：Profile 已綁定身分檔 hash；不符就不建立 proof reference。
            identity_target.unlink()
            raise ValueError(
                "the qualified tool identity does not match the Profile hash")
        extra = {
            **extra,
            "qualified_tool_identity_path": str(identity_target),
            "qualified_tool_identity_sha256":
                hashlib.sha256(identity_target.read_bytes()).hexdigest(),
        }

    return {
        **extra,
        "kind": "baseline_final_netlist",
        "producer_run_id": live.runs[F.SLOT_BASELINE].run_id,
        "producer_slot": F.SLOT_BASELINE,
        "producer_stage": "final",
        "source_path": str(source),
        "reference_dir": str(reference_dir),
        "reference_path": str(target),
        "sha256": digest,
        "bytes": target.stat().st_size,
    }


def _run_selection(controller: "FlowController", live: Any, *,
                   current_slot: str | None = None,
                   current_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """依固定 policy 判定每個候選的資格並排序。

    輸入全部是**已持久化的 deterministic 結果**：各 compare step 的
    `metric_comparisons` 與 `selection_eligibility`、各 EDA step 的
    `parameter_consumption.observed.core_area_um2`。不讀 LLM 敘述。
    """
    from terminux_engine.packages.orfs_run_intelligence import selection as SEL

    task = live.task
    policy = task.selection_policy or {}
    limit = float(policy.get("area_ratio_limit", 1.75))

    facts = _selection_facts(live, current_slot, current_facts)
    baseline = facts.get(F.SLOT_BASELINE, {})
    assessments = []
    for entry in task.candidate_plan:
        slot = str(entry["slot"])
        slot_facts = facts.get(slot, {})
        run_ref = live.runs.get(slot)
        ref = _variant_ref_for(live, slot)
        identity_ok = run_ref is not None and ref is not None
        assessments.append(SEL.assess_candidate(
            slot=slot, parameter_value=str(entry["value"]),
            run_id=run_ref.run_id if run_ref is not None else "",
            input_manifest_sha256=(ref.manifest_sha256 if ref is not None else ""),
            order_index=int(entry.get("order_index", 0)),
            eligibility=slot_facts.get("eligibility"),
            metric_comparisons=slot_facts.get("metric_comparisons") or (),
            baseline_core_area_um2=baseline.get("core_area_um2"),
            candidate_core_area_um2=slot_facts.get("core_area_um2"),
            area_ratio_limit=limit,
            identity_ok=identity_ok,
            identity_reason=(None if identity_ok else
                             "the run or candidate input reference for this slot "
                             "is missing"),
        ))

    baseline_run = live.runs.get(F.SLOT_BASELINE)
    result = SEL.select_best(
        baseline_slot=F.SLOT_BASELINE,
        baseline_run_id=baseline_run.run_id if baseline_run is not None else "",
        baseline_core_area_um2=baseline.get("core_area_um2"),
        baseline_setup=baseline.get("setup") or {},
        assessments=tuple(assessments), area_ratio_limit=limit)
    payload = result.to_json_dict()
    payload["retained"] = _retained_references(controller, live, result.selected_slot)
    return payload


def _selection_facts(live: Any, current_slot: str | None = None,
                     current_facts: dict[str, Any] | None = None
                     ) -> dict[str, dict[str, Any]]:
    """由已落盤的 step state 取出 selector 需要的 deterministic 事實。

    最後一步的事實還沒寫回 live state，因此由呼叫端直接帶進來。
    """
    facts: dict[str, dict[str, Any]] = {}
    task = live.task
    with live.lock:
        states = {k: dict(v) for k, v in live.steps.items()}
    if current_slot and current_facts:
        states = dict(states)
        states["__current__"] = {**current_facts, "__slot__": current_slot,
                                 "__capability__": CAP_COMPARE_RUNS}

    for step_id, state in states.items():
        capability = (state.get("__capability__")
                      or _capabilities_for(task.template_id).get(step_id))
        slot = state.get("__slot__") or (
            _slot_for_step(task, step_id) if step_id in
            _capabilities_for(task.template_id) else None)
        if slot is None:
            continue
        if capability == CAP_RUN_FLOW:
            if state.get("core_area_um2") is not None:
                facts.setdefault(slot, {})["core_area_um2"] = state["core_area_um2"]
        elif capability == CAP_COMPARE_RUNS:
            comparisons = state.get("metric_comparisons") or ()
            entry = facts.setdefault(slot, {})
            entry["metric_comparisons"] = comparisons
            entry["eligibility"] = state.get("selection_eligibility")
            # baseline 的 setup 數值由同一份逐指標比較取得（同單位、同精度）。
            base = facts.setdefault(F.SLOT_BASELINE, {})
            base.setdefault("setup", {})
            for item in comparisons:
                row = item if isinstance(item, dict) else item.to_json_dict()
                if row.get("metric_id") in SEL_SETUP_METRICS:
                    base["setup"].setdefault(
                        row["metric_id"], row.get("baseline_value"))
    return facts


def _retained_references(controller: "FlowController", live: Any,
                        selected_slot: str) -> dict[str, Any]:
    """被選結果與所有 slot 的保留位置。指向可用的保留資料，不是暫存目錄。

    **baseline 也要有完整的 `retained_input_ref`**。
    baseline 用的是 profile 的固定 snapshot，沒有 `VariantInputRef`；因此由
    profile 已持久化的引用重建同一個格式，並標 `kind=snapshot`／
    `derived=true`。只有在引用與 bytes/hash 都核對得過時才回傳；核對不過就
    如實 `unavailable`，**不補造**。
    """
    import hashlib

    retained: dict[str, Any] = {"selected_slot": selected_slot, "slots": {}}
    profile = controller._deps.profile_for_task(live.task.task_id)
    for slot, run_ref in live.runs.items():
        ref = _variant_ref_for(live, slot)
        if ref is not None:
            entry = {
                "kind": "variant", "derived": False,
                "run_id": run_ref.run_id,
                "input_root": ref.candidate_root,
                "input_manifest_path": ref.manifest_path,
                "input_manifest_sha256": ref.manifest_sha256,
                "input_tree_hash": ref.tree_hash,
                "config_sha256": ref.config_sha256,
                "scope": {"member_id": live.task.scope.member_id,
                          "project_id": live.task.scope.project_id},
                "source_task_id": live.task.task_id, "source_slot": slot,
            }
        else:
            entry = _baseline_retained_ref(profile, live, slot, run_ref, hashlib)
            baseline_import = getattr(live, "baseline_import", None) or {}
            if slot == F.SLOT_BASELINE and baseline_import:
                entry.update({
                    "source_task_id": baseline_import["source_task_id"],
                    "source_task_digest": baseline_import["source_task_digest"],
                    "source_result_event_id": baseline_import.get("result_event_id"),
                    "source_result_sha256": baseline_import.get("result_sha256"),
                    "baseline_import_digest": baseline_import.get("import_digest"),
                })
        retained["slots"][slot] = entry
    return retained


def _baseline_retained_ref(profile: Any, live: Any, slot: str, run_ref: Any,
                           hashlib: Any) -> dict[str, Any]:
    """由 profile 已持久化的 snapshot 引用重建 baseline 的保留身分。"""
    entry: dict[str, Any] = {
        "kind": "snapshot", "derived": True,
        "run_id": run_ref.run_id if run_ref is not None else None,
        "scope": {"member_id": live.task.scope.member_id,
                  "project_id": live.task.scope.project_id},
        "source_task_id": live.task.task_id, "source_slot": slot,
        "input_root": None, "input_manifest_path": None,
        "input_manifest_sha256": None, "input_tree_hash": None,
        "config_sha256": None,
    }
    path = Path(profile.input_manifest_path)
    if not path.is_file():
        entry["availability"] = "unavailable: the bound input manifest is missing"
        return entry
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != profile.input_manifest_sha256:
        entry["availability"] = (
            "unavailable: the input manifest no longer matches its bound hash")
        return entry
    entry.update({
        "input_root": profile.input_snapshot_root,
        "input_manifest_path": str(path),
        "input_manifest_sha256": actual,
        "input_tree_hash": live.task.baseline_input_tree_hash,
        "availability": "available",
    })
    return entry
