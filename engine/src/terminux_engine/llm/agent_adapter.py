# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent LLM Provider Adapter（薄殼）。

把現有 LLM provider 包裝成 AgentRunner 期待的 chat(messages, tools) 介面。

設計（C 路徑）：
- gemini_provider / openai_compatible_provider 自己 normalize 成同一份扁平
  dict 給 ResponseParser.parse_openai，adapter 純轉發
- 本地模型（llama_cpp）暫不支援 → raise NotImplementedError

Adapter 只負責 provider_type 分派，不做任何 message / tool / response 轉換
邏輯——這些都在 provider.chat() 內。這樣 provider 切換時不會有 adapter
膨脹的 conversion code 需要同步維護。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from terminux_engine.llm import agent_audit

_log = logging.getLogger(__name__)


class AgentLLMAdapter:
    """Wrap 一個 LLM provider，暴露 chat(messages, tools) -> dict 給 AgentRunner。"""

    def __init__(
        self,
        provider: Any,
        provider_type: Literal["gemini", "openai_compatible", "llama_cpp"] = "gemini",
    ) -> None:
        if provider is None:
            raise ValueError("AgentLLMAdapter requires a non-None provider")
        self._provider = provider
        self._provider_type = provider_type

    @property
    def provider_type(self) -> str:
        return self._provider_type

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_output_tokens: int | None = None,
    ) -> dict:
        """AgentRunner 入口。回傳 normalized dict 由 ResponseParser.parse_openai 解析。"""
        provider_tools = tools
        provider_to_canonical: dict[str, str] = {}
        if self._provider_type == "openai_compatible":
            provider_tools, provider_to_canonical = _provider_safe_tools(tools)
        tool_names = [
            str(item.get("function", {}).get("name", ""))
            for item in (provider_tools or []) if isinstance(item, dict)
        ]
        agent_audit.emit(
            "llm.provider.request", result_status="sent", advertised_tools=tool_names,
            result_value={"message_count": len(messages), "tool_count": len(tool_names)},
        )
        try:
            if self._provider_type == "gemini":
                response = self._chat_gemini(messages, provider_tools, max_output_tokens)
            elif self._provider_type == "openai_compatible":
                response = self._chat_openai_compatible(
                    messages, provider_tools, max_output_tokens)
                response = _restore_canonical_tool_names(response, provider_to_canonical)
            elif self._provider_type == "llama_cpp":
                response = self._chat_local(messages, provider_tools)
            else:
                raise ValueError(f"Unknown provider_type: {self._provider_type}")
        except Exception as exc:
            # provider error 只記型別與 digest，不能把 provider body（可能含回顯
            # prompt）寫進 audit。
            agent_audit.emit(
                "llm.provider.error", result_status="error",
                result_value={"error_type": type(exc).__name__},
            )
            raise
        calls = response.get("tool_calls", []) if isinstance(response, dict) else []
        agent_audit.emit(
            "llm.provider.response", result_status="received",
            result_value={"tool_call_count": len(calls) if isinstance(calls, list) else 0},
        )
        return response

    # ── Gemini：薄殼，純轉發 ────────────────────────────────────────────────

    def _chat_gemini(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        max_output_tokens: int | None,
    ) -> dict:
        # gemini_provider.chat() 內部負責所有 OpenAI ↔ Gemini SDK 轉換
        if max_output_tokens is None:
            return self._provider.chat(messages, tools=tools)
        return self._provider.chat(messages, tools=tools,
                                   max_output_tokens=max_output_tokens)

    # ── OpenAI-compatible：純透傳（provider 已 normalize 成扁平 dict）───────

    def _chat_openai_compatible(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        max_output_tokens: int | None,
    ) -> dict:
        if max_output_tokens is None:
            return self._provider.chat(messages, tools=tools)
        return self._provider.chat(messages, tools=tools,
                                   max_output_tokens=max_output_tokens)

    # ── Local：預留 ─────────────────────────────────────────────────

    def _chat_local(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict:
        raise NotImplementedError(
            "Local model agent support is not available in this release. "
            "Local model uses XML prompt path which has not been wired yet."
        )


_DOT_ESCAPE = "__dot__"


def _provider_safe_tools(
    tools: list[dict] | None,
) -> tuple[list[dict] | None, dict[str, str]]:
    """轉換僅供 OpenAI-compatible provider 使用的 function name。

    JSON-RPC 的 canonical tool name 可含 ``.``，但 OpenAI-compatible function
    calling 僅接受英數、底線與連字號。此 adapter boundary 不改 ToolSchema 或
    ToolRegistry；回應在 dispatch 前會還原為 canonical name。任何 alias collision
    都 fail-closed，絕不猜測要執行哪個 tool。
    """
    if not tools:
        return tools, {}
    converted: list[dict] = []
    provider_to_canonical: dict[str, str] = {}
    for entry in tools:
        if not isinstance(entry, dict):
            converted.append(entry)
            continue
        function = entry.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            converted.append(entry)
            continue
        canonical_name = function["name"]
        provider_name = canonical_name.replace(".", _DOT_ESCAPE)
        existing = provider_to_canonical.get(provider_name)
        if existing is not None and existing != canonical_name:
            raise ValueError("OpenAI-compatible tool name alias collision")
        provider_to_canonical[provider_name] = canonical_name
        copied_function = dict(function)
        copied_function["name"] = provider_name
        copied_entry = dict(entry)
        copied_entry["function"] = copied_function
        converted.append(copied_entry)
    return converted, provider_to_canonical


def _restore_canonical_tool_names(
    response: dict,
    provider_to_canonical: dict[str, str],
) -> dict:
    """將 provider-safe alias 還原；未知名稱保持原值，交由 registry fail-closed。"""
    calls = response.get("tool_calls")
    if not isinstance(calls, list):
        return response
    restored_calls: list[object] = []
    for call in calls:
        if not isinstance(call, dict):
            restored_calls.append(call)
            continue
        name = call.get("name")
        restored = dict(call)
        if isinstance(name, str) and name in provider_to_canonical:
            restored["name"] = provider_to_canonical[name]
        restored_calls.append(restored)
    restored_response = dict(response)
    restored_response["tool_calls"] = restored_calls
    return restored_response
