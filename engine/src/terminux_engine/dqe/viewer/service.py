# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer service facade for JSON-RPC handlers。"""

from __future__ import annotations

import base64
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from terminux_engine.dqe.checkin import CheckinRequest, checkin_job as run_checkin_job
from terminux_engine.dqe.purge import preview_purge as run_preview_purge
from terminux_engine.dqe.purge import purge_jobs as run_purge_jobs
from terminux_engine.dqe.viewer.adapters import (
    AdapterIssue,
    AdapterIssueCode,
    ArchivedIsvnAdapter,
    ArchivedIsvnQuery,
    TemporaryDbAdapter,
    TemporaryDbQuery,
)
from terminux_engine.dqe.viewer.adapters.refs import AdapterRefError, decode_ref
from terminux_engine.dqe.viewer.retention import (
    RetentionPolicyError,
    get_retention_policy,
    set_retention_policy,
    validate_retention_days,
)
from terminux_engine.evidence_viewer.models import to_dict

_DEFAULT_DQE_DB_PATH = Path.home() / ".terminux" / "dqe" / "dqe.sqlite"
MAX_ATTACHMENT_PREVIEW_BYTES = 5 * 1024 * 1024
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


class DqeViewerServiceError(RuntimeError):
    """DQE Viewer service 層受控錯誤。"""


class DqeViewerInvalidRefError(ValueError):
    """group_ref 格式錯誤。"""


def health() -> dict[str, Any]:
    """回傳 DQE Viewer read-only service capability。"""

    return {
        "service": "dqe_viewer",
        "api_version": "0.1",
        "status": "ok",
        "capabilities": {
            "archived_read": True,
            "temporary_read": True,
            "attachment_load": True,
            "checkin": True,
            "retention": True,
            "purge": True,
        },
    }


def list_archived_groups(version_dir: str) -> dict[str, Any]:
    """列出 archived isvn DQE groups。Empty directory 回傳 groups=[]。"""

    result = ArchivedIsvnAdapter().list_groups(
        ArchivedIsvnQuery(version_dir=version_dir)
    )
    if not result.ok:
        if _has_issue(result.issues, AdapterIssueCode.SOURCE_EMPTY):
            return {"groups": [], "issues": _issues_to_dicts(result.issues)}
        raise DqeViewerServiceError(_issues_message(result.issues))

    groups = [to_dict(group) for group in (result.value or [])]
    return {"groups": groups, "issues": _issues_to_dicts(result.issues)}


