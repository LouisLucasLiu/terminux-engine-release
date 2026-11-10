# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""已 claim proposal 的共用派送段。

從 `ipc.jsonrpc_server._execute_approved_proposal()` 原封抽出**claim 之後**
的那一段，讓既有的單筆 operator 路徑與新的 analysis task 路徑共用同一條
派送權威。抽出的範圍精確地是：

    drift revalidation → proposal-bound `ToolAuthorizationPolicy`
    → `ToolRegistry.execute()`（dangerous-tool gate）
    → `ProposalRegistry.finalize_execution()`（恰好一次）

**不含 claim**。claim 留在各自的 caller，因為兩條路徑對 claim 前後的順序
要求不同：task 路徑必須先把 dispatch intent 落盤，才可以 claim，
單筆 operator 路徑則維持既有順序。把 claim 也搬進來會讓這個約束無法表達。

與抽出前的行為差異只有一處，且是刻意修正的：`timed_out` 不再是寫死的
`False`，而是 `ToolCallResult.timed_out` 的真值；並額外回報
`worker_completion`——worker 是否**確定**已結束。timeout 只代表「我們不再
等」，不代表 action 已停止，因此 timeout 的結果一律是
`worker_completion=False`，呼叫端不得當成普通失敗。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from terminux_engine.llm.tool_safety import (
    ProposalExecutionBinding,
    ToolAuthorizationPolicy,
    canonicalize_arguments,
    reset_current_tool_authorization_policy,
    set_current_tool_authorization_policy,
)

EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
ACTION_SCHEMA_DRIFT = "ACTION_SCHEMA_DRIFT"


class _AuditSink(Protocol):
    def __call__(self, event: str, /, **fields: Any) -> None: ...


@dataclass(frozen=True)
class DispatchDeps:
    """派送一次已 claim proposal 所需的全部依賴。

    全部由 caller 注入；這個模組不讀任何 module-level global，因此
    daemon composition 與測試看到的是同一段程式碼。
    """

    proposal_registry: Any
    tools_registry: Any
    #: `() -> list[capability]`，capability 需有 `action_name` 與 `parameters`。
    catalog_provider: Callable[[], Any]
    #: `(parameters, arguments) -> bool`
    validate_arguments: Callable[[Any, dict[str, Any]], bool]
    unwrap_result: Callable[[Any], Any]
    audit: _AuditSink
    #: 寫進 audit 的來源標記，用來分辨單筆 operator 與 task child。
    policy_source: str = "proposal_bound_runtime_policy"
    request_policy_source: str = "operator_control_command"
    plan_registry: Any | None = None


@dataclass(frozen=True)
class DispatchOutcome:
    """一次派送的完整結果。

    `execution_status` 只有三個值：

    - ``SUCCEEDED`` / ``FAILED``：worker 已回報，`finalize_execution()` 已寫入。
    - ``DENIED``：連 executor 都沒進去（drift、依賴缺失）。

    `worker_completion` 為 False 時，**不可**把這次結果當成「什麼都沒發生」。
    """

    execution_status: str
    denial_code: str | None
    timed_out: bool
    worker_completion: bool
    #: action 的完整結果（未截斷）。失敗時是 error 內容。
    full_result: Any
    proposal_view: dict[str, Any] | None
    action_name: str | None = None
    elapsed_sec: float = 0.0
    audit_events: tuple[str, ...] = field(default=())

    @property
    def succeeded(self) -> bool:
        return self.execution_status == "SUCCEEDED"


