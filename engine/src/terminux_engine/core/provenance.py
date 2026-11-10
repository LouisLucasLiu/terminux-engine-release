# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

from datetime import datetime, timezone

from terminux_engine.ecc.models import (
    EventQuery,
    ProvenanceMismatch,
    ProvenanceObs,
    ProvenanceSource,
    ProvenanceSpec,
    QueryOrder,
    ShellEvent,
)
from terminux_engine.store.repository import Repository, RepositoryError


class ProvenanceError(Exception):
    """Provenance 操作失敗時拋出"""
    pass


class ProvenanceEngine:

    def __init__(self, repository: Repository) -> None:
        """
        repository: 已初始化的 Repository 實作（相依注入）
        """
        self._repository = repository

    # ── write_spec ────────────────────────────────────────────────────────────

    def write_spec(
        self,
        step:      str,
        member_id: str,
        data:      dict,
        source:    ProvenanceSource,
    ) -> None:
        """
        寫入 ProvenanceSpec。
        step / member_id 非空、data 非空 dict。
        """
        if not step:
            raise ProvenanceError("step must not be empty")
        if not member_id:
            raise ProvenanceError("member_id must not be empty")
        if not data:
            raise ProvenanceError("data must not be empty")

        record = ProvenanceSpec(
            step=step,
            member_id=member_id,
            data=data,
            source=source,
            written_at=datetime.now(timezone.utc),
        )
        try:
            self._repository.insert_provenance_spec(record)
        except RepositoryError as e:
            raise ProvenanceError(str(e)) from e

    # ── write_obs ─────────────────────────────────────────────────────────────

    def write_obs(
        self,
        step:      str,
        member_id: str,
        data:      dict,
        event_id:  str,
    ) -> None:
        """
        寫入 ProvenanceObs。
        step / member_id / event_id 非空、data 非空 dict。
        """
        if not step:
            raise ProvenanceError("step must not be empty")
        if not member_id:
            raise ProvenanceError("member_id must not be empty")
        if not data:
            raise ProvenanceError("data must not be empty")
        if not event_id:
            raise ProvenanceError("event_id must not be empty")

        record = ProvenanceObs(
            step=step,
            member_id=member_id,
            data=data,
            event_id=event_id,
            observed_at=datetime.now(timezone.utc),
        )
        try:
            self._repository.insert_provenance_obs(record)
        except RepositoryError as e:
            raise ProvenanceError(str(e)) from e

    # ── get_spec ──────────────────────────────────────────────────────────────

    def get_spec(
        self,
        step:      str,
        member_id: str,
    ) -> ProvenanceSpec | None:
        """
        查詢最新 ProvenanceSpec，找不到回傳 None。
        """
        if not step:
            raise ProvenanceError("step must not be empty")
        if not member_id:
            raise ProvenanceError("member_id must not be empty")

        try:
            return self._repository.query_provenance_spec(step, member_id)
        except RepositoryError as e:
            raise ProvenanceError(str(e)) from e

    # ── get_observations ──────────────────────────────────────────────────────

    def get_observations(
        self,
        step:      str,
        member_id: str,
    ) -> list[ProvenanceObs]:
        """
        查詢所有 ProvenanceObs，依 observed_at DESC 排序，無結果回傳空 list。
        """
        if not step:
            raise ProvenanceError("step must not be empty")
        if not member_id:
            raise ProvenanceError("member_id must not be empty")

        try:
            return self._repository.query_provenance_obs(step, member_id)
        except RepositoryError as e:
            raise ProvenanceError(str(e)) from e

    # ── diff ──────────────────────────────────────────────────────────────────

    def diff(
        self,
        step:      str,
        member_id: str,
    ) -> list[ProvenanceMismatch]:
        """
        比對最新 spec vs 最新 obs，回傳 key 層級的差異 list。
        spec 不存在或 obs 不存在時回傳 []。
        obs.data 有而 spec.data 沒有的 key → 忽略。
        spec.data 有而 obs.data 沒有的 key → mismatch（observed=None）。
        """
        if not step:
            raise ProvenanceError("step must not be empty")
        if not member_id:
            raise ProvenanceError("member_id must not be empty")

        spec = self.get_spec(step, member_id)
        if spec is None:
            return []

        obs_list = self.get_observations(step, member_id)
        if not obs_list:
            return []

        latest_obs = obs_list[0]   # query_provenance_obs 依 observed_at DESC

        mismatches: list[ProvenanceMismatch] = []
        for key, spec_val in spec.data.items():
            obs_val = latest_obs.data.get(key)
            if obs_val != spec_val:
                mismatches.append(ProvenanceMismatch(
                    key=key,
                    spec=spec_val,
                    observed=obs_val,   # key 不存在時為 None
                ))
        return mismatches

    # ── get_data_lineage ──────────────────────────────────────────────────────

    def get_data_lineage(self, artifact: str) -> list[ShellEvent]:
        """
        查詢 artifacts_detected 含 artifact 的所有 ShellEvent，依 timestamp ASC 排序。
        無結果回傳空 list。
        """
        if not artifact:
            raise ProvenanceError("artifact must not be empty")

        try:
            all_events = self._repository.query_events(
                EventQuery(order=QueryOrder.ASC, limit=None)
            )
        except RepositoryError as e:
            raise ProvenanceError(str(e)) from e

        return [e for e in all_events if artifact in e.artifacts_detected]
