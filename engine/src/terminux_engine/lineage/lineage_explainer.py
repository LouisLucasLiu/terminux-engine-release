# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""LLM Lineage Explainer

為 deterministic checker 的結果產生易讀的 explanation。
provider 失敗、JSON 無效或 schema 不符時回傳 None（best-effort）。
不修改 action／status／message。
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Provider Protocol ──────────────────────────────────────────────────────────

@runtime_checkable
class LineageExplainerProvider(Protocol):
    """LLM text completion provider 的抽象介面。"""
    def complete(self, prompt: str) -> str:
        ...


# ── Context & Result types ────────────────────────────────────────────────────

@dataclass(frozen=True)
class LineageExplanationContext:
    """傳給 explain_lineage_with_provider 的輸入 context。"""
    command: str
    cwd: str
    tool_name: str | None
    status: str           # "MIXED_LINEAGE" | "INSUFFICIENT_EVIDENCE"
    base_message: str
    evidence: list[str]
    suspicious_roles: list[str]   # str，不是 ArtifactRole enum
    runfile_path: str | None


@dataclass(frozen=True)
class LineageExplanation:
    """explain_lineage_with_provider 的回傳值。"""
    summary: str
    likely_cause: str
    suggested_actions: list[str]   # 允許空 list


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_lineage_explainer_prompt(ctx: LineageExplanationContext) -> str:
    """
    五段式 prompt 建構（在 Section 3 之前插入 status-aware
    rules 防幻覺）。順序固定：

    - Section 1：Task
    - Section 2：Input Context
    - Section 2.5：Status-aware Rules（防 status 推測偏離 ground truth）
    - Section 3：Output Schema（範例改為 generic，不再硬寫 MIXED_LINEAGE 案例）
    - Section 4：Output Rules（必須最後）
    """
    # Section 1：Task
    section1 = (
        "你是 IC 設計 EDA 工作流的 lineage 分析助理。請以繁體中文回答。\n"
        "一個 deterministic checker 已經決定了 lineage 狀態。\n"
        "你的任務是用繁體中文解釋結果並建議行動。\n"
        "不要改變狀態或決策。"
    )

    # Section 2：Input Context
    tool_str = ctx.tool_name if ctx.tool_name is not None else "unknown"
    runfile_str = ctx.runfile_path if ctx.runfile_path is not None else "unknown"
    evidence_str = ", ".join(ctx.evidence) if ctx.evidence else "none"
    roles_str = ", ".join(ctx.suspicious_roles) if ctx.suspicious_roles else "none"
    section2 = (
        f"Command: {ctx.command}\n"
        f"CWD: {ctx.cwd}\n"
        f"Tool: {tool_str}\n"
        f"Runfile: {runfile_str}\n"
        f"Status: {ctx.status}\n"
        f"Checker message: {ctx.base_message}\n"
        f"Evidence: {evidence_str}\n"
        f"Suspicious roles: {roles_str}"
    )

    # Section 2.5：Status-aware rules（幻覺防護）
    # 解決問題：之前 Section 3 範例硬寫 MIXED_LINEAGE 案例，當 Status =
    # INSUFFICIENT_EVIDENCE 時 LLM 因 few-shot bias 仍會輸出血緣混合的診斷。
    section_status_rules = (
        "重要規則（依當前 Status 解釋，不要推測其他 status）：\n"
        "\n"
        "- 若 Status = INSUFFICIENT_EVIDENCE：\n"
        "  · summary 必須說明「證據不足以判定」，而非斷言血緣有問題\n"
        "  · likely_cause 聚焦在為什麼證據不足（檔案找不到、欄位缺失、parser 失敗等）\n"
        "  · 不可以推測或暗示是 MIXED_LINEAGE / 血緣混合\n"
        "  · suggested_actions 應引導使用者補齊證據（檢查路徑、補欄位、重跑上游）\n"
        "\n"
        "- 若 Status = MIXED_LINEAGE：\n"
        "  · summary 說明血緣分支不一致\n"
        "  · likely_cause 解釋是哪個 artifact 跟哪個 RTL anchor 不匹配\n"
        "  · suggested_actions 給出替換 artifact 或重跑上游的具體步驟\n"
        "\n"
        "- 若 Status = CONSISTENT：\n"
        "  · summary 說明檢查通過\n"
        "  · likely_cause 寫「無異常」\n"
        "  · suggested_actions 為空 list\n"
        "\n"
        "絕對禁止：\n"
        "- 在 Status = INSUFFICIENT_EVIDENCE 時，輸出含「混合分支」「mixed lineage」"
        "「不同 RTL 版本」「formal_summary 來自...不同 RTL anchor」等暗示 MIXED_LINEAGE 的詞彙\n"
        "- 推測 Checker 沒判定的結論\n"
        "- 你的解釋必須忠實反映 Checker 的 Status 與 message，不可超出"
    )

    # Section 3：Output Schema（範例改 generic，避免 few-shot bias）
    section3 = (
        '輸出一個 JSON object，包含以下三個 key（值請以繁體中文撰寫）：\n'
        '\n'
        '{\n'
        '  "summary": "（一句話描述當前 Status 的情境，必須對應 Checker message）",\n'
        '  "likely_cause": "（根據 Checker message 與 evidence 推斷的可能原因）",\n'
        '  "suggested_actions": [\n'
        '    "（可執行的修正步驟 1）",\n'
        '    "（可執行的修正步驟 2）"\n'
        '  ]\n'
        '}\n'
        '\n'
        'Rules:\n'
        '- "summary": 一句話描述情境\n'
        '- "likely_cause": 根據 evidence 推斷的最可能根因；不確定時直接說明證據不足\n'
        '- "suggested_actions": 可操作的建議清單；不確定時使用空 list\n'
        '- 不要捏造 evidence 中未提及的檔名或步驟'
    )

    # Section 4：Output Rules（必須單獨一段，放在最後）
    section4 = (
        "只輸出 JSON object。\n"
        "不要包含 markdown 格式、code fences、額外說明或其他文字。\n"
        "不要輸出多個 JSON object。"
    )

    return "\n\n".join(
        [section1, section2, section_status_rules, section3, section4]
    )


