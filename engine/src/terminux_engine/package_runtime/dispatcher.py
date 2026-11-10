# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package match 與 phase invocation。"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Literal

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.loader import RuntimePackage
from terminux_engine.package_runtime.prompt_phase import run_prompt_phase
from terminux_engine.package_runtime.registry import PackageRegistry
from terminux_engine.package_runtime.result import PhaseResult, TimelineEvent

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawPhaseExecution:
    """一次 phase 呼叫的原始執行事實（不含 side-effect 施加）。"""

    package_id: str
    package_version: str
    started_at: float
    finished_at: float
    result: PhaseResult | None = None
    exception_type: str | None = None
    exception_repr: str | None = None
    non_phase_result_repr: str | None = None

    @property
    def kind(self) -> Literal["result", "exception", "non_phase_result"]:
        if self.result is not None:
            return "result"
        if self.exception_type is not None:
            return "exception"
        return "non_phase_result"


class PackageDispatcher:
    """對 event 做 package match 與 after-phase invocation。"""

    def __init__(self, registry: PackageRegistry) -> None:
        self.registry = registry

    def match_after(
        self, event: TimelineEvent
    ) -> tuple[list[RuntimePackage], list[str]]:
        """回傳有 after phase 的 packages 與缺 after phase 的 package ids。"""
        if event.tool_name is None:
            return [], []
        has_after: list[RuntimePackage] = []
        skipped_no_after: list[str] = []
        for package in self.registry.match(event):
            if package.has_phase("after"):
                has_after.append(package)
            else:
                skipped_no_after.append(package.id)
        return has_after, skipped_no_after

    async def execute_after(
        self,
        package: RuntimePackage,
        ctx: PackageContext,
    ) -> RawPhaseExecution:
        """呼叫單一 package after phase，回傳原始執行事實。"""
        started_at = time.time()
        try:
            if package.manifest.is_prompt_phase("after"):
                result = run_prompt_phase(
                    package.manifest.prompt_phase_spec("after"), ctx
                )
            else:
                raw_result = package.call_phase("after", ctx)
                result = await _await_if_needed(raw_result)
        except Exception as exc:
            finished_at = time.time()
            _LOG.warning("Package phase failed: %s after: %s", package.id, exc)
            return RawPhaseExecution(
                package_id=package.id,
                package_version=package.version,
                started_at=started_at,
                finished_at=finished_at,
                exception_type=type(exc).__name__,
                exception_repr=repr(exc),
            )

        finished_at = time.time()
        if not isinstance(result, PhaseResult):
            _LOG.warning("Package phase returned non-PhaseResult: %s after", package.id)
            return RawPhaseExecution(
                package_id=package.id,
                package_version=package.version,
                started_at=started_at,
                finished_at=finished_at,
                non_phase_result_repr=repr(result)[:200],
            )
        return RawPhaseExecution(
            package_id=package.id,
            package_version=package.version,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
        )


async def _await_if_needed(value: PhaseResult | Awaitable[PhaseResult]) -> PhaseResult:
    if inspect.isawaitable(value):
        return await value
    return value
