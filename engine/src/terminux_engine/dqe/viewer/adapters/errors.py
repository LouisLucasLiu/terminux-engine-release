# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer adapter structured issue types。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final


class AdapterIssueSeverity:
    INFO: Final[str] = "info"
    WARNING: Final[str] = "warning"
    ERROR: Final[str] = "error"

    ALLOWED: Final[tuple[str, ...]] = (INFO, WARNING, ERROR)


class AdapterIssueCode:
    SOURCE_NOT_FOUND: Final[str] = "source_not_found"
    SOURCE_UNAVAILABLE: Final[str] = "source_unavailable"
    SOURCE_EMPTY: Final[str] = "source_empty"
    INVALID_JSON: Final[str] = "invalid_json"
    SCHEMA_MISMATCH: Final[str] = "schema_mismatch"
    QUERY_FAILED: Final[str] = "query_failed"
    MISSING_REQUIRED_FIELD: Final[str] = "missing_required_field"
    MALFORMED_RECORD: Final[str] = "malformed_record"
    INCONSISTENT_GROUP_CONTEXT: Final[str] = "inconsistent_group_context"
    MISSING_ATTACHMENT: Final[str] = "missing_attachment"
    UNSAFE_ATTACHMENT_PATH: Final[str] = "unsafe_attachment_path"
    ATTACHMENT_NOT_FOUND: Final[str] = "attachment_not_found"
    ATTACHMENT_LOAD_FAILED: Final[str] = "attachment_load_failed"
    ATTACHMENT_TOO_LARGE: Final[str] = "attachment_too_large"
    BLOB_NULL: Final[str] = "blob_null"
    UNSUPPORTED_ATTACHMENT: Final[str] = "unsupported_attachment"
    UNKNOWN_ERROR: Final[str] = "unknown_error"
    INVALID_REF: Final[str] = "invalid_ref"


@dataclass
class AdapterIssue:
    """Adapter 可回傳給上層的結構化 issue。"""

    severity: str
    code: str
    message: str
    source_ref: str | None = None
    item_ref: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def issue(
    severity: str,
    code: str,
    message: str,
    *,
    source_ref: str | None = None,
    item_ref: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AdapterIssue:
    """建立 AdapterIssue；保持呼叫端簡潔。"""

    return AdapterIssue(
        severity=severity,
        code=code,
        message=message,
        source_ref=source_ref,
        item_ref=item_ref,
        detail=detail or {},
    )
