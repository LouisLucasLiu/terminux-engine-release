# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""bounded parallel scheduler——多 task、有上限、超限排隊。

取代 analysis task 以來的「單一 admission lock、第二個 task 一律 `TASK_BUSY`」。
規則：

* 上限是 **user-owned daemon resource setting**（環境變數），不藏在任何
  Profile 裡，也不硬寫成 1。policy 的 `max_concurrent_tasks` 只當**天花板**。
* 額度用盡時，已核准的 task 進入 `QUEUED`；資源釋放後由 scheduler 依
  **deterministic FIFO**（核准順序）啟動下一個。
* 兩個 task 要求同一個 writable root 時不平行寫入：後來者排隊，並清楚顯示
  `writable_root_busy:<root>`。這是避免互相破壞，不是限制工程內容。
* 結果無法確認的 task**不釋放**名額；後面的 task 排隊時看得到原因
  是 `worker_unconfirmed:<task_id>`，不是「忙碌」。
* EDA 與 provider 的平行度各自用 bounded semaphore 控制；沒有無上限的
  thread／subprocess。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

ENV_MAX_ACTIVE_TASKS = "TERMINUX_FLOW_MAX_ACTIVE_TASKS"
ENV_MAX_PARALLEL_EDA = "TERMINUX_FLOW_MAX_PARALLEL_EDA"
ENV_MAX_PARALLEL_PROVIDER = "TERMINUX_FLOW_MAX_PARALLEL_PROVIDER"

DEFAULT_MAX_ACTIVE_TASKS = 1
DEFAULT_MAX_PARALLEL_EDA = 1
DEFAULT_MAX_PARALLEL_PROVIDER = 1

QUEUE_REASON_ACTIVE_LIMIT = "active_task_limit"
QUEUE_REASON_WRITABLE_ROOT = "writable_root_busy"
QUEUE_REASON_UNCONFIRMED = "worker_unconfirmed"


class SchedulerConfigError(ValueError):
    """資源設定不是正整數。"""


def _read_limit(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SchedulerConfigError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value < 1:
        raise SchedulerConfigError(f"{name} must be at least 1, got {value}")
    return value


@dataclass(frozen=True)
class SchedulerLimits:
    """daemon 的資源上限。"""

    max_active_tasks: int = DEFAULT_MAX_ACTIVE_TASKS
    max_parallel_eda: int = DEFAULT_MAX_PARALLEL_EDA
    max_parallel_provider: int = DEFAULT_MAX_PARALLEL_PROVIDER
    #: 哪一個來源決定了 `max_active_tasks`（env／default／policy ceiling）。
    active_source: str = "default"

    @classmethod
    def from_env(cls, *, policy_ceiling: int | None = None) -> "SchedulerLimits":
        """從環境讀設定；`policy_ceiling`（activation policy 的
        `max_concurrent_tasks`）只能往下壓，不能往上放。"""
        active = _read_limit(ENV_MAX_ACTIVE_TASKS, DEFAULT_MAX_ACTIVE_TASKS)
        source = "env" if os.environ.get(ENV_MAX_ACTIVE_TASKS, "").strip() else "default"
        if policy_ceiling is not None and policy_ceiling >= 1 and policy_ceiling < active:
            active = policy_ceiling
            source = "policy_ceiling"
        return cls(
            max_active_tasks=active,
            max_parallel_eda=_read_limit(ENV_MAX_PARALLEL_EDA, DEFAULT_MAX_PARALLEL_EDA),
            max_parallel_provider=_read_limit(ENV_MAX_PARALLEL_PROVIDER,
                                              DEFAULT_MAX_PARALLEL_PROVIDER),
            active_source=source,
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "max_active_tasks": self.max_active_tasks,
            "max_parallel_eda": self.max_parallel_eda,
            "max_parallel_provider": self.max_parallel_provider,
            "active_source": self.active_source,
            "settings": {
                "max_active_tasks": ENV_MAX_ACTIVE_TASKS,
                "max_parallel_eda": ENV_MAX_PARALLEL_EDA,
                "max_parallel_provider": ENV_MAX_PARALLEL_PROVIDER,
            },
        }


@dataclass
class _QueuedEntry:
    key: str
    sequence: int
    approved_at_ms: int
    writable_roots: frozenset[str]
    on_dispatch: Callable[[], None]


@dataclass
class QueueView:
    key: str
    position: int
    reason: str


@dataclass
class SchedulerSnapshot:
    limits: SchedulerLimits
    active: list[str] = field(default_factory=list)
    queued: list[QueueView] = field(default_factory=list)
    unconfirmed: dict[str, str] = field(default_factory=dict)
    held_roots: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_json_dict(),
            "active": list(self.active),
            "queued": [{"key": q.key, "position": q.position, "reason": q.reason}
                       for q in self.queued],
            "unconfirmed": dict(self.unconfirmed),
            "held_writable_roots": dict(self.held_roots),
        }


