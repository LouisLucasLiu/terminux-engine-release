# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""受 Package 宣告驅動的單工具 FlowTask；共用 grant、reader 與 Kernel。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import flow_models as F
from terminux_engine.bounded_execution.analysis_tasks import flow_worker as W
from terminux_engine.bounded_execution.analysis_tasks import models as M
from terminux_engine.bounded_execution.analysis_tasks.canonical import monotonic_s
from terminux_engine.expert_ai.eap3 import digest_json
from terminux_engine.orchestration import legacy_docker_execution as LDE
from terminux_engine.package_runtime.approved_local_tool import ApprovedLocalToolAdapter


#: 這一次 create request 的 durable 身分 schema。
CREATE_IDENTITY_SCHEMA = "flow_task.create_identity.v1"


def flow_task_create_identity(
    proposal: dict[str, Any], *, profile_id: str, profile_digest: str,
    member_id: str, project_id: str,
) -> dict[str, Any]:
    """同一份 create request 的 **durable** 身分。

    分成兩層，兩層都只由 request 與被選用 Profile 的 canonical 內容推導，
    因此同一個 request 在重啟後、在另一個 process、在並行送達時完全一致：

    - `key`：這是「哪一次 create request」——Work、attempt、draft、Package、
      Profile 與 scope。它推導 task-created event 的 event id；
      `session_events.event_id` 是 PRIMARY KEY，所以 append 本身就是
      compare-and-append，沒有先查後寫的競爭窗口。
    - `content`：這次 request 的 canonical 內容——已核准 proposal digest、
      package tool、輸入身分、Profile digest，以及 formal path 的 descriptor、
      method、兩份 netlist 與 input manifest。同一個 `key` 配上不同的
      `content` 是 input drift，必須 fail closed，不得建立第二個 task。

    刻意**不**綁 task-generated 的 `proof_plan_digest`：那份 digest 含
    `task_id`，而 `task_id` 正是要由這個身分決定歸屬的東西（循環）。改為綁
    proof plan 的 canonical **材料**——descriptor、method、Package、兩個
    netlist 與 input manifest——它們共同唯一決定 plan 的內容。
    """
    tool = proposal.get("package_tool") or {}
    package = proposal.get("package") or {}
    source = proposal.get("source") or {}
    work = proposal.get("work_source") or {}
    formal = tool.get("formal_pair") if isinstance(tool.get("formal_pair"), dict) else None
    # single_instance_eco 的正式入口——與 `formal` 同一種「Package-owned
    # 專屬輸入格式，不硬塞進下面 generic candidate 編譯器」的處理方式。
    swap = tool.get("single_instance_eco") if isinstance(
        tool.get("single_instance_eco"), dict) else None
    key = {
        "schema_version": CREATE_IDENTITY_SCHEMA,
        "runner": "package_single_tool",
        "work_id": (work.get("work_id") or (formal or {}).get("work_id")
                    or (swap or {}).get("work_id")),
        "attempt_id": (work.get("attempt_id") or (formal or {}).get("attempt_id")
                       or (swap or {}).get("attempt_id")),
        "draft_id": source.get("draft_id"),
        "package_id": package.get("package_id"),
        "method": package.get("method"),
        "profile_id": profile_id,
        "member_id": member_id,
        "project_id": project_id,
    }
    content: dict[str, Any] = {
        "approved_proposal_digest": source.get("proposal_digest"),
        "canonical_proposal_digest": digest_json(proposal.get("canonical_proposal")),
        "package_identity": package,
        "package_tool_digest": digest_json(tool),
        "input": proposal.get("input"),
        "profile_digest": profile_digest,
    }
    if formal is not None:
        FP = _formal_module(formal)
        extension = formal.get("method_extension") or {}
        plan_material = {
            "proof_descriptor_sha256": FP.descriptor_digest(),
            "package_identity": formal.get("package_identity"),
            "method_extension_sha256": extension.get("descriptor_sha256"),
            "baseline": formal.get("baseline"), "candidate": formal.get("candidate"),
            "input_manifest": formal.get("input_manifest"),
            "top_module": formal.get("top_module"),
            # host-native proof 的工具身分是 plan 材料的一部分；container 版本
            # 的工具身分已在 descriptor 內，材料維持原樣。
            **({"native_tool": formal["native_tool"]} if "native_tool" in formal else {}),
        }
        content["formal_pair"] = {
            **plan_material,
            "method_identity": extension.get("method_identity"),
            "method_extension_version": extension.get("extension_version"),
            "proof_plan_material_sha256": digest_json(plan_material),
        }
    if swap is not None:
        # `repair_recipe` 是正式資料來源（`eap_extension.py::compile_plan()`
        # 已經為 single_instance_eco 產生的宣告式 dispatch payload，single-instance ECO
        # 先前新增）；這裡只綁它的內容，不另外發明第二套「這次要換什麼」
        # 的描述——與 `formal` 綁 `formal.get("baseline")`／`candidate` 等
        # canonical 材料同一個原則。
        recipe = swap.get("repair_recipe") or {}
        plan_material = {
            "method_identity": recipe.get("method_identity"),
            "target_instance": recipe.get("target_instance"),
            "expected_cell": recipe.get("expected_cell"),
            "replacement_cell": recipe.get("replacement_cell"),
            "library": recipe.get("library"), "stage": recipe.get("stage"),
            "source_candidate": recipe.get("source_candidate"),
            "docker_image": swap.get("docker_image"),
            # host-native 的工具身分（OpenROAD ＋ 元件庫 pin）；container 版本的
            # 材料維持原樣。
            **({"native_tool_binding_sha256": _native_swap_binding_sha256(swap)}
               if "native_tool" in swap else {}),
        }
        content["single_instance_eco"] = {
            **plan_material,
            "swap_plan_material_sha256": digest_json(plan_material),
        }
    return {"schema_version": CREATE_IDENTITY_SCHEMA, "key": key, "content": content,
            "key_digest": digest_json(key), "content_digest": digest_json(content)}


def create_identity_event_id(identity: dict[str, Any]) -> str:
    """由 create identity 的 key 推導 task-created event 的 durable event id。"""
    return "ftcreate" + str(identity["key_digest"])[:56]


def create_identity_task_id(identity: dict[str, Any]) -> str:
    """由 create identity 的 key 推導 FlowTask id。

    並行的兩個 create 必須組出**逐位元相同**的草案，否則它們會帶著不同的
    `task_id` 去搶同一個 durable 位置：EventStore 那一側會由 PRIMARY KEY 擋下，
    但更早的帳本 reservation（idempotency key 同樣由這個身分推導）會先因為
    identity 不一致而拒絕。task id 本身由身分決定，這個落差就不存在。
    """
    return "ftask_" + str(identity["key_digest"])[:32]


