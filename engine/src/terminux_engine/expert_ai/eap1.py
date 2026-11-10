# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""domain proposal 的零執行 Global／Domain 草案服務。

此模組只形成、檢查及保存草案；沒有 Kernel、ToolRegistry、grant、shell 或
candidate writer 的依賴，因此不能啟動 EDA 或修改設計輸入。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from terminux_engine.store.event_store import SessionEvent

SCHEMA_VERSION = 1
MAX_DISPATCHES = 8
GLOBAL_ROLE = "global_planner"
DOMAIN_ROLE = "domain_expert"

EVENT_PROBLEM = "expert_ai.problem.v1"
EVENT_PLAN = "expert_ai.plan.v1"
EVENT_CALL = "expert_ai.call.v1"
EVENT_PROPOSAL = "expert_ai.proposal.v1"
EVENT_DRAFT = "expert_ai.draft.v1"
EVENT_CANCEL = "expert_ai.cancel.v1"
EVENT_TYPES = frozenset({
    EVENT_PROBLEM, EVENT_PLAN, EVENT_CALL, EVENT_PROPOSAL, EVENT_DRAFT, EVENT_CANCEL,
})


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ExpertDescriptor:
    expert_id: str
    capabilities: tuple[str, ...]
    operations: tuple[str, ...]
    request_schema_version: int = SCHEMA_VERSION
    response_schema_version: int = SCHEMA_VERSION
    implementation_version: str = "eap1"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "capabilities": list(self.capabilities),
            "operations": list(self.operations),
            "request_schema_version": self.request_schema_version,
            "response_schema_version": self.response_schema_version,
            "implementation_version": self.implementation_version,
        }


@dataclass(frozen=True)
class EapScope:
    member_id: str
    project_id: str
    session_id: str
    project_source: str = "session"

    @classmethod
    def from_context(cls, context: Any) -> "EapScope":
        return cls(
            member_id=str(context.member_id), project_id=str(context.project_id),
            session_id=str(context.session_id),
            project_source=str(getattr(context, "project_source", "session")),
        )

    def to_json_dict(self) -> dict[str, str]:
        return {
            "member_id": self.member_id, "project_id": self.project_id,
            "session_id": self.session_id, "project_source": self.project_source,
        }


@dataclass(frozen=True)
class EvidenceRef:
    evidence_ref_id: str
    content_sha256: str

    def to_json_dict(self) -> dict[str, str]:
        return {"evidence_ref_id": self.evidence_ref_id, "content_sha256": self.content_sha256}


@dataclass(frozen=True)
class ResolvedEvidence:
    evidence_ref_id: str
    content_sha256: str
    content: dict[str, Any]


class EvidenceReader(Protocol):
    def __call__(self, ref: EvidenceRef, scope: EapScope) -> ResolvedEvidence | None: ...


@dataclass(frozen=True)
class ProblemInput:
    objective: str
    constraints: tuple[str, ...]
    environment: dict[str, Any]
    evidence_refs: tuple[EvidenceRef, ...]
    allowed_operations: tuple[str, ...]
    dispatch_budget: int = MAX_DISPATCHES
    available_evidence_refs: tuple[EvidenceRef, ...] = ()
    scope_override: EapScope | None = None


@dataclass(frozen=True)
class ExpertRequest:
    request_id: str
    logical_role: str
    problem_id: str
    plan_revision: int
    scope: EapScope
    capability: str | None
    objective: str
    constraints: tuple[str, ...]
    environment: dict[str, Any]
    evidence: tuple[ResolvedEvidence, ...]
    available_operations: tuple[str, ...]
    prior_response: dict[str, Any] | None = None
    repair_reason: str | None = None
    expert_catalog: tuple[dict[str, Any], ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "request_id": self.request_id,
            "logical_role": self.logical_role, "problem_id": self.problem_id,
            "plan_revision": self.plan_revision, "scope": self.scope.to_json_dict(),
            "capability": self.capability, "objective": self.objective,
            "constraints": list(self.constraints), "environment": dict(self.environment),
            "evidence": [
                {"evidence_ref_id": item.evidence_ref_id,
                 "content_sha256": item.content_sha256, "content": item.content}
                for item in self.evidence
            ],
            "available_operations": list(self.available_operations),
            "prior_response": self.prior_response, "repair_reason": self.repair_reason,
            "expert_catalog": [dict(item) for item in self.expert_catalog],
        }


@dataclass(frozen=True)
class CallResult:
    text: str | None
    provider: str = "unknown"
    model: str = "unknown"
    usage: dict[str, int | None] = field(default_factory=dict)
    transport_status: str = "ok"
    latency_ms: int | None = None
    #: provider transport 的安全診斷；不得放入 endpoint、header 或憑證內容。
    failure_detail: str | None = None
    #: 僅保存 JSON 外形，不保存模型文字或 evidence 內容，供正式路徑診斷。
    response_summary: dict[str, Any] | None = None


