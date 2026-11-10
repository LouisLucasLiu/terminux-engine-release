# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AnalysisTask v1 的固定資料模型。

這一層只認識結構、scope、hash、狀態與授權。它**不認識** ORFS、WNS/TNS、
comparability 或任何 EDA 語義——那些留在 `packages/orfs_run_intelligence/`
的範本與 summary。

與既有物件的關係（不得混用）：

- plan-only planner `planning.Plan`：不可執行的建議，本模組不讀它、不轉換它。
- `llm.plan_registry.ActionPlan`：daemon-memory-only 的逐步核准 outline，
  狀態 enum 完全獨立，本模組不修改它。
- `AnalysisTask`：固定兩步、一次根授權、非阻塞執行。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from terminux_engine.bounded_execution.analysis_tasks.canonical import (
    canonical_size,
    now_ms,
    sha256_payload,
)

SCHEMA_VERSION = 1
#: task snapshot 的 schema 版本。v2 起**沒有**草案
#: 期限（不再寫 `draft_expires_at_ms`，也不再據以拒絕核准）。v1 snapshot
#: 仍可讀，且其 digest 材料與當年位元等價（見 `_digest_material`）。
TASK_SNAPSHOT_SCHEMA_VERSION = 2

#: 固定值。草案沒有期限，不因時間經過失效。
AUTHORIZATION_TTL_SEC = 600
TASK_DEADLINE_SEC = 600
MAX_STEPS = 2
RUNTIME_RETRIES = 0
BODY_LLM_DISPATCHES = 3
MAX_RESULT_BYTES = 1_048_576  # 1 MiB，含 hash 欄位的完整 canonical envelope

MAX_QUESTION_CHARS = 4096

#: event types。EventStore 只 append，不改 schema。
EVENT_SNAPSHOT = "analysis_task.snapshot.v1"
EVENT_GRANT = "analysis_task.grant.v1"
EVENT_STARTED = "analysis_task.started.v1"
EVENT_STEP_INTENT = "analysis_task.step_intent.v1"
EVENT_STEP_RESULT = "analysis_task.step_result.v1"
EVENT_STEP_RECEIPT = "analysis_task.step_receipt.v1"
EVENT_CANCEL_REQUESTED = "analysis_task.cancel_requested.v1"
EVENT_TERMINAL = "analysis_task.terminal.v1"

TASK_EVENT_TYPES: tuple[str, ...] = (
    EVENT_SNAPSHOT, EVENT_GRANT, EVENT_STARTED, EVENT_STEP_INTENT,
    EVENT_STEP_RESULT, EVENT_STEP_RECEIPT, EVENT_CANCEL_REQUESTED, EVENT_TERMINAL,
)

# ── 狀態。新 task 專屬，不修改舊 plan enum ────────────────

TaskStatus = Literal[
    "DRAFT", "QUEUED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "FAILED",
    "BLOCKED", "CANCELLED", "OUTCOME_UNCONFIRMED", "INTERRUPTED", "EXPIRED",
]

TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({
    "COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "OUTCOME_UNCONFIRMED",
    "INTERRUPTED", "EXPIRED",
})

StepStatus = Literal[
    "PENDING", "DISPATCH_INTENT_RECORDED", "CLAIMED", "SUCCEEDED", "FAILED",
    "DENIED", "SKIPPED", "OUTCOME_UNCONFIRMED",
]

#: Package 摘要給出的業務分類。Common 只用它決定要不要派下一步。
BusinessClass = Literal["valid", "failed", "indeterminate"]

#: 結構化 execution outcome。**不從舊 proposal enum 反推。**
ExecutionOutcomeStatus = Literal["succeeded", "failed", "denied", "unconfirmed"]

#: 本地 action future 是否確認返回。與 provider 端不可觀測的工作分開。
WorkerCompletion = Literal["confirmed", "pending", "unknown"]

#: task-owned proposal 的授權來源標記。
AUTHORIZATION_SOURCE_TASK = "task_authorization"


def new_task_id() -> str:
    return f"atask_{uuid.uuid4().hex}"


def new_grant_id() -> str:
    return f"tgrant_{uuid.uuid4().hex}"


def new_daemon_instance_id() -> str:
    """daemon 啟動時產生一次。**不以 PID 或時間戳替代**。"""
    return f"dinst_{uuid.uuid4().hex}"


