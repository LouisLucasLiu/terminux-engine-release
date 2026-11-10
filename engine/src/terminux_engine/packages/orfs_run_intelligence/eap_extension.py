# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS 對 Engineering Work 公開的 Package capability entrypoint。"""

from __future__ import annotations

import json
from typing import Any

from terminux_engine.expert_ai.eap2 import build_flow_task_proposal
from terminux_engine.expert_ai.eap3 import PackageCapability, digest_json
from terminux_engine.packages.orfs_run_intelligence.proposal_validator import (
    PROPOSAL_ADAPTER_VERSION,
    PROPOSAL_SCHEMA_VERSION,
    VALIDATOR_VERSION,
    adapt_and_validate_proposal,
)
from terminux_engine.packages.orfs_run_intelligence.core_utilization import (
    BASELINE, MAX_CANDIDATES, MAX_DECREMENT, MAXIMUM, METHOD_ID, MINIMUM, PARAMETER,
    CoreUtilizationError, candidate_window, validate_candidate,
)
from terminux_engine.packages.orfs_run_intelligence.method_extensions import (
    CORE_UTILIZATION, SETUP_MARGIN, SINGLE_INSTANCE_ECO, extension_descriptor_sha256,
    host_native_variant,
)
from terminux_engine.packages.orfs_run_intelligence.proposal_validator import (
    CHANGE_KIND_CORE_UTILIZATION, CHANGE_KIND_TIMING_PARAMETER,
)
from terminux_engine.packages.orfs_run_intelligence import single_instance_eco as SIE

#: host-native Profile 的工具身分前綴（`FlowTaskProfile.tool_identity_ref`）。
NATIVE_TOOL_IDENTITY_PREFIX = "native-binding:sha256:"

#: 已驗證 change kind／parameter → 本 Package 的 method extension。
#: single_instance_cell_swap 的 `changes` 沒有 `parameter` 鍵，`.get()` 自然
#: 回傳 `None`，因此用 `(kind, None)` 當它的鍵，不需要另外改查找邏輯。
_EXTENSION_BY_CHANGE = {
    (CHANGE_KIND_TIMING_PARAMETER, SETUP_MARGIN.parameter): SETUP_MARGIN,
    (CHANGE_KIND_CORE_UTILIZATION, CORE_UTILIZATION.parameter): CORE_UTILIZATION,
    (SIE.CHANGE_KIND, None): SINGLE_INSTANCE_ECO,
}


def capability() -> PackageCapability:
    return PackageCapability(
        package_id="orfs_run_intelligence", capability="timing_analysis",
        version="eap3.orfs.v1", proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
        validate=adapt_and_validate_proposal,
        compile_plan=compile_plan, baseline_requirements=baseline_requirements,
        authorization_check=authorization_check,
        proposal_contract=proposal_contract())


