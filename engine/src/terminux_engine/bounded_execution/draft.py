# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Session-bound plan draft cache。

**不是 durable authority。** 它只是為了讓使用者在 `/plan-only` 之後能用
plan id 指名某個 step，而不必把整份 plan 貼回來。權威永遠是已 durable 的
`PlanSnapshot`。

硬性限制：

- key 為 plan id，且**必須**同時比對 member／project／session 才回傳；
- 跨 identity 一律視為不存在（不洩漏「這個 plan 存在」這件事）；
- TTL ＋ 容量上限，超過即 evict；evict 後使用者重新產生 plan 即可；
- 不保存 credential、raw prompt 或 LLM transcript；
- 不保存任何 executor handle。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_TTL_SECONDS = 1800.0
DEFAULT_MAX_ENTRIES = 32

#: 只保留 plan 的內容欄位。`created_from`（可能含 entry／profile 資訊）、
#: renderer 產物與任何 provider 原文都不進 cache。
_RETAINED_PLAN_FIELDS = (
    "schema_version", "plan_id", "mode", "goal", "catalog_snapshot_id",
    "observations", "unknowns", "steps", "global_stop_conditions",
    "limitations", "validation", "execution_allowed",
)


@dataclass(frozen=True)
class PlanDraftEntry:
    plan_id: str
    plan: dict[str, Any]
    member_id: str
    project_id: str
    session_id: str
    created_at: float

    def identity_matches(self, member_id: str, project_id: str, session_id: str) -> bool:
        return (self.member_id == member_id and self.project_id == project_id
                and self.session_id == session_id)


class PlanDraftCache:
    """Bounded、session-scoped、in-memory。Process 結束即消失——刻意如此。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time as _time

        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, PlanDraftEntry] = {}

    # ── internals ────────────────────────────────────────────────────

    def _evict_locked(self) -> None:
        now = self._clock()
        stale = [k for k, e in self._entries.items() if now - e.created_at >= self._ttl]
        for k in stale:
            del self._entries[k]
        while len(self._entries) > self._max:
            oldest = min(self._entries.items(), key=lambda kv: kv[1].created_at)[0]
            del self._entries[oldest]

    @staticmethod
    def _retain(plan: dict[str, Any]) -> dict[str, Any]:
        retained = {k: plan[k] for k in _RETAINED_PLAN_FIELDS if k in plan}
        # Plan-only 的語意是常數；即使呼叫端傳了別的值也強制為 False。
        retained["execution_allowed"] = False
        return retained

    # ── public ───────────────────────────────────────────────────────

    def put(self, plan: dict[str, Any], *, member_id: str, project_id: str,
            session_id: str) -> PlanDraftEntry | None:
        """保存一份 **已驗證通過** 的 plan。invalid plan 不保存。"""
        if (plan.get("validation") or {}).get("status") != "valid":
            return None
        plan_id = plan.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            return None
        entry = PlanDraftEntry(
            plan_id=plan_id, plan=self._retain(plan), member_id=member_id,
            project_id=project_id, session_id=session_id, created_at=self._clock(),
        )
        with self._lock:
            self._entries[plan_id] = entry
            self._evict_locked()
        return entry

    def get(self, plan_id: str, *, member_id: str, project_id: str,
            session_id: str) -> tuple[PlanDraftEntry | None, str | None]:
        """回傳 `(entry, denial_code)`。

        跨 identity 一律回 `PLAN_DRAFT_IDENTITY_MISMATCH`——刻意與
        `PLAN_DRAFT_NOT_FOUND` 分開，讓 host 能記錄真正的原因；但兩者對
        呼叫者都一樣拿不到內容。
        """
        from terminux_engine.bounded_execution.errors import (
            PLAN_DRAFT_IDENTITY_MISMATCH, PLAN_DRAFT_NOT_FOUND,
        )

        with self._lock:
            self._evict_locked()
            entry = self._entries.get(plan_id)
            if entry is None:
                return None, PLAN_DRAFT_NOT_FOUND
            if not entry.identity_matches(member_id, project_id, session_id):
                return None, PLAN_DRAFT_IDENTITY_MISMATCH
            return entry, None

    def __len__(self) -> int:
        with self._lock:
            self._evict_locked()
            return len(self._entries)


__all__ = ["DEFAULT_MAX_ENTRIES", "DEFAULT_TTL_SECONDS", "PlanDraftCache", "PlanDraftEntry"]
