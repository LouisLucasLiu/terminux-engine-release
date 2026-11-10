# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer v0.1 DQE-specific extension models。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


class DqeSourceType:
    TEMPORARY_DB: Final[str] = "temporary_db"
    ARCHIVED_ISVN: Final[str] = "archived_isvn"

    ALLOWED: Final[tuple[str, ...]] = (
        TEMPORARY_DB,
        ARCHIVED_ISVN,
    )


class DqeLifecycleState:
    TEMPORARY: Final[str] = "temporary"
    ARCHIVED: Final[str] = "archived"
    EXPIRED: Final[str] = "expired"
    INCOMPLETE: Final[str] = "incomplete"
    UNKNOWN: Final[str] = "unknown"

    ALLOWED: Final[tuple[str, ...]] = (
        TEMPORARY,
        ARCHIVED,
        EXPIRED,
        INCOMPLETE,
        UNKNOWN,
    )


class DqeCheckinResult:
    NOT_RUN: Final[str] = "not_run"
    SUCCESS_NEW: Final[str] = "success_new"
    SUCCESS_UPDATE: Final[str] = "success_update"
    FAILED: Final[str] = "failed"
    UNKNOWN: Final[str] = "unknown"

    ALLOWED: Final[tuple[str, ...]] = (
        NOT_RUN,
        SUCCESS_NEW,
        SUCCESS_UPDATE,
        FAILED,
        UNKNOWN,
    )


def _validate_allowed(value: str, allowed: tuple[str, ...], field_name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{field_name} must be one of {allowed}, got {value!r}")


@dataclass
class DqeRetentionView:
    """Temporary DQE_DB record 的 retention policy view。"""

    retention_days: int
    policy_applied_at: str | None = None
    evidence_time: str | None = None
    expires_at: str | None = None
    is_expired: bool = False
    retention_note: str | None = None


@dataclass
class DqeDbSourceView:
    """DQE_DB source metadata；不執行查詢。"""

    db_path: str
    schema_version: str | None = None
    root_dqe_db_num: str | None = None
    query_time: str | None = None
    grouped_by: str = "job_key"

    def __post_init__(self) -> None:
        if self.grouped_by != "job_key":
            raise ValueError("DqeDbSourceView.grouped_by must be 'job_key' in v0.1")


@dataclass
class DqeIsvnSourceView:
    """Archived isvn source metadata；不執行 isvn command。"""

    workspace_root: str | None = None
    version_path: str | None = None
    version_dir: str | None = None
    json_path: str | None = None
    attachment_dir: str | None = None
    isvn_status_hint: str | None = None


@dataclass
class DqeCheckinView:
    """Checkin capability/result view；不執行 checkin。"""

    can_checkin: bool
    target_workspace: str | None = None
    target_version_path: str | None = None
    last_attempt_at: str | None = None
    last_result: str | None = DqeCheckinResult.NOT_RUN
    last_message: str | None = None

    def __post_init__(self) -> None:
        if self.last_result is not None:
            _validate_allowed(self.last_result, DqeCheckinResult.ALLOWED, "last_result")


@dataclass
class DqeGroupExtension:
    """DQE-specific group extension；generic EvidenceGroupView 不固定這些欄位。"""

    job_key: str
    dqe_source_type: str
    dqe_lifecycle_state: str
    retention: DqeRetentionView | None = None
    dqe_db: DqeDbSourceView | None = None
    isvn: DqeIsvnSourceView | None = None
    checkin: DqeCheckinView | None = None
    dqe_capabilities: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_allowed(self.dqe_source_type, DqeSourceType.ALLOWED, "dqe_source_type")
        _validate_allowed(
            self.dqe_lifecycle_state,
            DqeLifecycleState.ALLOWED,
            "dqe_lifecycle_state",
        )
        if (
            self.dqe_source_type == DqeSourceType.ARCHIVED_ISVN
            and self.checkin is not None
            and self.checkin.can_checkin
        ):
            raise ValueError("archived DQE groups must not expose active checkin")


@dataclass
class DqeItemExtension:
    """DQE item raw field extension；reference only，不載入 blob/content。"""

    dqe_id: str
    raw_value: str | None = None
    raw_status: str | None = None
    date_time: str | None = None
    path: str | None = None
    meta: dict[str, object] = field(default_factory=dict)
    attachment_ref: str | None = None
    executable_ref: str | None = None

