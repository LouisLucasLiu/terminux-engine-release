# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`AnalysisTaskService`。

**沒有單一 worker 名額**。核准後的 task 交給 `TaskScheduler`
（bounded parallel；上限是 daemon resource setting）：有名額立刻開始，沒有就
進 `QUEUED`，資源釋放後依核准順序自動啟動。第二個 task 不再一律 `TASK_BUSY`。

profile 是 **task-scoped**——每個 flow task 在建立時從 user 提供的
來源（path／inline／本 session 已選用）載入、驗證並凍結成自己的 immutable
snapshot；daemon 不再持有 process-global active profile。

鎖的順序是固定的，不可反向：

    service admission lock → task lock

`start` 的驗證、grant／started 寫入與 live state 更新在同一個臨界區內完成，
cancel 不可能插到核准中間。其他步驟轉移與 cancel 只取得 task lock。要釋放
全域名額時，先放掉 task lock 再取 admission lock。**慢速 action 一律不在
任何臨界區內執行**（見 `executor.execute_task_step()` 步驟 5）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from terminux_engine.bounded_execution.analysis_tasks import errors as E
from terminux_engine.bounded_execution.analysis_tasks import executor as X
from terminux_engine.bounded_execution.analysis_tasks import models as M
from terminux_engine.bounded_execution.analysis_tasks.canonical import (
    monotonic_s, now_ms, sha256_payload,
)
from terminux_engine.bounded_execution.analysis_tasks import flow_models as F
from terminux_engine.bounded_execution.analysis_tasks import flow_profile as FP
from terminux_engine.bounded_execution.analysis_tasks import setup_margin_envelope as SME
from terminux_engine.bounded_execution.analysis_tasks.scheduler import (
    SchedulerLimits, TaskScheduler,
)
from terminux_engine.bounded_execution.analysis_tasks.store import (
    TaskEventWriteError, TaskEventWriter,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskServiceDeps:
    """daemon composition 注入的全部依賴。

    ORFS 專屬的三個函式（`build_steps`／`validate_runs`／`summarize`）由
    composition 從 `packages/orfs_run_intelligence/analysis_task_template.py`
    注入——Common 不 import ORFS。
    """

    event_store: Any
    daemon_instance_id: str
    proposal_registry: Any
    create_task_proposal: Callable[..., Any]
    dispatch_deps_factory: Callable[[], Any]
    catalog_provider: Callable[[], tuple[str, dict[str, dict[str, Any]]]]
    activation_gate: Any
    request_context: Callable[[], Any]
    #: `(current_run_id, baseline_run_id, question) -> tuple[TemplateStep, ...]`
    build_steps: Callable[..., Any]
    #: `() -> RunAccessResult`
    validate_runs: Callable[..., Any]
    #: `(capability_id, full_result) -> ActionSummary`
    summarize: Callable[[str, Any], Any]
    #: `(fn) -> None`，把 worker 丟到背景執行（非阻塞）。
    submit: Callable[[Callable[[], None]], Any]
    template_id: str
    #: `(current_run_id, baseline_run_id) -> TaskAccessBinding | None`。
    #: 沒有配置 policy 時回 None——但那種情況 run access 早已擋下，走不到這裡。
    access_binding_provider: Callable[..., Any] | None = None
    #: `() -> (ready: bool, reason: str | None)`。
    #: 分析執行依賴（provider adapter／router）是否可用。**不是** service
    #: 是否組裝的開關；只在 start 與每步派送前檢查。
    provider_readiness: Callable[[], tuple[bool, str | None]] | None = None
    #: `(member_id, project_id) -> (allowed, reason)`。
    #: **只用於 `get`**：讀取要求現行 server policy 仍允許這個 member/project，
    #: 但**不**要求舊 task 的 policy digest 與今天相同。
    #: `cancel` 刻意不使用它——policy 撤除、漂移、provider 缺席或 grant 過期
    #: 都不得讓使用者失去停止手段。
    read_access_check: Callable[[str, str], tuple[bool, str | None]] | None = None
    #: flow task 的依賴。未注入時 daemon 只提供 analysis task。
    flow: Any = None
    #: flow 路徑的 limited activation gate。**未注入 ⇒ 預設 disabled**，
    #: `create_flow_task`／`start_flow_task` 一律拒絕。`get`／`cancel` 不使用它。
    flow_activation_gate: Any = None
    #: bounded scheduler。未注入時依環境變數建立預設（active 上限 1）。
    scheduler: Any = None
    #: `(profile) -> str`，給 planner／`/profiles` 的方案描述。可選。
    describe_profile: Callable[[Any], str] | None = None
    #: 每個 session 最多記幾份已選用的 profile。
    max_selected_profiles_per_session: int = 16
    #: `outcome -> Domain response -> Global decision`；未注入時如實標示。
    eap_execution_feedback_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass
class _StepState:
    spec: M.TaskStepSpec
    status: str = "PENDING"
    execution_status: str | None = None
    action_status: str | None = None
    business_class: str | None = None
    result_event_id: str | None = None
    result_sha256: str | None = None
    background_work_may_continue: bool = False
    reason: str | None = None


@dataclass
class _LiveTask:
    """一筆 task 在**目前 daemon instance** 的 live state。

    重啟後這份 state 不存在；歷史只能由 `TaskHistoryReader` 唯讀判讀，
    **不重建執行權**。
    """

    task: M.AnalysisTask
    status: str = "DRAFT"
    lock: threading.RLock = field(default_factory=threading.RLock)
    grant: M.TaskAuthorizationGrant | None = None
    #: 下一筆 event 的 sequence。從 0 開始且不得有缺口——`TaskHistoryReader`
    #: 以 0-based 連續序列驗證歷史完整性。
    sequence: int = 0
    steps: dict[str, _StepState] = field(default_factory=dict)
    stop: bool = False
    startable: bool = True
    started_at_ms: int | None = None
    terminal_reason: str | None = None
    persistence_status: str = "durable"
    dispatched: set[str] = field(default_factory=set)
    #: 本 task 是否仍持有唯一 worker 名額。
    holds_slot: bool = False
    #: 是否有已 claim 但未確認返回的工作。
    unconfirmed_work: bool = False
    #: 本 task body 共用的 LLM dispatch 額度。start 時建立，終態後不再使用。
    budget: Any = None
    #: 排隊原因與時間（QUEUED 時才有值）。
    queue_reason: str | None = None
    queued_at_ms: int | None = None


@dataclass
class _LiveFlowTask:
    """一筆 flow task 在**目前 daemon instance** 的 live state。

    與 `_LiveTask` 分開，因為 flow task 多了 run／input 綁定與
    `current_operation`；但兩者共用同一個 admission lock 與 worker 名額
    （不另建第二套 lifecycle）。
    """

    task: Any
    status: str = "DRAFT"
    lock: threading.RLock = field(default_factory=threading.RLock)
    grant: M.TaskAuthorizationGrant | None = None
    sequence: int = 0
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    stop: bool = False
    startable: bool = True
    started_at_ms: int | None = None
    terminal_reason: str | None = None
    persistence_status: str = "durable"
    dispatched: set[str] = field(default_factory=set)
    holds_slot: bool = False
    budget: Any = None
    current_operation: str = F.OPERATION_IDLE
    #: slot -> RunRef。由 `on_run_created` 在 child 啟動前寫入。
    runs: dict[str, Any] = field(default_factory=dict)
    #: 已用掉的 EDA launch 次數。額度由 profile limits 固定。
    eda_launches_used: int = 0
    candidate_writes_used: int = 0
    #: 候選 input 的身分（s3 之後才有）。
    variant_ref: Any = None
    #: slot -> VariantInputRef。兩個候選各自獨立，不共用可寫路徑。
    variant_refs: dict[str, Any] = field(default_factory=dict)
    #: s8 的 deterministic 選擇結果（`flow_task.selection.v1` 的內容）。
    selection: dict[str, Any] | None = None
    #: s5 的固定修復判定（`flow_task.repair.v1` 的內容）。
    repair: dict[str, Any] | None = None
    #: 由 s1 派生的功能驗證參考（host-only）。
    verification_reference: dict[str, Any] | None = None
    #: s1 之後派生的實體邊界綁定（只有 v5 template 會有）。
    layout_binding: Any = None
    #: 每一段解說被拒的紀錄（含未續跑的）。
    narrative_rejections: list[dict[str, Any]] = field(default_factory=list)
    unconfirmed_work: bool = False
    #: task-owned immutable profile snapshot 與它的來源身分。
    profile: Any = None
    profile_source: Any = None
    #: 排隊原因與時間（QUEUED 時才有值）。
    queue_reason: str | None = None
    queued_at_ms: int | None = None
    #: slot -> {intent／executed commit 引用, state}。
    lineage: dict[str, Any] = field(default_factory=dict)
    #: 結果已交給 Global 的下一步；永遠不是下一次的執行授權。
    eap_feedback: dict[str, Any] | None = None
    #: 由本 daemon 驗證的 baseline-only child 導入身分。
    #: 這是 host-only state；provider/client 不能自行提供 run／grant／binding。
    baseline_import: dict[str, Any] | None = None


class _RuntimeHandle:
    """把 `_LiveTask` 包成 `executor.TaskRuntimeHandle`。"""

    def __init__(self, live: _LiveTask) -> None:
        self._live = live

    def acquire(self) -> Any:
        return self._live.lock.acquire()

    def release(self) -> None:
        self._live.lock.release()

    def next_sequence(self) -> int:
        value = self._live.sequence
        self._live.sequence += 1
        return value

    def stop_requested(self) -> bool:
        return self._live.stop

    def set_step_status(self, step_id: str, status: str, **fields: Any) -> None:
        state = self._live.steps[step_id]
        state.status = status
        for key, value in fields.items():
            setattr(state, key, value)

    def record_dispatched_step(self, step_id: str) -> None:
        self._live.dispatched.add(step_id)

    def dispatched_step_ids(self) -> frozenset[str]:
        return frozenset(self._live.dispatched)


def _invalid_production_reference(baseline_import: dict[str, Any],
                                  profile: Any) -> str | None:
    """candidate 授權前的 production reference 核對；不成立就回拒絕理由。"""
    policy = _baseline_reference_policy(profile)
    reference = baseline_import.get("verification_reference")
    record = (reference or {}).get("production_reference") if isinstance(
        reference, dict) else None
    if not policy["required_for_import"]:
        # qualification fixture：不得帶 production reference 冒充。
        if record is not None:
            return ("a QUALIFICATION_ONLY fixture baseline must not carry a "
                    "production functional reference")
        return None
    identity = getattr(profile, "declared_method_identity", None)
    requires = True
    produces = False
    if identity:
        try:
            from terminux_engine.packages.orfs_run_intelligence import (
                method_extensions as ME,
            )

            extension = ME.resolve_declared_identity(identity)
            requires = extension.requires_baseline_functional_reference
            produces = extension.produces_baseline_functional_reference
        except Exception:  # noqa: BLE001 - 認不得的身分照舊要求
            requires, produces = True, False
    if not produces:
        # 方法不產生 Package reference（例如既有 setup-margin 路徑）：沿用
        # 既有的 verification reference 檢查，不在這裡另加要求。
        return None
    if record is None:
        return ("the method declares it produces a baseline functional reference, "
                "but the imported baseline carries none") if requires else None
    from terminux_engine.packages.orfs_run_intelligence import (
        baseline_reference as BR,
    )

    run = baseline_import.get("run") or {}
    pinned_roles = getattr(getattr(profile, "execution", None),
                           "artifact_role_mapping", None)
    problems = BR.verify_reference(record, expected={
        "task_id": baseline_import.get("source_task_id"),
        "run_id": run.get("run_id"),
        "method_identity": identity,
        "manifest_sha256": profile.input_manifest_sha256,
        "tree_hash": baseline_import.get("input_tree_hash"),
        "docker_image_id": profile.docker_image_id,
        # host-native Profile 以工具 binding digest 比對來源 baseline 的工具身分。
        **({"native_tool_binding_sha256": profile.native_tool_binding.digest}
           if getattr(profile, "native_tool_binding", None) is not None else {}),
        # 方法宣告的 proof kind 與 Profile 釘住的 role allowlist 都要對得上。
        "required_proof_kinds": list(extension.required_proof_kinds),
        "artifact_role_mapping": (pinned_roles.to_json_dict()
                                  if pinned_roles is not None else None),
        "run_identity": {"result_event_id": baseline_import.get("result_event_id")},
    })
    return ("the baseline functional reference is not usable: "
            + "; ".join(problems)) if problems else None


def _baseline_reference_policy(profile: Any) -> dict[str, Any]:
    """由 method extension 的宣告決定 baseline functional reference 的要求。

    - 宣告「本方法產生」⇒ import 必須帶它（既有 setup-margin 安全要求不變）。
    - 宣告「不產生」＋ Profile 是 QUALIFICATION_ONLY fixture ⇒ 可以沒有，
      並如實標成 `NOT_PRODUCED_QUALIFICATION_ONLY`（不是 production proof）。
    - 其他情況（宣告不產生但不是 qualification）⇒ 仍然必須帶它：production
      路徑不得因為方法不自己產生就免除。
    """
    identity = getattr(profile, "declared_method_identity", None)
    if not identity:
        return {"required_for_import": True, "state": "REQUIRED"}
    try:
        from terminux_engine.packages.orfs_run_intelligence import (
            method_extensions as ME,
        )

        extension = ME.resolve_declared_identity(identity)
    except Exception:  # noqa: BLE001 - 認不得的身分照舊要求 reference
        return {"required_for_import": True, "state": "REQUIRED"}
    if getattr(profile, "is_qualification_fixture", False):
        # fixture 永遠不產生、也不得冒充 production reference。
        return {"required_for_import": False,
                "state": "NOT_PRODUCED_QUALIFICATION_ONLY"}
    if extension.produces_baseline_functional_reference:
        return {"required_for_import": True, "state": "REQUIRED"}
    return {"required_for_import": True, "state": "REQUIRED_NOT_PRODUCED_BY_METHOD"}


class AnalysisTaskService:
    """固定兩步分析 task 的唯一權威。"""

    def __init__(self, deps: TaskServiceDeps) -> None:
        self._deps = deps
        self._writer = TaskEventWriter(deps.event_store)
        #: bounded scheduler 取代單一 admission lock。analysis task
        #: 與 flow task 共用同一份名額（不另建第二套 lifecycle）。
        self._scheduler: TaskScheduler = (
            deps.scheduler if deps.scheduler is not None
            else TaskScheduler(SchedulerLimits.from_env()))
        #: session_id -> {profile_id: (profile, source, accepted input)}。只是方便
        #: 搜尋與自然語言對應的選擇清單，**不是**執行 allowlist。
        self._session_profiles: dict[str, dict[str, tuple[Any, Any, dict[str, Any]]]] = {}
        self._tasks: dict[str, _LiveTask] = {}
        #: flow task 與 analysis task 共用同一個 admission lock 與名額；
        #: 只是 live state 形狀不同，因此分開存放。
        self._flow_tasks: dict[str, _LiveFlowTask] = {}
        #: FlowTask bridge 重送同一個已固定 proposal 時回傳原 DRAFT，絕不重複建 task。
        self._eap_flow_tasks: dict[tuple[str, str, str, str, str, str], str] = {}
        self._tasks_lock = threading.Lock()
        # B1 只把 CORE wait intent 與其唯一 dependent proof task 接起來；task 的
        # 建立、取消、Kernel termination 與 reservation 一律仍走既有 FlowTask。
        self._dependency_lock = threading.RLock()
        self._dependency_children: dict[str, tuple[str, str]] = {}
        self._dependency_coordinator: Any = None
        if deps.flow is not None:
            from terminux_engine.bounded_execution.analysis_tasks.dependency_coordinator import (
                DependencyCoordinator,
            )

            self._dependency_coordinator = DependencyCoordinator(
                flow_service=self, controller=deps.flow)

    # ── B1 dependency 接線 ──────────────────────────────────────────

    def _dependency_core_accepts(self, core_task_id: str) -> bool:
        """只有仍在等待且未取消的 CORE 才能建立／啟動 proof child。"""
        with self._tasks_lock:
            live = self._flow_tasks.get(core_task_id)
        if live is None:
            return False
        with live.lock:
            return (live.status == F.STATUS_WAITING_DEPENDENCY and not live.stop)

    def _register_dependency_proof_task(self, core_task_id: str, task_id: str,
                                        task_digest: str) -> None:
        self._dependency_children[core_task_id] = (task_id, task_digest)

    def _find_dependency_proof_task(self, core_task_id: str, draft_id: str
                                    ) -> dict[str, Any] | None:
        """由既有 child snapshot 找回 coordinator 關係；不建立任何 task。"""
        known = self._dependency_children.get(core_task_id)
        if known is not None:
            task_id, task_digest = known
            with self._tasks_lock:
                live = self._flow_tasks.get(task_id)
            if live is not None:
                return {"task_id": task_id, "task_digest": task_digest,
                        "status": live.status}
        with self._tasks_lock:
            lives = list(self._flow_tasks.values())
        for live in lives:
            source = ((getattr(live.task, "eap_proposal", None) or {}).get(
                "source") or {})
            if source.get("draft_id") == draft_id:
                record = (live.task.task_id, live.task.task_digest)
                self._dependency_children[core_task_id] = record
                return {"task_id": record[0], "task_digest": record[1],
                        "status": live.status}
        return None

    def _cancel_dependency_proof_task(self, core_task_id: str) -> list[dict[str, Any]]:
        """取消已知的 dependent child，沿用既有 FlowTask／Kernel cleanup。"""
        known = self._dependency_children.get(core_task_id)
        if known is None:
            return []
        task_id, task_digest = known
        return [self.cancel_flow_task(task_id, task_digest)]

    def _coordinate_dependencies(self) -> None:
        """worker 收束後推進 B1；沒有等待 intent 時是唯讀 no-op。"""
        coordinator = self._dependency_coordinator
        if coordinator is None:
            return
        context = self._deps.request_context()
        from terminux_engine.bounded_execution.analysis_tasks.dependency_coordinator import (
            waiting_dependencies,
        )

        try:
            coordinator.reconcile(waiting_dependencies(
                self._deps.event_store, member_id=context.member_id))
        except Exception as exc:  # noqa: BLE001 - 不可讓 coordinator 破壞既有 worker 收束
            _LOG.warning("dependency coordinator reconciliation failed: %r", exc)

    def _run_flow_worker(self, flow: Any, live: _LiveFlowTask) -> None:
        """既有 worker 結束後才接 B1；不在 FlowTask lock 內做編排。"""
        flow.run(self, live)
        # FlowController.run() 會在 finally 清掉它自己建立的 ContextVar；B1 的
        # public FlowTask API 仍需同一筆 task 固定的 canonical scope 才能讀回／
        # 建立 child，因此在 coordinator 這一小段重新建立相同 context。
        from terminux_engine.ipc.request_context import (
            CanonicalRequestContext, reset_current_request_context,
            set_current_request_context,
        )

        task = live.task
        token = set_current_request_context(CanonicalRequestContext(
            session_id=task.scope.origin_session_id,
            member_id=task.scope.member_id, project_id=task.scope.project_id,
            project_source=task.scope.project_source))
        try:
            self._coordinate_dependencies()
        finally:
            reset_current_request_context(token)

    # ── create ────────────────────────────────────────────────

    def create(
        self, *, current_run_id: str, baseline_run_id: str, question: str,
    ) -> dict[str, Any]:
        context = self._deps.request_context()
        if not isinstance(question, str) or not question.strip():
            return _error(E.TASK_INPUT_INVALID, "question must be a non-empty string")
        if len(question) > M.MAX_QUESTION_CHARS:
            return _error(E.TASK_INPUT_INVALID,
                          f"question exceeds {M.MAX_QUESTION_CHARS} characters")
        if not current_run_id or not baseline_run_id:
            return _error(E.TASK_INPUT_INVALID, "both run ids are required")

        # run access 是 create 的必要條件：不成立就**不產生可授權的草案**。
        access = self._deps.validate_runs(
            current_run_id=current_run_id, baseline_run_id=baseline_run_id)
        decision = getattr(access, "decision", "UNVERIFIABLE")
        if decision != "ALLOWED":
            code = (E.TASK_RUN_ACCESS_UNVERIFIABLE if decision == "UNVERIFIABLE"
                    else E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE)
            return _error(code, getattr(access, "message", "run access denied"),
                          run_access=access.to_json_dict())

        snapshot_id, capabilities = self._deps.catalog_provider()
        template_steps = self._deps.build_steps(
            current_run_id=current_run_id, baseline_run_id=baseline_run_id,
            question=question)
        try:
            steps = tuple(
                self._build_step_spec(ts, snapshot_id, capabilities)
                for ts in template_steps
            )
        except _StepBuildError as exc:
            return _error(E.TASK_REVALIDATION_FAILED, str(exc))

        scope = M.TaskScope(
            member_id=context.member_id, project_id=context.project_id,
            origin_session_id=context.session_id,
            workspace=getattr(context, "workspace", "") or "",
            # analysis task 沒有 Profile 可推導；未宣告的 session 就如實
            # 記成 unset（值仍是 daemon 補的 UNDECLARED_PROJECT_ID）。
            project_source=("unset" if not getattr(context, "project_declared", True)
                            else "session"),
        )
        access_binding = None
        if self._deps.access_binding_provider is not None:
            access_binding = self._deps.access_binding_provider(
                current_run_id=current_run_id, baseline_run_id=baseline_run_id)
        task = M.build_task_snapshot(
            task_id=M.new_task_id(), daemon_instance_id=self._deps.daemon_instance_id,
            template_id=self._deps.template_id, scope=scope,
            current_run_id=current_run_id, baseline_run_id=baseline_run_id,
            question=question, steps=steps, access_binding=access_binding,
        )
        live = _LiveTask(task=task,
                         steps={s.step_id: _StepState(spec=s) for s in steps})
        try:
            self._writer.append_snapshot(task, sequence=_next_sequence(live))
        except TaskEventWriteError as exc:
            return _error(E.TASK_EVIDENCE_INVALID,
                          f"the task snapshot was not persisted: {exc}")

        with self._tasks_lock:
            self._tasks[task.task_id] = live
        view = self._view(live, run_access=access)
        return {"status": "ok", "task": view.to_json_dict()}

    def _build_step_spec(
        self, template_step: Any, snapshot_id: str,
        capabilities: dict[str, dict[str, Any]],
    ) -> M.TaskStepSpec:
        from terminux_engine.bounded_execution.binding import (
            compute_arguments_sha256, compute_capability_contract_sha256,
        )
        from terminux_engine.bounded_execution.eligibility import (
            check_capability_eligibility,
        )

        capability = capabilities.get(template_step.capability_id)
        if capability is None:
            raise _StepBuildError(
                f"capability {template_step.capability_id!r} is not registered")
        risk = str(capability.get("risk"))
        check = check_capability_eligibility(
            capability_id=template_step.capability_id, capability=capability,
            arguments=template_step.arguments, expected_risk=risk,
        )
        if not check.eligible:
            raise _StepBuildError(check.denial_code or "capability is not eligible")
        return M.TaskStepSpec(
            step_id=template_step.step_id, capability_id=template_step.capability_id,
            arguments=dict(template_step.arguments),
            arguments_sha256=compute_arguments_sha256(template_step.arguments),
            capability_contract_sha256=compute_capability_contract_sha256(capability),
            catalog_snapshot_id=snapshot_id, risk=risk,
            approval_required=bool(capability.get("approval_required")),
            manifest_timeout_sec=capability.get("timeout_sec"),
        )

    # ── start ────────────────────────────────────────────────

    def start(self, task_id: str, task_digest: str) -> dict[str, Any]:
        context = self._deps.request_context()
        live = self._lookup(task_id, context)
        if live is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, "no such task in this scope")

        with live.lock:
            denial = self._start_precheck_locked(live, task_digest, context)
            if denial is not None:
                return denial

            t0_ms, t0_mono = now_ms(), monotonic_s()
            grant = M.TaskAuthorizationGrant(
                grant_id=M.new_grant_id(), task_id=live.task.task_id,
                task_digest=live.task.task_digest,
                daemon_instance_id=self._deps.daemon_instance_id,
                scope=live.task.scope,
                approved_by_session_id=context.session_id,
                approved_at_ms=t0_ms,
                expires_at_ms=t0_ms + M.AUTHORIZATION_TTL_SEC * 1000,
                expires_at_monotonic=t0_mono + M.AUTHORIZATION_TTL_SEC,
                deadline_at_ms=t0_ms + M.TASK_DEADLINE_SEC * 1000,
                deadline_at_monotonic=t0_mono + M.TASK_DEADLINE_SEC,
                body_llm_dispatches=M.BODY_LLM_DISPATCHES,
            )
            try:
                self._writer.append_grant(live.task, grant,
                                          sequence=_next_sequence(live))
            except TaskEventWriteError as exc:
                # 沒有任何 action 會被啟動，也不回 accepted=true。
                live.startable = False
                live.persistence_status = "failed"
                self._force_terminal_locked(live, "BLOCKED",
                                            f"authorization not persisted: {exc}")
                return _error(E.TASK_EVIDENCE_INVALID,
                              "the authorization was not persisted; the task did "
                              "not start")
            live.grant = grant
            live.startable = False
            # 核准後交給 scheduler。沒有名額就 QUEUED（不是 TASK_BUSY），
            # 排隊期間沒有任何 child、沒有 LLM dispatch。
            decision, reason = self._scheduler.admit(
                live.task.task_id, writable_roots=(), approved_at_ms=t0_ms,
                on_dispatch=lambda: self._dispatch_analysis(live))
            if decision == "QUEUED":
                live.status = "QUEUED"
                live.queue_reason = reason
                live.queued_at_ms = t0_ms
                return {"status": "ok", "accepted": True, "queued": True,
                        "task": self._view(live).to_json_dict()}
        return self._dispatch_analysis(live)

    def _dispatch_analysis(self, live: _LiveTask) -> dict[str, Any]:
        """名額到手後（立即或出隊）真正開始：寫 started、換 RUNNING、submit worker。
        排隊期間被取消的 task 不啟動任何 worker，名額直接還回去。"""
        release_only = False
        response: dict[str, Any] | None = None
        with live.lock:
            if live.stop or live.status in M.TERMINAL_TASK_STATUSES or live.grant is None:
                release_only = True
                response = {"status": "ok", "accepted": False,
                            "task": self._view(live).to_json_dict()}
            else:
                try:
                    self._writer.append_started(live.task, live.grant,
                                                sequence=_next_sequence(live))
                except TaskEventWriteError as exc:
                    live.persistence_status = "failed"
                    self._force_terminal_locked(live, "BLOCKED",
                                                f"start not persisted: {exc}")
                    release_only = True
                    response = _error(E.TASK_EVIDENCE_INVALID,
                                      "the start record was not persisted; the "
                                      "task did not start")
                else:
                    live.status = "RUNNING"
                    live.started_at_ms = now_ms()
                    live.holds_slot = True
                    live.queue_reason = None
        if release_only:
            self._scheduler.release(live.task.task_id)
            return response or {"status": "ok", "accepted": False}
        # worker submit 在 task lock 外；失敗一樣不得回 accepted=true。
        try:
            self._deps.submit(lambda: self._run_task(live))
        except Exception as exc:  # noqa: BLE001
            with live.lock:
                self._force_terminal_locked(live, "BLOCKED",
                                            f"worker submit failed: {exc!r}")
            self._release_slot(live, True)
            return _error(E.TASK_WORKER_UNCONFIRMED,
                          "the analysis worker could not be started")
        return {"status": "ok", "accepted": True,
                "task": self._view(live).to_json_dict()}

    def _start_precheck_locked(
        self, live: _LiveTask, task_digest: str, context: Any,
    ) -> dict[str, Any] | None:
        if live.status != "DRAFT" or not live.startable:
            code = (E.TASK_ALREADY_STARTED if live.status == "RUNNING"
                    else E.TASK_ALREADY_STARTED)
            return _error(code, f"the task is already {live.status}",
                          task_id=live.task.task_id, task_status=live.status)
        if live.task.scope.origin_session_id != context.session_id:
            return _error(E.TASK_ORIGIN_SESSION_REQUIRED,
                          "only the session that created the task can start it")
        if live.task.task_digest != task_digest or not live.task.digest_matches():
            return _error(E.TASK_DIGEST_MISMATCH, "the task digest does not match")
        if live.task.daemon_instance_id != self._deps.daemon_instance_id:
            return _error(E.TASK_INSTANCE_EXPIRED,
                          "the task belongs to a previous daemon instance")
        # 草案**不因時間經過失效**。等待期間不消耗任何
        # 執行期限（grant／deadline 於下方核准當下才起算）。

        snapshot_id, capabilities = self._deps.catalog_provider()
        for spec in live.task.steps:
            capability = capabilities.get(spec.capability_id)
            if capability is None or snapshot_id != spec.catalog_snapshot_id:
                live.status = "BLOCKED"
                live.startable = False
                return _error(E.TASK_REVALIDATION_FAILED,
                              "the capability catalog changed; create a new task")

        access = self._deps.validate_runs(
            current_run_id=live.task.current_run_id,
            baseline_run_id=live.task.baseline_run_id)
        if getattr(access, "decision", "UNVERIFIABLE") != "ALLOWED":
            live.status = "BLOCKED"
            live.startable = False
            return _error(E.TASK_RUN_ACCESS_UNVERIFIABLE,
                          getattr(access, "message", "run access is not verifiable"),
                          run_access=access.to_json_dict())

        # 分析執行依賴不可用時，在 grant／worker 接受**之前**拒絕。
        # DRAFT 保留，無新 grant、intent、claim、名額占用或 LLM dispatch。
        if self._deps.provider_readiness is not None:
            ready, reason = self._deps.provider_readiness()
            if not ready:
                return _error(E.TASK_PROVIDER_UNAVAILABLE,
                              reason or "the analysis provider is not available")

        return None

    # ── worker（背景執行，不持任何鎖）────────────────────────────────

    def _task_context(self, live: _LiveTask) -> Any:
        """worker thread 用的 request context。

        完全由 **create 當下已通過 context boundary** 的 task scope 推導，
        caller 不能提供任何一欄。worker 跑在自己的 thread 上，request-scoped
        ContextVar 不會自動繼承，因此必須明確設定（見 `_run_task`）。
        """
        from terminux_engine.ipc.request_context import CanonicalRequestContext

        return CanonicalRequestContext(
            session_id=live.task.scope.origin_session_id,
            member_id=live.task.scope.member_id,
            project_id=live.task.scope.project_id,
            project_source=("profile" if live.task.scope.project_source == "profile"
                            else "session"),
        )

    def _run_task(self, live: _LiveTask) -> None:
        from terminux_engine.ipc.request_context import (
            reset_current_request_context, set_current_request_context,
        )

        from terminux_engine.bounded_execution.analysis_tasks.budget import (
            BUDGET_STOPPED, TaskBodyBudget, reset_current_task_budget,
            set_current_task_budget,
        )

        def _should_stop() -> str | None:
            """每次 LLM dispatch 前同時檢查 cancel/stop 與 grant deadline。"""
            if live.stop:
                return BUDGET_STOPPED
            grant = live.grant
            if grant is not None and monotonic_s() >= grant.deadline_at_monotonic:
                return BUDGET_STOPPED
            return None

        live.budget = TaskBodyBudget(M.BODY_LLM_DISPATCHES, should_stop=_should_stop)
        token = set_current_request_context(self._task_context(live))
        budget_token = set_current_task_budget(live.budget)
        try:
            self._run_task_locked_context(live)
        finally:
            # 完成或失敗一律 reset：下一個 task 不得沿用前一個的
            # grant／budget／scope。
            reset_current_task_budget(budget_token)
            reset_current_request_context(token)

    def _run_task_locked_context(self, live: _LiveTask) -> None:
        task_context = self._task_context(live)
        step_deps = X.StepExecutionDeps(
            writer=self._writer, proposal_registry=self._deps.proposal_registry,
            create_task_proposal=self._deps.create_task_proposal,
            dispatch_deps_factory=self._deps.dispatch_deps_factory,
            catalog_provider=self._deps.catalog_provider,
            summarize=self._deps.summarize,
            validate_run_access=lambda: self._deps.validate_runs(
                current_run_id=live.task.current_run_id,
                baseline_run_id=live.task.baseline_run_id),
            activation_gate=self._deps.activation_gate,
            # worker 用 task scope，不是「現在剛好是誰在打 IPC」。
            request_context=lambda: task_context,
            access_binding_provider=(
                (lambda: self._deps.access_binding_provider(
                    current_run_id=live.task.current_run_id,
                    baseline_run_id=live.task.baseline_run_id))
                if self._deps.access_binding_provider is not None else None
            ),
            provider_readiness=self._deps.provider_readiness,
        )
        handle = _RuntimeHandle(live)
        can_continue = True
        worker_confirmed = True
        last: X.StepExecutionResult | None = None
        try:
            for spec in live.task.steps:
                grant = live.grant
                if grant is None:
                    break
                last = X.execute_task_step(
                    step_deps, handle, task=live.task, grant=grant, step=spec,
                    previous_can_continue=can_continue)
                worker_confirmed = worker_confirmed and last.worker_confirmed
                can_continue = last.can_continue
                if not can_continue:
                    break
        except Exception as exc:  # noqa: BLE001 - worker 不得把例外吞掉又假裝成功
            _LOG.warning("analysis task worker failed: %r", exc)
            with live.lock:
                self._force_terminal_locked(live, "OUTCOME_UNCONFIRMED", repr(exc))
            worker_confirmed = False
        else:
            with live.lock:
                self._finalize_locked(live, last, can_continue)
        finally:
            if worker_confirmed:
                self._release_slot(live, True)
            else:
                # 沒有可用的完成觀測就保守地**不釋放**名額；後面排隊的
                # task 看得到原因是 `worker_unconfirmed:<task_id>`，不是忙碌。
                live.unconfirmed_work = True
                self.mark_worker_unconfirmed(live)
                _LOG.warning("analysis task %s left the worker slot unavailable",
                             live.task.task_id)

    def _finalize_locked(
        self, live: _LiveTask, last: X.StepExecutionResult | None, can_continue: bool,
    ) -> None:
        statuses = [s.status for s in live.steps.values()]
        for state in live.steps.values():
            if state.status == "PENDING":
                state.status = "SKIPPED"
                state.reason = state.reason or "the task stopped before this step ran"

        if last is None:
            status, reason = "BLOCKED", "no step was executed"
        elif any(s == "OUTCOME_UNCONFIRMED" for s in statuses):
            status, reason = "OUTCOME_UNCONFIRMED", "an action outcome is unconfirmed"
        elif live.stop:
            status, reason = "CANCELLED", live.terminal_reason or "cancelled"
        elif can_continue and all(
                s.status == "SUCCEEDED" for s in live.steps.values()):
            status, reason = "COMPLETED", None
        elif last.step_status in ("FAILED", "DENIED"):
            status, reason = "FAILED", last.error_code or last.reason
        else:
            status, reason = "BLOCKED", last.error_code or last.reason
        self._force_terminal_locked(live, status, reason)

    def _force_terminal_locked(
        self, live: _LiveTask, status: str, reason: str | None,
    ) -> None:
        live.status = status
        live.terminal_reason = reason
        try:
            self._writer.append_terminal(
                live.task, sequence=_next_sequence(live), status=status, reason=reason,
                step_references=[
                    {"step_id": s.spec.step_id, "status": s.status,
                     "result_event_id": s.result_event_id,
                     "result_sha256": s.result_sha256}
                    for s in live.steps.values()
                ],
                budget_consumed=self._budget_view(live),
            )
        except TaskEventWriteError as exc:
            _LOG.warning("analysis task terminal event not persisted: %r", exc)
            live.persistence_status = "failed"

    def _release_slot(self, live: _LiveTask, acquired: bool) -> None:
        """釋放 scheduler 名額；它可能在鎖外啟動下一個 queued task（不得持任何
        task lock 呼叫）。"""
        if not acquired:
            return
        live.holds_slot = False
        self._scheduler.release(live.task.task_id)

    def mark_worker_unconfirmed(self, live: Any) -> None:
        """結果無法確認就保守地**不釋放**名額，只在 scheduler 記原因。"""
        self._scheduler.mark_unconfirmed(live.task.task_id,
                                         "action outcome could not be confirmed")

    # ── flow task ───────────────────────────────────────────

    def _limited_activation_denial(
        self, context: Any, *, profile_id: str | None = None,
        profile: Any = None,
    ) -> dict[str, Any] | None:
        """flow 路徑的**同一個** limited activation gate。

        create、start 與每次 launch 前都走這裡。**未注入 gate ⇒ 拒絕**——
        沒有寬鬆預設。`get`／`cancel` 刻意不呼叫它：policy 撤除、漂移或
        kill switch 都不得讓使用者失去讀回與停止的能力。
        """
        gate = self._deps.flow_activation_gate
        if gate is None:
            return _error(
                E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                "limited production execution is not activated on this daemon")
        denial = gate.check(member_id=context.member_id,
                            project_id=context.project_id, profile=profile)
        if denial is None:
            return None
        code, message = denial
        details: dict[str, Any] = {"activation_denial_code": code}
        if profile_id is not None:
            details["profile_id"] = profile_id
        return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, message, **details)

    def _proposal_intake_decision(self, profile: Any) -> dict[str, Any]:
        """Phase A：Profile × activation policy 的手動 proposal intake 交集。

        gate 未注入 ⇒ 沒有 policy 可以宣告 intake ⇒ 不收（fail closed）。
        """
        from terminux_engine.bounded_execution.proposal_intake import (
            NOT_DECLARED_BY_POLICY, ProposalIntakeDecision,
        )

        gate = self._deps.flow_activation_gate
        decide = getattr(gate, "proposal_intake_decision", None)
        if gate is None or decide is None:
            # 看不懂 manual intake 的 gate 不可能核准它。
            return ProposalIntakeDecision.denied(
                NOT_DECLARED_BY_POLICY).to_json_dict()
        return decide(profile).to_json_dict()

    # ── 有效 project ─────────────────────────

    @staticmethod
    def _effective_task_context(context: Any, profile: Any) -> Any:
        """把 session context 換成**這次選用／建 task 的有效 context**。

        | session       | Profile        | 結果                                        |
        |---------------|----------------|---------------------------------------------|
        | 未宣告        | project_id=P   | 有效 project=P，project_source="profile"    |
        | 明確宣告 P    | project_id=P   | 接受，project_source="session"              |
        | 明確宣告 P    | project_id=Q   | 拒絕（不自動覆寫 P）                        |
        | 任一          | member 不符    | 拒絕（不從 Profile 改寫登入者身分）         |

        回傳 `CanonicalRequestContext` 或 error dict。**不改** session 本身：
        同一個未宣告 project 的 session 之後選 Q 的 Profile 仍是獨立判定。
        """
        from dataclasses import replace

        if profile.member_id != context.member_id:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "the profile's member_id does not match this session; a "
                          "profile can only be used by its own member")
        if getattr(context, "project_declared", True):
            if profile.project_id != context.project_id:
                return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                              "the profile's project_id does not match the project this "
                              "session declared; a profile can only be used by its own "
                              "member and project (the session's project is not overridden)")
            return context
        return replace(context, project_id=profile.project_id, project_source="profile")

    @staticmethod
    def _task_scoped_context(context: Any, task: Any) -> Any:
        """start／gate 用：以 task **固定**的 project 取代 caller 的 project；
        session_id／member_id 不變（origin session 檢查仍用原值）。"""
        from dataclasses import replace

        source = "profile" if task.scope.project_source == "profile" else "session"
        return replace(context, project_id=task.scope.project_id, project_source=source)

    # ── task-scoped profile 來源 ────────────────────────

    def _resolve_profile_source(
        self, context: Any, *, profile_path: str | None, profile_inline: Any,
        profile_id: str | None,
    ) -> tuple[Any, Any, dict[str, Any]] | dict[str, Any]:
        """三種來源擇一：`profile_path`（user 可讀的檔案）、`profile_inline`
        （符合共同格式的 JSON）、`profile_id`（本 session 已用 `/profile` 選用
        的一份）。同時給或都不給 ⇒ 結構化錯誤，不猜優先順序。"""
        given = [name for name, value in (("profile_path", profile_path),
                                          ("profile_inline", profile_inline),
                                          ("profile_id", profile_id))
                 if value not in (None, "")]
        if len(given) != 1:
            return _error(E.TASK_INPUT_INVALID,
                          "provide exactly one profile source: profile_path, "
                          "profile_inline or profile_id (a profile selected in "
                          f"this session); got {given or 'none'}")
        try:
            if profile_path is not None:
                return FP.load_profile_document_from_path(profile_path)
            if profile_inline is not None:
                return FP.build_profile_document_from_inline(profile_inline)
        except FP.FlowProfileError as exc:
            return _error(E.TASK_INPUT_INVALID, f"{exc.code}: {exc.message}",
                          profile_error_code=exc.code)
        selected = self._session_profiles.get(context.session_id, {})
        entry = selected.get(str(profile_id))
        if entry is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          f"no profile {profile_id!r} is selected in this session; "
                          "select one with /profile <absolute path> or pass "
                          "profile_path/profile_inline")
        return entry

    def select_profile(self, *, profile_path: str | None = None,
                       profile_inline: Any = None) -> dict[str, Any]:
        """把一份 user 的 block／project Profile 選進**本 session**。

        只做載入與驗證（形狀、scope），不執行任何東西；之後自然語言（planner）
        與 `/task-create <profile_id>` 都對應到這裡選用的清單。不是 allowlist：
        `create_flow_task` 隨時可直接給 `profile_path`／`profile_inline`。
        """
        context = self._deps.request_context()
        resolved = self._resolve_profile_source(
            context, profile_path=profile_path, profile_inline=profile_inline,
            profile_id=None)
        if isinstance(resolved, dict):
            return resolved
        profile, source, accepted = resolved
        effective = self._effective_task_context(context, profile)
        if isinstance(effective, dict):
            return effective
        envelope = self._verify_setup_margin_envelope(profile)
        if envelope is not None:
            return envelope
        with self._tasks_lock:
            bucket = self._session_profiles.setdefault(context.session_id, {})
            # 未宣告 project 的 session 可能先後選到不同 project 的
            # 同名 profile_id。索引仍以 profile_id 為 key，因此對歧義**明確報錯**
            # （不靜默覆寫、不選錯）；要用另一個就直接給 profile_path／inline。
            existing = bucket.get(profile.profile_id)
            if existing is not None and existing[0].project_id != profile.project_id:
                return _error(E.TASK_INPUT_INVALID,
                              f"profile_id {profile.profile_id!r} is already selected "
                              f"in this session for a different project; pass "
                              "profile_path or profile_inline to use this one, or "
                              "select it in a new session")
            bucket.pop(profile.profile_id, None)
            bucket[profile.profile_id] = (profile, source, accepted)
            limit = max(1, int(self._deps.max_selected_profiles_per_session))
            while len(bucket) > limit:
                bucket.pop(next(iter(bucket)))
        summary = self._profile_summary(profile, source)
        summary["project_id"] = effective.project_id
        summary["project_source"] = effective.project_source
        return {"status": "ok", "profile": summary}

    def list_profiles(self) -> dict[str, Any]:
        """本 session 已選用的 profile 清單（給 planner、`/profiles` 與 GUI）。"""
        context = self._deps.request_context()
        with self._tasks_lock:
            entries = list(self._session_profiles.get(context.session_id, {}).values())
        return {"status": "ok",
                "profiles": [self._profile_summary(p, src) for p, src, _ in entries]}

    def eap_profile_scope(self, profile_id: str) -> dict[str, Any]:
        """domain proposal 的唯讀 Profile scope 解析；不建立 task 或授權。"""
        context = self._deps.request_context()
        entry = self._session_profiles.get(context.session_id, {}).get(profile_id)
        if entry is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "no such selected profile in this session")
        profile, source, _ = entry
        effective = self._effective_task_context(context, profile)
        if isinstance(effective, dict):
            return effective
        return {"status": "ok", "project_id": effective.project_id,
                "project_source": effective.project_source,
                "profile_source": source.to_json_dict(),
                "environment": {"profile_id": profile.profile_id,
                                "design_id": profile.design_id,
                                "platform_id": profile.platform_id,
                                # Engineering Work work 授權預覽需要揭露 Profile 已固定的
                                # input／write／evidence 邊界；仍不包含 command、
                                # credential、grant 或 host binding。
                                "block": profile.design_id,
                                "source_root": str(profile.source_root),
                                "input_snapshot_root": str(profile.input_snapshot_root),
                                "input_manifest_path": str(profile.input_manifest_path),
                                "input_manifest_digest": profile.input_manifest_sha256,
                                "execution_root": str(profile.execution_root),
                                "dqe_db_path": str(profile.dqe_db_path),
                                "artifact_root": str(profile.artifact_root),
                                "tool_identity": getattr(
                                    profile, "tool_identity_ref", profile.docker_image_id),
                                "design_config_digest": profile.profile_digest,
                                "constraints_digest": profile.profile_digest},
                # Phase A：手動 proposal 的收件判定（兩側交集）。這裡不建立
                # 授權或 grant——只告訴 Engineering Work「可不可以收下這筆 proposal」。
                "proposal_intake": self._proposal_intake_decision(profile)}

    def prepare_eap_work_input(
        self, *, profile_id: str, work_id: str, source_root: str,
    ) -> dict[str, Any]:
        """由已選工程環境建立 work 專用 snapshot／manifest。

        caller 只選 source 與環境；manifest 路徑及 digest 由平台產生。來源
        必須等於 Profile 已限制的唯讀 root，不能藉 EAP work 擴大讀取範圍。
        """
        context = self._deps.request_context()
        selected = self._session_profiles.get(context.session_id, {})
        entry = selected.get(profile_id)
        if entry is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "the selected engineering environment is unavailable")
        profile, _source, accepted = entry
        effective = self._effective_task_context(context, profile)
        if isinstance(effective, dict):
            return effective
        requested = Path(source_root).resolve()
        allowed = Path(profile.source_root).resolve()
        if requested != allowed:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "source_root is outside the selected Profile boundary")
        derived_id = f"{profile_id}--{work_id}"
        existing = selected.get(derived_id)
        if existing is not None:
            prepared = existing[0]
            return {"status": "ok", "profile_id": derived_id,
                    "snapshot_root": prepared.input_snapshot_root,
                    "manifest_path": prepared.input_manifest_path,
                    "manifest_digest": prepared.input_manifest_sha256,
                    "input_digest": prepared.input_manifest_sha256,
                    "idempotent": True}
        exec_root = Path(profile.execution_root).resolve()
        snapshot_base = exec_root.parent / "snapshots" if exec_root.parent != exec_root else exec_root.with_name(exec_root.name + "_snapshots")
        target_root = snapshot_base / work_id / "input_snapshot"
        manifest_path = snapshot_base / work_id / "input_manifest.json"
        try:
            from terminux_engine.packages.orfs_run_intelligence.flow_inputs import (
                InputPreparationError, load_manifest, prepare_source_snapshot,
                verify_inventory,
            )
            # RC-4：setup-margin envelope 的 work input 必須與 Profile 已固定的
            # manifest 完全相同；不能在 work 建立當下取「source_root 此刻的內容」。
            pinned = (load_manifest(profile.input_manifest_path,
                                    expected_sha256=profile.input_manifest_sha256)
                      if SME.is_setup_margin_recipe(profile.repair_recipe) else None)
            if target_root.is_dir() and manifest_path.is_file():
                manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                manifest = load_manifest(
                    manifest_path, expected_sha256=manifest_digest)
                inventory = verify_inventory(manifest, target_root)
                if (not inventory.ok
                        or (pinned is not None and (
                            manifest.tree_hash != pinned.tree_hash
                            or manifest.source_revision != pinned.source_revision
                            or manifest.source_dirty != pinned.source_dirty))
                        or Path(manifest.source_root).resolve() != requested
                        or Path(manifest.snapshot_root).resolve() != target_root):
                    raise InputPreparationError(
                        "INPUT_INVENTORY_DRIFT",
                        "saved work input cannot be safely reattached")
                prepared_root = target_root
                prepared_manifest_path = manifest_path
                prepared_manifest_digest = manifest_digest
                prepared_manifest = manifest
                copy_method = "reattached_verified"
            else:
                snapshot = prepare_source_snapshot(
                    source_root=requested, snapshot_root=target_root,
                    manifest_path=manifest_path, design_config=profile.design_config,
                    design_id=profile.design_id, platform_id=profile.platform_id,
                    docker_image_id=profile.docker_image_id, pinned_manifest=pinned)
                prepared_root = snapshot.snapshot_root
                prepared_manifest_path = snapshot.manifest_path
                prepared_manifest_digest = snapshot.manifest_sha256
                prepared_manifest = snapshot.manifest
                copy_method = snapshot.copy_method
        except (InputPreparationError, OSError) as exc:
            return _error(E.TASK_REVALIDATION_FAILED,
                          f"{getattr(exc, 'code', type(exc).__name__)}: "
                          f"{getattr(exc, 'message', str(exc))}")
        prepared = profile.with_prepared_input(
            profile_id=derived_id, source_root=str(requested),
            input_snapshot_root=str(prepared_root),
            input_manifest_path=str(prepared_manifest_path),
            input_manifest_sha256=prepared_manifest_digest)
        source = FP.ProfileSource(
            kind="eap_work_prepared", logical_path=str(requested),
            resolved_path=str(requested), is_symlink=False,
            raw_sha256=prepared_manifest_digest,
            size_bytes=prepared_manifest_path.stat().st_size,
            profile_digest=prepared.profile_digest)
        envelope = self._verify_setup_margin_envelope(prepared)
        if envelope is not None:
            return envelope
        prepared_accepted = dict(accepted)
        prepared_accepted["profile_id"] = derived_id
        prepared_accepted["source_root"] = str(requested)
        prepared_accepted["input_snapshot_root"] = str(prepared_root)
        prepared_accepted["input_manifest_path"] = str(prepared_manifest_path)
        prepared_accepted["input_manifest_sha256"] = prepared_manifest_digest
        with self._tasks_lock:
            selected[derived_id] = (prepared, source, prepared_accepted)
        return {"status": "ok", "profile_id": derived_id,
                "snapshot_root": str(prepared_root),
                "manifest_path": str(prepared_manifest_path),
                "manifest_digest": prepared_manifest_digest,
                "input_digest": prepared_manifest_digest,
                "tree_hash": prepared_manifest.tree_hash,
                "copy_method": copy_method,
                "idempotent": copy_method == "reattached_verified"}

    @staticmethod
    def _verify_setup_margin_envelope(profile: Any) -> dict[str, Any] | None:
        """RC-4：setup-margin Profile 的 helper／suffix／工具身分逐一對實際 bytes。

        成功回 None；任何缺少或不符都回錯誤，不選用、不建立 work input。
        """
        recipe = getattr(profile, "repair_recipe", None)
        if not SME.is_setup_margin_recipe(recipe):
            return None
        from terminux_engine.packages.orfs_run_intelligence.flow_inputs import (
            InputPreparationError, load_manifest,
        )
        native_tool = None
        if getattr(profile, "is_native", False):
            # host-native：proof binary 由 Profile 的 FORMAL_YOSYS pin 授權，
            # qualified identity 依 native 契約核對（沒有 image）。
            pin = (getattr(profile.native_tool_binding, "tools", None) or {}).get(
                "FORMAL_YOSYS")
            if pin is None:
                return _error(E.TASK_REVALIDATION_FAILED,
                              f"{SME.ENVELOPE_INVALID}: a host-native setup-margin "
                              "Profile must pin native_tool_binding.tools.FORMAL_YOSYS")
            native_tool = pin.to_json_dict()
        try:
            manifest = load_manifest(profile.input_manifest_path,
                                     expected_sha256=profile.input_manifest_sha256)
            SME.verify_setup_margin_envelope(
                recipe, manifest=manifest, snapshot_root=profile.input_snapshot_root,
                parameter_name=profile.parameter.name,
                baseline_value=profile.parameter.baseline, native_tool=native_tool)
        except (SME.SetupMarginEnvelopeError, InputPreparationError, OSError) as exc:
            return _error(E.TASK_REVALIDATION_FAILED,
                          f"{getattr(exc, 'code', type(exc).__name__)}: "
                          f"{getattr(exc, 'message', str(exc))}")
        return None

    def _profile_summary(self, profile: Any, source: Any) -> dict[str, Any]:
        summary = dict(profile.public_summary())
        summary["profile_source"] = source.to_json_dict() if source is not None else None
        summary["execution_plan"] = profile.execution_plan()
        if self._deps.describe_profile is not None:
            try:
                summary["description"] = self._deps.describe_profile(profile)
            except Exception as exc:  # noqa: BLE001 - 描述失敗不影響選用
                summary["description"] = f"(description unavailable: {exc!r})"
        return summary

    def profile_for_profile_id(self, profile_id: str) -> Any:
        """本 session 已選用的 Profile。找不到就回 `None`，不猜。"""
        context = self._deps.request_context()
        resolved = self._resolve_profile_source(
            context, profile_path=None, profile_inline=None, profile_id=profile_id)
        if isinstance(resolved, dict):
            return None
        return resolved[0]

    def profile_for_task(self, task_id: str) -> Any:
        """task-owned immutable snapshot。live task 不在就是 None。"""
        with self._tasks_lock:
            live = self._flow_tasks.get(task_id)
        return live.profile if live is not None else None

    def list_tasks(self) -> dict[str, Any]:
        """本 daemon 內 caller scope 的所有 live task 與 scheduler 狀態。"""
        context = self._deps.request_context()
        rows: list[dict[str, Any]] = []
        with self._tasks_lock:
            flows = list(self._flow_tasks.values())
            tasks = list(self._tasks.values())
        declared = getattr(context, "project_declared", True)
        for live in flows:
            if not (live.task.scope.accessible_by(context) if declared
                    else self._readable_by_undeclared(live.task.scope, context)):
                continue
            with live.lock:
                rows.append({
                    "task_kind": "flow_task", "task_id": live.task.task_id,
                    "status": live.status, "profile_id": live.task.profile_id,
                    "design_id": live.task.design_id,
                    "platform_id": live.task.platform_id,
                    "created_at_ms": live.task.created_at_ms,
                    "current_operation": live.current_operation,
                    "queue": self._queue_view(live),
                })
        for live in tasks:
            if not (live.task.scope.accessible_by(context) if declared
                    else self._readable_by_undeclared(live.task.scope, context)):
                continue
            with live.lock:
                rows.append({
                    "task_kind": "analysis_task", "task_id": live.task.task_id,
                    "status": live.status, "profile_id": None, "design_id": None,
                    "platform_id": None, "created_at_ms": live.task.created_at_ms,
                    "queue": self._queue_view(live),
                })
        rows.sort(key=lambda r: (r["created_at_ms"], r["task_id"]))
        return {"status": "ok", "tasks": rows,
                "scheduler": self._scheduler.snapshot().to_json_dict()}

    def _queue_view(self, live: Any) -> dict[str, Any] | None:
        if live.status != "QUEUED":
            return None
        task_id = live.task.task_id
        return {
            "position": self._scheduler.queue_position(task_id),
            "reason": self._scheduler.queue_reason(task_id) or live.queue_reason,
            "queued_at_ms": live.queued_at_ms,
            "limits": self._scheduler.limits.to_json_dict(),
        }

    def create_flow_task(
        self, *, question: str, profile_path: str | None = None,
        profile_inline: Any = None, profile_id: str | None = None,
    ) -> dict[str, Any]:
        return self._create_flow_task(
            question=question, profile_path=profile_path,
            profile_inline=profile_inline, profile_id=profile_id)

    def create_eap_flow_task(
        self, *, question: str, profile_id: str, proposal: dict[str, Any],
    ) -> dict[str, Any]:
        """以 server 已讀回的 EAP proposal 建立可檢視、尚未授權的 FlowTask。"""
        if not isinstance(profile_id, str) or not profile_id:
            return _error(E.TASK_INPUT_INVALID, "profile_id must be a non-empty string")
        if not isinstance(proposal, dict):
            return _error(E.TASK_INPUT_INVALID, "the EAP proposal must be an object")
        source = proposal.get("source")
        if not isinstance(source, dict) or not all(
                isinstance(source.get(key), str) and source[key]
                for key in ("draft_id", "proposal_digest")):
            return _error(E.TASK_INPUT_INVALID,
                          "the EAP proposal source identity is incomplete")
        context = self._deps.request_context()
        replacement = proposal.get("replacement")
        replacement_key = (
            str(replacement.get("replaces_task_id", "")) + ":" +
            str(replacement.get("replaces_task_digest", ""))
            if isinstance(replacement, dict) else ""
        )
        key = (source["draft_id"], source["proposal_digest"], profile_id,
               context.member_id, context.project_id, replacement_key)
        with self._tasks_lock:
            existing = self._eap_flow_tasks.get(key)
        if existing is not None:
            try:
                live = self.get_or_hydrate_flow_task(existing, context)
            except Exception as exc:  # noqa: BLE001 - 不以 cache 補寫 durable task
                return _error(getattr(exc, "code", E.TASK_EVIDENCE_INVALID), str(exc))
            if live is not None:
                return {"status": "ok", "idempotent": True,
                        "task": self._flow_view(live).to_json_dict()}
        return self._create_flow_task(
            question=question, profile_id=profile_id, eap_proposal=proposal,
            eap_idempotency_key=key)

    def create_package_flow_task(
        self, *, question: str, profile_id: str, proposal: dict[str, Any],
    ) -> dict[str, Any]:
        """WorkRuntime 內部入口；以 Package 編譯的工具計畫建正式 DRAFT。"""
        source = proposal.get("source") if isinstance(proposal, dict) else None
        package = proposal.get("package") if isinstance(proposal, dict) else None
        if (not isinstance(source, dict) or not isinstance(package, dict)
                or not isinstance(proposal.get("package_tool"), dict)
                or not isinstance(source.get("draft_id"), str)
                or not isinstance(source.get("proposal_digest"), str)
                or not isinstance(package.get("package_id"), str)):
            return _error(E.TASK_INPUT_INVALID, "Package tool proposal is incomplete")
        from terminux_engine.bounded_execution.analysis_tasks.package_flow import (
            flow_task_create_identity,
        )

        context = self._deps.request_context()
        key = ("package-tool", source["draft_id"], source["proposal_digest"],
               package["package_id"], profile_id, context.member_id,
               context.project_id)
        # 這一次 create request 的 durable 身分。in-memory index 只是快取；
        # 權威來源是 EventStore 裡已持久化的 task-created event。
        resolved = self._resolve_profile_source(
            context, profile_path=None, profile_inline=None, profile_id=profile_id)
        if isinstance(resolved, dict):
            return resolved
        profile, _source_identity, _accepted = resolved
        effective = self._effective_task_context(context, profile)
        if isinstance(effective, dict):
            return effective
        identity = flow_task_create_identity(
            proposal, profile_id=profile.profile_id,
            profile_digest=profile.profile_digest, member_id=effective.member_id,
            project_id=effective.project_id)
        replayed = self._replay_package_flow_task(identity, effective)
        if replayed is not None:
            return replayed
        return self._create_flow_task(
            question=question, profile_id=profile_id,
            eap_package_proposal=proposal, eap_idempotency_key=key,
            create_identity=identity)

    def _stored_create_identity(self, identity_key_digest: str, context: Any
                                ) -> tuple[str, dict[str, Any]] | None:
        """由 durable EventStore 找回同一個 create identity 的 task-created 事件。

        歷史 task 沒有這個欄位；那些 task 一律不被比對，也不回寫。
        """
        for event in self._deps.event_store.query(
                user_id=context.member_id, event_type=F.EVENT_SNAPSHOT, order="asc"):
            payload = event.payload or {}
            task = payload.get("task")
            if not isinstance(task, dict):
                continue
            stored = (task.get("execution_plan") or {}).get("create_identity")
            if isinstance(stored, dict) and stored.get("key_digest") == identity_key_digest:
                return str(payload.get("task_id")), stored
        return None

    def _replay_package_flow_task(self, identity: dict[str, Any], context: Any,
                                  ) -> dict[str, Any] | None:
        """同一個 create identity 已經存在 ⇒ 回既有 task；內容不同 ⇒ fail closed。"""
        found = self._stored_create_identity(identity["key_digest"], context)
        if found is None:
            return None
        task_id, stored = found
        if stored.get("content_digest") != identity["content_digest"]:
            return _error(E.TASK_INPUT_INVALID,
                          "IDEMPOTENCY_INPUT_DRIFT: this create identity was already "
                          "used with different canonical inputs",
                          idempotency_key_digest=identity["key_digest"],
                          stored_content_digest=stored.get("content_digest"),
                          supplied_content_digest=identity["content_digest"],
                          task_id=task_id)
        try:
            live = self.get_or_hydrate_flow_task(task_id, context)
        except Exception as exc:  # noqa: BLE001 - evidence 不完整不得建立替代草案
            code = getattr(exc, "code", E.TASK_EVIDENCE_INVALID)
            return _error(code, getattr(exc, "message", str(exc)), task_id=task_id)
        if live is not None:
            return {"status": "ok", "idempotent": True,
                    "task": self._flow_view(live).to_json_dict()}
        replayed = self._get_flow_from_history(task_id, context, include_results=False)
        if isinstance(replayed, dict) and replayed.get("status") == "ok":
            return {**replayed, "idempotent": True}
        return {"status": "ok", "idempotent": True, "task": {"task_id": task_id}}

    def create_eap_baseline_task(
        self, *, question: str, profile_id: str, request: dict[str, Any],
    ) -> dict[str, Any]:
        """由已核准 Engineering Work work 建立單步 baseline task。

        ``request`` 只帶 work／parent authorization 身分；執行環境仍完全由
        已選 Profile 展開。相同 request 重送只會讀回同一份 DRAFT。
        """
        if not isinstance(profile_id, str) or not profile_id:
            return _error(E.TASK_INPUT_INVALID, "profile_id must be a non-empty string")
        if not isinstance(request, dict) or not all(
                isinstance(request.get(key), str) and request[key]
                for key in ("work_id", "request_id", "parent_authorization_id",
                            "parent_authorization_digest")):
            return _error(E.TASK_INPUT_INVALID,
                          "the baseline request source identity is incomplete")
        context = self._deps.request_context()
        key = ("baseline-only", request["work_id"], request["request_id"],
               profile_id, context.member_id, context.project_id)
        with self._tasks_lock:
            existing = self._eap_flow_tasks.get(key)
        if existing is not None:
            try:
                live = self.get_or_hydrate_flow_task(existing, context)
            except Exception as exc:  # noqa: BLE001 - 不以 cache 補寫 durable task
                return _error(getattr(exc, "code", E.TASK_EVIDENCE_INVALID), str(exc))
            if live is not None:
                return {"status": "ok", "idempotent": True,
                        "task": self._flow_view(live).to_json_dict()}
        return self._create_flow_task(
            question=question, profile_id=profile_id,
            eap_baseline_request=request, eap_idempotency_key=key)

    def _durable_baseline_import(
        self, *, task_id: str, task_digest: str, profile: Any, context: Any,
    ) -> dict[str, Any]:
        """terminal baseline task 的 durable 重建路徑。

        只在 live 查找失敗時才走到這裡，因此 active／未完成 task 的 instance
        gate 完全不受影響。來源位置由 Profile 釘住，不是 RPC 參數。
        """
        from terminux_engine.bounded_execution.analysis_tasks import (
            durable_baseline as DB,
        )

        if profile is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "the baseline task is not available in this scope")
        try:
            source = DB.DurableBaselineSource.from_profile(profile)
        except DB.DurableBaselineError as exc:
            # `details` 帶著 differing／source／target；丟掉它等於讓失敗現場
            # 無法診斷，只剩一句不指名欄位的 reason。
            return _error(E.TASK_EVIDENCE_INVALID, exc.reason,
                          **({"details": exc.details} if exc.details else {}))
        if source is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "the baseline task is not available in this scope")
        if (source.source_task_id != task_id
                or source.source_task_digest != task_digest):
            return _error(E.TASK_EVIDENCE_INVALID,
                          "the requested baseline is not the one pinned by the "
                          "Profile")
        policy = _baseline_reference_policy(profile)
        if not policy["required_for_import"]:
            return _error(E.TASK_EVIDENCE_INVALID,
                          "a method that does not produce a production baseline "
                          "reference has no durable import")
        try:
            rebuilt = DB.reconstruct_import(
                source, profile=profile,
                member_id=context.member_id, project_id=context.project_id,
                reference_policy_state=policy["state"],
                target_origin_session_id=getattr(context, "session_id", None),
                target_workspace=getattr(context, "workspace", "") or "",
                target_project_source=getattr(context, "project_source",
                                              "session"),
                work_id=getattr(context, "work_id", None))
        except DB.DurableBaselineError as exc:
            # `details` 帶著 differing／source／target；丟掉它等於讓失敗現場
            # 無法診斷，只剩一句不指名欄位的 reason。
            return _error(E.TASK_EVIDENCE_INVALID, exc.reason,
                          **({"details": exc.details} if exc.details else {}))
        except (OSError, ValueError) as exc:
            return _error(E.TASK_EVIDENCE_INVALID,
                          f"the durable baseline is unreadable: {exc}")
        return {"status": "ok", "baseline_import": rebuilt["baseline_import"],
                "durable_binding": rebuilt["durable_binding"]}

    def export_eap_baseline_import(
        self, *, task_id: str, task_digest: str, profile: Any = None,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        """匯出一份可被後續 Engineering Work candidate task 導入的 baseline。

        只接受同一個 daemon／member／project／session 中，已完成且證據
        持久化成功的 baseline-only task。匯出前會重驗 proof reference
        bytes/hash；通過只代表它可被本次 task digest 引用，不產生
        grant 或新的執行權。
        """
        context = self._deps.request_context()
        live = self._lookup_flow(task_id, context)
        if live is None:
            # live 查不到：可能是別的 daemon instance 產生的 **terminal**
            # baseline。改由 durable 紀錄重建，規則見 `durable_baseline`。
            if profile is None and profile_id:
                # Work 端只知道 profile_id；釘選在本 session 已選用的那一份
                # Profile 裡，由 server 自己解析，不由呼叫端提供內容。
                resolved = self._resolve_profile_source(
                    context, profile_path=None, profile_inline=None,
                    profile_id=profile_id)
                if not isinstance(resolved, dict):
                    profile = resolved[0]
            if profile is not None:
                # durable 重用的 target scope 必須與 live 路徑同樣由 Profile
                # 推導。session context 在構造上永遠產生不出
                # `project_source="profile"`：未宣告 project 得到
                # `("default", "unset")`，以 env 宣告得到 `(P, "session")`。
                # 直接拿 session context 當 target scope，會讓任何由 Profile
                # 宣告 project 的 baseline 都無法被重用。
                effective = self._effective_task_context(context, profile)
                if isinstance(effective, dict):
                    return effective
                context = effective
            return self._durable_baseline_import(
                task_id=task_id, task_digest=task_digest, profile=profile,
                context=context)
        with live.lock:
            task = live.task
            state = live.steps.get(F.STEP_RUN_BASELINE) or {}
            run_ref = live.runs.get(F.SLOT_BASELINE)
            reference = live.verification_reference
            # baseline 的功能驗證 reference 是否必須存在，由 method extension
            # 的宣告決定：宣告「本方法會產生」就必須有；宣告「不產生」的方法
            # 只有在 QUALIFICATION_ONLY fixture Profile 下才可以沒有，而且會
            # 如實標記成未建立 production proof（不得被當成 production import）。
            reference_policy = _baseline_reference_policy(live.profile)
            reference_required = reference_policy["required_for_import"]
            if (task.template_id != F.BASELINE_ONLY_TEMPLATE_ID
                    or task.task_digest != task_digest
                    or task.daemon_instance_id != self._deps.daemon_instance_id
                    or live.status != "COMPLETED"
                    or state.get("status") != "SUCCEEDED"
                    or live.persistence_status != "durable"
                    or run_ref is None
                    or (reference_required and not isinstance(reference, dict))):
                return _error(E.TASK_EVIDENCE_INVALID,
                              "the baseline task is not a completed verified import")
            if run_ref.task_id != task.task_id or run_ref.slot != F.SLOT_BASELINE:
                return _error(E.TASK_EVIDENCE_INVALID,
                              "the baseline run identity is inconsistent")
            if isinstance(reference, dict):
                invalid = self._invalid_verification_reference(reference)
                if invalid is not None:
                    return _error(E.TASK_EVIDENCE_INVALID, invalid)
            profile = live.profile
            body = {
                "schema_version": "flow_task.baseline_import.v1",
                "source_task_id": task.task_id,
                "source_task_digest": task.task_digest,
                "source_profile_id": task.profile_id,
                "source_profile_digest": task.profile_digest,
                "source_scope": task.scope.to_json_dict(),
                "input_manifest_sha256": task.baseline_input_manifest_sha256,
                "input_tree_hash": task.baseline_input_tree_hash,
                "run": run_ref.to_json_dict(),
                "baseline_functional_reference": reference_policy["state"],
                "output_root": str(profile.slot_output_root(
                    task.task_id, F.SLOT_BASELINE)),
                "variant": f"terminux_{task.task_id[-8:]}_{F.SLOT_BASELINE}",
                "verification_reference": (dict(reference)
                                           if isinstance(reference, dict) else None),
                "result_event_id": state.get("result_event_id"),
                "result_sha256": state.get("result_sha256"),
            }
        return {"status": "ok", "baseline_import": {
            **body, "import_digest": sha256_payload(body)}}

    @staticmethod
    def _invalid_verification_reference(reference: dict[str, Any]) -> str | None:
        """對 host 建立的 proof reference 重驗 bytes/hash。"""
        record = reference.get("production_reference")
        if isinstance(record, dict):
            # Package 的 method-neutral production reference 由它自己的
            # verifier 核對；setup-margin 專屬欄位不適用於其他 method。
            from terminux_engine.packages.orfs_run_intelligence import (
                baseline_reference as BR,
            )

            problems = BR.verify_reference(record, expected={})
            return ("; ".join(problems)) if problems else None
        pairs = (("reference_path", "sha256"),
                 ("post_cts_reference_path", "post_cts_sha256"),
                 ("post_resize_reference_path", "post_resize_sha256"),
                 ("qualified_tool_identity_path",
                  "qualified_tool_identity_sha256"))
        required = (("reference_path", "sha256"),
                    ("post_cts_reference_path", "post_cts_sha256"))
        for path_key, hash_key in required:
            if not isinstance(reference.get(path_key), str) or not isinstance(
                    reference.get(hash_key), str):
                return f"the baseline verification reference lacks {path_key}/{hash_key}"
        for path_key, hash_key in pairs:
            path_value = reference.get(path_key)
            hash_value = reference.get(hash_key)
            if path_value is None and hash_value is None:
                continue
            if not isinstance(path_value, str) or not isinstance(hash_value, str):
                return f"the baseline verification identity {path_key}/{hash_key} is incomplete"
            path = Path(path_value)
            if not path.is_file():
                return f"the baseline verification file is missing: {path_key}"
            if hashlib.sha256(path.read_bytes()).hexdigest() != hash_value:
                return f"the baseline verification file changed: {path_key}"
        return None

    def validate_eap_replacement_source(
        self, *, task_id: str, proposal: dict[str, Any], expected_scope: dict[str, Any],
    ) -> dict[str, Any]:
        """驗證可替代的歷史 EAP DRAFT，不賦予其任何執行權。

        這條狹窄內部路徑只接受同一 member/project、同一已保存 proposal 的歷史
        task；未啟動 draft 與因隔離而無 terminal receipt 的已啟動 task 會得到不同
        reason code。它不提供一般歷史 task 查詢，亦不會繞過 start 時的
        scope／digest／instance gate。
        """
        from terminux_engine.bounded_execution.analysis_tasks import flow_models as FM
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        context = self._deps.request_context()
        source = proposal.get("source") if isinstance(proposal, dict) else None
        if (not isinstance(task_id, str) or not task_id or not isinstance(source, dict)
                or not isinstance(expected_scope, dict)
                or expected_scope.get("member_id") != context.member_id):
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "replacement source is not available in this scope")
        try:
            history = TaskHistoryReader(self._deps.event_store, vocabulary=FM).load(
                task_id, member_id=context.member_id, project_id=context.project_id,
                any_project=True)
        except TaskEvidenceError as exc:
            return _error(E.TASK_EVIDENCE_INVALID, str(exc), task_id=task_id)
        if history is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "replacement source is not available in this scope")
        task = history.task
        task_source = ((getattr(task, "eap_proposal", None) or {}).get("source") or {})
        has_intent = any(step.intent_recorded or step.result_envelope is not None
                         or step.receipt is not None for step in history.steps.values())
        if (task.daemon_instance_id == self._deps.daemon_instance_id
                or task.scope.member_id != context.member_id
                or task.scope.project_id != expected_scope.get("project_id")
                or task.scope.project_source != "profile"
                or task_source.get("draft_id") != source.get("draft_id")
                or task_source.get("proposal_digest") != source.get("proposal_digest")
                or history.terminal is not None):
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "replacement source is not an unstarted historical EAP draft")
        reason_code = ("PRE_APPROVAL_DAEMON_TERMINATED"
                       if history.grant is None and not has_intent
                       else "UNCONFIRMED_EXECUTION_ISOLATED")
        return {"status": "ok", "replacement": {
            "replaces_task_id": task.task_id,
            "replaces_task_digest": task.task_digest,
            "reason_code": reason_code,
        }}

    def _create_flow_task(
        self, *, question: str, profile_path: str | None = None,
        profile_inline: Any = None, profile_id: str | None = None,
        eap_proposal: dict[str, Any] | None = None,
        eap_package_proposal: dict[str, Any] | None = None,
        eap_baseline_request: dict[str, Any] | None = None,
        eap_idempotency_key: tuple[str, ...] | None = None,
        create_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """建立固定五步的 flow task 草案。**不執行 EDA、不寫 candidate。**

        profile 由 user 在**這一次 create** 提供（path／inline）或從
        本 session 已選用的清單以 `profile_id` 指定；載入後凍結成 task 自己的
        immutable snapshot，之後不再重讀 live 檔案。

        create 只做輕量驗證：核對 profile、manifest 原始
        bytes/hash 與 inventory 引用一致性，再驗固定 config bytes。
        **不逐檔讀取並重算 3.2 GB**，也不重新複製 source——完整逐檔檢查由
        preparer 與 worker 負責。
        """
        flow = self._deps.flow
        if flow is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                          "flow tasks are not configured on this daemon")
        context = self._deps.request_context()
        if not isinstance(question, str) or not question.strip():
            return _error(E.TASK_INPUT_INVALID, "question must be a non-empty string")
        if len(question) > M.MAX_QUESTION_CHARS:
            return _error(E.TASK_INPUT_INVALID,
                          f"question exceeds {M.MAX_QUESTION_CHARS} characters")
        resolved = self._resolve_profile_source(
            context, profile_path=profile_path, profile_inline=profile_inline,
            profile_id=profile_id)
        if isinstance(resolved, dict):
            return resolved
        profile, source, accepted = resolved
        # Profile 合法性驗證後、project scope gate 與 digest 建立前，
        # 先解析**有效 project**（session 明確宣告 ⇒ 必須一致；未宣告 ⇒ 採用
        # Profile 的）。之後 gate、TaskScope、digest、事件都用這個有效 context。
        effective = self._effective_task_context(context, profile)
        if isinstance(effective, dict):
            return effective
        # 舊 container 模式不再建立新的可執行 task；Package 工具計畫的執行邊界
        # 由 draft 編譯核對（formal pair／ECO 各自的工具邊界）。
        if eap_package_proposal is None:
            legacy = _legacy_docker_denial(profile, None)
            if legacy is not None:
                return legacy
        # activation policy 只管 AI side effect 是否啟用、scope
        # 與（若部署有設）deployment restriction——不是 Profile allowlist。
        # allowed_projects 用**有效** project 比對：由 Profile 採用 Q 不能繞過
        # 只允許 P 的部署政策。
        denial = self._limited_activation_denial(
            effective, profile_id=profile.profile_id, profile=profile)
        if denial is not None:
            return denial
        baseline_import: dict[str, Any] | None = None
        if eap_proposal is not None:
            baseline_plan = eap_proposal.get("baseline_plan")
            if (isinstance(baseline_plan, dict)
                    and baseline_plan.get("mode") == "REUSE_VALIDATED_EAP3_BASELINE"):
                supplied = baseline_plan.get("reference")
                if not isinstance(supplied, dict):
                    return _error(E.TASK_INPUT_INVALID,
                                  "the Engineering Work baseline import is missing")
                exported = self.export_eap_baseline_import(
                    task_id=str(supplied.get("source_task_id", "")),
                    task_digest=str(supplied.get("source_task_digest", "")),
                    profile=profile)
                current = exported.get("baseline_import")
                if exported.get("status") != "ok" or current != supplied:
                    return _error(E.TASK_EVIDENCE_INVALID,
                                  "the Engineering Work baseline import is not current and verified")
                target_scope = {
                    "member_id": effective.member_id,
                    "project_id": effective.project_id,
                    "workspace": getattr(effective, "workspace", "") or "",
                    "project_source": getattr(
                        effective, "project_source", "session"),
                }
                durable_binding = exported.get("durable_binding")
                if isinstance(durable_binding, dict):
                    # 跨 daemon 的重用：來源 session 與本次 session 本來就不同。
                    # 用 target binding 判，而不是把 session 從 scope 規則刪掉。
                    binding = durable_binding
                    scope_ok = (
                        isinstance(binding, dict)
                        and all(binding.get(name) == value
                                for name, value in target_scope.items())
                        and binding.get("target_origin_session_id")
                        == effective.session_id
                        and binding.get("source_import_digest")
                        == current.get("import_digest"))
                else:
                    # `project_source` 是 additive 欄位：等於預設的 ``session``
                    # 時 `TaskScope` 不序列化它，但 target_scope 永遠帶著它。
                    # 直接比 dict 會在最常見的情形下必然不相等，因此兩側都先
                    # 正規化，再逐欄比較。
                    expected_scope = {**target_scope,
                                      "origin_session_id": effective.session_id}
                    actual_scope = dict(current.get("source_scope") or {})
                    actual_scope.setdefault("project_source", "session")
                    expected_scope.setdefault("project_source", "session")
                    scope_ok = actual_scope == expected_scope
                if isinstance(durable_binding, dict):
                    # durable 重用：target Profile 帶著 source 釘選，digest 必然
                    # 不同。Profile 身分已由重建端以 durable profile snapshot
                    # 的工程欄位逐項比對過，這裡只再確認輸入快照一致。
                    profile_ok = True
                else:
                    profile_ok = (
                        current.get("source_profile_id") == profile.profile_id
                        and current.get("source_profile_digest")
                        == profile.profile_digest)
                if (not profile_ok
                        or not scope_ok
                        or current.get("input_manifest_sha256")
                        != profile.input_manifest_sha256):
                    return _error(E.TASK_EVIDENCE_INVALID,
                                  "the imported baseline does not match the selected environment")
                # candidate 被授權**之前**：production baseline functional／formal
                # reference 必須存在、屬於這個 baseline task／run，且輸入、
                # scripts、image、工具身分與 evidence lineage 全部仍相符。
                invalid = _invalid_production_reference(current, profile)
                if invalid is not None:
                    return _error(E.TASK_EVIDENCE_INVALID, invalid)
                baseline_import = current
        try:
            draft = (
                flow.build_baseline_draft(
                    profile=profile, profile_source=source, question=question,
                    context=effective, daemon_instance_id=self._deps.daemon_instance_id,
                    request=eap_baseline_request)
                if eap_baseline_request is not None else
                flow.build_package_draft(
                    profile=profile, profile_source=source, question=question,
                    context=effective, daemon_instance_id=self._deps.daemon_instance_id,
                    proposal=eap_package_proposal, create_identity=create_identity)
                if eap_package_proposal is not None else
                flow.build_eap_draft(
                    profile=profile, profile_source=source, question=question,
                    context=effective, daemon_instance_id=self._deps.daemon_instance_id,
                    proposal=eap_proposal)
                if eap_proposal is not None else
                flow.build_draft(
                    profile=profile, profile_source=source, question=question,
                    context=effective, daemon_instance_id=self._deps.daemon_instance_id))
        except Exception as exc:  # noqa: BLE001 - 結構化拒絕，不猜
            code = getattr(exc, "code", None) or E.TASK_REVALIDATION_FAILED
            return _error(code, str(exc))

        task = draft.task
        # v2 Profile snapshot 必須在 task-created event 前 durable；task digest 也
        # 綁住其 digest，之後不能用目前磁碟上的 Profile 補值。
        from terminux_engine.bounded_execution.analysis_tasks import profile_snapshot as PS

        try:
            snapshot_v2 = PS.build_snapshot(task_id=task.task_id, document=accepted,
                                            profile=profile)
        except PS.ProfileSnapshotError as exc:
            return _error(E.TASK_EVIDENCE_INVALID, exc.message)
        task = replace(task, profile_snapshot={
            "schema_version": snapshot_v2["schema_version"],
            "snapshot_digest": snapshot_v2["snapshot_digest"],
            "accepted_input_sha256": snapshot_v2["accepted_input_sha256"],
            "canonical_profile_sha256": snapshot_v2["canonical_profile_sha256"],
            "expected_profile_digest": snapshot_v2["expected_profile_digest"],
            "profile_builder": dict(snapshot_v2["profile_builder"]),
        }, task_digest="")
        task = replace(task, task_digest=task.recompute_digest())
        live = _LiveFlowTask(
            task=task,
            steps={spec.step_id: {"step_id": spec.step_id,
                                  "capability_id": spec.capability_id,
                                  "status": "PENDING"} for spec in task.steps},
            profile=profile, profile_source=source,
        )
        if baseline_import is not None:
            run = baseline_import["run"]
            live.baseline_import = baseline_import
            live.runs[F.SLOT_BASELINE] = F.RunRef(
                run_id=run["run_id"], slot=run["slot"], task_id=run["task_id"],
                workflow_type=run["workflow_type"],
                created_at_ms=int(run["created_at_ms"]))
            imported_reference = baseline_import.get("verification_reference")
            live.verification_reference = (dict(imported_reference)
                                           if isinstance(imported_reference, dict)
                                           else None)
            live.steps.setdefault(F.STEP_RUN_BASELINE, {
                "step_id": F.STEP_RUN_BASELINE,
                "capability_id": "orfs_run_intelligence.run_flow",
            }).update({
                "status": "SUCCEEDED", "imported": True,
                "source_task_id": baseline_import["source_task_id"],
                "source_task_digest": baseline_import["source_task_digest"],
                "source_result_event_id": baseline_import.get("result_event_id"),
                "source_result_sha256": baseline_import.get("result_sha256"),
            })
        from terminux_engine.bounded_execution.analysis_tasks.package_flow import (
            create_identity_event_id,
        )
        from terminux_engine.bounded_execution.analysis_tasks.store import (
            TaskEventDuplicateError,
        )

        try:
            # 若 crash 發生在這裡與 task-created event 之間，這是一筆無權限的
            # orphan v2 snapshot；同一 deterministic task 重送可安全重用它。
            self._writer.append(
                event_type=F.EVENT_PROFILE_SNAPSHOT_V2, task_id=task.task_id,
                task_digest=task.task_digest,
                daemon_instance_id=task.daemon_instance_id, scope=task.scope,
                sequence=_next_sequence(live), body={"profile_snapshot": snapshot_v2},
                event_id=PS.snapshot_event_id(task.task_id))
        except TaskEventDuplicateError:
            existing = self._deps.event_store.get_event_by_id_for_scope(
                PS.snapshot_event_id(task.task_id), user_id=task.scope.member_id,
                project_id=task.scope.project_id)
            if ((existing is None or (existing.payload or {}).get("profile_snapshot")
                 != snapshot_v2)):
                return _error(E.TASK_EVIDENCE_INVALID,
                              "profile snapshot orphan identity drift")
            if create_identity is not None:
                # 另一個進程已先寫入同一 task 的 profile snapshot v2。
                # 輸家不應繼續寫入分歧草案，而應重用贏家首次持久化的真實建立時間與 task。
                import time
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    replayed = self._replay_package_flow_task(create_identity, effective)
                    if replayed is not None:
                        return replayed
                    time.sleep(0.02)
                return _error(E.TASK_EVIDENCE_INVALID,
                              "the task was created concurrently but timed out waiting for durable replay")
        try:
            # 帶 durable create identity 時，task-created event 的 id 由該身分
            # 推導。`session_events.event_id` 是 PRIMARY KEY，因此這一次 append
            # 就是 compare-and-append：並行的兩個 process 只有一個能寫進去。
            self._writer.append(
                event_type=F.EVENT_SNAPSHOT, task_id=task.task_id,
                task_digest=task.task_digest,
                daemon_instance_id=task.daemon_instance_id, scope=task.scope,
                sequence=_next_sequence(live), body={"task": task.to_json_dict()},
                event_id=(create_identity_event_id(create_identity)
                          if create_identity is not None else None))
            # v1 相容：保留既有 canonical snapshot；v2 才是可重建 authority。
            # 一併持久化；之後 live 檔案怎麼改都與這筆 task 無關。
            self._writer.append(
                event_type=F.EVENT_PROFILE_SNAPSHOT, task_id=task.task_id,
                task_digest=task.task_digest,
                daemon_instance_id=task.daemon_instance_id, scope=task.scope,
                sequence=_next_sequence(live),
                body={"profile": profile.to_json_dict(),
                      "profile_source": source.to_json_dict()})
            if baseline_import is not None:
                self._writer.append(
                    event_type=F.EVENT_INPUT_REFERENCE, task_id=task.task_id,
                    task_digest=task.task_digest,
                    daemon_instance_id=task.daemon_instance_id, scope=task.scope,
                    sequence=_next_sequence(live), body={
                        "slot": F.SLOT_BASELINE,
                        "kind": "validated_baseline_import",
                        "baseline_import": baseline_import})
        except TaskEventDuplicateError:
            # 另一個 process／另一次呼叫已經寫下同一個 create identity。
            # 這裡**不**建立第二個 task、不寫第二筆 created event，直接讀回它的。
            replayed = (self._replay_package_flow_task(create_identity, effective)
                        if create_identity is not None else None)
            if replayed is not None:
                return replayed
            import time
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                replayed = self._replay_package_flow_task(create_identity, effective)
                if replayed is not None:
                    return replayed
                time.sleep(0.02)
            return _error(E.TASK_EVIDENCE_INVALID,
                          "the task was created concurrently but could not be read back")
        except TaskEventWriteError as exc:
            return _error(E.TASK_EVIDENCE_INVALID,
                          f"the task snapshot was not persisted: {exc}")
        with self._tasks_lock:
            self._flow_tasks[task.task_id] = live
            if eap_idempotency_key is not None:
                self._eap_flow_tasks[eap_idempotency_key] = task.task_id
        return {"status": "ok", "task": self._flow_view(live).to_json_dict()}

    def start_flow_task(self, task_id: str, task_digest: str) -> dict[str, Any]:
        """operator-only 一次授權、非阻塞啟動。"""
        from terminux_engine.bounded_execution.analysis_tasks import profile_snapshot as PS

        self.reconcile_unconfirmed_slots()
        flow = self._deps.flow
        context = self._deps.request_context()
        try:
            live = self.get_or_hydrate_flow_task(task_id, context)
        except PS.ProfileSnapshotError as exc:
            return _error(exc.code, exc.message, task_id=task_id)
        if live is None or flow is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, "no such task in this scope")

        with live.lock:
            denial = self._start_flow_precheck_locked(live, task_digest, context)
            if denial is not None:
                return denial

            task = live.task
            t0_ms, t0_mono = now_ms(), monotonic_s()
            grant = M.TaskAuthorizationGrant(
                grant_id=M.new_grant_id(), task_id=task.task_id,
                task_digest=task.task_digest,
                daemon_instance_id=self._deps.daemon_instance_id,
                scope=task.scope, approved_by_session_id=context.session_id,
                approved_at_ms=t0_ms,
                expires_at_ms=t0_ms + task.task_deadline_sec * 1000,
                expires_at_monotonic=t0_mono + task.task_deadline_sec,
                deadline_at_ms=t0_ms + task.task_deadline_sec * 1000,
                deadline_at_monotonic=t0_mono + task.task_deadline_sec,
                body_llm_dispatches=int(task.limits.get(
                    "body_llm_dispatches", M.BODY_LLM_DISPATCHES)),
            )
            try:
                self._writer.append(
                    event_type=F.EVENT_GRANT, task_id=task.task_id,
                    task_digest=task.task_digest,
                    daemon_instance_id=task.daemon_instance_id,
                    scope=task.scope, sequence=_next_sequence(live),
                    body={"grant": grant.to_event_payload()})
            except TaskEventWriteError as exc:
                live.startable = False
                live.persistence_status = "failed"
                self._force_flow_terminal_locked(
                    live, "BLOCKED", f"authorization not persisted: {exc}")
                return _error(E.TASK_EVIDENCE_INVALID,
                              "the authorization was not persisted; the task "
                              "did not start")
            live.grant = grant
            live.startable = False
            # 核准後交給 scheduler。writable root 是 profile 宣告的
            # `execution_root`：同一個 root 的 task 不平行寫入，後來者排隊並
            # 看得到原因。名額不足也是排隊，不是 TASK_BUSY。
            roots = tuple((task.execution_plan or {}).get("writable_roots") or ())
            if not roots and live.profile is not None:
                roots = (str(live.profile.execution_root),)
            decision, reason = self._scheduler.admit(
                task.task_id, writable_roots=roots, approved_at_ms=t0_ms,
                on_dispatch=lambda: self._dispatch_flow(live))
            if decision == "QUEUED":
                live.status = "QUEUED"
                live.queue_reason = reason
                live.queued_at_ms = t0_ms
                try:
                    self._writer.append(
                        event_type=F.EVENT_QUEUED, task_id=task.task_id,
                        task_digest=task.task_digest,
                        daemon_instance_id=task.daemon_instance_id,
                        scope=task.scope, sequence=_next_sequence(live),
                        body={"grant_id": grant.grant_id, "reason": reason,
                              "queued_at_ms": t0_ms,
                              "scheduler": self._scheduler.snapshot().to_json_dict()})
                except TaskEventWriteError as exc:
                    live.persistence_status = "failed"
                    _LOG.warning("flow queued record not persisted: %r", exc)
                return {"status": "ok", "accepted": True, "queued": True,
                        "task": self._flow_view(live).to_json_dict()}
        return self._dispatch_flow(live)

    def _dispatch_flow(self, live: _LiveFlowTask) -> dict[str, Any]:
        """名額到手後（立即或出隊）真正開始：寫 started、換 RUNNING、submit worker。
        排隊期間被取消的 task 不啟動任何 child，名額直接還回去。"""
        flow = self._deps.flow
        release_only = False
        response: dict[str, Any] | None = None
        with live.lock:
            task = live.task
            grant = live.grant
            if (live.stop or live.status in M.TERMINAL_TASK_STATUSES
                    or grant is None or flow is None):
                release_only = True
                response = {"status": "ok", "accepted": False,
                            "task": self._flow_view(live).to_json_dict()}
            else:
                started_ms = now_ms()
                try:
                    self._writer.append(
                        event_type=F.EVENT_STARTED, task_id=task.task_id,
                        task_digest=task.task_digest,
                        daemon_instance_id=task.daemon_instance_id,
                        scope=task.scope, sequence=_next_sequence(live),
                        body={"grant_id": grant.grant_id,
                              "started_at_ms": started_ms,
                              "deadline_at_ms": grant.deadline_at_ms})
                except TaskEventWriteError as exc:
                    live.persistence_status = "failed"
                    self._force_flow_terminal_locked(
                        live, "BLOCKED", f"start not persisted: {exc}")
                    release_only = True
                    response = _error(E.TASK_EVIDENCE_INVALID,
                                      "the start record was not persisted; the "
                                      "task did not start")
                else:
                    live.status = "RUNNING"
                    live.started_at_ms = started_ms
                    live.holds_slot = True
                    live.queue_reason = None
        if release_only:
            self._scheduler.release(live.task.task_id)
            return response or {"status": "ok", "accepted": False}
        try:
            self._deps.submit(lambda: self._run_flow_worker(flow, live))
        except Exception as exc:  # noqa: BLE001
            with live.lock:
                self._force_flow_terminal_locked(
                    live, "BLOCKED", f"worker submit failed: {exc!r}")
            self._release_slot_flow(live, True)
            return _error(E.TASK_WORKER_UNCONFIRMED,
                          "the analysis worker could not be started")
        return {"status": "ok", "accepted": True,
                "task": self._flow_view(live).to_json_dict()}

    def _start_flow_precheck_locked(
        self, live: _LiveFlowTask, task_digest: str, context: Any,
    ) -> dict[str, Any] | None:
        task = live.task
        # 每一次 start 都重新過 gate：create 當時允許不代表現在仍允許
        # （policy 可能已被關閉或漂移）。用 task **固定**的 project，
        # 不以「目前選用的另一份 Profile」或 session 的預設值重新推導。
        denial = self._limited_activation_denial(
            self._task_scoped_context(context, task), profile_id=task.profile_id,
            profile=live.profile)
        if denial is not None:
            return denial
        if live.status != "DRAFT" or not live.startable:
            return _error(E.TASK_ALREADY_STARTED,
                          f"the task is already {live.status}",
                          task_id=task.task_id, task_status=live.status)
        if task.scope.origin_session_id != context.session_id:
            return _error(E.TASK_ORIGIN_SESSION_REQUIRED,
                          "only the session that created the task can start it")
        if task.task_digest != task_digest or not task.digest_matches():
            return _error(E.TASK_DIGEST_MISMATCH, "the task digest does not match")
        if task.daemon_instance_id != self._deps.daemon_instance_id:
            return _error(E.TASK_INSTANCE_EXPIRED,
                          "the task belongs to a previous daemon instance")
        # 草案**不因時間經過失效**。等待期間不消耗任何
        # 執行期限（grant／deadline 於 `start_flow_task` 核准當下才起算）。

        # 舊 container 模式在 preflight（`docker image inspect`）之前就拒絕；
        # task 停在 BLOCKED，不發出 grant、不占名額、不啟動 child。
        legacy = _legacy_docker_denial(live.profile, task)
        if legacy is not None:
            live.status = "BLOCKED"
            live.startable = False
            return legacy

        flow = self._deps.flow
        ready, reason = flow.preflight(live.task)
        if not ready:
            live.status = "BLOCKED"
            live.startable = False
            return _error(E.TASK_REVALIDATION_FAILED, reason or "preflight failed")

        # provider 不可用時，在占用名額／grant 之前拒絕。
        if self._deps.provider_readiness is not None:
            provider_ok, provider_reason = self._deps.provider_readiness()
            if not provider_ok:
                return _error(E.TASK_PROVIDER_UNAVAILABLE,
                              provider_reason or "the analysis provider is not available")
        return None

    def _release_slot_flow(self, live: _LiveFlowTask, acquired: bool) -> None:
        """釋放 scheduler 名額；可能在鎖外啟動下一個 queued task。"""
        if not acquired or not live.holds_slot:
            return
        live.holds_slot = False
        self._scheduler.release(live.task.task_id)

    def reconcile_unconfirmed_slots(self) -> list[str]:
        """對帳並收回已終結的 unconfirmed flow task 所占用的名額。

        三種情況處置中，情況 3「child 已結束但名額未釋放」應由 reconciliation 收回。
        遍歷 scheduler 中處於 unconfirmed 的 tasks，若其 live task 已進入 terminal
        狀態且所有 child run 均已在 ledger 達 terminal 狀態（或從未啟動），
        則安全 release 其 scheduler 名額，喚醒後續 queued task。
        回傳被釋放的 task_id 列表。不得持任何 task lock 呼叫 release。
        """
        from terminux_engine.bounded_execution.analysis_tasks.run_evidence import (
            TERMINAL_RUN_STATES,
        )

        snapshot = self._scheduler.snapshot()
        if not snapshot.unconfirmed:
            return []

        ledger = getattr(self._deps, "ledger", None)
        if ledger is None and self._deps.flow is not None:
            flow_deps = getattr(self._deps.flow, "_deps", None)
            step_deps = getattr(flow_deps, "step_deps", None)
            if step_deps is not None:
                ledger = getattr(step_deps, "ledger", None)

        to_release: list[str] = []
        for task_id in list(snapshot.unconfirmed.keys()):
            with self._tasks_lock:
                live = self._flow_tasks.get(task_id) or self._tasks.get(task_id)
            if live is None:
                continue
            with live.lock:
                task_terminal = live.status in M.TERMINAL_TASK_STATUSES
                runs = list(getattr(live, "runs", {}).values())
                dispatched = getattr(live, "dispatched", None)
                had_dispatch = bool(dispatched)

            if not task_terminal:
                continue

            all_terminal = True
            if runs:
                if ledger is None:
                    continue
                for run_ref in runs:
                    try:
                        rec = ledger.get_run(run_ref.run_id)
                    except Exception:  # noqa: BLE001
                        all_terminal = False
                        break
                    if rec is None:
                        all_terminal = False
                        break
                    st = getattr(rec, "status", None) or (rec.get("status") if isinstance(rec, dict) else None)
                    if st not in TERMINAL_RUN_STATES:
                        all_terminal = False
                        break
            else:
                # 無 child run 紀錄時，必須有可靠的「尚未啟動」依據才能釋放
                if had_dispatch:
                    all_terminal = False
            if all_terminal:
                with live.lock:
                    live.holds_slot = False
                to_release.append(task_id)

        reconciled: list[str] = []
        for task_id in to_release:
            self._scheduler.release(task_id)
            reconciled.append(task_id)
            _LOG.info("reconciled unconfirmed slot for task %s", task_id)
        return reconciled

    def record_eap_execution_feedback(self, live: _LiveFlowTask) -> None:
        """保存執行證據→EAP Domain 分析→Global 決策的關聯。"""
        if live.task.eap_proposal is None:
            return
        with live.lock:
            if live.eap_feedback is not None:
                return
            domain_responses = [
                {"step_id": step_id, "result_event_id": state.get("result_event_id"),
                 "result_sha256": state.get("result_sha256"),
                 "status": state.get("status")}
                for step_id, state in live.steps.items()
                if step_id in (F.STEP_EXPLAIN, F.STEP_COMPARE)
            ]
            outcome = {
                "source": dict(live.task.eap_proposal.get("source") or {}),
                "task_id": live.task.task_id, "task_digest": live.task.task_digest,
                "scope": live.task.scope.to_json_dict(), "terminal_status": live.status,
                "engineering_status": (live.repair or {}).get("repair_status"),
                "domain_step_receipts": domain_responses,
                "remaining_dispatches": max(0, int(live.task.limits.get(
                    "body_llm_dispatches", 3)) - int(getattr(live.budget, "used", 0))),
            }
        reviewer = self._deps.eap_execution_feedback_reviewer
        if reviewer is None:
            review = {"feedback_status": "NOT_RUN_REVIEWER_UNAVAILABLE",
                      "execution_allowed": False}
        else:
            budget = live.budget
            permits = ([] if budget is None else [budget.try_consume(), budget.try_consume()])
            if budget is None or not all(allowed for allowed, _reason in permits):
                review = {"feedback_status": "NOT_RUN_BUDGET_EXHAUSTED",
                          "execution_allowed": False}
            else:
                try:
                    review = reviewer(outcome)
                except Exception as exc:  # noqa: BLE001 - 結果不可確認，不猜 Global 決策
                    _LOG.warning("EAP Domain/Global feedback failed: %r", exc)
                    review = {"feedback_status": "UNCONFIRMED_REVIEWER_FAILURE",
                              "reason": repr(exc), "execution_allowed": False}
        feedback = {**outcome, **review}
        with live.lock:
            if live.eap_feedback is not None:
                return
            try:
                self._writer.append(
                    event_type=F.EVENT_EAP_FEEDBACK, task_id=live.task.task_id,
                    task_digest=live.task.task_digest,
                    daemon_instance_id=live.task.daemon_instance_id,
                    scope=live.task.scope, sequence=_next_sequence(live),
                    body={"feedback": feedback})
            except TaskEventWriteError as exc:
                _LOG.warning("EAP execution feedback was not persisted: %r", exc)
                return
            live.eap_feedback = feedback

    def _force_flow_terminal_locked(
        self, live: _LiveFlowTask, status: str, reason: str | None,
    ) -> None:
        live.status = status
        live.terminal_reason = reason
        live.current_operation = F.OPERATION_IDLE
        try:
            self._writer.append(
                event_type=F.EVENT_TERMINAL, task_id=live.task.task_id,
                task_digest=live.task.task_digest,
                daemon_instance_id=live.task.daemon_instance_id,
                scope=live.task.scope, sequence=_next_sequence(live),
                body={"status": status, "reason": reason,
                      "step_references": [dict(v) for v in live.steps.values()],
                      "runs": {k: v.to_json_dict() for k, v in live.runs.items()},
                      "budget_consumed": self._flow_budget_view(live)})
        except TaskEventWriteError as exc:
            _LOG.warning("flow task terminal event not persisted: %r", exc)
            live.persistence_status = "failed"

    def _lookup_flow(self, task_id: str, context: Any) -> _LiveFlowTask | None:
        try:
            live = self.get_or_hydrate_flow_task(task_id, context)
        except Exception:
            return None
        if live is None:
            return None
        # 明確宣告 project 的 session 比 member＋project；未宣告的只認
        # 自己建立的 task（origin session）。
        if not live.task.scope.accessible_by(context):
            return None
        return live

    def get_or_hydrate_flow_task(self, task_id: str, context: Any) -> _LiveFlowTask | None:
        """以 EventStore 為 authority 取得或重建 FlowTask cache。

        v1 task 只能走既有唯讀 reader；任何需要可操作 live state 的 v1 history
        一律 fail closed。v2 會重建 Profile，核對 task digest／scope／sequence 後
        才放入 cache；此函式本身絕不寫 event、ledger 或重新派送 worker。
        """
        from terminux_engine.bounded_execution.analysis_tasks import profile_snapshot as PS
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        with self._tasks_lock:
            cached = self._flow_tasks.get(task_id)
        # cache 的 scope 先過 access check，才可用 task 固定 project 讀 durable
        # projection；這讓未宣告 project 的 fallback session 不會被目前 session
        # 的空／預設 project 誤拒，也不會跨 scope 探測 task。
        if cached is not None and not cached.task.scope.accessible_by(context):
            return None
        reader = TaskHistoryReader(self._deps.event_store, vocabulary=F)
        try:
            history = reader.load(task_id, member_id=context.member_id,
                                  project_id=(cached.task.scope.project_id
                                              if cached is not None
                                              else context.project_id))
        except TaskEvidenceError:
            raise
        if history is None or not history.task.scope.accessible_by(context):
            return None
        if cached is not None:
            if (not cached.task.scope.accessible_by(context)
                    or cached.task.task_digest != history.task.task_digest
                    or cached.task.scope != history.task.scope
                    or cached.profile is None
                    or cached.profile.profile_digest != history.task.profile_digest
                    or cached.task.profile_snapshot != history.task.profile_snapshot):
                raise PS.ProfileSnapshotError(
                    "UNVERIFIABLE", "flow task cache differs from durable projection")
            return cached
        marker = getattr(history.task, "profile_snapshot", None)
        if not isinstance(marker, dict):
            raise PS.ProfileSnapshotError(
                "WORK_SCHEMA_UNSUPPORTED", "PROFILE_SNAPSHOT_RECONSTRUCTION_UNAVAILABLE")
        snapshots = history.informational.get(F.EVENT_PROFILE_SNAPSHOT_V2, [])
        if len(snapshots) != 1:
            raise PS.ProfileSnapshotError("UNVERIFIABLE", "profile snapshot v2 is missing")
        payload = snapshots[0].get("profile_snapshot")
        profile = PS.rebuild_snapshot(payload, task_id=task_id)
        if (marker.get("snapshot_digest") != payload.get("snapshot_digest")
                or marker.get("accepted_input_sha256") != payload.get("accepted_input_sha256")
                or marker.get("canonical_profile_sha256") != payload.get("canonical_profile_sha256")
                or marker.get("expected_profile_digest") != payload.get("expected_profile_digest")
                or marker.get("profile_builder") != payload.get("profile_builder")):
            raise PS.ProfileSnapshotError("UNVERIFIABLE", "task/profile snapshot binding differs")
        if profile.profile_digest != history.task.profile_digest:
            raise PS.ProfileSnapshotError("UNVERIFIABLE", "task/profile digest differs")
        terminal = history.terminal or {}
        if terminal:
            status = terminal.get("status")
            if status not in M.TERMINAL_TASK_STATUSES:
                raise PS.ProfileSnapshotError("UNVERIFIABLE", "terminal state is unknown")
        elif history.grant is None:
            status = "DRAFT"
        elif history.started is None:
            status = "GRANTED"
        else:
            status = "OUTCOME_UNCONFIRMED"
        queued = history.informational.get(F.EVENT_QUEUED, [])
        if not terminal and queued and history.started is None:
            status = "QUEUED"
        steps = {}
        for spec in history.task.steps:
            record = history.steps[spec.step_id]
            steps[spec.step_id] = {
                "step_id": spec.step_id, "capability_id": spec.capability_id,
                "status": record.status, "result_event_id": record.result_event_id,
                "result_sha256": record.result_sha256,
            }
        # grant／run 都是 durable lineage；hydrate 只重建其歷史表示，絕不把
        # 舊 daemon 的 authorization 或 child 恢復為可執行狀態。
        grant = None
        if history.grant is not None:
            raw_grant = history.grant
            required = ("grant_id", "task_digest", "approved_by_session_id",
                        "approved_at_ms", "expires_at_ms", "deadline_at_ms",
                        "body_llm_dispatches")
            if (any(raw_grant.get(key) is None for key in required)
                    or raw_grant.get("task_digest") != history.task.task_digest):
                raise PS.ProfileSnapshotError("UNVERIFIABLE", "grant lineage differs")
            try:
                grant = M.TaskAuthorizationGrant(
                    grant_id=str(raw_grant["grant_id"]), task_id=history.task.task_id,
                    task_digest=history.task.task_digest,
                    daemon_instance_id=history.task.daemon_instance_id,
                    scope=history.task.scope,
                    approved_by_session_id=str(raw_grant["approved_by_session_id"]),
                    approved_at_ms=int(raw_grant["approved_at_ms"]),
                    expires_at_ms=int(raw_grant["expires_at_ms"]),
                    # monotonic timestamp 無法跨 process 重建；零值保證它永遠
                    # 不能意外取得新執行權。
                    expires_at_monotonic=0.0,
                    deadline_at_ms=int(raw_grant["deadline_at_ms"]),
                    deadline_at_monotonic=0.0,
                    body_llm_dispatches=int(raw_grant["body_llm_dispatches"]),
                )
            except (TypeError, ValueError) as exc:
                raise PS.ProfileSnapshotError("UNVERIFIABLE", "grant is malformed") from exc
        runs: dict[str, Any] = {}
        for link in history.informational.get(F.EVENT_RUN_LINK, []):
            raw_run = link.get("run")
            if not isinstance(raw_run, dict):
                raise PS.ProfileSnapshotError("UNVERIFIABLE", "run lineage is malformed")
            try:
                run = F.RunRef(
                    run_id=str(raw_run["run_id"]), slot=str(raw_run["slot"]),
                    task_id=str(raw_run["task_id"]),
                    workflow_type=str(raw_run["workflow_type"]),
                    created_at_ms=int(raw_run["created_at_ms"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PS.ProfileSnapshotError("UNVERIFIABLE", "run lineage is malformed") from exc
            if run.task_id != history.task.task_id or run.slot in runs:
                raise PS.ProfileSnapshotError("UNVERIFIABLE", "run lineage differs")
            runs[run.slot] = run
        selections = history.informational.get(F.EVENT_SELECTION, [])
        lineages = history.informational.get(F.EVENT_LINEAGE, [])
        feedbacks = history.informational.get(F.EVENT_EAP_FEEDBACK, [])
        live = _LiveFlowTask(
            task=history.task, status=status, startable=False, profile=profile,
            steps=steps, sequence=(max(history.sequences) + 1 if history.sequences else 0),
            grant=grant, runs=runs,
            started_at_ms=(history.started or {}).get("started_at_ms"),
            terminal_reason=terminal.get("reason"), repair=history.repair,
            selection=(dict(selections[-1].get("selection"))
                       if selections and isinstance(selections[-1].get("selection"), dict)
                       else None),
            lineage=(dict(lineages[-1].get("lineage"))
                     if lineages and isinstance(lineages[-1].get("lineage"), dict)
                     else {}),
            eap_feedback=(dict(feedbacks[-1].get("feedback"))
                          if feedbacks and isinstance(feedbacks[-1].get("feedback"), dict)
                          else None),
        )
        # 第二個 concurrent hydrate 只能接受同一份 durable task，不能覆蓋 cache。
        with self._tasks_lock:
            existing = self._flow_tasks.get(task_id)
            if existing is not None:
                if existing.task.task_digest != history.task.task_digest:
                    raise PS.ProfileSnapshotError("UNVERIFIABLE", "cache task digest differs")
                return existing
            self._flow_tasks[task_id] = live
        return live

    @staticmethod
    def _flow_budget_view(live: _LiveFlowTask) -> dict[str, Any]:
        limits = live.task.limits
        used = live.budget.used if live.budget is not None else 0
        return {
            "body_llm_dispatch_limit": int(limits.get("body_llm_dispatches", 3)),
            "body_llm_dispatches_used": used,
            "eda_launch_limit": int(limits.get("eda_launches", 2)),
            "eda_launches_used": live.eda_launches_used,
            "candidate_write_limit": int(limits.get("candidate_writes", 1)),
            "candidate_writes_used": live.candidate_writes_used,
            "planning_dispatches": None,
            "provider_usage": "unknown",
            "sdk_retry_observable": None,
        }

    @staticmethod
    def _narrative_view_fields(task: Any, live: Any) -> dict[str, Any]:
        """解說狀態與工程結果分列。"""
        if task.template_id != F.SETUP_REPAIR_V2_TEMPLATE_ID:
            return {}
        from terminux_engine.bounded_execution.analysis_tasks import (
            narrative_policy as NP,
        )

        rejections = list(getattr(live, "narrative_rejections", []) or [])
        narrative_steps = [
            state for state in live.steps.values()
            if state.get("capability_id") in (
                "orfs_run_intelligence.explain_run",
                "orfs_run_intelligence.compare_runs")]
        attempted = [state for state in narrative_steps
                     if state.get("status") not in (None, "PENDING", "SKIPPED")]
        if not attempted and not rejections:
            # 還沒有任何一段解說被嘗試過（例如 DRAFT）。這時說「兩段解說都
            # 有效」是高估——什麼都還沒產生。不顯示，而不是報一個好消息。
            return {}
        # 「跑完」不等於「產生了解說」。2026-09-08 的真實 task 裡，s5 因為
        # 兩次執行每個 metric 都相同而沒有任何可引用的證據，Package 走
        # deterministic 路徑、**完全沒有呼叫模型**（support_status=
        # insufficient_evidence）。把那種情況算成 COMPLETE 是高估：實際上
        # 那一段沒有解說。以 business class 是不是 valid 為準。
        produced = [state for state in attempted
                    if state.get("business_class") == "valid"]
        rejected_ids = {r.get("step_id") for r in rejections}
        unavailable = [
            {"step_id": state.get("step_id"),
             "capability_id": state.get("capability_id"),
             "business_class": state.get("business_class"),
             "reason": "no explanation was produced for this step"}
            for state in attempted
            if state.get("business_class") != "valid"
            and state.get("step_id") not in rejected_ids]
        return {
            "narrative_status": NP.narrative_status(
                attempted=max(len(attempted), len(rejections)),
                rejected=max(len(attempted) - len(produced), len(rejections))),
            "narrative_rejections": rejections,
            **({"narrative_unavailable": unavailable} if unavailable else {}),
        }

    def _flow_view(self, live: _LiveFlowTask) -> F.FlowTaskView:
        task = live.task
        return F.FlowTaskView(
            task_id=task.task_id, task_digest=task.task_digest, status=live.status,
            template_id=task.template_id, profile_id=task.profile_id,
            question=task.question, design_id=task.design_id,
            platform_id=task.platform_id,
            parameter={"name": task.parameter_name,
                       "baseline": task.baseline_value,
                       "candidate": task.candidate_value},
            created_at_ms=task.created_at_ms,
            draft_expires_at_ms=task.draft_expires_at_ms,
            project_id=task.scope.project_id, project_source=task.scope.project_source,
            steps=[dict(v) for v in live.steps.values()],
            current_operation=live.current_operation,
            daemon_instance_id=task.daemon_instance_id,
            is_current_instance=task.daemon_instance_id == self._deps.daemon_instance_id,
            started_at_ms=live.started_at_ms,
            deadline_at_ms=live.grant.deadline_at_ms if live.grant else None,
            grant_id=live.grant.grant_id if live.grant else None,
            terminal_reason=live.terminal_reason,
            persistence_status=live.persistence_status,
            budget=self._flow_budget_view(live),
            runs=[v.to_json_dict() for v in live.runs.values()],
            candidate_plan=[dict(c) for c in task.candidate_plan],
            selection=live.selection,
            repair=live.repair,
            repair_recipe=task.repair_recipe,
            profile_source=task.profile_source,
            execution_plan=task.execution_plan,
            eap_proposal=task.eap_proposal,
            eap_feedback=live.eap_feedback,
            queue=self._queue_view(live),
            lineage=dict(live.lineage) if live.lineage else None,
            comparison=((live.repair or {}).get("comparison_basis")
                        if live.repair else None),
            execution_result={
                "eda_runs": {slot: {"run_id": ref.run_id}
                             for slot, ref in live.runs.items()},
                "step_status": {k: v.get("status") for k, v in live.steps.items()},
                "engineering_status": (live.repair or {}).get("repair_status"),
                "comparison_status": (live.repair or {}).get("comparison_status"),
            },
            **self._narrative_view_fields(task, live),
        )

    # ── get / cancel ────────────────────────────────────────────────

    def get(self, task_id: str, *, include_results: bool = False) -> dict[str, Any]:
        context = self._deps.request_context()
        if not getattr(context, "project_declared", True):
            return self._get_for_undeclared_session(
                task_id, context, include_results=include_results)
        # 同一組公開入口依 task 類型分派（不另建第二套 lifecycle）。
        flow_live = self._lookup_flow(task_id, context)
        if flow_live is not None:
            with flow_live.lock:
                payload = {"status": "ok",
                           "task": self._flow_view(flow_live).to_json_dict()}
                if include_results:
                    payload["results"] = self._read_back_flow_results(
                        flow_live, context)
                return payload
        live = self._lookup(task_id, context)
        if live is not None:
            denial = self._read_access_denial(live.task.scope)
            if denial is not None:
                return denial
            with live.lock:
                payload = {"status": "ok", "task": self._view(live).to_json_dict()}
                if include_results:
                    payload["results"] = self._read_back_results(live, context)
                return payload

        # Usage Model 的 FlowTask 歷史唯讀只依已保存的
        # member/project scope；analysis task 的 two-run analysis policy 是 AnalysisTask
        # 的資料來源授權，不是 FlowTask 歷史可見性的前置。
        historical = self._get_flow_from_history(
            task_id, context, include_results=include_results)
        if historical is not None:
            return historical
        if self._deps.read_access_check is not None:
            allowed, reason = self._deps.read_access_check(
                context.member_id, context.project_id)
            if not allowed:
                # 不洩漏 analysis task 是否存在：與查無此 task 同碼。
                return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
                              reason or "no such task in this scope")
        historical = self._get_from_history(
            task_id, context, include_results=include_results)
        if historical is not None:
            return historical
        return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, "no such task in this scope")

    def _get_for_undeclared_session(
        self, task_id: str, context: Any, *, include_results: bool,
    ) -> dict[str, Any]:
        """未宣告 project 的 session 的 `get`（**唯讀**）。

        live：沿用 `accessible_by`（只認建立這筆 task 的 session）。
        歷史（daemon 重啟後）：session id 已換、也沒有 project 可比對，因此以
        member 查 event store，再以 snapshot 固定的 scope 判定——只接受
        `project_source="profile"`（fallback 建立的 task）或 project 恰等於
        caller 預設值的 task；由**明確宣告** project 的 session 建立的 task 仍需
        明確宣告該 project 的 session 才能讀（與先前相同）。
        AnalysisTask 仍額外經 read_access_check；FlowTask 歷史只做上述
        Usage Model scope 判定。這裡不產生任何執行權：start／cancel 不經此路徑。
        """
        # live 的**讀取**規則與歷史一致：同 member 的 fallback（profile）task 可唯讀，
        # 不限 origin session；start／cancel 走 `_lookup_flow`／`_lookup`（accessible_by，
        # 只認 origin session），不受影響。
        with self._tasks_lock:
            flow_live = self._flow_tasks.get(task_id)
            live = self._tasks.get(task_id)
        if flow_live is not None and not self._readable_by_undeclared(
                flow_live.task.scope, context):
            flow_live = None
        if live is not None and not self._readable_by_undeclared(live.task.scope, context):
            live = None
        if flow_live is not None:
            with flow_live.lock:
                payload = {"status": "ok",
                           "task": self._flow_view(flow_live).to_json_dict()}
                if include_results:
                    payload["results"] = self._read_back_flow_results(
                        flow_live, context)
                return payload
        if live is not None:
            denial = self._read_access_denial(live.task.scope)
            if denial is not None:
                return denial
            with live.lock:
                payload = {"status": "ok", "task": self._view(live).to_json_dict()}
                if include_results:
                    payload["results"] = self._read_back_results(live, context)
                return payload
        historical = self._get_from_history(task_id, context,
                                            include_results=include_results,
                                            any_project=True)
        if historical is not None:
            return historical
        historical = self._get_flow_from_history(
            task_id, context, include_results=include_results, any_project=True)
        if historical is not None:
            return historical
        return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, "no such task in this scope")

    def _read_access_denial(self, scope: Any) -> dict[str, Any] | None:
        """`read_access_check` 以 task **固定**的 member／project 判定。"""
        if self._deps.read_access_check is None:
            return None
        allowed, reason = self._deps.read_access_check(scope.member_id, scope.project_id)
        if allowed:
            return None
        return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, reason or "no such task in this scope")

    @staticmethod
    def _history_readable_by_undeclared(scope: Any, context: Any) -> bool:
        """未宣告 session 可讀的歷史 task：fallback 建立的（profile），或
        project 恰等於 caller 的預設值（unset analysis task）。member 必須相符。"""
        if scope.member_id != context.member_id:
            return False
        return (getattr(scope, "project_source", "session") == "profile"
                or scope.project_id == context.project_id)

    def _readable_by_undeclared(self, scope: Any, context: Any) -> bool:
        """未宣告 session 的**唯讀**規則（live 與歷史相同）：自己建立的，或同 member
        的 fallback task。執行類操作不用這條。"""
        return scope.accessible_by(context) or self._history_readable_by_undeclared(scope, context)

    def _get_from_history(
        self, task_id: str, context: Any, *, include_results: bool,
        any_project: bool = False,
    ) -> dict[str, Any] | None:
        """由 EventStore 重建一筆舊 task 的唯讀視圖。

        回傳的 status 是唯讀判讀結果（`INTERRUPTED`／
        `OUTCOME_UNCONFIRMED`／`EXPIRED`／原終態），**不是**可執行狀態：
        `is_current_instance=False`，也沒有任何 worker 或 grant 可用。
        """
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader, interpret_restarted_task,
        )

        reader = TaskHistoryReader(self._deps.event_store)
        try:
            history = reader.load(task_id, member_id=context.member_id,
                                  project_id=context.project_id,
                                  any_project=any_project)
        except TaskEvidenceError as exc:
            return _error(E.TASK_EVIDENCE_INVALID, str(exc), task_id=task_id)
        if history is None:
            return None
        if any_project:
            if not self._history_readable_by_undeclared(history.task.scope, context):
                return None
            denial = self._read_access_denial(history.task.scope)
            if denial is not None:
                return denial

        status, reason = interpret_restarted_task(history)
        task = history.task
        steps: list[M.TaskStepView] = []
        for spec in task.steps:
            step_history = history.steps.get(spec.step_id)
            steps.append(M.TaskStepView(
                step_id=spec.step_id, capability_id=spec.capability_id,
                status=(step_history.status if step_history is not None else "PENDING"),
                arguments=dict(spec.arguments),
                result_event_id=(step_history.result_event_id
                                 if step_history is not None else None),
                result_sha256=(step_history.result_sha256
                               if step_history is not None else None),
            ))
        view = M.TaskView(
            task_id=task.task_id, task_digest=task.task_digest, status=status,
            template_id=task.template_id, current_run_id=task.current_run_id,
            baseline_run_id=task.baseline_run_id, question=task.question,
            created_at_ms=task.created_at_ms,
            draft_expires_at_ms=task.draft_expires_at_ms,
            project_id=task.scope.project_id, project_source=task.scope.project_source,
            steps=steps,
            daemon_instance_id=task.daemon_instance_id,
            is_current_instance=False, terminal_reason=reason,
            evidence_status=history.evidence_status,
            limitations=["這是重啟後對歷史事件的唯讀判讀，沒有可恢復的執行權。"],
        )
        payload: dict[str, Any] = {"status": "ok", "task": view.to_json_dict()}
        if include_results:
            payload["results"] = self._read_back_history_results(
                history, task, context)
        return payload

    def _get_flow_from_history(
        self, task_id: str, context: Any, *, include_results: bool,
        any_project: bool = False,
    ) -> dict[str, Any] | None:
        """由 EventStore 重建一筆歷史 **flow** task 的唯讀視圖。

        與 `_get_from_history` 的唯讀語義完全相同，只換成 flow 字彙：
        不重建 grant、不復活 registry、不寫任何資料，`is_current_instance`
        必為 False。任何驗證失敗都如實回報，不補預設值。
        """
        from terminux_engine.bounded_execution.analysis_tasks import flow_models as FM
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        reader = TaskHistoryReader(self._deps.event_store, vocabulary=FM)
        try:
            history = reader.load(task_id, member_id=context.member_id,
                                  project_id=context.project_id,
                                  any_project=any_project)
        except TaskEvidenceError as exc:
            return _error(E.TASK_EVIDENCE_INVALID, str(exc), task_id=task_id)
        if history is None:
            return None
        if any_project:
            if not self._history_readable_by_undeclared(history.task.scope, context):
                return None

        task = history.task
        terminal = history.terminal or {}
        steps: list[dict[str, Any]] = []
        for spec in task.steps:
            step_history = history.steps.get(spec.step_id)
            steps.append({
                "step_id": spec.step_id,
                "capability_id": spec.capability_id,
                "status": (step_history.status if step_history is not None
                           else "PENDING"),
                "result_event_id": (step_history.result_event_id
                                    if step_history is not None else None),
                "result_sha256": (step_history.result_sha256
                                  if step_history is not None else None),
            })
        view: dict[str, Any] = {
            "task_kind": "flow_task",
            "task_id": task.task_id,
            "task_digest": task.task_digest,
            "status": terminal.get("status") or "UNKNOWN",
            "template_id": task.template_id,
            "profile_id": getattr(task, "profile_id", None),
            "question": task.question,
            "created_at_ms": task.created_at_ms,
            "project_id": task.scope.project_id,
            "project_source": task.scope.project_source,
            "steps": steps,
            "daemon_instance_id": task.daemon_instance_id,
            "is_current_instance": False,
            "terminal": terminal,
            "terminal_reason": terminal.get("reason"),
            "evidence_status": history.evidence_status,
            "repair": history.repair,
            "repair_recipe": task.repair_recipe,
            **({"eap_proposal": dict(task.eap_proposal)}
               if getattr(task, "eap_proposal", None) is not None else {}),
            "limitations": [
                "這是重啟後對歷史事件的唯讀判讀，沒有可恢復的執行權。"],
        }
        feedback_events, _ = self._deps.event_store.query_task_events(
            task_id, user_id=task.scope.member_id, project_id=task.scope.project_id,
            event_types=[FM.EVENT_EAP_FEEDBACK], limit=1)
        if feedback_events:
            feedback = feedback_events[-1].payload.get("feedback")
            if isinstance(feedback, dict):
                view["eap_feedback"] = dict(feedback)
        payload: dict[str, Any] = {"status": "ok", "task": view}
        if include_results:
            payload["results"] = self._read_back_flow_history_results(
                history, context)
        return payload

    def _read_back_flow_history_results(
        self, history: Any, context: Any,
    ) -> list[dict[str, Any]]:
        """歷史 flow task 的逐步結果讀回。驗證規則與 live 路徑相同。"""
        from terminux_engine.bounded_execution.analysis_tasks import flow_models as FM
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        reader = TaskHistoryReader(self._deps.event_store, vocabulary=FM)
        results: list[dict[str, Any]] = []
        for step_id, state in sorted(history.steps.items()):
            entry: dict[str, Any] = {"step_id": step_id,
                                     "status": state.status}
            if not state.result_event_id or not state.result_sha256:
                entry["evidence_status"] = "no_persisted_result"
                results.append(entry)
                continue
            try:
                envelope = reader.load_result_envelope(
                    event_id=state.result_event_id,
                    expected_sha256=state.result_sha256,
                    task_id=history.task.task_id, step_id=step_id,
                    member_id=history.task.scope.member_id,
                    project_id=history.task.scope.project_id)
            except TaskEvidenceError as exc:
                entry["evidence_status"] = "invalid"
                entry["message"] = str(exc)
            else:
                entry["evidence_status"] = "verified"
                entry["envelope"] = envelope.to_json_dict()
            results.append(entry)
        return results

    def _read_back_history_results(
        self, history: Any, task: M.AnalysisTask, context: Any,
    ) -> list[dict[str, Any]]:
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        reader = TaskHistoryReader(self._deps.event_store)
        results: list[dict[str, Any]] = []
        for spec in task.steps:
            step_history = history.steps.get(spec.step_id)
            entry: dict[str, Any] = {"step_id": spec.step_id}
            if step_history is None or not step_history.result_event_id:
                entry["evidence_status"] = "no_persisted_result"
                results.append(entry)
                continue
            try:
                envelope = reader.load_result_envelope(
                    event_id=step_history.result_event_id,
                    expected_sha256=step_history.result_sha256,
                    task_id=task.task_id, step_id=spec.step_id,
                    member_id=task.scope.member_id, project_id=task.scope.project_id,
                )
            except TaskEvidenceError as exc:
                entry["evidence_status"] = "invalid"
                entry["message"] = str(exc)
            else:
                entry["evidence_status"] = "verified"
                entry["envelope"] = envelope.to_json_dict()
            results.append(entry)
        return results

    def _read_back_results(self, live: _LiveTask, context: Any) -> list[dict[str, Any]]:
        """由 EventStore 讀回**已驗證**的完整結果。

        只接受 hash 重算相符、且 scope 與 task 一致的 envelope；讀不到或驗
        不過就如實回報，不用 live 記憶體裡的內容補位（不在讀取時寫修復
        資料）。
        """
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        reader = TaskHistoryReader(self._deps.event_store)
        results: list[dict[str, Any]] = []
        for state in live.steps.values():
            entry: dict[str, Any] = {"step_id": state.spec.step_id,
                                     "status": state.status}
            if not state.result_event_id or not state.result_sha256:
                entry["evidence_status"] = "no_persisted_result"
                results.append(entry)
                continue
            try:
                envelope = reader.load_result_envelope(
                    event_id=state.result_event_id,
                    expected_sha256=state.result_sha256,
                    task_id=live.task.task_id, step_id=state.spec.step_id,
                    member_id=live.task.scope.member_id,
                    project_id=live.task.scope.project_id,
                )
            except TaskEvidenceError as exc:
                entry["evidence_status"] = "invalid"
                entry["error_code"] = getattr(exc, "code", E.TASK_EVIDENCE_INVALID)
                entry["message"] = str(exc)
            else:
                entry["evidence_status"] = "verified"
                entry["envelope"] = envelope.to_json_dict()
            results.append(entry)
        return results

    def _read_back_flow_results(
        self, live: _LiveFlowTask, context: Any,
    ) -> list[dict[str, Any]]:
        """由 EventStore 讀回**已驗證**的完整結果。規則同 analysis task。"""
        from terminux_engine.bounded_execution.analysis_tasks import flow_models as FM
        from terminux_engine.bounded_execution.analysis_tasks.reader import (
            TaskEvidenceError, TaskHistoryReader,
        )

        # flow task 寫的是 `flow_task.*.v1`；reader 預設的是 analysis task
        # 字彙，用錯會讓每一筆結果都被判成「referenced event is not a step
        # result」。驗證規則不變，只換型別名稱。
        reader = TaskHistoryReader(self._deps.event_store, vocabulary=FM)
        results: list[dict[str, Any]] = []
        for state in live.steps.values():
            entry: dict[str, Any] = {"step_id": state["step_id"],
                                     "status": state.get("status")}
            event_id = state.get("result_event_id")
            sha = state.get("result_sha256")
            if not event_id or not sha:
                entry["evidence_status"] = "no_persisted_result"
                results.append(entry)
                continue
            try:
                envelope = reader.load_result_envelope(
                    event_id=event_id, expected_sha256=sha,
                    task_id=live.task.task_id, step_id=state["step_id"],
                    # 以 task 固定的 scope 讀（存取權已由 accessible_by 判定）。
                    member_id=live.task.scope.member_id,
                    project_id=live.task.scope.project_id)
            except TaskEvidenceError as exc:
                entry["evidence_status"] = "invalid"
                entry["message"] = str(exc)
            else:
                entry["evidence_status"] = "verified"
                entry["envelope"] = envelope.to_json_dict()
            results.append(entry)
        return results

    def cancel_flow_task(self, task_id: str, task_digest: str) -> dict[str, Any]:
        """停止未派送步驟，並要求 Kernel 取消已知在途 run。

        **刻意不檢查** profile／provider／grant 過期——使用者不得因這些
        原因失去停止手段。
        """
        from terminux_engine.bounded_execution.analysis_tasks import profile_snapshot as PS

        context = self._deps.request_context()
        try:
            live = self.get_or_hydrate_flow_task(task_id, context)
        except PS.ProfileSnapshotError as exc:
            return _error(exc.code, exc.message, task_id=task_id)
        if live is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, "no such task in this scope")
        if live.task.task_digest != task_digest:
            return _error(E.TASK_DIGEST_MISMATCH, "the task digest does not match")
        with self._dependency_lock:
            with live.lock:
                if live.status in M.TERMINAL_TASK_STATUSES:
                    return {"status": "ok", "task": self._flow_view(live).to_json_dict()}
                # 先停止新 claim／materialization／LLM dispatch。
                waiting_dependency = live.status == F.STATUS_WAITING_DEPENDENCY
                live.stop = True
                live.startable = False
                had_dispatch = bool(live.dispatched)
                known_runs = list(live.runs.values())
                try:
                    self._writer.append(
                        event_type=F.EVENT_CANCEL_REQUESTED, task_id=live.task.task_id,
                        task_digest=live.task.task_digest,
                        daemon_instance_id=live.task.daemon_instance_id,
                        scope=live.task.scope, sequence=_next_sequence(live),
                        body={"requested_by_session_id": context.session_id,
                              "known_runs": [r.to_json_dict() for r in known_runs]})
                except TaskEventWriteError as exc:
                    live.persistence_status = "failed"
                    _LOG.warning("flow cancel record not persisted: %r", exc)
            # CORE cancel 的 durable stop 已先成立；在同一把接線鎖下取消已建立的
            # proof child，避免 completion path 同時恢復 s5。
            dependency_cancellations = self._cancel_dependency_proof_task(
                live.task.task_id) if waiting_dependency else []

        # 鎖外：由 Kernel 對已綁定 run 做 ownership-first cancel；再依本 slot
        # 的 cidfile 精確停止 container。
        cancel_results: list[dict[str, Any]] = []
        flow = self._deps.flow
        profile = live.profile
        if flow is not None and known_runs:
            for run_ref in known_runs:
                output_root = (
                    str(profile.slot_output_root(live.task.task_id, run_ref.slot))
                    if profile is not None
                    and live.task.template_id != F.PACKAGE_SINGLE_TOOL_TEMPLATE_ID
                    else None)
                if getattr(profile, "is_native", False):
                    cancel_results.append(flow.cancel_run(
                        run_ref, output_root=output_root, native=True))
                    continue
                docker = (getattr(getattr(profile, "execution", None),
                                  "docker_executable", None) or "docker")
                cancel_results.append(flow.cancel_run(
                    run_ref, output_root=output_root, docker_executable=docker))
        # 還在排隊的 task 直接移出 queue，從未啟動任何 child。
        was_queued = self._scheduler.cancel_queued(live.task.task_id)

        with live.lock:
            if live.status == "DRAFT":
                self._force_flow_terminal_locked(live, "CANCELLED",
                                                 "cancelled before start")
            elif live.status == "QUEUED" or was_queued:
                self._force_flow_terminal_locked(
                    live, "CANCELLED", "cancelled while queued; no child was started")
            elif had_dispatch and not waiting_dependency:
                live.status = "CANCEL_REQUESTED"
                live.terminal_reason = (
                    "cancel requested; claimed work may still be running")
            else:
                self._force_flow_terminal_locked(
                    live, "CANCELLED", "cancelled with no dispatched work")
            view = self._flow_view(live).to_json_dict()
        if not had_dispatch and live.holds_slot:
            self._release_slot_flow(live, True)
        return {"status": "ok", "task": view,
                "run_cancellations": cancel_results,
                "dependency_cancellations": dependency_cancellations}

    def cancel(self, task_id: str, task_digest: str) -> dict[str, Any]:
        """停止一筆 task。

        **刻意不檢查** access policy、provider readiness 或 grant 是否過期：
        使用者不得因為 policy 被撤除、漂移、provider 缺席或授權過期而失去
        停止手段。仍然檢查 kernel member、task 原 scope 與 digest。
        這個入口只停止原 task，不回傳越界完整結果、不賦予新執行權。
        """
        context = self._deps.request_context()
        if self._lookup_flow(task_id, context) is not None:
            return self.cancel_flow_task(task_id, task_digest)
        live = self._lookup(task_id, context)
        if live is None:
            return _error(E.TASK_NOT_FOUND_OR_OUT_OF_SCOPE, "no such task in this scope")
        if live.task.task_digest != task_digest:
            return _error(E.TASK_DIGEST_MISMATCH, "the task digest does not match")
        with live.lock:
            if live.status in M.TERMINAL_TASK_STATUSES:
                return {"status": "ok", "task": self._view(live).to_json_dict()}
            # 先設 live stop（禁止新 claim／LLM dispatch），再保存取消記錄。
            live.stop = True
            live.startable = False
            had_dispatch = bool(live.dispatched)
            try:
                self._writer.append_cancel_requested(
                    live.task, sequence=_next_sequence(live),
                    requested_by_session_id=context.session_id)
            except TaskEventWriteError as exc:
                # 保留 stop，回報 persistence failure；不撤回取消。
                live.persistence_status = "failed"
                _LOG.warning("cancel record not persisted: %r", exc)
            if live.status == "DRAFT":
                self._force_terminal_locked(live, "CANCELLED", "cancelled before start")
            elif had_dispatch:
                live.status = "CANCEL_REQUESTED"
                live.terminal_reason = "cancel requested; claimed work may still run"
            else:
                self._force_terminal_locked(live, "CANCELLED",
                                            "cancelled with no dispatched work")
            return {"status": "ok", "task": self._view(live).to_json_dict()}

    # ── 內部 ────────────────────────────────────────────────────────

    def _lookup(self, task_id: str, context: Any) -> _LiveTask | None:
        with self._tasks_lock:
            live = self._tasks.get(task_id)
        if live is None:
            return None
        # 明確宣告 project 的 session 比 member＋project；未宣告的只認
        # 自己建立的 task（origin session）。
        if not live.task.scope.accessible_by(context):
            return None
        return live

    @staticmethod
    def _budget_view(live: _LiveTask) -> dict[str, Any]:
        """三段分開顯示，不把 unknown 填零。"""
        used = live.budget.used if live.budget is not None else 0
        return {
            "body_llm_dispatch_limit": M.BODY_LLM_DISPATCHES,
            "body_llm_dispatches_used": used,
            # planning 階段的呼叫由 Agent turn 負責，task service 觀測不到。
            "planning_dispatches": None,
            # provider 端 usage 與 SDK retry 是否可觀測由 provider 決定。
            "provider_usage": "unknown",
            "sdk_retry_observable": None,
        }

    def _view(self, live: _LiveTask, *, run_access: Any = None) -> M.TaskView:
        task = live.task
        view = M.TaskView(
            task_id=task.task_id, task_digest=task.task_digest, status=live.status,
            template_id=task.template_id, current_run_id=task.current_run_id,
            baseline_run_id=task.baseline_run_id, question=task.question,
            created_at_ms=task.created_at_ms,
            draft_expires_at_ms=task.draft_expires_at_ms,
            project_id=task.scope.project_id, project_source=task.scope.project_source,
            steps=[
                M.TaskStepView(
                    step_id=s.spec.step_id, capability_id=s.spec.capability_id,
                    status=s.status, arguments=dict(s.spec.arguments),
                    execution_status=s.execution_status, action_status=s.action_status,
                    business_class=s.business_class,
                    result_event_id=s.result_event_id, result_sha256=s.result_sha256,
                    background_work_may_continue=s.background_work_may_continue,
                    reason=s.reason,
                )
                for s in live.steps.values()
            ],
            daemon_instance_id=task.daemon_instance_id,
            is_current_instance=task.daemon_instance_id == self._deps.daemon_instance_id,
            started_at_ms=live.started_at_ms,
            deadline_at_ms=live.grant.deadline_at_ms if live.grant else None,
            grant_expires_at_ms=live.grant.expires_at_ms if live.grant else None,
            terminal_reason=live.terminal_reason,
            persistence_status=live.persistence_status,
            budget=self._budget_view(live),
            queue=self._queue_view(live),
        )
        if run_access is not None:
            view.limitations = list(getattr(run_access, "unproven", ()) or ())
        return view


