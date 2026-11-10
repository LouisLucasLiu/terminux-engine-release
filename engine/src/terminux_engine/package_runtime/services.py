# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""RuntimeServices facade / stubs。"""

from __future__ import annotations

from contextvars import ContextVar

import json
import logging
import time
import uuid
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - 僅供型別註記
    from terminux_engine.llm.generation import CompletionMetadata

from terminux_engine.isvn.coordinate import DesignCoordinate
from terminux_engine.package_runtime.result import (
    NotificationRequest,
    PhaseResult,
    RecordRequest,
    ReportRequest,
)


class StoreService:
    """Record commit facade。

    若提供 EventStore，會把 RecordRequest 搬運成 SessionEvent；否則只保留在記憶體。
    """

    def __init__(
        self,
        event_store: Any | None = None,
        *,
        session_id: str = "",
        terminal_id: str = "default",
        user_id: str = "",
        project_id: str | None = None,
        package_id: str | None = None,
    ) -> None:
        self.event_store = event_store
        self.session_id = session_id
        self.terminal_id = terminal_id
        self.user_id = user_id
        self.project_id = project_id
        self.package_id = package_id
        self.records: list[RecordRequest] = []

    def commit(self, record: RecordRequest) -> str:
        """提交 RecordRequest，回傳 record key。"""
        self.records.append(record)
        if self.event_store is None:
            return record.key

        from terminux_engine.store.event_store import SessionEvent

        event = SessionEvent(
            event_id=uuid.uuid4().hex,
            session_id=self.session_id,
            terminal_id=self.terminal_id,
            user_id=self.user_id,
            project_id=self.project_id,
            package_id=self.package_id,
            timestamp=_timestamp_to_float(record.timestamp),
            event_type=_event_type_for_record(record),
            payload=dict(record.payload),
        )
        self.event_store.emit(event)
        return record.key

    def get_latest(
        self,
        n: int = 1,
        *,
        event_type: str | None = None,
        tool_name: str | None = None,
        after: float | None = None,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        """回傳本 package 最新 n 筆已持久化 record，最新在前。"""
        if self.package_id is None:
            raise ValueError("package_id is required for StoreService reads")
        if self.event_store is None:
            return []
        events = self.event_store.query(
            package_id=self.package_id,
            event_type=event_type,
            tool_name=tool_name,
            after=after,
            before=before,
            limit=n,
            order="desc",
        )
        return [event.model_dump() for event in events]


class NotifyService:
    """Notification emit facade。

    預設只把 notification 收集進 self.notifications（向後相容）。
    若 Core 注入 async_sink，emit 時額外把可顯示文字送上便車（例如 lineage
    server 的 _pending_async_notifications → agent_async → C++ NotificationBar）。
    NotifyService 不需知道 sink 底下接什麼（低耦合：不 import Core 側模組）。

    另加一個選填的 `sink`，它收到的是**完整的
    `NotificationRequest` 物件**。為什麼不重用 `async_sink`：`async_sink`
    的既有 payload 契約在沒有 actions 時只送 `notification.message`
    字串（對應 C++ NotificationBar 的既有期待），level 與 title 會被丟掉；
    改變它會影響既有 daemon 行為。因此新增這個 additive、預設 `None`
    的注入點——未注入時行為與修改前 byte-for-byte 相同。兩個 sink 各自
    獨立、各自 warn-only 保護，互不影響。
    """

    def __init__(
        self,
        async_sink: Callable[[Any], None] | None = None,
        *,
        sink: Callable[[NotificationRequest], None] | None = None,
    ) -> None:
        self.notifications: list[NotificationRequest] = []
        self._async_sink = async_sink
        self._sink = sink

    def emit(self, notification: NotificationRequest) -> None:
        """搬運 package 指定的 notification。"""
        self.notifications.append(notification)
        if self._sink is not None:
            try:
                self._sink(notification)
            except Exception as exc:  # warn-only：與 async_sink 同一政策
                _log.warning("NotifyService sink failed: %s", exc)
        if self._async_sink is not None:
            try:
                payload: str | dict[str, Any]
                if notification.actions:
                    payload = {
                        "type": "notification",
                        "notification_id": f"ntf_{uuid.uuid4().hex}",
                        "level": notification.level,
                        "title": notification.title,
                        "message": notification.message,
                        "actions": notification.actions,
                    }
                else:
                    payload = notification.message
                self._async_sink(payload)
            except Exception as exc:  # warn-only：投遞失敗不得中斷 package 流程
                _log.warning("NotifyService async_sink failed: %s", exc)


def package_origin_tag(package_id: str) -> str:
    """User package notification 的固定來源標記。"""
    return f"[Package {package_id}]"


class OriginTaggedNotifyService(NotifyService):
    """在 service 邊界為 notification 加上來源標記，再交給原本的 NotifyService。

    Package 作者不需要自行加前綴；無論 package 是回傳 `PhaseResult.notifications`
    還是直接呼叫 `ctx.services.notify.emit()`，都經過同一個 `emit()`。訊息已經以
    相同標記開頭時不重複加。`notifications` 與 sink 沿用 inner 的物件，收集與
    投遞行為與未包裝時相同。

    User package 的 notification 只送文字：`actions` 一律移除，訊息照常送出，
    並記一筆 `TERM_USER_PACKAGE_NOTIFICATION_ACTIONS_OMITTED`。內建 package
    不經過這個 service，行為不變。
    """

    def __init__(self, inner: NotifyService, *, tag: str) -> None:
        self._inner = inner
        self._tag = tag
        self.notifications = inner.notifications
        self._async_sink = inner._async_sink
        self._sink = inner._sink

    def emit(self, notification: NotificationRequest) -> None:
        """加上來源標記後轉交 inner service。"""
        message = notification.message if isinstance(notification.message, str) else str(notification.message)
        if not message.startswith(self._tag):
            message = f"{self._tag} {message}" if message else self._tag
        title = notification.title if isinstance(notification.title, str) else str(notification.title)
        if not title.startswith(self._tag):
            # title 會交給完整物件的 sink；同樣帶標記，避免 user package 冒用內建訊息標題。
            title = f"{self._tag} {title}" if title else self._tag
        actions = notification.actions
        if actions:
            count = len(actions) if isinstance(actions, (list, tuple)) else 1
            _log.warning("TERM_USER_PACKAGE_NOTIFICATION_ACTIONS_OMITTED origin=%s count=%d",
                         self._tag, count)
        tagged = NotificationRequest(
            level=notification.level,
            title=title,
            message=message,
        )
        self._inner.emit(tagged)


_REPORT_SERVICE_DEPRECATION_MESSAGE = (
    "ReportService and ReportRequest are deprecated; packages should "
    "persist durable metadata through RecordRequest and manage artifact "
    "files explicitly."
)


class ReportService:
    """Report render/register facade。

    Deprecated：`render()` 從未
    持久化任何資料，只把 request append 進 process 記憶體 list。維持此
    class 只是為了相容既有呼叫者（`DispatchEngine._apply_reports()`／
    Local Test Harness），新 package 不應使用它。
    """

    def __init__(self) -> None:
        self.reports: list[ReportRequest] = []

    def render(self, request: ReportRequest) -> str:
        """登記 report request，回傳 output name 或 report id。

        Deprecated：呼叫本方法會發出 `DeprecationWarning`。行為（append
        進記憶體、回傳值）維持不變，僅新增警告，不新增持久化。
        """
        warnings.warn(_REPORT_SERVICE_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
        self.reports.append(request)
        return request.report_path or request.output_name or request.report_id


class FileService:
    """Minimal file service。"""

    def exists(self, path: str) -> bool:
        """檢查檔案是否存在。"""
        return Path(path).exists()

    def read_text(self, path: str, max_bytes: int = 1_000_000) -> str:
        """讀取文字檔，最多 max_bytes。"""
        data = Path(path).read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class LlmAskResult:
    """`LlmService.ask_detailed()` 的回傳。

    `metadata` 是 vendor-neutral 的 `CompletionMetadata`（可能為 None——
    provider 不提供時就是不提供，不猜）。`error` 只在呼叫失敗時有值；
    失敗**不會**被靜默轉成空字串。
    """

    text: str | None
    metadata: "CompletionMetadata | None"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None and self.error is None


class LlmService:
    """Package prompt phase 使用的 LLM facade。"""

    def __init__(self, router: Any | None = None, *, budget: Any | None = None) -> None:
        self._router = router
        self._provider_called: ContextVar[bool] = ContextVar("package_provider_called", default=False)
        #: 只有 analysis task body 的呼叫會拿到 budget；既有非 task
        #: 呼叫傳 None，行為完全不變（不改全平台節流）。
        self._budget = budget

    def ask(self, prompt: str, *, timeout: float | None = 30.0,
            max_output_tokens: int | None = None) -> str | None:
        """以 LLMRouter background slot 取得單次文字回覆。

        Router 未注入、未配置或 provider 失敗時回傳 None，由 prompt runtime
        統一降級為 noop，避免影響 post-exec 主路徑。

        `max_output_tokens` 為 per-request output budget；不指定就沿用
        provider config 預設，既有呼叫端行為不變。
        """
        return self.ask_detailed(
            prompt, timeout=timeout, max_output_tokens=max_output_tokens).text

    def ask_detailed(self, prompt: str, *, timeout: float | None = 30.0,
                     max_output_tokens: int | None = None) -> "LlmAskResult":
        """`ask()` 的 additive 版本：文字之外一併回傳 completion metadata。

        既有 `ask()` 的簽名與回傳型別完全不變；metadata 走這條獨立的
        diagnostic channel，**不會**被拼進模型文字，也不會把 provider
        專屬物件洩漏給 Package（Part C）。
        """
        self._provider_called.set(False)
        # 唯一的扣額點。`ask()` 委派到這裡，因此 wrapper 互調不會
        # 重複扣額。沒有額度（或 task 已停止）就**不呼叫 router**。
        if self._budget is not None:
            allowed, reason = self._budget.try_consume()
            if not allowed:
                _log.info("task body LLM dispatch refused: %s", reason)
                return LlmAskResult(None, None, reason)
        if self._router is None:
            _log.warning("Package LLM router is unavailable")
            return LlmAskResult(None, None, "router_unavailable")
        try:
            from terminux_engine.llm.llm_router import LLMRouter, TaskType

            # 每個呼叫 context 獨立記錄，避免並行呼叫互相污染計次資訊。
            self._provider_called.set(
                not isinstance(self._router, LLMRouter)
                or self._router.get_provider(TaskType.BACKGROUND) is not None
            )

            # 明確型別判斷，不用 duck typing：`Mock()` 會自動長出任何屬性，
            # 用 getattr 偵測會把既有測試替身誤判成支援 metadata channel。
            # 非 LLMRouter 的注入物件走原本的 `generate()`，metadata 誠實
            # 標為 None——這是介面相容，不是 provider／model 的 silent fallback。
            if isinstance(self._router, LLMRouter):
                result = self._router.generate_with_metadata(
                    prompt, task_type=TaskType.BACKGROUND, timeout=timeout,
                    max_output_tokens=max_output_tokens,
                )
                raw, metadata = result.text, result.metadata
            else:
                raw = self._router.generate(
                    prompt, task_type=TaskType.BACKGROUND, timeout=timeout,
                )
                metadata = None
        except Exception as exc:
            _log.warning("Package LLM call failed: %s", exc)
            return LlmAskResult(None, None, f"{type(exc).__name__}: {exc}")
        text = raw if isinstance(raw, str) and raw.strip() else None
        return LlmAskResult(text, metadata, None)


@dataclass
class RuntimeHelpers:
    """Package Runtime 可使用的輔助工具。"""

    _resolve_fn: Callable | None = None

    def resolve_coordinate(
        self,
        filepath: str,
        project: str,
        timeout: float = 10.0,
    ) -> DesignCoordinate:
        """回傳 filepath 對應的 isvn 座標。"""
        if self._resolve_fn is None:
            return DesignCoordinate(
                phase=None,
                block=None,
                stage=None,
                flow=None,
                version=None,
                canonical=None,
                resolved_by="unresolved",
            )
        return self._resolve_fn(filepath, project, timeout)


@dataclass
class ResultFactory:
    """建立常用 PhaseResult 的 helper。"""

    def noop(self, message: str | None = None) -> PhaseResult:
        """建立 noop result。"""
        return PhaseResult(status="noop", message=message)

    def ok(self, message: str | None = None) -> PhaseResult:
        """建立 ok result。"""
        return PhaseResult(status="ok", message=message)

    def warn(self, message: str | None = None) -> PhaseResult:
        """建立 warn result。"""
        return PhaseResult(status="warn", message=message)

    def fail(self, message: str | None = None) -> PhaseResult:
        """建立 fail result。"""
        return PhaseResult(status="fail", message=message)


@dataclass
class RuntimeServices:
    """Runtime services facade。"""

    store: StoreService = field(default_factory=StoreService)
    notify: NotifyService = field(default_factory=NotifyService)
    report: ReportService = field(default_factory=ReportService)
    """Deprecated（Stage 1）：見 ReportService docstring。"""
    file: FileService = field(default_factory=FileService)
    llm: LlmService = field(default_factory=LlmService)

    # ── flow task 專用的 host 注入點 ──────────────────────
    #
    # 三者預設 None：一般 package dispatch 與既有 action 完全不受影響。
    # 只有 host 在派送**已核准的 flow task step** 時才會填入，因此
    # `prepare_variant`／`run_flow` 就算被別的路徑呼叫也拿不到能力。
    #
    # `flow_task_binding` 是 host-built 物件（不是序列化 dict），
    # `flow_execution_service` 是 Kernel 的正式 facade，
    # `flow_variant_writer` 是 Package 的受控寫入 helper。
    flow_task_binding: Any = None
    flow_execution_service: Any = None
    flow_variant_writer: Any = None
    #: 已核准 ECO hook 設定的受控寫入 helper。
    flow_repair_writer: Any = None
    # host 端的正式 scoped run input evidence reader，以及取得
    # canonical request context 的 callable。兩者都由 daemon composition
    # 注入；未注入時 Package 拿到 `None`，行為與 flow task 完全相同。
    run_input_evidence_reader: Any = None
    request_context: Any = None
    #: host 端的唯讀 Evidence Resolver（`evidence_integrity.evidence_resolver`）。
    #: 未注入時 Package 拿到 `None`，執行結果一律回報 `EXECUTION_UNVERIFIED`。
    execution_evidence_resolver: Any = None
    #: 把 Viewer 啟動參數交回 RPC client 的 host callback。
    dqe_viewer_launcher: Any = None


def _event_type_for_record(record: RecordRequest) -> str:
    """把 RecordRequest 映射到既有 EventStore event_type。"""
    record_type = record.record_type.strip()
    namespace = record.namespace.strip()
    return record_type or namespace or "package_record"


def _timestamp_to_float(timestamp: str | None) -> float:
    if timestamp is None:
        return time.time()
    try:
        return float(timestamp)
    except ValueError:
        return time.time()


def payload_json(record: RecordRequest) -> str:
    """測試/除錯用：序列化 payload，不解讀內容。"""
    return json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
