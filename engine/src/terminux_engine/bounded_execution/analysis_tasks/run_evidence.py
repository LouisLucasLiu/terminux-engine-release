# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""新 run 的完成證據驗證。

前段能否進入後段，取決於這裡的判定。規則刻意比既有 `Barrier` 嚴格：

- **單純存在 `dqe_summary.rpt` 不足**。
- `dqe_write=ignored` **只有**在能證明是同一次 dispatch 已成功寫入時才可用；
  預設不把 `ignored` 當成完成。
- 不輪詢到 DQE rows 就當成功：必須綁得回同一個 run／attempt／dispatch_key。

它只**讀**：ledger（lifecycle authority）、receipt projection（execution
evidence）、DQE DB（分析輸入）。不寫任何修復資料，也不改變 run 狀態。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: ledger 的終態集合（與 `orchestration.ledger._TERMINAL_RUN_STATES` 對齊）。
TERMINAL_RUN_STATES = frozenset({
    "completed", "failed", "timeout", "cancelled", "abandoned",
})
SUCCESS_RUN_STATE = "completed"

READY = "ready"
NOT_READY = "not_ready"
FAILED = "failed"
UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class RunEvidence:
    """一次 run 的完成證據判定。"""

    state: str
    reason: str
    run_id: str
    slot: str
    run_status: str | None = None
    terminal_reason: str | None = None
    process_exit_code: int | None = None
    timed_out: bool | None = None
    receipt_id: str | None = None
    receipt_content_hash: str | None = None
    artifact_manifest_id: str | None = None
    artifact_manifest_sha256: str | None = None
    dispatch_status: str | None = None
    evidence_status: str | None = None
    dqe_row_count: int = 0
    dqe_dispatch_key: str | None = None
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == READY

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "state": self.state, "reason": self.reason, "run_id": self.run_id,
            "slot": self.slot, "run_status": self.run_status,
            "terminal_reason": self.terminal_reason,
            "process_exit_code": self.process_exit_code,
            "timed_out": self.timed_out, "receipt_id": self.receipt_id,
            "receipt_content_hash": self.receipt_content_hash,
            "artifact_manifest_id": self.artifact_manifest_id,
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "dispatch_status": self.dispatch_status,
            "evidence_status": self.evidence_status,
            "dqe_row_count": self.dqe_row_count,
            "dqe_dispatch_key": self.dqe_dispatch_key,
            "missing": list(self.missing), "details": dict(self.details),
        }


