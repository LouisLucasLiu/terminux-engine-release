# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Tool Registry 的 read-only view。

既有 `ToolRegistry` 同時帶 read 與 **execute** 能力。Plan-only path 只需要
read，因此 composition 只把這個 view 交給 handler——handler 從頭到尾拿不到
`execute()`，不是「拿得到但不呼叫」。

這個 view 不知道任何工具名稱、vendor 或 domain 語意。
"""

from __future__ import annotations

from typing import Any

#: 從來源 registry 借用的**唯一**四個方法。任何 mutation／execution 介面
#: （`execute`、`register`、`unregister`）都不在這裡，也不會被代理。
_ALLOWED_METHODS = ("has_tool", "list_tools", "get_schemas_for_names", "get_tool_risks")


class CapabilityRegistryView:
    """Read-only capability 查詢介面。"""

    #: 明確列出這個 view 永遠不提供的東西；`assert_read_only()` 會檢查。
    FORBIDDEN_METHODS = ("execute", "register", "unregister", "call", "invoke")

    def __init__(self, source: Any) -> None:
        missing = [m for m in _ALLOWED_METHODS if not callable(getattr(source, m, None))]
        if missing:
            raise TypeError(f"registry source is missing read methods: {missing}")
        # 只保留 bound method，不保留 source 本身——避免任何人日後從 view
        # 反向拿回完整 registry。
        self._has_tool = source.has_tool
        self._list_tools = source.list_tools
        self._get_schemas_for_names = source.get_schemas_for_names
        self._get_tool_risks = source.get_tool_risks

    # ── read-only surface ────────────────────────────────────────────

    def has_tool(self, name: str) -> bool:
        return bool(self._has_tool(name))

    def list_tools(self) -> list[str]:
        return list(self._list_tools())

    def get_schemas_for_names(self, names: tuple[str, ...]) -> list[dict[str, Any]]:
        return list(self._get_schemas_for_names(names))

    def get_tool_risks(self) -> dict[str, Any]:
        return dict(self._get_tool_risks())

    # ── defence in depth ─────────────────────────────────────────────

    def assert_read_only(self) -> None:
        present = [m for m in self.FORBIDDEN_METHODS if hasattr(self, m)]
        if present:
            raise RuntimeError(
                f"CapabilityRegistryView must not expose execution methods: {present}")


__all__ = ["CapabilityRegistryView"]
