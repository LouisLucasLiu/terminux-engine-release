# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""單一 task step 的固定派送順序。

`execute_task_step()` 是新 analysis task 的正式 facade。它**不**把舊 plan-only planner
Plan／PlanDraftCache 改成可執行，也不依賴尚未接通的 typed plan-only 入口。

固定六步（順序不可調換）：

1. task lock 內驗 RUNNING、未取消、目前 instance、有效根授權、scope/session、
   deadline/budget、本步未派送、前一步結果可繼續。
2. 重建並核對 catalog snapshot id、capability contract hash、risk、
   approval_required、arguments hash、identity；再檢查 eligibility、
   activation、run access。第 2 步的規則直接委派
   `bounded_execution.binding.revalidate_binding_material()` 與
   `bounded_execution.eligibility.check_capability_eligibility()`，與 approved package action
   plan step 共用同一份，不另寫規則。
3. 建立 task-owned proposal 與 host 派生 binding；核准只來自相符的 live grant
   （`ProposalRegistry.approve_from_task_grant()`）。
4. **先 append step_intent，成功後才 claim。** 兩者都在同一個 task lock 內，
   cancel 不可能插進中間。claim 失敗就不派送，寫一筆確定的未派送 receipt；
   不刪 intent、不重試 claim。
5. 成功 claim 是分界。**釋放 task lock** 後才透過
   `dispatch_claimed_proposal()` 執行——否則 status/cancel 在長時間 action
   期間會失去回應。
6. 取得完整 `TaskActionOutcome`，寫完整結果與 receipt，再讀回驗證，才由
   呼叫端決定是否安排下一步。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from terminux_engine.bounded_execution.analysis_tasks import errors as E
from terminux_engine.bounded_execution.analysis_tasks import models as M
from terminux_engine.bounded_execution.analysis_tasks.canonical import monotonic_s
from terminux_engine.bounded_execution.analysis_tasks.store import (
    TaskEventWriteError, TaskEventWriter,
)

_LOG = logging.getLogger(__name__)


