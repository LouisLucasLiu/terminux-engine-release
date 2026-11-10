# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""LLM intent fallback for pre-exec hook.

fixed parser 失敗或結果為空時，以 injectable provider 嘗試從 runfile text 中
抽取 declared_inputs / declared_outputs，回傳 FallbackIntent。

失敗一律回傳 None，不往外拋 exception。
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Provider Protocol ──────────────────────────────────────────────────────────

@runtime_checkable
class IntentFallbackProvider(Protocol):
    """LLM text completion provider 的抽象介面。"""
    def complete(self, prompt: str) -> str: ...


# ── Context & result types ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class IntentFallbackContext:
    """傳給 infer_intent_with_provider 的輸入 context。"""
    command: str
    cwd: str
    runfile_path: str
    tool_name: str
    runfile_text: str


@dataclass(frozen=True)
class ArtifactItem:
    """LLM fallback 抽出的 artifact（role 與正規化後的 path）。"""
    role: str
    path: str


@dataclass
class FallbackIntent:
    """
    infer_intent_with_provider 的回傳值。
    declared_inputs / declared_outputs 的 items 是 ArtifactItem（以 attribute 存取）。
    交給 checker 前要先轉成 preexec_api._CheckerIntent。
    """
    job_id: str
    tool_name: str
    tool_category: str | None
    command: str
    cwd: str
    runfile_path: str
    declared_inputs: list[ArtifactItem]
    declared_outputs: list[ArtifactItem]


# ── Role validation ────────────────────────────────────────────────────────────

class ArtifactRole(str, Enum):
    """LLM 可以使用的合法 role 值。"""
    # declared_inputs
    RTL            = "rtl"
    SDC            = "sdc"
    UPF            = "upf"
    TESTBENCH      = "testbench"
    CDC_CFG        = "cdc_cfg"
    FORMAL_PROP    = "formal_prop"
    LINT_SUMMARY   = "lint_summary"
    SIM_SUMMARY    = "sim_summary"
    COV_SUMMARY    = "cov_summary"
    CDC_SUMMARY    = "cdc_summary"
    FORMAL_SUMMARY = "formal_summary"
    # declared_outputs
    NETLIST        = "netlist"
    LOG            = "log"
    REPORT         = "report"
    SUMMARY_JSON   = "summary_json"


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_intent_fallback_prompt(ctx: IntentFallbackContext) -> str:
    """
    產生送給 LLM 的 prompt，依序包含四個部分：
    1. 任務說明
    2. context 資訊
    3. 輸出 schema 與範例
    4. 格式限制
    """
    return (
        # Part 1：任務說明
        "You are analyzing an IC design EDA runfile.\n"
        "Extract the declared input and output artifacts from the runfile content below.\n"
        "\n"
        # Part 2：context 資訊
        f"Command: {ctx.command}\n"
        f"CWD: {ctx.cwd}\n"
        f"Tool: {ctx.tool_name}\n"
        f"Runfile path: {ctx.runfile_path}\n"
        "Runfile content:\n"
        f"{ctx.runfile_text}\n"
        "\n"
        # Part 3：輸出 schema 說明與範例
        "Output a single JSON object with exactly these two keys:\n"
        "\n"
        "{\n"
        '  "declared_inputs": [\n'
        '    {"role": "rtl", "path": "rtl/top.v"},\n'
        '    {"role": "sdc", "path": "constraints/top.sdc"}\n'
        "  ],\n"
        '  "declared_outputs": [\n'
        '    {"role": "netlist", "path": "out/top_syn.v"},\n'
        '    {"role": "log", "path": "out/syn.log"}\n'
        "  ]\n"
        "}\n"
        "\n"
        "Valid role values for declared_inputs:\n"
        "  rtl, sdc, upf, testbench, cdc_cfg, formal_prop,\n"
        "  lint_summary, sim_summary, cov_summary, cdc_summary, formal_summary\n"
        "\n"
        "Valid role values for declared_outputs:\n"
        "  netlist, log, report, summary_json\n"
        "\n"
        "If you cannot determine a value, use an empty list. Do not invent new roles.\n"
        "\n"
        # Part 4：格式限制（必須放在最後）
        "Output ONLY the JSON object.\n"
        "Do not include markdown formatting, code fences, explanation, or any other text.\n"
        "Do not output multiple JSON objects."
    )


# ── Inference ──────────────────────────────────────────────────────────────────

def infer_intent_with_provider(
    ctx: IntentFallbackContext,
    provider: IntentFallbackProvider,
) -> FallbackIntent | None:
    """
    以 LLM provider 從 runfile 抽出 artifact intent。

    成功時回傳 FallbackIntent。
    失敗時回傳 None（包含 provider 例外、JSON 解析失敗與 schema 不符）。
    例外不往外拋。
    """
    prompt = build_intent_fallback_prompt(ctx)

    # Step 1：呼叫 provider
    try:
        raw = provider.complete(prompt)
    except Exception:
        logger.warning(
            "IntentFallbackProvider.complete() raised an exception; returning None."
        )
        return None

    # Step 2：整理文字（strip 並移除 code fence）
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # 第一行是 ``` 或 ```json、最後一行是 ``` 時移除 fence
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    # Step 3：解析 JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM fallback: JSON parse failed. snippet=%r", text[:200])
        return None

    if not isinstance(data, dict):
        logger.warning("LLM fallback: top-level JSON is not a dict.")
        return None

    # Step 4：驗證 schema（declared_inputs / declared_outputs 不是 list 就回 None）
    raw_inputs = data.get("declared_inputs")
    raw_outputs = data.get("declared_outputs")

    if not isinstance(raw_inputs, list):
        logger.warning("LLM fallback: declared_inputs missing or not a list.")
        return None
    if not isinstance(raw_outputs, list):
        logger.warning("LLM fallback: declared_outputs missing or not a list.")
        return None

    # Step 5：逐項驗證 role 並正規化 path
    def _process_items(items: list) -> list[ArtifactItem]:
        result: list[ArtifactItem] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role_str = item.get("role")
            path_str = item.get("path")
            # role 與 path 不是字串就略過
            if not isinstance(role_str, str) or not isinstance(path_str, str):
                continue
            # 不合法的 role → 略過（不讓整個 fallback 失敗）
            try:
                ArtifactRole(role_str)
            except ValueError:
                logger.debug(
                    "LLM fallback: skipping item with invalid role %r", role_str
                )
                continue
            # 正規化 path：絕對路徑原樣保留，相對路徑以 cwd 為基準組合
            if path_str.startswith("/"):
                final_path = path_str
            else:
                final_path = str(Path(ctx.cwd) / path_str)
            result.append(ArtifactItem(role=role_str, path=final_path))
        return result

    declared_inputs = _process_items(raw_inputs)
    declared_outputs = _process_items(raw_outputs)

    # Step 6：組出 FallbackIntent
    stem = Path(ctx.runfile_path).stem if ctx.runfile_path else ""
    runfile_id = stem if stem else "unknown_runfile"

    return FallbackIntent(
        job_id=f"llm-intent:{ctx.tool_name}:{runfile_id}",
        tool_name=ctx.tool_name,
        tool_category=None,
        command=ctx.command,
        cwd=ctx.cwd,
        runfile_path=ctx.runfile_path,
        declared_inputs=declared_inputs,
        declared_outputs=declared_outputs,
    )
