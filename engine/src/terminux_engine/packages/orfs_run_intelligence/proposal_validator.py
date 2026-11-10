# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""domain proposal 的 ORFS 方案格式純驗證器；不建立 task、binding 或任何副作用。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks.canonical import sha256_payload
from terminux_engine.packages.orfs_run_intelligence.core_utilization import (
    METHOD_ID as CORE_UTILIZATION_METHOD_ID, PARAMETER as CORE_UTILIZATION_PARAMETER,
    CoreUtilizationError,
)
from terminux_engine.packages.orfs_run_intelligence import single_instance_eco as SIE

PROPOSAL_SCHEMA_VERSION = "orfs.eap_proposal.v1"
VALIDATOR_VERSION = "orfs.eap_proposal_validator.v1"
PROPOSAL_ADAPTER_VERSION = "orfs.eap_proposal_adapter.v1"
CHANGE_KIND_TIMING_PARAMETER = "timing_parameter"
CHANGE_KIND_CORE_UTILIZATION = "core_utilization"
SUPPORTED_PARAMETERS = frozenset({"SETUP_SLACK_MARGIN"})
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


@dataclass(frozen=True)
class ProposalValidation:
    passed: bool
    code: str | None = None
    message: str | None = None
    error_path: str | None = None
    expected: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        result = {"status": "PASSED" if self.passed else "REJECTED",
                  "validator_version": VALIDATOR_VERSION, "reason_code": self.code,
                  "message": self.message}
        if self.error_path is not None:
            result["error_path"] = self.error_path
        if self.expected is not None:
            result["expected"] = self.expected
        return result


