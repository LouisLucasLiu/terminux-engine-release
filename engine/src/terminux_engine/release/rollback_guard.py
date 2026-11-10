# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""發布前的唯讀資料相容檢查；此工具不切換 binary 或還原資料。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SUPPORTED_WORK_SCHEMAS = frozenset({"engineering_work.v1"})
SUPPORTED_FLOW_TASK_SCHEMAS = frozenset({1, 2})
SUPPORTED_ANALYSIS_TASK_SCHEMAS = frozenset({1, 2})


def inspect_event_store(path: Path) -> dict[str, Any]:
    """以 SQLite 唯讀交易核對 schema、歷史事件及單調用量。"""
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("EventStore must be a regular file")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT event_id, event_type, payload FROM session_events ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()

    reasons: list[dict[str, str]] = []
    seal = hashlib.sha256()
    work_rows: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    task_count = 0
    for event_id, event_type, raw_payload in rows:
        seal.update(json.dumps([event_id, event_type, raw_payload],
                               ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            reasons.append({"code": "EVENT_PAYLOAD_INVALID", "event_id": str(event_id)})
            continue
        if not isinstance(payload, dict):
            reasons.append({"code": "EVENT_PAYLOAD_INVALID", "event_id": str(event_id)})
            continue
        if event_type == "engineering_work.event.v1":
            work = payload.get("snapshot")
            if not isinstance(work, dict):
                reasons.append({"code": "WORK_SNAPSHOT_INVALID", "event_id": str(event_id)})
                continue
            if work.get("schema_version") not in SUPPORTED_WORK_SCHEMAS:
                reasons.append({"code": "WORK_SCHEMA_UNSUPPORTED", "event_id": str(event_id),
                                "stored_schema": str(work.get("schema_version"))})
            work_id = work.get("work_id")
            if isinstance(work_id, str):
                sequence = payload.get("sequence", work.get("revision", 0))
                if not isinstance(sequence, int):
                    reasons.append({"code": "WORK_SEQUENCE_INVALID", "event_id": str(event_id)})
                    continue
                work_rows.setdefault(work_id, []).append((sequence, work))
        elif event_type in {"flow_task.snapshot.v1", "analysis_task.snapshot.v1"}:
            task_count += 1
            snapshot = payload.get(
                "task" if event_type.startswith("flow_task.") else "snapshot")
            supported = (SUPPORTED_FLOW_TASK_SCHEMAS if event_type.startswith("flow_task.")
                         else SUPPORTED_ANALYSIS_TASK_SCHEMAS)
            if not isinstance(snapshot, dict) or snapshot.get("schema_version") not in supported:
                reasons.append({"code": "TASK_SCHEMA_UNSUPPORTED", "event_id": str(event_id),
                                "stored_schema": str((snapshot or {}).get("schema_version")
                                                     if isinstance(snapshot, dict) else None)})
    for work_id, versions in work_rows.items():
        prior: dict[str, int] = {}
        for _sequence, work in sorted(versions, key=lambda item: item[0]):
            used = (work.get("budget") or {}).get("used") or {}
            if not isinstance(used, dict):
                reasons.append({"code": "WORK_USAGE_INVALID", "work_id": work_id})
                continue
            for key, value in used.items():
                if not isinstance(value, int) or value < prior.get(key, 0):
                    reasons.append({"code": "WORK_USAGE_REGRESSED", "work_id": work_id,
                                    "usage_key": str(key)})
                elif value >= 0:
                    prior[key] = value
    return {
        "schema_version": "terminux.rollback_preflight.v1",
        "status": "DATA_COMPATIBLE_ONLY" if not reasons else "REJECTED",
        "event_count": len(rows), "event_seal_sha256": seal.hexdigest(),
        "work_count": len(work_rows), "task_snapshot_count": task_count,
        "supported": {"work": sorted(SUPPORTED_WORK_SCHEMAS),
                      "flow_task": sorted(SUPPORTED_FLOW_TASK_SCHEMAS),
                      "analysis_task": sorted(SUPPORTED_ANALYSIS_TASK_SCHEMAS)},
        "reasons": reasons,
        "limitations": [
            "唯讀資料相容不等於舊 binary 的執行相容；回退前仍須核對目標產物版本與 child 終態。",
            "本工具不刪事件、不重置用量、不建立授權，亦不切換執行檔。",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(description="唯讀 EventStore 回退相容預檢")
    parser.add_argument("--event-store", required=True, type=Path)
    parser.add_argument("--agent-socket", required=True, type=Path)
    parser.add_argument("--lineage-socket", required=True, type=Path)
    args = parser.parse_args(argv)
    from terminux_engine.ipc.daemon_owner import check_local_database_access
    owner_check = check_local_database_access()
    if owner_check:
        return owner_check
    try:
        from terminux_engine.release.cold_backup import _require_stopped

        _require_stopped((args.agent_socket, args.lineage_socket))
        result = inspect_event_store(args.event_store)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {"schema_version": "terminux.rollback_preflight.v1",
                  "status": "REJECTED", "reasons": [{"code": "STORE_UNREADABLE",
                                                "error_type": type(exc).__name__}]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "DATA_COMPATIBLE_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
