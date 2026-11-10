# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer manual purge workflow。

本模組只刪除 temporary DQE_DB 的 `Version_Dqe_Tab` rows。
不觸碰 isvn、archived package、retention policy、EventStore 或 scheduled clean。
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from terminux_engine.dqe.db import DqeDbManager
from terminux_engine.dqe.viewer.retention import get_retention_policy

DEFAULT_DQE_DB_PATH = Path.home() / ".terminux" / "dqe" / "dqe.sqlite"
CONFIRMATION_TOKEN_TTL_SECONDS = 5 * 60

MODE_SELECTED_JOBS = "selected_jobs"
MODE_EXPIRED_NOW = "expired_now"
STATUS_READY = "ready"
STATUS_PURGED = "purged"
STATUS_FAILED = "failed"


class PurgeManager(Protocol):
    def clean(self, retention_days: int = 14) -> int:
        ...

    def vacuum(self) -> None:
        ...


@dataclass(frozen=True)
class PurgePlan:
    token: str
    mode: str
    db_path: Path
    job_keys: tuple[str, ...]
    retention_days: int | None
    vacuum_requested: bool
    expires_at_epoch: float
    candidate_row_count: int
    candidate_job_count: int
    candidate_attachment_blob_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def expires_at_iso(self) -> str:
        return _epoch_to_iso(self.expires_at_epoch)


_TOKEN_CACHE: dict[str, PurgePlan] = {}


def preview_purge(
    *,
    mode: str,
    db_path: Path | None = None,
    job_keys: list[str] | tuple[str, ...] | None = None,
    vacuum_requested: bool = False,
    now: float | None = None,
) -> dict[str, object]:
    """建立 manual purge preview plan 並回傳 confirmation token。"""

    resolved_db_path = db_path or DEFAULT_DQE_DB_PATH
    now_epoch = now if now is not None else time.time()
    _expire_old_tokens(now_epoch)

    if mode == MODE_SELECTED_JOBS:
        normalized_job_keys = tuple(sorted(dict.fromkeys(job_keys or ())))
        if not normalized_job_keys:
            return _failed_preview(mode, resolved_db_path, "selected_jobs requires non-empty job_keys")
        stats = _preview_selected_jobs(resolved_db_path, normalized_job_keys)
        retention_days: int | None = None
    elif mode == MODE_EXPIRED_NOW:
        normalized_job_keys = ()
        policy = get_retention_policy()
        retention_days = policy.retention_days
        stats = _preview_expired_now(resolved_db_path, retention_days)
    else:
        return _failed_preview(mode, resolved_db_path, f"unsupported purge mode: {mode}")

    token = secrets.token_urlsafe(24)
    plan = PurgePlan(
        token=token,
        mode=mode,
        db_path=resolved_db_path,
        job_keys=normalized_job_keys,
        retention_days=retention_days,
        vacuum_requested=vacuum_requested,
        expires_at_epoch=now_epoch + CONFIRMATION_TOKEN_TTL_SECONDS,
        candidate_row_count=stats["candidate_row_count"],
        candidate_job_count=stats["candidate_job_count"],
        candidate_attachment_blob_count=stats["candidate_attachment_blob_count"],
        warnings=tuple(stats["warnings"]),
    )
    _TOKEN_CACHE[token] = plan
    return _plan_to_dict(plan)


def purge_jobs(
    *,
    confirmation_token: str,
    mode: str,
    now: float | None = None,
    manager_factory: Callable[[Path], PurgeManager] | None = None,
) -> dict[str, object]:
    """執行已確認的 purge plan；domain failure 以 status=failed 回傳。"""

    now_epoch = now if now is not None else time.time()
    _expire_old_tokens(now_epoch)
    plan = _TOKEN_CACHE.get(confirmation_token)
    if plan is None:
        return _failed_commit(mode, "confirmation token is stale or unknown")
    if plan.mode != mode:
        return _failed_commit(mode, "confirmation token mode mismatch")
    if plan.expires_at_epoch <= now_epoch:
        _TOKEN_CACHE.pop(confirmation_token, None)
        return _failed_commit(mode, "confirmation token expired")

    _TOKEN_CACHE.pop(confirmation_token, None)
    if plan.mode == MODE_SELECTED_JOBS:
        result = _purge_selected_jobs(plan)
    elif plan.mode == MODE_EXPIRED_NOW:
        result = _purge_expired_now(plan, manager_factory=manager_factory)
    else:
        return _failed_commit(mode, f"unsupported purge mode: {mode}")

    if result["status"] != STATUS_PURGED:
        return result

    if plan.vacuum_requested:
        manager = (manager_factory or DqeDbManager)(plan.db_path)
        try:
            manager.vacuum()
            result["vacuum_performed"] = True
        except Exception as exc:
            result["vacuum_performed"] = False
            result.setdefault("warnings", []).append(
                f"Rows were deleted, but VACUUM failed: {exc}"
            )
    return result


