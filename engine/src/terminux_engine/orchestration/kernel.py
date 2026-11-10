# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Sequential orchestration kernel。"""

from __future__ import annotations

import getpass
from terminux_engine.ipc.agent_socket import local_nodename
import logging
import threading
import time
import uuid
from dataclasses import dataclass, replace as _dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from terminux_engine.lineage.artifact_capture import (
    ArtifactCaptureResult,
    ArtifactCaptureService,
)
from terminux_engine.lineage.artifact_manifest import ProducerExecutionReference
from terminux_engine.orchestration.barrier import Barrier, BarrierResult
from terminux_engine.orchestration.domain_runner import (
    DomainRunState,
    DomainRunner,
    ToolSpec,
)
from terminux_engine.orchestration.dispatch_engine import DispatchEngine
from terminux_engine.orchestration.execution_contract import (
    ArtifactDiscoveryResult,
    PreparedExecution,
    TERMINAL_REASON_INTERRUPTED,
)
from terminux_engine.orchestration.checkpoint import (
    CheckpointValidationError,
    decode_checkpoint_v1,
    encode_checkpoint_v1,
)
from terminux_engine.orchestration.ledger import OrchestrationLedger
from terminux_engine.orchestration.outcome import DispatchOutcome
from terminux_engine.orchestration.subprocess_executor import (
    SubprocessExecutor,
    SubprocessResult,
)
from terminux_engine.orchestration.tool_registry import ToolRegistry
from terminux_engine.package_runtime.result import TimelineEvent

_LOG = logging.getLogger(__name__)

KernelState = Literal[
    "idle",
    "running",
    "completed",
    "failed",
    "timeout",
    "paused",
    "cancelled",
    "control_handoff",
]


class KernelAttachError(RuntimeError):
    """指定的 daemon 預建 run 無法供 kernel attach。"""


class KernelResumeError(RuntimeError):
    """Checkpoint restore 無法安全完成。"""


@dataclass(frozen=True)
class StepOutcome:
    """一個 kernel step 的完整生命週期結果。"""

    step_id: str
    attempt: int
    tool_id: str
    subprocess_result: SubprocessResult | None
    dispatch_outcome: DispatchOutcome | None
    dispatch_barrier: BarrierResult
    evidence_barrier: BarrierResult
    terminal_reason: str
    # vendor-neutral execution contract 的 PreparedExecution／artifact
    # discovery 結果，供呼叫端組裝 ExecutionReceipt。尾端、有預設值，對既有
    # positional/keyword construction 皆 backward-compatible。
    prepared_execution: "PreparedExecution | None" = None
    artifact_discovery: "ArtifactDiscoveryResult | None" = None
    # immutable artifact manifest capture 的結果。與
    # `subprocess_result`／`dispatch_barrier`／`evidence_barrier` 完全獨立
    # ——capture partial／failed 不得改寫 process、dispatch 或 evidence
    # 任何一個狀態。未注入 capture service 時為 None，
    # 行為與 vendor-neutral execution contract 完全相同。
    artifact_capture: "ArtifactCaptureResult | None" = None
    # adapter 選擇性實作的資源清理（例如孤兒 container）結果，與
    # `subprocess_result`／`artifact_discovery` 刻意分開——清理不是產物
    # 收集，逾時不會因為清理而被誤標成有產物或成功；清理本身失敗也不覆寫
    # 或影響其他既有欄位，只是額外如實記錄一份事實。未實作
    # `cleanup()` 的既有 adapter（`NativeArgvAdapter`／
    # `FormalPairToolAdapter` 等）此欄位維持 None，行為與新增前完全相同。
    cleanup_result: dict[str, Any] | None = None


#: 中斷但 process group 或 container 清理無法確認時的 run terminal reason。
TERMINAL_REASON_INTERRUPTED_UNCONFIRMED = "interrupted_cleanup_unconfirmed"
_INTERRUPTION_TERMINAL_STATES = frozenset(
    {"completed", "failed", "timeout", "cancelled", "abandoned", "paused"})


@dataclass(frozen=True)
class RunOutcome:
    """一個 orchestration run 的最終結果。"""

    run_id: str
    workflow_type: str
    domain_run_id: str | None
    final_status: Literal["completed", "failed", "timeout", "cancelled", "abandoned"]
    terminal_reason: str
    steps: list[StepOutcome]


