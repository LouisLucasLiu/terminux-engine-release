# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent 專用的 scoped Evidence Bundle query tool。"""

from __future__ import annotations

from typing import Any, Callable

from terminux_engine.llm.tool_registry import ToolSchema
from terminux_engine.orchestration.daemon_service import OrchestrationService


SCHEMA = ToolSchema(
    name="query_orchestration_evidence",
    description=(
        "SAFE, read-only discovery tool for questions about an orchestration run's "
        "DQE, reports, lineage, terminal outcome, or iteration evidence. Requires "
        "orchestration_run_id and returns only that trusted-session-visible run's "
        "bounded Evidence Bundle. availability=available is complete; unavailable or "
        "partial must be reported explicitly. Never use shell commands or filesystem "
        "search as a replacement."
    ),
    parameters={
        "type": "object",
        "properties": {
            "orchestration_run_id": {
                "type": "string",
                "description": "The orchestration run identity to query.",
            }
        },
        "required": ["orchestration_run_id"],
        "additionalProperties": False,
    },
)


def create_tool(service: OrchestrationService) -> Callable[..., Any]:
    """建立只委派同一 service authorization boundary 的 executor。

    executor 取得 service 的 caller 可見 bundle 與行程內的 `ReceiptIntegrity`：後者判定為
    veto（其他 member 寫入同 run id 的列、或 content hash 不自洽的本人列）時，以
    `task_scope.veto_run_evidence` 讓最終 Gate 在此結果進入 transcript 前以
    `EVIDENCE_INTEGRITY_DENIED` 拒送；判定本身不寫進回傳值。
    回傳前經 `task_scope.redact_unapproved_evidence`：本 turn 的外送 policy 沒有明確核准的
    資料類別（目前只有 `timing_path_evidence`），其 section 在進入 transcript 之前就移除，
    因此預設送出的 bytes 與沒有該 section 時完全相同；最終 Gate 仍會獨立拒絕任何漏網的類別。
    """
    from terminux_engine.llm.task_scope import redact_unapproved_evidence, veto_run_evidence

    def executor(*, orchestration_run_id: str) -> Any:
        response, integrity = service.evidence_bundle_request_with_integrity(orchestration_run_id)
        if integrity.vetoed:
            veto_run_evidence(orchestration_run_id)
        return redact_unapproved_evidence(response)

    return executor
