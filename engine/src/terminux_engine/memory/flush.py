# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Memory Flush — Compaction 前從對話中提取重要結論並存入 EventStore。

讓對話中尚未被結構化記錄的 insights 不因壓縮而遺失。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from terminux_engine.llm.llm_router import LLMRouter
    from terminux_engine.store.event_store import EventStore

logger = logging.getLogger(__name__)

# insight 行解析：- [topic] content
_TOPIC_RE = re.compile(r"\[([^\]]+)\]\s*(.+)")


class MemoryFlusher:
    """Compaction 前先 flush 重要結論到 event_store。"""

    FLUSH_PROMPT_TEMPLATE = """以下是一段工程師和 AI 助理的對話。
請列出對話中的重要結論、決策、建議，這些尚未被系統結構化記錄。
逐條列出，格式：
- [topic] 結論內容

只列重要的，跳過閒聊和重複內容。若沒有重要結論，回覆「無」。

[對話內容]
{conversation_text}"""

    def __init__(
        self,
        llm_router: LLMRouter,
        event_store: EventStore,
    ) -> None:
        self._llm_router = llm_router
        self._event_store = event_store

    def flush(
        self,
        conversation_text: str,
        terminal_id: str,
        session_id: str,
        user_id: str,
        compacted_from_turns: int = 0,
    ) -> list[dict[str, str]]:
        """從對話中提取 insights 並存成 conversation_insight events。

        回傳 list[{"topic": str, "content": str}]。
        LLM 呼叫失敗時回傳空 list，不 raise。
        """
        from terminux_engine.llm.llm_router import TaskType

        prompt = self.FLUSH_PROMPT_TEMPLATE.format(conversation_text=conversation_text)

        try:
            raw = self._llm_router.generate(prompt, task_type=TaskType.BACKGROUND)
        except Exception as exc:
            logger.warning("Memory flush LLM call failed: %s", exc)
            return []

        insights = self._parse_insights(raw)

        if insights:
            self._emit_insight_event(
                insights=insights,
                terminal_id=terminal_id,
                session_id=session_id,
                user_id=user_id,
                compacted_from_turns=compacted_from_turns,
            )

        return insights

    def _parse_insights(self, llm_response: str) -> list[dict[str, str]]:
        """解析 LLM 回應成 insights list。

        解析規則：
        - 若回覆包含「無」或空白 → 回空 list
        - 每行開頭 "- " 的嘗試解析
        - [topic] 用 regex 抽取
        - 無 [topic] → topic = "general"
        - 解析失敗的行跳過
        """
        text = llm_response.strip()
        if not text or "無" in text and len(text) < 10:
            return []

        results: list[dict[str, str]] = []

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue

            body = line[2:].strip()
            if not body:
                continue

            m = _TOPIC_RE.match(body)
            if m:
                results.append({"topic": m.group(1), "content": m.group(2)})
            else:
                results.append({"topic": "general", "content": body})

        return results

    def _emit_insight_event(
        self,
        insights: list[dict[str, str]],
        terminal_id: str,
        session_id: str,
        user_id: str,
        compacted_from_turns: int,
    ) -> None:
        """存成 conversation_insight event。"""
        from terminux_engine.store.event_store import SessionEvent

        self._event_store.emit(SessionEvent(
            event_id=uuid.uuid4().hex,
            session_id=session_id,
            terminal_id=terminal_id,
            user_id=user_id,
            timestamp=time.time(),
            event_type="conversation_insight",
            payload={
                "insights": insights,
                "compacted_from_turns": compacted_from_turns,
            },
        ))
