# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AnalysisTask lifecycle event 的 append-only writer。

只做一件事：把 task lifecycle event 以既有 `EventStore.emit()` append 進去。

硬性規則：

- **不改 EventStore schema、不新增 table／index、不做 migration。**
- Event id 由 host 在寫入前產生；`emit()` 正常返回代表該筆已 commit
  （`EventStore.emit()` 內部 INSERT 後即 `commit()`，失敗會拋出）。
  **不把 queued／in-memory 當成功。**
- `sequence` 由呼叫端在同一個 task lock 內遞增。一致性由 single-process
  鎖保證，**不是** DB logical-key uniqueness——`session_events` 只有
  `event_id` 是 PRIMARY KEY。
- EventStore row 的 `user_id`／`project_id`／`session_id` 與 payload 的
  `scope` 必須相符；本 writer 由同一份 `TaskScope` 同時填兩邊。
- Task lifecycle event 的 `package_id` 一律 `None`：它不是 Package 分析
  record。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import models as M
from terminux_engine.bounded_execution.analysis_tasks.canonical import now_ms

_LOG = logging.getLogger(__name__)


class TaskEventWriteError(RuntimeError):
    """append 失敗。呼叫端必須把它當成「未持久化」，不得繼續派工。"""

    def __init__(self, event_type: str, cause: BaseException | None = None) -> None:
        super().__init__(f"analysis task event not persisted: {event_type}")
        self.event_type = event_type
        self.cause = cause


class TaskEventDuplicateError(TaskEventWriteError):
    """這個 event id 已經存在——**別人贏了**，而不是寫入失敗。

    `session_events.event_id` 是 PRIMARY KEY，因此以 durable identity 推導出
    event id 再 append，就是一次 compare-and-append：沒有「先查後寫」的競爭
    窗口。呼叫端收到這個錯誤時，該筆事件確實已經持久化（由另一個 process 或
    另一次呼叫寫入），必須回頭讀回既有結果，不得重試或另建一份。
    """


#: **全域唯一的 sequence allocator。**
#:
#: setup repair F-2：先前 `service` 用後遞增、`flow_worker` 四處用前遞增，兩套
#: 語義混用的結果是固定缺一號（service 寫完 2 之後 live.sequence=3，worker
#: 先 `+=1` 才用，3 永遠沒被寫），並在結尾與 service 撞號（repair 與
#: terminal 都拿到 23）。這裡集中成一份，intent／run_link／
#: input_reference／disposition／repair／terminal 全部走它。
#:
#: 呼叫端必須持有該 task 的 lock。
def next_sequence(live: Any) -> int:
    """取下一個 sequence（0-based，post-increment）。"""
    value = int(live.sequence)
    live.sequence = value + 1
    return value


@dataclass(frozen=True)
class AppendedEvent:
    """一筆已 commit 的 event。`event_id` 是後續引用的唯一合法身分。"""

    event_id: str
    event_type: str
    sequence: int
    timestamp_ms: int


