# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""共享 package dispatch 編排、side-effect 與 outcome 組裝。"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import copy
from enum import Enum

from terminux_engine.isvn.coordinate import resolve_coordinate
from terminux_engine.orchestration.outcome import DispatchOutcome, PackageOutcome
from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.dispatcher import PackageDispatcher, RawPhaseExecution
from terminux_engine.package_runtime.loader import RuntimePackage
from terminux_engine.package_runtime.result import TimelineEvent
from terminux_engine.package_runtime.services import (
    OriginTaggedNotifyService,
    ResultFactory,
    RuntimeHelpers,
    RuntimeServices,
    StoreService,
    package_origin_tag,
)
from terminux_engine.package_runtime.user_packages import UserRuntimePackage

_LOG = logging.getLogger(__name__)


class EngineMode(str, Enum):
    """Engine 執行契約模式。"""

    IN_DAEMON = "in_daemon"
    STANDALONE = "standalone"


class DispatchEngine:
    """以明確 mode 執行 package after-phase dispatch。"""

    def __init__(
        self,
        dispatcher: PackageDispatcher,
        *,
        mode: EngineMode,
        services: RuntimeServices,
        helpers: RuntimeHelpers | None = None,
    ) -> None:
        if mode is EngineMode.IN_DAEMON:
            if services.store.event_store is None:
                raise ValueError("in_daemon mode requires real EventStore")
            if services.notify._async_sink is None:
                raise ValueError("in_daemon mode requires real notification sink")
        self.dispatcher = dispatcher
        self.mode = mode
        self.services = services
        self._helpers = helpers if helpers is not None else RuntimeHelpers(_resolve_fn=resolve_coordinate)
        self._result_factory = ResultFactory()

    async def dispatch(self, event: TimelineEvent) -> DispatchOutcome:
        """對 event 執行所有 matched after package。"""
        started_at = time.time()
        matched, skipped_no_after = self.dispatcher.match_after(event)
        packages: list[PackageOutcome] = []
        for package in matched:
            ctx = self._build_context(package, event)
            raw = await self.dispatcher.execute_after(package, ctx)
            packages.append(self._apply_and_build_outcome(raw, package, ctx))
        finished_at = time.time()
        return DispatchOutcome(
            event_id=event.event_id,
            tool_name=event.tool_name,
            source=event.source,
            started_at=started_at,
            finished_at=finished_at,
            packages=packages,
            skipped_no_after=skipped_no_after,
        )

    def dispatch_sync(self, event: TimelineEvent) -> DispatchOutcome:
        """同步包裝，供 standalone caller 使用。"""
        return asyncio.run(self.dispatch(event))

    def _build_context(
        self,
        package: RuntimePackage,
        event: TimelineEvent,
    ) -> PackageContext:
        return PackageContext(
            package_id=package.id,
            package_version=package.version,
            phase="after",
            event=event,
            services=self._services_for_package(
                package.id,
                origin_tag=(package_origin_tag(package.id)
                            if isinstance(package, UserRuntimePackage) else None),
            ),
            helpers=self._helpers,
            result=self._result_factory,
        )

    def _services_for_package(
        self, package_id: str, *, origin_tag: str | None = None,
    ) -> RuntimeServices:
        base_store = self.services.store
        scoped_store = StoreService(
            base_store.event_store,
            session_id=base_store.session_id,
            terminal_id=base_store.terminal_id,
            user_id=base_store.user_id,
            project_id=base_store.project_id,
            package_id=package_id,
        )
        scoped_store.records = base_store.records
        services = copy(self.services)
        services.store = scoped_store
        if origin_tag is not None:
            # 只有 user-installed package 走這裡；內建 package 的 notification 不變。
            services.notify = OriginTaggedNotifyService(self.services.notify, tag=origin_tag)
        return services

    def _apply_and_build_outcome(
        self,
        raw: RawPhaseExecution,
        package: RuntimePackage,
        ctx: PackageContext,
    ) -> PackageOutcome:
        if raw.kind == "exception":
            return PackageOutcome(
                package_id=raw.package_id,
                package_version=raw.package_version,
                status="error",
                message=f"exception: {raw.exception_type}",
                started_at=raw.started_at,
                finished_at=raw.finished_at,
                error=raw.exception_repr,
            )
        if raw.kind == "non_phase_result":
            error = f"non_phase_result: {raw.non_phase_result_repr}"
            return PackageOutcome(
                package_id=raw.package_id,
                package_version=raw.package_version,
                status="error",
                message=None,
                started_at=raw.started_at,
                finished_at=raw.finished_at,
                error=error,
            )

        result = raw.result
        if result is None:
            raise RuntimeError("RawPhaseExecution result kind requires PhaseResult")
        records_committed = 0
        notifications_emitted = 0
        reports_rendered = 0
        if result.status != "noop":
            records_committed = self._apply_records(ctx, result.records, package.id)
            notifications_emitted = self._apply_notifications(
                ctx, result.notifications, package.id
            )
            reports_rendered = self._apply_reports(ctx, result.reports, package.id)
        return PackageOutcome(
            package_id=raw.package_id,
            package_version=raw.package_version,
            status=result.status,
            message=result.message,
            started_at=raw.started_at,
            finished_at=raw.finished_at,
            facts=dict(result.facts),
            evidence=dict(result.evidence),
            records_committed=records_committed,
            notifications_emitted=notifications_emitted,
            reports_rendered=reports_rendered,
        )

    def _apply_records(self, ctx: PackageContext, records: list, package_id: str) -> int:
        committed = 0
        for record in records:
            try:
                if ctx.services is None:
                    raise RuntimeError("PackageContext services are unavailable")
                ctx.services.store.commit(record)
                committed += 1
            except Exception as exc:
                _LOG.warning("Package side-effect failed: %s record: %s", package_id, exc)
        return committed

    def _apply_notifications(
        self, ctx: PackageContext, notifications: list, package_id: str
    ) -> int:
        emitted = 0
        for notification in notifications:
            try:
                if ctx.services is None:
                    raise RuntimeError("PackageContext services are unavailable")
                ctx.services.notify.emit(notification)
                emitted += 1
            except Exception as exc:
                _LOG.warning(
                    "Package side-effect failed: %s notification: %s", package_id, exc
                )
        return emitted

    def _apply_reports(self, ctx: PackageContext, reports: list, package_id: str) -> int:
        rendered = 0
        for report in reports:
            try:
                if ctx.services is None:
                    raise RuntimeError("PackageContext services are unavailable")
                ctx.services.report.render(report)
                rendered += 1
            except Exception as exc:
                _LOG.warning("Package side-effect failed: %s report: %s", package_id, exc)
        return rendered
