# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""GCD ``CORE_UTILIZATION`` 的正式、可讀回工程契約。

這個模組故意不依賴 provider、ORFS 或 runner。它只定義可由 Package、
Runtime 與離線 qualification 共用的整數規則，避免把第二輪搜尋藏在 runner。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

METHOD_ID = "gcd.core_utilization.v1"
PARAMETER = "CORE_UTILIZATION"
BASELINE = 55
MINIMUM = 35
MAXIMUM = 55
MAX_DECREMENT = 10
MAX_CANDIDATES = 2


@dataclass(frozen=True)
class CandidateWindow:
    """由已執行候選推導出的下一次唯一合法區間。"""

    previous_value: int
    previous_attempt_id: str | None
    executed_candidates: tuple[int, ...]
    minimum: int
    maximum: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "baseline": BASELINE,
            "global_minimum": MINIMUM,
            "global_maximum": MAXIMUM,
            "max_decrement": MAX_DECREMENT,
            "max_candidates": MAX_CANDIDATES,
            "previous_executed_value": self.previous_value,
            "previous_attempt_id": self.previous_attempt_id,
            "executed_candidates": list(self.executed_candidates),
            "allowed_candidate_minimum": self.minimum,
            "allowed_candidate_maximum": self.maximum,
        }


class CoreUtilizationError(ValueError):
    """候選、歷史或續作依據不符合公開工程控制。"""


def _exact_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoreUtilizationError(f"{field} must be an exact integer")
    return value


def candidate_window(executed: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> CandidateWindow:
    """從 Work 已執行 attempts 推導下一次範圍；不接受猜測或不完整 lineage。"""
    if len(executed) > MAX_CANDIDATES:
        raise CoreUtilizationError("candidate attempt limit already exceeded")
    values: list[int] = []
    previous_attempt_id: str | None = None
    previous = BASELINE
    for index, row in enumerate(executed):
        if not isinstance(row, dict):
            raise CoreUtilizationError("executed attempt lineage is malformed")
        value = _exact_integer(row.get("candidate"), f"executed[{index}].candidate")
        attempt_id = row.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise CoreUtilizationError("executed attempt identity is missing")
        if value in values:
            raise CoreUtilizationError("an executed candidate cannot be repeated")
        if value >= previous or previous - value > MAX_DECREMENT:
            raise CoreUtilizationError("executed candidate violates decrement bound")
        if not MINIMUM <= value <= MAXIMUM:
            raise CoreUtilizationError("executed candidate is outside global range")
        values.append(value)
        previous, previous_attempt_id = value, attempt_id
    if len(values) >= MAX_CANDIDATES:
        raise CoreUtilizationError("no further candidate is authorized")
    return CandidateWindow(
        previous_value=previous,
        previous_attempt_id=previous_attempt_id,
        executed_candidates=tuple(values),
        minimum=max(MINIMUM, previous - MAX_DECREMENT),
        maximum=previous - 1,
    )


def validate_candidate(candidate: Any, window: CandidateWindow) -> None:
    """嚴格驗證一個候選；浮點、字串、提升與重複一律拒絕。"""
    value = _exact_integer(candidate, "candidate")
    if value in window.executed_candidates:
        raise CoreUtilizationError("candidate was already executed")
    if not window.minimum <= value <= window.maximum:
        raise CoreUtilizationError("candidate is outside the authorized decrement window")


def continuation_decision(result: dict[str, Any], *, window: CandidateWindow,
                          remaining: dict[str, int]) -> tuple[bool, str]:
    """判定是否**可以考慮**第二輪；任何不足均為 STOP。

    這不是 acceptance：setup 改善只是允許進入下一次提案，而非接受候選。
    """
    required = {
        "objective_unmet": True,
        "comparable": True,
        "selection_eligible": True,
        "no_new_protected_violations": True,
        "setup_improved": True,
        "timing_expert_supports_headroom": True,
        "comparison_expert_supports_revision": True,
    }
    for field, expected in required.items():
        if result.get(field) is not expected:
            return False, f"STOP_{field.upper()}"
    if window.previous_attempt_id is None:
        return False, "STOP_NO_FIRST_ATTEMPT"
    minimum_budget = {"engineering_attempts": 1, "provider_dispatches": 3,
                      "tool_launches": 1, "candidate_materializations": 1}
    for key, need in minimum_budget.items():
        value = remaining.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < need:
            return False, "STOP_REMAINING_BUDGET"
    return True, "REQUEST_REVISION_ELIGIBLE"


__all__ = ["BASELINE", "MAX_CANDIDATES", "MAX_DECREMENT", "MAXIMUM", "METHOD_ID",
           "MINIMUM", "PARAMETER", "CandidateWindow", "CoreUtilizationError",
           "candidate_window", "continuation_decision", "validate_candidate"]
