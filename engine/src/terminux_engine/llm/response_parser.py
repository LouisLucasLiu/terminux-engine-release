# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent Intelligence — LLM 回應解析。

支援雙路徑解析（Q6 決策）：

- parse_openai：從 normalized provider response dict 取 tool_calls / text
- parse_xml：從純文字回應抓 <tool_call> 區塊

兩條路徑共用 ParsedResponse 結構，AgentRunner 不需要知道 provider 細節。

parse_openai 接受兩種 dict 格式：

1. 扁平 normalized（agent adapter 會產這種）::

       {"tool_calls": [{"id"?, "name", "arguments": dict | json-str}], "text": "..."}

2. OpenAI Chat Completions 原生::

       {"choices": [{"message": {"content": "...", "tool_calls": [...]}}]}

parse_xml 識別格式::

    <tool_call>
    <name>tool_name</name>
    <arguments>{"arg": "value"}</arguments>
    </tool_call>

純文字內含 [NO_ACTION]（不論位置）→ no_action=True。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from terminux_engine.llm.agent_models import ToolCallRequest

_log = logging.getLogger(__name__)


_NO_ACTION_MARKER = "[NO_ACTION]"

# 公開（供 ToolRegistry get_schemas 'xml' 測試自驗）
_TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*<name>\s*(.*?)\s*</name>\s*"
    r"<arguments>\s*(.*?)\s*</arguments>\s*</tool_call>",
    re.DOTALL,
)


class ParsedResponse(BaseModel):
    """解析後的 LLM 回應。

    - has_tool_calls=True → tool_calls 非空，AgentRunner 接著執行
    - no_action=True → answer/tool_calls 應視為無動作
    - 兩者都 False → text 為最終答案
    """

    has_tool_calls: bool = False
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    text: str | None = None
    no_action: bool = False


class ResponseParser:
    """負責把 provider 回應解析成統一 ParsedResponse。"""

    # ── OpenAI / normalized dict 路徑 ─────────────────────────────────────────

    def parse_openai(self, response: dict[str, Any]) -> ParsedResponse:
        """解析 normalized provider response dict（見模組 docstring）。"""
        # OpenAI 原生 schema
        if "choices" in response and response["choices"]:
            msg = response["choices"][0].get("message", {}) or {}
            tool_calls_raw = msg.get("tool_calls") or []
            text = msg.get("content")
            tool_calls = [self._extract_openai_call(tc) for tc in tool_calls_raw]
            return self._build_response(tool_calls, text)

        # 扁平 normalized schema
        tool_calls_raw = response.get("tool_calls") or []
        text = response.get("text")
        tool_calls = [self._extract_flat_call(tc) for tc in tool_calls_raw]
        return self._build_response(tool_calls, text)

    @staticmethod
    def _extract_openai_call(tc: dict[str, Any]) -> ToolCallRequest:
        fn = tc.get("function", {}) if "function" in tc else tc
        args_raw = fn.get("arguments", {})
        args = _coerce_arguments(args_raw)
        return ToolCallRequest(
            tool_name=fn.get("name", ""),
            arguments=args,
            call_id=tc.get("id", ""),
        )

    @staticmethod
    def _extract_flat_call(tc: dict[str, Any]) -> ToolCallRequest:
        args = _coerce_arguments(tc.get("arguments", {}))
        return ToolCallRequest(
            tool_name=tc.get("name", ""),
            arguments=args,
            call_id=tc.get("id", ""),
            # Gemini 3.x opaque thought signature（沒有時為 None）。
            thought_signature=tc.get("thought_signature"),
        )

    # ── XML 路徑 ─────────────────────────────────────────────────────────────

    def parse_xml(self, response_text: str) -> ParsedResponse:
        """解析 LLM 純文字回應中的 <tool_call> 區塊與 [NO_ACTION] 標記。"""
        matches = _TOOL_CALL_PATTERN.findall(response_text)
        tool_calls: list[ToolCallRequest] = []
        for name, args_str in matches:
            try:
                args = json.loads(args_str)
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                _log.warning(
                    "Tool call arguments not valid JSON: %s", args_str[:200]
                )
                args = {}
            tool_calls.append(
                ToolCallRequest(
                    tool_name=name.strip(),
                    arguments=args,
                    call_id="",
                )
            )

        if tool_calls:
            return ParsedResponse(
                has_tool_calls=True,
                tool_calls=tool_calls,
                text=None,
                no_action=False,
            )

        if _NO_ACTION_MARKER in response_text:
            return ParsedResponse(no_action=True)

        text = response_text.strip()
        return ParsedResponse(text=text or None)

    # ── 共用 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_response(
        tool_calls: list[ToolCallRequest],
        text: str | None,
    ) -> ParsedResponse:
        if tool_calls:
            return ParsedResponse(
                has_tool_calls=True,
                tool_calls=tool_calls,
                text=None,
                no_action=False,
            )
        if text and _NO_ACTION_MARKER in text:
            return ParsedResponse(no_action=True)
        return ParsedResponse(text=text if text else None)


def _coerce_arguments(args_raw: Any) -> dict[str, Any]:
    """把 arguments 統一成 dict。

    - 已是 dict：直接回（None / 空 dict → {}）
    - 是 str：嘗試 json.loads；失敗回 {}
    - 其它型別：回 {}
    """
    if isinstance(args_raw, dict):
        return args_raw
    if isinstance(args_raw, str):
        try:
            parsed = json.loads(args_raw)
        except json.JSONDecodeError:
            _log.warning("Failed to parse tool arguments JSON: %s", args_raw[:200])
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
