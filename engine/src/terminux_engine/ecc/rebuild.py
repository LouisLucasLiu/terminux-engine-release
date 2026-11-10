# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""rebuild_ecc：從 EventStore 事件流重建 ECCObject。

從 SQLite event stream 重播出等效 ECCObject。
"""

from __future__ import annotations

import time

from terminux_engine.ecc.ecc_object import (
    CommandRecord,
    CornerCoverage,
    ECCObject,
    GateResult,
    MetricEntry,
    make_correlation_key,
)
from terminux_engine.ecc.playbook import Playbook, StageSpec
from terminux_engine.store.event_store import EventStore


def rebuild_ecc(
    event_store: EventStore,
    session_id: str,
    playbook: Playbook,
) -> ECCObject:
    """從 EventStore 事件流重建 ECCObject。

    讀取指定 session_id 的所有事件，按時間順序重播，
    重建出等效的 ECCObject 狀態。

    若 session_id 不存在於 event_store，回傳空殼 ECCObject。
    """
    # 查詢全部事件，按時間升序
    events = event_store.query(session_id=session_id, order="asc")

    # 無事件 → 回傳空殼（保留 session_id）
    if not events:
        now = time.time()
        return ECCObject(
            session_id=session_id,
            created_at=now,
            updated_at=now,
            correlation_key=make_correlation_key(session_id, now),
        )

    first_ts = events[0].timestamp
    ecc = ECCObject(
        session_id=session_id,
        created_at=first_ts,
        updated_at=first_ts,
        correlation_key=make_correlation_key(session_id, first_ts),
        work_directory=events[0].project_id or "",
    )

    # 建立 gate 定義查詢表（填補 metric_name / operator）
    gate_lookup: dict[str, tuple[str, str]] = {}
    for stage_spec in playbook.stage_specs:
        for gate in stage_spec.quality_gates:
            gate_lookup[gate.id] = (gate.metric, gate.operator)

    # 建立 stage spec 查詢表（初始化 corner_coverage）
    stage_spec_lookup: dict[str, StageSpec] = {
        s.id: s for s in playbook.stage_specs
    }

    # 重播事件
    for ev in events:
        p = ev.payload

        if ev.event_type == "command":
            ecc.command_history.append(CommandRecord(
                timestamp=ev.timestamp,
                command=p["command"],
                cwd=p["cwd"],
                tool_name=p.get("tool_name"),
            ))
            if p.get("cwd"):
                ecc.work_directory = p["cwd"]

        elif ev.event_type == "metric":
            ecc.metric_records.append(MetricEntry(
                metric_name=p["metric_name"],
                value=p["value"],
                corner=p.get("corner"),
                timestamp=ev.timestamp,
                source_report=p.get("source", ""),
            ))

        elif ev.event_type == "stage_change":
            new_stage = p["to_stage"]
            ecc.current_stage_id = new_stage
            ecc.stage_entered_at = ev.timestamp
            spec = stage_spec_lookup.get(new_stage)
            if spec is not None:
                ecc.applicable_gate_ids = [g.id for g in spec.quality_gates]
                # 初始化 corner coverage（若 stage 定義了 corner_list）
                if (
                    ecc.corner_coverage is None
                    and spec.required_reports
                    and spec.required_reports[0].corner_list
                ):
                    ecc.corner_coverage = CornerCoverage(
                        required_corners=list(spec.required_reports[0].corner_list),
                    )

        elif ev.event_type == "corner_update":
            passed = p["status"] == "pass"
            if ecc.corner_coverage is not None:
                ecc.corner_coverage.record_corner(p["corner"], passed)

        elif ev.event_type == "gate_eval":
            gate_id = p["gate_id"]
            metric_name, operator = gate_lookup.get(gate_id, ("", ""))
            ecc.gate_results.append(GateResult(
                gate_id=gate_id,
                metric_name=metric_name,
                actual_value=p["actual"],
                threshold=p["threshold"],
                operator=operator,
                passed=p["passed"],
                corner=p.get("corner"),
                timestamp=ev.timestamp,
                stage_id=ecc.current_stage_id or "",
            ))

        # 更新 updated_at
        ecc.updated_at = ev.timestamp
        ecc.correlation_key = make_correlation_key(session_id, ev.timestamp)

    return ecc