class ExpertAdapter(Protocol):
    def call(self, request: ExpertRequest) -> CallResult: ...


class ChatExpertAdapter:
    """把既有 chat provider 包成 EAP 共同介面，不向 Core 洩漏 provider 型別。"""

    def __init__(self, chat: Callable[..., Any], *, provider: str, model: str,
                 max_output_tokens: int | None = 1024,
                 domain_max_output_tokens: int | None = 1536) -> None:
        self._chat = chat
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._domain_max_output_tokens = domain_max_output_tokens

    def call(self, request: ExpertRequest) -> CallResult:
        system = (
            "你是 Terminux domain proposal 的 " + request.logical_role + "。"
            "只輸出符合下列回覆合約的一個 JSON object；不得輸出 Markdown、code fence、"
            "命令、tool call 或宣稱已執行 EDA。\n回覆合約："
            + _response_contract(request) + "\n請求：\n"
            + _canonical(request.to_json_dict())
        )
        started = time.monotonic()
        requested_output = (
            self._domain_max_output_tokens if request.logical_role == DOMAIN_ROLE
            else self._max_output_tokens
        )
        try:
            reply = self._chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": request.objective}], tools=None,
                max_output_tokens=requested_output)
        except Exception as error:
            is_timeout = bool(getattr(error, "is_provider_timeout", False))
            last_stage = getattr(error, "last_stage", None)
            detail = (
                f"provider_timeout:last_stage={last_stage or 'unknown'}"
                if is_timeout else f"provider_exception:{type(error).__name__}"
            )
            return CallResult(None, provider=self._provider, model=self._model,
                              transport_status="timeout" if is_timeout else "unavailable",
                              latency_ms=int((time.monotonic() - started) * 1000),
                              failure_detail=detail)
        if isinstance(reply, dict):
            text = reply.get("text")
        else:
            text = reply
        return CallResult(
            text=text if isinstance(text, str) else None, provider=self._provider,
            model=self._model,
            transport_status="ok" if isinstance(text, str) else "invalid_response",
            latency_ms=int((time.monotonic() - started) * 1000),
            response_summary=_response_summary(
                text, provider_finish_reason=(reply.get("finish_reason")
                                              if isinstance(reply, dict) else None)),
        )


def _response_contract(request: ExpertRequest) -> str:
    """依 logical role 提供可機器驗證的最小 JSON 合約，避免真實 provider 猜欄位。"""
    if request.logical_role == GLOBAL_ROLE:
        if request.prior_response is None:
            catalog = _canonical([dict(item) for item in request.expert_catalog])
            return (
                "輸出的頂層 kind 必須是 DELEGATE、NEED_EVIDENCE、REQUEST_USER_INPUT、BLOCKED 或 STOP。"
                "若 kind 為 DELEGATE，必須同時輸出 expert_id、capability、question（皆為非空字串）"
                "與可選 reason；expert_id/capability 必須來自可用 expert catalog：" + catalog + "。"
                "若不是 DELEGATE，必須輸出非空 message 與可選 reason_code。"
                "不得輸出 contract、delegate、non_delegate、expert_catalog 或 initial_global_decision 作為答案欄位或 kind。"
            )
        return (
            "輸出的頂層 kind 必須是 SUBMIT_PROPOSAL、COLLECT_EVIDENCE、REQUEST_REVISION、"
            "REQUEST_USER_INPUT 或 STOP，且必須有非空 message。不得輸出 contract、"
            "global_follow_up_decision、allowed_kind 或 required wrapper。"
        )
    if request.logical_role == DOMAIN_ROLE and "task_id" in request.environment:
        return "輸出的頂層 kind 必須是 EXECUTION_ANALYSIS，且必須有非空 message。"
    return (
        "輸出的頂層 kind 必須是 PROPOSAL、NEED_EVIDENCE、NO_PROPOSAL 或 UNSUPPORTED。"
        "非 PROPOSAL 必須有非空 message；NEED_EVIDENCE 的 requested_evidence_refs 只能使用請求提供的 ID。"
        "PROPOSAL 的 top-level 必須同時有 method（非空字串）、changes（非空 object）、"
        "basis_evidence_refs（只可用請求 evidence ID）、steps（非空 list）與 package_proposal。"
        "每一筆 top-level steps 必須同時有 step_id（唯一非空字串）、operation（只可用 available_operations）、"
        "arguments（object）、depends_on（list，且只能引用本 steps 的 step_id）。"
        "本 ORFS case 的 package_proposal 必須精確符合此一筆 declarative step："
        "package_schema_version=orfs.eap_proposal.v1；changes 的 keys 恰為 kind、parameter、candidate，"
        "值為 timing_parameter、SETUP_SLACK_MARGIN、同一個非負 decimal string；steps 的長度恰為 1，"
        "該 step 的 keys 必須包含 operation=orfs.eap.materialize_timing_parameter、"
        "arguments={parameter:SETUP_SLACK_MARGIN,candidate:<同一 decimal>}、depends_on=[]。"
        "不得輸出 contract、proposal、non_proposal 或 domain_response wrapper。"
    )


