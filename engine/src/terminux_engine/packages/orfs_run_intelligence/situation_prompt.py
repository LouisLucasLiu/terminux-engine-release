# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Situation prompt 組裝與輸出驗證。

單次 LLM 呼叫、最多一次 repair，且 **repair instruction 必須帶完整的 evidence
catalog 與 schema**：repair 那一輪若少了 catalog，修好的輸出就一定失去引用能力。

預設**不呼叫**真實 provider；`run_prompt()` 留給 validation 使用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.llm.fenced_output import strip_single_fence
from terminux_engine.situation.context import SituationContext, sha256_text
from terminux_engine.situation.snapshot import SUPPORT_STATUSES

PROMPT_ID = "orfs_assess_situation"
PROMPT_VERSION = "v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / "assess_situation_v1.md"

LLM_TIMEOUT_SECONDS = 120.0
MAX_ITEMS_PER_LIST = 12
MAX_TEXT_CHARS = 600

# ── Output budget ─────────────────────────────────────────
#
# 先前這裡呼叫 `llm_service.ask(prompt, timeout=...)`，完全不帶
# `max_output_tokens`。後果有兩層：
#   1. proactive intelligence 的 `_BudgetedLlm` 永遠以 `requested_output_tokens=0` 去
#      `reserve()`，`CycleBudgetLimits.max_requested_output_tokens` 這道
#      pre-call gate 在真實 proactive path 上**不可能觸發**；
#   2. 這條路徑也沒有 output budget 的保護——實測第一次
#      assessment 的 `finish_reason` 就是 `max_output_tokens`。
#
# 預算由**本檔自己的 schema 上限**推導，不是 magic number：
#   `interpretations` / `hypotheses` / `recommended_checks` 三個 list，
#   外加 `unknowns`，各最多 MAX_ITEMS_PER_LIST 筆；每筆最長 MAX_TEXT_CHARS，
#   再加上 id array 與 JSON 標點的固定額度。
#: Prompt 契約向 model 宣告的每個 list 上限。這是**推導 budget 用的 policy
#: 上限**，必須小於等於 `MAX_ITEMS_PER_LIST`（validator 的 fail-closed
#: backstop）。兩者刻意分開：prompt 說 4，validator 仍然容忍到 12，因此
#: model 稍微超出不會被當成 contract violation，但 budget 是依 prompt 實際
#: 要求的量推導，不是依 backstop 灌水。（與 explain_run `prompt_runner` 同慣例。）
PROMPT_ITEM_LIMIT_PER_LIST = 4
_BUDGET_LIST_COUNT = 4                      # 三個結構化 list ＋ unknowns
_CHARS_PER_ID_ARRAY = 240                   # evidence_refs / fact_ids / confirmation
_CHARS_JSON_OVERHEAD_PER_ITEM = 120         # 引號、逗號、key 名稱
_FIXED_SCHEMA_OVERHEAD_CHARS = 300          # 頂層 key 與 global_support_status
_CHARS_PER_TOKEN = 3.2
#: production model 是 thinking model：reasoning token 與可見輸出**共用**
#: 同一個 output budget，因此可見量必須再乘上 reasoning allowance。
_REASONING_ALLOWANCE_FACTOR = 3


