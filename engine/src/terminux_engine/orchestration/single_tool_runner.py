# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Domain-blind「執行一次工具」的最小 `DomainRunner`。

不屬於任何特定 vendor 或 domain——`workflow_config` 只接受
`tool_id`／`parameters`／`cwd`／`step_id`（選填）這四個完全 generic 的
概念，適用於 ORFS、`icc2_shell`，或未來任何走同一套 vendor-neutral
execution contract 的 tool。不得在本檔案內出現任何 ORFS／Docker 具名
邏輯。

完全重用既有 `OrchestrationKernel`／`DomainRunner` Protocol，
不新增第二套 lifecycle 或 terminal-state 判斷機制——本 runner 只負責回答
「下一步是什麼」「這一步結束了嗎」，實際的 dispatch／evidence／retry／
cancel／resume 語意完全由既有 Kernel 決定。
"""

from __future__ import annotations

import uuid
from typing import Any

from terminux_engine.orchestration.domain_runner import (
    DomainRunState,
    NextStepPlan,
    ToolSpec,
)
from terminux_engine.orchestration.outcome import DispatchOutcome

WORKFLOW_TYPE = "single_tool_execution"

# 對應 `kernel.py::_KNOWN_FAILURE_TERMINAL_REASONS` 需要收錄的字串（見該檔
# 的既有註解：非失敗即視為 completed，未列出的字串一律預設成功）。
TERMINAL_REASON_COMPLETED = "single_tool_execution_completed"
TERMINAL_REASON_FAILED = "single_tool_execution_failed"


class SingleToolExecutionConfigError(ValueError):
    """`workflow_config` 缺少必要欄位或型別不正確。"""


def validate_single_tool_workflow_config(config: dict[str, Any]) -> None:
    for key in ("tool_id", "parameters", "cwd"):
        if key not in config:
            raise SingleToolExecutionConfigError(f"missing required key: {key!r}")
    if not isinstance(config["tool_id"], str) or not config["tool_id"]:
        raise SingleToolExecutionConfigError("tool_id must be a non-empty string")
    if not isinstance(config["parameters"], dict):
        raise SingleToolExecutionConfigError("parameters must be a mapping")
    if not isinstance(config["cwd"], str) or not config["cwd"]:
        raise SingleToolExecutionConfigError("cwd must be a non-empty string")


class SingleToolRunner:
    """一次 `run()` 只執行一個 `ToolSpec`，然後依 exit code 決定終態。"""

    workflow_type = WORKFLOW_TYPE

    def start_run(self, config: dict[str, Any]) -> DomainRunState:
        validate_single_tool_workflow_config(config)
        step_id = config.get("step_id") or "step_1"
        # `event_metadata`（選填）：完全 opaque、由呼叫端決定內容的 dict，
        # 原樣附加到 TimelineEvent.metadata（見
        # `kernel.py::_build_timeline_event`）。Runner 本身不解讀內容——
        # 只是 domain-blind 的透傳欄位，讓需要額外 metadata 的 package
        # （例如既有 DQE package 讀取 `event.metadata["dqe_db_path"]`）
        # 能收到它，不代表 SingleToolRunner 認識任何 DQE／domain 概念。
        event_metadata = config.get("event_metadata") or {}
        if not isinstance(event_metadata, dict):
            raise SingleToolExecutionConfigError("event_metadata must be a mapping")
        return DomainRunState(
            domain_run_id=str(uuid.uuid4()),
            iteration=0,
            metadata={
                "tool_id": config["tool_id"],
                "parameters": dict(config["parameters"]),
                "cwd": config["cwd"],
                "step_id": step_id,
                "phase": "pending",
                "last_exit_code": None,
                "event_metadata": dict(event_metadata),
            },
        )

    def event_metadata(self, run_state: DomainRunState) -> dict[str, Any]:
        return dict(run_state.metadata.get("event_metadata") or {})

    def plan_next_steps(self, run_state: DomainRunState) -> NextStepPlan:
        metadata = run_state.metadata
        if metadata["phase"] == "pending":
            tool_spec = ToolSpec(
                tool_id=metadata["tool_id"],
                parameters=metadata["parameters"],
                cwd=metadata["cwd"],
                step_id=metadata["step_id"],
            )
            return NextStepPlan(is_terminal=False, tool_specs=[tool_spec])

        exit_code = metadata.get("last_exit_code")
        terminal_reason = (
            TERMINAL_REASON_COMPLETED if exit_code == 0 else TERMINAL_REASON_FAILED
        )
        return NextStepPlan(
            is_terminal=True,
            tool_specs=[],
            terminal_reason=terminal_reason,
            display=f"single tool execution finished (exit_code={exit_code})",
        )

    def record_step_outcome(
        self,
        run_state: DomainRunState,
        tool_spec: ToolSpec,
        dispatch_outcome: DispatchOutcome | None,
        exit_code: int | None,
    ) -> DomainRunState:
        new_metadata = dict(run_state.metadata)
        new_metadata["phase"] = "done"
        new_metadata["last_exit_code"] = exit_code
        return DomainRunState(
            domain_run_id=run_state.domain_run_id,
            iteration=run_state.iteration + 1,
            metadata=new_metadata,
        )
