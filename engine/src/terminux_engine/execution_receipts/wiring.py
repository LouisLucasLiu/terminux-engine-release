# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""把 execution receipt 投影到既有 EventStore 的 production wiring。

Vendor-neutral：這裡不認識 ORFS、也不認識任何 adapter。商用或 native adapter
走的是**完全相同**的機制——呼叫端只要交出一份 vendor-neutral execution contract／artifact lineage receipt 與一個
EventStore 就行。

未注入 projector 時，既有 caller 的行為完全不變（`project_execution_receipt`
在 `event_store is None` 時直接回 `None`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from terminux_engine.execution_receipts.projection import (
    RECEIPT_RECORD_TYPE,
    ProjectionResult,
    build_projection,
    project_receipt,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptRow:
    """一筆 receipt 投影與寫入它的 EventStore 身分。"""

    payload: dict[str, Any]
    user_id: str | None
    event_id: str | None
    session_id: str | None


class EventStoreReceiptProjector:
    """在既有 EventStore 之上的最小 append-only 介面。

    只有 `query`／`append` 兩個動作——沒有 update、沒有 delete，因此不可能被
    當成 mutable lifecycle ledger 使用。
    """

    def __init__(self, event_store: Any, *, session_id: str = "",
                 user_id: str = "", project_id: str | None = None) -> None:
        self._store = event_store
        self._session_id = session_id
        self._user_id = user_id
        self._project_id = project_id

    # ── read ─────────────────────────────────────────────────────────
    def query_by_receipt_id(self, receipt_id: str) -> list[dict[str, Any]]:
        return [r for r in self._all() if r.get("receipt_id") == receipt_id]

    def query_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        return [r for r in self._all() if r.get("run_id") == run_id]

    def query_by_content_hash(self, content_hash: str) -> list[dict[str, Any]]:
        return [r for r in self._all() if r.get("content_hash") == content_hash]

    def query_rows_by_run_id(self, run_id: str) -> list["ReceiptRow"]:
        """同 `query_by_run_id`，但連同寫入該列的 EventStore 身分一起回傳。

        供需要判斷「這筆投影是誰寫的」的呼叫端（例如 Run 綁定、OrchestrationService 的
        caller 擁有權邊界）使用；invalidation 排除規則與 `_all()` 完全相同。
        """
        return [row for row in self._all_rows() if row.payload.get("run_id") == run_id]

    def query_payloads_by_run_id(self, run_id: str) -> list[dict[str, Any]]:
        """只回傳**由本 projector 身分寫入**的 receipt 投影 payload（dict）。

        給既有以 dict 消費 receipt 的 flow 呼叫端（run input evidence、flow run evidence、
        package flow）使用：它們需要 payload 契約，但不得因此拿到別的寫入者的列。寫入者
        身分的判定與 `query_rows_by_run_id` 相同；projector 沒有身分（`user_id` 為空）時
        無法擔保任何一列，一律回空（fail closed）。此 reader **不能**替代帶身分的列作為
        caller 授權邊界——OrchestrationService 仍只接受 `query_rows_by_run_id`。
        """
        if not self._user_id:
            return []
        return [row.payload for row in self._all_rows()
                if row.payload.get("run_id") == run_id and row.user_id == self._user_id]

    def _all_rows(self) -> Iterable["ReceiptRow"]:
        from terminux_engine.evidence_integrity.invalidation import (
            load_invalidation_set,
        )

        invalidated = load_invalidation_set()
        rows = self._store.query(event_type=RECEIPT_RECORD_TYPE, limit=10000)
        for row in rows or []:
            payload = getattr(row, "payload", None)
            if not isinstance(payload, dict):
                continue
            if invalidated.event_invalidated(getattr(row, "event_id", None)):
                continue
            if invalidated.run_invalidated(payload.get("run_id")):
                continue
            yield ReceiptRow(payload=payload, user_id=getattr(row, "user_id", None),
                             event_id=getattr(row, "event_id", None),
                             session_id=getattr(row, "session_id", None))

    def _all(self) -> Iterable[dict[str, Any]]:
        """正式 receipt reader。**預設排除**已被 invalidation record 標記的紀錄。

        排除以 event_id 與 orchestration run_id 兩軸比對：原始 row
        仍在 EventStore 裡供 forensic 直接查詢，但不再進入任何產品判定。

        invalidation authority 讀不到時**不**靜默放行——`load_invalidation_set`
        會拋，讓呼叫端看到錯誤。
        """
        from terminux_engine.evidence_integrity.invalidation import (
            load_invalidation_set,
        )

        invalidated = load_invalidation_set()
        rows = self._store.query(event_type=RECEIPT_RECORD_TYPE, limit=10000)
        for row in rows or []:
            payload = getattr(row, "payload", None)
            if not isinstance(payload, dict):
                continue
            if invalidated.event_invalidated(getattr(row, "event_id", None)):
                continue
            if invalidated.run_invalidated(payload.get("run_id")):
                continue
            yield payload

    # ── write ────────────────────────────────────────────────────────
    def append(self, payload: dict[str, Any]) -> bool:
        import time
        from uuid import uuid4

        from terminux_engine.store.event_store import SessionEvent

        self._store.emit(SessionEvent(
            event_id=uuid4().hex, session_id=self._session_id,
            terminal_id="default", user_id=self._user_id,
            project_id=self._project_id, timestamp=time.time(),
            event_type=RECEIPT_RECORD_TYPE, payload=payload,
        ))
        return True


def project_execution_receipt(
    receipt: Any, *, event_store: Any | None,
    source_receipt_sha256: str | None = None,
    argv: Iterable[str] | None = None, cwd: str | None = None,
    session_id: str = "", user_id: str = "", project_id: str | None = None,
) -> ProjectionResult | None:
    """把一份**已完整形成**的 receipt 投影出去。

    `event_store` 為 `None` ⇒ 直接回 `None`，既有 caller 行為不變。
    投影失敗不會改變 tool execution 的結果——兩者是分開的軸。
    """
    if event_store is None:
        return None
    projector = EventStoreReceiptProjector(
        event_store, session_id=session_id, user_id=user_id, project_id=project_id)
    payload = build_projection(receipt, source_receipt_sha256=source_receipt_sha256,
                               argv=argv, cwd=cwd)
    result = project_receipt(
        payload,
        read_existing=lambda: projector.query_by_receipt_id(payload["receipt_id"]),
        write=projector.append)
    if not result.durable:
        _log.warning("execution receipt projection not durable: %s (%s)",
                     result.outcome, result.error_code)
    return result


__all__ = ["EventStoreReceiptProjector", "ReceiptRow", "project_execution_receipt"]
