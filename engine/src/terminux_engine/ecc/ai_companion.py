# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""AI Companion — 解讀 ECC Object 並生成人類可讀的解釋與建議。

使用現有 LLM provider 的 complete(prompt: str) -> str 介面。
LLM 不可用時有 deterministic fallback。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from terminux_engine.memory.compaction import ConversationCompactor
    from terminux_engine.memory.digest_models import RecentWorkHistoryPayload
    from terminux_engine.memory.flush import MemoryFlusher

from pydantic import BaseModel

from terminux_engine.ecc.ecc_object import ECCObject, GateResult, MetricEntry, SessionContinuity, SessionSummary
from terminux_engine.ecc.playbook import QualityGate, StageSpec
from terminux_engine.ecc.profile import EnrichedUserProfile
from terminux_engine.ecc.team import TeamSnapshot

logger = logging.getLogger(__name__)


# ─── 多輪對話記憶 ─────────────────────────────────────────────────────────────

#: 回合來源。只有 `agent_c2`（經 TaskScope 最終 Gate 送出並回來的 Agent 回合）可進入
#: 可外送投影；其餘一律只供本機顯示。`unattributed` 是未標記寫入者的安全預設。
PROVENANCE_AGENT_C2 = "agent_c2"
PROVENANCE_COMPANION_LOCAL = "companion_local"
PROVENANCE_COMPACTION_DERIVED = "compaction_derived"
PROVENANCE_IMPORTED = "imported"
PROVENANCE_UNATTRIBUTED = "unattributed"
PROVENANCES = frozenset({
    PROVENANCE_AGENT_C2, PROVENANCE_COMPANION_LOCAL, PROVENANCE_COMPACTION_DERIVED,
    PROVENANCE_IMPORTED, PROVENANCE_UNATTRIBUTED,
})

#: 可外送投影排除回合的原因（只計數，不含內容）。
WITHHELD_CONTEXT_REVOKED = "context_revoked"
WITHHELD_POLICY_CHANGED = "policy_changed"
WITHHELD_CONTENT_POLICY = "content_policy"
WITHHELD_LIMIT = "limit"
WITHHELD_NOT_EXPORTABLE = "not_exportable"
WITHHELD_REASONS = (WITHHELD_CONTEXT_REVOKED, WITHHELD_POLICY_CHANGED, WITHHELD_CONTENT_POLICY,
                    WITHHELD_LIMIT, WITHHELD_NOT_EXPORTABLE)
#: 「未指定 epoch」的哨兵：呼叫端未傳 `context_epoch` 時不做 epoch 比對（既有呼叫不變）。
_ANY_EPOCH = object()


@dataclass
class ConversationTurn:
    """一輪對話（user 或 assistant）。

    `provenance`／`policy_revision`／`destination` 由寫入者在寫入當下標記；
    既有的 `ConversationTurn(role=..., content=...)` 建構呼叫不受影響。
    """
    role: str  # "user" | "assistant" | "system"
    content: str
    provenance: str = PROVENANCE_UNATTRIBUTED
    policy_revision: str | None = None
    destination: str | None = None
    turn_id: str = ""
    sent_status: str | None = None
    #: 寫入當下的外送上下文 epoch（`llm.context_epoch`）；None 表示沒有可證明的 epoch。
    context_epoch: str | None = None


@dataclass(frozen=True)
class ExportProjection:
    """可外送投影的結果：實際投影的回合，以及每個原因被排除的回合數（不含內容）。"""

    turns: tuple[ConversationTurn, ...]
    withheld: dict[str, int]

    @property
    def text(self) -> str | None:
        return _render_exportable(list(self.turns)) if self.turns else None

    @property
    def withheld_total(self) -> int:
        return sum(self.withheld.values())


def _checked_provenance(value: str) -> str:
    """未知的 provenance 字串不得偽裝成可外送；一律降為 `unattributed`。"""
    return value if value in PROVENANCES else PROVENANCE_UNATTRIBUTED


def _render_exportable(turns: list[ConversationTurn]) -> str:
    """可外送投影的渲染格式（與 `get_history_text()` 的 user／assistant 部分相同）。"""
    lines = ["## Conversation History"]
    for turn in turns:
        label = "User" if turn.role == "user" else "Assistant"
        lines.append(f"{label}: {turn.content}")
    return "\n".join(lines) + "\n\n"