def derive_output_budget_tokens() -> int:
    """從結構化輸出上限推導 output budget。可重算、有界、可測試。"""
    per_item = (MAX_TEXT_CHARS + _CHARS_PER_ID_ARRAY
                + _CHARS_JSON_OVERHEAD_PER_ITEM)
    chars = (_BUDGET_LIST_COUNT * PROMPT_ITEM_LIMIT_PER_LIST * per_item
             + _FIXED_SCHEMA_OVERHEAD_CHARS)
    visible = chars / _CHARS_PER_TOKEN
    total = visible * _REASONING_ALLOWANCE_FACTOR
    # 取整到 1024 的倍數，讓數字穩定可重算
    return int(-(-total // 1024) * 1024)


#: 每次生成的 output budget。刻意是**有界的正數**推導值，不是 unlimited，
#: 也不由 package／action／vendor 名稱推導。
OUTPUT_BUDGET_TOKENS = derive_output_budget_tokens()
assert OUTPUT_BUDGET_TOKENS > 0
#: prompt 契約的 item 上限不得超過 schema 的 fail-closed backstop。
assert PROMPT_ITEM_LIMIT_PER_LIST <= MAX_ITEMS_PER_LIST

EVIDENCE_OPEN = "<<<EVIDENCE {ref_id} subject={subject} source={source} section={section} lines={lines}>>>"
EVIDENCE_CLOSE = "<<<END EVIDENCE>>>"

#: Provider adapter 已正規化的 tool-call 訊號。Situation 這一側只認得這些
from terminux_engine.output_safety import find_command_invocation
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    DOMAIN_COMMAND_WORDS,
)

#: 既有欄位名；vendor/provider-specific 的解析留在 adapter。
_TOOL_CALL_KEYS: tuple[str, ...] = (
    "tool_calls", "tool_call", "function_call", "functionCall", "toolCalls")

#: recommended_checks 不得含可執行內容。判斷交給 Common 的
#: domain-blind `output_safety`；此處只補本 package 的 domain 命令名。
#: 舊版的裸 substring 比對會誤判 `confirm`／`perform`／`platform` 等正常
#: 英文，且與 explain_run 各有一份不同步的清單。


#: 與 explain_run 對齊：只有「格式／結構沒寫對」可以 repair。
CODE_NOT_JSON = "not_json"
CODE_SCHEMA = "schema"
CODE_UNSAFE_COMMAND = "unsafe_command"
CODE_EVIDENCE_SCOPE = "evidence_scope"
REPAIRABLE_CODES: frozenset[str] = frozenset({CODE_NOT_JSON, CODE_SCHEMA})


class SituationOutputError(ValueError):
    """LLM 輸出違反契約——不產生 snapshot。

    `code` 決定能不能 repair。output-safety 與 evidence-scope 違規一律不得
    repair：再問一次只是給模型第二次繞過守則的機會（Hard Rule 5）。
    """

    def __init__(self, message: str, *, code: str = CODE_SCHEMA) -> None:
        super().__init__(message)
        self.code = code

    @property
    def repairable(self) -> bool:
        return self.code in REPAIRABLE_CODES


class SituationToolCallForbiddenError(RuntimeError):
    """Model 回了 tool／function call。

    這**不是**一般的 JSON parse failure，因此有專屬錯誤碼、**不做 repair**，
    也絕不執行、轉交、模擬或自動核准那個 call（Workstream B）。
    """

    code = "SITUATION_MODEL_TOOL_CALL_FORBIDDEN"

    def __init__(self, summary: dict[str, Any]) -> None:
        super().__init__(
            f"the model returned a tool/function call, which is forbidden in a "
            f"read-only situation assessment: {summary}")
        #: 只保留安全、可稽核的摘要——**絕不**保留 arguments 內容，
        #: 那是未受信任資料，不得被當成可執行的東西帶下去。
        self.summary = dict(summary)


class LlmUnavailableError(RuntimeError):
    """Provider 無回覆／timeout——不產生假 snapshot。"""


class SituationBudgetDeniedError(RuntimeError):
    """Per-cycle budget 在**呼叫 provider 之前**拒絕了這次生成。

    這不是 provider 失敗，也不是格式錯誤：沒有任何 provider call 發生，
    因此不得 repair、不得產生 snapshot、不得發通知。
    """

    code = "SITUATION_PROVIDER_BUDGET_DENIED"

    def __init__(self, reason: str) -> None:
        super().__init__(f"provider budget denied this generation: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class SituationAttempt:
    """一次生成的可記錄事實。initial 與 repair **分開記錄**。

    `requested_output_tokens` 是送給 budget/provider 的宣告值；
    `completion_metadata` 是 provider 回報的實際用量（可能為 None——
    provider 不給就是不給，不推估）。
    """

    kind: str  # "initial" | "repair"
    prompt_sha256: str
    raw_response_sha256: str
    raw_response_chars: int
    requested_output_tokens: int
    completion_metadata: dict[str, Any] | None
    validation_error: str | None = None
    validation_error_code: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "prompt_sha256": self.prompt_sha256,
                "raw_response_sha256": self.raw_response_sha256,
                "raw_response_chars": self.raw_response_chars,
                "requested_output_tokens": self.requested_output_tokens,
                "completion_metadata": self.completion_metadata,
                "validation_error": self.validation_error,
                "validation_error_code": self.validation_error_code}


@dataclass(frozen=True)
class SituationPromptRun:
    prompt_text: str
    prompt_sha256: str
    raw_response: str
    raw_response_sha256: str
    parsed: dict[str, Any]
    repaired: bool = False
    #: initial（＋可能的 repair）逐次記錄；cycle total 可由此重算。
    attempts: tuple[SituationAttempt, ...] = ()

    def accounting(self) -> dict[str, Any]:
        """可重算的 cycle-level 統計。不推估任何缺席的數字。"""
        requested = sum(a.requested_output_tokens for a in self.attempts)
        observed = [a.completion_metadata for a in self.attempts]
        unavailable = sum(1 for m in observed if not m)
        return {"provider_call_count": len(self.attempts),
                "requested_output_tokens_total": requested,
                "usage_unavailable_call_count": unavailable,
                "finish_reasons": [(m or {}).get("finish_reason") for m in observed],
                "attempts": [a.to_json_dict() for a in self.attempts]}


def prompt_template_sha256() -> str:
    return sha256_text(PROMPT_PATH.read_text(encoding="utf-8"))


def render_prompt(context: SituationContext, *, repair_reason: str | None = None) -> str:
    """Repair 也走這個函式——catalog 因此永遠不會在 repair 時消失。"""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    domain = context.domain_payload or {}
    readiness = domain.get("readiness") or {}

    readiness_text = "\n".join(
        [f"- status: {readiness.get('status')}",
         f"- authoritative: {readiness.get('authoritative')}",
         f"- formal_signoff_gate: {readiness.get('formal_signoff_gate')}",
         "- basis:"] + [f"    - {b}" for b in (readiness.get("basis") or ["(none)"])]
        + ["- blockers:"] + [f"    - {b.get('check_id')}"
                             for b in (readiness.get("blockers") or [])] or ["(none)"])

    facts = "\n".join(
        f"- {f.fact_id}: {f.text}"
        + (f" [value={f.value}{' ' + f.unit if f.unit else ''}]" if f.value is not None else "")
        + (f" [evidence: {list(f.evidence_ref_ids)}]" if f.evidence_ref_ids else "")
        for f in context.deterministic_facts) or "- (none)"

    missing = "\n".join(
        f"- {m.requested_source_id} ({m.subject_id}): {m.reason} -> {m.impact}"
        for m in context.missing_sources) or "- (none)"
    limitations = "\n".join(f"- {l}" for l in context.limitations) or "- (none)"

    blocks: list[str] = []
    for ref in context.evidence_refs:
        lines = (f"{ref.line_start}-{ref.line_end}"
                 if ref.line_start is not None else "none")
        body = ref.excerpt_text or (
            f"(structured record {ref.record_identity} row {ref.row_identity})")
        blocks.append(
            EVIDENCE_OPEN.format(ref_id=ref.evidence_ref_id, subject=ref.subject_id,
                                 source=ref.source_id,
                                 section=ref.section or ref.row_identity, lines=lines)
            + "\n" + body + "\n" + EVIDENCE_CLOSE)
    evidence = "\n\n".join(blocks) or "(no readable evidence excerpts)"

    rendered = (
        template.replace("{{QUESTION}}", context.question or
                         "Assess the current situation of this design.")
        .replace("{{READINESS}}", readiness_text)
        .replace("{{FACTS}}", facts)
        .replace("{{MISSING}}", missing)
        .replace("{{LIMITATIONS}}", limitations)
        .replace("{{EVIDENCE}}", evidence)
        # Budget 是依這兩個數字推導的；prompt 必須把同一組數字告訴 model，
        # 否則「宣告的預算」與「允許的輸出量」會不一致。
        .replace("{{ITEM_LIMIT}}", str(PROMPT_ITEM_LIMIT_PER_LIST))
        .replace("{{TEXT_LIMIT}}", str(MAX_TEXT_CHARS))
    )
    if repair_reason:
        rendered = (f"前一次輸出無法使用（{repair_reason}）。這次只輸出符合 schema 的 "
                    f"JSON，其餘規則與可用 id 完全相同。\n\n" + rendered)
    return rendered


def _tool_call_summary(value: Any) -> dict[str, Any] | None:
    """回傳安全摘要；`None` 代表沒有偵測到 tool call。

    只計數與記錄形狀——**不**回傳 arguments。
    """
    if isinstance(value, dict):
        for key in _TOOL_CALL_KEYS:
            payload = value.get(key)
            if not payload:
                continue
            count = len(payload) if isinstance(payload, (list, tuple)) else 1
            return {"signal": key, "call_count": count,
                    "provider_response_kind": "structured_response"}
        # OpenAI-style nested shape：`choices[].message.tool_calls`
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                message = (choice or {}).get("message") if isinstance(choice, dict) else None
                if isinstance(message, dict):
                    summary = _tool_call_summary(message)
                    if summary is not None:
                        return {**summary, "provider_response_kind": "choices_message"}
    return None


def reject_tool_calls(response: Any) -> None:
    """在**任何** JSON parsing 之前檢查。

    兩條路徑都要擋：

    1. provider 回結構化 response（adapter 已正規化的 `tool_calls` 等欄位）；
    2. production 的 `LlmService.ask()` 只回文字，因此 tool call 只可能以
       「整份回覆就是一個 tool-call envelope」的 JSON 形式出現——同樣拒絕。

    即使 response 同時含合法的文字 JSON，只要帶 tool call 就一律拒絕。
    """
    summary = _tool_call_summary(response)
    if summary is not None:
        raise SituationToolCallForbiddenError(summary)
    if not isinstance(response, str):
        return
    try:
        parsed = json.loads(strip_single_fence(response.strip()))
    except json.JSONDecodeError:
        return
    summary = _tool_call_summary(parsed)
    if summary is not None:
        raise SituationToolCallForbiddenError(
            {**summary, "provider_response_kind": "text_json_envelope"})


def validate_output(
    raw: str, *, known_fact_ids: set[str], known_evidence_ref_ids: set[str]
) -> dict[str, Any]:
    """嚴格驗證。任何違規都拋 `SituationOutputError`——不產生半份 snapshot。"""
    try:
        payload = json.loads(strip_single_fence(raw.strip()))
    except json.JSONDecodeError as exc:
        raise SituationOutputError(f"output is not valid JSON: {exc}",
                                   code=CODE_NOT_JSON) from exc
    if not isinstance(payload, dict):
        raise SituationOutputError("output is not a JSON object")

    required = {"interpretations", "hypotheses", "unknowns", "recommended_checks",
                "global_support_status"}
    extra = set(payload) - required
    if extra:
        raise SituationOutputError(f"output contains keys outside the contract: "
                                   f"{sorted(extra)}")
    missing = required - set(payload)
    if missing:
        raise SituationOutputError(f"output is missing required keys: {sorted(missing)}")

    if payload["global_support_status"] not in SUPPORT_STATUSES:
        raise SituationOutputError(
            f"invalid global_support_status: {payload['global_support_status']!r}")

    def _ids(item: dict, key: str, known: set[str], label: str) -> list[str]:
        values = item.get(key) or []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise SituationOutputError(f"{label}.{key} must be a list of strings")
        unknown = [v for v in values if v not in known]
        if unknown:
            raise SituationOutputError(f"{label} references unknown {key}: {unknown}",
                                       code=CODE_EVIDENCE_SCOPE)
        return values

    for label in ("interpretations", "hypotheses", "recommended_checks"):
        items = payload[label]
        if not isinstance(items, list):
            raise SituationOutputError(f"{label} is not a list")
        if len(items) > MAX_ITEMS_PER_LIST:
            raise SituationOutputError(
                f"{label} has {len(items)} items (max {MAX_ITEMS_PER_LIST})")

    for item in payload["interpretations"]:
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise SituationOutputError("interpretation text is missing or too long")
        refs = _ids(item, "evidence_refs", known_evidence_ref_ids, "interpretation")
        _ids(item, "fact_ids", known_fact_ids, "interpretation")
        if not refs:
            raise SituationOutputError(
                f"interpretation {item.get('claim_id')!r} has no evidence reference")
        if item.get("support_status") not in SUPPORT_STATUSES:
            raise SituationOutputError(
                f"interpretation {item.get('claim_id')!r} has an invalid support status")

    for item in payload["hypotheses"]:
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise SituationOutputError("hypothesis text is missing or too long")
        _ids(item, "supporting_evidence_refs", known_evidence_ref_ids, "hypothesis")
        _ids(item, "fact_ids", known_fact_ids, "hypothesis")
        confirmation = item.get("missing_confirmation") or []
        if not isinstance(confirmation, list) or not confirmation:
            raise SituationOutputError(
                f"hypothesis {item.get('claim_id')!r} must say what would confirm it")

    unknowns = payload["unknowns"]
    if not isinstance(unknowns, list) or not unknowns:
        raise SituationOutputError("unknowns must be a non-empty list")

    for item in payload["recommended_checks"]:
        description = item.get("description") or ""
        finding = find_command_invocation(
            description, extra_commands=DOMAIN_COMMAND_WORDS)
        if finding is not None:
            raise SituationOutputError(
                f"recommended check {item.get('check_id')!r} contains executable "
                f"content ({finding.reason}: {finding.marker!r})",
                code=CODE_UNSAFE_COMMAND)
    return payload


def _ask(llm_service: Any, prompt_text: str) -> tuple[Any, dict[str, Any] | None]:
    """送出一次生成，並**一律**帶上有界的 `max_output_tokens`。

    優先走 additive 的 `ask_detailed()` 以取得 completion metadata；注入物件
    沒有這條 channel 時退回 `ask()`，metadata 誠實標為 None——這是介面相容，
    不是 provider/model 的 silent fallback。

    每次呼叫都自己算出 `GenerationOptions` 等價的區域值，不共用任何 mutable
    狀態，因此並行 request 不會互相污染。
    """
    detailed = getattr(llm_service, "ask_detailed", None)
    if callable(detailed):
        result = detailed(prompt_text, timeout=LLM_TIMEOUT_SECONDS,
                          max_output_tokens=OUTPUT_BUDGET_TOKENS)
        # budget wrapper 在 provider call **之前**拒絕時，text 為 None 且
        # error 帶拒絕原因；此時 provider call count 為 0。
        error = getattr(result, "error", None)
        if getattr(result, "text", None) is None and error:
            # budget denial 與 provider failure 必須分開：前者代表**沒有**
            # 任何 provider call 發生，後者代表呼叫出去了但拿不到東西。
            from terminux_engine.proactive.budget import BUDGET_DENIAL_REASONS
            if str(error) in BUDGET_DENIAL_REASONS:
                raise SituationBudgetDeniedError(str(error))
            raise LlmUnavailableError(f"LLM provider call failed: {error}")
        metadata = result.metadata.to_json_dict() if result.metadata else None
        return result.text, metadata
    return llm_service.ask(prompt_text, timeout=LLM_TIMEOUT_SECONDS,
                           max_output_tokens=OUTPUT_BUDGET_TOKENS), None


def run_prompt(llm_service: Any, context: SituationContext) -> SituationPromptRun:
    """單次呼叫；失敗時最多一次 repair（repair 仍帶完整 catalog）。"""
    known_facts = {f.fact_id for f in context.deterministic_facts}
    known_refs = {r.evidence_ref_id for r in context.evidence_refs}
    attempts: list[SituationAttempt] = []

    def _record(kind: str, prompt: str, raw: Any, metadata: dict[str, Any] | None,
                error: SituationOutputError | None = None) -> None:
        text = raw if isinstance(raw, str) else ""
        attempts.append(SituationAttempt(
            kind=kind, prompt_sha256=sha256_text(prompt),
            raw_response_sha256=sha256_text(text), raw_response_chars=len(text),
            requested_output_tokens=OUTPUT_BUDGET_TOKENS,
            completion_metadata=metadata,
            validation_error=None if error is None else str(error),
            validation_error_code=None if error is None else error.code))

    prompt_text = render_prompt(context)
    raw, metadata = _ask(llm_service, prompt_text)
    # Workstream B：tool-call 檢查必須在 JSON parsing **之前**，而且
    # 不做 repair——它是 contract violation，不是格式失誤。
    reject_tool_calls(raw)
    if not isinstance(raw, str) or not raw.strip():
        _record("initial", prompt_text, raw, metadata)
        raise LlmUnavailableError("LLM provider returned no usable response")
    try:
        parsed = validate_output(raw, known_fact_ids=known_facts,
                                 known_evidence_ref_ids=known_refs)
        _record("initial", prompt_text, raw, metadata)
        return SituationPromptRun(prompt_text, sha256_text(prompt_text), raw,
                                  sha256_text(raw), parsed, repaired=False,
                                  attempts=tuple(attempts))
    except SituationOutputError as first_error:
        _record("initial", prompt_text, raw, metadata, first_error)
        # 只有純文字 JSON 格式／schema 錯誤才 repair，且走同一個
        # render_prompt()——context、evidence catalog 與 output restrictions
        # 全部保留。output-safety 與 evidence-scope 違規**不得** repair。
        if not first_error.repairable:
            raise
        repair_text = render_prompt(context, repair_reason=str(first_error))
        raw2, metadata2 = _ask(llm_service, repair_text)
        reject_tool_calls(raw2)
        if not isinstance(raw2, str) or not raw2.strip():
            _record("repair", repair_text, raw2, metadata2)
            raise LlmUnavailableError("LLM provider returned no usable repair response")
        parsed = validate_output(raw2, known_fact_ids=known_facts,
                                 known_evidence_ref_ids=known_refs)
        _record("repair", repair_text, raw2, metadata2)
        return SituationPromptRun(repair_text, sha256_text(repair_text), raw2,
                                  sha256_text(raw2), parsed, repaired=True,
                                  attempts=tuple(attempts))


__all__ = [
    "LLM_TIMEOUT_SECONDS",
    "OUTPUT_BUDGET_TOKENS",
    "PROMPT_ITEM_LIMIT_PER_LIST",
    "LlmUnavailableError",
    "SituationAttempt",
    "SituationBudgetDeniedError",
    "derive_output_budget_tokens",
    "PROMPT_ID",
    "PROMPT_PATH",
    "PROMPT_VERSION",
    "SituationOutputError",
    "SituationPromptRun",
    "SituationToolCallForbiddenError",
    "reject_tool_calls",
    "prompt_template_sha256",
    "render_prompt",
    "run_prompt",
    "validate_output",
]