def list_temporary_groups(
    db_path: str | None = None,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """列出 temporary DQE_DB groups。Missing/empty DB 回傳 groups=[]。"""

    effective_db_path = db_path or str(_DEFAULT_DQE_DB_PATH)
    effective_retention_days = (
        validate_retention_days(retention_days)
        if retention_days is not None
        else get_retention_policy().retention_days
    )
    result = TemporaryDbAdapter().list_groups(
        TemporaryDbQuery(
            db_path=effective_db_path,
            retention_days=effective_retention_days,
        )
    )
    if not result.ok:
        if (_has_issue(result.issues, AdapterIssueCode.SOURCE_EMPTY)
                or _has_issue(result.issues, AdapterIssueCode.SOURCE_NOT_FOUND)
                or _has_issue(result.issues, AdapterIssueCode.SOURCE_UNAVAILABLE)):
            return {"groups": [], "issues": _issues_to_dicts(result.issues)}
        raise DqeViewerServiceError(_issues_message(result.issues))

    groups = [to_dict(group) for group in (result.value or [])]
    return {"groups": groups, "issues": _issues_to_dicts(result.issues)}


def load_group(group_ref: str) -> dict[str, Any]:
    """依 structured group_ref 載入單一 group。"""

    try:
        payload = decode_ref(group_ref)
    except AdapterRefError as exc:
        raise DqeViewerInvalidRefError(str(exc)) from exc

    adapter = payload.get("adapter")
    ref_type = payload.get("ref_type")
    if adapter == "archived_isvn" and ref_type == "archived_group":
        result = ArchivedIsvnAdapter().load_group(group_ref)
    elif adapter == "temporary_db" and ref_type == "temporary_group":
        result = TemporaryDbAdapter().load_group(group_ref)
    else:
        raise DqeViewerInvalidRefError("group_ref must reference an archived_group or temporary_group")

    if not result.ok or result.value is None:
        raise DqeViewerServiceError(_issues_message(result.issues))

    return {
        "group": to_dict(result.value),
        "issues": _issues_to_dicts(result.issues),
    }


def load_attachment_for_viewer(attachment_ref: str) -> dict[str, Any]:
    """載入單一 attachment，回傳 GUI-facing JSON-safe result。"""

    try:
        payload = decode_ref(attachment_ref)
    except AdapterRefError as exc:
        raise DqeViewerInvalidRefError(str(exc)) from exc

    adapter = payload.get("adapter")
    ref_type = payload.get("ref_type")
    executable = _ref_is_executable(payload)
    if adapter == "archived_isvn" and ref_type == "archived_attachment":
        result = ArchivedIsvnAdapter().load_attachment(
            attachment_ref,
            max_bytes=MAX_ATTACHMENT_PREVIEW_BYTES,
        )
    elif adapter == "temporary_db" and ref_type == "temporary_attachment":
        result = TemporaryDbAdapter().load_attachment(
            attachment_ref,
            max_bytes=MAX_ATTACHMENT_PREVIEW_BYTES,
        )
    else:
        raise DqeViewerInvalidRefError(
            "attachment_ref must reference an archived_attachment or temporary_attachment"
        )

    if not result.ok or result.value is None:
        return _attachment_failure_result(
            attachment_ref=attachment_ref,
            issues=result.issues,
            executable=executable,
        )

    payload_value = result.value
    return _attachment_loaded_result(
        attachment_ref=attachment_ref,
        filename=payload_value.filename,
        mime_type=payload_value.mime_type,
        size_bytes=payload_value.size_bytes,
        content=payload_value.content,
        executable=executable,
        kind=_attachment_kind(payload),
    )


def get_retention() -> dict[str, Any]:
    """取得 persistent temporary retention policy。"""

    return get_retention_policy().to_dict()


def set_retention(retention_days: int, updated_by: str | None = None) -> dict[str, Any]:
    """設定 persistent temporary retention policy；不刪除任何 DQE_DB records。"""

    try:
        previous = get_retention_policy()
        policy = set_retention_policy(retention_days, updated_by=updated_by)
    except RetentionPolicyError:
        raise
    result = policy.to_dict()
    result.update(
        {
            "changed": previous.retention_days != policy.retention_days,
            "records_deleted": 0,
            "message": "Retention policy updated. No DQE_DB records were deleted.",
        }
    )
    return result


def checkin_job(params: dict[str, Any]) -> dict[str, Any]:
    """執行單一 temporary DQE job checkin，domain failure 也回傳結構化 result。"""

    request = CheckinRequest(
        job_key=str(params["job_key"]),
        workspace_root=str(params.get("workspace_root") or ""),
        target_version_path=str(params["target_version_path"]),
        db_path=params.get("db_path") if params.get("db_path") else None,
        dry_run=bool(params.get("dry_run", False)),
    )
    return run_checkin_job(request).to_dict()


def preview_purge(params: dict[str, Any]) -> dict[str, Any]:
    """建立 manual purge preview 與 confirmation token。"""

    return run_preview_purge(
        mode=str(params["mode"]),
        db_path=Path(str(params["db_path"])) if params.get("db_path") else None,
        job_keys=[str(item) for item in params.get("job_keys", [])],
        vacuum_requested=bool(params.get("vacuum_requested", False)),
    )


def purge_jobs(params: dict[str, Any]) -> dict[str, Any]:
    """執行已確認 token 的 manual purge。"""

    return run_purge_jobs(
        confirmation_token=str(params["confirmation_token"]),
        mode=str(params["mode"]),
    )


def _attachment_loaded_result(
    *,
    attachment_ref: str,
    filename: str,
    mime_type: str | None,
    size_bytes: int,
    content: bytes,
    executable: bool,
    kind: str,
) -> dict[str, Any]:
    classification = _classify_preview(content, filename=filename, mime_type=mime_type)
    base: dict[str, Any] = {
        "attachment_ref": attachment_ref,
        "filename": filename,
        "attachment_role": kind,
        "kind": kind,
        "mime_type": classification["mime_type"],
        "mime_type_source": classification["mime_type_source"],
        "size_bytes": size_bytes,
        "preview_kind": classification["preview_kind"],
        "preview_decision_source": classification["preview_decision_source"],
        "preview_confidence": classification["preview_confidence"],
        "executable": executable,
        "executable_allowed_to_run": False,
    }
    if classification["preview_kind"] in {"text", "image"}:
        base.update(
            {
                "load_status": "loaded",
                "semantic_state": "ready",
                "severity": "neutral",
                "encoding": "base64",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "message": "",
            }
        )
        return base

    base.update(
        {
            "load_status": "unsupported",
            "semantic_state": "warning",
            "severity": "warning",
            "failure_code": "unsupported_preview",
            "encoding": None,
            "content_base64": None,
            "message": "Attachment content type is unknown or unsupported for safe preview.",
        }
    )
    return base


def _attachment_failure_result(
    *,
    attachment_ref: str,
    issues: list[AdapterIssue],
    executable: bool,
) -> dict[str, Any]:
    primary = issues[0] if issues else None
    code = primary.code if primary is not None else AdapterIssueCode.UNKNOWN_ERROR
    detail = primary.detail if primary is not None else {}
    warning_codes = {
        AdapterIssueCode.ATTACHMENT_TOO_LARGE,
        AdapterIssueCode.UNSUPPORTED_ATTACHMENT,
    }
    load_status = "refused" if code == AdapterIssueCode.ATTACHMENT_TOO_LARGE else "failed"
    semantic_state = "warning" if code in warning_codes else "error"
    severity = "warning" if code in warning_codes else "error"
    return {
        "attachment_ref": attachment_ref,
        "load_status": load_status,
        "semantic_state": semantic_state,
        "severity": severity,
        "failure_code": code,
        "filename": detail.get("filename"),
        "size_bytes": detail.get("size_bytes"),
        "max_bytes": detail.get("max_bytes", MAX_ATTACHMENT_PREVIEW_BYTES),
        "encoding": None,
        "content_base64": None,
        "preview_kind": "metadata_only" if severity == "warning" else "none",
        "executable": executable,
        "executable_allowed_to_run": False,
        "message": primary.message if primary is not None else "Attachment load failed.",
        "issues": _issues_to_dicts(issues),
    }


def _classify_preview(
    content: bytes,
    *,
    filename: str,
    mime_type: str | None,
) -> dict[str, str | None]:
    image_mime = _sniff_image_mime(content)
    if image_mime is not None and (
        mime_type is None
        or mime_type.startswith("image/")
        or _filename_has_image_suffix(filename)
    ):
        return {
            "mime_type": image_mime,
            "mime_type_source": "content_sniff",
            "preview_kind": "image",
            "preview_decision_source": "image_signature",
            "preview_confidence": "medium",
        }

    if _looks_like_safe_text(content):
        return {
            "mime_type": mime_type if mime_type and mime_type.startswith("text/") else "text/plain",
            "mime_type_source": "content_sniff",
            "preview_kind": "text",
            "preview_decision_source": "content_sniff",
            "preview_confidence": "high",
        }

    return {
        "mime_type": mime_type,
        "mime_type_source": "extension_hint" if mime_type else "unknown",
        "preview_kind": "metadata_only",
        "preview_decision_source": "unsupported_fallback",
        "preview_confidence": "low",
    }


def _sniff_image_mime(content: bytes) -> str | None:
    for magic, mime_type in _IMAGE_MAGIC:
        if content.startswith(magic):
            return mime_type
    return None


def _looks_like_safe_text(content: bytes) -> bool:
    sample = content[:8192]
    if b"\x00" in sample:
        return False
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = sample.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not text:
        return True
    control_count = sum(
        1
        for ch in text
        if ord(ch) < 32 and ch not in "\t\r\n\f\b"
    )
    return (control_count / max(len(text), 1)) <= 0.02


def _filename_has_image_suffix(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def _ref_is_executable(payload: dict[str, Any]) -> bool:
    return (
        str(payload.get("kind", "")).lower() == "executable"
        or str(payload.get("blob_field", "")).upper() == "EXECUTABLE"
    )


def _attachment_kind(payload: dict[str, Any]) -> str:
    if payload.get("kind"):
        return str(payload["kind"])
    return "executable" if str(payload.get("blob_field", "")).upper() == "EXECUTABLE" else "attachment"


def _has_issue(issues: list[AdapterIssue], code: str) -> bool:
    return any(item.code == code for item in issues)


def _issues_message(issues: list[AdapterIssue]) -> str:
    if not issues:
        return "DQE Viewer service failed"
    return "; ".join(f"{item.code}: {item.message}" for item in issues)


def _issues_to_dicts(issues: list[AdapterIssue]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in issues:
        if is_dataclass(item):
            out.append(asdict(item))
        else:
            out.append(dict(item))
    return out
