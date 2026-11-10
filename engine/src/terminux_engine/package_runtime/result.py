# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime v1 data model。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PhaseName = Literal["before", "main", "after", "action"]
PhaseStatus = Literal["noop", "ok", "warn", "fail"]
NotificationLevel = Literal["info", "warning", "error"]


@dataclass
class TimelineEvent:
    """Core 提供給 package 的 structural event context。"""

    event_id: str
    trace_id: str | None

    command: str
    argv: list[str]
    cwd: str
    tool_name: str | None

    user: str
    host: str
    terminal_id: str | None
    source: str
    timestamp: str

    exit_code: int | None = None
    stdout_tail: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordRequest:
    """Package 要求 Core 持久化的一筆 opaque record。"""

    namespace: str
    record_type: str
    key: str
    payload: dict[str, Any]
    display_message: str | None = None
    timestamp: str | None = None

    # 預留給 stateful package。多數 package 不填。
    process_key: str | None = None
    index_tags: dict[str, str] = field(default_factory=dict)


@dataclass
class NotificationRequest:
    """Package 明確指定的 notification side-effect request。"""

    level: NotificationLevel
    title: str
    message: str
    actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReportRequest:
    """Package 要求 Core render/register report。

    Deprecated：`ReportService`
    從未持久化這個型別攜帶的資料，只做 process 記憶體登記。新 package
    請改用 `RecordRequest.payload` 保存 artifact 的 durable metadata/
    路徑，並自行負責產生 artifact 檔案本身。
    """

    report_id: str
    title: str
    report_path: str | None = None
    template: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    output_name: str | None = None


@dataclass
class PhaseResult:
    """Package phase 執行結果。"""

    status: PhaseStatus
    message: str | None = None

    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    records: list[RecordRequest] = field(default_factory=list)
    notifications: list[NotificationRequest] = field(default_factory=list)
    reports: list[ReportRequest] = field(default_factory=list)
    """Deprecated（Stage 1）：見 ReportRequest docstring。"""
