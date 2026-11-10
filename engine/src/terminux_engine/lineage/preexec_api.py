# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from terminux_engine.lineage.checker import check_synthesis_preflight
from terminux_engine.lineage.intent_fallback import (
    FallbackIntent,
    IntentFallbackContext,
    IntentFallbackProvider,
    infer_intent_with_provider,
)
from terminux_engine.lineage.lineage_explainer import (
    LineageExplanation,
    LineageExplanationContext,
    LineageExplainerProvider,
    explain_lineage_with_provider,
)
from terminux_engine.lineage.runfile_parser import parse_runfile
from terminux_engine.lineage.tool_identifier import identify_tool
from terminux_engine.resource_paths import bundled_knowledge_path

logger = logging.getLogger(__name__)


def _get_default_tool_signatures_path() -> Path:
    return bundled_knowledge_path("tool_signatures.yaml")


# ─── Decision model ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreExecDecision:
    action: str            # "allow" | "warn" | "block"
    status: str            # "CONSISTENT" | "MIXED_LINEAGE" | "INSUFFICIENT_EVIDENCE" | "UNSUPPORTED_TOOL"
    tool_name: str | None
    runfile_path: str | None
    message: str
    explanation: LineageExplanation | None = None
    # checker.findings 序列化版（每筆 dict 含 role / path / severity / status / message /
    # expected_rtl_checksum / observed_rtl_anchors）。chat query 用來告訴 LLM 具體哪個
    # input 出問題，不必 LLM 自己推測。tuple 因 frozen dataclass 不可帶 mutable default。
    findings: tuple[dict, ...] = ()


