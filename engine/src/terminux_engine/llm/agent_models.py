# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Agent Intelligence — 核心資料模型（Pydantic v2）。

定義 Agent loop 過程中流通的所有 model：

- ToolCallRequest / ToolCallResult：單次 tool 呼叫的請求與結果
- ToolEvidence：紀錄 Agent 查了什麼、拿到什麼（audit trail）
- AgentResult：一次完整 Agent run 的結構化結果
- AgentTask：提交給 AgentTaskQueue 的任務描述（由 AgentTaskQueue 消費）

本模組不依賴任何 LLM provider 或 ToolRegistry，純資料層。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """LLM 回傳的單次 tool 呼叫請求。"""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    #: Provider-specific opaque token（Gemini 3.x 的 `thought_signature`）。
    #: Terminux 不解讀內容，只在把同一個 model turn 送回 provider 時原樣
    #: 附上；provider 不提供時為 None。
    thought_signature: Any | None = None


class ToolCallResult(BaseModel):
    """ToolRegistry.execute 的回傳結構。

    所有 failure（unknown tool / executor raise / timeout）都會收斂為
    success=False，不拋出例外，讓 AgentRunner loop 可以繼續。
    """

    call_id: str
    tool_name: str
    result: str = ""
    success: bool
    error: str | None = None
    elapsed_sec: float = 0.0
    #: outer timeout 到期。**additive** 欄位，既有 caller 不受影響。
    #:
    #: `ThreadPoolExecutor` 無法 kill 已啟動的 thread，所以 timeout 只代表
    #: 「我們不再等」，**不代表 action 已停止**——它可能稍後才完成並寫入
    #: 副作用。呼叫端因此不得把 timeout 當成「沒發生任何事」的普通失敗
    #:。
    timed_out: bool = False


class ToolEvidence(BaseModel):
    """Agent 在一次 run 中查詢過的 tool 與結果摘要（audit trail）。

    result_summary 由 caller 自行截斷（建議 500 chars），model 層不做限制，
    避免 caller 拿到的長字串被靜默截斷而難以除錯。
    """

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    success: bool = True


class AgentResult(BaseModel):
    """Agent 一次完整 run 的結果。

    語意：

    - 正常給出建議：answer 為字串，no_action=False
    - 判斷無需動作：no_action=True，answer 為 None
    - LLM/parse/timeout 等失敗：error 為非空字串，answer 為 None
    """

    answer: str | None = None
    no_action: bool = False
    evidence: list[ToolEvidence] = Field(default_factory=list)
    rounds: int = 0
    elapsed_sec: float = 0.0
    trigger_source: str = "chat_query"
    error: str | None = None
    #: 穩定的拒絕碼（例如 `RUN_SCOPE_DENIED:llm.agent_runner`）；只在 host gate 拒絕時填入，
    #: 供 Profile 模式把精確原因直接回給學員而不進 fallback。
    error_code: str | None = None


class AgentTask(BaseModel):
    """提交給 AgentTaskQueue 的任務描述。"""

    task_id: str
    query: str
    system_context: str
    trigger_source: str
    created_at_epoch: float
    timeout_sec: float = 45.0
    max_rounds: int = 5


# ─── CommandBuffer 相關 ─────────────────────────────────


class BufferedCommand(BaseModel):
    """CommandBuffer 中的單一 command 記錄。

    `is_eda_tool` 由 caller 注入的 is_eda_tool_fn 決定；若無，固定 False。
    """

    command: str
    timestamp: float
    is_eda_tool: bool = False
    cwd: str | None = None


class CommandBatch(BaseModel):
    """CommandBuffer flush 時產出的批次。

    ecc_snapshot / recent_events 是 flush 當下的快照；caller 後續用此 batch
    建構 async Agent prompt。
    """

    batch_id: str
    commands: list[BufferedCommand]
    ecc_snapshot: dict | None = None
    recent_events: list[dict] | None = None
    flushed_at: float