def dispatch_claimed_proposal(
    deps: DispatchDeps,
    *,
    proposal_id: str,
    proposal_digest: str,
    claimed: dict[str, Any],
    member_id: str,
    project_id: str,
    session_id: str,
) -> DispatchOutcome:
    """派送一筆**已經 claim 成功**的 proposal。

    `claimed` 是 `ProposalRegistry.claim_for_execution()` 回傳的那一份；
    真正被執行的 action 與 arguments 一律以它為準，不接受 caller 另外傳入
    的版本。
    """
    action_name = claimed["action_name"]
    arguments = claimed["arguments"]
    if deps.plan_registry is not None:
        deps.plan_registry.on_step_execution_started(proposal_id)

    # registry/schema drift 檢查：proposal 建立當下的 arguments 必須仍通過
    # *現在* 的 catalog／parameter schema，否則直接 FAIL——approval 已被
    # claim 消耗，不回到 APPROVED，也不自動套用新 schema 重試。
    catalog = deps.catalog_provider()
    match = next((c for c in catalog if c.action_name == action_name), None)
    if match is None or not deps.validate_arguments(match.parameters, arguments):
        finalized = deps.proposal_registry.finalize_execution(
            proposal_id, status="FAILED", result={"error": ACTION_SCHEMA_DRIFT},
        )
        deps.audit("action.execution.failed", tool_call_id=proposal_id,
                   tool_name=action_name, classification="DANGEROUS",
                   denial_code=ACTION_SCHEMA_DRIFT, executor_entered=False,
                   policy_source=deps.request_policy_source)
        if deps.plan_registry is not None:
            deps.plan_registry.on_step_execution_finalized(proposal_id, "FAILED", None)
        return DispatchOutcome(
            execution_status="FAILED", denial_code=ACTION_SCHEMA_DRIFT,
            timed_out=False, worker_completion=True,
            full_result={"error": ACTION_SCHEMA_DRIFT}, proposal_view=finalized,
            action_name=action_name,
            audit_events=("action.execution.failed",),
        )

    # authorization policy 的建立與安裝**不是**注入點：它是這條路徑的唯一
    # 權威，全 `terminux_engine` 只有這裡會安裝 policy。
    binding = ProposalExecutionBinding(
        proposal_id=proposal_id, proposal_digest=proposal_digest,
        action_name=action_name,
        canonical_arguments=canonicalize_arguments(arguments),
        member_id=member_id, project_id=project_id, session_id=session_id,
    )
    policy = ToolAuthorizationPolicy(
        allowed_dangerous_tools=frozenset({action_name}), proposal_binding=binding,
    )
    token = set_current_tool_authorization_policy(policy)
    try:
        deps.audit("action.execution.authorized", tool_call_id=proposal_id,
                   tool_name=action_name, classification="DANGEROUS", decision="ALLOW",
                   executor_entered=False, policy_source=deps.policy_source)
        deps.audit("action.execution.started", tool_call_id=proposal_id,
                   tool_name=action_name, classification="DANGEROUS",
                   executor_entered=True, policy_source=deps.policy_source)
        result = deps.tools_registry.execute(
            action_name, arguments, tool_call_id=proposal_id,
        )
    finally:
        reset_current_tool_authorization_policy(token)

    timed_out = bool(getattr(result, "timed_out", False))
    elapsed = float(getattr(result, "elapsed_sec", 0.0) or 0.0)

    if result.success:
        full_result = deps.unwrap_result(result.result)
        finalized = deps.proposal_registry.finalize_execution(
            proposal_id, status="SUCCEEDED", result=full_result,
        )
        deps.audit("action.execution.succeeded", tool_call_id=proposal_id,
                   tool_name=action_name, classification="DANGEROUS",
                   executor_entered=True, result_status="success",
                   result_value=result.result, policy_source=deps.policy_source)
        status, event = "SUCCEEDED", "action.execution.succeeded"
    else:
        full_result = deps.unwrap_result(result.error)
        finalized = deps.proposal_registry.finalize_execution(
            proposal_id, status="FAILED", result=full_result,
        )
        deps.audit("action.execution.failed", tool_call_id=proposal_id,
                   tool_name=action_name, classification="DANGEROUS",
                   executor_entered=True, result_status="error",
                   result_value=result.error, policy_source=deps.policy_source)
        status, event = "FAILED", "action.execution.failed"

    if deps.plan_registry is not None:
        deps.plan_registry.on_step_execution_finalized(
            proposal_id, status,
            (finalized or {}).get("trusted_result") if finalized else None,
        )

    return DispatchOutcome(
        execution_status=status, denial_code=None, timed_out=timed_out,
        # timeout 表示 outer wait 放棄了，worker thread 可能仍在跑：
        # 這種情況下 worker 的完成與否**未知**，一律不宣稱已完成。
        worker_completion=not timed_out,
        full_result=full_result,
        proposal_view=finalized, action_name=action_name, elapsed_sec=elapsed,
        audit_events=("action.execution.authorized", "action.execution.started", event),
    )


__all__ = [
    "ACTION_SCHEMA_DRIFT",
    "EXECUTION_UNAVAILABLE",
    "DispatchDeps",
    "DispatchOutcome",
    "dispatch_claimed_proposal",
]
