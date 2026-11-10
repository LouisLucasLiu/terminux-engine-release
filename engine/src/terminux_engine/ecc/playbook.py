# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Playbook 資料模型與載入/查詢函式。

ECC Object 專用的 playbook loader，
處理 stage spec + quality gates + parsing rules 結構。
純載入與查詢，不做 gate evaluation、report parsing 或 AI 邏輯。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel


# ─── 子結構 ───────────────────────────────────────────────────────────────────

class ToolSignatures(BaseModel):
    """工具特徵，用於階段識別。"""
    commands: list[str]
    """可識別的命令清單"""
    work_directory_pattern: str
    """工作目錄的 glob pattern"""


class RequiredReport(BaseModel):
    """必備報告定義。"""
    pattern: str
    """報告檔案的 glob pattern"""
    min_count: int = 1
    corner_list: list[str] = []
    """若有指定，則為該 stage 需覆蓋的 corner 清單"""


class ParsingRule(BaseModel):
    """從報告中擷取指標的解析規則。"""
    id: str
    type: str = "regex"
    """解析類型，目前只支援 regex"""
    target_file: str
    """目標檔案的 glob pattern"""
    pattern: str
    """正則表達式，必須包含一個 capture group"""
    metric_name: str
    """萃取後的指標名稱"""
    data_type: str = "float"
    """資料類型：float / int"""


class QualityGate(BaseModel):
    """品質門檻定義。"""
    id: str
    metric: str
    """要評估的指標名稱"""
    operator: str
    """比較運算子：>= / <= / == / > / <"""
    threshold: float
    on_fail: str
    """失敗時的動作：WARN / BLOCK_NEXT_STAGE / BLOCK_TAPEOUT 等"""
    scope: str = "SINGLE"
    """評估範圍：SINGLE（單次）/ ALL_CORNERS（全部 corner）"""
    description: str = ""


class TriggerAction(BaseModel):
    """觸發條件與動作對應。"""
    condition: str
    """觸發條件表達式，例如 "GATE_WNS_TRIAL == FAIL" """
    actions: list[str]
    """動作清單"""


class CompletionRule(BaseModel):
    """階段完成規則。"""
    condition: str
    """完成條件表達式"""


class Milestone(BaseModel):
    """單一里程碑。"""
    id: str
    name: str
    stages: list[str]
    """所屬 stage ID 清單"""


class MilestoneModel(BaseModel):
    """里程碑模型。"""
    milestones: list[Milestone]


class StageSpec(BaseModel):
    """單一階段規格。"""
    id: str
    name: str
    tool_signatures: ToolSignatures
    required_reports: list[RequiredReport] = []
    parsing_rules: list[ParsingRule] = []
    quality_gates: list[QualityGate] = []
    trigger_action_map: list[TriggerAction] = []
    completion_rule: CompletionRule | None = None
    recommended_corner_order: list[str] = []
    """建議的 corner 執行順序（用於 proactive nudge）"""


# ─── 頂層結構 ─────────────────────────────────────────────────────────────────

class Playbook(BaseModel):
    """工作劇本頂層結構。"""
    id: str
    name: str
    description: str = ""
    playbook_type: str = "signoff_numeric"
    """Schema 類型的 discriminator；為未來分化不同 playbook schema 的切入點"""
    version: str = "0.0.0"
    """Playbook 本身的版本號，採 SemVer 2.0.0 格式"""
    milestone_model: MilestoneModel
    stage_specs: list[StageSpec]


# ─── 函式 ─────────────────────────────────────────────────────────────────────

def load_playbook(path: str) -> Playbook:
    """
    從 YAML 檔案載入 playbook。

    - 讀取 YAML
    - 解析 playbook 頂層 key
    - 回傳 Playbook 物件
    - 若檔案不存在，raise FileNotFoundError
    - 若 YAML 格式不合法或缺必要欄位，raise ValueError
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Playbook file not found: {path}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "playbook" not in raw:
        raise ValueError("YAML must contain a top-level 'playbook' key")

    try:
        return Playbook.model_validate(raw["playbook"])
    except Exception as exc:
        raise ValueError(f"Invalid playbook structure: {exc}") from exc


def identify_stage(
    playbook: Playbook, cwd: str, tool_name: str,
) -> StageSpec | None:
    """
    根據 cwd 和 tool_name 識別目前所處的 stage。

    比對邏輯：
    1. tool_name 必須在 stage.tool_signatures.commands 中
    2. cwd 必須匹配 stage.tool_signatures.work_directory_pattern（使用 fnmatch）
    3. 兩個條件都滿足才算匹配
    4. 若有多個 stage 匹配，回傳第一個（按 stage_specs 定義順序）
    5. 若無匹配，回傳 None
    """
    for stage in playbook.stage_specs:
        sigs = stage.tool_signatures
        if tool_name not in sigs.commands:
            continue
        pat = sigs.work_directory_pattern
        # 同時匹配 "*/sta/signoff/subdir" 和 "*/sta/signoff"
        # pattern 以 /* 結尾時，額外嘗試去掉 trailing /* 做精確比對
        if fnmatch.fnmatch(cwd, pat):
            return stage
        if pat.endswith("/*") and fnmatch.fnmatch(cwd, pat[:-2]):
            return stage
    return None


def get_quality_gates(stage: StageSpec) -> list[QualityGate]:
    """取得指定 stage 的 quality gates。"""
    return stage.quality_gates


def get_parsing_rules(stage: StageSpec) -> list[ParsingRule]:
    """取得指定 stage 的 parsing rules。"""
    return stage.parsing_rules
