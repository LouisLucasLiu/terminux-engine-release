# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent Intelligence — 自建 tool-use loop。

不依賴 llama-index-core 或任何外部 Agent framework。

行為摘要：

- 預設最多 5 輪 / 45 秒；可由建構子參數覆寫
- async mode 第一輪「triage」budget 為 10 秒，若 round 0 完成時超過 →
  立即返回 AgentResult(no_action=True)
- 超過 max_rounds 或 total timeout_sec → 呼叫 _best_effort()，請 LLM 基於
  已收集的 evidence 給出 best-effort 回答（不再給 tools）
- LLM 拋例外、parse 失敗等整體錯誤皆收斂為 AgentResult(error=...)，不傳播

對 LLM provider 的要求：

    def chat(messages: list[dict],
             tools: list[dict] | str | None = None) -> dict | str: ...

provider 可回傳 normalized dict（見 ResponseParser docstring）或純字串。
AgentRunner 不直接呼叫 GeminiCompletionProvider，由另外撰寫
adapter 串接。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Literal

from terminux_engine.ipc.request_context import try_get_current_request_context
from terminux_engine.llm.agent_models import (
    AgentResult,
    ToolCallRequest,
    ToolEvidence,
)
from terminux_engine.llm import agent_audit, task_scope
from terminux_engine.llm.evidence_first_planner import (
    PlanValidation, ResultInterpretation, StructuredTurnPlan, TurnIntent,
    active_plan_prompt_text, build_evidence_fallback, parse_plan, parse_proposed_action,
    planner_instruction, proposed_action_format_repair_eligible,
    proposed_action_repair_instruction, repair_instruction,
    task_profiles_prompt_text, trusted_result_prompt_text, validate_plan,
    validate_result_interpretation,
)
from terminux_engine.llm.plan_registry import (
    PlanCreationError, PlanRegistry, PlanStepDeclaration,
)
from terminux_engine.llm.proposal_registry import (
    CandidateBinding, HandleBinding, ProposableCapability, ProposalCreationError,
    ProposalRegistry, ResultBinding, binding_candidate_catalog_prompt_text,
    catalog_prompt_text, validate_action_arguments,
)
from terminux_engine.llm.response_parser import ParsedResponse, ResponseParser
from terminux_engine.llm.tool_registry import ToolRegistry, ToolRisk, _agent_tool_call_scope, _cloud_agent_mode

_log = logging.getLogger(__name__)

#: planner 選出零個 tool 時附加到 answer round 的 system message。這個回合不會送出任何
#: tool schema，但 Agent system prompt 的通用守則仍要求「先用 tool 查」；兩者衝突時，
#: 部分模型會把自己的 tool call 語法寫進 content（PROVIDER_UNPARSED_TOOL_CALL）。
#: 這段說明只陳述這個回合的事實（沒有 tool），不放寬任何 tool 權限，也不改變其他回合。
NO_TOOLS_TURN_NOTICE = (
    "\n\n[本回合沒有可用 tools / No tools in this turn]\n"
    "規劃階段沒有選擇任何 tool，本次請求也沒有提供 tool schema。請直接依上方 context 與使用者"
    "問題以文字回答，不要輸出任何 tool call、函式呼叫語法或 tool-call 標記；若 context 不足，"
    "請說明缺少什麼資訊，以及使用者可以怎麼取得；不要聲稱已執行或查詢任何 tool。上方「先使用 tool」"
    "的守則在本回合不適用；回答語言仍依上方指定。\n"
    "The plan selected no tools and this request carries no tool schemas. Answer in plain text "
    "from the context above and the user's question. Do not emit tool calls, function-call "
    "syntax or tool-call markup. If the context is insufficient, say what is missing and how "
    "the user can obtain it; do not claim that any tool was run or queried. The guidance above about "
    "using tools first does not apply to this turn; keep answering in the language specified above."
)


def _gate_error_code(error: BaseException) -> str | None:
    """只有 host gate 的拒絕（TaskScope／最終 Gate）才有穩定碼；provider 的其他錯誤不外露文字。"""
    from terminux_engine.llm.openai_compatible_provider import OutboundPayloadRejected

    if isinstance(error, (task_scope.TaskScopeRejected, OutboundPayloadRejected)):
        return str(error)
    return None


def _audit_scope_denial(code: str) -> None:
    """Profile＋`structural_c2` 下把 activate／track_message 階段的拒絕寫入外送 audit（best-effort）。

    這些拒絕發生在任何請求序列化之前，沒有外送可阻擋，因此寫入失敗只記 log。
    """
    if not task_scope.profile_containment_active():
        return
    policy = task_scope.policy_identity()
    if policy is None or not policy.structural:
        return
    from terminux_engine.llm import egress_audit

    stage = "activate" if code.startswith(("CONVERSATION_SOURCE_MISSING", "CONTEXT_SOURCE_DENIED")) else "track_message"
    try:
        egress_audit.record(stage=stage, decision="DENY", reason_code=code, request_index=0,
                            endpoint_host=policy.destination_host, model=policy.model, policy=policy,
                            payload=None, runs=task_scope.tracked_runs())
    except egress_audit.EgressAuditUnavailable as error:
        _log.warning("egress audit record failed stage=%s error=%s", stage, type(error).__name__)