# ── Scope ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskScope:
    """server context 的 scope。全部由 host 的 CanonicalRequestContext 決定，
    caller 不能提供任何一欄。"""

    member_id: str
    project_id: str
    origin_session_id: str
    workspace: str
    #: `project_id` 的來源。``session``＝session 明確宣告
    #: （舊 task 一律如此）；``profile``＝session 未宣告、由 Profile 決定；``unset``
    #: ＝session 未宣告且沒有 Profile 可推導（analysis task）。additive：只有
    #: 非 ``session`` 時才序列化，舊 digest 材料一個位元都不變。
    project_source: str = "session"

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "member_id": self.member_id,
            "project_id": self.project_id,
            "origin_session_id": self.origin_session_id,
            "workspace": self.workspace,
        }
        if self.project_source != "session":
            payload["project_source"] = self.project_source
        return payload

    def owner_matches(self, member_id: str, project_id: str) -> bool:
        """讀取／取消的 scope 判定：同 owner/project 即可，不要求同 session。

        `origin_session_id` 只保留 provenance。
        """
        return self.member_id == member_id and self.project_id == project_id

    def accessible_by(self, context: Any) -> bool:
        """依 caller 的 canonical context 判定可否存取這筆 task。

        - session **明確宣告** project（含 worker thread 的 task 專用 context）：
          沿用 `owner_matches`（同 member ＋ 同 project）。
        - session **未宣告** project：沒有 project scope 可比對，只允許**建立
          這筆 task 的 session**（origin session）存取——不是跨專案萬用權限。
        member 不符一律拒絕。
        """
        if self.member_id != getattr(context, "member_id", None):
            return False
        if getattr(context, "project_declared", True):
            return self.project_id == context.project_id
        return self.origin_session_id == getattr(context, "session_id", None)

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "TaskScope":
        return cls(
            member_id=str(payload["member_id"]),
            project_id=str(payload["project_id"]),
            origin_session_id=str(payload["origin_session_id"]),
            workspace=str(payload["workspace"]),
            project_source=str(payload.get("project_source") or "session"),
        )


# ── 存取綁定─────────────────────────────────────────

#: `access_binding` 的版本。additive 欄位，不改既有事件資料。
ACCESS_BINDING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TaskAccessBinding:
    """建立 task 當下、server policy 授權範圍的不可變快照。

    它記錄「這筆 task 是依哪一份 policy、對哪兩筆 run 的哪份 manifest
    授權的」。納入 `task_digest`；每步派送前按相同 policy 重驗。

    這**不是**歷史歸屬宣稱：它只表示當時的分析存取政策。
    """

    schema_version: int
    policy_id: str
    policy_digest: str
    member_id: str
    project_id: str
    dqe_db_path: str
    artifact_root: str
    #: `{run_id: manifest_sha256}`，兩筆。
    run_manifests: dict[str, str]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "member_id": self.member_id,
            "project_id": self.project_id,
            "dqe_db_path": self.dqe_db_path,
            "artifact_root": self.artifact_root,
            "run_manifests": dict(sorted(self.run_manifests.items())),
        }

    def binding_digest(self) -> str:
        return sha256_payload(self.to_json_dict())

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "TaskAccessBinding":
        return cls(
            schema_version=int(payload["schema_version"]),
            policy_id=str(payload["policy_id"]),
            policy_digest=str(payload["policy_digest"]),
            member_id=str(payload["member_id"]),
            project_id=str(payload["project_id"]),
            dqe_db_path=str(payload["dqe_db_path"]),
            artifact_root=str(payload["artifact_root"]),
            run_manifests=dict(payload["run_manifests"]),
        )


# ── Step 定義（範本產生，host 固定）────────────────────────────────────


@dataclass(frozen=True)
class TaskStepSpec:
    """一個固定步驟。arguments 在建立 task 時就完全確定，核准後不再改變。"""

    step_id: str
    capability_id: str
    arguments: dict[str, Any]
    arguments_sha256: str
    capability_contract_sha256: str
    catalog_snapshot_id: str
    risk: str
    approval_required: bool
    manifest_timeout_sec: float | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "capability_id": self.capability_id,
            "arguments": dict(self.arguments),
            "arguments_sha256": self.arguments_sha256,
            "capability_contract_sha256": self.capability_contract_sha256,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "risk": self.risk,
            "approval_required": self.approval_required,
            "manifest_timeout_sec": self.manifest_timeout_sec,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "TaskStepSpec":
        return cls(
            step_id=str(payload["step_id"]),
            capability_id=str(payload["capability_id"]),
            arguments=dict(payload["arguments"]),
            arguments_sha256=str(payload["arguments_sha256"]),
            capability_contract_sha256=str(payload["capability_contract_sha256"]),
            catalog_snapshot_id=str(payload["catalog_snapshot_id"]),
            risk=str(payload["risk"]),
            approval_required=bool(payload["approval_required"]),
            manifest_timeout_sec=payload.get("manifest_timeout_sec"),
        )


