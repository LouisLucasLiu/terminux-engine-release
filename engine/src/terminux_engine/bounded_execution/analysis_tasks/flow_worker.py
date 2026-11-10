# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""flow task 的五步 worker。

順序與 analysis task 相同的原則，只是多了兩件事：**啟動 EDA** 與 **等待完成證據**。

每一步固定：

1. 短臨界區內重驗 scope／profile／input／capability／期限／預算，
   保存 `step_intent`，再 claim 本步 proposal。
2. 慢速工作（inputs 逐檔驗證、候選複製、EDA 等待）**一律在鎖外**。
3. `run_flow` 的 claim 後只拿到「啟動交接」；step 要等真實的完成證據
   才寫可繼續的 receipt。**launch accepted ≠ step SUCCEEDED。**

跨步 binding：`s2`／`s4`／`s5` 的參數由 host 在執行當下，從已驗證的 Kernel
reference 具體化，再算本步 digest。LLM 沒有預填 run id 的位置。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from terminux_engine.bounded_execution.analysis_tasks import errors as E
from terminux_engine.bounded_execution.analysis_tasks import flow_models as F
from terminux_engine.bounded_execution.analysis_tasks import models as M
from terminux_engine.bounded_execution.analysis_tasks import run_evidence as RE
from terminux_engine.bounded_execution.analysis_tasks.canonical import monotonic_s
from terminux_engine.bounded_execution.analysis_tasks.store import (
    TaskEventWriteError,
    next_sequence,
)

_LOG = logging.getLogger(__name__)

#: scoped status／evidence 查詢的固定間隔。
EVIDENCE_POLL_INTERVAL_SEC = 1.0


@dataclass(frozen=True)
class FlowStepDeps:
    """flow worker 的全部依賴。由 daemon composition 注入。"""

    writer: Any
    proposal_registry: Any
    create_task_proposal: Callable[..., Any]
    dispatch_deps_factory: Callable[[], Any]
    catalog_provider: Callable[[], tuple[str, dict[str, Any]]]
    request_context: Callable[[], Any]
    activation_gate: Any
    #: `() -> FlowTaskProfile`，每次呼叫都重驗檔案與 digest（無 hot reload）。
    #: `(task_id) -> FlowTaskProfile`，回傳 task-owned immutable snapshot。
    profile_for_task: Callable[[str], Any]
    ledger: Any
    receipt_reader: Any
    #: `(binding, on_run_created) -> contextmanager`，把 binding 與執行
    #: facade 注入 package services。`on_run_created` 在 child 啟動**前**
    #: 被呼叫，讓 task service 先持久化 RunRef。
    bind_services: Callable[..., Any]
    #: `(manifest_path, sha256, root) -> VerifyResult`
    verify_inputs: Callable[..., Any]
    #: `(capability_id, full_result) -> ActionSummary`
    summarize: Callable[[str, Any], Any]
    #: `(output_root, platform, design, variant) -> EffectiveSettings`
    read_effective_settings: Callable[..., Any]
    #: `(output_root, platform, design, variant) -> ParameterConsumption`
    consumption_evidence: Callable[..., Any]
    #: EDA／provider 派送的平行度上限（BoundedSemaphore）。`None` ⇒ 不限。
    eda_slots: Any = None
    provider_slots: Any = None
    #: 既有 DLE ledger 路徑。`None` ⇒ 不寫 lineage（測試用）。
    lineage_ledger_path: Any = None


@dataclass
class FlowStepOutcome:
    step_status: str
    can_continue: bool
    error_code: str | None = None
    reason: str | None = None
    result_event_id: str | None = None
    result_sha256: str | None = None
    business_class: str | None = None
    worker_confirmed: bool = True
    persistence_status: str = "durable"
    details: dict[str, Any] = field(default_factory=dict)


