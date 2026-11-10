# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Dreaming Layer 1 — Period Digest / Daily Digest 產生器。

同步 API 產生 period_digest 和 daily_digest events，寫入 event_store
（search_index 由 EventStore.emit() hook 自動處理）；另以 ThreadPoolExecutor
提供非同步的 submit_immediate_digest 與 close。
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from terminux_engine.llm.llm_router import LLMRouter
    from terminux_engine.store.event_store import EventStore, SessionEvent

logger = logging.getLogger(__name__)


# ── 常數 ──────────────────────────────────────────────────────────────────────

ACTIVITY_GAP_THRESHOLD_SEC: float = 3600.0  # 1 小時

AGGREGATED_TERMINAL_ID: str = "aggregated"
"""跨 terminal 聚合的 digest events 使用的哨兵 terminal_id。

用於 period_digest(FULL) 和 daily_digest。真實 terminal_id 是 UUID 或 PID 格式，
不會與此哨兵值衝突。下游聚合分析（TeamSnapshot / EnrichedUserProfile）
若要做 per-terminal 統計需排除此值。
"""

AGGREGATED_SESSION_ID: str = "aggregated"
"""跨 session 聚合的 digest events 使用的哨兵 session_id。

用於 period_digest(FULL) 和 daily_digest — 這些事件不屬於任何特定 session。
值恰巧與 AGGREGATED_TERMINAL_ID 相同，但語義獨立；未來若修改其中之一，
另一個不應連帶變動。
"""


# ── Pydantic Models ──────────────────────────────────────────────────────────


class DigestType(str, Enum):
    IMMEDIATE = "immediate"
    FULL = "full"


class PeriodStats(BaseModel):
    """跨 terminal 合併後的結構化統計。Deterministic 產出。"""

    period_start: float
    period_end: float
    terminals_active: list[str] = Field(default_factory=list)
    corners_run: dict[str, int] = Field(default_factory=dict)
    gate_results: dict[str, str] = Field(default_factory=dict)
    wns_progression: dict[str, list[float]] = Field(default_factory=dict)
    tools_used: list[str] = Field(default_factory=list)
    chat_queries_count: int = 0
    conversation_insights: list[dict] = Field(default_factory=list)


class PeriodDigest(BaseModel):
    period_start: float
    period_end: float
    digest_type: DigestType
    terminal_id: str | None
    structured_stats: PeriodStats
    llm_summary: str


class DailyStats(BaseModel):
    """全天聚合統計。"""

    date: str
    periods_count: int
    total_corners_run: int
    corners_passed: list[str] = Field(default_factory=list)
    corners_pending: list[str] = Field(default_factory=list)
    total_chat_queries: int = 0
    terminals_used: list[str] = Field(default_factory=list)


class DailyDigest(BaseModel):
    date: str
    structured_stats: DailyStats
    llm_summary: str


# ── 純函式 ────────────────────────────────────────────────────────────────────


def _is_tool_run_command(event: Any) -> bool:
    """event 是否為被 tool_signatures 識別的 EDA tool command。

    判定依據：event_type == 'command' 且 payload.tool_name 存在且非空。
    tool_name 在 jsonrpc_server pre_exec 路徑中由 tool_signatures 識別後寫入 payload。
    """
    if event.event_type != "command":
        return False
    tool_name = event.payload.get("tool_name")
    return bool(tool_name)


def has_pending_job(events: list[Any]) -> bool:
    """檢查 events 中最後一個 tool_run command 是否尚未收尾。

    規則：
      1. 從後往前找第一個 _is_tool_run_command() 為 True 的 event
      2. 找不到 → False
      3. 找到後，檢查其後是否有 event_type in ('metric', 'gate_eval')
         有 → False（已收尾）；無 → True（未收尾）

    只看最後一個 EDA tool run 有無收尾，
    非 EDA command（cd/ls/git）完全不列入。
    """
    last_tool_idx = -1
    for i in range(len(events) - 1, -1, -1):
        if _is_tool_run_command(events[i]):
            last_tool_idx = i
            break

    if last_tool_idx < 0:
        return False

    # 檢查 last_tool_idx 之後是否有收尾 event
    for j in range(last_tool_idx + 1, len(events)):
        if events[j].event_type in ("metric", "gate_eval"):
            return False

    return True


