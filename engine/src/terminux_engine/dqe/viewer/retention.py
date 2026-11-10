# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer temporary retention policy store。

本模組只管理 retention N policy；不得清理、VACUUM、purge 或刪除 DQE_DB rows。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RETENTION_DAYS = 14
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
POLICY_SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path.home() / ".terminux" / "dqe" / "retention_policy.json"


class RetentionPolicyError(RuntimeError):
    """Retention policy read/write 受控錯誤。"""


class InvalidRetentionDaysError(ValueError):
    """retention_days 型別或範圍錯誤。"""


@dataclass(frozen=True)
class RetentionPolicy:
    retention_days: int
    default_retention_days: int
    min_retention_days: int
    max_retention_days: int
    config_path: str
    source: str
    updated_at: str | None
    updated_by: str | None
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_retention_policy(config_path: Path | None = None) -> RetentionPolicy:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return _policy(
            retention_days=DEFAULT_RETENTION_DAYS,
            config_path=path,
            source="default",
            updated_at=None,
            updated_by=None,
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("policy JSON must be an object")
        retention_days = data.get("retention_days")
        _validate_retention_days(retention_days)
        schema_version = data.get("schema_version")
        if schema_version != POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported retention policy schema_version")
        source = data.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        updated_at = data.get("updated_at")
        updated_by = data.get("updated_by")
        return _policy(
            retention_days=retention_days,
            config_path=path,
            source="configured",
            updated_at=updated_at if isinstance(updated_at, str) else None,
            updated_by=updated_by if isinstance(updated_by, str) else None,
        )
    except Exception as exc:
        return _policy(
            retention_days=DEFAULT_RETENTION_DAYS,
            config_path=path,
            source="default_due_to_invalid_config",
            updated_at=None,
            updated_by=None,
            warning=f"Invalid retention policy config: {exc}",
        )


def set_retention_policy(
    retention_days: int,
    *,
    updated_by: str | None = None,
    config_path: Path | None = None,
) -> RetentionPolicy:
    _validate_retention_days(retention_days)
    path = config_path or DEFAULT_CONFIG_PATH
    updated_at = _now_iso()
    payload: dict[str, Any] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "retention_days": retention_days,
        "updated_at": updated_at,
        "updated_by": updated_by,
        "source": "dqe_viewer",
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        try:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()
        finally:
            raise RetentionPolicyError("Failed to write retention policy") from exc

    return _policy(
        retention_days=retention_days,
        config_path=path,
        source="configured",
        updated_at=updated_at,
        updated_by=updated_by,
    )


def validate_retention_days(value: Any) -> int:
    _validate_retention_days(value)
    return int(value)


def _validate_retention_days(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRetentionDaysError("Invalid retention_days: expected integer 1..3650")
    if value < MIN_RETENTION_DAYS or value > MAX_RETENTION_DAYS:
        raise InvalidRetentionDaysError("Invalid retention_days: expected integer 1..3650")


def _policy(
    *,
    retention_days: int,
    config_path: Path,
    source: str,
    updated_at: str | None,
    updated_by: str | None,
    warning: str | None = None,
) -> RetentionPolicy:
    return RetentionPolicy(
        retention_days=retention_days,
        default_retention_days=DEFAULT_RETENTION_DAYS,
        min_retention_days=MIN_RETENTION_DAYS,
        max_retention_days=MAX_RETENTION_DAYS,
        config_path=str(config_path),
        source=source,
        updated_at=updated_at,
        updated_by=updated_by,
        warning=warning,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_RETENTION_DAYS",
    "MIN_RETENTION_DAYS",
    "MAX_RETENTION_DAYS",
    "POLICY_SCHEMA_VERSION",
    "InvalidRetentionDaysError",
    "RetentionPolicy",
    "RetentionPolicyError",
    "get_retention_policy",
    "set_retention_policy",
    "validate_retention_days",
]