def clear_confirmation_tokens() -> None:
    """測試用：清空 process-local token cache。"""

    _TOKEN_CACHE.clear()


def _preview_selected_jobs(db_path: Path, job_keys: tuple[str, ...]) -> dict[str, object]:
    if not db_path.exists():
        return {
            "candidate_row_count": 0,
            "candidate_job_count": 0,
            "candidate_attachment_blob_count": 0,
            "warnings": [f"DQE_DB does not exist: {db_path}"],
        }

    placeholders = ",".join("?" for _ in job_keys)
    conn = sqlite3.connect(str(db_path))
    try:
        existing_rows = conn.execute(
            f"""
            SELECT job_key,
                   COUNT(*) AS row_count,
                   SUM(CASE WHEN ATTACHMENT IS NOT NULL THEN 1 ELSE 0 END) AS attachment_count,
                   SUM(CASE WHEN EXECUTABLE IS NOT NULL THEN 1 ELSE 0 END) AS executable_count
            FROM Version_Dqe_Tab
            WHERE job_key IN ({placeholders})
            GROUP BY job_key
            """,
            job_keys,
        ).fetchall()
    finally:
        conn.close()

    existing_keys = {str(row[0]) for row in existing_rows}
    unknown = [key for key in job_keys if key not in existing_keys]
    return {
        "candidate_row_count": sum(int(row[1] or 0) for row in existing_rows),
        "candidate_job_count": len(existing_keys),
        "candidate_attachment_blob_count": sum(
            int(row[2] or 0) + int(row[3] or 0) for row in existing_rows
        ),
        "warnings": [f"Unknown job_key ignored in preview: {key}" for key in unknown],
    }


