# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""LLM runtime latency policy decision。

decide_runtime_mode() 讀取 LlmLatencyProbeResult，依 warm run 的 median／max
判定 sync_blocking_allowed 或 async_required_before_default_enable。

判定規則（固定）：
  median(warm_run_secs) <= 1.5 且 max(warm_run_secs) <= 3.0
      → sync_blocking_allowed
  其他情況（含沒有測量值）
      → async_required_before_default_enable
"""

import statistics
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LlmLatencyProbeResult:
    """benchmark 測量結果。"""
    cold_run_sec: float
    warm_run_secs: list[float]


@dataclass(frozen=True)
class LlmRuntimeDecision:
    """latency policy 的最終判定。"""
    mode: Literal["sync_blocking_allowed", "async_required_before_default_enable"]
    rationale: str


# ── 門檻（固定）────────────────────────────────────────────────────────────────
_SYNC_MEDIAN_THRESHOLD: float = 1.5   # seconds
_SYNC_MAX_THRESHOLD:    float = 3.0   # seconds


def decide_runtime_mode(result: LlmLatencyProbeResult) -> LlmRuntimeDecision:
    """
    LlmLatencyProbeResult → LlmRuntimeDecision。

    warm_run_secs 為空時（含 benchmark 失敗）採保守模式。
    """
    if not result.warm_run_secs:
        return LlmRuntimeDecision(
            mode="async_required_before_default_enable",
            rationale="No measurement available; conservatively assume async required.",
        )

    median = statistics.median(result.warm_run_secs)
    max_val = max(result.warm_run_secs)

    if median <= _SYNC_MEDIAN_THRESHOLD and max_val <= _SYNC_MAX_THRESHOLD:
        return LlmRuntimeDecision(
            mode="sync_blocking_allowed",
            rationale=(
                f"Warm median {median:.2f}s <= {_SYNC_MEDIAN_THRESHOLD}s "
                f"and max {max_val:.2f}s <= {_SYNC_MAX_THRESHOLD}s; "
                "sync blocking is acceptable for pre-exec path."
            ),
        )

    return LlmRuntimeDecision(
        mode="async_required_before_default_enable",
        rationale=(
            f"Warm median {median:.2f}s or max {max_val:.2f}s exceeds threshold "
            f"({_SYNC_MEDIAN_THRESHOLD}s / {_SYNC_MAX_THRESHOLD}s); "
            "async required before default enable."
        ),
    )
