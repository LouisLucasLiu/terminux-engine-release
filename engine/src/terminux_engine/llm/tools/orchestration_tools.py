# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""八項 core orchestration Agent tools。

直接對映 `OrchestrationService` 的八項 shared request facade
（`start_request`／`status_request`／`list_runs_request`／`pause_request`／
`resume_request`／`cancel_request`／`retry_request`／`abandon_request`）。
每個 executor 只呼叫注入的同一 service instance並原樣回傳結果；只有
`orchestration.status` 私下注入 `caller_kind="agent"` 以命中 Agent-only
per-run status cache。executor 使用明確 keyword-only 參數（而非 `**kwargs`），
缺少必要參數或多餘未知參數皆由 Python 呼叫綁定拋出 `TypeError`，交由既有
`ToolRegistry.execute()` 的 exception path 轉為 `ToolCallResult(success=False)`；
不建立第二套 schema validation 或 error envelope。
"""

from __future__ import annotations

from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolRegistry, ToolRisk, ToolSchema
from terminux_engine.orchestration.daemon_service import OrchestrationService

_START_SCHEMA = ToolSchema(
    name="orchestration.start",
    description=(
        "Start a new orchestration run for the given workflow. This call is "
        "non-blocking: it returns the new run's identity and its current "
        "durable state, but does not mean the workflow has finished running. "
        "Use orchestration.status with the returned run_id to check on "
        "progress afterward."
    ),
    parameters={
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "Workflow type to start.",
            },
            "config": {
                "type": "object",
                "description": "Workflow configuration object.",
            },
        },
        "required": ["workflow", "config"],
        "additionalProperties": False,
    },
)

_STATUS_SCHEMA = ToolSchema(
    name="orchestration.status",
    description=(
        "Return the current durable state of an orchestration run. Do not "
        "busy-poll this tool; when called by an Agent, the result may reflect "
        "state up to 1 second old due to an internal per-run cache."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run identity to query.",
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
)

_LIST_RUNS_SCHEMA = ToolSchema(
    name="orchestration.list_runs",
    description=(
        "Query orchestration runs, optionally filtered by workflow type or "
        "status. pending_review=true means the run is abandoned and is "
        "waiting for a human to decide how to dispose of it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "workflow_type": {
                "type": "string",
                "description": "Filter by workflow type; omit for all.",
            },
            "status": {
                "type": "string",
                "description": "Filter by run status; omit for all.",
            },
            "pending_review": {
                "type": "boolean",
                "description": (
                    "If true, only return abandoned runs awaiting manual "
                    "disposition (default false)."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum number of runs to return (default 20).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
)

_PAUSE_SCHEMA = ToolSchema(
    name="orchestration.pause",
    description=(
        "Propose a step-boundary pause request for a run. This only submits "
        "the request; it does not mean the current step has already stopped."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run identity to pause.",
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
)

_RESUME_SCHEMA = ToolSchema(
    name="orchestration.resume",
    description=(
        "Request that a paused run be resumed. The result only indicates "
        "whether this caller's spawn request was accepted; it does not mean "
        "a child process has claimed ownership or that the run has actually "
        "resumed executing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run identity to resume.",
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
)

_CANCEL_SCHEMA = ToolSchema(
    name="orchestration.cancel",
    description=(
        "Request that a run be cancelled. Defaults to a soft, step-boundary "
        "cancel; immediate=true uses an ownership-first termination path. "
        "The acknowledgement and reported mode do not mean termination has "
        "completed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run identity to cancel.",
            },
            "immediate": {
                "type": "boolean",
                "description": (
                    "If true, request ownership-first immediate termination "
                    "instead of the default soft cancel (default false)."
                ),
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
)

_RETRY_SCHEMA = ToolSchema(
    name="orchestration.retry",
    description=(
        "Retry an eligible terminal parent run by forking a new child run. "
        "step_id, if given, is an exact checkpoint-head validation and is "
        "not a rewind selector. Returns both the parent run and the "
        "resulting child run id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The terminal parent run identity to retry.",
            },
            "step_id": {
                "type": "string",
                "description": (
                    "Optional checkpoint-head step id for exact optimistic "
                    "validation; omit to skip this validation."
                ),
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
)

_ABANDON_SCHEMA = ToolSchema(
    name="orchestration.abandon",
    description=(
        "Dismiss an abandoned run that is pending human review. This only "
        "clears the pending-review disposition; it does not reopen the run, "
        "delete it, or change its result to success or failure."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The abandoned run identity to dismiss.",
            },
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
)


def _create_start(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, workflow: str, config: dict[str, Any]) -> Any:
        return service.start_request(workflow, config)

    return executor


def _create_status(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, run_id: str) -> Any:
        return service.status_request(run_id, caller_kind="agent")

    return executor


def _create_list_runs(service: OrchestrationService) -> Callable[..., Any]:
    def executor(
        *,
        workflow_type: str | None = None,
        status: str | None = None,
        pending_review: bool = False,
        limit: int = 20,
    ) -> Any:
        return service.list_runs_request(
            workflow_type=workflow_type,
            status=status,
            pending_review=pending_review,
            limit=limit,
        )

    return executor


def _create_pause(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, run_id: str) -> Any:
        return service.pause_request(run_id)

    return executor


def _create_resume(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, run_id: str) -> Any:
        return service.resume_request(run_id)

    return executor


def _create_cancel(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, run_id: str, immediate: bool = False) -> Any:
        return service.cancel_request(run_id, immediate=immediate)

    return executor


def _create_retry(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, run_id: str, step_id: str | None = None) -> Any:
        return service.retry_request(run_id, step_id=step_id)

    return executor


def _create_abandon(service: OrchestrationService) -> Callable[..., Any]:
    def executor(*, run_id: str) -> Any:
        return service.abandon_request(run_id)

    return executor


def register_orchestration_tools(
    registry: ToolRegistry,
    *,
    service: OrchestrationService,
) -> None:
    """註冊八項 `orchestration.*` core Agent tools 到 registry。

    只使用注入的同一 `service` instance；不建立第二個 OrchestrationService、
    ledger、socket 或 daemon client。Duplicate registration 沿用
    `ToolRegistry.register()` 既有 `ValueError`，不 silent overwrite。
    """
    registry.register("orchestration.start", _create_start(service), _START_SCHEMA, risk=ToolRisk.DANGEROUS)
    registry.register("orchestration.status", _create_status(service), _STATUS_SCHEMA, risk=ToolRisk.SAFE)
    registry.register(
        "orchestration.list_runs", _create_list_runs(service), _LIST_RUNS_SCHEMA, risk=ToolRisk.SAFE
    )
    registry.register("orchestration.pause", _create_pause(service), _PAUSE_SCHEMA, risk=ToolRisk.DANGEROUS)
    registry.register("orchestration.resume", _create_resume(service), _RESUME_SCHEMA, risk=ToolRisk.DANGEROUS)
    registry.register("orchestration.cancel", _create_cancel(service), _CANCEL_SCHEMA, risk=ToolRisk.DANGEROUS)
    registry.register("orchestration.retry", _create_retry(service), _RETRY_SCHEMA, risk=ToolRisk.DANGEROUS)
    registry.register(
        "orchestration.abandon", _create_abandon(service), _ABANDON_SCHEMA, risk=ToolRisk.DANGEROUS
    )
