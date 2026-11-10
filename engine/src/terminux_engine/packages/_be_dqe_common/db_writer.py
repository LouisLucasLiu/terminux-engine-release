# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""BE DQE package 共用的 DQE DB 寫入邏輯。

【架構註記】本模組讓 DQE package 直接寫入 DQE DB（SQLite），這是沿用
dc_shell_dqe 的既有 warn-only 例外。寫入失敗只記 warning 並回傳 failed receipt，
不得影響 package 的 EventStore record 與通知。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Literal

from terminux_engine.dqe.db import DqeDbManager, DqeRecord
from terminux_engine.dqe.db_policy import resolve_dqe_db_manager
from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.result import TimelineEvent

_LOG = logging.getLogger(__name__)
_NA = "_NA_"


@dataclass(frozen=True)
class DqeBatchWriteReceipt:
    """DQE batch 寫入的結構化 receipt，供 package after phase 攜帶到 evidence。"""

    status: Literal["written", "ignored", "failed"]
    job_key: str | None = None
    rows_written: int = 0
    rows_attempted: int = 0
    error: str | None = None


def resolve_db_manager(ctx: PackageContext) -> DqeDbManager:
    """委派給 canonical resolver（`terminux_engine.dqe.db_policy`）。

    五層優先序與既有行為完全不變；差別只在於缺少前四層來源時，第五層的
    production 預設 manager 是否可用改由 `TERMINUX_DQE_DEFAULT_DB_POLICY`
    決定（未設定時仍是 `allow`，行為與修改前相同）。呼叫方
    `write_dqe_batch()` 對所有 exception（含新的 `DqeDbIsolationError`）
    一律 warn-only 吞掉、回傳 `failed` receipt，不改變既有錯誤處理策略。
    """
    return resolve_dqe_db_manager(ctx)


def extract_eco_tags(ctx: PackageContext) -> tuple[str | None, int | None]:
    """從 event metadata 取出可選 ECO 標籤。

    Kernel 路徑使用 ``be_eco`` namespace；既有 daemon 手動路徑仍使用頂層欄位。
    """
    metadata = ctx.event.metadata or {}
    raw_be_eco = metadata.get("be_eco") or {}
    be_eco = raw_be_eco if isinstance(raw_be_eco, dict) else {}
    run_id = be_eco.get("eco_run_id") or metadata.get("eco_run_id")
    iteration = (
        be_eco["eco_iteration"]
        if "eco_iteration" in be_eco
        else metadata.get("eco_iteration")
    )
    return (
        str(run_id) if run_id else None,
        int(iteration) if iteration is not None else None,
    )


def extract_orchestration_metadata(event: TimelineEvent) -> dict[str, str | int | None]:
    """從保留的 orchestration metadata namespace 抽取 correlation 資訊。"""
    raw = event.metadata.get("orchestration") or {}
    orchestration = raw if isinstance(raw, dict) else {}
    attempt = orchestration.get("attempt")
    return {
        "orchestration_run_id": _optional_text(orchestration.get("run_id")),
        "step_id": _optional_text(orchestration.get("step_id")),
        "attempt": int(attempt) if attempt is not None else None,
        "dispatch_key": _optional_text(orchestration.get("dispatch_key")),
    }


def receipt_to_evidence(receipt: DqeBatchWriteReceipt) -> dict[str, Any]:
    """將 receipt 轉為 PhaseResult evidence 可直接攜帶的資料。"""
    evidence: dict[str, Any] = {
        "status": receipt.status,
        "rows_written": receipt.rows_written,
        "rows_attempted": receipt.rows_attempted,
        "job_key": receipt.job_key,
    }
    if receipt.error:
        evidence["error"] = receipt.error
    return evidence


def checks_to_dqe_records(
    checks: list[dict[str, Any]],
    *,
    report_path: str | None = None,
    orchestration_run_id: str | None = None,
    step_id: str | None = None,
    attempt: int | None = None,
    dispatch_key: str | None = None,
    source_event_id: str | None = None,
    package_id: str | None = None,
) -> list[DqeRecord]:
    """將 package check dict 轉為 DQE DB record。"""
    return [
        DqeRecord(
            dqe_id=str(check["id"]),
            value=str(check.get("value", "")),
            unit=str(check.get("unit", "")),
            status=str(check["status"]),
            reporter="",
            path=report_path,
            orchestration_run_id=orchestration_run_id,
            step_id=step_id,
            attempt=attempt,
            dispatch_key=dispatch_key,
            source_event_id=source_event_id,
            package_id=package_id,
            # package 若提供 exact semantic status
            # （`pass`／`warn`／`fail`／`unknown`）就一併持久化；沒提供時
            # 維持 None，既有 package 行為完全不變。
            semantic_status=(
                str(check["semantic_status"]) if check.get("semantic_status") else None
            ),
        )
        for check in checks
    ]


def write_dqe_batch(
    ctx: PackageContext,
    checks: list[dict[str, Any]],
    *,
    flow: str,
    dqe_meta: dict[str, dict[str, str]],
    report_path: str | None = None,
    orchestration_run_id: str | None = None,
    step_id: str | None = None,
    attempt: int | None = None,
    dispatch_key: str | None = None,
    source_event_id: str | None = None,
    package_id: str | None = None,
) -> DqeBatchWriteReceipt:
    """以共用 DQE DB writer 寫入 checks 與 metadata，失敗時回 failed receipt。"""
    rows_attempted = len(checks)
    try:
        manager = resolve_db_manager(ctx)
        eco_run_id, eco_iteration = extract_eco_tags(ctx)
        for check in checks:
            meta = dqe_meta.get(str(check["id"]), {})
            manager.upsert_dqe_meta(
                str(check["id"]),
                meta.get("title", str(check.get("title", check["id"]))),
                meta.get("description", ""),
                meta.get("reference", ""),
            )
        job_key, rows_written = manager.store_dqe_batch_with_result(
            checks_to_dqe_records(
                checks,
                report_path=report_path,
                orchestration_run_id=orchestration_run_id,
                step_id=step_id,
                attempt=attempt,
                dispatch_key=_optional_text(dispatch_key),
                source_event_id=source_event_id,
                package_id=package_id,
            ),
            block=str(ctx.event.metadata.get("block_name", _NA)),
            flow=flow,
            eco_run_id=eco_run_id,
            eco_iteration=eco_iteration,
        )
        if rows_written == 0 and rows_attempted > 0:
            return DqeBatchWriteReceipt(
                status="ignored",
                job_key=job_key,
                rows_attempted=rows_attempted,
            )
        return DqeBatchWriteReceipt(
            status="written",
            job_key=job_key,
            rows_written=rows_written,
            rows_attempted=rows_attempted,
        )
    except Exception as error:
        _LOG.warning("BE DQE DB write failed: %s", error)
        return DqeBatchWriteReceipt(
            status="failed",
            rows_attempted=rows_attempted,
            error=repr(error),
        )


def _optional_text(value: Any) -> str | None:
    """將缺席或空字串 correlation 值正規化為 SQLite NULL。"""
    if value is None:
        return None
    text = str(value)
    return text if text else None
