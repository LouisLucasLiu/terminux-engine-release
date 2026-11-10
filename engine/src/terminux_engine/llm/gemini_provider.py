# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Multi-Model LLM Provider：Google Gemini adapter。

GeminiCompletionProvider 與 LlamaCppCompletionProvider 完全對稱：
- lazy init：__init__ 不載入任何外部套件
- 同一 complete(prompt: str) -> str 簽名
- google-genai SDK 只在第一次 complete() 時 import（lazy import）

支援模型：gemini-3-pro / gemini-3-flash 等任何 Gemini API 可用模型。
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 僅供型別註記，避免 runtime import
    from terminux_engine.llm.generation import CompletionResult

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeminiProviderConfig:
    """Gemini API 初始化設定。"""
    model: str            # "gemini-3-pro", "gemini-3-flash", etc.
    api_key: str
    max_tokens: int = 4096
    temperature: float = 0.0


class GeminiCompletionProvider:
    """
    Google Gemini completion provider。

    與 LlamaCppCompletionProvider 直接互換（complete(prompt: str) -> str）。

    lazy loading：__init__ 不 import google.genai。
    第一次 complete() 呼叫時才初始化 client。
    """

    def __init__(self, config: GeminiProviderConfig) -> None:
        self._config = config
        self._client = None   # lazy 初始化。型：genai.Client | None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _init_client(self) -> None:
        """
        第一次 complete() 呼叫時的 lazy initializer。

        失敗類型：
        - google-genai 未安裝         → RuntimeError 包裝後 re-raise
        - Client 初始化失敗           → 上層傳播（fail-open 由 server 層負責）
        """
        try:
            from google import genai  # type: ignore[import]
        except ImportError as e:
            raise RuntimeError(
                f"google-genai package is not available: {e}"
            ) from e

        self._client = genai.Client(api_key=self._config.api_key)

    # ── Public API ─────────────────────────────────────────────────────────────

    #: 這個 provider 接受 per-request output budget（Router 以此判斷能力）。
    supports_output_budget: bool = True

    def complete(self, prompt: str, *, max_output_tokens: int | None = None) -> str:
        """
        呼叫 Gemini API 取得 completion，回傳純文字字串。

        - response.text 為 None / 空 → RuntimeError
        - API 呼叫失敗                → 上層傳播

        `max_output_tokens` 為 `None` 時沿用 `GeminiProviderConfig.max_tokens`，
        行為與加入此參數之前完全相同。
        """
        return self.complete_with_metadata(
            prompt, max_output_tokens=max_output_tokens).text

    def complete_with_metadata(
        self, prompt: str, *, max_output_tokens: int | None = None
    ) -> "CompletionResult":
        """與 `complete()` 相同的請求，但一併回傳 completion metadata。

        文字內容與 `complete()` 完全一致——這裡**不做任何本地裁切**，
        回傳的就是 SDK 給的 `response.text`，供稽核核對。
        """
        from terminux_engine.llm.generation import (
            FINISH_MAX_OUTPUT_TOKENS, FINISH_OTHER, FINISH_STOP, FINISH_UNAVAILABLE,
            CompletionMetadata, CompletionResult, sha256_text,
        )

        if self._client is None:
            self._init_client()

        from google.genai.types import GenerateContentConfig  # type: ignore[import]

        effective = (max_output_tokens if max_output_tokens is not None
                     else self._config.max_tokens)
        response = self._client.models.generate_content(
            model=self._config.model,
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=effective,
                temperature=self._config.temperature,
            ),
        )

        text = response.text
        if text is None:
            raise RuntimeError(
                f"Gemini returned no text. response={str(response)[:200]!r}"
            )

        raw_finish = None
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            reason = getattr(candidates[0], "finish_reason", None)
            raw_finish = getattr(reason, "name", None) or (
                str(reason) if reason is not None else None)
        finish = {
            "STOP": FINISH_STOP, "MAX_TOKENS": FINISH_MAX_OUTPUT_TOKENS,
        }.get(raw_finish or "", FINISH_OTHER if raw_finish else FINISH_UNAVAILABLE)

        usage = getattr(response, "usage_metadata", None)
        get = lambda name: getattr(usage, name, None) if usage is not None else None

        return CompletionResult(
            text=text,
            metadata=CompletionMetadata(
                provider="gemini", model=self._config.model,
                requested_output_tokens=max_output_tokens,
                effective_output_tokens=effective, budget_supported=True,
                finish_reason=finish, provider_finish_reason=raw_finish,
                response_chars=len(text), response_sha256=sha256_text(text),
                input_tokens=get("prompt_token_count"),
                output_tokens=get("candidates_token_count"),
                reasoning_tokens=get("thoughts_token_count"),
                total_tokens=get("total_token_count"),
            ),
        )

    # ── multi-turn chat + function calling ───────────────

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_output_tokens: int | None = None,
    ) -> dict:
        """
        Multi-turn chat with optional Gemini native function calling.

        messages: OpenAI-style message list
          - {"role": "system", "content": "..."}      → system_instruction（多筆連接）
          - {"role": "user", "content": "..."}        → user text part
          - {"role": "assistant", "content": "..."}   → model text part
          - {"role": "assistant", "tool_calls": [...]}→ model function_call parts
          - {"role": "tool", "tool_call_id"?, "name", "content"}
                                                       → user function_response part

        tools: OpenAI function calling schema list；None / 空 list 表示不用 tools。

        回傳 normalized dict（供 ResponseParser.parse_openai 解析）::

            {
                "tool_calls": [{"id": "call_0", "name": "...", "arguments": {...}}, ...],
                "text": "..."  # 或 None
            }
        """
        if self._client is None:
            self._init_client()

        from google.genai import types as genai_types  # type: ignore[import]

        system_instruction = _extract_system_instruction(messages)
        contents = _convert_messages_to_contents(messages, genai_types)
        gemini_tools = _convert_tools(tools, genai_types) if tools else None

        config_kwargs: dict = {
            "max_output_tokens": (max_output_tokens if max_output_tokens is not None
                                  else self._config.max_tokens),
            "temperature": self._config.temperature,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if gemini_tools:
            config_kwargs["tools"] = gemini_tools

        response = self._client.models.generate_content(
            model=self._config.model,
            contents=contents,
            config=genai_types.GenerateContentConfig(**config_kwargs),
        )
        return _normalize_response(response)


# ─── Helpers（module-level，方便單元測試）─────────────────────────────────────

def _extract_system_instruction(messages: list[dict]) -> str | None:
    """多筆 system message 連接成單一 system_instruction。"""
    parts = [
        m.get("content", "") or ""
        for m in messages
        if m.get("role") == "system"
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return "\n\n".join(parts)


def _convert_messages_to_contents(messages: list[dict], genai_types) -> list:
    """把 OpenAI-style messages 轉成 Gemini Content list（不含 system）。"""
    import json as _json

    contents: list = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue

        if role == "user":
            text = m.get("content", "") or ""
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=text)],
                )
            )

        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                parts = []
                for tc in tool_calls:
                    part = genai_types.Part.from_function_call(
                        name=tc.get("name", ""),
                        args=tc.get("arguments") or {},
                    )
                    # 原樣還原 Gemini 3.x 的 opaque thought signature；
                    # 舊 model 不回傳這個欄位時完全不受影響。
                    signature = tc.get("thought_signature")
                    if signature:
                        try:
                            part.thought_signature = signature
                        except Exception:  # noqa: BLE001 - 舊 SDK 沒這個欄位
                            pass
                    parts.append(part)
            else:
                text = m.get("content", "") or ""
                parts = [genai_types.Part.from_text(text=text)]
            contents.append(genai_types.Content(role="model", parts=parts))

        elif role == "tool":
            name = m.get("name", "") or ""
            raw = m.get("content", "")
            try:
                parsed = _json.loads(raw) if isinstance(raw, str) else raw
            except (_json.JSONDecodeError, TypeError):
                parsed = raw
            if not isinstance(parsed, dict):
                parsed = {"result": parsed}
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part.from_function_response(
                            name=name,
                            response=parsed,
                        )
                    ],
                )
            )

    return contents


