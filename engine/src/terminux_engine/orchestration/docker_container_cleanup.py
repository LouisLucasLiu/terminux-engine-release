# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""以名稱與 label 確認單一 container 已不存在的清理 helper。

- container 身分在 `docker run` 之前就由 run 身分推導（名稱加 label），不依賴
  docker client 事後才寫出的 cidfile；
- 只清理名稱**與**全部 label 都相符的 container；名稱相同但 label 不符時不 stop、
  不移除，回報 `identity_mismatch`；
- 反覆「查詢 → 必要時移除」，直到**連續**兩次由 dockerd 明確回報不存在才算完成；
  查詢失敗（daemon 無回應、權限錯誤、逾時）是 `unknown`，不當成不存在；
- 每一次 Docker 呼叫與每一輪 retry 都受同一個 monotonic 期限約束；期限內無法確認
  就回報 `ok=False`，由呼叫端決定如何處理，不無限等待；
- 只做 stop-only 維護：`SubprocessContainerOps` 的每一個指令都經
  `legacy_docker_execution.run_legacy_stop_only()` 核對形狀（`container inspect`、
  `stop`、`rm -f`），結構上不可能建立、啟動或重新啟動 container。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"

REASON_CONFIRMED_ABSENT = "confirmed_absent"
REASON_IDENTITY_MISMATCH = "identity_mismatch"
REASON_DEADLINE_EXCEEDED = "deadline_exceeded"
REASON_DOCKER_QUERY_FAILED = "docker_query_failed"

#: 單一次 Docker 呼叫的上限；實際 timeout 取這個值與剩餘時間的較小者。
DOCKER_CALL_TIMEOUT_SECONDS = 10.0
DEFAULT_CLEANUP_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
REQUIRED_ABSENT_OBSERVATIONS = 2

LABEL_RUN = "terminux.orfs.run"
LABEL_STEP = "terminux.orfs.step"
LABEL_OWNER = "terminux.owner"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_NO_SUCH_CONTAINER = ("no such container", "no such object")


@dataclass(frozen=True)
class ContainerIdentity:
    """一個 container 的名稱與必須全部相符的 label。"""

    name: str
    labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid container name: {self.name!r}")
        if not self.labels:
            raise ValueError("a container identity needs at least one label")

    @classmethod
    def for_run(cls, *, prefix: str, run_id: str, step_id: str,
                owner: str) -> "ContainerIdentity":
        """由 run 身分推導名稱與 run／step／owner 三個 label。"""
        if not run_id or not _NAME_RE.fullmatch(run_id):
            raise ValueError(f"invalid run id for a container name: {run_id!r}")
        return cls(name=f"{prefix}-{run_id}",
                   labels=((LABEL_RUN, run_id), (LABEL_STEP, step_id),
                           (LABEL_OWNER, owner)))

    def label_dict(self) -> dict[str, str]:
        return dict(self.labels)

    def docker_run_args(self) -> list[str]:
        args = ["--name", self.name]
        for key, value in self.labels:
            args.extend(["--label", f"{key}={value}"])
        return args

    def matches(self, labels: dict[str, str] | None) -> bool:
        actual = labels or {}
        return all(actual.get(key) == value for key, value in self.labels)


class ContainerOps(Protocol):
    """清理所需的三個 Docker 操作；每個呼叫都必須遵守傳入的 timeout。"""

    def inspect_labels(self, name: str, *, timeout: float) -> tuple[str, dict[str, str] | None]:
        ...

    def stop(self, name: str, *, grace_seconds: float, timeout: float) -> bool:
        ...

    def remove_force(self, name: str, *, timeout: float) -> bool:
        ...


class CleanupBudget(Protocol):
    """清理期限與模式；CLI 的中斷控制以第一次 signal 起算的總期限實作它。"""

    def cleanup_deadline(self, started_at: float) -> float:
        ...

    def forced(self) -> bool:
        ...

    def stop_grace_seconds(self) -> float:
        ...

    def poll_interval(self) -> float:
        ...


