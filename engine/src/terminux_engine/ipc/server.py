# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import asyncio
import os
import json
import logging
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Optional

from terminux_engine.core.ecc_manager import EccManager
from terminux_engine.core.event_recorder import EventRecorder
from terminux_engine.core.provenance import ProvenanceEngine
from terminux_engine.ecc.models import (
    CommandType,
    EventQuery,
    ProvenanceMismatch,
    ProvenanceSource,
    ProvenanceSpec,
    QueryOrder,
    ShellEvent,
)
from terminux_engine.ipc.peer_identity import (
    capture_daemon_euid,
    is_peer_authorized_for_daemon,
    resolve_account_name,
    resolve_peer_uid_from_asyncio_writer,
)
from terminux_engine.ipc.session_registry import (
    CanonicalSessionContextRegistry,
    InvalidProjectContextError,
    MemberAssertionMismatchError,
    PeerIdentityUnavailableError,
)
from terminux_engine.ipc.socket_lifecycle import (
    BoundUnixSocket,
    bind_owned_unix_socket,
    unlink_owned_unix_socket,
)

logger = logging.getLogger(__name__)


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class PendingCommand:
    """
    C++ 端 command.submit 後、command.complete 前的暫存狀態。
    每個 session 同一時間至多一個 pending_command。
    """
    command:          str
    work_directory:   str
    submit_timestamp: datetime
    command_type:     str          # 對應 CommandType 的字串值，由 C++ 提供
    targets:          list[str]
    env_snapshot:     dict
    stdout_chunks:    list[str] = field(default_factory=list)
    stderr_chunks:    list[str] = field(default_factory=list)

    MAX_STREAM_BYTES: ClassVar[int] = 4096   # 每個 stream 的累積上限（class variable）

    def get_stdout_summary(self) -> str:
        """
        合併 stdout_chunks，若總長度超過 MAX_STREAM_BYTES，
        保留前 MAX_STREAM_BYTES 個字元並附加 "...[TRUNCATED]"。
        """
        return self._summarize(self.stdout_chunks)

    def get_stderr_summary(self) -> str:
        """同 get_stdout_summary，對象為 stderr_chunks。"""
        return self._summarize(self.stderr_chunks)

    def _summarize(self, chunks: list[str]) -> str:
        text = "".join(chunks)
        if len(text) > self.MAX_STREAM_BYTES:
            return text[:self.MAX_STREAM_BYTES] + "...[TRUNCATED]"
        return text


@dataclass
class SessionState:
    session_id:      str
    member_id:       str
    project_id:      str
    shell:           str
    work_directory:  str
    created_at:      datetime
    pending_command: Optional[PendingCommand] = None
    #: project_id 的來源（session／unset）；只供顯示與診斷。
    project_source:  str = "session"
    writer:          Any = field(repr=False, default=None)
    # writer 在 connection handler 建立後立即設定，不通過建構子傳入
    # 建立此 session 時的 peer UID，revoke registry entry 時需要
    # 同一個 peer UID 才能撤銷（見 CanonicalSessionContextRegistry.revoke）。
    peer_uid:        Optional[int] = field(default=None)


@dataclass
class JsonRpcRequest:
    jsonrpc: str        # 必須為 "2.0"
    method:  str
    params:  dict
    id:      int | str | None   # None 表示 notification（無需回應）


@dataclass
class JsonRpcResponse:
    jsonrpc: str = "2.0"
    result:  dict | None = None
    error:   dict | None = None   # {"code": int, "message": str}
    id:      int | str | None = None


class RpcError:
    PARSE_ERROR      = -32700   # JSON 解析失敗
    INVALID_REQUEST  = -32600   # 非合法 JSON-RPC 格式
    METHOD_NOT_FOUND = -32601   # 未知 method
    INVALID_PARAMS   = -32602   # 缺少必填欄位或型別錯誤
    INTERNAL_ERROR   = -32603   # 處理過程拋出非預期 exception

    # Custom
    SESSION_NOT_INIT = -32000   # 尚未呼叫 session.init 即送其他請求
    SESSION_CONFLICT = -32001   # 同一連線重複呼叫 session.init
    NO_PENDING_CMD   = -32002   # command.output / command.complete 無對應 pending command
    PEER_UID_NOT_AUTHORIZED = -32003   # peer UID != daemon startup EUID


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_timestamp(value: str, field_name: str) -> datetime:
    """Parse ISO 8601 UTC timestamp string. Raises ValueError on failure."""
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid ISO 8601 timestamp for field: {field_name}")


