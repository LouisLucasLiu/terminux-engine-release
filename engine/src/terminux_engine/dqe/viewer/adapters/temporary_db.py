# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Temporary DQE_DB read-only adapter。"""

from __future__ import annotations

import mimetypes
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from terminux_engine.dqe.viewer.adapters.attachment_payload import AttachmentPayload
from terminux_engine.dqe.viewer.adapters.base import SourceAdapter
from terminux_engine.dqe.viewer.adapters.errors import (
    AdapterIssue,
    AdapterIssueCode,
    AdapterIssueSeverity,
    issue,
)
from terminux_engine.dqe.viewer.adapters.refs import (
    AdapterRefError,
    decode_ref,
    make_temporary_attachment_ref,
    make_temporary_group_ref,
)
from terminux_engine.dqe.viewer.adapters.requests import TemporaryDbQuery
from terminux_engine.dqe.viewer.adapters.responses import AdapterResult
from terminux_engine.dqe.viewer.models import (
    DqeCheckinView,
    DqeDbSourceView,
    DqeGroupExtension,
    DqeItemExtension,
    DqeRetentionView,
)
from terminux_engine.evidence_viewer.models import (
    AttachmentLocationView,
    AttachmentView,
    EvidenceCapabilityView,
    EvidenceContextView,
    EvidenceGroupView,
    EvidenceIdentityView,
    EvidenceIntegrityView,
    EvidenceItemView,
    EvidenceSourceView,
    build_attachment_summary,
    build_summary,
    normalize_context_field,
)

_VERSION_COLUMNS = {
    "id",
    "ProjectCode",
    "DqeDbNum",
    "Phase",
    "Block",
    "Stage",
    "Flow",
    "Version",
    "DQE_ID",
    "VALUE",
    "UNIT",
    "STATUS",
    "DATE_TIME",
    "Reporter",
    "PATH",
    "ATTACHMENT",
    "EXECUTABLE",
    "job_key",
    "eco_run_id",
    "eco_iteration",
}
_META_COLUMNS = {"DQE_ID", "TITLE", "DESCRIPTION", "REFERENCE"}
_BLOB_FIELDS = {"ATTACHMENT", "EXECUTABLE"}
_METADATA_SQL = """
SELECT
    v.id,
    v.ProjectCode,
    v.DqeDbNum,
    v.Phase,
    v.Block,
    v.Stage,
    v.Flow,
    v.Version,
    v.DQE_ID,
    v.VALUE,
    v.UNIT,
    v.STATUS,
    v.DATE_TIME,
    v.Reporter,
    v.PATH,
    CASE WHEN v.ATTACHMENT IS NOT NULL THEN 1 ELSE 0 END AS has_attachment,
    CASE WHEN v.EXECUTABLE IS NOT NULL THEN 1 ELSE 0 END AS has_executable,
    v.job_key,
    v.eco_run_id,
    v.eco_iteration,
    m.TITLE,
    m.DESCRIPTION,
    m.REFERENCE
FROM Version_Dqe_Tab v
LEFT JOIN Dqe_Meta_Tab m ON v.DQE_ID = m.DQE_ID
"""