_SAFE_SCHEMA_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _response_summary(text: Any, *, provider_finish_reason: Any = None) -> dict[str, Any] | None:
    """產生可追查但不含模型內容的回覆結構摘要。"""
    if not isinstance(text, str):
        return None
    summary: dict[str, Any] = {"text_chars": len(text)}
    if isinstance(provider_finish_reason, str) and _SAFE_SCHEMA_TOKEN.fullmatch(provider_finish_reason):
        summary["provider_finish_reason"] = provider_finish_reason
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        summary["json_type"] = "invalid"
        return summary
    if not isinstance(parsed, dict):
        summary["json_type"] = type(parsed).__name__
        return summary
    summary["json_type"] = "object"
    keys = sorted(key for key in parsed if isinstance(key, str))
    summary["top_level_keys"] = keys[:20]
    kind = parsed.get("kind")
    summary["kind"] = kind if isinstance(kind, str) and _SAFE_SCHEMA_TOKEN.fullmatch(kind) else "<absent_or_noncanonical>"
    for name in ("changes", "package_proposal"):
        value = parsed.get(name)
        if isinstance(value, dict):
            summary[f"{name}_keys"] = sorted(
                key for key in value if isinstance(key, str))[:20]
    for name in ("steps",):
        value = parsed.get(name)
        if isinstance(value, list):
            summary[f"{name}_count"] = len(value)
            if value and isinstance(value[0], dict):
                summary[f"{name}_first_keys"] = sorted(
                    key for key in value[0] if isinstance(key, str))[:20]
    package = parsed.get("package_proposal")
    if isinstance(package, dict) and isinstance(package.get("steps"), list):
        package_steps = package["steps"]
        summary["package_steps_count"] = len(package_steps)
        if package_steps and isinstance(package_steps[0], dict):
            summary["package_steps_first_keys"] = sorted(
                key for key in package_steps[0] if isinstance(key, str))[:20]
    return summary


class EventStoreEvidenceReader:
    """以既有 EventStore 的 row-level scope 讀取已存在事件作為分析資料。"""

    def __init__(self, event_store: Any) -> None:
        self._event_store = event_store

    def __call__(self, ref: EvidenceRef, scope: EapScope) -> ResolvedEvidence | None:
        event = self._event_store.get_event_by_id_for_scope(
            ref.evidence_ref_id, user_id=scope.member_id, project_id=scope.project_id)
        if event is None:
            return None
        content = {"event_type": event.event_type, "payload": event.payload}
        if not is_selectable_evidence_content(content):
            return None
        actual = _digest(content)
        if actual != ref.content_sha256:
            return None
        return ResolvedEvidence(ref.evidence_ref_id, actual, content)


def is_selectable_evidence_content(content: dict[str, Any]) -> bool:
    """拒絕已明確標示為不完整的 artifact 證據。

    EAP 只能以可重播、完整的 evidence 作為 proposal basis。這個判斷只讀
    producer 已寫入的 capture 狀態；不依 event type 猜測、不把缺少狀態的既有
    baseline event 誤判為 partial。
    """
    payload = content.get("payload")
    if not isinstance(payload, dict):
        return True
    candidates = [payload]
    manifest = payload.get("artifact_manifest")
    if isinstance(manifest, dict):
        candidates.append(manifest)
    for item in candidates:
        status = item.get("capture_status")
        if isinstance(status, str) and status.lower() in {"partial", "failed"}:
            return False
    return True


