# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`FlowExecutionService` — Package 與 Kernel 之間的正式 facade。

Package 的 `run_flow` action 透過這個 facade 請 Kernel 啟動一次執行。
它**只接受 host live `TaskExecutionBinding`**；Package 不直接呼叫 Docker、
subprocess 或獨立 CLI，也不自行掌管 EDA lifecycle。

為什麼需要這一層：Package 只能拿到 `RuntimeServices`，而
`OrchestrationService` 是 daemon 的物件。這個 facade 由 daemon composition
建立並注入 services，Package 只看得到「請幫我啟動這個 binding 的這個 slot」
這一個動作，看不到 ledger、child process 或 Docker。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class FlowExecutionError(RuntimeError):
    """啟動請求被拒或失敗。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


BINDING_REQUIRED = "FLOW_EXECUTION_BINDING_REQUIRED"
SERVICE_UNAVAILABLE = "FLOW_EXECUTION_SERVICE_UNAVAILABLE"
LAUNCH_FAILED = "FLOW_EXECUTION_LAUNCH_FAILED"


@dataclass(frozen=True)
class LaunchResult:
    """一次啟動交接的結果。

    `accepted=True` 只代表 **child 已啟動且 run 已記入 ledger**——
    它**不代表** EDA 已完成。完成證據由 task worker 另外等待。
    """

    accepted: bool
    run_id: str | None
    slot: str
    error_code: str | None = None
    message: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted, "run_id": self.run_id, "slot": self.slot,
            "error_code": self.error_code, "message": self.message,
            "note": ("launch accepted does not mean the EDA flow finished; "
                     "completion evidence is verified separately"),
        }


class FlowExecutionService:
    """Package-facing 的執行 facade。"""

    def __init__(
        self,
        orchestration_service: Any,
        *,
        on_run_created: Callable[[Any], None],
    ) -> None:
        self._service = orchestration_service
        self._on_run_created = on_run_created

    def start_run(self, binding: Any, slot: str) -> LaunchResult:
        """請 Kernel 啟動本 slot。非阻塞：回覆只是啟動交接。"""
        from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
            TaskExecutionBinding,
        )

        if not isinstance(binding, TaskExecutionBinding):
            raise FlowExecutionError(
                BINDING_REQUIRED,
                "run_flow requires a host-built TaskExecutionBinding; a "
                "serialized dict is not accepted")
        if self._service is None:
            raise FlowExecutionError(
                SERVICE_UNAVAILABLE, "the orchestration service is not configured")
        try:
            run_id = self._service.start_task_run(binding, slot, self._on_run_created)
        except Exception as exc:  # noqa: BLE001 - 交回結構化失敗，不吞掉
            raise FlowExecutionError(LAUNCH_FAILED, repr(exc)) from exc
        return LaunchResult(accepted=True, run_id=run_id, slot=slot)


__all__ = [
    "BINDING_REQUIRED",
    "LAUNCH_FAILED",
    "SERVICE_UNAVAILABLE",
    "FlowExecutionError",
    "FlowExecutionService",
    "LaunchResult",
]
