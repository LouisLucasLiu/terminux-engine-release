# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# 出處：本模組呼叫第三方開源工具 OpenROAD-flow-scripts（ORFS）與 OpenROAD
# （皆為 BSD 3-Clause，Terminux 未隨附），詳見 THIRD_PARTY_NOTICES.md 的 External EDA Tools 一節。
"""受限的 `orfs_single_run` workflow。

它**只**做一件事：用固定的 profile／input／output binding 跑一次完整的
ORFS flow。刻意不開放 generic `tool_id`、任意 config 或 Docker options——
config 的每一個欄位都由 host 從 live task binding 產生，caller 不能提供。

與既有 workflow 的關係：

- 重用既有 `SingleToolRunner` 的 DomainRunner 語義（一個 ToolSpec、依
  exit code 判終態），因此 `kernel._KNOWN_FAILURE_TERMINAL_REASONS` 已經
  認得 `single_tool_execution_failed`，不必新增終態語義。
- 重用既有 `orfs_dqe` package 與同一個 `DispatchEngine` 組法。
- 使用 `OrfsFlowTaskAdapter`（唯讀 inputs／獨立可寫 outputs／`--network=none`），
  不是可寫回 source 的既有 adapter。

`terminux-orfs-run` 的拋棄式 ledger **不在**這條路徑上：本 workflow 由
daemon 的正式 `OrchestrationService` 與同一個 durable ledger 驅動。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.domain_runner import DomainRunner
from terminux_engine.orchestration.single_tool_runner import SingleToolRunner
from terminux_engine.package_runtime.dispatcher import PackageDispatcher
from terminux_engine.package_runtime.loader import PackageLoader
from terminux_engine.package_runtime.registry import PackageRegistry
from terminux_engine.package_runtime.services import RuntimeServices
from terminux_engine.orchestration.dispatch_engine import DispatchEngine, EngineMode

WORKFLOW_TYPE = "orfs_single_run"

ORFS_DQE_PACKAGE_ID = "orfs_dqe"
ORFS_TOOL_ID = "orfs"

#: config 的固定欄位。多一個少一個都拒絕。工具身分另外恰好擇一：舊 Profile
#: 帶 `docker_image_id`，host-native Profile 帶 `native_tool_binding_sha256`。
_REQUIRED_KEYS = frozenset({
    "snapshot_flow_dir", "output_root", "design", "platform",
    "variant", "design_config_relpath", "step_id", "timeout_seconds",
})
_TOOL_IDENTITY_KEYS = frozenset({"docker_image_id", "native_tool_binding_sha256"})

#: 只有 candidate 的 EDA step 會帶這個唯讀 reference 目錄。
_OPTIONAL_KEYS = frozenset({"reference_dir", "dqe_db_path", "artifact_root"})

#: 這些 key 一旦出現就是有人想繞過固定 binding。
_FORBIDDEN_KEYS = frozenset({
    "tool_id", "parameters", "cwd", "command", "argv", "docker_options",
    "env", "environment", "network", "mounts", "volumes",
})


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def is_native_config(config: dict[str, Any]) -> bool:
    return "native_tool_binding_sha256" in config


class OrfsSingleRunConfigError(ValueError):
    """config 不符固定形狀。fail closed，不猜。"""


def validate_orfs_single_run_workflow_config(config: dict[str, Any]) -> None:
    """驗證 workflow config。

    這條 workflow 的 config **不是**使用者輸入：它由 host 依 profile 與
    live task binding 組出來。驗證仍然做滿——公開 `orchestration.start`
    若在缺 binding 的情況下送同名 workflow 進來，必須在這裡被擋下。
    """
    if not isinstance(config, dict):
        raise OrfsSingleRunConfigError("workflow config must be an object")
    present = set(config)
    forbidden = sorted(present & _FORBIDDEN_KEYS)
    if forbidden:
        raise OrfsSingleRunConfigError(
            f"config must not carry execution details: {forbidden}")
    unknown = sorted(present - _REQUIRED_KEYS - _OPTIONAL_KEYS - _TOOL_IDENTITY_KEYS)
    if unknown:
        raise OrfsSingleRunConfigError(f"unknown config key(s): {unknown}")
    missing = sorted(_REQUIRED_KEYS - present)
    if missing:
        raise OrfsSingleRunConfigError(f"missing config key(s): {missing}")
    identity = sorted(present & _TOOL_IDENTITY_KEYS)
    if len(identity) != 1:
        raise OrfsSingleRunConfigError(
            "config must carry exactly one tool identity "
            f"({sorted(_TOOL_IDENTITY_KEYS)}), got {identity}")

    for key in ("snapshot_flow_dir", "output_root"):
        value = config[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise OrfsSingleRunConfigError(f"{key} must be an absolute path")
    for key in (identity[0], "design", "platform", "variant",
                "design_config_relpath", "step_id"):
        value = config[key]
        if not isinstance(value, str) or not value.strip():
            raise OrfsSingleRunConfigError(f"{key} must be a non-empty string")
    if "docker_image_id" in config and not str(config["docker_image_id"]).startswith("sha256:"):
        raise OrfsSingleRunConfigError(
            "docker_image_id must be an immutable sha256 identity, not a tag")
    if ("native_tool_binding_sha256" in config
            and not _SHA256_RE.match(str(config["native_tool_binding_sha256"]))):
        raise OrfsSingleRunConfigError(
            "native_tool_binding_sha256 must be 64 lowercase hex characters")
    for key in ("dqe_db_path", "artifact_root"):
        if key in config and (
                not isinstance(config[key], str) or not Path(config[key]).is_absolute()):
            raise OrfsSingleRunConfigError(f"{key} must be an absolute path when present")
    relpath = Path(config["design_config_relpath"])
    if relpath.is_absolute() or ".." in relpath.parts:
        raise OrfsSingleRunConfigError(
            "design_config_relpath must be relative and inside the flow tree")
    timeout = config["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise OrfsSingleRunConfigError("timeout_seconds must be a positive number")


def _adapter_config(config: dict[str, Any], *, execution: dict[str, Any] | None = None,
                    task_identity: dict[str, Any] | None = None) -> Any:
    """`execution`（user 的 Profile 宣告）與 task identity **不**來自
    config——config 是公開 `orchestration.start` 也能送的形狀；它們來自 host
    在 `start_task_run()` 寫進 ledger 的 grant 綁定 metadata。"""
    from terminux_engine.orchestration.orfs_flow_task_adapter import (
        OrfsFlowTaskAdapterConfig,
    )

    identity = task_identity or {}
    if is_native_config(config):
        from terminux_engine.orchestration.orfs_flow_task_adapter import (
            OrfsNativeFlowTaskAdapterConfig,
        )

        return OrfsNativeFlowTaskAdapterConfig.from_execution(
            snapshot_flow_dir=Path(config["snapshot_flow_dir"]),
            output_root=Path(config["output_root"]),
            native_tool_binding_sha256=config["native_tool_binding_sha256"],
            design=config["design"], platform=config["platform"],
            variant=config["variant"],
            design_config_relpath=config["design_config_relpath"],
            reference_dir=(Path(config["reference_dir"])
                           if config.get("reference_dir") else None),
            execution=dict(execution) if execution else None,
            task_id=identity.get("task_id"), slot=identity.get("slot"),
            run_id=identity.get("run_id"))
    return OrfsFlowTaskAdapterConfig(
        snapshot_flow_dir=Path(config["snapshot_flow_dir"]),
        output_root=Path(config["output_root"]),
        docker_image_id=config["docker_image_id"],
        design=config["design"],
        platform=config["platform"],
        variant=config["variant"],
        design_config_relpath=config["design_config_relpath"],
        reference_dir=(Path(config["reference_dir"])
                       if config.get("reference_dir") else None),
        execution=dict(execution) if execution else None,
        task_id=identity.get("task_id"), slot=identity.get("slot"),
        run_id=identity.get("run_id"),
    )


def build_orfs_single_run_tool_registry(
    config: dict[str, Any], *, execution: dict[str, Any] | None = None,
    task_identity: dict[str, Any] | None = None,
) -> Any:
    """child 端的 ToolRegistry：只有這一個 ORFS tool，沒有別的。

    刻意不重用 `builtin_tools.get_default_registry()`——那份沒有 ORFS tool，
    而且帶了本 workflow 不需要的 fake tools。
    """
    from terminux_engine.orchestration.orfs_flow_task_adapter import (
        OrfsFlowTaskAdapter,
        OrfsNativeFlowTaskAdapter,
    )
    from terminux_engine.orchestration.tool_registry import ToolDefinition, ToolRegistry

    validate_orfs_single_run_workflow_config(config)
    if not is_native_config(config):
        from terminux_engine.orchestration.legacy_docker_execution import (
            reject_flow_task_execution,
        )

        reject_flow_task_execution(execution)
    adapter_config = _adapter_config(config, execution=execution,
                                     task_identity=task_identity)
    if is_native_config(config):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            tool_id=ORFS_TOOL_ID,
            executables=[str(adapter_config.env_executable)],
            event_tool_name="orfs",
            expected_packages=[ORFS_DQE_PACKAGE_ID],
            default_timeout=float(config["timeout_seconds"]),
            command_builder=lambda _p: [],
            adapter=OrfsNativeFlowTaskAdapter(adapter_config),
        ))
        return registry
    # 只剩 fixture corpus replay：replay CLI 模擬 docker CLI 邊界。
    executable = adapter_config.docker_executable
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        tool_id=ORFS_TOOL_ID,
        executables=[executable],
        event_tool_name="orfs",
        expected_packages=[ORFS_DQE_PACKAGE_ID],
        default_timeout=float(config["timeout_seconds"]),
        command_builder=lambda _p: [],
        adapter=OrfsFlowTaskAdapter(adapter_config),
    ))
    return registry


def _load_orfs_packages() -> list[Any]:
    from terminux_engine import packages as builtin_packages

    root = Path(builtin_packages.__file__).resolve().parent
    return PackageLoader().load_many([root / ORFS_DQE_PACKAGE_ID])


def build_orfs_single_run_engine() -> DispatchEngine:
    """與 `orfs_cli` 相同的 dispatch 組法：`orfs_dqe` package，STANDALONE 模式。"""
    return DispatchEngine(
        PackageDispatcher(PackageRegistry(_load_orfs_packages())),
        mode=EngineMode.STANDALONE,
        services=RuntimeServices(),
    )


class OrfsSingleRunRunner:
    """只做一件事：把本 workflow 的 config 轉成 `SingleToolRunner` 的形狀。

    Kernel 會把 **workflow config** 原樣交給 runner 的 `start_run()`，而
    `SingleToolRunner` 期待的是 `{tool_id, parameters, cwd, step_id}`。
    這一層做那個轉換，其餘全部委派——執行語義完全沿用既有 runner，
    包含它的終態 reason（`single_tool_execution_completed/failed`），因此
    不需要在 Kernel 新增任何終態語義。

    轉換結果**不接受 caller 提供的版本**：`tool_id` 固定、`parameters` 恆為
    空、`cwd` 一定是本 slot 的可寫輸出區。
    """

    workflow_type = WORKFLOW_TYPE

    def __init__(self) -> None:
        self._inner = SingleToolRunner()

    def start_run(self, config: dict[str, Any]) -> Any:
        return self._inner.start_run(single_tool_config_for(config))

    def plan_next_steps(self, run_state: Any) -> Any:
        return self._inner.plan_next_steps(run_state)

    def record_step_outcome(self, run_state: Any, tool_spec: Any,
                            dispatch_outcome: Any, exit_code: int | None) -> Any:
        return self._inner.record_step_outcome(
            run_state, tool_spec, dispatch_outcome, exit_code)

    def event_metadata(self, run_state: Any) -> dict[str, Any]:
        return self._inner.event_metadata(run_state)


def build_orfs_single_run_runner(config: dict[str, Any]) -> DomainRunner:
    """回傳跑這一步的 DomainRunner。"""
    validate_orfs_single_run_workflow_config(config)
    return OrfsSingleRunRunner()


def single_tool_config_for(config: dict[str, Any]) -> dict[str, Any]:
    """把本 workflow 的 config 轉成 `SingleToolRunner` 要的形狀。

    `cwd` 是本 slot 的可寫輸出區——**不是** input snapshot，因此 after phase
    不可能寫進唯讀的 inputs。
    """
    validate_orfs_single_run_workflow_config(config)
    return {
        "tool_id": ORFS_TOOL_ID,
        "parameters": {},
        "cwd": config["output_root"],
        "step_id": config["step_id"],
        "event_metadata": {
            "design": config["design"],
            "platform": config["platform"],
            "variant": config["variant"],
            **({"native_tool_binding_sha256": config["native_tool_binding_sha256"]}
               if is_native_config(config)
               else {"docker_image_id": config["docker_image_id"]}),
            # DQE package 只讀 TimelineEvent metadata 的正式解析規則；值由
            # task-owned host binding 建立，不是 RPC caller 可傳入的環境覆寫。
            **({"dqe_db_path": config["dqe_db_path"]}
               if config.get("dqe_db_path") else {}),
        },
    }


__all__ = [
    "ORFS_DQE_PACKAGE_ID",
    "ORFS_TOOL_ID",
    "WORKFLOW_TYPE",
    "OrfsSingleRunConfigError",
    "OrfsSingleRunRunner",
    "build_orfs_single_run_engine",
    "build_orfs_single_run_runner",
    "build_orfs_single_run_tool_registry",
    "is_native_config",
    "single_tool_config_for",
    "validate_orfs_single_run_workflow_config",
]
