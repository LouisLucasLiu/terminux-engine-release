# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""把 domain proposal 已提交且經 Package 驗證的方案固定為 FlowTask 草案。

此模組刻意沒有 grant、host binding、Kernel 或 candidate writer。那些只能在
既有 `/task-start <task_id> <task_digest>` 授權後，由 Flow runtime 建立。
"""

from __future__ import annotations

from typing import Any

from terminux_engine.bounded_execution.analysis_tasks.canonical import sha256_payload
from terminux_engine.packages.orfs_run_intelligence.proposal_validator import (
    flow_task_binding,
)


class EapBridgeError(ValueError):
    """EAP 草案不能安全地轉成 FlowTask 時的結構化錯誤。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def review_execution_outcome(
    outcome: dict[str, Any], *, domain_adapter: Any, global_adapter: Any,
) -> dict[str, Any]:
    """以真實 EAP Domain→Global 回合消費已保存的 task 結果。

    不重試、不建立 task、不授權執行；兩個 logical role 共用 task 既有的剩餘
    body LLM budget。呼叫端負責把回覆與 task event 同步持久化。
    """
    from terminux_engine.expert_ai.eap1 import (
        EapScope, ExpertRequest, ResolvedEvidence, _digest, _new_id,
    )

    if int(outcome.get("remaining_dispatches", 0)) < 2:
        return {"feedback_status": "NOT_RUN_BUDGET_EXHAUSTED",
                "execution_allowed": False}
    source = outcome.get("source")
    scope_data = outcome.get("scope")
    if not isinstance(source, dict) or not isinstance(scope_data, dict):
        return {"feedback_status": "UNCONFIRMED_INPUT_INVALID",
                "execution_allowed": False}
    # FlowTask 的持久化 scope 使用 ``origin_session_id``；domain proposal 的 RPC
    # payload 則仍使用 ``session_id``。兩者代表同一個建立者 session，這裡
    # 明確做相容讀取，不能因欄位命名差異把已完成 task 的結果丟掉。
    session_id = scope_data.get("origin_session_id", scope_data.get("session_id"))
    if not isinstance(session_id, str) or not session_id:
        return {"feedback_status": "UNCONFIRMED_INPUT_INVALID",
                "execution_allowed": False}
    scope = EapScope(str(scope_data["member_id"]), str(scope_data["project_id"]),
                     session_id, str(scope_data.get("project_source", "session")))
    evidence_content = dict(outcome)
    evidence = ResolvedEvidence(
        f"flow_task:{outcome['task_id']}:outcome", _digest(evidence_content), evidence_content)
    objective = "分析已核准 EAP proposal 的執行結果並決定下一步"
    domain_request = ExpertRequest(
        request_id=_new_id("eap2_req"), logical_role="domain_expert",
        problem_id=str(source["problem_id"]), plan_revision=int(source.get("plan_revision", 1)),
        scope=scope, capability="timing_analysis", objective=objective, constraints=(),
        environment={"task_id": outcome["task_id"], "task_digest": outcome["task_digest"]},
        evidence=(evidence,), available_operations=())
    domain_call = domain_adapter.call(domain_request)
    try:
        import json
        domain = json.loads(domain_call.text or "")
    except (TypeError, ValueError):
        return {"feedback_status": "DOMAIN_RESPONSE_INVALID", "execution_allowed": False,
                "domain_call": {"request_id": domain_request.request_id,
                                "transport_status": domain_call.transport_status}}
    if not isinstance(domain, dict) or domain.get("kind") != "EXECUTION_ANALYSIS" \
            or not isinstance(domain.get("message"), str) or not domain["message"].strip():
        return {"feedback_status": "DOMAIN_RESPONSE_INVALID", "execution_allowed": False,
                "domain_call": {"request_id": domain_request.request_id,
                                "transport_status": domain_call.transport_status}}
    domain_digest = _digest(domain)
    global_request = ExpertRequest(
        request_id=_new_id("eap2_req"), logical_role="global_planner",
        problem_id=str(source["problem_id"]), plan_revision=int(source.get("plan_revision", 1)),
        scope=scope, capability="timing_analysis", objective=objective, constraints=(),
        environment={"task_id": outcome["task_id"], "task_digest": outcome["task_digest"]},
        evidence=(evidence,), available_operations=(),
        prior_response={"domain_analysis": domain, "domain_response_sha256": domain_digest})
    global_call = global_adapter.call(global_request)
    try:
        import json
        decision = json.loads(global_call.text or "")
    except (TypeError, ValueError):
        return {"feedback_status": "GLOBAL_DECISION_INVALID", "execution_allowed": False,
                "domain_response": domain, "domain_response_sha256": domain_digest}
    if not isinstance(decision, dict) or decision.get("kind") not in {"STOP", "REQUEST_REVISION"} \
            or not isinstance(decision.get("message"), str) or not decision["message"].strip():
        return {"feedback_status": "GLOBAL_DECISION_INVALID", "execution_allowed": False,
                "domain_response": domain, "domain_response_sha256": domain_digest}
    return {
        "feedback_status": "DOMAIN_GLOBAL_CONSUMED",
        "domain_response": domain, "domain_response_sha256": domain_digest,
        "global_decision": {**decision, "based_on_domain_response_sha256": domain_digest},
        "provider_dispatches": 2, "execution_allowed": False,
    }


