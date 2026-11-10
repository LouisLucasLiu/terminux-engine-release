# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from terminux_engine.ecc.models import CommandType, ShellEvent
from terminux_engine.store.repository import Repository, RepositoryError

logger = logging.getLogger(__name__)

_MAX_SUMMARY_LEN = 4096
_TRUNCATE_SUFFIX = "...[truncated]"


class EventRecorderError(Exception):
    """EventRecorder 操作失敗時拋出"""
    pass


@dataclass
class _SessionState:
    event_id:       str
    member_id:      str
    project_id:     str
    command:        str
    work_directory: str
    command_type:   CommandType
    targets:        list[str]
    env_snapshot:   dict[str, str]
    submit_time:    datetime
    stdout_chunks:  list[str] = field(default_factory=list)
    stderr_chunks:  list[str] = field(default_factory=list)


def _build_summary(chunks: list[str]) -> str | None:
    """Join chunks → truncate at 4096 → None if empty."""
    text = "".join(chunks)
    if not text:
        return None
    if len(text) > _MAX_SUMMARY_LEN:
        return text[:_MAX_SUMMARY_LEN] + _TRUNCATE_SUFFIX
    return text


class EventRecorder:

    def __init__(self, repository: Repository) -> None:
        """
        repository: 已初始化的 Repository 實作（相依注入）
        """
        self._repository = repository
        self._sessions: dict[str, _SessionState] = {}

    def record_command_submit(
        self,
        session_id:     str,
        member_id:      str,
        project_id:     str,
        command:        str,
        work_directory: str,
        timestamp:      datetime,
        command_type:   CommandType,
        targets:        list[str],
        env_snapshot:   dict[str, str] = {},
    ) -> str:
        """
        接收 command.submit 事件，初始化 session 狀態。
        回傳生成的 event_id（UUID v4）。
        """
        if not member_id:
            raise EventRecorderError("member_id must not be empty")
        if not command:
            raise EventRecorderError("command must not be empty")
        if not work_directory.startswith("/"):
            raise EventRecorderError("work_directory must be an absolute path")

        if session_id in self._sessions:
            logger.warning(
                "session %s had uncommitted state, discarding", session_id
            )

        event_id = str(uuid.uuid4())
        self._sessions[session_id] = _SessionState(
            event_id=event_id,
            member_id=member_id,
            project_id=project_id,
            command=command,
            work_directory=work_directory,
            command_type=command_type,
            targets=list(targets),
            env_snapshot=dict(env_snapshot),
            submit_time=timestamp,
        )
        return event_id

    def record_command_output(
        self,
        session_id: str,
        stream:     str,
        content:    str,
        timestamp:  datetime,
    ) -> None:
        """
        接收 command.output 事件，累積至 buffer。
        session_id 不存在時拋出 EventRecorderError。
        """
        state = self._sessions.get(session_id)
        if state is None:
            raise EventRecorderError(
                f"no active command for session: {session_id}"
            )
        if stream == "stdout":
            state.stdout_chunks.append(content)
        else:
            state.stderr_chunks.append(content)

    def record_command_complete(
        self,
        session_id:         str,
        exit_code:          int,
        duration_ms:        int,
        timestamp:          datetime,
        targets_confirmed:  list[str],
        artifacts_detected: list[str],
    ) -> ShellEvent:
        """
        接收 command.complete 事件，組裝完整 ShellEvent 並持久化。
        回傳完整的 ShellEvent。
        session_id 不存在時拋出 EventRecorderError。
        repository 失敗時拋出 EventRecorderError。
        """
        state = self._sessions.get(session_id)
        if state is None:
            raise EventRecorderError(
                f"no active command for session: {session_id}"
            )
        if duration_ms < 0:
            raise EventRecorderError("duration_ms must be >= 0")

        # 合併 targets（聯集去重）
        merged_targets = list(set(state.targets) | set(targets_confirmed))

        # 組裝 summary（截斷超過 4096 字元）
        stdout_summary = _build_summary(state.stdout_chunks)
        stderr_summary = _build_summary(state.stderr_chunks)

        event = ShellEvent(
            event_id=state.event_id,
            session_id=session_id,
            member_id=state.member_id,
            project_id=state.project_id,
            timestamp=state.submit_time,
            work_directory=state.work_directory,
            block=None,               # 由 layer1.classifier 後續填入
            command=state.command,
            command_type=state.command_type,
            targets=merged_targets,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            exit_code=exit_code,
            duration_ms=duration_ms,
            extracted_data={},        # 由 layer1.extractor 後續填入
            artifacts_detected=list(artifacts_detected),
        )

        try:
            self._repository.insert_event(event)
        except RepositoryError as e:
            raise EventRecorderError(str(e)) from e

        del self._sessions[session_id]
        return event

    def record(self, event: ShellEvent) -> None:
        """
        直接持久化已建立的 ShellEvent。
        由 ipc.server 在 command.complete 時使用。
        repository 失敗時拋出 EventRecorderError。
        """
        try:
            self._repository.insert_event(event)
        except RepositoryError as e:
            raise EventRecorderError(str(e)) from e

    def update_extracted_data(
        self,
        event_id: str,
        extracted_data: dict,
        block: str | None,
    ) -> None:
        """
        更新已持久化 ShellEvent 的 extracted_data 與 block。
        由 layer1_pipeline 在 extractor.extract() 後呼叫。
        repository 失敗時拋出 EventRecorderError。
        """
        try:
            self._repository.update_event_extracted_data(event_id, extracted_data, block)
        except RepositoryError as e:
            raise EventRecorderError(str(e)) from e
