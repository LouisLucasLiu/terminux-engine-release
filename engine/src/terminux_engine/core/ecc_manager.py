# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

from terminux_engine.ecc.models import (
    CommandType,
    EventQuery,
    ProvenanceObs,
    ProvenanceSpec,
    QueryOrder,
    ShellEvent,
)
from terminux_engine.store.repository import Repository, RepositoryError


class EccManagerError(Exception):
    """EccManager 操作失敗時拋出"""
    pass


class EccManager:

    def __init__(self, repository: Repository) -> None:
        """
        repository: 已初始化的 Repository 實作（相依注入）
        """
        self._repository = repository

    def query_events(self, filters: EventQuery) -> list[ShellEvent]:
        """
        直接將 filters 傳入 repository.query_events。
        RepositoryError → 包裝成 EccManagerError 拋出。
        """
        try:
            return self._repository.query_events(filters)
        except RepositoryError as e:
            raise EccManagerError(str(e)) from e

    def get_event(self, event_id: str) -> ShellEvent | None:
        """
        依 event_id 查詢單一 ShellEvent。
        找不到回傳 None。

        實作備註：過渡設計，查詢全量後在 Python 層篩選，
        待 store.repository 未來擴充直接查詢後可優化。
        """
        if not event_id:
            raise EccManagerError("event_id must not be empty")
        try:
            events = self._repository.query_events(EventQuery(limit=None))
        except RepositoryError as e:
            raise EccManagerError(str(e)) from e

        for event in events:
            if event.event_id == event_id:
                return event
        return None

    def get_recent_events(
        self,
        member_id:      str,
        limit:          int,
        command_type:   CommandType | None = None,
        tool_signature: str | None = None,
        block:          str | None = None,
    ) -> list[ShellEvent]:
        """
        查詢指定 member 最近的 events，依 timestamp DESC 排序。
        RepositoryError → 包裝成 EccManagerError 拋出。
        """
        if not member_id:
            raise EccManagerError("member_id must not be empty")
        if limit <= 0:
            raise EccManagerError("limit must be > 0")

        query = EventQuery(
            member_id=member_id,
            command_type=command_type,
            tool_signature=tool_signature,
            block=block,
            limit=limit,
            order=QueryOrder.DESC,
        )
        try:
            return self._repository.query_events(query)
        except RepositoryError as e:
            raise EccManagerError(str(e)) from e

    def get_session_events(self, session_id: str) -> list[ShellEvent]:
        """
        查詢同一 session 的所有 events，依 timestamp ASC 排序。
        RepositoryError → 包裝成 EccManagerError 拋出。
        """
        if not session_id:
            raise EccManagerError("session_id must not be empty")

        query = EventQuery(
            session_id=session_id,
            order=QueryOrder.ASC,
        )
        try:
            return self._repository.query_events(query)
        except RepositoryError as e:
            raise EccManagerError(str(e)) from e