def _preview_expired_now(db_path: Path, retention_days: int) -> dict[str, object]:
    if not db_path.exists():
        return {
            "candidate_row_count": 0,
            "candidate_job_count": 0,
            "candidate_attachment_blob_count": 0,
            "warnings": [f"DQE_DB does not exist: {db_path}"],
        }
    cutoff_time = _cutoff_time(retention_days)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS row_count,
                   COUNT(DISTINCT job_key) AS job_count,
                   SUM(CASE WHEN ATTACHMENT IS NOT NULL THEN 1 ELSE 0 END) AS attachment_count,
                   SUM(CASE WHEN EXECUTABLE IS NOT NULL THEN 1 ELSE 0 END) AS executable_count
            FROM Version_Dqe_Tab
            WHERE DATE_TIME < ?
            """,
            (cutoff_time,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "candidate_row_count": int(row[0] or 0),
        "candidate_job_count": int(row[1] or 0),
        "candidate_attachment_blob_count": int(row[2] or 0) + int(row[3] or 0),
        "warnings": [],
    }


def _purge_selected_jobs(plan: PurgePlan) -> dict[str, object]:
    if not plan.db_path.exists():
        return _failed_commit(plan.mode, f"DQE_DB does not exist: {plan.db_path}", plan=plan)

    conn = sqlite3.connect(str(plan.db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_keys = _existing_job_keys(conn, plan.job_keys)
        if not existing_keys:
            conn.rollback()
            return _failed_commit(
                plan.mode,
                "no confirmed job_keys still exist",
                plan=plan,
            )
        deleted_rows = _delete_selected_jobs_in_transaction(conn, existing_keys)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return _failed_commit(plan.mode, f"selected job purge failed: {exc}", plan=plan)
    finally:
        conn.close()

    missing = [key for key in plan.job_keys if key not in set(existing_keys)]
    warnings = [f"Confirmed job_key no longer exists at commit: {key}" for key in missing]
    return {
        "status": STATUS_PURGED,
        "mode": plan.mode,
        "deleted_row_count": deleted_rows,
        "deleted_job_count": len(existing_keys),
        "vacuum_requested": plan.vacuum_requested,
        "vacuum_performed": False,
        "warnings": warnings,
    }


def _purge_expired_now(
    plan: PurgePlan,
    *,
    manager_factory: Callable[[Path], PurgeManager] | None = None,
) -> dict[str, object]:
    retention_days = plan.retention_days
    if retention_days is None:
        return _failed_commit(plan.mode, "expired_now token lacks retention_days", plan=plan)
    manager = (manager_factory or DqeDbManager)(plan.db_path)
    try:
        deleted_rows = manager.clean(retention_days=retention_days)
    except Exception as exc:
        return _failed_commit(plan.mode, f"expired clean failed: {exc}", plan=plan)
    return {
        "status": STATUS_PURGED,
        "mode": plan.mode,
        "deleted_row_count": deleted_rows,
        "deleted_job_count": None,
        "retention_days": retention_days,
        "vacuum_requested": plan.vacuum_requested,
        "vacuum_performed": False,
        "warnings": [],
    }


def _existing_job_keys(conn: sqlite3.Connection, job_keys: tuple[str, ...]) -> tuple[str, ...]:
    placeholders = ",".join("?" for _ in job_keys)
    rows = conn.execute(
        f"SELECT DISTINCT job_key FROM Version_Dqe_Tab WHERE job_key IN ({placeholders})",
        job_keys,
    ).fetchall()
    found = {str(row[0]) for row in rows}
    return tuple(key for key in job_keys if key in found)


def _delete_selected_jobs_in_transaction(
    conn: sqlite3.Connection,
    job_keys: tuple[str, ...],
    *,
    fail_after_delete: bool = False,
) -> int:
    placeholders = ",".join("?" for _ in job_keys)
    cur = conn.execute(
        f"DELETE FROM Version_Dqe_Tab WHERE job_key IN ({placeholders})",
        job_keys,
    )
    if fail_after_delete:
        raise RuntimeError("injected delete failure")
    return int(cur.rowcount)


def _plan_to_dict(plan: PurgePlan) -> dict[str, object]:
    return {
        "status": STATUS_READY,
        "mode": plan.mode,
        "candidate_job_count": plan.candidate_job_count,
        "candidate_row_count": plan.candidate_row_count,
        "candidate_attachment_blob_count": plan.candidate_attachment_blob_count,
        "job_keys": list(plan.job_keys),
        "retention_days": plan.retention_days,
        "vacuum_requested": plan.vacuum_requested,
        "confirmation_token": plan.token,
        "expires_at": plan.expires_at_iso(),
        "warnings": list(plan.warnings),
    }


def _failed_preview(mode: str, db_path: Path, message: str) -> dict[str, object]:
    return {
        "status": STATUS_FAILED,
        "mode": mode,
        "db_path": str(db_path),
        "candidate_job_count": 0,
        "candidate_row_count": 0,
        "candidate_attachment_blob_count": 0,
        "vacuum_requested": False,
        "confirmation_token": None,
        "warnings": [message],
    }


def _failed_commit(
    mode: str,
    message: str,
    *,
    plan: PurgePlan | None = None,
) -> dict[str, object]:
    return {
        "status": STATUS_FAILED,
        "mode": mode,
        "deleted_row_count": 0,
        "deleted_job_count": 0,
        "vacuum_requested": bool(plan.vacuum_requested) if plan else False,
        "vacuum_performed": False,
        "warnings": [message],
    }


def _expire_old_tokens(now_epoch: float) -> None:
    for token, plan in list(_TOKEN_CACHE.items()):
        if plan.expires_at_epoch <= now_epoch:
            _TOKEN_CACHE.pop(token, None)


def _cutoff_time(retention_days: int) -> str:
    return (datetime.now() - timedelta(days=retention_days)).strftime("%Y%m%d_%H%M%S")


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONFIRMATION_TOKEN_TTL_SECONDS",
    "DEFAULT_DQE_DB_PATH",
    "MODE_EXPIRED_NOW",
    "MODE_SELECTED_JOBS",
    "STATUS_FAILED",
    "STATUS_PURGED",
    "STATUS_READY",
    "clear_confirmation_tokens",
    "preview_purge",
    "purge_jobs",
]
