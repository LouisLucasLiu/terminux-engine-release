# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Generation request options 與 completion metadata（vendor-neutral）。

這一層刻意只描述**共通**概念：一次生成可以帶一個 output budget，完成後可以
拿回 finish reason 與 token usage。它不認識任何 provider SDK、EDA 工具或
domain 語意——provider 專屬的物件不得洩漏到這裡，也不得洩漏到 Package。

背景：`LLMRouter.generate(max_tokens=512)` 這個參數從來沒有被
轉交給 provider，`GeminiCompletionProvider.complete()` 只吃 prompt，於是實際
生效的一直是 `GeminiProviderConfig.max_tokens` 的 dataclass 預設 4096。
explain_run 的結構化輸出貼著這個上限，加上它沒有 repair round，就會**間歇性**整份
失敗（回應在 JSON 中途被截斷）。修法不是把 512 直接接上去——那會讓所有
未指定的呼叫端一起被砍到 512——而是讓 budget 成為 **optional、具名、
per-request** 的選項，未指定時沿用 provider config 預設。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

#: `finish_reason` 的 vendor-neutral 取值。provider 專屬字串會被正規化到這裡。
FINISH_STOP = "stop"
FINISH_MAX_OUTPUT_TOKENS = "max_output_tokens"
FINISH_OTHER = "other"
FINISH_UNAVAILABLE = "unavailable"
FINISH_REASONS: tuple[str, ...] = (
    FINISH_STOP, FINISH_MAX_OUTPUT_TOKENS, FINISH_OTHER, FINISH_UNAVAILABLE)

#: provider 不支援 per-request budget 時的原因碼。
BUDGET_UNSUPPORTED = "provider_does_not_accept_a_per_request_output_budget"


@dataclass(frozen=True)
class GenerationOptions:
    """一次生成的 per-request 選項。immutable，因此天生 concurrent-safe。

    `max_output_tokens` 為 `None` 代表「不指定」——沿用 provider config 的
    預設值，**不會**被降成任何較小的數字。
    """

    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer or None")


@dataclass(frozen=True)
class CompletionMetadata:
    """一次生成的可記錄結果。不含任何 provider 專屬物件。"""

    provider: str
    model: str
    requested_output_tokens: int | None
    effective_output_tokens: int | None
    budget_supported: bool
    finish_reason: str
    response_chars: int
    response_sha256: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    budget_unsupported_reason: str | None = None
    provider_finish_reason: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def truncated_by_budget(self) -> bool:
        """只有 provider 明說 MAX_TOKENS 才算；metadata 缺失時不猜。"""
        return self.finish_reason == FINISH_MAX_OUTPUT_TOKENS

    @property
    def finish_reason_available(self) -> bool:
        return self.finish_reason != FINISH_UNAVAILABLE

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model,
            "requested_output_tokens": self.requested_output_tokens,
            "effective_output_tokens": self.effective_output_tokens,
            "budget_supported": self.budget_supported,
            "budget_unsupported_reason": self.budget_unsupported_reason,
            "finish_reason": self.finish_reason,
            "provider_finish_reason": self.provider_finish_reason,
            "truncated_by_budget": self.truncated_by_budget,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "response_chars": self.response_chars,
            "response_sha256": self.response_sha256,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class CompletionResult:
    """文字 ＋ metadata。`text` 與既有 `complete()` 的回傳完全相同。"""

    text: str
    metadata: CompletionMetadata


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unsupported_budget_metadata(
    *, provider: str, model: str, text: str, requested: int | None,
    effective: int | None = None,
) -> CompletionMetadata:
    """給不支援 per-request budget 的 provider 用。

    **明確標示 unsupported**，不假裝已套用（Part B）。
    """
    return CompletionMetadata(
        provider=provider, model=model, requested_output_tokens=requested,
        effective_output_tokens=effective, budget_supported=False,
        budget_unsupported_reason=BUDGET_UNSUPPORTED if requested is not None else None,
        finish_reason=FINISH_UNAVAILABLE, response_chars=len(text),
        response_sha256=sha256_text(text),
        notes=("this provider exposes no completion metadata",))


__all__ = [
    "BUDGET_UNSUPPORTED",
    "CompletionMetadata",
    "CompletionResult",
    "FINISH_MAX_OUTPUT_TOKENS",
    "FINISH_OTHER",
    "FINISH_REASONS",
    "FINISH_STOP",
    "FINISH_UNAVAILABLE",
    "GenerationOptions",
    "sha256_text",
    "unsupported_budget_metadata",
]