class TemporaryDbAdapter(SourceAdapter):
    """以 read-only SQL 讀取 DQE_DB temporary jobs。"""

    def list_groups(
        self,
        query: TemporaryDbQuery,
    ) -> AdapterResult[list[EvidenceGroupView]]:
        db_path = Path(query.db_path)
        if not db_path.exists():
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.SOURCE_NOT_FOUND,
                        "DQE_DB file does not exist",
                        source_ref=str(db_path),
                    )
                ],
            )

        conn_result = _connect_read_only(db_path)
        if isinstance(conn_result, AdapterResult):
            return conn_result
        conn = conn_result
        try:
            schema_issues = _verify_schema(conn)
            if schema_issues:
                return AdapterResult(ok=False, issues=schema_issues)
            rows = _fetch_metadata_rows(conn, query)
        except sqlite3.Error as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.QUERY_FAILED,
                        "DQE_DB metadata query failed",
                        source_ref=str(db_path),
                        detail={"error": str(exc)},
                    )
                ],
            )
        finally:
            conn.close()

        if not rows:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.SOURCE_EMPTY,
                        "DQE_DB contains no matching DQE rows",
                        source_ref=str(db_path),
                    )
                ],
            )

        groups, issues = _build_groups_from_rows(
            rows=rows,
            db_path=str(db_path),
            retention_days=query.retention_days,
        )
        return AdapterResult(ok=True, value=groups, issues=issues)

    def load_group(self, group_ref: str) -> AdapterResult[EvidenceGroupView]:
        try:
            payload = decode_ref(group_ref)
            if payload["ref_type"] != "temporary_group":
                raise AdapterRefError("ref_type must be temporary_group")
        except AdapterRefError as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.INVALID_REF,
                        str(exc),
                        source_ref=group_ref,
                    )
                ],
            )

        result = self.list_groups(
            TemporaryDbQuery(
                db_path=str(payload["db_path"]),
                filters={"job_key": str(payload["job_key"])},
            )
        )
        if not result.ok or result.value is None:
            return AdapterResult(ok=False, issues=result.issues)
        if not result.value:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.SOURCE_EMPTY,
                        "temporary group not found",
                        source_ref=group_ref,
                    )
                ],
            )
        return AdapterResult(ok=True, value=result.value[0], issues=result.issues)

    def load_attachment(
        self,
        attachment_ref: str,
        max_bytes: int | None = None,
    ) -> AdapterResult[AttachmentPayload]:
        try:
            payload = decode_ref(attachment_ref)
            if payload["ref_type"] != "temporary_attachment":
                raise AdapterRefError("ref_type must be temporary_attachment")
            blob_field = str(payload["blob_field"])
            if blob_field not in _BLOB_FIELDS:
                raise AdapterRefError("blob_field must be ATTACHMENT or EXECUTABLE")
            db_path = Path(str(payload["db_path"]))
            record_id = int(payload["record_id"])
            job_key = str(payload["job_key"])
            dqe_id = str(payload["dqe_id"])
        except (AdapterRefError, KeyError, TypeError, ValueError) as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.INVALID_REF,
                        str(exc),
                        source_ref=attachment_ref,
                    )
                ],
            )

        if not db_path.exists():
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.SOURCE_NOT_FOUND,
                        "DQE_DB file does not exist",
                        source_ref=str(db_path),
                    )
                ],
            )

        conn_result = _connect_read_only(db_path)
        if isinstance(conn_result, AdapterResult):
            return conn_result
        conn = conn_result
        try:
            size_row = conn.execute(
                f"""
                SELECT length({blob_field})
                FROM Version_Dqe_Tab
                WHERE id = ? AND job_key = ? AND DQE_ID = ?
                """,
                (record_id, job_key, dqe_id),
            ).fetchone()
            if size_row is None:
                return AdapterResult(
                    ok=False,
                    issues=[
                        issue(
                            AdapterIssueSeverity.ERROR,
                            AdapterIssueCode.ATTACHMENT_NOT_FOUND,
                            "DQE_DB attachment row was not found",
                            source_ref=attachment_ref,
                        )
                    ],
                )
            if size_row[0] is None:
                return AdapterResult(
                    ok=False,
                    issues=[
                        issue(
                            AdapterIssueSeverity.ERROR,
                            AdapterIssueCode.BLOB_NULL,
                            "DQE_DB attachment blob is null",
                            source_ref=attachment_ref,
                        )
                    ],
                )
            size_bytes = int(size_row[0])
            if max_bytes is not None and size_bytes > max_bytes:
                return AdapterResult(
                    ok=False,
                    issues=[
                        issue(
                            AdapterIssueSeverity.ERROR,
                            AdapterIssueCode.ATTACHMENT_TOO_LARGE,
                            "DQE_DB attachment blob exceeds preview size limit",
                            source_ref=attachment_ref,
                            detail={
                                "size_bytes": size_bytes,
                                "max_bytes": max_bytes,
                                "blob_field": blob_field,
                            },
                        )
                    ],
                )
            row = conn.execute(
                f"""
                SELECT {blob_field}
                FROM Version_Dqe_Tab
                WHERE id = ? AND job_key = ? AND DQE_ID = ?
                """,
                (record_id, job_key, dqe_id),
            ).fetchone()
        except sqlite3.Error as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.ATTACHMENT_LOAD_FAILED,
                        "DQE_DB attachment query failed",
                        source_ref=attachment_ref,
                        detail={"error": str(exc)},
                    )
                ],
            )
        finally:
            conn.close()

        if row is None:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.ATTACHMENT_NOT_FOUND,
                        "DQE_DB attachment blob was not found",
                        source_ref=attachment_ref,
                    )
                ],
            )
        if row[0] is None:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.BLOB_NULL,
                        "DQE_DB attachment blob is null",
                        source_ref=attachment_ref,
                    )
                ],
            )

        content = bytes(row[0])
        extension = ".bin" if blob_field == "ATTACHMENT" else ".sh"
        filename = f"{dqe_id}_{blob_field.lower()}{extension}"
        return AdapterResult(
            ok=True,
            value=AttachmentPayload(
                attachment_ref=attachment_ref,
                filename=filename,
                mime_type=mimetypes.guess_type(filename)[0],
                size_bytes=len(content),
                content=content,
            ),
        )