_ACTION_MAP: dict[str, str] = {
    "CONSISTENT": "allow",
    "MIXED_LINEAGE": "warn",
    "INSUFFICIENT_EVIDENCE": "warn",
    "UNSUPPORTED_TOOL": "allow",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_insufficient_evidence(
    message: str,
    *,
    tool_name: str | None = None,
    runfile_path: str | None = None,
) -> PreExecDecision:
    """產生 INSUFFICIENT_EVIDENCE 的 PreExecDecision 的共用 helper。"""
    return PreExecDecision(
        action="warn",
        status="INSUFFICIENT_EVIDENCE",
        tool_name=tool_name,
        runfile_path=runfile_path,
        message=message,
    )


@dataclass
class _CheckerIntent:
    """
    FallbackIntent → 與 check_synthesis_preflight 相容的 duck-typed adapter。
    只保留 checker 實際讀取的欄位。
    """
    run_file: str
    stage: str
    declared_inputs: list[dict[str, str]]
    declared_outputs: list[dict[str, str]]


def _to_checker_intent(fallback: FallbackIntent, runfile_abs: Path) -> _CheckerIntent:
    """把 FallbackIntent 轉成與 checker 相容的 _CheckerIntent。"""
    return _CheckerIntent(
        run_file=str(runfile_abs),
        stage="unknown_stage",
        declared_inputs=[
            {"role": a.role, "path": a.path} for a in fallback.declared_inputs
        ],
        declared_outputs=[
            {"role": a.role, "path": a.path} for a in fallback.declared_outputs
        ],
    )


# ─── Public API ──────────────────────────────────────────────────────────────

def handle_pre_exec(
    command: str,
    cwd: str | Path,
    ledger_path: str | Path,
    *,
    tool_signatures_path: str | Path | None = None,
    lineage_rules_path: str | Path | None = None,
    # keyword-only 且有預設值，不影響既有呼叫端
    intent_fallback_provider: IntentFallbackProvider | None = None,
    enable_intent_fallback: bool = False,
    # keyword-only 且有預設值，不影響既有呼叫端
    lineage_explainer_provider: LineageExplainerProvider | None = None,
    enable_lineage_explainer: bool = False,
) -> PreExecDecision:
    """
    統一 pre-exec decision 入口。
    輸入 command + cwd + ledger_path，輸出 PreExecDecision。
    不做實際執行，不做 post-exec commit。

    keyword-only 參數：
    - enable_intent_fallback：True 時才嘗試 LLM fallback
    - intent_fallback_provider：None 時不嘗試 fallback（fail-safe）
    """
    cwd = Path(cwd)
    ledger_path = Path(ledger_path)
    sigs_path = (
        Path(tool_signatures_path)
        if tool_signatures_path is not None
        else _get_default_tool_signatures_path()
    )

    # ── Step 1：辨識 tool ──────────────────────────────────────────────────
    tool_match = identify_tool(command, sigs_path)
    if tool_match is None:
        return PreExecDecision(
            action="allow",
            status="UNSUPPORTED_TOOL",
            tool_name=None,
            runfile_path=None,
            message=(
                "Tool not recognized by lineage signatures; "
                "preflight check not applicable."
            ),
        )

    tool_name = tool_match.tool_name

    # ── Step 2：從 command 定位 runfile（只支援 -f <runfile>）──────────────
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    runfile_str: str | None = None
    for i, tok in enumerate(tokens):
        if tok == "-f" and i + 1 < len(tokens):
            runfile_str = tokens[i + 1]
            break

    if runfile_str is None:
        # 工具被識別但沒有 -f flag（如 sta_run -corner ...）：
        # lineage preflight 不適用，放行但保留 tool_name 供 ECC 使用
        return PreExecDecision(
            action="allow",
            status="INSUFFICIENT_EVIDENCE",
            tool_name=tool_name,
            runfile_path=None,
            message="No runfile specified; lineage preflight skipped.",
        )

    # ── Step 3：解析 runfile 為絕對路徑 ───────────────────────────────────
    runfile_abs = Path(runfile_str)
    if not runfile_abs.is_absolute():
        runfile_abs = (cwd / runfile_str).resolve()

    if not runfile_abs.exists():
        return PreExecDecision(
            action="warn",
            status="INSUFFICIENT_EVIDENCE",
            tool_name=tool_name,
            runfile_path=str(runfile_abs),
            message=(
                f"Runfile not found: {runfile_abs} "
                f"(command: {command}, cwd: {cwd}). "
                f"Check: is the path correct? Is it relative to cwd? "
                f"Does the file exist?"
            ),
        )

    # ── Step 4：parse runfile ────────────────────────────────────────────
    intent = None
    parser_failed = False
    try:
        intent = parse_runfile(runfile_abs, command=command)
    except ValueError:
        parser_failed = True

    # ── Step 4b：LLM fallback（下列條件全部成立時才執行）──────────────────
    # 条件：enable_intent_fallback == True
    #       AND intent_fallback_provider is not None
    #       AND（parser 拋出 ValueError
    #            OR parser 回傳的 declared_inputs 與 declared_outputs 都是空的）
    _should_fallback = (
        enable_intent_fallback
        and intent_fallback_provider is not None
        and (
            parser_failed
            or (
                intent is not None
                and len(intent.declared_inputs) == 0
                and len(intent.declared_outputs) == 0
            )
        )
    )

    if _should_fallback:
        # 只在觸發 fallback 時讀取 runfile 內容
        try:
            runfile_text = runfile_abs.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return _make_insufficient_evidence(
                "runfile not readable for fallback",
                tool_name=tool_name,
                runfile_path=str(runfile_abs),
            )

        ctx = IntentFallbackContext(
            command=command,
            cwd=str(cwd),
            runfile_path=str(runfile_abs),
            tool_name=tool_name,
            runfile_text=runfile_text,
        )
        fallback_result = infer_intent_with_provider(ctx, intent_fallback_provider)

        if fallback_result is not None:
            # FallbackIntent 先轉成與 checker 相容的 adapter，再交給 checker
            intent = _to_checker_intent(fallback_result, runfile_abs)
        else:
            # LLM fallback 失敗 → INSUFFICIENT_EVIDENCE（不中斷）
            return _make_insufficient_evidence(
                "LLM fallback did not produce a valid intent.",
                tool_name=tool_name,
                runfile_path=str(runfile_abs),
            )

    # ── parse 失敗且沒有 fallback ─────────────────────────────────────────
    if intent is None:
        return _make_insufficient_evidence(
            "Runfile parse failed.",
            tool_name=tool_name,
            runfile_path=str(runfile_abs),
        )

    # ── Step 5：checker 判定 ──────────────────────────────────────────────
    # ledger_path 原樣轉交，rules_path 傳 None 讓 checker 使用其預設機制
    check_result = check_synthesis_preflight(
        intent,
        cwd,           # project_root：cwd 為語義上最合適的基準
        ledger_path,   # 明確傳入，不使用 checker 的 default_ledger_path
        rules_path=lineage_rules_path,
    )

    # ── mapping → decision ───────────────────────────────────────────
    status = check_result.status
    action = _ACTION_MAP.get(status, "warn")
    message = check_result.findings[0].message if check_result.findings else ""

    # 序列化 findings：保留 role / path / anchors 等具體資訊，讓 chat query
    # 階段 LLM 能告訴使用者「具體是哪個 input 哪個 path 出問題」，
    # 不必只靠泛泛 message。tuple 因 PreExecDecision frozen。
    # 加性序列化 — category / suggestion / context 三個新欄位
    # 跟著走到 PreExecDecision.findings；consumer（jsonrpc_server / Agent）若
    # 不認識新欄位（舊版 caller）只會多接到 None / 空 dict，不影響行為。
    findings_data: tuple[dict, ...] = tuple(
        {
            "role": f.role,
            "path": f.path,
            "severity": f.severity,
            "status": f.status,
            "message": f.message,
            "expected_rtl_checksum": f.expected_rtl_checksum,
            "observed_rtl_anchors": list(f.observed_rtl_anchors),
            "category": getattr(f, "category", None),
            "suggestion": getattr(f, "suggestion", None),
            "context": dict(getattr(f, "context", None) or {}),
        }
        for f in check_result.findings
    )

    # ── Step 7：explainer（可選，best-effort）──────────────────────────
    # 觸發條件（AND 全部滿足）：
    #   enable_lineage_explainer == True
    #   AND lineage_explainer_provider is not None
    #   AND status in (MIXED_LINEAGE, INSUFFICIENT_EVIDENCE)
    explanation: LineageExplanation | None = None
    _should_explain = (
        enable_lineage_explainer
        and lineage_explainer_provider is not None
        and status in ("MIXED_LINEAGE", "INSUFFICIENT_EVIDENCE")
    )

    if _should_explain:
        # 從 findings 中提取 evidence（訊息清單）與 suspicious_roles（role 字串清單）
        evidence = [f.message for f in check_result.findings]
        suspicious_roles: list[str] = [
            f.role for f in check_result.findings if f.role is not None
        ]

        ctx_explain = LineageExplanationContext(
            command=command,
            cwd=str(cwd),
            tool_name=tool_name,
            status=status,
            base_message=message,
            evidence=evidence,
            suspicious_roles=suspicious_roles,
            runfile_path=str(runfile_abs),
        )
        # explain 失敗（None）也不崩潰，explanation 保持 None
        explanation = explain_lineage_with_provider(ctx_explain, lineage_explainer_provider)

    return PreExecDecision(
        action=action,
        status=status,
        tool_name=tool_name,
        runfile_path=str(runfile_abs),
        message=message,
        explanation=explanation,
        findings=findings_data,
    )
