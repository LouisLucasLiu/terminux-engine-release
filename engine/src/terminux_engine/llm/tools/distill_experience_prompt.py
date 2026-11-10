# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Experience distillation 的獨立 LLM prompt template。"""

from __future__ import annotations


DISTILL_EXPERIENCE_PROMPT_TEMPLATE = """你是 IC 設計專案的資深技術導師。
請從下列歷史 dreaming digest 與對話紀錄中，提煉可跨項目重複使用的工程經驗。

要求：
1. 只輸出有明確可複用價值的經驗；忽略專案名稱、個人資訊與一次性的雜訊。
2. 每個經驗必須可獨立閱讀，包含適用情境、觀察／原理、可操作建議與常見陷阱。
3. 每個經驗以 Markdown 撰寫，並嚴格包在下列 marker 中：
   [EXPERIENCE]
   # 簡短主題標題
   ...
   [/EXPERIENCE]
4. 不要輸出 marker 以外的文字；至少產生一個經驗，無法提煉時不要猜測。

來源專案：{source_project}
歷史素材：
{source_material}
"""


def build_distill_experience_prompt(source_project: str, source_material: str) -> str:
    """以來源專案與已受預算限制的素材建立 distillation prompt。"""
    return DISTILL_EXPERIENCE_PROMPT_TEMPLATE.format(
        source_project=source_project,
        source_material=source_material,
    )
