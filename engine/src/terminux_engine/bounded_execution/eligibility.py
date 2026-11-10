# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Step eligibility 與 catalog drift（approved package action、G-4）。

全部 deterministic：不呼叫 LLM、不猜、不自動修補。任一項不成立即回明確的
denial code。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: approved package action 的 execution allowlist。這是**硬編碼的白名單**：
#: 即使某個 capability 出現在 catalog 裡，不在這裡就不可執行。
ALLOWED_EXECUTION_CAPABILITIES: frozenset[str] = frozenset({
    "orfs_run_intelligence.explain_run",
    "orfs_run_intelligence.compare_runs",
    # 固定 flow template 的兩個內部步驟。加進 allowlist 只是讓它們
    # **可以**被派送；真正的限制在別處且未放寬——兩者都是 DANGEROUS、都
    # 只接受 task-owned proposal ＋ live grant，而且 action 本身在拿不到
    # host binding 時一律 fail closed。
    "orfs_run_intelligence.prepare_variant",
    "orfs_run_intelligence.prepare_timing_repair",
    "orfs_run_intelligence.run_flow",
})

_JSON_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "object": (dict,), "array": (list,),
}


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    denial_code: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _deny(code: str, message: str, **details: Any) -> EligibilityResult:
    return EligibilityResult(False, code, message, details)


