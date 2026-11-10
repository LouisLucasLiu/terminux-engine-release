# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Attachment metadata view model。"""

from __future__ import annotations

from dataclasses import dataclass, field

from terminux_engine.evidence_viewer.models.constants import (
    AttachmentLocationKind,
    AttachmentRole,
    AttachmentState,
    validate_allowed,
)


@dataclass
class AttachmentLocationView:
    """Attachment 的位置描述；不保存 blob 或檔案內容。"""

    location_kind: str
    uri: str | None = None
    relative_path: str | None = None
    db_ref: str | None = None
    display_path: str | None = None

    def __post_init__(self) -> None:
        validate_allowed(
            self.location_kind,
            AttachmentLocationKind.ALLOWED,
            "location_kind",
        )


@dataclass
class AttachmentView:
    """Attachment metadata；模型層不載入、不執行 attachment。"""

    attachment_id: str
    label: str | None
    attachment_role: str
    state: str
    location: AttachmentLocationView
    mime_hint: str | None = None
    size_hint: int | None = None
    preview_capability: str | None = None
    issue: str | None = None

    def __post_init__(self) -> None:
        validate_allowed(self.attachment_role, AttachmentRole.ALLOWED, "attachment_role")
        validate_allowed(self.state, AttachmentState.ALLOWED, "state")


@dataclass
class AttachmentSummaryView:
    """Evidence group 的 attachment 統計。"""

    attachment_count: int
    missing_count: int
    items: list[AttachmentView] = field(default_factory=list)


def build_attachment_summary(items: list[AttachmentView]) -> AttachmentSummaryView:
    """從 attachment list 推導數量與 missing 數。"""

    missing_count = sum(1 for item in items if item.state == AttachmentState.MISSING)
    return AttachmentSummaryView(
        attachment_count=len(items),
        missing_count=missing_count,
        items=list(items),
    )