def _connect_read_only(db_path: Path) -> sqlite3.Connection | AdapterResult[Any]:
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        # Viewer read path 必須嚴格使用 mode=ro；不可 fallback 到可能建立 DB 的一般連線。
        return AdapterResult(
            ok=False,
            issues=[
                issue(
                    AdapterIssueSeverity.ERROR,
                    AdapterIssueCode.SOURCE_UNAVAILABLE,
                    "cannot open DQE_DB read-only",
                    source_ref=str(db_path),
                    detail={"error": str(exc)},
                )
            ],
        )
    conn.row_factory = sqlite3.Row
    return conn


def _verify_schema(conn: sqlite3.Connection) -> list[AdapterIssue]:
    issues: list[AdapterIssue] = []
    version_columns = _table_columns(conn, "Version_Dqe_Tab")
    meta_columns = _table_columns(conn, "Dqe_Meta_Tab")
    if version_columns is None:
        issues.append(
            issue(
                AdapterIssueSeverity.ERROR,
                AdapterIssueCode.SCHEMA_MISMATCH,
                "Version_Dqe_Tab is missing",
            )
        )
    elif missing := sorted(_VERSION_COLUMNS - version_columns):
        issues.append(
            issue(
                AdapterIssueSeverity.ERROR,
                AdapterIssueCode.SCHEMA_MISMATCH,
                "Version_Dqe_Tab is missing required columns",
                detail={"missing": missing},
            )
        )
    if meta_columns is None:
        issues.append(
            issue(
                AdapterIssueSeverity.ERROR,
                AdapterIssueCode.SCHEMA_MISMATCH,
                "Dqe_Meta_Tab is missing",
            )
        )
    elif missing := sorted(_META_COLUMNS - meta_columns):
        issues.append(
            issue(
                AdapterIssueSeverity.ERROR,
                AdapterIssueCode.SCHEMA_MISMATCH,
                "Dqe_Meta_Tab is missing required columns",
                detail={"missing": missing},
            )
        )
    return issues


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str] | None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not rows:
        return None
    return {str(row["name"]) for row in rows}


def _fetch_metadata_rows(
    conn: sqlite3.Connection,
    query: TemporaryDbQuery,
) -> list[sqlite3.Row]:
    sql = _METADATA_SQL
    params: list[Any] = []
    where: list[str] = []
    filters = query.filters or {}
    supported_filters = {
        "job_key": "v.job_key",
        "project_code": "v.ProjectCode",
        "phase": "v.Phase",
        "block": "v.Block",
        "stage": "v.Stage",
        "flow": "v.Flow",
        "version": "v.Version",
        "eco_run_id": "v.eco_run_id",
        "eco_iteration": "v.eco_iteration",
    }
    for key, value in filters.items():
        if key not in supported_filters:
            continue
        where.append(f"{supported_filters[key]} = ?")
        params.append(value)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY v.job_key ASC, v.DQE_ID ASC, v.id ASC"
    if query.limit is not None:
        sql += " LIMIT ?"
        params.append(query.limit)
    if query.offset is not None:
        sql += " OFFSET ?"
        params.append(query.offset)
    return list(conn.execute(sql, params).fetchall())


