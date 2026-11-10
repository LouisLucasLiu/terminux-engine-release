# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
# 出處：本模組呼叫第三方開源工具 OpenROAD-flow-scripts（ORFS）與 OpenROAD
# （皆為 BSD 3-Clause，Terminux 未隨附），詳見 THIRD_PARTY_NOTICES.md 的 External EDA Tools 一節。
"""`terminux-orfs-run` — 從 Terminux Terminal 提交一次 typed ORFS request
給 Orchestration Kernel。

這是「最小 CLI／RPC entry」實作：一個獨立、self-contained 的
console-script，直接在 process 內建構真實的
`OrchestrationKernel`／`SubprocessExecutor`／`DispatchEngine`／
`OrchestrationLedger`（與既有 `orchestration.start` JSON-RPC 最終呼叫的
`child_run.py::_build_kernel()` 使用同一組 production class），呼叫
`kernel.run()` 阻塞直到完成，印出 `ExecutionReceipt` JSON。

**範圍聲明（明確、非隱藏）**：目前沒有把 `single_tool_execution` workflow
接進既有的 daemon child-process 監督機制（`child_run.py`／
`orchestration.start` RPC／heartbeat／`OrchestrationService`）——那套機制
針對的是需要背景執行、pause/cancel/resume 的長流程 domain run
（`be_eco`／`eda_convergence`），本身有一整層 daemon 生命週期管理，貿然
接入需要額外的 daemon 端整合驗證，不在這個 CLI 的範圍內。本 CLI 呼叫的是**完全
相同**的 Kernel／Ledger／Barrier／DispatchEngine class（重用要求
在「同一套 lifecycle authority」而非「同一個 process supervision 外殼」
的意義上被滿足），只是省略了 daemon 的背景子行程監督外殼——命令列本身
就是阻塞執行到完成，不需要背景監督。若未來需要背景執行／pause/resume
控制，屬於獨立、需要另行決定範圍的後續工作。

ORFS 以 host-native 方式執行：使用者以自己的帳號，透過管理員安裝的 ORFS 與 EDA
工具（`--env-script` 指定環境）執行 flow，輸出寫進使用者自己的 `--work-home`。不需要
container、root 或 sudo，也沒有 container 的執行路徑；以 root 身分執行會被拒絕。

Kernel 仍然是唯一持有／管理工具 subprocess 的角色——本 CLI 不自己組命令、不直接
呼叫 `subprocess`，只負責解析 argument、建立 adapter config、把它交給 Kernel。

receipt 預設投影到目前使用者的 EventStore（`--receipt-store` 可另指定，
`--no-receipt` 不投影）。Package after hook 產生的 record 寫入同一個 EventStore，
`--no-receipt` 時只留在記憶體。exit code：

- 0：tool execution 成功，且 receipt 已持久化（`--no-receipt` 時只看前者）；
- 1：tool execution 失敗（receipt 的問題另外回報在 stderr）；
- 2：參數錯誤，包含 `--ledger-db` 指向 daemon 的正式 ledger；
- 3：tool execution 成功，但 receipt 沒有持久化。

收到 SIGINT／SIGTERM 時，先終止工具的 process group 並確認它已不存在、完成可信的
evidence 與輸出，再以原 signal 結束（shell 看到 130／143）。只有確認 process group
已結束後才寫 `cancelled` receipt；無法確認時不寫 receipt，stderr 回報
`EXECUTION_CLEANUP_UNCONFIRMED`。terminal receipt 已完成之後才收到的 signal 不改變
exit code。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import shutil
import signal
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.lineage.artifact_capture import ArtifactCaptureService
from terminux_engine.orchestration.cli_interruption import (
    PHASE_EXECUTING,
    PHASE_FINALIZING,
    PHASE_PERSISTED,
    PHASE_PREPARING,
    CliInterruptController,
    FinalizationAborted,
    exit_with_signal,
)
from terminux_engine.orchestration.dispatch_engine import DispatchEngine, EngineMode
from terminux_engine.orchestration.execution_contract import build_receipt
from terminux_engine.orchestration.kernel import OrchestrationKernel
from terminux_engine.orchestration.ledger import OrchestrationLedger
from terminux_engine.orchestration.execution_contract import ExecutionContractError
from terminux_engine.orchestration.orfs_native_adapter import (
    ORFS_TOOL_ID,
    TOOL_OVERRIDE_NAMES,
    OrfsNativeAdapterConfig,
    build_orfs_native_tool_definition,
)
from terminux_engine.orchestration.single_tool_runner import SingleToolRunner
from terminux_engine.orchestration.subprocess_executor import InterruptibleSubprocessExecutor
from terminux_engine.orchestration.tool_registry import ToolRegistry
from terminux_engine.package_runtime.console_notifications import ConsoleNotificationSink
from terminux_engine.package_runtime.dispatcher import PackageDispatcher
from terminux_engine.package_runtime.loader import PackageLoader
from terminux_engine.package_runtime.registry import PackageRegistry
from terminux_engine.package_runtime.services import NotifyService, RuntimeServices, StoreService


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one ORFS flow through the Orchestration Kernel and record an "
            "execution receipt."
        )
    )
    parser.add_argument("--orfs-repo-root", required=True,
                        help="Host path to the ORFS installation root (may be read-only)")
    parser.add_argument(
        "--work-home", required=True,
        help=("Directory you own that receives this run's results/, reports/, logs/ and "
              "objects/ (ORFS WORK_HOME); created if its parent exists"))
    parser.add_argument(
        "--env-script", default=None,
        help="Environment script that puts the EDA tools in place (default: <orfs-repo-root>/env.sh)")
    parser.add_argument(
        "--make-executable", default=None,
        help="make used to run the flow (default: make found on PATH)")
    parser.add_argument("--openroad-exe", default=None,
                        help="Explicit openroad binary (ORFS OPENROAD_EXE)")
    parser.add_argument("--yosys-exe", default=None,
                        help="Explicit yosys binary (ORFS YOSYS_EXE)")
    # 舊版的 container 參數：只為了給出明確的遷移訊息，不提供任何執行路徑。
    parser.add_argument("--docker-image", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--design", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--variant", required=True, help="FLOW_VARIANT (must not be 'base')")
    parser.add_argument("--design-config", required=True, help="Path to config.mk relative to flow/")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--ledger-db",
        required=True,
        help=("Disposable SQLite path for the orchestration ledger "
              "(must not be the daemon's orchestration ledger)"),
    )
    receipt_target = parser.add_mutually_exclusive_group()
    receipt_target.add_argument(
        "--receipt-store",
        default=None,
        help=("Project the execution receipt into this EventStore instead of the "
              "current user's default store (TERMINUX_EVENT_STORE_PATH, or "
              "~/.terminux/ecc/event_store.db)."),
    )
    receipt_target.add_argument(
        "--no-receipt",
        action="store_true",
        help=("Do not project the execution receipt into any EventStore; "
              "exit codes keep their previous meaning (0/1)."),
    )
    parser.add_argument(
        "--receipt-out",
        required=True,
        help="Path to write the ExecutionReceipt JSON",
    )
    return parser.parse_args(argv)


#: ORFS 的 production DQE package id。ORFS-specific package
#: selection 留在這個 ORFS composition root；common loader、
#: registry 與 dispatcher 完全不知道 `orfs_dqe` 的存在。
ORFS_DQE_PACKAGE_ID = "orfs_dqe"


def load_orfs_packages() -> list[Any]:
    """以既有 standard package loader 載入 ORFS production package。

    與 `orchestration/runner_factory.py::_load_workflow_package_registry()`
    相同的作法：明確列出 package 目錄，交給既有 `PackageLoader`
    ——不動態掃描、不自建 loader、不載入測試 fixture。
    """
    from terminux_engine import packages as builtin_packages

    package_root = Path(builtin_packages.__file__).resolve().parent
    return PackageLoader().load_many([package_root / ORFS_DQE_PACKAGE_ID])


#: flow 成功但 receipt 沒有持久化時的 exit code。
EXIT_RECEIPT_NOT_DURABLE = 3
RECEIPT_NOT_DURABLE_CODE = "EXECUTION_RECEIPT_NOT_DURABLE"
PACKAGE_RECORD_STORE_UNAVAILABLE_CODE = "PACKAGE_RECORD_STORE_UNAVAILABLE"
LEDGER_SHARED_CODE = "LEDGER_SHARED_WITH_DAEMON"
INTERRUPTED_CODE = "EXECUTION_INTERRUPTED"
INTERRUPTED_BEFORE_START_CODE = "EXECUTION_INTERRUPTED_BEFORE_START"
CLEANUP_UNCONFIRMED_CODE = "EXECUTION_CLEANUP_UNCONFIRMED"
DOCKER_EXECUTION_REMOVED_CODE = "DOCKER_EXECUTION_REMOVED"
RUN_AS_ROOT_REFUSED_CODE = "NATIVE_RUN_AS_ROOT_REFUSED"
NATIVE_SETUP_INVALID_CODE = "NATIVE_SETUP_INVALID"
FINALIZATION_INTERRUPTED_CODE = "EXECUTION_FINALIZATION_INTERRUPTED"
SIGNAL_DURING_FINALIZATION_CODE = "SIGNAL_DURING_FINALIZATION"
SIGNAL_AFTER_PERSISTENCE_CODE = "SIGNAL_AFTER_PERSISTENCE"

_LEDGER_ENV = "TERMINUX_ORCHESTRATION_LEDGER_PATH"
_LEDGER_RELPATH = Path(".terminux") / "orchestration" / "ledger.sqlite"
_EVENT_STORE_ENV = "TERMINUX_EVENT_STORE_PATH"
_DEFAULT_EVENT_STORE = "~/.terminux/ecc/event_store.db"
#: receipt 投影與 Package record 共用的 EventStore session 標記。
_CLI_SESSION_ID = "orchestration-cli"


def default_receipt_store_path() -> Path:
    """目前使用者的 EventStore；與 daemon 及 `child_run` 使用相同規則。"""
    return Path(os.environ.get(_EVENT_STORE_ENV, os.path.expanduser(_DEFAULT_EVENT_STORE)))


def _passwd_home() -> Path | None:
    try:
        from terminux_engine.ipc.agent_socket import home_for_uid
        return home_for_uid(os.geteuid())
    except (KeyError, OSError):
        return None


def daemon_ledger_candidates() -> list[Path]:
    """daemon 正式 ledger 可能的位置（`OrchestrationLedger()` 的預設規則）。

    daemon 以 `OrchestrationLedger()` 開啟 ledger：先看
    `TERMINUX_ORCHESTRATION_LEDGER_PATH`，否則用 `$HOME` 下的預設路徑。
    passwd HOME 也列入，涵蓋 `$HOME` 被改過的 shell。
    """
    candidates: list[Path] = []
    override = os.environ.get(_LEDGER_ENV)
    if override:
        candidates.append(Path(override))
    candidates.append(Path.home() / _LEDGER_RELPATH)
    passwd_home = _passwd_home()
    if passwd_home is not None:
        candidates.append(passwd_home / _LEDGER_RELPATH)
    return candidates


def _same_file(left: Path, right: Path) -> bool:
    """以 realpath 比對（symlink、`..`、目錄別名），兩者都存在時再比 inode（hard link）。"""
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def shared_daemon_ledger(ledger_db: Path) -> Path | None:
    """`ledger_db` 指向 daemon 正式 ledger 時回傳對應的候選路徑，否則 `None`。"""
    for candidate in daemon_ledger_candidates():
        if _same_file(ledger_db, candidate):
            return candidate
    return None


@dataclass(frozen=True)
class OrfsCliComposition:
    """`terminux-orfs-run` 的 production composition root 產物。

    抽成獨立函式（而非埋在 `main()` 裡）純粹是為了讓測試能在不碰使用者
    真實 HOME 的前提下驗證**同一段** production wiring；`main()` 走的就是
    這條路徑，沒有任何 test-only 分支或 hidden flag。
    """

    tool_registry: ToolRegistry
    kernel: OrchestrationKernel
    artifact_capture_service: ArtifactCaptureService
    notification_sink: ConsoleNotificationSink
    ledger: OrchestrationLedger | None = None


def build_composition(
    adapter_config: OrfsNativeAdapterConfig,
    *,
    ledger_db: Path,
    timeout_seconds: float = 900.0,
    artifact_manifest_root: Path | None = None,
    subprocess_executor: object | None = None,
    notification_stream: object | None = None,
    record_store: StoreService | None = None,
) -> OrfsCliComposition:
    """建立 production Kernel，含 artifact lineage artifact capture service。

    `artifact_manifest_root=None`（`main()` 的行為）時使用既有 durable
    root `~/.terminux/lineage/artifact_manifests`，與
    `OrchestrationLedger.DEFAULT_DB_PATH` 同一個 `~/.terminux/` 慣例。

    `subprocess_executor=None`（`main()` 的行為）時由 Kernel 使用既有的
    production `SubprocessExecutor`；這個參數與 `OrchestrationKernel`
    本身既有的 optional DI 同一性質，讓測試能在不執行 EDA 工具的情況下
    走**完全相同**的 composition 與 capture 路徑。

    `notification_stream=None`（`main()` 的行為）時，
    Package 產生的 notification 會呈現在 CLI 的 stdout——這是 standalone
    Terminal 的使用者可觀察 sink。`ConsoleNotificationSink`
    是 vendor-neutral 的：它只認識 level／title／message／actions，不知道
    `orfs_dqe`、ORFS 或任何 metric。這裡只負責**注入**它。

    `record_store` 接收 Package 的 `RecordRequest`；`None` 時 record 只留在記憶體。
    `main()` 傳入與 receipt 投影相同 EventStore 與身分的 `StoreService`。

    capture service 與 notification sink 都是 domain-blind 的
    ——它們不知道 ORFS、artifact 檔名或 role；vendor-specific 的部分全部留在
    `build_orfs_native_tool_definition()` 建出的 adapter 與 `orfs_dqe` package 裡。
    """
    # production 端的 expected package 是真實的 `orfs_dqe`
    #。Barrier 的 evidence readiness 因此必須等到 `orfs_dqe`
    # 的 after phase 真的完成並持久化 DQE 結果才會 ready。
    tool_registry = ToolRegistry()
    tool_registry.register(
        build_orfs_native_tool_definition(
            adapter_config,
            expected_packages=[ORFS_DQE_PACKAGE_ID],
            default_timeout=timeout_seconds,
        )
    )

    # sink 顯示的是 Package Runtime 已處理的
    # `NotificationRequest` 原物件；CLI 不重讀 ORFS report、不重算 DQE、
    # 也不自行拼裝訊息。
    notification_sink = ConsoleNotificationSink(notification_stream)  # type: ignore[arg-type]
    services = RuntimeServices(
        notify=NotifyService(sink=notification_sink),
        store=record_store if record_store is not None else StoreService(),
    )
    engine = DispatchEngine(
        PackageDispatcher(PackageRegistry(load_orfs_packages())),
        mode=EngineMode.STANDALONE,
        services=services,
    )
    ledger = OrchestrationLedger(ledger_db)
    # production entry point 一律注入 capture service，
    # artifact manifest 不再只在測試路徑上產生。
    capture_service = ArtifactCaptureService.with_default_root(artifact_manifest_root)
    kernel = OrchestrationKernel(
        engine=engine,
        ledger=ledger,
        tool_registry=tool_registry,
        runner=SingleToolRunner(),
        subprocess_executor=subprocess_executor,
        artifact_capture_service=capture_service,
    )
    return OrfsCliComposition(
        tool_registry=tool_registry,
        kernel=kernel,
        artifact_capture_service=capture_service,
        notification_sink=notification_sink,
        ledger=ledger,
    )


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def _new_interrupt_controller() -> CliInterruptController:
    return CliInterruptController()


def _exit_with_signal(signo: int) -> None:
    exit_with_signal(signo)


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    args = _parse_args(argv)

    if args.docker_image is not None:
        print(f"error: {DOCKER_EXECUTION_REMOVED_CODE}: --docker-image is no longer supported; "
              "ORFS runs on the host with your own account. Use --env-script (default "
              "<orfs-repo-root>/env.sh) and --work-home instead.", file=sys.stderr)
        return 2
    if os.geteuid() == 0:
        print(f"error: {RUN_AS_ROOT_REFUSED_CODE}: run terminux-orfs-run as your own "
              "(non-root) account", file=sys.stderr)
        return 2

    from terminux_engine.ipc.daemon_owner import check_local_database_access
    owner_check = check_local_database_access()
    if owner_check:
        return owner_check

    if args.variant == "base":
        print("error: --variant must not be 'base' (reserved for the existing manual baseline run)", file=sys.stderr)
        return 2

    orfs_repo_root = Path(args.orfs_repo_root).resolve()
    flow_dir = orfs_repo_root / "flow"
    if not flow_dir.is_dir():
        print(f"error: flow directory not found: {flow_dir}", file=sys.stderr)
        return 2

    # CLI run 不得寫進 daemon 的正式 ledger（daemon 重啟時會把它當成遺留
    # run 處理）。必須在開啟 ledger、啟動 execution 之前拒絕。
    shared = shared_daemon_ledger(Path(args.ledger_db))
    if shared is not None:
        print(f"error: {LEDGER_SHARED_CODE}: --ledger-db refers to the daemon's "
              f"orchestration ledger ({shared}); use a separate disposable ledger path",
              file=sys.stderr)
        return 2

    try:
        adapter_config = _native_config(args, orfs_repo_root=orfs_repo_root, flow_dir=flow_dir)
    except (ExecutionContractError, OSError) as exc:
        print(f"error: {NATIVE_SETUP_INVALID_CODE}: {exc}", file=sys.stderr)
        return 2

    controller = _new_interrupt_controller()
    controller.install()
    try:
        code = _run(args, controller, adapter_config)
    finally:
        controller.restore()
    if code is None:
        # 必要的清理、evidence 持久化與輸出都已完成，才以原 signal 結束。
        signo = controller.signal_number or signal.SIGTERM
        _exit_with_signal(signo)
        return 128 + signo
    return code


def _absolute_executable(value: str | None, *, default_name: str | None,
                         label: str) -> Path | None:
    if value is None:
        if default_name is None:
            return None
        found = shutil.which(default_name)
        if found is None:
            raise ExecutionContractError(f"{label}: '{default_name}' was not found on PATH")
        return Path(found).absolute()
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ExecutionContractError(f"{label} must be an absolute path, got {value!r}")
    return path


def _prepare_work_home(value: str) -> Path:
    """`--work-home` 必須是使用者自己的目錄；不存在時只建立最後一層。"""
    work_home = Path(value).expanduser()
    if not work_home.is_absolute():
        work_home = Path.cwd() / work_home
    if not work_home.exists():
        work_home.mkdir(mode=0o750)
    return work_home.resolve()


def _native_config(args: argparse.Namespace, *, orfs_repo_root: Path,
                   flow_dir: Path) -> OrfsNativeAdapterConfig:
    env_script = (Path(args.env_script).expanduser() if args.env_script
                  else orfs_repo_root / "env.sh")
    if not env_script.is_absolute():
        env_script = Path.cwd() / env_script
    make = _absolute_executable(args.make_executable, default_name="make",
                                label="--make-executable")
    assert make is not None
    overrides: dict[str, Path] = {}
    for name, value in (("OPENROAD_EXE", args.openroad_exe), ("YOSYS_EXE", args.yosys_exe)):
        path = _absolute_executable(value, default_name=None,
                                    label=f"--{name.split('_')[0].lower()}-exe")
        if path is not None:
            overrides[name] = path
    assert set(overrides) <= TOOL_OVERRIDE_NAMES
    return OrfsNativeAdapterConfig(
        orfs_repo_root=orfs_repo_root,
        flow_dir=flow_dir,
        work_home=_prepare_work_home(args.work_home),
        env_script=env_script,
        make_executable=make,
        design=args.design,
        platform=args.platform,
        variant=args.variant,
        design_config_relpath=args.design_config,
        tool_overrides=overrides,
    )


def _open_record_event_store(args: argparse.Namespace) -> Any | None:
    """開啟 Package record 要寫入的 EventStore，路徑規則與 receipt 投影相同。

    `--no-receipt` 不寫任何 EventStore。開啟失敗不影響 flow：receipt 投影另以 exit 3 回報，
    這裡只說明這次 run 的 Package record 不會持久化。
    """
    if args.no_receipt:
        return None
    from terminux_engine.store.event_store import EventStore

    try:
        return EventStore(_store_path(args))
    except Exception as exc:  # noqa: BLE001 - 開不了 store 不得改變 flow 的結果
        print(f"warning: {PACKAGE_RECORD_STORE_UNAVAILABLE_CODE}: event store could not be "
              f"opened; package records will not be persisted: {exc!r}", file=sys.stderr)
        return None


def _run(args: argparse.Namespace, controller: CliInterruptController,
         adapter_config: OrfsNativeAdapterConfig) -> int | None:
    """開啟 record store、執行並收尾；回傳值同 `_run_flow`。"""
    record_event_store = _open_record_event_store(args)
    try:
        return _run_flow(args, controller, adapter_config, record_event_store)
    finally:
        if record_event_store is not None:
            record_event_store.close()


def _run_flow(args: argparse.Namespace, controller: CliInterruptController,
              adapter_config: OrfsNativeAdapterConfig,
              record_event_store: Any | None) -> int | None:
    """執行並收尾；回傳 exit code，`None` 代表要以收到的 signal 結束。"""
    step_id = f"orfs-{args.design}-{args.variant}"
    run_id = _new_run_id()
    work_home = adapter_config.work_home
    composition = build_composition(
        adapter_config,
        ledger_db=Path(args.ledger_db),
        timeout_seconds=args.timeout_seconds,
        subprocess_executor=InterruptibleSubprocessExecutor(controller),
        record_store=StoreService(record_event_store, session_id=_CLI_SESSION_ID,
                                  user_id=getpass.getuser(), project_id=None),
    )
    if controller.interrupted:
        print(f"error: {INTERRUPTED_BEFORE_START_CODE}: signal={controller.signal_number}; "
              "nothing was started and no receipt was written", file=sys.stderr)
        return None

    workflow_config = {
        "tool_id": ORFS_TOOL_ID,
        "parameters": {},
        # 工具的 cwd 與 output boundary 都是使用者的 work_home；flow/ 只讀不寫。
        "cwd": str(work_home),
        "step_id": step_id,
    }
    # run 先建立在這個 CLI 自己的 standalone ledger，run 身分因此在工具啟動之前
    # 就已固定；Kernel 以既有的 attach 路徑接手。
    ledger = composition.ledger or composition.kernel._ledger  # noqa: SLF001
    ledger.create_run(run_id=run_id, workflow_type=SingleToolRunner.workflow_type,
                      metadata={"workflow_config": workflow_config})
    ledger.start_run(run_id)
    controller.enter_phase(PHASE_EXECUTING)

    progress = {"receipt_out_written": False}
    try:
        run_outcome = composition.kernel.run(workflow_config, run_id=run_id)
        if _interrupted_branch(run_outcome, controller):
            return _finish_interrupted(args, composition, run_outcome, controller,
                                       run_id=run_id, work_home=work_home)
        if controller.phase in (PHASE_PREPARING, PHASE_EXECUTING):
            controller.enter_phase(PHASE_FINALIZING)
        return _finish(args, composition, run_outcome, controller, work_home=work_home,
                       progress=progress)
    except FinalizationAborted:
        receipt_state = ("written_not_projected" if progress["receipt_out_written"]
                         else "not_written")
        print(f"error: {FINALIZATION_INTERRUPTED_CODE}: run_id={run_id} "
              f"signal={controller.signal_number} "
              f"process_exit_code={controller.process_exit_code} receipt={receipt_state}; "
              "finalization did not complete and the run is not verified",
              file=sys.stderr)
        return None


def _interrupted_branch(run_outcome: Any, controller: CliInterruptController) -> bool:
    if run_outcome is not None:
        return run_outcome.final_status in {"cancelled", "abandoned"}
    return controller.interrupted


def _finish_interrupted(args: argparse.Namespace, composition: OrfsCliComposition,
                        run_outcome: Any, controller: CliInterruptController, *,
                        run_id: str, work_home: Path) -> None:
    """中斷之後：只有 Kernel 已確認中斷（run `cancelled`）才寫 cancelled receipt。"""
    step = run_outcome.steps[0] if run_outcome is not None and run_outcome.steps else None
    if run_outcome is None or run_outcome.final_status != "cancelled" or step is None:
        print(f"error: {CLEANUP_UNCONFIRMED_CODE} run_id={run_id} "
              f"reason={_unconfirmed_reason(run_outcome, step)}; "
              "no receipt was written and the interruption is not verified; check for "
              "remaining flow processes started from this run's work home", file=sys.stderr)
        return None
    receipt = _build_receipt(composition, run_outcome, step)
    receipt_json = receipt.to_json()
    _write_atomic(Path(args.receipt_out), receipt_json)
    state = "not_projected"
    if not args.no_receipt:
        projection = _project_receipt(
            receipt, receipt_json=receipt_json, store_path=_store_path(args),
            argv=_step_argv(step), cwd=str(work_home))
        print(json.dumps({"execution_receipt_projection": projection.to_json_dict()},
                         ensure_ascii=False))
        state = "durable" if projection.durable else "not_durable"
        if not projection.durable:
            print(f"error: {RECEIPT_NOT_DURABLE_CODE}: execution receipt was not persisted "
                  f"({projection.outcome}: {projection.error_code})", file=sys.stderr)
    print(receipt_json)
    print(f"error: {INTERRUPTED_CODE}: run_id={run_id} signal={controller.signal_number} "
          f"process_group=confirmed_gone receipt={state}", file=sys.stderr)
    return None


def _unconfirmed_reason(run_outcome: Any, step: Any) -> str:
    if run_outcome is None:
        return "terminal_state_conflict"
    result = getattr(step, "subprocess_result", None)
    if result is None or getattr(result, "process_group_confirmed_gone", None) is not True:
        return "process_group_alive"
    cleanup = getattr(step, "cleanup_result", None)
    if not isinstance(cleanup, dict):
        return "cleanup_unavailable"
    return str(cleanup.get("reason") or "cleanup_unconfirmed")


def _finish(args: argparse.Namespace, composition: OrfsCliComposition, run_outcome: Any,
            controller: CliInterruptController, *, work_home: Path,
            progress: dict[str, bool]) -> int | None:
    """正常結束或逾時的收尾：receipt 照實記錄真實結果。"""
    if run_outcome is None or not run_outcome.steps:
        print("error: kernel run produced no step outcome", file=sys.stderr)
        return 1

    step_outcome = run_outcome.steps[0]
    receipt = _build_receipt(composition, run_outcome, step_outcome)
    receipt_json = receipt.to_json()
    _write_atomic(Path(args.receipt_out), receipt_json)
    progress["receipt_out_written"] = True

    # receipt **完整形成之後**才投影。projection 與 tool execution
    # 的結果是分開的兩軸：投影失敗不改變 run 的結論，但 flow 成功時以
    # exit 3 表示這個 run 無法被 durable 查詢。
    flow_succeeded = run_outcome.final_status == "completed"
    projection = None
    if not args.no_receipt:
        projection = _project_receipt(
            receipt, receipt_json=receipt_json, store_path=_store_path(args),
            argv=_step_argv(step_outcome), cwd=str(work_home))
    controller.enter_phase(PHASE_PERSISTED)

    if projection is not None:
        print(json.dumps({"execution_receipt_projection": projection.to_json_dict()},
                         ensure_ascii=False))
    print(receipt_json)
    if projection is not None and not projection.durable:
        print(f"error: {RECEIPT_NOT_DURABLE_CODE}: execution receipt was not persisted "
              f"({projection.outcome}: {projection.error_code})", file=sys.stderr)
    if projection is None:
        code = 0 if flow_succeeded else 1
    elif not flow_succeeded:
        code = 1
    else:
        code = 0 if projection.durable else EXIT_RECEIPT_NOT_DURABLE

    if controller.interrupted and controller.phase_at_signal != PHASE_PERSISTED:
        # 工具已自行結束後才收到 signal：收尾已完成，receipt 記錄真實結果；
        # 仍以 signal 結束，不回報 0／1／3。
        state = ("not_projected" if projection is None
                 else "durable" if projection.durable else "not_durable")
        print(f"error: {SIGNAL_DURING_FINALIZATION_CODE}: run_id={run_outcome.run_id} "
              f"signal={controller.signal_number} "
              f"process_exit_code={controller.process_exit_code} "
              f"final_status={run_outcome.final_status} receipt={state}", file=sys.stderr)
        return None
    if controller.interrupted:
        print(f"note: {SIGNAL_AFTER_PERSISTENCE_CODE}: signal={controller.signal_number} "
              "arrived after the terminal receipt was finalized; exit code unchanged",
              file=sys.stderr)
    return code


def _build_receipt(composition: OrfsCliComposition, run_outcome: Any,
                   step_outcome: Any) -> Any:
    tool_registry = composition.tool_registry
    return build_receipt(
        run_id=run_outcome.run_id,
        step_outcome=step_outcome,
        event_tool_name=tool_registry.event_tool_name(ORFS_TOOL_ID),
        adapter_id=tool_registry.adapter_id(ORFS_TOOL_ID),
        adapter_version=tool_registry.adapter_version(ORFS_TOOL_ID),
    )


def _store_path(args: argparse.Namespace) -> Path:
    return Path(args.receipt_store) if args.receipt_store else default_receipt_store_path()


def _step_argv(step_outcome: Any) -> Any:
    prepared = getattr(step_outcome, "prepared_execution", None)
    return getattr(prepared, "argv", None) if prepared else None


def _write_atomic(path: Path, text: str) -> None:
    """先寫暫存檔再 `os.replace`：中途中斷不會留下半份 receipt。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _project_receipt(receipt: Any, *, receipt_json: str, store_path: Path,
                     argv: Any, cwd: str) -> Any:
    """投影 receipt；EventStore 無法開啟也回報為未持久化，不拋例外。"""
    import hashlib

    from terminux_engine.execution_receipts import wiring
    from terminux_engine.execution_receipts.projection import (
        OUTCOME_WRITE_FAILED,
        WRITE_FAILED_ERROR_CODE,
        ProjectionResult,
    )
    from terminux_engine.store.event_store import EventStore

    try:
        event_store = EventStore(store_path)
    except Exception as exc:  # noqa: BLE001 - 開不了 store 也屬於 receipt 未持久化
        return ProjectionResult(OUTCOME_WRITE_FAILED, None, WRITE_FAILED_ERROR_CODE,
                                f"event store could not be opened: {exc!r}")
    try:
        result = wiring.project_execution_receipt(
            receipt, event_store=event_store,
            source_receipt_sha256=hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
            argv=argv, cwd=cwd, session_id=_CLI_SESSION_ID,
            user_id=getpass.getuser(), project_id=None)
    except Exception as exc:  # noqa: BLE001
        return ProjectionResult(OUTCOME_WRITE_FAILED, None, WRITE_FAILED_ERROR_CODE,
                                f"{exc!r}")
    finally:
        event_store.close()
    if result is None:
        return ProjectionResult(OUTCOME_WRITE_FAILED, None, WRITE_FAILED_ERROR_CODE,
                                "no projection result")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