def split_into_periods(events: list[Any]) -> list[list[Any]]:
    """按 activity gap 切分 events 成工作時段。

    規則：
      1. events 按 timestamp 排序（跨 terminal 合併）
      2. 相鄰 gap > ACTIVITY_GAP_THRESHOLD_SEC 時：
         - 若 has_pending_job(current_period) → 不切（等 EDA job 跑完）
         - 否則切開
      3. 空 events → []
    """
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: e.timestamp)
    periods: list[list[Any]] = []
    current: list[Any] = [sorted_events[0]]

    for i in range(1, len(sorted_events)):
        gap = sorted_events[i].timestamp - sorted_events[i - 1].timestamp
        if gap > ACTIVITY_GAP_THRESHOLD_SEC and not has_pending_job(current):
            periods.append(current)
            current = [sorted_events[i]]
        else:
            current.append(sorted_events[i])

    if current:
        periods.append(current)

    return periods


# ── 內部 Helper ──────────────────────────────────────────────────────────────


def _compute_period_stats(
    events: list[Any],
    period_start: float,
    period_end: float,
) -> PeriodStats:
    """從 events 計算 PeriodStats。"""
    terminals: set[str] = set()
    corners_run: dict[str, int] = {}
    gate_results: dict[str, str] = {}
    wns_progression: dict[str, list[float]] = {}
    tools_used: set[str] = set()
    chat_queries_count = 0
    conversation_insights: list[dict] = []

    for e in events:
        if e.terminal_id:
            terminals.add(e.terminal_id)

        p = e.payload
        et = e.event_type

        if et == "gate_eval":
            corner = p.get("corner")
            if corner:
                corners_run[corner] = corners_run.get(corner, 0) + 1
                status = p.get("status", "")
                gate_results[corner] = status

        elif et == "metric":
            corner = p.get("corner")
            metric_name = p.get("metric_name", "")
            if corner and metric_name.lower() == "wns":
                wns_progression.setdefault(corner, []).append(
                    float(p.get("metric_value", 0.0))
                )

        elif et == "command":
            tool_name = p.get("tool_name")
            if tool_name:
                tools_used.add(tool_name)

        elif et == "chat_query":
            chat_queries_count += 1

        elif et == "conversation_insight":
            ins = p.get("insights", [])
            conversation_insights.extend(ins)

    return PeriodStats(
        period_start=period_start,
        period_end=period_end,
        terminals_active=sorted(terminals),
        corners_run=corners_run,
        gate_results=gate_results,
        wns_progression=wns_progression,
        tools_used=sorted(tools_used),
        chat_queries_count=chat_queries_count,
        conversation_insights=conversation_insights,
    )


def _format_iso(ts: float) -> str:
    """timestamp → 本地時區 ISO 8601 字串（絕對日期）。"""
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _format_insights_bullets(insights: list[dict]) -> str:
    """格式化 conversation insights 為 bullet list。無 insights 時回空字串。"""
    if not insights:
        return ""
    lines = []
    for ins in insights:
        topic = ins.get("topic", "general")
        content = ins.get("content", "")
        lines.append(f"- [{topic}] {content}")
    return "\n".join(lines)


# ── LLM Prompt 模板 ─────────────────────────────────────────────────────────

_IMMEDIATE_PROMPT = """\
你是 IC 設計工程師的工作日誌助手。根據以下單一 terminal 的工作時段統計，生成簡短摘要。

要求：
- 200 字以內（繁體中文）
- 包含：主要活動、進度、值得注意的 pattern
- 不要列出原始數字，已在結構化 stats 中

[Terminal]: {terminal_id}
[Period]: {period_start_iso} ~ {period_end_iso}
[Period Statistics]
{structured_stats_text}
{insights_section}\
摘要："""

