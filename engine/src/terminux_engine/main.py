# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""
terminux_engine.main
====================
Python Core 啟動入口。組裝所有模組並啟動 IPC Server。

執行方式：
    python -m terminux_engine
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from terminux_engine.core.ecc_manager import EccManager
from terminux_engine.core.event_bus import EventBus
from terminux_engine.core.event_recorder import EventRecorder
from terminux_engine.core.plugin_manager import PluginManager
from terminux_engine.core.provenance import ProvenanceEngine
from terminux_engine.ecc.models import ShellEvent
from terminux_engine.ipc.agent_socket import (
    AgentSocketError,
    daemon_agent_socket,
    daemon_lineage_socket,
    home_for_uid,
    legacy_migration_warning,
)
from terminux_engine.ipc.peer_identity import capture_daemon_euid
from terminux_engine.ipc import jsonrpc_server as lineage_server
from terminux_engine.ipc.daemon_owner import DaemonOwner, DaemonOwnerExists, _runtime_log_handler
from terminux_engine.ipc.tcp_bridge import TcpBridge
from terminux_engine.ipc.server import IPCServer
from terminux_engine.ipc.session_registry import CanonicalSessionContextRegistry
from terminux_engine.layer1.classifier import CommandClassifier
from terminux_engine.layer1.extractor import DataExtractor
from terminux_engine.layer1.trigger import TriggerEvaluator
from terminux_engine.layer2.context import ContextBuilder
from terminux_engine.layer2.llm import LlmClient
from terminux_engine.layer2.parser import ActionRouter
from terminux_engine import learner_strict
from terminux_engine.llm.task_scope import (
    loaded_authority_identity, outbound_policy_path, policy_identity, profile_containment_active,
)
from terminux_engine.layer3.executor import Executor
from terminux_engine.layer3.notification import Notifier
from terminux_engine.ipc.jsonrpc_server import (
    serve_forever,
    stop_orchestration_heartbeat_scanner,
)
from terminux_engine.store.sqlite import SQLiteRepository
from terminux_engine.resource_paths import bundled_knowledge_path, llm_config_path, plugin_dir

logger = logging.getLogger(__name__)

# ── 固定路徑 ─────────────────────────────────────────────────────────────────
# agent socket（IPCServer）由
# `ipc.agent_socket.daemon_agent_socket()` 在 `run()` 內依 process UID 查帳號資料
# 解析成 `<home>/.terminux/run/<nodename>/terminux.sock`；失敗時沒有 fallback，
# 不依賴 /run/user/<uid>／logind／XDG_RUNTIME_DIR。
# lineage socket 由同一個路徑模組解析，於取得 ownership 後才建立目錄。
_LINEAGE_SOCKET_PATH: str | None = None

# ── 發行資源與使用者可寫位置 ──────────────────────────────────────────────
# knowledge／設定範本由 wheel 提供；使用者設定與外掛獨立保留在 home。
_LLM_CONFIG_PATH = llm_config_path()
_EXTRACT_RULES   = bundled_knowledge_path("extract_rules.yaml")
_PLUGINS_DIR     = plugin_dir()
_DB_PATH         = Path.home() / ".terminux" / "terminux.db"


def _setup_logging() -> None:
    """設定 console logging；預設安全（ERROR），可用 env 提高詳細度以便除錯。

    預設只把 error code 寫到 stderr；正常啟停沒有 stderr，且不啟用可能
    攜帶 path/ref 的 debug stream。除錯時可設 `TERMINUX_LOG_LEVEL`（DEBUG/INFO/
    WARNING/ERROR/CRITICAL）取回詳細 log；無效值或未設一律 fallback 到 ERROR。
    """
    level_name = os.environ.get("TERMINUX_LOG_LEVEL", "ERROR").strip().upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.ERROR)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )


#: owner metadata 只公告下列已知的設定列舉值；其他字串一律改寫為 unrecognized。
_RECOGNIZED_LLM_BACKENDS = frozenset({"", "none", "openai_compatible", "gemini", "llama_cpp"})
_RECOGNIZED_EMBEDDING_BACKENDS = frozenset({"", "noop", "local", "gemini"})


