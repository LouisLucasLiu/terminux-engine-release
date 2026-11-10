# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Domain-neutral runner boundary between orchestration Kernel and domain packages.

Kernel 透過這個 domain-neutral 模組取得通用型別，不依賴任何特定 workflow 的模組。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from terminux_engine.orchestration.outcome import DispatchOutcome


@dataclass(frozen=True)
class ToolSpec:
    """一次工具執行的結構化規格。"""

    tool_id: str
    parameters: dict[str, Any]
    cwd: str
    step_id: str


@dataclass(frozen=True)
class DomainRunState:
    """Domain runner 對外的最小狀態摘要。"""

    domain_run_id: str
    iteration: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class NextStepPlan:
    """Domain runner 產出的下一批工具執行計畫。"""

    is_terminal: bool
    tool_specs: list[ToolSpec]
    display: str | None = None
    terminal_reason: str | None = None


class DomainRunner(Protocol):
    """Kernel 與 domain action 之間的最小協定。"""

    workflow_type: str

    def start_run(self, config: dict[str, Any]) -> DomainRunState: ...

    def plan_next_steps(self, run_state: DomainRunState) -> NextStepPlan: ...

    def record_step_outcome(
        self,
        run_state: DomainRunState,
        tool_spec: ToolSpec,
        dispatch_outcome: DispatchOutcome | None,
        exit_code: int | None,
    ) -> DomainRunState: ...

    def event_metadata(self, run_state: DomainRunState) -> dict[str, Any]: ...
