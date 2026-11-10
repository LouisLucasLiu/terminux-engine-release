# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""舊執行模式的新執行停用政策（ORFS run、flow task、formal pair、ECO 共用）。

不再接受新執行的舊模式：

- container（Docker）：`terminux-orfs-run` 的舊 adapter、flow task（Profile
  `runner=docker` 或未宣告 runner）、formal pair（`docker_executable` 工具邊界）、
  single-instance ECO（`host_binding.v1`）；狀態碼 `LEGACY_DOCKER_EXECUTION_DISABLED`。
- 舊 host runner（`runner=host`，沒有 `native_tool_binding`）：工具身分只是 Profile
  上的 image 標籤，沒有站點工具的 realpath＋SHA-256；狀態碼
  `LEGACY_HOST_RUNNER_EXECUTION_DISABLED`。

拒絕發生在任何 docker 呼叫、ledger run、child、輸出目錄或 run receipt 之前。新的
執行一律走 host-native（Profile `execution.runner=host_native` ＋
`native_tool_binding`）；沒有自動轉換，也沒有 Docker fallback。

不受影響的部分：

- 歷史 Profile、task、receipt、artifact manifest、proof descriptor、method
  extension、durable digest、EventStore 與 reader 的讀取與比對；
- 舊 container run 的停止與清理：只能經 `run_legacy_stop_only()`，它只接受固定形狀
  的 `inspect`／`stop`／`kill`／`rm -f`，結構上不可能建立或啟動 container；
- fixture corpus replay（`QUALIFICATION_ONLY`）：執行檔必須是套件內的 replay CLI，
  它模擬 docker CLI 邊界、從不啟動 container。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.execution_contract import ExecutionContractError

LEGACY_DOCKER_EXECUTION_DISABLED = "LEGACY_DOCKER_EXECUTION_DISABLED"
LEGACY_HOST_RUNNER_EXECUTION_DISABLED = "LEGACY_HOST_RUNNER_EXECUTION_DISABLED"

#: host-native flow task 的 runner 身分（與 `native_tool_binding` 的版本化 schema 一起）。
NATIVE_RUNNER = "host_native"

PATH_ORFS_RUN = "orfs_run"
PATH_FLOW_TASK = "flow_task"
PATH_FORMAL_PAIR = "formal_pair"
PATH_SINGLE_INSTANCE_ECO = "single_instance_eco"
PATH_LEGACY_STOP_ONLY = "legacy_container_stop_only"

_GUIDANCE = {
    PATH_ORFS_RUN: (
        "use terminux-orfs-run, which runs the CAD/IT-installed ORFS natively "
        "(adapter orfs.native)"),
    PATH_FLOW_TASK: (
        "authorize a new host-native Profile (execution.runner=host_native) that pins "
        "its site tools in execution.native_tool_binding"),
    PATH_FORMAL_PAIR: (
        "use a host-native Profile whose execution.native_tool_binding pins "
        "FORMAL_YOSYS (native formal pair contract)"),
    PATH_SINGLE_INSTANCE_ECO: (
        "configure the host ECO binding with schema "
        "gcd.single_instance_buffer_swap.host_binding.native.v1"),
    PATH_LEGACY_STOP_ONLY: (
        "legacy container maintenance may only inspect, stop, kill or remove an "
        "existing container"),
}

#: fixture corpus replay CLI 的模組路徑；installed console script 與 source tree
#: wrapper 都以它為入口。
_CORPUS_REPLAY_MODULE = b"terminux_engine.packages.orfs_run_intelligence.qualification.corpus_replay_cli"
_CORPUS_REPLAY_MAX_BYTES = 1 << 20
_QUALIFICATION_ONLY = "QUALIFICATION_ONLY"


def disabled_message(path: str, detail: str = "") -> str:
    """人看得懂、且以固定狀態碼開頭的拒絕訊息（container 模式）。"""
    guidance = _GUIDANCE.get(path, "authorize a host-native execution identity")
    parts = [f"{LEGACY_DOCKER_EXECUTION_DISABLED}: new {path} executions on the "
             "legacy Docker container backend are disabled"]
    if detail:
        parts.append(detail)
    parts.append(f"to run it, {guidance}")
    parts.append("historical runs and evidence remain readable")
    return "; ".join(parts)


