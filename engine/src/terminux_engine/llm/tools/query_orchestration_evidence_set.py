# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent 專用的 scoped multi-run Evidence Set query tool。"""

from __future__ import annotations

from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema
from terminux_engine.orchestration.daemon_service import OrchestrationService


SCHEMA = ToolSchema(
    name="query_orchestration_evidence_set",
    description=(
        "SAFE, read-only discovery tool for comparing 2 to 3 orchestration runs "
        "(status, steps/attempts, DQE, lineage) that are all visible to the current "
        "trusted session scope. Never use for a single run -- use "
        "query_orchestration_evidence instead. Runs the caller cannot access are "
        "reported as unavailable, identically to nonexistent runs; never assume a "
        "run failed just because it is unavailable. When composing the answer from "
        "this tool's result, keep a strict Situation Assessment discipline: "
        "(1) facts -- state only what a specific run/evidence field in the result "
        "directly proves, and reference which run it came from; "
        "(2) deltas -- differences between runs that the evidence itself shows; "
        "(3) never present chronology (one run happened before another) as proof of "
        "causality -- a later success is not automatically caused by an earlier "
        "run's evidence unless the evidence itself links them; "
        "(4) hypotheses -- possible explanations without direct supporting evidence "
        "must be labeled as hypotheses, never stated as fact; "
        "(5) if the user asserts something that conflicts with the evidence, side "
        "with the evidence and say so explicitly; "
        "(6) if evidence is insufficient for a claim, say so instead of guessing; "
        "(7) this tool is read-only -- it never executes, mutates, or proposes "
        "package actions; any execution request answer must state "
        "execution_status=NOT_EXECUTED."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 3,
                "description": "2 to 3 distinct orchestration run ids to compare.",
            },
            "comparison_focus": {
                "type": "string",
                "enum": ["status", "steps", "dqe", "lineage", "all"],
                "description": "Which aspect of the runs to focus the comparison on.",
            },
        },
        "required": ["run_ids"],
        "additionalProperties": False,
    },
)


def create_tool(service: OrchestrationService) -> Callable[..., Any]:
    """建立只委派同一 service authorization boundary 的 executor。

    回傳前經 `task_scope.redact_evidence_set_timing_path`：每個 bundle 的 `timing_path`
    section 在進入 transcript 之前移除。單 run 工具依本 turn 的外送 policy 決定是否保留該
    section；多 run 結果沒有對應的 Gate 類別，所以這裡不依 policy 放行，一律移除。
    """
    from terminux_engine.llm.task_scope import redact_evidence_set_timing_path

    def executor(*, run_ids: list[str], comparison_focus: str = "all") -> Any:
        response = service.evidence_set_request(run_ids, comparison_focus=comparison_focus)
        return redact_evidence_set_timing_path(response)

    return executor
