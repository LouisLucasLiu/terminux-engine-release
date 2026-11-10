# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""FE Lineage 整合進 ECC Object。

trace_to_roots 做 deterministic 的 ledger 追溯，
judge_lineage 呼叫 AI 判定 CONSISTENT / MIXED_LINEAGE，
trace_and_judge 串接完整流程。
"""

from __future__ import annotations

import json
import logging
import re

from terminux_engine.ecc.ai_companion import AICompanion
from terminux_engine.ecc.ecc_object import LineageSummary, RootInput
from terminux_engine.lineage.ledger import find_latest_commit_by_output_path
from terminux_engine.lineage.runfile_parser import parse_runfile
from terminux_engine.lineage.snapshot import build_snapshot

logger = logging.getLogger(__name__)


# ─── trace_to_roots ──────────────────────────────────────────────────────────

def trace_to_roots(
    declared_inputs: list[dict],
    ledger_path: str,
) -> tuple[list[RootInput], bool]:
    """
    從 declared_inputs 往上追溯到 root inputs。

    回傳 (root_inputs, trace_complete)。
    """
    roots: list[RootInput] = []
    trace_complete = True
    visited: set[str] = set()

    def _trace(path: str, role: str, traced_from: str | None) -> None:
        nonlocal trace_complete

        abs_path = str(path)
        if abs_path in visited:
            return
        visited.add(abs_path)

        # 取得 checksum
        try:
            snap = build_snapshot(abs_path, role)
        except FileNotFoundError:
            trace_complete = False
            return

        # 查 ledger：這個檔案是否為某次 tool run 的 output？
        commit, _ = find_latest_commit_by_output_path(ledger_path, abs_path)

        if commit is None:
            # 沒有 producer → 這是 root input
            roots.append(RootInput(
                role=role,
                path=abs_path,
                checksum_sha256=snap.checksum_sha256,
                traced_from=traced_from,
            ))
        else:
            # 有 producer → 遞迴追溯 producer 的 inputs
            for inp in commit.input_snapshots:
                _trace(inp.path, inp.role, abs_path)

    for d in declared_inputs:
        _trace(d["path"], d.get("role", "unknown"), None)

    return roots, trace_complete


# ─── judge_lineage ───────────────────────────────────────────────────────────

def _build_lineage_prompt(roots: list[RootInput]) -> str:
    """組裝 lineage 判定 prompt。"""
    if roots:
        root_lines = []
        for r in roots:
            line = (
                f"- role: {r.role}, path: {r.path}, "
                f"checksum: {r.checksum_sha256[:16]}..."
            )
            if r.traced_from:
                line += f", traced_from: {r.traced_from}"
            root_lines.append(line)
        roots_text = "\n".join(root_lines)
    else:
        roots_text = "(no root inputs found)"

    return (
        "You are an AI assistant for IC design data lineage governance.\n"
        "Your role is to determine if all root inputs of a synthesis run "
        "come from a consistent source.\n"
        "\n"
        "## Root Inputs Traced from Synthesis\n"
        "The following root inputs were traced by walking the production ledger\n"
        "from synthesis declared inputs back to their origins "
        "(files with no known producer):\n"
        "\n"
        f"{roots_text}\n"
        "\n"
        "## Instructions\n"
        "Analyze the root inputs above:\n"
        "1. Check if all root inputs appear to come from the same consistent source/branch\n"
        "2. Look for path patterns, branch names, or other indicators of source consistency\n"
        "3. If any root input appears to come from a different source than the others, flag it\n"
        "\n"
        "Respond in JSON format only:\n"
        "{\n"
        '  "judgment": "CONSISTENT" or "MIXED_LINEAGE" or "INSUFFICIENT_EVIDENCE",\n'
        '  "explanation": "..."\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- CONSISTENT: All root inputs appear to come from the same source\n"
        "- MIXED_LINEAGE: At least one root input appears to come from a different source\n"
        "- INSUFFICIENT_EVIDENCE: Cannot determine consistency\n"
    )


_BRANCH_RE = re.compile(r"/branch_([A-Za-z0-9_]+)/")


def _lineage_deterministic_fallback(roots: list[RootInput]) -> LineageSummary:
    """LLM 不可用時的 deterministic fallback。"""
    if not roots:
        return LineageSummary(
            synthesis_roots=roots,
            trace_complete=True,
            ai_judgment="INSUFFICIENT_EVIDENCE",
            ai_explanation="No root inputs found.",
        )

    branches: set[str] = set()
    for r in roots:
        m = _BRANCH_RE.search(r.path)
        if m:
            branches.add(m.group(1))

    if not branches:
        return LineageSummary(
            synthesis_roots=roots,
            trace_complete=True,
            ai_judgment="INSUFFICIENT_EVIDENCE",
            ai_explanation="Cannot determine branch from paths.",
        )

    if len(branches) == 1:
        return LineageSummary(
            synthesis_roots=roots,
            trace_complete=True,
            ai_judgment="CONSISTENT",
            ai_explanation=f"All roots from branch_{next(iter(branches))}.",
        )

    return LineageSummary(
        synthesis_roots=roots,
        trace_complete=True,
        ai_judgment="MIXED_LINEAGE",
        ai_explanation=f"Mixed branches detected: {', '.join(sorted(branches))}.",
    )


def _parse_lineage_response(raw: str) -> dict | None:
    """嘗試從 LLM 回傳中解析 JSON。"""
    m = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    text = m.group(1) if m else raw
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "judgment" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


def judge_lineage(
    roots: list[RootInput],
    ai_companion: AICompanion,
) -> LineageSummary:
    """呼叫 AI 判定 root inputs 是否一致。"""
    prompt = _build_lineage_prompt(roots)

    try:
        raw = ai_companion._llm_complete(prompt)
    except Exception:
        logger.warning("LLM call failed for lineage judgment, using fallback")
        return _lineage_deterministic_fallback(roots)

    parsed = _parse_lineage_response(raw)
    if parsed is not None:
        judgment = str(parsed.get("judgment", "INSUFFICIENT_EVIDENCE"))
        explanation = str(parsed.get("explanation", ""))
        return LineageSummary(
            synthesis_roots=roots,
            trace_complete=True,
            ai_judgment=judgment,
            ai_explanation=explanation,
        )

    logger.warning("LLM lineage response parse failed, using fallback")
    return _lineage_deterministic_fallback(roots)


# ─── trace_and_judge ─────────────────────────────────────────────────────────

def trace_and_judge(
    command: str,
    runfile_path: str,
    ledger_path: str,
    ai_companion: AICompanion,
) -> LineageSummary:
    """完整流程：parse runfile → trace to roots → AI judge。"""
    intent = parse_runfile(runfile_path, command)
    roots, trace_complete = trace_to_roots(intent.declared_inputs, ledger_path)
    summary = judge_lineage(roots, ai_companion)
    # 用實際 trace_complete 覆蓋（judge_lineage 預設 True）
    summary.trace_complete = trace_complete
    return summary
