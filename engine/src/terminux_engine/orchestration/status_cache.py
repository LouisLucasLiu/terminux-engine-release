# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent-only per-run RunView cache。"""

from __future__ import annotations

import copy
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from terminux_engine.orchestration.daemon_service import RunView


STATUS_CACHE_TTL_SECONDS = 1.0
STATUS_CACHE_CAPACITY = 1024


class _CacheEntry:
    """單一 run 的 cached RunView 副本與 insert 時刻。"""

    __slots__ = ("value", "cached_at")

    def __init__(self, value: RunView, cached_at: float) -> None:
        self.value = value
        self.cached_at = cached_at


class AgentStatusCache:
    """僅供 `caller_kind="agent"` 使用的 per-run RunView cache；owner 為 daemon 內
    單一 `OrchestrationService` instance，daemon restart 自然清空。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _CacheEntry] = {}

    def get_or_load(
        self,
        run_id: str,
        loader: Callable[[], RunView],
    ) -> RunView:
        """lookup、首次 loader 與 insert 在同一 lock 內完成，保證同 run
        concurrent first call 只做一次 durable projection。"""

        with self._lock:
            now = self._clock()
            entry = self._entries.get(run_id)
            if entry is not None and (now - entry.cached_at) < STATUS_CACHE_TTL_SECONDS:
                return copy.deepcopy(entry.value)

            value = loader()

            now = self._clock()
            self._evict_expired(now)
            if run_id not in self._entries and len(self._entries) >= STATUS_CACHE_CAPACITY:
                self._evict_oldest()
            self._entries[run_id] = _CacheEntry(copy.deepcopy(value), now)
            return copy.deepcopy(value)

    def _evict_expired(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if (now - entry.cached_at) >= STATUS_CACHE_TTL_SECONDS
        ]
        for key in expired:
            del self._entries[key]

    def _evict_oldest(self) -> None:
        # dict 保留 insertion order；tie 時 min() 回傳先插入者，
        # 符合「tie 以穩定 insert 順序決定」。
        oldest_key = min(self._entries, key=lambda key: self._entries[key].cached_at)
        del self._entries[oldest_key]
