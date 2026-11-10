# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol

from terminux_engine.core.ecc_manager import EccManager
from terminux_engine.core.plugin_manager import PluginManager
from terminux_engine.ecc.models import EventQuery, QueryOrder, ShellEvent
from terminux_engine.layer1.trigger import TriggeredPlugin

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Sentinel ───────────────────────────────────────────────────────────────────

_FIELD_NOT_FOUND = object()   # module-level 唯一實例


# ── LlmClient Protocol（layer2.llm 實作前的型別 placeholder）─────────────────

class LlmClient(Protocol):
    """layer2.llm.LlmClient 的 Protocol 定義，供型別檢查使用。"""
    async def evaluate_condition(
        self,
        condition: str,
        event: ShellEvent,
        query_result: list[dict],
    ) -> bool: ...


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class ParsedContextQuery:
    """
    從 TriggeredPlugin.context_query dict 解析而來。
    所有欄位皆選填，未出現在 dict 中的欄位使用預設值。
    """
    same_block:     bool       = False
    same_member:    bool       = False
    tool_signature: str | None = None
    limit:          int        = 10      # 合法範圍 1-100，超出 clamp
    fields:         list[str]  = field(default_factory=list)


@dataclass
class ReadyPlugin:
    """通過所有 Layer 2 條件後，傳給 Layer 3 的資料單元。"""
    plugin_id:     str
    event:         ShellEvent       # 觸發此 Plugin 的 event（引用）
    query_result:  list[dict]       # context_query 結果，已做 fields 過濾
    has_action_py: bool


@dataclass
class PerceptionContextImpl:
    """
    perception.py 的 evaluate(event_dict, context) 收到的 context 物件。
    """
    query_result: list[dict]
    ecc:          "EccQueryApi"


class EccQueryApi:
    """
    perception.py 可用的 ECC 查詢介面。
    包裝 EccManager，限制 perception.py 只能查詢，不能寫入。
    """

    def __init__(self, ecc_manager: EccManager, current_event: ShellEvent) -> None:
        self._ecc = ecc_manager
        self._event = current_event

    def query(
        self,
        member_id:      str | None = None,
        block:          str | None = None,
        tool_signature: str | None = None,
        limit:          int = 10,
    ) -> list[dict]:
        """
        查詢 ECC 歷史事件。
        回傳完整 event dict 清單（含 ShellEvent 所有欄位 + extracted_data）。
        同步呼叫，在 asyncio.to_thread 中執行。
        limit 合法範圍 1-100，超出則 clamp。
        """
        limit = min(max(limit, 1), 100)
        query = EventQuery(
            member_id=member_id,
            block=block,
            tool_signature=tool_signature,
            limit=limit,
            order=QueryOrder.DESC,
        )
        events = self._ecc.query_events(query)
        return [e.to_dict() for e in events]


# ── Helper functions ───────────────────────────────────────────────────────────

def _parse_context_query(raw: dict) -> ParsedContextQuery:
    """
    解析 plugin.yaml 的 context_query dict → ParsedContextQuery。
    型別不符時記錄 warning 並使用預設值。
    """
    same_block = raw.get("same_block", False)
    if not isinstance(same_block, bool):
        logger.warning("context_query.same_block must be bool, using False")
        same_block = False

    same_member = raw.get("same_member", False)
    if not isinstance(same_member, bool):
        logger.warning("context_query.same_member must be bool, using False")
        same_member = False

    tool_signature = raw.get("tool_signature")
    if tool_signature is not None and not isinstance(tool_signature, str):
        logger.warning("context_query.tool_signature must be str, using None")
        tool_signature = None

    limit = raw.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool):
        logger.warning("context_query.limit must be int, using 10")
        limit = 10
    limit = min(max(limit, 1), 100)

    fields = raw.get("fields", [])
    if not isinstance(fields, list):
        logger.warning("context_query.fields must be list, using []")
        fields = []

    return ParsedContextQuery(
        same_block=same_block,
        same_member=same_member,
        tool_signature=tool_signature,
        limit=limit,
        fields=fields,
    )


def _resolve_field(field_name: str, event_dict: dict) -> Any:
    """
    解析 field_name 對應的值（context_query fields 過濾版本）。
    1. event_dict 頂層 key（ShellEvent 直接欄位）
    2. event_dict["extracted_data"] 頂層 key
    3. 回傳 _FIELD_NOT_FOUND
    """
    if field_name in event_dict:
        return event_dict[field_name]
    extracted = event_dict.get("extracted_data")
    if isinstance(extracted, dict) and field_name in extracted:
        return extracted[field_name]
    return _FIELD_NOT_FOUND


