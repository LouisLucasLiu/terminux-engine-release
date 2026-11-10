# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE scheduled clean integration。

本模組只提供 unattended scheduled maintenance entry point：

1. 讀取 Temporary retention policy。
2. 以該 N 呼叫 DqeDbManager.clean(retention_days=N)。
3. clean 成功後才視設定呼叫 vacuum()。

本模組不得修改 retention policy、不得觸碰 isvn、不得刪除 archived package
或 EventStore。DqeDbManager.clean() 仍保持純機制，不讀 policy file。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from terminux_engine.dqe.db import DqeDbManager
from terminux_engine.dqe.viewer.retention import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_RETENTION_DAYS,
    get_retention_policy,
)
from terminux_engine.dqe.viewer.status_contract import severity_for_state

DEFAULT_DQE_DB_PATH = Path.home() / ".terminux" / "dqe" / "dqe.sqlite"
DEFAULT_LOG_PATH = Path.home() / ".terminux" / "dqe" / "scheduled_clean.log"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SUCCESS_DEGRADED = "success_degraded"

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_INVALID_ARGS = 2
EXIT_DEGRADED_SUCCESS = 3


class _DqeMaintenanceManager(Protocol):
    def clean(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        ...

    def vacuum(self) -> None:
        ...


@dataclass(frozen=True)
class ScheduledCleanResult:
    """Scheduled clean 結構化結果。"""

    status: str
    retention_days: int
    policy_source: str
    policy_warning: str | None
    policy_degraded: bool
    db_path: str
    policy_path: str
    cutoff_time: str
    expected_delete_count: int | None
    expected_count_available: bool
    expected_count_warning: str | None
    records_deleted: int
    vacuum_ran: bool
    dry_run: bool
    started_at: str
    finished_at: str
    severity: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def run_scheduled_clean(
    *,
    db_path: Path | None = None,
    policy_path: Path | None = None,
    dry_run: bool = False,
    vacuum: bool = True,
    log_path: Path | None = None,
    manager_factory: Callable[[Path], _DqeMaintenanceManager] | None = None,
) -> ScheduledCleanResult:
    """執行 scheduled clean。

    `manager_factory` 僅供測試注入，production 預設使用 DqeDbManager。
    """

    started_at = _now_iso()
    resolved_db_path = db_path or DEFAULT_DQE_DB_PATH
    resolved_policy_path = policy_path or DEFAULT_CONFIG_PATH
    resolved_log_path = log_path or DEFAULT_LOG_PATH

    try:
        policy = get_retention_policy(config_path=resolved_policy_path)
    except Exception as exc:
        result = _result(
            status=STATUS_FAILED,
            retention_days=DEFAULT_RETENTION_DAYS,
            policy_source="unavailable",
            policy_warning=None,
            policy_degraded=False,
            db_path=resolved_db_path,
            policy_path=resolved_policy_path,
            cutoff_time="",
            expected_delete_count=None,
            expected_count_available=False,
            expected_count_warning=None,
            records_deleted=0,
            vacuum_ran=False,
            dry_run=dry_run,
            started_at=started_at,
            error=f"Failed to load retention policy: {exc}",
        )
        _append_audit_log(resolved_log_path, "failure", result)
        return result

    retention_days = policy.retention_days
    cutoff_time = _cutoff_time(retention_days)
    policy_degraded = (
        policy.source == "default_due_to_invalid_config"
        or (policy.warning is not None and policy.source != "default")
    )
    result_policy_source = _scheduled_policy_source(policy.source, policy_degraded)

    expected_count: int | None
    expected_available: bool
    expected_warning: str | None
    try:
        expected_count = _count_expected_deletes(resolved_db_path, cutoff_time)
        expected_available = True
        expected_warning = None
    except Exception as exc:
        expected_count = None
        expected_available = False
        expected_warning = str(exc)

    pre_result = _result(
        status=STATUS_SUCCESS_DEGRADED if policy_degraded else STATUS_SUCCESS,
        retention_days=retention_days,
        policy_source=result_policy_source,
        policy_warning=policy.warning,
        policy_degraded=policy_degraded,
        db_path=resolved_db_path,
        policy_path=resolved_policy_path,
        cutoff_time=cutoff_time,
        expected_delete_count=expected_count,
        expected_count_available=expected_available,
        expected_count_warning=expected_warning,
        records_deleted=0,
        vacuum_ran=False,
        dry_run=dry_run,
        started_at=started_at,
        error=None,
    )
    _append_audit_log(resolved_log_path, "pre_clean", pre_result)

    if dry_run:
        _append_audit_log(resolved_log_path, "post_clean", pre_result)
        return pre_result

    manager = (manager_factory or DqeDbManager)(resolved_db_path)
    try:
        records_deleted = manager.clean(retention_days=retention_days)
    except Exception as exc:
        result = _result(
            status=STATUS_FAILED,
            retention_days=retention_days,
            policy_source=result_policy_source,
            policy_warning=policy.warning,
            policy_degraded=policy_degraded,
            db_path=resolved_db_path,
            policy_path=resolved_policy_path,
            cutoff_time=cutoff_time,
            expected_delete_count=expected_count,
            expected_count_available=expected_available,
            expected_count_warning=expected_warning,
            records_deleted=0,
            vacuum_ran=False,
            dry_run=False,
            started_at=started_at,
            error=f"clean failed: {exc}",
        )
        _append_audit_log(resolved_log_path, "post_clean", result)
        return result

    vacuum_ran = False
    if vacuum:
        try:
            manager.vacuum()
            vacuum_ran = True
        except Exception as exc:
            result = _result(
                status=STATUS_FAILED,
                retention_days=retention_days,
                policy_source=result_policy_source,
                policy_warning=policy.warning,
                policy_degraded=policy_degraded,
                db_path=resolved_db_path,
                policy_path=resolved_policy_path,
                cutoff_time=cutoff_time,
                expected_delete_count=expected_count,
                expected_count_available=expected_available,
                expected_count_warning=expected_warning,
                records_deleted=records_deleted,
                vacuum_ran=False,
                dry_run=False,
                started_at=started_at,
                error=f"vacuum failed after clean: {exc}",
            )
            _append_audit_log(resolved_log_path, "post_clean", result)
            return result

    result = _result(
        status=STATUS_SUCCESS_DEGRADED if policy_degraded else STATUS_SUCCESS,
        retention_days=retention_days,
        policy_source=result_policy_source,
        policy_warning=policy.warning,
        policy_degraded=policy_degraded,
        db_path=resolved_db_path,
        policy_path=resolved_policy_path,
        cutoff_time=cutoff_time,
        expected_delete_count=expected_count,
        expected_count_available=expected_available,
        expected_count_warning=expected_warning,
        records_deleted=records_deleted,
        vacuum_ran=vacuum_ran,
        dry_run=False,
        started_at=started_at,
        error=None,
    )
    _append_audit_log(resolved_log_path, "post_clean", result)
    return result


def exit_code_for_result(result: ScheduledCleanResult) -> int:
    if result.status == STATUS_SUCCESS:
        return EXIT_SUCCESS
    if result.status == STATUS_SUCCESS_DEGRADED:
        return EXIT_DEGRADED_SUCCESS
    return EXIT_FAILURE


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(
        prog="dqe-scheduled-clean",
        description="Run DQE scheduled clean using the Temporary retention policy.",
    )
    parser.add_argument("--db", dest="db_path", type=Path, default=None)
    parser.add_argument("--policy", dest="policy_path", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--no-vacuum", action="store_true", default=False)
    parser.add_argument("--log", dest="log_path", type=Path, default=None)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_INVALID_ARGS

    from terminux_engine.ipc.daemon_owner import check_local_database_access
    owner_check = check_local_database_access()
    if owner_check:
        return owner_check

    result = run_scheduled_clean(
        db_path=args.db_path,
        policy_path=args.policy_path,
        dry_run=args.dry_run,
        vacuum=not args.no_vacuum,
        log_path=args.log_path,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return exit_code_for_result(result)


def _count_expected_deletes(db_path: Path, cutoff_time: str) -> int:
    if not db_path.exists():
        return 0

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM Version_Dqe_Tab WHERE DATE_TIME < ?",
            (cutoff_time,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def _append_audit_log(log_path: Path, event: str, result: ScheduledCleanResult) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["event"] = event
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        fp.write("\n")


def _result(
    *,
    status: str,
    retention_days: int,
    policy_source: str,
    policy_warning: str | None,
    policy_degraded: bool,
    db_path: Path,
    policy_path: Path,
    cutoff_time: str,
    expected_delete_count: int | None,
    expected_count_available: bool,
    expected_count_warning: str | None,
    records_deleted: int,
    vacuum_ran: bool,
    dry_run: bool,
    started_at: str,
    error: str | None,
) -> ScheduledCleanResult:
    return ScheduledCleanResult(
        status=status,
        retention_days=retention_days,
        policy_source=policy_source,
        policy_warning=policy_warning,
        policy_degraded=policy_degraded,
        db_path=str(db_path),
        policy_path=str(policy_path),
        cutoff_time=cutoff_time,
        expected_delete_count=expected_delete_count,
        expected_count_available=expected_count_available,
        expected_count_warning=expected_count_warning,
        records_deleted=records_deleted,
        vacuum_ran=vacuum_ran,
        dry_run=dry_run,
        started_at=started_at,
        finished_at=_now_iso(),
        severity=_severity_for_status(status),
        error=error,
    )


def _severity_for_status(status: str) -> str:
    if status == STATUS_SUCCESS:
        return severity_for_state("action_success")
    if status == STATUS_SUCCESS_DEGRADED:
        return severity_for_state("success_degraded")
    if status == STATUS_FAILED:
        return severity_for_state("action_failed")
    return severity_for_state("warning")


def _scheduled_policy_source(source: str, degraded: bool) -> str:
    if degraded and source == "default_due_to_invalid_config":
        return "fallback_default"
    return source


def _cutoff_time(retention_days: int) -> str:
    return (
        datetime.now() - timedelta(days=retention_days)
    ).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_DQE_DB_PATH",
    "DEFAULT_LOG_PATH",
    "EXIT_DEGRADED_SUCCESS",
    "EXIT_FAILURE",
    "EXIT_INVALID_ARGS",
    "EXIT_SUCCESS",
    "ScheduledCleanResult",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "STATUS_SUCCESS_DEGRADED",
    "exit_code_for_result",
    "main",
    "run_scheduled_clean",
]
