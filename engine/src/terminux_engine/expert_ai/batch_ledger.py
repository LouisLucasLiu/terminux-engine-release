# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""EAP batch 的即時 append-only 帳本。

帳本只由**已持久化的權威事件**驅動：Work Runtime 的 `engineering_work.event.v1`
（預算 reserve／consume／release 與 provider dispatch／response／failure）、
FlowTask lifecycle 事件（建立、開始、ORFS launch、candidate materialization）
以及 task body narration permit。它不是輪詢快照，也不是事後報告。

規則：

- 每列有全域單調 `sequence`；追加在檔案鎖內重讀最後一列決定下一個值，因此
  daemon 中止重啟、或另一個 reader 開啟後仍然單調。
- 寫入後 `flush` + `fsync`；新建檔案另外 `fsync` 所在目錄。
- 只追加。既有列不修改、不刪除；最後一列不完整或 sequence 不連續即拒絕追加
  （fail closed），不自行修補。
- 未設定 `TERMINUX_EAP_BATCH_LEDGER_FILE` 時完全不啟用，既有行為不變。
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

LEDGER_ENV = "TERMINUX_EAP_BATCH_LEDGER_FILE"
SCHEMA_VERSION = "eap.batch_ledger.v2"
LEGACY_SCHEMA_VERSION = "eap.batch_ledger.v1"

#: Work budget key → ledger kind。
BUDGET_KINDS: dict[str, str] = {
    "provider_dispatches": "provider_dispatch",
    "tool_launches": "tool_launch",
    "formal_proof_launches": "formal_proof_launch",
    "candidate_materializations": "candidate_materialization",
    "engineering_attempts": "engineering_attempt",
}
OPERATIONS = frozenset({"reserve", "consume", "release", "prepared", "consumed",
                        "released", "dispatch", "response", "failure", "created",
                        "started", "linked", "milestone"})
#: 由 Work 事件直接記錄的 provider 生命週期 action。
_PROVIDER_ACTIONS: dict[str, str] = {
    "ROLE_DISPATCHED": "dispatch", "ROLE_COMPLETED": "response", "ROLE_FAILED": "failure",
    "EXPERT_DISPATCHED": "dispatch", "EXPERT_COMPLETED": "response",
    "EXPERT_FAILED": "failure",
}


class BatchLedgerError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "BATCH_LEDGER_INVALID"
        self.message = message


