# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

from abc import ABC, abstractmethod

from terminux_engine.ecc.models import (
    EventQuery,
    ProvenanceObs,
    ProvenanceSpec,
    ShellEvent,
)


class RepositoryError(Exception):
    """
    Store 存取失敗時拋出。
    欄位：
      message: str  原始錯誤說明
    """
    pass


class Repository(ABC):

    @abstractmethod
    def insert_event(self, event: ShellEvent) -> None:
        ...

    @abstractmethod
    def update_event_extracted_data(
        self,
        event_id: str,
        extracted_data: dict,
        block: str | None,
    ) -> None:
        ...

    @abstractmethod
    def query_events(self, filters: EventQuery) -> list[ShellEvent]:
        ...

    @abstractmethod
    def insert_provenance_spec(self, record: ProvenanceSpec) -> None:
        ...

    def query_provenance_spec(
        self, step: str, member_id: str
    ) -> ProvenanceSpec | None:
        """
        查詢符合 step + member_id 的最新 ProvenanceSpec。
        找不到時回傳 None。

        ABC 層在此驗證 step / member_id 非空，具體實作不需重複。
        """
        if not step:
            raise RepositoryError("step must not be empty")
        if not member_id:
            raise RepositoryError("member_id must not be empty")
        return self._query_provenance_spec(step, member_id)

    @abstractmethod
    def _query_provenance_spec(
        self, step: str, member_id: str
    ) -> ProvenanceSpec | None:
        ...

    @abstractmethod
    def insert_provenance_obs(self, record: ProvenanceObs) -> None:
        ...

    def query_provenance_obs(
        self, step: str, member_id: str
    ) -> list[ProvenanceObs]:
        """
        查詢符合 step + member_id 的所有 ProvenanceObs，無結果回傳空 list。

        ABC 層在此驗證 step / member_id 非空，具體實作不需重複。
        """
        if not step:
            raise RepositoryError("step must not be empty")
        if not member_id:
            raise RepositoryError("member_id must not be empty")
        return self._query_provenance_obs(step, member_id)

    @abstractmethod
    def _query_provenance_obs(
        self, step: str, member_id: str
    ) -> list[ProvenanceObs]:
        ...
