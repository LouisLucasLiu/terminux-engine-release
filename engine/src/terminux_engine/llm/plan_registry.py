# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Stepwise Approved Action Plans。

daemon-memory-only、session-bound 的 `ActionPlan`——只是既有 evidence-first planner～C6
`ProposalRegistry` 的**編排層**，本身完全不新增任何執行授權：每一步的
proposal 建立、approve、one-shot execute，全部原封不動透過既有
`ProposalRegistry.create()`／既有 operator control path 完成。這個模組
唯一新增的能力是：

1. 追蹤一個最多 3 步、只能線性推進的 outline（沒有 DAG／branch／parallel
   ——資料結構本身就是一個 list，不存在表達分支的欄位）。
2. 保證同一時間只有一個 step 可以被 materialize 成真正的 proposal。
3. host-side 判斷「這一步的 tool execution 是否成功」與「這一步的
   business result 是否失敗」，兩者失敗任一個都立即把整個 plan 轉成
   `STOPPED`——不允許 LLM 覆蓋這個 stop 判斷。
4. 沒有任何 `APPROVE_PLAN`／`EXECUTE_PLAN` 能力：整條 plan 永遠不能被
   一次核准或一次執行，每一步仍必須各自走 evidence-first planner（propose）→ operator
   `/approve`→ operator `/execute`（evidence-first planner one-shot）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from terminux_engine.ipc.request_context import CanonicalRequestContext
from terminux_engine.llm import agent_audit
from terminux_engine.llm.proposal_registry import (
    ActionProposal,
    CandidateBinding,
    HandleBinding,
    ProposableCapability,
    ProposalCreationError,
    ProposalRegistry,
    ResultBinding,
    _bounded_str_list,
)

PlanStatus = Literal[
    "DRAFT", "ACTIVE", "WAITING_APPROVAL", "WAITING_RESULT_REVIEW",
    "COMPLETED", "STOPPED", "CANCELLED", "EXPIRED",
]

StepStatus = Literal[
    "PLANNED", "PROPOSAL_PENDING", "APPROVED", "EXECUTING",
    "EXECUTION_SUCCEEDED", "EXECUTION_FAILED",
    "BUSINESS_SUCCEEDED", "BUSINESS_FAILED", "SKIPPED", "CANCELLED",
]

#: v1：只有 top-level business status 落在這個集合時，才視為明確失敗，
#: host 立即 STOP，不徵詢 LLM 意見。大小寫不敏感比對。
_BUSINESS_FAILURE_STATUSES = frozenset({"failed", "error", "fatal", "blocked"})

#: 「非執行類 capability」——即使某個 catalog action 名稱恰好等於這個
#: 字面值也一律拒絕，避免任何 plan step 指向 core mutation tool。
_FORBIDDEN_STEP_ACTIONS = frozenset({"run_command"})

_PLAN_TTL_SEC = 60 * 60.0  # 60 分鐘
_MAX_ACTIVE_PLANS_PER_SESSION = 4
_MIN_STEPS = 1
_MAX_STEPS = 3
_MAX_LIST_ITEMS = 20
_MAX_OBJECTIVE_CHARS = 500
_MAX_REASON_CHARS = 500


class PlanCreationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class PlanStepOutline:
    """單一 step 的 outline + materialization 狀態。future step（尚未
    materialize）只保留描述性欄位（`action_name`/`purpose`/
    `prerequisites`/`expected_effects`/`stop_conditions`）——沒有
    `proposal_id`/`arguments` 之類的可執行資訊，天生不可能「預先核准」
    或「持有可執行 token」。"""

    step_index: int
    action_name: str
    purpose: str
    prerequisites: list[str] = field(default_factory=list)
    expected_effects: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    status: StepStatus = "PLANNED"
    #: 只有這個 step 被 materialize 過才會有值；每次 materialize 都是全新
    #: id/digest，不會延續前一步或前一次 revision 的任何 proposal。
    proposal_id: str | None = None
    proposal_digest: str | None = None
    result_id: str | None = None
    result_digest: str | None = None
    execution_status: str | None = None
    business_status: str | None = None

    def outline_view(self) -> dict[str, Any]:
        """只含 outline 欄位，供 digest 計算與 provider-facing 顯示用；
        不含任何 proposal/result 身分（那些是 materialize 之後才存在的
        host-only 狀態）。"""
        return {
            "step_index": self.step_index,
            "action_name": self.action_name,
            "purpose": self.purpose,
            "prerequisites": self.prerequisites,
            "expected_effects": self.expected_effects,
            "stop_conditions": self.stop_conditions,
        }

    def public_view(self) -> dict[str, Any]:
        view = self.outline_view()
        view.update({
            "status": self.status,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "result_id": self.result_id,
            "result_digest": self.result_digest,
            "execution_status": self.execution_status,
            "business_status": self.business_status,
        })
        return view