def validate_proposal(proposal: dict[str, Any]) -> ProposalValidation:
    """驗證可規劃的設定修改；通過絕不表示該 action 已可執行。"""
    if proposal.get("package_schema_version") != PROPOSAL_SCHEMA_VERSION:
        return ProposalValidation(
            False, "PROPOSAL_SCHEMA_VERSION_INVALID",
            "unsupported package schema version", "$.package_schema_version",
            f'"{PROPOSAL_SCHEMA_VERSION}"')
    changes = proposal.get("changes")
    # single_instance_cell_swap 用完全不同的 `changes` 鍵集合（見
    # `single_instance_eco.REQUIRED_CHANGE_KEYS`），不是既有 timing_parameter／
    # core_utilization 三鍵格式的變體，因此在共用格式假設之前先分流。
    if isinstance(changes, dict) and changes.get("kind") == SIE.CHANGE_KIND:
        return _validate_single_instance_eco_proposal(proposal, changes)
    if not isinstance(changes, dict) or set(changes) != {"kind", "parameter", "candidate"}:
        return ProposalValidation(
            False, "CHANGE_FORMAT_INVALID",
            "changes must be an object containing exactly kind, parameter and candidate",
            "$.changes",
            '{"kind":"timing_parameter","parameter":"SETUP_SLACK_MARGIN",'
            '"candidate":"<non-negative decimal string>"}')
    core_utilization = (changes["kind"] == CHANGE_KIND_CORE_UTILIZATION
                        and changes["parameter"] == CORE_UTILIZATION_PARAMETER)
    if (changes["kind"] != CHANGE_KIND_TIMING_PARAMETER
            and not core_utilization) or (not core_utilization
                                          and changes["parameter"] not in SUPPORTED_PARAMETERS):
        return ProposalValidation(
            False, "CHANGE_UNSUPPORTED",
            "the Package does not support this change type or parameter",
            "$.changes", "kind=timing_parameter; parameter=SETUP_SLACK_MARGIN")
    candidate = changes["candidate"]
    if core_utilization:
        context = proposal.get("revision_context")
        if not isinstance(context, dict) or set(context) != {
                "previous_executed_value", "previous_attempt_id",
                "executed_candidates", "allowed_candidate_minimum",
                "allowed_candidate_maximum"}:
            return ProposalValidation(False, "REVISION_CONTEXT_INVALID",
                                      "core utilization needs an exact revision context",
                                      "$.revision_context", "the Package-owned five fields")
        try:
            executed = context["executed_candidates"]
            attempts = context.get("executed_attempts")
            # proposal context deliberately carries only public values; the Runtime
            # supplies identities to authorization. Validate the visible window here.
            if (isinstance(candidate, bool) or not isinstance(candidate, int)
                    or isinstance(executed, bool) or not isinstance(executed, list)
                    or any(isinstance(value, bool) or not isinstance(value, int)
                           for value in executed)):
                raise CoreUtilizationError("candidate context is not integer-only")
            previous = context["previous_executed_value"]
            if isinstance(previous, bool) or not isinstance(previous, int):
                raise CoreUtilizationError("previous value is invalid")
            attempt_id = context["previous_attempt_id"]
            if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id):
                raise CoreUtilizationError("previous attempt identity is invalid")
            if (not executed and (previous != 55 or attempt_id is not None)) or (
                    executed and (previous != executed[-1] or attempt_id is None)):
                raise CoreUtilizationError("previous candidate lineage differs")
            expected_minimum = max(35, previous - 10)
            if (context["allowed_candidate_minimum"] != expected_minimum
                    or context["allowed_candidate_maximum"] != previous - 1):
                raise CoreUtilizationError("published candidate window differs")
            if candidate in executed or not expected_minimum <= candidate <= previous - 1:
                raise CoreUtilizationError("candidate violates published window")
        except (CoreUtilizationError, KeyError):
            return ProposalValidation(False, "CORE_UTILIZATION_WINDOW_INVALID",
                                      "candidate or revision context violates the public bound",
                                      "$.changes.candidate", "exact integer in published range")
    elif not isinstance(candidate, str) or not _DECIMAL.fullmatch(candidate):
        return ProposalValidation(
            False, "CANDIDATE_INVALID",
            "candidate must be a non-negative decimal token",
            "$.changes.candidate", 'a decimal string such as "0.05"')
    steps = proposal.get("steps")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        return ProposalValidation(
            False, "STEP_FORMAT_INVALID",
            "one declarative materialization step is required", "$.steps",
            "an array containing exactly one materialization step")
    step = steps[0]
    expected_operation = ("orfs.eap.materialize_core_utilization"
                          if core_utilization else "orfs.eap.materialize_timing_parameter")
    if step.get("operation") != expected_operation or step.get("depends_on", []) != []:
        return ProposalValidation(
            False, "STEP_UNSUPPORTED",
            "the declarative FlowTask materialization step is required", "$.steps[0]",
            f"operation={expected_operation}; depends_on=[]")
    args = step.get("arguments")
    if args != {"parameter": changes["parameter"], "candidate": candidate}:
        return ProposalValidation(
            False, "STEP_CHANGE_MISMATCH",
            "step arguments must exactly match changes", "$.steps[0].arguments",
            "the same parameter and candidate as $.changes")
    return ProposalValidation(True)


def _validate_single_instance_eco_proposal(
    proposal: dict[str, Any], changes: dict[str, Any],
) -> ProposalValidation:
    """`single_instance_cell_swap` 的格式與固定範圍檢查。

    靜態範圍（key 集合、stage／library 允許值、buffer 白名單、no-op 拒絕）
    交給 `single_instance_eco.validate_single_instance_swap_change()`，這裡
    只補這一層獨有的：宣告式材料化 step 是否與 `changes` 完全一致。來源
    漂移核對（提案宣稱的 `expected_cell` 是否真的符合目前 instance）不在
    這裡——那需要 host 已驗證的 instance 快照，只能在材料化前、有 snapshot
    可查時做（`single_instance_eco.check_source_drift()`）。
    """
    check = SIE.validate_single_instance_swap_change(changes)
    if not check.passed:
        return ProposalValidation(
            False, check.reason_codes[0].upper(), check.reasons[0], "$.changes",
            f"an object with exactly {sorted(SIE.REQUIRED_CHANGE_KEYS)}")
    steps = proposal.get("steps")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        return ProposalValidation(
            False, "STEP_FORMAT_INVALID",
            "one declarative materialization step is required", "$.steps",
            "an array containing exactly one materialization step")
    step = steps[0]
    if (step.get("operation") != SIE.MATERIALIZE_OPERATION
            or step.get("depends_on", []) != []):
        return ProposalValidation(
            False, "STEP_UNSUPPORTED",
            "the declarative FlowTask materialization step is required", "$.steps[0]",
            f"operation={SIE.MATERIALIZE_OPERATION}; depends_on=[]")
    expected_args = {"target_instance": changes["target_instance"],
                     "expected_cell": changes["expected_cell"],
                     "replacement_cell": changes["replacement_cell"]}
    if step.get("arguments") != expected_args:
        return ProposalValidation(
            False, "STEP_CHANGE_MISMATCH",
            "step arguments must exactly match changes", "$.steps[0].arguments",
            "target_instance, expected_cell and replacement_cell from $.changes")
    return ProposalValidation(True)


