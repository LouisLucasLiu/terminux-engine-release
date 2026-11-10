# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Evidence Viewer v0.1 enum-like string constants 與輕量驗證。"""

from __future__ import annotations

from typing import Final


class EvidenceStatus:
    PASS: Final[str] = "pass"
    WARN: Final[str] = "warn"
    FAIL: Final[str] = "fail"
    UNKNOWN: Final[str] = "unknown"
    NOT_APPLICABLE: Final[str] = "not_applicable"

    ALLOWED: Final[tuple[str, ...]] = (
        PASS,
        WARN,
        FAIL,
        UNKNOWN,
        NOT_APPLICABLE,
    )


class IntegrityState:
    COMPLETE: Final[str] = "complete"
    INCOMPLETE: Final[str] = "incomplete"
    INVALID: Final[str] = "invalid"
    UNAVAILABLE: Final[str] = "unavailable"
    ERROR: Final[str] = "error"

    ALLOWED: Final[tuple[str, ...]] = (
        COMPLETE,
        INCOMPLETE,
        INVALID,
        UNAVAILABLE,
        ERROR,
    )


class AttachmentState:
    AVAILABLE: Final[str] = "available"
    DEFERRED: Final[str] = "deferred"
    MISSING: Final[str] = "missing"
    UNSUPPORTED: Final[str] = "unsupported"
    ERROR: Final[str] = "error"

    ALLOWED: Final[tuple[str, ...]] = (
        AVAILABLE,
        DEFERRED,
        MISSING,
        UNSUPPORTED,
        ERROR,
    )


class AttachmentRole:
    EVIDENCE_FILE: Final[str] = "evidence_file"
    IMAGE: Final[str] = "image"
    SCRIPT: Final[str] = "script"
    EXECUTABLE: Final[str] = "executable"
    REPORT_FRAGMENT: Final[str] = "report_fragment"
    DOCUMENT: Final[str] = "document"
    UNKNOWN: Final[str] = "unknown"

    ALLOWED: Final[tuple[str, ...]] = (
        EVIDENCE_FILE,
        IMAGE,
        SCRIPT,
        EXECUTABLE,
        REPORT_FRAGMENT,
        DOCUMENT,
        UNKNOWN,
    )


class AttachmentLocationKind:
    LOCAL_PATH: Final[str] = "local_path"
    RELATIVE_PATH: Final[str] = "relative_path"
    DB_BLOB_REF: Final[str] = "db_blob_ref"
    INLINE_REF: Final[str] = "inline_ref"
    EXTERNAL_URI: Final[str] = "external_uri"
    UNKNOWN: Final[str] = "unknown"

    ALLOWED: Final[tuple[str, ...]] = (
        LOCAL_PATH,
        RELATIVE_PATH,
        DB_BLOB_REF,
        INLINE_REF,
        EXTERNAL_URI,
        UNKNOWN,
    )


def validate_allowed(value: str, allowed: tuple[str, ...], field_name: str) -> None:
    """驗證 enum-like string 是否在允許集合內。"""

    if value not in allowed:
        raise ValueError(f"{field_name} must be one of {allowed}, got {value!r}")