class TaskRuntimeHandle(Protocol):
    """`AnalysisTaskService` 提供給 executor 的最小 live state 介面。"""

    def acquire(self) -> Any: ...
    def release(self) -> None: ...
    def next_sequence(self) -> int: ...
    #: 目前是否已被要求停止（cancel／deadline／budget）。必須持 task lock 讀。
    def stop_requested(self) -> bool: ...
    def set_step_status(self, step_id: str, status: str, **fields: Any) -> None: ...
    def record_dispatched_step(self, step_id: str) -> None: ...
    def dispatched_step_ids(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class StepExecutionDeps:
    """執行一個 step 所需的依賴。全部由 daemon composition 注入。"""

    writer: TaskEventWriter
    proposal_registry: Any
    #: `(action_name, arguments, task_owner, context) -> ActionProposal`
    create_task_proposal: Callable[..., Any]
    #: `() -> DispatchDeps`
    dispatch_deps_factory: Callable[[], Any]
    #: `() -> (catalog_snapshot_id, {capability_id: normalized_capability})`
    catalog_provider: Callable[[], tuple[str, dict[str, dict[str, Any]]]]
    #: ORFS 範本注入：`(capability_id, full_result) -> ActionSummary`
    summarize: Callable[[str, Any], Any]
    #: ORFS 範本注入：`() -> RunAccessResult`
    validate_run_access: Callable[[], Any]
    activation_gate: Any
    #: `() -> CanonicalRequestContext`（host 端 scope，caller 不能提供）
    request_context: Callable[[], Any]
    #: `() -> TaskAccessBinding | None`，用於每步重驗。
    access_binding_provider: Callable[[], Any] | None = None
    #: `() -> (ready, reason)`，每步派送前再檢查一次。
    provider_readiness: Callable[[], tuple[bool, str | None]] | None = None


@dataclass(frozen=True)
class StepExecutionResult:
    """一個 step 走完之後的結論。"""

    step_status: str
    outcome: M.TaskActionOutcome | None
    #: 下一步是否可以安排。`False` **不**代表失敗——indeterminate 也是 False。
    can_continue: bool
    error_code: str | None
    reason: str | None
    result_event_id: str | None = None
    result_sha256: str | None = None
    business_class: str | None = None
    #: 本地 action future 是否確定返回。False 時名額不得釋放。
    worker_confirmed: bool = True
    persistence_status: str = "durable"


def execute_task_step(
    deps: StepExecutionDeps,
    handle: TaskRuntimeHandle,
    *,
    task: M.AnalysisTask,
    grant: M.TaskAuthorizationGrant,
    step: M.TaskStepSpec,
    previous_can_continue: bool,
) -> StepExecutionResult:
    """執行固定兩步中的一步。呼叫端**不得**持有 task lock。"""
    handle.acquire()
    try:
        precheck = _precheck_locked(
            deps, handle, task=task, grant=grant, step=step,
            previous_can_continue=previous_can_continue,
        )
        if precheck is not None:
            return precheck

        prepared = _prepare_and_claim_locked(deps, handle, task=task, grant=grant, step=step)
    finally:
        handle.release()

    if isinstance(prepared, StepExecutionResult):
        return prepared

    # ── 步驟 5：claim 已成功，離開 task lock 才執行 ────────────────────
    proposal_id, proposal_digest, claimed, binding = prepared
    context = deps.request_context()
    dispatch = _dispatch(deps, proposal_id, proposal_digest, claimed, context)

    # ── 步驟 6：完整結果 → receipt → 讀回驗證 ─────────────────────────
    handle.acquire()
    try:
        return _persist_outcome_locked(
            deps, handle, task=task, grant=grant, step=step, binding=binding,
            proposal_id=proposal_id, dispatch=dispatch,
        )
    finally:
        handle.release()


# ── 步驟 1 ───────────────────────────────────────────────────────────


def _precheck_locked(
    deps: StepExecutionDeps, handle: TaskRuntimeHandle, *,
    task: M.AnalysisTask, grant: M.TaskAuthorizationGrant, step: M.TaskStepSpec,
    previous_can_continue: bool,
) -> StepExecutionResult | None:
    if handle.stop_requested():
        # 取消不是錯誤碼：這一步確定從未派送，只記原因（SKIPPED）。
        return _skipped(step, None,
                        "the task was stopped before this step was dispatched")
    if not previous_can_continue:
        return _skipped(step, None, "the previous step did not produce a continuable result")
    if step.step_id in handle.dispatched_step_ids():
        return _skipped(step, E.TASK_ALREADY_STARTED,
                        "this step already has a dispatch intent")
    if grant.task_id != task.task_id or grant.task_digest != task.task_digest:
        return _blocked(step, E.TASK_DIGEST_MISMATCH,
                        "the live grant does not match this task snapshot")
    if not task.digest_matches():
        return _blocked(step, E.TASK_DIGEST_MISMATCH,
                        "the task snapshot digest does not recompute")
    if monotonic_s() >= grant.expires_at_monotonic:
        return _blocked(step, E.TASK_INSTANCE_EXPIRED, "the root authorization expired")
    if monotonic_s() >= grant.deadline_at_monotonic:
        return _blocked(step, E.TASK_DEADLINE_EXCEEDED, "the task deadline was exceeded")

    context = deps.request_context()
    if (context.member_id != task.scope.member_id
            or context.project_id != task.scope.project_id):
        return _blocked(step, E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                        "the execution scope does not match the task owner")
    return None


# ── 步驟 2〜4（同一個 task lock 內）────────────────────────────────────


def _prepare_and_claim_locked(
    deps: StepExecutionDeps, handle: TaskRuntimeHandle, *,
    task: M.AnalysisTask, grant: M.TaskAuthorizationGrant, step: M.TaskStepSpec,
) -> tuple[str, str, dict[str, Any], M.TaskStepBinding] | StepExecutionResult:
    from terminux_engine.bounded_execution.binding import revalidate_binding_material
    from terminux_engine.bounded_execution.eligibility import check_capability_eligibility

    if not getattr(deps.activation_gate, "enabled", False):
        return _blocked(step, E.TASK_REVALIDATION_FAILED,
                        "bounded execution is not activated on this daemon")

    context = deps.request_context()
    snapshot_id, capabilities = deps.catalog_provider()
    capability = capabilities.get(step.capability_id)

    denial = revalidate_binding_material(
        capability=capability or {}, catalog_snapshot_id=snapshot_id,
        arguments=step.arguments,
        member_id=context.member_id, project_id=context.project_id,
        session_id=context.session_id,
        expected_capability_id=step.capability_id,
        expected_catalog_snapshot_id=step.catalog_snapshot_id,
        expected_capability_contract_sha256=step.capability_contract_sha256,
        expected_arguments_sha256=step.arguments_sha256,
        expected_risk=step.risk,
        expected_identity=(task.scope.member_id, task.scope.project_id,
                           task.scope.origin_session_id),
    )
    if denial is not None:
        return _blocked(step, E.TASK_REVALIDATION_FAILED, denial)

    eligibility = check_capability_eligibility(
        capability_id=step.capability_id, capability=capability,
        arguments=step.arguments, expected_risk=step.risk,
    )
    if not eligibility.eligible:
        return _blocked(step, E.TASK_REVALIDATION_FAILED,
                        eligibility.denial_code or "capability is not eligible")

    access = deps.validate_run_access()
    if getattr(access, "decision", "UNVERIFIABLE") != "ALLOWED":
        return _blocked(step, E.TASK_RUN_ACCESS_UNVERIFIABLE,
                        getattr(access, "reason_code", "run access is not verifiable"))

    # 本步必須在**與建立當下相同**的 policy 下派送。policy 撤除、
    # 變動或 digest 漂移都不派送——不消耗新的 root grant 或 step claim。
    if deps.access_binding_provider is not None:
        expected = task.access_binding
        try:
            current = deps.access_binding_provider()
        except Exception as exc:  # noqa: BLE001
            return _blocked(step, E.TASK_RUN_ACCESS_UNVERIFIABLE, repr(exc))
        if expected is None or current is None:
            return _blocked(step, E.TASK_RUN_ACCESS_UNVERIFIABLE,
                            "this task has no access binding to revalidate")
        if current.binding_digest() != expected.binding_digest():
            return _blocked(step, E.TASK_RUN_ACCESS_UNVERIFIABLE,
                            "the analysis access policy changed since this task "
                            "was created")

    # 派送前再確認分析執行依賴仍可用。
    if deps.provider_readiness is not None:
        ready, reason = deps.provider_readiness()
        if not ready:
            return _blocked(step, E.TASK_PROVIDER_UNAVAILABLE,
                            reason or "the analysis provider is not available")

    # ── 步驟 3：task-owned proposal ＋ host 派生 binding ──────────────
    try:
        proposal = deps.create_task_proposal(
            action_name=step.capability_id, arguments=dict(step.arguments),
            context=context, task_id=task.task_id, step_id=step.step_id,
            grant_id=grant.grant_id,
        )
    except Exception as exc:  # noqa: BLE001 - 建不出來就是不派送
        return _blocked(step, E.TASK_REVALIDATION_FAILED, repr(exc))

    binding = M.TaskStepBinding(
        task_id=task.task_id, step_id=step.step_id, grant_id=grant.grant_id,
        capability_id=step.capability_id, arguments_sha256=step.arguments_sha256,
        capability_contract_sha256=step.capability_contract_sha256,
        catalog_snapshot_id=step.catalog_snapshot_id, risk=step.risk,
        scope=task.scope, proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        access_binding_digest=(task.access_binding.binding_digest()
                               if task.access_binding is not None else None),
    )

    approved, approve_denial = deps.proposal_registry.approve_from_task_grant(
        proposal.proposal_id, proposal.proposal_digest, context,
        grant=grant, step_id=step.step_id,
    )
    if approved is None:
        return _blocked(step, E.TASK_PROPOSAL_CONTROL_DENIED,
                        approve_denial or "the live grant did not authorize this step")

    # ── 步驟 4：intent 先落盤，成功後才 claim ─────────────────────────
    try:
        deps.writer.append_step_intent(task, binding, sequence=handle.next_sequence())
    except TaskEventWriteError as exc:
        # intent 沒落盤就不能 claim：否則 crash 之後無法分辨「派送過」與
        # 「從未派送」。
        return _blocked(step, E.TASK_EVIDENCE_INVALID, repr(exc))
    handle.record_dispatched_step(step.step_id)
    handle.set_step_status(step.step_id, "DISPATCH_INTENT_RECORDED")

    if handle.stop_requested():
        # cancel 只可能發生在 intent 之前（claim 與 intent 在同一 lock 內）。
        return _unclaimed_receipt(deps, handle, task, step, binding,
                                  E.TASK_PROPOSAL_CONTROL_DENIED,
                                  "the task was cancelled before the claim")

    claimed, claim_denial = deps.proposal_registry.claim_for_task_execution(
        proposal.proposal_id, proposal.proposal_digest, context,
        grant=grant, step_id=step.step_id,
    )
    if claimed is None:
        return _unclaimed_receipt(deps, handle, task, step, binding,
                                  E.TASK_PROPOSAL_CONTROL_DENIED,
                                  claim_denial or "claim failed")

    handle.set_step_status(step.step_id, "CLAIMED")
    return proposal.proposal_id, proposal.proposal_digest, claimed, binding


# ── 步驟 5 ───────────────────────────────────────────────────────────


def _dispatch(
    deps: StepExecutionDeps, proposal_id: str, proposal_digest: str,
    claimed: dict[str, Any], context: Any,
) -> Any:
    from terminux_engine.bounded_execution.approved_action_executor import (
        dispatch_claimed_proposal,
    )

    return dispatch_claimed_proposal(
        deps.dispatch_deps_factory(),
        proposal_id=proposal_id, proposal_digest=proposal_digest, claimed=claimed,
        member_id=context.member_id, project_id=context.project_id,
        session_id=context.session_id,
    )


# ── 步驟 6 ───────────────────────────────────────────────────────────


def _persist_outcome_locked(
    deps: StepExecutionDeps, handle: TaskRuntimeHandle, *,
    task: M.AnalysisTask, grant: M.TaskAuthorizationGrant, step: M.TaskStepSpec,
    binding: M.TaskStepBinding, proposal_id: str, dispatch: Any,
) -> StepExecutionResult:
    summary = deps.summarize(step.capability_id, dispatch.full_result)
    business_class = getattr(summary, "business_class", None)

    if dispatch.timed_out:
        execution_status: M.ExecutionOutcomeStatus = "unconfirmed"
        worker_completion: M.WorkerCompletion = "unknown"
    elif dispatch.execution_status == "SUCCEEDED":
        execution_status, worker_completion = "succeeded", "confirmed"
    elif dispatch.denial_code is not None:
        execution_status, worker_completion = "denied", "confirmed"
    else:
        execution_status, worker_completion = "failed", "confirmed"

    outcome = M.TaskActionOutcome(
        execution_status=execution_status, timed_out=dispatch.timed_out,
        worker_completion=worker_completion, full_result=dispatch.full_result,
        action_status=getattr(summary, "action_status", None),
        error_code=dispatch.denial_code,
        package_record_refs=tuple(getattr(summary, "record_refs", ()) or ()),
    )

    envelope = M.build_result_envelope(
        task_id=task.task_id, step_id=step.step_id, proposal_id=proposal_id,
        grant_id=grant.grant_id, capability_id=step.capability_id,
        arguments_sha256=step.arguments_sha256, execution_status=execution_status,
        action_status=outcome.action_status, business_class=business_class,
        full_result=dispatch.full_result,
        package_record_refs=outcome.package_record_refs,
    )

    result_event_id: str | None = None
    result_sha256: str | None = None
    persistence = "durable"
    error_code: str | None = outcome.error_code

    if envelope.canonical_size() > M.MAX_RESULT_BYTES:
        # 超限不裁切內容：寫一筆小型失敗 receipt，明說結果未持久化。
        persistence = "failed"
        error_code = E.RESULT_TOO_LARGE
    else:
        try:
            appended = deps.writer.append_step_result(
                task, envelope, sequence=handle.next_sequence())
            result_event_id, result_sha256 = appended.event_id, envelope.result_sha256
        except TaskEventWriteError as exc:
            _LOG.warning("analysis task result not persisted: %r", exc)
            persistence = "failed"
            error_code = E.TASK_EVIDENCE_INVALID
        except Exception as exc:  # noqa: BLE001 - canonical 編碼失敗等
            _LOG.warning("analysis task result not serializable: %r", exc)
            persistence = "failed"
            error_code = E.RESULT_NOT_SERIALIZABLE

    if outcome.is_unconfirmed:
        step_status = "OUTCOME_UNCONFIRMED"
    elif execution_status == "succeeded" and persistence == "durable":
        step_status = "SUCCEEDED"
    elif execution_status == "denied":
        step_status = "DENIED"
    else:
        step_status = "FAILED"

    can_continue = (
        step_status == "SUCCEEDED" and business_class == "valid"
        and persistence == "durable"
    )

    try:
        deps.writer.append_step_receipt(
            task, sequence=handle.next_sequence(), step_id=step.step_id,
            step_status=step_status, outcome=outcome.to_json_dict(),
            persistence_status=persistence, result_event_id=result_event_id,
            result_sha256=result_sha256, can_continue=can_continue,
            error_code=error_code,
        )
    except TaskEventWriteError as exc:
        _LOG.warning("analysis task receipt not persisted: %r", exc)
        persistence = "failed"
        step_status = "OUTCOME_UNCONFIRMED"
        can_continue = False

    handle.set_step_status(
        step.step_id, step_status, execution_status=execution_status,
        action_status=outcome.action_status, business_class=business_class,
        result_event_id=result_event_id, result_sha256=result_sha256,
        background_work_may_continue=outcome.is_unconfirmed,
    )
    return StepExecutionResult(
        step_status=step_status, outcome=outcome, can_continue=can_continue,
        error_code=error_code, reason=None, result_event_id=result_event_id,
        result_sha256=result_sha256, business_class=business_class,
        worker_confirmed=worker_completion == "confirmed",
        persistence_status=persistence,
    )


# ── 小工具 ───────────────────────────────────────────────────────────


def _unclaimed_receipt(
    deps: StepExecutionDeps, handle: TaskRuntimeHandle, task: M.AnalysisTask,
    step: M.TaskStepSpec, binding: M.TaskStepBinding, error_code: str, reason: str,
) -> StepExecutionResult:
    """intent 已落盤但沒有 claim：寫一筆**確定的未派送** receipt。

    不刪 intent、不再 claim。
    """
    persistence = "durable"
    try:
        deps.writer.append_step_receipt(
            task, sequence=handle.next_sequence(), step_id=step.step_id,
            step_status="DENIED",
            outcome={"execution_status": "denied", "timed_out": False,
                     "worker_completion": "confirmed", "dispatched": False},
            persistence_status="durable", result_event_id=None, result_sha256=None,
            can_continue=False, error_code=error_code,
        )
    except TaskEventWriteError:
        persistence = "failed"
    handle.set_step_status(step.step_id, "DENIED", reason=reason)
    return StepExecutionResult(
        step_status="DENIED", outcome=None, can_continue=False,
        error_code=error_code, reason=reason, persistence_status=persistence,
    )


def _skipped(step: M.TaskStepSpec, error_code: str | None, reason: str) -> StepExecutionResult:
    return StepExecutionResult(
        step_status="SKIPPED", outcome=None, can_continue=False,
        error_code=error_code, reason=reason,
    )


def _blocked(step: M.TaskStepSpec, error_code: str, reason: str) -> StepExecutionResult:
    return StepExecutionResult(
        step_status="SKIPPED", outcome=None, can_continue=False,
        error_code=error_code, reason=reason,
    )


__all__ = [
    "StepExecutionDeps",
    "StepExecutionResult",
    "TaskRuntimeHandle",
    "execute_task_step",
]