# ── Task snapshot ──────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class AnalysisTask:
    """Immutable task snapshot。同一 `task_id` 下**不得** revise 任何欄位；
    要改就建立新草案並重新授權。

    `draft_expires_at_ms`：v1 snapshot 的歷史欄位。**只供讀取舊資料**：有值就
    原樣保留並照舊 digest 規則計算；v2 起為 `None`、不進 digest、任何程式碼
    都不再用它判草案是否過期。
    """

    schema_version: int
    task_id: str
    daemon_instance_id: str
    template_id: str
    scope: TaskScope
    current_run_id: str
    baseline_run_id: str
    question: str
    created_at_ms: int
    draft_expires_at_ms: int | None = None
    authorization_ttl_sec: int
    task_deadline_sec: int
    limits: dict[str, Any]
    steps: tuple[TaskStepSpec, ...]
    task_digest: str
    #: additive。舊 task（含既有歷史）沒有這一欄，讀回時
    #: 為 `None`——**不重算舊 digest 冒充新授權**，也不因此獲得執行權。
    access_binding: TaskAccessBinding | None = None

    def step(self, step_id: str) -> TaskStepSpec | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def to_json_dict(self) -> dict[str, Any]:
        payload = self._digest_material()
        payload["task_digest"] = self.task_digest
        return payload

    def _digest_material(self) -> dict[str, Any]:
        material: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "daemon_instance_id": self.daemon_instance_id,
            "template_id": self.template_id,
            "scope": self.scope.to_json_dict(),
            "current_run_id": self.current_run_id,
            "baseline_run_id": self.baseline_run_id,
            "question": self.question,
            "created_at_ms": self.created_at_ms,
            # v1 snapshot 帶 `draft_expires_at_ms` ⇒ 照原位置、原鍵序
            # 進 digest（舊 digest 不變）；v2 沒有這一欄 ⇒ 不出現在材料裡。
            **({"draft_expires_at_ms": self.draft_expires_at_ms}
               if self.draft_expires_at_ms is not None else {}),
            "authorization_ttl_sec": self.authorization_ttl_sec,
            "task_deadline_sec": self.task_deadline_sec,
            "limits": dict(self.limits),
            "steps": [s.to_json_dict() for s in self.steps],
        }
        # additive：只有帶 access_binding 的新 task 才把它算進 digest。
        # 沒有這一欄的舊 snapshot 其 digest 材料與 v0.1 位元等價。
        if self.access_binding is not None:
            material["access_binding"] = self.access_binding.to_json_dict()
        return material

    def recompute_digest(self) -> str:
        """由 snapshot 除 task_digest 外的全部欄位重算。"""
        return sha256_payload(self._digest_material())

    def digest_matches(self) -> bool:
        return self.recompute_digest() == self.task_digest

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "AnalysisTask":
        return cls(
            schema_version=int(payload["schema_version"]),
            task_id=str(payload["task_id"]),
            daemon_instance_id=str(payload["daemon_instance_id"]),
            template_id=str(payload["template_id"]),
            scope=TaskScope.from_json_dict(payload["scope"]),
            current_run_id=str(payload["current_run_id"]),
            baseline_run_id=str(payload["baseline_run_id"]),
            question=str(payload["question"]),
            created_at_ms=int(payload["created_at_ms"]),
            draft_expires_at_ms=(int(payload["draft_expires_at_ms"])
                                 if payload.get("draft_expires_at_ms") is not None
                                 else None),
            authorization_ttl_sec=int(payload["authorization_ttl_sec"]),
            task_deadline_sec=int(payload["task_deadline_sec"]),
            limits=dict(payload["limits"]),
            steps=tuple(TaskStepSpec.from_json_dict(s) for s in payload["steps"]),
            task_digest=str(payload["task_digest"]),
            access_binding=(
                TaskAccessBinding.from_json_dict(payload["access_binding"])
                if payload.get("access_binding") is not None else None
            ),
        )