def _build_groups_from_rows(
    *,
    rows: list[sqlite3.Row],
    db_path: str,
    retention_days: int | None,
) -> tuple[list[EvidenceGroupView], list[AdapterIssue]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["job_key"])].append(row)

    groups: list[EvidenceGroupView] = []
    issues: list[AdapterIssue] = []
    for job_key, group_rows in grouped.items():
        group_issues = _group_context_issues(job_key, group_rows)
        issues.extend(group_issues)
        groups.append(
            _build_group(
                rows=group_rows,
                db_path=db_path,
                job_key=job_key,
                retention_days=retention_days,
                group_issues=group_issues,
            )
        )
    return groups, issues


def _build_group(
    *,
    rows: list[sqlite3.Row],
    db_path: str,
    job_key: str,
    retention_days: int | None,
    group_issues: list[AdapterIssue],
) -> EvidenceGroupView:
    first = rows[0]
    items: list[EvidenceItemView] = []
    all_attachments: list[AttachmentView] = []
    for row in rows:
        attachments = _build_temporary_attachments(row, db_path)
        all_attachments.extend(attachments)
        status_raw = _optional_str(row["STATUS"])
        dqe_id = str(row["DQE_ID"])
        item = EvidenceItemView(
            item_id=dqe_id,
            title=_optional_str(row["TITLE"]),
            primary_value=_value_to_string(row["VALUE"]),
            unit=_optional_str(row["UNIT"]),
            status=status_raw,
            status_raw=status_raw,
            description=_optional_str(row["DESCRIPTION"]),
            reference=_optional_str(row["REFERENCE"]),
            source_path=_optional_str(row["PATH"]),
            updated_at=_optional_str(row["DATE_TIME"]),
            attributes={"record_id": row["id"], "reporter": _optional_str(row["Reporter"])},
            attachments=attachments,
            domain_extension=DqeItemExtension(
                dqe_id=dqe_id,
                raw_value=_value_to_string(row["VALUE"]),
                raw_status=status_raw,
                date_time=_optional_str(row["DATE_TIME"]),
                path=_optional_str(row["PATH"]),
                meta={
                    "title": _optional_str(row["TITLE"]),
                    "description": _optional_str(row["DESCRIPTION"]),
                    "reference": _optional_str(row["REFERENCE"]),
                },
                attachment_ref=attachments[0].location.db_ref if row["has_attachment"] else None,
                executable_ref=(
                    attachments[-1].location.db_ref if row["has_executable"] else None
                ),
            ),
        )
        items.append(item)

    latest_time = max(str(row["DATE_TIME"]) for row in rows)
    retention = _build_retention(latest_time, retention_days)
    lifecycle = "expired" if retention and retention.is_expired else "temporary"
    group_ref = make_temporary_group_ref(db_path=db_path, job_key=job_key)
    context = _build_context_from_row(first)
    return EvidenceGroupView(
        evidence_type="dqe",
        source=EvidenceSourceView(
            adapter_id="temporary_db_adapter",
            source_kind="sqlite_database",
            source_uri=db_path,
            source_display_name=Path(db_path).name,
            loaded_at="",
            read_only=True,
            raw_source_hint={"group_ref": group_ref},
        ),
        identity=EvidenceIdentityView(
            group_id=job_key,
            display_name=job_key,
            group_label="DQE Job",
            primary_time=latest_time,
            sort_key=latest_time,
        ),
        context=context,
        summary=build_summary(items),
        items=items,
        attachment_summary=build_attachment_summary(all_attachments),
        integrity=EvidenceIntegrityView(
            integrity_state="incomplete" if group_issues else "complete",
            issue_count=len(group_issues),
            issues=[item.message for item in group_issues],
        ),
        capabilities=EvidenceCapabilityView(
            can_open_detail=True,
            can_preview_attachment=True,
            can_copy_path=True,
            can_export=True,
            domain_actions=["checkin_to_isvn"],
        ),
        domain_extension=DqeGroupExtension(
            job_key=job_key,
            dqe_source_type="temporary_db",
            dqe_lifecycle_state=lifecycle,
            retention=retention,
            dqe_db=DqeDbSourceView(
                db_path=db_path,
                schema_version=None,
                root_dqe_db_num=_optional_str(first["DqeDbNum"]),
                query_time=None,
                grouped_by="job_key",
            ),
            isvn=None,
            checkin=DqeCheckinView(can_checkin=True, last_result="not_run"),
            dqe_capabilities={"group_ref": group_ref},
        ),
    )