# ── IPCServer ─────────────────────────────────────────────────────────────────

class IPCServer:

    def __init__(
        self,
        socket_path:        str,
        event_recorder:     Optional[EventRecorder] = None,
        ecc_manager:        Optional[EccManager] = None,
        provenance_manager: Optional[ProvenanceEngine] = None,
        layer1_callback:    Optional[Callable[[ShellEvent], Awaitable[None]]] = None,
        chat_handler:       Optional[Callable[[str, str, datetime], Awaitable[tuple[str, list[str]]]]] = None,
        max_message_bytes:  int = 1_048_576,
        session_registry:   Optional[CanonicalSessionContextRegistry] = None,
        daemon_euid:        Optional[int] = None,
    ) -> None:
        self._socket_path        = socket_path
        self._event_recorder     = event_recorder
        self._ecc_manager        = ecc_manager
        self._provenance_manager = provenance_manager
        self._layer1_callback    = layer1_callback
        self._chat_handler       = chat_handler
        self._max_message_bytes  = max_message_bytes
        self._sessions:    dict[str, SessionState] = {}   # session_id → SessionState
        self._connections: dict[Any, str] = {}            # writer → session_id
        # 所有已接受、尚未結束的連線（含未 session.init 者）；stop 時需主動關閉。
        self._active_writers: set[Any] = set()
        self._server = None
        self._socket_identity: BoundUnixSocket | None = None
        # canonical session registry。未注入時本 instance 建立私有
        # registry（例如獨立測試/嵌入情境）；main.py production bootstrap 必須
        # 注入與 lineage JSON-RPC server 共用的同一份 instance（process 內
        # 只能有一份 canonical registry）。
        self._session_registry: CanonicalSessionContextRegistry = (
            session_registry if session_registry is not None
            else CanonicalSessionContextRegistry()
        )
        # per-member daemon guard 的錨點。未注入時（獨立測試／
        # 嵌入情境）用本 process 目前的 EUID；main.py production bootstrap
        # 必須傳入與 lineage listener 相同的、只在啟動時算一次的值。
        self._daemon_euid: int = (
            daemon_euid if daemon_euid is not None else capture_daemon_euid()
        )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def session_registry(self) -> CanonicalSessionContextRegistry:
        """本 instance 使用的 canonical session registry（唯讀存取）。

        production bootstrap 應把同一個 instance 注入 lineage JSON-RPC
        server，讓兩個 listener 共用同一份 canonical session authority
        。
        """
        return self._session_registry

    async def start(self) -> None:
        """啟動 asyncio UDS server，開始接受連線，不返回（持續運行）。"""
        raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity: BoundUnixSocket | None = None
        try:
            identity = bind_owned_unix_socket(raw_socket, self._socket_path)
            # socket 檔只允許本使用者存取（connect 需要 write bit）。
            # identity 剛驗過是本 process 建立、owner 為本 uid 的 socket。
            os.chmod(identity.path, 0o600)
            raw_socket.setblocking(False)
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                sock=raw_socket,
            )
        except Exception:
            raw_socket.close()
            if identity is not None:
                unlink_owned_unix_socket(identity)
            raise

        self._socket_identity = identity
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """停止接受新連線，關閉 server，再安全移除本 process 建立的 socket。"""
        if self._server is not None:
            self._server.close()
            # Python 3.12 的 wait_closed() 會等所有已接受連線結束；client 保持
            # 閒置連線時 daemon 會卡在這裡、socket 也不會被移除。收束時由 server
            # 主動關閉連線，handler 讀到 EOF 後照常清理 session。
            for writer in list(self._active_writers):
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - 已斷線的 transport 不影響收束
                    pass
            await self._server.wait_closed()
            self._server = None
        if self._socket_identity is not None:
            unlink_owned_unix_socket(self._socket_identity)
            self._socket_identity = None

    async def send_notification(self, session_id: str, notification: dict) -> bool:
        """
        對指定 session_id 的連線推送 JSON-RPC notification。
        成功推送：True；session_id 不存在或連線已斷：False。
        不拋出 exception。
        """
        state = self._sessions.get(session_id)
        if state is None or state.writer is None:
            logger.warning("send_notification: session %s not found", session_id)
            return False
        try:
            data = json.dumps(notification).encode() + b'\n'
            state.writer.write(data)
            await state.writer.drain()
            return True
        except Exception as e:
            logger.warning(
                "send_notification: write failed for session %s: %s", session_id, e
            )
            return False

    # ── Connection Handler ────────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: Any,
    ) -> None:
        buffer = bytearray()
        self._active_writers.add(writer)
        try:
            while True:
                try:
                    chunk = await reader.read(65536)
                except ConnectionResetError as e:
                    logger.info("connection reset during read: %s", e)
                    break

                if not chunk:
                    break  # EOF

                buffer.extend(chunk)

                # Process all complete messages (newline-delimited)
                while b'\n' in buffer:
                    idx = buffer.index(b'\n')
                    message_bytes = bytes(buffer[:idx])
                    buffer = buffer[idx + 1:]

                    if len(message_bytes) > self._max_message_bytes:
                        resp = self._make_error(None, RpcError.PARSE_ERROR, "message too large")
                        await self._write_response(writer, resp)
                        writer.close()
                        return

                    await self._process_message(message_bytes, writer)

                # Buffer accumulated without newline exceeds limit
                if len(buffer) > self._max_message_bytes:
                    resp = self._make_error(None, RpcError.PARSE_ERROR, "message too large")
                    await self._write_response(writer, resp)
                    writer.close()
                    return

        except (ConnectionResetError, BrokenPipeError) as e:
            logger.info("connection abruptly closed: %s", e)
        finally:
            self._active_writers.discard(writer)
            sid = self._connections.pop(writer, None)
            if sid and sid in self._sessions:
                logger.info("session %s cleaned up on disconnect", sid)
                state = self._sessions.pop(sid)
                if state.peer_uid is not None:
                    self._session_registry.revoke(session_id=sid, peer_uid=state.peer_uid)
            try:
                writer.close()
            except Exception:
                pass

    async def _process_message(self, message_bytes: bytes, writer: Any) -> None:
        # Parse JSON
        try:
            data = json.loads(message_bytes)
        except json.JSONDecodeError as e:
            resp = self._make_error(None, RpcError.PARSE_ERROR, f"JSON parse error: {e}")
            await self._write_response(writer, resp)
            return

        # Validate JSON-RPC 2.0 format
        if (
            not isinstance(data, dict)
            or data.get("jsonrpc") != "2.0"
            or not isinstance(data.get("method"), str)
        ):
            req_id = data.get("id") if isinstance(data, dict) else None
            resp = self._make_error(req_id, RpcError.INVALID_REQUEST, "invalid JSON-RPC request")
            await self._write_response(writer, resp)
            return

        # Notification (no "id" field) → silently ignore
        if "id" not in data:
            return

        request = JsonRpcRequest(
            jsonrpc=data["jsonrpc"],
            method=data["method"],
            params=data.get("params", {}),
            id=data["id"],
        )

        response = await self._dispatch(request, writer)
        await self._write_response(writer, response)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(
        self,
        request: JsonRpcRequest,
        writer: Any,
    ) -> JsonRpcResponse:
        method = request.method
        params = request.params if isinstance(request.params, dict) else {}
        req_id = request.id

        session_id = self._connections.get(writer)
        session    = self._sessions.get(session_id) if session_id else None

        # All methods except session.init require an established session
        if method != "session.init" and session is None:
            return self._make_error(req_id, RpcError.SESSION_NOT_INIT, "session not initialized")

        try:
            if method == "session.init":
                return await self._handle_session_init(params, req_id, writer)
            elif method == "session.end":
                return await self._handle_session_end(params, req_id, session)
            elif method == "command.submit":
                return await self._handle_command_submit(params, req_id, session)
            elif method == "command.output":
                return await self._handle_command_output(params, req_id, session)
            elif method == "command.complete":
                return await self._handle_command_complete(params, req_id, session)
            elif method == "chat.query":
                return await self._handle_chat_query(params, req_id, session)
            elif method == "ecc.query":
                return await self._handle_ecc_query(params, req_id, session)
            elif method == "provenance.write_spec":
                return await self._handle_provenance_write_spec(params, req_id, session)
            elif method == "provenance.diff":
                return await self._handle_provenance_diff(params, req_id, session)
            else:
                return self._make_error(req_id, RpcError.METHOD_NOT_FOUND, f"method not found: {method}")
        except Exception as e:
            logger.exception("internal error handling method %s: %s", method, e)
            return self._make_error(req_id, RpcError.INTERNAL_ERROR, "internal server error")

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_session_init(
        self,
        params: dict,
        req_id: int | str | None,
        writer: Any,
    ) -> JsonRpcResponse:
        if writer in self._connections:
            return self._make_error(req_id, RpcError.SESSION_CONFLICT, "session already initialized")

        for f in ("member_id", "shell", "work_directory"):
            if f not in params or not isinstance(params[f], str):
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")
        # `project_id` 省略或 null ⇒ 未宣告；給了就必須是
        # 非空字串。來源（session／unset）**只由 daemon 決定**，client 傳來的
        # `project_source` 一律忽略（不能自稱）。
        declared_project = params.get("project_id")
        if declared_project is not None and (
                not isinstance(declared_project, str) or not declared_project):
            return self._make_error(req_id, RpcError.INVALID_PARAMS,
                                    "project_id must be a non-empty string or omitted")

        # member_id 由 Linux UDS peer credential（SO_PEERCRED）
        # 驗證，既有 C++ 傳入值只作 compatibility assertion；mismatch／peer
        # 不可得一律 fail-closed，不 fallback 到 client 自稱值。
        peer_uid = resolve_peer_uid_from_asyncio_writer(writer)
        if peer_uid is None:
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS, "peer identity unavailable"
            )
        # per-member daemon guard——這個 daemon process 只接受
        # 與它啟動時 EUID 相同的 peer；不同 UID 在建立 session（因此也在任何
        # 後續 request／workspace 操作）之前就 fail-closed。刻意放在 member assertion 檢查之前：這是比
        # per-request member 比對更粗、更早的邊界。
        if not is_peer_authorized_for_daemon(peer_uid, self._daemon_euid):
            return self._make_error(
                req_id, RpcError.PEER_UID_NOT_AUTHORIZED,
                "peer uid not authorized for this daemon",
            )
        canonical_member = resolve_account_name(peer_uid)
        if canonical_member is None:
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS, "peer identity unavailable"
            )
        if params["member_id"] != canonical_member:
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS, "member assertion mismatch"
            )

        try:
            context = self._session_registry.create(
                peer_uid=peer_uid,
                member_id=canonical_member,
                project_id=declared_project,
            )
        except (MemberAssertionMismatchError, InvalidProjectContextError, PeerIdentityUnavailableError) as exc:
            return self._make_error(req_id, RpcError.INVALID_PARAMS, str(exc))

        session_id = context.session_id
        state = SessionState(
            session_id=session_id,
            member_id=canonical_member,
            project_id=context.project_id,
            project_source=context.project_source,
            shell=params["shell"],
            work_directory=params["work_directory"],
            created_at=datetime.now(timezone.utc),
            writer=writer,
            peer_uid=peer_uid,
        )
        self._sessions[session_id] = state
        self._connections[writer] = session_id

        return JsonRpcResponse(id=req_id, result={
            "session_id": session_id, "status": "ok",
            # 回報 daemon 決定的有效 project 與來源，讓 client 能顯示
            # 「session 未宣告 project（將採用 Profile 的 project）」。
            "project_id": context.project_id,
            "project_source": context.project_source,
        })

    async def _handle_session_end(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        if params.get("session_id") != session.session_id:
            return self._make_error(req_id, RpcError.INVALID_PARAMS, "session_id mismatch")

        session.pending_command = None
        del self._sessions[session.session_id]
        if session.writer in self._connections:
            del self._connections[session.writer]
        if session.peer_uid is not None:
            self._session_registry.revoke(session_id=session.session_id, peer_uid=session.peer_uid)

        return JsonRpcResponse(id=req_id, result={"status": "ok"})

    async def _handle_command_submit(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        required = (
            "session_id", "command", "work_directory", "timestamp",
            "command_type", "targets", "env_snapshot",
        )
        for f in required:
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        valid_types = {ct.value for ct in CommandType}
        if params["command_type"] not in valid_types:
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS, f"invalid command_type: {params['command_type']}"
            )

        try:
            submit_ts = _parse_timestamp(params["timestamp"], "timestamp")
        except ValueError as e:
            return self._make_error(req_id, RpcError.INVALID_PARAMS, str(e))

        if not isinstance(params["targets"], list):
            return self._make_error(req_id, RpcError.INVALID_PARAMS, "targets must be a list")

        if not isinstance(params["env_snapshot"], dict):
            return self._make_error(req_id, RpcError.INVALID_PARAMS, "env_snapshot must be a dict")

        if session.pending_command is not None:
            logger.warning(
                "session %s: previous command %r not completed, discarding",
                session.session_id, session.pending_command.command,
            )

        session.pending_command = PendingCommand(
            command=params["command"],
            work_directory=params["work_directory"],
            submit_timestamp=submit_ts,
            command_type=params["command_type"],
            targets=list(params["targets"]),
            env_snapshot=dict(params["env_snapshot"]),
        )
        return JsonRpcResponse(id=req_id, result={"status": "ok"})

    async def _handle_command_output(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        for f in ("session_id", "stream", "content", "timestamp"):
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        if params["stream"] not in ("stdout", "stderr"):
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS, "stream must be 'stdout' or 'stderr'"
            )

        if session.pending_command is None:
            return self._make_error(req_id, RpcError.NO_PENDING_CMD, "no pending command")

        if params["stream"] == "stdout":
            session.pending_command.stdout_chunks.append(params["content"])
        else:
            session.pending_command.stderr_chunks.append(params["content"])

        return JsonRpcResponse(id=req_id, result={"status": "ok"})

    async def _handle_command_complete(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        required = (
            "session_id", "exit_code", "duration_ms", "timestamp",
            "targets_confirmed", "artifacts_detected",
        )
        for f in required:
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        try:
            complete_ts = _parse_timestamp(params["timestamp"], "timestamp")
        except ValueError as e:
            return self._make_error(req_id, RpcError.INVALID_PARAMS, str(e))

        # Handle missing pending command
        if session.pending_command is None:
            logger.warning(
                "session %s: command.complete received without prior command.submit",
                session.session_id,
            )
            pending = PendingCommand(
                command="[no_submit]",
                work_directory=session.work_directory,
                submit_timestamp=complete_ts,
                command_type="other",
                targets=[],
                env_snapshot={},
            )
        else:
            pending = session.pending_command

        event_id = str(uuid.uuid4())
        stdout_summary = pending.get_stdout_summary() or None
        stderr_summary = pending.get_stderr_summary() or None

        event = ShellEvent(
            event_id=event_id,
            session_id=session.session_id,
            member_id=session.member_id,
            project_id=session.project_id,
            timestamp=complete_ts,
            work_directory=pending.work_directory,
            block=None,
            command=pending.command,
            command_type=CommandType(pending.command_type),
            targets=pending.targets,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            exit_code=params["exit_code"],
            duration_ms=params["duration_ms"],
            env_snapshot=pending.env_snapshot,
            extracted_data={},
            artifacts_detected=list(params.get("artifacts_detected", [])),
        )

        self._event_recorder.record(event)
        session.pending_command = None

        if self._layer1_callback is not None:
            async def _run_layer1(ev: ShellEvent) -> None:
                try:
                    await self._layer1_callback(ev)
                except Exception as exc:
                    logger.error("layer1_callback raised exception: %s", exc, exc_info=True)
            asyncio.create_task(_run_layer1(event))

        return JsonRpcResponse(id=req_id, result={"status": "ok", "event_id": event_id})

    async def _handle_chat_query(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        if self._chat_handler is None:
            return self._make_error(
                req_id, RpcError.METHOD_NOT_FOUND, "chat handler not available"
            )

        for f in ("session_id", "query", "timestamp"):
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        try:
            ts = _parse_timestamp(params["timestamp"], "timestamp")
        except ValueError as e:
            return self._make_error(req_id, RpcError.INVALID_PARAMS, str(e))

        answer, references = await self._chat_handler(session.session_id, params["query"], ts)
        return JsonRpcResponse(id=req_id, result={"answer": answer, "references": references})

    async def _handle_ecc_query(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        for f in ("session_id", "filters"):
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        filters = params["filters"]
        if not isinstance(filters, dict):
            return self._make_error(req_id, RpcError.INVALID_PARAMS, "filters must be a dict")

        query_kwargs: dict[str, Any] = {}

        for str_f in ("member_id", "project_id", "session_id", "block"):
            if str_f in filters:
                if not isinstance(filters[str_f], str):
                    return self._make_error(
                        req_id, RpcError.INVALID_PARAMS, f"filters.{str_f} must be a string"
                    )
                query_kwargs[str_f] = filters[str_f]

        if "command_type" in filters:
            if not isinstance(filters["command_type"], str):
                return self._make_error(
                    req_id, RpcError.INVALID_PARAMS, "filters.command_type must be a string"
                )
            try:
                query_kwargs["command_type"] = CommandType(filters["command_type"])
            except ValueError:
                return self._make_error(
                    req_id, RpcError.INVALID_PARAMS,
                    f"invalid command_type: {filters['command_type']}"
                )

        for ts_f, query_f in (("from_time", "after"), ("to_time", "before")):
            if ts_f in filters:
                try:
                    query_kwargs[query_f] = _parse_timestamp(filters[ts_f], ts_f)
                except ValueError as e:
                    return self._make_error(req_id, RpcError.INVALID_PARAMS, str(e))

        limit = filters.get("limit", 50)
        if not isinstance(limit, int) or not (1 <= limit <= 1000):
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS,
                "limit must be an integer between 1 and 1000"
            )
        query_kwargs["limit"] = limit

        query = EventQuery(order=QueryOrder.DESC, **query_kwargs)
        events = await asyncio.to_thread(self._ecc_manager.query_events, query)

        return JsonRpcResponse(id=req_id, result={
            "events": [self._event_to_dict(e) for e in events],
            "total": len(events),
        })

    async def _handle_provenance_write_spec(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        for f in ("session_id", "step", "member_id", "data", "source"):
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        valid_sources = {s.value for s in ProvenanceSource}
        if params["source"] not in valid_sources:
            return self._make_error(
                req_id, RpcError.INVALID_PARAMS, f"invalid source: {params['source']}"
            )

        source = ProvenanceSource(params["source"])
        await asyncio.to_thread(
            self._provenance_manager.write_spec,
            params["step"], params["member_id"], params["data"], source,
        )

        return JsonRpcResponse(id=req_id, result={"status": "ok"})

    async def _handle_provenance_diff(
        self,
        params: dict,
        req_id: int | str | None,
        session: SessionState,
    ) -> JsonRpcResponse:
        for f in ("session_id", "step", "member_id"):
            if f not in params:
                return self._make_error(req_id, RpcError.INVALID_PARAMS, f"missing required field: {f}")

        mismatches: list[ProvenanceMismatch] = await asyncio.to_thread(
            self._provenance_manager.diff,
            params["step"], params["member_id"],
        )

        return JsonRpcResponse(id=req_id, result={
            "mismatches": [
                {"key": m.key, "spec": m.spec, "observed": m.observed}
                for m in mismatches
            ],
            "has_mismatch": len(mismatches) > 0,
        })

    # ── Internal Helpers ──────────────────────────────────────────────────────

    def _make_error(
        self,
        req_id: int | str | None,
        code: int,
        message: str,
    ) -> JsonRpcResponse:
        return JsonRpcResponse(id=req_id, error={"code": code, "message": message})

    async def _write_response(self, writer: Any, response: JsonRpcResponse) -> None:
        d: dict = {"jsonrpc": response.jsonrpc, "id": response.id}
        if response.error is not None:
            d["error"] = response.error
        else:
            d["result"] = response.result
        try:
            writer.write(json.dumps(d).encode() + b'\n')
            await writer.drain()
        except Exception as e:
            logger.warning("failed to write response: %s", e)

    def _event_to_dict(self, event: ShellEvent) -> dict:
        return {
            "event_id":           event.event_id,
            "session_id":         event.session_id,
            "member_id":          event.member_id,
            "project_id":         event.project_id,
            "timestamp":          event.timestamp.isoformat(),
            "work_directory":     event.work_directory,
            "block":              event.block,
            "command":            event.command,
            "command_type":       event.command_type.value,
            "targets":            event.targets,
            "stdout_summary":     event.stdout_summary,
            "stderr_summary":     event.stderr_summary,
            "exit_code":          event.exit_code,
            "duration_ms":        event.duration_ms,
            "extracted_data":     event.extracted_data,
            "artifacts_detected": event.artifacts_detected,
        }
