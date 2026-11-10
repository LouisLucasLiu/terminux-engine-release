# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import getpass
import errno
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from terminux_engine.ipc.agent_socket import local_nodename
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from terminux_engine.ecc.accumulator import ECCAccumulator
from terminux_engine.ecc.ai_companion import AICompanion, ConversationBuffer
from terminux_engine.ecc.block_name_extractor import (
    extract_block_name_from_lineage,
    extract_block_name_from_runfile,
)
from terminux_engine.ecc.gate_evaluator import evaluate_gates
from terminux_engine.ecc.playbook import Playbook, get_quality_gates, load_playbook
from terminux_engine.ecc.profile import build_enriched_profile
from terminux_engine.ecc.team import build_team_snapshot
from terminux_engine.ecc.store import (
    ECCStore,
    load_continuity_from_events,
    load_recent_sessions_from_events,
)
from terminux_engine.store.event_store import EventStore, SessionEvent
from terminux_engine.core.i18n import tr as _tr
from terminux_engine.core.preferences import (
    language_native_name,
    load_preferred_language,
    set_preferred_language,
)
from terminux_engine.dqe.viewer import service as dqe_viewer_service
from terminux_engine.dqe.viewer.service import DqeViewerInvalidRefError, DqeViewerServiceError
from terminux_engine.dqe.viewer.retention import (
    InvalidRetentionDaysError,
    RetentionPolicyError,
)
from terminux_engine.lineage.postexec_api import handle_post_exec
from terminux_engine.lineage.preexec_api import (
    PreExecDecision,
    handle_pre_exec,
)
from terminux_engine.ipc.peer_identity import (
    is_peer_authorized_for_daemon,
    resolve_peer_uid_from_socket,
)
from terminux_engine.ipc.request_context import (
    CanonicalRequestContext,
    get_current_request_context,
    reset_current_request_context,
    set_current_request_context,
    try_get_current_request_context,
)
from terminux_engine.ipc.session_registry import (
    CanonicalSessionContextRegistry,
    InvalidSessionContextError,
)
from terminux_engine.ipc.socket_lifecycle import (
    BoundUnixSocket,
    bind_owned_unix_socket,
    unlink_owned_unix_socket,
)
from terminux_engine.llm import agent_audit
from terminux_engine.llm.tool_safety import (
    ProposalExecutionBinding,
    ToolAuthorizationPolicy,
    canonicalize_arguments,
    reset_current_tool_authorization_policy,
    set_current_tool_authorization_policy,
)
from terminux_engine.llm.llama_cpp_provider import (
    LlamaCppCompletionProvider,
    LlamaCppProviderConfig,
)
from terminux_engine.memory.compaction import ConversationCompactor
from terminux_engine.memory.digest_loader import load_recent_work_history
from terminux_engine.memory.dreaming import (
    ACTIVITY_GAP_THRESHOLD_SEC,
    Dreamer,
    run_daily_cron,
    split_into_periods,
)
from terminux_engine.memory.embedder import Embedder, NoOpEmbedder
from terminux_engine.memory.flush import MemoryFlusher
from terminux_engine.memory.hybrid_search import HybridSearch
from terminux_engine.memory.search_index import SearchIndex
from terminux_engine.observability.log_schema import ObservabilityEvent
from terminux_engine.observability.preexec_log import append_observability_event
from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.action_registry import ActionRegistry
from terminux_engine.package_runtime.errors import (
    ActionRegistryError,
    DuplicatePackageActionIdError,
    DuplicatePackageIdError,
)
from terminux_engine.package_runtime.dispatcher import PackageDispatcher
from terminux_engine.package_runtime.registry import PackageRegistry, discover_and_register_packages
from terminux_engine.package_runtime.result import TimelineEvent
from terminux_engine.package_runtime.user_packages import (
    UserPackageActivation,
    UserPackageManager,
    builtin_package_ids,
    resolve_user_packages_root,
    user_packages_disabled,
)
from terminux_engine.tool_identity.catalog import (
    ORIGIN_USER,
    CatalogError,
    ToolCatalog,
    load_catalog,
)
from terminux_engine.tool_identity.user_catalog import build_user_tool_scope
from terminux_engine.tool_identity.host_validation import (
    HostValidationResult,
    ValidationDecision,
    validate_shell_post_exec_tool_identity,
)
from terminux_engine.execution_trace.trace_id import parse_trace_root, parse_trace_self
from terminux_engine.orchestration.dispatch_engine import DispatchEngine, EngineMode
from terminux_engine.orchestration.daemon_service import (
    OrchestrationApiError,
    OrchestrationService,
)
from terminux_engine.orchestration.heartbeat_scanner import HeartbeatScanner
from terminux_engine.orchestration.ledger import OrchestrationLedger
from terminux_engine.orchestration.recovery_scanner import RecoveryScanner
from terminux_engine.package_runtime.services import (
    LlmService,
    NotifyService,
    RuntimeServices,
    StoreService,
)
from terminux_engine.server.terminal_stream import StreamManager, TerminalStream
from terminux_engine.resource_paths import bundled_knowledge_path

_log = logging.getLogger(__name__)
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_BUILTIN_PACKAGES_DIR = Path(__file__).resolve().parents[1] / "packages"

# ─── module-level LLM provider singleton ────────────────────────────────────
# 第一次呼叫 pre_exec 時才初始化。
# 初始化失敗時設 _runtime_provider_failed = True，之後不再嘗試（fail-open）。

_runtime_provider: Any = None  # LlamaCppCompletionProvider | GeminiCompletionProvider | None
_runtime_provider_failed: bool = False

# ─── JSON-RPC 2.0 error codes ────────────────────────────────────────────────

_ERR_INVALID_REQUEST = -32600
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603
# session scope isolation：context-required method 缺少／無效 transport
# context 時的 public error code。wrong-peer 與 unknown/revoked session 共用
# 同一個 _ERR_INVALID_SESSION_CONTEXT，對外不可區分。
_ERR_SESSION_CONTEXT_REQUIRED = -32010
_ERR_INVALID_SESSION_CONTEXT = -32011
# session scope isolation 新增：peer UID != daemon startup EUID（per-member daemon
# guard）。這是 connection-level fail-closed，比 context-required method
# 檢查更早、範圍更廣——涵蓋這個 listener 上的每一筆 request，不只是
# orchestration.* / ecc.chat_query。
_ERR_PEER_UID_NOT_AUTHORIZED = -32012
# shell.post_exec host tool identity validation（見 tool_identity/
# host_validation.py）：derived identity 與 client claim 不一致、或 claim
# 無法驗證、或 request 內容 malformed 時的統一 public error code。三種
# DENY_* decision 皆對外回傳同一個 code——不對外洩漏是 mismatch／
# unverifiable／malformed 哪一種，細節只進 daemon audit log（見 reason_code）。
_ERR_TOOL_IDENTITY_REJECTED = -32020

_ORCHESTRATION_METHODS = frozenset({
    "orchestration.start",
    "orchestration.status",
    "orchestration.pause",
    "orchestration.resume",
    "orchestration.cancel",
    "orchestration.retry",
    "orchestration.abandon",
    "orchestration.list_runs",
})
_EVIDENCE_BUNDLE_METHOD = "orchestration.evidence_bundle"
_EVIDENCE_SET_METHOD = "orchestration.evidence_set"
# operator-only proposal control API。從不註冊成 Agent tool，provider
# 永遠看不到、也不能呼叫這四個 method。
_PROPOSAL_METHODS = frozenset({
    "agent.proposal.list", "agent.proposal.get",
    "agent.proposal.approve", "agent.proposal.reject",
    "agent.proposal.execute",
})
# operator-only trusted-result control API。與 _PROPOSAL_METHODS 同一
# 原則——從不註冊成 Agent tool，provider 永遠看不到、也不能呼叫。
_RESULT_METHODS = frozenset({
    "agent.result.list", "agent.result.get", "agent.result.use", "agent.result.clear",
})

# plan-only 的 typed IPC entry。Additive 獨立 method——
# 它**不是** Agent method 的一個 flag，`mode` 是 required 且經 enum 驗證，
# 缺失或未知一律拒絕，絕不 fallback 到 normal execution path。
_PLAN_ONLY_METHOD = "agent.plan_only"

# plan step proposal 的 typed IPC entry。Additive；approval 與 execution
# 仍走既有的 `/approve`／`/execute` 與 `agent.proposal.*`，approved package action 不另造 approval
# authority。
_PLAN_PROPOSE_METHOD = "agent.plan.propose_step"

# 問題→Global→Domain→不可執行草案。此組 method 不註冊為 Agent tool，
# 也不與既有可啟動的 flow task 共用入口。
_EAP1_METHODS = frozenset({
    "agent.eap1.create", "agent.eap1.get", "agent.eap1.cancel",
    "agent.eap1.evidence.list", "agent.eap1.evidence.select",
})

# FlowTask bridge：只把 server 讀回的 domain proposal draft 固定成既有 FlowTask DRAFT。
# 使用者核准與執行仍完全走 `agent.flow_task.start`／`/task-start`。
_EAP2_METHODS = frozenset({"agent.eap2.create_flow_task"})

# 持久化工程工作。所有控制 method 都經 canonical request context；
# provider 不會取得這些 operator-only method。
_EAP3_METHODS = frozenset({
    "agent.eap3.work.create", "agent.eap3.work.create_from_saved_proposal",
    "agent.eap3.work.authorization_preview",
    "agent.eap3.work.approve", "agent.eap3.work.continue",
    "agent.eap3.work.get", "agent.eap3.work.list", "agent.eap3.work.answer",
    "agent.eap3.work.pause", "agent.eap3.work.resume", "agent.eap3.work.cancel",
    "agent.eap3.work.takeover",
    "agent.eap3.work.proposal.submit", "agent.eap3.work.proposal.approve",
    "agent.eap3.work.effective_config",
})

# 固定兩步分析 task 的 typed IPC entry。四個 method 都經既有
# CanonicalRequestContext 邊界；caller 不能提供 member/project/session、
# workspace、DB path、grant 或 policy——那些全部由 host 決定。
# 它們**不**註冊成 ToolRegistry tool，provider 無法呼叫。
_ANALYSIS_TASK_METHODS = frozenset({
    "agent.analysis_task.create", "agent.analysis_task.start",
    "agent.analysis_task.get", "agent.analysis_task.cancel",
})

# 固定五步 flow task 的 typed IPC entry。與 analysis task 相同的
# context-required 邊界；caller 不能自帶 member/project/session、workspace、
# run_id、grant、policy、路徑、command、參數值或 budget。
_FLOW_TASK_METHODS = frozenset({
    "agent.flow_task.create", "agent.flow_task.start",
    "agent.flow_task.get", "agent.flow_task.cancel",
    # task-scoped profile 選用與清單、本 daemon 的 task 清單。
    "agent.flow_task.select_profile", "agent.flow_task.list_profiles",
    "agent.flow_task.list",
})

# context-required allowlist。最低固定覆蓋 8 個 direct
# orchestration.* method 與 production Agent／`@@` entrypoint（`ecc.chat_query`，
# 見 `_try_agent_chat_query` 的唯一呼叫點）。其餘既有 context-free lineage
# methods 維持現況，不藉此全面改造。
_CONTEXT_REQUIRED_METHODS = _ORCHESTRATION_METHODS | frozenset({
    "ecc.chat_query", _EVIDENCE_BUNDLE_METHOD, _EVIDENCE_SET_METHOD,
    _PLAN_ONLY_METHOD, _PLAN_PROPOSE_METHOD,
}) | _PROPOSAL_METHODS | _RESULT_METHODS | _ANALYSIS_TASK_METHODS | _FLOW_TASK_METHODS | _EAP1_METHODS | _EAP2_METHODS | _EAP3_METHODS

_SUPPORTED_METHODS = frozenset({
    "lineage.pre_exec", "lineage.post_exec", "ecc.chat_query",
    "shell.pre_exec", "shell.post_exec",
    "notify.poll",
    "package.invoke_action",
    "llm.ask",
    "rag.reload",
    "extensions.reload", "extensions.status",
    "terminal.register", "terminal.unregister",
    "preferences.set_language", "preferences.get_language",
    "dqe_viewer.health", "dqe_viewer.list_archived_groups",
    "dqe_viewer.list_temporary_groups", "dqe_viewer.load_group",
    "dqe_viewer.checkin_job", "dqe_viewer.get_retention",
    "dqe_viewer.set_retention", "dqe_viewer.load_attachment",
    "dqe_viewer.preview_purge", "dqe_viewer.purge_jobs",
}) | _ORCHESTRATION_METHODS | frozenset({_EVIDENCE_BUNDLE_METHOD, _EVIDENCE_SET_METHOD, _PLAN_ONLY_METHOD, _PLAN_PROPOSE_METHOD}) | _PROPOSAL_METHODS | _RESULT_METHODS | _ANALYSIS_TASK_METHODS | _FLOW_TASK_METHODS | _EAP1_METHODS | _EAP2_METHODS | _EAP3_METHODS

_package_registry_cache: PackageRegistry | None = None
_package_registry_lock = threading.Lock()
_package_dispatch_disabled_logged = False
_package_action_registry_cache: ActionRegistry | None = None
_tool_catalog_cache: ToolCatalog | None = None
_tool_catalog_lock = threading.Lock()
_tool_catalog_load_failed_logged = False
_package_action_registry_lock = threading.Lock()
_pending_package_actions: dict[str, dict[str, Any]] = {}
_pending_package_actions_lock = threading.Lock()
_PACKAGE_ACTION_TTL_SEC = 10 * 60

# ─── ANSI sanitization ──────────────────────────────────────────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def _sanitize_command(cmd: str) -> str:
    """移除 ANSI escape sequences，回傳乾淨的 command 字串。"""
    return _ANSI_RE.sub('', cmd).strip()


# ─── module-level ECC state ──────────────────────────────────────────────────
# serve_forever() 啟動時初始化，handle_jsonrpc_request 使用。
# AI companion 使用 deterministic fallback（lambda p: ""）
# _ecc_lock 保護 accumulator / companion / playbook 的並行存取

_ecc_accumulator: ECCAccumulator | None = None
_ecc_companion: AICompanion | None = None
_ecc_playbook: Playbook | None = None
_event_store: EventStore | None = None
_event_store_opened = threading.Event()
_event_store_closed = threading.Event()
#: `_init_ecc_inner` 啟動的 startup backfill thread。它會查詢 `_event_store`；
#: `_reset_ecc_state()` 關閉 EventStore 前必須先等它結束（見該函式）。
_startup_backfill_thread: threading.Thread | None = None
_startup_backfill_stop_event: threading.Event = threading.Event()
#: 統一關機時等待背景 workers 的總上限（秒）。
_TOTAL_SHUTDOWN_TIMEOUT_SEC: float = 6.0
_STARTUP_BACKFILL_JOIN_SEC: float = 5.0
_stream_manager: StreamManager | None = None
_hybrid_search: HybridSearch | None = None
_user_id: str = ""
_project_id: str | None = None
_dreamer: Dreamer | None = None
_jsonl_ingestor: "JsonlIngestor | None" = None
_log_sweeper: "LogSweeper | None" = None
#: proactive intelligence activation（2026-09-06）：daemon-owned proactive runner。
_proactive_runner: Any = None
_orchestration_service: OrchestrationService | None = None
#: 既有 flow 呼叫端（run input evidence、flow run evidence、package flow）的 receipt reader：
#: `run_id -> list[dict]`，只含 daemon 身分寫入的 payload。與 OrchestrationService 的帶身分
#: 列 reader 是兩個不同契約，由 `_build_receipt_readers` 一起建立。
_receipt_payload_reader: Any | None = None
_orchestration_heartbeat_scanner: HeartbeatScanner | None = None
# canonical session registry。由 main.py bootstrap 建立單一 instance
# 後，透過 serve_forever(session_registry=...) 注入，與 IPCServer 共用同一份
# canonical session authority（process 內只能有一份 registry；本 module
# 只透過 registry.resolve() 這個 public read-only API 讀取，不讀
# IPCServer._sessions，也不建立第二套 session model）。
_session_registry: CanonicalSessionContextRegistry | None = None
# per-member daemon guard 的錨點。由 main.py bootstrap 用
# peer_identity.capture_daemon_euid() 只算一次後，透過
# serve_forever(daemon_euid=...)／serve_once(daemon_euid=...) 注入，與
# IPCServer 使用同一個數字。None 時（獨立
# 測試、未設定）視為 guard 未啟用；production bootstrap 一律會注入。
_daemon_euid: int | None = None
_ecc_lock = threading.Lock()
_llm_lock = threading.Lock()  # 序列化 LLM 推理（llama-cpp-python 非 thread-safe）
_replaying: bool = False       # True: _init_ecc() / rebuild 進行中，抑制 nudge 生成

# ─── Agent state ────────────────────────────────────────
# AgentRunner + ToolRegistry + LLMRouter（for session lock）。Gemini backend 才會初始化；
# 失敗時保持 None，chat_query 自動 fallback 到 ai_companion single-shot 路徑。
_agent_runner: Any = None
_agent_router: Any = None
_agent_tools_registry: Any = None
# daemon-memory-only proposal state machine；重啟即全部失效，無持久化。
_proposal_registry: Any = None
#: daemon-memory-only，同 `_proposal_registry` 的生命週期（daemon
#: 重啟即失效，沒有任何持久化）。
_plan_registry: Any = None
_rag_index: Any = None
_rag_embedding: Any = None
_rag_rebuild_stop_event: threading.Event | None = None
# 此開關只抑制 daemon 自行排程的 shared RAG rebuild；既有 index 仍可載入並
# 供查詢使用。它讓受控 smoke 能保證閒置期間不會因 embedding 產生 provider
# 請求，而不是以吞掉 429 的方式掩蓋背景工作。
_ENV_DISABLE_RAG_BACKGROUND_REBUILD = "TERMINUX_DISABLE_RAG_BACKGROUND_REBUILD"
# Package prompt phase 專用的 LLMRouter 注入點。jsonrpc_server 採 module-level
# state，不為此將 daemon 重構為 class。
_package_llm_router: Any = None
# domain proposal 的本地取消旗標跨 request 存活；它不宣稱可以取消遠端 provider。
_eap1_cancelled: set[str] = set()

# ─── Async Agent pipeline ──────────────────────────────
# CommandBuffer 累積 pre-exec command，flush 時組 prompt → AgentTaskQueue.submit。
# Agent 與 package 通知保留於帶序號緩衝，各 GUI 透過 notify.poll 獨立讀取。
_command_buffer: Any = None
_agent_task_queue: Any = None
_pending_async_notifications: deque[tuple[int, dict]] = deque(maxlen=500)
_pending_async_seq = 0
_pending_async_lock = threading.Lock()
_package_dispatch_inflight: dict[str, list[threading.Event]] = {}
_package_dispatch_inflight_lock = threading.Lock()
_package_dispatch_threads: set[threading.Thread] = set()
_package_dispatch_threads_lock = threading.Lock()
_active_request_threads: set[threading.Thread] = set()
_active_request_lock = threading.Lock()
_server_sock: socket.socket | None = None
_rag_rebuild_thread: threading.Thread | None = None
_PACKAGE_DISPATCH_DRAIN_WAIT_SEC = 10.0

# 已有 canonical context 的開檔請求依 session 隔離；None 保留無 context 的相容行為。
_pending_open_paths: dict[str | None, list[str]] = {}
_pending_open_paths_lock = threading.Lock()


def _append_pending_open_path(path: str) -> None:
    """open_report tool 的 on_open callback：記下要開啟的報表路徑。"""
    if not path:
        return
    context = try_get_current_request_context()
    session_id = context.session_id if context is not None else None
    with _pending_open_paths_lock:
        _pending_open_paths.setdefault(session_id, []).append(path)


def _drain_pending_open_path() -> str | None:
    """只取本 session 的路徑；無 session 的舊來源維持先取先得。"""
    context = try_get_current_request_context()
    session_id = context.session_id if context is not None else None
    with _pending_open_paths_lock:
        paths = _pending_open_paths.pop(session_id, [])
        if session_id is not None:
            paths = _pending_open_paths.pop(None, []) + paths
        return paths[-1] if paths else None

# Dreaming Agent state
_deep_analysis_runner: Any = None
_deep_analysis_summary: str | None = None

# cache team_snapshot at module level（之前是 _init_ecc_inner local）
_team_snapshot: Any = None

# B 方案 hand-off cache：shell_hook 跑完 lineage check 拿到 warn/block 後，把
# 結果存進這個 dict，供緊接著進來的 C++ qt_terminal_typed pre_exec 拿回。沒有
# 這條 hand-off 的話，C++ 拿 SKIPPED_NON_AUTHORITATIVE 顯示不出 NotificationBar。
# Key = cwd, Value = (timestamp, decision_dict)。簡單時間窗 dedup（5s）。
_RECENT_LINEAGE_WARN_TTL_SEC = 5.0
_recent_lineage_warns: dict[str, tuple[float, dict]] = {}
_recent_lineage_warns_lock = threading.Lock()

# Gap detection 閾值從 dreaming.ACTIVITY_GAP_THRESHOLD_SEC import（見檔頭）


class _RouterEmbedderAdapter(Embedder):
    """把 LLMRouter.embed() 包成 Embedder interface。"""

    def __init__(self, router: Any) -> None:
        self._router = router

    def embed(self, text: str) -> list[float] | None:
        return self._router.embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        return self._router.embed_batch(texts)


def _get_stream(terminal_id: str = "default") -> TerminalStream | None:
    """取得指定 terminal 的 stream，不存在則自動 register。
    StreamManager 未初始化時回傳 None。
    """
    if _stream_manager is None:
        return None
    from terminux_engine.llm.task_scope import load_profile
    if load_profile() is not None:
        context = try_get_current_request_context()
        if context is None:
            return None
        terminal_id = f"{context.session_id}:{terminal_id}"
    stream = _stream_manager.get_stream(terminal_id)
    if stream is None:
        stream = _stream_manager.register_terminal(terminal_id)
    return stream


_MAX_TRACE_FIELD_LEN = 128


def _sanitize_trace_field(value: Any, *, allow_empty: bool = False) -> str | None:
    """Observability carrier v1：驗證 client-declared trace 欄位（純
    metadata，不影響 dispatch）。型別錯誤／過長／格式不符一律回傳 `None`
    （丟棄該欄位，不拒絕整個 request）。格式驗證重用
    `execution_trace/trace_id.py::parse_trace_root()`／`parse_trace_self()`，
    `trace_self`／`trace_parent` 共用同一種 `inv_{epoch_ns}_{pid}` 格式
    （`trace_parent` 允許空字串代表根層級，用 `allow_empty=True`）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if len(value) > _MAX_TRACE_FIELD_LEN:
        return None
    if value == "":
        return value if allow_empty else None
    try:
        parse_trace_root(value)
        return value
    except ValueError:
        pass
    try:
        parse_trace_self(value)
        return value
    except ValueError:
        return None


def _emit_event(
    event_type: str,
    payload: dict,
    terminal_id: str = "default",
    *,
    trace_root: str | None = None,
    trace_self: str | None = None,
    trace_parent: str | None = None,
) -> str | None:
    """Server 層 emit helper。若 _event_store 為 None 或 accumulator 未初始化，靜默跳過。
    _replaying 為 True 時也靜默跳過（避免重播期間產生重複事件）。

    Observability carrier v1：新增 optional `trace_root`/`trace_self`/
    `trace_parent`（client-declared metadata，純 correlation 用途）。成功寫入時
    回傳新產生的 `event_id`（供呼叫端作為後續 audit 序列的 `origin_event_id`），
    跳過／失敗時回傳 `None`。
    """
    if _event_store is None or _ecc_accumulator is None or _replaying:
        return None
    try:
        from terminux_engine.store.event_store import SessionEvent
        # 優先從指定 stream 取 session_id / block_name
        _stream = _get_stream(terminal_id) if _stream_manager is not None else None
        _acc = _stream.accumulator if _stream is not None else _ecc_accumulator
        _event_id = uuid4().hex
        _event_store.emit(SessionEvent(
            event_id=_event_id,
            session_id=_acc.session_id if _acc is not None else "",
            terminal_id=terminal_id,
            user_id=_user_id,
            project_id=_project_id,
            timestamp=time.time(),
            event_type=event_type,
            payload=payload,
            block_name=_acc.current_block_name if _acc is not None else None,
            trace_root=trace_root or "",
            trace_self=trace_self or "",
            trace_parent=trace_parent or "",
        ))
        return _event_id
    except Exception as exc:
        _log.warning("EventStore emit failed (%s): %s", event_type, exc)
        return None


def _resolve_setup_script() -> Path:
    """環境變數優先；預設使用 wheel 內的 wrapper 安裝腳本。"""
    override = os.environ.get("TERMINUX_SETUP_SCRIPT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "resources" / "scripts" / "terminux-setup-wrappers"


_wrapper_setup_result: dict[str, str] = {}


def _run_auto_setup() -> dict[str, str]:
    """Daemon startup 時自動安裝 wrappers + hooks。冪等，失敗不阻止啟動。"""
    if os.environ.get("TERMINUX_AUTO_SETUP_WRAPPERS", "1") == "0":
        _log.info("TERM_SETUP_DISABLED")
        return {"wrapper_setup": "disabled"}
    script = _resolve_setup_script()
    if not script.exists():
        _log.warning("TERM_SETUP_SCRIPT_MISSING")
        return {"wrapper_setup": "failed", "wrapper_setup_error": "TERM_SETUP_SCRIPT_MISSING"}

    try:
        command = ([str(script)] if os.environ.get("TERMINUX_SETUP_SCRIPT")
                   else ["/bin/bash", str(script)])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**os.environ, "TERMINUX_VENV_PYTHON": sys.executable},
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.warning("TERM_SETUP_TIMEOUT")
        return {"wrapper_setup": "failed", "wrapper_setup_error": "TERM_SETUP_TIMEOUT (10s)"}
    except OSError:
        _log.warning("TERM_SETUP_EXEC_FAILED")
        return {"wrapper_setup": "failed", "wrapper_setup_error": "TERM_SETUP_EXEC_FAILED"}

    if result.returncode == 0:
        _log.info("TERM_SETUP_COMPLETE")
        return {"wrapper_setup": "ok"}
    else:
        _log.warning("TERM_SETUP_FAILED code=%d", result.returncode)
        return {"wrapper_setup": "failed",
                "wrapper_setup_error": f"TERM_SETUP_FAILED code={result.returncode}"}


#: STA playbook 的預設位置（字面 `~` 形式；顯示用，載入時才展開）。
_DEFAULT_PLAYBOOK_DISPLAY_PATH = "~/.terminux/ecc/sta_signoff_playbook.yaml"


def _playbook_unavailable_message(source: str, *, missing: bool) -> str:
    """playbook 無法載入時的 ERROR 文案。不含任何展開後的路徑或環境變數值。"""
    impact = "ECC 功能（stage 追蹤、quality gate、nudge）已停用；EventStore 與 package dispatch 不受影響。"
    if source == "env":
        return ("TERMINUX_PLAYBOOK_PATH 指定的 playbook 無法載入。" + impact
                + "補救：修正 TERMINUX_PLAYBOOK_PATH 指向的檔案，或取消設定並部署到 "
                + f"{_DEFAULT_PLAYBOOK_DISPLAY_PATH}，然後重啟 daemon。")
    if source == "argument":
        return ("指定的 playbook 無法載入。" + impact
                + "補救：修正呼叫端傳入的 playbook 路徑，然後重啟 daemon。")
    reason = "找不到 STA playbook" if missing else "STA playbook 無法載入"
    return (f"{reason}（預設位置 {_DEFAULT_PLAYBOOK_DISPLAY_PATH}）。" + impact
            + "補救：部署到上述位置，或以 TERMINUX_PLAYBOOK_PATH 指定，然後重啟 daemon。")


def _init_ecc(
    playbook_path: str | None = None,
    ecc_store_path: str | None = None,
    event_store_path: str | None = None,
    *,
    parent_daemon_socket: str | None = None,
) -> None:
    """初始化 ECC pipeline state（在 serve_forever 中呼叫）。"""
    global _ecc_accumulator, _ecc_companion, _ecc_playbook, _event_store, _user_id, _project_id, _replaying, _stream_manager, _hybrid_search

    _replaying = True
    try:
        _init_ecc_inner(
            playbook_path,
            ecc_store_path,
            event_store_path,
            parent_daemon_socket=parent_daemon_socket,
        )
    finally:
        _replaying = False


def _init_ecc_inner(
    playbook_path: str | None = None,
    ecc_store_path: str | None = None,
    event_store_path: str | None = None,
    *,
    parent_daemon_socket: str | None = None,
) -> None:
    global _ecc_accumulator, _ecc_companion, _ecc_playbook, _event_store, _user_id, _project_id, _stream_manager, _hybrid_search
    global _agent_runner, _agent_router, _agent_tools_registry, _package_llm_router, _proposal_registry
    global _rag_index, _rag_embedding, _rag_rebuild_stop_event
    global _command_buffer, _agent_task_queue
    global _deep_analysis_runner, _deep_analysis_summary
    global _team_snapshot, _orchestration_service, _orchestration_heartbeat_scanner
    global _receipt_payload_reader
    global _startup_backfill_thread

    # Wrapper auto-setup（冪等，失敗不阻止 daemon 啟動）
    global _wrapper_setup_result
    _wrapper_setup_result = _run_auto_setup()

    # playbook 來源：default（預設位置）／env（TERMINUX_PLAYBOOK_PATH）／argument
    # （呼叫端明確傳入）。只用來挑選不含展開路徑的 ERROR 文案。
    if playbook_path is not None:
        playbook_source = "argument"
    elif "TERMINUX_PLAYBOOK_PATH" in os.environ:
        playbook_source = "env"
        playbook_path = os.environ["TERMINUX_PLAYBOOK_PATH"]
    else:
        playbook_source = "default"
        playbook_path = os.path.expanduser(_DEFAULT_PLAYBOOK_DISPLAY_PATH)

    if ecc_store_path is None:
        ecc_store_path = os.environ.get(
            "TERMINUX_ECC_STORE_PATH",
            os.path.expanduser("~/.terminux/ecc/ecc_store.jsonl"),
        )

    # playbook 只供 ECC（stage 追蹤、quality gate、nudge）使用。載入失敗
    # 時只停用 ECC；EventStore、package dispatch 與其餘服務照常初始化——以前
    # 它和下方整段組裝共用同一個 try，缺 playbook 會連 EventStore 一起跳過，
    # 所有 after package 都因 `in_daemon mode requires real EventStore` 失敗。
    #
    # 預設 log 等級（ERROR）的輸出不得帶出展開後的路徑。ERROR 行只放
    # 字面 `~` 形式的預設位置或環境變數名稱（不含其值），也不含例外訊息
    # （它本身就帶路徑）；完整路徑與例外放在 INFO 的 DETAIL。
    try:
        _ecc_playbook = load_playbook(playbook_path)
    except Exception as playbook_exc:
        _ecc_playbook = None
        _log.error("TERM_ECC_PLAYBOOK_UNAVAILABLE：%s", _playbook_unavailable_message(
            playbook_source, missing=isinstance(playbook_exc, FileNotFoundError)))
        _log.info("TERM_ECC_PLAYBOOK_UNAVAILABLE_DETAIL source=%s path=%s error=%s",
                  playbook_source, playbook_path, playbook_exc)

    try:
        # ── Archive playbook on startup (best-effort, never blocks)
        if _ecc_playbook is not None:
            try:
                from terminux_engine.ecc.playbook_archive import archive_playbook_if_absent
                archive_root = Path.home() / ".terminux" / "ecc" / "archive"
                archive_playbook_if_absent(
                    playbook_id=_ecc_playbook.id,
                    version=_ecc_playbook.version,
                    source_yaml_path=Path(playbook_path),
                    archive_root=archive_root,
                )
            except Exception:
                _log.error("TERM_ARCHIVE_HOOK_FAILED")

        # ── Search 基礎設施（SearchIndex + Embedder + HybridSearch）──────
        # 先建 search infra，再建 EventStore（讓 event hook 從一開始就生效）
        _search_index: SearchIndex | None = None
        _embedder: Embedder = NoOpEmbedder()
        _llm_router_inst = None
        _compactor: ConversationCompactor | None = None
        _flusher: MemoryFlusher | None = None

        try:
            from terminux_engine.llm.llm_router import LLMRouter
            from terminux_engine.llm.router_config import load_router_config

            from terminux_engine.llm.task_scope import (
                contain_router_config, profile_containment_active,
            )

            _router_config = contain_router_config(load_router_config())
            if profile_containment_active():
                _log.info("TERM_PROFILE_CONTAINMENT disabled=remote_router_providers")
            _llm_router_inst = LLMRouter(_router_config)
            _embedder = _RouterEmbedderAdapter(_llm_router_inst)
            _log.info("LLMRouter initialized (backend config loaded)")
        except Exception as lr_exc:
            _log.warning("LLMRouter initialization failed (non-fatal): %s", lr_exc)

        _package_llm_router = _llm_router_inst

        try:
            terminux_dir = os.path.expanduser("~/.terminux")
            search_index_dir = os.path.join(terminux_dir, "memory")
            os.makedirs(search_index_dir, exist_ok=True)
            search_index_path = os.path.join(search_index_dir, "search_index.db")
            _search_index = SearchIndex(search_index_path)
            _hybrid_search = HybridSearch(_search_index, _embedder)
            _log.info("TERM_SEARCH_INDEX_READY")
        except Exception as si_exc:
            _log.warning("SearchIndex initialization failed (non-fatal): %s", si_exc)
            _hybrid_search = None

        # Event Store（non-fatal）— 傳入 search_index + embedder 讓 hook 生效
        if event_store_path is None:
            event_store_path = os.environ.get(
                "TERMINUX_EVENT_STORE_PATH",
                os.path.expanduser("~/.terminux/ecc/event_store.db"),
            )
        try:
            _event_store = EventStore(
                event_store_path,
                search_index=_search_index,
                embedder=_embedder,
            )
            _event_store_opened.set()
            _event_store_closed.clear()
        except Exception as es_exc:
            # ERROR 行不含路徑與例外訊息；細節在 INFO 的 DETAIL。
            _log.error(
                "EventStore initialization failed (TERM_EVENT_STORE_UNAVAILABLE)："
                "package dispatch（含 DQE 擷取）與事件紀錄將無法運作。補救：確認 "
                "EventStore 路徑可寫（預設 ~/.terminux/ecc/event_store.db，可用 "
                "TERMINUX_EVENT_STORE_PATH 指定），然後重啟 daemon；路徑與錯誤見 "
                "TERMINUX_LOG_LEVEL=INFO 的 TERM_EVENT_STORE_UNAVAILABLE_DETAIL。",
            )
            _log.info("TERM_EVENT_STORE_UNAVAILABLE_DETAIL path=%s error=%s",
                      event_store_path, es_exc)
            _event_store = None

        try:
            ledger = OrchestrationLedger()
            # 注入既有 receipt projection 的 scoped 讀取。
            # 在此之前 production 從未注入它，`_read_receipt_projection()`
            # 永遠回 []（analysis task F-2）。它是 **execution evidence** 的來源，
            # **不是** run lifecycle authority——ledger 仍然是後者。
            _receipt_rows_reader, _receipt_payload_reader, _timing_path_reader = (
                _build_receipt_readers(_event_store, user_id=_user_id or getpass.getuser(),
                                       project_id=_project_id))
            from terminux_engine.packages.orfs_dqe import timing_path as _orfs_timing_path

            _orchestration_service = OrchestrationService(
                ledger, parent_daemon_socket=parent_daemon_socket,
                # 帶寫入者身分的列：service 只接受全部屬於 caller 本人的 receipt 投影。
                receipt_projection_reader=_receipt_rows_reader,
                timing_path_reader=_timing_path_reader,
                # timing record 必須能由已驗證 report bytes 以同一個 adapter 重新解析才外送。
                timing_path_reparsers={
                    _orfs_timing_path.ADAPTER_ROLE: _orfs_timing_path.parse_worst_setup_path},
                # 唯讀 Evidence Resolver，與 receipt reader 讀同一份 EventStore。
                execution_evidence_resolver=(
                    _build_execution_evidence_resolver(
                        _event_store, ledger, user_id=_user_id or getpass.getuser())
                    if _event_store is not None else None),
            )
            # 恰好一個 public NotifyService，復用既有 process-global
            # pending-buffer sink；同一 instance 注入 recovery 與 heartbeat
            # scanner，兩者共用同一 producer routing。
            _orchestration_notify_service = NotifyService(
                async_sink=_append_package_pending_notification
            )
            recovery_report = RecoveryScanner(
                ledger,
                _orchestration_service,
                notify_service=_orchestration_notify_service,
            ).scan_once()
            _log.info(
                "Orchestration recovery complete: re_attached=%s pending_review=%s",
                recovery_report.re_attached,
                recovery_report.marked_pending_review,
            )
            if recovery_report.marked_pending_review:
                _log.warning(
                    "Orchestration runs require review: %s",
                    recovery_report.marked_pending_review,
                )
            _orchestration_heartbeat_scanner = HeartbeatScanner(
                ledger,
                _orchestration_service,
                notify_service=_orchestration_notify_service,
            )
            _orchestration_heartbeat_scanner.start()
        except Exception as orchestration_exc:
            _log.warning(
                "Orchestration service initialization failed (non-fatal): %s",
                orchestration_exc,
            )
            _orchestration_service = None
            _orchestration_heartbeat_scanner = None

        # ── Compactor / Flusher ───────────────────────────────────────────
        from terminux_engine.llm.task_scope import profile_containment_active

        if profile_containment_active():
            # compaction 與 flush 會把完整對話送往 LLM，flush 結果再進背景索引。
            _log.info("TERM_PROFILE_CONTAINMENT disabled=conversation_compaction")
        elif _llm_router_inst is not None and _event_store is not None:
            _compactor = ConversationCompactor(_llm_router_inst)
            _flusher = MemoryFlusher(_llm_router_inst, _event_store)
            _log.info("Compactor and Flusher initialized")

        _user_id = getpass.getuser()
        _project_id = Path.home().parent.name

        # Multi-stream：建立 StreamManager 並 register "default" stream。
        # ECCAccumulator 需要 playbook；沒有 playbook 時兩者維持 None，runtime
        # 各入口（post_exec／pre_exec／chat_query／terminal.register）已處理 None。
        if _ecc_playbook is not None:
            _stream_manager = StreamManager(
                playbook=_ecc_playbook,
                event_store=_event_store,
                user_id=_user_id,
                session_id="",  # 由 accumulator 自動生成
                project_id=_project_id,
                compactor=_compactor,
                flusher=_flusher,
            )
            _default_stream = _stream_manager.register_terminal("default")
            _ecc_accumulator = _default_stream.accumulator
        else:
            _stream_manager = None
            _ecc_accumulator = None

        # 載入歷史 session（排除當前 session）
        current_sid = (_ecc_accumulator.get_current().session_id
                       if _ecc_accumulator is not None else None)
        if _event_store is not None:
            session_history = load_recent_sessions_from_events(
                _event_store, _user_id, n=3, exclude_session_id=current_sid,
            )
            last_session = load_continuity_from_events(
                _event_store, _user_id, exclude_session_id=current_sid,
            )
        else:
            # fallback 到 JSONL（EventStore 初始化失敗時）
            store = ECCStore(ecc_store_path)
            session_history = store.load_recent_sessions(n=3, exclude_session_id=current_sid)
            last_session = store.load_continuity(exclude_session_id=current_sid)

        # 從 event store 聚合 enriched user profile
        user_profile = None
        if _event_store is not None:
            try:
                user_profile = build_enriched_profile(_event_store, _user_id)
            except Exception as prof_exc:
                _log.warning("EnrichedUserProfile build failed (non-fatal): %s", prof_exc)

        # 建立 team snapshot（non-fatal）
        team_snapshot = None
        if _project_id is not None:
            try:
                team_snapshot = build_team_snapshot(_project_id)
                _log.info("Team snapshot: %d members", len(team_snapshot.members))
            except Exception as team_exc:
                _log.warning("Failed to build team snapshot (non-fatal): %s", team_exc)
        # expose to module scope so helpers can read it
        _team_snapshot = team_snapshot

        # 使用真實 LLM（若環境變數設定），否則 deterministic fallback
        provider = _get_runtime_provider()
        if provider is not None:
            _ecc_companion = AICompanion(
                llm_complete=provider.complete,
                session_history=session_history,
                last_session=last_session,
                user_profile=user_profile,
                team_snapshot=team_snapshot,
            )
            _log.info("ECC AI companion initialized with LLM provider")
        else:
            _ecc_companion = AICompanion(
                llm_complete=lambda p: "",
                session_history=session_history,
                last_session=last_session,
                user_profile=user_profile,
                team_snapshot=team_snapshot,
            )
            _log.info("ECC AI companion using deterministic fallback (no LLM configured)")
        if session_history:
            _log.info("Loaded %d previous session(s) for cross-session memory", len(session_history))
        if last_session:
            _log.info("TERM_SESSION_CONTINUITY_READY")
        # emit session_start event
        llm_backend = os.environ.get("TERMINUX_LLM_BACKEND", "none")
        if _ecc_accumulator is not None:
            _ecc_accumulator.emit_session_start(llm_backend=llm_backend)

        # ── AgentRunner setup（non-fatal）─────
        # 只在 backend 屬於 Agent-capable 集合 + provider 已就緒時建立。失敗或
        # backend 不符 → _agent_runner 保持 None，chat_query 自動 fallback 到
        # ai_companion。
        _AGENT_CAPABLE_BACKENDS = {"gemini", "openai_compatible"}
        try:
            _backend_lower = (llm_backend or "").lower()
            # ── task control 與 provider 組裝分離 ──
            # ToolRegistry／core tools／package action tools／ProposalRegistry／
            # PlanRegistry 是 **task service 與 Agent 共用的同一份** registry，
            # 由 production composition 無條件建立一次。Provider adapter／router
            # 只是「分析執行依賴」，它不可用時 registry 仍然存在——否則
            # task control、歷史查詢與取消會一起消失。
            #
            # 這裡刻意**不**另建一套缺少 dangerous gate 的測試 registry。
            _provider_ready = (
                provider is not None and _backend_lower in _AGENT_CAPABLE_BACKENDS
            )
            from terminux_engine.llm.agent_adapter import AgentLLMAdapter
            from terminux_engine.llm.agent_runner import AgentRunner
            from terminux_engine.llm.tool_registry import ToolRegistry
            from terminux_engine.llm.tools import register_core_tools

            # 嘗試 load RAG index（不存在 → 仍註冊 tool，
            # executor 自動回 available=false；不阻塞 Agent 啟動）
            _rag_index = None
            _rag_index_dir = os.path.expanduser("~/.terminux/rag_index")
            _rag_embed = None
            try:
                from terminux_engine.llm.embedding_factory import build_rag_embedding
                from terminux_engine.llm.rag_index import RAGIndex
                from terminux_engine.llm.task_scope import rag_embedding_allowed

                if rag_embedding_allowed():
                    _rag_embed = build_rag_embedding()
                else:
                    _log.info("TERM_PROFILE_CONTAINMENT disabled=rag_embedding")
                if _rag_embed is not None:
                    if os.path.isdir(_rag_index_dir):
                        _candidate = RAGIndex(_rag_index_dir, embedding=_rag_embed)
                        if _candidate.load():
                            _rag_index = _candidate
                            _log.info(
                                "RAG index loaded from %s", _rag_index_dir
                            )
                        else:
                            _log.info(
                                "RAG index not loadable at %s; "
                                "retrieve_knowledge will be unavailable",
                                _rag_index_dir,
                            )
                    else:
                        _log.info(
                            "RAG index does not exist at %s; "
                            "shared rebuild will initialize it",
                            _rag_index_dir,
                        )
                else:
                    _log.info(
                        "RAG index skipped (embedding_configured=%s, dir_exists=%s)",
                        _rag_embed is not None,
                        os.path.isdir(_rag_index_dir),
                    )
            except Exception as rag_exc:
                _log.warning(
                    "RAG index initialization failed (non-fatal): %s",
                    rag_exc,
                )
                _rag_index = None

            _rag_embedding = _rag_embed

            _ledger_path = os.path.expanduser("~/.terminux/lineage/ledger.jsonl")
            _agent_tools_registry = ToolRegistry()
            _allowed_dirs = [
                str(Path.home() / ".terminux"),
                "/tmp",
                os.getcwd(),
            ]
            register_core_tools(
                _agent_tools_registry,
                event_store=_event_store,
                ledger_path=_ledger_path,
                accumulator=_ecc_accumulator,
                team_snapshot=team_snapshot,
                user_id=_user_id,
                rag_index=lambda: _rag_index,
                allowed_dirs=_allowed_dirs,
                playbook=_ecc_playbook,
                profile_loader=_build_profile,
                report_open_callback=_append_pending_open_path,
                llm_router=_llm_router_inst,
                project_id=_project_id,
                dispatch_callback=_schedule_package_dispatch,
                orchestration_service=_orchestration_service,
            )
            from terminux_engine.llm.package_action_tools import (
                register_package_action_tools,
            )

            _package_action_tools = register_package_action_tools(
                _agent_tools_registry,
                _get_package_registry().packages,
                _get_package_action_registry(),
                _build_action_context,
            )
            # ProposalRegistry／PlanRegistry 與上面的 tool registry 一樣
            # 不依賴 provider。task service 與 AgentRunner 共用這同一份物件；
            # 沒有 provider 時 task 的查詢、取消、Profile 與草案建立仍可用。
            global _proposal_registry, _plan_registry
            if _proposal_registry is None:
                from terminux_engine.llm.proposal_registry import ProposalRegistry
                _proposal_registry = ProposalRegistry()
            if _plan_registry is None:
                from terminux_engine.llm.plan_registry import PlanRegistry
                _plan_registry = PlanRegistry(_proposal_registry)
            if _provider_ready:
                _adapter = AgentLLMAdapter(provider, provider_type=_backend_lower)

                _agent_runner = AgentRunner(
                    llm_provider=_adapter,
                    tool_registry=_agent_tools_registry,
                    tool_format="openai",
                    enable_read_only_planner=True,
                    proposal_registry=_proposal_registry,
                    proposable_catalog_provider=_general_proposable_catalog,
                    plan_registry=_plan_registry,
                    # 與 typed `agent.analysis_task.create` 是**同一個**
                    # validator/service；Agent 只提議三個參數，其餘由 host 產生。
                    analysis_task_creator=_create_analysis_task_from_intent,
                    # 與 typed `agent.flow_task.create` 是同一個
                    # service 呼叫；可用 profile 由 server policy 決定。
                    flow_task_creator=_create_flow_task_from_intent,
                    task_profile_provider=_list_task_profiles_for_caller,
                )
                _agent_router = _llm_router_inst
                _log.info(
                    "AgentRunner initialized (%s backend, %d tools, package_actions=%d, rag=%s)",
                    _backend_lower,
                    len(_agent_tools_registry.list_tools()),
                    len(_package_action_tools),
                    _rag_index is not None,
                )

                _schedule_shared_rag_rebuild(
                    index_dir=Path(_rag_index_dir),
                    user_knowledge_dir=Path.home() / ".terminux" / "rag_knowledge",
                    embedding=_rag_embed,
                )

                # ── CommandBuffer + AgentTaskQueue ──────────
                try:
                    from terminux_engine.llm.agent_task_queue import AgentTaskQueue
                    from terminux_engine.llm.command_buffer import CommandBuffer

                    _agent_task_queue = AgentTaskQueue(
                        agent_runner=_agent_runner,
                        max_concurrent=2,
                        max_queue_depth=3,
                        on_result=_on_async_agent_result,
                    )
                    _command_buffer = CommandBuffer(
                        window_sec=30.0,
                        dedup_sec=15.0,
                        on_flush=_on_command_buffer_flush,
                        ecc_snapshot_fn=_get_ecc_snapshot,
                        recent_events_fn=_get_recent_events,
                        is_eda_tool_fn=_is_eda_tool,
                    )
                    _log.info("Async Agent pipeline initialized (CommandBuffer + Queue)")
                except Exception as buf_exc:
                    _log.warning(
                        "Async Agent pipeline init failed (non-fatal): %s", buf_exc
                    )
                    _command_buffer = None
                    _agent_task_queue = None

                # ── Dreaming AgentRunner（analysis slot, 120s/8） ──
                try:
                    from terminux_engine.llm.llm_router import TaskType

                    _analysis_provider = None
                    if _llm_router_inst is not None:
                        _analysis_provider = _llm_router_inst.get_provider(
                            TaskType.ANALYSIS
                        )
                    if _analysis_provider is not None:
                        _da_adapter = AgentLLMAdapter(
                            _analysis_provider, provider_type=_backend_lower
                        )
                        _deep_analysis_runner = AgentRunner(
                            llm_provider=_da_adapter,
                            tool_registry=_agent_tools_registry,
                            tool_format="openai",
                            max_rounds=8,
                            timeout_sec=120.0,
                        )
                        _log.info("Dreaming AgentRunner initialized (analysis slot)")
                    else:
                        _log.info("Dreaming AgentRunner skipped: no analysis-slot provider")
                except Exception as da_exc:
                    _log.warning("Dreaming AgentRunner init failed (non-fatal): %s", da_exc)
                    _deep_analysis_runner = None

                # ── load 最近 deep_analysis 注入後續 chat prompt ──
                try:
                    from terminux_engine.memory.dreaming import (
                        find_recent_deep_analysis,
                    )

                    _deep_analysis_summary = find_recent_deep_analysis(
                        _event_store, _user_id, max_age_days=7
                    )
                    if _deep_analysis_summary:
                        _log.info(
                            "Loaded recent deep_analysis summary (%d chars) for prompt context",
                            len(_deep_analysis_summary),
                        )
                except Exception as ds_exc:
                    _log.warning("deep_analysis summary lookup failed (non-fatal): %s", ds_exc)
                    _deep_analysis_summary = None
            else:
                _log.info(
                    "AgentRunner not initialized "
                    "(backend=%s, provider_ready=%s); chat_query falls back to ai_companion",
                    llm_backend, provider is not None,
                )
        except Exception as agent_exc:
            _log.warning(
                "AgentRunner initialization failed (non-fatal, "
                "chat_query falls back to ai_companion): %s",
                agent_exc,
            )
            _agent_runner = None
            _agent_router = None
            # **不**清掉共用 registry——它是 task service 與
            # Agent 共用的同一份；provider 段失敗只影響 Agent。
            # 若 registry 本身沒建起來，它本來就還是 None。

        # ── Dreamer（non-fatal）─────────────────────────────────────────────
        _init_dreamer(_event_store, _llm_router_inst)

        # ── JSONL ingestor（non-fatal）────────────────────────
        # 定時讀 ~/.terminux/lineage.jsonl，將 wrapper 寫入的 tool_exec events
        # ingest 到 event_store。daemon=True thread，process exit 時自動結束。
        _init_jsonl_ingestor(_event_store, _user_id)

        # ── lineage.jsonl log sweeper（non-fatal）───────────────
        # 定時 rotate + gzip + purge 舊備份。與 ingestor sidecar 自然協調。
        _init_log_sweeper()
        _init_proactive_runner()
        # analysis access policy 只在啟動時載入一次。
        _init_analysis_access_policy()
        # flow execution profile 同樣只在啟動時載入一次。
        _init_flow_task_profile()
        # limited activation policy 同樣只在啟動時載入一次；未配置
        # 就是預設 disabled，flow 的 create／start／launch 全部拒絕。
        _init_limited_activation_policy()

        # ── startup backfill safety net ─────────────────────────
        if _event_store is not None and _llm_router_inst is not None and _user_id:
            _startup_backfill_stop_event.clear()
            _startup_backfill_thread = threading.Thread(
                target=_startup_backfill_safety_net,
                args=(_event_store, _llm_router_inst, _user_id, time.time()),
                daemon=True,
                name="terminux-startup-backfill",
            )
            _startup_backfill_thread.start()
            _log.info("Startup backfill thread launched")

        _log.info("TERM_ECC_PIPELINE_READY")
    except Exception as exc:
        # ERROR 行只帶例外型別；例外訊息可能含路徑，放在 INFO 的 DETAIL。
        _log.error(
            "ECC pipeline initialization failed (TERM_ECC_PIPELINE_INIT_FAILED) "
            "error_type=%s：此後的 daemon 服務（ECC、Agent、orchestration、背景 "
            "ingest 等）可能未初始化；請以 TERMINUX_LOG_LEVEL=INFO 重啟 daemon，"
            "查看 TERM_ECC_PIPELINE_INIT_FAILED_DETAIL 與完整初始化 log。",
            type(exc).__name__,
        )
        _log.info("TERM_ECC_PIPELINE_INIT_FAILED_DETAIL error=%s", exc)
        _ecc_accumulator = None
        _ecc_companion = None
        _ecc_playbook = None
        _stream_manager = None
        _hybrid_search = None


def stop_orchestration_heartbeat_scanner() -> None:
    """停止 orchestration heartbeat scanner，供 daemon shutdown 呼叫。"""
    global _orchestration_heartbeat_scanner
    if _orchestration_heartbeat_scanner is not None:
        _orchestration_heartbeat_scanner.stop()
        _orchestration_heartbeat_scanner = None


def _shutdown_all_background_workers(
    total_timeout: float | None = None,
) -> list[str]:
    """統一第二階段關機：先對所有背景元件發出停止訊號，再以共用總期限依序等待。

    1. 先對所有元件發出停止訊號（non-blocking）。
    2. 以共用的總期限（total_timeout）依序 join，每個元件只能使用剩餘時間。
    3. 盤點所有讀寫 EventStore 的背景元件，回傳仍存活的名稱清單。
    保證冪等。
    """
    global _startup_backfill_thread, _jsonl_ingestor, _dreamer
    global _proactive_runner, _log_sweeper, _rag_rebuild_stop_event
    global _command_buffer, _agent_task_queue, _rag_rebuild_thread

    if total_timeout is None:
        total_timeout = _TOTAL_SHUTDOWN_TIMEOUT_SEC

    start_time = time.monotonic()
    deadline = start_time + total_timeout

    # ── 步驟 A：先對所有元件發出停止訊號（Non-blocking）────────────────
    _startup_backfill_stop_event.set()

    if _jsonl_ingestor is not None:
        try:
            _jsonl_ingestor.signal_stop()
        except Exception as exc:
            _log.warning("JsonlIngestor signal_stop failed: %s", exc)

    if _dreamer is not None:
        try:
            _dreamer.signal_stop()
        except Exception as exc:
            _log.warning("Dreamer signal_stop failed: %s", exc)

    if _proactive_runner is not None:
        try:
            _proactive_runner.signal_stop()
        except Exception as exc:
            _log.warning("Proactive runner signal_stop failed: %s", exc)

    if _log_sweeper is not None:
        try:
            _log_sweeper.signal_stop()
        except Exception as exc:
            _log.warning("LogSweeper signal_stop failed: %s", exc)

    _shutdown_shared_rag_rebuild()

    if _command_buffer is not None:
        try:
            _command_buffer.shutdown()
        except Exception:
            _log.warning("TERM_COMMAND_BUFFER_SHUTDOWN_FAILED")
        _command_buffer = None

    if _agent_task_queue is not None:
        try:
            _agent_task_queue.signal_stop()
        except Exception:
            _log.warning("TERM_AGENT_QUEUE_SIGNAL_STOP_FAILED")

    # ── 步驟 B：共用總期限依序 join，每個元件只能使用剩餘時間 ─────────
    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    # 1. Startup backfill
    if _startup_backfill_thread is not None:
        try:
            if _startup_backfill_thread.is_alive():
                _startup_backfill_thread.join(timeout=_remaining())
        except Exception as exc:
            _log.warning("Startup backfill join failed: %s", exc)
        if not _startup_backfill_thread.is_alive():
            _startup_backfill_thread = None

    # 2. JSONL Ingestor
    if _jsonl_ingestor is not None:
        try:
            _jsonl_ingestor.join(timeout=_remaining())
        except Exception as exc:
            _log.warning("JsonlIngestor join failed: %s", exc)
        if not _jsonl_ingestor.is_alive:
            _jsonl_ingestor = None

    # 3. Dreamer
    if _dreamer is not None:
        try:
            _dreamer.join(timeout=_remaining())
        except Exception as exc:
            _log.warning("Dreamer join failed: %s", exc)
        if not _dreamer.is_alive:
            _dreamer = None

    # 4. Proactive Runner
    if _proactive_runner is not None:
        try:
            _proactive_runner.join(timeout=_remaining())
        except Exception as exc:
            _log.warning("Proactive runner join failed: %s", exc)
        if not _proactive_runner.is_alive:
            _proactive_runner = None

    # 5. Log Sweeper
    if _log_sweeper is not None:
        try:
            _log_sweeper.join(timeout=_remaining())
        except Exception as exc:
            _log.warning("LogSweeper join failed: %s", exc)
        if not _log_sweeper.is_alive:
            _log_sweeper = None

    # 6. Agent Task Queue
    if _agent_task_queue is not None:
        try:
            _agent_task_queue.join(timeout=_remaining())
        except Exception:
            _log.warning("TERM_AGENT_QUEUE_JOIN_FAILED")
        if not _agent_task_queue.is_alive:
            _agent_task_queue = None

    # 7. Package Dispatch
    with _package_dispatch_threads_lock:
        pkg_threads = list(_package_dispatch_threads)
    for pkg_t in pkg_threads:
        rem = _remaining()
        if rem <= 0:
            break
        try:
            if pkg_t.is_alive():
                pkg_t.join(timeout=rem)
        except Exception as exc:
            _log.warning("Package dispatch thread join failed: %s", exc)

    # 8. Active JSON-RPC Requests
    with _active_request_lock:
        req_threads = list(_active_request_threads)
    for req_t in req_threads:
        rem = _remaining()
        if rem <= 0:
            break
        try:
            if req_t.is_alive():
                req_t.join(timeout=rem)
        except Exception as exc:
            _log.warning("Active request thread join failed: %s", exc)

    # 9. Shared RAG Rebuild
    if _rag_rebuild_thread is not None:
        try:
            if _rag_rebuild_thread.is_alive():
                _rag_rebuild_thread.join(timeout=_remaining())
        except Exception as exc:
            _log.warning("Shared RAG rebuild join failed: %s", exc)
        if not _rag_rebuild_thread.is_alive():
            _rag_rebuild_thread = None

    _rag_rebuild_stop_event = None

    elapsed = time.monotonic() - start_time
    _log.info("TERM_SHUTDOWN_WORKERS_ELAPSED：背景元件停止總耗時 %.3fs", elapsed)

    # ── 步驟 C：通用規則判定讀寫 EventStore 的背景元件 ──────────────────
    unclosed_event_store_workers: list[str] = []
    if _startup_backfill_thread is not None and _startup_backfill_thread.is_alive():
        unclosed_event_store_workers.append("startup_backfill")
    if _jsonl_ingestor is not None and _jsonl_ingestor.is_alive:
        unclosed_event_store_workers.append("jsonl_ingestor")
    if _dreamer is not None and _dreamer.is_alive:
        unclosed_event_store_workers.append("dreamer")
    if _proactive_runner is not None and _proactive_runner.is_alive:
        unclosed_event_store_workers.append("proactive_runner")
    if _agent_task_queue is not None and _agent_task_queue.is_alive:
        unclosed_event_store_workers.append("agent_task_queue")
    with _package_dispatch_threads_lock:
        if any(t.is_alive() for t in _package_dispatch_threads):
            unclosed_event_store_workers.append("package_dispatch")
    with _package_dispatch_inflight_lock:
        if bool(_package_dispatch_inflight) and "package_dispatch" not in unclosed_event_store_workers:
            unclosed_event_store_workers.append("package_dispatch")
    with _active_request_lock:
        if any(t.is_alive() for t in _active_request_threads):
            unclosed_event_store_workers.append("active_jsonrpc_requests")

    return unclosed_event_store_workers


def _reset_ecc_state() -> None:
    """重設 ECC 與 Orchestration 的 module-global state，停止背景掃描與清除服務引用。"""
    global _ecc_accumulator, _ecc_companion, _ecc_playbook, _event_store, _user_id, _project_id
    global _stream_manager, _hybrid_search, _agent_runner, _agent_router, _agent_tools_registry
    global _package_llm_router, _proposal_registry, _plan_registry
    global _orchestration_service, _orchestration_notify_service, _receipt_payload_reader
    global _orchestration_heartbeat_scanner, _analysis_task_service, _startup_backfill_thread
    global _rag_rebuild_thread
    _event_store_closed.clear()

    # 第一階段：停止進線與外部調度
    stop_orchestration_heartbeat_scanner()
    if _orchestration_service is not None:
        try:
            _orchestration_service.shutdown_registered_children()
        except Exception:
            pass

    # 第二階段：先對所有元件發出停止訊號，再以共用期限依序等待
    unclosed_workers = _shutdown_all_background_workers()

    # 第三階段：通用規則——所有讀寫 EventStore 的元件確認結束才關閉 EventStore
    if _event_store is not None:
        if unclosed_workers:
            _log.warning(
                "TERM_EVENT_STORE_CLOSE_SKIPPED：背景元件仍在執行 (%s)，"
                "不關閉 EventStore 連線以免在使用中被釋放，交由程序結束時回收",
                ", ".join(unclosed_workers),
            )
        else:
            try:
                _event_store.close()
                _event_store_closed.set()
            except Exception:
                pass

    _ecc_accumulator = None
    _ecc_companion = None
    _ecc_playbook = None
    _event_store = None
    _user_id = ""
    _project_id = None
    _receipt_payload_reader = None
    _stream_manager = None
    _hybrid_search = None
    _agent_runner = None
    _agent_router = None
    _agent_tools_registry = None
    from terminux_engine.llm import run_binding
    run_binding.clear()
    _package_llm_router = None
    _proposal_registry = None
    # PlanRegistry 包著上面那份 ProposalRegistry；不一起清掉，下次組裝會新建
    # ProposalRegistry 卻沿用舊的 PlanRegistry（兩者每次都會建立）。
    _plan_registry = None
    _orchestration_service = None
    _orchestration_notify_service = None
    _orchestration_heartbeat_scanner = None
    _analysis_task_service = None
    _startup_backfill_thread = None
    _startup_backfill_stop_event.clear()
    _rag_rebuild_thread = None


def _schedule_shared_rag_rebuild(
    *,
    index_dir: Path,
    user_knowledge_dir: Path,
    embedding: Any,
) -> None:
    """在 daemon 啟動後非阻塞地重建含 shared knowledge 的 RAG index。"""
    global _rag_index, _rag_rebuild_stop_event
    if os.environ.get(_ENV_DISABLE_RAG_BACKGROUND_REBUILD, "").strip().lower() \
            in {"1", "true", "yes"}:
        _log.info("Shared RAG rebuild disabled by %s", _ENV_DISABLE_RAG_BACKGROUND_REBUILD)
        return
    if embedding is None:
        _log.info("Shared RAG rebuild skipped: embedding unavailable")
        return
    try:
        from terminux_engine.llm.rag_index import RAGIndex
        from terminux_engine.rag.knowledge_codec import resolve_shared_knowledge_dir
        from terminux_engine.rag.shared_knowledge import start_background_rebuild

        if _rag_rebuild_stop_event is not None:
            _rag_rebuild_stop_event.set()
        stop_event = threading.Event()
        _rag_rebuild_stop_event = stop_event

        def _reload_index() -> None:
            global _rag_index
            candidate = RAGIndex(index_dir, embedding=embedding)
            if candidate.load():
                _rag_index = candidate
                _log.info("Shared RAG index reloaded from %s", index_dir)
            else:
                _log.warning("Shared RAG index rebuild succeeded but reload failed")

        global _rag_rebuild_thread
        _rag_rebuild_thread = start_background_rebuild(
            user_knowledge_dir=user_knowledge_dir,
            index_dir=index_dir,
            shared_knowledge_dir=resolve_shared_knowledge_dir(),
            embedding=embedding,
            on_reloaded=_reload_index,
            stop_event=stop_event,
        )
    except Exception as error:
        _log.warning("Shared RAG rebuild scheduling failed (non-fatal): %s", error)


def _shutdown_shared_rag_rebuild() -> None:
    """通知背景 rebuild 在當前 build 結束後清理暫存目錄並離開。"""
    global _rag_rebuild_stop_event
    if _rag_rebuild_stop_event is not None:
        _rag_rebuild_stop_event.set()
        _rag_rebuild_stop_event = None


def _init_dreamer(
    event_store: EventStore | None,
    llm_router: Any,
) -> None:
    """daemon 啟動時初始化 Dreamer（在 _init_ecc_inner 內呼叫）。"""
    global _dreamer
    if event_store is None or llm_router is None:
        _log.warning("Dreamer initialization skipped (event_store=%s, llm_router=%s)",
                      event_store is not None, llm_router is not None)
        return
    try:
        _dreamer = Dreamer(
            event_store=event_store,
            llm_router=llm_router,
            deep_analysis_runner=_deep_analysis_runner,
        )
        _log.info(
            "Dreamer initialized (deep_analysis_runner=%s)",
            _deep_analysis_runner is not None,
        )
    except Exception as exc:
        _log.warning("Dreamer initialization failed (non-fatal): %s", exc)
        _dreamer = None


def _shutdown_dreamer() -> None:
    """daemon 關閉時呼叫。"""
    global _dreamer
    if _dreamer is not None:
        _dreamer.close()
        _dreamer = None


def _init_jsonl_ingestor(
    event_store: EventStore | None,
    user_id: str,
) -> None:
    """daemon 啟動時初始化 JsonlIngestor（在 _init_ecc_inner 內呼叫）。"""
    global _jsonl_ingestor
    if event_store is None or not user_id:
        _log.warning(
            "JsonlIngestor skipped (event_store=%s, user_id=%r)",
            event_store is not None, user_id,
        )
        return
    try:
        from terminux_engine.lineage.jsonl_ingestor import JsonlIngestor
        jsonl_path = Path.home() / ".terminux" / "lineage.jsonl"
        _jsonl_ingestor = JsonlIngestor(
            jsonl_path=jsonl_path,
            event_store=event_store,
            user_id=user_id,
        )
        _jsonl_ingestor.start()
        _log.info("TERM_JSONL_INGESTOR_READY")
    except Exception as exc:
        _log.warning("JsonlIngestor initialization failed (non-fatal): %s", exc)
        _jsonl_ingestor = None


def _shutdown_jsonl_ingestor() -> None:
    """daemon 關閉時呼叫（與 _shutdown_dreamer 對稱）。"""
    global _jsonl_ingestor
    if _jsonl_ingestor is not None:
        try:
            _jsonl_ingestor.stop()
        except Exception as exc:
            _log.warning("JsonlIngestor stop failed: %s", exc)
        _jsonl_ingestor = None


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _package_dispatch_disabled() -> bool:
    return _env_truthy("TERMINUX_DISABLE_PACKAGES")


def _get_tool_catalog() -> ToolCatalog | None:
    """載入並快取 Canonical EDA Tool Catalog v1（唯讀，daemon 生命週期內只
    載入一次）。載入失敗（malformed／找不到檔案）不得讓 daemon 崩潰，只影響
    `shell.post_exec` host validation——回傳 `None` 時呼叫端須將所有 event
    視為 unknown/non-EDA（dispatch 一律 forbidden，fail closed，不 fallback
    到未驗證的 client claim）。"""
    global _tool_catalog_cache, _tool_catalog_load_failed_logged
    with _tool_catalog_lock:
        if _tool_catalog_cache is not None:
            return _tool_catalog_cache
        try:
            _tool_catalog_cache = load_catalog()
        except CatalogError as exc:
            if not _tool_catalog_load_failed_logged:
                _tool_catalog_load_failed_logged = True
                _log.error("TERM_TOOL_CATALOG_LOAD_FAILED: %s", exc)
            return None
        return _tool_catalog_cache


def _get_package_registry() -> PackageRegistry:
    """載入並快取內建 packages registry。

    透過 `registry.py::discover_and_register_packages()` 做 two-pass
    安全組裝：Pass 1 發現＋驗證全部 manifest（含跨 package 的 duplicate
    package/action id 檢查，零 import），Pass 1 全部通過才進 Pass 2
    真正建立 registry。若偵測到任何 duplicate identity，不嘗試判斷
    「哪一個是合法的」贏家——依既有「package 目錄不存在則零 package」
    的既有降級慣例，記清楚的 error log 後回傳空 registry，不讓
    daemon startup 因此整體中止（未改變既有 daemon startup 對
    Package Runtime load failure 的容錯政策，只是把「duplicate
    identity」這個新增的檢查也納入同一種「記 log、降級」處理方式）。
    """
    global _package_registry_cache
    with _package_registry_lock:
        if _package_registry_cache is not None:
            return _package_registry_cache

        package_dirs = sorted(
            path for path in _BUILTIN_PACKAGES_DIR.iterdir()
            if (path / "package.yaml").is_file()
        ) if _BUILTIN_PACKAGES_DIR.is_dir() else []

        try:
            registry = discover_and_register_packages(package_dirs)
        except (DuplicatePackageIdError, DuplicatePackageActionIdError) as exc:
            _log.error(
                "Package registry assembly rejected due to duplicate identity: %s. "
                "0 packages registered.",
                exc,
            )
            registry = PackageRegistry()

        _package_registry_cache = registry
        _log.info("Package registry initialized: %d package(s)", len(registry.packages))
        return registry


# User-installed package（`~/.terminux/packages`）。內建 registry 與 action
# registry 完全不含 user package；只有 after-phase dispatch 會把目前的 user
# activation 接在內建 package 之後。`extensions.reload` 是唯一的手動啟用入口，
# 沒有檔案監看，也不重啟 daemon。
_user_package_manager = UserPackageManager(
    root_resolver=lambda: resolve_user_packages_root(create=False),
    builtin_ids=lambda: builtin_package_ids(_BUILTIN_PACKAGES_DIR),
    # 使用者工具 overlay 與 user package 在同一次 activation 一起發布。
    tool_scope_builder=lambda previous: build_user_tool_scope(_get_tool_catalog(), previous),
)


class _PinnedUserActivation:
    """一次 `shell.post_exec` 固定使用的 user activation lease。

    request handler 以它的 effective tool catalog 驗證身分，dispatch 執行緒再用
    同一份 activation 的 package；lease 由最後持有者釋放（只釋放一次）。
    """

    def __init__(self, manager: UserPackageManager, activation: UserPackageActivation) -> None:
        self._manager = manager
        self.activation = activation
        self.transferred = False
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._manager.release(self.activation)


def _pin_user_activation() -> _PinnedUserActivation | None:
    """取得 user activation lease；停用或失敗時回傳 None（只用內建 package 與內建工具）。"""
    if user_packages_disabled():
        return None
    manager = _user_package_manager
    try:
        return _PinnedUserActivation(manager, manager.acquire())
    except Exception as exc:
        _log.warning("User extension activation unavailable; built-in only: %s", exc)
        return None


def _effective_tool_catalog(activation: UserPackageActivation | None) -> ToolCatalog | None:
    """activation 內的 effective catalog；沒有 tool scope 時退回內建 catalog（既有行為）。"""
    if activation is not None and activation.tools is not None:
        return activation.tools.catalog
    return _get_tool_catalog()


@contextmanager
def _user_package_dispatch_lease() -> Iterator[UserPackageActivation | None]:
    """取得 user activation lease；取得失敗只記 log，dispatch 退回只用內建 package。"""
    with ExitStack() as stack:
        activation: UserPackageActivation | None = None
        if not user_packages_disabled():
            try:
                activation = stack.enter_context(_user_package_manager.lease())
            except Exception as exc:
                _log.warning("User package activation unavailable; built-in packages only: %s", exc)
        yield activation


def _dispatch_registry_with_user_packages(
    activation: UserPackageActivation | None,
    *,
    user_packages_only: bool = False,
) -> PackageRegistry:
    """內建 registry 加上 user package；任何組合問題都退回內建 registry。

    `user_packages_only`：事件的工具是使用者登記的工具時，只交給 user package，
    內建 package 永遠不會被使用者工具的事件觸發。
    """
    if user_packages_only:
        if activation is None or user_packages_disabled():
            return PackageRegistry()
        return PackageRegistry(list(activation.packages))
    builtin = _get_package_registry()
    if activation is None or not activation.packages or user_packages_disabled():
        return builtin
    try:
        return PackageRegistry([*builtin.packages, *activation.packages])
    except DuplicatePackageIdError as exc:
        _log.error("User package registry rejected; built-in packages only: %s", exc)
        return builtin


def _extensions_payload(activation: UserPackageActivation) -> dict[str, Any]:
    return {
        # user_packages 與 user_tools 屬於同一個 activation（同一個 activation_id）。
        "activation_id": activation.activation_id,
        "scopes": {
            "user_packages": activation.to_dict(),
            "user_tools": activation.tools.to_dict() if activation.tools is not None else None,
        },
        "builtin_packages": sorted(package.id for package in _get_package_registry().packages),
    }


def _handle_extensions_request(method: str, req_id: Any, params: dict) -> dict:
    if params:
        return _err(req_id, _ERR_INVALID_PARAMS, f"{method} does not accept params")
    try:
        if method == "extensions.reload":
            outcome = _user_package_manager.reload()
            payload = _extensions_payload(outcome.activation)
            payload["reload"] = outcome.to_dict()
            return _ok(req_id, payload)
        return _ok(req_id, _extensions_payload(_user_package_manager.ensure_active()))
    except Exception as exc:
        _log.error("%s failed: %s", method, exc)
        return _err(req_id, _ERR_INTERNAL, "Internal error")


def _get_package_action_registry() -> ActionRegistry:
    """載入並快取 package action registry。

    Duplicate action id 已經在 `_get_package_registry()` 的 Pass 1
    （`discover_and_register_packages()`）被排除——能走到這裡的
    package 之間必然沒有 duplicate action id，因此 `registry.register()`
    的 duplicate 檢查在正常情況下不會觸發，僅作為 defense-in-depth。

    每個 package 的 action 解析（會真正 import handler module）以
    package 為單位獨立 try/except：單一 package 的某個 action 若解析
    失敗（例如 manifest 宣告的函式在檔案中不存在——這是既有
    entrypoint containment 檢查不會攔到的一類問題，只有真的 import 才
    會發現），只跳過該 package 的全部 action（避免半個 package 的
    action 進了 registry），不影響其他 package——修正先前既有的
    fragility（原本任一 action 解析失敗會讓整個函式呼叫失敗，波及全部
    package 的 action registry）。
    """
    global _package_action_registry_cache
    with _package_action_registry_lock:
        if _package_action_registry_cache is not None:
            return _package_action_registry_cache

        registry = ActionRegistry()
        for package in _get_package_registry().packages:
            try:
                resolved_actions = [
                    (action_name, package.resolve_action(action_name))
                    for action_name in package.manifest.actions
                ]
            except Exception as exc:
                _log.warning(
                    "Package action resolution failed, skipping package: %s: %s",
                    package.id, exc,
                )
                continue
            for action_name, func in resolved_actions:
                registry.register(action_name, func, package_id=package.id)
        _package_action_registry_cache = registry
        return registry


def _prune_pending_package_actions(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _PACKAGE_ACTION_TTL_SEC
    expired = [
        notification_id
        for notification_id, entry in _pending_package_actions.items()
        if float(entry.get("created_at", 0.0)) < cutoff
    ]
    for notification_id in expired:
        _pending_package_actions.pop(notification_id, None)


def _register_pending_package_notification(payload: dict[str, Any]) -> None:
    notification_id = payload.get("notification_id")
    actions = payload.get("actions")
    if not isinstance(notification_id, str) or not notification_id:
        return
    if not isinstance(actions, list) or not actions:
        return

    callable_actions: dict[str, dict[str, Any]] = {}
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            continue
        action_name = raw_action.get("action")
        if not isinstance(action_name, str) or not action_name:
            continue
        raw_args = raw_action.get("args", {})
        callable_actions[action_name] = raw_args if isinstance(raw_args, dict) else {}
    if not callable_actions:
        return

    with _pending_package_actions_lock:
        _prune_pending_package_actions()
        _pending_package_actions[notification_id] = {
            "created_at": time.time(),
            "actions": callable_actions,
            "payload": dict(payload),
        }


def _build_action_context(action_name: str, params: dict[str, Any]) -> PackageContext:
    # package id 取自 action registry 註冊時驗證過的 owner，不是把
    # action name 拆字串猜的。解析不到就讓 ActionRegistryError 往上拋——由
    # `_invoke_package_action()` 既有的 unknown_action 分支處理，不猜一個
    # package id 繼續往下走。
    package_id = _get_package_action_registry().owner_of(action_name)
    terminal_id = params.get("terminal_id") if isinstance(params.get("terminal_id"), str) else "default"
    facts = {"action_args": params.get("args", {}) if isinstance(params.get("args"), dict) else {}}
    # Flow step 的 action context 必須讀 task-owned Profile 所固定的 evidence
    # sink，不能退回 daemon HOME 的預設 DQE，也不能接受 caller supplied path。
    # flow binding context 僅由正式 task grant path 安裝，離開該 dispatch 即 reset。
    from terminux_engine.bounded_execution.analysis_tasks.flow_binding_context import (
        get_current_flow_context,
    )
    flow_context = get_current_flow_context()
    if flow_context is not None:
        facts.update(flow_context.evidence_facts)
    return PackageContext(
        package_id=package_id,
        package_version="",
        phase="action",
        event=TimelineEvent(
            event_id=f"pkg_action_{uuid4().hex}",
            trace_id=None,
            command="package.invoke_action",
            argv=["package.invoke_action", action_name],
            cwd=str(Path.cwd()),
            tool_name=None,
            user=_user_id or getpass.getuser(),
            host=local_nodename(),
            terminal_id=terminal_id,
            source="package.invoke_action",
            timestamp=str(time.time()),
            metadata={
                "notification_id": params.get("notification_id", ""),
                "action": action_name,
                "language": load_preferred_language(),
            },
        ),
        facts=facts,
        # action path 過去沒有注入 package_id，`StoreService.get_latest()`
        # 因此在 action 內一律 raise。dispatch path 早已由
        # `DispatchEngine._services_for_package()` 逐 package 注入；這裡補上
        # 同一個身分，兩條路徑的 record scope 才一致。
        services=_build_package_runtime_services(
            terminal_id=terminal_id, package_id=package_id,
        ),
    )


def _invoke_package_action(params: dict[str, Any]) -> dict[str, Any]:
    notification_id = params.get("notification_id")
    action_name = params.get("action")
    args = params.get("args", {})
    if not isinstance(notification_id, str) or not notification_id:
        raise ValueError("Missing or invalid required field: notification_id")
    if not isinstance(action_name, str) or not action_name:
        raise ValueError("Missing or invalid required field: action")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("Field 'args' must be an object")

    with _pending_package_actions_lock:
        _prune_pending_package_actions()
        entry = _pending_package_actions.get(notification_id)
        if entry is None:
            return {
                "status": "refused",
                "reason": "unknown_or_expired_notification",
                "notification_id": notification_id,
            }
        allowed = entry.get("actions", {})
        if not isinstance(allowed, dict) or action_name not in allowed:
            return {
                "status": "refused",
                "reason": "action_not_allowed_for_notification",
                "notification_id": notification_id,
                "action": action_name,
            }

    registry = _get_package_action_registry()
    ctx = _build_action_context(action_name, params)
    query = json.dumps(args, ensure_ascii=False, sort_keys=True)
    try:
        result = registry.call(action_name, ctx, query=query)
    except ActionRegistryError as exc:
        return {
            "status": "refused",
            "reason": "unknown_action",
            "message": str(exc),
            "notification_id": notification_id,
            "action": action_name,
        }
    except Exception as exc:
        _log.warning("package action failed: %s", exc)
        return {
            "status": "failed",
            "reason": "action_failed",
            "message": str(exc),
            "notification_id": notification_id,
            "action": action_name,
        }

    response = {
        "status": "ok",
        "notification_id": notification_id,
        "action": action_name,
        "result": result,
    }
    if isinstance(result, dict) and isinstance(result.get("launch_viewer"), dict):
        response["launch_viewer"] = result["launch_viewer"]
    return response


def _build_package_timeline_event(
    *,
    command: str,
    tool_name: str | None,
    cwd: str,
    exit_code: int,
    stdout_tail: list,
    source: str,
    terminal_id: str,
    extra_metadata: dict[str, Any] | None = None,
    origin_event_id: str | None = None,
    trace_self: str | None = None,
) -> TimelineEvent:
    _stream = _get_stream(terminal_id) if _stream_manager is not None else None
    _acc = _stream.accumulator if _stream is not None else _ecc_accumulator
    block_name = _acc.current_block_name if _acc is not None else None
    metadata: dict[str, Any] = {
        "project_id": _project_id or "",
    }
    if block_name:
        metadata["block_name"] = block_name
        metadata["ecc_state"] = {"block_name": block_name}
    dqe_db_path = os.environ.get("TERMINUX_DQE_DB_PATH")
    if dqe_db_path:
        metadata["dqe_db_path"] = dqe_db_path
    # Observability carrier v1：origin_event_id 純 correlation metadata，
    # 不改變任何既有 metadata key 的語意。
    if origin_event_id is not None:
        metadata["origin_event_id"] = origin_event_id
    # 額外 metadata 僅補上不存在的欄位，保留 daemon 建立的 design metadata。
    for key, value in (extra_metadata or {}).items():
        metadata.setdefault(key, value)

    return TimelineEvent(
        event_id=f"pkg_{uuid4().hex}",
        # Observability carrier v1：trace_id 承接 client-declared trace_self
        # ——純 correlation metadata，不是 dispatch 判斷依據。
        trace_id=trace_self,
        command=command,
        argv=command.split(),
        cwd=cwd,
        tool_name=tool_name,
        user=_user_id or getpass.getuser(),
        host=local_nodename(),
        terminal_id=terminal_id,
        source=source,
        timestamp=str(time.time()),
        exit_code=exit_code,
        stdout_tail=[str(item) for item in stdout_tail],
        metadata=metadata,
    )


def _command_matches_loaded_package(command: str) -> bool:
    """以 package manifest `when.tools` 判斷 command 是否可能觸發 package。

    lineage.post_exec 的 `result.tool_name` 可能因 runfile/ledger/tool-signature 問題
    為 None；但 package dispatch 的 matching 實際上只看 command first token
    對 manifest when.tools。GUI drain 是否等待 wrapper package dispatch，應以
    package matching 規則為準。
    """
    parts = command.split()
    if not parts:
        return False
    first_token = parts[0]
    try:
        registry = _get_package_registry()
    except Exception as exc:
        _log.debug("package registry unavailable for command match: %s", exc)
        return False
    for package in registry.packages:
        tools = package.manifest.when_tools
        if tools and first_token in tools:
            return True
    return False


def _build_package_runtime_services(
    *, terminal_id: str, package_id: str | None = None,
) -> RuntimeServices:
    _stream = _get_stream(terminal_id) if _stream_manager is not None else None
    _acc = _stream.accumulator if _stream is not None else _ecc_accumulator
    session_id = _acc.session_id if _acc is not None else ""
    store = StoreService(
        _event_store,
        session_id=session_id,
        terminal_id=terminal_id,
        user_id=_user_id,
        project_id=_project_id,
        # `None` 維持既有 dispatch path 的行為：`DispatchEngine` 會為每個
        # package 再建一份帶 package_id 的 scoped store。
        package_id=package_id,
    )
    notify = NotifyService(async_sink=_append_package_pending_notification)
    # analysis task body 的呼叫會在 contextvar 裡看到自己的 budget；
    # 其餘呼叫拿到 None，與接線前完全等價。
    from terminux_engine.bounded_execution.analysis_tasks.budget import (
        get_current_task_budget,
    )

    llm = LlmService(router=_package_llm_router, budget=get_current_task_budget())
    # 只有在派送**已核准的 flow task step** 時，contextvar 內才
    # 會有 binding；其餘呼叫拿到 None，`prepare_variant`／`run_flow` 因此
    # 一律 fail closed。
    from terminux_engine.bounded_execution.analysis_tasks.flow_binding_context import (
        get_current_flow_context,
    )

    flow_context = get_current_flow_context()
    return RuntimeServices(
        store=store, notify=notify, llm=llm,
        flow_task_binding=flow_context.binding if flow_context else None,
        flow_execution_service=flow_context.execution if flow_context else None,
        flow_variant_writer=flow_context.variant_writer if flow_context else None,
        flow_repair_writer=(getattr(flow_context, "repair_writer", None)
                            if flow_context else None),
        # 正式的 scoped run input evidence reader。它只讀既有
        # ledger／task 事件／input manifest，不寫任何東西。
        run_input_evidence_reader=_build_run_input_evidence_reader(),
        request_context=_current_canonical_context,
        # 與 evidence bundle 共用同一個唯讀 Evidence Resolver。
        execution_evidence_resolver=getattr(
            _orchestration_service, "_execution_evidence_resolver", None),
        dqe_viewer_launcher=lambda payload: dict(payload),
    )


def _default_dqe_db_path() -> Path | None:
    """Evidence Resolver 讀 DQE 的預設位置；與 `DqeDbManager` 的第四、五層規則相同。

    `TERMINUX_DQE_DEFAULT_DB_POLICY=deny`（或設定值不合法）時不退回預設 DB。
    """
    from terminux_engine.dqe.db_policy import get_default_db_policy

    explicit = os.environ.get("TERMINUX_DQE_DB_PATH")
    if explicit:
        return Path(explicit).expanduser()
    try:
        if get_default_db_policy() == "deny":
            return None
    except Exception:  # noqa: BLE001 - 設定不合法就不讀預設 DB
        return None
    return Path.home() / ".terminux" / "dqe" / "dqe.sqlite"


def _build_execution_evidence_resolver(event_store: Any, ledger: Any, *,
                                       user_id: str) -> Any:
    """daemon 的唯讀 Evidence Resolver。

    ledger 查詢沿用既有的 caller 範圍（`get_run_for_scope`／`list_runs`）；沒有
    canonical request context 時不查 ledger，只依目前使用者的 receipt 判定。
    """
    from terminux_engine.evidence_integrity.evidence_resolver import (
        ExecutionEvidenceResolver,
    )

    def _ledger_lookup(run_id: str) -> Any:
        context = _current_canonical_context()
        if context is None:
            return None
        return ledger.get_run_for_scope(
            run_id, member_id=context.member_id, project_id=context.project_id)

    def _ledger_recent(limit: int) -> list[Any]:
        context = _current_canonical_context()
        if context is None:
            return []
        return ledger.list_runs(member_id=context.member_id,
                                project_id=context.project_id, limit=limit)

    return ExecutionEvidenceResolver(
        event_store=event_store, current_user=user_id,
        dqe_db_path=_default_dqe_db_path(),
        ledger_lookup=_ledger_lookup, ledger_recent=_ledger_recent)


def _current_canonical_context() -> Any:
    """目前這一次呼叫的 canonical scope。沒有就回 `None`（Package 端維持 unknown）。"""
    from terminux_engine.ipc.request_context import get_current_request_context

    try:
        return get_current_request_context()
    except Exception:  # noqa: BLE001
        return None


def _build_receipt_readers(event_store: Any, *, user_id: str,
                           project_id: str | None) -> tuple[Any, Any, Any]:
    """同一份 EventStore 上的三個唯讀 reader，契約各自不同：

    - rows reader：`run_id -> list[ReceiptRow]`，帶寫入者身分；**只**給 OrchestrationService
      當 caller 擁有權邊界（caller 的 `member_id` 必須等於每一列的 `user_id`）。
    - payload reader：`run_id -> list[dict]`，只含 daemon 身分（`user_id`）寫入的 payload；給既有
      以 dict 消費 receipt 的 flow 呼叫端（run input evidence、flow run evidence、package flow）。
      它不是授權邊界，不得注入 OrchestrationService。
    - timing path reader：單一 setup path 證據 record。

    沒有 EventStore 時三者都是 `None`，呼叫端行為與從未注入時相同。
    """
    if event_store is None:
        return None, None, None
    from terminux_engine.execution_receipts.wiring import EventStoreReceiptProjector
    from terminux_engine.timing_evidence.record_reader import EventStoreTimingPathReader

    projector = EventStoreReceiptProjector(
        event_store, session_id="orchestration-daemon", user_id=user_id, project_id=project_id)
    timing_reader = EventStoreTimingPathReader(event_store, user_id=user_id).query_by_run_id
    return projector.query_rows_by_run_id, projector.query_payloads_by_run_id, timing_reader


_run_input_evidence_reader: Any = None


def _build_run_input_evidence_reader() -> Any:
    """單一實例；缺任何一個來源就回 `None`（Package 端維持 unknown）。"""
    global _run_input_evidence_reader
    if _run_input_evidence_reader is not None:
        return _run_input_evidence_reader
    if _event_store is None or _orchestration_service is None:
        return None
    from terminux_engine.bounded_execution.analysis_tasks.run_input_evidence import (
        RunInputEvidenceReader,
    )

    # dict payload 契約（daemon 身分寫入的列），不是 OrchestrationService 的帶身分列 reader。
    receipt_reader = _receipt_payload_reader
    ledger = getattr(_orchestration_service, "_ledger", None)
    if ledger is None or receipt_reader is None:
        return None

    def _baseline_manifest(task_id: str, manifest_sha256: str) -> str | None:
        """baseline manifest 的引用只能來自**這個 task** 自己的 profile snapshot。"""
        import hashlib

        service = _analysis_task_service
        profile = service.profile_for_task(task_id) if service is not None else None
        if profile is None:
            return None
        path = Path(profile.input_manifest_path)
        if not path.is_file():
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest_sha256:
            return None
        return str(path)

    _run_input_evidence_reader = RunInputEvidenceReader(
        ledger=ledger, event_store=_event_store, receipt_reader=receipt_reader,
        baseline_manifest_provider=_baseline_manifest)
    return _run_input_evidence_reader


def _register_package_dispatch_inflight(terminal_id: str) -> threading.Event:
    event = threading.Event()
    with _package_dispatch_inflight_lock:
        _package_dispatch_inflight.setdefault(terminal_id, []).append(event)
    return event


def _finish_package_dispatch_inflight(terminal_id: str, event: threading.Event) -> None:
    with _package_dispatch_inflight_lock:
        events = _package_dispatch_inflight.get(terminal_id)
        if events is not None:
            try:
                events.remove(event)
            except ValueError:
                pass
            if not events:
                _package_dispatch_inflight.pop(terminal_id, None)
    event.set()


def _has_pending_async_notification() -> bool:
    with _pending_async_lock:
        return bool(_pending_async_notifications)


def _wait_for_package_dispatch_inflight(
    terminal_id: str | None,
    *,
    wait_for_late_start: bool = False,
) -> None:
    """GUI drain 前短暫等待同 terminal 的 package dispatch 完成。

    wrapper shell.post_exec 會非同步跑 package after-phase；GUI lineage.post_exec
    可能在 package 還沒 append notification 前就先 drain；更糟的是 GUI
    lineage.post_exec 可能比 wrapper shell.post_exec 更早到 daemon，此時 in-flight
    尚未登記。對已識別出 EDA tool 的 GUI post_exec，允許短暫等待 late-start
    wrapper dispatch，避免第一個 dc_shell notification 延後到下一個 command 才顯示。
    """
    tid = terminal_id or "default"
    deadline = time.monotonic() + _PACKAGE_DISPATCH_DRAIN_WAIT_SEC
    saw_inflight = False
    while True:
        if _has_pending_async_notification():
            return
        with _package_dispatch_inflight_lock:
            events = list(_package_dispatch_inflight.get(tid, ()))
        if events:
            saw_inflight = True
        elif not wait_for_late_start or saw_inflight:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if not events:
            time.sleep(min(remaining, 0.05))
            continue
        for event in events:
            event.wait(min(remaining, 0.05))


def _schedule_package_dispatch(
    *,
    command: str,
    tool_name: str | None,
    cwd: str,
    exit_code: int,
    stdout_tail: list,
    source: str,
    terminal_id: str,
    extra_metadata: dict[str, Any] | None = None,
    origin_event_id: str | None = None,
    trace_root: str | None = None,
    trace_self: str | None = None,
    trace_parent: str | None = None,
    user_activation_lease: _PinnedUserActivation | None = None,
    user_packages_only: bool = False,
) -> None:
    """非阻塞 schedule generic package post-exec dispatch。

    此路徑只負責 package runtime v1 的 after phase；不碰 before/main/action。
    任何錯誤都 warn-only，不能影響 wrapper / shell.post_exec caller。

    `tool_name` 必須是呼叫端已經完成身分判斷後的最終值（`lineage.post_exec`
    傳入 `identify_tool()` 的既有結果，行為完全不變；`shell.post_exec` 傳入
    host tool identity validation 後的 canonical id，見
    `tool_identity/host_validation.py` 與 `_run_post_exec_pipeline` 的
    `package_dispatch_tool_name` 參數）。本函式本身不做任何身分推導或驗證，
    只依 `tool_name` 是否為 falsy 決定要不要 dispatch——維持這個函式在
    catalog host validation 導入前的原始簡單契約，避免 `lineage.post_exec`
    被意外波及（`lineage.post_exec` 不在重構範圍內）。

    `origin_event_id`/`trace_*`（observability carrier v1）純粹讀取轉送，
    不影響上面這段既有 gate 判斷任何一步。

    `user_activation_lease`：呼叫端已取得、並用來驗證工具身分的 activation。
    真的排入 dispatch 時，lease 的所有權轉給 dispatch 執行緒（執行結束後釋放），
    確保這次 dispatch 使用與身分驗證相同的 package／catalog 組合；沒有排入時
    由呼叫端釋放。未提供時（`lineage.post_exec`）沿用既有的 lease 方式。

    `user_packages_only`：使用者工具的事件只交給 user package（見
    `_dispatch_registry_with_user_packages`）。
    """
    if not tool_name:
        return

    if _package_dispatch_disabled():
        global _package_dispatch_disabled_logged
        if not _package_dispatch_disabled_logged:
            _package_dispatch_disabled_logged = True
            _log.info("Package dispatch disabled via TERMINUX_DISABLE_PACKAGES")
        return

    if origin_event_id is not None:
        _emit_event("package.dispatch.scheduled", {
            "origin_event_id": origin_event_id,
        }, terminal_id=terminal_id,
            trace_root=trace_root, trace_self=trace_self, trace_parent=trace_parent,
        )

    dispatch_done = _register_package_dispatch_inflight(terminal_id)
    if user_activation_lease is not None:
        user_activation_lease.transferred = True

    @contextmanager
    def _dispatch_activation() -> Iterator[UserPackageActivation | None]:
        if user_activation_lease is None:
            with _user_package_dispatch_lease() as leased:
                yield leased
            return
        try:
            yield (None if user_packages_disabled() else user_activation_lease.activation)
        finally:
            user_activation_lease.release()

    def _runner() -> None:
        try:
            import asyncio as _asyncio

            if origin_event_id is not None:
                _emit_event("package.dispatch.started", {
                    "origin_event_id": origin_event_id,
                }, terminal_id=terminal_id,
                    trace_root=trace_root, trace_self=trace_self, trace_parent=trace_parent,
                )

            # 整個 dispatch 固定使用同一份 user package activation；期間 reload
            # 不會讓這次 dispatch 改讀新的 generation，舊 generation 也不會被清理。
            with _dispatch_activation() as user_activation:
                registry = _dispatch_registry_with_user_packages(
                    user_activation, user_packages_only=user_packages_only)
                event = _build_package_timeline_event(
                    command=command,
                    tool_name=tool_name,
                    cwd=cwd,
                    exit_code=exit_code,
                    stdout_tail=stdout_tail,
                    source=source,
                    terminal_id=terminal_id,
                    extra_metadata=extra_metadata,
                    origin_event_id=origin_event_id,
                    trace_self=trace_self,
                )
                dispatcher = PackageDispatcher(registry)
                engine = DispatchEngine(
                    dispatcher,
                    mode=EngineMode.IN_DAEMON,
                    services=_build_package_runtime_services(terminal_id=terminal_id),
                )
                outcome = _asyncio.run(engine.dispatch(event))
                if origin_event_id is not None:
                    for _pkg_outcome in outcome.packages:
                        _emit_event("package.dispatch.finished", {
                            "origin_event_id": origin_event_id,
                            "package_id": _pkg_outcome.package_id,
                            "status": _pkg_outcome.status,
                        }, terminal_id=terminal_id,
                            trace_root=trace_root, trace_self=trace_self, trace_parent=trace_parent,
                        )
        except Exception as exc:
            _log.warning("package dispatch failed: %s", exc)
        finally:
            if user_activation_lease is not None:
                user_activation_lease.release()  # 冪等；確保例外路徑也歸還
            with _package_dispatch_threads_lock:
                _package_dispatch_threads.discard(threading.current_thread())
            _finish_package_dispatch_inflight(terminal_id, dispatch_done)

    t = threading.Thread(target=_runner, name="package-dispatch", daemon=True)
    with _package_dispatch_threads_lock:
        _package_dispatch_threads.add(t)
    try:
        t.start()
    except Exception as exc:
        with _package_dispatch_threads_lock:
            _package_dispatch_threads.discard(t)
        _finish_package_dispatch_inflight(terminal_id, dispatch_done)
        if user_activation_lease is not None:
            user_activation_lease.release()
        _log.warning("package dispatch thread spawn failed: %s", exc)


def _handle_dqe_viewer_launch_query(query: str, ask_id: str) -> dict | None:
    """處理使用者明確要求開啟 DQE Viewer 的 chat 指令，不交給知識庫問答。

    只接受同時含 DQE Viewer 名稱與開啟動詞的請求，避免「DQE Viewer 是什麼」
    這類資訊問題被誤判為 side effect。daemon 只回傳啟動參數，
    executable 驗證與 process 啟動由發出請求的 client 負責。
    """
    normalized = query.lower()
    viewer_names = ("dqe viewer", "dqe_viewer", "dqeviewer")
    launch_verbs = (
        "開啟", "打開", "打开", "啟動", "启动", "顯示", "显示",
        "open", "launch", "start", "show",
    )
    if not any(name in normalized for name in viewer_names):
        return None
    if not any(verb in normalized for verb in launch_verbs):
        return None

    result: dict[str, Any] = {}
    try:
        from terminux_engine.dqe import viewer_launcher

        result = viewer_launcher.launch_viewer()
    except Exception as exc:
        _log.warning("DQE Viewer launch query failed: %s", exc)
        answer = "無法開啟 DQE Viewer。請確認 Viewer 的部署設定後再試一次。"
    else:
        if result.get("status") == "requested":
            answer = "已將 DQE Viewer 啟動請求交給 client。"
        else:
            message = result.get("message")
            if isinstance(message, str) and message:
                answer = f"無法開啟 DQE Viewer：{message}"
            else:
                answer = "無法開啟 DQE Viewer。"

    response: dict = {"answer": answer, "query": query}
    if isinstance(result, dict) and isinstance(result.get("launch_viewer"), dict):
        response["launch_viewer"] = result["launch_viewer"]
    if ask_id:
        response["ask_id"] = ask_id
    return response


def _init_log_sweeper() -> None:
    """daemon 啟動時初始化 LogSweeper（在 _init_ecc_inner 內呼叫）。"""
    global _log_sweeper
    try:
        from terminux_engine.lineage.log_sweeper import LogSweeper
        jsonl_path = Path.home() / ".terminux" / "lineage.jsonl"
        _log_sweeper = LogSweeper(jsonl_path=jsonl_path)
        _log_sweeper.start()
        _log.info("LogSweeper started: %s", jsonl_path)
    except Exception as exc:
        _log.warning("LogSweeper initialization failed (non-fatal): %s", exc)
        _log_sweeper = None


class _ProactiveNotifier:
    """把 proactive intelligence 的 NotificationRequest 送進**既有**的 pending-notification sink。

    不新建任何通知通道，也不附任何 command／action button——proactive intelligence 的通知
    是 non-authoritative read-only intelligence。
    """

    def emit(self, request: Any) -> None:
        payload = {
            "type": "notification",
            "level": getattr(request, "level", "info"),
            "title": getattr(request, "title", ""),
            "message": getattr(request, "message", ""),
            # 刻意固定為空：proactive intelligence 不提供可執行的動作。
            "actions": [],
            "source": "proactive_intelligence",
            "authoritative": False,
        }
        _append_package_pending_notification(payload)


def _init_proactive_runner() -> None:
    """daemon 啟動時掛上 proactive runner（在 _init_ecc_inner 內呼叫）。

    只有 production gate 為 enabled 時才註冊，而且**恰好註冊一次**。
    任何依賴缺失一律不註冊（fail closed，不降級成半套）。
    Runner 拿到的是 `ProactiveDeps`——裡面沒有 ActionRegistry、
    ProposalRegistry、Kernel、executor 或 shell runner，這一點由
    `ProactiveDeps` 的欄位定義與欄位集合的測試守住。
    """
    global _proactive_runner
    if _proactive_runner is not None:
        return
    try:
        from terminux_engine.proactive.activation import production_proactive_gate

        gate = production_proactive_gate()
        if not gate.enabled:
            _log.info("Proactive runner not registered: %s", gate.reason)
            return
        # `_agent_router` 是 module-level 的 LLMRouter（`_init_ecc_inner` 內
        # 設定）；`_llm_router_inst` 只是那個函式的區域變數，這裡拿不到。
        if _event_store is None or _agent_router is None:
            _log.info("Proactive runner not registered: event store or router unavailable")
            return

        from terminux_engine.packages.orfs_run_intelligence.proactive_provider import (
            OrfsProactiveProvider,
        )
        from terminux_engine.packages.orfs_run_intelligence.situation_prompt import (
            OUTPUT_BUDGET_TOKENS,
        )
        from terminux_engine.package_runtime.services import LlmService, StoreService
        from terminux_engine.proactive.budget import derive_cycle_limits
        from terminux_engine.proactive.composition import (
            PRODUCTION_MAX_CANDIDATES,
            build_production_coordinator,
        )
        from terminux_engine.proactive.runner import ProactiveRunner

        # Package 自己決定要看哪個 DQE DB；Common 不知道 ORFS 的存在。
        store = StoreService(_event_store, session_id="proactive",
                             user_id=_user_id or "", project_id=_project_id,
                             package_id="orfs_run_intelligence")
        # No-backfill：activation boundary 設在**啟用當下**，
        # 因此 proactive intelligence 只分析啟用之後才記錄的 evaluation，不會回頭把歷史 run
        # 全部掃一遍。boundary 不是 gate，也不讀環境變數——它就是「這個
        # daemon 從什麼時候開始看」這個事實。
        activation_boundary = time.strftime("%Y%m%d_%H%M%S")
        provider = OrfsProactiveProvider(
            dqe_db_path=os.path.expanduser("~/.terminux/dqe/dqe.sqlite"),
            llm=LlmService(_agent_router), store=store,
            activation_boundary=activation_boundary)
        # per-cycle provider budget。這裡是唯一知道 ORFS 常數的地方——
        # Common 不得認識任何 package，所以推導在呼叫端做、結果才傳進去。
        #
        # `_MAX_ASSESSMENT_CALLS_PER_CANDIDATE = 2`：situation prompt 是
        # initial ＋ 最多一次 bounded repair（`situation_prompt.run_prompt()`），
        # 兩次共用同一份 cycle 預算。
        #
        # 這是**天花板不是節流閥**：預算若砍掉正常候選，該筆發現會延到下一個
        # cycle 才處理（見 coordinator 的 claim/budget 順序），實務上限值取結構
        # 上不可能超過的量，只擋 prompt／回應暴脹與 usage 觀測不到的情況。
        _MAX_ASSESSMENT_CALLS_PER_CANDIDATE = 2
        budget_limits = derive_cycle_limits(
            max_candidates=PRODUCTION_MAX_CANDIDATES,
            max_calls_per_candidate=_MAX_ASSESSMENT_CALLS_PER_CANDIDATE,
            requested_output_tokens_per_call=OUTPUT_BUDGET_TOKENS,
        )
        coordinator = build_production_coordinator(
            provider=provider,
            record_store=_EventStoreRecordView(
                _event_store, session_id="proactive",
                user_id=_user_id or "", project_id=_project_id),
            notifier=_ProactiveNotifier(),
            budget_limits=budget_limits,
        )
        _proactive_runner = ProactiveRunner(coordinator)
        _proactive_runner.start()
        _log.info("Proactive runner started (%s, boundary=%s, budget=%s)",
                  gate.reason, activation_boundary, budget_limits.to_json_dict())
    except Exception as exc:
        _log.warning("Proactive runner initialization failed (non-fatal): %s", exc)
        _proactive_runner = None


def _shutdown_proactive_runner() -> None:
    """daemon 關閉時呼叫；graceful stop + join，不留 thread。"""
    global _proactive_runner
    if _proactive_runner is not None:
        try:
            _proactive_runner.stop()
        except Exception as exc:
            _log.warning("Proactive runner stop failed: %s", exc)
        _proactive_runner = None


def _shutdown_log_sweeper() -> None:
    """daemon 關閉時呼叫（與 _shutdown_jsonl_ingestor 對稱）。"""
    global _log_sweeper
    if _log_sweeper is not None:
        try:
            _log_sweeper.stop()
        except Exception as exc:
            _log.warning("LogSweeper stop failed: %s", exc)
        _log_sweeper = None


# ─── per-user gap detection helpers ─────────────────────────────

def _query_latest_command_event(
    event_store: EventStore,
    user_id: str,
) -> SessionEvent | None:
    """最近 30 天內最新的 command event（per-user，跨 terminal）。"""
    now = time.time()
    lookback_30d = now - 30 * 86400.0
    events = event_store.query_events_in_range(
        user_id=user_id,
        start_ts=lookback_30d,
        end_ts=now,
        event_types=["command"],
    )
    return events[-1] if events else None


def _compute_immediate_period_bounds(
    event_store: EventStore,
    user_id: str,
    terminal_id: str,
    anchor_ts: float,
) -> tuple[float, float] | None:
    """找 anchor_ts 前 24hr 的最後一個連續工作時段邊界。"""
    _LOOKBACK_SEC = 86400.0  # 24hr
    events = event_store.query_events_in_range(
        user_id=user_id,
        start_ts=anchor_ts - _LOOKBACK_SEC,
        end_ts=anchor_ts + 1.0,  # +1 以 inclusive 涵蓋 anchor 本身
        terminal_id=terminal_id,
    )
    # 過濾 digest events（對齊 run_daily_cron pattern）
    _DIGEST_TYPES = {"period_digest", "daily_digest"}
    filtered = [e for e in events if e.event_type not in _DIGEST_TYPES]
    periods = split_into_periods(filtered)
    if not periods:
        return None
    last_period = periods[-1]
    return (last_period[0].timestamp, last_period[-1].timestamp)


def _check_gap_and_trigger_immediate_digest(
    event_store: EventStore,
    dreamer: Dreamer,
    user_id: str,
    now_epoch: float,
) -> None:
    """per-user gap detection；gap > 1hr → trigger last-active-terminal 的 immediate digest。

    Non-blocking：任何 failure（無 last command、無 lookback events、
    已有 immediate digest、dreamer busy）靜默 skip。
    """
    try:
        last_cmd = _query_latest_command_event(event_store, user_id)
    except Exception as exc:
        _log.warning("gap detection query failed (non-fatal): %s", exc)
        return

    if last_cmd is None:
        return  # 首次 command，無 gap 可言

    gap = now_epoch - last_cmd.timestamp
    if gap <= ACTIVITY_GAP_THRESHOLD_SEC:
        return  # 無 gap

    # digest 對象是 last_cmd 所在的 terminal（不是 current command 的 terminal）
    last_terminal_id = last_cmd.terminal_id
    if not last_terminal_id:
        return  # defensive

    bounds = _compute_immediate_period_bounds(
        event_store=event_store,
        user_id=user_id,
        terminal_id=last_terminal_id,
        anchor_ts=last_cmd.timestamp,
    )
    if bounds is None:
        return  # 24hr lookback 無 event

    period_start, period_end = bounds

    # 重入保護：同 period 已有 immediate digest 就 skip
    try:
        existing = event_store.find_period_digests_by_range(
            user_id=user_id,
            period_start=period_start,
            period_end=period_end,
        )
    except Exception as exc:
        _log.warning("gap detection existing check failed (non-fatal): %s", exc)
        return

    if any(e.payload.get("digest_type") == "immediate" for e in existing):
        return  # 已有 immediate digest，skip

    future = dreamer.submit_immediate_digest(
        user_id=user_id,
        terminal_id=last_terminal_id,
        period_start=period_start,
        period_end=period_end,
    )
    if future is None:
        return  # dreamer 已 close

    _log.info(
        "Gap trigger submitted: user=%s terminal=%s gap=%.1fs period=%.1f~%.1f",
        user_id, last_terminal_id, gap, period_start, period_end,
    )


# ─── startup backfill helpers ───────────────────────────────────

def _query_latest_daily_digest_date(
    event_store: EventStore,
    user_id: str,
) -> date | None:
    """最近 daily_digest event 的日期（從 payload['date'] 解析）。"""
    now = time.time()
    lookback_1y = now - 365 * 86400.0
    events = event_store.query_events_in_range(
        user_id=user_id,
        start_ts=lookback_1y,
        end_ts=now,
        event_types=["daily_digest"],
    )
    if not events:
        return None
    last_event = events[-1]
    date_str = last_event.payload.get("date")
    if not date_str:
        return None
    return date.fromisoformat(date_str)


def _startup_backfill_safety_net(
    event_store: EventStore,
    llm_router: Any,
    user_id: str,
    now_epoch: float,
) -> None:
    """daemon startup backfill：補跑 cron 漏跑的 daily_digest。

    Non-blocking：runs in a daemon thread so startup is not delayed.
    Silently skips on any failure.
    """
    if _startup_backfill_stop_event.is_set():
        return

    try:
        last_digest_date = _query_latest_daily_digest_date(event_store, user_id)
    except Exception as exc:
        _log.warning("startup backfill query failed (non-fatal): %s", exc)
        return

    if _startup_backfill_stop_event.is_set():
        return

    if last_digest_date is None:
        _log.info("startup backfill: no previous daily_digest, skipping")
        return

    today = datetime.fromtimestamp(now_epoch).date()
    gap_days = (today - last_digest_date).days

    if gap_days < 2:
        return  # 昨天已跑或今天會跑，不用 backfill

    missing_dates = [
        (last_digest_date + timedelta(days=i)).isoformat()
        for i in range(1, gap_days)
    ]

    _log.info(
        "startup backfill: last daily_digest=%s, today=%s, backfilling %d days",
        last_digest_date.isoformat(), today.isoformat(), len(missing_dates),
    )

    for date_str in missing_dates:
        if _startup_backfill_stop_event.is_set():
            _log.info("startup backfill stopped early by shutdown signal")
            return
        try:
            result = run_daily_cron(
                event_store=event_store,
                llm_router=llm_router,
                user_id=user_id,
                date_str=date_str,
                dreamer=None,  # 讓 run_daily_cron 自建 Dreamer
            )
            _log.info(
                "startup backfill [%s]: periods=%d appended=%d upgraded=%d daily=%s errors=%d",
                date_str,
                result.periods_processed,
                result.full_digests_appended,
                result.immediate_digests_upgraded,
                "ok" if result.daily_digest_created else "FAIL",
                len(result.errors),
            )
        except Exception as exc:
            _log.warning("startup backfill [%s] failed: %s", date_str, exc)
            # 繼續下一天，不中斷


# ─── Env var names ────────────────────────────────────────────────────────────
_ENV_LLM_BACKEND                   = "TERMINUX_LLM_BACKEND"       # "llama_cpp" | "gemini" | "openai_compatible"
_ENV_LLM_MODEL_PATH                = "TERMINUX_LLM_MODEL_PATH"
_ENV_LLM_DISABLE_THINKING          = "TERMINUX_LLM_DISABLE_THINKING"  # "1" 啟用（Qwen3 需）
_ENV_GEMINI_API_KEY                = "TERMINUX_GEMINI_API_KEY"
_ENV_GEMINI_MODEL                  = "TERMINUX_GEMINI_MODEL"
# openai_compatible（DeepSeek / OpenAI / Groq / vLLM / Together）
_ENV_OPENAI_API_KEY                = "TERMINUX_OPENAI_API_KEY"
_ENV_OPENAI_MODEL                  = "TERMINUX_OPENAI_MODEL"
_ENV_OPENAI_BASE_URL               = "TERMINUX_OPENAI_BASE_URL"
_ENV_ENABLE_LINEAGE_EXPLAINER      = "TERMINUX_ENABLE_LINEAGE_EXPLAINER_RUNTIME"
_ENV_ENABLE_INTENT_FALLBACK        = "TERMINUX_ENABLE_INTENT_FALLBACK_RUNTIME"


def _get_runtime_provider() -> LlamaCppCompletionProvider | None:
    """
    回傳 module-level singleton（第一次呼叫時才初始化）。

    backend 路由：
    - "llama_cpp" → LlamaCppCompletionProvider
    - "gemini"    → GeminiCompletionProvider
    - 其他或未設定 → None

    先前初始化失敗 → None（fail-open）
    先前初始化成功 → 回傳既有 instance
    """
    global _runtime_provider, _runtime_provider_failed

    if _runtime_provider_failed:
        return None

    if _runtime_provider is not None:
        return _runtime_provider

    backend = os.environ.get(_ENV_LLM_BACKEND, "").strip()

    if backend == "llama_cpp":
        model_path = os.environ.get(_ENV_LLM_MODEL_PATH, "").strip()
        if not model_path:
            return None
        disable_thinking = os.environ.get(_ENV_LLM_DISABLE_THINKING, "").strip() == "1"
        try:
            config = LlamaCppProviderConfig(
                model_path=model_path,
                disable_thinking=disable_thinking,
            )
            _runtime_provider = LlamaCppCompletionProvider(config)
            return _runtime_provider
        except Exception as exc:
            _log.warning("Failed to create LlamaCpp runtime provider: %s", exc)
            _runtime_provider_failed = True
            return None

    elif backend == "gemini":
        from terminux_engine.llm.task_scope import profile_containment_active

        if profile_containment_active():
            # Gemini provider 沒有 TaskScope 最終 Gate。
            _log.info("TERM_PROFILE_CONTAINMENT disabled=gemini_runtime_provider")
            _runtime_provider_failed = True
            return None
        # 憑證優先來自 daemon 專屬的 0600 設定檔（`llm.credential_file`），
        # 環境變數只是相容性回退。這條 daemon 自己的 provider bootstrap 原本
        # **只**讀環境變數，於是「把 key 移出環境」會讓 provider 直接不啟用。
        from terminux_engine.llm.credential_file import secret as _credential_secret

        api_key = (_credential_secret(_ENV_GEMINI_API_KEY) or "").strip()
        # 未明確指定 model 時使用 production default
        # （`llm.router_config.DEFAULT_GEMINI_MODEL`），與 `_config_from_env()`
        # 一致。明確設定的 env 永遠優先；這裡不做多 model fallback。
        # 修正前這條路徑要求 env 必須同時給 API_KEY 與 MODEL，等於讓 daemon
        # 完全繞過 default，只要沒設 MODEL 就整個 Agent 降級成 deterministic
        # fallback（`openai_compatible` 分支本來就已經有 default，Gemini 沒有）。
        from terminux_engine.llm.router_config import DEFAULT_GEMINI_MODEL

        model = os.environ.get(_ENV_GEMINI_MODEL, "").strip() or DEFAULT_GEMINI_MODEL
        if not api_key:
            _log.warning("GEMINI backend: missing API_KEY")
            return None
        try:
            from terminux_engine.llm.gemini_provider import (
                GeminiCompletionProvider,
                GeminiProviderConfig,
            )
            _runtime_provider = GeminiCompletionProvider(
                GeminiProviderConfig(model=model, api_key=api_key)
            )
            return _runtime_provider
        except Exception as exc:
            _log.warning("Failed to create Gemini runtime provider: %s", exc)
            _runtime_provider_failed = True
            return None

    elif backend == "openai_compatible":
        # 同上——key 與 base_url 都先問憑證檔，再回退環境變數。
        from terminux_engine.llm.credential_file import secret as _credential_secret

        api_key = (_credential_secret(_ENV_OPENAI_API_KEY) or "").strip()
        model = os.environ.get(_ENV_OPENAI_MODEL, "").strip() or "deepseek-chat"
        base_url = (
            (_credential_secret(_ENV_OPENAI_BASE_URL) or "").strip()
            or "https://api.deepseek.com"
        )
        if not api_key:
            _log.warning(
                "openai_compatible backend: %s not set", _ENV_OPENAI_API_KEY
            )
            return None
        try:
            from terminux_engine.llm.openai_compatible_provider import (
                OpenAICompatibleProvider, OpenAICompatibleProviderConfig,
                configured_timeout_sec,
            )
            _runtime_provider = OpenAICompatibleProvider(
                OpenAICompatibleProviderConfig(
                    api_key=api_key,
                    model=model,
                    base_url=base_url,
                    timeout_sec=configured_timeout_sec(),
                    enforce_hard_timeout=True,
                )
            )
            _log.info(
                "openai_compatible runtime provider initialized "
                "(model=%s base_url=%s)",
                model, base_url,
            )
            return _runtime_provider
        except Exception as exc:
            _log.warning(
                "Failed to create openai_compatible runtime provider: %s", exc
            )
            _runtime_provider_failed = True
            return None

    else:
        return None


# ─── Async Agent helpers ──────────────────────────────


def _get_ecc_snapshot() -> dict | None:
    """CommandBuffer flush 時用，給 async triage prompt 灌 ECC 狀態。

    重用 get_current_ecc tool 的 executor，行為與 chat_query 路徑一致。
    """
    if _ecc_accumulator is None:
        return None
    try:
        from terminux_engine.llm.tools.get_current_ecc import create_tool

        snapshot = create_tool(_ecc_accumulator)()
        return snapshot if isinstance(snapshot, dict) else None
    except Exception as e:
        _log.warning("_get_ecc_snapshot failed: %s", e)
        return None


def _get_recent_events() -> list[dict] | None:
    """撈當前 user 最近 5 條 event 給 async triage prompt。"""
    if _event_store is None or not _user_id:
        return None
    try:
        events = _event_store.query(user_id=_user_id, order="desc", limit=5)
    except Exception as e:
        _log.warning("_get_recent_events query failed: %s", e)
        return None
    out: list[dict] = []
    for ev in events:
        payload = getattr(ev, "payload", None) or {}
        summary = json.dumps(payload, ensure_ascii=False, default=str)[:160]
        out.append(
            {
                "event_type": getattr(ev, "event_type", ""),
                "timestamp": getattr(ev, "timestamp", 0.0),
                "payload_summary": summary,
            }
        )
    return out


_DEFAULT_TOOL_SIGS_PATH = bundled_knowledge_path("tool_signatures.yaml")


def _is_eda_tool(command: str) -> bool:
    """CommandBuffer acceleration trigger 用。失敗時保守回 False。"""
    if not command or not command.strip():
        return False
    sig_path = (
        os.environ.get("TERMINUX_TOOL_SIGNATURES_PATH") or str(_DEFAULT_TOOL_SIGS_PATH)
    )
    if not Path(sig_path).exists():
        return False
    try:
        from terminux_engine.lineage.tool_identifier import identify_tool

        return identify_tool(command, sig_path) is not None
    except Exception as e:
        _log.debug("_is_eda_tool identify failed (non-fatal): %s", e)
        return False


def _build_profile() -> dict | None:
    """get_user_profile tool 的 loader。失敗時回 None。"""
    if _event_store is None or not _user_id:
        return None
    try:
        from terminux_engine.ecc.profile import build_enriched_profile

        profile = build_enriched_profile(_event_store, _user_id)
        return profile.model_dump()
    except Exception as e:
        _log.warning("_build_profile failed: %s", e)
        return None


def _on_command_buffer_flush(batch: Any) -> None:
    """CommandBuffer flush callback：組 async triage prompt + submit AgentTask。"""
    if _agent_task_queue is None:
        _log.debug("CommandBuffer flush: agent_task_queue not ready, dropping batch")
        return
    try:
        from terminux_engine.llm.agent_models import AgentTask
        from terminux_engine.llm.agent_prompt import (
            build_async_triage_prompt,
            build_async_triage_system_prompt,
        )

        prompt = build_async_triage_prompt(batch)
        task = AgentTask(
            task_id=f"async_{batch.batch_id}",
            query=prompt,
            system_context=build_async_triage_system_prompt(
                load_preferred_language()
            ),
            trigger_source="command_buffer",
            created_at_epoch=batch.flushed_at,
        )
        _agent_task_queue.submit(task)
    except Exception as e:
        _log.warning("_on_command_buffer_flush failed (non-fatal): %s", e)


def _on_async_agent_result(task: Any, result: Any) -> None:
    """AgentTaskQueue worker 完成 callback：寫 event + buffered-attach。

    - result.no_action=True → 完全跳過（不存 event、不掛 pending）
    - result.error → 只 log，不投遞
    - result.answer 非空 → event_store.append(agent_analysis) + pending list 加一筆
    """
    if result is None:
        return
    if getattr(result, "no_action", False):
        return
    err = getattr(result, "error", None)
    if err:
        _log.warning("Async Agent task %s returned error: %s", task.task_id, err)
        return
    answer = getattr(result, "answer", None)
    if not answer:
        return

    evidence = getattr(result, "evidence", None) or []
    payload = {
        "trigger": getattr(task, "trigger_source", "command_buffer"),
        "task_id": getattr(task, "task_id", ""),
        "answer": answer,
        "evidence_count": len(evidence),
        "evidence": [
            {
                "tool_name": getattr(e, "tool_name", ""),
                "arguments": getattr(e, "arguments", {}),
                "success": getattr(e, "success", False),
            }
            for e in evidence[:10]
        ],
        "rounds": getattr(result, "rounds", 0),
        "elapsed_sec": getattr(result, "elapsed_sec", 0.0),
    }

    # 1) Event store
    _emit_event("agent_analysis", payload)

    # 2) 留給各 GUI 的獨立 cursor 讀取。
    _append_async_notification(
        {
            "answer": answer,
            "trigger": payload["trigger"],
            "task_id": payload["task_id"],
            "evidence_count": payload["evidence_count"],
            "elapsed_sec": payload["elapsed_sec"],
        }
    )


def _append_async_notification(payload: dict) -> None:
    """在同一把鎖內分配單調序號並保存通知。"""
    global _pending_async_seq
    with _pending_async_lock:
        _pending_async_seq += 1
        _pending_async_notifications.append((_pending_async_seq, dict(payload)))


def _append_package_pending_notification(payload: Any) -> None:
    """Generic package NotifyService 的 async_sink。

    通知只由 notify.poll 依各 GUI cursor 讀取。
    """
    if not payload:
        return
    try:
        if isinstance(payload, dict):
            item = dict(payload)
            if item.get("type") == "notification":
                _register_pending_package_notification(item)
        else:
            item = {
                "answer": str(payload),
                "trigger": "package",
                "task_id": "",
                "evidence_count": 0,
                "elapsed_sec": 0.0,
            }
        _append_async_notification(item)
    except Exception as exc:
        _log.warning("package pending notification append failed: %s", exc)


# ─── prompt context helpers ────────────────────────────


def _get_session_history_summary() -> list[dict] | None:
    """撈當前 user 最近 3 個 session 的簡短摘要，給 chat prompt 用。

    比 list_sessions tool 更精簡：只取 session_id / stage / corners_done /
    command_count，省 prompt token。失敗或無資料 → None。
    """
    if _event_store is None or not _user_id:
        return None
    try:
        sids = _event_store.get_session_ids(user_id=_user_id, n=3)
    except Exception as e:
        _log.warning("_get_session_history_summary get_session_ids failed: %s", e)
        return None
    if not sids:
        return None

    out: list[dict] = []
    for sid in sids:
        try:
            events = _event_store.query(session_id=sid, order="asc")
        except Exception:
            continue
        if not events:
            continue
        stage: str | None = None
        cmd_count = 0
        corners_passed = 0
        corners_failed = 0
        corners_total = 0
        created_at = float(getattr(events[0], "timestamp", 0.0) or 0.0)
        for ev in events:
            et = getattr(ev, "event_type", "")
            payload = getattr(ev, "payload", None) or {}
            if et == "stage_change":
                to_stage = payload.get("to_stage")
                if to_stage:
                    stage = to_stage
            elif et == "command":
                cmd_count += 1
            elif et == "corner_update":
                corners_total += 1
                status = payload.get("status", "")
                if status == "pass":
                    corners_passed += 1
                elif status == "fail":
                    corners_failed += 1
        out.append(
            {
                "session_id": sid,
                "last_stage_id": stage,
                "created_at": created_at,
                "corners_passed": corners_passed,
                "corners_failed": corners_failed,
                "corners_total": corners_total,
                "command_count": cmd_count,
            }
        )
    return out or None


def _get_user_profile_dict() -> dict | None:
    """既有 _build_profile 的 alias， 供 prompt 路徑使用。"""
    return _build_profile()


def _get_team_summary() -> str | None:
    """從 _team_snapshot 取文字摘要：每個 member 一行（user / block / corners / wns）。"""
    if _team_snapshot is None:
        return None
    try:
        members = list(getattr(_team_snapshot, "members", []) or [])
    except Exception as e:
        _log.warning("_get_team_summary read failed: %s", e)
        return None
    if not members:
        return None
    lines: list[str] = []
    for m in members[:10]:
        uid = getattr(m, "user_id", "?")
        blocks = ", ".join(list(getattr(m, "block_names", None) or []) or ["?"])
        n_pass = len(getattr(m, "corners_passed", None) or [])
        n_fail = len(getattr(m, "corners_failed", None) or [])
        n_run = len(getattr(m, "corners_run", None) or [])
        lines.append(
            f"- {uid} (block={blocks}): {n_pass}/{n_run} corners pass, {n_fail} fail"
        )
    return "\n".join(lines)


def _get_playbook_summary() -> str | None:
    """playbook overview（id / version / stage count / corner count）。"""
    if _ecc_playbook is None:
        return None
    try:
        stages = list(getattr(_ecc_playbook, "stage_specs", []) or [])
        corner_set: set[str] = set()
        for s in stages:
            for c in getattr(s, "recommended_corner_order", []) or []:
                if c:
                    corner_set.add(c)
        return (
            f"id={getattr(_ecc_playbook, 'id', '')}, "
            f"version={getattr(_ecc_playbook, 'version', '')}, "
            f"stages={len(stages)}, corners={len(corner_set)}"
        )
    except Exception as e:
        _log.warning("_get_playbook_summary failed: %s", e)
        return None


_RECENT_CHECK_EVENT_TYPES = ("lineage_check", "gate_eval")

# lineage_check severity 優先序。同一次 preflight 可能同時 emit
# MIXED_LINEAGE + INSUFFICIENT_EVIDENCE；prompt 要把更具體、更嚴重的排前面，
# 避免 LLM 只看第一筆就回答較弱的那個。
_LINEAGE_SEVERITY: dict[str, int] = {
    "MIXED_LINEAGE": 3,
    "INSUFFICIENT_EVIDENCE": 2,
    "CONSISTENT": 1,
}


def _human_time_ago(
    epoch: float, now: float | None = None, language: str | None = None
) -> str:
    """剛剛 / N 分鐘前 / N 小時前 / N 天前（依 preferred language）。"""
    if now is None:
        now = time.time()
    delta = max(0.0, now - epoch)
    if delta < 60:
        return _tr("time_just_now", language)
    if delta < 3600:
        return _tr("time_minutes_ago", language, n=int(delta / 60))
    if delta < 86400:
        return _tr("time_hours_ago", language, n=int(delta / 3600))
    return _tr("time_days_ago", language, n=int(delta / 86400))


def _get_recent_checks(minutes: int = 10) -> list[dict] | None:
    """撈最近 N 分鐘內 lineage_check + gate_eval events，給 Agent prompt 用。

    使用者問「幫我看看問題」這類模糊問題時，Agent 之前看不到
    pre-exec 觸發的 lineage warning。本 helper 從 event_store 撈最近的檢查
    結果交給 build_agent_system_prompt 渲染。

    輸出：簡化 dict list，按 timestamp 倒序，最多 5 筆。失敗 → None。
    """
    if _event_store is None or not _user_id:
        return None
    if minutes <= 0:
        minutes = 10
    after = time.time() - minutes * 60.0
    now = time.time()
    _lang = load_preferred_language()
    merged: list[tuple[float, dict]] = []

    for et in _RECENT_CHECK_EVENT_TYPES:
        try:
            events = _event_store.query(
                user_id=_user_id,
                event_type=et,
                after=after,
                order="desc",
                limit=5,
            )
        except Exception as e:
            _log.warning("_get_recent_checks query %s failed: %s", et, e)
            continue
        for ev in events:
            ts = float(getattr(ev, "timestamp", 0.0) or 0.0)
            payload = getattr(ev, "payload", None) or {}
            entry: dict = {
                "event_type": et,
                "time": _human_time_ago(ts, now=now, language=_lang),
                "timestamp": ts,
            }
            if et == "lineage_check":
                entry["status"] = payload.get("judgment") or payload.get("status") or "?"
                entry["tool"] = payload.get("tool", "")
                entry["action"] = payload.get("action", "")
                entry["message"] = payload.get("message", "")
                # findings 結構化路徑（emit payload 帶 findings list[dict]
                # 含 category / suggestion / context / artifact_path / artifact_role）。
                # 舊 event（無 findings 欄位）→ fallback 用 message 文字。
                findings_raw = payload.get("findings") or []
                structured: list[dict] = []
                for f in findings_raw:
                    if not isinstance(f, dict):
                        continue
                    structured.append({
                        "category": f.get("category"),
                        "artifact": f.get("path"),
                        "role": f.get("role"),
                        "expected": f.get("expected_rtl_checksum"),
                        "actual_anchors": list(f.get("observed_rtl_anchors") or []),
                        "suggestion": f.get("suggestion"),
                        "context": dict(f.get("context") or {}),
                        "severity": f.get("severity"),
                    })
                if structured:
                    entry["findings"] = structured
                # 向下相容：舊 'suspicious' 欄位仍 surface（若有）
                susp = payload.get("suspicious")
                if susp and not structured:
                    entry["suspicious"] = (
                        [s.get("path", s) if isinstance(s, dict) else s for s in susp][:5]
                    )
            elif et == "gate_eval":
                passed = bool(payload.get("passed"))
                entry["status"] = "PASS" if passed else "FAIL"
                entry["corner"] = payload.get("corner", "")
                entry["gate_id"] = payload.get("gate_id", "")
                entry["actual"] = payload.get("actual")
                entry["threshold"] = payload.get("threshold")
            merged.append((ts, entry))

    if not merged:
        return None

    # split + 雙重排序
    # - lineage_check：先 severity 降序、再 timestamp 降序；放前面
    # - gate_eval：純 timestamp 降序；放後面
    # 設計理由：使用者問模糊問題時最可能是在問血緣 mismatch（MIXED_LINEAGE）；
    # 把它排在 INSUFFICIENT_EVIDENCE 前面避免 LLM 只看第一筆就答較弱的那個。
    lineage_entries = [e for _, e in merged if e["event_type"] == "lineage_check"]
    gate_entries = [(ts, e) for ts, e in merged if e["event_type"] == "gate_eval"]

    lineage_entries.sort(
        key=lambda e: (
            _LINEAGE_SEVERITY.get(e.get("status", ""), 0),
            e.get("timestamp", 0.0),
        ),
        reverse=True,
    )
    gate_entries.sort(key=lambda x: x[0], reverse=True)
    ordered = lineage_entries + [e for _, e in gate_entries]
    return ordered[:5]


def _get_conversation_history_text(conv: Any) -> str | None:
    """從 ConversationBuffer 拿格式化的歷史文字。None / 空 → None。

    Profile 模式下只回傳 `bind_conversation()` 為本 session 計算的可外送快照，不再
    直接讀 buffer：顯示用歷史與可外送歷史是兩個投影，prompt 只能取後者。
    """
    from terminux_engine.llm.task_scope import exportable_history_snapshot, profile_containment_active

    if profile_containment_active():
        text = exportable_history_snapshot()
        return text if text and text.strip() else None
    if conv is None:
        return None
    try:
        text = conv.get_history_text()
    except Exception as e:
        _log.warning("conversation.get_history_text failed: %s", e)
        return None
    return text if text and text.strip() else None


def _unwrap_tool_result_for_trusted_result(value: Any) -> Any:
    """evidence-first planner 現場發現並修復的缺陷：`ToolRegistry.execute()` 回傳的
    `ToolCallResult.result` 型別固定是 `str`——非字串 executor 回傳值
    （例如 package action handler 回傳的 dict）在那一層已經被
    `json.dumps()` 序列化成字串一次，供 chat message content 使用。
    `_proposal_registry.finalize_execution(result=...)` 內部
    （`_create_trusted_result_locked`）會**再對這個值呼叫一次
    `json.dumps()`**，若原始值不是字串本來就沒問題；但這裡如果直接傳入
    `result.result`（已經是字串），就會造成雙重 JSON 編碼——
    `TrustedActionResult.result_payload` 存的會是一個「JSON 字串的
    JSON 字串」，`json.loads()` 一次只會解回一個 Python str（不是原本的
    dict），後續所有依賴 `result_payload` 是 JSON object 的機制
    （evidence-first planner handle catalog／evidence-first planner binding candidate／evidence-first planner business
    status 分類）都會被悄悄降級成「整個 payload 只有一個 string 型別
    的 root handle」，不會拋出例外，難以察覺。這裡在存進
    TrustedActionResult 之前，嘗試把已經被字串化過一次的值解回原本的
    結構；解析失敗（代表 executor 原本就回傳一個非 JSON 的純文字）則
    保留原始字串，行為不變。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# ─── Agent chat helper ──────────────────────────────────

def _general_proposable_catalog() -> list:
    """一般 Agent proposal／plan 用的 catalog（AgentRunner 的 proposable catalog）。

    一般路徑拿不到 flow task 的 host binding，因此 flow task 專用的 capability
    不讓 LLM 看到、也不能被提案。task-owned proposal 與派送仍用完整
    catalog（`_create_task_owned_proposal`、`_build_dispatch_deps`）。
    """
    from terminux_engine.llm.proposal_registry import build_proposable_capability_catalog
    from terminux_engine.packages.orfs_run_intelligence.planning_metadata import (
        FLOW_TASK_ONLY_CAPABILITY_IDS,
    )

    return [item for item in build_proposable_capability_catalog(
        _get_package_registry().packages, _get_package_action_registry(),
    ) if item.action_name not in FLOW_TASK_ONLY_CAPABILITY_IDS]


def _build_dispatch_deps(policy_source: str) -> "Any":
    """組出 `approved_action_executor.dispatch_claimed_proposal()` 的依賴。

    `policy_source` 只影響 audit 標記，讓單筆 operator 派送與 analysis task
    的 child 派送在 audit trail 上可以分辨；授權路徑完全相同。
    """
    from terminux_engine.bounded_execution.approved_action_executor import DispatchDeps
    from terminux_engine.llm.proposal_registry import (
        build_proposable_capability_catalog, validate_action_arguments,
    )

    return DispatchDeps(
        proposal_registry=_proposal_registry,
        tools_registry=_agent_tools_registry,
        catalog_provider=lambda: build_proposable_capability_catalog(
            _get_package_registry().packages, _get_package_action_registry(),
        ),
        validate_arguments=validate_action_arguments,
        unwrap_result=_unwrap_tool_result_for_trusted_result,
        audit=agent_audit.emit,
        request_policy_source=policy_source,
        plan_registry=_plan_registry,
    )


def _execute_approved_proposal(
    proposal_id: str, proposal_digest: str, context: CanonicalRequestContext,
) -> dict[str, Any]:
    """唯一合法的一次性核准執行路徑。

    operator control → ``ProposalRegistry.claim_for_execution()``（atomic
    one-shot claim）→ proposal-bound ``ToolAuthorizationPolicy``（host 建立，
    provider 永遠看不到）→ ``ToolRegistry.execute()``→
    package-action executor → ``ActionRegistry.call()``。這裡永遠不直接呼叫
    ``ActionRegistry`` 或任何 action executor——一律透過與 Agent 平常呼叫
    DANGEROUS tool 相同的 ``_agent_tools_registry.execute()``，只是這次的
    policy 精確綁定只允許這一筆已核准 proposal 的 action/arguments/scope。

    claim 之後的整段派送已抽到
    `bounded_execution.approved_action_executor.dispatch_claimed_proposal()`，
    與 analysis task 共用同一份程式碼。這個函式保留 claim、handle catalog
    audit 與**原本的回傳形狀**，既有 caller 不受影響。
    """
    from terminux_engine.bounded_execution.approved_action_executor import (
        dispatch_claimed_proposal,
    )

    agent_audit.emit("action.execution.requested", tool_call_id=proposal_id,
                     executor_entered=False, policy_source="operator_control_command")

    if _proposal_registry is None or _agent_tools_registry is None:
        agent_audit.emit("action.execution.control_denied", tool_call_id=proposal_id,
                         denial_code="EXECUTION_UNAVAILABLE", executor_entered=False,
                         policy_source="operator_control_command")
        return {"proposal_id": proposal_id, "denial_code": "EXECUTION_UNAVAILABLE", "proposal": None}

    claimed, denial_code = _proposal_registry.claim_for_execution(proposal_id, proposal_digest, context)
    if claimed is None:
        event = (
            "action.execution.replay_denied" if denial_code == "PROPOSAL_ALREADY_EXECUTED"
            else "action.execution.control_denied"
        )
        agent_audit.emit(event, tool_call_id=proposal_id, denial_code=denial_code, executor_entered=False,
                         policy_source="operator_control_command")
        return {"proposal_id": proposal_id, "denial_code": denial_code, "proposal": None}

    outcome = dispatch_claimed_proposal(
        _build_dispatch_deps("operator_control_command"),
        proposal_id=proposal_id, proposal_digest=proposal_digest, claimed=claimed,
        member_id=context.member_id, project_id=context.project_id,
        session_id=context.session_id,
    )
    action_name = outcome.action_name or claimed["action_name"]
    finalized = outcome.proposal_view

    # TrustedActionResult 建立的同一刻，host 已 deterministic
    # 建出這筆的 handle catalog（`ProposalRegistry._create_trusted_result_
    # locked()`）；這裡只負責 audit，不重建。
    trusted_result = (finalized or {}).get("trusted_result") if finalized else None
    if trusted_result:
        catalog_view = _proposal_registry.get_handle_catalog_for_scope(
            trusted_result["result_id"], trusted_result["result_digest"], context,
        )
        agent_audit.emit("action.result.handle_catalog.created", tool_call_id=trusted_result["result_id"],
                         tool_name=action_name, executor_entered=False,
                         result_value={
                             "handle_count": len(catalog_view["handles"]) if catalog_view else 0,
                             "truncated": catalog_view["truncated"] if catalog_view else None,
                         })
    # 抽取前 drift 也是回 denial_code=None（approval 已被 claim 消耗，結果是
    # 一筆 FAILED proposal，不是 control denial）；維持原三個 key 的形狀。
    # `timed_out`／`worker_completion` 是 additive：既有 caller 不讀就不受影響，
    # 需要正確傳遞 timeout 的 caller（approved package action bridge）才讀。
    return {"proposal_id": proposal_id, "denial_code": None, "proposal": finalized,
            "timed_out": outcome.timed_out,
            "worker_completion": outcome.worker_completion}


# ── plan-only production wiring ────────────────────────
#
# 依賴隔離是這一段的重點。`_build_plan_only_dependencies()` 只交出三樣東西：
# LLM provider、Tool Registry 的 **read-only view**、以及 package-owned
# planning metadata exposure。它刻意**不**把 `_agent_tools_registry` 本身、
# `_get_package_action_registry()`、`_proposal_registry`、`_plan_registry`、
# `_event_store` 或 Kernel 交出去——plan-only path 從頭到尾拿不到 executor
#。

_PLAN_ONLY_COMMAND = "/plan-only"


def _build_plan_only_dependencies() -> Any | None:
    """組出 plan-only 的最小依賴；任一項缺失回 None（fail closed，不降級）。"""
    from terminux_engine.planning.handler import PlanOnlyDependencies
    from terminux_engine.planning.registry_view import CapabilityRegistryView
    from terminux_engine.packages.orfs_run_intelligence import planning_metadata

    if _agent_tools_registry is None:
        return None
    provider = _get_runtime_provider()
    if provider is None:
        return None
    try:
        from terminux_engine.llm.agent_adapter import AgentLLMAdapter

        # Plan service 只需要 `chat(messages, tools=None)`；沿用既有 adapter，
        # 不新增 provider 抽象。
        backend = os.environ.get(_ENV_LLM_BACKEND, "").strip()
        if backend not in {"gemini", "openai_compatible", "llama_cpp"}:
            return None
        llm = AgentLLMAdapter(provider, provider_type=backend)
        view = CapabilityRegistryView(_agent_tools_registry)
        return PlanOnlyDependencies(
            llm_provider=llm,
            registry_view=view,
            # flow task 專用的 capability 不進 plan-only catalog。
            exposures=((planning_metadata.plan_only_capability_ids(),
                        planning_metadata.planning_metadata),),
            registry_snapshot_ref="ai3_plan_only_v1:daemon_tool_registry",
        )
    except Exception as exc:  # noqa: BLE001 - 組不出來就是沒有這個能力
        _log.warning("plan-only dependencies unavailable: %r", exc)
        return None


def _run_plan_only(goal: str) -> dict[str, Any]:
    """單一入口：slash command 與 typed IPC method 都走這裡。"""
    from terminux_engine.planning.handler import PLAN_ONLY_MODE, handle_plan_only

    response = handle_plan_only(
        goal=goal,
        mode=PLAN_ONLY_MODE,
        deps=_build_plan_only_dependencies(),
        created_from={"entry": "daemon", "profile_id": "ai3_plan_only_v1"},
    )
    # 把**已驗證通過**的 plan 放進 session-bound、有 TTL／容量
    # 上限的 draft cache，讓使用者之後能用 plan id 指名某個 step。
    # 這**不**改變 plan-only 的語意：不建立 proposal、不執行、不寫任何
    # durable store，`execution_allowed` 仍是 False。
    _remember_plan_draft(response)
    return response


def _remember_plan_draft(response: dict[str, Any]) -> None:
    """Best-effort：失敗只記 log，絕不影響 plan-only 的回覆。"""
    if response.get("status") != "valid":
        return
    plan = response.get("plan")
    if not isinstance(plan, dict):
        return
    try:
        context = get_current_request_context()
        _get_plan_draft_cache().put(
            plan, member_id=context.member_id, project_id=context.project_id,
            session_id=context.session_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning("plan draft retention skipped (non-fatal): %r", exc)


def _try_plan_only_command(query: str) -> str | None:
    """在**任何** Agent／tool／proposal 邏輯之前攔截 `/plan-only <goal>`。

    刻意獨立於 `_try_proposal_control_command()`：那個函式在
    `_proposal_registry is None` 時會直接放行，而且它的職責是 proposal 控制。
    Plan-only 對 proposal registry 沒有、也不該有任何依賴。

    Command name 是 exact match，`/plan`（evidence-first planner execution-plan viewer）不受
    影響，也不會被 prefix 誤攔。
    """
    from terminux_engine.planning.rendering import USAGE, render_plan_only_response

    stripped = query.strip()
    if not stripped.startswith("/"):
        return None
    # 只看第一個 token；goal 保留原始 Unicode 與內部空白。
    head, _, remainder = stripped.partition(" ")
    if head != _PLAN_ONLY_COMMAND:
        return None
    goal = remainder.strip()
    if not goal:
        return USAGE
    return render_plan_only_response(_run_plan_only(goal))


# ── Global／Domain 不可執行草案 ───────────────────────────────

_eap3_service: Any = None
_eap3_lock = threading.Lock()


def _build_eap3_service() -> Any | None:
    """以正式 EventStore、Profile scope 與 FlowTask service 組裝 Engineering Work。"""
    if _event_store is None:
        return None
    binding_file = os.environ.get("TERMINUX_EAP3_BINDINGS_FILE", "").strip()
    if not binding_file:
        return None
    from terminux_engine.llm.task_scope import profile_containment_active

    if profile_containment_active():
        # provider binding 以 urllib 直接外送，不經 TaskScope 最終 Gate。
        _log.info("TERM_PROFILE_CONTAINMENT disabled=eap3_provider_bindings")
        return None
    flow_service = _get_analysis_task_service()
    if flow_service is None:
        return None
    try:
        from terminux_engine.expert_ai.eap3 import EngineeringWorkService
        from terminux_engine.expert_ai.eap3_extensions import (
            FixtureProcessRuntime, FlowTaskWorkRuntime, ProviderBindingRouter,
            build_package_registry,
        )

        fixture_runtime_file = os.environ.get(
            "TERMINUX_EAP3_FIXTURE_RUNTIME_FILE", "").strip()
        fixture_only = bool(fixture_runtime_file)
        eap4_catalog_file = os.environ.get(
            "TERMINUX_EAP4_EXPERT_CATALOG_FILE", "").strip()
        service_type: Any = EngineeringWorkService
        service_extra: dict[str, Any] = {}
        if eap4_catalog_file:
            from terminux_engine.expert_ai.eap4 import (
                ExpertBindingRouter, ExpertCapabilityCatalog,
                MultiExpertWorkService,
            )
            catalog = ExpertCapabilityCatalog.from_file(eap4_catalog_file)
            experts = ExpertBindingRouter.from_files(
                catalog=catalog, binding_path=binding_file,
                fixture_only=fixture_only)
            service_type = MultiExpertWorkService
            service_extra["expert_catalog"] = catalog
        else:
            experts = ProviderBindingRouter.from_file(
                binding_file, fixture_only=fixture_only)
        runtime = (FixtureProcessRuntime.from_file(fixture_runtime_file)
                   if fixture_runtime_file else FlowTaskWorkRuntime(flow_service))

        def profile_resolver(profile_id: str) -> dict[str, Any]:
            return flow_service.eap_profile_scope(profile_id)

        def submit_work(fn: Any) -> None:
            """把核准 request 的 canonical context 固定給背景 work driver。"""
            context = get_current_request_context()

            def run() -> None:
                token = set_current_request_context(context)
                try:
                    with agent_audit.agent_turn():
                        fn()
                finally:
                    reset_current_request_context(token)

            _submit_analysis_worker(run)

        package_registry = build_package_registry()
        service = service_type(
            event_store=_event_store, scope_provider=get_current_request_context,
            profile_resolver=profile_resolver, packages=package_registry,
            experts=experts, runtime=runtime,
            daemon_instance_id=str(_daemon_instance_id or "daemon-unavailable"),
            submit=submit_work, notify=_append_package_pending_notification,
            poll_interval_sec=float(os.environ.get(
                "TERMINUX_EAP3_AUTODRIVE_POLL_SEC", "0.1")),
            **service_extra)
        # 記錄組裝當下的來源身分；之後設定檔變動不能冒充已載入的 binding。
        service._eap5_loaded_binding = {
            "path": str(Path(binding_file).resolve()),
            "sha256": hashlib.sha256(Path(binding_file).read_bytes()).hexdigest(),
            "source": "daemon_environment_at_service_build",
        }
        bound_routers = (experts._routers.values()
                         if hasattr(experts, "_routers") else (experts,))
        service._eap5_binding_values = sorted(({
            "binding_id": row["binding_id"], "role": row["role"],
            "capabilities": list(row["capabilities"]), "model": row["model"],
            "adapter": row["adapter"], "timeout_sec": row["timeout_sec"],
            "max_output_tokens": row["max_output_tokens"],
            "max_response_bytes": row["max_response_bytes"],
            "endpoint_origin": (
                urllib.parse.urlparse(row["endpoint"]).scheme + "://" +
                urllib.parse.urlparse(row["endpoint"]).netloc.rsplit("@", 1)[-1]),
        } for router in bound_routers for row in router._bindings),
            key=lambda row: row["binding_id"])
        service._eap5_package_values = package_registry.descriptors()
        runtime_module = Path(sys.modules[type(runtime).__module__].__file__).resolve()
        service._eap5_runtime_origin = {
            "class": type(runtime).__name__,
            "module_path": str(runtime_module),
            "module_sha256": hashlib.sha256(runtime_module.read_bytes()).hexdigest(),
            "source": "daemon_loaded_module_at_service_build",
        }
        from terminux_engine.resource_paths import llm_config_path
        loaded_llm_path = llm_config_path()
        service._eap5_loaded_config = {
            "llm_config": {
                "path": str(loaded_llm_path),
                "sha256": (hashlib.sha256(loaded_llm_path.read_bytes()).hexdigest()
                           if loaded_llm_path.is_file() else None),
                "source": (
                    "explicit_environment" if os.environ.get("TERMINUX_LLM_CONFIG_PATH")
                    else "user_file" if loaded_llm_path ==
                    Path.home() / ".terminux/config/llm_config.yaml"
                    else "packaged_template"),
            },
            "background": {
                "rag_rebuild_disabled": os.environ.get(
                    "TERMINUX_DISABLE_RAG_BACKGROUND_REBUILD", "").strip().lower()
                    in {"1", "true", "yes"},
                "embedding_backend": os.environ.get(
                    "TERMINUX_EMBEDDING_BACKEND", "").strip() or "default",
                "source": "daemon_environment_at_service_build",
            },
        }
        return service
    except Exception as exc:  # noqa: BLE001 - 組裝失敗即不開放副作用
        _log.warning("Engineering Work service unavailable: %r", exc)
        return None


def _get_eap3_service() -> Any | None:
    global _eap3_service  # noqa: PLW0603
    with _eap3_lock:
        if _eap3_service is None:
            _eap3_service = _build_eap3_service()
        return _eap3_service


def _run_eap3(operation: str, **kwargs: Any) -> dict[str, Any]:
    service = _get_eap3_service()
    if service is None:
        return {"status": "error", "error_code": "EAP3_SERVICE_UNAVAILABLE",
                "message": "Engineering Work requires provider bindings, EventStore and Flow runtime",
                "execution_allowed": False}
    try:
        with agent_audit.agent_turn():
            return getattr(service, operation)(**kwargs)
    except Exception as exc:  # noqa: BLE001 - public RPC 回穩定錯誤，不洩漏設定
        code = getattr(exc, "code", "EAP3_INTERNAL_ERROR")
        return {"status": "error", "error_code": code,
                "message": str(getattr(exc, "message", "Engineering Work operation failed")),
                "execution_allowed": False}

def _build_eap1_service() -> Any | None:
    """沿用 daemon provider 與 EventStore；缺任一依賴即 fail closed。"""
    if _event_store is None:
        return None
    provider = _get_runtime_provider()
    if provider is None:
        return None
    try:
        from terminux_engine.expert_ai.eap1 import (
            ChatExpertAdapter, EventStoreEvidenceReader, ExpertDescriptor,
            ExpertDraftService,
        )
        from terminux_engine.llm.agent_adapter import AgentLLMAdapter

        backend = os.environ.get(_ENV_LLM_BACKEND, "").strip()
        if backend not in {"gemini", "openai_compatible", "llama_cpp"}:
            return None
        adapter = AgentLLMAdapter(provider, provider_type=backend)
        model = str(getattr(getattr(provider, "_config", None), "model", "unknown"))
        role_adapter = ChatExpertAdapter(adapter.chat, provider=backend, model=model)
        descriptor = ExpertDescriptor(
            expert_id="orfs.timing", capabilities=("timing_analysis",),
            operations=("orfs.eap.materialize_timing_parameter",),
            implementation_version="eap1.orfs-preview.v1",
        )
        return ExpertDraftService(
            event_store=_event_store, scope_provider=get_current_request_context,
            evidence_reader=EventStoreEvidenceReader(_event_store),
            global_adapter=role_adapter,
            experts={descriptor.expert_id: (descriptor, role_adapter)},
            cancelled=_eap1_cancelled,
        )
    except Exception as exc:  # noqa: BLE001 - 不能組成完整入口即不對外開放
        _log.warning("domain proposal service unavailable: %r", exc)
        return None


def _parse_eap1_problem(params: dict[str, Any]) -> tuple[Any | None, str | None]:
    """驗證 client 可提供的問題資料；identity 與 scope 一律由 server 注入。"""
    from terminux_engine.expert_ai.eap1 import EvidenceRef, ProblemInput

    allowed = {"objective", "constraints", "environment", "evidence_refs",
               "available_evidence_refs", "allowed_operations", "dispatch_budget", "profile_id"}
    unexpected = sorted(set(params) - allowed)
    if unexpected:
        return None, f"unexpected params for agent.eap1.create: {unexpected}"
    objective = params.get("objective")
    constraints = params.get("constraints", [])
    environment = params.get("environment")
    refs = params.get("evidence_refs", [])
    available_refs = params.get("available_evidence_refs", [])
    operations = params.get("allowed_operations")
    budget = params.get("dispatch_budget", 8)
    profile_id = params.get("profile_id")
    if not isinstance(objective, str) or not objective.strip():
        return None, "'objective' must be a non-empty string"
    if not isinstance(constraints, list) or any(not isinstance(x, str) for x in constraints):
        return None, "'constraints' must be an array of strings"
    if not isinstance(environment, dict):
        return None, "'environment' must be an object"
    if not isinstance(refs, list):
        return None, "'evidence_refs' must be an array"
    if not isinstance(available_refs, list):
        return None, "'available_evidence_refs' must be an array"
    parsed_refs = []
    for item in refs:
        if not isinstance(item, dict) or set(item) != {"evidence_ref_id", "content_sha256"} \
                or not isinstance(item["evidence_ref_id"], str) \
                or not isinstance(item["content_sha256"], str):
            return None, "each evidence ref requires only evidence_ref_id and content_sha256 strings"
        parsed_refs.append(EvidenceRef(item["evidence_ref_id"], item["content_sha256"]))
    parsed_available = []
    for item in available_refs:
        if not isinstance(item, dict) or set(item) != {"evidence_ref_id", "content_sha256"} \
                or not isinstance(item["evidence_ref_id"], str) \
                or not isinstance(item["content_sha256"], str):
            return None, "each available evidence ref requires only evidence_ref_id and content_sha256 strings"
        parsed_available.append(EvidenceRef(item["evidence_ref_id"], item["content_sha256"]))
    if not isinstance(operations, list) or any(not isinstance(x, str) for x in operations):
        return None, "'allowed_operations' must be an array of strings"
    if not isinstance(budget, int):
        return None, "'dispatch_budget' must be an integer"
    scope_override = None
    if profile_id is not None:
        if not isinstance(profile_id, str) or not profile_id:
            return None, "'profile_id' must be a non-empty string"
        service = _get_analysis_task_service()
        resolved = service.eap_profile_scope(profile_id) if service is not None else None
        if not isinstance(resolved, dict) or resolved.get("status") != "ok":
            return None, "the selected profile is unavailable or out of scope"
        from terminux_engine.expert_ai.eap1 import EapScope
        context = get_current_request_context()
        scope_override = EapScope(context.member_id, str(resolved["project_id"]), context.session_id,
                                  str(resolved["project_source"]))
        environment = {**dict(resolved.get("environment") or {}), **environment}
    return ProblemInput(
        objective=objective, constraints=tuple(constraints), environment=environment,
        evidence_refs=tuple(parsed_refs), allowed_operations=tuple(operations),
        dispatch_budget=budget, available_evidence_refs=tuple(parsed_available),
        scope_override=scope_override), None


def _run_eap1(operation: str, **kwargs: Any) -> dict[str, Any]:
    # 查詢與取消是既有證據／本地狀態操作，provider 壞掉不能讓它們消失。
    if operation == "get":
        if _event_store is None:
            return {"status": "ERROR", "reason_code": "EAP1_STORE_UNAVAILABLE",
                    "message": "the domain proposal event store is unavailable", "execution_allowed": False}
        from terminux_engine.expert_ai.eap1 import ExpertDraftReader
        draft = ExpertDraftReader(_event_store).load(kwargs["draft_id"], get_current_request_context())
        return draft or {"status": "ERROR", "reason_code": "EAP1_DRAFT_NOT_FOUND",
                         "message": "no domain proposal draft is available in this scope",
                         "execution_allowed": False}
    if operation == "cancel":
        if _event_store is None:
            return {"status": "ERROR", "reason_code": "EAP1_STORE_UNAVAILABLE",
                    "message": "the domain proposal event store is unavailable", "execution_allowed": False}
        from terminux_engine.expert_ai.eap1 import EapScope, EVENT_CANCEL
        from terminux_engine.store.event_store import SessionEvent
        context = get_current_request_context()
        scope = EapScope.from_context(context)
        problem_id = kwargs["problem_id"]
        _eap1_cancelled.add(problem_id)
        _event_store.emit(SessionEvent(
            event_id=uuid4().hex, session_id=scope.session_id, terminal_id="default",
            user_id=scope.member_id, project_id=scope.project_id, package_id=None,
            timestamp=time.time(), event_type=EVENT_CANCEL,
            payload={"schema_version": 1, "problem_id": problem_id,
                     "scope": scope.to_json_dict(), "provider_outcome": "unknown"},
        ))
        return {"status": "CANCELLED", "problem_id": problem_id,
                "provider_outcome": "unknown", "execution_allowed": False}
    if operation == "evidence_select":
        if _event_store is None:
            return {"status": "ERROR", "reason_code": "EAP1_STORE_UNAVAILABLE", "execution_allowed": False}
        from terminux_engine.expert_ai.eap1 import EapScope, _digest
        scope = kwargs.get("scope_override") or EapScope.from_context(get_current_request_context())
        event = _event_store.get_event_by_id_for_scope(kwargs["event_id"], user_id=scope.member_id,
                                                        project_id=scope.project_id)
        if event is None:
            return {"status": "ERROR", "reason_code": "EVIDENCE_NOT_FOUND_OR_OUT_OF_SCOPE", "execution_allowed": False}
        content = {"event_type": event.event_type, "payload": event.payload}
        from terminux_engine.expert_ai.eap1 import is_selectable_evidence_content
        if not is_selectable_evidence_content(content):
            return {"status": "ERROR", "reason_code": "EVIDENCE_PARTIAL_OR_INCOMPLETE",
                    "execution_allowed": False}
        return {"status": "OK", "evidence_ref": {"evidence_ref_id": event.event_id,
                "content_sha256": _digest(content)}, "event_type": event.event_type,
                "execution_allowed": False}
    if operation == "evidence_list":
        if _event_store is None:
            return {"status": "ERROR", "reason_code": "EAP1_STORE_UNAVAILABLE",
                    "execution_allowed": False}
        from terminux_engine.expert_ai.eap1 import EapScope
        scope = kwargs.get("scope_override") or EapScope.from_context(get_current_request_context())
        event_type = kwargs.get("event_type")
        events = _event_store.query(
            user_id=scope.member_id,
            project_id=scope.project_id,
            event_type=event_type,
            limit=kwargs["limit"],
            order="desc",
        )
        from terminux_engine.expert_ai.eap1 import is_selectable_evidence_content
        # 僅列出選取所需的 metadata；實際 payload 與 digest 一律由 select
        # 以相同 server-authoritative scope 取得，避免 client 自行組 evidence。
        return {"status": "OK", "events": [
            {"event_id": event.event_id, "event_type": event.event_type,
             "timestamp": event.timestamp}
            for event in events
            if is_selectable_evidence_content(
                {"event_type": event.event_type, "payload": event.payload})
        ], "execution_allowed": False}
    service = _build_eap1_service()
    if service is None:
        return {"status": "ERROR", "reason_code": "EAP1_SERVICE_UNAVAILABLE",
                "message": "domain proposal requires the configured EventStore and LLM provider",
                "execution_allowed": False}
    return getattr(service, operation)(**kwargs)


# ── approved bounded package action execution ──────────
#
# 依賴隔離同 plan-only planner：`_build_bounded_execution_dependencies()` 只交出 draft
# cache、既有 ProposalRegistry、既有 EventStore 的 record view、capability
# provider 與**永遠 disabled** 的 production activation gate。它不交出
# shell、Kernel、SubprocessExecutor 或 ORFS adapter。

#: 需要 provider 的操作被拒時附在訊息最後的補救方式。
_PROVIDER_REMEDY = ("set TERMINUX_LLM_BACKEND to a supported provider "
                    "(gemini or openai_compatible) and restart the daemon")

_PLAN_PROPOSE_COMMAND = "/plan-propose"
_plan_draft_cache: Any = None


def _get_plan_draft_cache() -> Any:
    global _plan_draft_cache  # noqa: PLW0603
    if _plan_draft_cache is None:
        from terminux_engine.bounded_execution.draft import PlanDraftCache

        _plan_draft_cache = PlanDraftCache()
    return _plan_draft_cache


class _EventStoreRecordView:
    """approved package action 需要的最小 record 介面，實作在**既有** EventStore 之上。

    只有 `put`／`query` 兩個方法——沒有 update、沒有 delete，也拿不到
    EventStore 本身。approved package action 因此不可能新增第三套 ledger。
    """

    def __init__(self, event_store: Any, *, session_id: str, user_id: str,
                 project_id: str | None) -> None:
        self._event_store = event_store
        self._session_id = session_id
        self._user_id = user_id
        self._project_id = project_id

    def put(self, record_type: str, key: str, payload: dict[str, Any]) -> bool:
        from uuid import uuid4

        from terminux_engine.store.event_store import SessionEvent

        try:
            self._event_store.emit(SessionEvent(
                event_id=uuid4().hex, session_id=self._session_id,
                terminal_id="default", user_id=self._user_id,
                project_id=self._project_id, package_id="bounded_execution",
                timestamp=time.time(), event_type=record_type,
                payload={**payload, "record_key": key},
            ))
            return True
        except Exception as exc:  # noqa: BLE001 - 寫入失敗必須是可觀察的 False
            _log.error("bounded execution record write failed (%s): %r", record_type, exc)
            return False

    def query(self, record_type: str, **filters: Any) -> list[dict[str, Any]]:
        try:
            events = self._event_store.query(event_type=record_type, order="asc")
        except Exception as exc:  # noqa: BLE001
            _log.warning("bounded execution record query failed (%s): %r", record_type, exc)
            return []
        out: list[dict[str, Any]] = []
        for event in events:
            payload = event.payload or {}
            if all(payload.get(k) == v for k, v in filters.items()):
                out.append(dict(payload))
        return out


def _build_bounded_execution_dependencies(terminal_id: str = "default") -> Any | None:
    """組出 approved package action 的最小依賴。任一項缺失回 None（fail closed，不降級）。"""
    from terminux_engine.bounded_execution.activation import production_activation_gate
    from terminux_engine.bounded_execution.service import BoundedExecutionDeps

    if _proposal_registry is None or _event_store is None or _agent_tools_registry is None:
        return None
    try:
        context = get_current_request_context()
    except Exception:
        return None

    from terminux_engine.packages.orfs_run_intelligence import planning_metadata
    from terminux_engine.planning.profile import PROFILE_ID, build_ai3_snapshot
    from terminux_engine.planning.registry_view import CapabilityRegistryView

    view = CapabilityRegistryView(_agent_tools_registry)

    def catalog_provider() -> tuple[str, dict[str, Any]]:
        # 必須與 plan-only 的 exposure 相同，否則 plan 記下的 catalog
        # snapshot id 與這裡重算的不一致，每一步都會被判為 catalog drift。
        snapshot = build_ai3_snapshot(
            view,
            exposures=[(planning_metadata.plan_only_capability_ids(),
                        planning_metadata.planning_metadata)],
            registry_snapshot_ref="ai3_plan_only_v1:daemon_tool_registry",
        )
        return snapshot.snapshot_id, {c.capability_id: c.to_json_dict()
                                      for c in snapshot.capabilities}

    return BoundedExecutionDeps(
        draft_cache=_get_plan_draft_cache(),
        proposal_registry=_proposal_registry,
        record_store=_EventStoreRecordView(
            _event_store, session_id=context.session_id, user_id=context.member_id,
            project_id=context.project_id),
        catalog_provider=catalog_provider,
        # Activation 決定只來自版本控制的 source，沒有任何 request／env 可以改。
        activation_gate=production_activation_gate(),
        # 2026-09-06 啟用後注入的是**既有**的 approved-proposal bridge
        # （`_execute_approved_proposal`），不是 `ActionRegistry.call()`。
        # approved package action 因此沒有第二條 execution authority：proposal 建立、explicit
        # approval、one-shot claim、allowlist 與 drift revalidation 全部沿用
        # evidence-first planner 那一條路徑。
        action_executor=_approved_proposal_action_executor,
        clock=time.time,
    )


def _approved_proposal_action_executor(
    *, action_name: str, arguments: dict[str, Any],
    proposal_id: str, proposal_digest: str,
) -> dict[str, Any]:
    """approved package action 的 execution bridge：轉交既有的一次性核准執行路徑。

    刻意**不**碰 `ActionRegistry`、`ToolRegistry.execute()` 或任何 executor——
    只呼叫 `_execute_approved_proposal()`，讓 claim、proposal-bound
    authorization policy、dangerous-tool gate 與 receipt 寫入全部維持單一權威。

    `action_name` 與 `arguments` 由 caller 傳入只是為了讓 outcome 可讀；
    真正被執行的內容一律以 registry 內那筆已核准 proposal 為準，
    這裡不用它們去組任何呼叫。
    """
    try:
        context = get_current_request_context()
    except Exception:
        return {"success": False, "error": {"code": "PROPOSAL_NO_REQUEST_CONTEXT"},
                "timed_out": False}
    outcome = _execute_approved_proposal(proposal_id, proposal_digest, context)
    denial = outcome.get("denial_code")
    if denial is not None:
        # control denial：executor 從未進入，因此不可能 timeout。
        return {"success": False, "error": {"code": denial}, "timed_out": False}
    view = outcome.get("proposal") or {}
    return {"success": view.get("execution_status") == "SUCCEEDED",
            "result": view.get("execution_result"),
            # analysis task 指定修正：原本寫死 False，會把「我們不再等，但 worker 可能
            # 仍在跑」誤報成普通失敗。改為傳遞真值。
            "timed_out": bool(outcome.get("timed_out", False)),
            "proposal_id": proposal_id,
            "action_name": view.get("action_name", action_name)}


def _run_plan_propose(plan_id: str, step_id: str) -> dict[str, Any]:
    from terminux_engine.bounded_execution import service as bounded_service
    from terminux_engine.bounded_execution.errors import (
        BOUNDED_EXECUTION_PROVIDER_UNAVAILABLE, PLAN_DRAFT_NOT_FOUND,
    )

    # 沒有 provider 時，執行 plan 的路徑在入口就停用。ProposalRegistry
    # 本身不依賴 provider，但已持久化的 plan 不應因此在無
    # provider 的 daemon 上被轉成提案、走到 package action。
    if _agent_router is None:
        return {"status": "error", "error_code": BOUNDED_EXECUTION_PROVIDER_UNAVAILABLE,
                "message": ("plan execution requires an LLM provider, which is not "
                            "configured on this daemon; no proposal was created; "
                            + _PROVIDER_REMEDY),
                "action_execution_count": 0, "proposal_claimed": False}

    deps = _build_bounded_execution_dependencies()
    if deps is None:
        return {"status": "error", "error_code": PLAN_DRAFT_NOT_FOUND,
                "message": "bounded execution is not configured on this daemon",
                "action_execution_count": 0, "proposal_claimed": False}
    context = get_current_request_context()
    return bounded_service.propose_step(
        deps, plan_id=plan_id, step_id=step_id, member_id=context.member_id,
        project_id=context.project_id, session_id=context.session_id)


def _try_plan_propose_command(query: str) -> str | None:
    """`/plan-propose <plan_id> <step_id>`。只建立提案，永不執行。"""
    from terminux_engine.bounded_execution.rendering import USAGE, render

    stripped = query.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    if parts[0] != _PLAN_PROPOSE_COMMAND:
        return None
    if len(parts) != 3:
        return USAGE
    return render(_run_plan_propose(parts[1], parts[2]))


# ── AnalysisTask 的 production composition 與單一入口 ───────────
#
# 依賴隔離：service 拿到的是既有 EventStore、既有 ProposalRegistry、既有
# tool registry 的 dispatch deps、以及 ORFS Package 注入的三個純函式。
# 它**不**拿到 shell、Kernel、SubprocessExecutor 或 orchestration service。

_ANALYSIS_TASK_COMMANDS = ("/task", "/task-start", "/task-cancel",
                           # task 清單、profile 選用／清單、明確建立。
                           "/tasks", "/profile", "/profiles", "/task-create")
_EAP3_WORK_COMMANDS = frozenset({
    "/work-create", "/work-authorize", "/work-approve", "/work-continue",
    "/work", "/works", "/work-answer", "/work-pause", "/work-resume",
    "/work-cancel", "/work-takeover",
})
_analysis_task_service: Any = None
_analysis_task_lock = threading.Lock()
_daemon_instance_id: str | None = None
#: daemon 啟動時載入一次的 analysis access policy guard。
#: 沒有配置就是 None——**沒有寬鬆預設**，run access 一律 UNVERIFIABLE。
#: 無 hot reload：之後每個授權點只確認檔案仍在且 digest 與啟動時一致。
_analysis_access_guard: Any = None
_analysis_access_guard_error: str | None = None


#: **永遠是 None**。daemon 不再持有 process-global active profile；
#: 保留名稱只為了讓舊的 check script 能 import。
_flow_profile_guard: Any = None
_flow_profile_error: str | None = None

#: 一個 daemon 一份 bounded scheduler（analysis task 與 flow task 共用）。
_flow_scheduler: Any = None


def _get_flow_scheduler() -> Any:
    """依環境變數建立 scheduler；activation policy 的 `max_concurrent_tasks`
    只當天花板。啟動後只建一次。"""
    global _flow_scheduler  # noqa: PLW0603
    if _flow_scheduler is None:
        from terminux_engine.bounded_execution.analysis_tasks.scheduler import (
            SchedulerLimits, TaskScheduler,
        )

        ceiling = None
        if _limited_activation_guard is not None:
            ceiling = _limited_activation_guard.startup_policy.budgets.max_concurrent_tasks
        limits = SchedulerLimits.from_env(policy_ceiling=ceiling)
        _flow_scheduler = TaskScheduler(limits)
        _log.info("TERM_FLOW_SCHEDULER_READY %s", limits.to_json_dict())
    return _flow_scheduler

#: daemon 啟動時載入一次的 limited activation policy guard。
#: 沒有配置就是 None ⇒ **flow 的 create／start／launch 全部拒絕**（預設 disabled）。
_limited_activation_guard: Any = None
_limited_activation_error: str | None = None


def _init_limited_activation_policy() -> None:
    """daemon 啟動時載入一次 limited activation policy（無 hot reload）。

    載入失敗只記錄，不阻止 daemon 啟動——但也**不**因此放行：guard 會是
    None，flow 路徑一律拒絕，analysis task 與其他既有功能不受影響。
    """
    global _limited_activation_guard, _limited_activation_error  # noqa: PLW0603
    from terminux_engine.bounded_execution.limited_activation import (
        POLICY_ENV, LimitedActivationError, load_guard_from_env,
    )

    try:
        _limited_activation_guard = load_guard_from_env()
    except LimitedActivationError as exc:
        _limited_activation_guard = None
        _limited_activation_error = f"{exc.code}: {exc.message}"
        _log.warning("limited activation policy not loaded: %s",
                     _limited_activation_error)
        return
    if _limited_activation_guard is None:
        _log.info("TERM_LIMITED_ACTIVATION_NOT_CONFIGURED (%s unset)", POLICY_ENV)
    else:
        policy = _limited_activation_guard.startup_policy
        _log.info("TERM_LIMITED_ACTIVATION_LOADED policy_id=%s digest=%s state=%s",
                  policy.policy_id, policy.policy_digest, policy.state)


def _build_limited_activation_gate() -> Any:
    """flow 路徑的正式 gate：既有 base gate ＋ limited activation policy。"""
    from terminux_engine.bounded_execution.activation import production_activation_gate
    from terminux_engine.bounded_execution.limited_activation import (
        LimitedActivationGate,
    )

    return LimitedActivationGate(base=production_activation_gate(),
                                 guard=_limited_activation_guard)


def _init_flow_task_profile() -> None:
    """daemon **不再**載入 process-global active profile。

    profile 由每個 task 的來源決定（`profile_path`／`profile_inline`／本
    session 以 `/profile` 選用）。舊的 `TERMINUX_FLOW_TASK_PROFILE_FILE` 若仍
    設著，只記 deprecation warning 並忽略——不轉成全域 profile，也不猜它與
    request 來源的優先順序。
    """
    global _flow_profile_guard, _flow_profile_error  # noqa: PLW0603
    from terminux_engine.bounded_execution.analysis_tasks.flow_profile import (
        PROFILE_ENV, deprecated_env_profile_path,
    )

    _flow_profile_guard = None
    if deprecated_env_profile_path():
        _flow_profile_error = f"{PROFILE_ENV} is deprecated and ignored"
        _log.warning(
            "TERM_FLOW_TASK_PROFILE_ENV_IGNORED %s is deprecated and is "
            "ignored; profiles are task-scoped — pass profile_path/profile_inline "
            "to agent.flow_task.create or select one with /profile <absolute path>",
            PROFILE_ENV)
    else:
        _flow_profile_error = None
        _log.info("TERM_FLOW_TASK_PROFILE_TASK_SCOPED (no daemon-global profile)")


def _init_analysis_access_policy() -> None:
    """daemon 啟動時呼叫一次。載入失敗只記錄，不阻止 daemon 啟動——
    但也**不**因此放行：resolver 會是 None，run access 一律 UNVERIFIABLE。"""
    global _analysis_access_guard, _analysis_access_guard_error  # noqa: PLW0603
    from terminux_engine.bounded_execution.analysis_tasks.access_policy import (
        POLICY_ENV, AccessPolicyError, load_guard_from_env,
    )

    try:
        _analysis_access_guard = load_guard_from_env()
    except AccessPolicyError as exc:
        _analysis_access_guard = None
        _analysis_access_guard_error = f"{exc.code}: {exc.message}"
        _log.warning("analysis access policy not loaded: %s", _analysis_access_guard_error)
        return
    if _analysis_access_guard is None:
        _log.info("TERM_ANALYSIS_ACCESS_POLICY_NOT_CONFIGURED (%s unset)", POLICY_ENV)
    else:
        _log.info("TERM_ANALYSIS_ACCESS_POLICY_LOADED policy_id=%s digest=%s",
                  _analysis_access_guard.startup_policy.policy_id,
                  _analysis_access_guard.startup_policy.policy_digest)


def _get_daemon_instance_id() -> str:
    """本 daemon process 的唯一 instance id，啟動後只算一次。

    刻意**不**用 PID 或時間戳：兩者都可能重複，而 task 的
    `is_current_instance` 判定必須是「同一個 process 生命週期」。
    """
    global _daemon_instance_id  # noqa: PLW0603
    if _daemon_instance_id is None:
        from terminux_engine.bounded_execution.analysis_tasks.models import (
            new_daemon_instance_id,
        )

        _daemon_instance_id = new_daemon_instance_id()
    return _daemon_instance_id


def _analysis_task_package_context() -> Any:
    """給 ORFS `validate_task_runs()` 用的 PackageContext。

    走既有的 `_build_package_runtime_services()`，因此 DQE DB 一樣由既有
    五層政策解析，caller 不能指定 DB 或路徑。
    """
    from terminux_engine.packages.orfs_run_intelligence.planning_metadata import (
        EXPLAIN_RUN,
    )

    ctx = _build_action_context(EXPLAIN_RUN, {"args": {}})
    # `_build_package_runtime_services()` 帶的是 daemon 全域的 `_user_id`／
    # `_project_id`（後者是 `Path.home().parent.name`，不是 session 的
    # project）。analysis access policy 必須比對**這次 request 的 canonical
    # scope**，所以在這裡以 kernel 已驗證的 request context 覆寫。
    # 這兩個值都不是 caller 提供的：member 由 SO_PEERCRED 解析，project 由
    # session 建立時固定且 session 內不可變。
    request_context = get_current_request_context()
    if ctx.services is not None and ctx.services.store is not None:
        ctx.services.store.user_id = request_context.member_id
        ctx.services.store.project_id = request_context.project_id
    return ctx


def _create_task_owned_proposal(
    *, action_name: str, arguments: dict[str, Any], context: Any,
    task_id: str, step_id: str, grant_id: str,
) -> Any:
    """建立一筆 task-owned proposal。只有 task service 會呼叫這裡。"""
    from terminux_engine.llm.proposal_registry import (
        TaskProposalOwner, build_proposable_capability_catalog,
    )

    catalog = build_proposable_capability_catalog(
        _get_package_registry().packages, _get_package_action_registry(),
    )
    return _proposal_registry.create(
        action_name=action_name, arguments=arguments,
        reason=f"analysis task {task_id} step {step_id}",
        evidence_references=[], catalog=catalog, context=context,
        task_owner=TaskProposalOwner(task_id=task_id, step_id=step_id,
                                     grant_id=grant_id),
    )


def _submit_analysis_worker(fn: Any) -> None:
    """單一 in-daemon analysis worker。沒有 queue——名額由 service 的
    admission lock 控制，這裡只負責把工作丟到背景，讓 IPC 立即返回。"""
    threading.Thread(target=fn, name="analysis-task-worker", daemon=True).start()


def _build_analysis_task_service() -> Any | None:
    """組出 service；任一依賴缺失回 None（fail closed，不降級）。"""
    from terminux_engine.bounded_execution.activation import production_activation_gate
    from terminux_engine.bounded_execution.analysis_tasks.service import (
        AnalysisTaskService, TaskServiceDeps,
    )
    from terminux_engine.packages.orfs_run_intelligence import (
        analysis_task_template as template,
    )
    from terminux_engine.planning.registry_view import CapabilityRegistryView
    from terminux_engine.packages.orfs_run_intelligence import planning_metadata
    from terminux_engine.planning.profile import build_ai3_snapshot

    # service 的組裝**不**依賴 provider。只要共用 registry 與 store
    # 在，task control／查詢／取消就必須可用；provider 只是執行依賴。
    if _event_store is None or _proposal_registry is None or _agent_tools_registry is None:
        return None

    view = CapabilityRegistryView(_agent_tools_registry)

    def catalog_provider() -> tuple[str, dict[str, Any]]:
        snapshot = build_ai3_snapshot(
            view,
            exposures=[(planning_metadata.exposed_capability_ids(),
                        planning_metadata.planning_metadata)],
            registry_snapshot_ref="ai3_plan_only_v1:daemon_tool_registry",
        )
        return snapshot.snapshot_id, {c.capability_id: c.to_json_dict()
                                      for c in snapshot.capabilities}

    from terminux_engine.bounded_execution.analysis_tasks.access_policy import (
        PolicyProjectAccessResolver,
    )

    # 正式 resolver 由 server policy 支持。沒有配置 policy
    # 時仍是 None ⇒ run access 一律 UNVERIFIABLE（沒有寬鬆預設）。
    project_access = (
        PolicyProjectAccessResolver(_analysis_access_guard)
        if _analysis_access_guard is not None else None
    )

    def validate_runs(*, current_run_id: str, baseline_run_id: str) -> Any:
        try:
            return template.validate_task_runs(
                _analysis_task_package_context(), current_run_id, baseline_run_id,
                project_access=project_access,
            )
        except Exception as exc:  # noqa: BLE001 - 檢查本身失敗就是不可證明
            _log.warning("analysis task run access check failed: %r", exc)
            return template.RunAccessResult(
                decision="UNVERIFIABLE",
                reason_code="RUN_ACCESS_CHECK_FAILED",
                message=f"the run access check could not be completed: {exc!r}",
                runs={},
            )

    def access_binding_provider(*, current_run_id: str, baseline_run_id: str) -> Any:
        """由**目前有效**的 policy 產生 access binding。

        policy 缺席、變動或不涵蓋這兩筆 run 時回 None——上游的 run access
        檢查早已擋下，這裡不另外放行。
        """
        from terminux_engine.bounded_execution.analysis_tasks.access_policy import (
            AccessPolicyError,
        )
        from terminux_engine.bounded_execution.analysis_tasks.models import (
            ACCESS_BINDING_SCHEMA_VERSION, TaskAccessBinding,
        )

        if _analysis_access_guard is None:
            return None
        try:
            policy = _analysis_access_guard.current()
        except AccessPolicyError:
            return None
        manifests = {}
        for run_id in (current_run_id, baseline_run_id):
            allowed = policy.allowed_run(run_id)
            if allowed is None:
                return None
            manifests[run_id] = allowed.manifest_sha256
        return TaskAccessBinding(
            schema_version=ACCESS_BINDING_SCHEMA_VERSION,
            policy_id=policy.policy_id, policy_digest=policy.policy_digest,
            member_id=policy.member_id, project_id=policy.project_id,
            dqe_db_path=policy.dqe_db_path, artifact_root=policy.artifact_root,
            run_manifests=manifests,
        )

    def provider_readiness() -> tuple[bool, str | None]:
        """分析執行依賴是否可用。

        readiness 只表示**本地設定與 adapter 可用**；它不發 provider
        ping，也不保證 quota 或遠端服務健康。
        """
        if _agent_router is None:
            return False, ("the analysis provider is not configured on this daemon; "
                           "the task was not started and no authorization was used; "
                           + _PROVIDER_REMEDY)
        return True, None

    def read_access_check(member_id: str, project_id: str) -> tuple[bool, str | None]:
        """`get` 要求現行 policy 仍允許這個 member/project。

        **不**要求舊 task 的 policy digest 與今天相同——只有執行授權需要
        綁原 digest。
        """
        from terminux_engine.bounded_execution.analysis_tasks.access_policy import (
            AccessPolicyError,
        )

        if _analysis_access_guard is None:
            return False, "no analysis access policy is configured on this daemon"
        try:
            policy = _analysis_access_guard.current()
        except AccessPolicyError as exc:
            return False, exc.message
        if not policy.matches_scope(member_id, project_id):
            return False, "the analysis access policy does not cover this member/project"
        return True, None

    def eap_execution_feedback_reviewer(outcome: dict[str, Any]) -> dict[str, Any]:
        """只在 EAP task terminal 後呼叫兩個 logical role；不給 execution authority。"""
        provider = _get_runtime_provider()
        backend = os.environ.get(_ENV_LLM_BACKEND, "").strip()
        if provider is None or backend not in {"gemini", "openai_compatible", "llama_cpp"}:
            return {"feedback_status": "NOT_RUN_PROVIDER_UNAVAILABLE",
                    "execution_allowed": False}
        from terminux_engine.expert_ai.eap1 import ChatExpertAdapter
        from terminux_engine.expert_ai.eap2 import review_execution_outcome
        from terminux_engine.llm.agent_adapter import AgentLLMAdapter

        adapter = AgentLLMAdapter(provider, provider_type=backend)
        model = str(getattr(getattr(provider, "_config", None), "model", "unknown"))
        role_adapter = ChatExpertAdapter(adapter.chat, provider=backend, model=model)
        return review_execution_outcome(
            outcome, domain_adapter=role_adapter, global_adapter=role_adapter)

    return AnalysisTaskService(TaskServiceDeps(
        event_store=_event_store,
        daemon_instance_id=_get_daemon_instance_id(),
        proposal_registry=_proposal_registry,
        create_task_proposal=_create_task_owned_proposal,
        dispatch_deps_factory=lambda: _build_dispatch_deps("analysis_task_control"),
        catalog_provider=catalog_provider,
        activation_gate=production_activation_gate(),
        request_context=get_current_request_context,
        build_steps=template.build_task_steps,
        validate_runs=validate_runs,
        summarize=template.summarize_task_action,
        submit=_submit_analysis_worker,
        template_id=template.TEMPLATE_ID,
        access_binding_provider=access_binding_provider,
        provider_readiness=provider_readiness,
        read_access_check=read_access_check,
        flow=_build_flow_controller_for(
            catalog_provider=catalog_provider,
            # flow 的每次派送走 limited activation gate，不是裸的
            # base gate。analysis task 仍用 `activation_gate` 上面那一個。
            activation_gate=_build_limited_activation_gate(),
        ),
        flow_activation_gate=_build_limited_activation_gate(),
        # bounded scheduler 與 profile 描述（給 planner／`/profiles`）。
        scheduler=_get_flow_scheduler(),
        describe_profile=_describe_fixed_plan,
        eap_execution_feedback_reviewer=eap_execution_feedback_reviewer,
    ))


def _consumption_evidence_for_task(output_root: Any, *, platform: str, design: str,
                                   variant: str, expected_value: Any = None,
                                   parameter_name: str = "CORE_UTILIZATION") -> Any:
    """消費證據依**這個 task** 的參數名稱選來源，不讀 daemon-global profile。"""
    from terminux_engine.packages.orfs_run_intelligence import flow_evidence as FE

    return FE.consumption_evidence_for_parameter(
        parameter_name, output_root, platform=platform, design=design,
        variant=variant, expected_value=expected_value)


def _build_flow_controller_for(*, catalog_provider: Any, activation_gate: Any) -> Any:
    """組出 flow controller。

    profile 是 task-scoped，controller 永遠可組；每一個需要 profile 的
    點都經由 `profile_for_task(task_id)` 取 task-owned snapshot。lineage 一律
    寫進既有 DLE ledger（`~/.terminux/lineage/ledger.jsonl`）。
    """
    from terminux_engine.bounded_execution.analysis_tasks import flow_composition as FC
    from terminux_engine.bounded_execution.analysis_tasks import flow_worker as FW
    from terminux_engine.bounded_execution.analysis_tasks.store import TaskEventWriter
    from terminux_engine.lineage.flow_lineage import default_flow_ledger_path
    from terminux_engine.packages.orfs_run_intelligence import (
        analysis_task_template as template,
        flow_evidence as FE,
    )

    def _task_lookup(task_id: str) -> Any:
        service = _analysis_task_service
        if service is None:
            return None
        live = service._flow_tasks.get(task_id)
        return live.task if live is not None else None

    def _live_flow_lookup(task_id: str) -> Any:
        service = _analysis_task_service
        if service is None:
            return None
        return service._flow_tasks.get(task_id)

    def _profile_for_task(task_id: str) -> Any:
        service = _analysis_task_service
        return service.profile_for_task(task_id) if service is not None else None

    variant_writer = FC.build_variant_writer(_profile_for_task, _task_lookup)
    repair_writer = FC.build_repair_writer(_profile_for_task, _task_lookup,
                                           _live_flow_lookup)
    binder = FC.build_service_binder(lambda: _orchestration_service, variant_writer,
                                     repair_writer, _profile_for_task)
    scheduler = _get_flow_scheduler()

    step_deps = FW.FlowStepDeps(
        writer=TaskEventWriter(_event_store),
        proposal_registry=_proposal_registry,
        create_task_proposal=_create_task_owned_proposal,
        dispatch_deps_factory=lambda: _build_dispatch_deps("flow_task_control"),
        catalog_provider=catalog_provider,
        request_context=get_current_request_context,
        activation_gate=activation_gate,
        profile_for_task=_profile_for_task,
        ledger=_orchestration_service._ledger if _orchestration_service else None,
        # dict payload 契約（daemon 身分寫入的列）；package flow／run evidence 以 dict 消費。
        receipt_reader=_receipt_payload_reader,
        bind_services=binder,
        verify_inputs=FC.verify_inputs_for,
        summarize=template.summarize_task_action,
        read_effective_settings=FE.read_effective_settings,
        consumption_evidence=_consumption_evidence_for_task,
        eda_slots=scheduler.eda_slots,
        provider_slots=scheduler.provider_slots,
        lineage_ledger_path=default_flow_ledger_path(),
    )
    return FC.build_flow_controller(
        profile_for_task=_profile_for_task, step_deps=step_deps,
        orchestration_service_getter=lambda: _orchestration_service,
        task_lookup=_task_lookup,
    )


def _get_analysis_task_service() -> Any | None:
    global _analysis_task_service  # noqa: PLW0603
    with _analysis_task_lock:
        if _analysis_task_service is None:
            try:
                _analysis_task_service = _build_analysis_task_service()
            except Exception as exc:  # noqa: BLE001 - 組不出來就是沒有這個能力
                _log.warning("analysis task service unavailable: %r", exc)
                return None
        return _analysis_task_service


def _analysis_task_unavailable() -> dict[str, Any]:
    from terminux_engine.bounded_execution.analysis_tasks.errors import (
        TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
    )

    return {"status": "error", "error_code": TASK_NOT_FOUND_OR_OUT_OF_SCOPE,
            "message": "analysis tasks are not configured on this daemon"}


def _run_analysis_task(operation: str, **kwargs: Any) -> dict[str, Any]:
    """typed IPC、slash control 與 Agent intent 的**唯一**共用入口。

    三條路徑呼叫同一個 service，不各自複製授權或狀態邏輯。
    """
    service = _get_analysis_task_service()
    if service is None:
        return _analysis_task_unavailable()
    with agent_audit.agent_turn():
        return getattr(service, operation)(**kwargs)


def _create_analysis_task_from_intent(
    *, current_run_id: str, baseline_run_id: str, question: str,
) -> dict[str, Any]:
    """Agent 的 `proposed_analysis_task` 的 host 端消費點。

    走與 typed `agent.analysis_task.create` **完全相同**的
    `_run_analysis_task("create", ...)`——不複製任何驗證或授權邏輯，也不
    因為來源是 LLM 而放寬任何一項。
    """
    return _run_analysis_task(
        "create", current_run_id=current_run_id,
        baseline_run_id=baseline_run_id, question=question,
    )


def _create_flow_task_from_intent(*, profile_id: str, question: str) -> dict[str, Any]:
    """Agent 的 `proposed_flow_task` 的 host 端消費點。

    走與 typed `agent.flow_task.create` **完全相同**的 service 呼叫；
    不因為來源是 LLM 而放寬任何一項。
    """
    return _run_analysis_task("create_flow_task", profile_id=profile_id,
                              question=question)


def _list_task_profiles_for_caller() -> list[dict[str, Any]]:
    """SAFE `list_task_profiles`：列的是 caller **本 session 已選用**的
    task-scoped profile（`/profile <path>` 或 `agent.flow_task.select_profile`），
    不是 daemon-global 的一份。每筆含 profile_id、方案描述、來源身分與執行
    計畫摘要；**不含 secrets**。
    """
    service = _get_analysis_task_service()
    if service is None:
        return []
    try:
        response = service.list_profiles()
    except Exception as exc:  # noqa: BLE001 - 取不到就是沒有可用 profile
        _log.warning("task profiles unavailable to this caller: %r", exc)
        return []
    if response.get("status") != "ok":
        return []
    return list(response.get("profiles") or [])


def _describe_fixed_plan(profile: Any) -> str:
    """依 profile **自己的**固定內容描述這個方案會做什麼。

    每個 template 的敘述都由 profile 的結構化欄位組出來——不是把所有
    profile 都說成「改一個參數再比較」（v3 的兩個 slot 參數值相同，那樣寫
    會讀起來像沒做事，模型因此無法把使用者的請求對應到它）。
    """
    design = f"{profile.design_id} on {profile.platform_id}"
    parameter = profile.parameter
    recipe = profile.repair_recipe
    # v5 的 recipe 是**切換合成策略**，沒有 restructure 的
    # algorithm／target／invocations 欄位。用它們去描述會直接 AttributeError，
    # 結果是這個 profile 根本送不到 planner 面前，使用者的自然語言也就永遠
    # 對應不到它（attempt 1 就是這樣沒有形成 DRAFT）。
    # 這個 recipe 的唯一工程操作是一份**已核准的 RTL patch**。
    # 用合成策略或 restructure 欄位去描述它同樣會 AttributeError，profile 也
    # 就送不到 planner 面前。必須明講會修改隔離候選的哪兩個模組。
    if recipe is not None and getattr(recipe, "rtl_patch", None) is not None:
        patch = recipe.rtl_patch
        modules = " and ".join(patch.modules)
        return (
            f"Run a complete flow for {design} twice with the same SDC, "
            f"libraries, tool image and synthesis settings: once as the "
            f"baseline with the original RTL, and once as the candidate after "
            f"applying one approved, server-fixed RTL patch to an isolated "
            f"copy of {patch.path} ({recipe.recipe_id}). The patch rewrites the "
            f"combinational arithmetic of {modules} into a fixed "
            f"parallel-prefix borrow network to repair setup timing; module "
            f"interfaces, state, reset, enable, handshake and every other "
            f"source file stay byte-identical. The candidate reuses the "
            f"baseline's exact physical core/die bounds "
            f"({recipe.geometry_policy_id}). Both the post-resize and the final "
            f"netlists are proven equivalent to the baseline, and the two runs "
            f"are judged by the fixed acceptance policy "
            f"{recipe.acceptance_policy_id}."
            + (f" Both mapped equivalence proofs are produced by the fixed "
               f"proof policy {recipe.proof_policy_id}: each flop contributes "
               f"exactly one canonical state bit, so the proof covers every "
               f"input and every state."
               if getattr(recipe, "proof_policy_id", None) else "")
            + " The design requirements and constraints are unchanged.")
    if recipe is not None and hasattr(recipe, "strategy_variable"):
        return (
            f"Run a complete flow for {design} twice with the same RTL, SDC, "
            f"libraries and tool image: once as the baseline with "
            f"{recipe.strategy_variable}={recipe.baseline_value} (area-oriented "
            f"synthesis mapping), and once as the candidate with "
            f"{recipe.strategy_variable}={recipe.candidate_value} "
            f"(speed-oriented synthesis mapping) to repair setup timing. The "
            f"candidate reuses the baseline's exact physical core/die bounds "
            f"({recipe.geometry_policy_id}) instead of re-deriving them from "
            f"utilization. Both post-resize and final netlists are proven "
            f"equivalent, and the two runs are judged by the fixed acceptance "
            f"policy {recipe.acceptance_policy_id}. This is the approved, "
            f"server-fixed resynthesis plan ({recipe.recipe_id}); the design "
            f"requirements and constraints are unchanged.")
    # 這個 recipe 的唯一工程操作是**一個 ORFS timing 參數**。它既沒有
    # `rtl_patch`／`strategy_variable`，也沒有 restructure 的
    # `algorithm`／`target`／`invocations`——用下面那個分支去描述它會直接
    # AttributeError，profile 就送不到 planner 面前，使用者的自然語言也就
    # 永遠對應不到它（parametric setup repair attempt 1 就是這樣沒有形成 DRAFT，和 v5／v0.8 當年
    # 踩到的是同一個坑）。
    if recipe is not None and hasattr(recipe, "method"):
        method = recipe.method
        stages = ", ".join(method.consumption_stages)
        return (
            f"Run a complete flow for {design} twice with the same RTL, SDC, "
            f"clock, libraries, corner and tool image: once as the baseline "
            f"with {method.variable}={method.baseline_value} (the ORFS "
            f"default), and once as the candidate with "
            f"{method.variable}={method.candidate_value} to repair setup "
            f"timing. That variable is an existing, documented ORFS timing "
            f"option consumed by repair_timing during the {stages} stages; it "
            f"makes setup repair aim for a positive slack margin instead of "
            f"zero, and it does not relax the clock or change any timing "
            f"requirement. The candidate differs from the baseline by exactly "
            f"that one setting ({recipe.recipe_id}). Both the post-CTS and the "
            f"final netlists are proven equivalent to the baseline by the "
            f"fixed proof policy {recipe.proof_policy_id}, and the two runs "
            f"are judged by the fixed acceptance policy "
            f"{recipe.acceptance_policy_id}. The design requirements and "
            f"constraints are unchanged.")
    if getattr(profile, "is_setup_repair", False) and recipe:
        return (
            f"Run a complete flow for {design} twice at the same "
            f"{parameter.name}={parameter.baseline}: once as the baseline, and "
            f"once with one approved, server-fixed {recipe.algorithm} step "
            f"({recipe.recipe_id}, target {recipe.target}, "
            f"{recipe.invocations} invocation) applied inside the flow to "
            f"repair setup timing. The two runs are then compared and judged by "
            f"the fixed acceptance policy {recipe.acceptance_policy_id}. The "
            f"design requirements, constraints and tool version are unchanged; "
            f"the only difference is the approved step.")
    if getattr(profile, "is_bounded_search", False):
        values = ", ".join(parameter.candidates)
        return (
            f"Run a complete flow for {design} once as the baseline at "
            f"{parameter.name}={parameter.baseline}, then once for each fixed "
            f"candidate value ({values}), compare each candidate against the "
            f"baseline, and keep the best result under the fixed selection "
            f"policy.")
    return (
        f"Run a complete flow for {design} twice: once as the baseline and once "
        f"with {parameter.name} changed from {parameter.baseline} to "
        f"{parameter.candidate}, then compare the two runs.")


def _try_analysis_task_command(query: str) -> str | None:
    """`/task`、`/task-start`、`/task-cancel`。第一 token 精確比對。

    與 `/plan`／`/plan-only`／`/plan-propose` 不會互相誤攔：這裡只認上面
    三個完整 token，且參數個數必須精確相符。
    """
    from terminux_engine.bounded_execution.analysis_tasks.flow_rendering import (
        render_flow_task, render_profile_list, render_profile_selection,
        render_task_list,
    )
    from terminux_engine.bounded_execution.analysis_tasks.rendering import USAGE, render

    stripped = query.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command = parts[0]
    if command not in _ANALYSIS_TASK_COMMANDS:
        return None

    # token 數精確比對；不符一律回 USAGE，不猜。
    if command == "/tasks":
        if len(parts) != 1:
            return USAGE
        return render_task_list(_run_analysis_task("list_tasks"))
    if command == "/profiles":
        if len(parts) != 1:
            return USAGE
        return render_profile_list(_run_analysis_task("list_profiles"))
    if command == "/profile":
        if len(parts) != 2:
            return USAGE
        return render_profile_selection(
            _run_analysis_task("select_profile", profile_path=parts[1]))
    if command == "/task-create":
        if len(parts) < 3:
            return USAGE
        response = _run_analysis_task(
            "create_flow_task", profile_id=parts[1], question=" ".join(parts[2:]))
        # 只有真的建立了草案才加開頭；失敗時第一行就是拒絕訊息。
        if response.get("status") == "error":
            return render_flow_task(response)
        return ("【已建立執行任務草案，尚未授權，未執行任何 EDA】\n"
                + render_flow_task(response))

    # `/task`／`/task-start`／`/task-cancel` 依 task 類型交同一個 service 處理，
    # 並用對應的 renderer（flow task 顯示 profile 來源、執行計畫、queue、lineage
    # 與工程判定；舊版錯用 analysis renderer，看不到這些）。
    is_flow = _is_flow_task(parts[1]) if len(parts) > 1 else False
    show = render_flow_task if is_flow else render
    if command == "/task":
        if len(parts) != 2:
            return USAGE
        return show(_run_analysis_task("get", task_id=parts[1]))
    if command == "/task-start":
        if len(parts) != 3:
            return USAGE
        operation = "start_flow_task" if is_flow else "start"
        return show(_run_analysis_task(operation, task_id=parts[1],
                                       task_digest=parts[2]))
    if len(parts) != 3:
        return USAGE
    operation = "cancel_flow_task" if is_flow else "cancel"
    return show(_run_analysis_task(operation, task_id=parts[1],
                                   task_digest=parts[2]))


def _try_eap3_work_command(query: str) -> str | None:
    """Engineering Work 正式人用入口；與 typed RPC 共用 `_run_eap3`。"""
    from terminux_engine.expert_ai.eap3_rendering import USAGE, render

    stripped = query.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    command = parts[0]
    if command not in _EAP3_WORK_COMMANDS:
        return None
    if command == "/works":
        return render(_run_eap3("list")) if len(parts) == 1 else USAGE
    if command == "/work-create":
        if len(parts) < 4:
            return USAGE
        return render(_run_eap3("create", profile_id=parts[1], package_id=parts[2],
                                objective=" ".join(parts[3:])))
    if command == "/work-authorize":
        if len(parts) != 3:
            return USAGE
        mode = {"plan": "PLAN_ONLY", "proposal": "PER_PROPOSAL",
                "bounded": "BOUNDED_EXPLORATION"}.get(parts[2])
        return USAGE if mode is None else render(_run_eap3(
            "preview_authorization", work_id=parts[1], mode=mode))
    if command == "/work-answer":
        if len(parts) < 3:
            return USAGE
        return render(_run_eap3("answer", work_id=parts[1],
                                message=" ".join(parts[2:])))
    operations = {
        "/work": "get", "/work-approve": "approve",
        "/work-continue": "continue_work", "/work-pause": "pause",
        "/work-resume": "resume", "/work-cancel": "cancel",
        "/work-takeover": "takeover",
    }
    if len(parts) != 2:
        return USAGE
    return render(_run_eap3(operations[command], work_id=parts[1]))


def _is_flow_task(task_id: str) -> bool:
    """這個 id 是否為 flow task。只看 host 產生的固定前綴，不猜。"""
    return isinstance(task_id, str) and task_id.startswith("ftask_")


def _try_proposal_control_command(query: str) -> str | None:
    """`/proposals`、`/approve <id> <digest>`、`/reject <id> <digest>`
    在送往 provider 之前由 host 截取。回傳 None 代表不是 control command，
    caller 應照舊走 Agent/LLM 路徑——自然語言（例如「我同意」）永遠不會命中
    這裡的嚴格 slash-command 格式，所以永遠不會改變任何 proposal 狀態。"""
    stripped = query.strip()
    if not stripped.startswith("/"):
        return None
    if _proposal_registry is None:
        return None
    parts = stripped.split()
    command = parts[0]
    try:
        context = get_current_request_context()
    except Exception:
        return None

    with agent_audit.agent_turn():
        if command == "/proposals":
            proposals = _proposal_registry.list_for_scope(context)
            if not proposals:
                return "目前沒有 pending 或近期的 proposal。"
            lines = ["Proposals（此 session/member/project 範圍內）："]
            for p in proposals:
                lines.append(
                    f"- {p['proposal_id']}  status={p['status']}  action={p['action_name']}  "
                    f"digest={p['proposal_digest']}"
                )
            return "\n".join(lines)

        if command in ("/approve", "/reject"):
            if len(parts) != 3:
                return f"用法：{command} <proposal_id> <proposal_digest>"
            proposal_id, proposal_digest = parts[1], parts[2]
            transition = _proposal_registry.approve if command == "/approve" else _proposal_registry.reject
            target = "APPROVED" if command == "/approve" else "REJECTED"
            view, denial_code = transition(proposal_id, proposal_digest, context)
            if view is None:
                agent_audit.emit("action.proposal.control_denied", tool_call_id=proposal_id,
                                 denial_code=denial_code, executor_entered=False,
                                 policy_source="chat_query_control_command")
                return "無法核准/拒絕：proposal 不存在、已過期、digest 不符，或不屬於目前的 session。"
            agent_audit.emit(
                "action.proposal.approved" if target == "APPROVED" else "action.proposal.rejected",
                tool_call_id=proposal_id, tool_name=view.get("action_name"),
                result_status=target, executor_entered=False,
                policy_source="chat_query_control_command",
            )
            if target == "APPROVED" and _plan_registry is not None:
                _plan_registry.on_step_approved(proposal_id)
            return (
                f"proposal_id={proposal_id}\n"
                f"proposal_status={view['status']}\n"
                f"execution_status={view['execution_status']}\n"
                f"用 /execute {proposal_id} {proposal_digest} 一次性執行（僅同一 session 可執行一次）。"
            )

        if command == "/execute":
            if len(parts) != 3:
                return f"用法：{command} <proposal_id> <proposal_digest>"
            proposal_id, proposal_digest = parts[1], parts[2]
            outcome = _execute_approved_proposal(proposal_id, proposal_digest, context)
            if outcome["denial_code"] is not None:
                return "無法執行：proposal 不存在、未核准、已執行過、digest 不符，或不屬於目前的 session。"
            view = outcome["proposal"] or {}
            return (
                f"proposal_id={proposal_id}\n"
                f"proposal_status={view.get('status')}\n"
                f"execution_status={view.get('execution_status')}\n"
                f"execution_result={view.get('execution_result')}"
            )

        if command == "/results":
            results = _proposal_registry.list_trusted_results_for_scope(context)
            if not results:
                return "目前沒有可用的 trusted action result。"
            active = _proposal_registry.peek_active_result(context)
            active_id = active.get("result_id") if active else None
            lines = ["Trusted action results（此 session/member/project 範圍內）："]
            for r in results:
                marker = " (active)" if r["result_id"] == active_id else ""
                lines.append(
                    f"- {r['result_id']}{marker}  action={r['action_name']}  "
                    f"execution_status={r['execution_status']}  digest={r['result_digest']}"
                )
            return "\n".join(lines)

        if command == "/use-result":
            if len(parts) != 3:
                return f"用法：{command} <result_id> <result_digest>"
            result_id, result_digest = parts[1], parts[2]
            ok = _proposal_registry.set_active_result(result_id, result_digest, context)
            agent_audit.emit("action.result.selected" if ok else "action.result.selection_denied",
                             tool_call_id=result_id, executor_entered=False,
                             policy_source="chat_query_control_command")
            if not ok:
                return "無法選擇：result 不存在、已過期、digest 不符，或不屬於目前的 session。"
            return f"active_result_id={result_id}\n下一個相關 turn 會帶入這筆 result 的內容。"

        if command == "/clear-result":
            _proposal_registry.clear_active_result(context)
            agent_audit.emit("action.result.cleared", executor_entered=False,
                             policy_source="chat_query_control_command")
            return "已清除 active result；後續 turn 不會再自動帶入任何舊的 result。"

        # ── plan control path。刻意不提供 /approve-plan、
        # /execute-plan、/approve-all——每一步仍必須各自走 /approve、
        # /execute，這裡只負責 plan 本身的唯讀查詢與 cancel。
        if command in ("/approve-plan", "/execute-plan", "/approve-all"):
            agent_audit.emit("action.plan.control_denied", denial_code="PLAN_BATCH_CONTROL_NOT_SUPPORTED",
                             executor_entered=False, policy_source="chat_query_control_command")
            return (
                "這個能力不存在：plan 沒有整條一次核准或一次執行的機制。"
                "每一步都必須各自走 /approve <proposal_id> <proposal_digest> 與 "
                "/execute <proposal_id> <proposal_digest>。"
            )

        if _plan_registry is None:
            return None

        if command == "/plans":
            plans = _plan_registry.list_for_scope(context)
            if not plans:
                return "目前沒有 plan。"
            lines = ["Plans（此 session/member/project 範圍內）："]
            for p in plans:
                lines.append(
                    f"- {p['plan_id']}  status={p['status']}  revision={p['revision']}  "
                    f"active_step={p['active_step_index']}/{len(p['steps'])}  digest={p['plan_digest']}"
                )
            return "\n".join(lines)

        if command == "/plan":
            if len(parts) != 2:
                return "用法：/plan <plan_id>"
            view = _plan_registry.get_for_scope(parts[1], context)
            if view is None:
                agent_audit.emit("action.plan.control_denied", tool_call_id=parts[1],
                                 denial_code="PLAN_NOT_FOUND", executor_entered=False,
                                 policy_source="chat_query_control_command")
                return "無法查看：plan 不存在、已過期，或不屬於目前的 session。"
            lines = [
                f"plan_id={view['plan_id']}", f"plan_digest={view['plan_digest']}",
                f"revision={view['revision']}", f"status={view['status']}",
                f"objective={view['objective']}", f"active_step_index={view['active_step_index']}",
                "steps:",
            ]
            for s in view["steps"]:
                lines.append(
                    f"  - step_index={s['step_index']} action={s['action_name']} status={s['status']} "
                    f"proposal_id={s['proposal_id']} execution_status={s['execution_status']} "
                    f"business_status={s['business_status']}"
                )
            return "\n".join(lines)

        if command == "/use-plan":
            if len(parts) != 2:
                return "用法：/use-plan <plan_id>"
            ok = _plan_registry.set_active_plan(parts[1], context)
            if not ok:
                agent_audit.emit("action.plan.control_denied", tool_call_id=parts[1],
                                 denial_code="PLAN_NOT_FOUND", executor_entered=False,
                                 policy_source="chat_query_control_command")
                return "無法選擇：plan 不存在、已過期，或不屬於目前的 session。"
            return f"active_plan_id={parts[1]}\n下一個相關 turn 會以這個 plan 為 continuation context。"

        if command == "/cancel-plan":
            if len(parts) != 3:
                return "用法：/cancel-plan <plan_id> <plan_digest>"
            view, denial_code = _plan_registry.cancel_plan(parts[1], parts[2], context)
            if view is None:
                agent_audit.emit("action.plan.control_denied", tool_call_id=parts[1],
                                 denial_code=denial_code, executor_entered=False,
                                 policy_source="chat_query_control_command")
                return "無法取消：plan 不存在、已結束、digest 不符，或不屬於目前的 session。"
            return f"plan_id={parts[1]}\nplan_status={view['status']}"
    return None


_PROFILE_INVALID_ANSWER = (
    "TASK_POLICY_INVALID: this administrator profile is invalid. No AI request was sent."
)
_PROFILE_AGENT_UNAVAILABLE_ANSWER = (
    "AGENT_UNAVAILABLE: the AI agent is not configured in this administrator profile. "
    "No AI request was sent."
)
_PROFILE_REQUEST_FAILED_ANSWER = (
    "AGENT_REQUEST_FAILED: the AI request was refused or failed. No fallback was attempted."
)
_PROFILE_AGENT_BUSY_ANSWER = (
    "AGENT_BUSY: the AI agent is handling another request; try again shortly. "
    "No AI request was sent and no fallback was attempted."
)
_STABLE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?::[A-Za-z0-9_.]+)?$")


#: 本 request 最近一次 Agent turn 的歷史投影摘要（只有計數）；`ecc.chat_query` 處理器在呼叫前
#: 重設、呼叫後讀取並還原，GUI 據此顯示未納入 AI 上下文的回合數。
_chat_context_status: ContextVar[dict[str, Any] | None] = ContextVar("terminux_chat_context_status", default=None)


def _try_agent_chat_query(query: str, accumulator: Any, conversation: Any = None,
                          status_out: dict[str, Any] | None = None) -> tuple[str | None, list]:
    """任務Profile以正式stream buffer建立來源邊界，關閉時保留原路徑。

    本 turn 的歷史投影摘要（只有計數；見 `task_scope.history_status`）寫入 `_chat_context_status`，
    `status_out` 給定時另外寫入該 dict。摘要不寫入對話歷史，也不進入任何請求。
    """
    from terminux_engine.llm.task_scope import TaskScopeRejected, bind_conversation, history_status, load_profile
    try:
        profile = load_profile()
    except TaskScopeRejected:
        # Profile 檔存在但無效：學員看到固定碼，不是 RPC 內部錯誤；沒有任何請求被送出。
        return _PROFILE_INVALID_ANSWER, []
    if profile is not None:
        from terminux_engine.llm.run_binding import AUTHORITY_UNAVAILABLE
        # 沒有 EventStore 時傳入「權威不可用」：綁定 Run 的核對 fail-closed，而不是被略過。
        projector: Any = AUTHORITY_UNAVAILABLE
        if _event_store is not None:
            from terminux_engine.execution_receipts.wiring import EventStoreReceiptProjector
            projector = EventStoreReceiptProjector(_event_store, user_id=_user_id)
        denied_names_for = getattr(_agent_runner, "denied_capability_names", None)
        denied = tuple(denied_names_for(profile)) if callable(denied_names_for) else ()
        with bind_conversation(conversation, projector=projector, denied_names=denied):
            status = history_status()
            if status is not None:
                _chat_context_status.set(status)
                if status_out is not None:
                    status_out.update(status)
            return _try_agent_chat_query_impl(query, accumulator, conversation)
    return _try_agent_chat_query_impl(query, accumulator, conversation)


def _profile_denial_answer(result: Any) -> str:
    """Profile 模式把 host gate 的穩定碼直接作為回答；其他錯誤只給固定句，不外露文字。"""
    code = getattr(result, "error_code", None)
    if isinstance(code, str) and _STABLE_CODE_RE.match(code):
        return f"{code}: the AI request was refused by the local policy. No fallback was attempted."
    error = getattr(result, "error", None)
    if isinstance(error, str) and _STABLE_CODE_RE.match(error):
        return f"{error}: the AI request was refused by the local policy. No fallback was attempted."
    return _PROFILE_REQUEST_FAILED_ANSWER


def _try_agent_chat_query_impl(
    query: str,
    accumulator: Any,
    conversation: Any = None,
) -> tuple[str | None, list]:
    """嘗試用 AgentRunner 回答 `@@` query。

    回傳 (answer, evidence_list)：
    - (str, list)：Agent 成功給出 non-empty answer，caller 用此 answer
    - (None, []) ：Agent 不可用、超時、失敗、或回 NO_ACTION → caller 應 fallback
                   到 ai_companion 路徑

    本函數不會拋例外。failure 路徑都收斂成 (None, [])；唯一例外是模型回了
    未解析的 tool call：回 (明確錯誤訊息, evidence)，因為 fallback 會
    打到同一個 provider。
    """
    from terminux_engine.llm.task_scope import bound_context_epoch, bound_policy_identity, profile_containment_active

    contained = profile_containment_active()
    if _agent_runner is None:
        return (_PROFILE_AGENT_UNAVAILABLE_ANSWER, []) if contained else (None, [])

    try:
        # 1) Build minimal ECC snapshot 透過 get_current_ecc tool 的 executor
        from terminux_engine.llm.agent_prompt import build_agent_system_prompt
        from terminux_engine.llm.tools.get_current_ecc import (
            create_tool as _create_ecc_tool,
        )

        ecc_tool = _create_ecc_tool(accumulator) if accumulator is not None else None
        try:
            ecc_state = ecc_tool() if ecc_tool is not None else None
        except Exception as ecc_exc:
            _log.warning("Agent: ecc snapshot build failed (%s); using None", ecc_exc)
            ecc_state = None

        # 補齊所有 prompt context section
        try:
            _ph_recent_checks = _get_recent_checks()
        except Exception:
            _ph_recent_checks = None
        try:
            _ph_session_history = _get_session_history_summary()
        except Exception:
            _ph_session_history = None
        try:
            _ph_user_profile = _get_user_profile_dict()
        except Exception:
            _ph_user_profile = None
        try:
            _ph_team_summary = _get_team_summary()
        except Exception:
            _ph_team_summary = None
        try:
            _ph_playbook_summary = _get_playbook_summary()
        except Exception:
            _ph_playbook_summary = None
        _ph_conv_text = _get_conversation_history_text(conversation)

        system_prompt = build_agent_system_prompt(
            ecc_state=ecc_state,
            recent_checks=_ph_recent_checks,
            session_history=_ph_session_history,
            user_profile=_ph_user_profile,
            team_snapshot_summary=_ph_team_summary,
            playbook_context=_ph_playbook_summary,
            conversation_history_text=_ph_conv_text,
            deep_analysis_summary=_deep_analysis_summary,
            language=load_preferred_language(),
        )

        # 2) Session lock on realtime slot（2s timeout；忙就讓給 fallback）
        if _agent_router is not None:
            acquired = _agent_router.acquire_session("realtime", timeout=2.0)
            if not acquired:
                if contained:
                    _log.info("Agent: realtime session lock busy in profile mode → fixed answer")
                    return _PROFILE_AGENT_BUSY_ANSWER, []
                _log.info("Agent: realtime session lock busy → fallback to ai_companion")
                return None, []
        else:
            acquired = False

        # 這是唯一一次 consume active TrustedActionResult 的呼叫點——
        # mode="auto"（`/execute` 成功後自動設定）讀一次就清除，避免陳舊
        # result 被無限期帶進之後所有不相關的 turn；mode="explicit"
        # （`/use-result` 明確選擇）維持不變。只從 daemon-side
        # ProposalRegistry 取得，query 文字裡任何看起來像 host result 的
        #內容都不會被這裡採信。
        _trusted_result = None
        if _proposal_registry is not None:
            try:
                _chat_context = try_get_current_request_context()
                if _chat_context is not None:
                    _trusted_result = _proposal_registry.consume_active_result_for_turn(_chat_context)
            except Exception as _tr_exc:
                _log.warning("Agent: consume_active_result_for_turn failed (non-fatal): %s", _tr_exc)

        try:
            result = _agent_runner.run(
                query=query,
                system_context=system_prompt,
                trigger_source="chat_query",
                mode="sync",
                trusted_result=_trusted_result,
            )
        finally:
            if acquired and _agent_router is not None:
                try:
                    _agent_router.release_session("realtime")
                except Exception as rel_exc:
                    _log.warning("Agent: release_session raised: %s", rel_exc)

        if result.error:
            # 模型回了未解析的 tool call 時，fallback 也會打到同一個
            # provider 而再失敗；直接把明確的錯誤當成回覆顯示。
            from terminux_engine.llm.unparsed_tool_call import user_message_from_error

            unparsed = user_message_from_error(result.error)
            if unparsed is not None:
                _log.warning("AgentRunner error: %s", unparsed)
                return unparsed, list(result.evidence)
            if contained:
                # Profile 模式沒有 companion fallback：拒絕碼直接回給學員，
                # 不讓 ECC 摘要（cwd／命令）進入回答或歷史。
                _log.warning("AgentRunner error in profile mode: %s", result.error_code or result.error)
                if isinstance(result.error_code, str) and result.error_code.startswith("EVIDENCE_"):
                    # 綁定 Run 的 evidence 已不可信：之前由它衍生的回合不得再隨歷史外送。
                    from terminux_engine.llm import context_epoch
                    context_epoch.advance(get_current_request_context().session_id,
                                          context_epoch.REASON_EVIDENCE_DENIED)
                return _profile_denial_answer(result), list(result.evidence)
            _log.warning(
                "AgentRunner error: %s → fallback to ai_companion", result.error
            )
            return None, []
        if not result.answer:
            return (_PROFILE_REQUEST_FAILED_ANSWER, []) if contained else (None, [])

        if conversation is not None:
            if contained:
                # 只有經最終 Gate 送出並回來的回合才標記為可外送，並綁定本 turn 的
                # policy revision 與目的地；policy 讀不到時回合仍留在本機但不可外送。
                # epoch 是 turn 開始時的值：turn 期間若已解除／重新綁定，這組回合屬於舊 epoch，
                # 之後不會進入新 epoch 的可外送歷史。
                identity = bound_policy_identity()
                epoch = bound_context_epoch()
                revision = identity.sha256 if identity is not None else None
                destination = identity.destination if identity is not None else None
                provenance = "agent_c2" if identity is not None and epoch is not None else "unattributed"
                conversation.add_user(query, provenance=provenance, policy_revision=revision,
                                      destination=destination, context_epoch=epoch)
                conversation.add_assistant(result.answer, provenance=provenance,
                                           policy_revision=revision, destination=destination,
                                           context_epoch=epoch)
            else:
                conversation.add_user(query)
                conversation.add_assistant(result.answer)
        return result.answer, list(result.evidence)

    except Exception as exc:
        _log.warning("Agent chat path failed (non-fatal, fallback): %s", exc)
        return (_PROFILE_REQUEST_FAILED_ANSWER, []) if contained else (None, [])


def _answer_query_without_ecc(query: str) -> str:
    """沒有 ECC context 時的單次 provider 問答；失敗只回報 LLM 不可用。"""
    unavailable = "AI 問答暫時不可用：LLM provider 未設定或未回應，請檢查 LLM 設定後重試。"
    acquired = _llm_lock.acquire(timeout=15.0)
    if not acquired:
        return "AI 問答暫時忙碌，請稍後重試。"
    try:
        provider = _get_runtime_provider()
        if provider is None:
            return unavailable
        prompt = (
            "你是嵌入 EDA terminal 的 IC 設計助理。"
            f"請依使用者偏好語言 {load_preferred_language()} 簡潔回答。\n"
            "ECC 未啟用；請勿臆測當前 session、stage、quality gate 或 corner 狀態。\n"
            f"使用者問題：{query}"
        )
        raw = provider.complete(prompt)
        return raw.strip() if raw and raw.strip() else unavailable
    except Exception as exc:
        _log.warning("Chat without ECC failed (non-fatal): %s", exc)
        return unavailable
    finally:
        _llm_lock.release()


# ─── Observability helpers ────────────────────────────────────────────────────

def _append_preexec_log(record: dict) -> None:
    """
    Backward-compatible shim。
    record dict → ObservabilityEvent → append_observability_event()。
    不得移除此函式。
    """
    try:
        event = ObservabilityEvent(**record)
        append_observability_event(event)
    except Exception as exc:
        _log.warning("Failed to write preexec observability log: %s", exc)


# ─── PreExecDecision serializer ───────────────────────────────────────────────

def _serialize_preexec_decision(decision: PreExecDecision) -> dict:
    """
    PreExecDecision → JSON-RPC result dict。
    - 只包含 action／status／message（不含 tool_name／runfile_path）
    - explanation 為 None 時 key 仍然存在，值為 JSON null
    - 不使用 dataclasses.asdict()（會混入 tool_name／runfile_path）
    """
    result: dict = {
        "action": decision.action,
        "status": decision.status,
        "message": decision.message,
        "explanation": None,
    }
    if decision.explanation is not None:
        result["explanation"] = {
            "summary": decision.explanation.summary,
            "likely_cause": decision.explanation.likely_cause,
            "suggested_actions": list(decision.explanation.suggested_actions),
        }
    return result


# ─── Response helpers ─────────────────────────────────────────────────────────

def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


# ─── orchestration.* params validation（G-2）───────────────────────────────────

_ORCHESTRATION_START_KEYS = frozenset({"workflow", "config"})
_ORCHESTRATION_STATUS_KEYS = frozenset({"run_id"})
_ORCHESTRATION_LIST_RUNS_KEYS = frozenset({"workflow_type", "status", "pending_review", "limit"})
_ORCHESTRATION_PAUSE_KEYS = frozenset({"run_id"})
_ORCHESTRATION_RESUME_KEYS = frozenset({"run_id"})
_ORCHESTRATION_CANCEL_KEYS = frozenset({"run_id", "immediate"})
_ORCHESTRATION_RETRY_KEYS = frozenset({"run_id", "step_id"})
_ORCHESTRATION_ABANDON_KEYS = frozenset({"run_id"})
_EVIDENCE_BUNDLE_KEYS = frozenset({"orchestration_run_id"})
_EVIDENCE_SET_KEYS = frozenset({"run_ids", "comparison_focus"})


def _check_orchestration_unknown_keys(params: dict, allowed: frozenset[str]) -> str | None:
    unknown = set(params) - allowed
    if unknown:
        return f"unknown params key(s): {sorted(unknown)}"
    return None


def _check_orchestration_start_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_START_KEYS)
    if unknown:
        return unknown
    if "workflow" not in params or type(params["workflow"]) is not str:
        return "Missing or invalid required field: 'workflow'"
    if "config" not in params or type(params["config"]) is not dict:
        return "Missing or invalid required field: 'config'"
    return None


def _check_orchestration_status_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_STATUS_KEYS)
    if unknown:
        return unknown
    if "run_id" not in params or type(params["run_id"]) is not str:
        return "Missing or invalid required field: 'run_id'"
    return None


def _check_orchestration_list_runs_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_LIST_RUNS_KEYS)
    if unknown:
        return unknown
    if (
        "workflow_type" in params
        and params["workflow_type"] is not None
        and type(params["workflow_type"]) is not str
    ):
        return "Field 'workflow_type' must be a string or null"
    if "status" in params and params["status"] is not None and type(params["status"]) is not str:
        return "Field 'status' must be a string or null"
    if "pending_review" in params and type(params["pending_review"]) is not bool:
        return "Field 'pending_review' must be a boolean"
    if "limit" in params and type(params["limit"]) is not int:
        return "Field 'limit' must be an int"
    return None


def _check_orchestration_pause_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_PAUSE_KEYS)
    if unknown:
        return unknown
    if "run_id" not in params or type(params["run_id"]) is not str:
        return "Missing or invalid required field: 'run_id'"
    return None


def _check_orchestration_resume_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_RESUME_KEYS)
    if unknown:
        return unknown
    if "run_id" not in params or type(params["run_id"]) is not str:
        return "Missing or invalid required field: 'run_id'"
    return None


def _check_orchestration_cancel_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_CANCEL_KEYS)
    if unknown:
        return unknown
    if "run_id" not in params or type(params["run_id"]) is not str:
        return "Missing or invalid required field: 'run_id'"
    if "immediate" in params and type(params["immediate"]) is not bool:
        return "Field 'immediate' must be a boolean"
    return None


def _check_orchestration_retry_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_RETRY_KEYS)
    if unknown:
        return unknown
    if "run_id" not in params or type(params["run_id"]) is not str:
        return "Missing or invalid required field: 'run_id'"
    if "step_id" in params and params["step_id"] is not None and type(params["step_id"]) is not str:
        return "Field 'step_id' must be a string or null"
    return None


def _check_orchestration_abandon_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _ORCHESTRATION_ABANDON_KEYS)
    if unknown:
        return unknown
    if "run_id" not in params or type(params["run_id"]) is not str:
        return "Missing or invalid required field: 'run_id'"
    return None


def _check_evidence_bundle_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _EVIDENCE_BUNDLE_KEYS)
    if unknown:
        return unknown
    if "orchestration_run_id" not in params or type(params["orchestration_run_id"]) is not str:
        return "Missing or invalid required field: 'orchestration_run_id'"
    return None


def _check_evidence_set_params(params: dict) -> str | None:
    unknown = _check_orchestration_unknown_keys(params, _EVIDENCE_SET_KEYS)
    if unknown:
        return unknown
    run_ids = params.get("run_ids")
    if type(run_ids) is not list or not (2 <= len(run_ids) <= 3):
        return "Field 'run_ids' must be a list of 2 to 3 strings"
    if not all(type(item) is str for item in run_ids):
        return "Field 'run_ids' must be a list of strings"
    comparison_focus = params.get("comparison_focus", "all")
    if comparison_focus not in {"status", "steps", "dqe", "lineage", "all"}:
        return "Field 'comparison_focus' must be one of status|steps|dqe|lineage|all"
    return None


def _run_orchestration_facade(req_id: Any, call: Callable[[], dict]) -> dict:
    """執行 shared request facade 呼叫並統一映射 error／success；不洩漏 traceback。"""
    try:
        result = call()
    except OrchestrationApiError as exc:
        return _err(req_id, exc.code, exc.message, exc.data)
    except Exception:
        _log.exception("orchestration request failed unexpectedly")
        return _err(req_id, _ERR_INTERNAL, "internal error")
    return _ok(req_id, result)


# ─── Params validation ────────────────────────────────────────────────────────

def _check_pre_exec_params(params: dict) -> str | None:
    for field in ("command", "cwd", "ledger_path"):
        if field not in params:
            return f"Missing required field: '{field}'"
    return None


def _check_post_exec_params(params: dict) -> str | None:
    for field in ("command", "cwd", "exit_code", "ledger_path"):
        if field not in params:
            return f"Missing required field: '{field}'"
    if not isinstance(params["exit_code"], int):
        return "Field 'exit_code' must be an int"
    return None


def _check_shell_pre_exec_params(params: dict) -> str | None:
    if "command" not in params:
        return "Missing required field: 'command'"
    return None


def _check_shell_post_exec_params(params: dict) -> str | None:
    if "exit_code" not in params:
        return "Missing required field: 'exit_code'"
    if not isinstance(params["exit_code"], int):
        return "Field 'exit_code' must be an int"
    # command／tool_name 皆為 optional（既有 client 可能省略 command，見
    # dedup_cmd fallback），但若提供必須是 str——否則 host tool identity
    # validation 無法安全處理，此前會在 _sanitize_command 內對非 str 呼叫
    # .sub() 造成未處理的 exception，改在此處明確拒絕。
    if "command" in params and not isinstance(params["command"], str):
        return "Field 'command' must be a string"
    if "tool_name" in params and params["tool_name"] is not None and not isinstance(
        params["tool_name"], str
    ):
        return "Field 'tool_name' must be a string or null"
    return None


def _run_local_command_parsers(query: str) -> str | None:
    """依既有優先序執行五個本機指令 parser；回傳 None 代表不是任何已知指令。

    plan-only 比 proposal control 更早攔截，且不依賴 proposal registry；
    `/task*` 與 `/work*` 是精確 token 比對，不會攔到 `/plan` 或自然語言。
    """
    for parser in (_try_plan_only_command, _try_plan_propose_command, _try_eap3_work_command,
                   _try_analysis_task_command, _try_proposal_control_command):
        answer = parser(query)
        if answer is not None:
            return answer
    return None


_PROFILE_PROVIDER_UNSUPPORTED = (
    "PROFILE_PROVIDER_UNSUPPORTED: this administrator profile requires the "
    "openai_compatible backend. No AI request was sent."
)
_LOCAL_COMMAND_DISABLED_IN_PROFILE_MODE = (
    "LOCAL_COMMAND_DISABLED_IN_PROFILE_MODE: control commands are not available "
    "in this administrator profile. Nothing was executed or sent."
)


#: `ecc.chat_query` 的 `origin` 值域：GUI 以 `@@` 打字送出的控制意圖，或右鍵選字。
CHAT_ORIGIN_AT_COMMAND = "at_command"
CHAT_ORIGIN_SELECTION = "selection"
_CHAT_ORIGINS = frozenset({CHAT_ORIGIN_AT_COMMAND, CHAT_ORIGIN_SELECTION})
#: Default 模式下要求 client 必須帶 `origin` 才能使用本機指令（舊 GUI 的文字一律視為資料）。
#: 預設關閉以維持 Default 既有行為；Profile 模式不受此旗標影響，一律要求 `origin`。
_ENV_CHAT_ORIGIN_REQUIRED = "TERMINUX_CHAT_QUERY_REQUIRE_ORIGIN"
#: 過渡選項：Profile 下沒有 `origin` 的 client 也放行嚴格文法的 `/bind-run`／`/unbind-run`。
#: 預設關閉；開啟代表接受「選取文字恰為該指令時會觸發綁定」的風險邊界。
_ENV_I2_PRIME_BIND_COMMANDS = "TERMINUX_C2_I2_PRIME_BIND_COMMANDS"
_RUN_BINDING_COMMANDS = frozenset({"/bind-run", "/unbind-run"})
_I2_PRIME_GRAMMAR = re.compile(r"^/(bind-run|unbind-run)(\s|$)")


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _profile_contained_chat_answer(query: str, origin: str | None = None) -> str | None:
    """Profile 模式下在 embedding、planner 與 provider 之前決定的回覆。

    `/` 開頭的文字只有在 client 明確標示 `origin=at_command` 時才是控制意圖，且 Profile
    模式只提供 `/bind-run`／`/unbind-run` 兩個本機指令，其餘仍一律拒絕。`selection`
    文字永遠是資料；沒有 `origin` 的舊 client 維持既有的全部拒絕，除非管理端
    明示開啟過渡選項。`origin` 本身不授予任何權限：綁定仍由 EventStore 與 Profile 規則判定。
    """
    from terminux_engine.llm.task_scope import profile_containment_active

    if not profile_containment_active():
        return None
    stripped = query.strip()
    if stripped.startswith("/"):
        if origin == CHAT_ORIGIN_AT_COMMAND:
            binding_answer = _try_run_binding_command(stripped)
            return binding_answer if binding_answer is not None else _LOCAL_COMMAND_DISABLED_IN_PROFILE_MODE
        if origin == CHAT_ORIGIN_SELECTION:
            pass  # 選取文字是資料，交給 C2 類別規則
        elif _flag_enabled(_ENV_I2_PRIME_BIND_COMMANDS) and _I2_PRIME_GRAMMAR.match(stripped):
            binding_answer = _try_run_binding_command(stripped)
            return binding_answer if binding_answer is not None else _LOCAL_COMMAND_DISABLED_IN_PROFILE_MODE
        else:
            return _LOCAL_COMMAND_DISABLED_IN_PROFILE_MODE
    if os.environ.get(_ENV_LLM_BACKEND, "").strip().lower() != "openai_compatible":
        return _PROFILE_PROVIDER_UNSUPPORTED
    return None


def _try_run_binding_command(query: str) -> str | None:
    """`/bind-run <absolute receipt path>` 與 `/unbind-run`；只在 Profile 模式由呼叫端決定可達。

    回傳 None 代表不是這兩個指令。所有驗證在 `llm.run_binding`：receipt 只是定位器，
    EventStore 投影與 Profile `run_binding` 規則才決定是否綁定。
    """
    from terminux_engine.llm import run_binding
    from terminux_engine.llm.task_scope import TaskScopeRejected, load_profile

    parts = query.strip().split(maxsplit=1)
    if not parts or parts[0] not in _RUN_BINDING_COMMANDS:
        return None
    try:
        context = get_current_request_context()
    except Exception:
        return f"{run_binding.CODE_DISABLED}: a canonical session context is required."
    try:
        profile = load_profile()
    except TaskScopeRejected:
        return _PROFILE_INVALID_ANSWER
    if profile is None or profile.run_binding is None:
        return f"{run_binding.CODE_DISABLED}: this administrator profile does not permit run binding."
    with agent_audit.agent_turn():
        if parts[0] == "/unbind-run":
            if len(parts) != 1:
                return f"{run_binding.CODE_USAGE}: /unbind-run takes no arguments."
            result = run_binding.unbind(context=context)
        else:
            if len(parts) != 2 or not parts[1].strip():
                return f"{run_binding.CODE_USAGE}: /bind-run <absolute receipt path>"
            projector = None
            if _event_store is not None:
                from terminux_engine.execution_receipts.wiring import EventStoreReceiptProjector
                projector = EventStoreReceiptProjector(_event_store, user_id=_user_id)
            result = run_binding.bind(receipt_path=parts[1].strip(), profile=profile,
                                      projector=projector, context=context)
        agent_audit.emit("run_binding.command", tool_name=parts[0], result_status=result.code,
                         run_id=result.run_id, executor_entered=False,
                         policy_source="chat_query_control_command")
    return result.message


def _check_chat_query_params(params: dict) -> str | None:
    if "query" not in params or not isinstance(params["query"], str):
        return "Missing or invalid required field: 'query'"
    if "origin" in params and params["origin"] is not None and (
            not isinstance(params["origin"], str) or params["origin"] not in _CHAT_ORIGINS):
        return "Field 'origin' must be one of: at_command, selection"
    return None


def _check_llm_ask_params(params: dict) -> str | None:
    """驗證 standalone 借用 daemon LLM 的請求參數。"""
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "Missing or invalid required field: 'prompt'"
    timeout = params.get("timeout")
    if timeout is not None and (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        return "Field 'timeout' must be a positive number or null"
    return None


def _check_dqe_list_archived_groups_params(params: dict) -> str | None:
    version_dir = params.get("version_dir")
    if not isinstance(version_dir, str) or not version_dir:
        return "Missing or invalid required field: 'version_dir'"
    if not Path(version_dir).is_dir():
        return "Field 'version_dir' must be an existing directory"
    return None


def _check_dqe_list_temporary_groups_params(params: dict) -> str | None:
    db_path = params.get("db_path")
    if db_path is not None and not isinstance(db_path, str):
        return "Field 'db_path' must be a string or null"
    if "retention_days" not in params:
        return None
    retention_days = params.get("retention_days")
    if not isinstance(retention_days, int) or isinstance(retention_days, bool):
        return "Field 'retention_days' must be an int"
    if retention_days <= 0 or retention_days > 3650:
        return "Field 'retention_days' must be in range 1..3650"
    return None


def _check_dqe_load_group_params(params: dict) -> str | None:
    group_ref = params.get("group_ref")
    if not isinstance(group_ref, str) or not group_ref:
        return "Missing or invalid required field: 'group_ref'"
    return None


def _check_dqe_load_attachment_params(params: dict) -> str | None:
    attachment_ref = params.get("attachment_ref")
    if not isinstance(attachment_ref, str) or not attachment_ref:
        return "Missing or invalid required field: 'attachment_ref'"
    preview_request_id = params.get("preview_request_id")
    if preview_request_id is not None and not isinstance(preview_request_id, str):
        return "Field 'preview_request_id' must be a string or null"
    return None


def _check_dqe_checkin_job_params(params: dict) -> str | None:
    job_key = params.get("job_key")
    if not isinstance(job_key, str) or not job_key:
        return "Missing or invalid required field: 'job_key'"
    target_version_path = params.get("target_version_path")
    if not isinstance(target_version_path, str) or not target_version_path:
        return "Missing or invalid required field: 'target_version_path'"
    target_path = Path(target_version_path)
    if target_path.is_absolute() or ".." in target_path.parts:
        return "Field 'target_version_path' must be a safe relative path"
    workspace_root = params.get("workspace_root")
    if workspace_root is not None and not isinstance(workspace_root, str):
        return "Field 'workspace_root' must be a string"
    db_path = params.get("db_path")
    if db_path is not None and not isinstance(db_path, str):
        return "Field 'db_path' must be a string or null"
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return "Field 'dry_run' must be a boolean"
    return None


def _check_dqe_set_retention_params(params: dict) -> str | None:
    retention_days = params.get("retention_days")
    if not isinstance(retention_days, int) or isinstance(retention_days, bool):
        return "Invalid retention_days: expected integer 1..3650"
    if retention_days < 1 or retention_days > 3650:
        return "Invalid retention_days: expected integer 1..3650"
    updated_by = params.get("updated_by")
    if updated_by is not None and not isinstance(updated_by, str):
        return "Field 'updated_by' must be a string or null"
    return None


def _check_dqe_preview_purge_params(params: dict) -> str | None:
    mode = params.get("mode")
    if mode not in {"selected_jobs", "expired_now"}:
        return "Field 'mode' must be selected_jobs or expired_now"
    vacuum_requested = params.get("vacuum_requested", False)
    if not isinstance(vacuum_requested, bool):
        return "Field 'vacuum_requested' must be a boolean"
    db_path = params.get("db_path")
    if db_path is not None and not isinstance(db_path, str):
        return "Field 'db_path' must be a string or null"
    if mode == "selected_jobs":
        job_keys = params.get("job_keys")
        if not isinstance(job_keys, list) or not job_keys:
            return "Field 'job_keys' must be a non-empty list for selected_jobs"
        if any(not isinstance(item, str) or not item for item in job_keys):
            return "Field 'job_keys' must contain non-empty strings"
    elif "job_keys" in params and params["job_keys"] not in (None, []):
        return "Field 'job_keys' is only valid for selected_jobs"
    return None


def _check_dqe_purge_jobs_params(params: dict) -> str | None:
    token = params.get("confirmation_token")
    if not isinstance(token, str) or not token:
        return "Missing or invalid required field: 'confirmation_token'"
    mode = params.get("mode")
    if mode not in {"selected_jobs", "expired_now"}:
        return "Field 'mode' must be selected_jobs or expired_now"
    return None


# ─── post-exec ECC pipeline（lineage.post_exec / shell.post_exec 共用）─────────

_UNSET: Any = object()


def _run_post_exec_pipeline(
    *,
    command: str,
    cwd: str,
    exit_code: int,
    stdout_tail: list,
    source: str,
    terminal_id: str,
    tool_name: str | None,
    # shell.post_exec host tool identity validation 用（見 tool_identity/
    # host_validation.py）：實際餵給 package dispatch 的 tool_name。
    # 保持 `_UNSET`（sentinel，非 None）時，package dispatch 沿用上面的
    # `tool_name` 參數，與本函式既有行為完全一致——這是 `lineage.post_exec`
    # 的路徑，不在重構範圍內，`identify_tool()` 的結果原封不動繼續
    # 同時驅動 ECC gate 與 package dispatch。只有 `shell.post_exec` 的
    # handler 會明確傳入這個參數（值可能是某個 canonical id，也可能是
    # `None`——`None` 代表 host validation 判定不得 dispatch，即使
    # `tool_name` 參數本身非空），此時 package dispatch 改用這個值，
    # 但 ECC gate / nudge / corner 邏輯依然只看上面的 `tool_name` 參數，
    # 不受影響。
    package_dispatch_tool_name: str | None | Any = _UNSET,
    # Observability carrier v1：純 correlation metadata。origin_event_id 是這次 request 的「received」事件 event_id
    # （`lineage.post_exec` 目前恆為 None，GUI 尚未接線）；trace_* 是
    # client-declared metadata，皆只讀轉送，不參與任何 dispatch 判斷。
    origin_event_id: str | None = None,
    trace_root: str | None = None,
    trace_self: str | None = None,
    trace_parent: str | None = None,
    # shell.post_exec 已固定的 user activation（見 `_schedule_package_dispatch`）。
    user_activation_lease: _PinnedUserActivation | None = None,
    # 使用者工具的事件只交給 user package。
    package_dispatch_user_packages_only: bool = False,
) -> dict:
    """共用的 post-exec ECC pipeline。

    從 lineage.post_exec / shell.post_exec 兩個 handler 共同呼叫，目的：把
    accumulator update + gate evaluation + nudge generation + AI fallback
    response 統一在一處執行。

    Args:
        command:     已 ANSI-sanitized 的命令字串
        cwd:         工作目錄
        exit_code:   退出碼
        stdout_tail: stdout 行清單（shell hook 通常空陣列）
        source:      "qt_terminal" / "shell_hook" 等
        terminal_id: multi-stream id
        tool_name:   "EDA tool 識別" — caller 端決定（lineage 端用 result.tool_name；
                     shell 端用 command first token）。None 表非 EDA → 跳過 gate eval。
                     這個值同時是 package dispatch 的預設來源，除非
                     `package_dispatch_tool_name` 被明確覆寫。

    Returns:
        {"ecc_action": str, "ecc_message": str, "ecc_nudge": str, "gate_status": str}
    """
    _dispatch_tool_name = (
        tool_name if package_dispatch_tool_name is _UNSET else package_dispatch_tool_name
    )
    out = {
        "ecc_action": "allow",
        "ecc_message": "",
        "ecc_nudge":   "",
        "gate_status": "SKIP",
    }

    _stream = _get_stream(terminal_id)
    _stream_acc = _stream.accumulator if _stream is not None else _ecc_accumulator

    if _stream_acc is None or _ecc_playbook is None:
        _schedule_package_dispatch(
            command=command,
            tool_name=_dispatch_tool_name,
            cwd=cwd,
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            source=source,
            terminal_id=terminal_id,
            origin_event_id=origin_event_id,
            trace_root=trace_root,
            trace_self=trace_self,
            trace_parent=trace_parent,
            user_activation_lease=user_activation_lease,
            user_packages_only=package_dispatch_user_packages_only,
        )
        return out

    try:
        # 從 command 中嘗試擷取 corner（格式：-corner <name>）
        _post_corner: str | None = None
        _cmd_parts = command.split()
        for _ci, _cp in enumerate(_cmd_parts):
            if _cp == "-corner" and _ci + 1 < len(_cmd_parts):
                _post_corner = _cmd_parts[_ci + 1]
                break

        # tool_name fallback：caller 沒給就用 command first token
        _post_tool_name = tool_name
        if _post_tool_name is None and _cmd_parts:
            _post_tool_name = _cmd_parts[0]

        # Lock 1: accumulator mutation + snapshot
        _post_lock = _stream.lock if _stream is not None else _ecc_lock
        with _post_lock:
            _stream_acc.on_post_exec(
                stdout_lines=stdout_tail,
                report_paths=[],
                exit_code=exit_code,
                corner=_post_corner,
                cwd=cwd,
                tool_name=_post_tool_name,
                command=command,
                source=source,
            )
            _log.debug(
                "ECC post_exec [%s]: cmd=%s, stage=%s, metrics=%d",
                source, command,
                _stream_acc.get_current().current_stage_id,
                len(_stream_acc.get_current().metric_records),
            )
            ecc = _stream_acc.get_current()
            stage_spec = None
            if ecc.current_stage_id is not None:
                for s in _ecc_playbook.stage_specs:
                    if s.id == ecc.current_stage_id:
                        stage_spec = s
                        break

        if _stream is not None:
            _stream.update_activity()

        # 非 EDA 命令（caller 沒識別出 tool_name）跳過 gate / nudge
        _is_eda_command = tool_name is not None

        if stage_spec is not None and _is_eda_command:
            gates = get_quality_gates(stage_spec)
            gate_results = evaluate_gates(ecc, gates)

            if gate_results:
                with _post_lock:
                    _stream_acc.record_gate_results(gate_results)

            failed = [g for g in gate_results if not g.passed]

            if _post_corner is not None:
                current_corner_failed = [
                    g for g in failed if g.corner == _post_corner
                ]
            else:
                current_corner_failed = []

            if current_corner_failed and _ecc_companion is not None:
                _corner_gate_results = [
                    g for g in gate_results
                    if _post_corner is None or g.corner == _post_corner
                ]
                companion_resp = _ecc_companion._deterministic_fallback(
                    _corner_gate_results, stage_spec,
                )
                if companion_resp.judgment == "warn":
                    out["ecc_action"]  = "warn"
                    out["ecc_message"] = companion_resp.explanation

                _emit_event("ai_response", {
                    "trigger":       "post_exec",
                    "response_text": companion_resp.explanation,
                    "model":         "deterministic",
                    "is_fallback":   True,
                }, terminal_id=terminal_id)

            # Proactive nudge（_replaying guard：rebuild / init 期間不生 nudge）
            if _ecc_companion is not None and not _replaying:
                nudge = _ecc_companion.generate_nudge(ecc, stage_spec)
                if nudge:
                    out["ecc_nudge"] = nudge
                    _emit_event("nudge", {
                        "nudge_text": nudge,
                    }, terminal_id=terminal_id)

        # gate_status: EDA → EVALUATED；非 EDA → SKIP
        out["gate_status"] = "EVALUATED" if _is_eda_command else "SKIP"

    except Exception as _ecc_exc:
        _log.warning("ECC post_exec pipeline failed: %s", _ecc_exc)

    # nudge 附加規則：Gate FAIL + nudge → 合併到 ecc_message；
    # Gate PASS + nudge → 靜默（不升級 ecc_action）。nudge 仍透過 ecc_nudge 回傳。
    if out["ecc_nudge"] and out["ecc_message"]:
        out["ecc_message"] = f"{out['ecc_message']}\n{out['ecc_nudge']}"

    # ── Package Runtime v1: generic after-phase dispatch（背景 thread，不阻塞 caller）
    _schedule_package_dispatch(
        command=command,
        tool_name=_dispatch_tool_name,
        cwd=cwd,
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        source=source,
        terminal_id=terminal_id,
        origin_event_id=origin_event_id,
        trace_root=trace_root,
        trace_self=trace_self,
        trace_parent=trace_parent,
        user_activation_lease=user_activation_lease,
        user_packages_only=package_dispatch_user_packages_only,
    )

    return out


# ─── Public API ───────────────────────────────────────────────────────────────

def _attach_run_binding_revocation(registry: CanonicalSessionContextRegistry) -> None:
    """session.end／連線中斷撤銷 session 時，同步清除該 session 的 Run 綁定。"""
    from terminux_engine.llm import run_binding

    registry.add_revoke_listener(run_binding.revoke_session)


def _resolve_request_context_or_error(
    request_obj: dict,
    req_id: Any,
    peer_uid: int | None,
) -> CanonicalRequestContext | dict:
    """context-required method 的 transport-level context 解析。

    回傳 ``CanonicalRequestContext``（成功）或 JSON-RPC error dict（失敗，
    caller 應直接回傳，不得進入 AgentRunner／OrchestrationService dispatch）。

    驗證順序：parse terminux_context → obtain connection peer
    uid（由 caller 傳入）→ registry.resolve(session_id, peer_uid)。
    """
    terminux_context = request_obj.get("terminux_context")
    if not isinstance(terminux_context, dict):
        return _err(
            req_id, _ERR_SESSION_CONTEXT_REQUIRED,
            "terminux_context.session_id is required for this method",
        )
    session_id = terminux_context.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return _err(
            req_id, _ERR_SESSION_CONTEXT_REQUIRED,
            "terminux_context.session_id is required for this method",
        )
    if peer_uid is None:
        # 這條連線無法取得 peer credential（見 peer_identity.py），無法證明
        # session_id 屬於目前連線的 caller；fail-closed，不 fallback。
        _log.warning("TERM_CONTEXT_PEER_IDENTITY_UNAVAILABLE method=%s", request_obj.get("method"))
        return _err(req_id, _ERR_INVALID_SESSION_CONTEXT, "invalid session context")
    if _session_registry is None:
        _log.warning("TERM_CONTEXT_REGISTRY_UNAVAILABLE method=%s", request_obj.get("method"))
        return _err(req_id, _ERR_INVALID_SESSION_CONTEXT, "invalid session context")
    try:
        return _session_registry.resolve(session_id=session_id, peer_uid=peer_uid)
    except InvalidSessionContextError as exc:
        _log.warning(
            "TERM_CONTEXT_INVALID_SESSION method=%s reason=%s",
            request_obj.get("method"), exc.reason,
        )
        return _err(req_id, _ERR_INVALID_SESSION_CONTEXT, "invalid session context")


def handle_jsonrpc_request(
    request_obj: dict,
    *,
    tool_signatures_path: str | Path | None = None,
    lineage_rules_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    # 呼叫端（_handle_connection／serve_once）從 accepted UDS socket
    # 解析出的 peer UID；直接呼叫本函式（測試、serve_once 以外情境）時預設
    # None，只影響 context-required methods（見 _CONTEXT_REQUIRED_METHODS）。
    peer_uid: int | None = None,
) -> dict:
    """處理單筆 JSON-RPC 2.0 request，包裹 session scope isolation context boundary 後委派給
    ``_dispatch_jsonrpc_request``（原本的完整 dispatch 邏輯，未變更）。

    對 `_CONTEXT_REQUIRED_METHODS` 內的 method：先解析 transport-level
    context，成功才 set ContextVar 並進入 dispatch，失敗直接回傳 error（不
    建立 AgentRunner call／OrchestrationService call）；`try/finally` 確保
    ContextVar 一定 reset。其餘既有 method 完全不受影響（fall through 到與
    session scope isolation 之前相同的行為）。
    """
    method = request_obj.get("method") if isinstance(request_obj, dict) else None
    if isinstance(method, str) and method in _CONTEXT_REQUIRED_METHODS:
        req_id = request_obj.get("id") if isinstance(request_obj, dict) else None
        context_or_error = _resolve_request_context_or_error(request_obj, req_id, peer_uid)
        if isinstance(context_or_error, dict):
            return context_or_error
        token = set_current_request_context(context_or_error)
        try:
            return _dispatch_jsonrpc_request(
                request_obj,
                tool_signatures_path=tool_signatures_path,
                lineage_rules_path=lineage_rules_path,
                ledger_path=ledger_path,
            )
        finally:
            reset_current_request_context(token)

    return _dispatch_jsonrpc_request(
        request_obj,
        tool_signatures_path=tool_signatures_path,
        lineage_rules_path=lineage_rules_path,
        ledger_path=ledger_path,
    )


def _handle_shell_post_exec(
    req_id: Any, params: dict, pin: _PinnedUserActivation | None,
) -> dict:
    """`shell.post_exec`（參數已驗證）。`pin` 是本次 request 固定使用的 user activation。"""
    exit_code = params.get("exit_code", 0)
    cwd = params.get("cwd", "")
    command = _sanitize_command(params.get("command", ""))
    source = params.get("source", "shell_hook")
    _sp_terminal_id = params.get("terminal_id", "default")
    # Observability carrier v1：client-declared trace metadata（純
    # correlation 用途）。Malformed 直接丟棄該欄位，
    # 不影響本次 request 其餘處理。
    _sp_trace_root = _sanitize_trace_field(params.get("trace_root"))
    _sp_trace_self = _sanitize_trace_field(params.get("trace_self"))
    _sp_trace_parent = _sanitize_trace_field(params.get("trace_parent"), allow_empty=True)
    # origin_event_id：這次 shell.post_exec request 的「received」事件
    # 自己的 event_id，作為後續 audit 序列（validated/scheduled/started/
    # finished）的 correlation key。
    _sp_origin_event_id = _emit_event("shell_post_exec", {
        "exit_code": exit_code, "cwd": cwd,
        "command": command, "source": source,
        "rpc_method": "shell.post_exec",
    }, terminal_id=_sp_terminal_id,
        trace_root=_sp_trace_root, trace_self=_sp_trace_self, trace_parent=_sp_trace_parent,
    )

    # dedup_cmd fallback：shell hook 沒帶 command 時用 accumulator
    # 的 _last_pre_exec_cmd 推斷
    _sp_stream = _get_stream(_sp_terminal_id)
    _sp_acc = _sp_stream.accumulator if _sp_stream is not None else _ecc_accumulator
    dedup_cmd = command
    if not dedup_cmd and _sp_acc is not None and _sp_acc._last_pre_exec_cmd:
        dedup_cmd = _sp_acc._last_pre_exec_cmd

    # 呼叫共用 ECC pipeline（accumulator + gate eval + nudge + ecc_action）
    # tool_name = command first token；無 → None（pipeline 內視為非 EDA 跳過 gate）。
    # 這個值只餵給既有 ECC gate/nudge/corner 邏輯，行為完全不變——host tool
    # identity validation（見下）是獨立於此、只影響 package dispatch 的判斷。
    _sh_tool_parts = dedup_cmd.split() if dedup_cmd else []
    _sh_tool_name = _sh_tool_parts[0] if _sh_tool_parts else None

    # shell.post_exec host tool identity validation：client 可選擇性提供
    # `tool_name` 宣稱值（目前沒有任何 official client 會送這個欄位），
    # 但一律只作 comparison，不可作 dispatch authority——見
    # tool_identity/host_validation.py。DENY 決定時直接回傳穩定的
    # JSON-RPC error，並提早 return（連 ECC gate pipeline 都
    # 不執行——DENY 代表無法信任這個 event 的 identity 宣稱，不只是
    # package dispatch 要擋，見 dispatch ordering）。
    _sp_claimed_tool_name = params.get("tool_name")
    # 本次 request 固定使用的 effective catalog（內建＋使用者工具）；與 dispatch
    # 使用同一份 activation。沒有 user activation 時就是內建 catalog（既有行為）。
    _sp_catalog = _effective_tool_catalog(pin.activation if pin is not None else None)
    # Package-dispatch 專用的 tool_name：與上面 `_sh_tool_name`（餵給既有
    # ECC gate/nudge/corner 邏輯，行為不變）刻意分離。Catalog 載入失敗時
    # fail closed（維持 None，不 fallback 到未驗證的 `_sh_tool_name` 或
    # client claim）；ECC gate 不受影響，仍照舊用 `_sh_tool_name` 執行。
    _sp_dispatch_tool_name: str | None = None
    # 只有使用者工具的事件會填入（見下方 wrapper-only 規則）；內建工具保持空 dict。
    _sp_user_tool_fields: dict[str, Any] = {}
    if _sp_catalog is not None:
        _sp_validation = validate_shell_post_exec_tool_identity(
            command=dedup_cmd,
            claimed_tool_name=_sp_claimed_tool_name,
            catalog=_sp_catalog,
        )
        _log.info(
            "TERM_TOOL_IDENTITY_VALIDATION source=%s decision=%s "
            "reason=%s derived=%s claim=%s dispatch_allowed=%s",
            source, _sp_validation.decision.value, _sp_validation.reason_code,
            _sp_validation.derived_canonical_id, _sp_validation.normalized_claim,
            _sp_validation.dispatch_allowed,
        )
        # 使用者登記的工具只接受 wrapper 送出、且 request 本身帶有 command 的事件；
        # shell hook 以 `_last_pre_exec_cmd` 推斷、或其他來源的事件只記錄不 dispatch。
        # `source` 是 client 自行宣告的值，這裡只用來歸屬與避免重複，不是安全邊界。
        # 內建工具不經過這條規則，行為不變。
        if (_sp_validation.dispatch_allowed
                and _sp_catalog.origin_of(_sp_validation.derived_canonical_id or "") == ORIGIN_USER):
            _sp_explicit_command = bool(command.strip())
            _sp_user_dispatch = source == "wrapper" and _sp_explicit_command
            _sp_user_tool_fields = {
                "tool_origin": ORIGIN_USER,
                "dispatch_scope": "user_tool_wrapper_only",
                "user_tool_dispatch": _sp_user_dispatch,
            }
            if not _sp_user_dispatch:
                _log.info(
                    "TERM_USER_TOOL_DISPATCH_SKIPPED tool=%s source=%s explicit_command=%s "
                    "(user tools dispatch only from wrapper events that carry the command)",
                    _sp_validation.derived_canonical_id, source, _sp_explicit_command,
                )
        if _sp_origin_event_id is not None:
            _emit_event("post_exec.validated", {
                "origin_event_id": _sp_origin_event_id,
                "rpc_method": "shell.post_exec",
                "client_source": source,
                "validation_decision": _sp_validation.decision.value,
                **_sp_user_tool_fields,
            }, terminal_id=_sp_terminal_id,
                trace_root=_sp_trace_root, trace_self=_sp_trace_self, trace_parent=_sp_trace_parent,
            )
        if not _sp_validation.dispatch_allowed and _sp_validation.decision in (
            ValidationDecision.DENY_MISMATCH,
            ValidationDecision.DENY_UNVERIFIABLE_CLAIM,
            ValidationDecision.DENY_MALFORMED,
        ):
            return _err(
                req_id,
                _ERR_TOOL_IDENTITY_REJECTED,
                f"shell.post_exec tool identity rejected: {_sp_validation.reason_code}",
            )
        _sp_dispatch_tool_name = _sp_validation.timeline_tool_name
        if _sp_user_tool_fields and not _sp_user_tool_fields["user_tool_dispatch"]:
            _sp_dispatch_tool_name = None
    else:
        _log.error(
            "TERM_TOOL_CATALOG_UNAVAILABLE source=%s: package dispatch fail-closed "
            "(no fallback to unvalidated tool_name)",
            source,
        )

    _pipeline = _run_post_exec_pipeline(
        command=dedup_cmd,
        cwd=cwd,
        exit_code=exit_code,
        stdout_tail=[],
        source=source,
        terminal_id=_sp_terminal_id,
        tool_name=_sh_tool_name,
        package_dispatch_tool_name=_sp_dispatch_tool_name,
        origin_event_id=_sp_origin_event_id,
        trace_root=_sp_trace_root,
        trace_self=_sp_trace_self,
        trace_parent=_sp_trace_parent,
        user_activation_lease=pin,
        package_dispatch_user_packages_only=bool(_sp_user_tool_fields),
    )
    result_dict = {"status": "ok"}
    result_dict.update(_pipeline)
    return _ok(req_id, result_dict)


def _dispatch_jsonrpc_request(
    request_obj: dict,
    *,
    tool_signatures_path: str | Path | None = None,
    lineage_rules_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> dict:
    """
    處理單筆 JSON-RPC 2.0 request dict，回傳 response dict。
    路徑優先順序：request params > function kwargs。
    """
    # ── 基本結構驗證（→ -32600）────────────────────────────────────────────
    if not isinstance(request_obj, dict):
        return _err(None, _ERR_INVALID_REQUEST, "Invalid Request")

    req_id = request_obj.get("id")   # 盡早取得，用於 error response

    if request_obj.get("jsonrpc") != "2.0":
        return _err(req_id, _ERR_INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'")
    if "id" not in request_obj:
        return _err(None, _ERR_INVALID_REQUEST, "Invalid Request: missing id")
    if not isinstance(request_obj.get("method"), str):
        return _err(req_id, _ERR_INVALID_REQUEST, "Invalid Request: method must be a string")
    if not isinstance(request_obj.get("params"), dict):
        return _err(req_id, _ERR_INVALID_REQUEST, "Invalid Request: params must be a dict")

    req_id = request_obj["id"]
    method = request_obj["method"]
    params = request_obj["params"]

    # ── Method routing（→ -32601）────────────────────────────────────────
    if method not in _SUPPORTED_METHODS:
        return _err(req_id, _ERR_METHOD_NOT_FOUND, f"Method not found: '{method}'")

    # ── Path priority: request params > function kwargs ─────────────────
    def _resolve(key: str, fallback: str | Path | None) -> str | Path | None:
        return params[key] if key in params else fallback

    if method == "orchestration.start":
        error = _check_orchestration_start_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.start_request(params["workflow"], params["config"]),
        )
    if method == "orchestration.status":
        error = _check_orchestration_status_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.status_request(params["run_id"]),
        )
    if method == "orchestration.pause":
        error = _check_orchestration_pause_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.pause_request(params["run_id"]),
        )
    if method == "orchestration.resume":
        error = _check_orchestration_resume_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.resume_request(params["run_id"]),
        )
    if method == "orchestration.cancel":
        error = _check_orchestration_cancel_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.cancel_request(
                params["run_id"],
                immediate=params.get("immediate", False),
            ),
        )
    if method == "orchestration.retry":
        error = _check_orchestration_retry_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.retry_request(
                params["run_id"],
                step_id=params.get("step_id"),
            ),
        )
    if method == "orchestration.abandon":
        error = _check_orchestration_abandon_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.abandon_request(params["run_id"]),
        )
    if method == "orchestration.list_runs":
        error = _check_orchestration_list_runs_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.list_runs_request(
                workflow_type=params.get("workflow_type"),
                status=params.get("status"),
                pending_review=params.get("pending_review", False),
                limit=params.get("limit", 20),
            ),
        )
    if method == _EVIDENCE_BUNDLE_METHOD:
        error = _check_evidence_bundle_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.evidence_bundle_request(
                params["orchestration_run_id"]
            ),
        )
    if method == _EVIDENCE_SET_METHOD:
        error = _check_evidence_set_params(params)
        if error:
            return _err(req_id, _ERR_INVALID_PARAMS, error)
        if _orchestration_service is None:
            return _err(req_id, _ERR_INTERNAL, "orchestration service unavailable")
        return _run_orchestration_facade(
            req_id,
            lambda: _orchestration_service.evidence_set_request(
                params["run_ids"],
                comparison_focus=params.get("comparison_focus", "all"),
            ),
        )

    # ── agent.eap3.work.* — 持久化工程工作與有界多輪控制 ───────────────
    if method in _EAP3_METHODS:
        server_authoritative = {
            "member_id", "project_id", "session_id", "scope", "grant",
            "grant_id", "task_id", "task_digest", "authorization_digest",
            "used", "reserved", "execution_allowed",
        }
        forbidden = sorted(set(params) & server_authoritative)
        if forbidden:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"server-authoritative fields may not be supplied: {forbidden}")
        allowed = {
            "agent.eap3.work.create": {"objective", "profile_id", "package_id",
                "constraints", "criteria", "block", "method", "baseline_reference",
                "source_root", "request_id", "preferred_experts", "required_reviews"},
            "agent.eap3.work.create_from_saved_proposal": {
                "source_work_id", "source_proposal_id", "expected_proposal_digest",
                "profile_id", "objective", "constraints", "criteria", "source_root",
                "request_id"},
            "agent.eap3.work.authorization_preview": {"work_id", "mode", "limits",
                "allowed_capabilities", "allowed_methods", "allowed_changes", "writable_roots",
                "stop_conditions", "dispatch_limits"},
            "agent.eap3.work.approve": {"work_id"},
            "agent.eap3.work.continue": {"work_id", "max_transitions"},
            "agent.eap3.work.get": {"work_id"},
            "agent.eap3.work.list": set(),
            "agent.eap3.work.answer": {"work_id", "message", "objective", "method"},
            "agent.eap3.work.pause": {"work_id"},
            "agent.eap3.work.resume": {"work_id"},
            "agent.eap3.work.cancel": {"work_id"},
            "agent.eap3.work.takeover": {"work_id"},
            "agent.eap3.work.proposal.submit": {"work_id", "proposal",
                "method_identity", "origin", "request_id"},
            "agent.eap3.work.proposal.approve": {"work_id", "proposal_id",
                "proposal_digest", "request_id"},
            "agent.eap3.work.effective_config": {"work_id"},
        }[method]
        unexpected = sorted(set(params) - allowed)
        if unexpected:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"unexpected params for {method}: {unexpected}")
        if method == "agent.eap3.work.list":
            return _ok(req_id, _run_eap3("list"))
        work_id = params.get("work_id")
        create_methods = {
            "agent.eap3.work.create",
            "agent.eap3.work.create_from_saved_proposal",
        }
        if method not in create_methods and (
                not isinstance(work_id, str) or not work_id):
            return _err(req_id, _ERR_INVALID_PARAMS, "work_id must be a non-empty string")
        if method == "agent.eap3.work.create":
            for field in ("objective", "profile_id", "package_id"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be a non-empty string")
            for field in ("constraints", "criteria"):
                value = params.get(field, [])
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be an array of strings")
            for field in ("preferred_experts", "required_reviews"):
                value = params.get(field, [])
                if not isinstance(value, list) or any(
                        not isinstance(item, str) for item in value):
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be an array of strings")
            create_kwargs = {
                "objective": params["objective"], "profile_id": params["profile_id"],
                "package_id": params["package_id"],
                "constraints": params.get("constraints"),
                "criteria": params.get("criteria"), "block": params.get("block"),
                "method": params.get("method"),
                "source_root": params.get("source_root"),
                "baseline_reference": params.get("baseline_reference"),
                "request_id": params.get("request_id"),
            }
            if "preferred_experts" in params:
                create_kwargs["preferred_experts"] = params["preferred_experts"]
            if "required_reviews" in params:
                create_kwargs["required_reviews"] = params["required_reviews"]
            return _ok(req_id, _run_eap3("create", **create_kwargs))
        if method == "agent.eap3.work.create_from_saved_proposal":
            for field in (
                    "source_work_id", "source_proposal_id",
                    "expected_proposal_digest", "profile_id", "objective"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be a non-empty string")
            for field in ("constraints", "criteria"):
                value = params.get(field, [])
                if not isinstance(value, list) or any(
                        not isinstance(item, str) for item in value):
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be an array of strings")
            return _ok(req_id, _run_eap3(
                "create_from_saved_proposal",
                source_work_id=params["source_work_id"],
                source_proposal_id=params["source_proposal_id"],
                expected_proposal_digest=params["expected_proposal_digest"],
                profile_id=params["profile_id"], objective=params["objective"],
                constraints=params.get("constraints"), criteria=params.get("criteria"),
                source_root=params.get("source_root"),
                request_id=params.get("request_id")))
        if method == "agent.eap3.work.proposal.submit":
            # Phase A：actor／scope 一律由 authenticated session 與 Work 取得，
            # client 只能提供 proposal 內容、method 身分與冪等鍵。
            if not isinstance(params.get("proposal"), dict) or not params["proposal"]:
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "proposal must be a non-empty object")
            if not isinstance(params.get("method_identity"), str) \
                    or not params["method_identity"].strip():
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "method_identity must be a non-empty string")
            for field in ("origin", "request_id"):
                if field in params and (not isinstance(params[field], str)
                                        or not params[field].strip()):
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be a non-empty string")
            return _ok(req_id, _run_eap3(
                "submit_proposal", work_id=work_id, proposal=params["proposal"],
                method_identity=params["method_identity"],
                **({"origin": params["origin"]} if "origin" in params else {}),
                request_id=params.get("request_id")))
        if method == "agent.eap3.work.proposal.approve":
            for field in ("proposal_id", "proposal_digest"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"{field} must be a non-empty string")
            if "request_id" in params and (not isinstance(params["request_id"], str)
                                           or not params["request_id"].strip()):
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "request_id must be a non-empty string")
            return _ok(req_id, _run_eap3(
                "approve_proposal", work_id=work_id,
                proposal_id=params["proposal_id"],
                proposal_digest=params["proposal_digest"],
                request_id=params.get("request_id")))
        if method == "agent.eap3.work.authorization_preview":
            return _ok(req_id, _run_eap3(
                "preview_authorization", work_id=work_id, mode=params.get("mode", "BOUNDED_EXPLORATION"),
                limits=params.get("limits"), allowed_capabilities=params.get("allowed_capabilities"),
                allowed_methods=params.get("allowed_methods"),
                allowed_changes=params.get("allowed_changes"),
                writable_roots=params.get("writable_roots"),
                stop_conditions=params.get("stop_conditions"),
                **({"dispatch_limits": params["dispatch_limits"]}
                   if "dispatch_limits" in params else {})))
        if method == "agent.eap3.work.effective_config":
            inspected = _run_eap3("get", work_id=work_id)
            if inspected.get("status") != "ok":
                return _ok(req_id, inspected)
            from importlib.metadata import version
            work = inspected["work"]
            environment = work.get("environment") or {}
            authorization = work.get("authorization") or work.get(
                "authorization_preview") or {}
            service = _get_eap3_service()
            child_ids: list[str] = []
            collection = (work.get("baseline") or {}).get("collection") or {}
            if isinstance(collection.get("task_id"), str):
                child_ids.append(collection["task_id"])
            imported_baseline = (((work.get("baseline") or {}).get("reference") or {})
                                 .get("execution_import") or {})
            if isinstance(imported_baseline.get("source_task_id"), str):
                child_ids.append(imported_baseline["source_task_id"])
            child_ids.extend(row["task_id"] for row in work.get("attempts", [])
                             if isinstance(row, dict) and isinstance(
                                 row.get("task_id"), str))
            children = []
            for child_id in dict.fromkeys(child_ids):
                child_result = _run_analysis_task("get", task_id=child_id)
                if child_result.get("status") != "ok":
                    children.append({"task_id": child_id, "status": "UNAVAILABLE"})
                    continue
                task = child_result["task"]
                children.append({
                    "task_id": child_id, "task_digest": task.get("task_digest"),
                    "status": task.get("status"),
                    "profile_source": task.get("profile_source"),
                    "execution_plan": task.get("execution_plan"),
                    "parameter": task.get("parameter"),
                    "runs": task.get("runs"),
                })
            result = {
                "status": "ok", "schema_version": "eap5.effective_config.v1",
                "work_id": work_id, "work_revision": work.get("revision"),
                "package_version": version("terminux-engine"),
                "runtime": dict(service._eap5_runtime_origin),
                "profile": {"source": work.get("profile_source"),
                            "environment": environment,
                            "source_kind": "work_frozen_snapshot"},
                "authorization": {
                    "source_kind": ("approved_snapshot" if work.get("authorization")
                                    else "preview_snapshot" if authorization else "none"),
                    "authorization_digest": authorization.get("authorization_digest"),
                    "limits": authorization.get("limits"),
                    "writable_roots": authorization.get("writable_roots"),
                },
                "evidence_sinks": {
                    "dqe_db_path": environment.get("dqe_db_path"),
                    "artifact_root": environment.get("artifact_root"),
                    "execution_root": environment.get("execution_root"),
                    "source_kind": "work_frozen_profile",
                },
                "llm_config": dict(service._eap5_loaded_config["llm_config"]),
                "provider_bindings": dict(service._eap5_loaded_binding),
                "provider_binding_values": list(service._eap5_binding_values),
                "package_capabilities": list(service._eap5_package_values),
                "background": dict(service._eap5_loaded_config["background"]),
                "children": children,
            }
            return _ok(req_id, result)
        operation = method.rsplit(".", 1)[-1]
        if operation == "continue":
            operation = "continue_work"
            return _ok(req_id, _run_eap3(operation, work_id=work_id,
                                          max_transitions=params.get("max_transitions", 64)))
        if operation == "answer":
            if not isinstance(params.get("message"), str) or not params["message"].strip():
                return _err(req_id, _ERR_INVALID_PARAMS, "message must be a non-empty string")
            return _ok(req_id, _run_eap3(operation, work_id=work_id, message=params["message"],
                                          objective=params.get("objective"),
                                          method=params.get("method")))
        return _ok(req_id, _run_eap3(operation, work_id=work_id))

    # ── agent.eap2.* — 已提交方案→既有 FlowTask DRAFT bridge ────────────
    if method in _EAP2_METHODS:
        if set(params) not in ({"draft_id", "profile_id"},
                               {"draft_id", "profile_id", "replacement_task_id"}) \
                or not isinstance(params.get("draft_id"), str) or not params["draft_id"] \
                or not isinstance(params.get("profile_id"), str) or not params["profile_id"] \
                or ("replacement_task_id" in params and
                    (not isinstance(params["replacement_task_id"], str) or
                     not params["replacement_task_id"])):
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "agent.eap2.create_flow_task requires draft_id/profile_id and optional replacement_task_id")
        draft = _run_eap1("get", draft_id=params["draft_id"])
        if draft.get("status") == "ERROR":
            return _ok(req_id, draft)
        try:
            from terminux_engine.expert_ai.eap2 import (
                EapBridgeError, build_flow_task_proposal,
            )

            replacement = None
            if "replacement_task_id" in params:
                service = _get_analysis_task_service()
                if service is None:
                    return _err(req_id, _ERR_INTERNAL, "flow task service unavailable")
                validated = service.validate_eap_replacement_source(
                    task_id=params["replacement_task_id"],
                    proposal=build_flow_task_proposal(draft),
                    expected_scope=dict(draft.get("scope") or {}))
                replacement = validated.get("replacement")
                if validated.get("status") != "ok" or not isinstance(replacement, dict):
                    return _ok(req_id, {"status": "ERROR",
                                        "reason_code": "EAP_REPLACEMENT_SOURCE_INVALID",
                                        "message": "replacement requires a historical unstarted FlowTask draft",
                                        "execution_allowed": False})
            proposal = build_flow_task_proposal(draft, replacement=replacement)
        except EapBridgeError as exc:
            return _ok(req_id, {"status": "ERROR", "reason_code": exc.code,
                                "message": exc.message, "execution_allowed": False})
        service = _get_analysis_task_service()
        if service is None:
            return _err(req_id, _ERR_INTERNAL, "flow task service unavailable")
        question = "EAP proposal " + str(proposal["source"]["proposal_digest"])
        return _ok(req_id, service.create_eap_flow_task(
            question=question, profile_id=params["profile_id"], proposal=proposal))

    # ── agent.eap1.* — domain proposal Global／Domain 不可執行草案入口 ───────────
    if method in _EAP1_METHODS:
        if method == "agent.eap1.create":
            problem, error = _parse_eap1_problem(params)
            if error is not None:
                return _err(req_id, _ERR_INVALID_PARAMS, error)
            return _ok(req_id, _run_eap1("create_draft", problem=problem))
        if method in {"agent.eap1.evidence.list", "agent.eap1.evidence.select"}:
            allowed_params = ({"profile_id", "event_type", "limit"}
                              if method == "agent.eap1.evidence.list"
                              else {"event_id", "profile_id"})
            if set(params) - allowed_params:
                return _err(req_id, _ERR_INVALID_PARAMS,
                            f"unexpected params for {method}")
            if method == "agent.eap1.evidence.select" and (
                    not isinstance(params.get("event_id"), str) or not params["event_id"]):
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "agent.eap1.evidence.select requires a non-empty 'event_id'")
            if method == "agent.eap1.evidence.list":
                event_type = params.get("event_type")
                limit = params.get("limit", 20)
                if event_type is not None and (not isinstance(event_type, str) or not event_type):
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                "'event_type' must be a non-empty string when supplied")
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                "'limit' must be an integer from 1 through 100")
            scope_override = None
            profile_id = params.get("profile_id")
            if profile_id is not None:
                if not isinstance(profile_id, str) or not profile_id:
                    return _err(req_id, _ERR_INVALID_PARAMS, "'profile_id' must be a non-empty string")
                service = _get_analysis_task_service()
                resolved = service.eap_profile_scope(profile_id) if service is not None else None
                if not isinstance(resolved, dict) or resolved.get("status") != "ok":
                    return _err(req_id, _ERR_INVALID_PARAMS, "the selected profile is unavailable or out of scope")
                from terminux_engine.expert_ai.eap1 import EapScope
                context = get_current_request_context()
                scope_override = EapScope(context.member_id, str(resolved["project_id"]),
                                          context.session_id, str(resolved["project_source"]))
            if method == "agent.eap1.evidence.list":
                return _ok(req_id, _run_eap1("evidence_list", event_type=params.get("event_type"),
                                              limit=params.get("limit", 20),
                                              scope_override=scope_override))
            return _ok(req_id, _run_eap1("evidence_select", event_id=params["event_id"],
                                          scope_override=scope_override))
        if method == "agent.eap1.get":
            if set(params) != {"draft_id"} or not isinstance(params.get("draft_id"), str) \
                    or not params["draft_id"]:
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "agent.eap1.get requires only a non-empty 'draft_id'")
            return _ok(req_id, _run_eap1("get", draft_id=params["draft_id"]))
        if set(params) != {"problem_id"} or not isinstance(params.get("problem_id"), str) \
                or not params["problem_id"]:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "agent.eap1.cancel requires only a non-empty 'problem_id'")
        return _ok(req_id, _run_eap1("cancel", problem_id=params["problem_id"]))

    # ── agent.plan_only — plan-only planner typed plan-only entry ────────────────────
    # Additive、獨立的 method。`mode` 是 required 且經 enum 驗證：缺失或未知
    # 一律拒絕，**絕不** fallback 到 normal Agent path。client 不得
    # 提供 risk、input schema、approval requirement、catalog snapshot id 或
    # `execution_allowed`——那些全部由 server 端權威資料決定。
    if method == _PLAN_ONLY_METHOD:
        from terminux_engine.planning.handler import (
            MODE_INVALID as _PLAN_MODE_INVALID,
            PLAN_ONLY_MODE as _PLAN_ONLY_MODE_VALUE,
            handle_plan_only as _handle_plan_only,
        )

        client_supplied = sorted(
            set(params) & {"risk", "input_schema", "approval_required",
                           "catalog_snapshot_id", "execution_allowed", "tools",
                           "capabilities"}
        )
        if client_supplied:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"these fields are server-authoritative and may not be supplied "
                        f"by the client: {client_supplied}")
        mode = params.get("mode")
        if mode != _PLAN_ONLY_MODE_VALUE:
            return _ok(req_id, {
                "status": "error", "mode": _PLAN_ONLY_MODE_VALUE,
                "goal": params.get("goal") if isinstance(params.get("goal"), str) else None,
                "plan": None, "validation": {"status": "not_run", "errors": []},
                "catalog_snapshot_id": None, "execution_allowed": False,
                "proposal_created": False, "tool_calls": 0,
                "error_code": _PLAN_MODE_INVALID,
                "message": (f"params.mode is required and must be "
                            f"{_PLAN_ONLY_MODE_VALUE!r}; got {mode!r}"),
            })
        return _ok(req_id, _handle_plan_only(
            goal=params.get("goal"),
            mode=mode,
            deps=_build_plan_only_dependencies(),
            created_from={"entry": _PLAN_ONLY_METHOD, "profile_id": "ai3_plan_only_v1"},
        ))

    # ── agent.plan.propose_step — approved package action typed plan-step proposal entry ────
    # 只建立提案。approval 與 execution 仍走既有 `agent.proposal.*` 與
    # `/approve`／`/execute`；approved package action 不另造 approval authority。
    if method == _PLAN_PROPOSE_METHOD:
        from terminux_engine.bounded_execution.errors import PLAN_STEP_NOT_ELIGIBLE

        client_supplied = sorted(
            set(params) & {"risk", "approval_required", "arguments", "capability",
                           "capability_id", "catalog_snapshot_id", "execution_allowed",
                           "bounded_execution_enabled"}
        )
        if client_supplied:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"these fields are server-authoritative and may not be supplied "
                        f"by the client: {client_supplied}")
        plan_id = params.get("plan_id")
        step_id = params.get("step_id")
        if not isinstance(plan_id, str) or not plan_id or not isinstance(step_id, str) \
                or not step_id:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "Missing or invalid required fields: 'plan_id' and 'step_id'")
        return _ok(req_id, _run_plan_propose(plan_id, step_id))

    # ── agent.analysis_task.* — analysis task 固定兩步分析 task ────────────────────
    # 四個 method 都經 CanonicalRequestContext 邊界。client 不能提供
    # member/project/session、workspace、DB path、grant、policy、task_id、
    # digest、步驟陣列或預算——全部由 host 產生／驗證。未列明的參數一律
    # 拒絕，不悄悄忽略。
    if method in _ANALYSIS_TASK_METHODS:
        _server_authoritative = {
            "member_id", "project_id", "session_id", "workspace", "dqe_db_path",
            "db_path", "grant", "grant_id", "policy", "steps", "budget",
            "task_digest" if method == "agent.analysis_task.create" else "",
            "task_id" if method == "agent.analysis_task.create" else "",
        } - {""}
        client_supplied = sorted(set(params) & _server_authoritative)
        if client_supplied:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"these fields are server-authoritative and may not be supplied "
                        f"by the client: {client_supplied}")

        _allowed = {
            "agent.analysis_task.create": {"current_run_id", "baseline_run_id", "question"},
            "agent.analysis_task.start": {"task_id", "task_digest"},
            "agent.analysis_task.get": {"task_id", "include_results"},
            "agent.analysis_task.cancel": {"task_id", "task_digest"},
        }[method]
        unexpected = sorted(set(params) - _allowed)
        if unexpected:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"unexpected params for {method}: {unexpected}")

        if method == "agent.analysis_task.create":
            for field in ("current_run_id", "baseline_run_id", "question"):
                if not isinstance(params.get(field), str) or not params[field]:
                    return _err(req_id, _ERR_INVALID_PARAMS,
                                f"Missing or invalid required field: {field!r}")
            if params["current_run_id"] == params["baseline_run_id"]:
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "current_run_id and baseline_run_id must differ")
            return _ok(req_id, _run_analysis_task(
                "create", current_run_id=params["current_run_id"],
                baseline_run_id=params["baseline_run_id"],
                question=params["question"]))

        task_id = params.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "Missing or invalid required field: 'task_id'")
        if method == "agent.analysis_task.get":
            include = params.get("include_results", False)
            if not isinstance(include, bool):
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "'include_results' must be a boolean")
            return _ok(req_id, _run_analysis_task("get", task_id=task_id,
                                                  include_results=include))
        task_digest = params.get("task_digest")
        if not isinstance(task_digest, str) or not task_digest:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "Missing or invalid required field: 'task_digest'")
        operation = "start" if method == "agent.analysis_task.start" else "cancel"
        return _ok(req_id, _run_analysis_task(operation, task_id=task_id,
                                              task_digest=task_digest))

    # ── agent.flow_task.* — flow task 固定五步 flow task ────────────────────
    if method in _FLOW_TASK_METHODS:
        _server_authoritative = {
            "member_id", "project_id", "session_id", "workspace", "run_id",
            "grant", "grant_id", "policy", "profile", "steps", "budget",
            "path", "paths", "command", "argv", "docker_image_id",
            "parameter", "parameter_value", "limits", "timeout_sec",
            "task_digest" if method == "agent.flow_task.create" else "",
            "task_id" if method == "agent.flow_task.create" else "",
        } - {""}
        client_supplied = sorted(set(params) & _server_authoritative)
        if client_supplied:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"these fields are server-authoritative and may not be "
                        f"supplied by the client: {client_supplied}")
        _allowed = {
            # profile 來源三選一（path／inline／本 session 已選用的 id）。
            "agent.flow_task.create": {"profile_id", "profile_path",
                                       "profile_inline", "question"},
            "agent.flow_task.start": {"task_id", "task_digest"},
            "agent.flow_task.get": {"task_id", "include_results"},
            "agent.flow_task.cancel": {"task_id", "task_digest"},
            "agent.flow_task.select_profile": {"profile_path", "profile_inline"},
            "agent.flow_task.list_profiles": set(),
            "agent.flow_task.list": set(),
        }[method]
        unexpected = sorted(set(params) - _allowed)
        if unexpected:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        f"unexpected params for {method}: {unexpected}")

        def _profile_source_params() -> tuple[dict[str, Any] | None, str | None]:
            """三種來源的型別檢查；互斥由 service 決定並回結構化錯誤。"""
            source: dict[str, Any] = {}
            path = params.get("profile_path")
            if path is not None:
                if not isinstance(path, str) or not path.strip():
                    return None, "'profile_path' must be a non-empty string"
                source["profile_path"] = path
            inline = params.get("profile_inline")
            if inline is not None:
                if not isinstance(inline, dict):
                    return None, "'profile_inline' must be a JSON object"
                source["profile_inline"] = inline
            selected = params.get("profile_id")
            if selected is not None:
                if not isinstance(selected, str) or not selected.strip():
                    return None, "'profile_id' must be a non-empty string"
                source["profile_id"] = selected
            return source, None

        if method == "agent.flow_task.create":
            question = params.get("question")
            if not isinstance(question, str) or not question.strip():
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "Missing or invalid required field: 'question'")
            source, error = _profile_source_params()
            if error is not None:
                return _err(req_id, _ERR_INVALID_PARAMS, error)
            return _ok(req_id, _run_analysis_task(
                "create_flow_task", question=question, **(source or {})))
        if method == "agent.flow_task.select_profile":
            source, error = _profile_source_params()
            if error is not None:
                return _err(req_id, _ERR_INVALID_PARAMS, error)
            return _ok(req_id, _run_analysis_task("select_profile", **(source or {})))
        if method == "agent.flow_task.list_profiles":
            return _ok(req_id, _run_analysis_task("list_profiles"))
        if method == "agent.flow_task.list":
            return _ok(req_id, _run_analysis_task("list_tasks"))

        task_id = params.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "Missing or invalid required field: 'task_id'")
        if method == "agent.flow_task.get":
            include = params.get("include_results", False)
            if not isinstance(include, bool):
                return _err(req_id, _ERR_INVALID_PARAMS,
                            "'include_results' must be a boolean")
            return _ok(req_id, _run_analysis_task("get", task_id=task_id,
                                                  include_results=include))
        task_digest = params.get("task_digest")
        if not isinstance(task_digest, str) or not task_digest:
            return _err(req_id, _ERR_INVALID_PARAMS,
                        "Missing or invalid required field: 'task_digest'")
        operation = ("start_flow_task" if method == "agent.flow_task.start"
                     else "cancel_flow_task")
        return _ok(req_id, _run_analysis_task(operation, task_id=task_id,
                                              task_digest=task_digest))

    # ── agent.proposal.* — evidence-first planner operator-only control API ──────────────
    # 這四個 method 從不註冊成 Agent tool，provider 無法呼叫；只有已通過
    # context boundary 的 direct JSON-RPC caller（例如 terminux-chat 的
    # slash-command 攔截）能用。approve/reject 只改變 proposal 狀態，永遠
    # 不呼叫任何 action handler。
    if method in _PROPOSAL_METHODS:
        if _proposal_registry is None:
            return _err(req_id, _ERR_INTERNAL, "proposal registry unavailable")
        context = get_current_request_context()
        with agent_audit.agent_turn():
            if method == "agent.proposal.list":
                return _ok(req_id, {"proposals": _proposal_registry.list_for_scope(context)})
            if method == "agent.proposal.get":
                proposal_id = params.get("proposal_id")
                if not isinstance(proposal_id, str) or not proposal_id:
                    return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'proposal_id'")
                view = _proposal_registry.get_for_scope(proposal_id, context)
                agent_audit.emit("action.proposal.viewed", tool_call_id=proposal_id,
                                 result_status="found" if view else "not_found", executor_entered=False)
                if view is None:
                    return _err(req_id, -32004, "unknown proposal")
                return _ok(req_id, {"proposal": view})
            if method == "agent.proposal.execute":
                proposal_id = params.get("proposal_id")
                proposal_digest = params.get("proposal_digest")
                if not isinstance(proposal_id, str) or not proposal_id:
                    return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'proposal_id'")
                if not isinstance(proposal_digest, str) or not proposal_digest:
                    return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'proposal_digest'")
                outcome = _execute_approved_proposal(proposal_id, proposal_digest, context)
                if outcome["denial_code"] is not None:
                    return _err(req_id, -32004, "proposal not available for execution")
                view = outcome["proposal"] or {}
                return _ok(req_id, {
                    "proposal": view,
                    "proposal_status": view.get("status"),
                    "execution_status": view.get("execution_status"),
                })
            # approve / reject
            proposal_id = params.get("proposal_id")
            proposal_digest = params.get("proposal_digest")
            if not isinstance(proposal_id, str) or not proposal_id:
                return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'proposal_id'")
            if not isinstance(proposal_digest, str) or not proposal_digest:
                return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'proposal_digest'")
            target = "APPROVED" if method == "agent.proposal.approve" else "REJECTED"
            transition = _proposal_registry.approve if method == "agent.proposal.approve" else _proposal_registry.reject
            view, denial_code = transition(proposal_id, proposal_digest, context)
            if view is None:
                agent_audit.emit("action.proposal.control_denied", tool_call_id=proposal_id,
                                 denial_code=denial_code, executor_entered=False,
                                 policy_source="operator_control_command")
                return _err(req_id, -32004, "proposal not available")
            agent_audit.emit(
                "action.proposal.approved" if target == "APPROVED" else "action.proposal.rejected",
                tool_call_id=proposal_id, tool_name=view.get("action_name"),
                result_status=target, executor_entered=False,
                policy_source="operator_control_command",
            )
            return _ok(req_id, {
                "proposal": view,
                "proposal_status": view["status"],
                "execution_status": view["execution_status"],
            })

    # ── agent.result.* — evidence-first planner operator-only TrustedActionResult control ──
    # 這四個 method 從不註冊成 Agent tool；missing/expired/foreign/wrong-
    # session 一律回傳同一個 generic「不可用」，不對外洩漏 result 是否存在。
    if method in _RESULT_METHODS:
        if _proposal_registry is None:
            return _err(req_id, _ERR_INTERNAL, "proposal registry unavailable")
        context = get_current_request_context()
        with agent_audit.agent_turn():
            if method == "agent.result.list":
                return _ok(req_id, {"results": _proposal_registry.list_trusted_results_for_scope(context)})
            if method == "agent.result.clear":
                _proposal_registry.clear_active_result(context)
                agent_audit.emit("action.result.cleared", executor_entered=False,
                                 policy_source="operator_control_command")
                return _ok(req_id, {"cleared": True})
            result_id = params.get("result_id")
            result_digest = params.get("result_digest")
            if not isinstance(result_id, str) or not result_id:
                return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'result_id'")
            if not isinstance(result_digest, str) or not result_digest:
                return _err(req_id, _ERR_INVALID_PARAMS, "Missing or invalid required field: 'result_digest'")
            if method == "agent.result.get":
                view = _proposal_registry.get_trusted_result_for_scope(result_id, result_digest, context)
                agent_audit.emit("action.result.viewed", tool_call_id=result_id,
                                 result_status="found" if view else "unavailable", executor_entered=False)
                if view is None:
                    return _err(req_id, -32004, "result unavailable")
                return _ok(req_id, {"result": view})
            # agent.result.use
            ok = _proposal_registry.set_active_result(result_id, result_digest, context)
            agent_audit.emit("action.result.selected" if ok else "action.result.selection_denied",
                             tool_call_id=result_id, executor_entered=False,
                             policy_source="operator_control_command")
            if not ok:
                return _err(req_id, -32004, "result unavailable")
            return _ok(req_id, {"active_result_id": result_id})

    # ── rag.reload ─────────────────────────────────────────────────────
    # 手動觸發 shared fingerprint 檢查；背景 rebuild 成功後才原子替換 index，
    # 因此目前的查詢不會被阻塞或讀到半成品。
    if method == "rag.reload":
        if params:
            return _err(req_id, _ERR_INVALID_PARAMS, "rag.reload does not accept params")
        if _rag_embedding is None:
            return _err(req_id, _ERR_INTERNAL, "RAG embedding is unavailable")
        _schedule_shared_rag_rebuild(
            index_dir=Path.home() / ".terminux" / "rag_index",
            user_knowledge_dir=Path.home() / ".terminux" / "rag_knowledge",
            embedding=_rag_embedding,
        )
        return _ok(req_id, {"status": "accepted"})

    # ── extensions.reload／extensions.status ─────────────────────────────
    # 手動啟用 user package：先完整建出新的 activation 再一次替換；失敗時保留
    # 上一份。status 只回報 daemon 目前實際載入的 generation。
    if method in ("extensions.reload", "extensions.status"):
        return _handle_extensions_request(method, req_id, params)

    # ── DQE Viewer read-only service methods（state-isolated）─────────────
    if method == "dqe_viewer.health":
        return _ok(req_id, dqe_viewer_service.health())

    if method == "dqe_viewer.get_retention":
        try:
            return _ok(req_id, dqe_viewer_service.get_retention())
        except Exception:
            _log.error("TERM_DQE_GET_RETENTION_FAILED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.set_retention":
        param_err = _check_dqe_set_retention_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(
                req_id,
                dqe_viewer_service.set_retention(
                    retention_days=params["retention_days"],
                    updated_by=params.get("updated_by"),
                ),
            )
        except InvalidRetentionDaysError:
            return _err(req_id, _ERR_INVALID_PARAMS, "Invalid retention days")
        except RetentionPolicyError:
            _log.error("TERM_DQE_SET_RETENTION_FAILED")
            return _err(req_id, _ERR_INTERNAL, "Failed to write retention policy")
        except Exception:
            _log.error("TERM_DQE_SET_RETENTION_UNEXPECTED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.list_archived_groups":
        param_err = _check_dqe_list_archived_groups_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(
                req_id,
                dqe_viewer_service.list_archived_groups(str(params["version_dir"])),
            )
        except DqeViewerServiceError:
            _log.error("TERM_DQE_ARCHIVED_LIST_FAILED")
            return _err(req_id, _ERR_INTERNAL, "DQE Viewer service error")
        except Exception:
            _log.error("TERM_DQE_ARCHIVED_LIST_UNEXPECTED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.list_temporary_groups":
        param_err = _check_dqe_list_temporary_groups_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(
                req_id,
                dqe_viewer_service.list_temporary_groups(
                    db_path=params.get("db_path"),
                    retention_days=params.get("retention_days"),
                ),
            )
        except DqeViewerServiceError:
            _log.error("TERM_DQE_TEMPORARY_LIST_FAILED")
            return _err(req_id, _ERR_INTERNAL, "DQE Viewer service error")
        except Exception:
            _log.error("TERM_DQE_TEMPORARY_LIST_UNEXPECTED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.load_group":
        param_err = _check_dqe_load_group_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(req_id, dqe_viewer_service.load_group(str(params["group_ref"])))
        except DqeViewerInvalidRefError:
            return _err(req_id, _ERR_INVALID_PARAMS, "Invalid group reference")
        except DqeViewerServiceError:
            _log.error("TERM_DQE_GROUP_LOAD_FAILED")
            return _err(req_id, _ERR_INTERNAL, "DQE Viewer service error")
        except Exception:
            _log.error("TERM_DQE_GROUP_LOAD_UNEXPECTED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.load_attachment":
        param_err = _check_dqe_load_attachment_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(
                req_id,
                dqe_viewer_service.load_attachment_for_viewer(
                    str(params["attachment_ref"])
                ),
            )
        except DqeViewerInvalidRefError:
            return _err(req_id, _ERR_INVALID_PARAMS, "Invalid attachment reference")
        except Exception:
            _log.error("TERM_DQE_ATTACHMENT_LOAD_FAILED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.checkin_job":
        param_err = _check_dqe_checkin_job_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(req_id, dqe_viewer_service.checkin_job(params))
        except Exception:
            _log.error("TERM_DQE_CHECKIN_FAILED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.preview_purge":
        param_err = _check_dqe_preview_purge_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(req_id, dqe_viewer_service.preview_purge(params))
        except Exception:
            _log.error("TERM_DQE_PURGE_PREVIEW_FAILED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    if method == "dqe_viewer.purge_jobs":
        param_err = _check_dqe_purge_jobs_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            return _ok(req_id, dqe_viewer_service.purge_jobs(params))
        except Exception:
            _log.error("TERM_DQE_PURGE_FAILED")
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    # ── terminal.register ──────────────────────────────────────────────
    if method == "terminal.register":
        _tid = params.get("terminal_id", "")
        if not _tid:
            return _err(req_id, _ERR_INVALID_PARAMS, "Missing required field: 'terminal_id'")
        if _stream_manager is not None:
            _stream_manager.register_terminal(_tid)
        return _ok(req_id, {"status": "registered", "terminal_id": _tid})

    # ── terminal.unregister ────────────────────────────────────────────
    if method == "terminal.unregister":
        _tid = params.get("terminal_id", "")
        if not _tid:
            return _err(req_id, _ERR_INVALID_PARAMS, "Missing required field: 'terminal_id'")
        if _stream_manager is not None:
            _stream_manager.unregister_terminal(_tid)
        return _ok(req_id, {"status": "unregistered", "terminal_id": _tid})

    # ── preferences.get_language ───────────────────────────────────────
    # GUI 啟動時讀回目前偏好語言以設定下拉勾選狀態。
    if method == "preferences.get_language":
        return _ok(req_id, {"language": load_preferred_language()})

    # ── preferences.set_language ───────────────────────────────────────
    # GUI 下拉選擇 → daemon 寫入 ~/.terminux/preferences.json（daemon 自己的
    # HOME，避免跨帳號權限問題）。非法值由 set_preferred_language normalize。
    if method == "preferences.set_language":
        _lang_in = params.get("language", "")
        if not _lang_in:
            return _err(req_id, _ERR_INVALID_PARAMS, "Missing required field: 'language'")
        _applied = set_preferred_language(str(_lang_in))
        return _ok(req_id, {"status": "ok", "language": _applied})

    # ── llm.ask ────────────────────────────────────────────────────────
    # standalone 透過同帳號 daemon 借用既有 LLMRouter；失敗沿用 package LLM
    # facade 的降級語意，回傳 null 而非讓 daemon 崩潰。
    if method == "llm.ask":
        param_err = _check_llm_ask_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        timeout_value = params.get("timeout", 30.0)
        timeout = float(timeout_value) if timeout_value is not None else None
        # input validation 完成後、呼叫 LlmService(...).ask() 前
        # acquire 既有 `_llm_lock`；只包住 router/service ask 本身，不包
        # 上面的 param 檢查或下面的 response 序列化。
        if not _llm_lock.acquire(timeout=15.0):
            return _ok(req_id, {"result": None, "provider_called": False})
        try:
            service = LlmService(router=_package_llm_router)
            response = service.ask(params["prompt"], timeout=timeout)
        finally:
            _llm_lock.release()
        return _ok(req_id, {"result": response, "provider_called": service._provider_called.get()})

    # ── lineage.pre_exec ─────────────────────────────────────────────────
    if method == "lineage.pre_exec":
        param_err = _check_pre_exec_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            # === per-user gap detection（在所有既有 logic 之前）===
            if _event_store is not None and _dreamer is not None and _user_id:
                try:
                    _check_gap_and_trigger_immediate_digest(
                        event_store=_event_store,
                        dreamer=_dreamer,
                        user_id=_user_id,
                        now_epoch=time.time(),
                    )
                except Exception as _gap_exc:
                    _log.warning("gap detection failed (non-fatal): %s", _gap_exc)

            # ANSI sanitization（Bug 3+2 防護）
            params["command"] = _sanitize_command(params["command"])
            if params["command"].count("> ") + params["command"].count("$ ") >= 2:
                _log.warning("Possible echo state machine glitch in pre_exec command: %r",
                             params["command"])

            # B 方案：source==qt_terminal_typed 表示 cmd 來自 C++ keystroke buffer，
            # 未經 shell history/alias/glob 展開（!N、aliases 等）。lineage check 用
            # 這種 cmd 會抓不到 EDA tool。直接 short-circuit：跳過 handle_pre_exec
            # 與 nudge，回最低限度的 ALLOW decision 給 caller。權威 pre_exec 由
            # shell hook 以 source=shell_hook 補送（含 expanded cmd），lineage check
            # 在那條路徑做。
            _early_source = params.get("source", "qt_terminal")
            if _early_source == "qt_terminal_typed":
                # B 方案 hand-off：C++ 在 user 按 Enter 立刻 fire pre_exec，
                # wrapper 的 shell_hook pre_exec 通常稍晚到（tcsh expansion +
                # exec 開銷）。typed 進來時 cache 通常還空。
                # 短輪詢等 shell_hook 寫入 cache：
                #   - 800ms 仍空 → fast-fail SKIPPED（不卡非 EDA cmd 的 prompt）
                #   - 看到 PENDING marker（shell_hook 跑 explainer 中）→ 延長等
                #     到 20s，讓 LLM 把 AI 解釋寫進 final decision
                #   - 看到 final decision → 立刻 hand-off 給 C++
                _short_deadline = time.monotonic() + 0.8
                _long_deadline  = time.monotonic() + 20.0
                _cached_decision: dict | None = None
                _saw_pending = False
                while True:
                    _now_mono = time.monotonic()
                    if _now_mono >= _long_deadline:
                        break
                    if not _saw_pending and _now_mono >= _short_deadline:
                        break
                    _now = time.time()
                    with _recent_lineage_warns_lock:
                        _entry = _recent_lineage_warns.get(params["cwd"])
                        if _entry is not None:
                            _ts, _decision = _entry
                            if _now - _ts > _RECENT_LINEAGE_WARN_TTL_SEC:
                                _recent_lineage_warns.pop(params["cwd"], None)
                            elif _decision.get("action") == "PENDING":
                                _saw_pending = True
                            else:
                                _cached_decision = _decision
                                _recent_lineage_warns.pop(params["cwd"], None)
                                break
                    time.sleep(0.05)  # 50ms 輪詢間隔
                if _cached_decision is not None:
                    _log.debug(
                        "lineage.pre_exec: hand-off recent shell_hook warn to "
                        "qt_terminal_typed (cwd=%s, action=%s)",
                        params["cwd"], _cached_decision.get("action"),
                    )
                    return _ok(req_id, _cached_decision)
                _log.debug(
                    "lineage.pre_exec: skipping lineage check (source=qt_terminal_typed, "
                    "cmd=%s, saw_pending=%s) — awaiting authoritative shell_hook pre_exec",
                    params["command"], _saw_pending,
                )
                return _ok(req_id, {
                    "action": "ALLOW",
                    "message": "",
                    "status": "SKIPPED_NON_AUTHORITATIVE",
                })

            # preexec_request_id generated after validation, before handle_pre_exec
            preexec_request_id = uuid4().hex
            provider = _get_runtime_provider()
            enable_explainer = os.environ.get(_ENV_ENABLE_LINEAGE_EXPLAINER) == "1"
            enable_fallback  = os.environ.get(_ENV_ENABLE_INTENT_FALLBACK)   == "1"

            # B 方案 hand-off pending marker：當 source==shell_hook 而且開了
            # explainer 時，先寫一個 PENDING 進 cache（讓 C++ qt_terminal_typed
            # 端 polling 知道「LLM 跑中、值得等」），handle_pre_exec 跑完才覆寫
            # 為 final decision。C++ poll 看到 pending 會延長 wait 到 20s（給 LLM
            # 足夠時間）。沒 pending 就 fast-fail 800ms（一般 cmd 不卡 prompt）。
            _is_shell_hook = (_early_source == "shell_hook")
            if _is_shell_hook and enable_explainer:
                with _recent_lineage_warns_lock:
                    _recent_lineage_warns[params["cwd"]] = (
                        time.time(),
                        {"action": "PENDING", "message": "", "explanation": None},
                    )

            # total_preexec_latency_ms = server call-site elapsed time
            _t_start = time.monotonic()
            decision = handle_pre_exec(
                command=params["command"],
                cwd=params["cwd"],
                ledger_path=_resolve("ledger_path", ledger_path),
                tool_signatures_path=_resolve("tool_signatures_path", tool_signatures_path),
                lineage_rules_path=_resolve("lineage_rules_path", lineage_rules_path),
                lineage_explainer_provider=provider if enable_explainer else None,
                enable_lineage_explainer=enable_explainer,
                intent_fallback_provider=provider if enable_fallback else None,
                enable_intent_fallback=enable_fallback,
            )
            _total_ms = (time.monotonic() - _t_start) * 1000.0

            # B 方案 hand-off：如果 source==shell_hook（權威）跑出 warn/block，
            # 寫進 _recent_lineage_warns cache，讓緊接著進來的 C++ qt_terminal_typed
            # pre_exec 能拿回這個 decision 顯示到 NotificationBar（C++ source 自己
            # 拿 typed cmd 跑 lineage check 沒意義，cmd 可能是 !N 等未展開字串）。
            if _early_source == "shell_hook" and decision.action in ("warn", "block"):
                with _recent_lineage_warns_lock:
                    _recent_lineage_warns[params["cwd"]] = (
                        time.time(),
                        _serialize_preexec_decision(decision),
                    )

            # decision_source / intent_source inferred by server
            _explanation_present = decision.explanation is not None
            _decision_source = (
                "checker_plus_llm_explainer"
                if (_explanation_present and provider is not None and enable_explainer)
                else "checker_only"
            )
            _intent_source = "unknown" if enable_fallback else "fixed_parser"

            # Multi-stream dispatch
            _terminal_id = params.get("terminal_id", "default")
            _stream = _get_stream(_terminal_id)

            # ── Gap detection（只在 lineage.pre_exec 主路徑，shell_hook 不做）──
            if _stream is not None:
                _event_ts = time.time()
                with _stream.lock:
                    _gap = _event_ts - _stream.last_activity

                    if _gap > ACTIVITY_GAP_THRESHOLD_SEC:
                        # period_end = 舊的 last_activity（不含休息時間）
                        # 在 stream.lock 內 submit（submit 是 non-blocking）
                        if _dreamer is not None:
                            _dreamer.submit_immediate_digest(
                                user_id=_user_id,
                                terminal_id=_terminal_id,
                                period_start=_stream.period_start,
                                period_end=_stream.last_activity,
                            )
                        _stream.reset_period()
                        _log.info(
                            "Gap detected on terminal %s (%.1f sec); submitted immediate digest",
                            _terminal_id, _gap,
                        )

            # emit lineage_check event（僅在 lineage check 實際執行時）
            # 附帶結構化 findings（list of dict），讓 consumer
            # （_get_recent_checks / Agent prompt）能讀 category / suggestion /
            # context 而不必再 string-parse message。
            if decision.status != "UNSUPPORTED_TOOL":
                _lc_payload: dict = {
                    "tool": decision.tool_name,
                    "judgment": decision.status,
                    "action": decision.action,
                    "message": decision.message,
                }
                _lc_findings = list(getattr(decision, "findings", ()) or ())
                if _lc_findings:
                    # findings 已是 dict（preexec_api 序列化過）
                    _lc_payload["findings"] = _lc_findings
                _emit_event("lineage_check", _lc_payload, terminal_id=_terminal_id)

            # ── block_name 三層提取（在 on_pre_exec 之前，確保 emit 帶有 block_name）──
            _stream_acc = _stream.accumulator if _stream is not None else _ecc_accumulator
            if _stream_acc is not None:
                try:
                    _resolved_ledger = _resolve("ledger_path", ledger_path)
                    _block = extract_block_name_from_runfile(decision.runfile_path)
                    if _block is None:
                        _block = extract_block_name_from_lineage(
                            str(_resolved_ledger) if _resolved_ledger else None,
                        )
                    if _block is not None:
                        _bn_lock = _stream.lock if _stream is not None else _ecc_lock
                        with _bn_lock:
                            _stream_acc.update_block_name(_block)
                    elif _ecc_companion is not None:
                        # Layer 3: async AI fallback（fire-and-forget，不阻塞）
                        def _async_infer_block_name(
                            cmd: str = params["command"],
                            cwd: str = params["cwd"],
                            _sa: Any = _stream_acc,
                            _sl: Any = _stream.lock if _stream is not None else _ecc_lock,
                        ) -> None:
                            try:
                                _llm_acquired = _llm_lock.acquire(timeout=5.0)
                                if not _llm_acquired:
                                    return
                                try:
                                    inferred = _ecc_companion.infer_block_name(cmd, cwd)
                                finally:
                                    _llm_lock.release()
                                if inferred is not None and _sa is not None:
                                    with _sl:
                                        _sa.update_block_name(inferred)
                            except Exception as _ai_exc:
                                _log.warning("Async block_name AI inference failed: %s", _ai_exc)

                        threading.Thread(
                            target=_async_infer_block_name,
                            daemon=True,
                        ).start()
                except Exception as _bn_exc:
                    _log.warning("block_name extraction failed: %s", _bn_exc)

            # ECC pre_exec hook（不影響 decision，失敗靜默忽略）
            # tool_name 優先使用 lineage decision，fallback 取 command 第一個 token
            _ecc_tool_name = decision.tool_name
            if _ecc_tool_name is None:
                _ecc_parts = params["command"].split()
                _ecc_tool_name = _ecc_parts[0] if _ecc_parts else None
            _pre_source = params.get("source", "qt_terminal")
            if _stream_acc is not None:
                try:
                    _pre_lock = _stream.lock if _stream is not None else _ecc_lock
                    with _pre_lock:
                        # findings 走 getattr 防禦：tests 內某些 FakePreExecDecision
                        # 沒帶這個欄位（older fixture），不應因此整個 on_pre_exec 失敗
                        _stream_acc.on_pre_exec(
                            command=params["command"],
                            cwd=params["cwd"],
                            tool_name=_ecc_tool_name,
                            source=_pre_source,
                            preexec_result=decision.action,
                            preexec_message=decision.message or "",
                            preexec_findings=list(getattr(decision, "findings", ()) or ()),
                        )
                        _log.debug(
                            "ECC pre_exec: cmd=%s, cwd=%s, tool=%s, stage=%s, source=%s",
                            params["command"], params["cwd"], _ecc_tool_name,
                            _stream_acc.get_current().current_stage_id,
                            _pre_source,
                        )
                    if _stream is not None:
                        _stream.update_activity()
                except Exception as _ecc_exc:
                    _log.warning("ECC on_pre_exec failed: %s", _ecc_exc)

            # ── structured observability log（不影響 result，失敗靜默忽略）──
            try:
                _append_preexec_log({
                    "event_type": "preexec_decision",
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "preexec_request_id": preexec_request_id,
                    "explanation_job_id": None,
                    "command": params["command"],
                    "tool_name": decision.tool_name,
                    "runfile_path": decision.runfile_path,
                    "action": decision.action,
                    "status": decision.status,
                    "decision_source": _decision_source,
                    "intent_source": _intent_source,
                    "explanation_present": _explanation_present,
                    "surfaced_mode": "explanation" if _explanation_present else "message_fallback",
                    "runtime_provider_used": provider is not None and (enable_explainer or enable_fallback),
                    "runtime_provider_error": "provider initialization failed" if _runtime_provider_failed else None,
                    "async_explanation_status": "not_requested",
                    "ipc_request_latency_ms": None,
                    "checker_latency_ms": None,
                    "intent_fallback_latency_ms": None,
                    "explainer_latency_ms": None,
                    "surface_latency_ms": None,
                    "total_preexec_latency_ms": _total_ms,
                })
            except Exception as _log_exc:
                _log.warning("Observability log call failed: %s", _log_exc)

            # ── feed CommandBuffer（authoritative path 只在這裡 append；
            # shell.pre_exec 不再 append 以避免雙觸發）──
            if _command_buffer is not None:
                try:
                    _command_buffer.append(
                        command=params["command"], cwd=params["cwd"]
                    )
                except Exception as _cb_exc:
                    _log.debug("CommandBuffer.append non-fatal: %s", _cb_exc)

            _pre_resp = _serialize_preexec_decision(decision)
            return _ok(req_id, _pre_resp)
        except Exception as _le:
            _log.exception("lineage.pre_exec handler raised: %s", _le)
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    # ── lineage.post_exec ────────────────────────────────────────────────
    if method == "lineage.post_exec":
        param_err = _check_post_exec_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        try:
            # ANSI sanitization（Bug 3+2 防護）
            params["command"] = _sanitize_command(params["command"])
            if params["command"].count("> ") + params["command"].count("$ ") >= 2:
                _log.warning("Possible echo state machine glitch in post_exec command: %r",
                             params["command"])

            # optional caller_pid 從 RPC params 取（int|None），
            # 傳給 handle_post_exec → lookup_trace_for_post_exec(fallback_pid=...)。
            # C++ 端目前不送 pid → caller_pid=None → 既有行為。
            _caller_pid_raw = params.get("pid")
            try:
                _caller_pid: int | None = int(_caller_pid_raw) if _caller_pid_raw is not None else None
            except (TypeError, ValueError):
                _caller_pid = None
            if "nodename" in params and params["nodename"] != local_nodename():
                _caller_pid = None
            result = handle_post_exec(
                command=params["command"],
                cwd=params["cwd"],
                exit_code=params["exit_code"],
                ledger_path=_resolve("ledger_path", ledger_path),
                tool_signatures_path=_resolve("tool_signatures_path", tool_signatures_path),
                caller_pid=_caller_pid,
                nodename=params.get("nodename"),
                post_exec_ts=(float(params["ts"])
                              if isinstance(params.get("ts"), (int, float)) else None),
            )
            result_dict = asdict(result)

            # ECC pipeline 由 _run_post_exec_pipeline() 處理，與 shell.post_exec 共用
            _terminal_id = params.get("terminal_id", "default")
            _post_source = params.get("source", "qt_terminal")
            _stdout_tail: list[str] = params.get("stdout_tail", [])
            if not isinstance(_stdout_tail, list):
                _stdout_tail = []

            _pipeline = _run_post_exec_pipeline(
                command=params["command"],
                cwd=params["cwd"],
                exit_code=params["exit_code"],
                stdout_tail=_stdout_tail,
                source=_post_source,
                terminal_id=_terminal_id,
                tool_name=result.tool_name,   # lineage 識別出的 EDA tool
            )
            result_dict.update(_pipeline)
            return _ok(req_id, result_dict)
        except Exception:
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    # ── shell.pre_exec ─────────────────────────────────────────────────
    if method == "shell.pre_exec":
        param_err = _check_shell_pre_exec_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        command = _sanitize_command(params.get("command", ""))
        cwd = params.get("cwd", "")
        source = params.get("source", "shell_hook")
        _sh_terminal_id = params.get("terminal_id", "default")
        _emit_event("shell_pre_exec", {
            "command": command, "cwd": cwd, "source": source,
        }, terminal_id=_sh_terminal_id)
        # 觸發 accumulator（去重 guard 處理 qt_terminal lineage.pre_exec 與 shell_hook
        # 同命令重複觸發的情況：500ms 內同 command 只處理一次）
        _sh_stream = _get_stream(_sh_terminal_id)
        _sh_acc = _sh_stream.accumulator if _sh_stream is not None else _ecc_accumulator
        if _sh_acc is not None and command:
            try:
                _sh_tool_parts = command.split()
                _sh_tool_name = _sh_tool_parts[0] if _sh_tool_parts else None
                _sh_lock = _sh_stream.lock if _sh_stream is not None else _ecc_lock
                with _sh_lock:
                    _sh_acc.on_pre_exec(
                        command=command,
                        cwd=cwd,
                        tool_name=_sh_tool_name,
                        source=source,
                    )
                    _log.debug(
                        "shell.pre_exec accumulator: cmd=%s, source=%s",
                        command, source,
                    )
                if _sh_stream is not None:
                    _sh_stream.update_activity()
            except Exception as _sh_exc:
                _log.warning("shell.pre_exec accumulator failed: %s", _sh_exc)
        _sh_resp: dict = {"status": "ok"}
        return _ok(req_id, _sh_resp)

    # ── shell.post_exec ────────────────────────────────────────────────
    if method == "shell.post_exec":
        param_err = _check_shell_post_exec_params(params)
        if param_err:
            return _err(req_id, _ERR_INVALID_PARAMS, param_err)
        _sp_pin = _pin_user_activation()
        try:
            return _handle_shell_post_exec(req_id, params, _sp_pin)
        finally:
            if _sp_pin is not None and not _sp_pin.transferred:
                _sp_pin.release()

    # ── notify.poll ─────────────────────────────────────────────────────
    # 每個 GUI 以自己的 cursor 讀取同一份緩衝；首次連線只取得目前位置。
    if method == "notify.poll":
        after_seq = params.get("after_seq")
        if "after_seq" in params and (isinstance(after_seq, bool)
                                       or not isinstance(after_seq, int)):
            return _err(req_id, _ERR_INVALID_PARAMS, "Field 'after_seq' must be an int")
        with _pending_async_lock:
            _np_resp = {
                "notifications": ([dict(payload) for seq, payload in _pending_async_notifications
                                   if seq > after_seq] if after_seq is not None else []),
                "latest_seq": _pending_async_seq,
                "instance_id": _get_daemon_instance_id(),
            }
        return _ok(req_id, _np_resp)

    # ── package.invoke_action ───────────────────────────────────────────
    if method == "package.invoke_action":
        try:
            result = _invoke_package_action(params)
            return _ok(req_id, result)
        except ValueError as exc:
            return _err(req_id, _ERR_INVALID_PARAMS, str(exc))
        except Exception as exc:
            _log.warning("package.invoke_action failed: %s", exc)
            return _err(req_id, _ERR_INTERNAL, "Internal error")

    # ── ecc.chat_query ────────────────────────────────────────────────
    # method == "ecc.chat_query"
    param_err = _check_chat_query_params(params)
    if param_err:
        return _err(req_id, _ERR_INVALID_PARAMS, param_err)
    try:
        # ANSI sanitization（Bug 3+2 防護）
        query = _sanitize_command(params["query"])
        ask_id = params.get("ask_id", "")
        _cq_origin = params.get("origin") if isinstance(params.get("origin"), str) else None
        # Profile 模式的拒絕／本機指令必須早於 stream／Profile 載入、embedding 與 provider。
        _contained_answer = _profile_contained_chat_answer(query, _cq_origin) if query else None
        if _contained_answer is None and query:
            # Profile 檔存在但無效：固定碼直達學員，不讓下游任何 load_profile() 變成 RPC 內部錯誤。
            from terminux_engine.llm.task_scope import (
                TaskScopeRejected as _ProfileRejected, load_profile as _load_profile_for_chat,
            )
            try:
                _load_profile_for_chat()
            except _ProfileRejected:
                _contained_answer = _PROFILE_INVALID_ANSWER
        if _contained_answer is not None:
            _contained_result: dict = {"answer": _contained_answer, "query": query}
            if ask_id:
                _contained_result["ask_id"] = ask_id
            return _ok(req_id, _contained_result)
        # Default 模式：只有明確標示 `at_command` 的 `/` 文字才在 embedding 之前走本機指令
        # parser；`selection` 永遠是資料。沒有 `origin` 的舊 client 維持既有順序（parser 在
        # hybrid search 之後），除非管理端以 TERMINUX_CHAT_QUERY_REQUIRE_ORIGIN 要求一律視為資料。
        _cq_slash = bool(query) and query.strip().startswith("/")
        _cq_control_early = _cq_slash and _cq_origin == CHAT_ORIGIN_AT_COMMAND
        _cq_control_legacy = (_cq_slash and _cq_origin is None
                              and not _flag_enabled(_ENV_CHAT_ORIGIN_REQUIRED))
        if _cq_control_early:
            _early_answer = _run_local_command_parsers(query)
            if _early_answer is not None:
                _early_result: dict = {"answer": _early_answer, "query": query}
                if ask_id:
                    _early_result["ask_id"] = ask_id
                return _ok(req_id, _early_result)
        _cq_terminal_id = params.get("terminal_id", "default")
        _cq_stream = _get_stream(_cq_terminal_id)
        _cq_acc = _cq_stream.accumulator if _cq_stream is not None else _ecc_accumulator
        if not query:
            return _ok(req_id, {"answer": "", "query": ""})

        # ── lineage graph keyword check（不走 LLM，直接生成 HTML）──
        _LINEAGE_GRAPH_KEYWORDS = (
            "lineage graph", "lineage dag", "blood line",
            "show lineage", "血緣圖", "祖譜圖",
        )
        if any(kw in query.lower() for kw in _LINEAGE_GRAPH_KEYWORDS):
            _graph_lang = load_preferred_language()
            try:
                from terminux_engine.lineage.graph import render_lineage_html
                from terminux_engine.lineage.ledger import load_job_commits as _load_commits

                _ledger = os.path.expanduser("~/.terminux/lineage/ledger.jsonl")
                _commits = _load_commits(_ledger)
                _graph_open_path: str | None = None
                if _commits:
                    _html_path = os.path.expanduser("~/.terminux/lineage_graph.html")
                    Path(_html_path).parent.mkdir(parents=True, exist_ok=True)
                    _html = render_lineage_html(_commits)
                    with open(_html_path, "w", encoding="utf-8") as _f:
                        _f.write(_html)
                    _graph_open_path = _html_path
                    answer = _tr(
                        "lineage_graph_generated", _graph_lang,
                        path=_html_path, count=len(_commits),
                    )
                else:
                    answer = _tr("lineage_graph_empty", _graph_lang)
            except Exception as _graph_exc:
                _log.warning("Lineage graph generation failed: %s", _graph_exc)
                answer = _tr("lineage_graph_failed", _graph_lang, error=_graph_exc)
                _graph_open_path = None
            _graph_result: dict = {"answer": answer, "query": query}
            # open_path：C++ 端以 QUrl::fromLocalFile 在使用者 GUI display 開啟
            if _graph_open_path:
                _graph_result["open_path"] = _graph_open_path
            if ask_id:
                _graph_result["ask_id"] = ask_id
            return _ok(req_id, _graph_result)

        # 明確的「開啟 DQE Viewer」是使用者 side-effect 指令，不應落入 RAG 問答。
        _dqe_viewer_result = _handle_dqe_viewer_launch_query(query, ask_id)
        if _dqe_viewer_result is not None:
            return _ok(req_id, _dqe_viewer_result)

        # ── 搜索相關歷史（在 LLM lock 外執行，不阻塞）──
        # 明確的控制意圖文字（at_command 的 `/…`）不進 embedding。
        _cq_related: list = []
        if _hybrid_search is not None and not _cq_control_early:
            try:
                _cq_related = _hybrid_search.search(
                    query=query,
                    user_id=_user_id,
                    limit=3,
                    time_window_days=90,
                )
            except Exception as _search_exc:
                _log.warning("Hybrid search failed (non-fatal): %s", _search_exc)

        # ── 取其他 terminal 摘要 ──
        _cq_other_terminals: list[dict] = []
        if _stream_manager is not None:
            _cq_other_terminals = _stream_manager.get_other_streams_summary(_cq_terminal_id)

        # ── 載入 recent_work_history（失敗降級為 None）──
        _cq_recent_history = None
        if _event_store is not None and _user_id and _ecc_companion is not None:
            try:
                _cq_recent_history = load_recent_work_history(
                    event_store=_event_store,
                    user_id=_user_id,
                    now_epoch=time.time(),
                    recent_history_days=_ecc_companion.RECENT_HISTORY_DAYS,
                    max_digest_chars=_ecc_companion.MAX_DIGEST_CHARS,
                    max_today_immediate=_ecc_companion.MAX_TODAY_IMMEDIATE,
                    max_immediate_chars=_ecc_companion.MAX_IMMEDIATE_CHARS,
                )
            except Exception as _rwh_exc:
                _log.warning(
                    "load_recent_work_history failed (non-fatal, falling back to session_history): %s",
                    _rwh_exc,
                )
                _cq_recent_history = None

        answer = ""
        # operator control command 在送往 provider 之前由 host 截取；
        # 不論 ECC/companion 狀態如何都優先處理，自然語言（例如「我同意」）
        # 不會落到這裡，因為它不符合這個嚴格的 slash-command 格式。
        # 帶 `origin=at_command` 的請求已在 hybrid search 之前跑過同一組 parser；
        # 這裡只服務沒有 `origin` 的舊 client（Default 既有順序）。
        if _cq_control_legacy:
            _legacy_answer = _run_local_command_parsers(query)
            if _legacy_answer is not None:
                return _ok(req_id, {"answer": _legacy_answer, "query": query})

        # Agent evidence（若走 Agent 路徑）
        _agent_evidence_list: list = []
        # Profile 模式本 turn 的歷史投影摘要（只有計數）；GUI 據此顯示未納入 AI 上下文的回合數。
        _cq_context_status: dict = {}
        if _cq_acc is not None and _ecc_companion is not None:
            # Lock: 只拿 ECC snapshot
            _cq_lock = _cq_stream.lock if _cq_stream is not None else _ecc_lock
            with _cq_lock:
                ecc = _cq_acc.get_current()

            # 先試 Agent 路徑。回 (str, list) 即用；
            # 回 (None, []) 表示要 fallback 到 ai_companion 既有邏輯。
            _cq_conv_for_agent = (
                _cq_stream.conversation if _cq_stream is not None else None
            )
            _cq_status_token = _chat_context_status.set(None)
            try:
                _agent_answer, _agent_evidence_list = _try_agent_chat_query(
                    query, _cq_acc, conversation=_cq_conv_for_agent
                )
                _cq_context_status = dict(_chat_context_status.get() or {})
            finally:
                _chat_context_status.reset(_cq_status_token)
            if _agent_answer is not None:
                answer = _agent_answer
            else:
                # LLM 推理 — timeout=15s，逾時回 fallback
                _llm_acquired = _llm_lock.acquire(timeout=15.0)
                try:
                    if _llm_acquired:
                        # per-stream conversation buffer
                        _cq_conv = _cq_stream.conversation if _cq_stream is not None else None
                        answer = _ecc_companion.answer_query(
                            query=query,
                            ecc=ecc,
                            conversation=_cq_conv,
                            related_history=_cq_related if _cq_related else None,
                            other_terminals=_cq_other_terminals if _cq_other_terminals else None,
                            recent_work_history=_cq_recent_history,
                        )
                    else:
                        _log.info("LLM lock busy (15s timeout), using deterministic fallback for chat_query")
                        ecc_summary = _ecc_companion.summarize_ecc(ecc)
                        answer = _ecc_companion._deterministic_query_fallback(query, ecc_summary)
                finally:
                    if _llm_acquired:
                        _llm_lock.release()
        else:
            # ECC 僅是問答的選用 context；缺少 playbook 不阻擋 Agent／provider。
            _cq_status_token = _chat_context_status.set(None)
            try:
                _agent_answer, _agent_evidence_list = _try_agent_chat_query(query, None)
                _cq_context_status = dict(_chat_context_status.get() or {})
            finally:
                _chat_context_status.reset(_cq_status_token)
            answer = (_agent_answer if _agent_answer is not None
                      else _answer_query_without_ecc(query))

        # emit chat_query event
        _llm_backend = os.environ.get("TERMINUX_LLM_BACKEND", "none")
        _chat_query_payload: dict = {
            "query": query,
            "response": answer,
            "model": _llm_backend,
        }
        if _agent_evidence_list:
            _chat_query_payload["evidence"] = [
                {
                    "tool_name": e.tool_name,
                    "arguments": e.arguments,
                    "success": e.success,
                }
                for e in _agent_evidence_list
            ]
        _emit_event("chat_query", _chat_query_payload, terminal_id=_cq_terminal_id)
        if _cq_stream is not None:
            _cq_stream.update_activity()

        _chat_result: dict = {"answer": answer, "query": query}
        if ask_id:
            _chat_result["ask_id"] = ask_id
        if _cq_context_status:
            _chat_result["context_status"] = {"schema": "terminux.chat_context_status.v1", **_cq_context_status}
        # open_report tool 若在本次 agent run 內被呼叫 → 附 open_path 給 C++ 開啟
        _cq_open_path = _drain_pending_open_path()
        if _cq_open_path:
            _chat_result["open_path"] = _cq_open_path
        return _ok(req_id, _chat_result)
    except Exception:
        return _err(req_id, _ERR_INTERNAL, "Internal error")


def _daemon_guard_error(req_id: Any, peer_uid: int | None) -> dict | None:
    """per-member daemon guard——回傳 error dict（fail-closed）
    或 ``None``（放行）。

    只在 module-level ``_daemon_euid`` 已被 bootstrap 設定時才強制執行
    （production `main.py` 一律會設定；`_daemon_euid is None` 代表獨立
    測試／未接線情境，維持既有行為不受影響，向後相容）。這個檢查發生在
    request 被 parse／dispatch 之前，涵蓋這個 listener 收到的**每一筆**
    request，不只是 context-required 的 orchestration.* / ecc.chat_query
    （不同 UID 必須在 session/request/workspace 操作前 fail-closed）。
    """
    if _daemon_euid is None:
        return None
    if is_peer_authorized_for_daemon(peer_uid, _daemon_euid):
        return None
    return _err(req_id, _ERR_PEER_UID_NOT_AUTHORIZED, "peer uid not authorized for this daemon")


def serve_once(
    socket_path: str | Path,
    *,
    tool_signatures_path: str | Path | None = None,
    lineage_rules_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    _ready_event: threading.Event | None = None,
    # 與 serve_forever 對稱接線，供測試直接注入 registry。
    session_registry: CanonicalSessionContextRegistry | None = None,
    # 與 serve_forever 對稱接線，供測試直接注入 daemon EUID 錨點。
    daemon_euid: int | None = None,
) -> None:
    """
    Unix Domain Socket server：接受一個 client 連線，完成一次 request/response，結束。
    line-based protocol：request 與 response 各佔一行（以 \\n 分隔）。
    _ready_event：bind 完成後 set，供測試同步。
    """
    global _session_registry, _daemon_euid  # noqa: PLW0603
    if session_registry is not None:
        _session_registry = session_registry
        _attach_run_binding_revocation(session_registry)
    if daemon_euid is not None:
        _daemon_euid = daemon_euid
    path = Path(socket_path)
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity: BoundUnixSocket | None = None
    try:
        identity = bind_owned_unix_socket(server_sock, path)
        server_sock.listen(1)

        if _ready_event is not None:
            _ready_event.set()  # 通知測試：server 已就緒

        conn, _ = server_sock.accept()
        with conn:
            # peer UID 必須來自 accepted socket 本身（root of
            # trust），不是 client params。
            peer_uid = resolve_peer_uid_from_socket(conn)
            # 讀取到第一個 \n 為止
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk

            line = data.split(b"\n")[0]
            try:
                request_obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                request_obj = {}   # 將由 handle_jsonrpc_request 回傳 -32600

            # 在 dispatch 之前擋下未授權 peer——不進
            # handle_jsonrpc_request，不碰任何 session/request/workspace 邏輯。
            guard_error = _daemon_guard_error(
                request_obj.get("id") if isinstance(request_obj, dict) else None,
                peer_uid,
            )
            if guard_error is not None:
                conn.sendall((json.dumps(guard_error) + "\n").encode("utf-8"))
                return

            response = handle_jsonrpc_request(
                request_obj,
                tool_signatures_path=tool_signatures_path,
                lineage_rules_path=lineage_rules_path,
                ledger_path=ledger_path,
                peer_uid=peer_uid,
            )
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
    finally:
        server_sock.close()
        if identity is not None:
            unlink_owned_unix_socket(identity)


def _handle_connection(
    conn: socket.socket,
    *,
    tool_signatures_path: str | Path | None = None,
    lineage_rules_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> None:
    """處理單一 client 連線：讀一行 JSON request，回傳一行 JSON response。"""
    curr_t = threading.current_thread()
    with _active_request_lock:
        _active_request_threads.add(curr_t)
    try:
        with conn:
            # peer UID 必須來自 accepted socket 本身（root of
            # trust），不是 client params；一個連線只處理一筆 request，peer
            # UID 剛好對應到這一筆 request 的 caller，不需額外 pinning。
            peer_uid = resolve_peer_uid_from_socket(conn)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk

            if not data.strip():
                return

            line = data.split(b"\n")[0]
            try:
                request_obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                request_obj = {}

            # 在 dispatch 之前擋下未授權 peer——不進
            # handle_jsonrpc_request，不碰任何 session/request/workspace 邏輯。
            guard_error = _daemon_guard_error(
                request_obj.get("id") if isinstance(request_obj, dict) else None,
                peer_uid,
            )
            if guard_error is not None:
                conn.sendall((json.dumps(guard_error) + "\n").encode("utf-8"))
                return

            response = handle_jsonrpc_request(
                request_obj,
                tool_signatures_path=tool_signatures_path,
                lineage_rules_path=lineage_rules_path,
                ledger_path=ledger_path,
                peer_uid=peer_uid,
            )
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
    except Exception as exc:
        _log.error("TERM_JSONRPC_CONNECTION_FAILED: %s", exc)
    finally:
        with _active_request_lock:
            _active_request_threads.discard(curr_t)


def serve_forever(
    socket_path: str,
    tool_signatures_path: str | Path | None = None,
    lineage_rules_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    runtime_provider: LlamaCppCompletionProvider | None = None,
    playbook_path: str | None = None,
    ecc_store_path: str | None = None,
    _ready_event: threading.Event | None = None,
    _stop_event: threading.Event | None = None,
    # main.py bootstrap 注入與 IPCServer 共用的同一個
    # CanonicalSessionContextRegistry instance。
    session_registry: CanonicalSessionContextRegistry | None = None,
    # main.py bootstrap 注入與 IPCServer 共用的同一個
    # daemon EUID 錨點。
    daemon_euid: int | None = None,
) -> None:
    """
    持續監聽 socket_path，接受並處理 JSON-RPC 2.0 請求。

    - 每次收到連線，呼叫 handle_jsonrpc_request() 處理後繼續等待下一條
    - 單次請求若拋出 exception，記錄 error log 後繼續監聽
    - _stop_event 被設定時，結束監聽並清理 socket
    - KeyboardInterrupt / SystemExit：記錄 log 後清理 socket 並終止
    - socket 已存在時 fail-closed，保留 endpoint 供 operator 驗證與隔離

    _ready_event：bind 完成後 set，供測試同步。
    _stop_event：設定後，下一次 accept() 超時時離開，供測試控制停止。
    """
    global _runtime_provider, _session_registry, _daemon_euid  # noqa: PLW0603
    if runtime_provider is not None:
        _runtime_provider = runtime_provider
    if session_registry is not None:
        _session_registry = session_registry
        _attach_run_binding_revocation(session_registry)
    if daemon_euid is not None:
        _daemon_euid = daemon_euid

    path = Path(socket_path)
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    global _server_sock
    _server_sock = server_sock
    identity: BoundUnixSocket | None = None
    try:
        try:
            identity = bind_owned_unix_socket(server_sock, path)
        except OSError as bind_exc:
            # 以前這裡只記一行 `TERM_SOCKET_BIND_FAILED` 就 return，
            # 把最有用的區分丟掉了——`bind_owned_unix_socket()` 對「路徑已存在」
            # 丟的是 `EADDRINUSE / TERM_SOCKET_CONFLICT`，那是完全不同、而且
            # operator 可以處理的情況。更糟的是這個 return 會跳過 `_init_ecc()`，
            # 於是 Agent 與 proactive intelligence 都不會啟動，但 daemon 的另一個
            # listener 仍然起得來，外觀上像是正常運作。
            #
            # 兩個既有性質**都不改**：
            #   1. 既存 endpoint 一律保留，不在 startup 猜測 stale socket、
            #      也不自動 unlink（見 `socket_lifecycle` 的 docstring——
            #      自動 unlink 可能劫持另一個合法 process 的 endpoint）。
            #   2. **絕不把 endpoint path 寫進 log**；第一行維持精確的
            #      `TERM_SOCKET_BIND_FAILED`。path 可能洩漏部署結構，
            #      而 operator 本來就知道自己設定的 endpoint 在哪。
            _log.error("TERM_SOCKET_BIND_FAILED")
            _log.error("TERM_SOCKET_BIND_FAILED_DETAIL code=%s errno=%s",
                       str(bind_exc), bind_exc.errno)
            _log.error("TERM_LINEAGE_LISTENER_NOT_STARTED: Agent 與 proactive "
                       "intelligence 不會啟動（_init_ecc 已跳過）")
            if bind_exc.errno == errno.EADDRINUSE:
                _log.error(
                    "TERM_SOCKET_CONFLICT_HINT: 設定的 lineage endpoint 已存在。"
                    "請先確認沒有 daemon 正在使用它——connect 得上代表有人在聽，"
                    "**不可移除**；確認是遺留檔之後再手動移除或改名，"
                    "本程序不會自行判定或刪除。")
            return

        # 只有 endpoint 已確認由本 process 建立後才初始化 runtime state。
        # B1：serve_forever 實際 bind 的 identity.path 是唯一權威
        # daemon socket 來源，交給 OrchestrationService 作 child 的 exact endpoint。
        _init_ecc(
            playbook_path=playbook_path,
            ecc_store_path=ecc_store_path,
            parent_daemon_socket=str(identity.path),
        )

        server_sock.listen(5)
        server_sock.settimeout(1.0)  # 每 1s 輪詢 _stop_event

        _log.info("TERM_LINEAGE_LISTENING")
        if _ready_event is not None:
            _ready_event.set()

        try:
            while True:
                if _stop_event is not None and _stop_event.is_set():
                    _log.info("TERM_LINEAGE_STOP_EVENT")
                    break

                try:
                    conn, _ = server_sock.accept()
                except socket.timeout:
                    continue  # 輪詢 _stop_event
                except OSError:
                    _log.error("TERM_LINEAGE_ACCEPT_FAILED")
                    break

                # 每個連線在獨立 thread 處理，accept loop 立刻回去等下一個
                threading.Thread(
                    target=_handle_connection,
                    args=(conn,),
                    kwargs={
                        "tool_signatures_path": tool_signatures_path,
                        "lineage_rules_path": lineage_rules_path,
                        "ledger_path": ledger_path,
                    },
                    daemon=True,
                ).start()

        except (KeyboardInterrupt, SystemExit):
            _log.info("TERM_LINEAGE_SHUTTING_DOWN")
    finally:
        try:
            server_sock.close()
        except Exception:
            pass
        if _server_sock is server_sock:
            _server_sock = None
        # 初始化中途失敗也必須先收束 EventStore，才能移除本次 endpoint。
        if identity is not None:
            _reset_ecc_state()
            unlink_owned_unix_socket(identity)