@dataclass
class ActionPlan:
    plan_id: str
    plan_digest: str
    revision: int
    objective: str
    member_id: str
    project_id: str
    session_id: str
    #: plan-level immutable input（例如：本次 plan 依據的
    #: orchestration_run_id 與說明）；`create_plan()` 建立後永不
    #: 修改，即使 REVISE 也不變（REVISE 只能改 future step outline）。
    immutable_inputs: dict[str, Any]
    evidence_references: list[str]
    steps: list[PlanStepOutline]
    #: 1-based；指向 `steps` 裡目前允許 materialize 的唯一 step。
    active_step_index: int
    created_at: float
    expires_at: float
    status: PlanStatus = "ACTIVE"

    def is_expired(self, now: float) -> bool:
        return self.status not in ("COMPLETED", "STOPPED", "CANCELLED", "EXPIRED") and now >= self.expires_at

    def active_step(self) -> PlanStepOutline | None:
        if 1 <= self.active_step_index <= len(self.steps):
            return self.steps[self.active_step_index - 1]
        return None

    def public_view(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "revision": self.revision,
            "objective": self.objective,
            "evidence_references": self.evidence_references[:_MAX_LIST_ITEMS],
            "steps": [s.public_view() for s in self.steps],
            "active_step_index": self.active_step_index,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


def compute_plan_digest(
    *, objective: str, steps: list[PlanStepOutline], member_id: str, project_id: str,
    session_id: str, revision: int, expires_at: float,
) -> str:
    payload = {
        "objective": objective,
        "steps": [s.outline_view() for s in steps],
        "member_id": member_id, "project_id": project_id, "session_id": session_id,
        "revision": revision, "expires_at": expires_at,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PlanStepDeclaration:
    """provider 對單一 step 的宣告——只有描述性欄位，沒有任何 argument。"""

    action_name: str
    purpose: str
    prerequisites: list[str] = field(default_factory=list)
    expected_effects: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)


class PlanRegistry:
    """daemon-memory-only plan orchestration layer。所有 transition 皆在
    單一 lock 內完成；呼叫既有 `ProposalRegistry` 的部分刻意在 lock 外
    進行（`ProposalRegistry` 自己有鎖），避免跨鎖死結。"""

    def __init__(self, proposal_registry: ProposalRegistry, clock: Any = time.time) -> None:
        self._proposal_registry = proposal_registry
        self._lock = threading.Lock()
        self._plans: dict[str, ActionPlan] = {}
        #: proposal_id -> (plan_id, step_index)；只有透過
        #: `materialize_step()` 建立的 proposal 才會出現在這裡。
        self._proposal_to_plan: dict[str, tuple[str, int]] = {}
        #: plan_id -> "success"|"indeterminate"；只有 plan 處於
        #: WAITING_RESULT_REVIEW 時才有意義，`apply_continuation()` 用
        #: 它限制允許的 decision 集合，決定後立即清除。
        self._classification: dict[str, str] = {}
        #: session_id -> plan_id；`/use-plan` 明確選擇，供 provider prompt
        #: 教學與自然語言 continuation 時判斷「使用者說的是哪個 plan」。
        #: 同一 session 若同時有多個 active plan 而使用者沒有明確
        #: `/use-plan`，host 不猜測，`_fetch_active_plan_context()` 之類
        #: 的呼叫端會回傳 None（fail-closed，不隱含選一個）。
        self._active_plan: dict[str, str] = {}
        self._clock = clock

    # ── 內部：TTL 惰性過期 ───────────────────────────────────────────────

    def _expire_stale_locked(self) -> None:
        now = self._clock()
        for plan in self._plans.values():
            if plan.is_expired(now):
                plan.status = "EXPIRED"
                self._classification.pop(plan.plan_id, None)

    @staticmethod
    def _scope_matches(plan: ActionPlan, context: CanonicalRequestContext) -> bool:
        return (
            plan.session_id == context.session_id
            and plan.member_id == context.member_id
            and plan.project_id == context.project_id
        )

    # ── plan 建立 ─────────────────────────────────────────────

    def create_plan(
        self,
        *,
        objective: str,
        steps: Iterable[PlanStepDeclaration],
        evidence_references: list[str],
        catalog: list[ProposableCapability],
        context: CanonicalRequestContext,
        immutable_inputs: dict[str, Any] | None = None,
    ) -> ActionPlan:
        """建立一筆新 plan；**不會建立任何 proposal，不會呼叫任何 action
        handler**。只驗證 outline 的結構合法性：

        1. 1～3 步（`_MIN_STEPS`～`_MAX_STEPS`）。
        2. 每一步的 `action_name` 必須存在於當前 turn 的 proposable
           catalog（等同 evidence-first planner 的既有限制——`run_command`／core mutation
           從不出現在這個 catalog 裡，天生排除）。
        3. `action_name` 額外對照 `_FORBIDDEN_STEP_ACTIONS` 黑名單，雙重
           保險。
        4. 資料結構本身是 `list[PlanStepOutline]`，依 `step_index`
           1..N 線性排列——沒有任何欄位可以表達依賴其他 step 以外的
           step，也沒有欄位可以表達「同時進行」，DAG/branch/parallel
           在這個 schema 下無法被表達，不需要額外執行期檢查。
        """
        steps_list = list(steps)
        if not (_MIN_STEPS <= len(steps_list) <= _MAX_STEPS):
            raise PlanCreationError("PLAN_STEP_COUNT_INVALID")
        catalog_names = {c.action_name for c in catalog}
        for decl in steps_list:
            if decl.action_name in _FORBIDDEN_STEP_ACTIONS:
                raise PlanCreationError("PLAN_STEP_ACTION_FORBIDDEN")
            if decl.action_name not in catalog_names:
                raise PlanCreationError("PLAN_STEP_ACTION_UNKNOWN")

        with self._lock:
            self._expire_stale_locked()
            active_count = sum(
                1 for p in self._plans.values()
                if p.session_id == context.session_id
                and p.status not in ("COMPLETED", "STOPPED", "CANCELLED", "EXPIRED")
            )
            if active_count >= _MAX_ACTIVE_PLANS_PER_SESSION:
                raise PlanCreationError("PLAN_ACTIVE_LIMIT_EXCEEDED")

            now = self._clock()
            expires_at = now + _PLAN_TTL_SEC
            step_outlines = [
                PlanStepOutline(
                    step_index=i + 1, action_name=decl.action_name, purpose=decl.purpose,
                    prerequisites=_bounded_str_list(decl.prerequisites),
                    expected_effects=_bounded_str_list(decl.expected_effects),
                    stop_conditions=_bounded_str_list(decl.stop_conditions),
                )
                for i, decl in enumerate(steps_list)
            ]
            plan_id = f"plan_{uuid.uuid4().hex}"
            objective_bounded = str(objective)[:_MAX_OBJECTIVE_CHARS]
            digest = compute_plan_digest(
                objective=objective_bounded, steps=step_outlines, member_id=context.member_id,
                project_id=context.project_id, session_id=context.session_id, revision=1,
                expires_at=expires_at,
            )
            plan = ActionPlan(
                plan_id=plan_id, plan_digest=digest, revision=1, objective=objective_bounded,
                member_id=context.member_id, project_id=context.project_id, session_id=context.session_id,
                immutable_inputs=dict(immutable_inputs or {}), evidence_references=list(evidence_references)[:_MAX_LIST_ITEMS],
                steps=step_outlines, active_step_index=1, created_at=now, expires_at=expires_at, status="ACTIVE",
            )
            self._plans[plan_id] = plan
            self._active_plan[context.session_id] = plan_id
        agent_audit.emit("action.plan.created", tool_call_id=plan_id, executor_entered=False,
                         result_status="ACTIVE", result_value={"step_count": len(step_outlines)})
        agent_audit.emit("action.plan.validated", tool_call_id=plan_id, executor_entered=False,
                         result_status="valid")
        return plan

    # ── step materialization ───────────────────────────────────

    def materialize_step(
        self,
        plan_id: str,
        plan_digest: str,
        context: CanonicalRequestContext,
        *,
        arguments: dict[str, Any],
        reason: str,
        expected_effects: Any = None,
        possible_side_effects: Any = None,
        preconditions: Any = None,
        evidence_references: list[str],
        catalog: list[ProposableCapability],
        result_bindings: list[ResultBinding] | None = None,
        handle_bindings: list[HandleBinding] | None = None,
        candidate_bindings: list[CandidateBinding] | None = None,
        active_result_ref: tuple[str, str] | None = None,
    ) -> ActionProposal:
        """只能 materialize 目前的 active step（`plan.active_step_index`
        指到的那一個），且只能在 `status == "ACTIVE"` 且該 step 尚未
        materialize（`status == "PLANNED"`）時呼叫一次；產生的 proposal
        完全透過既有 `ProposalRegistry.create()`——evidence-first planner～C6 的
        materialization／candidate 驗證規則原封不動套用，這裡不重新實作
        任何一段。materialize 成功後 plan 轉 `WAITING_APPROVAL`，
        operator 之後透過既有 `/approve`／`/execute` 走完整個 evidence-first planner/C2
        流程——`PlanRegistry` 自己完全不持有、也不能授予任何執行權。
        """
        with self._lock:
            self._expire_stale_locked()
            plan = self._plans.get(plan_id)
            if plan is None or not self._scope_matches(plan, context):
                raise PlanCreationError("PLAN_NOT_FOUND")
            if plan.plan_digest != plan_digest:
                raise PlanCreationError("PLAN_DIGEST_MISMATCH")
            if plan.status != "ACTIVE":
                raise PlanCreationError(f"PLAN_NOT_ACTIVE_{plan.status}")
            step = plan.active_step()
            if step is None:
                raise PlanCreationError("PLAN_NO_ACTIVE_STEP")
            if step.status != "PLANNED":
                raise PlanCreationError("PLAN_STEP_ALREADY_MATERIALIZED")
            action_name = step.action_name
            step_index = step.step_index

        # `ProposalRegistry.create()` 有自己的鎖；刻意在 PlanRegistry 的
        # lock 外呼叫，避免巢狀鎖。materialize 失敗（任何 ProposalCreationError）
        # 時 step/plan 狀態完全不變——不會留下半成品狀態。
        proposal = self._proposal_registry.create(
            action_name=action_name, arguments=arguments, reason=str(reason)[:_MAX_REASON_CHARS],
            expected_effects=expected_effects, possible_side_effects=possible_side_effects,
            preconditions=preconditions, evidence_references=evidence_references, catalog=catalog,
            context=context, result_bindings=result_bindings, handle_bindings=handle_bindings,
            candidate_bindings=candidate_bindings, active_result_ref=active_result_ref,
        )

        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                return proposal
            step = plan.steps[step_index - 1]
            step.status = "PROPOSAL_PENDING"
            step.proposal_id = proposal.proposal_id
            step.proposal_digest = proposal.proposal_digest
            plan.status = "WAITING_APPROVAL"
            self._proposal_to_plan[proposal.proposal_id] = (plan_id, step_index)
        agent_audit.emit("action.plan.step.materialized", tool_call_id=proposal.proposal_id,
                         tool_name=action_name, executor_entered=False, result_status="PENDING",
                         result_value={"plan_id": plan_id, "step_index": step_index,
                                       "proposal_digest_prefix": proposal.proposal_digest[:12]})
        return proposal

    def on_step_approved(self, proposal_id: str) -> None:
        """由呼叫端在既有 `/approve` 成功之後呼叫；只有 plan-bound proposal
        才有對應項目，其餘完全不受影響。"""
        with self._lock:
            link = self._proposal_to_plan.get(proposal_id)
            if link is None:
                return
            plan_id, step_index = link
            plan = self._plans.get(plan_id)
            if plan is None:
                return
            step = plan.steps[step_index - 1]
            step.status = "APPROVED"
        agent_audit.emit("action.plan.step.approved", tool_call_id=proposal_id, executor_entered=False,
                         result_value={"plan_id": plan_id, "step_index": step_index})

    def on_step_execution_started(self, proposal_id: str) -> None:
        """由呼叫端在 `claim_for_execution()` 成功、實際呼叫 handler 之前
        呼叫一次。"""
        with self._lock:
            link = self._proposal_to_plan.get(proposal_id)
            if link is None:
                return
            plan_id, step_index = link
            plan = self._plans.get(plan_id)
            if plan is None:
                return
            step = plan.steps[step_index - 1]
            step.status = "EXECUTING"
        agent_audit.emit("action.plan.step.started", tool_call_id=proposal_id, executor_entered=True,
                         result_value={"plan_id": plan_id, "step_index": step_index})

    # ── execution/business outcome hook ─────────────────────────

    def on_step_execution_finalized(
        self, proposal_id: str, execution_status: Literal["SUCCEEDED", "FAILED"],
        trusted_result_view: dict[str, Any] | None,
    ) -> None:
        """由呼叫端（`jsonrpc_server._execute_approved_proposal()`）在
        既有 evidence-first planner one-shot execution chain 完成後呼叫一次；只有透過
        `materialize_step()` 建立的 proposal 才會在 `_proposal_to_plan`
        裡有對應項目，其餘（非 plan 產生的）proposal 完全不影響任何
        plan 狀態。

        host guard（不可被 LLM 覆蓋，這裡是唯一決定 STOP 與否的地方）：

        - `execution_status != "SUCCEEDED"` → 立即 `EXECUTION_FAILED`／
          plan `STOPPED`。
        - execution 成功，但 trusted result 的 top-level `status`
          落在 `_BUSINESS_FAILURE_STATUSES` → `BUSINESS_FAILED`／plan
          `STOPPED`。
        - execution 成功，`status` 缺失／非字串／result
          `truncated`／不完整 → 不視為成功，也不視為失敗，plan 轉
          `WAITING_RESULT_REVIEW` 但内部標記為 indeterminate——之後
          `apply_continuation()` 只接受 `INDETERMINATE`／`STOP`。
        - execution 成功，`status` 是其他字串 → `BUSINESS_SUCCEEDED`，
          plan 轉 `WAITING_RESULT_REVIEW`，允許 Agent 之後提出
          `CONTINUE`/`COMPLETE`/`STOP`/`REVISE`。
        """
        emit_event: str | None = None
        emit_extra: dict[str, Any] = {}
        with self._lock:
            link = self._proposal_to_plan.get(proposal_id)
            if link is None:
                return
            plan_id, step_index = link
            plan = self._plans.get(plan_id)
            if plan is None:
                return
            step = plan.steps[step_index - 1]
            step.execution_status = execution_status
            if execution_status != "SUCCEEDED":
                step.status = "EXECUTION_FAILED"
                plan.status = "STOPPED"
                self._classification.pop(plan_id, None)
                emit_event = "action.plan.step.execution_failed"
            else:
                step.status = "EXECUTION_SUCCEEDED"
                if trusted_result_view is not None:
                    step.result_id = trusted_result_view.get("result_id")
                    step.result_digest = trusted_result_view.get("result_digest")
                business_status, classification = self._classify_business_result(trusted_result_view)
                step.business_status = business_status
                emit_extra = {"business_status": business_status}
                if classification == "failure":
                    step.status = "BUSINESS_FAILED"
                    plan.status = "STOPPED"
                    self._classification.pop(plan_id, None)
                    emit_event = "action.plan.step.business_failed"
                else:
                    if classification == "success":
                        step.status = "BUSINESS_SUCCEEDED"
                        emit_event = "action.plan.step.business_succeeded"
                    plan.status = "WAITING_RESULT_REVIEW"
                    self._classification[plan_id] = classification
        agent_audit.emit("action.plan.step.execution_succeeded" if execution_status == "SUCCEEDED"
                         else "action.plan.step.execution_failed",
                         tool_call_id=proposal_id, executor_entered=True,
                         result_value={"plan_id": plan_id, "step_index": step_index})
        if emit_event and emit_event != "action.plan.step.execution_failed":
            agent_audit.emit(emit_event, tool_call_id=proposal_id, executor_entered=True,
                             result_value={"plan_id": plan_id, "step_index": step_index, **emit_extra})
        if plan is not None and plan.status == "STOPPED":
            agent_audit.emit("action.plan.stopped", tool_call_id=plan_id, executor_entered=False,
                             result_value={"step_index": step_index, **emit_extra})

    @staticmethod
    def _classify_business_result(
        trusted_result_view: dict[str, Any] | None,
    ) -> tuple[str | None, Literal["success", "failure", "indeterminate"]]:
        if trusted_result_view is None:
            return None, "indeterminate"
        if not trusted_result_view.get("complete", False) or trusted_result_view.get("truncated", False):
            return None, "indeterminate"
        try:
            payload = json.loads(trusted_result_view.get("result_payload", "null"))
        except json.JSONDecodeError:
            return None, "indeterminate"
        if not isinstance(payload, dict):
            return None, "indeterminate"
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            return None, "indeterminate"
        if status.lower() in _BUSINESS_FAILURE_STATUSES:
            return status, "failure"
        return status, "success"

    # ── result-driven continuation ──────────────────────────────

    def apply_continuation(
        self, plan_id: str, plan_digest: str, context: CanonicalRequestContext,
        *, decision: str, revised_future_steps: list[PlanStepDeclaration] | None = None,
    ) -> tuple[ActionPlan | None, str | None]:
        """唯一能讓 plan 從 `WAITING_RESULT_REVIEW` 往前推進的入口；只接受
        host 已定義的 5 種 decision 之一。自然語言「繼續全部執行」之類的
        句子，一律先由 Agent/host 轉譯成這裡的一個 decision 值再呼叫，
        而且**這個呼叫本身只會 materialize 下一步的 proposal（如果
        decision 是 CONTINUE），絕不會執行任何東西**。"""
        with self._lock:
            self._expire_stale_locked()
            plan = self._plans.get(plan_id)
            if plan is None or not self._scope_matches(plan, context):
                return None, "PLAN_NOT_FOUND"
            if plan.plan_digest != plan_digest:
                return None, "PLAN_DIGEST_MISMATCH"
            if plan.status != "WAITING_RESULT_REVIEW":
                return None, f"PLAN_NOT_WAITING_RESULT_REVIEW_{plan.status}"
            classification = self._classification.get(plan_id, "indeterminate")
            if classification == "indeterminate" and decision not in ("INDETERMINATE", "STOP"):
                return None, "PLAN_CONTINUATION_REQUIRES_INDETERMINATE_OR_STOP"

            if decision == "STOP":
                plan.status = "STOPPED"
            elif decision == "COMPLETE":
                plan.status = "COMPLETED"
            elif decision == "INDETERMINATE":
                pass  # 維持 WAITING_RESULT_REVIEW，host 不自動前進
            elif decision == "CONTINUE":
                if plan.active_step_index >= len(plan.steps):
                    return None, "PLAN_NO_MORE_STEPS"
                plan.active_step_index += 1
                plan.status = "ACTIVE"
            elif decision == "REVISE":
                code = self._revise_locked(plan, revised_future_steps or [])
                if code is not None:
                    return None, code
            else:
                return None, "PLAN_CONTINUATION_DECISION_INVALID"

            self._classification.pop(plan_id, None)

        agent_audit.emit("action.plan.result_reviewed", tool_call_id=plan_id, executor_entered=False,
                         result_status=decision)
        event_by_decision = {
            "STOP": "action.plan.stopped", "COMPLETE": "action.plan.completed",
            "REVISE": "action.plan.revised",
        }.get(decision)
        if event_by_decision is not None:
            agent_audit.emit(event_by_decision, tool_call_id=plan_id, executor_entered=False,
                             result_value={"revision": plan.revision})
        return plan, None

    def _revise_locked(self, plan: ActionPlan, revised_future_steps: list[PlanStepDeclaration]) -> str | None:
        """只能修訂**尚未 materialize** 的 future step outline（`status
        == "PLANNED"`）；已完成的 step（`EXECUTION_SUCCEEDED`／
        `BUSINESS_SUCCEEDED`／`BUSINESS_FAILED`／`EXECUTION_FAILED`）
        完全不可變。任何 future step 若因架構問題已經有 proposal（正常
        流程下不會發生，因為 materialize 一次只作用在 active step），
        防禦性地一併 cancel 掉，絕不留下一筆孤兒 proposal 可以被之後
        誤用。"""
        future_start = plan.active_step_index  # 0-based index of the first not-yet-completed future step
        future_slots = plan.steps[future_start:]
        if len(revised_future_steps) != len(future_slots):
            return "PLAN_REVISION_STEP_COUNT_MISMATCH"
        for decl in revised_future_steps:
            if decl.action_name in _FORBIDDEN_STEP_ACTIONS:
                return "PLAN_STEP_ACTION_FORBIDDEN"

        for offset, (old_step, decl) in enumerate(zip(future_slots, revised_future_steps)):
            if old_step.proposal_id is not None and old_step.status in ("PROPOSAL_PENDING", "APPROVED"):
                self._proposal_registry.reject(old_step.proposal_id, old_step.proposal_digest or "", CanonicalRequestContext(
                    session_id=plan.session_id, member_id=plan.member_id, project_id=plan.project_id,
                ))
            plan.steps[future_start + offset] = PlanStepOutline(
                step_index=old_step.step_index, action_name=decl.action_name, purpose=decl.purpose,
                prerequisites=_bounded_str_list(decl.prerequisites),
                expected_effects=_bounded_str_list(decl.expected_effects),
                stop_conditions=_bounded_str_list(decl.stop_conditions),
                status="CANCELLED" if old_step.proposal_id is not None else "PLANNED",
            )
            if plan.steps[future_start + offset].status == "CANCELLED":
                # 取消動作只是記錄舊 slot 的下場；新 outline 本身仍是待
                # materialize 的 PLANNED 狀態，不應該一併被標成 CANCELLED。
                plan.steps[future_start + offset].status = "PLANNED"

        plan.revision += 1
        plan.active_step_index += 1
        plan.status = "ACTIVE"
        plan.plan_digest = compute_plan_digest(
            objective=plan.objective, steps=plan.steps, member_id=plan.member_id,
            project_id=plan.project_id, session_id=plan.session_id, revision=plan.revision,
            expires_at=plan.expires_at,
        )
        return None

    # ── control path（不得由 provider 呼叫）─────────────────────

    def list_for_scope(self, context: CanonicalRequestContext) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_stale_locked()
            return [
                p.public_view() for p in sorted(self._plans.values(), key=lambda p: p.created_at)
                if self._scope_matches(p, context)
            ]

    def get_for_scope(self, plan_id: str, context: CanonicalRequestContext) -> dict[str, Any] | None:
        with self._lock:
            self._expire_stale_locked()
            plan = self._plans.get(plan_id)
            if plan is None or not self._scope_matches(plan, context):
                return None
            return plan.public_view()

    def set_active_plan(self, plan_id: str, context: CanonicalRequestContext) -> bool:
        with self._lock:
            self._expire_stale_locked()
            plan = self._plans.get(plan_id)
            if plan is None or not self._scope_matches(plan, context):
                return False
            self._active_plan[context.session_id] = plan_id
            return True

    def peek_active_plan(self, context: CanonicalRequestContext) -> dict[str, Any] | None:
        with self._lock:
            self._expire_stale_locked()
            plan_id = self._active_plan.get(context.session_id)
            if plan_id is None:
                return None
            plan = self._plans.get(plan_id)
            if plan is None or not self._scope_matches(plan, context):
                return None
            return plan.public_view()

    def clear_active_plan(self, context: CanonicalRequestContext) -> None:
        with self._lock:
            self._active_plan.pop(context.session_id, None)

    def cancel_plan(self, plan_id: str, plan_digest: str, context: CanonicalRequestContext) -> tuple[dict[str, Any] | None, str | None]:
        with self._lock:
            self._expire_stale_locked()
            plan = self._plans.get(plan_id)
            if plan is None or not self._scope_matches(plan, context):
                return None, "PLAN_NOT_FOUND"
            if plan.plan_digest != plan_digest:
                return None, "PLAN_DIGEST_MISMATCH"
            if plan.status in ("COMPLETED", "STOPPED", "CANCELLED", "EXPIRED"):
                return None, f"PLAN_NOT_CANCELLABLE_{plan.status}"
            step = plan.active_step()
            if step is not None and step.proposal_id is not None and step.status in ("PROPOSAL_PENDING", "APPROVED"):
                self._proposal_registry.reject(step.proposal_id, step.proposal_digest or "", context)
                step.status = "CANCELLED"
            plan.status = "CANCELLED"
            self._classification.pop(plan_id, None)
        agent_audit.emit("action.plan.cancelled", tool_call_id=plan_id, executor_entered=False)
        return plan.public_view(), None