def _package_record_sha256() -> str | None:
    """已安裝套件 dist-info RECORD 的 SHA256；由原始碼目錄執行時沒有 RECORD，回傳 None。

    RECORD 列出套件每個檔案的雜湊，因此它的 SHA256 可作為實際載入之安裝版本的身分，
    不受版本字串相同的影響。只看正在執行的套件所在目錄，不查其他已安裝的副本。
    """
    import terminux_engine

    package_dir = Path(terminux_engine.__file__).resolve().parent
    for candidate in sorted(package_dir.parent.glob("terminux_engine-*.dist-info")):
        try:
            return hashlib.sha256((candidate / "RECORD").read_bytes()).hexdigest()
        except OSError:
            continue
    return None


def _enumerated(value: str | None, recognized: frozenset[str]) -> str:
    """只公告已知的設定列舉值；其他內容一律以 unrecognized 取代，不外洩任意字串。"""
    normalized = (value or "").strip().lower()
    return normalized if normalized in recognized else "unrecognized"


def _runtime_identity() -> dict[str, object]:
    """daemon 自己公告的非機密啟動身分，寫入 owner metadata 供 launcher 與部署檢查核對。

    不含路徑、環境變數值、Profile 內容或任何憑證：Profile 只公告 env 是否存在，
    外送 policy 只公告是否已設定，backend 只公告已知列舉值。Profile 模式另公告 daemon 自己
    驗證成功的 Profile／policy bytes 與路徑字串的 sha256（`task_scope.loaded_authority_identity`）。
    """
    identity: dict[str, object] = {
        "learner_strict": learner_strict.LEARNER_STRICT is True,
        "profile_containment": profile_containment_active(),
        "egress_policy_configured": outbound_policy_path() is not None,
        "approval_mode": _approval_mode(),
        "egress_audit_digest": _egress_audit_digest(),
        "audit_key_id": _audit_key_id(),
        "llm_backend": _enumerated(os.environ.get("TERMINUX_LLM_BACKEND"), _RECOGNIZED_LLM_BACKENDS),
        "embedding_backend": _enumerated(os.environ.get("TERMINUX_EMBEDDING_BACKEND"),
                                         _RECOGNIZED_EMBEDDING_BACKENDS),
        "log_level": logging.getLevelName(logging.getLogger().getEffectiveLevel()),
        "package_record_sha256": _package_record_sha256(),
    }
    if profile_containment_active():
        # daemon 自己驗證成功的 Profile／policy bytes 身分（只含雜湊），供 launcher 判斷能否沿用。
        identity.update(loaded_authority_identity())
    return identity


def _approval_mode() -> str:
    """policy 的 `approval_mode` 列舉：無 Profile＝`none`；Profile 下 policy 讀不到＝`unavailable`。"""
    if not profile_containment_active():
        return "none"
    policy = policy_identity()
    return policy.approval_mode if policy is not None else "unavailable"


def _egress_audit_digest() -> str:
    """外送 audit 的 payload 完整性欄模式；設定值無效時公告 `unrecognized`。"""
    from terminux_engine.llm import egress_audit

    try:
        return egress_audit.digest_mode()
    except egress_audit.EgressAuditUnavailable:
        return "unrecognized"


def _audit_key_id() -> str | None:
    from terminux_engine.llm import egress_audit

    try:
        return egress_audit.key_id()
    except egress_audit.EgressAuditUnavailable:
        return None


def _sweep_egress_audit() -> None:
    """Profile 模式啟動時依保存天數清理外送 audit 日檔；目錄不可用只記錄，由最終 Gate 拒絕外送。"""
    if not profile_containment_active():
        return
    from terminux_engine.llm import egress_audit

    try:
        removed = egress_audit.sweep()
    except egress_audit.EgressAuditUnavailable as exc:
        logger.warning("TERM_EGRESS_AUDIT_UNAVAILABLE %s", exc)
        return
    logger.info("TERM_EGRESS_AUDIT_SWEEP removed=%d retention_days=%d key_id=%s",
                len(removed), egress_audit.RETENTION_DAYS, _audit_key_id())


