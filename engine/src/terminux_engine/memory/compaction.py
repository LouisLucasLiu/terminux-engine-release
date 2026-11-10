# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Conversation Compaction — 呼叫 LLM 壓縮對話歷史成摘要。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terminux_engine.llm.llm_router import LLMRouter

logger = logging.getLogger(__name__)


class ConversationCompactor:
    """壓縮對話歷史，保留關鍵資訊。"""

    COMPACTION_PROMPT_TEMPLATE = """以下是一段工程師和 AI 助理的對話。
請把這段對話壓縮成重點摘要，保留：
- 每個 corner / block / topic 的討論結論
- 具體的數字（WNS、TNS、violation count 等）
- user 做的決策
- 未解決的問題
- 具體的技術建議

摘要控制在 2000 字以內。用條列式整理，不要省略重要細節。

[對話內容]
{conversation_text}"""

    def __init__(self, llm_router: LLMRouter) -> None:
        self._llm_router = llm_router

    def compact(self, conversation_text: str) -> str:
        """呼叫 LLM 壓縮對話。

        LLM 呼叫失敗時回傳 fallback 訊息。
        """
        from terminux_engine.llm.llm_router import TaskType

        prompt = self.COMPACTION_PROMPT_TEMPLATE.format(
            conversation_text=conversation_text,
        )

        try:
            result = self._llm_router.generate(prompt, task_type=TaskType.BACKGROUND)
            if result and result.strip():
                return result.strip()
        except Exception as exc:
            logger.warning("Conversation compaction LLM call failed: %s", exc)

        # fallback：用行數估算輪數
        turn_count = conversation_text.count("User:") + conversation_text.count("Assistant:")
        return f"[對話摘要自動產生失敗，原始對話有 {turn_count} 輪]"