def build_flow_task_proposal(
    draft: dict[str, Any], *, replacement: dict[str, str] | None = None,
) -> dict[str, Any]:
    """從持久化 domain proposal draft 建立 task-owned proposal binding。

    呼叫端只能交 draft id；proposal 與 digest 一律由 server 讀回，因此不可
    靠重送 JSON 偷換 candidate、steps 或 evidence basis。
    """
    if draft.get("status") != "DRAFT_READY":
        raise EapBridgeError("EAP_DRAFT_NOT_READY",
                             "only a submitted domain proposal can create a FlowTask draft")
    draft_id = draft.get("draft_id")
    problem_id = draft.get("problem_id")
    domain = draft.get("proposal")
    if not isinstance(draft_id, str) or not draft_id or not isinstance(problem_id, str) \
            or not problem_id or not isinstance(domain, dict):
        raise EapBridgeError("EAP_DRAFT_INVALID", "the stored domain proposal draft is incomplete")
    global_decision = draft.get("global_decision")
    integration = global_decision.get("integration") if isinstance(global_decision, dict) else None
    if not isinstance(integration, dict) or integration.get("kind") != "SUBMIT_PROPOSAL":
        raise EapBridgeError("EAP_GLOBAL_DECISION_NOT_SUBMITTED",
                             "Global did not submit this Domain proposal")
    package_proposal = domain.get("package_proposal")
    if not isinstance(package_proposal, dict):
        raise EapBridgeError("PACKAGE_PROPOSAL_REQUIRED",
                             "the Domain response has no Package proposal")
    try:
        package = flow_task_binding(package_proposal)
    except ValueError as exc:
        raise EapBridgeError(str(exc), "the Package rejected the stored proposal") from exc
    domain_digest = sha256_payload(domain)
    result = {
        "source": {
            "problem_id": problem_id,
            "draft_id": draft_id,
            "plan_revision": int(draft.get("plan_revision", 1)),
            "proposal_digest": domain_digest,
            "package_proposal_digest": package["proposal_digest"],
        },
        "package": package,
        "expert": dict(draft.get("expert") or {}),
        # 完整內容（含證據、preconditions、validation requirements）一起進
        # FlowTask digest，讓使用者看到與核准的是同一份方案。
        "domain_proposal": dict(domain),
        "basis_evidence_refs": list(draft.get("basis_evidence_refs") or []),
        # 既有 FlowTask 的 s1 一律重新產生本 task 的 baseline run；只沿用
        # Profile 所凍結的 input snapshot，絕不混用舊 task 的 run 結果。
        "baseline_plan": {
            "mode": "RERUN_FROM_PROFILE_INPUT_SNAPSHOT",
            "requires_same_profile_input": True,
            "requires_same_tool_and_comparison_basis": True,
        },
        "execution_binding": "PENDING_USER_APPROVAL",
        "execution_allowed": False,
    }
    if replacement is not None:
        required = ("replaces_task_id", "replaces_task_digest", "reason_code")
        if any(not isinstance(replacement.get(key), str) or not replacement[key]
               for key in required):
            raise EapBridgeError("EAP_REPLACEMENT_INVALID",
                                 "the replacement task identity is incomplete")
        # 這是新 task digest 的一部分。替代只重用已保存且重新驗證的 proposal，
        # 絕不重用舊 task 的 grant、host binding 或任何執行狀態。
        result["replacement"] = {key: replacement[key] for key in required}
    return result


__all__ = ["EapBridgeError", "build_flow_task_proposal", "review_execution_outcome"]
