# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Dispatch completion 與 DQE evidence readiness barrier。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from terminux_engine.orchestration.dispatch_engine import DispatchEngine
from terminux_engine.orchestration.outcome import DispatchOutcome, PackageOutcome
from terminux_engine.package_runtime.result import TimelineEvent


@dataclass(frozen=True)
class BarrierResult:
    """單一 barrier 判定的狀態與可觀察細節。"""

    kind: Literal["dispatch", "evidence"]
    state: Literal["completed", "ready", "not_ready", "timeout", "failed"]
    reason: str | None = None
    details: dict[str, Any] | None = None


class Barrier:
    """同步 dispatch 與 evidence barrier。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def wait_dispatch_completion(
        self,
        engine: DispatchEngine,
        event: TimelineEvent,
        *,
        timeout: float,
    ) -> BarrierResult:
        """執行 dispatch 並以 timeout 保護等待邊界。"""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(engine.dispatch_sync, event)
        try:
            outcome = future.result(timeout=timeout)
        except TimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            return BarrierResult(
                kind="dispatch",
                state="timeout",
                reason="dispatch_timeout",
                details={"outcome": None},
            )
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            return BarrierResult(
                kind="dispatch",
                state="failed",
                reason=repr(exc),
                details={"outcome": None},
            )
        executor.shutdown(wait=True)
        return BarrierResult(
            kind="dispatch",
            state="completed",
            details={"outcome": outcome},
        )

    def check_evidence_readiness(
        self,
        *,
        dispatch_outcome: DispatchOutcome,
        expected_packages: list[str],
        cwd: Path,
        timeout: float,
    ) -> BarrierResult:
        """同步判定 expected packages 的 receipt 或 report evidence 是否就緒。"""
        started_at = self._clock()
        outcomes = {outcome.package_id: outcome for outcome in dispatch_outcome.packages}
        ready_packages: list[str] = []
        checks: dict[str, dict[str, Any]] = {}
        report_exists = (cwd / "dqe_summary.rpt").is_file()
        for package_id in expected_packages:
            package_outcome = outcomes.get(package_id)
            if package_outcome is None:
                return self._not_ready(
                    f"expected_package_not_matched:{package_id}", checks
                )
            if package_outcome.status not in {"ok", "warn", "fail"}:
                return self._not_ready(
                    f"expected_package_not_terminal_evidence:{package_id}", checks
                )
            receipt_status = self._receipt_status(package_outcome)
            persisted = receipt_status in {"written", "ignored"}
            checks[package_id] = {
                "status": package_outcome.status,
                "receipt_status": receipt_status,
                "report_exists": report_exists,
            }
            if not persisted and not report_exists:
                return self._not_ready(
                    f"evidence_data_unavailable:{package_id}", checks
                )
            ready_packages.append(package_id)
        if self._clock() - started_at > timeout:
            return BarrierResult(
                kind="evidence",
                state="timeout",
                reason="evidence_timeout",
                details={"checks": checks},
            )
        return BarrierResult(
            kind="evidence",
            state="ready",
            details={"packages": ready_packages, "checks": checks},
        )

    @staticmethod
    def _receipt_status(package_outcome: PackageOutcome) -> str | None:
        raw_receipt = package_outcome.evidence.get("dqe_write")
        receipt = raw_receipt if isinstance(raw_receipt, dict) else {}
        status = receipt.get("status")
        return status if isinstance(status, str) else None

    @staticmethod
    def _not_ready(reason: str, checks: dict[str, dict[str, Any]]) -> BarrierResult:
        return BarrierResult(
            kind="evidence",
            state="not_ready",
            reason=reason,
            details={"checks": checks},
        )
