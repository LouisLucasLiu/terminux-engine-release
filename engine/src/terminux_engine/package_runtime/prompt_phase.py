# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Prompt 定義 after phase 的執行流程。"""

from __future__ import annotations

import logging

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.manifest import PromptPhaseSpec
from terminux_engine.package_runtime.prompt_result import convert_prompt_response
from terminux_engine.package_runtime.prompt_template import render_prompt_template
from terminux_engine.package_runtime.result import PhaseResult

_log = logging.getLogger(__name__)

_SYSTEM_PROMPTS = {
    "judgment": """你是一個 IC 設計分析助理。閱讀任務內容後給出判斷。
回覆第一行必須是 [STATUS: ok]、[STATUS: warn] 或 [STATUS: fail]。
第二行起說明具體理由。""",
    "digest": """你是一個 IC 設計分析助理。整理任務內容的關鍵發現。
回覆第一行必須是 [DIGEST]。
後續以簡潔條列列出重點。""",
    "comparison": """你是一個 IC 設計分析助理。比較本次與歷史資料。
回覆第一行必須是 [COMPARISON: improved]、[COMPARISON: degraded] 或
[COMPARISON: unchanged]。第二行起說明比較依據。""",
}


def run_prompt_phase(spec: PromptPhaseSpec, ctx: PackageContext) -> PhaseResult:
    """讀 prompt、render、呼叫 LLM 並轉換為標準 PhaseResult。"""
    try:
        template = spec.prompt_path.read_text(encoding="utf-8")
        rendered = render_prompt_template(template, ctx)
        prompt = _build_prompt(spec.pattern, rendered)
        if ctx.services is None:
            _log.warning("Prompt phase has no RuntimeServices")
            return PhaseResult(status="noop", message="Prompt runtime services unavailable.")
        response = ctx.services.llm.ask(prompt)
        if response is None:
            _log.warning("Prompt phase LLM returned no response")
            return PhaseResult(status="noop", message="Prompt LLM unavailable.")
        result = convert_prompt_response(spec.pattern, response, ctx)
        if result is None:
            _log.warning("Prompt phase response parse failed: %s", response[:300])
            return PhaseResult(status="noop", message="Prompt response was not parseable.")
        return result
    except Exception as exc:
        _log.warning("Prompt phase failed: %s", exc)
        return PhaseResult(status="noop", message="Prompt phase failed.")


def _build_prompt(pattern: str, rendered: str) -> str:
    system_prompt = _SYSTEM_PROMPTS[pattern]
    return f"{system_prompt}\n\n# 任務內容\n{rendered}"