# `DomainRunner.plan_next_steps()` 回傳 `is_terminal=True` 時，`terminal_reason`
# 是唯一自然終態成功／失敗訊號（Kernel 本身不理解任何 domain 語意，包含
# generic 測試用的 fake runner 可能回傳任意不屬於任何已知 workflow 的
# `terminal_reason` 字串，預設仍須視為 `completed`，不得改成 fail-closed
# 預設——否則會破壞既有 domain-blind 測試對「未知 reason 預設成功」的既定
# 行為）。既有 be_eco 只用兩個明確失敗字串。eda_convergence 有 15 個
# domain outcome，只有 `completed` 是自然成功終態，其餘 14 個
# （human_review_required／max_iterations／need_more_context／
# no_safe_proposal／no_change_needed／expert_unavailable／
# expert_response_invalid／correlation_mismatch／base_script_drift／
# materialization_failed／dqe_persistence_failed／repeated_recipe／
# repeated_no_improvement／degradation_detected）全部必須映射為 Kernel
# `failed`；若不明確列出這些字串，它們會落入舊有「非失敗即視為
# completed」的 default 分支而被誤判。延續既有 be_eco 的作法：擴充失敗字串
# blocklist，不改變任何既有 default-completed 行為。
_KNOWN_FAILURE_TERMINAL_REASONS = frozenset(
    {"be_eco_failed", "be_eco_max_iterations"}
    | {
        f"eda_convergence_{outcome}"
        for outcome in (
            "human_review_required", "max_iterations", "need_more_context",
            "no_safe_proposal", "no_change_needed", "expert_unavailable",
            "expert_response_invalid", "correlation_mismatch", "base_script_drift",
            "materialization_failed", "dqe_persistence_failed", "repeated_recipe",
            "repeated_no_improvement", "degradation_detected",
        )
    }
    # `single_tool_runner.py::SingleToolRunner` 的唯一失敗終態字串
    # （exit_code != 0）。同一種「非失敗即視為 completed」default-completed
    # 分支慣例，只是新增一個 workflow 的失敗字串，非新增判斷機制。
    | {"single_tool_execution_failed"}
)


