# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Minimal production runner-selection factory。

只集中 production `DomainRunner` + `DispatchEngine` selection；不掃描檔案、
不建立 plugin registry或 service locator。`be_eco` 與 `eda_convergence` 的 runner
以固定模組名稱延遲載入，不在這次安裝中時回報 `WorkflowNotInstalledError`。`child_run.py`
的 `_build_kernel()` 透過本模組取得 runner，取代原本硬寫
`BeEcoRunner(...)` 的單一分支。
"""

from __future__ import annotations

from pathlib import Path
import importlib
import importlib.util
from typing import Any

from terminux_engine.orchestration.dispatch_engine import DispatchEngine, EngineMode
from terminux_engine.orchestration.domain_runner import DomainRunner
from terminux_engine.package_runtime.dispatcher import PackageDispatcher
from terminux_engine.package_runtime.loader import PackageLoader, RuntimePackage
from terminux_engine.package_runtime.registry import PackageRegistry
from terminux_engine.package_runtime.services import RuntimeServices
from terminux_engine.orchestration.ipc_llm import IpcLlmService, JsonRpcIpcClient

BE_ECO_WORKFLOW = "be_eco"
EDA_CONVERGENCE_WORKFLOW = "eda_convergence"

#: 受限的單次 ORFS 執行。**只**接受由 host live task binding
#: 產生的固定 profile/input/output 設定；不開放 generic tool_id 或任意 config。
ORFS_SINGLE_RUN_WORKFLOW = "orfs_single_run"
PACKAGE_SINGLE_TOOL_WORKFLOW = "package_single_tool"

_SUPPORTED_WORKFLOWS = frozenset({
    BE_ECO_WORKFLOW, EDA_CONVERGENCE_WORKFLOW, ORFS_SINGLE_RUN_WORKFLOW,
    PACKAGE_SINGLE_TOOL_WORKFLOW,
})

WORKFLOW_NOT_INSTALLED = "workflow_not_installed"

#: optional workflow → runner 模組。是否已安裝只看這個模組本身是否存在。
_OPTIONAL_WORKFLOW_MODULES = {
    BE_ECO_WORKFLOW: "terminux_engine.orchestration.be_eco_runner",
    EDA_CONVERGENCE_WORKFLOW: "terminux_engine.orchestration.eda_convergence_runner",
}


class WorkflowNotInstalledError(ValueError):
    """workflow 名稱合法，但它的 runner 模組不在這次安裝中。"""

    code = WORKFLOW_NOT_INSTALLED

    def __init__(self, workflow: str) -> None:
        super().__init__(f"orchestration workflow is not installed: {workflow!r}")
        self.workflow = workflow


def _import_workflow_module(module_name: str, workflow: str) -> Any:
    """延遲載入 optional workflow 的 runner 模組。

    只有該模組本身（或它所在的上層 package）不存在時才視為未安裝；模組存在但
    內部 import 失敗時照常拋出原始錯誤，不把真正的問題當成「未安裝」。
    """
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and (module_name == missing or module_name.startswith(missing + ".")):
            raise WorkflowNotInstalledError(workflow) from exc
        raise


def _dqe_package_root() -> Path:
    from terminux_engine import packages as builtin_packages

    return Path(builtin_packages.__file__).resolve().parent


def _load_workflow_package_registry(runner_module: Any) -> PackageRegistry:
    """載入 optional workflow 的 runner 模組在 `DQE_PACKAGE_IDS` 宣告的 DQE package。

    workflow 本身的 package actions 由 runner 直接呼叫，不經 DispatchEngine 事件派發；
    這裡只載入處理工具事件的 DQE package。
    """
    package_root = _dqe_package_root()
    package_dirs = [package_root / package_id for package_id in runner_module.DQE_PACKAGE_IDS]
    return PackageRegistry(PackageLoader().load_many(package_dirs))


def _package_single_tool_dqe_registry(config: dict[str, Any]) -> PackageRegistry:
    """從已釘住 descriptor hook 載入單工具 DQE package。

    Common Runtime 不知道 evaluator、method 或 CAD 名稱；只驗證 hook 的
    Python namespace 與回傳的既有 RuntimePackage identity。
    """
    hook = config.get("dqe_hook")
    if hook is None:
        return PackageRegistry([])
    spec = hook["registry_loader_spec"]
    module_name, separator, callable_name = spec.partition(":")
    if not separator or not module_name or not callable_name:
        raise ValueError("package DQE hook loader spec is invalid")
    loader = getattr(importlib.import_module(module_name), callable_name, None)
    package = loader() if callable(loader) else None
    if not isinstance(package, RuntimePackage):
        # 不接受任意 dict 或 pseudo-package；這不是第二套 dispatcher。
        raise ValueError("package DQE hook did not return RuntimePackage")
    if package.id != hook["package_id"]:
        raise ValueError("package DQE hook identity differs from descriptor")
    return PackageRegistry([package])


def is_workflow_installed(workflow: str) -> bool:
    """optional workflow 的 runner 模組是否在這次安裝中。

    非 optional 的 workflow 一律回 True；名稱是否受支援另由 `is_supported_workflow` 判斷。
    只查模組是否存在，不載入它。
    """
    module_name = _OPTIONAL_WORKFLOW_MODULES.get(workflow)
    if module_name is None:
        return True
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def unsupported_workflow_error(workflow: str) -> ValueError:
    return ValueError(f"unsupported orchestration workflow: {workflow!r}")


def is_supported_workflow(workflow: str) -> bool:
    return workflow in _SUPPORTED_WORKFLOWS


def build_tool_registry(*, workflow: str, config: dict[str, Any],
                        run_id: str | None = None,
                        run_metadata: dict[str, Any] | None = None) -> Any:
    """依 workflow 建立 child 端的 `ToolRegistry`。

    既有兩個 workflow 沿用 `builtin_tools.get_default_registry()`
    （icc2_shell／mmmc_sum／pt_shell）。`orfs_single_run` 需要 ORFS tool，
    而 builtin registry 沒有它——因此在這裡依 workflow 分派，而不是讓
    `child_run` 硬寫一份。

    `run_metadata` 是 ledger 內這筆 run 的 metadata。只有 host 的
    `start_task_run()` 會寫 `agent_task` 子樹（含 user 的 `execution` 宣告與
    task identity）；公開 `orchestration.start` 沒有它，因此拿不到這條路。
    """
    from terminux_engine.orchestration.builtin_tools import get_default_registry

    if workflow == ORFS_SINGLE_RUN_WORKFLOW:
        from terminux_engine.orchestration.orfs_single_run_runner import (
            build_orfs_single_run_tool_registry,
        )

        agent_task = (run_metadata or {}).get("agent_task") or {}
        execution = agent_task.get("execution") if isinstance(agent_task, dict) else None
        identity = {
            "task_id": agent_task.get("task_id") if isinstance(agent_task, dict) else None,
            "slot": agent_task.get("slot") if isinstance(agent_task, dict) else None,
            "run_id": run_id,
        }
        return build_orfs_single_run_tool_registry(
            config, execution=execution if isinstance(execution, dict) else None,
            task_identity=identity)
    if workflow == PACKAGE_SINGLE_TOOL_WORKFLOW:
        from terminux_engine.orchestration.tool_registry import ToolDefinition, ToolRegistry
        from terminux_engine.package_runtime.approved_local_tool import (
            ApprovedLocalToolAdapter, build_package_adapter, validate_package_single_tool_config,
        )

        validate_package_single_tool_config(config)
        if config.get("schema_version") == "package.adapter_binding.v1":
            adapter = build_package_adapter(config)
            hook = config.get("dqe_hook")
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id=config["tool_id"], executables=[config["host_executable"]],
                event_tool_name=config["tool_id"], expected_packages=(
                    [hook["package_id"]] if isinstance(hook, dict) else []),
                default_timeout=float(config["timeout_seconds"]),
                command_builder=lambda _parameters: [], adapter=adapter))
            return registry
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            tool_id=config["tool_id"], executables=[config["executable"]],
            event_tool_name=config["tool_id"], expected_packages=[],
            default_timeout=float(config["timeout_seconds"]),
            command_builder=lambda _parameters: [],
            adapter=ApprovedLocalToolAdapter(
                executable=config["executable"],
                executable_sha256=config["executable_sha256"],
                argument_template=config["argument_template"],
                materialized_sha256=config["materialized_sha256"],
                artifact_paths=config["artifact_paths"],
                candidate_value=config["candidate_value"])))
        return registry
    return get_default_registry()


def build_domain_runner_and_engine(
    *,
    workflow: str,
    config: dict[str, Any],
    parent_daemon_socket: str | None,
    language: str,
    orchestration_run_id: str,
) -> tuple[DomainRunner, DispatchEngine]:
    """依 workflow 建立 production `DomainRunner` 與其 `DispatchEngine`。

    Unknown workflow 在 Kernel 建構前 fail closed（`unsupported_workflow_error`）。
    """
    if workflow == BE_ECO_WORKFLOW:
        be_eco_runner = _import_workflow_module(
            _OPTIONAL_WORKFLOW_MODULES[BE_ECO_WORKFLOW], BE_ECO_WORKFLOW)
        be_eco_runner.validate_be_eco_workflow_config(config)
        services = RuntimeServices()
        engine = DispatchEngine(
            PackageDispatcher(_load_workflow_package_registry(be_eco_runner)),
            mode=EngineMode.STANDALONE,
            services=services,
        )
        runner: DomainRunner = be_eco_runner.BeEcoRunner(
            llm_service_factory=lambda: IpcLlmService(JsonRpcIpcClient(parent_daemon_socket)),
            workspace_root=Path(config["workspace"]),
            services=services,
            dqe_db_path=config["dqe_db_path"],
            language=language,
        )
        return runner, engine

    if workflow == EDA_CONVERGENCE_WORKFLOW:
        eda_convergence_runner = _import_workflow_module(
            _OPTIONAL_WORKFLOW_MODULES[EDA_CONVERGENCE_WORKFLOW], EDA_CONVERGENCE_WORKFLOW)
        eda_convergence_runner.validate_eda_convergence_workflow_config(config)
        services = RuntimeServices()
        engine = DispatchEngine(
            PackageDispatcher(_load_workflow_package_registry(eda_convergence_runner)),
            mode=EngineMode.STANDALONE,
            services=services,
        )
        eda_runner: DomainRunner = eda_convergence_runner.EdaConvergenceRunner(
            llm_service_factory=lambda: IpcLlmService(JsonRpcIpcClient(parent_daemon_socket)),
            workspace_root=Path(config["workspace"]),
            services=services,
            dqe_db_path=config["dqe_db_path"],
            language=language,
            orchestration_run_id=orchestration_run_id,
        )
        return eda_runner, engine

    if workflow == ORFS_SINGLE_RUN_WORKFLOW:
        from terminux_engine.orchestration.orfs_single_run_runner import (
            build_orfs_single_run_engine,
            build_orfs_single_run_runner,
            validate_orfs_single_run_workflow_config,
        )

        validate_orfs_single_run_workflow_config(config)
        orfs_engine = build_orfs_single_run_engine()
        return build_orfs_single_run_runner(config), orfs_engine

    if workflow == PACKAGE_SINGLE_TOOL_WORKFLOW:
        from terminux_engine.package_runtime.approved_local_tool import (
            PackageSingleToolRunner, validate_package_single_tool_config,
        )

        validate_package_single_tool_config(config)
        engine = DispatchEngine(
            PackageDispatcher(_package_single_tool_dqe_registry(config)), mode=EngineMode.STANDALONE,
            services=RuntimeServices())
        return PackageSingleToolRunner(), engine

    raise unsupported_workflow_error(workflow)