def default_limits() -> dict[str, Any]:
    return {
        "max_steps": MAX_STEPS,
        "runtime_retries": RUNTIME_RETRIES,
        "body_llm_dispatches": BODY_LLM_DISPATCHES,
        "result_bytes": MAX_RESULT_BYTES,
    }


def build_task_snapshot(
    *,
    task_id: str,
    daemon_instance_id: str,
    template_id: str,
    scope: TaskScope,
    current_run_id: str,
    baseline_run_id: str,
    question: str,
    steps: list[TaskStepSpec] | tuple[TaskStepSpec, ...],
    created_at_ms: int | None = None,
    access_binding: TaskAccessBinding | None = None,
) -> AnalysisTask:
    """建立 snapshot 並就地算出 `task_digest`。"""
    created = now_ms() if created_at_ms is None else int(created_at_ms)
    partial = AnalysisTask(
        schema_version=TASK_SNAPSHOT_SCHEMA_VERSION, task_id=task_id,
        daemon_instance_id=daemon_instance_id, template_id=template_id,
        scope=scope, current_run_id=current_run_id, baseline_run_id=baseline_run_id,
        question=question, created_at_ms=created,
        # 新草案沒有期限（None ⇒ 不寫入、不進 digest）。
        draft_expires_at_ms=None,
        authorization_ttl_sec=AUTHORIZATION_TTL_SEC,
        task_deadline_sec=TASK_DEADLINE_SEC, limits=default_limits(),
        steps=tuple(steps), task_digest="", access_binding=access_binding,
    )
    return replace(partial, task_digest=partial.recompute_digest())


# ── 根授權 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskAuthorizationGrant:
    """host 在 `start` 全部驗證通過後產生的根授權。

    **只存在於目前 daemon 的記憶體**；持久化的 `analysis_task.grant.v1`
    只是「曾經核准」的歷史，不授予新 daemon instance 執行權。
    """

    grant_id: str
    task_id: str
    task_digest: str
    daemon_instance_id: str
    scope: TaskScope
    #: 核准當下的 actor（= start 的 caller session）。
    approved_by_session_id: str
    approved_at_ms: int
    expires_at_ms: int
    #: process-local monotonic 期限；牆上時鐘回撥不得延長授權。
    expires_at_monotonic: float
    deadline_at_ms: int
    deadline_at_monotonic: float
    body_llm_dispatches: int

    def to_event_payload(self) -> dict[str, Any]:
        """持久化用：**不含** monotonic 欄位（跨 process 無意義）。"""
        return {
            "grant_id": self.grant_id,
            "task_digest": self.task_digest,
            "approved_by_session_id": self.approved_by_session_id,
            "approved_at_ms": self.approved_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "deadline_at_ms": self.deadline_at_ms,
            "body_llm_dispatches": self.body_llm_dispatches,
        }


# ── 本步 binding ──────────────────────────────────────────────


@dataclass(frozen=True)
class TaskStepBinding:
    """host 由根授權派生的本步授權。記錄 root grant 與實際 arguments 身分。"""

    task_id: str
    step_id: str
    grant_id: str
    capability_id: str
    arguments_sha256: str
    capability_contract_sha256: str
    catalog_snapshot_id: str
    risk: str
    scope: TaskScope
    proposal_id: str | None = None
    proposal_digest: str | None = None
    authorization_source: str = AUTHORIZATION_SOURCE_TASK
    #: 本步綁定的 access_binding digest。
    access_binding_digest: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "grant_id": self.grant_id,
            "capability_id": self.capability_id,
            "arguments_sha256": self.arguments_sha256,
            "capability_contract_sha256": self.capability_contract_sha256,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "risk": self.risk,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "authorization_source": self.authorization_source,
            "access_binding_digest": self.access_binding_digest,
        }