def _validate_proposal(proposal: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from terminux_engine.bounded_execution.analysis_tasks.flow_controller import FlowDraftError

    tool = proposal.get("package_tool")
    source = proposal.get("source")
    canonical = proposal.get("canonical_proposal")
    input_data = proposal.get("input")
    if (not isinstance(tool, dict) or not isinstance(source, dict)
            or not isinstance(canonical, dict) or not isinstance(input_data, dict)
            or source.get("proposal_digest") != digest_json(canonical)):
        raise FlowDraftError("TASK_INPUT_INVALID", "Package proposal lineage is incomplete")
    # Package-owned formal proof input uses the pre-existing formal task schema;
    # it has no candidate materialization and therefore must not be forced into
    # the generic JSON-candidate compiler below.
    formal = tool.get("formal_pair")
    if isinstance(formal, dict):
        required = {"work_id", "attempt_id", "package_identity", "method_extension",
                    "baseline", "candidate", "input_manifest", "top_module"}
        # 工具邊界恰好擇一：container 契約帶 `docker_executable`，host-native
        # 契約帶 Profile 授權的 `native_tool`（realpath ＋ SHA-256）。
        boundary = {"docker_executable", "native_tool"} & set(formal)
        if len(boundary) != 1:
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 "Formal proof proposal must declare exactly one tool boundary")
        if boundary == {"docker_executable"}:
            raise _legacy_docker_draft_error(LDE.PATH_FORMAL_PAIR)
        required |= boundary
        # `production_proof` 是唯一的可選鍵：它把這次 proof 標成某個 CORE
        # Work／attempt 底下的**相依** production proof，而不是 Phase B／C 那種
        # 獨立的 qualification。其餘鍵集仍然嚴格相等，不接受任何其他擴充。
        # `cell_libraries` 是 gate-level 等價性證明的必要輸入（標準元件的
        # 功能模型）。它由 CORE 的 intent 依 platform 宣告解析後帶進來，
        # 內容與版本進 proof_plan_digest。
        optional = {"production_proof", "cell_libraries"}
        if not required <= set(formal) or set(formal) - required - optional:
            raise FlowDraftError("TASK_INPUT_INVALID", "Formal proof proposal is incomplete")
        if "production_proof" in formal and not isinstance(
                formal["production_proof"], bool):
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 "production_proof must be a boolean")
        return tool, input_data
    # Package-owned single_instance_eco（single-instance ECO 有界 ECO swap）輸入——與
    # `formal` 同一個原則：專屬 task schema，不強塞進下面 generic
    # candidate 編譯器（那個編譯器假設的是「一段可 materialize 的候選文字
    # 內容」，不是「載入既有 ODB、換一個 instance、獨立讀回」這種形狀）。
    swap = tool.get("single_instance_eco")
    if isinstance(swap, dict):
        from terminux_engine.packages.orfs_run_intelligence import (
            single_instance_eco as SIE,
            single_instance_eco_host as SIEH,
        )

        # host 綁定欄位全部由 `single_instance_eco_host.verify_native_source_candidate()`
        # 產生；`work_id`／`attempt_id` 由 Work runtime 的 `work_source` 提供，
        # 這裡只接受舊形狀的相容欄位，不要求 compile_plan 預先知道 attempt。
        if "native_tool" not in swap:
            # container 綁定在任何主機核驗（讀 ODB、查 image）之前就拒絕。
            raise _legacy_docker_draft_error(LDE.PATH_SINGLE_INSTANCE_ECO)
        host_bound = {
            "verified_pre_swap_snapshot", "native_tool", "lib_files", "flow_dir",
            "platform_dir", "source_results_dir", "source_design_file",
            "source_sdc_file", "source_odb_host_path", "source_odb_sha256",
        }
        required = {"repair_recipe", *host_bound}
        optional = {"work_id", "attempt_id", "num_cores"}
        if not required <= set(swap) or set(swap) - required - optional:
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 "Single instance ECO proposal is incomplete")
        recipe = swap["repair_recipe"]
        # `repair_recipe` 是正式資料來源（`eap_extension.py::compile_plan()`）
        # ——這裡只核對它真的長那個樣子、身分對得上，不重新解讀語意。
        if (not isinstance(recipe, dict)
                or recipe.get("method_identity") != SIE.METHOD_ID
                or recipe.get("operation") != SIE.MATERIALIZE_OPERATION
                or not isinstance(recipe.get("target_instance"), str)
                or not recipe.get("target_instance")
                or recipe.get("expected_cell") not in SIE.NON_CLOCK_BUFFER_FAMILY_SET
                or recipe.get("replacement_cell") not in SIE.NON_CLOCK_BUFFER_FAMILY_SET
                or recipe.get("library") not in SIE.SUPPORTED_LIBRARIES
                or recipe.get("stage") not in SIE.SUPPORTED_STAGES):
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 "Single instance ECO repair_recipe is invalid")
        changes = canonical.get("changes") or {}
        if any(recipe.get(key) != changes.get(key) for key in (
                "target_instance", "expected_cell", "replacement_cell", "library",
                "stage", "source_candidate")):
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 "Single instance ECO recipe differs from the proposal")
        # 每次建 draft／grant 前都由 host 重新核驗來源與 snapshot：不信任
        # 計畫裡攜帶的 snapshot，只接受與 host 此刻重算結果逐欄相同的內容。
        # 計畫的執行邊界必須就是這台主機目前設定的那一種。
        try:
            if SIEH.configured_binding_mode() != SIEH.BINDING_MODE_HOST_NATIVE:
                raise FlowDraftError(
                    "TASK_INPUT_INVALID",
                    "Single instance ECO plan was compiled for another execution mode "
                    "than this host's ECO binding")
            verified = SIEH.verify_native_source_candidate(
                changes=recipe, binding=SIEH.load_native_host_binding())
        except SIEH.SourceVerificationError as exc:
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 f"Single instance ECO source verification failed: "
                                 f"{exc.code}") from exc
        if any(swap.get(key) != verified.get(key) for key in host_bound):
            raise FlowDraftError("TASK_INPUT_INVALID",
                                 "Single instance ECO host binding differs from "
                                 "the host-verified source evidence")
        return tool, input_data
    pointer = tool.get("candidate_pointer")
    candidate: Any = canonical
    if not isinstance(pointer, list) or not pointer or any(
            not isinstance(part, str) or not part for part in pointer):
        raise FlowDraftError("TASK_INPUT_INVALID", "Package candidate pointer is invalid")
    for part in pointer:
        if not isinstance(candidate, dict) or part not in candidate:
            raise FlowDraftError("TASK_INPUT_INVALID", "Package candidate pointer is missing")
        candidate = candidate[part]
    content = tool.get("materialized_content")
    relative = tool.get("materialized_relative_path")
    if (not isinstance(candidate, str) or candidate != tool.get("candidate_value")
            or not isinstance(content, str) or len(content.encode("utf-8")) > 65536
            or not isinstance(relative, str) or Path(relative).is_absolute()
            or ".." in Path(relative).parts or not relative
            or json.loads(content) != canonical
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != tool.get("materialized_sha256")):
        raise FlowDraftError("TASK_INPUT_INVALID", "Package materialization differs from proposal")
    ApprovedLocalToolAdapter(
        executable=tool["executable"],
        executable_sha256=tool["executable_sha256"],
        argument_template=tool["argument_template"],
        materialized_sha256=tool["materialized_sha256"],
        artifact_paths=tool["artifact_paths"],
        candidate_value=tool["candidate_value"])
    manifest_path = Path(str(input_data.get("manifest_path") or ""))
    snapshot_root = Path(str(input_data.get("snapshot_root") or ""))
    if (not manifest_path.is_absolute() or not snapshot_root.is_absolute()
            or not manifest_path.is_relative_to(snapshot_root)
            or not manifest_path.is_file() or not snapshot_root.is_dir()
            or hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            != input_data.get("manifest_digest")):
        raise FlowDraftError("TASK_INPUT_INVALID", "Package input manifest is invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or digest_json(files) != input_data.get("tree_hash"):
        raise FlowDraftError("TASK_INPUT_INVALID", "Package input tree identity differs")
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise FlowDraftError("TASK_INPUT_INVALID", "Package input inventory is malformed")
        path = (snapshot_root / row["path"]).resolve()
        if (not path.is_relative_to(snapshot_root) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != row.get("sha256")):
            raise FlowDraftError("TASK_INPUT_INVALID", "Package input inventory changed")
    return tool, input_data


def build_package_draft(
    *, profile: Any, profile_source: Any, question: str, context: Any,
    daemon_instance_id: str, proposal: dict[str, Any],
    create_identity: dict[str, Any] | None = None,
) -> Any:
    """只保存由 Package compiler 定義且可由 host 重驗的執行內容。"""
    from terminux_engine.bounded_execution.analysis_tasks.flow_controller import (
        FlowDraft, FlowDraftError,
    )
    from terminux_engine.bounded_execution.binding import compute_arguments_sha256

    tool, input_data = _validate_proposal(proposal)
    if not profile.matches_scope(context.member_id, context.project_id):
        raise FlowDraftError("TASK_NOT_FOUND_OR_OUT_OF_SCOPE", "Profile scope differs")
    package = proposal.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("package_id"), str):
        raise FlowDraftError("TASK_INPUT_INVALID", "Package identity is missing")
    task_id = (create_identity_task_id(create_identity) if create_identity
               else F.new_flow_task_id())
    source_created = None
    if isinstance(proposal.get("source"), dict):
        source_created = proposal["source"].get("created_at_ms")
    if source_created is None and isinstance(proposal.get("work_source"), dict):
        source_created = proposal["work_source"].get("created_at_ms")
    draft_created_at_ms = int(source_created) if source_created is not None else None
    task_root = (Path(profile.execution_root) / task_id).resolve()
    authorized_roots = [Path(item).resolve() for item in (
        proposal.get("authorization_writable_roots") or [])
                        if isinstance(item, str) and Path(item).is_absolute()]
    if (not task_root.is_relative_to(Path(profile.execution_root).resolve())
            or not any(task_root.is_relative_to(root) for root in authorized_roots)):
        raise FlowDraftError("TASK_INPUT_INVALID", "task root is outside Profile")
    capability_id = package["package_id"] + ".run_approved_tool"
    formal_spec = tool.get("formal_pair")
    if isinstance(formal_spec, dict):
        # `_validate_proposal()` 已拒絕 container 工具邊界；proof 只在 Profile
        # 授權的 `FORMAL_YOSYS` 上執行。
        FP = _formal_module(formal_spec)
        authorized = _profile_formal_tool(profile)
        if authorized is None or authorized != formal_spec["native_tool"]:
            raise FlowDraftError(
                "TASK_INPUT_INVALID",
                "the formal proof tool is not the FORMAL_YOSYS binary authorized by "
                "the host-native Profile")

        # task/grant/run identity 的 authoritative 值只有 daemon child 前才有。
        # DRAFT 僅保存 PENDING placeholders；adapter 的 generic host binding hook
        # 會在 ledger run id 配置後一次替換，child 無此能力。
        proof_task = FP.build_task(
            work_id=formal_spec["work_id"], attempt_id=formal_spec["attempt_id"],
            task_id=task_id, task_digest="PENDING", grant_id="PENDING", run_id="PENDING",
            package_identity=formal_spec["package_identity"],
            method_extension=formal_spec["method_extension"],
            baseline=formal_spec["baseline"], candidate=formal_spec["candidate"],
            input_manifest=formal_spec["input_manifest"], task_root=task_root,
            top_module=formal_spec["top_module"],
            cell_libraries=tuple(formal_spec.get("cell_libraries") or ()),
            tool=formal_spec["native_tool"])
        adapter_config = FP.workflow_config(
            task=proof_task, artifact_root=profile.artifact_root, step_id="s1")
        formal_tool = {"formal_pair": True, "adapter_config": adapter_config}
        arguments = {"proof_plan_digest": proof_task["proof_plan_digest"],
                     "proof_descriptor_sha256": proof_task["proof_descriptor_sha256"]}
        step = M.TaskStepSpec(
            step_id="s1", capability_id=capability_id, arguments=arguments,
            arguments_sha256=compute_arguments_sha256(arguments),
            capability_contract_sha256=digest_json({"package_id": package["package_id"],
                                                    "proof": proof_task["proof_descriptor"]}),
            catalog_snapshot_id=proof_task["proof_descriptor_sha256"], risk="tool_execution",
            approval_required=True, manifest_timeout_sec=float(FP.TIMEOUT_SECONDS))
        limits = {**profile.limits.to_json_dict(), "eda_launches": 1, "candidate_writes": 0}
        plan = {
            "schema_version": "package.flow_task_plan.v1", "runner": "package_single_tool",
            "package_id": package["package_id"], "method": package.get("method"),
            "tool": formal_tool, "task_root": str(task_root),
            "input": input_data, "artifact_root": profile.artifact_root,
            "writable_roots": [str(root) for root in authorized_roots], "budget": limits,
            # 相依的 production proof 不是 qualification 證據：它會被 CORE
            # comparison 採用，因此不得標成 qualification_only。
            "qualification_only": not bool(formal_spec.get("production_proof")),
            **({"create_identity": create_identity} if create_identity else {}),
        }
        scope = M.TaskScope(
            member_id=context.member_id, project_id=context.project_id,
            origin_session_id=context.session_id,
            workspace=getattr(context, "workspace", "") or "",
            project_source=getattr(context, "project_source", "session"))
        task = F.build_flow_task_snapshot(
            task_id=task_id, daemon_instance_id=daemon_instance_id, scope=scope,
            profile_id=profile.profile_id, profile_digest=profile.profile_digest,
            question=question, design_id=profile.design_id, platform_id=profile.platform_id,
            parameter_name="formal_equivalence", baseline_value="NOT_REQUIRED",
            candidate_value="NOT_REQUIRED", baseline_input_manifest_sha256=input_data["manifest_digest"],
            baseline_input_tree_hash=input_data["tree_hash"], expected_candidate_tree_hash=None,
            expected_candidate_config_sha256=None, task_deadline_sec=profile.limits.task_deadline_sec,
            run_timeout_sec=FP.TIMEOUT_SECONDS, limits=limits, steps=(step,),
            created_at_ms=draft_created_at_ms,
            template_id=F.PACKAGE_SINGLE_TOOL_TEMPLATE_ID, candidate_plan=(),
            profile_source=profile_source.to_json_dict(), execution_plan=plan,
            eap_proposal=proposal)
        return FlowDraft(task=task, profile=profile)
    swap_spec = tool.get("single_instance_eco")
    if isinstance(swap_spec, dict):
        from terminux_engine.packages.orfs_run_intelligence import (
            single_instance_eco as SIE,
            single_instance_eco_native as SIEN,
        )

        recipe = swap_spec["repair_recipe"]
        # task/grant/run identity 的 authoritative 值只有 daemon child 前才有
        # ——與 `formal_spec` 分支同一個 PENDING placeholder 慣例；
        # `repair_recipe` 是正式資料來源，這裡只把它的內容原樣帶進
        # `SIEN.build_task()`，不重新詮釋。`_validate_proposal()` 已拒絕
        # container 綁定。
        work_source = proposal.get("work_source") or {}
        swap_task = SIEN.build_task(
            work_id=work_source.get("work_id") or swap_spec.get("work_id"),
            attempt_id=work_source.get("attempt_id") or swap_spec.get("attempt_id"),
            task_id=task_id, task_digest="PENDING", grant_id="PENDING",
            run_id="PENDING", target_instance=recipe["target_instance"],
            expected_cell=recipe["expected_cell"],
            replacement_cell=recipe["replacement_cell"], library=recipe["library"],
            stage=recipe["stage"], source_candidate=recipe["source_candidate"],
            verified_pre_swap_snapshot=swap_spec["verified_pre_swap_snapshot"],
            task_root=task_root, openroad=swap_spec["native_tool"],
            lib_files=swap_spec["lib_files"], flow_dir=swap_spec["flow_dir"],
            platform_dir=swap_spec["platform_dir"],
            source_results_dir=swap_spec["source_results_dir"],
            source_design_file=swap_spec["source_design_file"],
            source_sdc_file=swap_spec["source_sdc_file"],
            source_odb_host_path=swap_spec["source_odb_host_path"],
            source_odb_sha256=swap_spec["source_odb_sha256"],
            num_cores=swap_spec.get("num_cores", 1))
        adapter_config = SIEN.workflow_config(
            task=swap_task, artifact_root=profile.artifact_root, step_id="s1")
        swap_tool = {"single_instance_eco": True, "adapter_config": adapter_config}
        arguments = {"swap_plan_digest": swap_task["swap_plan_digest"],
                     "target_instance": swap_task["target_instance"],
                     "replacement_cell": swap_task["replacement_cell"]}
        step = M.TaskStepSpec(
            step_id="s1", capability_id=capability_id, arguments=arguments,
            arguments_sha256=compute_arguments_sha256(arguments),
            capability_contract_sha256=digest_json({
                "package_id": package["package_id"],
                "swap": {"method_identity": SIE.METHOD_ID,
                        "target_instance": swap_task["target_instance"],
                        "expected_cell": swap_task["expected_cell"],
                        "replacement_cell": swap_task["replacement_cell"]}}),
            catalog_snapshot_id=swap_task["swap_plan_digest"], risk="tool_execution",
            approval_required=True,
            manifest_timeout_sec=float(SIE.EXECUTION_PHASE_TIMEOUT_SECONDS))
        limits = {**profile.limits.to_json_dict(), "eda_launches": 1, "candidate_writes": 1}
        plan = {
            "schema_version": "package.flow_task_plan.v1", "runner": "package_single_tool",
            "package_id": package["package_id"], "method": package.get("method"),
            "tool": swap_tool, "task_root": str(task_root),
            "input": input_data, "artifact_root": profile.artifact_root,
            "writable_roots": [str(root) for root in authorized_roots], "budget": limits,
            # single-instance ECO 是有界工具邊界驗證（modify + 獨立讀回），不到 legalize／
            # CTS／route／final，不是完整候選——固定標 qualification_only，
            # 不讓它被誤當成可直接餵給 CORE comparison 的候選。
            "qualification_only": True,
            **({"create_identity": create_identity} if create_identity else {}),
        }
        scope = M.TaskScope(
            member_id=context.member_id, project_id=context.project_id,
            origin_session_id=context.session_id,
            workspace=getattr(context, "workspace", "") or "",
            project_source=getattr(context, "project_source", "session"))
        task = F.build_flow_task_snapshot(
            task_id=task_id, daemon_instance_id=daemon_instance_id, scope=scope,
            profile_id=profile.profile_id, profile_digest=profile.profile_digest,
            question=question, design_id=profile.design_id, platform_id=profile.platform_id,
            parameter_name=SIE.PARAMETER_NAME, baseline_value="NOT_REQUIRED",
            candidate_value="NOT_REQUIRED",
            baseline_input_manifest_sha256=input_data["manifest_digest"],
            baseline_input_tree_hash=input_data["tree_hash"],
            expected_candidate_tree_hash=None, expected_candidate_config_sha256=None,
            task_deadline_sec=profile.limits.task_deadline_sec,
            run_timeout_sec=SIE.EXECUTION_PHASE_TIMEOUT_SECONDS, limits=limits, steps=(step,),
            created_at_ms=draft_created_at_ms,
            template_id=F.PACKAGE_SINGLE_TOOL_TEMPLATE_ID, candidate_plan=(),
            profile_source=profile_source.to_json_dict(), execution_plan=plan,
            eap_proposal=proposal)
        return FlowDraft(task=task, profile=profile)
    arguments = {"candidate_value": tool["candidate_value"],
                 "materialized_sha256": tool["materialized_sha256"]}
    step = M.TaskStepSpec(
        step_id="s1", capability_id=capability_id, arguments=arguments,
        arguments_sha256=compute_arguments_sha256(arguments),
        capability_contract_sha256=digest_json({
            "package_id": package["package_id"], "tool": tool}),
        catalog_snapshot_id=digest_json({"tool_id": tool["tool_id"],
                                         "executable_sha256": tool["executable_sha256"]}),
        risk="tool_execution", approval_required=True,
        manifest_timeout_sec=float(profile.limits.run_timeout_sec))
    limits = {**profile.limits.to_json_dict(), "eda_launches": 1,
              "candidate_writes": 1}
    plan = {
        "schema_version": "package.flow_task_plan.v1", "runner": "package_single_tool",
        "package_id": package["package_id"], "method": package.get("method"),
        "candidate_value": tool["candidate_value"],
        "tool": tool, "task_root": str(task_root),
        "input": input_data, "artifact_root": profile.artifact_root,
        "writable_roots": [str(root) for root in authorized_roots],
        "budget": limits,
        **({"create_identity": create_identity} if create_identity else {}),
    }
    scope = M.TaskScope(
        member_id=context.member_id, project_id=context.project_id,
        origin_session_id=context.session_id,
        workspace=getattr(context, "workspace", "") or "",
        project_source=getattr(context, "project_source", "session"))
    task = F.build_flow_task_snapshot(
        task_id=task_id, daemon_instance_id=daemon_instance_id, scope=scope,
        profile_id=profile.profile_id, profile_digest=profile.profile_digest,
        question=question, design_id=profile.design_id, platform_id=profile.platform_id,
        parameter_name=str(package.get("method") or "package_change"),
        baseline_value="NOT_REQUIRED", candidate_value=tool["candidate_value"],
        baseline_input_manifest_sha256=input_data["manifest_digest"],
        baseline_input_tree_hash=input_data["tree_hash"],
        expected_candidate_tree_hash=digest_json({
            "input": input_data["tree_hash"], "change": tool["materialized_sha256"]}),
        expected_candidate_config_sha256=tool["materialized_sha256"],
        task_deadline_sec=profile.limits.task_deadline_sec,
        run_timeout_sec=profile.limits.run_timeout_sec, limits=limits,
        steps=(step,), created_at_ms=draft_created_at_ms,
        template_id=F.PACKAGE_SINGLE_TOOL_TEMPLATE_ID,
        candidate_plan=({"slot": F.SLOT_CANDIDATE, "order_index": 0,
                         "value": tool["candidate_value"],
                         "expected_config_sha256": tool["materialized_sha256"]},),
        profile_source=profile_source.to_json_dict(), execution_plan=plan,
        eap_proposal=proposal)
    return FlowDraft(task=task, profile=profile)


