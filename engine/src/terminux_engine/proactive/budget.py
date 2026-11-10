# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Proactive cycle 的 provider budget。

Vendor-neutral：只認識 **call 數與 token 數**。任何金額都必須由呼叫端注入
pricing policy 才會出現——Common **不含**任何 provider 價目。

設計重點：

* **先 reserve 再呼叫**：call count 與 requested output budget 都在呼叫
  provider **之前**檢查；不足就直接拒絕，不會先花錢再說。
* **依實際 usage 更新**：回應後用 `GenerationMetadata` 的 input／output／
  total token 更新；metadata 缺失時明確標記 `usage_unavailable`，**不猜**。
* **看不到 usage 就不能無限制繼續**：連續無法觀察 usage 會讓 budget 進入
  exhausted，而不是預設「大概還好」。
* bounded repair 與 initial 呼叫**同屬一個 cycle**，共用同一份預算。
* 被 dedup／not_claimed 的 candidate **不消耗** 預算——它根本沒呼叫 provider。
* 沒有全域 scheduler、store 或 ledger；不做 daily／monthly accounting。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

RESERVED = "reserved"
DENIED_CALL_LIMIT = "call_limit_exhausted"
DENIED_REQUESTED_TOKENS = "requested_output_token_limit_exhausted"
DENIED_OBSERVED_INPUT = "observed_input_token_limit_exhausted"
DENIED_OBSERVED_OUTPUT = "observed_output_token_limit_exhausted"
DENIED_OBSERVED_TOTAL = "observed_total_token_limit_exhausted"
DENIED_USAGE_UNOBSERVABLE = "usage_unobservable_limit_reached"

COST_UNAVAILABLE = "cost_estimate_unavailable"

#: 連續幾次拿不到 usage metadata 就停止——看不到用量就不能無限制花下去。
DEFAULT_MAX_UNOBSERVED_CALLS = 2

#: 所有「預算在 provider call 之前拒絕」的原因。呼叫端可以用它把 budget
#: denial 與 provider failure 分開——兩者的正確處置不同。
BUDGET_DENIAL_REASONS: frozenset[str] = frozenset({
    DENIED_CALL_LIMIT, DENIED_REQUESTED_TOKENS, DENIED_OBSERVED_INPUT,
    DENIED_OBSERVED_OUTPUT, DENIED_OBSERVED_TOTAL, DENIED_USAGE_UNOBSERVABLE,
})