# ── Inference ─────────────────────────────────────────────────────────────────

def explain_lineage_with_provider(
    ctx: LineageExplanationContext,
    provider: LineageExplainerProvider,
) -> "LineageExplanation | None":
    """
    以 provider 產生 lineage explanation。

    成功時回傳 LineageExplanation。
    失敗時回傳 None（包含 provider 例外、JSON 解析失敗與 schema 不符）。
    例外不往外拋。
    """
    prompt = build_lineage_explainer_prompt(ctx)

    # Step 1：呼叫 provider
    try:
        raw = provider.complete(prompt)
    except Exception:
        logger.warning("LineageExplainerProvider.complete() raised; returning None.")
        return None

    # Step 2：整理文字（strip 並移除 code fence）
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # 第一行 ```(json) + 最後行 ``` → 移除最外層 fence
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    # Step 3：JSON parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LineageExplainer: JSON parse failed. snippet=%r", text[:200])
        return None

    if not isinstance(data, dict):
        logger.warning("LineageExplainer: top-level JSON is not a dict.")
        return None

    # Step 4：schema 驗證
    summary = data.get("summary")
    likely_cause = data.get("likely_cause")
    suggested_actions = data.get("suggested_actions")

    if not isinstance(summary, str) or not summary:
        logger.warning("LineageExplainer: 'summary' missing or not a non-empty str.")
        return None
    if not isinstance(likely_cause, str) or not likely_cause:
        logger.warning("LineageExplainer: 'likely_cause' missing or not a non-empty str.")
        return None
    if not isinstance(suggested_actions, list):
        logger.warning("LineageExplainer: 'suggested_actions' missing or not a list.")
        return None
    for item in suggested_actions:
        if not isinstance(item, str):
            logger.warning("LineageExplainer: 'suggested_actions' contains non-str item.")
            return None

    # Step 5：組裝回傳
    return LineageExplanation(
        summary=summary,
        likely_cause=likely_cause,
        suggested_actions=suggested_actions,
    )