class _LineageThread(threading.Thread):
    """將 listener 的就緒及失敗狀態帶回主事件迴圈。"""

    def __init__(self, stop_event: threading.Event,
                 session_registry: CanonicalSessionContextRegistry, daemon_euid: int) -> None:
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None
        super().__init__(target=self._serve, args=(stop_event, session_registry, daemon_euid),
                         daemon=True, name="jsonrpc-lineage-server")

    def _serve(self, stop_event: threading.Event,
               session_registry: CanonicalSessionContextRegistry, daemon_euid: int) -> None:
        try:
            serve_forever(_LINEAGE_SOCKET_PATH, _stop_event=stop_event,
                          _ready_event=self.ready, session_registry=session_registry,
                          daemon_euid=daemon_euid)
        except BaseException as exc:
            self.error = exc
        finally:
            if not self.ready.is_set() and self.error is None:
                self.error = RuntimeError("TERM_LINEAGE_START_FAILED")
            self.finished.set()


async def _watch_lineage(thread: _LineageThread) -> None:
    while not thread.finished.is_set():
        await asyncio.sleep(0.02)
    if thread.error is not None:
        raise thread.error
    raise RuntimeError("TERM_LINEAGE_LISTENER_STOPPED")


async def _run_bridge(bridge: TcpBridge, owner: DaemonOwner, ipc_server: IPCServer,
                      lineage: _LineageThread, server_task: asyncio.Task[None],
                      shutdown: asyncio.Event) -> None:
    try:
        while not shutdown.is_set():
            if lineage.finished.is_set():
                raise RuntimeError("TERM_LINEAGE_START_FAILED") from lineage.error
            if server_task.done():
                await server_task
                raise RuntimeError("TERM_IPC_START_FAILED")
            if lineage.ready.is_set() and ipc_server._server is not None:
                await bridge.start()
                assert bridge.core_port is not None and bridge.lineage_port is not None
                owner.metadata.update(lineage_server._wrapper_setup_result)
                owner.metadata.update(_runtime_identity())
                owner.mark_ready(bridge.core_port, bridge.lineage_port, bridge.token)
                await shutdown.wait()
                return
            await asyncio.sleep(0.02)
    finally:
        await bridge.close()


async def _run_unix_only(owner: DaemonOwner, ipc_server: IPCServer,
                         lineage: _LineageThread, server_task: asyncio.Task[None],
                         shutdown: asyncio.Event) -> None:
    """兩個本機 listener 就緒後公告服務，不建立遠端 bridge。"""
    while not shutdown.is_set():
        if lineage.finished.is_set():
            raise RuntimeError("TERM_LINEAGE_START_FAILED") from lineage.error
        if server_task.done():
            await server_task
            raise RuntimeError("TERM_IPC_START_FAILED")
        if lineage.ready.is_set() and ipc_server._server is not None:
            owner.metadata.update(lineage_server._wrapper_setup_result)
            owner.metadata.update(_runtime_identity())
            owner.mark_unix_ready()
            await shutdown.wait()
            return
        await asyncio.sleep(0.02)


def _unix_only_ipc_enabled() -> bool:
    value = os.environ.get("TERMINUX_UNIX_ONLY_IPC", "0")
    if value not in {"0", "1"}:
        raise ValueError("TERM_UNIX_ONLY_IPC_INVALID")
    return value == "1"


class _DaemonRuntime:
    """保存部分啟動時已建立的資源，供統一收束。"""

    def __init__(self) -> None:
        self.shutdown = asyncio.Event()
        self.lineage_stop = threading.Event()
        self.registry = CanonicalSessionContextRegistry()
        self.lineage: _LineageThread | None = None
        self.repository: SQLiteRepository | None = None
        self.ipc: IPCServer | None = None
        self.bridge: TcpBridge | None = None
        self.bridge_task: asyncio.Task[None] | None = None
        self.tasks: list[asyncio.Task[None]] = []
        self.log_handler: logging.Handler | None = None
        self.console_handlers: list[logging.Handler] = []

    def _close_log(self) -> None:
        """在釋放所有權前停止寫檔，避免與下一個 instance 共用檔案。"""
        if self.log_handler is not None:
            logging.getLogger().handlers = self.console_handlers
            self.log_handler.close()
            self.log_handler = None