def proposal_contract() -> dict[str, Any]:
    """提供 Domain 與 validator 共用的 Package-owned 回覆格式。"""
    return {
        "type": "object",
        "required": ["package_schema_version", "changes", "steps"],
        "properties": {
            "package_schema_version": {"const": PROPOSAL_SCHEMA_VERSION},
            "changes": {"type": "object",
                        "required_exact": ["kind", "parameter", "candidate"],
                        "one_of": [
                {"type": "object", "required_exact": ["kind", "parameter", "candidate"],
                 "properties": {"kind": {"const": "timing_parameter"},
                                "parameter": {"const": "SETUP_SLACK_MARGIN"},
                                "candidate": {"type": "non_negative_decimal_string"}}},
                {"type": "object", "required_exact": ["kind", "parameter", "candidate"],
                 "properties": {"kind": {"const": "core_utilization"},
                                "parameter": {"const": PARAMETER},
                                "candidate": {"type": "integer", "minimum": MINIMUM,
                                              "maximum": MAXIMUM}}},
            ]},
            "steps": {
                "type": "array", "length": 1,
                "items": {
                    "operation": {"one_of": ["orfs.eap.materialize_timing_parameter",
                                                "orfs.eap.materialize_core_utilization"]},
                    "arguments": {
                        "type": "object",
                        "required_exact": ["parameter", "candidate"],
                        "properties": {
                            "parameter": {"equals": "$.changes.parameter"},
                            "candidate": {"equals": "$.changes.candidate"},
                        },
                    },
                    "depends_on": {"const": []},
                },
            },
        },
        "core_utilization_control": {
            "method_identity": METHOD_ID, "baseline": BASELINE,
            "global_range": [MINIMUM, MAXIMUM], "max_decrement": MAX_DECREMENT,
            "max_candidates": MAX_CANDIDATES,
            "required_revision_context": ["previous_executed_value",
                                           "previous_attempt_id", "executed_candidates",
                                           "allowed_candidate_minimum",
                                           "allowed_candidate_maximum"],
        },
        # single_instance_cell_swap 用完全不同的 `changes`／`steps` 形狀
        # （見 `single_instance_eco.py`），不硬塞進上面兩個方法共用、且已被
        # 既有測試逐位元組核對過的 `changes.one_of`／`steps` 結構；沿用
        # `core_utilization_control` 同一種做法，另開一個自己的區塊描述完整
        # 格式，Domain 一樣能從 `proposal_contract` 讀到。
        "single_instance_eco_control": {
            "method_identity": SIE.METHOD_ID, "change_kind": SIE.CHANGE_KIND,
            "required_change_keys": sorted(SIE.REQUIRED_CHANGE_KEYS),
            "required_source_candidate_keys": sorted(SIE.REQUIRED_SOURCE_CANDIDATE_KEYS),
            "supported_stages": sorted(SIE.SUPPORTED_STAGES),
            "supported_libraries": sorted(SIE.SUPPORTED_LIBRARIES),
            "approved_non_clock_buffer_family": list(SIE.NON_CLOCK_BUFFER_FAMILY),
            "materialize_operation": SIE.MATERIALIZE_OPERATION,
            "step_arguments": ["target_instance", "expected_cell", "replacement_cell"],
        },
        "compatibility_adapter": {
            "version": PROPOSAL_ADAPTER_VERSION,
            "accepts": "single-element changes array containing one object",
            "rule": "unwrap only; preserve every member; then run Package validator",
            "rejects": ["empty array", "multiple changes", "non-object element"],
        },
    }


