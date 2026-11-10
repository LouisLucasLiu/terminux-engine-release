# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AgentTaskQueue。

Async Agent 任務的 thread pool + pending queue。

設計：
- ThreadPoolExecutor(max_workers=max_concurrent) 跑真正的 AgentRunner
- collections.deque(maxlen=max_queue_depth) 當 pending；達上限自動丟「最舊」
- 每個 worker 完成後：先呼叫 on_result(task, result)，再從 pending 拉一個排
- AgentRunner.run() 拋例外 → 收斂為 AgentResult(error=...)，queue 不掛
- shutdown 後 submit 回 False（不再接新任務）；可選擇等 active 任務跑完
"""

from __future__ import annotations

import collections
import concurrent.futures
import logging
import threading
from typing import Callable

from terminux_engine.llm.agent_models import AgentResult, AgentTask
from terminux_engine.llm.agent_runner import AgentRunner

_log = logging.getLogger(__name__)


class AgentTaskQueue:
    """非同步 Agent task 派發 + 限流。"""

    def __init__(
        self,
        *,
        agent_runner: AgentRunner,
        max_concurrent: int = 2,
        max_queue_depth: int = 3,
        on_result: Callable[[AgentTask, AgentResult], None] | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_queue_depth < 0:
            raise ValueError("max_queue_depth must be >= 0")

        self._runner = agent_runner
        self._max_concurrent = max_concurrent
        self._max_queue_depth = max_queue_depth
        self._on_result = on_result

        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_concurrent,
            thread_name_prefix="agent-task",
        )
        self._pending: collections.deque[AgentTask] = collections.deque(
            maxlen=max_queue_depth if max_queue_depth > 0 else None
        )
        self._active: set[concurrent.futures.Future] = set()
        self._lock = threading.Lock()
        self._shutdown_flag = False

    # ── Public ──────────────────────────────────────────────────────────────

    def submit(self, task: AgentTask) -> bool:
        """提交任務。非阻塞。shutdown 後回 False。否則一律 True（含 queue 滿丟舊）。"""
        future_to_arm: concurrent.futures.Future | None = None
        queued = False
        with self._lock:
            if self._shutdown_flag:
                return False
            if len(self._active) < self._max_concurrent:
                future_to_arm = self._executor.submit(self._run_task, task)
                self._active.add(future_to_arm)
            else:
                # deque(maxlen=N) 自動 drop oldest from left when at cap
                self._pending.append(task)
                queued = True
        # CRITICAL：add_done_callback 必須在 lock 外呼叫。Mock runner 跑得快時，
        # 若此時 future 已 done，add_done_callback 會同步在當前 thread 觸發
        # _on_task_done。_on_task_done 內部又會取同一個 Lock → deadlock。
        if future_to_arm is not None:
            future_to_arm.add_done_callback(self._on_task_done)
            _log.info(
                "AgentTaskQueue submit: task_id=%s trigger=%s started",
                task.task_id, task.trigger_source,
            )
        elif queued:
            _log.info(
                "AgentTaskQueue submit: task_id=%s trigger=%s queued",
                task.task_id, task.trigger_source,
            )
        return True

    def signal_stop(self) -> None:
        """非阻塞發出停止信號：拒絕新任務、清空 pending，並取消未開始的 futures。"""
        with self._lock:
            if self._shutdown_flag:
                return
            self._shutdown_flag = True
            self._pending.clear()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            _log.warning("Executor shutdown raised: %s", e)

    def join(self, timeout: float | None = None) -> bool:
        """使用 concurrent.futures.wait 等待 active futures。"""
        with self._lock:
            actives = list(self._active)
        if actives and timeout is not None and timeout > 0:
            done, not_done = concurrent.futures.wait(actives, timeout=timeout)
            return len(not_done) == 0
        elif actives and timeout is None:
            concurrent.futures.wait(actives)
            return True
        return len(actives) == 0

    @property
    def is_alive(self) -> bool:
        """是否有 active task 仍在執行。"""
        with self._lock:
            return len(self._active) > 0

    def shutdown(self, wait: bool = True, timeout: float = 10.0) -> None:
        """關閉 queue。

        - 立刻拒絕後續 submit
        - 清空 pending（不會再排程）
        - wait=True：等 active futures 完成（最多 timeout 秒）
        """
        self.signal_stop()
        if wait:
            self.join(timeout=timeout)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown_flag

    # ── Internal ────────────────────────────────────────────────────────────

    def _run_task(self, task: AgentTask) -> tuple[AgentTask, AgentResult]:
        """worker thread 主體。AgentRunner.run() 拋例外時收斂為 error result。"""
        try:
            result = self._runner.run(
                query=task.query,
                system_context=task.system_context,
                trigger_source=task.trigger_source,
                mode="async",
            )
        except Exception as e:
            _log.warning(
                "AgentRunner raised in async path (task=%s): %s", task.task_id, e
            )
            result = AgentResult(
                answer=None,
                error=f"AgentRunner exception: {e}",
                trigger_source=task.trigger_source,
            )
        return task, result

    def _on_task_done(self, future: concurrent.futures.Future) -> None:
        """worker 完成：discard active、決定要不要拉 pending、呼叫 on_result。"""
        next_task: AgentTask | None = None
        with self._lock:
            self._active.discard(future)
            if not self._shutdown_flag and self._pending:
                next_task = self._pending.popleft()

        # 取結果（_run_task 已收斂例外 → 這裡 future.result() 不該 raise）
        try:
            task, result = future.result()
        except Exception as e:
            _log.warning("Future returned with unexpected exception: %s", e)
            task = None
            result = None

        if task is not None and result is not None:
            _log.info(
                "AgentTaskQueue done: task_id=%s rounds=%d evidence=%d "
                "no_action=%s answer_len=%d error=%s",
                task.task_id,
                getattr(result, "rounds", 0),
                len(getattr(result, "evidence", []) or []),
                getattr(result, "no_action", False),
                len(getattr(result, "answer", None) or ""),
                getattr(result, "error", None),
            )
            if self._on_result is not None:
                try:
                    self._on_result(task, result)
                except Exception as e:
                    _log.warning("on_result callback raised: %s", e)

        # 拉下一個 pending 進來跑。走 submit() path → add_done_callback 在 lock
        # 外觸發，避免同步 callback 引發 reentrant lock deadlock。
        if next_task is not None:
            self.submit(next_task)
