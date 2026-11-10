# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Archived isvn DQE package adapter。"""

from __future__ import annotations

import json
import mimetypes
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
    make_archived_attachment_ref,
    make_archived_group_ref,
)
from terminux_engine.dqe.viewer.adapters.requests import ArchivedIsvnQuery
from terminux_engine.dqe.viewer.adapters.responses import AdapterResult
from terminux_engine.dqe.viewer.models import DqeGroupExtension, DqeIsvnSourceView
from terminux_engine.dqe.viewer.models import DqeItemExtension
from terminux_engine.evidence_viewer.models import (
    AttachmentLocationView,
    AttachmentSummaryView,
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


class ArchivedIsvnAdapter(SourceAdapter):
    """讀取 version_dir 內的 dqe_*.json archived package。"""

    def list_groups(
        self,
        query: ArchivedIsvnQuery,
    ) -> AdapterResult[list[EvidenceGroupView]]:
        version_dir_result = _resolve_version_dir(query)
        if isinstance(version_dir_result, AdapterIssue):
            return AdapterResult(ok=False, issues=[version_dir_result])

        version_dir = version_dir_result
        package_paths = sorted(version_dir.glob(query.package_glob))
        if not package_paths:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.SOURCE_EMPTY,
                        "version_dir does not contain archived DQE packages",
                        source_ref=str(version_dir),
                    )
                ],
            )

        groups: list[EvidenceGroupView] = []
        issues: list[AdapterIssue] = []
        for package_path in package_paths:
            result = self._load_package(package_path, version_dir)
            issues.extend(result.issues)
            if result.value is not None:
                groups.append(result.value)

        if not groups:
            issues.append(
                issue(
                    AdapterIssueSeverity.ERROR,
                    AdapterIssueCode.SOURCE_EMPTY,
                    "no valid archived DQE package was loaded",
                    source_ref=str(version_dir),
                )
            )
            return AdapterResult(ok=False, issues=issues)

        return AdapterResult(ok=True, value=groups, issues=issues)

    def load_group(self, group_ref: str) -> AdapterResult[EvidenceGroupView]:
        try:
            payload = decode_ref(group_ref)
            if payload["ref_type"] != "archived_group":
                raise AdapterRefError("ref_type must be archived_group")
            package_path = Path(str(payload["package_path"]))
            version_dir = Path(str(payload["version_dir"]))
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
        return self._load_package(package_path, version_dir)

    def load_attachment(
        self,
        attachment_ref: str,
        max_bytes: int | None = None,
    ) -> AdapterResult[AttachmentPayload]:
        try:
            payload = decode_ref(attachment_ref)
            if payload["ref_type"] != "archived_attachment":
                raise AdapterRefError("ref_type must be archived_attachment")
            version_dir = Path(str(payload["version_dir"]))
            relative_path = str(payload["relative_path"])
            resolved = _safe_resolve_under(version_dir, relative_path)
            if resolved is None:
                return AdapterResult(
                    ok=False,
                    issues=[
                        issue(
                            AdapterIssueSeverity.ERROR,
                            AdapterIssueCode.UNSAFE_ATTACHMENT_PATH,
                            "attachment path escapes version_dir",
                            source_ref=attachment_ref,
                            detail={"relative_path": relative_path},
                        )
                    ],
                )
            if not resolved.exists() or not resolved.is_file():
                return AdapterResult(
                    ok=False,
                    issues=[
                        issue(
                            AdapterIssueSeverity.ERROR,
                            AdapterIssueCode.ATTACHMENT_NOT_FOUND,
                            "attachment file does not exist",
                            source_ref=attachment_ref,
                            detail={"path": str(resolved)},
                        )
                    ],
                )
            size_bytes = resolved.stat().st_size
            if max_bytes is not None and size_bytes > max_bytes:
                return AdapterResult(
                    ok=False,
                    issues=[
                        issue(
                            AdapterIssueSeverity.ERROR,
                            AdapterIssueCode.ATTACHMENT_TOO_LARGE,
                            "attachment file exceeds preview size limit",
                            source_ref=attachment_ref,
                            detail={
                                "path": str(resolved),
                                "size_bytes": size_bytes,
                                "max_bytes": max_bytes,
                            },
                        )
                    ],
                )
            content = resolved.read_bytes()
        except AdapterRefError as exc:
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
        except OSError as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.ATTACHMENT_LOAD_FAILED,
                        "attachment file cannot be read",
                        source_ref=attachment_ref,
                        detail={"error": str(exc)},
                    )
                ],
            )

        return AdapterResult(
            ok=True,
            value=AttachmentPayload(
                attachment_ref=attachment_ref,
                filename=resolved.name,
                mime_type=mimetypes.guess_type(resolved.name)[0],
                size_bytes=len(content),
                content=content,
            ),
        )

    def _load_package(
        self,
        package_path: Path,
        version_dir: Path,
    ) -> AdapterResult[EvidenceGroupView]:
        issues: list[AdapterIssue] = []
        try:
            data = json.loads(package_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.INVALID_JSON,
                        "archived DQE package is invalid JSON",
                        source_ref=str(package_path),
                        detail={"error": str(exc)},
                    )
                ],
            )
        except OSError as exc:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.QUERY_FAILED,
                        "archived DQE package cannot be read",
                        source_ref=str(package_path),
                        detail={"error": str(exc)},
                    )
                ],
            )

        missing = [key for key in ("job_key", "address", "DQES") if key not in data]
        if missing:
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.MISSING_REQUIRED_FIELD,
                        "archived DQE package misses required top-level fields",
                        source_ref=str(package_path),
                        detail={"missing": missing},
                    )
                ],
            )
        if not isinstance(data["DQES"], list) or not isinstance(data["address"], dict):
            return AdapterResult(
                ok=False,
                issues=[
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.MALFORMED_RECORD,
                        "archived DQE package has malformed address or DQES",
                        source_ref=str(package_path),
                    )
                ],
            )

        job_key = str(data["job_key"])
        items: list[EvidenceItemView] = []
        all_attachments: list[AttachmentView] = []
        for raw_item in data["DQES"]:
            if not isinstance(raw_item, dict) or not raw_item.get("DQE_ID"):
                issues.append(
                    issue(
                        AdapterIssueSeverity.WARNING,
                        AdapterIssueCode.MALFORMED_RECORD,
                        "DQE item is malformed and was skipped",
                        source_ref=str(package_path),
                        detail={"item": raw_item if isinstance(raw_item, dict) else {}},
                    )
                )
                continue

            attachments = self._build_archived_attachments(
                raw_item=raw_item,
                version_dir=version_dir,
                job_key=job_key,
                issues=issues,
            )
            all_attachments.extend(attachments)
            status_raw = _optional_str(raw_item.get("STATUS"))
            item = EvidenceItemView(
                item_id=str(raw_item["DQE_ID"]),
                title=_optional_str(raw_item.get("TITLE")),
                primary_value=_value_to_string(raw_item.get("VALUE")),
                unit=_optional_str(raw_item.get("UNIT")),
                status=status_raw,
                status_raw=status_raw,
                description=_optional_str(raw_item.get("DESCRIPTION")),
                reference=_optional_str(raw_item.get("REFERENCE")),
                source_path=_optional_str(raw_item.get("PATH")),
                updated_at=_optional_str(raw_item.get("DATE_TIME")),
                attributes={},
                attachments=attachments,
                domain_extension=DqeItemExtension(
                    dqe_id=str(raw_item["DQE_ID"]),
                    raw_value=_value_to_string(raw_item.get("VALUE")),
                    raw_status=status_raw,
                    date_time=_optional_str(raw_item.get("DATE_TIME")),
                    path=_optional_str(raw_item.get("PATH")),
                    meta={
                        "title": _optional_str(raw_item.get("TITLE")),
                        "description": _optional_str(raw_item.get("DESCRIPTION")),
                        "reference": _optional_str(raw_item.get("REFERENCE")),
                    },
                    attachment_ref=_optional_str(raw_item.get("ATTACHMENT")),
                    executable_ref=_optional_str(raw_item.get("EXECUTABLE")),
                ),
            )
            items.append(item)

        address = data["address"]
        context = _build_context(
            project=address.get("project"),
            phase=address.get("phase"),
            block=address.get("block"),
            flow=address.get("flow"),
            stage=address.get("stage"),
            version=address.get("version"),
        )
        group_ref = make_archived_group_ref(
            version_dir=str(version_dir),
            package_path=str(package_path),
            job_key=job_key,
        )
        group = EvidenceGroupView(
            evidence_type="dqe",
            source=EvidenceSourceView(
                adapter_id="archived_isvn_adapter",
                source_kind="json_file",
                source_uri=str(package_path),
                source_display_name=package_path.name,
                loaded_at="",
                read_only=True,
                raw_source_hint={"group_ref": group_ref},
            ),
            identity=EvidenceIdentityView(
                group_id=job_key,
                display_name=package_path.name,
                group_label="Archived DQE Package",
                primary_time=_primary_time(items),
                sort_key=_primary_time(items) or job_key,
            ),
            context=context,
            summary=build_summary(items),
            items=items,
            attachment_summary=build_attachment_summary(all_attachments),
            integrity=EvidenceIntegrityView(
                integrity_state="incomplete" if _has_attachment_warning(issues) else "complete",
                issue_count=len(issues),
                issues=[item.message for item in issues],
            ),
            capabilities=EvidenceCapabilityView(
                can_open_detail=True,
                can_preview_attachment=True,
                can_copy_path=True,
                can_export=True,
                domain_actions=[],
            ),
            domain_extension=DqeGroupExtension(
                job_key=job_key,
                dqe_source_type="archived_isvn",
                dqe_lifecycle_state="archived",
                retention=None,
                dqe_db=None,
                isvn=DqeIsvnSourceView(
                    workspace_root=None,
                    version_path=None,
                    version_dir=str(version_dir),
                    json_path=str(package_path),
                    attachment_dir=str(version_dir / "attachments"),
                    isvn_status_hint=None,
                ),
                checkin=None,
                dqe_capabilities={"group_ref": group_ref},
            ),
        )
        return AdapterResult(ok=True, value=group, issues=issues)

    def _build_archived_attachments(
        self,
        *,
        raw_item: dict[str, Any],
        version_dir: Path,
        job_key: str,
        issues: list[AdapterIssue],
    ) -> list[AttachmentView]:
        attachments: list[AttachmentView] = []
        for field_name, role, kind in (
            ("ATTACHMENT", "evidence_file", "attachment"),
            ("EXECUTABLE", "executable", "executable"),
        ):
            raw_path = _optional_str(raw_item.get(field_name))
            if not raw_path:
                continue
            dqe_id = str(raw_item["DQE_ID"])
            adapter_ref = make_archived_attachment_ref(
                version_dir=str(version_dir),
                relative_path=raw_path,
                job_key=job_key,
                dqe_id=dqe_id,
                kind=kind,
            )
            resolved = _safe_resolve_under(version_dir, raw_path)
            if resolved is None:
                state = "error"
                issues.append(
                    issue(
                        AdapterIssueSeverity.ERROR,
                        AdapterIssueCode.UNSAFE_ATTACHMENT_PATH,
                        "attachment path escapes version_dir",
                        source_ref=str(version_dir),
                        item_ref=dqe_id,
                        detail={"relative_path": raw_path},
                    )
                )
            elif not resolved.exists():
                state = "missing"
                issues.append(
                    issue(
                        AdapterIssueSeverity.WARNING,
                        AdapterIssueCode.ATTACHMENT_NOT_FOUND,
                        "attachment file is missing",
                        source_ref=str(version_dir),
                        item_ref=dqe_id,
                        detail={"relative_path": raw_path},
                    )
                )
            else:
                state = "available"

            attachments.append(
                AttachmentView(
                    attachment_id=f"{dqe_id}:{kind}",
                    label=field_name,
                    attachment_role=role,
                    state=state,
                    location=AttachmentLocationView(
                        location_kind="relative_path",
                        uri=adapter_ref,
                        relative_path=raw_path,
                        display_path=raw_path,
                    ),
                    mime_hint=mimetypes.guess_type(raw_path)[0],
                    size_hint=resolved.stat().st_size if resolved and resolved.exists() else None,
                    preview_capability="deferred",
                    issue=None if state == "available" else state,
                )
            )
        return attachments