def _convert_tools(tools: list[dict], genai_types) -> list:
    """OpenAI function calling schema → Gemini Tool list。"""
    decls = []
    for t in tools or []:
        if t.get("type") == "function" and "function" in t:
            fn = t["function"]
        else:
            fn = t
        decls.append(
            genai_types.FunctionDeclaration(
                name=fn.get("name", ""),
                description=fn.get("description", ""),
                parameters_json_schema=fn.get("parameters", {}) or {"type": "object"},
            )
        )
    if not decls:
        return []
    return [genai_types.Tool(function_declarations=decls)]


def _normalize_response(response) -> dict:
    """Gemini 原生 response → AgentRunner 期待的 normalized dict。

    結構：response.candidates[0].content.parts → 每個 part 可能有 function_call
    或 text。多個 function_call 全部回傳（支援 parallel tool calls）。
    """
    tool_calls: list = []
    text_chunks: list[str] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return {"tool_calls": [], "text": None}
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or [] if content else []

        for i, p in enumerate(parts):
            fc = getattr(p, "function_call", None)
            fc_name = getattr(fc, "name", "") if fc is not None else ""
            if fc is not None and fc_name:
                args = getattr(fc, "args", None) or {}
                if not isinstance(args, dict):
                    try:
                        args = dict(args)
                    except (TypeError, ValueError):
                        args = {}
                call: dict = {
                    "id": f"call_{i}",
                    "name": fc_name,
                    "arguments": args,
                }
                # Gemini 3.x：function call part 會帶一個 opaque
                # `thought_signature`，下一次把同一個 model turn 送回去時
                # **必須**原樣附上，否則 provider 回
                # `400 INVALID_ARGUMENT ... missing a thought_signature`。
                # Terminux 不解讀它的內容，只負責原樣攜帶。
                signature = getattr(p, "thought_signature", None)
                if signature:
                    call["thought_signature"] = signature
                tool_calls.append(call)
                continue
            txt = getattr(p, "text", None)
            if txt:
                text_chunks.append(txt)
    except Exception:
        # 結構意外 → 退到 response.text
        fallback = getattr(response, "text", None)
        return {"tool_calls": [], "text": fallback}

    return {
        "tool_calls": tool_calls,
        "text": "\n".join(text_chunks) if text_chunks else None,
    }