async def _stop_owned(owner: DaemonOwner, state: _DaemonRuntime) -> None:
    state.shutdown.set()
    try:
        # 先停止 bridge，包含尚在等待驗證行的連線，再關閉 Unix listener。
        if state.bridge_task is not None:
            state.bridge_task.cancel()
            await asyncio.gather(state.bridge_task, return_exceptions=True)
        if state.bridge is not None:
            await state.bridge.close()
        if state.ipc is not None:
            await state.ipc.stop()
    finally:
        state.lineage_stop.set()
        if state.lineage is not None and state.lineage.ident is not None:
            await asyncio.to_thread(state.lineage.join, 8.0)
        for task in state.tasks:
            if not task.done():
                task.cancel()
        if state.tasks:
            await asyncio.gather(*state.tasks, return_exceptions=True)
        state.registry.clear()
        repository_closed = True
        if state.repository is not None:
            try:
                state.repository.close()
            except Exception:
                repository_closed = False
                logger.error("TERM_DAEMON_OWNER_LOCK_RETAINED reason=repository_close_failed")
        if state.lineage is not None and state.lineage.is_alive():
            logger.error("TERM_DAEMON_OWNER_LOCK_RETAINED reason=lineage_join_timeout")
        elif (lineage_server._event_store_opened.is_set()
              and not lineage_server._event_store_closed.is_set()):
            logger.error("TERM_DAEMON_OWNER_LOCK_RETAINED reason=event_store_close_unconfirmed")
        elif repository_closed:
            state._close_log()
            owner.release()


def _build_lineage_thread(
    stop_event: threading.Event,
    session_registry: CanonicalSessionContextRegistry,
    daemon_euid: int,
) -> _LineageThread:
    """組裝 JSON-RPC lineage server daemon thread（未啟動）。

    stop_event 由 main 的 signal handler 設定，讓 thread 先正常關 listener
    並安全清理自己建立的 socket，不能依賴 daemon thread 被 process 直接終止。

    `session_registry` 與注入 `IPCServer` 的是同一個 instance
    （見 `run()`），讓兩個 listener 共用唯一的 canonical session authority
。

    `daemon_euid` 同樣是 `run()` 內用
    `peer_identity.capture_daemon_euid()` 只算一次的值，與注入 `IPCServer`
    的是同一個數字，兩個 listener 執行同一個 per-member daemon guard
    （daemon 只接受 peer_uid == daemon startup EUID 的 client）。
    """
    return _LineageThread(stop_event, session_registry, daemon_euid)


def _maybe_subscribe_layer1(event_bus: EventBus, handler) -> bool:
    """依 TERMINUX_DISABLE_PLUGINS 決定是否 subscribe layer1 pipeline。

    truthy（1/true/yes）→ 不 subscribe，plugin pipeline 永不被觸發，回傳 False。
    未設 / 空字串 → 照常 subscribe（行為與 gate 前完全一致），回傳 True。
    """
    if os.environ.get("TERMINUX_DISABLE_PLUGINS", "").strip().lower() in ("1", "true", "yes"):
        logger.info("TERM_LAYER1_DISABLED")
        return False
    event_bus.subscribe("shell_event", handler, "Layer1Pipeline")
    logger.info("TERM_LAYER1_READY")
    return True


async def run() -> None:
    _setup_logging()
    unix_only_ipc = _unix_only_ipc_enabled()
    legacy_socket = home_for_uid(os.geteuid()) / ".terminux" / "terminux.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(legacy_socket))
    except OSError:
        pass
    else:
        logger.error("TERM_LEGACY_DAEMON_RUNNING path=%s；請先停止舊版 daemon。", legacy_socket)
        raise SystemExit(4)
    try:
        owner = DaemonOwner.acquire()
    except DaemonOwnerExists as exc:
        logger.error("%s", exc)
        raise SystemExit(4) from exc
    state = _DaemonRuntime()
    lineage_server._event_store_opened.clear()
    lineage_server._event_store_closed.clear()
    try:
        state.log_handler = _runtime_log_handler("daemon.log")
        root_logger = logging.getLogger()
        state.console_handlers = root_logger.handlers[:]
        root_logger.handlers = [state.log_handler]
        await _run_owned(owner, state, unix_only_ipc=unix_only_ipc)
    except Exception as exc:
        logger.error("TERM_SERVICE_START_FAILED %s: %s", type(exc).__name__, exc)
        raise SystemExit(3) from exc
    finally:
        try:
            await _stop_owned(owner, state)
        finally:
            state._close_log()
    logger.info("TERM_SERVICE_STOPPED")


