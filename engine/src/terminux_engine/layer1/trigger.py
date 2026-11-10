# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from terminux_engine.core.plugin_manager import (
    Condition,
    PluginManager,
    PluginTriggerDef,
)
from terminux_engine.ecc.models import ShellEvent

logger = logging.getLogger(__name__)


# ── Sentinel ───────────────────────────────────────────────────────────────────

_FIELD_NOT_FOUND = object()   # module-level 唯一實例


# ── Constants ──────────────────────────────────────────────────────────────────

EVENT_TYPES: set[str] = {
    "on_command_complete",   # 目前唯一支援的 event type
}

OP_VALUES: set[str] = {"eq", "ne", "gt", "lt", "gte", "lte", "contains", "regex"}


# ── Output Type ────────────────────────────────────────────────────────────────

@dataclass
class TriggeredPlugin:
    """
    Layer 1 通過後回傳給 Layer 2 的資料單元。
    定義於 layer1.trigger，因為它是 trigger 的輸出，不是 plugin 定義的一部分。
    """
    plugin_id:           str
    event:               ShellEvent    # 觸發此 Plugin 的 event（引用，非複製）
    context_query:       dict | None   # 原樣傳遞給 Layer 2
    ai_condition:        str | None    # 原樣傳遞給 Layer 2
    advanced_perception: bool
    has_action_py:       bool


# ── TriggerEvaluator ───────────────────────────────────────────────────────────

class TriggerEvaluator:

    def __init__(self, plugin_manager: PluginManager) -> None:
        self._plugin_manager = plugin_manager

    def evaluate(self, event: ShellEvent) -> list[TriggeredPlugin]:
        """
        對 event 評估所有已載入 Plugin 的 Layer 1 條件。
        回傳符合條件的 TriggeredPlugin 清單（可為空 list）。
        不拋出 exception；整體 exception 捕獲後記錄 error log 並回傳空 list。
        """
        try:
            plugins: list[PluginTriggerDef] = self._plugin_manager.get_trigger_defs()
            triggered: list[TriggeredPlugin] = []

            for plugin in plugins:
                if not self._match_event_type(plugin.event_type, plugin.plugin_id):
                    continue
                if not self._match_conditions(plugin.conditions, event):
                    continue
                triggered.append(TriggeredPlugin(
                    plugin_id=plugin.plugin_id,
                    event=event,
                    context_query=plugin.context_query,
                    ai_condition=plugin.ai_condition,
                    advanced_perception=plugin.advanced_perception,
                    has_action_py=plugin.has_action_py,
                ))

            return triggered

        except Exception as e:
            logger.error(
                "Unexpected error in evaluate(): %s", e, exc_info=True
            )
            return []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _match_event_type(self, event_type: str, plugin_id: str) -> bool:
        """
        檢查 event_type 是否為已知合法值。
        "on_command_complete"：永遠回傳 True。
        未知 event_type：記錄 warning，回傳 False。
        """
        if event_type in EVENT_TYPES:
            return True
        logger.warning(
            "unknown event_type: %s in plugin %s", event_type, plugin_id
        )
        return False

    def _match_conditions(
        self, conditions: list[Condition], event: ShellEvent
    ) -> bool:
        """
        評估所有 conditions（AND 關係）。
        空 list → 無條件通過（True）。
        任一條件 False → 立即回傳 False（short-circuit）。
        """
        for condition in conditions:
            field_value = self._resolve_field(condition.field, event)
            if field_value is _FIELD_NOT_FOUND:
                logger.warning(
                    "field %r not found in event or extracted_data",
                    condition.field,
                )
                return False
            if not self._apply_op(condition.op, field_value, condition.value):
                return False
        return True

    def _resolve_field(self, field_name: str, event: ShellEvent) -> Any:
        """
        解析 field_name 對應的值。
        1. command_type：回傳 event.command_type.value（str）
        2. ShellEvent 直接屬性：getattr
        3. event.extracted_data 頂層 key
        4. 兩者皆無：回傳 _FIELD_NOT_FOUND
        """
        # 特殊欄位：command_type 回傳字串值供 YAML 比對
        if field_name == "command_type":
            return event.command_type.value

        # ShellEvent 直接屬性
        val = getattr(event, field_name, _FIELD_NOT_FOUND)
        if val is not _FIELD_NOT_FOUND:
            return val

        # extracted_data 頂層 key
        return event.extracted_data.get(field_name, _FIELD_NOT_FOUND)

    def _apply_op(
        self, op: str, field_value: Any, condition_value: Any
    ) -> bool:
        """
        對 field_value 與 condition_value 套用 op 運算，回傳 bool。
        TypeError / ValueError：捕獲，記錄 warning，回傳 False。
        未知 op：記錄 warning，回傳 False。
        """
        try:
            if op == "eq":
                return field_value == condition_value
            elif op == "ne":
                return field_value != condition_value
            elif op == "gt":
                return field_value > condition_value
            elif op == "lt":
                return field_value < condition_value
            elif op == "gte":
                return field_value >= condition_value
            elif op == "lte":
                return field_value <= condition_value
            elif op == "contains":
                return condition_value in field_value
            elif op == "regex":
                try:
                    pattern = re.compile(str(condition_value))
                except re.error as e:
                    logger.warning(
                        "invalid regex pattern %r: %s", condition_value, e
                    )
                    return False
                return pattern.search(str(field_value)) is not None
            else:
                logger.warning("unknown op: %s", op)
                return False

        except (TypeError, ValueError) as e:
            logger.warning(
                "op %r comparison failed "
                "(field_value type: %s, condition_value type: %s): %s",
                op,
                type(field_value).__name__,
                type(condition_value).__name__,
                e,
            )
            return False
