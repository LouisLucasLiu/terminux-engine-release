# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Comparison prompt 組裝與輸出驗證。

沿用 explain_run 的 `validate_llm_output()`，只多要求並驗證一個
`comparability_statement`——不重寫既有 validator，也不放寬任何既有保證。

預設**不呼叫**真實 provider；`run_prompt()` 只在 validation 或
真實 demonstration 時使用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.context import ContextSnapshot
from terminux_engine.packages.orfs_run_intelligence.schemas import (
    LlmOutputError,
    sha256_text,
    validate_llm_output,
)

PROMPT_ID = "orfs_run_compare"
PROMPT_VERSION = "v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / "compare_runs_v1.md"

LLM_TIMEOUT_SECONDS = 120.0
MAX_COMPARABILITY_STATEMENT_CHARS = 1200

EVIDENCE_OPEN = "<<<EVIDENCE {ref_id} subject={subject} source={source} section={section} lines={lines}>>>"
EVIDENCE_CLOSE = "<<<END EVIDENCE>>>"


class LlmUnavailableError(RuntimeError):
    """Provider 無回覆／timeout——不產生假 insight。"""


@dataclass(frozen=True)
class ComparisonPromptRun:
    prompt_text: str
    prompt_sha256: str
    raw_response: str
    raw_response_sha256: str
    parsed: dict[str, Any]


def prompt_template_sha256() -> str:
    return sha256_text(PROMPT_PATH.read_text(encoding="utf-8"))


def render_prompt(snapshot: ContextSnapshot) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    decision = (snapshot.domain_payload or {}).get("comparison_decision") or {}

    comparability = "\n".join([
        f"- status: {decision.get('status')}",
        f"- policy: {decision.get('policy_id')} {decision.get('policy_version')}",
        f"- matched_dimensions: {', '.join(decision.get('matched_dimensions') or []) or '(none)'}",
        "- controlled_changes: " + (
            ", ".join(f"{c['dimension']}" for c in decision.get("controlled_changes") or [])
            or "(none)"),
        "- mismatches: " + (
            ", ".join(f"{m['dimension']}" for m in decision.get("mismatches") or [])
            or "(none)"),
        "- unknowns:",
    ] + [f"    - {u}" for u in (decision.get("unknowns") or ["(none)"])])

    facts = "\n".join(
        f"- {f.fact_id}: {f.statement}"
        + (f" [delta={f.value}{' ' + f.unit if f.unit else ''}]" if f.value is not None else "")
        for f in snapshot.deterministic_facts
    ) or "- (none)"

    missing = "\n".join(
        f"- {m.requested_source_id} ({m.subject_role}): {m.reason} -> {m.impact}"
        for m in snapshot.missing_context
    ) or "- (none)"

    blocks: list[str] = []
    for source in snapshot.sources:
        for ref in source.evidence_refs:
            lines = (f"{ref.line_start}-{ref.line_end}"
                     if ref.line_start is not None else "none")
            blocks.append(
                EVIDENCE_OPEN.format(ref_id=ref.evidence_ref_id, subject=ref.subject_role,
                                     source=source.source_id, section=ref.locator, lines=lines)
                + "\n" + (ref.excerpt_text or "(this excerpt is empty)")
                + "\n" + EVIDENCE_CLOSE
            )
    evidence = "\n\n".join(blocks) or "(no readable evidence excerpts)"

    return (
        template.replace("{{QUESTION}}", snapshot.question or
                         "Compare these two runs and explain what changed.")
        .replace("{{COMPARABILITY}}", comparability)
        .replace("{{FACTS}}", facts)
        .replace("{{MISSING}}", missing)
        .replace("{{EVIDENCE}}", evidence)
    )


def validate_comparison_output(
    raw: str, *, known_fact_ids: set[str], known_evidence_ref_ids: set[str]
) -> dict[str, Any]:
    """驗證比較用回覆：既有四個 list ＋ 必要的 `comparability_statement`。"""
    text = raw.strip()
    try:
        payload = json.loads(_strip_fence(text))
    except json.JSONDecodeError as exc:
        raise LlmOutputError(f"output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LlmOutputError("output is not a JSON object")

    statement = payload.pop("comparability_statement", None)
    if not isinstance(statement, str) or not statement.strip():
        raise LlmOutputError("comparability_statement is required and must be a non-empty string")
    if len(statement) > MAX_COMPARABILITY_STATEMENT_CHARS:
        raise LlmOutputError(
            f"comparability_statement exceeds {MAX_COMPARABILITY_STATEMENT_CHARS} characters")

    parsed = validate_llm_output(
        json.dumps(payload, ensure_ascii=False),
        known_fact_ids=known_fact_ids,
        known_evidence_ref_ids=known_evidence_ref_ids,
    )
    return {"comparability_statement": statement.strip(), **parsed}


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return text
    body = stripped[3:-3]
    newline = body.find("\n")
    if newline == -1:
        return text
    language = body[:newline].strip()
    if language and not language.isalnum():
        return text
    return body[newline + 1:]


def run_prompt(llm_service: Any, snapshot: ContextSnapshot) -> ComparisonPromptRun:
    """單次呼叫並驗證。失敗一律拋出，由 action 轉成誠實的失敗狀態。"""
    prompt_text = render_prompt(snapshot)
    raw = llm_service.ask(prompt_text, timeout=LLM_TIMEOUT_SECONDS)
    if not isinstance(raw, str) or not raw.strip():
        raise LlmUnavailableError("LLM provider returned no usable response")
    parsed = validate_comparison_output(
        raw,
        known_fact_ids={f.fact_id for f in snapshot.deterministic_facts},
        known_evidence_ref_ids={r.evidence_ref_id for r in snapshot.evidence_refs()},
    )
    return ComparisonPromptRun(
        prompt_text=prompt_text, prompt_sha256=sha256_text(prompt_text),
        raw_response=raw, raw_response_sha256=sha256_text(raw), parsed=parsed,
    )


__all__ = [
    "LLM_TIMEOUT_SECONDS",
    "LlmUnavailableError",
    "MAX_COMPARABILITY_STATEMENT_CHARS",
    "PROMPT_ID",
    "PROMPT_PATH",
    "PROMPT_VERSION",
    "ComparisonPromptRun",
    "prompt_template_sha256",
    "render_prompt",
    "run_prompt",
    "validate_comparison_output",
]
