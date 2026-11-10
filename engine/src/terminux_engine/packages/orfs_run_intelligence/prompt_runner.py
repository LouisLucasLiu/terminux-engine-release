# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Prompt 組裝、bounded LLM 呼叫與輸出驗證。

- 每次 action **最多兩次** LLM 呼叫：一次 initial ＋ 最多一次 repair。
  Repair 只在「格式／結構沒寫對」時發生，且走同一個 `render_prompt()`，
  所以 schema、context、evidence catalog 一個都不會在 repair 時消失。safety 與 evidence-scope 違規**不得** repair。
- Report 內容一律包在明確 delimiter 內，並在 prompt 中宣告為 untrusted data
。
- 回覆一律經 `schemas.validate_llm_output()`；任何違規都不產生 insight。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.packages.orfs_run_intelligence.context_builder import ContextSnapshot
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    _MAX_ITEMS_PER_LIST,
    LlmOutputError,
    sha256_text,
    validate_llm_output,
)

PROMPT_ID = "orfs_run_explain"
PROMPT_VERSION = "v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / "explain_run_v1.md"

LLM_TIMEOUT_SECONDS = 120.0

# ── Output budget 推導 ─────────────────────────────────
#
# 舊行為：router 從未把 budget 轉交 provider，實際生效的是
# `GeminiProviderConfig.max_tokens` 的 dataclass 預設 4096。explain_run 的結構化
# 輸出貼著這個上限，於是**間歇性**在 JSON 中途被截斷。
#
# 這裡從**結構化輸出的上限**推導一個有界值，不使用「很大」或 unlimited：
#
#   4 個 list（interpretations／hypotheses／unknowns／recommended_checks）
#   × 每個 list 的 prompt 契約上限 4 items
#   = 16 items（schema 的 fail-closed backstop 是每 list 12 items）
#
#   每個 item 的字元上限：
#     statement            ≤ 3 句 ≈ 300 chars
#     validation_needed    ≤ 300 chars（只有 hypotheses 有，這裡一律計入）
#     fact_ids/evidence_ref_ids ≤ 6 個 id × 40 chars = 240 chars
#     JSON key／標點／縮排 overhead        ≈ 120 chars
#                                        ─────────────
#                                          960 chars
#
#   16 items × 960 + 固定 schema overhead 200 = 15,560 chars
#   ÷ 3.2 chars/token ≈ 4,860 visible tokens
#
# 再乘上 reasoning allowance：production model 是 thinking model，reasoning
# token 與可見輸出**共用**同一個 output budget（實測：可見約 1,290 tokens
# 就把 4,096 用完）。以 3× 為界 ⇒ 14,580，取整到 16,384。
PROMPT_ITEM_LIMIT_PER_LIST = 4
_LIST_COUNT = 4
_CHARS_PER_STATEMENT = 300
_CHARS_PER_VALIDATION_NEEDED = 300
_CHARS_PER_ID_ARRAY = 240
_CHARS_JSON_OVERHEAD_PER_ITEM = 120
_FIXED_SCHEMA_OVERHEAD_CHARS = 200
_CHARS_PER_TOKEN = 3.2
_REASONING_ALLOWANCE_FACTOR = 3