def package_preflight(task: Any, profile: Any) -> tuple[bool, str | None]:
    """核准前重驗 task/profile/tool/輸入，不能把 preview 當 grant。"""
    from terminux_engine.bounded_execution.analysis_tasks.flow_controller import FlowDraftError

    if profile is None or profile.profile_digest != task.profile_digest:
        return False, "task-owned Profile does not match"
    try:
        _validate_proposal(task.eap_proposal or {})
    except FlowDraftError as exc:
        if exc.code == LDE.LEGACY_DOCKER_EXECUTION_DISABLED:
            return False, exc.message
        return False, f"Package plan preflight failed: {type(exc).__name__}"
    except (KeyError, OSError, ValueError) as exc:
        return False, f"Package plan preflight failed: {type(exc).__name__}"
    tool = task.execution_plan.get("tool") or {}
    if tool.get("formal_pair") is True:
        config = tool.get("adapter_config")
        proof = (config or {}).get("parameters", {}).get("proof_task")
        if (not isinstance(config, dict) or not isinstance(proof, dict)
                or proof.get("task_id") != task.task_id
                or proof.get("task_digest") != "PENDING"
                or proof.get("grant_id") != "PENDING" or proof.get("run_id") != "PENDING"
                or proof.get("proof_plan_digest") != digest_json(
                    {key: value for key, value in proof.items() if key != "proof_plan_digest"})):
            return False, "Formal proof draft binding differs"
    elif tool.get("single_instance_eco") is True:
        # 與 `formal_pair` 同一個原因需要專屬分支——`swap_tool`
        # （`{"single_instance_eco": True, "adapter_config": ...}`）本來就
        # 不等於 `proposal["package_tool"]["single_instance_eco"]`（那是
        # 呼叫端提供的原始輸入，這裡是編譯後的 plan），下面 `elif` 的逐位元
        # 比對必然失敗，需要另外核對 PENDING binding 的形狀正不正確。
        config = tool.get("adapter_config")
        swap = (config or {}).get("parameters", {}).get("swap_task")
        if (not isinstance(config, dict) or not isinstance(swap, dict)
                or swap.get("task_id") != task.task_id
                or swap.get("task_digest") != "PENDING"
                or swap.get("grant_id") != "PENDING" or swap.get("run_id") != "PENDING"
                or swap.get("swap_plan_digest") != digest_json(
                    {key: value for key, value in swap.items() if key != "swap_plan_digest"})):
            return False, "Single instance ECO draft binding differs"
    elif task.execution_plan.get("tool") != task.eap_proposal.get("package_tool"):
        return False, "Package tool plan changed after draft"
    if not task.digest_matches():
        return False, "FlowTask digest differs"
    return True, None


