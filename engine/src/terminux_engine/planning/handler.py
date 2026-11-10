# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Plan-only request handler。

固定序列，中間不插入任何 tool selection、proposal creation 或 authorization：

    validate context/goal/mode
    → assemble authoritative bounded catalog
    → call isolated Plan service (tools=None)
    → parse model result
    → deterministic plan validation
    → build structured response

Handler 的依賴只有：LLM provider、`CapabilityRegistryView`（read-only）、
planning metadata exposures。它拿不到 executor、ActionRegistry、
ProposalRegistry、Kernel、SubprocessExecutor 或任何可寫的 store。

Vendor-neutral：這裡沒有任何工具名稱、metric 或 domain 語意。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from terminux_engine.planning.capability import CapabilitySnapshot
from terminux_engine.planning.plan import MAX_GOAL_CHARS
from terminux_engine.planning.profile import build_ai3_snapshot
from terminux_engine.planning.registry_view import CapabilityRegistryView
from terminux_engine.planning.service import (
    PLAN_ONLY_NOT_JSON,
    PLAN_ONLY_NO_RESPONSE,
    PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL,
    PLAN_ONLY_TOOL_CALL_FORBIDDEN,
    AgentRunMode,
    PlanOnlyPlanner,
    PlanRequest,
)

_LOG = logging.getLogger(__name__)

PLAN_ONLY_MODE = AgentRunMode.PLAN_ONLY.value

# ── error model ──────────────────────────────────────────────────
GOAL_REQUIRED = "PLAN_ONLY_GOAL_REQUIRED"
MODE_INVALID = "PLAN_ONLY_MODE_INVALID"
SERVICE_UNAVAILABLE = "PLAN_ONLY_SERVICE_UNAVAILABLE"
CATALOG_EMPTY = "PLAN_ONLY_CATALOG_EMPTY"
CATALOG_CONFLICT = "PLAN_ONLY_CATALOG_CONFLICT"
PROVIDER_UNAVAILABLE = "PLAN_ONLY_PROVIDER_UNAVAILABLE"
#: provider 有設定，但推論服務沒解析模型的 tool call。
PROVIDER_UNPARSED_TOOL_CALL = PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL
MODEL_TOOL_CALL_FORBIDDEN = "PLAN_ONLY_MODEL_TOOL_CALL_FORBIDDEN"
RESPONSE_PARSE_FAILED = "PLAN_ONLY_RESPONSE_PARSE_FAILED"
PLAN_INVALID = "PLAN_ONLY_PLAN_INVALID"
EXECUTION_CAPABILITY_PRESENT = "PLAN_ONLY_EXECUTION_CAPABILITY_PRESENT"
INTERNAL_ERROR = "PLAN_ONLY_INTERNAL_ERROR"

PLAN_ONLY_ERROR_CODES: tuple[str, ...] = (
    GOAL_REQUIRED, MODE_INVALID, SERVICE_UNAVAILABLE, CATALOG_EMPTY,
    CATALOG_CONFLICT, PROVIDER_UNAVAILABLE, PROVIDER_UNPARSED_TOOL_CALL,
    MODEL_TOOL_CALL_FORBIDDEN, RESPONSE_PARSE_FAILED, PLAN_INVALID, EXECUTION_CAPABILITY_PRESENT,
    INTERNAL_ERROR,
)

#: Plan service 的 denial code → 本層的穩定 error code。
_SERVICE_DENIAL_MAP = {
    PLAN_ONLY_TOOL_CALL_FORBIDDEN: MODEL_TOOL_CALL_FORBIDDEN,
    PLAN_ONLY_NO_RESPONSE: PROVIDER_UNAVAILABLE,
    PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL: PROVIDER_UNPARSED_TOOL_CALL,
    PLAN_ONLY_NOT_JSON: RESPONSE_PARSE_FAILED,
    "PLAN_ONLY_MODE_REQUIRED": MODE_INVALID,
}


@dataclass(frozen=True)
class PlanOnlyDependencies:
    """Plan-only path 的**完整**依賴集合。

    刻意不是一個通用 container：它只有這三樣東西，任何 executor 類的依賴
    都無處可放。
    """

    llm_provider: Any
    registry_view: CapabilityRegistryView
    exposures: tuple[tuple[tuple[str, ...], Callable[[str], dict[str, Any] | None] | None], ...]
    registry_snapshot_ref: str = ""

    def assert_no_execution_capability(self) -> None:
        forbidden = ("tool_registry", "action_registry", "proposal_registry",
                     "plan_registry", "kernel", "dispatch_engine",
                     "subprocess_executor", "event_store", "store", "executor")
        present = [name for name in forbidden if hasattr(self, name)]
        if present:
            raise RuntimeError(
                f"plan-only dependencies must not carry execution capability: {present}")
        self.registry_view.assert_read_only()


def _error(code: str, message: str, *, goal: str | None = None,
           snapshot_id: str | None = None, extra: dict | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "mode": PLAN_ONLY_MODE,
        "goal": goal,
        "plan": None,
        "validation": {"status": "not_run", "errors": []},
        "catalog_snapshot_id": snapshot_id,
        "execution_allowed": False,
        "proposal_created": False,
        "tool_calls": 0,
        "error_code": code,
        "message": message,
    }
    if extra:
        payload.update(extra)
    return payload


def assemble_snapshot(deps: PlanOnlyDependencies) -> CapabilitySnapshot:
    """每次 request 都從 runtime 權威來源重建，不讀任何 development sample。"""
    return build_ai3_snapshot(
        deps.registry_view,
        exposures=[(ids, provider) for ids, provider in deps.exposures],
        registry_snapshot_ref=deps.registry_snapshot_ref,
    )