class ExpertDraftService:
    """產品 service：Global 委派、Domain 回覆、草案檢查與 append-only 保存。"""

    def __init__(
        self, *, event_store: Any, scope_provider: Callable[[], Any],
        evidence_reader: EvidenceReader, global_adapter: ExpertAdapter,
        experts: dict[str, tuple[ExpertDescriptor, ExpertAdapter]],
        cancelled: set[str] | None = None,
    ) -> None:
        self._event_store = event_store
        self._scope_provider = scope_provider
        self._evidence_reader = evidence_reader
        self._global_adapter = global_adapter
        self._experts = dict(experts)
        self._cancelled = cancelled if cancelled is not None else set()

    def create_draft(self, problem: ProblemInput) -> dict[str, Any]:
        """由問題建立 domain proposal 草案；永遠不建立可執行 task 或 grant。"""
        invalid = self._validate_problem(problem)
        if invalid is not None:
            return self._error(invalid, "problem input is invalid")
        scope = problem.scope_override or EapScope.from_context(self._scope_provider())
        problem_id = _new_id("eap_problem")
        draft_id = _new_id("eap_draft")
        evidence, missing = self._resolve_evidence(problem.evidence_refs, scope)
        self._persist(EVENT_PROBLEM, scope, problem_id, {
            "problem": self._problem_json(problem), "missing_evidence_refs": missing,
        })
        if not evidence:
            response = self._base_response(problem_id, draft_id, scope, dispatches=0)
            response.update({
                "status": "BLOCKED_NEED_EVIDENCE", "reason_code": "BASELINE_EVIDENCE_REQUIRED",
                "data_request": {"missing_evidence_refs": missing,
                                 "side_effects_authorized": False},
                "execution_allowed": False,
            })
            self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
            return response
        if problem.dispatch_budget < 2:
            return self._error("DISPATCH_BUDGET_EXHAUSTED", "at least two dispatches are required")

        global_request = ExpertRequest(
            request_id=_new_id("eap_req"), logical_role=GLOBAL_ROLE, problem_id=problem_id,
            plan_revision=1, scope=scope, capability=None, objective=problem.objective,
            constraints=problem.constraints, environment=problem.environment, evidence=evidence,
            available_operations=problem.allowed_operations,
            expert_catalog=tuple(descriptor.to_json_dict()
                                 for descriptor, _adapter in self._experts.values()),
        )
        global_payload, count, error = self._call_json(global_request, self._global_adapter,
                                                        problem.dispatch_budget, scope)
        global_dispatches = count
        if error is not None:
            return self._terminal_error(problem_id, draft_id, scope, count, error)
        if problem_id in self._cancelled:
            return self._cancelled_response(problem_id, draft_id, scope, count)
        decision, error = self._validate_global_decision(global_payload)
        if error is not None:
            return self._terminal_error(problem_id, draft_id, scope, count, error)
        self._persist(EVENT_PLAN, scope, problem_id, {"plan_revision": 1, "decision": decision})
        if decision["kind"] != "DELEGATE":
            response = self._base_response(problem_id, draft_id, scope, dispatches=count)
            response["provider_dispatches_by_role"] = {GLOBAL_ROLE: global_dispatches,
                                                        DOMAIN_ROLE: 0}
            response.update({"status": decision["kind"], "reason_code": decision["reason_code"],
                             "message": decision["message"]})
            self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
            return response
        expert_id = decision["expert_id"]
        bound = self._experts.get(expert_id)
        if bound is None:
            return self._terminal_error(problem_id, draft_id, scope, count,
                                        "EXPERT_CAPABILITY_UNAVAILABLE")
        descriptor, adapter = bound
        allowed = tuple(op for op in problem.allowed_operations if op in descriptor.operations)
        if not allowed:
            return self._terminal_error(problem_id, draft_id, scope, count,
                                        "NO_SUPPORTED_OPERATION")
        domain_request = ExpertRequest(
            request_id=_new_id("eap_req"), logical_role=DOMAIN_ROLE, problem_id=problem_id,
            plan_revision=1, scope=scope, capability=decision["capability"],
            objective=decision["question"], constraints=problem.constraints,
            environment=problem.environment, evidence=evidence, available_operations=allowed,
            prior_response=decision,
        )
        domain_payload, added, error = self._call_json(
            domain_request, adapter, problem.dispatch_budget - count, scope)
        count += added
        if error is not None:
            return self._terminal_error(problem_id, draft_id, scope, count, error)
        if problem_id in self._cancelled:
            return self._cancelled_response(problem_id, draft_id, scope, count)
        domain, error = self._validate_domain_response(
            domain_payload, domain_request, descriptor, problem.available_evidence_refs)
        if error is not None:
            return self._terminal_error(problem_id, draft_id, scope, count, error)
        if domain["kind"] == "NEED_EVIDENCE" and domain["requested_evidence_refs"]:
            extra, unresolved = self._resolve_evidence(
                tuple(domain["requested_evidence_refs"]), scope)
            if unresolved or not extra:
                response = self._base_response(problem_id, draft_id, scope, dispatches=count)
                response["provider_dispatches_by_role"] = {GLOBAL_ROLE: global_dispatches,
                                                            DOMAIN_ROLE: added}
                response.update({"status": "BLOCKED_NEED_EVIDENCE",
                                 "reason_code": "REQUESTED_EVIDENCE_UNAVAILABLE",
                                 "data_request": {"missing_evidence_refs": unresolved,
                                                  "side_effects_authorized": False}})
                self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
                return response
            follow_up = ExpertRequest(
                request_id=_new_id("eap_req"), logical_role=DOMAIN_ROLE,
                problem_id=problem_id, plan_revision=2, scope=scope,
                capability=decision["capability"], objective=decision["question"],
                constraints=problem.constraints, environment=problem.environment,
                evidence=tuple((*evidence, *extra)), available_operations=allowed,
                prior_response=domain,
            )
            follow_payload, follow_count, follow_error = self._call_json(
                follow_up, adapter, problem.dispatch_budget - count, scope)
            count += follow_count
            added += follow_count
            if follow_error is not None:
                return self._terminal_error(problem_id, draft_id, scope, count, follow_error)
            if problem_id in self._cancelled:
                return self._cancelled_response(problem_id, draft_id, scope, count)
            domain, error = self._validate_domain_response(
                follow_payload, follow_up, descriptor, problem.available_evidence_refs)
            if error is not None or domain["kind"] == "NEED_EVIDENCE":
                return self._terminal_error(problem_id, draft_id, scope, count,
                                            error or "REPEATED_EVIDENCE_REQUEST")
            evidence = tuple((*evidence, *extra))
            self._persist(EVENT_PLAN, scope, problem_id,
                          {"plan_revision": 2, "decision": {"kind": "COLLECT_EVIDENCE",
                                                                "evidence_refs": [x.evidence_ref_id for x in extra]}})
        integration_request = ExpertRequest(
            request_id=_new_id("eap_req"), logical_role=GLOBAL_ROLE, problem_id=problem_id,
            plan_revision=2 if domain_request.plan_revision == 2 else 1, scope=scope,
            capability=decision.get("capability"), objective=problem.objective,
            constraints=problem.constraints, environment=problem.environment, evidence=evidence,
            available_operations=allowed, prior_response=domain,
            expert_catalog=tuple(descriptor.to_json_dict()
                                 for descriptor, _adapter in self._experts.values()),
        )
        integration_payload, integration_count, integration_error = self._call_json(
            integration_request, self._global_adapter, problem.dispatch_budget - count, scope)
        count += integration_count
        global_dispatches += integration_count
        if integration_error is not None:
            return self._terminal_error(problem_id, draft_id, scope, count, integration_error)
        integration, integration_error = self._validate_global_integration(integration_payload, domain)
        if integration_error is not None:
            return self._terminal_error(problem_id, draft_id, scope, count, integration_error)
        if integration["kind"] == "REQUEST_REVISION":
            revision_request = ExpertRequest(
                request_id=_new_id("eap_req"), logical_role=DOMAIN_ROLE, problem_id=problem_id,
                plan_revision=integration_request.plan_revision + 1, scope=scope,
                capability=decision["capability"], objective=decision["question"],
                constraints=problem.constraints, environment=problem.environment, evidence=evidence,
                available_operations=allowed,
                prior_response={"proposal": domain, "global_revision_request": integration},
            )
            revision_payload, revision_count, revision_error = self._call_json(
                revision_request, adapter, problem.dispatch_budget - count, scope)
            count += revision_count
            added += revision_count
            if revision_error is not None:
                return self._terminal_error(problem_id, draft_id, scope, count, revision_error)
            if problem_id in self._cancelled:
                return self._cancelled_response(problem_id, draft_id, scope, count)
            domain, revision_error = self._validate_domain_response(
                revision_payload, revision_request, descriptor, problem.available_evidence_refs)
            if revision_error is not None or domain["kind"] != "PROPOSAL":
                return self._terminal_error(problem_id, draft_id, scope, count,
                                            revision_error or "REVISION_DID_NOT_PRODUCE_PROPOSAL")
            final_request = ExpertRequest(
                request_id=_new_id("eap_req"), logical_role=GLOBAL_ROLE, problem_id=problem_id,
                plan_revision=revision_request.plan_revision, scope=scope,
                capability=decision["capability"], objective=problem.objective,
                constraints=problem.constraints, environment=problem.environment, evidence=evidence,
                available_operations=allowed, prior_response=domain,
                expert_catalog=tuple(descriptor.to_json_dict()
                                     for descriptor, _adapter in self._experts.values()),
            )
            final_payload, final_count, final_error = self._call_json(
                final_request, self._global_adapter, problem.dispatch_budget - count, scope)
            count += final_count
            global_dispatches += final_count
            if final_error is not None:
                return self._terminal_error(problem_id, draft_id, scope, count, final_error)
            integration, final_error = self._validate_global_integration(final_payload, domain)
            if final_error is not None:
                return self._terminal_error(problem_id, draft_id, scope, count, final_error)
        self._persist(EVENT_PLAN, scope, problem_id,
                      {"plan_revision": integration_request.plan_revision,
                       "domain_response_request_id": domain_request.request_id,
                       "global_decision": integration})
        if integration["kind"] != "SUBMIT_PROPOSAL":
            response = self._base_response(problem_id, draft_id, scope, dispatches=count)
            response["provider_dispatches_by_role"] = {GLOBAL_ROLE: global_dispatches,
                                                        DOMAIN_ROLE: added}
            response.update({"status": integration["kind"], "reason_code": integration["kind"],
                             "message": integration["message"], "global_decision": integration})
            self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
            return response
        self._persist(EVENT_PROPOSAL, scope, problem_id, {"proposal": domain})
        response = self._base_response(problem_id, draft_id, scope, dispatches=count)
        response["provider_dispatches_by_role"] = {GLOBAL_ROLE: global_dispatches,
                                                    DOMAIN_ROLE: added}
        response.update(self._consume_domain(domain, {**decision, "integration": integration}, descriptor, problem, evidence))
        self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
        return response

    def cancel(self, problem_id: str) -> dict[str, Any]:
        """記錄本地取消；無法觀測 provider 時不把它敘述為遠端取消。"""
        scope = EapScope.from_context(self._scope_provider())
        self._cancelled.add(problem_id)
        self._persist(EVENT_CANCEL, scope, problem_id, {"provider_outcome": "unknown"})
        return {"status": "CANCELLED", "problem_id": problem_id,
                "provider_outcome": "unknown", "execution_allowed": False}

    def _validate_problem(self, problem: ProblemInput) -> str | None:
        if not isinstance(problem.objective, str) or not problem.objective.strip():
            return "OBJECTIVE_REQUIRED"
        if not isinstance(problem.environment, dict):
            return "ENVIRONMENT_INVALID"
        if not problem.allowed_operations or any(not isinstance(op, str) or not op for op in problem.allowed_operations):
            return "ALLOWED_OPERATIONS_REQUIRED"
        if problem.dispatch_budget < 0 or problem.dispatch_budget > MAX_DISPATCHES:
            return "DISPATCH_BUDGET_INVALID"
        return None

    def _resolve_evidence(self, refs: tuple[EvidenceRef, ...], scope: EapScope) -> tuple[tuple[ResolvedEvidence, ...], list[str]]:
        resolved: list[ResolvedEvidence] = []
        missing: list[str] = []
        for ref in refs:
            item = self._evidence_reader(ref, scope)
            if item is None:
                missing.append(ref.evidence_ref_id)
            else:
                resolved.append(item)
        return tuple(resolved), missing

    def _call_json(self, request: ExpertRequest, adapter: ExpertAdapter, budget: int,
                   scope: EapScope) -> tuple[dict[str, Any] | None, int, str | None]:
        if budget < 1:
            return None, 0, "DISPATCH_BUDGET_EXHAUSTED"
        result = adapter.call(request)
        self._persist_call(scope, request, result)
        payload = self._parse_json(result.text)
        if payload is not None:
            return payload, 1, None
        if result.transport_status != "ok":
            return None, 1, "PROVIDER_UNAVAILABLE"
        if budget < 2:
            return None, 1, "INVALID_RESPONSE_BUDGET_EXHAUSTED"
        repair = ExpertRequest(**{**request.__dict__, "request_id": _new_id("eap_req"),
                                  "repair_reason": "response must be a JSON object"})
        retry = adapter.call(repair)
        self._persist_call(scope, repair, retry)
        payload = self._parse_json(retry.text)
        return payload, 2, None if payload is not None else "INVALID_RESPONSE"

    @staticmethod
    def _parse_json(text: str | None) -> dict[str, Any] | None:
        if not isinstance(text, str):
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _validate_global_decision(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        kind = payload.get("kind")
        if kind in {"NEED_EVIDENCE", "REQUEST_USER_INPUT", "BLOCKED", "STOP"}:
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                return {}, "GLOBAL_DECISION_INVALID"
            return {"kind": kind, "message": message,
                    "reason_code": str(payload.get("reason_code") or kind)}, None
        if kind != "DELEGATE":
            return {}, "GLOBAL_DECISION_INVALID"
        expert_id = payload.get("expert_id")
        capability = payload.get("capability")
        question = payload.get("question")
        if not all(isinstance(value, str) and value.strip()
                   for value in (expert_id, capability, question)):
            return {}, "GLOBAL_DECISION_INVALID"
        descriptor = self._experts.get(expert_id, (None, None))[0]
        if descriptor is None or capability not in descriptor.capabilities:
            return {}, "EXPERT_CAPABILITY_UNAVAILABLE"
        return {"kind": kind, "expert_id": expert_id, "capability": capability,
                "question": question, "reason": str(payload.get("reason") or "")}, None

    @staticmethod
    def _validate_global_integration(payload: dict[str, Any], domain: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        kind = payload.get("kind")
        message = payload.get("message")
        if kind not in {"SUBMIT_PROPOSAL", "COLLECT_EVIDENCE", "REQUEST_REVISION", "REQUEST_USER_INPUT", "STOP"}:
            return {}, "GLOBAL_INTEGRATION_INVALID"
        if not isinstance(message, str) or not message.strip():
            return {}, "GLOBAL_INTEGRATION_INVALID"
        if kind == "SUBMIT_PROPOSAL" and domain.get("kind") != "PROPOSAL":
            return {}, "GLOBAL_SUBMISSION_MISMATCH"
        return {"kind": kind, "message": message,
                "domain_response_kind": domain.get("kind"),
                "domain_proposal_id": domain.get("proposal_id")}, None

    @staticmethod
    def _validate_domain_response(payload: dict[str, Any], request: ExpertRequest,
                                  descriptor: ExpertDescriptor,
                                  available_refs: tuple[EvidenceRef, ...]) -> tuple[dict[str, Any], str | None]:
        kind = payload.get("kind")
        if kind in {"NEED_EVIDENCE", "NO_PROPOSAL", "UNSUPPORTED"}:
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                return {}, "DOMAIN_RESPONSE_INVALID"
            requested: list[EvidenceRef] = []
            raw_requested = payload.get("requested_evidence_refs", [])
            if kind == "NEED_EVIDENCE":
                if not isinstance(raw_requested, list):
                    return {}, "DOMAIN_RESPONSE_INVALID"
                known = {ref.evidence_ref_id: ref for ref in available_refs}
                for item in raw_requested:
                    if not isinstance(item, str) or item not in known:
                        return {}, "REQUESTED_EVIDENCE_SCOPE_INVALID"
                    requested.append(known[item])
            return {"kind": kind, "message": message,
                    "unknowns": list(payload.get("unknowns") or []),
                    "requested_evidence_refs": requested}, None
        if kind != "PROPOSAL":
            return {}, "DOMAIN_RESPONSE_INVALID"
        method, changes, steps = payload.get("method"), payload.get("changes"), payload.get("steps")
        if not isinstance(method, str) or not method.strip():
            return {}, "DOMAIN_PROPOSAL_METHOD_INVALID"
        if not isinstance(changes, dict) or not changes:
            return {}, "DOMAIN_PROPOSAL_CHANGES_INVALID"
        if not isinstance(steps, list) or not steps:
            return {}, "DOMAIN_PROPOSAL_STEPS_REQUIRED"
        ids: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                return {}, "DOMAIN_PROPOSAL_STEP_INVALID"
            step_id, operation = step.get("step_id"), step.get("operation")
            if not isinstance(step_id, str) or not step_id or step_id in ids:
                return {}, "DOMAIN_PROPOSAL_STEP_ID_INVALID"
            ids.add(step_id)
            if operation not in request.available_operations or operation not in descriptor.operations:
                return {}, "PROPOSAL_OPERATION_UNAVAILABLE"
            if not isinstance(step.get("arguments"), dict):
                return {}, "DOMAIN_PROPOSAL_STEP_ARGUMENTS_INVALID"
            if not isinstance(step.get("depends_on", []), list):
                return {}, "DOMAIN_PROPOSAL_STEP_DEPENDENCIES_INVALID"
        if any(dep not in ids for step in steps for dep in step.get("depends_on", [])):
            return {}, "PROPOSAL_DEPENDENCY_INVALID"
        if ExpertDraftService._has_cycle(steps):
            return {}, "PROPOSAL_DEPENDENCY_CYCLE"
        refs = payload.get("basis_evidence_refs", [])
        if not isinstance(refs, list) or any(ref not in {e.evidence_ref_id for e in request.evidence} for ref in refs):
            return {}, "PROPOSAL_EVIDENCE_SCOPE_INVALID"
        package_proposal = payload.get("package_proposal")
        if not isinstance(package_proposal, dict):
            return {}, "PACKAGE_PROPOSAL_REQUIRED"
        from terminux_engine.packages.orfs_run_intelligence.proposal_validator import validate_proposal
        package_validation = validate_proposal(package_proposal)
        if not package_validation.passed:
            return {}, str(package_validation.code or "PACKAGE_PROPOSAL_REJECTED")
        package_candidate = package_proposal["changes"]["candidate"]
        stated_candidate = payload.get("candidate")
        if stated_candidate is not None and stated_candidate != package_candidate:
            return {}, "DOMAIN_PROPOSAL_CANDIDATE_MISMATCH"
        return {
            "kind": kind, "proposal_id": str(payload.get("proposal_id") or _new_id("eap_proposal")),
            "originating_request_id": request.request_id, "basis_evidence_refs": refs,
            "method": method, "candidate": package_candidate, "changes": changes,
            "steps": steps, "expected_effect": payload.get("expected_effect"),
            "validation_requirements": list(payload.get("validation_requirements") or []),
            "preconditions": list(payload.get("preconditions") or []),
            "unknowns": list(payload.get("unknowns") or []),
            "package_proposal": package_proposal,
            "proposal_validation": package_validation.to_json_dict(),
        }, None

    @staticmethod
    def _has_cycle(steps: list[dict[str, Any]]) -> bool:
        graph = {str(step["step_id"]): set(step.get("depends_on", [])) for step in steps}
        visited: set[str] = set()
        active: set[str] = set()
        def visit(node: str) -> bool:
            if node in active:
                return True
            if node in visited:
                return False
            active.add(node)
            cyclic = any(visit(dep) for dep in graph[node])
            active.remove(node)
            visited.add(node)
            return cyclic
        return any(visit(node) for node in graph)

    def _consume_domain(self, domain: dict[str, Any], decision: dict[str, Any],
                        descriptor: ExpertDescriptor, problem: ProblemInput,
                        evidence: tuple[ResolvedEvidence, ...]) -> dict[str, Any]:
        if domain["kind"] != "PROPOSAL":
            return {"status": domain["kind"], "reason_code": domain["kind"],
                    "message": domain["message"], "unknowns": domain.get("unknowns", []),
                    "execution_allowed": False}
        specification = [
            {"step_id": step["step_id"], "operation": step["operation"],
             "arguments": dict(step["arguments"]), "depends_on": list(step.get("depends_on", []))}
            for step in domain["steps"]
        ]
        return {
            "status": "DRAFT_READY", "plan_revision": 1,
            "global_decision": decision, "expert": descriptor.to_json_dict(),
            "proposal": domain, "execution_specification": specification,
            "basis_evidence_refs": [item.evidence_ref_id for item in evidence],
            "unknowns": list(domain["unknowns"]),
            "execution_allowed": False,
            "execution_status": "EAP2_WIRING_REQUIRED",
            "message": "Domain proposal was consumed into a non-executable domain proposal draft.",
        }

    def _base_response(self, problem_id: str, draft_id: str, scope: EapScope, *, dispatches: int) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "problem_id": problem_id,
                "draft_id": draft_id, "scope": scope.to_json_dict(),
                "provider_dispatches_total": dispatches,
                "provider_dispatches_by_role": {}, "real_eda_launches": 0,
                "candidate_source_writes": 0, "execution_allowed": False}

    def _terminal_error(self, problem_id: str, draft_id: str, scope: EapScope,
                        dispatches: int, code: str) -> dict[str, Any]:
        response = self._base_response(problem_id, draft_id, scope, dispatches=dispatches)
        response.update({"status": "ERROR", "reason_code": code,
                         "message": "No executable draft was created."})
        self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
        return response

    def _cancelled_response(self, problem_id: str, draft_id: str, scope: EapScope,
                            dispatches: int) -> dict[str, Any]:
        response = self._base_response(problem_id, draft_id, scope, dispatches=dispatches)
        response.update({"status": "CANCELLED", "reason_code": "LOCAL_CANCELLED",
                         "provider_outcome": "unknown",
                         "message": "Late provider output was recorded but not consumed."})
        self._persist(EVENT_DRAFT, scope, problem_id, {"draft": response})
        return response

    def _persist_call(self, scope: EapScope, request: ExpertRequest, result: CallResult) -> None:
        self._persist(EVENT_CALL, scope, request.problem_id, {
            "request_id": request.request_id, "logical_role": request.logical_role,
            "capability": request.capability, "provider": result.provider, "model": result.model,
            "transport_status": result.transport_status, "usage": result.usage,
            "latency_ms": result.latency_ms,
            "failure_detail": result.failure_detail,
            "response_summary": result.response_summary,
            "response_sha256": _digest(result.text) if result.text is not None else None,
        })

    def _persist(self, event_type: str, scope: EapScope, problem_id: str, body: dict[str, Any]) -> None:
        self._event_store.emit(SessionEvent(
            event_id=uuid.uuid4().hex, session_id=scope.session_id, terminal_id="default",
            user_id=scope.member_id, project_id=scope.project_id, package_id=None,
            timestamp=time.time(), event_type=event_type,
            payload={"schema_version": SCHEMA_VERSION, "problem_id": problem_id,
                     "scope": scope.to_json_dict(), **body},
        ))

    @staticmethod
    def _problem_json(problem: ProblemInput) -> dict[str, Any]:
        return {"objective": problem.objective, "constraints": list(problem.constraints),
                "environment": dict(problem.environment),
                "evidence_refs": [ref.to_json_dict() for ref in problem.evidence_refs],
                "available_evidence_refs": [ref.to_json_dict() for ref in problem.available_evidence_refs],
                "allowed_operations": list(problem.allowed_operations),
                "dispatch_budget": problem.dispatch_budget}

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"status": "ERROR", "reason_code": code, "message": message,
                "execution_allowed": False, "real_eda_launches": 0,
                "candidate_source_writes": 0}