class TaskScheduler:
    """一個 daemon 一份。呼叫端持自己的 task lock 時**不得**在鎖內呼叫
    `release()`（它會在鎖外啟動下一個 task）。"""

    def __init__(self, limits: SchedulerLimits) -> None:
        self._limits = limits
        self._lock = threading.Lock()
        self._active: dict[str, frozenset[str]] = {}
        self._unconfirmed: dict[str, str] = {}
        self._queue: list[_QueuedEntry] = []
        self._held_roots: dict[str, str] = {}
        self._sequence = 0
        # 派送層級的平行度。worker 在鎖外 acquire，步驟結束後 release。
        self.eda_slots = threading.BoundedSemaphore(limits.max_parallel_eda)
        self.provider_slots = threading.BoundedSemaphore(limits.max_parallel_provider)

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    # ── admission ────────────────────────────────────────────────────

    def _blocking_reason_locked(self, roots: frozenset[str]) -> str | None:
        occupied = len(self._active)
        if occupied >= self._limits.max_active_tasks:
            if self._unconfirmed:
                holder = sorted(self._unconfirmed)[0]
                return f"{QUEUE_REASON_UNCONFIRMED}:{holder}"
            return QUEUE_REASON_ACTIVE_LIMIT
        for root in sorted(roots):
            holder = self._held_roots.get(root)
            if holder is not None:
                return f"{QUEUE_REASON_WRITABLE_ROOT}:{root}"
        return None

    def _activate_locked(self, key: str, roots: frozenset[str]) -> None:
        self._active[key] = roots
        for root in roots:
            self._held_roots[root] = key

    def admit(
        self, key: str, *, writable_roots: Any, approved_at_ms: int,
        on_dispatch: Callable[[], None],
    ) -> tuple[str, str | None]:
        """回傳 `("RUNNING", None)` 或 `("QUEUED", reason)`。

        RUNNING 代表名額已配給這個 key，**呼叫端**負責立刻啟動 worker。
        QUEUED 代表已排入 FIFO；輪到時 scheduler 會在鎖外呼叫 `on_dispatch`。
        """
        roots = frozenset(str(r) for r in (writable_roots or ()))
        with self._lock:
            if key in self._active or any(e.key == key for e in self._queue):
                raise ValueError(f"task {key!r} is already admitted")
            reason = self._blocking_reason_locked(roots)
            if reason is None:
                self._activate_locked(key, roots)
                return "RUNNING", None
            self._sequence += 1
            self._queue.append(_QueuedEntry(
                key=key, sequence=self._sequence, approved_at_ms=approved_at_ms,
                writable_roots=roots, on_dispatch=on_dispatch))
            return "QUEUED", reason

    # ── release／drain ───────────────────────────────────────────────

    def _drain_locked(self) -> list[_QueuedEntry]:
        """依 FIFO 找出現在可以啟動的 entry；不跳過順序：第一個被 writable
        root 擋住的 entry 之後仍可能有可啟動的（不同 root），但**名額**永遠
        依序分配——這樣 queue 順序既 deterministic 又可查。"""
        dispatched: list[_QueuedEntry] = []
        remaining: list[_QueuedEntry] = []
        for entry in self._queue:
            if self._blocking_reason_locked(entry.writable_roots) is None:
                self._activate_locked(entry.key, entry.writable_roots)
                dispatched.append(entry)
            else:
                remaining.append(entry)
        self._queue = remaining
        return dispatched

    def release(self, key: str) -> list[str]:
        """釋放名額與 writable roots，並啟動下一批可執行的 queued task。
        回傳被啟動的 key（依順序）。**必須在任何 task lock 之外呼叫。**"""
        with self._lock:
            roots = self._active.pop(key, frozenset())
            self._unconfirmed.pop(key, None)
            for root in roots:
                if self._held_roots.get(root) == key:
                    self._held_roots.pop(root, None)
            dispatched = self._drain_locked()
        for entry in dispatched:
            entry.on_dispatch()
        return [entry.key for entry in dispatched]

    def mark_unconfirmed(self, key: str, reason: str) -> None:
        """結果無法確認就保守地**不釋放**名額，只記原因。"""
        with self._lock:
            if key in self._active:
                self._unconfirmed[key] = reason

    def cancel_queued(self, key: str) -> bool:
        """把還在排隊的 task 移出。從未啟動任何 child。"""
        with self._lock:
            before = len(self._queue)
            self._queue = [e for e in self._queue if e.key != key]
            return len(self._queue) != before

    # ── 查詢 ─────────────────────────────────────────────────────────

    def queue_position(self, key: str) -> int | None:
        with self._lock:
            for index, entry in enumerate(self._queue, start=1):
                if entry.key == key:
                    return index
        return None

    def queue_reason(self, key: str) -> str | None:
        with self._lock:
            for entry in self._queue:
                if entry.key == key:
                    return self._blocking_reason_locked(entry.writable_roots) or \
                        QUEUE_REASON_ACTIVE_LIMIT
        return None

    def is_active(self, key: str) -> bool:
        with self._lock:
            return key in self._active

    def snapshot(self) -> SchedulerSnapshot:
        with self._lock:
            queued = [
                QueueView(key=e.key, position=i,
                          reason=self._blocking_reason_locked(e.writable_roots)
                          or QUEUE_REASON_ACTIVE_LIMIT)
                for i, e in enumerate(self._queue, start=1)]
            return SchedulerSnapshot(
                limits=self._limits, active=sorted(self._active),
                queued=queued, unconfirmed=dict(self._unconfirmed),
                held_roots=dict(self._held_roots))


__all__ = [
    "DEFAULT_MAX_ACTIVE_TASKS",
    "DEFAULT_MAX_PARALLEL_EDA",
    "DEFAULT_MAX_PARALLEL_PROVIDER",
    "ENV_MAX_ACTIVE_TASKS",
    "ENV_MAX_PARALLEL_EDA",
    "ENV_MAX_PARALLEL_PROVIDER",
    "QUEUE_REASON_ACTIVE_LIMIT",
    "QUEUE_REASON_UNCONFIRMED",
    "QUEUE_REASON_WRITABLE_ROOT",
    "QueueView",
    "SchedulerConfigError",
    "SchedulerLimits",
    "SchedulerSnapshot",
    "TaskScheduler",
]