def read_rows(path: str | Path) -> list[dict[str, Any]]:
    """讀回並驗證整份帳本。任何不完整、不可解析或不連續即拋出。"""
    file_path = Path(path)
    if not file_path.exists():
        return []
    raw = file_path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise BatchLedgerError("the ledger ends with an incomplete row")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise BatchLedgerError(f"ledger row {number} is not JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise BatchLedgerError(f"ledger row {number} is not an object")
        rows.append(row)
    live = [row for row in rows if row.get("schema_version") in {
        LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}]
    for previous, current in zip(live, live[1:]):
        if int(current.get("sequence", 0)) != int(previous.get("sequence", 0)) + 1:
            raise BatchLedgerError(
                f"ledger sequence is not monotonic at {current.get('sequence')}")
    if live and int(live[0].get("sequence", 0)) != 1:
        raise BatchLedgerError("the first live ledger row must have sequence 1")
    reservations: dict[str, dict[str, Any]] = {}
    idempotency: dict[str, str] = {}
    for row in live:
        if row.get("schema_version") != SCHEMA_VERSION:
            continue
        operation = row.get("operation")
        reservation_id = row.get("reservation_id")
        if operation == "prepared":
            if not isinstance(reservation_id, str) or not reservation_id:
                raise BatchLedgerError("prepared row has no reservation identity")
            if reservation_id in reservations:
                raise BatchLedgerError("reservation identity is duplicated")
            key = row.get("idempotency_key")
            if not isinstance(key, str) or not key:
                raise BatchLedgerError("prepared row has no idempotency key")
            old_id = idempotency.get(key)
            if old_id is not None and old_id != reservation_id:
                raise BatchLedgerError("idempotency key maps to multiple reservations")
            reservations[reservation_id] = row
            idempotency[key] = reservation_id
        elif operation in {"consumed", "released"}:
            if not isinstance(reservation_id, str) or reservation_id not in reservations:
                raise BatchLedgerError("reservation settlement has no prepared lineage")
            prepared = reservations[reservation_id]
            if any(row.get(key) != prepared.get(key)
                   for key in ("source", "work_id", "task_id", "kind", "quantity")):
                raise BatchLedgerError("reservation settlement lineage mismatches preparation")
            if "event_id" not in (row.get("evidence_ref") or {}):
                raise BatchLedgerError("reservation settlement has no durable event reference")
            if prepared.get("settled_operation") is not None:
                raise BatchLedgerError("reservation is settled more than once")
            prepared["settled_operation"] = operation
    return rows


def totals(rows: list[dict[str, Any]], work_id: str) -> dict[str, dict[str, int]]:
    """由即時列重算某 Work 的 consumed／outstanding reserved（依 budget key）。"""
    result = {key: {"consumed": 0, "reserved": 0} for key in BUDGET_KINDS}
    kinds = {kind: key for key, kind in BUDGET_KINDS.items()}
    for row in rows:
        if row.get("schema_version") not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION} \
                or row.get("work_id") != work_id:
            continue
        key = kinds.get(str(row.get("kind")))
        if key is None or row.get("source") != "work_runtime":
            continue
        quantity = int(row.get("quantity") or 0)
        operation = row.get("operation")
        if operation in {"reserve", "prepared"}:
            result[key]["reserved"] += quantity
        elif operation in {"consume", "consumed"}:
            result[key]["consumed"] += quantity
            result[key]["reserved"] = max(0, result[key]["reserved"] - quantity)
        elif operation in {"release", "released"}:
            result[key]["reserved"] = max(0, result[key]["reserved"] - quantity)
    return result