def run_package_steps(controller: Any, service: Any, live: Any) -> bool:
    """grant 後 materialize、派 Kernel child、核實 receipt／manifest 再終結。"""
    task = live.task
    grant = live.grant
    profile = live.profile
    deps = controller._deps.step_deps
    step = task.steps[0]
    plan = task.execution_plan
    tool = plan["tool"]
    if tool.get("formal_pair") is True:
        return _run_formal_pair_steps(controller, service, live)
    if tool.get("single_instance_eco") is True:
        return _run_single_instance_eco_steps(controller, service, live)
    root = Path(plan["task_root"])
    relative = tool["materialized_relative_path"]
    candidate_file = (root / relative).resolve()
    if (grant is None or not task.digest_matches() or live.stop
            or not candidate_file.is_relative_to(root)
            or not root.is_relative_to(Path(profile.execution_root).resolve())):
        with live.lock:
            service._force_flow_terminal_locked(live, "BLOCKED", "Package task binding is invalid")
        return True
    with live.lock:
        live.current_operation = F.OPERATION_PREPARING_VARIANT
        deps.writer.append(
            event_type=F.EVENT_STEP_INTENT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=W.next_sequence(live),
            body={"step_id": step.step_id, "capability_id": step.capability_id,
                  "grant_id": grant.grant_id, "arguments_sha256": step.arguments_sha256})
        live.dispatched.add(step.step_id)
    # 額度先於 side effect：candidate 檔案與外部工具都還沒發生時就保留。
    ledger, reservations = _prepare_package_tool_reservations(
        task, step_id=step.step_id, capability_id=step.capability_id,
        grant_id=grant.grant_id)
    root.mkdir(parents=True, exist_ok=False)
    candidate_file.parent.mkdir(parents=True, exist_ok=False)
    with candidate_file.open("x", encoding="utf-8") as stream:
        stream.write(tool["materialized_content"])
    if hashlib.sha256(candidate_file.read_bytes()).hexdigest() != tool["materialized_sha256"]:
        if ledger is not None:
            # candidate 已經寫出來了，但工具沒有啟動：materialization 照計，
            # tool launch 釋放。兩者分開結算，不合併成一個數字。
            _settle_formal_reservations(
                ledger, [row for row in reservations
                         if row.get("kind") == "candidate_materialization"],
                operation="consumed", event_type=F.EVENT_STEP_INTENT,
                event_id=str(step.step_id))
            _settle_formal_reservations(
                ledger, [row for row in reservations
                         if row.get("kind") == "tool_launch"],
                operation="released", event_type=F.EVENT_STEP_INTENT,
                event_id=str(step.step_id))
        with live.lock:
            service._force_flow_terminal_locked(live, "OUTCOME_UNCONFIRMED",
                                               "Package materialization hash differs")
        return False
    with live.lock:
        live.candidate_writes_used += 1
        live.eda_launches_used += 1
        live.current_operation = F.OPERATION_WAITING_EXECUTION
    from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
        TaskExecutionBinding,
    )

    binding = TaskExecutionBinding(
        task_id=task.task_id, task_digest=task.task_digest, grant_id=grant.grant_id,
        slot=F.SLOT_CANDIDATE, member_id=task.scope.member_id,
        project_id=task.scope.project_id, session_id=task.scope.origin_session_id,
        profile_id=task.profile_id, profile_digest=task.profile_digest,
        input_root=plan["input"]["snapshot_root"],
        input_manifest_path=plan["input"]["manifest_path"],
        input_manifest_sha256=plan["input"]["manifest_digest"],
        input_tree_hash=plan["input"]["tree_hash"], output_root=str(root),
        docker_image_id=profile.docker_image_id, design_id=task.design_id,
        platform_id=task.platform_id, variant="package_candidate",
        design_config_relpath=profile.design_config,
        run_timeout_sec=task.run_timeout_sec, step_id=step.step_id,
        artifact_root=plan["artifact_root"], package_tool=tool)

    def on_run_created(run_ref: Any) -> None:
        event_id = W.persist_fact_event(
            deps, live, task, event_type=F.EVENT_RUN_LINK,
            body={"step_id": step.step_id, "slot": F.SLOT_CANDIDATE,
                  "run": run_ref.to_json_dict()})
        if event_id is None:
            raise RuntimeError("Package task run link was not persisted")
        with live.lock:
            live.runs[F.SLOT_CANDIDATE] = run_ref

    if controller._deps.start_package_run is None:
        raise RuntimeError("Package task-run binding is unavailable")
    run_id = controller._deps.start_package_run(binding, on_run_created)
    if ledger is not None:
        # child 已啟動：之後的 timeout、tool error 或證據不完整都不能把已經
        # 發生的 launch 退回 RELEASED。
        _settle_formal_reservations(
            ledger, reservations, operation="consumed",
            event_type=F.EVENT_RUN_LINK, event_id=str(run_id))
    deadline = min(grant.deadline_at_monotonic,
                   monotonic_s() + float(task.run_timeout_sec) + 30)
    run = None
    while monotonic_s() < deadline:
        run = deps.ledger.get_run(run_id)
        if run is not None and run.status in {"completed", "failed", "timeout", "cancelled"}:
            break
        time.sleep(0.1)
    if run is None or run.status not in {"completed", "failed", "timeout", "cancelled"}:
        with live.lock:
            service._force_flow_terminal_locked(live, "OUTCOME_UNCONFIRMED",
                                               "Package child outcome is unknown")
        return False
    receipt = None
    manifest = None
    receipt_deadline = min(deadline, monotonic_s() + 5.0)
    while monotonic_s() < receipt_deadline:
        receipts = list(deps.receipt_reader(run_id)) if deps.receipt_reader else []
        receipt = receipts[-1] if receipts else None
        if isinstance(receipt, dict):
            reference = receipt.get("artifact_manifest_reference")
            digest = receipt.get("artifact_manifest_sha256")
            if isinstance(reference, str) and isinstance(digest, str):
                path = Path(reference)
                if (path.is_file() and path.resolve().is_relative_to(
                        Path(plan["artifact_root"]).resolve())
                        and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    break
        time.sleep(0.05)
    observed = (manifest or {}).get("observations") or []
    outputs: dict[str, dict[str, Any]] = {}
    for relative_output in tool["artifact_paths"]:
        expected = (root / relative_output).resolve()
        observation = next((row for row in observed
                            if row.get("resolved_path") == str(expected)
                            and row.get("availability") == "present"
                            and row.get("stability") == "stable"), None)
        if observation is None or not expected.is_file():
            break
        content_hash = (observation.get("content_identity") or {}).get("digest")
        if hashlib.sha256(expected.read_bytes()).hexdigest() != content_hash:
            break
        outputs[relative_output] = {"sha256": content_hash,
                                    "path": str(expected)}
        if expected.suffix == ".json" and expected.stat().st_size <= 65536:
            outputs[relative_output]["data"] = json.loads(
                expected.read_text(encoding="utf-8"))
    verified = (run.status == "completed" and isinstance(receipt, dict)
                and receipt.get("process_exit_code") == 0
                and receipt.get("timed_out") is False
                and manifest is not None and manifest.get("capture_status") == "complete"
                and (manifest.get("producer") or {}).get("run_id") == run_id
                and len(outputs) == len(tool["artifact_paths"]))
    full_result = {
        "run_id": run_id, "candidate": task.candidate_value,
        "materialization": {"path": str(candidate_file),
                            "sha256": tool["materialized_sha256"]},
        "receipt": receipt, "manifest": {
            "id": manifest.get("manifest_id"),
            "sha256": receipt.get("artifact_manifest_sha256"),
            "reference": receipt.get("artifact_manifest_reference")}
            if manifest and receipt else None,
        "outputs": outputs, "evidence_verified": verified,
    }
    result_event_id, result_sha256, persistence = W.persist_step_result(
        deps, live, task, grant, step_id=step.step_id,
        capability_id=step.capability_id, arguments_sha=step.arguments_sha256,
        proposal_id="package_" + task.task_id, execution_status=run.status,
        action_status="ok" if verified else "unconfirmed",
        business_class="package_tool_result", full_result=full_result)
    status = "SUCCEEDED" if verified and persistence == "durable" else "OUTCOME_UNCONFIRMED"
    receipt_status = W.persist_step_receipt(
        deps, live, task, step_id=step.step_id, step_status=status,
        outcome={"run_id": run_id, "verified": verified},
        persistence_status=persistence, result_event_id=result_event_id,
        result_sha256=result_sha256, can_continue=verified)
    with live.lock:
        live.steps[step.step_id].update({
            "status": status, "result_event_id": result_event_id,
            "result_sha256": result_sha256, "evidence_status": (
                "verified" if verified else "unconfirmed")})
        service._force_flow_terminal_locked(
            live, "COMPLETED" if status == "SUCCEEDED" and receipt_status == "durable"
            else "OUTCOME_UNCONFIRMED",
            None if status == "SUCCEEDED" and receipt_status == "durable"
            else "Package tool evidence is incomplete")
    return status == "SUCCEEDED" and receipt_status == "durable"


#: 一次 formal proof child 消耗的既有 Work budget 種類。兩者都用既有
#: `BatchLedger` reservation API，不另建 ledger 或 reservation protocol。
FORMAL_RESERVATION_KINDS = ("tool_launch", "formal_proof_launch")
#: 一般 package 單工具的用量：它在同一步裡先寫出 candidate，再啟動外部工具。
#: 這兩件事都是已發生的外部動作，必須在 child 之前 durable 保留、之後結算。
#: 沒有它們，Work 端的 `_actual_child_consumption()` 會在帳本裡查到 0，
#: 然後把**真的已經啟動過**的工具額度退回去。
PACKAGE_TOOL_RESERVATION_KINDS = ("tool_launch", "candidate_materialization")


def formal_launch_identity(task: Any) -> str:
    """這一次 formal launch 的 **durable** 身分。

    刻意不用 `task_id`：daemon 重啟後同一份 proposal 會得到一個新的 FlowTask
    id，若冪等鍵綁在 task 上，重送就會再派一個 child 並再消耗一次額度。Work、
    attempt 與已核准的 proposal digest 三者跨重啟穩定，且都已在 task digest
    之內，因此它們才是「同一次邏輯 launch」的身分。
    """
    proof = ((task.execution_plan.get("tool") or {}).get("adapter_config") or {}
             ).get("parameters", {}).get("proof_task") or {}
    source = (task.eap_proposal or {}).get("source") or {}
    identity = {"work_id": proof.get("work_id"),
                "attempt_id": proof.get("attempt_id"),
                "proof_descriptor_sha256": proof.get("proof_descriptor_sha256"),
                "proposal_digest": source.get("proposal_digest")}
    # 還要綁住「證的是哪兩份 netlist、在哪一份輸入之上」：同一個 Work／attempt
    # 下可以有不同的候選，光靠 proposal digest 分辨不了，而 proof 的結論只對
    # 那一對輸入成立。
    #
    # 這**改變了**既有 qualification launch 的 identity digest（`proof_task` 一定
    # 帶這三項）。影響僅限於「未來的新 launch 如何去重」：已完成的 Phase B／C
    # 證據不含 launch id，也不會被重算。重跑那些 qualification 會得到新的
    # launch id，因此不會誤判成已結算——這是保守方向，不是漏洞。
    identity.update({
        "baseline_sha256": (proof.get("baseline") or {}).get("sha256"),
        "candidate_sha256": (proof.get("candidate") or {}).get("sha256"),
        "input_manifest_sha256": (proof.get("input_manifest") or {}).get(
            "manifest_sha256"),
        "input_tree_hash": (proof.get("input_manifest") or {}).get("tree_hash"),
    })
    return digest_json(identity)


def _formal_reservation_key(launch_id: str, step_id: str, kind: str) -> str:
    return f"formal-pair:{launch_id}:{step_id}:{kind}"


def _package_tool_reservation_key(task_id: str, step_id: str, kind: str) -> str:
    return f"package-tool:{task_id}:{step_id}:{kind}"


def _prepare_package_tool_reservations(
    task: Any, *, step_id: str, capability_id: str, grant_id: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """一般 package 單工具的 durable 額度保留；在任何 child side effect 之前。

    idempotency key 由 task／step／kind 推導，因此重送不會產生第二份可消耗額度。
    """
    from terminux_engine.expert_ai import batch_ledger as BL

    ledger = BL.configured_ledger()
    if ledger is None:
        return None, []
    identity = BL.task_identity(task)
    rows = [ledger.prepare_reservation(
        source="flow_task", work_id=identity.get("work_id"), task_id=task.task_id,
        kind=kind, quantity=1,
        idempotency_key=_package_tool_reservation_key(task.task_id, step_id, kind),
        identity={"attempt_id": identity.get("attempt_id"),
                  "authorization_digest": identity.get("authorization_digest"),
                  "session_id": task.scope.origin_session_id, "step_id": step_id,
                  "capability_id": capability_id, "grant_id": grant_id})
            for kind in PACKAGE_TOOL_RESERVATION_KINDS]
    return ledger, rows


def formal_launch_already_settled(launch_id: str, step_id: str) -> bool:
    """這一次 launch 是否已在帳本結算過。

    `prepare_reservation` 本身以 idempotency key 去重，因此重送不會產生第二份
    可消耗額度；但重送仍可能再派一個 child。已結算即代表 launch 的結果已定，
    必須在建立 child 之前拒絕。
    """
    from terminux_engine.expert_ai import batch_ledger as BL

    ledger = BL.configured_ledger()
    if ledger is None:
        return False
    keys = {_formal_reservation_key(launch_id, step_id, kind)
            for kind in FORMAL_RESERVATION_KINDS}
    rows = BL.read_rows(ledger.path)
    settled = {row.get("reservation_id") for row in rows
               if row.get("operation") in {"consumed", "released"}}
    return any(row.get("idempotency_key") in keys
               and row.get("operation") == "prepared"
               and row.get("reservation_id") in settled
               for row in rows)


def _prepare_formal_reservations(task: Any, *, step_id: str, capability_id: str,
                                 grant_id: str, launch_id: str | None = None,
                                 ) -> tuple[Any, list[dict[str, Any]]]:
    """在任何 child side effect 之前 durable 保留兩種額度。

    reservation 帶 FlowTask 所屬 Work 的身分（work_id／attempt_id／授權
    digest），因此同一份 qualification Work 的用量可由既有帳本重算。
    """
    from terminux_engine.expert_ai import batch_ledger as BL

    ledger = BL.configured_ledger()
    if ledger is None:
        return None, []
    identity = BL.task_identity(task)
    launch_id = launch_id or formal_launch_identity(task)
    rows = [ledger.prepare_reservation(
        source="flow_task", work_id=identity.get("work_id"), task_id=task.task_id,
        kind=kind, quantity=1,
        idempotency_key=_formal_reservation_key(launch_id, step_id, kind),
        identity={"attempt_id": identity.get("attempt_id"),
                  "authorization_digest": identity.get("authorization_digest"),
                  "session_id": task.scope.origin_session_id, "step_id": step_id,
                  "capability_id": capability_id, "grant_id": grant_id,
                  "formal_launch_id": launch_id})
            for kind in FORMAL_RESERVATION_KINDS]
    return ledger, rows


def _settle_formal_reservations(ledger: Any, rows: list[dict[str, Any]], *,
                                operation: str, event_type: str, event_id: str) -> None:
    """以同一份已落盤的 FlowTask 事件結算兩筆 reservation。

    child 一旦啟動即 CONSUMED——timeout 與 tool error 都是已發生的用量，
    不得退回 RELEASED；只有確定沒有 child 被啟動時才 RELEASED。
    """
    for row in rows:
        ledger.settle_reservation(
            str(row["reservation_id"]), operation=operation,
            evidence_ref={"event_type": event_type, "event_id": event_id,
                          "reservation_id": row["reservation_id"]})


def _run_formal_pair_steps(controller: Any, service: Any, live: Any) -> bool:
    """formal descriptor 的單工具分支；不影響一般 non-zero exit 語意。"""
    from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
        TaskExecutionBinding,
    )
    from terminux_engine.dqe.db import DqeDbManager

    task, grant, profile = live.task, live.grant, live.profile
    deps, step, plan = controller._deps.step_deps, task.steps[0], task.execution_plan
    tool, root = plan["tool"], Path(plan["task_root"]).resolve()
    config = tool.get("adapter_config")
    FP = _formal_module_for_task(((config or {}).get("parameters") or {}).get("proof_task")
                                 if isinstance(config, dict) else None)
    if (grant is None or live.stop or not task.digest_matches() or not isinstance(config, dict)
            or root != Path(config.get("output_root", "")).resolve()
            or not root.is_relative_to(Path(profile.execution_root).resolve())):
        with live.lock:
            service._force_flow_terminal_locked(live, "BLOCKED", "Formal proof binding is invalid")
        return False
    launch_id = formal_launch_identity(task)
    if formal_launch_already_settled(launch_id, step.step_id):
        # 這個 task 的 launch 結果已經定了；重送不得再建立第二個 child。
        with live.lock:
            service._force_flow_terminal_locked(
                live, "BLOCKED", "Formal proof launch was already settled for this task")
        return False
    with live.lock:
        live.current_operation = F.OPERATION_WAITING_EXECUTION
        intent = deps.writer.append(
            event_type=F.EVENT_STEP_INTENT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=W.next_sequence(live),
            body={"step_id": step.step_id, "capability_id": step.capability_id,
                  "grant_id": grant.grant_id, "arguments_sha256": step.arguments_sha256})
        live.dispatched.add(step.step_id)
        # 這是 proof tool launch，不是 candidate materialization。
        live.eda_launches_used += 1
    if controller._deps.start_package_run is None:
        raise RuntimeError("Formal proof task-run binding is unavailable")
    # step intent 已落盤（可作為 release 的 evidence），child 仍未啟動：
    # 此處是保留額度的唯一正確位置。
    ledger, reservations = _prepare_formal_reservations(
        task, step_id=step.step_id, capability_id=step.capability_id,
        grant_id=grant.grant_id, launch_id=launch_id)
    # reservation 已準備、但 Kernel child 尚未建立的窄窗口仍可收到 cancel。
    # 沿用既有 ledger 規則：沒有 child 就 RELEASED；一旦 run link 落盤則永遠
    # CONSUMED。不得在 coordinator 另造一套 reservation 語意。
    with live.lock:
        cancelled_before_launch = live.stop
    if cancelled_before_launch:
        if ledger is not None:
            _settle_formal_reservations(ledger, reservations, operation="released",
                                        event_type=F.EVENT_STEP_INTENT,
                                        event_id=intent.event_id)
        with live.lock:
            service._force_flow_terminal_locked(
                live, "CANCELLED", "cancelled before the formal child was started")
        return False
    binding = TaskExecutionBinding(
        task_id=task.task_id, task_digest=task.task_digest, grant_id=grant.grant_id,
        slot=F.SLOT_CANDIDATE, member_id=task.scope.member_id,
        project_id=task.scope.project_id, session_id=task.scope.origin_session_id,
        profile_id=task.profile_id, profile_digest=task.profile_digest,
        input_root=plan["input"]["snapshot_root"],
        input_manifest_path=plan["input"]["manifest_path"],
        input_manifest_sha256=plan["input"]["manifest_digest"],
        input_tree_hash=plan["input"]["tree_hash"], output_root=str(root),
        docker_image_id=profile.docker_image_id, design_id=task.design_id,
        platform_id=task.platform_id, variant="formal_pair_qualification",
        design_config_relpath=profile.design_config, run_timeout_sec=FP.TIMEOUT_SECONDS,
        step_id=step.step_id, artifact_root=plan["artifact_root"],
        dqe_db_path=profile.dqe_db_path, package_tool=tool)

    link: dict[str, str] = {}

    def on_run_created(run_ref: Any) -> None:
        event_id = W.persist_fact_event(
            deps, live, task, event_type=F.EVENT_RUN_LINK,
            body={"step_id": step.step_id, "slot": F.SLOT_CANDIDATE,
                  "run": run_ref.to_json_dict()})
        if event_id is None:
            raise RuntimeError("Formal proof run link was not persisted")
        link["event_id"] = event_id
        with live.lock:
            live.runs[F.SLOT_CANDIDATE] = run_ref

    try:
        run_id = controller._deps.start_package_run(binding, on_run_created)
    except BaseException:
        # Kernel 在 child 啟動前就拒絕或失敗：沒有任何 process 被啟動，
        # 因此兩筆 reservation 都以已落盤的 step intent 事件 RELEASED。
        if ledger is not None:
            _settle_formal_reservations(ledger, reservations, operation="released",
                                        event_type=F.EVENT_STEP_INTENT,
                                        event_id=intent.event_id)
        raise
    if ledger is not None:
        # child 已啟動。之後的 timeout／tool error／evidence 不完整都不能
        # 把已發生的 launch 退回 RELEASED。
        _settle_formal_reservations(ledger, reservations, operation="consumed",
                                    event_type=F.EVENT_RUN_LINK,
                                    event_id=link["event_id"])
    deadline = min(grant.deadline_at_monotonic, monotonic_s() + FP.TIMEOUT_SECONDS + 30)
    run = None
    receipt: dict[str, Any] | None = None
    while monotonic_s() < deadline:
        run = deps.ledger.get_run(run_id)
        receipts = list(deps.receipt_reader(run_id)) if deps.receipt_reader else []
        receipt = receipts[-1] if receipts else None
        if run is not None and run.status in {"completed", "failed", "timeout", "cancelled"} \
                and isinstance(receipt, dict):
            break
        time.sleep(0.1)
    if run is None or not isinstance(receipt, dict):
        with live.lock:
            service._force_flow_terminal_locked(live, "OUTCOME_UNCONFIRMED",
                                               "Formal proof receipt is unavailable")
        return False
    manifest = _load_manifest_from_receipt(receipt, plan["artifact_root"])
    raw = _load_formal_raw_result(manifest)
    try:
        dqe_rows = DqeDbManager(Path(profile.dqe_db_path)).read_records_by_orchestration_run(
            run_id, package_id=FP.proof_descriptor()["dqe_hook"]["package_id"])
        dqe = FP.dqe_reference(rows=dqe_rows, run_id=run_id, step_id=step.step_id,
                               dispatch_key=f"{run_id}:{step.step_id}:1")
        envelope = FP.attest_result(task=raw_to_task(raw), tool_result=raw,
                                    receipt=receipt, manifest=manifest, dqe=dqe)
    except (FP.FormalPairError, OSError, ValueError, TypeError) as exc:
        with live.lock:
            service._force_flow_terminal_locked(live, "OUTCOME_UNCONFIRMED",
                                               f"Formal proof evidence is invalid: {exc}")
        return False
    classification = envelope["classification"]
    # 已完成判讀的工程結果（含 `UNPROVEN`）都算這一步**成功地得到結論**：
    # proof 跑完了、證據齊備、分類成立。能不能選用 candidate 是 eligibility
    # 的事，不在這裡決定。只有 TOOL_ERROR／TIMEOUT／無法分類才是 FAILED。
    valid = classification in set(FP.DECIDABLE_CLASSIFICATIONS)
    qualification_only = bool(plan.get("qualification_only", True))
    full_result = {"qualification_only": qualification_only,
                   "proof_status": classification,
                   "proof_envelope": envelope,
                   "proof_envelope_sha256": envelope["durable_envelope_sha256"],
                   "dqe_dispatch_key": dqe["dispatch_key"], "dqe_row_set_sha256": dqe["row_set_sha256"],
                   "receipt": receipt, "artifact_manifest": manifest}
    result_event_id, result_sha256, persistence = W.persist_step_result(
        deps, live, task, grant, step_id=step.step_id, capability_id=step.capability_id,
        arguments_sha=step.arguments_sha256, proposal_id="package_" + task.task_id,
        execution_status=run.status, action_status="ok" if valid else "failed",
        business_class="formal_pair_proof", full_result=full_result)
    step_status = "SUCCEEDED" if valid and persistence == "durable" else "FAILED"
    receipt_status = W.persist_step_receipt(
        deps, live, task, step_id=step.step_id, step_status=step_status,
        outcome={"run_id": run_id, "proof_status": classification},
        persistence_status=persistence, result_event_id=result_event_id,
        result_sha256=result_sha256, can_continue=valid)
    with live.lock:
        live.steps[step.step_id].update({"status": step_status, "result_event_id": result_event_id,
                                          "result_sha256": result_sha256,
                                          "evidence_status": "verified" if valid else "failed"})
        service._force_flow_terminal_locked(
            live, "COMPLETED" if valid and receipt_status == "durable" else "FAILED",
            None if valid and receipt_status == "durable" else "Formal proof failed")
    return valid and receipt_status == "durable"


#: Kernel run 的終態（ledger `orchestration_runs.status`）。
_RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})


