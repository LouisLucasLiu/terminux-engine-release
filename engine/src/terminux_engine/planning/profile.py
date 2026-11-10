# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""plan-only planner plan profile。

Catalog selection **不**寫死在 Common planner 裡：這個 profile 是唯一決定
「這一版暴露哪些 capability」的地方，而且它只能從既有 Tool Registry 已註冊的
集合中挑選，不能憑空新增。

Profile 本身知道 package 名稱是刻意的——它是 composition/配置層，不是
Common planner；`planning/capability.py`、`plan.py`、`validator.py`、
`service.py` 四個檔案仍然完全 domain-blind。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from terminux_engine.planning.capability import (
    CapabilitySnapshot,
    build_capability_snapshot,
)

PROFILE_ID = "ai3_plan_only_v1"


def registry_entries_from_tool_registry(
    tool_registry: Any, *, capability_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """把既有 `ToolRegistry` 的 entry 轉成 assembler 需要的最小 dict。

    只讀既有的 `get_schemas_for_names()` 與 `get_tool_risks()`——不新增
    registry、不改 ToolRegistry、不自己維護第二份名稱清單。
    未註冊的 id 直接跳過，由 assembler 記成 `not_present_in_tool_registry`。
    """
    wanted = tuple(dict.fromkeys(capability_ids))
    present = tuple(name for name in wanted if tool_registry.has_tool(name))
    if not present:
        return []
    risks = tool_registry.get_tool_risks()
    entries: list[dict[str, Any]] = []
    for item in tool_registry.get_schemas_for_names(present):
        function = item.get("function") or {}
        name = function.get("name")
        risk = risks.get(name)
        entries.append({
            "capability_id": name,
            "description": function.get("description") or "",
            "input_schema": function.get("parameters") or {},
            "risk": getattr(risk, "value", risk),
        })
    return entries


def build_ai3_snapshot(
    tool_registry: Any,
    *,
    exposures: Iterable[tuple[Iterable[str], Callable[[str], dict[str, Any] | None] | None]],
    registry_snapshot_ref: str = "",
) -> CapabilitySnapshot:
    """依 profile 建立 bounded snapshot。

    `exposures` 每一項是 (capability ids, metadata provider)。Provider 只能
    補 metadata；它提供的 id 若不在 Tool Registry 裡就會被排除。
    """
    exposed: list[str] = []
    providers: dict[str, Callable[[str], dict[str, Any] | None]] = {}
    for ids, provider in exposures:
        for capability_id in ids:
            if capability_id not in exposed:
                exposed.append(capability_id)
            if provider is not None:
                providers[capability_id] = provider

    entries = registry_entries_from_tool_registry(tool_registry, capability_ids=exposed)
    entry_ids = {e["capability_id"] for e in entries}

    # Profile 也把 registry 裡**沒被暴露**的其他 capability 一併列出，讓
    # snapshot 的 `excluded` 能誠實顯示「還有什麼存在但沒進 catalog」。
    for name in tool_registry.list_tools():
        if name not in entry_ids and name not in exposed:
            entries.append({"capability_id": name, "description": "", "input_schema": {},
                            "risk": getattr(tool_registry.get_tool_risks().get(name), "value",
                                            None)})

    def metadata_provider(capability_id: str) -> dict[str, Any] | None:
        provider = providers.get(capability_id)
        return provider(capability_id) if provider is not None else None

    return build_capability_snapshot(
        registry_entries=entries,
        exposed_capability_ids=exposed,
        metadata_provider=metadata_provider,
        registry_snapshot_ref=registry_snapshot_ref,
    )


__all__ = ["PROFILE_ID", "build_ai3_snapshot", "registry_entries_from_tool_registry"]
