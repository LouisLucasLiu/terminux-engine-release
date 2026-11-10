# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent System Prompt 組裝。

`@@` chat query 的 system prompt builder。獨立於 ai_companion.py（後者保留
single-shot 路徑用），兩條 prompt 邏輯暫時並存（已知限制：待 Agent 路徑穩定後
可考慮統一）。

conversation_history 雖在 signature 內，但**不**會放進 system prompt——它
由 caller 注入到 messages list 內（user query 之前），這樣 AgentRunner 的
tool-use loop 才能正確 append assistant/tool 結果。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

from terminux_engine.core.preferences import (
    DEFAULT_LANGUAGE,
    language_native_name,
)
from terminux_engine.llm.agent_models import CommandBatch

_log = logging.getLogger(__name__)

# _HEADER / ASYNC_TRIAGE_SYSTEM_PROMPT 內寫死的語言指示句，依 preferred language
# 動態替換（見 _apply_language）。
_HEADER_LANG_SENTINEL = "- 全程用繁體中文回答"
_TRIAGE_LANG_SENTINEL = "- 用繁體中文回答"


def _language_directive(language: str) -> str:
    """偏好語言 → 強指示句（雙語確保任何 backend 都能正確 follow）。"""
    native = language_native_name(language)
    return f"- 一律使用「{native}」回答使用者（always respond to the user in {native}）"


def _apply_language(prompt: str, sentinel: str, language: str) -> str:
    return prompt.replace(sentinel, _language_directive(language))


_HEADER = """\
你是 Terminux AI Companion，一個 IC 設計工程師的終端伴侶。

你可以使用 tools 查詢：
- 歷史事件與 metric 趨勢
- 血緣帳本（artifact producer chain）
- 當前 session 的 ECC 狀態
- 同 project 其他工程師的進度

回答原則：
- 先查資料再回答，不要猜
- 引用具體數據（哪個 corner、什麼數值、什麼時間、哪個 session_id）
- 給出可執行的建議，不要只描述問題
- 如果資料不足以做判斷，明確說不足在哪
- 全程用繁體中文回答
- 當問題涉及 orchestration run、DQE、report、lineage 或 domain outcome，且使用者
  提供 `orchestration_run_id` 時，回答前必須先呼叫 SAFE 的
  `query_orchestration_evidence`。只可依其 bounded result 回答；若 availability
  為 unavailable 或 partial，必須明示，不得以 run_command、filesystem search
  或猜測代替。
- 當使用者的問題看起來是在引用之前的系統警告或檢查結果時：
  · 先查看 [最近的檢查結果] section
  · 如果 section 中有相關的 lineage_check 或 gate_eval，直接基於該結果回答
  · 如果需要更多細節，使用 query_events 或 query_ledger tool 追查
- 當使用者問「問題」「錯誤」「怎麼修」但沒有明確指出是什麼時：
  · 最近的 lineage_check / gate_eval 結果通常就是使用者在問的東西
  · 不要忽略這些結果而去報告「無資料」
- 回答 lineage 問題時：
  · 明確指出哪個 artifact 有問題、它來自哪個 branch、預期應來自哪個 branch
  · 給出具體可執行的修正步驟（替換哪個檔案、或重跑哪個上游 job）
  · 如果需要查血緣，用 query_ledger tool 追溯 producer chain
- 當你看到 lineage 問題（MIXED_LINEAGE / INSUFFICIENT_EVIDENCE / BRANCH_MISMATCH）時，
  主動進行調查，不要只複述系統訊息：
  · 用 query_ledger 查可疑 artifact 的 producer chain（誰產的、什麼時候、用什麼 input）
  · 用 read_file_head 讀可疑 artifact 的前 50 行，看內容是否有線索（header 註解、
    timestamp、commit hash、上游 RTL 版本字串等）
  · 比對不同 artifact 的路徑，注意 branch tag（branch_A / branch_B 等目錄名稱差異）
  · 如果發現路徑中 branch 不一致，即使系統報的是 INSUFFICIENT_EVIDENCE，也要主動指出
  · 給出具體的修正步驟：該換哪個檔案、從哪個 branch 取、或該重跑哪個上游 job
- 禁止在沒有使用任何 tool 的情況下回答「我無法確定」「我沒有足夠資訊」「無法指出」。
  在這樣回答之前，你必須至少嘗試一個相關 tool：
  · 使用者問特定 artifact 的問題 → 先用 query_ledger 查血緣
  · 使用者問檔案內容或具體位置 → 先用 query_ledger 查路徑，再用 read_file_head 讀內容
  · 使用者問歷史或趨勢 → 先用 query_events 或 query_metric_trend 查
  只有在 tool 回傳的結果確實不足時，才可以說「目前資訊不足以確定」，並說明你查了什麼、缺什麼。
- 當 [最近的檢查結果] 有多筆 lineage_check 時：
  · MIXED_LINEAGE 比 INSUFFICIENT_EVIDENCE 更嚴重、更具體，優先回應
  · 不要只看第一筆就回答，先掃過所有檢查結果
  · 如果同時有 MIXED_LINEAGE 和 INSUFFICIENT_EVIDENCE，先解釋 MIXED_LINEAGE 的問題，
    再提及 INSUFFICIENT_EVIDENCE 作為額外發現
"""