@dataclass(frozen=True)
class CycleBudgetLimits:
    """一個 cycle 的硬上限。全部是 call 數或 token 數，沒有金額。"""

    max_provider_calls: int
    max_requested_output_tokens: int
    max_observed_input_tokens: int
    max_observed_output_tokens: int
    max_observed_total_tokens: int
    max_unobserved_usage_calls: int = DEFAULT_MAX_UNOBSERVED_CALLS

    def __post_init__(self) -> None:
        for name in ("max_provider_calls", "max_requested_output_tokens",
                     "max_observed_input_tokens", "max_observed_output_tokens",
                     "max_observed_total_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_unobserved_usage_calls < 0:
            raise ValueError("max_unobserved_usage_calls must not be negative")

    def to_json_dict(self) -> dict[str, int]:
        return {"max_provider_calls": self.max_provider_calls,
                "max_requested_output_tokens": self.max_requested_output_tokens,
                "max_observed_input_tokens": self.max_observed_input_tokens,
                "max_observed_output_tokens": self.max_observed_output_tokens,
                "max_observed_total_tokens": self.max_observed_total_tokens,
                "max_unobserved_usage_calls": self.max_unobserved_usage_calls}


def derive_cycle_limits(
    *,
    max_candidates: int,
    max_calls_per_candidate: int,
    requested_output_tokens_per_call: int,
    input_allowance_tokens_per_call: int | None = None,
) -> CycleBudgetLimits:
    """從結構化上限推導一個 cycle 的 `CycleBudgetLimits`。可重算、無魔術數字。

    這是**安全天花板，不是節流閥**。理由是 lifecycle 語意：`RECORD_FAILED`
    屬於 `SUPPRESSING_RECORD_TYPES`，而本版**不自動重跑既有 trigger**
    （見 `lifecycle.py`）。因此預算若在正常運作中砍掉一個已 claim 的
    candidate，那筆發現就**永久消失**。所以上限一律取結構上不可能超過的值：
    它只擋 pathological runaway（prompt 暴脹、回應暴脹、usage 觀測不到），
    不擋正常分析。

    Vendor-neutral：只吃 call 數與 token 數，不認識任何 package、action、
    provider 或金額。呼叫端負責提供 domain 常數。

    `input_allowance_tokens_per_call` 未給時沿用 output 的 per-call 額度——
    prompt 本身是有界的（結構化欄位＋item 上限），實測遠低於此。
    """
    for name, value in (("max_candidates", max_candidates),
                        ("max_calls_per_candidate", max_calls_per_candidate),
                        ("requested_output_tokens_per_call",
                         requested_output_tokens_per_call)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if input_allowance_tokens_per_call is None:
        input_allowance_tokens_per_call = requested_output_tokens_per_call
    elif (not isinstance(input_allowance_tokens_per_call, int)
          or isinstance(input_allowance_tokens_per_call, bool)
          or input_allowance_tokens_per_call <= 0):
        raise ValueError("input_allowance_tokens_per_call must be a positive integer")

    calls = max_candidates * max_calls_per_candidate
    requested_output = calls * requested_output_tokens_per_call
    observed_input = calls * input_allowance_tokens_per_call
    # observed output 不可能超過自己 request 的量——provider 受 max_tokens 約束。
    observed_output = requested_output
    # total 必須容納 reasoning／thinking token：某些 provider 回報的 total
    # 會大於 input + output。取兩者之和為上限，不另外加係數。
    observed_total = observed_input + observed_output
    return CycleBudgetLimits(
        max_provider_calls=calls,
        max_requested_output_tokens=requested_output,
        max_observed_input_tokens=observed_input,
        max_observed_output_tokens=observed_output,
        max_observed_total_tokens=observed_total,
    )


@dataclass(frozen=True)
class Reservation:
    """一次 provider call 的預留結果。`granted=False` 時**不得**呼叫 provider。"""

    granted: bool
    reason: str
    requested_output_tokens: int | None = None
    call_index: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {"granted": self.granted, "reason": self.reason,
                "requested_output_tokens": self.requested_output_tokens,
                "call_index": self.call_index}


@dataclass
class CycleBudget:
    """單一 cycle 的預算狀態。沒有跨 cycle 累計、沒有持久化。"""

    limits: CycleBudgetLimits
    #: optional、由呼叫端注入的 `(input_tokens, output_tokens) -> float` 估價。
    #: Common 不提供任何預設價目。
    pricing_policy: Callable[[int, int], float] | None = None

    provider_calls: int = 0
    requested_output_tokens: int = 0
    observed_input_tokens: int = 0
    observed_output_tokens: int = 0
    observed_total_tokens: int = 0
    unobserved_usage_calls: int = 0
    exhausted_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    # ── reserve ──────────────────────────────────────────────────────
    def reserve(self, *, requested_output_tokens: int) -> Reservation:
        """在呼叫 provider **之前** 檢查。任何一項不足就直接拒絕。"""
        if self.exhausted_reason is not None:
            return self._deny(self.exhausted_reason, requested_output_tokens)
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            return self._exhaust(DENIED_CALL_LIMIT, requested_output_tokens)
        if (self.requested_output_tokens + requested_output_tokens
                > self.limits.max_requested_output_tokens):
            return self._exhaust(DENIED_REQUESTED_TOKENS, requested_output_tokens)
        if self.unobserved_usage_calls >= self.limits.max_unobserved_usage_calls > 0:
            return self._exhaust(DENIED_USAGE_UNOBSERVABLE, requested_output_tokens)
        self.provider_calls += 1
        self.requested_output_tokens += requested_output_tokens
        reservation = Reservation(True, RESERVED, requested_output_tokens,
                                  self.provider_calls)
        self.events.append({"event": "reserved", **reservation.to_json_dict()})
        return reservation

    def _deny(self, reason: str, requested: int) -> Reservation:
        denial = Reservation(False, reason, requested)
        self.events.append({"event": "denied", **denial.to_json_dict()})
        return denial

    def _exhaust(self, reason: str, requested: int) -> Reservation:
        self.exhausted_reason = reason
        return self._deny(reason, requested)

    # ── settle ───────────────────────────────────────────────────────
    def record_usage(self, metadata: Any | None) -> dict[str, Any]:
        """依 `CompletionMetadata` 更新實際用量。

        metadata 或其 token 欄位缺失時標 `usage_unavailable`，**不推估**。
        """
        payload: dict[str, Any] = {"event": "usage"}
        get = (lambda name: getattr(metadata, name, None)) if metadata else (
            lambda name: None)
        input_tokens, output_tokens = get("input_tokens"), get("output_tokens")
        total_tokens = get("total_tokens")
        if input_tokens is None and output_tokens is None and total_tokens is None:
            self.unobserved_usage_calls += 1
            payload.update({"usage_available": False,
                            "reason": "usage_unavailable",
                            "unobserved_usage_calls": self.unobserved_usage_calls})
            if self.unobserved_usage_calls >= self.limits.max_unobserved_usage_calls > 0:
                self.exhausted_reason = DENIED_USAGE_UNOBSERVABLE
                payload["exhausted_reason"] = self.exhausted_reason
            self.events.append(payload)
            return payload

        self.observed_input_tokens += int(input_tokens or 0)
        self.observed_output_tokens += int(output_tokens or 0)
        self.observed_total_tokens += int(
            total_tokens if total_tokens is not None
            else (input_tokens or 0) + (output_tokens or 0))
        payload.update({"usage_available": True,
                        "input_tokens": input_tokens, "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                        "finish_reason": get("finish_reason")})
        for value, limit, reason in (
            (self.observed_input_tokens, self.limits.max_observed_input_tokens,
             DENIED_OBSERVED_INPUT),
            (self.observed_output_tokens, self.limits.max_observed_output_tokens,
             DENIED_OBSERVED_OUTPUT),
            (self.observed_total_tokens, self.limits.max_observed_total_tokens,
             DENIED_OBSERVED_TOTAL),
        ):
            if value > limit and self.exhausted_reason is None:
                self.exhausted_reason = reason
                payload["exhausted_reason"] = reason
        self.events.append(payload)
        return payload

    # ── report ───────────────────────────────────────────────────────
    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    def remaining(self) -> dict[str, int]:
        return {
            "provider_calls": max(0, self.limits.max_provider_calls
                                  - self.provider_calls),
            "requested_output_tokens": max(
                0, self.limits.max_requested_output_tokens
                - self.requested_output_tokens),
            "observed_input_tokens": max(
                0, self.limits.max_observed_input_tokens - self.observed_input_tokens),
            "observed_output_tokens": max(
                0, self.limits.max_observed_output_tokens
                - self.observed_output_tokens),
            "observed_total_tokens": max(
                0, self.limits.max_observed_total_tokens - self.observed_total_tokens),
        }

    def cost_estimate(self) -> dict[str, Any]:
        """只有注入 pricing policy 才有金額；否則明確標 unavailable。"""
        if self.pricing_policy is None:
            return {"available": False, "reason": COST_UNAVAILABLE,
                    "note": ("Common tracks calls and tokens only; a monetary "
                             "estimate requires an injected pricing policy")}
        try:
            amount = float(self.pricing_policy(self.observed_input_tokens,
                                               self.observed_output_tokens))
        except Exception as exc:  # noqa: BLE001 - 估價失敗不影響 budget 判定
            return {"available": False, "reason": "pricing_policy_failed",
                    "error": f"{exc!r}"}
        return {"available": True, "amount": amount,
                "input_tokens": self.observed_input_tokens,
                "output_tokens": self.observed_output_tokens}

    def to_json_dict(self) -> dict[str, Any]:
        return {"limits": self.limits.to_json_dict(),
                "provider_calls": self.provider_calls,
                "requested_output_tokens": self.requested_output_tokens,
                "observed_input_tokens": self.observed_input_tokens,
                "observed_output_tokens": self.observed_output_tokens,
                "observed_total_tokens": self.observed_total_tokens,
                "unobserved_usage_calls": self.unobserved_usage_calls,
                "exhausted": self.exhausted,
                "exhausted_reason": self.exhausted_reason,
                "remaining": self.remaining(),
                "cost_estimate": self.cost_estimate(),
                "events": list(self.events)}


__all__ = [
    "BUDGET_DENIAL_REASONS",
    "COST_UNAVAILABLE", "CycleBudget", "CycleBudgetLimits",
    "DEFAULT_MAX_UNOBSERVED_CALLS", "DENIED_CALL_LIMIT", "DENIED_OBSERVED_INPUT",
    "DENIED_OBSERVED_OUTPUT", "DENIED_OBSERVED_TOTAL", "DENIED_REQUESTED_TOKENS",
    "DENIED_USAGE_UNOBSERVABLE", "RESERVED", "Reservation",
    "derive_cycle_limits",
]