class ConversationBuffer:
    """固定大小的多輪對話緩衝區。

    trim 策略（compactor=None 時）：先 trim by turns（超過 max_turns 從頭刪），
    再 trim by chars（合計字元數超過 max_chars 時從頭刪，保留至少 2 turn）。

    compaction 策略（compactor 不為 None 時）：容量到 75% 時觸發 LLM compaction，
    摘要注入 system turn + 保留最近 3 輪原始對話。
    """

    COMPACTION_THRESHOLD = 0.75  # 容量到 75% 時觸發
    RECENT_KEEP_COUNT = 6       # compaction 後保留最近 N 條 message（3 輪）

    def __init__(
        self,
        max_turns: int = 20,
        max_chars: int = 8000,
        compactor: ConversationCompactor | None = None,
        flusher: MemoryFlusher | None = None,
        terminal_id: str = "default",
        session_id: str = "",
        user_id: str = "",
    ) -> None:
        self.max_turns = max_turns
        self.max_chars = max_chars
        self._turns: list[ConversationTurn] = []
        self._compactor = compactor
        self._flusher = flusher
        self._terminal_id = terminal_id
        self._session_id = session_id
        self._user_id = user_id
        self._compaction_metadata: dict[str, Any] | None = None

    def add_user(self, query: str, *, provenance: str = PROVENANCE_UNATTRIBUTED,
                 policy_revision: str | None = None, destination: str | None = None,
                 turn_id: str = "", context_epoch: str | None = None) -> None:
        """記錄使用者輸入；未標記來源的寫入者得到不可外送的預設 provenance。"""
        self._turns.append(ConversationTurn(
            role="user", content=query, provenance=_checked_provenance(provenance),
            policy_revision=policy_revision, destination=destination, turn_id=turn_id,
            context_epoch=context_epoch))
        if self._compactor is not None:
            self._maybe_compact()
        else:
            self._trim()

    def add_assistant(self, response: str, *, provenance: str = PROVENANCE_UNATTRIBUTED,
                      policy_revision: str | None = None, destination: str | None = None,
                      turn_id: str = "", context_epoch: str | None = None) -> None:
        """記錄助理回應；provenance 語意同 `add_user`。"""
        self._turns.append(ConversationTurn(
            role="assistant", content=response, provenance=_checked_provenance(provenance),
            policy_revision=policy_revision, destination=destination, turn_id=turn_id,
            context_epoch=context_epoch))
        if self._compactor is not None:
            self._maybe_compact()
        else:
            self._trim()

    @property
    def usage_ratio(self) -> float:
        """當前容量使用比例 (0.0 ~ 1.0+)。取 turns 與 chars 兩個比例的較大值。"""
        if self.max_turns <= 0 and self.max_chars <= 0:
            return 0.0
        turns_ratio = len(self._turns) / self.max_turns if self.max_turns > 0 else 0.0
        total_chars = sum(len(t.content) for t in self._turns)
        chars_ratio = total_chars / self.max_chars if self.max_chars > 0 else 0.0
        return max(turns_ratio, chars_ratio)

    @property
    def compaction_metadata(self) -> dict[str, Any] | None:
        """最近一次 compaction 的 metadata。None 表示尚未 compaction 過。"""
        return self._compaction_metadata

    def get_history_text(self) -> str:
        """格式化為 prompt 段落。空 buffer 回傳空字串。"""
        if not self._turns:
            return ""
        lines = ["## Conversation History"]
        for t in self._turns:
            if t.role == "system":
                # compaction 產生的摘要直接放入，不加 label
                lines.append(t.content)
            else:
                label = "User" if t.role == "user" else "Assistant"
                lines.append(f"{label}: {t.content}")
        return "\n".join(lines) + "\n\n"

    def get_history_text_for_epoch(self, context_epoch: str | None, *, include_untagged: bool = True) -> str:
        """`get_history_text()` 的同一格式，但略過帶有**其他** epoch 標記的回合。

        `include_untagged=True` 時未標記 epoch 的既有回合照常出現（legacy 行為）；為 False 時一併略過
        （未標記回合無法證明屬於目前的外送授權）。
        全部被略過時回傳空字串。
        """
        kept = [t for t in self._turns if t.context_epoch == context_epoch
                or (include_untagged and t.context_epoch is None)]
        if not kept:
            return ""
        lines = ["## Conversation History"]
        for t in kept:
            if t.role == "system":
                lines.append(t.content)
            else:
                label = "User" if t.role == "user" else "Assistant"
                lines.append(f"{label}: {t.content}")
        return "\n".join(lines) + "\n\n"

    def get_exportable_history(self, *, policy_revision: str | None, destination: str | None,
                               max_turns: int | None = None, max_chars: int | None = None,
                               assistant_max_chars: int | None = None,
                               context_epoch: Any = _ANY_EPOCH,
                               content_filter: Callable[[str], bool] | None = None) -> str | None:
        """可外送投影：只渲染 `agent_c2` 且 policy revision／destination 相等的 user／assistant 回合。

        格式與 `get_history_text()` 相同（同一個標頭與 `User:`／`Assistant:` 標籤），
        但 `system` 回合（compaction 摘要）、fallback 與未標記回合一律不出現。
        `policy_revision` 或 `destination` 為 None 時沒有任何回合符合，回傳 None。
        `max_turns`／`max_chars` 由最新回合往前保留，`max_chars` 以**渲染後的完整文字**
        長度計算（與最終 Gate 檢查的對象相同）；`assistant_max_chars` 排除過長的助理
        回合。投影為空時回傳 None（不是空字串），讓 prompt 的 `conversation_history`
        明確為 null。其餘參數見 `export_projection`。
        """
        return self.export_projection(policy_revision=policy_revision, destination=destination,
                                      max_turns=max_turns, max_chars=max_chars,
                                      assistant_max_chars=assistant_max_chars, context_epoch=context_epoch,
                                      content_filter=content_filter).text

    def exportable_turn_count(self, *, policy_revision: str | None, destination: str | None,
                              max_turns: int | None = None, max_chars: int | None = None,
                              assistant_max_chars: int | None = None,
                              context_epoch: Any = _ANY_EPOCH,
                              content_filter: Callable[[str], bool] | None = None) -> int:
        """`get_exportable_history` 同一組條件下實際投影的回合數（供最終 Gate 核對上限）。"""
        return len(self.export_projection(policy_revision=policy_revision, destination=destination,
                                          max_turns=max_turns, max_chars=max_chars,
                                          assistant_max_chars=assistant_max_chars, context_epoch=context_epoch,
                                          content_filter=content_filter).turns)

    def export_projection(self, *, policy_revision: str | None, destination: str | None,
                          max_turns: int | None = None, max_chars: int | None = None,
                          assistant_max_chars: int | None = None,
                          context_epoch: Any = _ANY_EPOCH,
                          content_filter: Callable[[str], bool] | None = None) -> ExportProjection:
        """一次算出可外送回合與各原因的排除數。

        `context_epoch` 給定時（含 None）只投影寫入當下標記為同一 epoch 的回合；epoch 不同
        或缺少 epoch 標記的 `agent_c2` 回合計為 `context_revoked`。`content_filter(text)` 回傳
        False 的回合計為 `content_policy`：先逐回合判定，再對整段渲染結果判定，仍不通過時
        由最舊回合開始捨棄，直到整段通過為止。排除數只計 user／assistant 回合。
        """
        withheld = {reason: 0 for reason in WITHHELD_REASONS}
        if policy_revision is None or destination is None:
            withheld[WITHHELD_NOT_EXPORTABLE] = sum(1 for turn in self._turns if turn.role in ("user", "assistant"))
            return ExportProjection((), withheld)
        eligible: list[ConversationTurn] = []
        for turn in self._turns:
            if turn.role not in ("user", "assistant"):
                continue
            if turn.provenance != PROVENANCE_AGENT_C2:
                withheld[WITHHELD_NOT_EXPORTABLE] += 1
            elif context_epoch is not _ANY_EPOCH and (turn.context_epoch is None
                                                      or turn.context_epoch != context_epoch):
                withheld[WITHHELD_CONTEXT_REVOKED] += 1
            elif turn.policy_revision != policy_revision or turn.destination != destination:
                withheld[WITHHELD_POLICY_CHANGED] += 1
            elif (assistant_max_chars is not None and turn.role == "assistant"
                  and len(turn.content) > assistant_max_chars):
                withheld[WITHHELD_LIMIT] += 1
            elif content_filter is not None and not content_filter(_render_exportable([turn])):
                withheld[WITHHELD_CONTENT_POLICY] += 1
            else:
                eligible.append(turn)
        if max_turns is not None:
            kept = eligible[-max_turns:] if max_turns > 0 else []
            withheld[WITHHELD_LIMIT] += len(eligible) - len(kept)
            eligible = kept
        if max_chars is not None:
            while eligible and len(_render_exportable(eligible)) > max_chars:
                eligible.pop(0)
                withheld[WITHHELD_LIMIT] += 1
        if content_filter is not None:
            while eligible and not content_filter(_render_exportable(eligible)):
                eligible.pop(0)
                withheld[WITHHELD_CONTENT_POLICY] += 1
        return ExportProjection(tuple(eligible), withheld)

    def clear(self) -> None:
        """清空所有對話記錄。"""
        self._turns.clear()

    def _trim(self) -> None:
        """按 turns 數量及字元數裁剪（既有硬切邏輯）。"""
        # 1. trim by turns
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns:]

        # 2. trim by chars（保留至少 2 turn）
        while len(self._turns) > 2:
            total = sum(len(t.content) for t in self._turns)
            if total <= self.max_chars:
                break
            self._turns.pop(0)

    def _maybe_compact(self) -> None:
        """檢查容量，必要時觸發 compaction。"""
        if self.usage_ratio < self.COMPACTION_THRESHOLD:
            return
        self._do_compaction()

    def _do_compaction(self) -> None:
        """完整的 compaction 流程：flush → compact → 重組 buffer。"""
        conversation_text = self.get_history_text()
        turn_count = len(self._turns)

        # Step 1: Memory Flush（提取 insights 存成 events）
        insights: list[dict[str, str]] = []
        if self._flusher is not None:
            try:
                insights = self._flusher.flush(
                    conversation_text=conversation_text,
                    terminal_id=self._terminal_id,
                    session_id=self._session_id,
                    user_id=self._user_id,
                    compacted_from_turns=turn_count,
                )
            except Exception as exc:
                logger.warning("Memory flush failed during compaction: %s", exc)

        # Step 2: Compact（壓縮對話成摘要）
        try:
            summary = self._compactor.compact(conversation_text)  # type: ignore[union-attr]
        except Exception:
            summary = f"[對話摘要自動產生失敗，原始對話有 {turn_count} 輪]"

        # Step 3: 重組 buffer
        keep_count = min(self.RECENT_KEEP_COUNT, len(self._turns))
        recent_turns = self._turns[-keep_count:] if keep_count > 0 else []

        self._turns.clear()

        # 注入摘要作為 system context；衍生內容繼承最嚴格的來源，永不外送。
        self._turns.append(
            ConversationTurn(role="system", content=f"[先前對話摘要]\n{summary}",
                             provenance=PROVENANCE_COMPACTION_DERIVED)
        )

        # 恢復最近的對話
        self._turns.extend(recent_turns)

        # 記錄 metadata
        self._compaction_metadata = {
            "compacted_at": time.time(),
            "compacted_from_turns": turn_count,
            "compacted_to_summary_chars": len(summary),
            "insights_count": len(insights),
        }

    @property
    def turns(self) -> list[ConversationTurn]:
        """回傳當前 turns（唯讀用途）。"""
        return list(self._turns)