def authorization_check(
    proposal: dict[str, Any], boundary: dict[str, Any],
) -> dict[str, Any] | None:
    if boundary.get("kind") == "ANY_PACKAGE_VALIDATED_CHANGE":
        return None
    changes = proposal.get("changes") or {}
    if changes.get("kind") == SIE.CHANGE_KIND:
        if boundary.get("kind") != "single_instance_eco_bounds":
            return {"reason_code": "CHANGE_BOUNDARY_UNSUPPORTED", "required": boundary}
        # 「同一個 instance 換到同一顆 cell」不得重複派工；history 只由
        # boundary 帶進來的已執行 lineage 判斷，不信任 proposal 自稱的內容
        # ——與 CORE_UTILIZATION／SETUP_SLACK_MARGIN 同一個原則。
        target = changes.get("target_instance")
        replacement = changes.get("replacement_cell")
        for prior in boundary.get("executed_swaps") or []:
            if (prior.get("target_instance") == target
                    and prior.get("replacement_cell") == replacement):
                return {"reason_code": "CHANGE_VALUE_ALREADY_EXECUTED",
                        "required": f"{target}->{replacement}"}
        return None
    if changes.get("parameter") == PARAMETER:
        if boundary.get("kind") != "core_utilization_bounds":
            return {"reason_code": "CHANGE_BOUNDARY_UNSUPPORTED", "required": boundary}
        try:
            attempts = boundary.get("executed_attempts")
            if not isinstance(attempts, list):
                raise CoreUtilizationError("runtime attempt history missing")
            window = candidate_window(attempts)
            validate_candidate(changes.get("candidate"), window)
            published = (proposal.get("revision_context") or {})
            if published != {key: value for key, value in window.to_json_dict().items()
                             if key in {"previous_executed_value", "previous_attempt_id",
                                        "executed_candidates", "allowed_candidate_minimum",
                                        "allowed_candidate_maximum"}}:
                raise CoreUtilizationError("proposal context differs from runtime history")
        except CoreUtilizationError as exc:
            return {"reason_code": "CORE_UTILIZATION_OUT_OF_SCOPE", "required": str(exc)}
        return None
    if boundary.get("kind") != "timing_parameter_bounds":
        return {"reason_code": "CHANGE_BOUNDARY_UNSUPPORTED", "required": boundary}
    if changes.get("parameter") != boundary.get("parameter"):
        return {"reason_code": "CHANGE_PARAMETER_OUT_OF_SCOPE",
                "required": changes.get("parameter")}
    from decimal import Decimal, InvalidOperation
    try:
        candidate = Decimal(str(changes.get("candidate")))
        minimum = Decimal(str(boundary["minimum"]))
        maximum = Decimal(str(boundary["maximum"]))
    except (InvalidOperation, KeyError):
        return {"reason_code": "CHANGE_BOUNDARY_INVALID", "required": boundary}
    if not minimum <= candidate <= maximum:
        return {"reason_code": "CHANGE_VALUE_OUT_OF_SCOPE", "required": str(candidate)}
    # 續作不能重花一次真實 EDA 額度重播已經真的跑過的候選值；history 只由
    # host 端已執行 lineage 推導（見 `eap3.py::_authorization_denial`），不是
    # 這裡自己去猜或信任 proposal 自稱的內容。沒有 history 就不比對（例如
    # 第一輪，或 boundary 沒有宣告過往紀錄）。
    for prior in boundary.get("executed_attempts") or []:
        try:
            prior_candidate = Decimal(str(prior.get("candidate")))
        except (InvalidOperation, TypeError):
            continue
        if prior_candidate == candidate:
            return {"reason_code": "CHANGE_VALUE_ALREADY_EXECUTED",
                    "required": str(candidate)}
    return None