def _dqe_rows_for_run(dqe_db_path: str, run_id: str) -> list[dict[str, Any]]:
    """唯讀查這個 run 的 DQE rows。查不到就是 0 筆，不猜。"""
    path = Path(dqe_db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM Version_Dqe_Tab WHERE orchestration_run_id = ?", (run_id,))
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def evaluate_run_evidence(
    *,
    run_id: str,
    slot: str,
    ledger: Any,
    receipt_reader: Any,
    dqe_db_path: str,
    expected_task_id: str,
    expected_slot: str,
    parameter_name: str | None = None,
    expected_parameter_value: str | None = None,
    effective_settings: Any = None,
    consumption: Any = None,
) -> RunEvidence:
    """判定這個 run 是否已提供完整的完成證據。

    順序刻意：先看 lifecycle（ledger），再看 execution evidence（receipt），
    再看分析輸入（DQE），**最後看設定的來源與實際使用**。任何一層不成立就
    停在該層，不往下推論。

    最後一層是 flow task 的明確要求：`make print-%` 回報的值只是**有效值
    線索**，單靠它不能證明值來自核准的 config，也不能證明 floorplan 真的
    消費了它。因此給了 `expected_parameter_value` 時，下列兩者都必須成立才
    會是 `ready`：

    - `effective_settings` 有可辨識來源，且該參數的有效值等於預期值；
    - `consumption` 證明工具**實際收到並使用**了這個值。

    兩者都由本 slot 專屬的輸出區取得，因此天然綁定同一個 run／attempt。
    """
    run = None
    try:
        run = ledger.get_run(run_id)
    except Exception:  # noqa: BLE001 - 讀不到就是未確認
        return RunEvidence(state=UNCONFIRMED, reason="ledger_unreadable",
                           run_id=run_id, slot=slot)
    if run is None:
        return RunEvidence(state=UNCONFIRMED, reason="run_not_in_ledger",
                           run_id=run_id, slot=slot)

    status = getattr(run, "status", None)
    terminal_reason = getattr(run, "terminal_reason", None)

    # run 必須確實屬於這個 task 的這個 slot——不接受別的 run 冒充。
    metadata = getattr(run, "metadata", None)
    agent_task = metadata.get("agent_task") if isinstance(metadata, dict) else None
    if not isinstance(agent_task, dict):
        return RunEvidence(state=FAILED, reason="run_is_not_task_owned",
                           run_id=run_id, slot=slot, run_status=status,
                           terminal_reason=terminal_reason)
    if (agent_task.get("task_id") != expected_task_id
            or agent_task.get("slot") != expected_slot):
        return RunEvidence(state=FAILED, reason="run_belongs_to_another_task_or_slot",
                           run_id=run_id, slot=slot, run_status=status,
                           terminal_reason=terminal_reason)

    if status not in TERMINAL_RUN_STATES:
        return RunEvidence(state=NOT_READY, reason="run_still_active",
                           run_id=run_id, slot=slot, run_status=status,
                           terminal_reason=terminal_reason)
    if status != SUCCESS_RUN_STATE:
        return RunEvidence(state=FAILED, reason=f"run_terminal_status_{status}",
                           run_id=run_id, slot=slot, run_status=status,
                           terminal_reason=terminal_reason)

    # ── execution evidence：durable receipt projection ──
    receipts: list[dict[str, Any]] = []
    if receipt_reader is not None:
        try:
            receipts = list(receipt_reader(run_id))
        except Exception:  # noqa: BLE001
            receipts = []
    if not receipts:
        return RunEvidence(state=UNCONFIRMED, reason="no_execution_receipt",
                           run_id=run_id, slot=slot, run_status=status,
                           terminal_reason=terminal_reason,
                           missing=("execution_receipt",))
    latest = receipts[-1]
    exit_code = latest.get("process_exit_code")
    timed_out = latest.get("timed_out")
    dispatch_status = latest.get("dispatch_status")
    evidence_status = latest.get("evidence_status")

    missing: list[str] = []
    if latest.get("artifact_manifest_id") is None:
        missing.append("artifact_manifest")
    if exit_code != 0:
        return RunEvidence(
            state=FAILED, reason="process_exit_code_not_zero", run_id=run_id,
            slot=slot, run_status=status, terminal_reason=terminal_reason,
            process_exit_code=exit_code, timed_out=timed_out,
            receipt_id=latest.get("receipt_id"),
            receipt_content_hash=latest.get("content_hash"),
            dispatch_status=dispatch_status, evidence_status=evidence_status)
    if timed_out:
        return RunEvidence(
            state=UNCONFIRMED, reason="execution_timed_out", run_id=run_id,
            slot=slot, run_status=status, terminal_reason=terminal_reason,
            process_exit_code=exit_code, timed_out=True,
            receipt_id=latest.get("receipt_id"),
            dispatch_status=dispatch_status, evidence_status=evidence_status)

    # ── DQE 寫入關聯：必須綁得回同一個 run ──
    rows = _dqe_rows_for_run(dqe_db_path, run_id)
    if not rows:
        return RunEvidence(
            state=NOT_READY, reason="no_dqe_rows_for_this_run", run_id=run_id,
            slot=slot, run_status=status, terminal_reason=terminal_reason,
            process_exit_code=exit_code, timed_out=timed_out,
            receipt_id=latest.get("receipt_id"),
            receipt_content_hash=latest.get("content_hash"),
            artifact_manifest_id=latest.get("artifact_manifest_id"),
            artifact_manifest_sha256=latest.get("artifact_manifest_sha256"),
            dispatch_status=dispatch_status, evidence_status=evidence_status,
            missing=tuple(missing + ["dqe_rows"]))

    dispatch_keys = {str(r.get("dispatch_key")) for r in rows if r.get("dispatch_key")}
    if not dispatch_keys:
        missing.append("dqe_dispatch_key")

    # 必要 WNS/TNS **與單位**必須可讀。缺就是證據不足，不用 indeterminate
    # 冒充有效比較。metric 識別欄位是 `DQE_ID`（實際 schema）。
    metrics = {str(r.get("DQE_ID") or ""): r for r in rows}
    timing = {name: row for name, row in metrics.items()
              if "wns" in name.lower() or "tns" in name.lower()}
    if not timing:
        missing.append("timing_metrics")
    else:
        unreadable = [
            name for name, row in timing.items()
            if not str(row.get("VALUE") or "").strip()
            or not str(row.get("UNIT") or "").strip()
        ]
        if unreadable:
            missing.append(f"timing_metric_units:{sorted(unreadable)}")

    # ── 設定的來源與實際使用 ──
    settings_view: dict[str, Any] | None = None
    consumption_view: dict[str, Any] | None = None
    if expected_parameter_value is not None:
        name = parameter_name or ""
        if effective_settings is None:
            missing.append("effective_settings")
        else:
            settings_view = (effective_settings.to_json_dict()
                             if hasattr(effective_settings, "to_json_dict")
                             else dict(effective_settings))
            source = str(settings_view.get("source") or "unavailable")
            actual = (settings_view.get("values") or {}).get(name)
            if source == "unavailable":
                missing.append("effective_settings_source")
            elif actual != expected_parameter_value:
                return RunEvidence(
                    state=FAILED, reason="effective_value_mismatch", run_id=run_id,
                    slot=slot, run_status=status, terminal_reason=terminal_reason,
                    process_exit_code=exit_code, timed_out=timed_out,
                    receipt_id=latest.get("receipt_id"),
                    dqe_row_count=len(rows),
                    details={"parameter": name, "expected": expected_parameter_value,
                             "effective": actual, "settings_source": source})
        if consumption is None:
            missing.append("parameter_consumption")
        else:
            consumption_view = (consumption.to_json_dict()
                                if hasattr(consumption, "to_json_dict")
                                else dict(consumption))
            if not consumption_view.get("consumed"):
                return RunEvidence(
                    state=FAILED, reason="parameter_not_consumed", run_id=run_id,
                    slot=slot, run_status=status, terminal_reason=terminal_reason,
                    process_exit_code=exit_code, timed_out=timed_out,
                    receipt_id=latest.get("receipt_id"), dqe_row_count=len(rows),
                    details={"consumption": consumption_view})

    if missing:
        return RunEvidence(
            state=NOT_READY, reason="evidence_incomplete", run_id=run_id,
            slot=slot, run_status=status, terminal_reason=terminal_reason,
            process_exit_code=exit_code, timed_out=timed_out,
            receipt_id=latest.get("receipt_id"),
            receipt_content_hash=latest.get("content_hash"),
            artifact_manifest_id=latest.get("artifact_manifest_id"),
            artifact_manifest_sha256=latest.get("artifact_manifest_sha256"),
            dispatch_status=dispatch_status, evidence_status=evidence_status,
            dqe_row_count=len(rows),
            dqe_dispatch_key=sorted(dispatch_keys)[0] if dispatch_keys else None,
            missing=tuple(missing))

    return RunEvidence(
        state=READY, reason="complete", run_id=run_id, slot=slot,
        run_status=status, terminal_reason=terminal_reason,
        process_exit_code=exit_code, timed_out=timed_out,
        receipt_id=latest.get("receipt_id"),
        receipt_content_hash=latest.get("content_hash"),
        artifact_manifest_id=latest.get("artifact_manifest_id"),
        artifact_manifest_sha256=latest.get("artifact_manifest_sha256"),
        dispatch_status=dispatch_status, evidence_status=evidence_status,
        dqe_row_count=len(rows),
        dqe_dispatch_key=sorted(dispatch_keys)[0],
        details={
            "metric_names": sorted(m for m in metrics if m)[:20],
            "effective_settings": settings_view,
            "parameter_consumption": consumption_view,
            "timing_metrics": {
                name: {"value": row.get("VALUE"), "unit": row.get("UNIT"),
                       "semantic_status": row.get("semantic_status")}
                for name, row in sorted(timing.items())
            },
        },
    )


__all__ = [
    "FAILED", "NOT_READY", "READY", "SUCCESS_RUN_STATE", "TERMINAL_RUN_STATES",
    "UNCONFIRMED", "RunEvidence", "evaluate_run_evidence",
]
