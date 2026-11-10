# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Structured adapter reference helpers。"""

from __future__ import annotations

import base64
import json
from typing import Any


class AdapterRefError(ValueError):
    """Adapter ref decode/validation error。"""


def encode_ref(payload: dict[str, Any]) -> str:
    """base64url(json.dumps(payload, sort_keys=True))。"""

    _validate_ref_payload(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_ref(ref: str) -> dict[str, Any]:
    """Decode 並驗證 adapter ref payload。"""

    try:
        padded = ref + "=" * (-len(ref) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AdapterRefError(f"invalid adapter ref: {exc}") from exc

    if not isinstance(payload, dict):
        raise AdapterRefError("adapter ref payload must be an object")
    _validate_ref_payload(payload)
    return payload


def make_archived_group_ref(
    *,
    version_dir: str,
    package_path: str,
    job_key: str,
) -> str:
    return encode_ref(
        {
            "ref_version": 1,
            "ref_type": "archived_group",
            "adapter": "archived_isvn",
            "version_dir": version_dir,
            "package_path": package_path,
            "job_key": job_key,
        }
    )


def make_temporary_group_ref(*, db_path: str, job_key: str) -> str:
    return encode_ref(
        {
            "ref_version": 1,
            "ref_type": "temporary_group",
            "adapter": "temporary_db",
            "db_path": db_path,
            "job_key": job_key,
        }
    )


def make_archived_attachment_ref(
    *,
    version_dir: str,
    relative_path: str,
    job_key: str,
    dqe_id: str,
    kind: str,
) -> str:
    return encode_ref(
        {
            "ref_version": 1,
            "ref_type": "archived_attachment",
            "adapter": "archived_isvn",
            "version_dir": version_dir,
            "relative_path": relative_path,
            "job_key": job_key,
            "dqe_id": dqe_id,
            "kind": kind,
        }
    )


def make_temporary_attachment_ref(
    *,
    db_path: str,
    record_id: int,
    job_key: str,
    dqe_id: str,
    blob_field: str,
) -> str:
    if blob_field not in {"ATTACHMENT", "EXECUTABLE"}:
        raise AdapterRefError("blob_field must be ATTACHMENT or EXECUTABLE")
    return encode_ref(
        {
            "ref_version": 1,
            "ref_type": "temporary_attachment",
            "adapter": "temporary_db",
            "db_path": db_path,
            "record_id": record_id,
            "job_key": job_key,
            "dqe_id": dqe_id,
            "blob_field": blob_field,
        }
    )


def _validate_ref_payload(payload: dict[str, Any]) -> None:
    if payload.get("ref_version") != 1:
        raise AdapterRefError("adapter ref must contain ref_version=1")
    if not payload.get("ref_type"):
        raise AdapterRefError("adapter ref must contain ref_type")
    if not payload.get("adapter"):
        raise AdapterRefError("adapter ref must contain adapter")