# ── ContextBuilder ─────────────────────────────────────────────────────────────

class ContextBuilder:

    def __init__(
        self,
        ecc_manager:    EccManager,
        llm_client:     Any,          # LlmClient（layer2.llm 實作後可精確型別）
        plugin_manager: PluginManager,
    ) -> None:
        self._ecc_manager    = ecc_manager
        self._llm_client     = llm_client
        self._plugin_manager = plugin_manager

    async def process(
        self, triggered: list[TriggeredPlugin]
    ) -> AsyncIterator[ReadyPlugin]:
        """
        對每個 TriggeredPlugin 並行執行 Layer 2 評估。
        yield 通過所有條件的 ReadyPlugin。
        不拋出 exception。
        """
        if not triggered:
            return

        coroutines = [self._process_one(tp) for tp in triggered]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Plugin %s processing failed: %s",
                    triggered[i].plugin_id, result,
                    exc_info=result,
                )
            elif result is not None:
                yield result  # type: ignore[misc]

    async def _process_one(self, tp: TriggeredPlugin) -> ReadyPlugin | None:
        """
        單一 Plugin 的 Layer 2 處理流程。
        通過所有條件回傳 ReadyPlugin，任一條件不通過回傳 None。
        不捕獲 DB / to_thread exception（讓 gather 以 return_exceptions 捕獲）。
        """
        # 1. 解析 context_query
        parsed = (
            _parse_context_query(tp.context_query)
            if tp.context_query is not None
            else ParsedContextQuery()
        )

        # 2. 執行 context_query（同步 DB 查詢，在 thread 中執行）
        query_result: list[dict] = await asyncio.to_thread(
            self._execute_context_query, parsed, tp.event
        )

        # 3. 評估 ai_condition
        if tp.ai_condition is not None:
            try:
                passed = await self._llm_client.evaluate_condition(
                    condition=tp.ai_condition,
                    event=tp.event,
                    query_result=query_result,
                )
            except Exception as e:
                logger.error(
                    "llm evaluate_condition failed for plugin %s: %s",
                    tp.plugin_id, e, exc_info=True,
                )
                return None
            if not passed:
                return None

        # 4. 評估 advanced_perception
        if tp.advanced_perception:
            perception_module = self._plugin_manager.get_perception_module(
                tp.plugin_id
            )
            if perception_module is None:
                logger.warning(
                    "advanced_perception=True but no perception.py for plugin %s",
                    tp.plugin_id,
                )
                # 視同通過，繼續
            else:
                context = PerceptionContextImpl(
                    query_result=query_result,
                    ecc=EccQueryApi(self._ecc_manager, tp.event),
                )
                try:
                    raw_result = await asyncio.to_thread(
                        perception_module.evaluate,
                        tp.event.to_dict(),
                        context,
                    )
                except Exception as e:
                    logger.warning(
                        "perception.py evaluate() raised exception for plugin %s: %s",
                        tp.plugin_id, e, exc_info=True,
                    )
                    return None

                try:
                    passed = bool(raw_result)
                except Exception as e:
                    logger.warning(
                        "bool() conversion failed for perception result "
                        "from plugin %s: %s",
                        tp.plugin_id, e,
                    )
                    passed = False

                if not passed:
                    return None

        # 5. 所有條件通過
        return ReadyPlugin(
            plugin_id=tp.plugin_id,
            event=tp.event,
            query_result=query_result,
            has_action_py=tp.has_action_py,
        )

    def _execute_context_query(
        self, parsed: ParsedContextQuery, event: ShellEvent
    ) -> list[dict]:
        """
        同步 DB 查詢，由 asyncio.to_thread 在 thread 中呼叫。
        回傳已做 fields 過濾的 list[dict]。
        """
        query = EventQuery(
            block=event.block if parsed.same_block else None,
            member_id=event.member_id if parsed.same_member else None,
            tool_signature=parsed.tool_signature,
            limit=min(max(parsed.limit, 1), 100),
            order=QueryOrder.DESC,
        )

        events = self._ecc_manager.query_events(query)

        # 排除當前 event 本身
        events = [e for e in events if e.event_id != event.event_id]

        # 轉為 dict
        event_dicts = [e.to_dict() for e in events]

        # fields 過濾
        if not parsed.fields:
            return event_dicts

        filtered: list[dict] = []
        for event_dict in event_dicts:
            f: dict = {}
            for field_name in parsed.fields:
                value = _resolve_field(field_name, event_dict)
                if value is not _FIELD_NOT_FOUND:
                    f[field_name] = value
            filtered.append(f)
        return filtered