async def _run_owned(owner: DaemonOwner, state: _DaemonRuntime, *, unix_only_ipc: bool) -> None:
    global _LINEAGE_SOCKET_PATH
    logger.info("TERM_SERVICE_STARTING")
    # 啟動身分只含布林、列舉值與套件雜湊，供部署檢查與 daemon.log 對照。
    logger.info("TERM_RUNTIME_IDENTITY %s",
                " ".join(f"{key}={value}" for key, value in sorted(_runtime_identity().items())))
    _sweep_egress_audit()

    # agent socket 路徑只由 process UID 決定，任何一項檢查不過就
    # 拒絕啟動並回報精確原因（uid 與預期路徑都在訊息內）。legacy 的
    # `/tmp/terminux.sock` 與 `/run/user/<uid>/...` 只警告，不刪除、不接管。
    try:
        agent_socket = daemon_agent_socket()
        _LINEAGE_SOCKET_PATH = str(daemon_lineage_socket().path)
    except AgentSocketError as exc:
        logger.error("TERM_AGENT_SOCKET_UNAVAILABLE %s", exc)
        raise SystemExit(2) from exc
    logger.info("TERM_AGENT_SOCKET_RESOLVED path=%s source=%s uid=%s",
                agent_socket.path, agent_socket.source, agent_socket.uid)
    legacy_warning = legacy_migration_warning()
    if legacy_warning:
        logger.warning(legacy_warning)

    loop = asyncio.get_running_loop()
    shutdown_event = state.shutdown
    lineage_stop_event = state.lineage_stop

    # lineage endpoint 的 parent 必須在 thread bind 前存在；否則 fail-closed
    # socket lifecycle 會正確拒絕不存在的 endpoint，卻讓首次啟動產生競態。
    Path(_LINEAGE_SOCKET_PATH).parent.mkdir(parents=True, exist_ok=True)
    owner.clear_stale_socket(agent_socket.path)
    owner.clear_stale_socket(_LINEAGE_SOCKET_PATH)

    # process 內唯一一份 canonical session registry，注入
    # IPCServer 與 lineage JSON-RPC server 兩個獨立 listener。
    session_registry = state.registry

    # daemon 啟動當下的 effective UID，只算一次，注入兩個
    # listener 作為 per-member daemon guard 的錨點（daemon 只接受
    # peer_uid == daemon startup EUID 的 client；不同 UID 必須在 session/request/workspace 操作前
    # fail-closed）。
    daemon_euid = capture_daemon_euid()

    # ── JSON-RPC lineage server（daemon thread，在 IPCServer 之前啟動）──────────
    # serve_forever 是 blocking 函式（socket.accept），以 daemon thread 執行。
    _lineage_thread = _build_lineage_thread(lineage_stop_event, session_registry, daemon_euid)
    state.lineage = _lineage_thread
    _lineage_thread.start()
    logger.info("TERM_LINEAGE_THREAD_STARTED")

    # ── Store ──────────────────────────────────────────────────────────────────
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.info("TERM_STORE_READY")
    repository = SQLiteRepository(str(_DB_PATH))
    state.repository = repository

    # ── Core managers ──────────────────────────────────────────────────────────
    ecc_manager        = EccManager(repository)
    event_recorder     = EventRecorder(repository)
    provenance_manager = ProvenanceEngine(repository)

    # ── Plugin manager ─────────────────────────────────────────────────────────
    logger.info("TERM_PLUGINS_READY")
    plugin_manager = PluginManager(str(_PLUGINS_DIR))

    # ── Layer 1 ────────────────────────────────────────────────────────────────
    logger.info("TERM_EXTRACTOR_READY")
    classifier = CommandClassifier(plugin_manager)
    extractor  = DataExtractor(str(_EXTRACT_RULES))
    evaluator  = TriggerEvaluator(plugin_manager)

    # ── LLM client ─────────────────────────────────────────────────────────────
    logger.info("TERM_LLM_READY")
    llm_client = LlmClient(str(_LLM_CONFIG_PATH))

    # ── EventBus（先建立，供 IPCServer layer1_callback 使用）──────────────────
    event_bus = EventBus()

    # ── IPC server ─────────────────────────────────────────────────────────────
    ipc_server = IPCServer(
        socket_path=str(agent_socket.path),
        event_recorder=event_recorder,
        ecc_manager=ecc_manager,
        provenance_manager=provenance_manager,
        layer1_callback=lambda e: asyncio.create_task(
            event_bus.publish("shell_event", e)
        ),
        session_registry=session_registry,
        daemon_euid=daemon_euid,
    )
    state.ipc = ipc_server

    # ── Layer 3（依賴 ipc_server）─────────────────────────────────────────────
    notifier = Notifier(ipc_server)
    executor = Executor(ipc_server, plugin_manager)

    # ── Layer 2 ────────────────────────────────────────────────────────────────
    ctx_builder   = ContextBuilder(ecc_manager, llm_client, plugin_manager)
    action_router = ActionRouter(llm_client, plugin_manager, notifier, executor, ecc_manager)

    # ── Layer 1 Pipeline handler ───────────────────────────────────────────────
    async def layer1_pipeline(event: ShellEvent) -> None:
        try:
            classifier.classify(event)
            extractor.extract(event)
            # extractor 已填入 extracted_data 與 block，更新 DB
            # 改用 asyncio.to_thread：query_events 也經由 to_thread，避免 main
            # thread 與 thread pool 同時使用同一個 conn
            await asyncio.to_thread(
                event_recorder.update_extracted_data,
                event.event_id, event.extracted_data, event.block
            )
            triggered = evaluator.evaluate(event)
            if triggered:
                ready_stream = ctx_builder.process(triggered)
                await action_router.process(ready_stream)
        except Exception:
            logger.error("TERM_LAYER1_PIPELINE_FAILED")

    # 開發隔離 gate：TERMINUX_DISABLE_PLUGINS truthy 時不 subscribe，plugin
    # post-exec pipeline 永不被觸發（5 個元件照常建構、只是不掛 event）。
    _maybe_subscribe_layer1(event_bus, layer1_pipeline)

    # ── Signal handling ────────────────────────────────────────────────────────
    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("TERM_SERVICE_SHUTDOWN_SIGNAL")
        stop_orchestration_heartbeat_scanner()
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # ── Start ──────────────────────────────────────────────────────────────────
    logger.info("TERM_IPC_STARTING")
    server_task   = asyncio.create_task(ipc_server.start())
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    lineage_task = asyncio.create_task(_watch_lineage(_lineage_thread))
    if unix_only_ipc:
        bridge_task = asyncio.create_task(_run_unix_only(
            owner, ipc_server, _lineage_thread, server_task, shutdown_event))
    else:
        bridge = TcpBridge(str(agent_socket.path), _LINEAGE_SOCKET_PATH)
        state.bridge = bridge
        bridge_task = asyncio.create_task(_run_bridge(
            bridge, owner, ipc_server, _lineage_thread, server_task, shutdown_event))
    state.bridge_task = bridge_task
    state.tasks = [server_task, shutdown_task, lineage_task, bridge_task]

    await asyncio.wait(
        {server_task, shutdown_task, lineage_task, bridge_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in (server_task, lineage_task, bridge_task):
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                raise error
    if not shutdown_event.is_set():
        raise RuntimeError("TERM_SERVICE_LISTENER_STOPPED")


def main() -> None:
    from terminux_engine.version_cli import _check_help_arg, check_version_arg
    _check_help_arg(
        "usage: terminux-daemon [-h] [--help] [--version]\n\n"
        "啟動 Terminux daemon；LLM、socket 與資料位置由環境變數設定。\n"
        "  -h, --help  顯示用法後退出\n"
        "  --version   顯示版本後退出"
    )
    check_version_arg()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
