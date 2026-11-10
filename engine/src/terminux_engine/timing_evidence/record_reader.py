# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""從既有 EventStore 讀回 `timing_path_evidence` record 的唯讀介面。

與 `execution_receipts.wiring.EventStoreReceiptProjector` 同一模式：只有 query、沒有
update／delete；排除已作廢的 event 與 run；一併回傳寫入者身分，供 bundle 組裝端
做本人過濾。Record 由 tool adapter 的 after hook 經 `RecordRequest` 寫入，這裡不解讀
payload 的語意，只做 run_id 過濾。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from terminux_engine.timing_evidence.model import RECORD_TYPE

_MAX_ROWS = 10000


@dataclass(frozen=True)
class TimingPathRow:
    """一筆 record payload 與寫入它的 EventStore 身分。"""

    payload: dict[str, Any]
    user_id: str | None
    event_id: str | None


class EventStoreTimingPathReader:
    """`query_by_run_id(run_id)` 回傳該 run 的全部 `timing_path_evidence` 列（含外人寫入的）。"""

    def __init__(self, event_store: Any, *, user_id: str | None = None) -> None:
        self._store = event_store
        self._user_id = user_id

    def query_by_run_id(self, run_id: str) -> list[TimingPathRow]:
        return [row for row in self._all_rows()
                if isinstance(row.payload.get("source"), dict)
                and row.payload["source"].get("run_id") == run_id]

    def _all_rows(self) -> Iterable[TimingPathRow]:
        from terminux_engine.evidence_integrity.invalidation import load_invalidation_set

        invalidated = load_invalidation_set()
        rows = self._store.query(event_type=RECORD_TYPE, limit=_MAX_ROWS)
        for row in rows or []:
            payload = getattr(row, "payload", None)
            if not isinstance(payload, dict):
                continue
            if invalidated.event_invalidated(getattr(row, "event_id", None)):
                continue
            source = payload.get("source")
            if isinstance(source, dict) and invalidated.run_invalidated(source.get("run_id")):
                continue
            yield TimingPathRow(payload=payload, user_id=getattr(row, "user_id", None),
                                event_id=getattr(row, "event_id", None))


__all__ = ["EventStoreTimingPathReader", "TimingPathRow"]
