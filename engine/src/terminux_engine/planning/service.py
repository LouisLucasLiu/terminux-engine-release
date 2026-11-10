# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`PlanOnlyPlanner`。

**By construction 不可能執行任何東西**：這個類別的建構子只接受
一個 LLM provider 與一份 capability snapshot。它沒有 tool executor、沒有
`ActionRegistry`、沒有 `ProposalRegistry`、沒有 Kernel、沒有
`SubprocessExecutor`，也不安裝任何 authorization policy——不是「有但不用」，
是根本拿不到 reference。

Model 若回 tool call／function call，一律 `PLAN_ONLY_TOOL_CALL_FORBIDDEN`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from terminux_engine.llm.fenced_output import strip_single_fence
from terminux_engine.llm.unparsed_tool_call import UNPARSED_TOOL_CALL_CODE
from terminux_engine.planning.capability import CapabilitySnapshot
from terminux_engine.planning.plan import (
    MAX_STEPS,
    PLAN_MODE,
    PLAN_SCHEMA_VERSION,
    Plan,
)
from terminux_engine.planning.validator import validate_plan


class AgentRunMode(str, Enum):
    """Agent request 的正式 mode。

    這是 runtime 的 typed field，**不是** prompt 裡的一句「不要執行」
。"""

    NORMAL = "normal"
    PLAN_ONLY = "plan_only"


#: Plan-only turn 專屬的拒絕代碼（validator 的 plan error code 之外）。
PLAN_ONLY_TOOL_CALL_FORBIDDEN = "PLAN_ONLY_TOOL_CALL_FORBIDDEN"
PLAN_ONLY_NO_RESPONSE = "PLAN_ONLY_NO_RESPONSE"
PLAN_ONLY_NOT_JSON = "PLAN_ONLY_NOT_JSON"
#: provider 回覆的 content 是推論服務沒解析的 tool call。
PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL = "PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL"

#: 允許的 provider 回覆 key。出現 tool call 相關 key 即拒絕。
_TOOL_CALL_KEYS = ("tool_calls", "function_call", "tool_call", "functionCall")


@dataclass(frozen=True)
class PlanRequest:
    goal: str
    mode: AgentRunMode = AgentRunMode.PLAN_ONLY
    observations: tuple[str, ...] = ()
    created_from: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanResult:
    plan: Plan | None
    raw_response: str | None
    repaired: bool
    denial_code: str | None = None
    errors: tuple[dict[str, Any], ...] = ()

    @property
    def valid(self) -> bool:
        return self.plan is not None and self.plan.validation.valid

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_json_dict() if self.plan is not None else None,
            "repaired": self.repaired,
            "denial_code": self.denial_code,
            "errors": [dict(e) for e in self.errors],
        }