# ── 執行結果 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskActionOutcome:
    """一次 action dispatch 的結構化結果。欄位固定。

    `execution_status` 與 `timed_out`／`worker_completion` 是不同的軸：
    timeout **不是** 已知失敗，它是「不知道」。
    """

    execution_status: ExecutionOutcomeStatus
    timed_out: bool
    worker_completion: WorkerCompletion
    full_result: Any
    action_status: str | None
    error_code: str | None
    package_record_refs: tuple[str, ...] = ()

    @property
    def is_confirmed_failure(self) -> bool:
        """確定已返回且失敗。timeout／unconfirmed **不算**。"""
        return self.execution_status in ("failed", "denied") and not self.timed_out

    @property
    def is_unconfirmed(self) -> bool:
        return self.execution_status == "unconfirmed" or self.timed_out

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "execution_status": self.execution_status,
            "timed_out": self.timed_out,
            "worker_completion": self.worker_completion,
            "action_status": self.action_status,
            "error_code": self.error_code,
            "package_record_refs": list(self.package_record_refs),
        }


# ── 完整結果 envelope ─────────────────────────────────────────


@dataclass(frozen=True)
class TaskResultEnvelope:
    """`analysis_task.step_result.v1` 的 envelope。

    `result_sha256` 對「除 result_sha256 外的完整 envelope」計算，**不含**
    EventStore 的儲存包裝。完整 canonical envelope（含 hash）最多 1 MiB。
    """

    task_id: str
    step_id: str
    proposal_id: str | None
    grant_id: str
    capability_id: str
    arguments_sha256: str
    execution_status: str
    action_status: str | None
    business_class: BusinessClass | None
    full_result: Any
    package_record_refs: tuple[str, ...]
    result_sha256: str

    def _material(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "proposal_id": self.proposal_id,
            "grant_id": self.grant_id,
            "capability_id": self.capability_id,
            "arguments_sha256": self.arguments_sha256,
            "execution_status": self.execution_status,
            "action_status": self.action_status,
            "business_class": self.business_class,
            "full_result": self.full_result,
            "package_record_refs": list(self.package_record_refs),
        }

    def to_json_dict(self) -> dict[str, Any]:
        payload = self._material()
        payload["result_sha256"] = self.result_sha256
        return payload

    def recompute_sha256(self) -> str:
        return sha256_payload(self._material())

    def canonical_size(self) -> int:
        return canonical_size(self.to_json_dict())

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "TaskResultEnvelope":
        return cls(
            task_id=str(payload["task_id"]),
            step_id=str(payload["step_id"]),
            proposal_id=payload.get("proposal_id"),
            grant_id=str(payload["grant_id"]),
            capability_id=str(payload["capability_id"]),
            arguments_sha256=str(payload["arguments_sha256"]),
            execution_status=str(payload["execution_status"]),
            action_status=payload.get("action_status"),
            business_class=payload.get("business_class"),
            full_result=payload.get("full_result"),
            package_record_refs=tuple(payload.get("package_record_refs") or []),
            result_sha256=str(payload["result_sha256"]),
        )


def build_result_envelope(
    *,
    task_id: str,
    step_id: str,
    proposal_id: str | None,
    grant_id: str,
    capability_id: str,
    arguments_sha256: str,
    execution_status: str,
    action_status: str | None,
    business_class: BusinessClass | None,
    full_result: Any,
    package_record_refs: tuple[str, ...] | list[str],
) -> TaskResultEnvelope:
    """建立 envelope 並算出 `result_sha256`。

    呼叫端**必須**先用 `canonical_size()` 驗大小上限；本函式不裁切內容，
    超限時由呼叫端寫小型失敗 receipt。
    """
    partial = TaskResultEnvelope(
        task_id=task_id, step_id=step_id, proposal_id=proposal_id, grant_id=grant_id,
        capability_id=capability_id, arguments_sha256=arguments_sha256,
        execution_status=execution_status, action_status=action_status,
        business_class=business_class, full_result=full_result,
        package_record_refs=tuple(package_record_refs), result_sha256="",
    )
    return replace(partial, result_sha256=partial.recompute_sha256())


# ── 對外 view ─────────────────────────────────────────────────


@dataclass
class TaskStepView:
    step_id: str
    capability_id: str
    status: StepStatus
    arguments: dict[str, Any]
    execution_status: str | None = None
    action_status: str | None = None
    business_class: str | None = None
    result_event_id: str | None = None
    result_sha256: str | None = None
    background_work_may_continue: bool = False
    reason: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "capability_id": self.capability_id,
            "status": self.status,
            "arguments": dict(self.arguments),
            "execution_status": self.execution_status,
            "action_status": self.action_status,
            "business_class": self.business_class,
            "result_event_id": self.result_event_id,
            "result_sha256": self.result_sha256,
            "background_work_may_continue": self.background_work_may_continue,
            "reason": self.reason,
        }


