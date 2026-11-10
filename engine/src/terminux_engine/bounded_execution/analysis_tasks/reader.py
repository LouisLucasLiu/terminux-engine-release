# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AnalysisTask 歷史的 scoped、驗證式唯讀 reader。

這個 reader **只讀**。它不寫 recovery event、不復活 `ProposalRegistry`、
也不把 durable 的 `analysis_task.grant.v1` 轉回可執行的授權——那筆 event
只證明「曾經核准」。

驗證項目（任一不成立 ⇒ `TASK_EVIDENCE_INVALID`，不降級成「取最新一筆」）：

1. row scope 與 payload scope 同時相符，且兩者一致。
2. 恰好一筆 snapshot，且 `task_digest` 可由 snapshot 內容重算得到。
3. 每筆 event 的 `task_id`／`task_digest` 與 snapshot 相符。
4. `sequence` 不重複、不缺段（從 0 起連續）。
5. step result 的 `result_sha256` 可重算，且 task/step/capability 與 snapshot
   的固定 step 相符。
6. step receipt 引用的 `result_event_id`／`result_sha256` 必須指向同一 task
   scope 內、內容 hash 相符的那一筆 result event。

讀取有界：命中 `limit` 即視為 evidence incomplete（`truncated=True`），
**不**當成完整歷史。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks import errors as E
from terminux_engine.bounded_execution.analysis_tasks import models as M

#: 單一 task 的 lifecycle event 上限。固定兩步 × 每步至多 3 筆
#: （intent／result／receipt）＋ snapshot／grant／started／cancel／terminal，
#: 正常上限遠低於此；留餘裕是為了讓「異常多筆」被偵測成 truncated 而不是
#: 被靜默截斷。
MAX_TASK_EVENTS = 256


