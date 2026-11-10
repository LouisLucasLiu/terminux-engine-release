# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Domain-neutral Evidence Review view models。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.evidence_viewer.models.attachment import (
    AttachmentSummaryView,
    AttachmentView,
)
from terminux_engine.evidence_viewer.models.constants import (
    IntegrityState,
    validate_allowed,
)
from terminux_engine.evidence_viewer.models.context import EvidenceContextView
from terminux_engine.evidence_viewer.models.summary import (
    EvidenceSummaryView,
    normalize_status,
)


@dataclass
class EvidenceSourceView:
    """Evidence group 來源描述；不包含讀取邏輯。"""

    adapter_id: str
    source_kind: str
    source_uri: str
    source_display_name: str | None
    loaded_at: str
    read_only: bool
    raw_source_hint: dict[str, Any] | None = None


@dataclass
class EvidenceIdentityView:
    """Evidence group 的 generic identity。"""

    group_id: str
    display_name: str
    group_label: str | None = None
    primary_time: str | None = None
    sort_key: str | int | float | None = None


@dataclass
class EvidenceIntegrityView:
    """Evidence group 載入與完整性狀態。"""

    integrity_state: str
    issue_count: int = 0
    issues: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_allowed(self.integrity_state, IntegrityState.ALLOWED, "integrity_state")


@dataclass
class EvidenceCapabilityView:
    """Generic action affordance 描述；不執行 action。"""

    can_open_detail: bool
    can_preview_attachment: bool
    can_copy_path: bool
    can_export: bool
    domain_actions: list[str] = field(default_factory=list)


@dataclass
class EvidenceItemView:
    """Evidence group 內的一筆 evidence item。"""

    item_id: str
    title: str | None
    primary_value: str | None
    unit: str | None
    status: str
    status_raw: str | None = None
    description: str | None = None
    reference: str | None = None
    source_path: str | None = None
    updated_at: str | None = None
    attributes: dict[str, Any] | None = None
    attachments: list[AttachmentView] = field(default_factory=list)
    domain_extension: object | None = None

    def __post_init__(self) -> None:
        self.status = normalize_status(self.status)


@dataclass
class EvidenceGroupView:
    """一組可 review 的 evidence；generic 層不固定任何 DQE lifecycle 欄位。"""

    evidence_type: str
    source: EvidenceSourceView
    identity: EvidenceIdentityView
    context: EvidenceContextView
    summary: EvidenceSummaryView
    items: list[EvidenceItemView]
    attachment_summary: AttachmentSummaryView
    integrity: EvidenceIntegrityView
    capabilities: EvidenceCapabilityView
    domain_extension: object


def validate_evidence_group(group: EvidenceGroupView) -> list[str]:
    """執行輕量 validation，回傳問題字串；不做 DB / 檔案 / domain policy 檢查。"""

    issues: list[str] = []
    forbidden_generic_fields = (
        "job_key",
        "retention_days",
        "purge",
        "temporary",
        "expired",
        "checkin_to_isvn",
        "dqe_lifecycle_state",
    )

    for field_name in forbidden_generic_fields:
        if hasattr(group, field_name):
            issues.append(f"EvidenceGroupView must not expose generic field: {field_name}")

    if group.summary.total_count != len(group.items):
        issues.append("summary.total_count does not match items length")

    return issues