class AgentRunner:
    """Agent 自建 loop。"""

    def __init__(
        self,
        llm_provider: Any,
        tool_registry: ToolRegistry,
        response_parser: ResponseParser | None = None,
        max_rounds: int = 5,
        timeout_sec: float = 45.0,
        triage_timeout_sec: float = 10.0,
        tool_format: Literal["openai", "xml"] = "openai",
        clock: Callable[[], float] | None = None,
        enable_read_only_planner: bool = False,
        proposal_registry: ProposalRegistry | None = None,
        proposable_catalog_provider: Callable[[], list[ProposableCapability]] | None = None,
        plan_registry: PlanRegistry | None = None,
        analysis_task_creator: Callable[..., dict] | None = None,
        flow_task_creator: Callable[..., dict] | None = None,
        task_profile_provider: Callable[[], list] | None = None,
    ) -> None:
        self._llm = llm_provider
        self._tools = tool_registry
        self._parser = response_parser or ResponseParser()
        self._max_rounds = max_rounds
        self._timeout_sec = timeout_sec
        self._triage_timeout_sec = triage_timeout_sec
        self._tool_format = tool_format
        self._clock = clock or time.monotonic
        self._enable_read_only_planner = enable_read_only_planner
        #: 兩者皆 None 時，proposed_action 一律不建立 proposal
        #: （fail-closed；不會因為缺依賴而報錯，只是沒有這個能力）。
        self._proposal_registry = proposal_registry
        self._proposable_catalog_provider = proposable_catalog_provider
        #: None 時 proposed_plan/plan_continuation 一律被忽略
        #: （fail-closed，不會建立任何 plan，也不影響既有 evidence-first planner～C6 單步
        #: proposal 行為）。
        self._plan_registry = plan_registry
        #: host 注入的 create 入口，簽名與 typed
        #: `agent.analysis_task.create` **完全相同**（同一個 validator／
        #: service）。None 時 `proposed_analysis_task` 一律被忽略
        #: （fail-closed，不影響任何既有行為）。
        self._analysis_task_creator = analysis_task_creator
        #: host 注入的 flow task create 入口與可用 profile 列表。
        #: 未注入時 `proposed_flow_task` 一律被忽略（fail-closed）。
        self._flow_task_creator = flow_task_creator
        self._task_profile_provider = task_profile_provider

    # ── Public ───────────────────────────────────────────────────────────────

    def denied_capability_names(self, profile: task_scope.TaskProfile | None) -> tuple[str, ...]:
        """Profile 未授權的已註冊工具名稱；最終 Gate 與歷史投影共用這一份清單。"""
        if profile is None:
            return ()
        return tuple(name for name in self._tools.list_tools() if name not in profile.tools)

    def run(
        self,
        query: str,
        system_context: str,
        trigger_source: str = "chat_query",
        mode: Literal["sync", "async"] = "sync",
        trusted_result: dict[str, Any] | None = None,
    ) -> AgentResult:
        """執行一次 Agent run。所有失敗都收斂成 AgentResult，不會拋出例外。

        ``trusted_result``（可選）是 host 端已從 ``ProposalRegistry.
        consume_active_result_for_turn()`` 取出的 bounded TrustedActionResult
        view——呼叫端（`jsonrpc_server._try_agent_chat_query`）只會呼叫一次
        consume，取得的值直接傳進來，AgentRunner 本身不重新查詢、也不會把
        使用者 query 文字裡任何看起來像 host result 的內容當真。"""
        turn_started = time.monotonic()
        with agent_audit.agent_turn() as turn_id, _agent_tool_call_scope():
            agent_audit.emit("turn.started", result_status="running")
            result: AgentResult | None = None
            try:
                profile = task_scope.load_profile()
                if profile is not None and (mode != "sync" or trigger_source != "chat_query" or trusted_result is not None):
                    return AgentResult(answer=None, error="TASK_CONTEXT_DENIED", trigger_source=trigger_source)
                schemas = self._tools.get_schemas_for_names(profile.tools) if profile else []
                if profile and any(self._tools.get_tool_risks().get(name) is not ToolRisk.SAFE for name in profile.tools):
                    return AgentResult(answer=None, error="TASK_TOOL_DENIED", trigger_source=trigger_source)
                with task_scope.activate(profile, system_context, schemas,
                                         self.denied_capability_names(profile), query=query):
                    result = self._run(query, system_context, trigger_source, mode, trusted_result)
                return result
            except (task_scope.TaskScopeRejected, ValueError) as exc:
                code = str(exc) if isinstance(exc, task_scope.TaskScopeRejected) else None
                result = AgentResult(answer=None, error=type(exc).__name__, trigger_source=trigger_source,
                                     error_code=code)
                if code is not None:
                    _audit_scope_denial(code)
                return result
            finally:
                agent_audit.emit(
                    "turn.finished",
                    result_status=("error" if result is None or result.error else "success"),
                    result_value={
                        "answer_present": bool(result and result.answer),
                        "evidence_count": len(result.evidence) if result else 0,
                        "turn_id": turn_id,
                    },
                    duration_ms=(time.monotonic() - turn_started) * 1000.0,
                )

    def _run(
        self,
        query: str,
        system_context: str,
        trigger_source: str,
        mode: Literal["sync", "async"],
        trusted_result: dict[str, Any] | None = None,
    ) -> AgentResult:
        start = self._clock()
        handle_catalog = self._fetch_handle_catalog(trusted_result)
        if trusted_result:
            system_context = system_context + trusted_result_prompt_text(trusted_result, handle_catalog)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_context},
            {"role": "user", "content": query},
        ]
        evidence: list[ToolEvidence] = []
        capability_names: tuple[str, ...] | None = (task_scope.current().profile.tools if task_scope.current() else None)
        planner_repaired = False
        plan_for_proposal: StructuredTurnPlan | None = None
        discarded_proposed_action_code: str | None = None
        result_interpretation_denial_code: str | None = None
        proposable_catalog: list[ProposableCapability] = []
        if task_scope.current() is None and self._enable_read_only_planner and mode == "sync" and trigger_source == "chat_query":
            proposable_catalog = [] if _cloud_agent_mode() else self._build_proposable_catalog()
            binding_candidate_catalogs = self._fetch_binding_candidate_catalogs(trusted_result, proposable_catalog)
            active_plan = self._fetch_active_plan_context()
            validation = self._prepare_read_only_plan(
                query, proposable_catalog, trusted_result, handle_catalog, binding_candidate_catalogs, active_plan,
            )
            if not validation.valid:
                if trusted_result is not None:
                    answer = ("目前有 HOST_ACTION_RESULT context，但無法產生合法的 read-only "
                              "plan；未執行任何 tool。execution_status=NOT_EXECUTED。")
                else:
                    answer = ("目前無法產生可驗證的 read-only evidence plan；未執行任何 tool。"
                              "execution_status=NOT_EXECUTED。請補充可查證的範圍或 orchestration_run_id。")
                return AgentResult(
                    answer=answer,
                    evidence=evidence, rounds=0, elapsed_sec=self._clock() - start,
                    trigger_source=trigger_source,
                )
            plan = validation.plan
            # ACTION_RESULT_ANALYSIS 只有在 host 端真的有 active
            # trusted_result 時才有意義；model 誤判/幻覺出這個 intent 但 host
            # 沒有對應 context 時，明確回 RESULT_CONTEXT_REQUIRED，不猜測
            # 「剛才」指什麼、不呼叫任何 tool。
            if plan is not None and plan.intent is TurnIntent.ACTION_RESULT_ANALYSIS and trusted_result is None:
                agent_audit.emit("turn.action_result.context_required", result_status="required",
                                 executor_entered=False)
                return AgentResult(
                    answer=(
                        "RESULT_CONTEXT_REQUIRED：目前沒有可用的 active trusted action result。"
                        "請先用 /results 查看，或用 /use-result <result_id> <result_digest> 選擇一筆。"
                    ),
                    evidence=evidence, rounds=0, elapsed_sec=self._clock() - start,
                    trigger_source=trigger_source,
                )
            if plan is not None and plan.intent is TurnIntent.ACTION_RESULT_ANALYSIS and trusted_result is not None:
                agent_audit.emit("turn.action_result.context_selected", result_status="selected",
                                 executor_entered=False,
                                 result_value={"result_id": trusted_result.get("result_id")})
            capability_names = validation.selected_tools
            planner_repaired = validation.repaired
            plan_for_proposal = validation.plan
            discarded_proposed_action_code = validation.discarded_proposed_action_code
            if plan_for_proposal is not None and plan_for_proposal.result_interpretation is not None:
                result_interpretation_denial_code = self._validate_and_attach_result_interpretation(plan_for_proposal)
            agent_audit.emit("turn.capabilities.selected", result_status="selected",
                             advertised_tools=list(capability_names),
                             result_value={"tool_count": len(capability_names)})
            if not capability_names:
                # 零 tool 的 answer round：明確告知模型本回合沒有 tool，避免它在沒有 schema 的
                # 情況下自行輸出 tool call 標記。只改這個回合的 system message 內容。
                messages[0] = {**messages[0], "content": messages[0]["content"] + NO_TOOLS_TURN_NOTICE}
                agent_audit.emit("turn.no_tools_notice.applied", result_status="applied",
                                 result_value={"tool_count": 0})

        round_limit = min(self._max_rounds, 4) if capability_names is not None else self._max_rounds
        for round_num in range(round_limit):
            # 進入每一輪前先檢查總 budget
            elapsed = self._clock() - start
            if elapsed > self._timeout_sec:
                if planner_repaired:
                    self._emit_repair_outcome(evidence, "budget_exhausted")
                return self._best_effort(
                    messages, evidence, trigger_source, round_num, start
                )

            # 呼叫 LLM
            try:
                tool_schemas = (self._tools.get_schemas_for_names(capability_names)
                                if capability_names is not None else self._tools.get_schemas(format=self._tool_format))
                if isinstance(tool_schemas, list):
                    agent_audit.emit(
                        "llm.tools.advertised",
                        result_status="advertised",
                        advertised_tools=[
                            str(item.get("function", {}).get("name", ""))
                            for item in tool_schemas if isinstance(item, dict)
                        ],
                        result_value={"tool_count": len(tool_schemas)},
                    )
                response = self._llm.chat(messages, tools=tool_schemas)
            except Exception as e:
                _log.warning(
                    "AgentRunner: LLM call failed at round %d: %s", round_num, e
                )
                return AgentResult(
                    answer=None,
                    no_action=False,
                    evidence=evidence,
                    rounds=round_num,
                    elapsed_sec=self._clock() - start,
                    trigger_source=trigger_source,
                    error=f"LLM call failed: {e}",
                    error_code=_gate_error_code(e),
                )

            rounds_done = round_num + 1
            elapsed = self._clock() - start

            # async triage：第一輪結束時若超過 triage budget 直接 NO_ACTION
            if (
                mode == "async"
                and round_num == 0
                and elapsed > self._triage_timeout_sec
            ):
                _log.info(
                    "AgentRunner: async triage exceeded budget "
                    "(%.2fs > %.2fs), returning NO_ACTION",
                    elapsed,
                    self._triage_timeout_sec,
                )
                return AgentResult(
                    answer=None,
                    no_action=True,
                    evidence=evidence,
                    rounds=rounds_done,
                    elapsed_sec=elapsed,
                    trigger_source=trigger_source,
                )

            # 解析回應
            try:
                parsed = self._parse(response)
            except Exception as e:
                _log.warning("AgentRunner: parse failed: %s", e)
                return AgentResult(
                    answer=None,
                    evidence=evidence,
                    rounds=rounds_done,
                    elapsed_sec=elapsed,
                    trigger_source=trigger_source,
                    error=f"Response parse failed: {e}",
                )

            if parsed.no_action:
                return AgentResult(
                    answer=None,
                    no_action=True,
                    evidence=evidence,
                    rounds=rounds_done,
                    elapsed_sec=elapsed,
                    trigger_source=trigger_source,
                )

            if not parsed.has_tool_calls:
                if planner_repaired:
                    self._emit_repair_outcome(evidence, "answered")
                answer_text = parsed.text
                interpretation_suffix = self._format_result_interpretation(
                    plan_for_proposal.result_interpretation if plan_for_proposal else None,
                    result_interpretation_denial_code,
                )
                if interpretation_suffix:
                    answer_text = f"{answer_text}\n\n{interpretation_suffix}"
                proposal_suffix = self._maybe_create_proposal(plan_for_proposal, evidence, proposable_catalog, trusted_result)
                if proposal_suffix is None and discarded_proposed_action_code is not None:
                    proposal_suffix = (
                        "proposed_action_status=DISCARDED\n"
                        f"reason={discarded_proposed_action_code}\n"
                        "execution_status=NOT_EXECUTED"
                    )
                if proposal_suffix:
                    answer_text = f"{answer_text}\n\n{proposal_suffix}"
                plan_suffix = self._maybe_create_plan(plan_for_proposal, proposable_catalog)
                if plan_suffix:
                    answer_text = f"{answer_text}\n\n{plan_suffix}"
                continuation_suffix = self._maybe_apply_plan_continuation(plan_for_proposal)
                if continuation_suffix:
                    answer_text = f"{answer_text}\n\n{continuation_suffix}"
                task_suffix = self._maybe_create_analysis_task(plan_for_proposal)
                if task_suffix:
                    answer_text = f"{answer_text}\n\n{task_suffix}"
                flow_suffix = self._maybe_create_flow_task(plan_for_proposal)
                if flow_suffix:
                    answer_text = f"{answer_text}\n\n{flow_suffix}"
                return AgentResult(
                    answer=answer_text,
                    no_action=False,
                    evidence=evidence,
                    rounds=rounds_done,
                    elapsed_sec=elapsed,
                    trigger_source=trigger_source,
                )

            # 有 tool calls → 執行並把 result append 回 messages
            self._execute_tool_calls(parsed.tool_calls, messages, evidence, round_num, capability_names)

        # 超過 max_rounds：best_effort
        if planner_repaired:
            self._emit_repair_outcome(evidence, "max_rounds_exceeded")
        return self._best_effort(
            messages, evidence, trigger_source, self._max_rounds, start
        )

    # ── Internal ─────────────────────────────────────────────────────────────

    def _parse(self, response: Any) -> ParsedResponse:
        if self._tool_format == "openai":
            if isinstance(response, str):
                # provider 回傳純字串（無 tool calls）→ 包成 text-only dict
                return self._parser.parse_openai({"text": response})
            return self._parser.parse_openai(response or {})

        # xml：response 必須是字串
        if isinstance(response, str):
            text = response
        elif isinstance(response, dict):
            text = response.get("text", "") or ""
        else:
            text = ""
        return self._parser.parse_xml(text)

    def _emit_repair_outcome(self, evidence: list[ToolEvidence], decision_status: str) -> None:
        """只在 host deterministic evidence fallback 觸發過的 turn 上，
        串起「evidence 是否足夠」與「最終決策」這兩個 repair audit 事件。不影響
        未經 fallback 的一般 turn（evidence-first planner 既有 audit 行為不變）。"""
        sufficiency = "sufficient" if any(e.success for e in evidence) else "insufficient"
        agent_audit.emit("turn.evidence.sufficiency", result_status=sufficiency,
                         result_value={"evidence_count": len(evidence)})
        agent_audit.emit("turn.decision.completed", result_status=decision_status,
                         policy_source="host_deterministic_repair")

    def _maybe_create_proposal(
        self,
        plan: StructuredTurnPlan | None,
        evidence: list[ToolEvidence],
        catalog: list[ProposableCapability],
        trusted_result: dict[str, Any] | None = None,
    ) -> str | None:
        """只在 intent 為 ACTION_PROPOSAL/EXECUTION_REQUEST 且 model
        宣告了 proposed_action 時嘗試建立一筆 PENDING proposal。完全不呼叫
        action handler；建立失敗只記 audit、不影響本 turn 已產生的 evidence
        答案。回傳值只是要附加在 answer 後面的 host-composed 摘要文字。

        若 proposed_action 帶 result_bindings，一併轉成
        `proposal_registry.ResultBinding` 交給 `ProposalRegistry.create()`
        做 deterministic resolve；這是全新 proposal（全新 id/digest），不
        繼承任何前一筆 proposal 的狀態。"""
        if self._proposal_registry is None or plan is None or plan.proposed_action is None:
            return None
        if plan.intent not in (TurnIntent.ACTION_PROPOSAL, TurnIntent.EXECUTION_REQUEST):
            return None
        context = try_get_current_request_context()
        if context is None:
            agent_audit.emit("action.proposal.validation_failed", denial_code="PROPOSAL_NO_REQUEST_CONTEXT",
                             tool_name=plan.proposed_action.action_name, executor_entered=False)
            return None

        evidence_references = [
            f"{e.tool_name}:{'success' if e.success else 'error'}" for e in evidence
        ]
        proposed = plan.proposed_action
        result_bindings = [
            ResultBinding(
                argument_name=b.argument_name, result_id=b.result_id,
                result_digest=b.result_digest, json_pointer=b.json_pointer,
            )
            for b in proposed.result_bindings
        ]
        handle_bindings = [
            HandleBinding(
                argument_name=b.argument_name, result_id=b.result_id,
                result_digest=b.result_digest, handle_id=b.handle_id,
            )
            for b in proposed.handle_bindings
        ]
        candidate_bindings = [
            CandidateBinding(argument_name=b.argument_name, candidate_id=b.candidate_id)
            for b in proposed.candidate_bindings
        ]
        active_result_ref = (
            (trusted_result["result_id"], trusted_result["result_digest"])
            if trusted_result is not None else None
        )
        is_follow_up = bool(result_bindings or handle_bindings or candidate_bindings)
        for cb in candidate_bindings:
            agent_audit.emit("action.binding_candidate.selected", tool_call_id=cb.candidate_id,
                             tool_name=proposed.action_name, executor_entered=False,
                             result_value={"argument_name": cb.argument_name})
        for hb in handle_bindings:
            agent_audit.emit("action.result.handle.selected", tool_call_id=hb.handle_id,
                             tool_name=proposed.action_name, executor_entered=False,
                             result_value={"argument_name": hb.argument_name, "result_id": hb.result_id})
        if handle_bindings or result_bindings:
            # provider 的 literal_arguments（legacy `arguments`
            # 欄位）與 handle_bindings 在這裡 normalize 成同一份 materialization
            # 輸入；這個事件只記錄「這個 turn 嘗試了 normalize」，不代表最終
            # 一定成功（成功/失敗各自的事件在下面分別 emit）。
            agent_audit.emit("action.proposal.arguments.normalized", tool_name=proposed.action_name,
                             executor_entered=False,
                             result_value={
                                 "literal_argument_count": len(proposed.normalized_literal_arguments()),
                                 "handle_binding_count": len(handle_bindings),
                             })
        # proposed_action 若帶 plan_id/plan_digest，代表這是在
        # materialize 一筆既有 plan 的 active step——改走
        # `PlanRegistry.materialize_step()`（內部仍然呼叫同一個
        # `ProposalRegistry.create()`，只是多做 plan/step 狀態記錄），不是
        # 一筆獨立 proposal。沒有 plan_id 時完全是既有 evidence-first planner～C6 行為，
        # 不受影響。
        is_plan_step = bool(proposed.plan_id and proposed.plan_digest)
        if is_plan_step and self._plan_registry is None:
            agent_audit.emit("action.proposal.validation_failed", denial_code="PLAN_REGISTRY_UNAVAILABLE",
                             tool_name=proposed.action_name, executor_entered=False)
            return None
        try:
            if is_plan_step:
                proposal = self._plan_registry.materialize_step(
                    proposed.plan_id, proposed.plan_digest, context,
                    arguments=proposed.normalized_literal_arguments(), reason=proposed.reason,
                    expected_effects=proposed.expected_effects, possible_side_effects=proposed.possible_side_effects,
                    preconditions=proposed.preconditions, evidence_references=evidence_references,
                    catalog=catalog, result_bindings=result_bindings,
                    handle_bindings=handle_bindings, candidate_bindings=candidate_bindings,
                    active_result_ref=active_result_ref,
                )
            else:
                proposal = self._proposal_registry.create(
                    action_name=proposed.action_name, arguments=proposed.normalized_literal_arguments(), reason=proposed.reason,
                    expected_effects=proposed.expected_effects, possible_side_effects=proposed.possible_side_effects,
                    preconditions=proposed.preconditions, evidence_references=evidence_references,
                    catalog=catalog, context=context, result_bindings=result_bindings,
                    handle_bindings=handle_bindings, candidate_bindings=candidate_bindings,
                    active_result_ref=active_result_ref,
                )
        except (ProposalCreationError, PlanCreationError) as exc:
            agent_audit.emit("action.proposal.validation_failed", denial_code=exc.code,
                             tool_name=proposed.action_name, executor_entered=False)
            if handle_bindings:
                agent_audit.emit("action.proposal.materialization_failed", denial_code=exc.code,
                                 tool_name=proposed.action_name, executor_entered=False)
                agent_audit.emit("action.result.handle.denied", denial_code=exc.code,
                                 tool_name=proposed.action_name, executor_entered=False)
            if candidate_bindings:
                event = {
                    "PROPOSAL_BINDING_CANDIDATE_AMBIGUOUS": "action.binding_candidate.ambiguous",
                    "PROPOSAL_BINDING_CANDIDATE_UNKNOWN": "action.binding_candidate.unknown",
                    "RESULT_HANDLE_NOT_BINDABLE": "action.binding_candidate.not_bindable",
                    "RESULT_HANDLE_UNKNOWN": "action.binding_candidate.unknown",
                }.get(exc.code)
                if event is not None:
                    agent_audit.emit(event, denial_code=exc.code, tool_name=proposed.action_name,
                                     executor_entered=False)
            return None

        agent_audit.emit("action.proposal.created", tool_call_id=proposal.proposal_id,
                         tool_name=proposal.action_name, classification=proposal.risk_class,
                         result_status="PENDING", executor_entered=False,
                         result_value={"digest_prefix": proposal.proposal_digest[:12]})
        handle_argument_names = [name for name, src in proposal.argument_provenance.items() if src == "handle"]
        auto_bound_names = [name for name, src in proposal.argument_provenance.items() if src == "host_auto_bound"]
        legacy_repaired_names = [
            name for name, src in proposal.argument_provenance.items() if src == "legacy_normalized_auto_repair"
        ]
        candidate_names = [name for name, src in proposal.argument_provenance.items() if src == "candidate"]
        host_authoritative_names = handle_argument_names + auto_bound_names + legacy_repaired_names + candidate_names
        if handle_bindings:
            agent_audit.emit("action.result.binding.resolved", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={"binding_count": len(handle_bindings)})
        for arg_name in handle_argument_names:
            agent_audit.emit("action.proposal.handle_authoritative", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={"argument_name": arg_name})
        if candidate_bindings or auto_bound_names:
            agent_audit.emit("action.binding_candidates.created", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={"candidate_argument_count": len(candidate_names) + len(auto_bound_names)})
        for arg_name in auto_bound_names:
            agent_audit.emit("action.binding_candidate.auto_resolved", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={"argument_name": arg_name})
        for arg_name in legacy_repaired_names:
            agent_audit.emit("action.binding_candidate.legacy_normalized", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={"argument_name": arg_name})
        # 被 handle 覆寫掉的衝突 literal——只記錄
        # digest/type（`ActionProposal.ignored_literals` 本身就已經是
        # redacted 格式），絕不把原始值寫進 audit。
        for arg_name, meta in proposal.ignored_literals.items():
            agent_audit.emit("action.proposal.literal_ignored", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={"argument_name": arg_name, **meta})
        if handle_bindings or result_bindings:
            agent_audit.emit("action.proposal.arguments.materialized", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, executor_entered=False,
                             result_value={
                                 "proposal_digest_prefix": proposal.proposal_digest[:12],
                                 "handle_authoritative_arguments": handle_argument_names,
                                 "ignored_literal_arguments": list(proposal.ignored_literals),
                             })
        if is_follow_up:
            agent_audit.emit("action.follow_up.proposal.created", tool_call_id=proposal.proposal_id,
                             tool_name=proposal.action_name, result_status="PENDING", executor_entered=False,
                             result_value={"prior_result_refs": proposal.prior_result_refs})

        provenance_lines: list[str] = []
        if proposal.ignored_literals:
            ignored_names = ", ".join(sorted(proposal.ignored_literals))
            provenance_lines.append(
                f"host_authoritative_arguments={sorted(host_authoritative_names)}（來自 trusted result "
                f"handle/binding candidate）\n"
                f"literal_ignored_arguments=[{ignored_names}]（provider 提供的 literal 與 host 解析出的值不同，"
                f"已被忽略，最終值一律以 host 解析結果為準）"
            )
        elif host_authoritative_names:
            provenance_lines.append(
                f"host_authoritative_arguments={sorted(host_authoritative_names)}（來自 trusted result handle/binding candidate）"
            )
        provenance_text = ("\n" + "\n".join(provenance_lines)) if provenance_lines else ""
        # operator 在核准前必須看得到這筆 action 宣告的
        # effects 與 side effects，不能只看到 action name 與 arguments。
        effects_text = "".join(
            f"\n{label}={values}"
            for label, values in (
                ("expected_effects", list(proposal.expected_effects)),
                ("possible_side_effects", list(proposal.possible_side_effects)),
                ("preconditions", list(proposal.preconditions)),
            )
            if values
        )
        return (
            f"【Proposal 已建立，等待 operator 核准】\n"
            f"proposal_id={proposal.proposal_id}\n"
            f"proposal_digest={proposal.proposal_digest}\n"
            f"action={proposal.action_name}\n"
            f"risk_class={proposal.risk_class}\n"
            f"materialized_arguments={proposal.arguments}{provenance_text}{effects_text}\n"
            f"proposal_status=PENDING\n"
            f"execution_status={proposal.execution_status}\n"
            f"這只是一筆待核准的提案，不會有任何 action 被實際執行。"
            f"我（Agent）無法自行核准；請用 operator control command "
            f"（例如 /approve {proposal.proposal_id} {proposal.proposal_digest}）在同一個 session 內核准或拒絕。"
        )

    def _validate_and_attach_result_interpretation(self, plan: StructuredTurnPlan) -> str | None:
        """驗證 plan.result_interpretation 的每一條
        observed_facts reference；不合格就地清空（``plan.result_interpretation
        = None``）並回傳 denial code，讓呼叫端明示丟棄理由，不靜默放行未經
        驗證的「事實」宣稱。"""
        if self._proposal_registry is None:
            plan.result_interpretation = None
            return "RESULT_INTERPRETATION_UNAVAILABLE"
        context = try_get_current_request_context()
        if context is None:
            plan.result_interpretation = None
            return "RESULT_INTERPRETATION_NO_REQUEST_CONTEXT"
        valid, code = validate_result_interpretation(
            plan.result_interpretation, self._proposal_registry, context,
        )
        if not valid:
            agent_audit.emit("turn.result_interpretation.invalid", denial_code=code, executor_entered=False)
            agent_audit.emit("action.result.handle.denied", denial_code=code, executor_entered=False)
            plan.result_interpretation = None
            return code
        agent_audit.emit("turn.result_interpretation.validated", result_status="valid", executor_entered=False)
        if plan.result_interpretation.observed_facts:
            agent_audit.emit("action.result.handle.selected", executor_entered=False,
                             result_value={"observed_fact_count": len(plan.result_interpretation.observed_facts)})
        return None

    @staticmethod
    def _format_result_interpretation(
        interpretation: ResultInterpretation | None, denial_code: str | None,
    ) -> str | None:
        if interpretation is None:
            if denial_code is None:
                return None
            return (
                "result_interpretation_status=DISCARDED\n"
                f"reason={denial_code}\n"
                "execution_status=NOT_EXECUTED"
            )
        lines = ["【Result Interpretation（依 HOST_ACTION_RESULT 驗證過的事實）】"]
        for fact in interpretation.observed_facts:
            lines.append(f"- {fact.claim}（ref={fact.reference}）")
        if interpretation.interpretation:
            lines.append(f"interpretation: {interpretation.interpretation}")
        lines.append(f"completion_decision: {interpretation.completion_decision}")
        if interpretation.missing_information:
            lines.append("missing_information: " + "; ".join(interpretation.missing_information))
        if interpretation.recommended_next_step:
            lines.append(f"recommended_next_step: {interpretation.recommended_next_step}")
        lines.append("execution_status=NOT_EXECUTED")
        return "\n".join(lines)

    def _fetch_handle_catalog(self, trusted_result: dict[str, Any] | None) -> dict[str, Any] | None:
        """只從 daemon 內部 `ProposalRegistry` 讀取這筆
        trusted_result 的 host deterministic handle catalog；沒有 registry
        或沒有 trusted_result 時回傳 None（provider 完全看不到任何 handle，
        `proposed_action`/`result_interpretation` 裡的 handle_bindings/
        observed_facts 之後在驗證階段就會全部失敗，fail-closed）。"""
        if not trusted_result or self._proposal_registry is None:
            return None
        context = try_get_current_request_context()
        if context is None:
            return None
        try:
            return self._proposal_registry.get_handle_catalog_for_scope(
                trusted_result["result_id"], trusted_result["result_digest"], context,
            )
        except Exception:
            return None

    def _fetch_binding_candidate_catalogs(
        self, trusted_result: dict[str, Any] | None, catalog: list[ProposableCapability],
    ) -> dict[str, dict[str, Any]]:
        """對 catalog 內每個 proposable action，各自從 host
        deterministic 建出這個 action 專屬的 BindingCandidateCatalog
        （provider-safe view，不含 handle_id）。沒有 trusted_result／registry
        ／catalog 時回傳空 dict（provider 完全看不到任何候選，
        candidate_bindings 之後在 `ProposalRegistry.create()` 會全部驗證
        失敗，fail-closed）。"""
        if not trusted_result or self._proposal_registry is None or not catalog:
            return {}
        context = try_get_current_request_context()
        if context is None:
            return {}
        catalogs: dict[str, dict[str, Any]] = {}
        for cap in catalog:
            try:
                view = self._proposal_registry.get_binding_candidate_catalog_for_scope(
                    cap.action_name, cap.parameters,
                    trusted_result["result_id"], trusted_result["result_digest"], context,
                )
            except Exception:
                view = None
            if view:
                catalogs[cap.action_name] = view
        return catalogs

    def _build_proposable_catalog(self) -> list[ProposableCapability]:
        """每個 turn 重新從 registry 建立，不快取、不信任 provider
        自報能力。沒有 provider 時回傳空 list（proposed_action 不會有作用）。"""
        if self._proposable_catalog_provider is None:
            return []
        try:
            return self._proposable_catalog_provider()
        except Exception:
            return []

    def _fetch_active_plan_context(self) -> dict[str, Any] | None:
        """只讀取這個 session 明確用 `/use-plan` 選過的 plan
        （或剛建立、自動成為 active 的那一筆）；沒有 plan_registry 或沒有
        request context 時回傳 None，provider 完全看不到任何 plan
        context，`plan_continuation` 之後在驗證階段會全部失敗，
        fail-closed。"""
        if self._plan_registry is None:
            return None
        context = try_get_current_request_context()
        if context is None:
            return None
        try:
            return self._plan_registry.peek_active_plan(context)
        except Exception:
            return None

    def _maybe_create_plan(
        self, plan: StructuredTurnPlan | None, catalog: list[ProposableCapability],
    ) -> str | None:
        """只在 intent 為 PLAN_PROPOSAL 且 model 宣告了
        proposed_plan 時嘗試建立一筆 plan。完全不建立任何 proposal、不
        呼叫任何 action handler。"""
        if self._plan_registry is None or plan is None or plan.proposed_plan is None:
            return None
        if plan.intent is not TurnIntent.PLAN_PROPOSAL:
            return None
        context = try_get_current_request_context()
        if context is None:
            agent_audit.emit("action.plan.validation_failed", denial_code="PLAN_NO_REQUEST_CONTEXT",
                             executor_entered=False)
            return None
        proposed = plan.proposed_plan
        step_decls = [
            PlanStepDeclaration(
                action_name=s.action_name, purpose=s.purpose, prerequisites=s.prerequisites,
                expected_effects=s.expected_effects, stop_conditions=s.stop_conditions,
            )
            for s in proposed.steps
        ]
        try:
            new_plan = self._plan_registry.create_plan(
                objective=proposed.objective, steps=step_decls, evidence_references=[],
                catalog=catalog, context=context,
            )
        except PlanCreationError as exc:
            agent_audit.emit("action.plan.validation_failed", denial_code=exc.code, executor_entered=False)
            return None
        step_lines = [f"  {s.step_index}. {s.action_name} — {s.purpose}" for s in new_plan.steps]
        return (
            "【Plan 已建立，尚未提出任何 proposal，未執行任何 action】\n"
            f"plan_id={new_plan.plan_id}\n"
            f"plan_digest={new_plan.plan_digest}\n"
            f"objective={new_plan.objective}\n"
            "steps:\n" + "\n".join(step_lines) + "\n"
            f"active_step_index={new_plan.active_step_index}\n"
            "plan_status=ACTIVE\n"
            "execution_status=NOT_EXECUTED\n"
            "只有目前這一步可以被提案；之後每一步都要在前一步結果出來後才會"
            "個別提案，各自需要獨立的 /approve 與 /execute，沒有一次核准整條"
            "plan 的機制。"
        )

    def _maybe_create_analysis_task(self, plan: StructuredTurnPlan | None) -> str | None:
        """只在 intent 為 ANALYSIS_TASK_PROPOSAL 且模型宣告了
        `proposed_analysis_task` 時，用 host 的 create 入口建立一筆 **DRAFT**。

        建立草案**不執行任何 action、不消耗任何授權**。回覆文字完全由 host
        已知的 task view 產生——模型不能自行聲稱「已開始」或「已完成」。
        """
        if self._analysis_task_creator is None or plan is None:
            return None
        if plan.proposed_analysis_task is None:
            return None
        if plan.intent is not TurnIntent.ANALYSIS_TASK_PROPOSAL:
            return None
        proposed = plan.proposed_analysis_task
        current = (proposed.current_run_id or "").strip()
        baseline = (proposed.baseline_run_id or "").strip()
        question = (proposed.question or "").strip()
        # 缺 run id 時提問，**不**用 `_latest_run_id()` 之類補值。
        if not current or not baseline:
            return ("需要兩個 run id 才能建立這個固定兩步分析：請提供要解釋的 "
                    "run 與要比較的 baseline run。（未建立任何 task）")
        if current == baseline:
            return "current 與 baseline 必須是不同的 run。（未建立任何 task）"
        if not question:
            return "需要一個非空的問題描述。（未建立任何 task）"
        if len(question) > 4096:
            return "問題描述超過 4096 字元。（未建立任何 task）"

        response = self._analysis_task_creator(
            current_run_id=current, baseline_run_id=baseline, question=question,
        )
        from terminux_engine.bounded_execution.analysis_tasks.rendering import render

        # 只有真的建立了草案才加開頭；失敗時第一行就是拒絕訊息。
        if response.get("status") == "error":
            return render(response)
        return ("【已建立分析 task 草案，尚未授權，未執行任何 action】\n"
                + render(response))

    def _maybe_create_flow_task(self, plan: StructuredTurnPlan | None) -> str | None:
        """只在 intent 為 FLOW_TASK_PROPOSAL 且模型宣告了
        `proposed_flow_task` 時，用 host 的 create 入口建立一筆 **DRAFT**。

        建立草案**不執行 EDA、不寫 candidate、不消耗任何授權**。回覆文字
        完全由 host 已知的 task view 產生。
        """
        if self._flow_task_creator is None or plan is None:
            return None
        if plan.proposed_flow_task is None:
            return None
        if plan.intent is not TurnIntent.FLOW_TASK_PROPOSAL:
            return None
        proposed = plan.proposed_flow_task
        profile_id = (proposed.profile_id or "").strip()
        question = (proposed.question or "").strip()
        if not profile_id:
            return "需要一個 server 已公開的 profile_id 才能建立這個任務。（未建立任何 task）"
        if not question:
            return "需要一個非空的問題描述。（未建立任何 task）"
        if len(question) > 4096:
            return "問題描述超過 4096 字元。（未建立任何 task）"
        response = self._flow_task_creator(profile_id=profile_id, question=question)
        from terminux_engine.bounded_execution.analysis_tasks.flow_rendering import (
            render_flow_task,
        )

        # 只有真的建立了草案才加開頭；失敗時第一行就是拒絕訊息。
        if response.get("status") == "error":
            return render_flow_task(response)
        return ("【已建立執行任務草案，尚未授權，未執行任何 EDA】\n"
                + render_flow_task(response))

    def _maybe_apply_plan_continuation(self, plan: StructuredTurnPlan | None) -> str | None:
        """只在 intent 為 PLAN_CONTINUATION 且 model 宣告了
        plan_continuation 時套用。這個呼叫本身**永遠不會執行任何
        action**——CONTINUE 只是讓下一步的 active_step_index 往前移，
        使用者仍必須另外請 Agent 針對新的 active step 提出
        proposed_action 才會建立下一筆 proposal。"""
        if self._plan_registry is None or plan is None or plan.plan_continuation is None:
            return None
        if plan.intent is not TurnIntent.PLAN_CONTINUATION:
            return None
        context = try_get_current_request_context()
        if context is None:
            agent_audit.emit("action.plan.validation_failed", denial_code="PLAN_NO_REQUEST_CONTEXT",
                             executor_entered=False)
            return None
        decision = plan.plan_continuation
        revised_steps = [
            PlanStepDeclaration(
                action_name=s.action_name, purpose=s.purpose, prerequisites=s.prerequisites,
                expected_effects=s.expected_effects, stop_conditions=s.stop_conditions,
            )
            for s in decision.revised_steps
        ] if decision.decision == "REVISE" else None
        updated_plan, denial_code = self._plan_registry.apply_continuation(
            decision.plan_id, decision.plan_digest, context,
            decision=decision.decision, revised_future_steps=revised_steps,
        )
        if updated_plan is None:
            agent_audit.emit("action.plan.control_denied", tool_call_id=decision.plan_id,
                             denial_code=denial_code, executor_entered=False)
            return (
                f"plan_continuation_status=DENIED\nreason={denial_code}\n"
                "execution_status=NOT_EXECUTED"
            )
        return (
            f"plan_id={updated_plan.plan_id}\n"
            f"plan_digest={updated_plan.plan_digest}\n"
            f"plan_revision={updated_plan.revision}\n"
            f"plan_status={updated_plan.status}\n"
            f"decision={decision.decision}\n"
            "execution_status=NOT_EXECUTED\n"
            "這個決定本身不會執行任何 action；若 plan_status=ACTIVE，需要"
            "另外針對新的 active step 提出 proposed_action 才會建立下一筆"
            "proposal。"
        )

    def _prepare_read_only_plan(
        self, query: str, catalog: list[ProposableCapability],
        trusted_result: dict[str, Any] | None = None,
        handle_catalog: dict[str, Any] | None = None,
        binding_candidate_catalogs: dict[str, dict[str, Any]] | None = None,
        active_plan: dict[str, Any] | None = None,
    ) -> PlanValidation:
        """模型 proposal 最多一次 provider-side repair；兩次都失敗時，嘗試
        evidence-first planner host-side deterministic evidence fallback（不再呼叫 LLM，每個
        turn 最多一次）；仍不合格才 fail-closed，絕不開始 tool loop。"""
        catalog_text = catalog_prompt_text(catalog)
        trusted_result_text = trusted_result_prompt_text(trusted_result, handle_catalog)
        binding_candidate_text = binding_candidate_catalog_prompt_text(binding_candidate_catalogs or {})
        plan_prompt_text = active_plan_prompt_text(active_plan)
        task_profiles_text = ""
        if self._task_profile_provider is not None:
            try:
                task_profiles_text = task_profiles_prompt_text(
                    self._task_profile_provider())
            except Exception:  # noqa: BLE001 - 取不到就是沒有可用 profile
                task_profiles_text = ""
        attempts: list[tuple[StructuredTurnPlan | None, str | None]] = []
        for attempt in range(2):
            try:
                response = self._llm.chat(
                    [{"role": "system", "content": planner_instruction(query, catalog_text, trusted_result_text, binding_candidate_text, plan_prompt_text, task_profiles_text) if attempt == 0 else repair_instruction()},
                     {"role": "user", "content": query}], tools=None)
                plan = parse_plan(response)
                agent_audit.emit("turn.plan.proposed", result_status="proposed",
                                 advertised_tools=plan.proposed_safe_tools,
                                 result_value={"intent": plan.intent.value, "step_count": len(plan.steps)})
                validation = validate_plan(plan, self._tools, query)
                agent_audit.emit("turn.plan.validation", result_status="valid" if validation.valid else "invalid",
                                 denial_code=validation.code, advertised_tools=list(validation.selected_tools),
                                 result_value={"step_count": len(plan.steps)})
                if validation.valid:
                    return self._maybe_repair_proposed_action_format(
                        validation, catalog, query, handle_catalog, binding_candidate_catalogs,
                    )
                attempts.append((plan, validation.code))
            except Exception:
                agent_audit.emit("turn.plan.validation", result_status="invalid", denial_code="PLAN_PARSE_FAILED")
                attempts.append((None, "PLAN_PARSE_FAILED"))
            if attempt == 0:
                agent_audit.emit("turn.plan.repaired", result_status="requested")

        # provider-side repair 用盡；嘗試 host-side deterministic fallback，
        # 用第一個「denial code 屬於可修復類別」的 attempt 資訊建構，最多一次。
        for failed_plan, denial_code in attempts:
            fallback = build_evidence_fallback(failed_plan, denial_code, self._tools, query, catalog)
            if fallback.valid:
                agent_audit.emit("turn.plan.repaired", result_status="host_deterministic_repair_applied",
                                 denial_code=denial_code, policy_source="host_deterministic_repair")
                agent_audit.emit("turn.plan.validation", result_status="valid", policy_source="host_deterministic_repair",
                                 advertised_tools=list(fallback.selected_tools),
                                 result_value={"step_count": len(fallback.plan.steps) if fallback.plan else 0})
                if fallback.plan is not None and fallback.plan.proposed_action is not None:
                    agent_audit.emit("action.proposal.repair_preserved", result_status="preserved",
                                     tool_name=fallback.plan.proposed_action.action_name, executor_entered=False)
                elif fallback.discarded_proposed_action_code is not None:
                    agent_audit.emit("action.proposal.repair_discarded", result_status="discarded",
                                     denial_code=fallback.discarded_proposed_action_code, executor_entered=False)
                return fallback
        agent_audit.emit("turn.plan.repaired", result_status="host_deterministic_repair_ineligible")
        return PlanValidation(False, "PLAN_REPAIR_EXHAUSTED", (), None)

    def _maybe_repair_proposed_action_format(
        self, validation: PlanValidation, catalog: list[ProposableCapability], query: str,
        handle_catalog: dict[str, Any] | None = None,
        binding_candidate_catalogs: dict[str, dict[str, Any]] | None = None,
    ) -> PlanValidation:
        """plan 本身已合法（`validate_plan()` 通過），但
        `proposed_action` 缺失／不合格時，允許**這個 user turn 內最多一次**
        額外的 bounded provider repair request——只問 proposed_action 這個
        小結構，不重跑整份 plan、repair pass 不給任何 tool（`tools=None`）、
        host 不自行發明 action 或 arguments。repair 出來的結果會再走一次
        完整的 `ProposalRegistry.create()` 驗證（在 `_maybe_create_proposal`
        裡），這裡只先做 catalog/schema 的第一層過濾。"""
        plan = validation.plan
        if plan is None or not proposed_action_format_repair_eligible(plan, catalog, query):
            return validation
        catalog_text = catalog_prompt_text(catalog)
        agent_audit.emit("action.proposal.format_repair.requested", denial_code="PROPOSED_ACTION_MISSING",
                         result_status="requested", result_value={"provider_call_count": 1})
        binding_candidate_text = binding_candidate_catalog_prompt_text(binding_candidate_catalogs or {})
        try:
            response = self._llm.chat(
                [{"role": "system", "content": proposed_action_repair_instruction(catalog_text, handle_catalog, binding_candidate_text)},
                 {"role": "user", "content": query}], tools=None)
            repaired_action = parse_proposed_action(response)
        except Exception:
            agent_audit.emit("action.proposal.format_repair.failed",
                             denial_code="PROPOSED_ACTION_FORMAT_REPAIR_PARSE_FAILED",
                             result_value={"provider_call_count": 1})
            return validation
        match = next((c for c in catalog if c.action_name == repaired_action.action_name), None)
        # 若 repaired_action 帶 handle_bindings/result_bindings，required
        # argument 可能故意留白、由 host 之後 resolve 補上——這裡只做「action
        # 存在」的第一層過濾，完整（含 binding 解析後）的 schema 驗證留給
        # `_maybe_create_proposal()` → `ProposalRegistry.create()` 做，避免
        # 提早用「還沒補值的 arguments」誤判成不合格。沒有任何 binding 時，
        # 維持原本嚴格檢查（及早拒絕明顯不合格的 JSON）。
        has_bindings = bool(
            repaired_action.handle_bindings or repaired_action.result_bindings
            or repaired_action.candidate_bindings
        )
        if match is None or (not has_bindings and not validate_action_arguments(match.parameters, repaired_action.normalized_literal_arguments())):
            agent_audit.emit("action.proposal.format_repair.failed",
                             denial_code="PROPOSED_ACTION_FORMAT_REPAIR_INVALID",
                             result_value={"provider_call_count": 1})
            return validation
        repaired_plan = plan.model_copy(update={"proposed_action": repaired_action})
        agent_audit.emit("action.proposal.format_repair.succeeded", tool_name=repaired_action.action_name,
                         result_value={"provider_call_count": 1})
        return PlanValidation(
            True, validation.code, validation.selected_tools, repaired_plan,
            repaired=validation.repaired, discarded_proposed_action_code=validation.discarded_proposed_action_code,
        )

    def _execute_tool_calls(
        self,
        tool_calls: list[ToolCallRequest],
        messages: list[dict[str, Any]],
        evidence: list[ToolEvidence],
        round_num: int,
        capability_names: tuple[str, ...] | None = None,
    ) -> None:
        # 紀錄 assistant 一次 round 內所有 tool_calls
        assistant_calls = []
        for i, call in enumerate(tool_calls):
            cid = call.call_id or f"call_{round_num}_{i}"
            assistant_calls.append(
                {
                    "id": cid,
                    "name": call.tool_name,
                    "arguments": call.arguments,
                }
            )
            if call.thought_signature is not None:
                # 原樣帶回 provider 的 opaque token（Gemini 3.x 要求）；
                # 其他 provider 不會有這個欄位，行為完全不變。
                assistant_calls[-1]["thought_signature"] = call.thought_signature
        task_scope.track_message({"role": "assistant", "tool_calls": assistant_calls}, "provider_tool_calls")
        messages.append({"role": "assistant", "tool_calls": assistant_calls})

        # 逐個執行 tool，append tool message + 紀錄 evidence
        for i, call in enumerate(tool_calls):
            cid = call.call_id or f"call_{round_num}_{i}"
            agent_audit.emit(
                "llm.tool_call.requested", tool_call_id=cid, tool_name=call.tool_name,
                argument_keys=list(call.arguments), argument_value=call.arguments,
                run_id=(
                    call.arguments.get("orchestration_run_id")
                    if isinstance(call.arguments.get("orchestration_run_id"), str)
                    else call.arguments.get("run_id") if isinstance(call.arguments.get("run_id"), str) else None
                ),
            )
            if _cloud_agent_mode() and call.tool_name not in (
                "query_orchestration_evidence", "query_orchestration_evidence_set"
            ):
                from terminux_engine.llm.agent_models import ToolCallResult
                result = ToolCallResult(call_id="", tool_name=call.tool_name, success=False,
                                        error="CLOUD_AGENT_CAPABILITY_DENIED", elapsed_sec=0.0)
            elif capability_names is not None and call.tool_name not in capability_names:
                # 未advertise的 DANGEROUS call 仍走 registry backstop；SAFE
                # call 則不進 executor，避免 capability escape。
                if task_scope.current() is None and self._tools.get_tool_risks().get(call.tool_name) is ToolRisk.DANGEROUS:
                    result = self._tools.execute(call.tool_name, call.arguments, tool_call_id=cid)
                else:
                    from terminux_engine.llm.agent_models import ToolCallResult
                    result = ToolCallResult(call_id="", tool_name=call.tool_name, success=False,
                                            error="TOOL_NOT_ADVERTISED", elapsed_sec=0.0)
            elif task_scope.current() is not None and (
                set(call.arguments) != {"orchestration_run_id"}
                or call.arguments.get("orchestration_run_id") not in task_scope.current().effective_run_ids
            ):
                from terminux_engine.llm.agent_models import ToolCallResult
                result = ToolCallResult(call_id="", tool_name=call.tool_name, success=False,
                                        error="RUN_SCOPE_DENIED", elapsed_sec=0.0)
            else:
                result = self._tools.execute(call.tool_name, call.arguments, tool_call_id=cid)
            summary_src = result.result if result.success else (result.error or "")
            evidence.append(
                ToolEvidence(
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    result_summary=summary_src[:500],
                    success=result.success,
                )
            )
            content = (
                result.result if result.success else f"ERROR: {result.error}"
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": call.tool_name,
                    "content": content,
                }
            )
            task_scope.track_message(messages[-1], "tool_registry")
            agent_audit.emit(
                "tool.result.delivered", tool_call_id=cid, tool_name=call.tool_name,
                result_status="success" if result.success else "error",
                result_value=content, duration_ms=result.elapsed_sec * 1000.0,
            )

    def _best_effort(
        self,
        messages: list[dict[str, Any]],
        evidence: list[ToolEvidence],
        trigger_source: str,
        rounds: int,
        start: float,
    ) -> AgentResult:
        """budget 用完時，請 LLM 基於已收集的 evidence 給最後一次回答。"""
        if evidence:
            summary_lines = [
                f"- {e.tool_name}({e.arguments}) → success={e.success}, "
                f"result={e.result_summary[:200]}"
                for e in evidence
            ]
            evidence_text = "\n".join(summary_lines)
        else:
            evidence_text = "（沒有 evidence）"

        followup = list(messages)
        followup.append(
            {
                "role": "user",
                "content": (
                    "budget 已用完。請依下列已收集的 evidence 給出簡短的 best-effort "
                    "回答，不要再呼叫任何 tool：\n" + evidence_text
                ),
            }
        )

        try:
            task_scope.track_message(followup[-1], "budget_summary")
            response = self._llm.chat(followup, tools=None)
            parsed = self._parse(response)
            text = parsed.text or "（無法生成最終回答）"
            elapsed = self._clock() - start
            return AgentResult(
                answer=text,
                no_action=False,
                evidence=evidence,
                rounds=rounds,
                elapsed_sec=elapsed,
                trigger_source=trigger_source,
            )
        except Exception as e:
            elapsed = self._clock() - start
            _log.warning("AgentRunner: best_effort failed: %s", e)
            return AgentResult(
                answer=None,
                no_action=False,
                evidence=evidence,
                rounds=rounds,
                elapsed_sec=elapsed,
                trigger_source=trigger_source,
                error=f"Agent best-effort failed: {e}",
                error_code=_gate_error_code(e),
            )