class OrchestrationKernel:
    """以 ``run()`` 與 ``step()`` 驅動的 sequential state machine。"""

    def __init__(
        self,
        *,
        engine: DispatchEngine,
        ledger: OrchestrationLedger,
        tool_registry: ToolRegistry,
        runner: DomainRunner,
        subprocess_executor: SubprocessExecutor | None = None,
        barrier: Barrier | None = None,
        clock: Callable[[], float] = time.time,
        dispatch_timeout: float = 60.0,
        evidence_timeout: float = 5.0,
        shutdown_requested: threading.Event | None = None,
        artifact_capture_service: "ArtifactCaptureService | None" = None,
    ) -> None:
        self._engine = engine
        self._ledger = ledger
        self._tool_registry = tool_registry
        self._runner = runner
        self._subprocess_executor = subprocess_executor or SubprocessExecutor()
        self._barrier = barrier or Barrier(clock=clock)
        self._clock = clock
        self._dispatch_timeout = dispatch_timeout
        self._evidence_timeout = evidence_timeout
        self._state: KernelState = "idle"
        self._current_run_id: str | None = None
        self._domain_state: DomainRunState | None = None
        self._pending_tool_specs: list[ToolSpec] = []
        self._step_outcomes: list[StepOutcome] = []
        self._run_outcome: RunOutcome | None = None
        self._shutdown_requested = shutdown_requested or threading.Event()
        self._next_attempt_reason: Literal["initial", "manual_retry"] = "initial"
        # 未注入時完全不做 artifact capture（既有 vendor-neutral execution contract 呼叫端與測試
        # 行為 byte-for-byte 不變，也不會意外寫入使用者的 durable store）。
        self._artifact_capture_service = artifact_capture_service

    def run(
        self,
        workflow_config: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> RunOutcome | None:
        """初始化並阻塞執行，直到 domain 或 step 到達 terminal。"""
        self._initialize(workflow_config, run_id=run_id)
        return self._run_loop(run_id=run_id)

    def run_from_checkpoint(
        self,
        *,
        run_id: str,
        mode: Literal["resume", "retry"],
        child_pid: int,
    ) -> RunOutcome | None:
        """以已驗證 checkpoint restore 同一 run，且由 conditional claim 決定唯一 owner。"""
        if self._state != "idle":
            raise RuntimeError("kernel instance can run only one workflow")
        run = self._ledger.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.kernel_checkpoint_json is None or run.domain_run_id is None:
            result = self._ledger.fail_on_checkpoint_error_if_active(
                run_id,
                phase="deserialize",
            )
            if result.applied:
                raise KernelResumeError("run has no restorable checkpoint")
            return None

        checkpoint_json = run.kernel_checkpoint_json
        try:
            if mode == "resume":
                restored = decode_checkpoint_v1(
                    checkpoint_json,
                    expected_domain_run_id=run.domain_run_id,
                )
                claim = self._ledger.claim_resume_if_paused(
                    run_id,
                    child_pid=child_pid,
                    heartbeat_at=self._clock(),
                    expected_checkpoint_json=checkpoint_json,
                )
            elif mode == "retry":
                claim = self._ledger.claim_retry_child_if_unclaimed(
                    run_id,
                    child_pid=child_pid,
                    heartbeat_at=self._clock(),
                )
                if not claim.applied:
                    return None
                restored = decode_checkpoint_v1(
                    checkpoint_json,
                    expected_domain_run_id=run.domain_run_id,
                )
            else:
                raise ValueError(f"unknown checkpoint mode: {mode}")
        except CheckpointValidationError as exc:
            if mode == "retry":
                raise KernelResumeError("retry checkpoint validation failed") from exc
            result = self._ledger.fail_on_checkpoint_error_if_active(
                run_id,
                phase="deserialize",
            )
            if result.applied:
                raise KernelResumeError("checkpoint validation failed") from exc
            return None

        if not claim.applied:
            return None
        self._current_run_id = run_id
        self._domain_state = restored.domain_state
        self._pending_tool_specs = restored.pending_tool_specs
        self._state = "running"
        if mode == "retry" and self._pending_tool_specs:
            self._next_attempt_reason = "manual_retry"
        return self._run_loop(run_id=run_id)

    def _run_loop(self, *, run_id: str | None) -> RunOutcome | None:
        while self._state == "running" and self._run_outcome is None:
            self.step(run_id=run_id)
        return self._run_outcome

    def step(self, *, run_id: str | None = None) -> StepOutcome | None:
        """推進一個 tool step；已 terminal 時回傳 ``None``。"""
        if self._state != "running":
            return None
        if self._state == "idle" or self._current_run_id is None or self._domain_state is None:
            raise RuntimeError("kernel must be initialized through run() before step()")
        if self._apply_boundary_control():
            return None
        if not self._pending_tool_specs:
            plan = self._runner.plan_next_steps(self._domain_state)
            if plan.is_terminal:
                final_status: Literal["completed", "failed"] = (
                    "failed"
                    if plan.terminal_reason in _KNOWN_FAILURE_TERMINAL_REASONS
                    else "completed"
                )
                if self._complete_naturally(
                    final_status, plan.terminal_reason or "domain_complete"
                ):
                    return None
                self._apply_boundary_control()
                return None
            if not plan.tool_specs:
                if self._complete_naturally("failed", "domain_returned_no_tool_specs"):
                    return None
                self._apply_boundary_control()
                return None
            self._pending_tool_specs.extend(plan.tool_specs)

        tool_spec = self._pending_tool_specs[0]
        try:
            checkpoint_json = encode_checkpoint_v1(
                safe_point="before_step",
                domain_state=self._domain_state,
                pending_tool_specs=self._pending_tool_specs,
                current_step_id=tool_spec.step_id,
            )
        except CheckpointValidationError:
            self._handle_checkpoint_serialization_error()
            return None
        permission = self._ledger.save_before_step_if_runnable(
            self._current_run_id,
            checkpoint_json=checkpoint_json,
        )
        if not permission.applied:
            self._apply_boundary_control()
            return None
        popped_tool_spec = self._pending_tool_specs.pop(0)
        if popped_tool_spec.step_id != tool_spec.step_id:
            raise RuntimeError("kernel pending ToolSpec head changed after checkpoint commit")
        tool_spec = popped_tool_spec
        step_outcome = self._execute_kernel_step(tool_spec, self._domain_state)
        self._step_outcomes.append(step_outcome)
        record_attempt = getattr(self._runner, "record_step_attempt", None)
        if callable(record_attempt):
            self._domain_state = record_attempt(
                self._domain_state,
                tool_spec,
                step_outcome.attempt,
            )
        self._domain_state = self._runner.record_step_outcome(
            self._domain_state,
            tool_spec,
            step_outcome.dispatch_outcome,
            step_outcome.subprocess_result.exit_code
            if step_outcome.subprocess_result is not None
            else None,
        )
        if step_outcome.terminal_reason == TERMINAL_REASON_INTERRUPTED:
            self._finalize_interrupted_step(step_outcome)
            return step_outcome
        if step_outcome.terminal_reason != "step_complete":
            final_status: Literal["failed", "timeout"] = (
                "timeout"
                if step_outcome.subprocess_result is not None
                and step_outcome.subprocess_result.timed_out
                else "failed"
            )
            result = self._ledger.finalize_step_failure_if_no_immediate_cancel(
                self._current_run_id,
                final_status=final_status,
                terminal_reason=step_outcome.terminal_reason,
            )
            if result.applied:
                self._set_terminal_outcome(final_status, step_outcome.terminal_reason)
            else:
                self._adopt_control_winner()
            return step_outcome
        self._apply_boundary_control()
        return step_outcome

    def _initialize(
        self,
        workflow_config: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None:
        if self._state != "idle":
            raise RuntimeError("kernel instance can run only one workflow")
        domain_state = self._runner.start_run(workflow_config)
        if run_id is None:
            self._current_run_id = self._ledger.create_run(
                workflow_type=self._runner.workflow_type,
                domain_run_id=domain_state.domain_run_id,
                metadata={"workflow_config": workflow_config},
            )
            self._ledger.start_run(self._current_run_id)
        else:
            self._current_run_id = run_id
            self._attach_existing_run(run_id, domain_state)
        self._domain_state = domain_state
        self._state = "running"

    def _attach_existing_run(self, run_id: str, domain_state: DomainRunState) -> None:
        """驗證 daemon 預建 run 並補寫 runner 產出的 domain run id。"""
        run = self._ledger.get_run(run_id)
        if run is None:
            raise KernelAttachError(f"run not found in ledger: {run_id}")
        if run.status != "running":
            raise KernelAttachError(
                f"cannot attach to run {run_id} in status {run.status}"
            )
        self._ledger.update_domain_run_id(run_id, domain_state.domain_run_id)

    def _complete_naturally(
        self,
        final_status: Literal["completed", "failed"],
        terminal_reason: str,
    ) -> bool:
        """以 control-aware conditional operation 提交自然終態。"""
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        result = self._ledger.complete_run_if_no_control_pending(
            self._current_run_id,
            final_status=final_status,
            terminal_reason=terminal_reason,
        )
        if not result.applied:
            return False
        self._set_terminal_outcome(final_status, terminal_reason)
        return True

    def _after_step_checkpoint(self) -> str:
        """將 record_step_outcome 後的完整 domain boundary 保存為 after_step。"""
        if self._domain_state is None:
            raise RuntimeError("kernel domain state is unavailable")
        current_step_id = (
            self._pending_tool_specs[0].step_id if self._pending_tool_specs else None
        )
        return encode_checkpoint_v1(
            safe_point="after_step",
            domain_state=self._domain_state,
            pending_tool_specs=self._pending_tool_specs,
            current_step_id=current_step_id,
        )

    def _apply_boundary_control(self) -> bool:
        """在完整 domain step boundary 依固定優先序處理已接受 control。"""
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        run = self._ledger.get_run(self._current_run_id)
        if run is None:
            raise KeyError(f"run not found: {self._current_run_id}")
        if run.status != "running":
            self._adopt_control_winner()
            return True
        if run.cancel_requested_at is not None and run.cancel_immediate == 1:
            # Immediate cancel 的 terminal ownership 屬 F-4 daemon path；kernel 只停止推進。
            self._state = "control_handoff"
            return True
        if run.cancel_requested_at is not None and run.cancel_immediate == 0:
            return self._finalize_boundary_control("soft_cancel")
        if run.pause_requested_at is not None:
            return self._finalize_boundary_control("user_pause")
        if self._shutdown_requested.is_set():
            return self._finalize_boundary_control("daemon_shutdown")
        return False

    def _finalize_boundary_control(
        self,
        control: Literal["soft_cancel", "user_pause", "daemon_shutdown"],
    ) -> bool:
        """以同一個 after-step checkpoint 完成 soft cancel 或 pause disposition。"""
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        try:
            checkpoint_json = self._after_step_checkpoint()
        except CheckpointValidationError:
            self._handle_checkpoint_serialization_error()
            return True
        if control == "soft_cancel":
            result = self._ledger.finalize_cancel_if_requested(
                self._current_run_id,
                immediate=False,
                checkpoint_json=checkpoint_json,
            )
            if result.applied:
                self._state = "cancelled"
                return True
        else:
            source: Literal["user", "daemon_shutdown"] = (
                "user" if control == "user_pause" else "daemon_shutdown"
            )
            result = self._ledger.pause_with_checkpoint_if_allowed(
                self._current_run_id,
                checkpoint_json=checkpoint_json,
                source=source,
            )
            if result.applied:
                self._state = "paused"
                return True
        self._adopt_control_winner()
        return self._state != "running"

    def _handle_checkpoint_serialization_error(self) -> None:
        """checkpoint 失敗只在尚未被 cancel 取得 ownership 時標 failed。"""
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        result = self._ledger.fail_on_checkpoint_error_if_active(
            self._current_run_id,
            phase="serialize",
        )
        if result.applied:
            self._set_terminal_outcome("failed", "checkpoint_serialization_failed")
        else:
            self._adopt_control_winner()

    def _adopt_control_winner(self) -> None:
        """conditional loser 只依 durable row 停止，不寫入第二個 terminal 事實。"""
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        run = self._ledger.get_run(self._current_run_id)
        if run is None:
            raise KeyError(f"run not found: {self._current_run_id}")
        if run.status in {"paused", "cancelled"}:
            self._state = run.status
        elif run.status in {"completed", "failed", "timeout"}:
            self._state = run.status
        elif run.cancel_requested_at is not None and run.cancel_immediate == 1:
            self._state = "control_handoff"

    def _finalize_interrupted_step(self, step_outcome: StepOutcome) -> None:
        """前景 CLI 中斷分支的 run 終態（daemon 的 executor 不會走到這裡）。

        process group 已確認不存在、而且 adapter 的 cleanup 回報 `ok=True`，才把
        run 轉成 `cancelled`；任一項無法確認就轉成 `abandoned`。兩者都經由同一個
        條件式更新，只作用在仍為 `running` 的 run；已有終態時不覆蓋，只依 durable
        row 停止。
        """
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        result = step_outcome.subprocess_result
        cleanup = step_outcome.cleanup_result
        confirmed = (
            result is not None
            and getattr(result, "process_group_confirmed_gone", None) is True
            and isinstance(cleanup, dict)
            and cleanup.get("ok") is True
        )
        final_status: Literal["cancelled", "abandoned"] = (
            "cancelled" if confirmed else "abandoned")
        terminal_reason = (TERMINAL_REASON_INTERRUPTED if confirmed
                           else TERMINAL_REASON_INTERRUPTED_UNCONFIRMED)
        applied = self._ledger.finalize_step_failure_if_no_immediate_cancel(
            self._current_run_id, final_status=final_status,
            terminal_reason=terminal_reason)
        if applied.applied:
            self._set_terminal_outcome(final_status, terminal_reason)
            return
        run = self._ledger.get_run(self._current_run_id)
        status = run.status if run is not None else None
        self._state = status if status in _INTERRUPTION_TERMINAL_STATES else "control_handoff"

    def _set_terminal_outcome(
        self,
        final_status: Literal["completed", "failed", "timeout", "cancelled", "abandoned"],
        terminal_reason: str,
    ) -> None:
        if self._current_run_id is None or self._domain_state is None:
            raise RuntimeError("kernel state is incomplete")
        self._state = final_status
        self._run_outcome = RunOutcome(
            run_id=self._current_run_id,
            workflow_type=self._runner.workflow_type,
            domain_run_id=self._domain_state.domain_run_id,
            final_status=final_status,
            terminal_reason=terminal_reason,
            steps=list(self._step_outcomes),
        )

    def _execute_kernel_step(
        self,
        tool_spec: ToolSpec,
        domain_state: DomainRunState,
    ) -> StepOutcome:
        """外層包裝：不管 `_execute_kernel_step_body()` 走哪一條既有路徑
        （正常完成、dispatch 失敗、timeout），或中途拋出未預期例外，都在
        `finally` 呼叫既有 `ToolRegistry.cleanup_execution()`（資源清理，
        由 adapter 選擇性實作，例如 `SingleInstanceEcoSwapAdapter.cleanup()`
        清 cidfile 對應的孤兒 container）——**不是** `discover_artifacts()`
        的一部分，兩者刻意分開：`discover_artifacts()` 只在既有的兩個
        非 timeout 路徑被呼叫（沿用既有行為，本次未改），逾時不會因為
        清理而被誤標成有產物／成功。

        清理結果透過 `dataclasses.replace()` 事後貼回已經組好的
        `StepOutcome`（`cleanup_result` 是新增的尾端欄位，見該
        dataclass），不覆寫既有任何欄位；清理本身失敗時
        `cleanup_execution()` 如實回報 `{"ok": False, ...}`，不拿掉、不
        改寫 `subprocess_result`／`dispatch_barrier`／`terminal_reason`
        這些既有、屬於「工具本身跑得怎樣」的欄位。

        用 `try/finally` 包住 body 呼叫，不是只在正常回傳時才清理：
        body 中途拋出未預期例外時，`finally` 一樣先跑清理，原例外再照
        Python 既有語意繼續往外傳——這個函式不吞掉、不改寫任何例外，只
        確保清理不會因為例外而被跳過。`prepare_execution()` 本身失敗
        （例如來源漂移，zero dispatch）時 `prepared_holder[0]` 從未被
        設值，代表真的什麼都沒啟動，這裡正確地不觸發任何清理。"""
        prepared_holder: list["PreparedExecution | None"] = [None]
        try:
            step_outcome = self._execute_kernel_step_body(
                tool_spec, domain_state, prepared_holder)
        finally:
            prepared = prepared_holder[0]
            cleanup_result = (
                self._tool_registry.cleanup_execution(
                    tool_spec.tool_id, tool_spec.parameters, prepared,
                    cwd=Path(tool_spec.cwd))
                if prepared is not None else None)
        if cleanup_result is None:
            return step_outcome
        self._record_resource_cleanup(cleanup_result)
        return _dataclass_replace(step_outcome, cleanup_result=cleanup_result)

    def _record_resource_cleanup(self, cleanup_result: dict[str, Any]) -> None:
        """把 adapter 的資源清理結果另外 append 到 ledger 的
        `resource_cleanup` transition——與 daemon 在 immediate cancel 後代為
        清理時寫的是同一種獨立紀錄，讓兩條清理路徑的結果都有 durable 讀回點
        （receipt projection 的固定欄位不含清理結果）。只記錄、不改 run 狀態；
        記錄失敗只寫 log，不影響工具結果。"""
        recorder = getattr(self._ledger, "record_resource_cleanup", None)
        if self._current_run_id is None or not callable(recorder):
            return
        try:
            recorder(self._current_run_id, ok=cleanup_result.get("ok") is True,
                     detail={"source": "in_process_cleanup", **cleanup_result})
        except Exception:  # noqa: BLE001 - 清理紀錄不得改寫工具結果
            _LOG.exception("resource cleanup record not persisted: run_id=%s",
                           self._current_run_id)

    def _execute_kernel_step_body(
        self,
        tool_spec: ToolSpec,
        domain_state: DomainRunState,
        prepared_holder: list["PreparedExecution | None"],
    ) -> StepOutcome:
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        expected_packages = self._tool_registry.expected_packages(tool_spec.tool_id)
        attempt, dispatch_key = self._ledger.create_step(
            run_id=self._current_run_id,
            step_id=tool_spec.step_id,
            tool_id=tool_spec.tool_id,
            parameters=tool_spec.parameters,
            cwd=tool_spec.cwd,
            expected_packages=expected_packages,
            attempt_reason=self._next_attempt_reason,
        )
        self._next_attempt_reason = "initial"
        # request -> PreparedExecution 由 tool 對應的 adapter 組裝
        # （既有 tool 走 NativeArgvAdapter，行為與舊版 build_argv() 相同；
        # 見 tool_registry.py::prepare_execution()）。Kernel 本身完全不理解
        # argv 是怎麼被組出來的。
        prepared = self._tool_registry.prepare_execution(
            tool_spec.tool_id,
            tool_spec.parameters,
            cwd=Path(tool_spec.cwd),
        )
        # 只有 `prepare_execution()` 真的成功（代表可能已經有副作用，例如
        # 寫出 cidfile）才記錄下來給外層 `_execute_kernel_step()` 的
        # `finally` 用來觸發清理；`prepare_execution()` 本身失敗時這行
        # 不會被執行到，維持 zero-dispatch 語意不變。
        prepared_holder[0] = prepared
        self._ledger.transition_command(
            self._current_run_id, tool_spec.step_id, attempt, to_state="running"
        )
        subprocess_result = self._subprocess_executor.execute(
            argv=prepared.argv,
            cwd=prepared.cwd,
            env=prepared.env,
            timeout=prepared.timeout_seconds,
        )
        if getattr(subprocess_result, "interrupted", False):
            # 前景 CLI 的中斷：不做 discovery、capture 與 DQE dispatch。command 使用
            # 既有的 `failed` 狀態，並保留中斷原因；run 的終態由 `step()` 決定。
            self._ledger.transition_command(
                self._current_run_id,
                tool_spec.step_id,
                attempt,
                to_state="failed",
                exit_code=(subprocess_result.exit_code
                           if getattr(subprocess_result, "process_started", True)
                           else None),
                reason=TERMINAL_REASON_INTERRUPTED,
            )
            self._ledger.mark_step_terminal(
                self._current_run_id,
                tool_spec.step_id,
                attempt,
                terminal_reason=TERMINAL_REASON_INTERRUPTED,
            )
            return StepOutcome(
                step_id=tool_spec.step_id,
                attempt=attempt,
                tool_id=tool_spec.tool_id,
                subprocess_result=subprocess_result,
                dispatch_outcome=None,
                dispatch_barrier=BarrierResult(
                    kind="dispatch", state="not_ready", reason="command_interrupted"
                ),
                evidence_barrier=BarrierResult(
                    kind="evidence", state="not_ready", reason="no_dispatch"
                ),
                terminal_reason=TERMINAL_REASON_INTERRUPTED,
                prepared_execution=prepared,
            )
        if subprocess_result.timed_out:
            self._ledger.transition_command(
                self._current_run_id,
                tool_spec.step_id,
                attempt,
                to_state="timeout",
                exit_code=subprocess_result.exit_code,
            )
            self._ledger.mark_step_terminal(
                self._current_run_id,
                tool_spec.step_id,
                attempt,
                terminal_reason="command_timeout",
            )
            return StepOutcome(
                step_id=tool_spec.step_id,
                attempt=attempt,
                tool_id=tool_spec.tool_id,
                subprocess_result=subprocess_result,
                dispatch_outcome=None,
                dispatch_barrier=BarrierResult(
                    kind="dispatch", state="not_ready", reason="command_did_not_finish"
                ),
                evidence_barrier=BarrierResult(
                    kind="evidence", state="not_ready", reason="no_dispatch"
                ),
                terminal_reason="command_timeout",
                prepared_execution=prepared,
            )

        self._ledger.transition_command(
            self._current_run_id,
            tool_spec.step_id,
            attempt,
            to_state="completed" if subprocess_result.exit_code == 0 else "failed",
            exit_code=subprocess_result.exit_code,
        )
        artifact_discovery = self._tool_registry.discover_artifacts(
            tool_spec.tool_id,
            tool_spec.parameters,
            prepared,
            cwd=Path(tool_spec.cwd),
            process_exit_code=subprocess_result.exit_code,
        )
        # artifact lineage capture timing：subprocess 完成 → adapter discovery
        # → **artifact capture ＋ manifest 持久化** → receipt／event 取得
        # manifest reference → Package dispatch。capture 必須在 dispatch
        # 之前完成，否則 Package 只能自己重新猜 artifact 路徑。
        artifact_capture = self._capture_artifacts(
            tool_spec, artifact_discovery, attempt
        )
        self._ledger.transition_dispatch(
            self._current_run_id, tool_spec.step_id, attempt, to_state="running"
        )
        event = self._build_timeline_event(
            tool_spec,
            prepared,
            subprocess_result,
            attempt,
            dispatch_key,
            domain_state,
            artifact_capture,
        )
        dispatch_barrier = self._barrier.wait_dispatch_completion(
            self._engine, event, timeout=self._dispatch_timeout
        )
        if dispatch_barrier.state != "completed":
            dispatch_state: Literal["failed", "timeout"] = (
                "timeout" if dispatch_barrier.state == "timeout" else "failed"
            )
            self._ledger.transition_dispatch(
                self._current_run_id,
                tool_spec.step_id,
                attempt,
                to_state=dispatch_state,
                reason=dispatch_barrier.reason,
            )
            terminal_reason = f"dispatch_{dispatch_barrier.state}"
            self._ledger.mark_step_terminal(
                self._current_run_id,
                tool_spec.step_id,
                attempt,
                terminal_reason=terminal_reason,
            )
            return StepOutcome(
                step_id=tool_spec.step_id,
                attempt=attempt,
                tool_id=tool_spec.tool_id,
                subprocess_result=subprocess_result,
                dispatch_outcome=None,
                dispatch_barrier=dispatch_barrier,
                evidence_barrier=BarrierResult(
                    kind="evidence", state="not_ready", reason="dispatch_not_completed"
                ),
                terminal_reason=terminal_reason,
                prepared_execution=prepared,
                artifact_discovery=artifact_discovery,
                artifact_capture=artifact_capture,
            )

        details = dispatch_barrier.details or {}
        dispatch_outcome = details.get("outcome")
        if not isinstance(dispatch_outcome, DispatchOutcome):
            raise RuntimeError("completed dispatch barrier did not return DispatchOutcome")
        self._ledger.transition_dispatch(
            self._current_run_id,
            tool_spec.step_id,
            attempt,
            to_state="completed",
            outcome_summary=self._summarize_outcome(dispatch_outcome),
        )
        evidence_barrier = self._barrier.check_evidence_readiness(
            dispatch_outcome=dispatch_outcome,
            expected_packages=expected_packages,
            cwd=Path(tool_spec.cwd),
            timeout=self._evidence_timeout,
        )
        evidence_state: Literal["ready", "not_ready", "timeout"] = (
            evidence_barrier.state
            if evidence_barrier.state in {"ready", "not_ready", "timeout"}
            else "not_ready"
        )
        self._ledger.transition_evidence(
            self._current_run_id,
            tool_spec.step_id,
            attempt,
            to_state=evidence_state,
            reason=evidence_barrier.reason,
        )
        terminal_reason = (
            "step_complete"
            if evidence_barrier.state == "ready"
            else "evidence_unavailable"
        )
        self._ledger.mark_step_terminal(
            self._current_run_id,
            tool_spec.step_id,
            attempt,
            terminal_reason=terminal_reason,
        )
        return StepOutcome(
            step_id=tool_spec.step_id,
            attempt=attempt,
            tool_id=tool_spec.tool_id,
            subprocess_result=subprocess_result,
            dispatch_outcome=dispatch_outcome,
            dispatch_barrier=dispatch_barrier,
            evidence_barrier=evidence_barrier,
            terminal_reason=terminal_reason,
            prepared_execution=prepared,
            artifact_discovery=artifact_discovery,
            artifact_capture=artifact_capture,
        )

    def _capture_artifacts(
        self,
        tool_spec: ToolSpec,
        artifact_discovery: "ArtifactDiscoveryResult | None",
        attempt: int,
    ) -> "ArtifactCaptureResult | None":
        """建立並持久化這一步的 immutable artifact manifest。

        Kernel 完全不解讀 candidate 的 role 或內容——只是把 adapter 回報的
        opaque candidates 與 output roots 交給 domain-blind capture service
。未注入 service 或沒有 discovery 結果時回 None。

        Capture service 本身承諾永不拋例外（見
        `lineage.artifact_capture`），因此這裡不需要、也不得加入額外的
        try/except 把 artifact 問題升級成 execution 失敗。
        """
        service = self._artifact_capture_service
        if service is None or artifact_discovery is None:
            return None
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")

        boundaries = list(artifact_discovery.output_roots) or [Path(tool_spec.cwd)]
        producer = ProducerExecutionReference(
            run_id=self._current_run_id,
            step_id=tool_spec.step_id,
            attempt=attempt,
            tool_id=tool_spec.tool_id,
            event_tool_name=self._tool_registry.event_tool_name(tool_spec.tool_id),
            adapter_id=self._tool_registry.adapter_id(tool_spec.tool_id),
            adapter_version=self._tool_registry.adapter_version(tool_spec.tool_id),
            # Receipt 與 manifest 的雙向對應鍵：run/step/attempt 三元
            # 組唯一決定一筆 receipt，不需要（也不應該）在 capture 當下就
            # 依賴尚未組裝完成的 receipt 物件。
            receipt_reference=(
                f"{self._current_run_id}/{tool_spec.step_id}/{attempt}"
            ),
        )
        return service.capture(
            artifact_discovery.artifact_candidates,
            producer=producer,
            output_boundaries=boundaries,
        )

    def _build_timeline_event(
        self,
        tool_spec: ToolSpec,
        prepared: "PreparedExecution",
        subprocess_result: SubprocessResult,
        attempt: int,
        dispatch_key: str,
        domain_state: DomainRunState,
        artifact_capture: "ArtifactCaptureResult | None" = None,
    ) -> TimelineEvent:
        if self._current_run_id is None:
            raise RuntimeError("kernel run id is unavailable")
        # event 必須反映「真正被執行的」argv/cwd（`prepared`，來自
        # `ToolRegistry.prepare_execution()`），不得再獨立呼叫舊版
        # `build_argv()` 重建一份可能不一致的版本（ORFS 等使用自訂
        # adapter 的 tool，`build_argv()`／`command_builder` 已不是真正
        # 執行路徑，繼續呼叫它會產生與實際執行不符的 event）。
        argv = prepared.argv
        return TimelineEvent(
            event_id=str(uuid.uuid4()),
            trace_id=None,
            command=" ".join(argv),
            argv=argv,
            cwd=str(prepared.cwd),
            tool_name=self._tool_registry.event_tool_name(tool_spec.tool_id),
            user=getpass.getuser(),
            host=local_nodename(),
            terminal_id=None,
            source="orchestration.kernel",
            timestamp=datetime.now(timezone.utc).isoformat(),
            exit_code=subprocess_result.exit_code,
            stdout_tail=subprocess_result.stdout_tail,
            metadata={
                "orchestration": {
                    "run_id": self._current_run_id,
                    "step_id": tool_spec.step_id,
                    "attempt": attempt,
                    "dispatch_key": dispatch_key,
                },
                # 中性 manifest metadata。Package 可以據此讀到
                # 已持久化的 manifest，但 Core 不解讀 manifest 內的 role
                # 或 content。
                **(
                    {"artifact_lineage": artifact_capture.to_event_metadata()}
                    if artifact_capture is not None
                    else {}
                ),
                **self._runner.event_metadata(domain_state),
            },
        )

    @staticmethod
    def _summarize_outcome(outcome: DispatchOutcome) -> dict[str, Any]:
        return {
            "event_id": outcome.event_id,
            "packages": [
                {
                    "package_id": package.package_id,
                    "status": package.status,
                    "records_committed": package.records_committed,
                    "notifications_emitted": package.notifications_emitted,
                    "error": package.error,
                }
                for package in outcome.packages
            ],
            "skipped_no_after": list(outcome.skipped_no_after),
        }

    def _finish_run(
        self,
        final_status: Literal["completed", "failed"],
        terminal_reason: str,
    ) -> None:
        if self._current_run_id is None or self._domain_state is None:
            raise RuntimeError("kernel state is incomplete")
        self._ledger.complete_run(
            self._current_run_id,
            final_status=final_status,
            terminal_reason=terminal_reason,
        )
        self._state = final_status
        self._run_outcome = RunOutcome(
            run_id=self._current_run_id,
            workflow_type=self._runner.workflow_type,
            domain_run_id=self._domain_state.domain_run_id,
            final_status=final_status,
            terminal_reason=terminal_reason,
            steps=list(self._step_outcomes),
        )
