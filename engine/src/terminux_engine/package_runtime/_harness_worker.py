# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Local Test Harness v1 — disposable child worker。

只做一件事：讀 spec.json，用 production `PackageLoader`／
`PackageRegistry`／`PackageDispatcher`／`DispatchEngine`（STANDALONE
mode）對一個 disposable event 執行一次 after phase dispatch，把結構化
執行事實寫進 result.json。**不做任何 assertion 判斷**——assertion 全部
留在 parent（`test_harness.py::evaluate_case()`），維持 worker 極簡、
容易被獨立驗證。

執行環境由 parent 透過 `subprocess.run(env=...)` 最小化，本檔案不依賴任何 production-only 環境變數。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from terminux_engine.dqe.db import DqeDbManager
from terminux_engine.orchestration.dispatch_engine import DispatchEngine, EngineMode
from terminux_engine.package_runtime.dispatcher import PackageDispatcher
from terminux_engine.package_runtime.loader import PackageLoader
from terminux_engine.package_runtime.registry import PackageRegistry
from terminux_engine.package_runtime.result import TimelineEvent
from terminux_engine.package_runtime.services import (
    FileService,
    LlmService,
    NotifyService,
    ReportService,
    RuntimeHelpers,
    RuntimeServices,
    StoreService,
)
from terminux_engine.store.event_store import EventStore


def _build_event(spec_event: dict[str, Any]) -> TimelineEvent:
    return TimelineEvent(
        event_id=spec_event["event_id"],
        trace_id=None,
        command=spec_event["command"],
        argv=list(spec_event["argv"]),
        cwd=spec_event["cwd"],
        tool_name=spec_event["tool_name"],
        user=spec_event["user"],
        host=spec_event["host"],
        terminal_id=spec_event["terminal_id"],
        source=spec_event["source"],
        timestamp=spec_event["timestamp"],
        exit_code=spec_event.get("exit_code"),
        stdout_tail=list(spec_event.get("stdout_tail") or []),
        metadata=dict(spec_event.get("metadata") or {}),
    )


def run_worker(spec_path: Path, result_path: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    package_dir = Path(spec["package_dir"])
    package_id = spec["package_id"]
    store_db = Path(spec["store_db"])

    package = PackageLoader().load(package_dir)
    registry = PackageRegistry([package])
    dispatcher = PackageDispatcher(registry)

    event_store = EventStore(store_db)
    try:
        store = StoreService(
            event_store,
            session_id="test",
            terminal_id="package-test",
            user_id="package-test",
            project_id=None,
            package_id=None,
        )
        services = RuntimeServices(
            store=store,
            notify=NotifyService(),
            report=ReportService(),
            file=FileService(),
            llm=LlmService(router=None),
        )
        # DQE Test Isolation Enforcement v1：無條件注入 per-case disposable
        # DQE DB manager，不判斷 package 是否真的會寫 DQE DB——這是
        # resolve_dqe_db_manager() 五層優先序的最高優先層（services 注入），
        # 讓任何 DQE-writing package 的 case 完全不需要記得提供 dqe_db_path。
        services.dqe_db_manager = DqeDbManager(Path(spec["dqe_db_path"]))
        engine = DispatchEngine(
            dispatcher,
            mode=EngineMode.STANDALONE,
            services=services,
            helpers=RuntimeHelpers(_resolve_fn=None),
        )

        event = _build_event(spec["event"])
        outcome = engine.dispatch_sync(event)

        package_outcome = next(
            (p for p in outcome.packages if p.package_id == package_id), None
        )
        not_dispatched = (
            package_outcome is None and package_id not in outcome.skipped_no_after
        )

        sql_rows = event_store.query(package_id=package_id, order="asc")
        in_memory_records = store.records

        records: list[dict[str, Any]] = []
        for idx, sql_row in enumerate(sql_rows):
            in_memory = in_memory_records[idx] if idx < len(in_memory_records) else None
            records.append({
                "namespace": in_memory.namespace if in_memory is not None else None,
                "record_type": sql_row.event_type,
                "key": in_memory.key if in_memory is not None else None,
                "payload": sql_row.payload,
            })

        notifications = [
            {
                "level": n.level,
                "title": n.title,
                "message": n.message,
                "actions": n.actions,
            }
            for n in services.notify.notifications
        ]
        reports = [
            {
                "report_id": r.report_id,
                "title": r.title,
                "report_path": r.report_path,
                "template": r.template,
                "data": r.data,
                "output_name": r.output_name,
            }
            for r in services.report.reports
        ]

        result: dict[str, Any] = {
            "kind": "ok",
            "not_dispatched": not_dispatched,
            "sql_record_count": len(sql_rows),
            "in_memory_record_count": len(in_memory_records),
            "records": records,
            "notifications": notifications,
            "reports": reports,
        }
        if package_outcome is not None:
            result["phase_status"] = package_outcome.status
            result["message"] = package_outcome.message
            result["facts"] = package_outcome.facts
            result["evidence"] = package_outcome.evidence
            result["error"] = package_outcome.error
        else:
            result["phase_status"] = None
            result["message"] = None
            result["facts"] = {}
            result["evidence"] = {}
            result["error"] = None

        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    finally:
        event_store.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _harness_worker.py <spec.json> <result.json>", file=sys.stderr)
        return 2
    run_worker(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