def build_agent_system_prompt(
    *,
    ecc_state: dict | None = None,
    recent_checks: list[dict] | None = None,
    session_history: list[dict] | None = None,
    user_profile: dict | None = None,
    team_snapshot_summary: str | None = None,
    playbook_context: str | None = None,
    conversation_history: list[dict] | None = None,
    conversation_history_text: str | None = None,
    deep_analysis_summary: str | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """組裝 Agent system prompt。

    legacy `conversation_history: list[dict]` 參數保留**但不**渲染——
    原本預留，後來因 agent_runner.py 不修改而無法做
    messages-prefix 注入，改用 `conversation_history_text`：
    caller 直接傳 ConversationBuffer.get_history_text() 的字串，本函數
    渲染成 [對話歷史] section（option B）。

    deep_analysis_summary：Dreaming Agent 上次離線分析結論。
    """
    from terminux_engine.llm.task_scope import load_profile, build_system_prompt
    task_profile = load_profile()
    if task_profile is not None:
        return build_system_prompt(task_profile, conversation_history_text)

    _ = conversation_history  # 故意未使用；legacy 預留

    sections: list[str] = [_apply_language(_HEADER, _HEADER_LANG_SENTINEL, language)]

    sections.append(_section("[當前 Session 狀態]", _format_ecc_state(ecc_state)))
    recent_block = _format_recent_checks(recent_checks)
    if recent_block:
        sections.append(recent_block)
    sections.append(_section("[使用者背景]", _format_user_profile(user_profile)))
    sections.append(_section("[團隊狀態]", team_snapshot_summary))
    sections.append(_section("[近期 Session 摘要]", _format_session_history(session_history)))
    sections.append(_section("[Playbook 參考]", playbook_context))
    if conversation_history_text and conversation_history_text.strip():
        sections.append(_section("[對話歷史]", conversation_history_text.strip()))
    if deep_analysis_summary:
        sections.append(_format_deep_analysis_section(deep_analysis_summary))

    return "\n\n".join(sections).rstrip() + "\n"


def _format_deep_analysis_section(summary: str) -> str:
    return (
        "[離線分析摘要]\n"
        "（以下是你上次離線分析的結論，可作為本次 session 的背景參考）\n\n"
        f"{summary}"
    )


def _format_recent_checks(checks: list[dict] | None) -> str | None:
    """[最近的檢查結果] section。

    優先用結構化 findings（category / artifact / expected / suggestion）渲染；
    若 event 沒帶 findings（舊 event），fallback 用自由文字 message。
    """
    if not checks:
        return None
    lines = [
        "[最近的檢查結果]",
        "以下是最近幾分鐘內的系統檢查結果，使用者的提問很可能與這些結果有關：",
        "",
    ]
    for c in checks:
        if not isinstance(c, dict):
            continue
        et = c.get("event_type", "?")
        when = c.get("time", "")
        when_part = f"[{when}] " if when else ""
        if et == "lineage_check":
            status = c.get("status") or c.get("judgment") or "?"
            findings = c.get("findings") or []
            lines.append(f"- {when_part}lineage_check: {status}")
            if findings:
                lines.append("  發現:")
                for f in findings:
                    if not isinstance(f, dict):
                        continue
                    lines.extend(_render_finding(f))
            else:
                # 舊 event fallback：用 message 文字
                msg = c.get("message", "")
                if msg:
                    lines.append(f"  {msg}")
                suspicious = c.get("suspicious") or []
                if suspicious:
                    lines.append(
                        f"  可疑 artifact: {', '.join(map(str, suspicious))}"
                    )
        elif et == "gate_eval":
            status = c.get("status", "?")
            corner = c.get("corner", "")
            corner_part = f"corner {corner}, " if corner else ""
            metric = c.get("metric_name") or c.get("gate_id", "")
            actual = c.get("actual")
            actual_part = f"{metric} = {actual}" if actual is not None else metric
            lines.append(
                f"- {when_part}gate_eval: {status} — {corner_part}{actual_part}".rstrip(
                    " —"
                )
            )
        else:
            lines.append(f"- {when_part}{et}: {c.get('message', '')}")
    lines.append("")
    lines.append("當使用者問「問題在哪」「怎麼修」等模糊問題時，優先基於這些檢查結果回答。")
    return "\n".join(lines)


def _render_finding(f: dict) -> list[str]:
    """單一 finding 渲染成 prompt 的 bullet list（縮排 4 空白）。"""
    cat = f.get("category") or "UNKNOWN"
    artifact = f.get("artifact") or ""
    role = f.get("role")
    sev = f.get("severity")
    head = f"    • {cat}"
    if artifact:
        head += f": {artifact}"
    if role:
        head += f" (role: {role})"
    if sev:
        head += f" [{sev}]"
    out = [head]
    # context 摘要：command + cwd 等
    ctx = f.get("context") or {}
    if isinstance(ctx, dict) and ctx:
        ctx_parts = [f"{k}={v}" for k, v in ctx.items() if v][:4]
        if ctx_parts:
            out.append(f"      context: {', '.join(ctx_parts)}")
    # expected vs actual
    expected = f.get("expected")
    actual = f.get("actual_anchors")
    if expected:
        out.append(f"      expected: {expected}")
    if actual:
        actual_preview = ", ".join(str(a)[:16] for a in actual[:3])
        out.append(f"      actual: {actual_preview}")
    sugg = f.get("suggestion")
    if sugg:
        out.append(f"      建議: {sugg}")
    return out


# ── Section formatters ───────────────────────────────────────────────────────

def _section(title: str, body: str | None) -> str:
    """每個 section 統一格式：title + body 或「無資料」。"""
    if body is None or not str(body).strip():
        return f"{title}\n無資料"
    return f"{title}\n{body}"


def _format_ecc_state(state: dict | None) -> str | None:
    if not state:
        return None
    if state.get("active") is False:
        return "無 active session"
    try:
        keep_keys = (
            "session_id",
            "stage",
            "tool_name",
            "block_name",
            "work_directory",
            "corners_done",
            "corners_pending",
            "latest_metrics",
            "gate_results",
            "command_count",
        )
        compact = {k: state.get(k) for k in keep_keys if k in state}
        return json.dumps(compact, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        _log.warning("agent_prompt: ecc_state serialization failed: %s", e)
        return str(state)[:500]


def _format_user_profile(profile: dict | None) -> str | None:
    if not profile:
        return None
    try:
        return json.dumps(profile, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(profile)[:500]


# ── Dreaming deep_analysis 用 prompt ───────────────────


_DEEP_ANALYSIS_SYSTEM_PROMPT_TEMPLATE = """你是 Terminux AI Companion，正在進行離線深度分析。

目標使用者：{user_id}
分析範圍：最近 7 天
當前時間：{current_time}

請自行使用 tools 調查以下方向：
1. 跨 session 的 metric 趨勢（WNS / TNS / NVP），是否有持續惡化或改善的 pattern
2. 重複出現的問題是否有共同 root cause 跡象
3. 和團隊其他成員在同 block 的進度比對
4. 是否有值得 PM 注意的進展或風險
5. 使用者的工作節奏是否有異常（例如深夜大量操作、同一 corner 過多迭代）

分析規則：
- 用 tools 查詢真實資料，不要猜測
- 每個發現都要有具體數據支持
- 如果資料不足以做判斷，明確說明不足在哪

輸出格式（嚴格遵守）：
## 關鍵發現
（最多 3 點，每點一句話 + 支持數據）

## 風險項
（如有，最多 2 點）

## 建議下一步
（最多 3 點可執行的建議）

## PM 摘要
（一段話，50 字以內，適合 PM 快速掃過）

若分析後發現無特別值得報告的內容，輸出：
## 關鍵發現
最近 7 天運作正常，無異常 pattern。

不要輸出空報告。"""


def build_deep_analysis_system_prompt(user_id: str, current_time: str) -> str:
    """填入 user_id / current_time，回傳 dreaming agent 的 system prompt。"""
    return _DEEP_ANALYSIS_SYSTEM_PROMPT_TEMPLATE.format(
        user_id=user_id or "unknown",
        current_time=current_time or "",
    )


def build_deep_analysis_query(user_id: str) -> str:
    """Dreaming agent 的 user message。"""
    return f"請對使用者 {user_id or 'unknown'} 進行最近 7 天的深度分析。"


# ── async triage 用 prompt ─────────────────────────────


ASYNC_TRIAGE_SYSTEM_PROMPT = """你是 Terminux AI Companion，一個 IC 設計工程師的終端伴侶。

你正在背景分析使用者的操作序列。

你的任務：
1. 評估這個操作序列在當前 context 下是否有值得注意的觀察
2. 如果是 routine operation、符合預期且無異常，回覆 [NO_ACTION]
3. 如果你發現以下任何情況，使用 tools 查詢更多資料後給出具體建議：
   - 檔案操作可能影響血緣完整性（複製、移動、覆蓋關鍵 artifact）
   - metric 趨勢異常（連續惡化、突然 regression）
   - 操作順序與 playbook 建議不符
   - 到達 milestone 或值得記錄的進展
   - 其他工程師的經驗可能有參考價值
   - 任何你認為對使用者有價值的觀察

回覆規則：
- 若判定 routine operation：只回覆 [NO_ACTION]，不加任何文字
- 若有建議：直接給出觀察和建議，簡潔具體，不超過 200 字
- 用繁體中文回答
- 引用具體數據支持你的觀察"""


def build_async_triage_system_prompt(language: str = DEFAULT_LANGUAGE) -> str:
    """ASYNC_TRIAGE_SYSTEM_PROMPT 的語言化版本（背景 nudge 也 follow 偏好語言）。"""
    return _apply_language(
        ASYNC_TRIAGE_SYSTEM_PROMPT, _TRIAGE_LANG_SENTINEL, language
    )


def build_async_triage_prompt(batch: CommandBatch) -> str:
    """把 CommandBatch 轉成 async Agent 的 user message。"""
    lines: list[str] = []
    if batch.commands:
        span_sec = max(0.0, batch.commands[-1].timestamp - batch.commands[0].timestamp)
        lines.append(f"使用者在最近 {span_sec:.0f} 秒內執行了以下操作序列：\n")
        for i, bc in enumerate(batch.commands, 1):
            try:
                ts = _dt.datetime.fromtimestamp(bc.timestamp).isoformat(timespec="seconds")
            except (ValueError, OSError, OverflowError):
                ts = ""
            tag = " [EDA]" if bc.is_eda_tool else ""
            cwd_part = f"  (cwd={bc.cwd})" if bc.cwd else ""
            lines.append(f"{i}. [{ts}]{tag} {bc.command}{cwd_part}")
    else:
        lines.append("（空 command batch）")

    lines.append("")
    lines.append("[當前 ECC 狀態]")
    if batch.ecc_snapshot:
        try:
            lines.append(
                json.dumps(batch.ecc_snapshot, ensure_ascii=False, indent=2, default=str)
            )
        except Exception:
            lines.append(str(batch.ecc_snapshot)[:500])
    else:
        lines.append("無 active session")

    lines.append("")
    lines.append("[最近事件]")
    if batch.recent_events:
        for ev in batch.recent_events[:5]:
            if isinstance(ev, dict):
                et = ev.get("event_type", "?")
                tts = ev.get("timestamp", "")
                summary = ev.get("payload_summary", "")
                lines.append(f"- [{tts}] {et}{(': ' + summary) if summary else ''}")
            else:
                lines.append(f"- {ev}")
    else:
        lines.append("無資料")

    lines.append("")
    lines.append("請評估這個操作序列。")
    return "\n".join(lines)


def _format_session_history(history: list[dict] | None) -> str | None:
    if not history:
        return None
    lines: list[str] = []
    for i, entry in enumerate(history[:5], 1):
        if not isinstance(entry, dict):
            lines.append(f"{i}. {entry}")
            continue
        sid = entry.get("session_id", "?")
        stage = entry.get("last_stage_id") or entry.get("stage") or "?"
        when = entry.get("created_at") or entry.get("timestamp") or ""
        passed = entry.get("corners_passed", 0)
        failed = entry.get("corners_failed", 0)
        total = entry.get("corners_total", 0)
        lines.append(
            f"{i}. {sid} stage={stage} corners={passed}/{total} pass, "
            f"{failed} fail, t={when}"
        )
    return "\n".join(lines)