def host_runner_disabled_message(detail: str = "") -> str:
    """舊 host runner 的拒絕訊息。"""
    parts = [f"{LEGACY_HOST_RUNNER_EXECUTION_DISABLED}: new flow task executions on the "
             "legacy host runner are disabled; it binds no site tool identity (only a "
             "Docker image label)"]
    if detail:
        parts.append(detail)
    parts.append(f"to run it, {_GUIDANCE[PATH_FLOW_TASK]}")
    parts.append("historical runs and evidence remain readable")
    return "; ".join(parts)


class LegacyExecutionDisabled(ExecutionContractError):
    """要求以舊執行模式開始新執行；在任何副作用前拒絕。`code` 為固定狀態碼。"""

    code = ""

    def __init__(self, path: str, message: str) -> None:
        self.execution_path = path
        self.message = message
        super().__init__(message)

    def to_json_dict(self) -> dict[str, Any]:
        return {"status": "rejected", "error_code": self.code,
                "execution_path": self.execution_path, "message": self.message}


class LegacyDockerExecutionDisabled(LegacyExecutionDisabled):
    """要求以舊 container 模式開始新執行。"""

    code = LEGACY_DOCKER_EXECUTION_DISABLED

    def __init__(self, path: str, detail: str = "") -> None:
        super().__init__(path, disabled_message(path, detail))


class LegacyHostRunnerExecutionDisabled(LegacyExecutionDisabled):
    """要求以舊 host runner（沒有 native tool binding）開始新執行。"""

    code = LEGACY_HOST_RUNNER_EXECUTION_DISABLED

    def __init__(self, detail: str = "") -> None:
        super().__init__(PATH_FLOW_TASK, host_runner_disabled_message(detail))