def _resolve_version_dir(query: ArchivedIsvnQuery) -> Path | AdapterIssue:
    if query.version_dir:
        version_dir = Path(query.version_dir)
    elif query.workspace_root and query.version_path:
        version_dir = Path(query.workspace_root) / query.version_path
    else:
        return issue(
            AdapterIssueSeverity.ERROR,
            AdapterIssueCode.SOURCE_NOT_FOUND,
            "version_dir is required when workspace_root/version_path are not both set",
        )
    if not version_dir.exists() or not version_dir.is_dir():
        return issue(
            AdapterIssueSeverity.ERROR,
            AdapterIssueCode.SOURCE_NOT_FOUND,
            "version_dir does not exist",
            source_ref=str(version_dir),
        )
    return version_dir


def _safe_resolve_under(root: Path, relative_path: str) -> Path | None:
    try:
        root_resolved = root.resolve()
        target = (root_resolved / relative_path).resolve()
        target.relative_to(root_resolved)
        return target
    except (OSError, ValueError):
        return None


def _build_context(
    *,
    project: object,
    phase: object,
    block: object,
    flow: object,
    stage: object,
    version: object,
) -> EvidenceContextView:
    project_field = normalize_context_field("project", "Project", project)
    phase_field = normalize_context_field("phase", "Phase", phase)
    block_field = normalize_context_field("block", "Block", block)
    flow_field = normalize_context_field("flow", "Flow", flow)
    stage_field = normalize_context_field("stage", "Stage", stage)
    version_field = normalize_context_field("version", "Version", version)
    path = [
        project_field,
        phase_field,
        block_field,
        flow_field,
        stage_field,
        version_field,
    ]
    display_path = " / ".join(field.display_value for field in path if field.display_value)
    return EvidenceContextView(
        project=project_field,
        phase=phase_field,
        block=block_field,
        flow=flow_field,
        stage=stage_field,
        version=version_field,
        context_path=path,
        display_path=display_path,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _value_to_string(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _primary_time(items: list[EvidenceItemView]) -> str | None:
    times = [item.updated_at for item in items if item.updated_at]
    return max(times) if times else None


def _has_attachment_warning(issues: list[AdapterIssue]) -> bool:
    return any(
        item.code
        in {
            AdapterIssueCode.ATTACHMENT_NOT_FOUND,
            AdapterIssueCode.UNSAFE_ATTACHMENT_PATH,
        }
        for item in issues
    )