def validate_arguments_against_schema(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> list[str]:
    """對 Tool Registry 的 JSON Schema 子集做最小、deterministic 的驗證。

    與 plan-only planner validator 同一套規則；無法理解的 schema 一律回報，不靜默放行。
    """
    problems: list[str] = []
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ["capability input schema is not a JSON object schema"]
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ["capability input schema has no properties map"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        problems.append(f"unknown argument(s): {unknown}")
    for name in schema.get("required") or []:
        if name not in arguments:
            problems.append(f"missing required argument: {name}")
    for name, value in arguments.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        checks = _JSON_TYPE_CHECKS.get(expected) if isinstance(expected, str) else None
        if checks is None:
            continue
        if isinstance(value, bool) and bool not in checks:
            problems.append(f"argument {name} must be {expected}")
        elif not isinstance(value, checks):
            problems.append(f"argument {name} must be {expected}")
    return problems


def check_capability_eligibility(
    *,
    capability_id: str,
    capability: dict[str, Any] | None,
    arguments: dict[str, Any],
    expected_risk: str,
) -> EligibilityResult:
    """與 plan snapshot 無關的 capability 層檢查（allowlist／schema／risk）。

    `check_step_eligibility()`（approved package action plan step）與 analysis task 的固定兩步
    都委派到這裡，確保兩條路徑套用同一份規則，而不是各寫一套。
    """
    from terminux_engine.bounded_execution.errors import (
        PLAN_CAPABILITY_NOT_EXECUTABLE,
        PLAN_CATALOG_DRIFT_REPLAN_REQUIRED,
        PLAN_STEP_NOT_ELIGIBLE,
    )

    if capability_id not in ALLOWED_EXECUTION_CAPABILITIES:
        return _deny(PLAN_CAPABILITY_NOT_EXECUTABLE,
                     f"{capability_id!r} is not in the approved-action v1 execution allowlist",
                     allowlist=sorted(ALLOWED_EXECUTION_CAPABILITIES))
    if capability is None:
        return _deny(PLAN_CATALOG_DRIFT_REPLAN_REQUIRED,
                     f"{capability_id!r} is no longer registered")

    problems = validate_arguments_against_schema(
        capability.get("input_schema") or {}, arguments)
    if problems:
        return _deny(PLAN_STEP_NOT_ELIGIBLE,
                     "arguments do not satisfy the current input schema",
                     problems=problems)

    if str(capability.get("risk")) != str(expected_risk):
        return _deny(PLAN_STEP_NOT_ELIGIBLE,
                     "the capability risk changed since the plan was produced",
                     plan_risk=expected_risk, current_risk=capability.get("risk"))
    if not capability.get("approval_required"):
        return _deny(PLAN_STEP_NOT_ELIGIBLE,
                     "approved actions only propose capabilities that require explicit approval")
    return EligibilityResult(True, details={"capability_id": capability_id})


def check_step_eligibility(
    *,
    snapshot: Any,
    step_id: str,
    capability: dict[str, Any] | None,
    current_catalog_snapshot_id: str,
    completed_step_ids: set[str],
    proposed_step_ids: set[str],
    requesting_identity: tuple[str, str, str],
    triggered_stop_conditions: list[str] | None = None,
) -> EligibilityResult:
    """Proposal 建立前的 deterministic 檢查。

    `snapshot` 是 `PlanSnapshot`；`capability` 是**當前** Tool Registry
    組出的 normalized capability（`None` 代表已不存在）。
    """
    from terminux_engine.bounded_execution.errors import (
        PLAN_CATALOG_DRIFT_REPLAN_REQUIRED,
        PLAN_DEPENDENCY_NOT_SATISFIED,
        PLAN_DRAFT_IDENTITY_MISMATCH,
        PLAN_STEP_ALREADY_EXECUTED,
        PLAN_STEP_ALREADY_PROPOSED,
        PLAN_STEP_NOT_ELIGIBLE,
        PLAN_STEP_NOT_FOUND,
        PLAN_STOP_CONDITION_REACHED,
    )

    member_id, project_id, session_id = requesting_identity
    created_by = dict(getattr(snapshot, "created_by", {}) or {})
    if (created_by.get("member_id"), created_by.get("project_id"),
            created_by.get("session_id")) != (member_id, project_id, session_id):
        return _deny(PLAN_DRAFT_IDENTITY_MISMATCH,
                     "the requesting identity does not match the plan snapshot owner")

    # ── catalog drift：任何差異都要求 replan（不做 semantic diff）
    if current_catalog_snapshot_id != snapshot.catalog_snapshot_id:
        return _deny(PLAN_CATALOG_DRIFT_REPLAN_REQUIRED,
                     "the capability catalog changed since the plan was produced; "
                     "produce a new plan",
                     plan_catalog=snapshot.catalog_snapshot_id,
                     current_catalog=current_catalog_snapshot_id)

    step = snapshot.step(step_id)
    if step is None:
        return _deny(PLAN_STEP_NOT_FOUND, f"step {step_id!r} is not in this plan")
    if sum(1 for s in snapshot.steps if s.get("step_id") == step_id) != 1:
        return _deny(PLAN_STEP_NOT_ELIGIBLE, f"step id {step_id!r} is not unique")

    if step_id in completed_step_ids:
        return _deny(PLAN_STEP_ALREADY_EXECUTED,
                     f"step {step_id!r} already has a terminal execution receipt")
    if step_id in proposed_step_ids:
        return _deny(PLAN_STEP_ALREADY_PROPOSED,
                     f"step {step_id!r} already has a live proposal")

    capability_id = step.get("capability")
    capability_check = check_capability_eligibility(
        capability_id=capability_id, capability=capability,
        arguments=step.get("arguments") or {}, expected_risk=str(step.get("risk")),
    )
    if not capability_check.eligible:
        return capability_check

    for dependency in step.get("depends_on") or []:
        if dependency not in completed_step_ids:
            return _deny(PLAN_DEPENDENCY_NOT_SATISFIED,
                         f"step {step_id!r} depends on {dependency!r}, which has no "
                         f"durable completed receipt yet")

    if triggered_stop_conditions:
        return _deny(PLAN_STOP_CONDITION_REACHED,
                     "a deterministic stop condition is already satisfied",
                     conditions=list(triggered_stop_conditions))

    return EligibilityResult(True, details={"step": step, "capability_id": capability_id})


__all__ = [
    "ALLOWED_EXECUTION_CAPABILITIES",
    "EligibilityResult",
    "check_capability_eligibility",
    "check_step_eligibility",
    "validate_arguments_against_schema",
]