class PlanOnlyPlanner:
    """只產生並驗證 plan。沒有任何執行能力。"""

    #: 明確列出這個服務**永遠不會**持有的依賴。這行不只是註解——
    #: `assert_no_execution_capability()` 會在 runtime 再檢查一次。
    FORBIDDEN_ATTRIBUTES: tuple[str, ...] = (
        "_tools", "_tool_registry", "_executor", "_action_registry",
        "_proposal_registry", "_plan_registry", "_kernel", "_dispatch_engine",
        "_subprocess_executor", "_ledger",
    )

    def __init__(self, llm_provider: Any, snapshot: CapabilitySnapshot) -> None:
        self._llm = llm_provider
        self._snapshot = snapshot

    # ── Public ───────────────────────────────────────────────────────

    @property
    def snapshot(self) -> CapabilitySnapshot:
        return self._snapshot

    def assert_no_execution_capability(self) -> None:
        """Fail-closed 自我檢查。任何人若日後把 executor 注進來就會爆。"""
        present = [a for a in self.FORBIDDEN_ATTRIBUTES if hasattr(self, a)]
        if present:
            raise RuntimeError(
                f"PlanOnlyPlanner must not hold execution capability: {present}")

    def plan(self, request: PlanRequest) -> PlanResult:
        """產生並驗證一份 plan。永遠不執行任何 step。"""
        if request.mode is not AgentRunMode.PLAN_ONLY:
            return PlanResult(plan=None, raw_response=None, repaired=False,
                              denial_code="PLAN_ONLY_MODE_REQUIRED",
                              errors=({"code": "PLAN_MODE_INVALID",
                                       "message": "PlanOnlyPlanner only accepts "
                                                  "AgentRunMode.PLAN_ONLY"},))
        self.assert_no_execution_capability()

        created_from = dict(request.created_from)
        created_from.setdefault("authoritative_observations", list(request.observations))
        created_from.setdefault("mode", PLAN_MODE)

        instruction = self.build_instruction(request)
        response, denial, detail = self._ask(instruction, request.goal)
        if denial is not None:
            return PlanResult(plan=None, raw_response=None, repaired=False,
                              denial_code=denial,
                              errors=({"code": denial, "message": detail or denial},))

        payload, parse_error = _parse(response)
        repaired = False
        if parse_error is not None:
            # 最多一次 repair，且**必須**帶同一份 capability snapshot。
            repair = self.build_instruction(request, repair_reason=parse_error)
            response2, denial2, detail2 = self._ask(repair, request.goal)
            if denial2 is not None:
                return PlanResult(plan=None, raw_response=response, repaired=True,
                                  denial_code=denial2,
                                  errors=({"code": denial2, "message": detail2 or denial2},))
            payload, parse_error = _parse(response2)
            repaired = True
            response = response2
            if parse_error is not None:
                return PlanResult(plan=None, raw_response=response, repaired=True,
                                  denial_code=PLAN_ONLY_NOT_JSON,
                                  errors=({"code": PLAN_ONLY_NOT_JSON,
                                           "message": parse_error},))

        plan = validate_plan(payload, self._snapshot, created_from=created_from)
        return PlanResult(plan=plan, raw_response=response, repaired=repaired,
                          errors=plan.validation.errors)

    def build_instruction(self, request: PlanRequest, *, repair_reason: str | None = None) -> str:
        """Repair 那一輪也帶同一份 catalog，修好的 plan 才不會失去 catalog 的對應。"""
        catalog_lines: list[str] = []
        for capability in self._snapshot.capabilities:
            catalog_lines.append(
                f"- {capability.capability_id} (risk={capability.risk}, "
                f"approval_required={capability.approval_required}, "
                f"availability={capability.availability['state']})\n"
                f"    description: {capability.description}\n"
                f"    input_schema: {json.dumps(capability.input_schema, ensure_ascii=False)}\n"
                f"    preconditions: {list(capability.preconditions)}\n"
                f"    evidence_produced: {list(capability.evidence_produced)}\n"
                f"    side_effects: {list(capability.side_effects)}\n"
                f"    limitations: {capability.availability['limitations']}"
            )
        header = (
            f"前一次輸出無法解析（{repair_reason}）。這次只輸出 JSON。\n\n"
            if repair_reason else "")
        observations = "\n".join(f"- {o}" for o in request.observations) or "- (none)"
        return (
            header
            + "你正在為使用者規劃步驟。這是 **plan-only** 模式：你只產生一份計畫，"
            "**不會**有任何步驟被執行，你也沒有任何工具可以呼叫。不要回 tool call／"
            "function call；那會被直接拒絕。\n\n"
            "只輸出一個 JSON object，不要說明、不要 markdown code fence。欄位："
            "`goal`、`observations`、`unknowns`、`steps`、`global_stop_conditions`、"
            "`limitations`。\n"
            f"`steps` 最多 {MAX_STEPS} 個，每個含 `step_id`（小寫英數／`_`／`-`）、"
            "`capability`（必須是下方清單中的 exact id）、`arguments`（具體值，"
            "不可用任何 placeholder 或跨步驟引用）、`depends_on`（同一份 plan 內的 "
            "step_id，不可成環）、`preconditions`、`expected_evidence`、"
            "`stop_conditions`（三者都不可為空）。\n"
            "不要填 `risk`、`approval_required`、`side_effects`、`plan_id` 或 "
            "`validation`——那些由 host 依權威資料填入，你填了也會被覆寫並記為不一致。\n"
            "不支援 if／loop／branch／retry／dynamic binding。\n"
            "`observations` 只能寫下方 OBSERVATIONS 已提供的內容；不得宣稱任何步驟"
            "已經執行、已經通過或已經改善。\n"
            f"`expected_evidence` 是「這一步**預期**會產生什麼」，不是已經觀察到的結果。\n\n"
            "OBSERVATIONS（唯一權威來源）：\n" + observations + "\n\n"
            "可用 capability（只有這些）：\n" + "\n".join(catalog_lines) + "\n\n"
            "GOAL：\n" + request.goal
        )

    # ── Internal ─────────────────────────────────────────────────────

    def _ask(self, instruction: str, goal: str) -> tuple[str | None, str | None, str | None]:
        """呼叫 provider，並拒絕任何 tool call 形式的回覆。

        回傳 (text, denial code, detail)。detail 只在能說出真正原因時才有值。
        """
        try:
            # 刻意不傳 tools：plan-only turn 不對 provider 宣告任何工具。
            response = self._llm.chat(
                [{"role": "system", "content": instruction},
                 {"role": "user", "content": goal}], tools=None)
        except Exception as exc:  # noqa: BLE001 - provider 失敗不得讓 planner 崩潰
            # provider 其實有設定、只是推論服務沒解析 tool call；
            # 帶出真正原因，不要說成 provider 不可用。訊息本身不含回覆內容。
            if getattr(exc, "code", None) == UNPARSED_TOOL_CALL_CODE:
                return None, PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL, str(exc)
            return None, PLAN_ONLY_NO_RESPONSE, None

        if isinstance(response, dict):
            for key in _TOOL_CALL_KEYS:
                if response.get(key):
                    return None, PLAN_ONLY_TOOL_CALL_FORBIDDEN, None
            text = response.get("text")
        else:
            text = response
        if not isinstance(text, str) or not text.strip():
            return None, PLAN_ONLY_NO_RESPONSE, None
        return text, None, None


def _parse(response: str | None) -> tuple[Any, str | None]:
    if not isinstance(response, str):
        return None, "response is not text"
    try:
        return json.loads(strip_single_fence(response)), None
    except json.JSONDecodeError as exc:
        return None, f"response is not valid JSON: {exc}"


__all__ = [
    "PLAN_ONLY_NOT_JSON",
    "PLAN_ONLY_NO_RESPONSE",
    "PLAN_ONLY_PROVIDER_UNPARSED_TOOL_CALL",
    "PLAN_ONLY_TOOL_CALL_FORBIDDEN",
    "AgentRunMode",
    "PlanOnlyPlanner",
    "PlanRequest",
    "PlanResult",
]
