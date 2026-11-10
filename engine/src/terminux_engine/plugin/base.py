# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ══════════════════════════════════════════════════════════════════════════════
# 基礎 API Protocol
# 以下 Protocol 由 engine 在執行時注入實作，plugin 作者只需知道介面。
# ══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class EccQueryApi(Protocol):
    """
    ECC（Engineer Command Chronicle）查詢介面。
    提供歷史事件查詢能力，供 perception.py 或 action.py 做額外查詢。
    """

    def query(
        self,
        member_id:      str | None = None,
        block:          str | None = None,
        tool_signature: str | None = None,
        limit:          int        = 10,
    ) -> list[dict]:
        """
        查詢歷史 ShellEvent，回傳符合條件的事件 list（dict 格式）。
        所有參數均為選填過濾條件（AND 關係）。
        無符合結果時回傳空 list，不拋出 exception。

        member_id：過濾特定成員的事件。
        block：過濾特定 block 的事件。
        tool_signature：過濾特定 EDA tool 的事件（例如 "primetime"）。
        limit：最多回傳筆數，預設 10，clamp [1, 100]。
        """
        ...


@runtime_checkable
class LlmApi(Protocol):
    """
    LLM 呼叫介面。
    供 action.py 在執行時呼叫 LLM 進行分析或生成。
    注意：此為同步介面（action.py 為同步函式）。
    """

    def query(self, prompt: str, data: dict) -> "LlmQueryResult":
        """
        呼叫 LLM 並回傳結果。
        prompt：自然語言描述的任務說明。
        data：傳入 LLM 的結構化資料（會序列化為 JSON 附加於 prompt）。
        失敗時回傳 LlmQueryResult(content="")，不拋出 exception。
        """
        ...


@runtime_checkable
class NotifyApi(Protocol):
    """
    Notification 介面。
    供 action.py 送出通知給工程師。
    """

    def inline(self, content: str) -> None:
        """
        在工程師的 terminal 顯示 inline 文字通知。
        空字串：跳過，不發送。
        即時送出（不緩衝）。
        """
        ...

    def sidebar(self, title: str, content: str) -> None:
        """
        送出帶有標題與內容的 sidebar 通知（Option B，目前 engine 跳過此呼叫）。
        API 預留，未來啟用時不需修改 plugin。
        """
        ...


@runtime_checkable
class ExecutorApi(Protocol):
    """
    命令執行介面。
    供 action.py 建議或執行 shell 命令。
    需要 Plugin 的 permission 達到對應等級，否則靜默跳過。
    """

    def suggest(self, command: str, reason: str) -> None:
        """
        建議一個 shell 命令，工程師確認後才執行。
        需要 Plugin permission >= SUGGEST。
        授權不足：靜默跳過，不通知 plugin。
        command：要執行的 shell 命令字串。
        reason：說明為什麼建議此命令（顯示給工程師看）。
        """
        ...

    def exec(self, command: str, reason: str) -> None:
        """
        自動執行一個 shell 命令（不需工程師確認）。
        需要 Plugin permission == AUTO_EXEC。
        授權不足：靜默跳過，不通知 plugin。
        command：要執行的 shell 命令字串。
        reason：說明為什麼執行此命令（記錄於 log）。
        """
        ...


# ══════════════════════════════════════════════════════════════════════════════
# LLM 回傳型別
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class LlmQueryResult:
    """
    LlmApi.query() 的回傳型別。
    content：LLM 完整回覆文字。
    """
    content: str

    @property
    def detail(self) -> str:
        """
        相容 Plugin 範例程式碼中 diagnosis.detail 的存取方式。
        回傳 content。
        """
        return self.content


# ══════════════════════════════════════════════════════════════════════════════
# Context Protocol
# Plugin 函式的 context 參數型別。
# ══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class PerceptionContext(Protocol):
    """
    perception.py 的 evaluate(event, context) 的 context 參數型別。

    使用範例：
        def evaluate(event: dict, context: PerceptionContext) -> bool:
            history = context.query_result
            if len(history) < 3:
                return False
            wns_values = [r["wns"] for r in history[-3:]]
            return all(wns_values[i] < wns_values[i-1] for i in range(1, 3))
    """

    @property
    def query_result(self) -> list[dict]:
        """
        context_query 的查詢結果（歷史事件 list，dict 格式）。
        唯讀，不應修改。
        無歷史資料時為空 list。
        """
        ...

    @property
    def ecc(self) -> EccQueryApi:
        """
        ECC 查詢 API，可在 evaluate() 內做額外查詢。
        """
        ...


@runtime_checkable
class ActionContext(Protocol):
    """
    action.py 的 execute(event, context) 的 context 參數型別。

    使用範例：
        def execute(event: dict, context: ActionContext) -> None:
            history = context.query_result
            wns_trend = [r["wns"] for r in history[-3:]]
            diagnosis = context.llm.query(
                prompt="分析以下三次 timing ECO 惡化的可能原因",
                data={"wns_trend": wns_trend}
            )
            context.notify.inline(f"⚠️ 連續 3 次 timing ECO 惡化：{wns_trend}")
            context.notify.sidebar(
                title="Timing ECO 趨勢分析",
                content=diagnosis.detail
            )
    """

    @property
    def query_result(self) -> list[dict]:
        """
        context_query 的查詢結果（歷史事件 list，dict 格式）。
        唯讀，不應修改。
        """
        ...

    @property
    def ecc(self) -> EccQueryApi:
        """ECC 查詢 API，可做額外查詢。"""
        ...

    @property
    def llm(self) -> LlmApi:
        """LLM 呼叫 API。"""
        ...

    @property
    def notify(self) -> NotifyApi:
        """Notification API（inline / sidebar）。"""
        ...

    @property
    def executor(self) -> ExecutorApi:
        """命令執行 API（suggest / exec）。"""
        ...


# ══════════════════════════════════════════════════════════════════════════════
# 公開 API 清單（plugin 作者可 import 的所有名稱）
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Context
    "PerceptionContext",
    "ActionContext",
    # API Protocol
    "EccQueryApi",
    "LlmApi",
    "NotifyApi",
    "ExecutorApi",
    # 回傳型別
    "LlmQueryResult",
]
