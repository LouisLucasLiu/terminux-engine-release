# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Shared dispatch engine 的結構化結果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class PackageOutcome:
    """單一 package 在此次 event 的執行結果。"""

    package_id: str
    package_version: str
    status: Literal["ok", "warn", "fail", "noop", "error"]
    message: str | None
    started_at: float
    finished_at: float
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    records_committed: int = 0
    notifications_emitted: int = 0
    reports_rendered: int = 0
    error: str | None = None


@dataclass(frozen=True)
class DispatchOutcome:
    """一次 event dispatch 的結構化結果。"""

    event_id: str
    tool_name: str | None
    source: str
    started_at: float
    finished_at: float
    packages: list[PackageOutcome]
    skipped_no_after: list[str]