class TaskEvidenceError(E.TaskError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(E.TASK_EVIDENCE_INVALID, message, **details)


@dataclass
class StepHistory:
    step_id: str
    capability_id: str
    intent_recorded: bool = False
    receipt: dict[str, Any] | None = None
    result_event_id: str | None = None
    result_envelope: M.TaskResultEnvelope | None = None
    #: 本步的解說降級判定（只有 v2 repair template 會有）。
    analysis_disposition: dict[str, Any] | None = None

    @property
    def has_trusted_receipt(self) -> bool:
        return self.receipt is not None

    @property
    def result_sha256(self) -> str | None:
        return self.result_envelope.result_sha256 if self.result_envelope else None

    @property
    def status(self) -> str:
        """由歷史事件判讀本步狀態。**不由 intent 猜 claim 成功**。

        - 有可信 receipt ⇒ receipt 上記的狀態。
        - 有 intent 但沒有 receipt ⇒ `OUTCOME_UNCONFIRMED`（不知道跑了沒）。
        - 只有 result 沒有 receipt ⇒ 一樣是 `OUTCOME_UNCONFIRMED`：不由
          result 推論 finalization 已完整。
        - 都沒有 ⇒ `PENDING`。
        """
        if self.receipt is not None:
            status = self.receipt.get("step_status")
            return status if isinstance(status, str) and status else "OUTCOME_UNCONFIRMED"
        if self.intent_recorded or self.result_envelope is not None:
            return "OUTCOME_UNCONFIRMED"
        return "PENDING"


@dataclass
class TaskHistory:
    """一筆 task 的完整、已驗證歷史。"""

    task: M.AnalysisTask
    steps: dict[str, StepHistory]
    grant: dict[str, Any] | None = None
    started: dict[str, Any] | None = None
    cancel_requested: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    truncated: bool = False
    event_count: int = 0
    sequences: list[int] = field(default_factory=list)
    #: flow task 的固定修復判定（`flow_task.repair.v1`）。只有 flow
    #: 字彙會填它；analysis 字彙沒有這個事件型別，維持 None。純唯讀投影，
    #: 內容原樣保留，不重新計算判定。
    repair: dict[str, Any] | None = None
    #: 不指向 step 的資訊型事件（queued／profile_snapshot／lineage），
    #: 依型別原樣投影，依序保留。
    informational: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def evidence_status(self) -> str:
        return "incomplete" if self.truncated else "ok"

    @property
    def narrative_rejections(self) -> list[dict[str, Any]]:
        """依 step 順序列出被判定為「解說降級」的步驟。"""
        return [dict(s.analysis_disposition) for s in self.steps.values()
                if s.analysis_disposition is not None]


class TaskHistoryReader:
    """以既有 EventStore 的 scoped 唯讀查詢重建 task 歷史。"""

    def __init__(self, event_store: Any, *, max_events: int = MAX_TASK_EVENTS,
                 vocabulary: Any = M) -> None:
        """`vocabulary` 是這一類 task 的事件型別常數來源。

        flow task 用的是 `flow_task.*.v1`，與 analysis task 的
        `analysis_task.*.v1` 是兩套字彙。reader 的驗證邏輯完全相同，差別只在
        型別名稱——因此把字彙注入進來，**不**放寬任何一邊的型別檢查，也不
        接受兩套字彙混用。
        """
        if event_store is None:
            raise ValueError("event_store is required")
        self._event_store = event_store
        self._max_events = max_events
        self._vocab = vocabulary

    # ── public ───────────────────────────────────────────────────────

    def load(
        self, task_id: str, *, member_id: str, project_id: str,
        any_project: bool = False,
    ) -> TaskHistory | None:
        """讀取並驗證。查無或跨 scope 一律回 `None`（呼叫端收斂成同一錯誤）。

        ``any_project=True`` 時只以 member 查，project 以
        snapshot 記錄的 `scope.project_id` 為準（之後每筆事件仍必須與它一致）。
        這是**唯讀**入口；呼叫端負責決定該 caller 能否讀這個 project 的 task。
        """
        events, truncated = self._event_store.query_task_events(
            task_id, user_id=member_id, project_id=project_id,
            event_types=self._vocab.TASK_EVENT_TYPES, limit=self._max_events,
            any_project=any_project,
        )
        if not events:
            return None
        return self._build(task_id, events, truncated, member_id=member_id,
                           project_id=None if any_project else project_id)

    def load_result_envelope(
        self,
        *,
        event_id: str,
        expected_sha256: str,
        task_id: str,
        step_id: str,
        member_id: str,
        project_id: str,
    ) -> M.TaskResultEnvelope:
        """依 event_id 取單筆完整結果，並驗 scope／關聯／hash。

        歧義或缺失一律拋 `TASK_EVIDENCE_INVALID`，**不** fallback 到
        「同 task 的最新一筆 result」。
        """
        event = self._event_store.get_event_by_id_for_scope(
            event_id, user_id=member_id, project_id=project_id)
        if event is None:
            raise TaskEvidenceError("result event not found in scope",
                                    task_id=task_id, step_id=step_id)
        if event.event_type != self._vocab.EVENT_STEP_RESULT:
            raise TaskEvidenceError("referenced event is not a step result",
                                    task_id=task_id, step_id=step_id)
        payload = event.payload or {}
        self._require_scope(payload, member_id, project_id, task_id)
        if payload.get("task_id") != task_id or payload.get("step_id") != step_id:
            raise TaskEvidenceError("result event does not belong to this task step",
                                    task_id=task_id, step_id=step_id)
        envelope = self._parse_envelope(payload.get("envelope"), task_id, step_id)
        if envelope.result_sha256 != expected_sha256:
            raise TaskEvidenceError("result hash does not match the receipt",
                                    task_id=task_id, step_id=step_id)
        return envelope

    # ── internal ─────────────────────────────────────────────────────

    @staticmethod
    def _require_scope(
        payload: dict[str, Any], member_id: str, project_id: str, task_id: str,
    ) -> None:
        scope = payload.get("scope")
        if not isinstance(scope, dict):
            raise TaskEvidenceError("event has no scope", task_id=task_id)
        if scope.get("member_id") != member_id or scope.get("project_id") != project_id:
            # row scope 已由 SQL 過濾；payload scope 不符代表資料不一致。
            raise TaskEvidenceError("event scope does not match the row scope",
                                    task_id=task_id)

    def _parse_envelope(
        self, raw: Any, task_id: str, step_id: str,
    ) -> M.TaskResultEnvelope:
        if not isinstance(raw, dict):
            raise TaskEvidenceError("result envelope is missing or malformed",
                                    task_id=task_id, step_id=step_id)
        try:
            envelope = M.TaskResultEnvelope.from_json_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskEvidenceError(f"result envelope is unreadable: {exc}",
                                    task_id=task_id, step_id=step_id) from exc
        if envelope.recompute_sha256() != envelope.result_sha256:
            raise TaskEvidenceError("result envelope hash is inconsistent",
                                    task_id=task_id, step_id=step_id)
        return envelope

    def _build(
        self, task_id: str, events: list[Any], truncated: bool,
        *, member_id: str, project_id: str | None,
    ) -> TaskHistory:
        """`project_id=None`：以 snapshot 的 scope.project_id 為準（any_project 讀取）。"""
        snapshots = [e for e in events if e.event_type == self._vocab.EVENT_SNAPSHOT]
        if len(snapshots) != 1:
            raise TaskEvidenceError(
                f"expected exactly one snapshot, found {len(snapshots)}",
                task_id=task_id)
        snapshot_payload = snapshots[0].payload or {}
        if project_id is None:
            # any_project：project 以 snapshot 事件的 row scope 為準；解析出
            # snapshot 後再與 `task.scope` 互相核對（下方），之後每筆事件都須一致。
            row_scope = snapshot_payload.get("scope")
            if not isinstance(row_scope, dict) or not isinstance(row_scope.get("project_id"), str):
                raise TaskEvidenceError("event has no scope", task_id=task_id)
            project_id = row_scope["project_id"]
            derived_project = True
        else:
            derived_project = False
        self._require_scope(snapshot_payload, member_id, project_id, task_id)
        # snapshot body 的欄位名也由 vocabulary 決定：analysis task 用
        # `snapshot`，flow task 用 `task`。寫死其中一個會讓另一種歷史
        # 永遠讀不回來（setup repair F-1／F-2 核對時發現）。
        body_key = getattr(self._vocab, "TASK_SNAPSHOT_BODY_KEY", "snapshot")
        raw_snapshot = snapshot_payload.get(body_key)
        if not isinstance(raw_snapshot, dict):
            raise TaskEvidenceError("snapshot body is missing", task_id=task_id)
        # snapshot 的型別由 vocabulary 決定：analysis task 與 flow task 各自
        # 有自己的 schema，用錯型別會讓整段歷史讀不回來。
        task_cls = getattr(self._vocab, "TASK_SNAPSHOT_TYPE", M.AnalysisTask)
        try:
            task = task_cls.from_json_dict(raw_snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskEvidenceError(f"snapshot is unreadable: {exc}",
                                    task_id=task_id) from exc
        if task.task_id != task_id:
            raise TaskEvidenceError("snapshot task_id mismatch", task_id=task_id)
        if not task.digest_matches():
            raise TaskEvidenceError("snapshot digest does not match its content",
                                    task_id=task_id)
        if derived_project and task.scope.project_id != project_id:
            # any_project：row scope 與 snapshot 內容不一致 ⇒ 資料損毀，不採用。
            raise TaskEvidenceError("snapshot scope mismatch", task_id=task_id)
        if not task.scope.owner_matches(member_id, project_id):
            raise TaskEvidenceError("snapshot scope mismatch", task_id=task_id)

        sequences: list[int] = []
        history = TaskHistory(
            task=task,
            steps={s.step_id: StepHistory(step_id=s.step_id,
                                          capability_id=s.capability_id)
                   for s in task.steps},
            truncated=truncated,
            event_count=len(events),
        )

        for event in events:
            payload = event.payload or {}
            self._require_scope(payload, member_id, project_id, task_id)
            if payload.get("task_id") != task_id:
                raise TaskEvidenceError("event task_id mismatch", task_id=task_id)
            if payload.get("task_digest") != task.task_digest:
                raise TaskEvidenceError("event task_digest mismatch", task_id=task_id)
            sequence = payload.get("sequence")
            if not isinstance(sequence, int):
                raise TaskEvidenceError("event sequence is missing", task_id=task_id)
            sequences.append(sequence)
            self._absorb(history, event.event_type, payload, event.event_id, task_id)

        ordered = sorted(sequences)
        if len(set(ordered)) != len(ordered):
            raise TaskEvidenceError("duplicate event sequence", task_id=task_id)
        if not truncated and ordered and ordered != list(range(len(ordered))):
            raise TaskEvidenceError("event sequence has gaps", task_id=task_id)
        history.sequences = ordered
        return history

    def _absorb(
        self, history: TaskHistory, event_type: str, payload: dict[str, Any],
        event_id: str, task_id: str,
    ) -> None:
        if event_type == self._vocab.EVENT_SNAPSHOT:
            return
        if event_type == self._vocab.EVENT_GRANT:
            history.grant = dict(payload.get("grant") or {})
            return
        if event_type == self._vocab.EVENT_STARTED:
            history.started = {k: payload.get(k) for k in
                               ("grant_id", "started_at_ms", "deadline_at_ms")}
            return
        if event_type == self._vocab.EVENT_CANCEL_REQUESTED:
            history.cancel_requested = {
                "requested_by_session_id": payload.get("requested_by_session_id"),
                "reason": payload.get("reason"),
            }
            return
        if event_type == getattr(self._vocab, "EVENT_REPAIR", None):
            # 固定 acceptance policy 的判定結果。原樣投影，不改寫、不重算。
            body = payload.get("repair")
            history.repair = dict(body) if isinstance(body, dict) else dict(payload)
            return

        if event_type == self._vocab.EVENT_TERMINAL:
            history.terminal = {k: payload.get(k) for k in
                                ("status", "reason", "step_references",
                                 "budget_consumed")}
            return
        if event_type in getattr(self._vocab, "INFORMATIONAL_EVENT_TYPES", ()):
            history.informational.setdefault(event_type, []).append(
                {k: v for k, v in payload.items()
                 if k not in ("scope", "task_id", "task_digest",
                              "daemon_instance_id", "sequence")})
            return

        step_id = payload.get("step_id")
        step = history.steps.get(step_id) if isinstance(step_id, str) else None
        if step is None:
            raise TaskEvidenceError(
                f"event references unknown step {step_id!r}", task_id=task_id)

        if event_type == self._vocab.EVENT_STEP_INTENT:
            step.intent_recorded = True
            return
        if event_type == self._vocab.EVENT_STEP_RESULT:
            envelope = self._parse_envelope(payload.get("envelope"), task_id, step.step_id)
            if envelope.step_id != step.step_id:
                raise TaskEvidenceError("result envelope step mismatch",
                                        task_id=task_id, step_id=step.step_id)
            if envelope.capability_id != step.capability_id:
                raise TaskEvidenceError("result envelope capability mismatch",
                                        task_id=task_id, step_id=step.step_id)
            if step.result_envelope is not None:
                raise TaskEvidenceError("more than one result for the same step",
                                        task_id=task_id, step_id=step.step_id)
            step.result_envelope = envelope
            step.result_event_id = event_id
            return
        if event_type == getattr(self._vocab, "EVENT_ANALYSIS_DISPOSITION", None):
            self._absorb_disposition(history, step, payload, task_id)
            return
        if event_type == self._vocab.EVENT_STEP_RECEIPT:
            if step.receipt is not None:
                raise TaskEvidenceError("more than one receipt for the same step",
                                        task_id=task_id, step_id=step.step_id)
            step.receipt = {
                "step_status": payload.get("step_status"),
                "outcome": payload.get("outcome"),
                "persistence_status": payload.get("persistence_status"),
                "result_event_id": payload.get("result_event_id"),
                "result_sha256": payload.get("result_sha256"),
                "can_continue": payload.get("can_continue"),
                "error_code": payload.get("error_code"),
            }
            referenced = step.receipt["result_event_id"]
            if referenced is not None:
                if step.result_event_id != referenced:
                    raise TaskEvidenceError(
                        "receipt references a result event that is not this step's",
                        task_id=task_id, step_id=step.step_id)
                if (step.result_envelope is None
                        or step.result_envelope.result_sha256
                        != step.receipt["result_sha256"]):
                    raise TaskEvidenceError("receipt result hash mismatch",
                                            task_id=task_id, step_id=step.step_id)
            return


    def _absorb_disposition(
        self, history: TaskHistory, step: StepHistory, payload: dict[str, Any],
        task_id: str,
    ) -> None:
        """只有 v2 repair template 的 task 才承認這個事件。

        舊 template 的歷史裡出現它，一律當成證據不一致而拒絕——不能讓一個
        後加的事件型別回頭改寫舊 task 的判讀。
        """
        from terminux_engine.bounded_execution.analysis_tasks import (
            narrative_policy as NP,
        )

        task = history.task
        v2 = getattr(self._vocab, "SETUP_REPAIR_V2_TEMPLATE_ID", None)
        if v2 is None or getattr(task, "template_id", None) != v2:
            raise TaskEvidenceError(
                "an analysis disposition is only valid for the v2 setup repair "
                "template", task_id=task_id, step_id=step.step_id)
        if step.analysis_disposition is not None:
            raise TaskEvidenceError("more than one disposition for the same step",
                                    task_id=task_id, step_id=step.step_id)
        if payload.get("capability_id") != step.capability_id:
            raise TaskEvidenceError("disposition capability mismatch",
                                    task_id=task_id, step_id=step.step_id)
        if payload.get("template_id") != task.template_id:
            raise TaskEvidenceError("disposition template mismatch",
                                    task_id=task_id, step_id=step.step_id)
        if payload.get("profile_digest") != task.profile_digest:
            raise TaskEvidenceError("disposition profile digest mismatch",
                                    task_id=task_id, step_id=step.step_id)
        if payload.get("policy_id") != NP.POLICY_ID:
            raise TaskEvidenceError("disposition carries an unknown policy",
                                    task_id=task_id, step_id=step.step_id)
        if payload.get("disposition") != NP.DISPOSITION_CONTINUE:
            raise TaskEvidenceError("only a continue disposition is recorded",
                                    task_id=task_id, step_id=step.step_id)
        # 必須確實指回本步那一筆失敗結果，否則就是張冠李戴。
        referenced = payload.get("failed_result_event_id")
        if referenced is None or step.result_event_id != referenced:
            raise TaskEvidenceError(
                "the disposition does not reference this step's failed result",
                task_id=task_id, step_id=step.step_id)
        if (step.result_envelope is None
                or step.result_envelope.result_sha256
                != payload.get("failed_result_sha256")):
            raise TaskEvidenceError("disposition result hash mismatch",
                                    task_id=task_id, step_id=step.step_id)
        step.analysis_disposition = {
            "step_id": step.step_id, "capability_id": step.capability_id,
            "policy_id": payload.get("policy_id"),
            "disposition": payload.get("disposition"),
            "failure_kind": payload.get("failure_kind"),
            "reason": payload.get("reason"),
        }


def interpret_restarted_task(history: TaskHistory) -> tuple[str, str]:
    """新 daemon 對舊 task 的**唯讀**判讀。回傳 `(status, reason)`。

    這個函式不產生任何執行權，也不寫任何 recovery 記錄。
    """
    if history.truncated:
        return ("OUTCOME_UNCONFIRMED",
                "task history could not be read completely; evidence is incomplete")

    if history.terminal is not None:
        status = history.terminal.get("status")
        if isinstance(status, str) and status in M.TERMINAL_TASK_STATUSES:
            return status, str(history.terminal.get("reason") or "recorded terminal state")
        return ("OUTCOME_UNCONFIRMED", "terminal event has an unrecognised status")

    # 有 intent、卻沒有可信 receipt ⇒ 不知道到底派送成功沒有。
    for step in history.steps.values():
        if step.intent_recorded and not step.has_trusted_receipt:
            return ("OUTCOME_UNCONFIRMED",
                    f"step {step.step_id} recorded a dispatch intent but has no "
                    "trusted receipt; whether it actually ran is unknown")
        # 只有 result、沒有 receipt ⇒ 同樣不能推論 finalization 已完成。
        if step.result_envelope is not None and not step.has_trusted_receipt:
            return ("OUTCOME_UNCONFIRMED",
                    f"step {step.step_id} has a result but no completion receipt")

    if history.grant is None:
        # 從未啟動的舊 draft。不再依草案年齡判
        # EXPIRED（v1 snapshot 的 `draft_expires_at_ms` 只是歷史欄位）；已落盤
        # 為 EXPIRED 的 terminal event 在上方原樣採用，不復活也不改寫。
        return ("INTERRUPTED",
                "the draft belongs to a previous daemon instance and can no longer "
                "be started; create a new task")

    return ("INTERRUPTED",
            "the daemon restarted before this task finished; completed results are "
            "retained and the remaining steps were never executed")



__all__ = [
    "MAX_TASK_EVENTS",
    "StepHistory",
    "TaskEvidenceError",
    "TaskHistory",
    "TaskHistoryReader",
    "interpret_restarted_task",
]