class FlowStopped(RuntimeError):
    """task 已被要求停止（cancel／deadline）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── 每步共用的重驗 ───────────────────────────────────────────────────


def revalidate_step_locked(
    deps: FlowStepDeps, live: Any, task: Any, grant: Any, step_id: str,
    *, resolved_arguments: dict[str, Any], capability_id: str,
) -> tuple[str, dict[str, Any], str]:
    """短臨界區內的重驗。回傳 `(snapshot_id, capability, arguments_sha256)`。

    規則與 analysis task 相同、由同一份 helper 提供；這裡額外加上 profile 與
    input 綁定的重驗。呼叫端必須持 task lock。
    """
    from terminux_engine.bounded_execution.binding import (
        compute_arguments_sha256, compute_capability_contract_sha256,
        revalidate_binding_material,
    )
    from terminux_engine.bounded_execution.eligibility import (
        check_capability_eligibility,
    )

    if live.stop:
        raise FlowStopped("the task was stopped before this step was dispatched")
    if not getattr(deps.activation_gate, "enabled", False):
        raise FlowStopped("bounded execution is not activated on this daemon")
    if monotonic_s() >= grant.deadline_at_monotonic:
        raise FlowStopped("the task deadline was exceeded")
    if not task.digest_matches():
        raise FlowStopped("the task snapshot digest does not recompute")

    # profile 是 task-owned immutable snapshot；這裡驗的是「本
    # task 持有的那一份」與 DRAFT 時綁進 digest 的身分一致，**不**重讀
    # live 檔案——live 檔案改了也換不掉既有 task 的內容。
    profile = deps.profile_for_task(task.task_id)
    if profile is None or profile.profile_digest != task.profile_digest:
        raise FlowStopped("the task-owned profile snapshot is missing or does not "
                          "match the approved task digest")

    context = deps.request_context()
    if (context.member_id != task.scope.member_id
            or context.project_id != task.scope.project_id):
        raise FlowStopped("the execution scope does not match the task owner")

    # **每一次派送前**都走同一個 limited activation gate（scope ＋
    # profile 身分）。kill switch 在這裡生效——已在跑的 task 不會再派下一步，
    # 但 `get`／`cancel` 不經過這裡，讀回與停止一律保留。
    check = getattr(deps.activation_gate, "check", None)
    if callable(check):
        denial = check(member_id=context.member_id,
                       project_id=context.project_id, profile=profile)
        if denial is not None:
            raise FlowStopped(f"limited activation denied this dispatch: "
                              f"{denial[0]}: {denial[1]}")

    snapshot_id, capabilities = deps.catalog_provider()
    capability = capabilities.get(capability_id)
    if capability is None:
        raise FlowStopped(f"capability {capability_id!r} is no longer registered")

    arguments_sha = compute_arguments_sha256(resolved_arguments)
    denial = revalidate_binding_material(
        capability=capability, catalog_snapshot_id=snapshot_id,
        arguments=resolved_arguments,
        member_id=context.member_id, project_id=context.project_id,
        session_id=context.session_id,
        expected_capability_id=capability_id,
        expected_catalog_snapshot_id=snapshot_id,
        expected_capability_contract_sha256=compute_capability_contract_sha256(capability),
        expected_arguments_sha256=arguments_sha,
        expected_risk=str(capability.get("risk")),
        expected_identity=(task.scope.member_id, task.scope.project_id,
                           task.scope.origin_session_id),
    )
    if denial is not None:
        raise FlowStopped(f"step revalidation failed: {denial}")

    eligibility = check_capability_eligibility(
        capability_id=capability_id, capability=capability,
        arguments=resolved_arguments, expected_risk=str(capability.get("risk")),
    )
    if not eligibility.eligible:
        raise FlowStopped(
            f"capability is not eligible: {eligibility.denial_code}")
    return snapshot_id, capability, arguments_sha


def claim_step_locked(
    deps: FlowStepDeps, live: Any, task: Any, grant: Any, *,
    step_id: str, capability_id: str, resolved_arguments: dict[str, Any],
    snapshot_id: str, capability: dict[str, Any], arguments_sha: str,
) -> tuple[str, str, dict[str, Any]]:
    """建立 task-owned proposal、核准、**先落盤 intent 再 claim**。

    回傳 `(proposal_id, proposal_digest, claimed)`。呼叫端必須持 task lock。
    """
    from terminux_engine.bounded_execution.binding import (
        compute_capability_contract_sha256,
    )

    context = deps.request_context()
    proposal = deps.create_task_proposal(
        action_name=capability_id, arguments=dict(resolved_arguments),
        context=context, task_id=task.task_id, step_id=step_id,
        grant_id=grant.grant_id,
    )
    binding = M.TaskStepBinding(
        task_id=task.task_id, step_id=step_id, grant_id=grant.grant_id,
        capability_id=capability_id, arguments_sha256=arguments_sha,
        capability_contract_sha256=compute_capability_contract_sha256(capability),
        catalog_snapshot_id=snapshot_id, risk=str(capability.get("risk")),
        scope=task.scope, proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        access_binding_digest=task.profile_digest,
    )
    approved, denial = deps.proposal_registry.approve_from_task_grant(
        proposal.proposal_id, proposal.proposal_digest, context,
        grant=grant, step_id=step_id)
    if approved is None:
        raise FlowStopped(f"the live grant did not authorize this step: {denial}")

    # **intent 先落盤，成功後才 claim。**
    sequence = next_sequence(live)
    deps.writer.append(
        event_type=F.EVENT_STEP_INTENT, task_id=task.task_id,
        task_digest=task.task_digest,
        daemon_instance_id=task.daemon_instance_id, scope=task.scope,
        sequence=sequence,
        body={"step_id": step_id, "binding": binding.to_json_dict()},
    )
    live.dispatched.add(step_id)

    if live.stop:
        raise FlowStopped("the task was cancelled before the claim")
    claimed, claim_denial = deps.proposal_registry.claim_for_task_execution(
        proposal.proposal_id, proposal.proposal_digest, context,
        grant=grant, step_id=step_id)
    if claimed is None:
        raise FlowStopped(f"claim failed: {claim_denial}")
    return proposal.proposal_id, proposal.proposal_digest, claimed


def dispatch_step(deps: FlowStepDeps, *, proposal_id: str, proposal_digest: str,
                  claimed: dict[str, Any], binding: Any,
                  task_id: str,
                  on_run_created: Any = None,
                  imported_run_dqe_overrides: dict[str, str] | None = None,
                  allow_llm: bool | None = None,
                  formal_proof: dict | None = None,
                  ) -> Any:
    """在鎖外派送。binding 只在這一次呼叫期間注入 package services。"""
    from terminux_engine.bounded_execution.approved_action_executor import (
        dispatch_claimed_proposal,
    )

    context = deps.request_context()
    # 分析步驟（s2／s5）不需要也不應偽造 EDA host binding，但仍必須取得
    # task-owned Profile evidence scope。task_id 由 controller 的已核准 task
    # 傳入，從不取自 action arguments。
    with deps.bind_services(binding, on_run_created, task_id=task_id,
                            imported_run_dqe_overrides=imported_run_dqe_overrides,
                            allow_llm=allow_llm,
                            formal_proof=formal_proof):
        return dispatch_claimed_proposal(
            deps.dispatch_deps_factory(),
            proposal_id=proposal_id, proposal_digest=proposal_digest,
            claimed=claimed, member_id=context.member_id,
            project_id=context.project_id, session_id=context.session_id,
        )


# ── 等待完成證據 ─────────────────────────────────────────────────


def wait_for_run_evidence(
    deps: FlowStepDeps, live: Any, *, run_id: str, slot: str, task_id: str,
    dqe_db_path: str, deadline_monotonic: float,
    parameter_name: str | None = None,
    expected_parameter_value: str | None = None,
    settings_provider: Callable[[], tuple[Any, Any]] | None = None,
) -> Any:
    """對已記錄的 run 做 scoped status／evidence 查詢，間隔 1 秒。

    **不重新 start**、不建 completion bus、不輪詢到 DQE rows 就當成功。
    task／run deadline 到達就停止等待並回報未確認。
    """
    last = None
    while True:
        if live.stop:
            return RE.RunEvidence(state=RE.UNCONFIRMED, reason="task_stopped",
                                  run_id=run_id, slot=slot)
        settings = consumption = None
        if settings_provider is not None:
            settings, consumption = settings_provider()
        last = RE.evaluate_run_evidence(
            run_id=run_id, slot=slot, ledger=deps.ledger,
            receipt_reader=deps.receipt_reader, dqe_db_path=dqe_db_path,
            expected_task_id=task_id, expected_slot=slot,
            parameter_name=parameter_name,
            expected_parameter_value=expected_parameter_value,
            effective_settings=settings, consumption=consumption)
        if last.state in (RE.READY, RE.FAILED):
            return last
        if monotonic_s() >= deadline_monotonic:
            return RE.RunEvidence(
                state=RE.UNCONFIRMED, reason="deadline_reached_while_waiting",
                run_id=run_id, slot=slot, run_status=last.run_status,
                terminal_reason=last.terminal_reason, missing=last.missing)
        time.sleep(EVIDENCE_POLL_INTERVAL_SEC)


# ── 結果與 receipt ───────────────────────────────────────────────────


def persist_step_result(
    deps: FlowStepDeps, live: Any, task: Any, grant: Any, *, step_id: str,
    capability_id: str, arguments_sha: str, proposal_id: str,
    execution_status: str, action_status: str | None,
    business_class: str | None, full_result: Any,
    package_record_refs: tuple[str, ...] = (),
) -> tuple[str | None, str | None, str]:
    """寫完整結果 envelope。回傳 `(event_id, sha256, persistence_status)`。"""
    envelope = M.build_result_envelope(
        task_id=task.task_id, step_id=step_id, proposal_id=proposal_id,
        grant_id=grant.grant_id, capability_id=capability_id,
        arguments_sha256=arguments_sha, execution_status=execution_status,
        action_status=action_status, business_class=business_class,
        full_result=full_result, package_record_refs=package_record_refs,
    )
    if envelope.canonical_size() > M.MAX_RESULT_BYTES:
        return None, None, "failed"
    try:
        sequence = next_sequence(live)
        appended = deps.writer.append(
            event_type=F.EVENT_STEP_RESULT, task_id=task.task_id,
            task_digest=task.task_digest,
            daemon_instance_id=task.daemon_instance_id, scope=task.scope,
            sequence=sequence,
            body={"step_id": step_id, "envelope": envelope.to_json_dict()},
        )
    except TaskEventWriteError as exc:
        _LOG.warning("flow task result not persisted: %r", exc)
        return None, None, "failed"
    except Exception as exc:  # noqa: BLE001 - canonical 編碼失敗等
        _LOG.warning("flow task result not serializable: %r", exc)
        return None, None, "failed"
    return appended.event_id, envelope.result_sha256, "durable"


def persist_step_receipt(
    deps: FlowStepDeps, live: Any, task: Any, *, step_id: str, step_status: str,
    outcome: dict[str, Any], persistence_status: str,
    result_event_id: str | None, result_sha256: str | None,
    can_continue: bool, error_code: str | None = None,
) -> str:
    try:
        sequence = next_sequence(live)
        deps.writer.append(
            event_type=F.EVENT_STEP_RECEIPT, task_id=task.task_id,
            task_digest=task.task_digest,
            daemon_instance_id=task.daemon_instance_id, scope=task.scope,
            sequence=sequence,
            body={
                "step_id": step_id, "step_status": step_status,
                "outcome": dict(outcome), "persistence_status": persistence_status,
                "result_event_id": result_event_id, "result_sha256": result_sha256,
                "can_continue": bool(can_continue), "error_code": error_code,
            },
        )
    except TaskEventWriteError as exc:
        _LOG.warning("flow task receipt not persisted: %r", exc)
        return "failed"
    return persistence_status


def persist_fact_event(
    deps: FlowStepDeps, live: Any, task: Any, *, event_type: str,
    body: dict[str, Any],
) -> str | None:
    """寫一筆事實／引用型事件（run_link／input_reference／milestone）。

    這些**不是**第二個 EDA ledger——它們只記錄「host 已知的引用」。
    """
    try:
        sequence = next_sequence(live)
        appended = deps.writer.append(
            event_type=event_type, task_id=task.task_id,
            task_digest=task.task_digest,
            daemon_instance_id=task.daemon_instance_id, scope=task.scope,
            sequence=sequence, body=dict(body),
        )
    except TaskEventWriteError as exc:
        _LOG.warning("flow task fact event not persisted (%s): %r", event_type, exc)
        return None
    return appended.event_id


__all__ = [
    "EVIDENCE_POLL_INTERVAL_SEC",
    "FlowStepDeps",
    "FlowStepOutcome",
    "FlowStopped",
    "claim_step_locked",
    "dispatch_step",
    "persist_fact_event",
    "persist_step_receipt",
    "persist_step_result",
    "revalidate_step_locked",
    "wait_for_run_evidence",
]
