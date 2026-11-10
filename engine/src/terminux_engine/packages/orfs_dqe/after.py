# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_dqe` 的 after phase。

Post-exec、warn-only 治理：讀 artifact lineage immutable manifest → deterministic
評估 → 持久化 DQE 結果 → 需要時通知。

**不做**：shell、subprocess、rerun ORFS、修改 design／report、自動修復、
LLM 呼叫、Package action。

**狀態分離**：DQE 的品質判定（`pass/warn/fail/unknown`）
與 Package 本身的執行狀態是兩件事。品質不合格一律回 `PhaseResult.warn`
——`PhaseResult.fail` 只保留給「evaluator 自己失敗」（manifest 不可信、
policy 損壞等），避免把「設計沒過」講成「package 壞掉」。
"""

from __future__ import annotations

import logging
from typing import Any

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.result import (
    NotificationRequest,
    PhaseResult,
    RecordRequest,
)
from terminux_engine.packages._be_dqe_common import db_writer
from terminux_engine.packages.orfs_dqe import evaluator, timing_path
from terminux_engine.packages.orfs_dqe.manifest_input import (
    ManifestInput,
    ManifestInputError,
    ManifestNotApplicable,
    load_manifest_input,
    orchestration_metadata,
)
from terminux_engine.packages.orfs_dqe.models import (
    DQE_NAMESPACE,
    DQE_RECORD_TYPE,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    DqeEvaluation,
)

_LOG = logging.getLogger(__name__)

TOOL_NAME = "orfs"
DQE_FLOW = "orfs_finish"

#: DQE DB 的 status 欄位沿用既有 BE DQE package 的三值慣例
#: （`icc2_shell_dqe`／`ptpx_dqe`：OK／WARNING／ERROR）。
_DB_STATUS = {
    STATUS_PASS: "OK",
    STATUS_WARN: "WARNING",
    STATUS_FAIL: "ERROR",
    STATUS_UNKNOWN: "WARNING",
}


def run(ctx: PackageContext) -> PhaseResult:
    if ctx.event.tool_name != TOOL_NAME:
        return PhaseResult(status="noop", message="Not an orfs event.")

    try:
        manifest_input = load_manifest_input(ctx.event)
    except ManifestNotApplicable as exc:
        # 非 Kernel production event（shell hook 等）：不評估、不通知，
        # 避免對同一次執行產生重複 DQE。
        return PhaseResult(status="noop", message=f"Not a Kernel-managed orfs run: {exc}")
    except ManifestInputError as exc:
        return _evaluator_failure(ctx, f"artifact manifest is unusable: {exc}")

    try:
        evaluation = evaluator.evaluate(
            manifest_input, process_exit_code=ctx.event.exit_code
        )
    except Exception as exc:  # noqa: BLE001 - evaluator 失敗不得偽裝成品質判定
        _LOG.warning("orfs_dqe evaluation failed: %r", exc)
        return _evaluator_failure(ctx, f"DQE evaluation failed: {exc!r}")

    return _emit(ctx, evaluation, manifest_input)


# ── side effects ───────────────────────────────────────────────────────


def _emit(ctx: PackageContext, evaluation: DqeEvaluation,
          manifest_input: ManifestInput) -> PhaseResult:
    orchestration = orchestration_metadata(ctx.event)
    receipt = db_writer.write_dqe_batch(
        ctx,
        _db_checks(evaluation),
        flow=DQE_FLOW,
        dqe_meta=_db_meta(evaluation),
        report_path=evaluation.artifact_manifest["manifest_reference"],
        orchestration_run_id=orchestration.get("run_id"),
        step_id=orchestration.get("step_id"),
        attempt=orchestration.get("attempt"),
        dispatch_key=orchestration.get("dispatch_key"),
        source_event_id=ctx.event.event_id,
        package_id=evaluator.PACKAGE_ID,
    )

    overall = evaluation.overall_status
    counts = evaluation.counts
    payload = evaluation.to_json_dict()
    payload["dqe_write"] = db_writer.receipt_to_evidence(receipt)

    record = RecordRequest(
        namespace=DQE_NAMESPACE,
        record_type=DQE_RECORD_TYPE,
        key=evaluation.evaluation_id,
        payload=payload,
        display_message=(
            f"ORFS DQE {overall}: "
            f"{counts['pass']} pass / {counts['warn']} warn / "
            f"{counts['fail']} fail / {counts['unknown']} unknown"
        ),
        index_tags={
            "manifest_id": evaluation.artifact_manifest["manifest_id"],
            "policy_version": evaluation.policy_version,
            "overall_status": overall,
        },
    )

    # 單一 worst reported setup path 的逐級證據：獨立 record、獨立 key，與 DQE 判定分離。
    # `collect_record` 永不拋出；解析失敗時留下 `status="unavailable"` 的訊號。
    records = [record, timing_path.collect_record(manifest_input, ctx.event)]

    notifications: list[NotificationRequest] = []
    if overall != STATUS_PASS:
        notifications.append(_notification(evaluation))

    # 三個 dict 互相獨立：facts／evidence／record payload 不共享物件。
    facts: dict[str, Any] = {
        "overall_status": overall,
        "counts": dict(counts),
        "evaluation_id": evaluation.evaluation_id,
        "policy_version": evaluation.policy_version,
        "manifest_id": evaluation.artifact_manifest["manifest_id"],
    }
    evidence: dict[str, Any] = {
        # Barrier 既有的 vendor-neutral evidence 契約（見
        # `orchestration/barrier.py::_receipt_status()`）：只有真的持久化
        # 成功才會讓 evidence readiness 成立。
        "dqe_write": db_writer.receipt_to_evidence(receipt),
        "evaluation_id": evaluation.evaluation_id,
        "manifest_id": evaluation.artifact_manifest["manifest_id"],
        "manifest_sha256": evaluation.artifact_manifest["manifest_sha256"],
        "overall_status": overall,
    }

    return PhaseResult(
        status="ok" if overall == STATUS_PASS else "warn",
        message=f"ORFS DQE overall={overall} ({evaluation.evaluation_id})",
        facts=facts,
        evidence=evidence,
        records=records,
        notifications=notifications,
    )


def _evaluator_failure(ctx: PackageContext, message: str) -> PhaseResult:
    """Evaluator 本身失敗：留下可追溯的 failure record ＋ error notification。

    刻意**不**輸出任何 quality judgment——沒有可信輸入時給不出誠實的
    DQE 結果。
    """
    orchestration = orchestration_metadata(ctx.event)
    key = f"orfs-dqe-error:{ctx.event.event_id}"
    return PhaseResult(
        status="fail",
        message=message,
        facts={"overall_status": None, "evaluator_error": message},
        evidence={"evaluator_error": message},
        records=[
            RecordRequest(
                namespace=DQE_NAMESPACE,
                record_type="orfs_dqe_error",
                key=key,
                payload={
                    "schema_version": 1,
                    "record_type": "orfs_dqe_error",
                    "event_id": ctx.event.event_id,
                    "tool_name": ctx.event.tool_name,
                    "orchestration": dict(orchestration),
                    "error": message,
                },
                display_message=f"ORFS DQE could not run: {message}",
            )
        ],
        notifications=[
            NotificationRequest(
                level="error",
                title="ORFS DQE unavailable",
                message=(
                    f"{message} — no design-quality judgment was produced for this run."
                ),
            )
        ],
    )


def _notification(evaluation: DqeEvaluation) -> NotificationRequest:
    counts = evaluation.counts
    overall = evaluation.overall_status
    worst = [
        c for c in evaluation.checks if c.status == STATUS_FAIL
    ] or [c for c in evaluation.checks if c.status == STATUS_WARN] or [
        c for c in evaluation.checks if c.status == STATUS_UNKNOWN
    ]
    highlights = "; ".join(f"{c.check_id}={c.status} ({c.message})" for c in worst[:3])
    level = "error" if overall == STATUS_FAIL else "warning"
    title = (
        "ORFS DQE: insufficient evidence"
        if overall == STATUS_UNKNOWN
        else f"ORFS DQE: {overall}"
    )
    return NotificationRequest(
        level=level,
        title=title,
        message=(
            f"run {evaluation.producer.get('run_id')} / "
            f"step {evaluation.producer.get('step_id')} — "
            f"{counts['fail']} fail, {counts['warn']} warn, "
            f"{counts['unknown']} unknown. {highlights} "
            f"[manifest {evaluation.artifact_manifest['manifest_id']}]"
        ),
    )


def _db_checks(evaluation: DqeEvaluation) -> list[dict[str, Any]]:
    """轉成既有 DQE DB writer 的 check dict（`_be_dqe_common.db_writer`）。"""
    rows: list[dict[str, Any]] = []
    for check in evaluation.checks:
        rows.append(
            {
                "id": check.check_id,
                "title": check.check_id,
                "value": "" if check.observed_value is None else str(check.observed_value),
                "unit": check.unit or "",
                "status": _DB_STATUS[check.status],
                # legacy `status` 把 warn 與 unknown 都壓成
                # WARNING；exact semantic status 另外保存，讓下游能區分。
                "semantic_status": check.status,
            }
        )
    return rows


def _db_meta(evaluation: DqeEvaluation) -> dict[str, dict[str, str]]:
    return {
        check.check_id: {
            "title": check.check_id,
            "description": check.message,
            "reference": evaluation.artifact_manifest["manifest_reference"],
        }
        for check in evaluation.checks
    }


__all__ = ["run"]