def _build_temporary_attachments(row: sqlite3.Row, db_path: str) -> list[AttachmentView]:
    attachments: list[AttachmentView] = []
    for flag_name, blob_field, role, kind in (
        ("has_attachment", "ATTACHMENT", "evidence_file", "attachment"),
        ("has_executable", "EXECUTABLE", "executable", "executable"),
    ):
        if not row[flag_name]:
            continue
        dqe_id = str(row["DQE_ID"])
        adapter_ref = make_temporary_attachment_ref(
            db_path=db_path,
            record_id=int(row["id"]),
            job_key=str(row["job_key"]),
            dqe_id=dqe_id,
            blob_field=blob_field,
        )
        attachments.append(
            AttachmentView(
                attachment_id=f"{dqe_id}:{kind}",
                label=blob_field,
                attachment_role=role,
                state="deferred",
                location=AttachmentLocationView(
                    location_kind="db_blob_ref",
                    uri=adapter_ref,
                    db_ref=adapter_ref,
                    display_path=f"{row['job_key']}:{dqe_id}:{blob_field}",
                ),
                mime_hint=None,
                size_hint=None,
                preview_capability="deferred",
                issue=None,
            )
        )
    return attachments


def _group_context_issues(
    job_key: str,
    rows: list[sqlite3.Row],
) -> list[AdapterIssue]:
    fields = ("ProjectCode", "Phase", "Block", "Stage", "Flow", "Version")
    first = rows[0]
    issues: list[AdapterIssue] = []
    for row in rows[1:]:
        mismatched = [
            field for field in fields if str(row[field]) != str(first[field])
        ]
        if mismatched:
            issues.append(
                issue(
                    AdapterIssueSeverity.WARNING,
                    AdapterIssueCode.INCONSISTENT_GROUP_CONTEXT,
                    "rows under one job_key have inconsistent context",
                    source_ref=job_key,
                    detail={"fields": mismatched, "record_id": row["id"]},
                )
            )
    return issues


def _build_context_from_row(row: sqlite3.Row) -> EvidenceContextView:
    project = normalize_context_field("project", "Project", row["ProjectCode"])
    phase = normalize_context_field("phase", "Phase", row["Phase"])
    block = normalize_context_field("block", "Block", row["Block"])
    flow = normalize_context_field("flow", "Flow", row["Flow"])
    stage = normalize_context_field("stage", "Stage", row["Stage"])
    version = normalize_context_field("version", "Version", row["Version"])
    path = [project, phase, block, flow, stage, version]
    display_path = " / ".join(item.display_value for item in path if item.display_value)
    return EvidenceContextView(
        project=project,
        phase=phase,
        block=block,
        flow=flow,
        stage=stage,
        version=version,
        context_path=path,
        display_path=display_path,
    )


def _build_retention(
    evidence_time: str,
    retention_days: int | None,
) -> DqeRetentionView | None:
    if retention_days is None:
        return None
    parsed = _parse_dqe_time(evidence_time)
    if parsed is None:
        return DqeRetentionView(
            retention_days=retention_days,
            evidence_time=evidence_time,
            is_expired=False,
            retention_note="DATE_TIME could not be parsed",
        )
    expires_at_dt = parsed + timedelta(days=retention_days)
    return DqeRetentionView(
        retention_days=retention_days,
        evidence_time=evidence_time,
        expires_at=expires_at_dt.strftime("%Y%m%d_%H%M%S"),
        is_expired=datetime.now() > expires_at_dt,
    )


def _parse_dqe_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _value_to_string(value: object) -> str:
    if value is None:
        return ""
    return str(value)
