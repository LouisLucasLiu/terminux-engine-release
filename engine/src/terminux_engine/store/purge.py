# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""EventStore 依日期區間歸檔與清除的維運工具。

此工具使用獨立 SQLite 連線；建議在 Terminux daemon 停止時執行，避免與持續寫入的
EventStore 並行操作。
"""

from __future__ import annotations

import getpass
import json
import os
import sqlite3
import time
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any


_RANGE_SQL = "WHERE timestamp >= ? AND timestamp < ?"


def count_events_in_range(db_path: str, from_date: str, to_date: str) -> int:
    """計算區間內事件筆數。純查詢，不改 DB。"""
    start_epoch, end_epoch = _date_range_to_epochs(from_date, to_date)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM session_events {_RANGE_SQL}",
            (start_epoch, end_epoch),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def archive_events_in_range(
    db_path: str,
    from_date: str,
    to_date: str,
    archive_dir: str,
) -> str:
    """把區間內事件 dump 成 JSONL 檔，回傳 archive 檔路徑，不刪 DB。"""
    start_epoch, end_epoch = _date_range_to_epochs(from_date, to_date)
    conn = _connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM session_events "
            f"{_RANGE_SQL} ORDER BY timestamp ASC",
            (start_epoch, end_epoch),
        ).fetchall()
    finally:
        conn.close()

    return _write_archive(rows, from_date, to_date, archive_dir)


def purge_events_in_range(
    db_path: str,
    from_date: str,
    to_date: str,
    *,
    force: bool = False,
    archive_dir: str | None = None,
) -> dict[str, Any]:
    """依日期區間預覽或執行 EventStore 清除。

    預設為 dry-run。force 模式會先完成 archive 寫入，才執行參數化 DELETE。
    """
    start_epoch, end_epoch = _date_range_to_epochs(from_date, to_date)
    result_range = {"from": from_date, "to": to_date}

    if not force:
        event_count = count_events_in_range(db_path, from_date, to_date)
        return {"would_delete": event_count, "range": result_range}

    _ensure_force_user_allowed()
    event_count = count_events_in_range(db_path, from_date, to_date)
    if event_count == 0:
        return {"deleted": 0, "archive_path": None, "range": result_range}

    resolved_archive_dir = archive_dir or str(Path(db_path).parent / "archive")

    # archive 寫入（包含 fsync + atomic replace）成功後，才可以進入 DELETE。
    archive_path = archive_events_in_range(
        db_path,
        from_date,
        to_date,
        resolved_archive_dir,
    )

    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            f"DELETE FROM session_events {_RANGE_SQL}",
            (start_epoch, end_epoch),
        )
        conn.commit()
        deleted = cursor.rowcount
    finally:
        conn.close()

    return {"deleted": deleted, "archive_path": archive_path, "range": result_range}


def _connect(db_path: str) -> sqlite3.Connection:
    """建立與 EventStore 相同 WAL 慣例的獨立 SQLite 連線。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _date_range_to_epochs(from_date: str, to_date: str) -> tuple[float, float]:
    """將 local-time 日期區間換算為 [from, to+1) 的 epoch。"""
    start_date = date.fromisoformat(from_date)
    end_date = date.fromisoformat(to_date)
    if end_date < start_date:
        raise ValueError("to_date must not be earlier than from_date")

    start = datetime.combine(start_date, datetime_time.min)
    end = datetime.combine(end_date + timedelta(days=1), datetime_time.min)
    return start.timestamp(), end.timestamp()


def _write_archive(
    rows: list[sqlite3.Row],
    from_date: str,
    to_date: str,
    archive_dir: str,
) -> str:
    """將 rows 完整寫入並 fsync 後，以原子 replace 發布 archive 檔。"""
    target_dir = Path(archive_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"purged_{from_date}_to_{to_date}_{time.time_ns()}.jsonl"
    archive_path = target_dir / filename
    temporary_path = target_dir / f".{filename}.tmp"

    try:
        with temporary_path.open("x", encoding="utf-8") as archive_file:
            for row in rows:
                event = dict(row)
                event["payload"] = json.loads(event["payload"])
                archive_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            archive_file.flush()
            os.fsync(archive_file.fileno())
        temporary_path.replace(archive_path)
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        raise

    return str(archive_path)


def _ensure_force_user_allowed() -> None:
    """確認 force 操作的本機帳號符合允許帳號。"""
    current_user = getpass.getuser()
    allowed_user = os.environ.get("TERMINUX_PURGE_ALLOW_USER", current_user)
    if current_user != allowed_user:
        raise PermissionError(
            "purge --force is only allowed for "
            f"TERMINUX_PURGE_ALLOW_USER={allowed_user!r}; current user is {current_user!r}"
        )