def compile_plan(proposal: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    method = str(proposal.get("method") or (
        proposal.get("changes") or {}).get("parameter") or "")
    domain = {"kind": "PROPOSAL",
              "method": method,
              "package_proposal": proposal, "message": "validated Package proposal"}
    draft = {
        "status": "DRAFT_READY", "draft_id": "workdraft_" + digest_json({
            "work_id": context["work"]["work_id"],
            "revision": context["work"]["revision"], "proposal": proposal})[:24],
        "problem_id": context["work"]["work_id"],
        "plan_revision": context["work"]["revision"], "proposal": domain,
        "global_decision": {"integration": {"kind": "SUBMIT_PROPOSAL"}},
        "basis_evidence_refs": [context["baseline"].get("reference")],
    }
    flow = build_flow_task_proposal(draft)
    changes = proposal.get("changes") or {}
    extension = _EXTENSION_BY_CHANGE.get((changes.get("kind"), changes.get("parameter")))
    if extension is None:
        raise ValueError("validated proposal has no registered method extension")
    # 執行模式由已核准 Profile 的工具身分決定（host-native binding 或 image）；
    # host-native 時 formal proof 改釘 native 契約，不沿用 container descriptor。
    tool_identity = str(((context.get("work") or {}).get("environment") or {}).get(
        "tool_identity") or "")
    if tool_identity.startswith(NATIVE_TOOL_IDENTITY_PREFIX):
        extension = host_native_variant(extension)
    # flow_task_proposal 隨 task.eap_proposal 進 task digest，是唯一 authority。
    pinned = extension.pinned()
    flow["method_extension"] = pinned
    # Package 身分與 method 身分一起固定進 FlowTask digest。
    flow["package_identity"] = {"package_id": "orfs_run_intelligence",
                                "version": "eap3.orfs.v1",
                                "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
                                "validator_version": VALIDATOR_VERSION}
    if changes.get("kind") == SIE.CHANGE_KIND:
        return _compile_single_instance_swap(proposal, context, flow, pinned, method)
    baseline_reused = context["baseline"].get("status") in {"REUSED_VALIDATED", "COMPLETE"}
    baseline_reference = context["baseline"].get("reference") or {}
    baseline_import = baseline_reference.get("execution_import")
    if baseline_reused and not isinstance(baseline_import, dict):
        raise ValueError("validated baseline lacks a host execution import")
    if baseline_reused:
        flow["baseline_plan"] = {
            "mode": "REUSE_VALIDATED_EAP3_BASELINE",
            "reference": baseline_import,
        }
    # 由已釘住的 method extension 決定要不要保留 formal proof 的額度。
    formal_proofs = 1 if "formal_equivalence" in (
        extension.required_proof_kinds or ()) else 0
    roots = list(context["authorization"].get("writable_roots") or [])
    core_context = proposal.get("revision_context") if (
        (proposal.get("changes") or {}).get("parameter") == PARAMETER) else None
    return {"schema_version": "eap3.execution_plan.v1", "runtime": "flow_task",
            "package_id": "orfs_run_intelligence", "method": method,
            "canonical_proposal": proposal, "flow_task_proposal": flow,
            "baseline": context["baseline"], "baseline_mode": (
                "REUSE_VALIDATED" if baseline_reused else "RERUN"),
            # method 宣告需要 formal proof 時，那個 proof 是**另一個** FlowTask
            # 的一次工具啟動。不在計畫裡宣告，Work 層就既不保留也不 gate 它：
            # proof 會在授權之外跑掉，而 Work 的 counter 仍顯示 0。
            "tool_launches": (1 if baseline_reused else 2) + formal_proofs,
            **({"formal_proof_launches": formal_proofs} if formal_proofs else {}),
            "candidate_materializations": 1, "writable_roots": roots,
            # 頂層副本僅供既有檢視；內容與 digest 必須與 flow_task_proposal 相同。
            "method_extension": _mirror_pin(pinned, flow["method_extension"]),
            **({"core_utilization_revision_context": core_context,
                "candidate_plan": {"parameter": PARAMETER,
                                   "candidate": proposal["changes"]["candidate"],
                                   "revision_context": core_context},
                "repair_recipe": {"method_identity": METHOD_ID,
                                  "operation": "orfs.eap.materialize_core_utilization",
                                  "parameter": PARAMETER,
                                  "candidate": proposal["changes"]["candidate"]}}
               if core_context is not None else {})}


def _single_instance_swap_recipe(changes: dict[str, Any]) -> dict[str, Any]:
    return {
        "method_identity": SIE.METHOD_ID,
        "operation": SIE.MATERIALIZE_OPERATION,
        "stage": changes.get("stage"),
        "library": changes.get("library"),
        "target_instance": changes.get("target_instance"),
        "expected_cell": changes.get("expected_cell"),
        "replacement_cell": changes.get("replacement_cell"),
        "source_candidate": changes.get("source_candidate"),
    }


def _compile_single_instance_swap(
    proposal: dict[str, Any], context: dict[str, Any], flow: dict[str, Any],
    pinned: dict[str, Any], method: str,
) -> dict[str, Any]:
    """single_instance_cell_swap 編譯成正式 Package FlowTask 計畫。

    - 來源候選與目標 instance 由 host 依已登記、digest 核對過的來源證據核驗
      （`single_instance_eco_host.verify_source_candidate()`）；
      `verified_pre_swap_snapshot` 是 host 解析結果，proposal 不能提供或覆寫。
      核驗失敗在這裡 raise，EAP3 記成 PLAN_REJECTED，零派發。
    - 工具 pin、liberty、flow 與 platform 目錄都來自 host-native 綁定設定，
      不來自 proposal；container 綁定（`host_binding.v1`）不再產生計畫。
    - Work input 身分沿用 Profile 已固定的 snapshot manifest（raw bytes digest
      必須等於 Profile 宣告值）；ECO 工具本身讀的是 host 登記的來源結果目錄。
    - baseline 為 NOT_REQUIRED：本方法宣告的 baseline 功能 reference 這裡不
      產生，驗收因此如實停在 UNVERIFIABLE（見 `evaluate_swap_evidence()`）。
    """
    from terminux_engine.packages.orfs_run_intelligence import (
        single_instance_eco_host as SIEH,
    )
    from terminux_engine.packages.orfs_run_intelligence.flow_inputs import load_manifest

    changes = proposal.get("changes") or {}
    # 主機 ECO 綁定的 schema 決定執行邊界（container 或 host-native）；兩者的
    # 核驗規則相同，回傳的 host 欄位不同。
    if SIEH.configured_binding_mode() != SIEH.BINDING_MODE_HOST_NATIVE:
        # container 綁定不再產生可派發的計畫；不讀來源、不查 image。
        from terminux_engine.orchestration.legacy_docker_execution import (
            PATH_SINGLE_INSTANCE_ECO,
            LegacyDockerExecutionDisabled,
        )

        raise LegacyDockerExecutionDisabled(PATH_SINGLE_INSTANCE_ECO)
    verified = SIEH.verify_native_source_candidate(
        changes=changes, binding=SIEH.load_native_host_binding())
    environment = context["work"].get("environment") or {}
    manifest_path = environment.get("input_manifest_path")
    manifest_digest = environment.get("input_manifest_digest")
    snapshot_root = environment.get("input_snapshot_root")
    if not all(isinstance(value, str) and value for value in (
            manifest_path, manifest_digest, snapshot_root)):
        raise ValueError("the Work has no Profile-fixed input identity")
    manifest = load_manifest(manifest_path, expected_sha256=manifest_digest)
    recipe = _single_instance_swap_recipe(changes)
    flow["source"] = {**(flow.get("source") or {}),
                      "proposal_digest": digest_json(proposal)}
    # 沿用既有 `flow_task_binding()` 的 Package 方案內容，只補上 Package
    # 單工具入口需要的 Package／method 身分。
    flow["package"] = {**flow["package"], "package_id": "orfs_run_intelligence",
                       "method": method}
    flow["input"] = {"snapshot_root": snapshot_root, "manifest_path": manifest_path,
                     "manifest_digest": manifest_digest,
                     "tree_hash": manifest.tree_hash}
    flow["package_tool"] = {"single_instance_eco": {
        "repair_recipe": recipe, **verified}}
    return {"schema_version": "eap3.execution_plan.v1", "runtime": "flow_task",
            "package_id": "orfs_run_intelligence", "method": method,
            "canonical_proposal": proposal, "flow_task_proposal": flow,
            "baseline": context["baseline"], "baseline_mode": "NOT_REQUIRED",
            "tool_launches": 1, "candidate_materializations": 1,
            "writable_roots": list(context["authorization"].get("writable_roots") or []),
            "method_extension": _mirror_pin(pinned, flow["method_extension"]),
            "repair_recipe": recipe}


def _mirror_pin(pinned: dict[str, Any], authority: dict[str, Any]) -> dict[str, Any]:
    mirror = json.loads(json.dumps(pinned))
    if (digest_json(mirror) != digest_json(authority)
            or mirror["descriptor_sha256"] != extension_descriptor_sha256(mirror["descriptor"])):
        raise ValueError("method extension mirror differs from the plan authority")
    return mirror


def baseline_requirements(work: dict[str, Any]) -> dict[str, Any]:
    environment = work.get("environment") or {}
    if work.get("requested_method") == SIE.PARAMETER_NAME:
        # 有界 ECO 只對 host 登記的既有來源做一次操作，不收集新的
        # ORFS baseline（那會是另一次真實工具執行）；驗收因此停在
        # UNVERIFIABLE，不宣稱工程達標。
        return {"package_id": "orfs_run_intelligence",
                "input_digest": (work.get("input") or {}).get("input_digest"),
                "baseline_mode": "NOT_REQUIRED", "required": []}
    return {"package_id": "orfs_run_intelligence",
            "input_digest": (work.get("input") or {}).get("input_digest"),
            "tool_identity": environment.get("tool_identity"),
            "design_config_digest": environment.get("design_config_digest"),
            "constraints_digest": environment.get("constraints_digest"),
            "required": ["receipt", "manifest", "dqe", "policy"]}


__all__ = ["authorization_check", "baseline_requirements", "capability",
           "compile_plan", "proposal_contract"]