def handle_plan_only(
    *,
    goal: Any,
    mode: Any,
    deps: PlanOnlyDependencies | None,
    observations: Iterable[str] = (),
    created_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """處理一次 plan-only request，回傳 machine-readable response。

    永不拋出：所有失敗都收斂成帶穩定 `error_code` 的 structured response，
    而且一律 `execution_allowed=False`、`proposal_created=False`、
    `tool_calls=0`。
    """
    # ── 1. mode（typed，且不得 fallback 到 normal Agent）─────────────
    mode_value = mode.value if isinstance(mode, AgentRunMode) else mode
    if mode_value != PLAN_ONLY_MODE:
        return _error(MODE_INVALID,
                      f"mode must be {PLAN_ONLY_MODE!r}; plan-only never falls back to "
                      f"the normal agent path",
                      goal=goal if isinstance(goal, str) else None)

    # ── 2. goal ──────────────────────────────────────────────────────
    if not isinstance(goal, str) or not goal.strip():
        return _error(GOAL_REQUIRED, "a non-empty goal is required")
    if len(goal) > MAX_GOAL_CHARS:
        return _error(GOAL_REQUIRED,
                      f"goal exceeds {MAX_GOAL_CHARS} characters", goal=goal[:200])

    if deps is None:
        return _error(SERVICE_UNAVAILABLE,
                      "the plan-only service is not configured on this daemon", goal=goal)

    try:
        deps.assert_no_execution_capability()
    except Exception as exc:  # noqa: BLE001
        _LOG.error("plan-only dependency isolation check failed: %r", exc)
        return _error(EXECUTION_CAPABILITY_PRESENT,
                      "the plan-only path is not properly isolated; refusing to plan",
                      goal=goal)

    # ── 3. 權威 catalog ──────────────────────────────────────────────
    try:
        snapshot = assemble_snapshot(deps)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("plan-only capability snapshot assembly failed: %r", exc)
        return _error(INTERNAL_ERROR, f"capability snapshot could not be assembled: {exc!r}",
                      goal=goal)

    if not snapshot.capabilities:
        return _error(CATALOG_EMPTY,
                      "no capability is available for planning on this daemon",
                      goal=goal, snapshot_id=snapshot.snapshot_id,
                      extra={"excluded": [dict(e) for e in snapshot.excluded]})

    conflicts = [{"capability_id": c.capability_id, "conflicts": [dict(x) for x in c.conflicts]}
                 for c in snapshot.capabilities if c.conflicts]
    if conflicts:
        # Conflict 代表 owner metadata 想覆寫 authority——fail closed，不猜。
        return _error(CATALOG_CONFLICT,
                      "capability metadata conflicts with the authoritative registry",
                      goal=goal, snapshot_id=snapshot.snapshot_id,
                      extra={"conflicts": conflicts})

    # ── 4./5./6. isolated service → parse → deterministic validation ─
    try:
        planner = PlanOnlyPlanner(deps.llm_provider, snapshot)
        planner.assert_no_execution_capability()
        result = planner.plan(PlanRequest(
            goal=goal, mode=AgentRunMode.PLAN_ONLY,
            observations=tuple(observations),
            created_from=dict(created_from or {}),
        ))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("plan-only planning failed: %r", exc)
        return _error(INTERNAL_ERROR, f"planning failed: {exc!r}",
                      goal=goal, snapshot_id=snapshot.snapshot_id)

    if result.denial_code is not None:
        code = _SERVICE_DENIAL_MAP.get(result.denial_code, INTERNAL_ERROR)
        message = f"plan-only request denied: {result.denial_code}"
        if code == PROVIDER_UNPARSED_TOOL_CALL and result.errors:
            # 沿用 provider 的完整訊息（PROVIDER_UNPARSED_TOOL_CALL: …；補救方式）。
            message = str(result.errors[0].get("message") or message)
        return _error(code, message,
                      goal=goal, snapshot_id=snapshot.snapshot_id,
                      extra={"repaired": result.repaired})

    plan = result.plan
    if plan is None:
        return _error(INTERNAL_ERROR, "the plan service returned no plan",
                      goal=goal, snapshot_id=snapshot.snapshot_id)

    # ── 7. structured response ───────────────────────────────────────
    response: dict[str, Any] = {
        "status": "valid" if plan.validation.valid else "invalid",
        "mode": PLAN_ONLY_MODE,
        "goal": goal,
        "plan": plan.to_json_dict(),
        "validation": plan.validation.to_json_dict(),
        "catalog_snapshot_id": snapshot.snapshot_id,
        # 由 server 固定，絕不採信 model 輸出。
        "execution_allowed": False,
        "proposal_created": False,
        "tool_calls": 0,
        "repaired": result.repaired,
        "message": ("Plan produced and validated. Nothing was executed."
                    if plan.validation.valid
                    else "The draft plan did not pass deterministic validation. "
                         "Nothing was executed."),
    }
    if not plan.validation.valid:
        response["error_code"] = PLAN_INVALID
    return response


__all__ = [
    "CATALOG_CONFLICT",
    "CATALOG_EMPTY",
    "EXECUTION_CAPABILITY_PRESENT",
    "GOAL_REQUIRED",
    "INTERNAL_ERROR",
    "MODEL_TOOL_CALL_FORBIDDEN",
    "MODE_INVALID",
    "PLAN_INVALID",
    "PLAN_ONLY_ERROR_CODES",
    "PLAN_ONLY_MODE",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_UNPARSED_TOOL_CALL",
    "PlanOnlyDependencies",
    "RESPONSE_PARSE_FAILED",
    "SERVICE_UNAVAILABLE",
    "assemble_snapshot",
    "handle_plan_only",
]
