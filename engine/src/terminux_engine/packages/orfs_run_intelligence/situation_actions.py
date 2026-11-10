# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_run_intelligence.assess_situation` action 的 entry point。

Use case 實作在 `situation_service.py`——proactive intelligence 把它抽出來，讓 proactive
path 能共用同一份邏輯而不必經過 `ActionRegistry.call()`。

**這裡必須留一個真正的頂層 `def assess_situation`。**
`package_runtime/validator.py` 對 entrypoint module 做 AST 靜態掃描，要求
`tree.body` 內有同名的頂層 `FunctionDef`；import 進來的名字**不算**。
之前把本檔改成純 re-export，導致 `MISSING_HANDLER_FUNCTION`，整棵
production package tree 驗證不過。validator 的 containment 規則是刻意的，
不得為了遷就本 package 而放寬——要配合的是這裡。
"""

from __future__ import annotations

from typing import Any

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.packages.orfs_run_intelligence import situation_service
from terminux_engine.packages.orfs_run_intelligence.situation_service import (
    DEFAULT_QUESTION,
    AssessmentRequest,
    assess,
)


def assess_situation(ctx: PackageContext, query: str | None = None) -> dict[str, Any]:
    """Action entry point。永不拋出到 Agent——失敗一律回結構化狀態。

    薄轉呼叫，**不複製任何 domain logic**：參數原樣傳遞、回傳值原樣回傳、
    exception 不攔截也不改寫（`situation_service` 自己已經把失敗轉成結構化
    狀態；這裡若加 try/except 反而會吞掉真正的程式錯誤）。
    """
    return situation_service.assess_situation(ctx, query)


__all__ = ["DEFAULT_QUESTION", "AssessmentRequest", "assess", "assess_situation"]