def _next_sequence(live: "_LiveTask | _LiveFlowTask") -> int:
    """委派給**唯一**的 allocator（`store.next_sequence`）。

    setup repair F-2：這裡曾與 `flow_worker` 各有一套語義（後遞增 vs 前遞增），
    結果是固定缺一號並在結尾撞號。集中成一份後不再有第二個計數器。
    """
    from terminux_engine.bounded_execution.analysis_tasks.store import (
        next_sequence,
    )

    return next_sequence(live)


class _StepBuildError(RuntimeError):
    """範本步驟無法對上目前 catalog。"""


def _legacy_docker_denial(profile: Any, task: Any) -> dict[str, Any] | None:
    """新執行若會走舊執行模式，回對應的停用狀態碼。

    Package 工具 task 看它自己的工具邊界（formal pair／ECO，舊 container 模式回
    `LEGACY_DOCKER_EXECUTION_DISABLED`）；其餘 flow task 看 Profile 的執行宣告
    （舊 container 模式或沒有 native tool binding 的舊 host runner）。只用在
    create／start 這類「要開始新執行」的邊界，讀取與取消不經過這裡。
    """
    from terminux_engine.orchestration import legacy_docker_execution as LDE

    proposal = getattr(task, "eap_proposal", None) if task is not None else None
    tool = proposal.get("package_tool") if isinstance(proposal, dict) else None
    if isinstance(tool, dict):
        from terminux_engine.bounded_execution.analysis_tasks.package_flow import (
            legacy_docker_execution_path,
        )

        path = legacy_docker_execution_path(tool)
        if path is None:
            return None
        return _error(E.LEGACY_DOCKER_EXECUTION_DISABLED, LDE.disabled_message(path),
                      execution_path=path)
    if profile is None:
        # 沒有 task-owned Profile 時由 preflight 如實拒絕，這裡不猜執行模式。
        return None
    denial = LDE.profile_execution_denial(profile)
    if denial is None:
        return None
    return _error(denial.code, denial.message, execution_path=denial.execution_path)


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error_code": code,
                               "message": message}
    payload.update(details)
    return payload


__all__ = ["AnalysisTaskService", "TaskServiceDeps"]