def reconcile(rows: list[dict[str, Any]], *, work: dict[str, Any],
              flow_tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """即時帳本與 Runtime counter 的共同核對。回傳逐項結果與 `matched`。"""
    work_id = str(work.get("work_id"))
    budget = work.get("budget") or {}
    limits = budget.get("limits") or {}
    used = budget.get("used") or {}
    reserved = budget.get("reserved") or {}
    ledger = totals(rows, work_id)
    checks: dict[str, Any] = {}
    for key in BUDGET_KINDS:
        checks[key] = {
            "runtime_used": int(used.get(key, 0)),
            "ledger_consumed": ledger[key]["consumed"],
            "runtime_reserved": int(reserved.get(key, 0)),
            "ledger_reserved": ledger[key]["reserved"],
            "limit": limits.get(key),
        }
        checks[key]["matched"] = (
            checks[key]["runtime_used"] == checks[key]["ledger_consumed"]
            and checks[key]["runtime_reserved"] == checks[key]["ledger_reserved"]
            and (limits.get(key) is None or
                 checks[key]["ledger_consumed"] + checks[key]["ledger_reserved"]
                 <= int(limits[key])))
    task_rows = [row for row in rows if row.get("schema_version") in {
                 LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
                 and row.get("work_id") == work_id and row.get("source") == "flow_task"]
    task_checks: dict[str, Any] = {}
    for task in flow_tasks or []:
        task_id = str(task.get("task_id"))
        counters = task.get("budget") or {}
        mine = [row for row in task_rows if row.get("task_id") == task_id]
        count = {kind: sum(int(row.get("quantity") or 0) for row in mine
                           if row.get("kind") == kind and row.get("operation") in {
                               "consume", "consumed"})
                 for kind in ("tool_launch", "candidate_materialization", "flow_narration")}
        task_checks[task_id] = {
            "eda_launches_used": counters.get("eda_launches_used"),
            "ledger_tool_launches": count["tool_launch"],
            "candidate_writes_used": counters.get("candidate_writes_used"),
            "ledger_candidate_materializations": count["candidate_materialization"],
            "body_llm_dispatches_used": counters.get("body_llm_dispatches_used"),
            "ledger_flow_narrations": count["flow_narration"],
            "created": any(row.get("operation") == "created" for row in mine),
            "started": any(row.get("operation") == "started" for row in mine),
        }
        entry = task_checks[task_id]
        entry["matched"] = (entry["created"] and entry["started"]
            and entry["eda_launches_used"] == entry["ledger_tool_launches"]
            and entry["candidate_writes_used"] == entry["ledger_candidate_materializations"]
            and entry["body_llm_dispatches_used"] == entry["ledger_flow_narrations"])
    return {"work": checks, "flow_tasks": task_checks,
            "matched": all(row["matched"] for row in checks.values())
            and all(row["matched"] for row in task_checks.values())}


class BatchLedger:
    """單一檔案的 append-only 帳本。thread-safe，並以檔案鎖跨行程保證單調。"""

    def __init__(self, path: str | Path) -> None:
        file_path = Path(path)
        if not file_path.is_absolute():
            raise BatchLedgerError("the batch ledger path must be absolute")
        if not file_path.parent.is_dir():
            raise BatchLedgerError("the batch ledger directory does not exist")
        self._path = file_path
        self._lock = threading.Lock()
        # 開啟時即驗證既有內容；壞帳本不得靜默接續。
        read_rows(file_path)
        #: Work Runtime 同步點：work_id → 已入帳的 {key: (consumed, reserved)}。
        self._marks: dict[str, dict[str, tuple[int, int]]] = {}

    @property
    def path(self) -> Path:
        return self._path

    def append(self, **fields: Any) -> dict[str, Any]:
        operation = fields.get("operation")
        if operation not in OPERATIONS:
            raise BatchLedgerError(f"unsupported ledger operation: {operation!r}")
        with self._lock:
            created = not self._path.exists()
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                rows = read_rows(self._path)
                live = [row for row in rows if row.get("schema_version") in {
                    LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}]
                sequence = int(live[-1]["sequence"]) + 1 if live else 1
                row = {"schema_version": SCHEMA_VERSION, "sequence": sequence,
                       "timestamp": time.time(), **fields}
                data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
                written = os.write(descriptor, data)
                if written != len(data):
                    raise BatchLedgerError("the ledger row was not fully written")
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            if created:
                directory = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        return row

    def prepare_reservation(
        self, *, source: str, work_id: str | None, task_id: str | None,
        kind: str, quantity: int, idempotency_key: str, identity: dict[str, Any],
    ) -> dict[str, Any]:
        """在外部動作前 durable 寫入 reservation。

        此方法以 idempotency key 去重；相同 key 只能回傳同一筆 PREPARED 列，
        不會產生第二份可消耗額度。它刻意不與 EventStore 宣稱為原子交易。
        """
        if kind not in set(BUDGET_KINDS.values()) | {"flow_narration", "flow_task"}:
            raise BatchLedgerError(f"unsupported reservation kind: {kind}")
        if type(quantity) is not int or quantity < 0:
            raise BatchLedgerError("reservation quantity must be a nonnegative integer")
        if not idempotency_key:
            raise BatchLedgerError("reservation idempotency key is required")
        with self._lock:
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                rows = read_rows(self._path)
                previous = next((row for row in rows
                                 if row.get("schema_version") == SCHEMA_VERSION
                                 and row.get("operation") == "prepared"
                                 and row.get("idempotency_key") == idempotency_key), None)
                if previous is not None:
                    expected = {"source": source, "work_id": work_id, "task_id": task_id,
                                "kind": kind, "quantity": quantity}
                    if any(previous.get(key) != value for key, value in expected.items()):
                        raise BatchLedgerError("idempotency key conflicts with reservation identity")
                    return previous
                live = [row for row in rows if row.get("schema_version") in {
                    LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}]
                sequence = int(live[-1]["sequence"]) + 1 if live else 1
                reservation_id = "lres_" + uuid.uuid4().hex
                row = {
                    "schema_version": SCHEMA_VERSION, "sequence": sequence,
                    "timestamp": time.time(), "source": source, "work_id": work_id,
                    "task_id": task_id, "kind": kind, "quantity": quantity,
                    "operation": "prepared", "reservation_state": "RESERVED",
                    "reservation_id": reservation_id, "idempotency_key": idempotency_key,
                    **identity,
                }
                data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
                if os.write(descriptor, data) != len(data):
                    raise BatchLedgerError("the ledger reservation was not fully written")
                os.fsync(descriptor)
                return row
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def settle_reservation(
        self, reservation_id: str, *, operation: str, evidence_ref: dict[str, Any],
    ) -> dict[str, Any]:
        """以同一份持久事件將 reservation 結算為 CONSUMED 或 RELEASED。"""
        if operation not in {"consumed", "released"}:
            raise BatchLedgerError("reservation settlement must be consumed or released")
        if not evidence_ref.get("event_id"):
            raise BatchLedgerError("reservation settlement requires an event identity")
        with self._lock:
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                rows = read_rows(self._path)
                prepared = next((row for row in rows
                                 if row.get("schema_version") == SCHEMA_VERSION
                                 and row.get("operation") == "prepared"
                                 and row.get("reservation_id") == reservation_id), None)
                if prepared is None:
                    raise BatchLedgerError("reservation preparation is unavailable")
                settled = [row for row in rows if row.get("schema_version") == SCHEMA_VERSION
                           and row.get("reservation_id") == reservation_id
                           and row.get("operation") in {"consumed", "released"}]
                if settled:
                    if settled[0].get("operation") != operation:
                        raise BatchLedgerError("reservation has a conflicting settlement")
                    return settled[0]
                live = [row for row in rows if row.get("schema_version") in {
                    LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}]
                sequence = int(live[-1]["sequence"]) + 1 if live else 1
                row = {key: prepared.get(key) for key in (
                    "source", "work_id", "task_id", "kind", "quantity", "attempt_id",
                    "authorization_digest", "session_id", "dispatch_class", "request_id",
                    "step_id", "capability_id", "grant_id", "idempotency_key")}
                row.update({"schema_version": SCHEMA_VERSION, "sequence": sequence,
                            "timestamp": time.time(), "operation": operation,
                            "reservation_state": operation.upper(),
                            "reservation_id": reservation_id,
                            "evidence_ref": dict(evidence_ref)})
                data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
                if os.write(descriptor, data) != len(data):
                    raise BatchLedgerError("the ledger settlement was not fully written")
                os.fsync(descriptor)
                return row
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    # ── Work Runtime ────────────────────────────────────────────────

    def record_work_event(self, work: dict[str, Any], *, action: str,
                          detail: dict[str, Any], event_id: str) -> list[dict[str, Any]]:
        """由一筆已持久化的 Work 事件同步預算變化與 provider 生命週期。"""
        work_id = str(work["work_id"])
        authorization = work.get("authorization") or {}
        common = {
            "source": "work_runtime", "work_id": work_id,
            "authorization_digest": authorization.get("authorization_digest"),
            "session_id": (work.get("scope") or {}).get("origin_session_id"),
            "evidence_ref": {"event_type": "engineering_work.event.v1",
                             "event_id": event_id, "work_sequence": work.get("sequence"),
                             "action": action},
        }
        request_id = detail.get("request_id") if isinstance(detail, dict) else None
        call = next((row for row in reversed(work.get("role_calls") or [])
                     if request_id and row.get("request_id") == request_id), None)
        attempts = work.get("attempts") or []
        attempt = attempts[-1] if attempts else {}
        written: list[dict[str, Any]] = []
        reservation_identities = detail.get("reservation_identities") if isinstance(detail, dict) else None
        # v2 Work snapshot 已帶 reservation 狀態時，所有預算都只能由 reservation
        # 結算；即使本事件沒有新 reservation 也不可退回 v1 的快照差額補帳。
        if "_ledger_reservations" in work:
            if reservation_identities is None:
                reservation_identities = []
            if not isinstance(reservation_identities, list):
                raise BatchLedgerError("work event reservation lineage is invalid")
            for reservation in reservation_identities:
                if not isinstance(reservation, dict):
                    raise BatchLedgerError("work event reservation lineage is invalid")
                reservation_id = reservation.get("reservation_id")
                settlement = reservation.get("settlement")
                if not isinstance(reservation_id, str) or settlement not in {
                        "CONSUMED", "RELEASED", "PREPARED"}:
                    raise BatchLedgerError("work event reservation identity is invalid")
                if settlement != "PREPARED":
                    written.append(self.settle_reservation(
                        reservation_id, operation=settlement.lower(),
                        evidence_ref={**common["evidence_ref"], "reservation_id": reservation_id}))
            # v2 的預算完全由 reservation 驅動，絕不再依事件後快照差額補帳。
            lifecycle = _PROVIDER_ACTIONS.get(action)
            if lifecycle is not None:
                written.append(self.append(
                    **common, kind="provider_call", operation=lifecycle, quantity=0,
                    request_id=request_id or (detail or {}).get("call_key"),
                    dispatch_class=(call or detail or {}).get("dispatch_class", "mainline"),
                    purpose=(call or detail or {}).get("purpose"), role=(call or detail or {}).get("role"),
                    expert_id=(detail or {}).get("expert_id"),
                    request_digest=(call or detail or {}).get("request_digest"),
                    response_digest=(detail or {}).get("response_digest")
                    or (call or {}).get("response_digest"),
                    error_code=(detail or {}).get("error_code"),
                    reservation_ids=[row.get("reservation_id") for row in reservation_identities]))
            return written
        if work_id not in self._marks:
            recovered = totals(read_rows(self._path), work_id)
            self._marks[work_id] = {key: (value["consumed"], value["reserved"])
                                    for key, value in recovered.items()}
        marks = self._marks[work_id]
        budget = work.get("budget") or {}
        for key, kind in BUDGET_KINDS.items():
            used = int((budget.get("used") or {}).get(key, 0))
            reserved = int((budget.get("reserved") or {}).get(key, 0))
            ledger_used, ledger_reserved = marks.get(key, (0, 0))
            identity = {
                "kind": kind,
                "attempt_id": attempt.get("attempt_id") if key in (
                    "engineering_attempts", "tool_launches", "formal_proof_launches",
                    "candidate_materializations") else None,
                "task_id": attempt.get("task_id") if key != "provider_dispatches" else None,
                "dispatch_class": ((call or {}).get("dispatch_class", "mainline")
                                   if key == "provider_dispatches" and call else None),
                "request_id": request_id,
            }
            if used > ledger_used:
                from_reservation = min(used - ledger_used, ledger_reserved)
                written.append(self.append(**common, **identity, operation="consume",
                                           quantity=used - ledger_used,
                                           from_reservation=from_reservation,
                                           runtime_used=used, runtime_reserved=reserved))
                ledger_used, ledger_reserved = used, ledger_reserved - from_reservation
            if reserved > ledger_reserved:
                written.append(self.append(**common, **identity, operation="reserve",
                                           quantity=reserved - ledger_reserved,
                                           runtime_used=used, runtime_reserved=reserved))
            elif reserved < ledger_reserved:
                written.append(self.append(**common, **identity, operation="release",
                                           quantity=ledger_reserved - reserved,
                                           runtime_used=used, runtime_reserved=reserved))
            marks[key] = (used, reserved)
        lifecycle = _PROVIDER_ACTIONS.get(action)
        if lifecycle is not None:
            written.append(self.append(
                **common, kind="provider_call", operation=lifecycle, quantity=0,
                request_id=request_id or (detail or {}).get("call_key"),
                dispatch_class=(call or detail or {}).get("dispatch_class", "mainline"),
                purpose=(call or detail or {}).get("purpose"),
                role=(call or detail or {}).get("role"),
                expert_id=(detail or {}).get("expert_id"),
                request_digest=(call or detail or {}).get("request_digest"),
                response_digest=(detail or {}).get("response_digest")
                or (call or {}).get("response_digest"),
                error_code=(detail or {}).get("error_code")))
        return written

    def work_totals(self, work_id: str) -> dict[str, dict[str, int]]:
        return totals(read_rows(self._path), work_id)

    # ── FlowTask ───────────────────────────────────────────────────

    def record_task_event(self, *, event_type: str, event_id: str, task_id: str,
                          session_id: str | None, body: dict[str, Any],
                          identity: dict[str, Any]) -> dict[str, Any] | None:
        """FlowTask 建立、開始、ORFS launch、materialization 與 run 連結。"""
        if not identity.get("work_id"):
            # 重啟後 writer 沒有 snapshot 快取：沿用本帳本已記錄的建立列身分。
            created = next((row for row in read_rows(self._path)
                            if row.get("schema_version") == SCHEMA_VERSION
                            and row.get("task_id") == task_id
                            and row.get("operation") == "created"), None)
            if created is not None:
                identity = {key: created.get(key) for key in
                            ("work_id", "attempt_id", "authorization_digest")}
        common = {"source": "flow_task", "task_id": task_id, "session_id": session_id,
                  "evidence_ref": {"event_type": event_type, "event_id": event_id},
                  **identity}
        if event_type == "flow_task.snapshot.v1":
            return self.append(**common, kind="flow_task", operation="created", quantity=1)
        if event_type == "flow_task.started.v1":
            return self.append(**common, kind="flow_task", operation="started", quantity=1,
                               grant_id=body.get("grant_id"))
        if event_type == "flow_task.step_intent.v1":
            binding = body.get("binding") or {}
            capability = str(binding.get("capability_id") or "")
            kind = ("tool_launch" if capability.endswith(".run_flow")
                    else "candidate_materialization" if ".prepare_" in capability
                    else None)
            if kind is None:
                return None
            return self.append(**common, kind=kind, operation="consume", quantity=1,
                               step_id=body.get("step_id"), capability_id=capability,
                               grant_id=binding.get("grant_id"))
        if event_type == "flow_task.run_link.v1":
            run = body.get("run") or {}
            return self.append(**common, kind="tool_launch", operation="linked", quantity=0,
                               run_id=run.get("run_id"), slot=run.get("slot"),
                               step_id=body.get("step_id"))
        return None

    def prepare_task_event(self, *, event_type: str, event_id: str, task_id: str,
                           session_id: str | None, body: dict[str, Any],
                           identity: dict[str, Any]) -> dict[str, Any] | None:
        """在 FlowTask 權威事件前保留其 lifecycle／外部動作用量。"""
        if not identity.get("work_id"):
            created = next((row for row in read_rows(self._path)
                            if row.get("schema_version") in {
                                LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
                            and row.get("task_id") == task_id
                            and row.get("operation") == "created"), None)
            if created is not None:
                identity = {key: created.get(key) for key in (
                    "work_id", "attempt_id", "authorization_digest")}
        if event_type == "flow_task.snapshot.v1":
            kind, quantity = "flow_task", 0
        elif event_type == "flow_task.started.v1":
            kind, quantity = "flow_task", 0
        elif event_type == "flow_task.step_intent.v1":
            capability = str((body.get("binding") or {}).get("capability_id") or "")
            if capability.endswith(".run_flow"):
                kind, quantity = "tool_launch", 1
            elif ".prepare_" in capability:
                kind, quantity = "candidate_materialization", 1
            else:
                return None
        else:
            return None
        return self.prepare_reservation(
            source="flow_task", work_id=identity.get("work_id"), task_id=task_id,
            kind=kind, quantity=quantity, idempotency_key=f"flow-event:{event_id}",
            identity={"attempt_id": identity.get("attempt_id"),
                      "authorization_digest": identity.get("authorization_digest"),
                      "session_id": session_id, "event_type": event_type,
                      "step_id": body.get("step_id"),
                      "capability_id": (body.get("binding") or {}).get("capability_id"),
                      "grant_id": (body.get("binding") or {}).get("grant_id")})

    def settle_task_event(self, reservation: dict[str, Any], *, event_type: str,
                          event_id: str) -> dict[str, Any]:
        """FlowTask 事件成功落盤後立刻消耗其 reservation。"""
        settled = self.settle_reservation(
            str(reservation["reservation_id"]), operation="consumed",
            evidence_ref={"event_type": event_type, "event_id": event_id,
                          "reservation_id": reservation["reservation_id"]})
        lifecycle = {"flow_task.snapshot.v1": "created",
                     "flow_task.started.v1": "started"}.get(event_type)
        if lifecycle is not None:
            self.append(source="flow_task", work_id=reservation.get("work_id"),
                        task_id=reservation.get("task_id"), kind="flow_task",
                        operation=lifecycle, quantity=1,
                        attempt_id=reservation.get("attempt_id"),
                        authorization_digest=reservation.get("authorization_digest"),
                        session_id=reservation.get("session_id"),
                        evidence_ref={"event_type": event_type, "event_id": event_id,
                                      "reservation_id": reservation["reservation_id"]})
        return settled

    def record_narration(self, *, task_id: str, session_id: str | None,
                         identity: dict[str, Any], used: int, limit: int) -> dict[str, Any]:
        """task body narration permit：取得額度即視為一次 provider dispatch。"""
        return self.append(source="flow_task", kind="flow_narration", operation="consume",
                           quantity=1, task_id=task_id, session_id=session_id,
                           dispatch_class="flow_narration", runtime_used=used,
                           runtime_limit=limit,
                           evidence_ref={"counter": "task_body_budget"}, **identity)


_CONFIGURED: dict[str, BatchLedger] = {}
_CONFIGURED_LOCK = threading.Lock()


def configured_ledger() -> BatchLedger | None:
    """由 daemon 環境取得（單一行程共用）的帳本；未設定回 None。"""
    path = os.environ.get(LEDGER_ENV)
    if not path:
        return None
    with _CONFIGURED_LOCK:
        ledger = _CONFIGURED.get(path)
        if ledger is None:
            ledger = BatchLedger(path)
            _CONFIGURED[path] = ledger
        return ledger


def task_identity(task: Any) -> dict[str, Any]:
    """由 FlowTask snapshot 取出 Work／attempt／授權身分。"""
    proposal = getattr(task, "eap_proposal", None)
    if not isinstance(proposal, dict):
        return {"work_id": None, "attempt_id": None, "authorization_digest": None}
    source = proposal.get("work_source") or proposal.get("source") or {}
    return {"work_id": source.get("work_id"), "attempt_id": source.get("attempt_id"),
            "authorization_digest": source.get("parent_authorization_digest")}


__all__ = [
    "BUDGET_KINDS", "BatchLedger", "BatchLedgerError", "LEDGER_ENV", "SCHEMA_VERSION",
    "configured_ledger", "read_rows", "reconcile", "task_identity", "totals",
]