# ─── 回應模型 ─────────────────────────────────────────────────────────────────

class CompanionResponse(BaseModel):
    """AI companion 的回應。"""
    judgment: str
    """allow / warn"""
    explanation: str
    """人類可讀的判定說明"""
    suggestions: list[str]
    """建議行動清單"""
    severity: str
    """info / warning / critical"""


# ─── Recent Work History helper ───────────────────────────────────────────────


def _has_recent_work_history(payload: "RecentWorkHistoryPayload") -> bool:
    """判斷 RecentWorkHistoryPayload 是否有可渲染資料。

    Edge cases:
    - 完全無 digest（新 user）-> False
    - 只有今天 immediate 沒過去 daily -> True
    - 只有過去 daily 今天沒 immediate -> True
    - 兩者都有 -> True
    """
    return bool(payload.daily_digests) or bool(payload.today_immediates)


# ─── AICompanion ──────────────────────────────────────────────────────────────

class AICompanion:
    """AI 陪伴者。"""

    RECENT_HISTORY_DAYS = 5
    MAX_DIGEST_CHARS = 400
    MAX_TODAY_IMMEDIATE = 5
    MAX_IMMEDIATE_CHARS = 200

    def __init__(
        self,
        llm_complete: Callable[[str], str],
        session_history: list[SessionSummary] | None = None,
        last_session: "SessionContinuity | None" = None,
        user_profile: "EnrichedUserProfile | None" = None,
        team_snapshot: "TeamSnapshot | None" = None,
    ) -> None:
        self._llm_complete = llm_complete
        self._session_history: list[SessionSummary] = session_history or []
        self._conversation = ConversationBuffer()
        self._last_session: SessionContinuity | None = last_session
        self._user_profile: EnrichedUserProfile | None = user_profile
        self._team_snapshot: TeamSnapshot | None = team_snapshot

    # ─── summarize ────────────────────────────────────────────────────────

    def summarize_ecc(self, ecc: ECCObject) -> str:
        """將 ECC Object 壓縮成適合 LLM context 的結構化文字摘要。

        包含歷史趨勢（同 metric/corner 最近 3 筆）、corner 完成順序、
        以及事件歷史，讓 LLM 能做跨命令分析。
        """
        parts: list[str] = []
        parts.append(f"Session: {ecc.session_id}")
        parts.append(f"Stage: {ecc.current_stage_id or '(none)'}")
        parts.append(f"Work directory: {ecc.work_directory or '(none)'}")

        # command_history：最近 5 筆（含 postexec_summary）
        recent_cmds = ecc.command_history[-5:]
        if recent_cmds:
            parts.append("Recent commands:")
            for c in recent_cmds:
                line = f"  - {c.command} (tool={c.tool_name}, result={c.preexec_result})"
                if c.postexec_summary:
                    line += f" → {c.postexec_summary}"
                parts.append(line)

        # metric_records：按 (metric_name, corner) 分組，保留最近 3 筆顯示趨勢
        if ecc.metric_records:
            stage_ts = ecc.stage_entered_at or 0.0
            stage_metrics = [m for m in ecc.metric_records if m.timestamp >= stage_ts]
            if not stage_metrics:
                stage_metrics = ecc.metric_records

            # 分組收集所有值
            grouped: dict[tuple[str, str | None], list[MetricEntry]] = {}
            for m in stage_metrics:
                key = (m.metric_name, m.corner)
                grouped.setdefault(key, []).append(m)

            parts.append("Metrics:")
            for (metric_name, corner), entries in grouped.items():
                # 按 timestamp 降序排列，取最近 3 筆
                entries_sorted = sorted(entries, key=lambda e: e.timestamp, reverse=True)[:3]
                corner_label = f" [{corner}]" if corner else ""
                values = [e.value for e in entries_sorted]

                if len(values) == 1:
                    parts.append(f"  - {metric_name}{corner_label}: {values[0]} (1 run)")
                else:
                    # 最新 ← 前一次 ← 更前一次
                    trend_str = " \u2190 ".join(str(v) for v in values)
                    parts.append(
                        f"  - {metric_name}{corner_label}: {trend_str} ({len(values)} runs)"
                    )

        # gate_results：當前 stage
        if ecc.gate_results:
            stage_gates = [
                g for g in ecc.gate_results
                if g.stage_id == (ecc.current_stage_id or "")
            ]
            if stage_gates:
                parts.append("Gate results:")
                for g in stage_gates:
                    status = "PASS" if g.passed else "FAIL"
                    corner_label = f" [{g.corner}]" if g.corner else ""
                    parts.append(
                        f"  - {g.gate_id}: {status}"
                        f" ({g.metric_name}={g.actual_value}, threshold {g.operator} {g.threshold})"
                        f"{corner_label}"
                    )

        # corner_coverage：含完成順序
        cc = ecc.corner_coverage
        if cc is not None:
            total = len(cc.required_corners)
            done = len(cc.completed_pass) + len(cc.completed_fail)
            parts.append(f"Corner coverage: {done}/{total}")
            if cc.completed_pass or cc.completed_fail:
                completed_items: list[str] = []
                for c in cc.completed_pass:
                    completed_items.append(f"{c} (PASS)")
                for c in cc.completed_fail:
                    completed_items.append(f"{c} (FAIL)")
                parts.append(f"  completed: {', '.join(completed_items)}")
            if cc.pending:
                parts.append(f"  remaining: {len(cc.pending)} corners")

        # event_history：最近 5 筆
        recent_events = ecc.event_history[-5:]
        if recent_events:
            parts.append("Recent events:")
            for ev in recent_events:
                parts.append(f"  - [{ev.event_type}] {ev.detail}")

        # lineage_summary
        ls = ecc.lineage_summary
        if ls is not None:
            parts.append(f"Lineage: judgment={ls.ai_judgment or 'none'}, trace_complete={ls.trace_complete}")
            if ls.synthesis_roots:
                parts.append("  Roots: " + ", ".join(
                    f"{r.role}={r.path}" for r in ls.synthesis_roots
                ))

        return "\n".join(parts)

    # ─── proactive nudge（deterministic，不呼叫 LLM）──────────────────────

    def generate_nudge(self, ecc: ECCObject, stage: StageSpec) -> str:
        """根據 CornerCoverage + recommended_corner_order 產生下一步提示。

        不呼叫 LLM，純 deterministic。
        - 若無 corner_coverage 或 recommended_corner_order → 空字串
        - all corners complete + all pass → 恭喜完成訊息
        - all corners complete + 有 fail → 列出失敗 corner
        - 尚有 pending → 建議下一個 corner（按推薦順序）
        """
        cc = ecc.corner_coverage
        order = stage.recommended_corner_order

        if cc is None or not order:
            return ""

        done = set(cc.completed_pass) | set(cc.completed_fail)
        total = len(cc.required_corners)
        done_count = len(done)

        if cc.is_complete:
            if cc.is_all_pass:
                return (
                    f"所有 {total} 個 corner 全部通過，"
                    f"stage {stage.name} signoff 已滿足。"
                )
            fail_list = ", ".join(cc.completed_fail)
            return (
                f"全部 {total} 個 corner 已跑完，"
                f"但以下 corner 未通過：{fail_list}。"
                f"建議優先修正後重新執行。"
            )

        # 找推薦順序中下一個尚未完成的 corner
        next_corner: str | None = None
        for c in order:
            if c not in done:
                next_corner = c
                break

        # 若推薦順序中都完成了，找 required_corners 中的 pending
        if next_corner is None:
            pending = cc.pending
            next_corner = pending[0] if pending else None

        remaining = total - done_count
        if next_corner is not None:
            return (
                f"已完成 {done_count}/{total} 個 corner，"
                f"剩餘 {remaining} 個。"
                f"建議下一個跑 {next_corner}。"
            )

        return f"已完成 {done_count}/{total} 個 corner。"

    # ─── recent work history 格式化 ──────────────────────────────────────

    def _format_recent_work_history(
        self,
        payload: RecentWorkHistoryPayload,
    ) -> str:
        """將 RecentWorkHistoryPayload 格式化為 [Recent Work History] prompt 段落。

        payload 無資料時回傳空字串（caller 判斷要不要 fallback 到 _format_session_history）。
        """
        if not _has_recent_work_history(payload):
            return ""

        from datetime import datetime

        lines = ["[Recent Work History]"]

        # 今天的 immediate digest 放最上面，按 period_start ASC
        for imm in payload.today_immediates:
            start_hm = datetime.fromtimestamp(imm.period_start).strftime("%H:%M")
            end_hm = datetime.fromtimestamp(imm.period_end).strftime("%H:%M")
            date_str = datetime.fromtimestamp(imm.period_start).strftime("%Y-%m-%d")
            lines.append(f"[{date_str} 今天 {start_hm}-{end_hm}] {imm.llm_summary}")

        # 過去的 daily digest，按 date DESC（已在 loader 排好）
        for d in payload.daily_digests:
            lines.append(f"[{d.date}] {d.llm_summary}")

        return "\n".join(lines) + "\n\n"

    # ─── session history 格式化 ──────────────────────────────────────────

    def _format_session_history(self) -> str:
        """將 session_history 格式化為 prompt 段落。"""
        if not self._session_history:
            return ""

        lines = ["## Previous Session History"]
        for s in self._session_history:
            line = (
                f"- Session {s.session_id}: "
                f"stage={s.last_stage_id or 'none'}, "
                f"corners={s.corners_passed}pass/{s.corners_failed}fail/{s.corners_total}total, "
                f"gates={s.gate_pass_count}pass/{s.gate_fail_count}fail, "
                f"commands={s.command_count}"
            )
            if s.final_wns is not None:
                line += f", wns={s.final_wns}"
            if s.final_tns is not None:
                line += f", tns={s.final_tns}"
            lines.append(line)

        return "\n".join(lines) + "\n\n"

    # ─── prompt 組裝 ──────────────────────────────────────────────────────

    def _build_prompt(
        self,
        ecc_summary: str,
        gate_results: list[GateResult],
        stage: StageSpec,
    ) -> str:
        """組裝給 LLM 的完整 prompt。"""
        # gate results 文字
        gate_lines: list[str] = []
        for g in gate_results:
            status = "PASS" if g.passed else "FAIL"
            line = (
                f"- {g.gate_id}: {status}"
                f" ({g.metric_name} = {g.actual_value}, threshold {g.operator} {g.threshold})"
            )
            if g.corner:
                line += f" [corner: {g.corner}]"
            gate_lines.append(line)
        gate_text = "\n".join(gate_lines) if gate_lines else "(no gate results)"

        # recommended corner order 區段
        corner_order_text = ""
        if stage.recommended_corner_order:
            corner_order_text = (
                "## Recommended Corner Order\n"
                + ", ".join(stage.recommended_corner_order)
                + "\n\n"
            )

        # session history 區段
        history_text = self._format_session_history()
        profile_text = self._format_user_profile()
        team_text = self._format_team_snapshot(self._team_snapshot)
        last_session_text = self._format_last_session()

        return (
            "你是 IC 設計流程品質管控的 AI 助理。\n"
            "你的角色是分析 timing analysis 結果並提供清晰的解釋。\n"
            "請以繁體中文回答。\n"
            "\n"
            "## Current Stage\n"
            f"Stage: {stage.name} ({stage.id})\n"
            "\n"
            f"{profile_text}"
            f"{team_text}"
            f"{history_text}"
            f"{last_session_text}"
            "## ECC Object Summary\n"
            f"{ecc_summary}\n"
            "\n"
            f"{corner_order_text}"
            "## Quality Gate Evaluation Results\n"
            f"{gate_text}\n"
            "\n"
            "## Instructions\n"
            "Based on the above information:\n"
            '1. Determine if this is an "allow" (all gates passed) or "warn" (any gate failed) situation\n'
            '2. Determine severity: "info" (all pass), "warning" (minor fails), "critical" (signoff/tapeout blocked)\n'
            "3. Provide a clear explanation in 2-3 sentences, mentioning specific metric values and thresholds\n"
            "4. Suggest concrete next actions if any gate failed\n"
            "5. TREND ANALYSIS: If historical values are available for the same metric/corner,\n"
            "   compare current vs previous values. State whether the metric is improving,\n"
            "   degrading, or stable. Quantify the change (e.g., 'WNS improved by 42% from\n"
            "   -0.152ns to -0.087ns over 2 iterations').\n"
            "6. PROACTIVE NEXT-STEP: Based on the command history and corner coverage status,\n"
            "   suggest what the engineer should do next. Use the Recommended Corner Order\n"
            "   (if available) to suggest which corner to run next.\n"
            "7. CROSS-SESSION COMPARISON: If previous session history is available,\n"
            "   compare current metrics with previous sessions and note improvements or\n"
            "   regressions.\n"
            "\n"
            "Respond in JSON format only:\n"
            "{\n"
            '  "judgment": "allow" or "warn",\n'
            '  "severity": "info" or "warning" or "critical",\n'
            '  "explanation": "（繁體中文）...",\n'
            '  "suggestions": ["（繁體中文）...", "..."]\n'
            "}\n"
        )

    # ─── LLM 回應解析 ────────────────────────────────────────────────────

    def _parse_llm_response(self, raw: str) -> CompanionResponse | None:
        """嘗試從 LLM 回傳中解析 JSON CompanionResponse。"""
        # 嘗試擷取 ```json ... ``` 區塊
        m = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        text = m.group(1) if m else raw

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        try:
            return CompanionResponse(
                judgment=str(data.get("judgment", "warn")),
                explanation=str(data.get("explanation", "")),
                suggestions=[str(s) for s in data.get("suggestions", [])],
                severity=str(data.get("severity", "warning")),
            )
        except Exception:
            return None

    # ─── deterministic fallback ───────────────────────────────────────────

    def _deterministic_fallback(
        self,
        gate_results: list[GateResult],
        stage: StageSpec,
    ) -> CompanionResponse:
        """LLM 不可用時的 deterministic fallback。"""
        # 建立 gate_id → on_fail 對照表
        on_fail_map: dict[str, str] = {}
        for g in stage.quality_gates:
            on_fail_map[g.id] = g.on_fail

        failed = [g for g in gate_results if not g.passed]

        if not failed:
            # 全部通過
            lines = [f"{g.gate_id}: PASS" for g in gate_results]
            explanation = "All quality gates passed." if gate_results else "No gate results available."
            return CompanionResponse(
                judgment="allow",
                severity="info",
                explanation=explanation,
                suggestions=[],
            )

        # 有 fail → 判定 severity
        severity = "warning"
        for f in failed:
            on_fail = on_fail_map.get(f.gate_id, "")
            if "BLOCK_TAPEOUT" in on_fail:
                severity = "critical"
                break

        # explanation
        fail_lines = []
        for f in failed:
            corner_label = f" [{f.corner}]" if f.corner else ""
            fail_lines.append(
                f"{f.gate_id}: FAIL ({f.metric_name}={f.actual_value}, "
                f"threshold {f.operator} {f.threshold}){corner_label}"
            )
        explanation = "Failed gates: " + "; ".join(fail_lines)

        return CompanionResponse(
            judgment="warn",
            severity=severity,
            explanation=explanation,
            suggestions=[],
        )

    # ─── 主要 API ────────────────────────────────────────────────────────

    def judge_and_explain(
        self,
        ecc_summary: str,
        gate_results: list[GateResult],
        stage: StageSpec,
    ) -> CompanionResponse:
        """呼叫 LLM 做判定/解釋/建議，失敗時 fallback。"""
        prompt = self._build_prompt(ecc_summary, gate_results, stage)

        try:
            raw = self._llm_complete(prompt)
        except Exception:
            logger.warning("LLM call failed, using deterministic fallback")
            return self._deterministic_fallback(gate_results, stage)

        parsed = self._parse_llm_response(raw)
        if parsed is not None:
            return parsed

        logger.warning("LLM response parse failed, using deterministic fallback")
        return self._deterministic_fallback(gate_results, stage)

    # ─── chat query 問題分類 ──────────────────────────────────────────

    _LINEAGE_KEYWORDS = frozenset([
        "lineage", "blood", "branch", "mixed", "mismatch",
        "ancestor", "producer", "input", "output",
        "synthesis input", "formal_summary", "anchor",
        "血緣", "來源", "追溯",
    ])
    _ECC_KEYWORDS = frozenset([
        "corner", "coverage", "wns", "tns", "timing",
        "signoff", "sta_run", "sta_", " sta ",
        "pass", "fail", "gate",
        "metric", "threshold",
    ])

    def _classify_query(self, query: str) -> str:
        """依 keyword 將使用者問題分類為 lineage / ecc / general。"""
        q = f" {query.lower()} "  # 前後加空格，讓 " sta " 能匹配行首行尾
        has_lineage = any(kw in q for kw in self._LINEAGE_KEYWORDS)
        has_ecc = any(kw in q for kw in self._ECC_KEYWORDS)

        if has_lineage and not has_ecc:
            return "lineage"
        if has_ecc and not has_lineage:
            return "ecc"
        # 兩者都有或都沒有 → general
        return "general"

    # ─── chat query context 組裝 ──────────────────────────────────────

    @staticmethod
    def _format_latest_preexec_warning(ecc: ECCObject) -> str:
        """若最近一筆 command 有 warn/block，回傳 prompt 用的「當下警告」區塊；否則空字串。
        這個區塊會放在 prompt 最前面，讓 LLM 對「請解釋」「為什麼」這種模糊 query 時
        優先解釋這條警告，而不是抓歷史 profile 的 corner WNS / fail count 作答。
        若 lineage.checker 帶回 findings（包含具體 role/path/anchor），一併展開，
        讓 AI 能告訴使用者**哪一個 input 血緣壞掉**而非只說泛泛的規則。
        """
        if not ecc.command_history:
            return ""
        last = ecc.command_history[-1]
        if last.preexec_result not in ("warn", "block"):
            return ""
        msg = (last.preexec_message or "").strip()
        if not msg:
            return ""

        lines = [
            "## ⚠ Latest Pre-Exec Decision (most relevant to current query)",
            f"Command: `{last.command}`",
            f"Result : **{last.preexec_result}**",
            f"Reason : {msg}",
        ]

        # 把 checker findings 攤開印出，AI 直接照引用即可
        if last.preexec_findings:
            lines.append("")
            lines.append("Affected inputs (from lineage.checker, deterministic):")
            for f in last.preexec_findings:
                role = f.get("role") or "(unknown role)"
                path = f.get("path") or "(unknown path)"
                lines.append(f"- **{role}**: {path}")
                expected = f.get("expected_rtl_checksum")
                observed = f.get("observed_rtl_anchors") or []
                if expected:
                    lines.append(f"    expected RTL anchor : {expected[:16]}…")
                if observed:
                    obs_short = ", ".join(a[:16] + "…" for a in observed if a)
                    lines.append(f"    observed RTL anchors: [{obs_short}]")
                fmsg = (f.get("message") or "").strip()
                if fmsg and fmsg != msg:
                    lines.append(f"    note: {fmsg}")

        return "\n".join(lines) + "\n\n"

    def _build_query_context(self, query_type: str, ecc: ECCObject) -> str:
        """依問題分類選擇性注入 context，避免無關資料汙染 AI 回答。"""
        parts: list[str] = []

        if query_type == "lineage":
            # 只注入 lineage 相關：command_history + lineage_summary
            ls = ecc.lineage_summary
            if ls is not None:
                parts.append(f"Lineage: judgment={ls.ai_judgment or 'none'}, trace_complete={ls.trace_complete}")
                if ls.synthesis_roots:
                    parts.append("  Roots: " + ", ".join(
                        f"{r.role}={r.path}" for r in ls.synthesis_roots
                    ))
            recent_cmds = ecc.command_history[-5:]
            if recent_cmds:
                parts.append("Recent commands:")
                for c in recent_cmds:
                    line = f"  - {c.command} (tool={c.tool_name}, result={c.preexec_result})"
                    if c.postexec_summary:
                        line += f" → {c.postexec_summary}"
                    parts.append(line)
            return "\n".join(parts) if parts else "(No lineage data available)"

        if query_type == "ecc":
            # 完整 ECC 摘要（含 metrics, gates, corners）
            return self.summarize_ecc(ecc)

        # general：精簡版（stage + 最近命令 + corner 進度，不含完整 metrics/lineage）
        brief: list[str] = []
        brief.append(f"Session: {ecc.session_id}")
        brief.append(f"Stage: {ecc.current_stage_id or '(none)'}")
        recent_cmds = ecc.command_history[-3:]
        if recent_cmds:
            brief.append("Recent commands:")
            for c in recent_cmds:
                brief.append(f"  - {c.command} (tool={c.tool_name})")
        cc = ecc.corner_coverage
        if cc is not None:
            total = len(cc.required_corners)
            done = len(cc.completed_pass) + len(cc.completed_fail)
            brief.append(f"Corner coverage: {done}/{total}")
        ls = ecc.lineage_summary
        if ls is not None:
            brief.append(f"Lineage: judgment={ls.ai_judgment or 'none'}")
        return "\n".join(brief)

    def _query_instructions(self, query_type: str) -> str:
        """依問題類型產生指示區段。"""
        base = (
            "重要：只引用與使用者問題直接相關的資料。\n"
            "- 如果 prompt 開頭有「⚠ Latest Pre-Exec Decision」區塊，且使用者問題是「請解釋」「為什麼」「怎麼回事」這類**沒有明確主題**的問題，請以該區塊的 Reason 為主軸回答，不要引用 Your Profile / Team Status / Session History 的歷史 corner / WNS / fail count 資料。\n"
            "- 如果問題是關於 lineage / blood lineage / 血緣，不要引用 STA corner、WNS、TNS 等 timing 資料。\n"
            "- 如果問題是關於 corner coverage / timing / signoff，不要引用 lineage anchor、branch、producer chain 等資料。\n"
            "- 如果不確定，只提供直接回答問題的最少資訊。\n"
        )
        if query_type == "lineage":
            return base + "使用者問的是 lineage / 血緣相關問題。只引用 lineage 資料回答。\n"
        if query_type == "ecc":
            return base + "使用者問的是 ECC / timing / corner 相關問題。引用 ECC 資料回答。\n"
        return base

    # ─── chat query API ────────────────────────────────────────────────

    def answer_query(
        self,
        query: str,
        ecc: ECCObject,
        conversation: ConversationBuffer | None = None,
        related_history: list[Any] | None = None,
        other_terminals: list[dict] | None = None,
        recent_work_history: RecentWorkHistoryPayload | None = None,
    ) -> str:
        """
        回答使用者的自然語言問題。

        流程：
        1. 依問題分類（lineage / ecc / general）選擇性注入 context
        2. 組裝 prompt：context + related history + other terminals + conversation history + 使用者問題
        3. 呼叫 LLM（或 deterministic fallback）
        4. 記錄 user/assistant turn 到 ConversationBuffer
        5. 回傳回答文字（保證非空）

        conversation: 外部傳入的 per-stream ConversationBuffer。
                      None 時使用內部 buffer（向下相容）。
        related_history: HybridSearch 回傳的 SearchResult list。
        other_terminals: StreamManager.get_other_streams_summary() 回傳的 dict list。
        """
        # 優先用外部傳入的 buffer，沒傳就用內部的
        conv = conversation if conversation is not None else self._conversation

        query_type = self._classify_query(query)
        context = self._build_query_context(query_type, ecc)
        conv_text = conv.get_history_text()
        instructions = self._query_instructions(query_type)

        # 通用 context（所有分類都注入）
        profile_text = self._format_user_profile()
        team_text = self._format_team_snapshot(self._team_snapshot)
        # Recent Work History 與 Previous Session History 互斥，優先使用前者
        if recent_work_history is not None and _has_recent_work_history(recent_work_history):
            history_text = self._format_recent_work_history(recent_work_history)
        else:
            history_text = self._format_session_history()
        last_session_text = self._format_last_session()
        other_terminals_text = self._format_other_terminals(other_terminals)
        related_history_text = self._format_related_history(related_history)

        # 最近一筆 pre_exec warn/block 的訊息（沒有則為空）放在 prompt 最前面，
        # 防止 LLM 對「請解釋」「為什麼」這類模糊 query 抓歷史 profile 數據作答。
        latest_warning_text = self._format_latest_preexec_warning(ecc)

        prompt = (
            "你是嵌入 EDA terminal 的 IC 設計助理。請以繁體中文回答。\n"
            "以下是目前的 session 狀態：\n\n"
            f"{latest_warning_text}"
            f"{context}\n\n"
            f"{profile_text}"
            f"{team_text}"
            f"{history_text}"
            f"{last_session_text}"
            f"{other_terminals_text}"
            f"{related_history_text}"
            f"{conv_text}"
            f"使用者問題：{query}\n\n"
            f"{instructions}"
            "\n"
            "請以繁體中文簡潔回答，聚焦在與問題直接相關的可操作資訊。"
        )

        # 記錄 user turn（在 LLM 呼叫前記錄，確保 prompt 包含歷史但不含本次）。
        # companion 路徑沒有經過 TaskScope 最終 Gate，回合只供本機顯示。
        conv.add_user(query, provenance=PROVENANCE_COMPANION_LOCAL)

        try:
            raw = self._llm_complete(prompt)
            if raw and raw.strip():
                answer = raw.strip()
                conv.add_assistant(answer, provenance=PROVENANCE_COMPANION_LOCAL)
                return answer
        except Exception as exc:
            logger.warning("LLM answer_query failed: %s", exc)

        # Deterministic fallback：回傳 context 摘要（含 cwd／命令摘要，只能留在本機）
        fallback = self._deterministic_query_fallback(query, context)
        conv.add_assistant(fallback, provenance=PROVENANCE_COMPANION_LOCAL)
        return fallback

    def _deterministic_query_fallback(self, query: str, ecc_summary: str) -> str:
        """LLM 不可用時的 fallback：回傳 ECC Object 摘要。"""
        return f"[Deterministic fallback — LLM unavailable]\n{ecc_summary}"

    # ─── other terminals 格式化 ────────────────────────────────────────────

    def _format_other_terminals(self, other_terminals: list[dict] | None) -> str:
        """格式化其他 terminal 的狀態摘要。空或 None 回傳空字串。"""
        if not other_terminals:
            return ""

        lines = ["[Other Active Terminals]"]
        for t in other_terminals:
            tid = t.get("terminal_id", "?")
            stage = t.get("stage") or "unknown"
            corner = t.get("corner") or ""
            block = t.get("block_name") or ""
            gate = t.get("last_gate_status") or ""
            idle = t.get("is_idle", False)

            if idle:
                lines.append(f"- {tid}: idle")
            else:
                parts = [stage]
                if block:
                    parts.insert(0, block)
                if corner:
                    parts.append(corner)
                if gate:
                    parts.append(f"— {gate}")
                lines.append(f"- {tid}: {' / '.join(parts)}")

        return "\n".join(lines) + "\n\n"

    # ─── related history 格式化 ────────────────────────────────────────────

    def _format_related_history(self, related_history: list[Any] | None) -> str:
        """格式化 hybrid search 回傳的相關歷史。空或 None 回傳空字串。"""
        if not related_history:
            return ""

        from datetime import datetime

        lines = [
            "[Related History]",
            "以下是過去的相關討論（供參考，非當前 session）：",
        ]
        for r in related_history:
            ts = getattr(r, "timestamp", 0.0)
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            tid = getattr(r, "terminal_id", "default")
            tid_str = f", {tid}" if tid and tid != "default" else ""

            content = getattr(r, "content", "")
            # 截斷到前 200 字
            content_preview = content[:200].replace("\n", " ")
            if len(content) > 200:
                content_preview += "..."

            lines.append(f"- [{dt}{tid_str}] {content_preview}")

        return "\n".join(lines) + "\n\n"

    # ─── last session 格式化 ──────────────────────────────────────────────

    def _format_last_session(self) -> str:
        """將 SessionContinuity 格式化為 prompt 段落。"""
        s = self._last_session
        if s is None:
            return ""

        lines = ["## Last Session"]
        lines.append(f"Session: {s.session_id}, stage={s.last_stage_id or 'none'}")

        if s.corners_passed:
            lines.append(f"Passed corners: {', '.join(s.corners_passed)}")
        if s.corners_failed:
            lines.append(f"Failed corners: {', '.join(s.corners_failed)}")
        if s.corners_pending:
            lines.append(f"Pending corners: {', '.join(s.corners_pending)}")

        if s.metric_trends:
            lines.append("Metric trends:")
            for name, values in s.metric_trends.items():
                trend = " <- ".join(str(v) for v in values)
                lines.append(f"  - {name}: {trend}")

        if s.recent_commands:
            lines.append(f"Last commands: {', '.join(s.recent_commands)}")

        if s.failed_gates:
            lines.append(f"Failed gates: {', '.join(s.failed_gates)}")

        if s.last_ai_suggestion:
            lines.append(f"Last AI suggestion: {s.last_ai_suggestion}")

        return "\n".join(lines) + "\n\n"

    # ─── user profile 格式化 ──────────────────────────────────────────────

    def _format_user_profile(self) -> str:
        """將 EnrichedUserProfile 格式化為 prompt 段落。"""
        p = self._user_profile
        if p is None:
            return ""

        # 空資料
        if p.total_sessions == 0 and p.total_commands == 0:
            return (
                "## Your Profile\n"
                "No sufficient history data yet. Profile will be available after a few sessions.\n\n"
            )

        lines = [f"## Your Profile (last {p.event_window_days} days)"]
        lines.append(f"Sessions: {p.total_sessions} | Commands: {p.total_commands}")

        duration_part = f"Avg session: {p.avg_session_duration_min} min"
        if p.typical_work_hours:
            hours_str = ", ".join(str(h) for h in p.typical_work_hours)
            duration_part += f" | Peak hours: {hours_str}"
        lines.append(duration_part)

        if p.bottleneck_corners:
            lines.append("")
            lines.append("Bottleneck corners (most gate failures):")
            for bc in p.bottleneck_corners:
                lines.append(f"- {bc.corner}: {bc.fail_count} fails")

        if p.corner_first_run_wns:
            lines.append("")
            lines.append("First-run WNS per corner:")
            for cw in p.corner_first_run_wns:
                lines.append(f"- {cw.corner}: {cw.first_wns_ns:+.3f} ns")

        if p.most_used_tools:
            lines.append("")
            tools_str = ", ".join(
                f"{t.tool_name} ({t.run_count})" for t in p.most_used_tools
            )
            lines.append(f"Most used tools: {tools_str}")

        return "\n".join(lines) + "\n\n"

    # ─── team snapshot 格式化 ────────────────────────────────────────────

    def _format_team_snapshot(self, team: TeamSnapshot | None) -> str:
        """將 TeamSnapshot 格式化為 prompt 段落。"""
        if team is None or not team.members:
            return "## Team Status\nNo team data available.\n\n"

        lines = [f"## Team Status ({team.project_id}, {len(team.members)} engineers)"]
        lines.append("")

        for m in team.members:
            # last_active 相對時間
            lines.append(f"{m.user_id}")
            if m.block_names:
                lines.append(f"  Blocks: {', '.join(m.block_names)}")
            lines.append(f"  Recent session: {m.recent_commands} commands")
            if m.corners_run:
                lines.append(f"  Corners run: {', '.join(m.corners_run)}")
            passed_str = ", ".join(m.corners_passed) if m.corners_passed else "(none)"
            lines.append(f"  Passed: {passed_str}")
            failed_str = ", ".join(m.corners_failed) if m.corners_failed else "(none)"
            lines.append(f"  Failed: {failed_str}")
            if m.latest_wns:
                wns_parts = [f"{c}={v:+.3f}" for c, v in sorted(m.latest_wns.items())]
                lines.append(f"  Latest WNS: {', '.join(wns_parts)}")
            lines.append("")

        return "\n".join(lines) + "\n"

    # ─── block_name AI fallback（Layer 3）─────────────────────────────────

    def infer_block_name(self, command: str, cwd: str) -> str | None:
        """Layer 3 AI fallback：讓 LLM 從 command + cwd 推斷 IC block name。

        回傳 block name 字串，或 None 表示無法推斷。
        不拋出 exception，失敗靜默記錄 warning。
        """
        prompt = (
            "你是 IC 設計專家。請從以下 command 和工作目錄中推斷 IC block 名稱。\n"
            "只回傳 block name（一個單詞），不要任何解釋。\n"
            "如果無法確定，只回傳 UNKNOWN。\n\n"
            f"Command: {command}\n"
            f"Working directory: {cwd}\n"
        )
        try:
            raw = self._llm_complete(prompt)
            if raw and raw.strip() and raw.strip().upper() != "UNKNOWN":
                # 取第一行、第一個 token，避免 LLM 回傳多餘文字
                candidate = raw.strip().splitlines()[0].strip()
                if candidate and candidate.upper() != "UNKNOWN":
                    return candidate
        except Exception as exc:
            logger.warning("AI block_name inference failed: %s", exc)
        return None
