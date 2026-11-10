# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Run selector 與 DQE 讀取。

只做兩件事：把 `run_id`（或 latest verified selector）解析成一次確定的
DQE run，並把該 run 的 DQE rows 讀回來。**不掃 filesystem、不猜路徑**
。

Run 的挑選一律走正式欄位（`orchestration_run_id`／`DATE_TIME`），不靠 mtime。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from terminux_engine.dqe.db import DqeDbManager
from terminux_engine.dqe.db_policy import (
    resolve_dqe_db_manager,
    resolve_dqe_db_manager_for_run,
)
from terminux_engine.package_runtime.context import PackageContext

DQE_PACKAGE_ID = "orfs_dqe"

#: Kernel 產生的 run id 格式：`run_` ＋ 32 個 hex。
RUN_ID_PATTERN = re.compile(r"^run_[0-9a-f]{32}$")

RESOLVED_EXACT = "exact_run_id"
RESOLVED_LATEST = "latest_verified_run"
#: host 有 Evidence Resolver 時的 latest-run 標示。前者是目前使用者自己
#: 有執行證據的最新 run；後者是找不到任何執行證據時退回的 DQE 最新一筆，只能做唯讀診斷。
RESOLVED_LATEST_EXECUTION = "latest_execution_evidence_run"
RESOLVED_LATEST_DQE_ONLY = "latest_dqe_run_without_execution_evidence"


class RunSelectionError(RuntimeError):
    """找不到符合條件的 run，或 `run_id` 格式不合法。"""


@dataclass(frozen=True)
class DqeRun:
    """一次已完成、已持久化 DQE 的 ORFS run。"""

    run_id: str
    step_id: str | None
    attempt: int | None
    manifest_reference: str | None
    resolved_by: str
    rows: list[dict[str, Any]]
    #: 這些 rows 實際來自的 DQE DB。供 host 的 Evidence Resolver 讀同一份 DB。
    db_path: str | None = None

    @property
    def overall_status(self) -> str:
        """由 per-check semantic status 重算 overall（與 policy 同一規則）。

        刻意重算而不是信任某一列：DQE DB 保存的是 per-check 結果，overall
        不是 DB 欄位。規則與 `orfs_dqe` 的 aggregation 一致。
        """
        statuses = {row["semantic_status"] for row in self.rows}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        if "unknown" in statuses or "legacy_ambiguous" in statuses:
            return "unknown"
        return "pass" if statuses else "unknown"

    @property
    def has_ambiguous_legacy_rows(self) -> bool:
        return any(row["semantic_status_source"] == "legacy_ambiguous" for row in self.rows)


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise RunSelectionError(
            f"run_id must match {RUN_ID_PATTERN.pattern!r}; got {run_id!r}"
        )
    return run_id


def resolve_run(ctx: PackageContext, run_id: str | None) -> DqeRun:
    """解析 target run。`run_id=None` → 最新一次有 DQE 的 run。

    當 run_id 對應的是 imported baseline run 時，
    ``resolve_dqe_db_manager_for_run`` 會由
    ``FlowBindingContext.imported_run_dqe_overrides`` 導向來源 DQE DB；
    其他 run 仍走標準五層優先序。
    """
    # Per-run 解析：imported baseline run → 來源 DB；其他 → 目標 DB。
    manager = resolve_dqe_db_manager_for_run(ctx, run_id=run_id)
    if run_id is not None:
        resolved = validate_run_id(run_id)
        # 明確指名一筆已作廢的 run 也要擋——不能靠「沒被 latest 選中」代替。
        from terminux_engine.evidence_integrity.invalidation import (
            load_invalidation_set,
        )

        if load_invalidation_set().run_invalidated(resolved):
            raise RunSelectionError(
                f"run {resolved} was invalidated by an evidence invalidation "
                f"record and cannot be used as evidence")
        resolved_by = RESOLVED_EXACT
    else:
        resolved = _latest_run_id(manager)
        if resolved is None:
            raise RunSelectionError("no ORFS DQE run has been recorded yet")
        resolved_by = RESOLVED_LATEST

    rows = manager.read_records_by_orchestration_run(resolved, package_id=DQE_PACKAGE_ID)
    if not rows:
        raise RunSelectionError(f"no {DQE_PACKAGE_ID} rows recorded for run {resolved}")

    first = rows[0]
    return DqeRun(
        run_id=resolved,
        step_id=first.get("step_id"),
        attempt=first.get("attempt"),
        manifest_reference=first.get("PATH"),
        resolved_by=resolved_by,
        rows=rows,
        db_path=str(manager._db_path),  # noqa: SLF001 - manager 未公開 path getter
    )


def dqe_db_path_for(ctx: PackageContext, run_id: str | None) -> str:
    """`resolve_run` 會使用的 DQE DB 路徑（同一套解析規則）。"""
    manager = resolve_dqe_db_manager_for_run(ctx, run_id=run_id)
    return str(manager._db_path)  # noqa: SLF001 - manager 未公開 path getter


def _latest_run_id(manager: DqeDbManager) -> str | None:
    """以正式欄位挑最新的 run——`DATE_TIME` 最大者，不看檔案 mtime。

    **預設跳過已作廢的 run**。作廢的 row 仍留在表裡，但 latest-run
    selection 不得挑中它——否則一次 stub／污染寫入就會讓「最新 run」指向
    一筆不成立的執行。所以這裡往下找第一個未作廢的 run，而不是只看第一列。
    """
    from terminux_engine.evidence_integrity.invalidation import load_invalidation_set

    db_path = manager._db_path  # noqa: SLF001 - manager 未公開 path getter
    if not db_path.exists():
        return None
    invalidated = load_invalidation_set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT orchestration_run_id, MAX(DATE_TIME) AS dt, MAX(id) AS rid "
            "FROM Version_Dqe_Tab "
            "WHERE package_id = ? AND orchestration_run_id IS NOT NULL "
            "GROUP BY orchestration_run_id ORDER BY dt DESC, rid DESC",
            (DQE_PACKAGE_ID,),
        ).fetchall()
    finally:
        conn.close()
    for run_id, _dt, _rid in rows:
        if not invalidated.run_invalidated(run_id):
            return run_id
    return None


__all__ = [
    "DQE_PACKAGE_ID",
    "DqeRun",
    "RESOLVED_EXACT",
    "RESOLVED_LATEST",
    "RESOLVED_LATEST_DQE_ONLY",
    "RESOLVED_LATEST_EXECUTION",
    "RUN_ID_PATTERN",
    "RunSelectionError",
    "resolve_run",
    "dqe_db_path_for",
    "validate_run_id",
]