def _field(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def is_corpus_replay_executable(executable: Any) -> bool:
    """`executable` 是否為套件內的 fixture corpus replay CLI。

    以內容判定：解析後的檔案必須是小型 script（`#!`），且以 replay CLI
    模組為入口。名稱、路徑或 Profile 宣告都不足以證明它不是 container client。
    """
    if not isinstance(executable, str) or not executable.strip():
        return False
    candidate = (executable if os.path.isabs(executable)
                 else shutil.which(executable))
    if not candidate:
        return False
    try:
        path = Path(candidate).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > _CORPUS_REPLAY_MAX_BYTES:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    return data.startswith(b"#!") and _CORPUS_REPLAY_MODULE in data


def is_fixture_corpus_replay(execution: Any) -> bool:
    """`QUALIFICATION_ONLY` fixture corpus，且執行檔確實是 replay CLI。"""
    if execution is None:
        return False
    corpus = _field(execution, "fixture_corpus")
    if corpus is None or _field(corpus, "purpose") != _QUALIFICATION_ONLY:
        return False
    return is_corpus_replay_executable(_field(execution, "docker_executable") or "docker")


def flow_task_execution_denial(execution: Any, *,
                               detail: str = "") -> LegacyExecutionDisabled | None:
    """flow task 的執行宣告若會以舊模式啟動新 run，回傳對應的拒絕；否則 `None`。

    `execution` 可以是 Profile 的執行宣告物件或其 JSON dict；`None` 代表舊
    Profile 的預設（docker runner）。

    - 有 `native_tool_binding` ⇒ host-native，不拒絕（工具 pin 由 adapter 核對）；
    - `runner=host`，或宣告 `host_native` 卻沒有 binding ⇒ 舊 host runner；
    - 其餘（`docker`、未宣告）⇒ container，除非是確實由 replay CLI 執行的
      fixture corpus replay。
    """
    if execution is not None and _field(execution, "native_tool_binding") is not None:
        return None
    runner = (_field(execution, "runner") if execution is not None else None) or "docker"
    if runner in ("host", NATIVE_RUNNER):
        return LegacyHostRunnerExecutionDisabled(detail)
    if is_fixture_corpus_replay(execution):
        return None
    return LegacyDockerExecutionDisabled(PATH_FLOW_TASK, detail)


def execution_selects_legacy_docker(execution: Any) -> bool:
    """flow task 的執行宣告是否會以舊 container 模式啟動新 run。"""
    return isinstance(flow_task_execution_denial(execution), LegacyDockerExecutionDisabled)


def profile_execution_denial(profile: Any) -> LegacyExecutionDisabled | None:
    """Profile 的新 flow task run 會被哪一種舊模式拒絕；可執行時回 `None`。"""
    return flow_task_execution_denial(getattr(profile, "execution", None))


def profile_selects_legacy_docker(profile: Any) -> bool:
    """Profile 是否會以舊 container 模式啟動新的 flow task run。"""
    return execution_selects_legacy_docker(getattr(profile, "execution", None))


def reject_flow_task_execution(execution: Any, *, detail: str = "") -> None:
    """flow task 新執行的共同檢查；舊 container 模式或舊 host runner 即拋出。"""
    denial = flow_task_execution_denial(execution, detail=detail)
    if denial is not None:
        raise denial


# ── 舊 container 的 stop-only 維護 ─────────────────────────────────────

#: container 名稱或 id。不得以 `-` 開頭（不能被當成 option）。
_CONTAINER_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_INSPECT_FORMATS = frozenset({"{{json .Config.Labels}}", "{{.State.Running}}"})


def _stop_only_shape(argv: list[str]) -> bool:
    if not argv or any(not isinstance(token, str) for token in argv):
        return False
    target = argv[-1]
    if not _CONTAINER_REF_RE.match(target):
        return False
    if argv[0] == "container":
        argv = argv[1:]
    match argv:
        case ["inspect", "--format", fmt, _]:
            return fmt in _INSPECT_FORMATS
        case ["stop", "--time", seconds, _]:
            return seconds.isdigit()
        case ["kill", _]:
            return True
        case ["rm", "-f", _]:
            return True
    return False


def legacy_stop_only_argv(docker_executable: str, argv: list[str]) -> list[str]:
    """核對 stop-only 維護指令的形狀；只允許查詢、停止、kill 與強制移除既有 container。

    `run`／`create`／`start`／`restart`／`exec`／`pull`／`build`、額外 option 或以 `-`
    開頭的目標一律拒絕——這條路徑不可能被拿來啟動 workload。
    """
    if not _stop_only_shape(list(argv)):
        raise LegacyDockerExecutionDisabled(
            PATH_LEGACY_STOP_ONLY, f"refused legacy container command shape {list(argv)!r}")
    return [docker_executable, *argv]


def run_legacy_stop_only(docker_executable: str, argv: list[str], *,
                         timeout: float) -> subprocess.CompletedProcess[str]:
    """執行一個已核對形狀的 stop-only 維護指令；逾時與 OSError 由呼叫端處理。"""
    return subprocess.run(legacy_stop_only_argv(docker_executable, argv),
                          capture_output=True, text=True, timeout=timeout, check=False)


__all__ = [
    "LEGACY_DOCKER_EXECUTION_DISABLED",
    "LEGACY_HOST_RUNNER_EXECUTION_DISABLED",
    "LegacyDockerExecutionDisabled",
    "LegacyExecutionDisabled",
    "LegacyHostRunnerExecutionDisabled",
    "NATIVE_RUNNER",
    "PATH_FLOW_TASK",
    "PATH_FORMAL_PAIR",
    "PATH_LEGACY_STOP_ONLY",
    "PATH_ORFS_RUN",
    "PATH_SINGLE_INSTANCE_ECO",
    "disabled_message",
    "execution_selects_legacy_docker",
    "flow_task_execution_denial",
    "host_runner_disabled_message",
    "is_corpus_replay_executable",
    "is_fixture_corpus_replay",
    "legacy_stop_only_argv",
    "profile_execution_denial",
    "profile_selects_legacy_docker",
    "reject_flow_task_execution",
    "run_legacy_stop_only",
]
