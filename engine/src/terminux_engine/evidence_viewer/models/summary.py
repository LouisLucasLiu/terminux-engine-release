# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Evidence status normalization 與 summary derivation。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from terminux_engine.evidence_viewer.models.constants import EvidenceStatus

if TYPE_CHECKING:
    from terminux_engine.evidence_viewer.models.evidence import EvidenceItemView

_PASS_VALUES = {"pass", "passed", "ok"}
_WARN_VALUES = {"warn", "warning"}
_FAIL_VALUES = {"fail", "failed", "fatal", "error"}
_NA_VALUES = {"n/a", "na", "notapplicable", "not applicable"}
_WORST_PRIORITY = (
    EvidenceStatus.FAIL,
    EvidenceStatus.WARN,
    EvidenceStatus.UNKNOWN,
    EvidenceStatus.NOT_APPLICABLE,
    EvidenceStatus.PASS,
)


@dataclass
class EvidenceSummaryView:
    """Evidence group 的 status 統計。"""

    total_count: int
    pass_count: int
    warn_count: int
    fail_count: int
    unknown_count: int
    not_applicable_count: int
    worst_status: str
    status_breakdown: dict[str, int] = field(default_factory=dict)


def normalize_status(raw_status: object) -> str:
    """把來源 status 轉成 EvidenceStatus 的穩定字串。"""

    if raw_status is None:
        return EvidenceStatus.UNKNOWN

    text = str(raw_status).strip()
    if text == "":
        return EvidenceStatus.UNKNOWN

    lowered = text.lower()
    compact = lowered.replace("_", "").replace("-", "").strip()

    if lowered in _PASS_VALUES:
        return EvidenceStatus.PASS
    if lowered in _WARN_VALUES:
        return EvidenceStatus.WARN
    if lowered in _FAIL_VALUES:
        return EvidenceStatus.FAIL
    if lowered in _NA_VALUES or compact in {"notapplicable"}:
        return EvidenceStatus.NOT_APPLICABLE
    if lowered == EvidenceStatus.UNKNOWN:
        return EvidenceStatus.UNKNOWN

    return EvidenceStatus.UNKNOWN


def build_summary(items: list["EvidenceItemView"]) -> EvidenceSummaryView:
    """從 item list 推導 summary。"""

    counts = {status: 0 for status in EvidenceStatus.ALLOWED}
    for item in items:
        counts[normalize_status(item.status)] += 1

    if not items:
        worst_status = EvidenceStatus.UNKNOWN
    else:
        worst_status = next(
            status for status in _WORST_PRIORITY if counts[status] > 0
        )

    return EvidenceSummaryView(
        total_count=len(items),
        pass_count=counts[EvidenceStatus.PASS],
        warn_count=counts[EvidenceStatus.WARN],
        fail_count=counts[EvidenceStatus.FAIL],
        unknown_count=counts[EvidenceStatus.UNKNOWN],
        not_applicable_count=counts[EvidenceStatus.NOT_APPLICABLE],
        worst_status=worst_status,
        status_breakdown=dict(counts),
    )