class FixedCleanupBudget:
    """沒有中斷時的預設期限：從清理開始起算固定秒數，不做 graceful stop。"""

    def __init__(self, seconds: float = DEFAULT_CLEANUP_SECONDS,
                 poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        self._seconds = seconds
        self._poll_interval = poll_interval

    def cleanup_deadline(self, started_at: float) -> float:
        return started_at + self._seconds

    def forced(self) -> bool:
        return False

    def stop_grace_seconds(self) -> float:
        return 0.0

    def poll_interval(self) -> float:
        return self._poll_interval


class SubprocessContainerOps:
    """以指定的 docker 執行檔實作 `ContainerOps`（只查詢、停止與移除既有 container）。"""

    def __init__(self, docker_executable: str = "docker") -> None:
        self._docker = docker_executable

    def _run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        from terminux_engine.orchestration.legacy_docker_execution import (
            LegacyExecutionDisabled,
            run_legacy_stop_only,
        )

        try:
            return run_legacy_stop_only(self._docker, argv, timeout=max(timeout, 0.001))
        except LegacyExecutionDisabled:
            # 形狀不符（例如名稱不合法）時不呼叫 docker；結果是 unknown。
            return None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def inspect_labels(self, name: str, *, timeout: float) -> tuple[str, dict[str, str] | None]:
        completed = self._run(
            ["container", "inspect", "--format", "{{json .Config.Labels}}", name], timeout)
        if completed is None:
            return UNKNOWN, None
        if completed.returncode != 0:
            # 只有 dockerd 明確回報「沒有這個 container」才算 absent。
            message = (completed.stderr or "").lower()
            if any(marker in message for marker in _NO_SUCH_CONTAINER):
                return ABSENT, None
            return UNKNOWN, None
        try:
            parsed = json.loads(completed.stdout.strip() or "null")
        except json.JSONDecodeError:
            return UNKNOWN, None
        if parsed is None:
            return PRESENT, {}
        if not isinstance(parsed, dict):
            return UNKNOWN, None
        return PRESENT, {str(k): str(v) for k, v in parsed.items()}

    def stop(self, name: str, *, grace_seconds: float, timeout: float) -> bool:
        completed = self._run(["stop", "--time", str(int(grace_seconds)), name], timeout)
        return completed is not None and completed.returncode == 0

    def remove_force(self, name: str, *, timeout: float) -> bool:
        completed = self._run(["rm", "-f", name], timeout)
        return completed is not None and completed.returncode == 0


def release_container(
    ops: ContainerOps, identity: ContainerIdentity, *, budget: CleanupBudget,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    required_absent_observations: int = REQUIRED_ABSENT_OBSERVATIONS,
) -> dict[str, Any]:
    """清理 `identity` 指定的 container，並確認它已不存在。

    回傳 `attempted`、`ok`、`reason`、`container_name`、`removals`、
    `observations`、`detail`。`ok` 只有在連續 `required_absent_observations` 次
    明確 absent 時才為 True。
    """
    started_at = clock()
    observations: list[dict[str, Any]] = []
    removals = 0
    consecutive_absent = 0
    stop_attempted = False
    #: 是否曾由 dockerd 得到明確答案（present 或 absent）。
    definitive = False

    def remaining() -> float:
        return budget.cleanup_deadline(started_at) - clock()

    def result(ok: bool, reason: str, detail: str) -> dict[str, Any]:
        return {"attempted": removals > 0 or stop_attempted, "ok": ok, "reason": reason,
                "container_name": identity.name, "removals": removals,
                "observations": observations, "detail": detail}

    while True:
        left = remaining()
        if left <= 0:
            if observations and not definitive:
                return result(False, REASON_DOCKER_QUERY_FAILED,
                              "docker could not report the container state before the "
                              "cleanup deadline; manual check required")
            return result(False, REASON_DEADLINE_EXCEEDED,
                          "container absence could not be confirmed before the cleanup "
                          "deadline; manual check required")
        state, labels = ops.inspect_labels(
            identity.name, timeout=min(DOCKER_CALL_TIMEOUT_SECONDS, left))
        definitive = definitive or state in (PRESENT, ABSENT)
        entry: dict[str, Any] = {"state": state}
        if state == ABSENT:
            consecutive_absent += 1
        else:
            consecutive_absent = 0
        if state == PRESENT:
            if not identity.matches(labels):
                entry["identity"] = "mismatch"
                observations.append(entry)
                return result(False, REASON_IDENTITY_MISMATCH,
                              "a container with this name does not carry this run's "
                              "labels; it was not stopped or removed")
            grace = budget.stop_grace_seconds()
            left = remaining()
            if not stop_attempted and not budget.forced() and grace > 0 and left > 0:
                stop_attempted = True
                entry["stopped"] = ops.stop(
                    identity.name, grace_seconds=min(grace, left),
                    timeout=min(grace + DOCKER_CALL_TIMEOUT_SECONDS, left))
                left = remaining()
            if left > 0:
                entry["removed"] = ops.remove_force(
                    identity.name, timeout=min(DOCKER_CALL_TIMEOUT_SECONDS, left))
                removals += 1
        observations.append(entry)
        if consecutive_absent >= required_absent_observations:
            return result(True, REASON_CONFIRMED_ABSENT,
                          "container removed and confirmed absent" if removals
                          else "no container with this identity exists")
        pause = min(budget.poll_interval(), max(remaining(), 0.0))
        if pause > 0:
            sleep(pause)


__all__ = [
    "ABSENT", "DEFAULT_CLEANUP_SECONDS", "DOCKER_CALL_TIMEOUT_SECONDS", "LABEL_OWNER",
    "LABEL_RUN", "LABEL_STEP", "PRESENT", "REASON_CONFIRMED_ABSENT",
    "REASON_DEADLINE_EXCEEDED", "REASON_DOCKER_QUERY_FAILED", "REASON_IDENTITY_MISMATCH",
    "UNKNOWN", "CleanupBudget", "ContainerIdentity", "ContainerOps", "FixedCleanupBudget",
    "SubprocessContainerOps", "release_container",
]