@dataclass
class TaskView:
    """`create`／`start`／`get`／`cancel` 共用的對外 view。

    `persistence_status` 明示終態是否已 durable。
    """

    task_id: str
    task_digest: str
    status: TaskStatus
    template_id: str
    current_run_id: str
    baseline_run_id: str
    question: str
    created_at_ms: int
    #: 歷史欄位：只有 v1 snapshot 有值；新草案為 None（沒有核准期限）。
    draft_expires_at_ms: int | None = None
    #: task 固定的有效 project 與來源（session／profile／unset）。
    project_id: str | None = None
    project_source: str | None = None
    steps: list[TaskStepView] = field(default_factory=list)
    daemon_instance_id: str | None = None
    is_current_instance: bool = False
    started_at_ms: int | None = None
    deadline_at_ms: int | None = None
    grant_expires_at_ms: int | None = None
    terminal_reason: str | None = None
    persistence_status: str = "durable"
    budget: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    evidence_status: str = "ok"
    #: QUEUED 時的排隊位置／原因；其他狀態為 None（不輸出）。
    queue: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            **({"queue": dict(self.queue)} if self.queue is not None else {}),
            "task_id": self.task_id,
            "task_digest": self.task_digest,
            "status": self.status,
            "template_id": self.template_id,
            "current_run_id": self.current_run_id,
            "baseline_run_id": self.baseline_run_id,
            "question": self.question,
            "created_at_ms": self.created_at_ms,
            "draft_expires_at_ms": self.draft_expires_at_ms,
            "project_id": self.project_id,
            "project_source": self.project_source,
            "steps": [s.to_json_dict() for s in self.steps],
            "daemon_instance_id": self.daemon_instance_id,
            "is_current_instance": self.is_current_instance,
            "started_at_ms": self.started_at_ms,
            "deadline_at_ms": self.deadline_at_ms,
            "grant_expires_at_ms": self.grant_expires_at_ms,
            "terminal_reason": self.terminal_reason,
            "persistence_status": self.persistence_status,
            "budget": dict(self.budget),
            "limitations": list(self.limitations),
            "evidence_status": self.evidence_status,
        }



#: reader 的字彙介面：這個 vocabulary 的 snapshot 由哪個型別解讀。
TASK_SNAPSHOT_TYPE = AnalysisTask
#: analysis task 的 snapshot 放在 body 的這個欄位。
TASK_SNAPSHOT_BODY_KEY = "snapshot"

__all__ = [
    "AUTHORIZATION_SOURCE_TASK",
    "AUTHORIZATION_TTL_SEC",
    "BODY_LLM_DISPATCHES",
    "EVENT_CANCEL_REQUESTED",
    "EVENT_GRANT",
    "EVENT_SNAPSHOT",
    "EVENT_STARTED",
    "EVENT_STEP_INTENT",
    "EVENT_STEP_RECEIPT",
    "EVENT_STEP_RESULT",
    "EVENT_TERMINAL",
    "MAX_QUESTION_CHARS",
    "MAX_RESULT_BYTES",
    "MAX_STEPS",
    "RUNTIME_RETRIES",
    "SCHEMA_VERSION",
    "TASK_SNAPSHOT_SCHEMA_VERSION",
    "TASK_DEADLINE_SEC",
    "TASK_EVENT_TYPES", "TASK_SNAPSHOT_TYPE", "TASK_SNAPSHOT_BODY_KEY",
    "TERMINAL_TASK_STATUSES",
    "AnalysisTask",
    "BusinessClass",
    "ExecutionOutcomeStatus",
    "StepStatus",
    "ACCESS_BINDING_SCHEMA_VERSION",
    "TaskAccessBinding",
    "TaskActionOutcome",
    "TaskAuthorizationGrant",
    "TaskResultEnvelope",
    "TaskScope",
    "TaskStatus",
    "TaskStepBinding",
    "TaskStepSpec",
    "TaskStepView",
    "TaskView",
    "WorkerCompletion",
    "build_result_envelope",
    "build_task_snapshot",
    "default_limits",
    "new_daemon_instance_id",
    "new_grant_id",
    "new_task_id",
]