def adapt_and_validate_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """只將無歧義的單元素 ``changes`` array 轉為正式 object 格式。"""
    changes = proposal.get("changes")
    normalized = proposal
    adaptation_status = "UNCHANGED"
    adaptation_reason: str | None = None
    if isinstance(changes, list):
        if len(changes) != 1 or not isinstance(changes[0], dict):
            return {
                "status": "REJECTED", "validator_version": VALIDATOR_VERSION,
                "reason_code": "CHANGE_ARRAY_AMBIGUOUS",
                "message": ("changes array can only be adapted when it contains "
                            "exactly one object"),
                "error_path": "$.changes",
                "expected": "one object; no values may be guessed or discarded",
                "proposal_adapter_version": PROPOSAL_ADAPTER_VERSION,
                "adaptation_status": "REJECTED",
            }
        normalized = {**proposal, "changes": dict(changes[0])}
        adaptation_status = "ADAPTED"
        adaptation_reason = "SINGLE_CHANGE_ARRAY_UNWRAPPED"
    result = validate_proposal(normalized).to_json_dict()
    result.update({
        "proposal_adapter_version": PROPOSAL_ADAPTER_VERSION,
        "adaptation_status": adaptation_status,
    })
    if adaptation_reason is not None:
        result["adaptation_reason"] = adaptation_reason
        result["source_proposal_digest"] = sha256_payload(proposal)
        result["normalized_proposal_digest"] = sha256_payload(normalized)
        result["normalized_proposal"] = normalized
    return result


def flow_task_binding(proposal: dict[str, Any]) -> dict[str, Any]:
    """把已驗證的 Package 方案轉為 FlowTask 可固定的資料。

    這裡只描述 ORFS 的方案格式與工程限制；不建立 task、grant、host binding
    或 candidate source。通過純驗證也絕不代表可執行。
    """
    validation = validate_proposal(proposal)
    if not validation.passed:
        raise ValueError(validation.code or "PACKAGE_PROPOSAL_REJECTED")
    changes = proposal["changes"]
    if changes.get("kind") == SIE.CHANGE_KIND:
        # 沒有單一數值候選；`parameter_name` 固定用方法自己的名稱，`candidate`
        # 取這個提案唯一在改的那個 instance（與 `MethodExtension.parameter`
        # 同一個常數來源，見 `single_instance_eco.PARAMETER_NAME`）。
        parameter_name, candidate = SIE.PARAMETER_NAME, changes["target_instance"]
    else:
        parameter_name, candidate = changes["parameter"], changes["candidate"]
    return {
        "package_schema_version": PROPOSAL_SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "proposal_digest": sha256_payload(proposal),
        "parameter_name": parameter_name,
        "candidate": candidate,
        "changes": dict(changes),
        "steps": [dict(step) for step in proposal["steps"]],
        "execution_binding": "NOT_IMPLEMENTED",
        "execution_allowed": False,
    }


__all__ = ["CHANGE_KIND_CORE_UTILIZATION", "CHANGE_KIND_TIMING_PARAMETER", "CORE_UTILIZATION_METHOD_ID", "PROPOSAL_ADAPTER_VERSION",
           "PROPOSAL_SCHEMA_VERSION", "SUPPORTED_PARAMETERS", "VALIDATOR_VERSION",
           "ProposalValidation", "adapt_and_validate_proposal", "flow_task_binding",
           "validate_proposal"]