def _read_swap_evidence(
    manifest: dict[str, Any] | None, *, root: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    """只讀 artifact manifest 登記、且內容 digest 仍相符的 swap 證據。"""
    from terminux_engine.packages.orfs_run_intelligence import (
        single_instance_eco as SIE,
        single_instance_eco_adapter as SIEA,
    )

    evidence: dict[str, Any] = {}
    digests: dict[str, str] = {}
    for row in (manifest or {}).get("observations") or []:
        role = str(row.get("adapter_role") or "")
        if not role.startswith(SIE.METHOD_ID + "."):
            continue
        name = role[len(SIE.METHOD_ID) + 1:]
        if name not in SIEA.EVIDENCE_ROLES or row.get("availability") != "present":
            continue
        path = Path(str(row.get("resolved_path") or ""))
        expected = (row.get("content_identity") or {}).get("digest")
        if (not path.is_file() or not path.resolve().is_relative_to(root)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            continue
        digests[name] = str(expected)
        if path.suffix == ".json" and path.stat().st_size <= 65536:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(payload, dict):
                evidence[name] = payload
    return evidence, digests


def _run_single_instance_eco_steps(controller: Any, service: Any, live: Any) -> bool:
    """single_instance_eco 的正式 Package 單工具分支。

    與 `_run_formal_pair_steps()` 同一條路徑：step intent → durable 額度保留
    → 派發前取消檢查 → daemon `start_task_run()`（Kernel child）→ 結算額度
    → 等 run 終態與 receipt → 由 artifact manifest 讀回證據 → 判定 → 由
    runtime 自動保存 repair verdict（`EVENT_REPAIR`）、step result 與 receipt
    → FlowTask 終態。Work 層的 `FlowTaskWorkRuntime.poll()` 再由同一份
    task view 取出 `engineering_result`，不需要任何人另外組 outcome。
    """
    from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
        TaskExecutionBinding,
    )
    from terminux_engine.packages.orfs_run_intelligence import (
        single_instance_eco as SIE,
        single_instance_eco_adapter as SIEA,
    )

    task, grant, profile = live.task, live.grant, live.profile
    deps, step, plan = controller._deps.step_deps, task.steps[0], task.execution_plan
    tool, root = plan["tool"], Path(plan["task_root"]).resolve()
    config = tool.get("adapter_config")
    swap_task = ((config or {}).get("parameters") or {}).get("swap_task") \
        if isinstance(config, dict) else None
    if (grant is None or live.stop or not task.digest_matches()
            or not isinstance(config, dict) or not isinstance(swap_task, dict)
            or root != Path(config.get("output_root", "")).resolve()
            or not root.is_relative_to(Path(profile.execution_root).resolve())):
        with live.lock:
            service._force_flow_terminal_locked(
                live, "BLOCKED", "Single instance ECO binding is invalid")
        return False
    if controller._deps.start_package_run is None:
        raise RuntimeError("Single instance ECO task-run binding is unavailable")
    with live.lock:
        live.current_operation = F.OPERATION_WAITING_EXECUTION
        intent = deps.writer.append(
            event_type=F.EVENT_STEP_INTENT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=W.next_sequence(live),
            body={"step_id": step.step_id, "capability_id": step.capability_id,
                  "grant_id": grant.grant_id, "arguments_sha256": step.arguments_sha256})
        live.dispatched.add(step.step_id)
    # 額度先於任何 child side effect 保留（tool launch ＋ candidate ODB 寫出）。
    ledger, reservations = _prepare_package_tool_reservations(
        task, step_id=step.step_id, capability_id=step.capability_id,
        grant_id=grant.grant_id)

    def _release_reservations() -> None:
        if ledger is not None:
            _settle_formal_reservations(ledger, reservations, operation="released",
                                        event_type=F.EVENT_STEP_INTENT,
                                        event_id=intent.event_id)

    with live.lock:
        cancelled_before_launch = live.stop
    if cancelled_before_launch:
        _release_reservations()
        with live.lock:
            service._force_flow_terminal_locked(
                live, "CANCELLED", "cancelled before the ECO child was started")
        return False
    binding = TaskExecutionBinding(
        task_id=task.task_id, task_digest=task.task_digest, grant_id=grant.grant_id,
        slot=F.SLOT_CANDIDATE, member_id=task.scope.member_id,
        project_id=task.scope.project_id, session_id=task.scope.origin_session_id,
        profile_id=task.profile_id, profile_digest=task.profile_digest,
        input_root=plan["input"]["snapshot_root"],
        input_manifest_path=plan["input"]["manifest_path"],
        input_manifest_sha256=plan["input"]["manifest_digest"],
        input_tree_hash=plan["input"]["tree_hash"], output_root=str(root),
        docker_image_id=profile.docker_image_id, design_id=task.design_id,
        platform_id=task.platform_id, variant="single_instance_eco",
        design_config_relpath=profile.design_config,
        run_timeout_sec=SIE.EXECUTION_PHASE_TIMEOUT_SECONDS, step_id=step.step_id,
        artifact_root=plan["artifact_root"], package_tool=tool)

    link: dict[str, Any] = {}

    def on_run_created(run_ref: Any) -> None:
        # 取消與啟動交錯：cancel_flow_task() 在同一把 live.lock 下設 stop 並
        # 取 known_runs。這裡在同一把鎖下「檢查 stop → 保存 run link → 登記
        # run」，因此不是 cancel 先到（拒絕，daemon 不啟動 child），就是 cancel
        # 看得到這個 run 並對它發出 Kernel cancel。
        with live.lock:
            if live.stop:
                raise RuntimeError("cancel was requested before the ECO child started")
            event_id = W.persist_fact_event(
                deps, live, task, event_type=F.EVENT_RUN_LINK,
                body={"step_id": step.step_id, "slot": F.SLOT_CANDIDATE,
                      "run": run_ref.to_json_dict()})
            if event_id is None:
                raise RuntimeError("Single instance ECO run link was not persisted")
            link.update({"event_id": event_id, "run_ref": run_ref})
            live.runs[F.SLOT_CANDIDATE] = run_ref

    try:
        run_id = controller._deps.start_package_run(binding, on_run_created)
    except BaseException:
        # daemon 在 child 啟動前就拒絕（含上面的取消）：沒有 process 被啟動。
        _release_reservations()
        with live.lock:
            if live.stop:
                service._force_flow_terminal_locked(
                    live, "CANCELLED", "cancelled before the ECO child was started")
                return False
        raise
    if ledger is not None:
        # child 已啟動；之後的 timeout／tool error／取消都不能退回 RELEASED。
        _settle_formal_reservations(ledger, reservations, operation="consumed",
                                    event_type=F.EVENT_RUN_LINK,
                                    event_id=str(link["event_id"]))
    with live.lock:
        live.eda_launches_used += 1
        live.candidate_writes_used += 1
        stop_after_start = live.stop
    late_cancel: dict[str, Any] | None = None
    if stop_after_start:
        # cancel 可能在 run 仍是 pending 時送達（ledger 只接受 running／paused
        # 的 cancel），此時 Kernel 端沒有記下取消。run 已 running，再送一次。
        late_cancel = controller.cancel_run(link["run_ref"])
    deadline = min(grant.deadline_at_monotonic,
                   monotonic_s() + float(SIE.EXECUTION_PHASE_TIMEOUT_SECONDS) + 30)
    run = None
    while monotonic_s() < deadline:
        run = deps.ledger.get_run(run_id)
        if run is not None and run.status in _RUN_TERMINAL_STATUSES:
            break
        time.sleep(0.1)
    if run is None or run.status not in _RUN_TERMINAL_STATUSES:
        with live.lock:
            service._force_flow_terminal_locked(
                live, "OUTCOME_UNCONFIRMED", "Single instance ECO child outcome is unknown")
        return False
    receipt: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    if run.status != "cancelled":
        receipt_deadline = min(deadline, monotonic_s() + 5.0)
        while monotonic_s() < receipt_deadline:
            receipts = list(deps.receipt_reader(run_id)) if deps.receipt_reader else []
            receipt = receipts[-1] if receipts else None
            if isinstance(receipt, dict):
                break
            time.sleep(0.05)
        if isinstance(receipt, dict):
            try:
                manifest = _load_manifest_from_receipt(receipt, plan["artifact_root"])
            except (OSError, ValueError):
                manifest = None
    # 清理結果的唯一 durable 讀回點是 ledger 的 resource_cleanup transition：
    # 正常路徑由 Kernel（runner 行程內）寫入；runner 被 SIGKILL 時由 daemon
    # 代為清理後寫入。這裡原樣讀回，取最後一筆作為這次 run 的清理結論。
    cleanup_records = [
        {"to_state": row.to_state, "detail": json.loads(row.reason or "null")}
        for row in deps.ledger.list_transitions(run_id=run_id,
                                                state_kind="resource_cleanup")]
    cleanup_result = cleanup_records[-1]["detail"] if cleanup_records else None
    evidence, digests = _read_swap_evidence(manifest, root=root)
    bound_task = json.loads(json.dumps(swap_task))
    bound_task["run_id"] = run_id
    bound_task["task_digest"] = task.task_digest
    bound_task["grant_id"] = grant.grant_id
    bound_task.pop("swap_plan_digest", None)
    bound_task["swap_plan_digest"] = digest_json(bound_task)
    exit_code = receipt.get("process_exit_code") if isinstance(receipt, dict) else None
    timed_out = bool((receipt or {}).get("timed_out")) or run.status == "timeout"
    evaluator = SIEA
    if bound_task.get("schema_version") != SIEA.TASK_SCHEMA:
        from terminux_engine.packages.orfs_run_intelligence import (
            single_instance_eco_native as SIEN,
        )

        evaluator = SIEN
    verdict = evaluator.evaluate_swap_evidence(
        task=bound_task, process_exit_code=exit_code, timed_out=timed_out,
        evidence=evidence, evidence_digests=digests, cleanup_result=cleanup_result,
        run_status=run.status)
    with live.lock:
        live.repair = verdict
    W.persist_fact_event(deps, live, task, event_type=F.EVENT_REPAIR,
                         body={"step_id": step.step_id, "repair": verdict})
    operation_ok = (run.status == "completed" and verdict["readback_verified"]
                    and manifest is not None
                    and (manifest.get("producer") or {}).get("run_id") == run_id)
    full_result = {
        "qualification_only": True, "run_id": run_id, "run_status": run.status,
        "receipt": receipt, "manifest": ({
            "id": manifest.get("manifest_id"),
            "sha256": receipt.get("artifact_manifest_sha256"),
            "reference": receipt.get("artifact_manifest_reference")}
            if manifest is not None and isinstance(receipt, dict) else None),
        "evidence_digests": digests, "repair_status": verdict["repair_status"],
        "resource_cleanup_records": cleanup_records, "late_cancel": late_cancel,
    }
    if run.status == "cancelled":
        action_status, step_status = "cancelled", "CANCELLED"
    elif receipt is None:
        action_status, step_status = "unconfirmed", "OUTCOME_UNCONFIRMED"
    elif operation_ok:
        action_status, step_status = "ok", "SUCCEEDED"
    else:
        action_status, step_status = "failed", "FAILED"
    result_event_id, result_sha256, persistence = W.persist_step_result(
        deps, live, task, grant, step_id=step.step_id,
        capability_id=step.capability_id, arguments_sha=step.arguments_sha256,
        proposal_id="package_" + task.task_id, execution_status=run.status,
        action_status=action_status, business_class="single_instance_eco_swap",
        full_result=full_result)
    if persistence != "durable" and step_status == "SUCCEEDED":
        step_status = "OUTCOME_UNCONFIRMED"
    receipt_status = W.persist_step_receipt(
        deps, live, task, step_id=step.step_id, step_status=step_status,
        outcome={"run_id": run_id, "run_status": run.status,
                 "repair_status": verdict["repair_status"]},
        persistence_status=persistence, result_event_id=result_event_id,
        result_sha256=result_sha256, can_continue=step_status == "SUCCEEDED")
    terminal, reason = {
        "SUCCEEDED": ("COMPLETED", None),
        "CANCELLED": ("CANCELLED", "the ECO run was cancelled"),
        "FAILED": ("FAILED", "Single instance ECO tool operation did not complete"),
    }.get(step_status, ("OUTCOME_UNCONFIRMED", "Single instance ECO evidence is incomplete"))
    if terminal == "COMPLETED" and receipt_status != "durable":
        terminal, reason = "OUTCOME_UNCONFIRMED", "Single instance ECO receipt was not persisted"
    with live.lock:
        live.steps[step.step_id].update({
            "status": step_status, "result_event_id": result_event_id,
            "result_sha256": result_sha256,
            "evidence_status": "verified" if step_status == "SUCCEEDED" else "failed"})
        service._force_flow_terminal_locked(live, terminal, reason)
    return terminal == "COMPLETED"


def _load_manifest_from_receipt(receipt: dict[str, Any], artifact_root: str) -> dict[str, Any]:
    reference, digest = receipt.get("artifact_manifest_reference"), receipt.get("artifact_manifest_sha256")
    path = Path(str(reference or ""))
    if (not path.is_file() or not path.resolve().is_relative_to(Path(artifact_root).resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
        raise ValueError("artifact manifest identity differs")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("capture_status") != "complete":
        raise ValueError("artifact manifest is incomplete")
    return manifest


def _load_formal_raw_result(manifest: dict[str, Any]) -> dict[str, Any]:
    observation = next((row for row in manifest.get("observations", [])
                        if row.get("adapter_role") == "orfs.formal_pair.formal_pair_raw_tool_result.json"), None)
    path = Path(str((observation or {}).get("resolved_path") or ""))
    expected = ((observation or {}).get("content_identity") or {}).get("digest")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("formal raw result identity differs")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("formal raw result is invalid")
    return raw


def legacy_docker_execution_path(tool: Any) -> str | None:
    """Package 工具計畫是否要求以舊 container 模式執行；回傳受影響的路徑。

    formal pair 以 `docker_executable` 工具邊界、ECO 以 container 主機綁定
    （沒有 `native_tool`）判定；其他 Package 工具不經 Docker，回 `None`。
    """
    if not isinstance(tool, dict):
        return None
    formal = tool.get("formal_pair")
    if isinstance(formal, dict) and "docker_executable" in formal:
        return LDE.PATH_FORMAL_PAIR
    swap = tool.get("single_instance_eco")
    if isinstance(swap, dict) and "native_tool" not in swap:
        return LDE.PATH_SINGLE_INSTANCE_ECO
    return None


def _legacy_docker_draft_error(path: str) -> Any:
    from terminux_engine.bounded_execution.analysis_tasks.flow_controller import FlowDraftError

    return FlowDraftError(LDE.LEGACY_DOCKER_EXECUTION_DISABLED, LDE.disabled_message(path))


def _native_swap_binding_sha256(swap: dict[str, Any]) -> str:
    from terminux_engine.packages.orfs_run_intelligence import single_instance_eco_native

    return single_instance_eco_native.native_tool_binding_sha256(
        openroad=swap.get("native_tool") or {}, lib_files=swap.get("lib_files") or {})


def _formal_module(formal_spec: dict[str, Any]) -> Any:
    """proof 提案的契約模組：帶 `native_tool` 的是 host-native，其餘是 container 版本。"""
    if "native_tool" in formal_spec:
        from terminux_engine.packages.orfs_run_intelligence import formal_pair_native

        return formal_pair_native
    from terminux_engine.packages.orfs_run_intelligence import formal_pair

    return formal_pair


def _formal_module_for_task(proof_task: Any) -> Any:
    from terminux_engine.packages.orfs_run_intelligence import formal_pair, formal_pair_native

    return formal_pair_native if formal_pair_native.is_native_task(proof_task) else formal_pair


def _profile_formal_tool(profile: Any) -> dict[str, str] | None:
    binding = getattr(profile, "native_tool_binding", None)
    pin = (getattr(binding, "tools", None) or {}).get("FORMAL_YOSYS")
    return pin.to_json_dict() if pin is not None else None


def raw_to_task(raw: dict[str, Any]) -> dict[str, Any]:
    """從 raw 的 host-bound facts 還原 attestation 所需的 immutable task view。"""
    from terminux_engine.packages.orfs_run_intelligence import formal_pair_native
    from terminux_engine.packages.orfs_run_intelligence.formal_pair import descriptor_digest

    if formal_pair_native.is_native_raw(raw):
        return formal_pair_native.raw_to_task(raw)

    keys = ("work_id", "attempt_id", "task_id", "task_digest", "grant_id", "run_id",
            "proof_plan_digest", "package_identity", "method_extension", "baseline",
            "candidate", "input_manifest")
    task = {key: raw[key] for key in keys}
    task["script"] = {"sha256": raw["script_sha256"]}
    task["proof_descriptor_sha256"] = descriptor_digest()
    return task


__all__ = ["CREATE_IDENTITY_SCHEMA", "FORMAL_RESERVATION_KINDS",
           "PACKAGE_TOOL_RESERVATION_KINDS", "build_package_draft",
           "create_identity_event_id", "create_identity_task_id",
           "flow_task_create_identity",
           "formal_launch_already_settled", "formal_launch_identity",
           "package_preflight", "run_package_steps"]