_FULL_PROMPT = """\
你是 IC 設計工程師的工作日誌助手。根據以下跨 terminal 的工作時段統計，生成摘要。

此時段 user 使用了 {terminal_count} 個 terminal 同時作業。

要求：
- 500 字以內（繁體中文）
- 包含：主要成果 / 未完成事項 / 值得注意的 pattern
- 不要列出原始數字，已在結構化 stats 中

[Period]: {period_start_iso} ~ {period_end_iso}
[Period Statistics]
{structured_stats_text}
{insights_section}\
摘要："""

_DAILY_PROMPT = """\
以下是今天的工作時段摘要。生成一份全天整合報告。

要求：
- 800 字以內（繁體中文）
- 包含：全天進度、跨時段趨勢、明天建議優先事項
- 不要重複 stats 中的原始數字

[Date]: {date}
[Daily Statistics]
{daily_stats_text}

{period_summaries}
報告："""


# ── Dreamer Class ────────────────────────────────────────────────────────────


class Dreamer:
    """Dreaming Layer 1 — 同步 produce API + 非同步 submit executor。"""

    def __init__(
        self,
        event_store: EventStore,
        llm_router: LLMRouter,
        max_workers: int = 2,
        deep_analysis_runner: Any | None = None,
    ) -> None:
        """search_index 由 EventStore.emit() hook 自動處理，
        Dreamer 不需要直接持有 SearchIndex 參照。

        以 ThreadPoolExecutor(max_workers=2) 排隊執行。

        deep_analysis_runner：AgentRunner instance，
        用 analysis-slot provider 建立、配 timeout=120 / max_rounds=8。
        若 None，produce_deep_analysis 會回 status='skipped'。
        """
        self._event_store = event_store
        self._llm_router = llm_router
        self._deep_analysis_runner = deep_analysis_runner
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="terminux-dreamer",
        )
        self._closed = False
        self._active_futures: set[Future[Any]] = set()
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────────

    def _discard_future(self, future: Future[Any]) -> None:
        with self._lock:
            self._active_futures.discard(future)

    def submit_immediate_digest(
        self,
        user_id: str,
        terminal_id: str,
        period_start: float,
        period_end: float,
    ) -> Future[PeriodDigest] | None:
        """非同步提交 immediate period digest 任務。

        以 ThreadPoolExecutor(max_workers=2) 排隊執行。
        已 close 時回傳 None（不拋異常，方便 caller 在 shutdown 期間優雅處理）。

        回傳值：Future 可用於等待完成（測試用），daemon 正常路徑不等待。
        """
        if self._closed:
            logger.warning(
                "submit_immediate_digest called after close; skipping (terminal=%s)",
                terminal_id,
            )
            return None

        future = self._executor.submit(
            self.produce_immediate_period_digest,
            user_id=user_id,
            terminal_id=terminal_id,
            period_start=period_start,
            period_end=period_end,
        )
        with self._lock:
            self._active_futures.add(future)
        future.add_done_callback(self._discard_future)

        logger.info(
            "Submitted immediate digest: terminal=%s, period=%.1f~%.1f",
            terminal_id, period_start, period_end,
        )
        return future

    def signal_stop(self) -> None:
        """非阻塞發出停止信號：拒絕新任務並取消排隊中的任務。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            logger.warning("Dreamer signal_stop failed: %s", exc)

    def join(self, timeout: float | None = None) -> bool:
        """使用 concurrent.futures.wait 等待進行中的 active futures。"""
        with self._lock:
            actives = list(self._active_futures)
        if actives and timeout is not None and timeout > 0:
            done, not_done = concurrent.futures.wait(actives, timeout=timeout)
            return len(not_done) == 0
        elif actives and timeout is None:
            concurrent.futures.wait(actives)
            return True
        return len(actives) == 0

    @property
    def is_alive(self) -> bool:
        """是否有 worker 仍在執行。"""
        with self._lock:
            return any(not f.done() for f in self._active_futures)

    def close(self, timeout: float | None = None) -> bool:
        """關閉 executor（冪等）。daemon shutdown 時由 jsonrpc_server 呼叫。"""
        self.signal_stop()
        return self.join(timeout=timeout)

    def produce_immediate_period_digest(
        self,
        user_id: str,
        terminal_id: str,
        period_start: float,
        period_end: float,
    ) -> PeriodDigest:
        """產生 per-terminal 的 immediate period digest（200 字以內 LLM summary）。

        LLM summary 失敗時留空（llm_summary==""），本函式不重試。
        """
        logger.info(
            "Producing immediate period digest: terminal=%s, period=%.1f~%.1f",
            terminal_id, period_start, period_end,
        )

        # 1. 撈 events
        events = self._event_store.query_events_in_range(
            user_id=user_id,
            start_ts=period_start,
            end_ts=period_end,
            terminal_id=terminal_id,
        )

        # 2. 計算 stats
        stats = _compute_period_stats(events, period_start, period_end)
        stats.terminals_active = [terminal_id]

        # 3. LLM summary
        llm_summary = self._generate_immediate_summary(terminal_id, stats)

        # 4. 構造 PeriodDigest
        digest = PeriodDigest(
            period_start=period_start,
            period_end=period_end,
            digest_type=DigestType.IMMEDIATE,
            terminal_id=terminal_id,
            structured_stats=stats,
            llm_summary=llm_summary,
        )

        # 5. 寫入 event_store（search_index 由 emit() hook 自動處理）
        event = self._make_period_digest_event(digest, user_id)
        self._event_store.emit(event)

        logger.info(
            "Immediate period digest produced: llm_summary_length=%d",
            len(llm_summary),
        )
        return digest

    def produce_full_period_digest(
        self,
        user_id: str,
        period_start: float,
        period_end: float,
    ) -> PeriodDigest:
        """產生跨 terminal 的 full period digest（500 字以內）。

        Append-only：不覆寫既有 immediate digest，追加新的 FULL event。
        LLM summary 失敗時留空，本函式不重試。
        """
        logger.info(
            "Producing full period digest: period=%.1f~%.1f",
            period_start, period_end,
        )

        # 1. 撈所有 terminal 的 events
        events = self._event_store.query_events_in_range(
            user_id=user_id,
            start_ts=period_start,
            end_ts=period_end,
        )

        # 2. 計算 stats
        stats = _compute_period_stats(events, period_start, period_end)

        # 3. LLM summary
        llm_summary = self._generate_full_summary(stats)

        # 4. 構造 PeriodDigest
        digest = PeriodDigest(
            period_start=period_start,
            period_end=period_end,
            digest_type=DigestType.FULL,
            terminal_id=None,
            structured_stats=stats,
            llm_summary=llm_summary,
        )

        # 5. 寫入 event_store（append，不覆寫既有 immediate）
        event = self._make_period_digest_event(digest, user_id)
        self._event_store.emit(event)

        logger.info(
            "Full period digest produced: llm_summary_length=%d",
            len(llm_summary),
        )
        return digest

    def produce_daily_digest(
        self,
        user_id: str,
        date_str: str,
    ) -> DailyDigest:
        """產生當天的 daily digest（800 字以內）。

        LLM summary 失敗時留空，本函式不重試。
        """
        logger.info("Producing daily digest: date=%s", date_str)

        # 1. 日期 → 時間範圍（本地時區）
        dt_start = datetime.fromisoformat(date_str)
        start_ts = dt_start.timestamp()
        end_ts = dt_start.replace(
            hour=23, minute=59, second=59, microsecond=999999,
        ).timestamp()

        # 2. 撈當日 period_digest events
        all_digests = self._event_store.query_events_in_range(
            user_id=user_id,
            start_ts=start_ts,
            end_ts=end_ts,
            event_types=["period_digest"],
        )

        # 每 period 優先取 FULL，fallback 取 IMMEDIATE
        selected_digests = self._select_best_digests(all_digests)

        # 3. 計算 DailyStats
        daily_stats = self._compute_daily_stats(date_str, selected_digests)

        # 4. LLM summary
        llm_summary = self._generate_daily_summary(
            date_str, daily_stats, selected_digests,
        )

        # 5. 構造 DailyDigest
        digest = DailyDigest(
            date=date_str,
            structured_stats=daily_stats,
            llm_summary=llm_summary,
        )

        # 6. 寫入 event_store
        from terminux_engine.store.event_store import SessionEvent

        event = SessionEvent(
            event_id=uuid.uuid4().hex,
            session_id=AGGREGATED_SESSION_ID,
            terminal_id=AGGREGATED_TERMINAL_ID,
            user_id=user_id,
            timestamp=end_ts,
            event_type="daily_digest",
            payload={
                "date": date_str,
                "structured_stats": daily_stats.model_dump(),
                "llm_summary": llm_summary,
            },
        )
        self._event_store.emit(event)

        logger.info(
            "Daily digest produced: llm_summary_length=%d",
            len(llm_summary),
        )
        return digest

    # ── deep_analysis（Agent-driven） ──────────────────

    def produce_deep_analysis(
        self,
        user_id: str,
        date_str: str,
    ) -> dict:
        """跑 Dreaming AgentRunner，把分析結論寫進 event_store。

        回傳 dict：{"status": "completed" | "no_finding" | "error",
                   "analysis": str | None, "error": str | None}

        - runner=None → status='skipped'（caller 應在呼叫前自行檢查；保險再擋一次）
        - Agent.no_action=True → status='no_finding'，event 仍寫但 analysis=None
        - Agent.error 或 raise → status='error'，event 仍寫供事後追蹤
        - 正常完成 → status='completed'，event 帶完整 analysis
        """
        from datetime import datetime as _dt, timezone as _tz

        if self._deep_analysis_runner is None:
            return {"status": "skipped", "analysis": None, "error": None}

        analyzed_at_iso = (
            _dt.now(_tz.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )

        try:
            from terminux_engine.llm.agent_prompt import (
                build_deep_analysis_query,
                build_deep_analysis_system_prompt,
            )

            system_prompt = build_deep_analysis_system_prompt(user_id, analyzed_at_iso)
            query = build_deep_analysis_query(user_id)
            result = self._deep_analysis_runner.run(
                query=query,
                system_context=system_prompt,
                trigger_source="dreaming",
                mode="sync",
            )
        except Exception as exc:
            logger.warning("deep_analysis runner raised: %s", exc)
            self._emit_deep_analysis_event(
                user_id=user_id,
                analyzed_at_iso=analyzed_at_iso,
                payload_extra={"status": "error", "error": str(exc), "analysis": None},
            )
            return {"status": "error", "analysis": None, "error": str(exc)}

        # Agent 收斂出的 error / no_action 都走 event but mark status
        if getattr(result, "error", None):
            self._emit_deep_analysis_event(
                user_id=user_id,
                analyzed_at_iso=analyzed_at_iso,
                payload_extra={
                    "status": "error",
                    "error": result.error,
                    "analysis": None,
                    "rounds": getattr(result, "rounds", 0),
                    "elapsed_sec": getattr(result, "elapsed_sec", 0.0),
                },
            )
            return {"status": "error", "analysis": None, "error": result.error}

        if getattr(result, "no_action", False):
            self._emit_deep_analysis_event(
                user_id=user_id,
                analyzed_at_iso=analyzed_at_iso,
                payload_extra={
                    "status": "no_finding",
                    "analysis": None,
                    "rounds": getattr(result, "rounds", 0),
                    "elapsed_sec": getattr(result, "elapsed_sec", 0.0),
                },
            )
            return {"status": "no_finding", "analysis": None, "error": None}

        answer = getattr(result, "answer", None) or ""
        evidence = getattr(result, "evidence", None) or []
        self._emit_deep_analysis_event(
            user_id=user_id,
            analyzed_at_iso=analyzed_at_iso,
            payload_extra={
                "status": "completed",
                "analysis": answer,
                "evidence_count": len(evidence),
                "rounds": getattr(result, "rounds", 0),
                "elapsed_sec": getattr(result, "elapsed_sec", 0.0),
            },
        )
        return {"status": "completed", "analysis": answer, "error": None}

    def _emit_deep_analysis_event(
        self,
        *,
        user_id: str,
        analyzed_at_iso: str,
        payload_extra: dict,
    ) -> None:
        """寫一筆 deep_analysis event 進 event_store。失敗只 log。"""
        from uuid import uuid4 as _uuid4
        import time as _t
        from terminux_engine.store.event_store import SessionEvent

        payload = dict(payload_extra)
        payload["analyzed_at"] = analyzed_at_iso
        payload["date_str"] = analyzed_at_iso[:10]
        try:
            self._event_store.emit(
                SessionEvent(
                    event_id=_uuid4().hex,
                    session_id="dreaming",
                    user_id=user_id,
                    project_id=None,
                    timestamp=_t.time(),
                    event_type="deep_analysis",
                    payload=payload,
                    block_name=None,
                )
            )
        except Exception as exc:
            logger.warning("emit deep_analysis event failed: %s", exc)

    # ── 內部 helper ─────────────────────────────────────────────────────────

    def _generate_immediate_summary(
        self, terminal_id: str, stats: PeriodStats,
    ) -> str:
        """產生 immediate period 的 LLM summary（200 字）。"""
        insights_bullets = _format_insights_bullets(stats.conversation_insights)
        insights_section = (
            f"\n[Conversation Insights]\n{insights_bullets}\n\n"
            if insights_bullets else "\n"
        )

        prompt = _IMMEDIATE_PROMPT.format(
            terminal_id=terminal_id,
            period_start_iso=_format_iso(stats.period_start),
            period_end_iso=_format_iso(stats.period_end),
            structured_stats_text=yaml.safe_dump(
                stats.model_dump(), allow_unicode=True,
            ),
            insights_section=insights_section,
        )
        return self._call_llm(prompt, timeout=30.0)

    def _generate_full_summary(self, stats: PeriodStats) -> str:
        """產生 full period 的 LLM summary（500 字）。"""
        insights_bullets = _format_insights_bullets(stats.conversation_insights)
        insights_section = (
            f"\n[Conversation Insights]\n{insights_bullets}\n\n"
            if insights_bullets else "\n"
        )

        prompt = _FULL_PROMPT.format(
            terminal_count=len(stats.terminals_active),
            period_start_iso=_format_iso(stats.period_start),
            period_end_iso=_format_iso(stats.period_end),
            structured_stats_text=yaml.safe_dump(
                stats.model_dump(), allow_unicode=True,
            ),
            insights_section=insights_section,
        )
        return self._call_llm(prompt, timeout=60.0)

    def _generate_daily_summary(
        self,
        date_str: str,
        daily_stats: DailyStats,
        selected_digests: list[Any],
    ) -> str:
        """產生 daily digest 的 LLM summary（800 字）。"""
        period_parts: list[str] = []
        for i, d_event in enumerate(selected_digests, 1):
            p = d_event.payload
            ps = p.get("period_start", 0)
            pe = p.get("period_end", 0)
            summary = p.get("llm_summary", "（無摘要）")
            period_parts.append(
                f"[Period {i}: {_format_iso(ps)} ~ {_format_iso(pe)}]\n{summary}"
            )

        prompt = _DAILY_PROMPT.format(
            date=date_str,
            daily_stats_text=yaml.safe_dump(
                daily_stats.model_dump(), allow_unicode=True,
            ),
            period_summaries=(
                "\n\n".join(period_parts) if period_parts
                else "（當日無工作時段記錄）"
            ),
        )
        return self._call_llm(prompt, timeout=90.0)

    def _call_llm(self, prompt: str, timeout: float) -> str:
        """呼叫 LLM，失敗時回 ""，不 retry。"""
        from terminux_engine.llm.llm_router import TaskType

        try:
            result = self._llm_router.generate(
                prompt, task_type=TaskType.BACKGROUND, timeout=timeout,
            )
            return result.strip() if result else ""
        except Exception as exc:
            logger.warning("Dreamer LLM summary failed: %s", exc)
            return ""

    def _make_period_digest_event(
        self, digest: PeriodDigest, user_id: str,
    ) -> Any:
        """構造 period_digest SessionEvent。"""
        from terminux_engine.store.event_store import SessionEvent

        return SessionEvent(
            event_id=uuid.uuid4().hex,
            session_id=AGGREGATED_SESSION_ID,
            terminal_id=digest.terminal_id or AGGREGATED_TERMINAL_ID,
            user_id=user_id,
            timestamp=digest.period_end,
            event_type="period_digest",
            payload={
                "period_start": digest.period_start,
                "period_end": digest.period_end,
                "digest_type": digest.digest_type.value,
                "structured_stats": digest.structured_stats.model_dump(),
                "llm_summary": digest.llm_summary,
            },
        )

    @staticmethod
    def _select_best_digests(digest_events: list[Any]) -> list[Any]:
        """每個 period 優先取 FULL digest，fallback 取 IMMEDIATE。"""
        groups: dict[tuple[float, float], list[Any]] = {}
        for e in digest_events:
            key = (
                e.payload.get("period_start", 0.0),
                e.payload.get("period_end", 0.0),
            )
            groups.setdefault(key, []).append(e)

        selected: list[Any] = []
        for key in sorted(groups.keys()):
            digests = groups[key]
            full = [
                d for d in digests
                if d.payload.get("digest_type") == "full"
            ]
            selected.append(full[0] if full else digests[0])
        return selected

    @staticmethod
    def _compute_daily_stats(
        date_str: str, selected_digests: list[Any],
    ) -> DailyStats:
        """從選定的 period_digest events 計算 DailyStats。"""
        total_corners_run = 0
        all_corners_passed: set[str] = set()
        all_corners: set[str] = set()
        total_chat_queries = 0
        all_terminals: set[str] = set()

        for d in selected_digests:
            stats = d.payload.get("structured_stats", {})

            corners_run = stats.get("corners_run", {})
            total_corners_run += sum(corners_run.values())

            gate_results = stats.get("gate_results", {})
            for corner, status in gate_results.items():
                all_corners.add(corner)
                if status.lower() == "pass":
                    all_corners_passed.add(corner)

            total_chat_queries += stats.get("chat_queries_count", 0)
            all_terminals.update(stats.get("terminals_active", []))

        corners_pending = sorted(all_corners - all_corners_passed)

        return DailyStats(
            date=date_str,
            periods_count=len(selected_digests),
            total_corners_run=total_corners_run,
            corners_passed=sorted(all_corners_passed),
            corners_pending=corners_pending,
            total_chat_queries=total_chat_queries,
            terminals_used=sorted(all_terminals),
        )


# ── DailyCronResult + run_daily_cron ─────────────────────────────────────────


@dataclass
class DailyCronResult:
    """Daily cron 的執行結果摘要。"""

    date: str
    periods_processed: int
    full_digests_appended: int
    immediate_digests_upgraded: int  # 同 period 已有 immediate，本次 append 新 FULL 的數量
    daily_digest_created: bool
    errors: list[str] = field(default_factory=list)
    # deep_analysis 狀態
    deep_analysis_status: str = "skipped"  # skipped | completed | no_finding | error
    deep_analysis_error: str | None = None


def run_daily_cron(
    event_store: EventStore,
    llm_router: LLMRouter,
    user_id: str,
    date_str: str,
    dreamer: Dreamer | None = None,
) -> DailyCronResult:
    """Daily cron orchestration：逐 period 升級 + 產 daily_digest。

    若 dreamer is None → 自建，流程結束前 close。
    若 caller 傳入 → 不負責 close。
    """
    own_dreamer = dreamer is None
    if own_dreamer:
        dreamer = Dreamer(event_store=event_store, llm_router=llm_router)

    try:
        return _run_daily_cron_inner(event_store, dreamer, user_id, date_str)
    finally:
        if own_dreamer:
            dreamer.close()


def _run_daily_cron_inner(
    event_store: EventStore,
    dreamer: Dreamer,
    user_id: str,
    date_str: str,
) -> DailyCronResult:
    """run_daily_cron 的內部實作。"""
    # 1. 時區與日期邊界
    dt_start = datetime.fromisoformat(date_str).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    start_ts = dt_start.timestamp()
    end_ts = dt_start.replace(
        hour=23, minute=59, second=59, microsecond=999999,
    ).timestamp()

    # 2. 撈 events 並過濾 digest events
    all_events = event_store.query_events_in_range(
        user_id=user_id,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    _DIGEST_TYPES = {"period_digest", "daily_digest"}
    filtered = [e for e in all_events if e.event_type not in _DIGEST_TYPES]

    # 3. 切分 periods
    periods = split_into_periods(filtered)

    # 4. 逐 period 處理
    periods_processed = 0
    full_digests_appended = 0
    immediate_digests_upgraded = 0
    errors: list[str] = []

    for period in periods:
        period_start = period[0].timestamp
        period_end = period[-1].timestamp
        try:
            existing = event_store.find_period_digests_by_range(
                user_id=user_id,
                period_start=period_start,
                period_end=period_end,
            )

            has_full = any(
                e.payload.get("digest_type") == "full" for e in existing
            )
            has_immediate = any(
                e.payload.get("digest_type") == "immediate" for e in existing
            )

            if has_full:
                # 已有 FULL → skip
                pass
            elif has_immediate:
                # 已有 IMMEDIATE → 升級為 FULL（append-only）
                dreamer.produce_full_period_digest(
                    user_id=user_id,
                    period_start=period_start,
                    period_end=period_end,
                )
                immediate_digests_upgraded += 1
            else:
                # 無 digest → 新建 FULL
                dreamer.produce_full_period_digest(
                    user_id=user_id,
                    period_start=period_start,
                    period_end=period_end,
                )
                full_digests_appended += 1

        except Exception as exc:
            iso_start = _format_iso(period_start)
            iso_end = _format_iso(period_end)
            errors.append(f"period {iso_start}~{iso_end}: {exc}")

        periods_processed += 1

    # 5. 產 daily_digest
    daily_digest_created = False
    try:
        dreamer.produce_daily_digest(user_id=user_id, date_str=date_str)
        daily_digest_created = True
    except Exception as exc:
        errors.append(f"daily_digest: {exc}")

    # 6. deep_analysis（Agent-driven 開放式分析）
    deep_status = "skipped"
    deep_error: str | None = None
    if dreamer._deep_analysis_runner is not None:
        try:
            outcome = dreamer.produce_deep_analysis(user_id=user_id, date_str=date_str)
            deep_status = outcome.get("status", "completed")
            if deep_status == "error":
                deep_error = outcome.get("error")
                errors.append(f"deep_analysis: {deep_error}")
        except Exception as exc:
            deep_status = "error"
            deep_error = str(exc)
            errors.append(f"deep_analysis: {exc}")

    return DailyCronResult(
        date=date_str,
        periods_processed=periods_processed,
        full_digests_appended=full_digests_appended,
        immediate_digests_upgraded=immediate_digests_upgraded,
        daily_digest_created=daily_digest_created,
        errors=errors,
        deep_analysis_status=deep_status,
        deep_analysis_error=deep_error,
    )


# ─── deep_analysis helpers ─────────────────────────────


def find_recent_deep_analysis(
    event_store: EventStore | None,
    user_id: str,
    max_age_days: int = 7,
) -> str | None:
    """查最近 N 天內最新一筆 deep_analysis event 的 analysis 文字。

    找不到 / event_store unavailable / 該 event 的 analysis 為空 → 回 None。
    """
    if event_store is None or not user_id:
        return None
    import time as _t

    cutoff = _t.time() - max(1, max_age_days) * 86400
    try:
        events = event_store.query(
            user_id=user_id,
            event_type="deep_analysis",
            after=cutoff,
            order="desc",
            limit=1,
        )
    except Exception as exc:
        logger.warning("find_recent_deep_analysis query failed: %s", exc)
        return None
    if not events:
        return None
    payload = getattr(events[0], "payload", None) or {}
    analysis = payload.get("analysis")
    if not analysis or not isinstance(analysis, str):
        return None
    analyzed_at = payload.get("analyzed_at") or ""
    if analyzed_at:
        return f"{analysis}\n\n（分析時間：{analyzed_at}）"
    return analysis