class ExpertDraftReader:
    """從 append-only EventStore 重建 domain proposal 草案，不再呼叫模型。"""

    def __init__(self, event_store: Any) -> None:
        self._event_store = event_store

    def load(self, draft_id: str, context: Any) -> dict[str, Any] | None:
        scope = EapScope.from_context(context)
        events = self._event_store.query(user_id=scope.member_id, order="asc")
        for event in events:
            payload = event.payload
            stored = payload.get("scope")
            if not isinstance(stored, dict):
                continue
            allowed = (stored.get("member_id") == scope.member_id and
                       ((getattr(context, "project_declared", True) and
                         stored.get("project_id") == scope.project_id) or
                        (not getattr(context, "project_declared", True) and
                         (stored.get("session_id") == scope.session_id or
                          stored.get("project_source") == "profile"))))
            if allowed and event.event_type == EVENT_DRAFT and payload.get("draft", {}).get("draft_id") == draft_id:
                return dict(payload["draft"])
        return None


__all__ = [
    "CallResult", "ChatExpertAdapter", "DOMAIN_ROLE", "EapScope", "EvidenceRef",
    "EventStoreEvidenceReader", "ExpertDescriptor", "ExpertDraftReader", "ExpertDraftService",
    "ExpertRequest", "GLOBAL_ROLE", "MAX_DISPATCHES", "ProblemInput", "ResolvedEvidence",
]