class TaskEventWriter:
    """把 task lifecycle event 寫進既有 EventStore。

    `terminal_id` 固定為 `"default"`——task lifecycle 不屬於任何特定
    terminal stream，也不參與 ECC 的 per-terminal 聚合。
    """

    def __init__(self, event_store: Any, *, terminal_id: str = "default") -> None:
        if event_store is None:
            raise ValueError("event_store is required")
        self._event_store = event_store
        self._terminal_id = terminal_id

    @property
    def event_store(self) -> Any:
        """唯讀取用同一個 EventStore。

        跨 FlowTask 的證據解析（例如 CORE 比較要找回它那一次相依 proof task
        的 durable envelope）必須從**已持久化的事件**重建，不能依賴記憶體物件，
        也不能由 harness 傳路徑進來。這個 property 只是把同一個 store 交出去，
        不提供任何寫入捷徑。
        """
        return self._event_store

    def append(
        self,
        *,
        event_type: str,
        task_id: str,
        task_digest: str,
        daemon_instance_id: str,
        scope: M.TaskScope,
        sequence: int,
        body: dict[str, Any],
        timestamp_ms: int | None = None,
        event_id: str | None = None,
    ) -> AppendedEvent:
        """append 一筆 event。成功返回代表已 commit；失敗一律拋出。"""
        # 同一個 writer 也寫 `flow_task.*.v1`。兩組型別各自固定，
        # 不接受任意字串——共用讀取器依實際 schema 與 scoped task id 解析。
        from terminux_engine.bounded_execution.analysis_tasks.flow_models import (
            FLOW_TASK_EVENT_TYPES,
        )

        if event_type not in M.TASK_EVENT_TYPES and event_type not in FLOW_TASK_EVENT_TYPES:
            raise ValueError(f"unknown task event type: {event_type!r}")
        stamp = now_ms() if timestamp_ms is None else int(timestamp_ms)
        # 由 durable identity 推導的 event id 讓 append 本身成為 CAS；其餘
        # 事件維持隨機 id，行為不變。
        event_id = uuid.uuid4().hex if event_id is None else event_id
        ledger_reservation: dict[str, Any] | None = None
        if event_type in FLOW_TASK_EVENT_TYPES:
            # FlowTask create／start／step intent 的 reservation 必須早於 EventStore
            # 與後續 child side effect；JSONL 與 EventStore 並非同一 transaction。
            from terminux_engine.expert_ai.batch_ledger import configured_ledger

            ledger = configured_ledger()
            if ledger is not None:
                snapshot = next((body[key] for key in ("task", "snapshot")
                                 if isinstance(body.get(key), dict)), None)
                identities = self.__dict__.setdefault("_ledger_identities", {})
                if event_type == "flow_task.snapshot.v1" and snapshot is not None:
                    source = ((snapshot.get("eap_proposal") or {}).get("work_source")
                              or (snapshot.get("eap_proposal") or {}).get("source") or {})
                    identities[task_id] = {
                        "work_id": source.get("work_id"),
                        "attempt_id": source.get("attempt_id"),
                        "authorization_digest": source.get("parent_authorization_digest")}
                ledger_reservation = ledger.prepare_task_event(
                    event_type=event_type, event_id=event_id, task_id=task_id,
                    session_id=scope.origin_session_id, body=body,
                    identity=dict(identities.get(task_id) or {}))
                if ledger_reservation is not None:
                    body = {**body, "ledger_reservation": {
                        "reservation_id": ledger_reservation["reservation_id"],
                        "state": "PREPARED"}}
        payload: dict[str, Any] = {
            # 共同欄位
            "schema_version": M.SCHEMA_VERSION,
            "task_id": task_id,
            "task_digest": task_digest,
            "daemon_instance_id": daemon_instance_id,
            "scope": scope.to_json_dict(),
            "sequence": int(sequence),
            "timestamp_ms": stamp,
        }
        # body 不得覆寫共同欄位——避免呼叫端意外改掉 scope 或 sequence。
        conflicting = sorted(set(body) & set(payload))
        if conflicting:
            raise ValueError(
                f"event body may not override reserved fields: {conflicting}")
        payload.update(body)

        from terminux_engine.store.event_store import SessionEvent

        try:
            self._event_store.emit(SessionEvent(
                event_id=event_id,
                session_id=scope.origin_session_id,
                terminal_id=self._terminal_id,
                user_id=scope.member_id,
                project_id=scope.project_id,
                package_id=None,
                timestamp=stamp / 1000.0,
                event_type=event_type,
                payload=payload,
            ))
        except sqlite3.IntegrityError as exc:
            # 同一個 event id 已存在：事件**已經**持久化，只是不是我們寫的。
            # reservation 的 idempotency key 也由同一個 event id 推導，因此它
            # 屬於贏的那一方；這裡絕不可以把它結算掉。
            _LOG.info("analysis task event already persisted (%s): %r", event_type, exc)
            raise TaskEventDuplicateError(event_type, exc) from exc
        except Exception as exc:  # noqa: BLE001 - 寫入失敗必須是可觀察的錯誤
            if ledger_reservation is not None:
                # EventStore 未落盤代表 child 尚未可以合法派送；明確釋放 reservation。
                # 釋放本身失敗（例如 reservation 已由贏的一方結算）不得蓋掉原始
                # 錯誤——呼叫端要看到的是「事件沒有落盤」。
                try:
                    ledger.settle_reservation(
                        str(ledger_reservation["reservation_id"]), operation="released",
                        evidence_ref={"event_type": event_type, "event_id": event_id,
                                      "event_persisted": False,
                                      "reservation_id": ledger_reservation["reservation_id"]})
                except Exception as release_exc:  # noqa: BLE001
                    _LOG.warning("reservation release after a failed append could not "
                                 "be settled (%s): %r", event_type, release_exc)
            _LOG.error("analysis task event append failed (%s): %r", event_type, exc)
            raise TaskEventWriteError(event_type, exc) from exc
        if event_type in FLOW_TASK_EVENT_TYPES:
            if ledger_reservation is not None:
                ledger.settle_task_event(ledger_reservation, event_type=event_type,
                                         event_id=event_id)
            elif event_type == "flow_task.run_link.v1":
                # run link 沒有新增可消耗額度，仍保留與已 PREPARED launch 的關聯。
                self._record_ledger(event_type, event_id, task_id, scope, body)
        return AppendedEvent(event_id=event_id, event_type=event_type,
                             sequence=int(sequence), timestamp_ms=stamp)

    def _record_ledger(self, event_type: str, event_id: str, task_id: str,
                       scope: M.TaskScope, body: dict[str, Any]) -> None:
        """FlowTask 權威事件落盤後入即時帳本；未設定帳本時不做事。"""
        from terminux_engine.expert_ai.batch_ledger import configured_ledger

        ledger = configured_ledger()
        if ledger is None:
            return
        # analysis task 用 `snapshot`，flow task 用 `task` 保存 snapshot 內容。
        snapshot = next((body[key] for key in ("task", "snapshot")
                         if isinstance(body.get(key), dict)), None)
        if event_type != "flow_task.snapshot.v1":
            snapshot = None
        identities = self.__dict__.setdefault("_ledger_identities", {})
        if snapshot is not None:
            source = ((snapshot.get("eap_proposal") or {}).get("work_source")
                      or (snapshot.get("eap_proposal") or {}).get("source") or {})
            identities[task_id] = {
                "work_id": source.get("work_id"), "attempt_id": source.get("attempt_id"),
                "authorization_digest": source.get("parent_authorization_digest")}
        ledger.record_task_event(
            event_type=event_type, event_id=event_id, task_id=task_id,
            session_id=scope.origin_session_id, body=body,
            identity=dict(identities.get(task_id) or {}))

    # ── 各 event 的具名 helper（只組 body，共同欄位由 append() 統一填）──

    def append_snapshot(self, task: M.AnalysisTask, *, sequence: int) -> AppendedEvent:
        return self.append(
            event_type=M.EVENT_SNAPSHOT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"snapshot": task.to_json_dict()},
        )

    def append_grant(
        self, task: M.AnalysisTask, grant: M.TaskAuthorizationGrant, *, sequence: int,
    ) -> AppendedEvent:
        """「曾經核准」的歷史。重啟後讀到它**不代表**仍有執行權。"""
        return self.append(
            event_type=M.EVENT_GRANT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"grant": grant.to_event_payload()},
        )

    def append_started(
        self, task: M.AnalysisTask, grant: M.TaskAuthorizationGrant, *, sequence: int,
    ) -> AppendedEvent:
        return self.append(
            event_type=M.EVENT_STARTED, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"grant_id": grant.grant_id,
                  "started_at_ms": grant.approved_at_ms,
                  "deadline_at_ms": grant.deadline_at_ms},
        )

    def append_step_intent(
        self, task: M.AnalysisTask, binding: M.TaskStepBinding, *, sequence: int,
    ) -> AppendedEvent:
        """**派送前**的意圖。它成功落盤後才可以 claim。

        重啟後看到 intent 卻沒有可信 receipt ⇒ 該步 OUTCOME_UNCONFIRMED；
        **不得**由 intent 推論 claim 一定成功。
        """
        return self.append(
            event_type=M.EVENT_STEP_INTENT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"step_id": binding.step_id, "binding": binding.to_json_dict()},
        )

    def append_step_result(
        self, task: M.AnalysisTask, envelope: M.TaskResultEnvelope, *, sequence: int,
    ) -> AppendedEvent:
        """完整結果 envelope。呼叫端必須先驗過 canonical 大小上限。"""
        return self.append(
            event_type=M.EVENT_STEP_RESULT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"step_id": envelope.step_id, "envelope": envelope.to_json_dict()},
        )

    def append_step_receipt(
        self,
        task: M.AnalysisTask,
        *,
        sequence: int,
        step_id: str,
        step_status: str,
        outcome: dict[str, Any],
        persistence_status: str,
        result_event_id: str | None,
        result_sha256: str | None,
        can_continue: bool,
        error_code: str | None = None,
    ) -> AppendedEvent:
        return self.append(
            event_type=M.EVENT_STEP_RECEIPT, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={
                "step_id": step_id,
                "step_status": step_status,
                "outcome": dict(outcome),
                "persistence_status": persistence_status,
                "result_event_id": result_event_id,
                "result_sha256": result_sha256,
                "can_continue": bool(can_continue),
                "error_code": error_code,
            },
        )

    def append_cancel_requested(
        self, task: M.AnalysisTask, *, sequence: int, requested_by_session_id: str,
        reason: str | None = None,
    ) -> AppendedEvent:
        return self.append(
            event_type=M.EVENT_CANCEL_REQUESTED, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"requested_by_session_id": requested_by_session_id,
                  "reason": reason},
        )

    def append_terminal(
        self, task: M.AnalysisTask, *, sequence: int, status: str,
        reason: str | None, step_references: list[dict[str, Any]],
        budget_consumed: dict[str, Any],
    ) -> AppendedEvent:
        return self.append(
            event_type=M.EVENT_TERMINAL, task_id=task.task_id,
            task_digest=task.task_digest, daemon_instance_id=task.daemon_instance_id,
            scope=task.scope, sequence=sequence,
            body={"status": status, "reason": reason,
                  "step_references": list(step_references),
                  "budget_consumed": dict(budget_consumed)},
        )


__all__ = ["AppendedEvent", "TaskEventWriteError", "TaskEventWriter"]