def derive_output_budget_tokens() -> int:
    """從結構化輸出上限推導 output budget。可重算、有界、可測試。"""
    per_item = (_CHARS_PER_STATEMENT + _CHARS_PER_VALIDATION_NEEDED
                + _CHARS_PER_ID_ARRAY + _CHARS_JSON_OVERHEAD_PER_ITEM)
    chars = (_LIST_COUNT * PROMPT_ITEM_LIMIT_PER_LIST * per_item
             + _FIXED_SCHEMA_OVERHEAD_CHARS)
    visible = chars / _CHARS_PER_TOKEN
    total = visible * _REASONING_ALLOWANCE_FACTOR
    # 取整到 1024 的倍數，讓數字穩定可重算
    return int(-(-total // 1024) * 1024)


#: explain_run 每次生成的 output budget。刻意是**有界**的推導值，不是 unlimited。
OUTPUT_BUDGET_TOKENS = derive_output_budget_tokens()

#: prompt 契約的 item 上限不得超過 schema 的 fail-closed backstop。
assert PROMPT_ITEM_LIMIT_PER_LIST <= _MAX_ITEMS_PER_LIST

EVIDENCE_OPEN = "<<<EVIDENCE {ref_id} role={role} section={section} lines={lines}>>>"
EVIDENCE_CLOSE = "<<<END EVIDENCE>>>"


class LlmUnavailableError(RuntimeError):
    """Provider 無回覆／timeout——不產生假 insight。"""


@dataclass(frozen=True)
class Attempt:
    """一次生成的可記錄事實。initial 與 repair 分開記錄（Part D.7）。"""

    kind: str  # "initial" | "repair"
    prompt_sha256: str
    raw_response: str
    raw_response_sha256: str
    completion_metadata: dict[str, Any] | None
    validation_error: str | None = None
    validation_error_code: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "prompt_sha256": self.prompt_sha256,
                "raw_response_sha256": self.raw_response_sha256,
                "raw_response_chars": len(self.raw_response),
                "completion_metadata": self.completion_metadata,
                "validation_error": self.validation_error,
                "validation_error_code": self.validation_error_code}


@dataclass(frozen=True)
class PromptRun:
    prompt_text: str
    prompt_sha256: str
    raw_response: str
    raw_response_sha256: str
    parsed: dict[str, list[dict[str, Any]]]
    repaired: bool = False
    attempts: tuple[Attempt, ...] = ()


def prompt_template_sha256() -> str:
    return sha256_text(PROMPT_PATH.read_text(encoding="utf-8"))


def render_prompt(context: ContextSnapshot, *, repair_reason: str | None = None) -> str:
    """Repair 也走這個函式——catalog 與 schema 因此不會在 repair 時消失。"""
    template = PROMPT_PATH.read_text(encoding="utf-8")

    identity = "\n".join([
        f"- run_id: {context.producer.get('run_id')}",
        f"- step_id: {context.producer.get('step_id')}",
        f"- attempt: {context.producer.get('attempt')}",
        f"- tool: {context.producer.get('event_tool_name')} "
        f"(adapter {context.producer.get('adapter_id')})",
        f"- artifact_manifest_id: {context.artifact_manifest['manifest_id']}",
        # 缺少 Terminal Outcome 的 snapshot（例如手動組出的舊形狀）一律視為未驗證。
        f"- execution_outcome: "
        f"{context.execution.get('terminal_outcome') or 'EXECUTION_UNVERIFIED'}",
        f"- dqe_overall_status: {context.dqe['overall_status']}",
    ])

    facts = "\n".join(
        f"- {f.id}: {f.statement}"
        + (f" [value={f.value}{' ' + f.unit if f.unit else ''}]" if f.value is not None else "")
        for f in context.facts
    )

    evidence_blocks: list[str] = []
    for ref in context.evidence_refs:
        lines = (
            f"{ref.line_start}-{ref.line_end}"
            if ref.line_start is not None
            else "none (artifact is empty)"
        )
        evidence_blocks.append(
            EVIDENCE_OPEN.format(
                ref_id=ref.evidence_ref_id, role=ref.adapter_role,
                section=ref.section, lines=lines,
            )
            + "\n" + (ref.excerpt_text or "(this artifact is empty)")
            + "\n" + EVIDENCE_CLOSE
        )
    evidence = "\n\n".join(evidence_blocks) or "(no readable evidence excerpts)"

    unknowns = "\n".join(f"- {u}" for u in context.unknowns) or "- (none)"

    rendered = (
        template.replace("{{QUESTION}}", context.question)
        .replace("{{IDENTITY}}", identity)
        .replace("{{FACTS}}", facts)
        .replace("{{EVIDENCE}}", evidence)
        .replace("{{UNKNOWNS}}", unknowns)
    )
    if repair_reason:
        # 只在最前面加一段說明。schema、facts、evidence catalog、id 範圍與
        # output restrictions 全部保留在下面，一個字都沒少。
        rendered = (
            "前一次輸出無法使用："
            f"{repair_reason}\n"
            "這次請只輸出符合下列 schema 的完整 JSON。可用的 id、證據範圍與"
            "所有規則與上一次完全相同，未列出的 id 一律不得使用。\n\n"
            + rendered
        )
    return rendered


def _ask(llm_service: Any, prompt_text: str) -> tuple[str | None, dict[str, Any] | None]:
    """呼叫 provider。優先走 additive 的 metadata channel，沒有就退回 `ask()`。

    Provider 失敗**不會**被吞掉——回 `None`，由呼叫端轉成誠實的失敗，
    而且失敗不觸發 repair（Part D 禁止對 provider failure repair）。
    """
    detailed = getattr(llm_service, "ask_detailed", None)
    if callable(detailed):
        result = detailed(prompt_text, timeout=LLM_TIMEOUT_SECONDS,
                          max_output_tokens=OUTPUT_BUDGET_TOKENS)
        metadata = result.metadata.to_json_dict() if result.metadata else None
        return result.text, metadata
    return llm_service.ask(prompt_text, timeout=LLM_TIMEOUT_SECONDS), None


def run_prompt(llm_service: Any, context: ContextSnapshot) -> PromptRun:
    """一次 initial ＋ 最多一次 repair。失敗一律拋出，由 action 轉成誠實狀態。

    Repair 只在 `LlmOutputError.repairable` 為真（JSON 不完整／格式或 schema
    沒寫對）時發生。safety、evidence-scope、unsupported-fact 與 provider
    failure 一律**不** repair。
    """
    known_facts = {f.id for f in context.facts}
    known_refs = {r.evidence_ref_id for r in context.evidence_refs}
    attempts: list[Attempt] = []

    prompt_text = render_prompt(context)
    raw, metadata = _ask(llm_service, prompt_text)
    if not isinstance(raw, str) or not raw.strip():
        raise LlmUnavailableError("LLM provider returned no usable response")

    try:
        parsed = validate_llm_output(raw, known_fact_ids=known_facts,
                                     known_evidence_ref_ids=known_refs)
    except LlmOutputError as first_error:
        attempts.append(Attempt("initial", sha256_text(prompt_text), raw,
                                sha256_text(raw), metadata, str(first_error),
                                first_error.code))
        if not first_error.repairable:
            raise
        # Repair 走**同一個** render_prompt()，因此 schema、context、
        # evidence catalog 與 output restrictions 全部原樣保留；只在最前面
        # 加上「上一次為什麼不能用」。
        repair_text = render_prompt(context, repair_reason=str(first_error))
        raw2, metadata2 = _ask(llm_service, repair_text)
        if not isinstance(raw2, str) or not raw2.strip():
            raise LlmUnavailableError("LLM provider returned no usable repair response")
        try:
            parsed = validate_llm_output(raw2, known_fact_ids=known_facts,
                                         known_evidence_ref_ids=known_refs)
        except LlmOutputError as second_error:
            attempts.append(Attempt("repair", sha256_text(repair_text), raw2,
                                    sha256_text(raw2), metadata2,
                                    str(second_error), second_error.code))
            raise
        attempts.append(Attempt("repair", sha256_text(repair_text), raw2,
                                sha256_text(raw2), metadata2))
        return PromptRun(repair_text, sha256_text(repair_text), raw2,
                         sha256_text(raw2), parsed, repaired=True,
                         attempts=tuple(attempts))

    attempts.append(Attempt("initial", sha256_text(prompt_text), raw,
                            sha256_text(raw), metadata))
    return PromptRun(
        prompt_text=prompt_text,
        prompt_sha256=sha256_text(prompt_text),
        raw_response=raw,
        raw_response_sha256=sha256_text(raw),
        parsed=parsed,
        attempts=tuple(attempts),
    )


__all__ = [
    "Attempt",
    "LLM_TIMEOUT_SECONDS",
    "OUTPUT_BUDGET_TOKENS",
    "derive_output_budget_tokens",
    "LlmUnavailableError",
    "LlmOutputError",
    "PROMPT_ID",
    "PROMPT_PATH",
    "PROMPT_VERSION",
    "PromptRun",
    "prompt_template_sha256",
    "render_prompt",
    "run_prompt",
]
