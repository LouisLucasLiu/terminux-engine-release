# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime mechanical guardrail helpers。"""

from __future__ import annotations

from terminux_engine.package_runtime.result import PhaseResult


def result_has_side_effects(result: PhaseResult) -> bool:
    """檢查 PhaseResult 是否包含 side-effect request。"""
    return bool(result.records or result.notifications or result.reports)
