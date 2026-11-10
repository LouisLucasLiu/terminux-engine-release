# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""將已載入 package 的 action metadata 轉成 Agent tools。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from terminux_engine.llm.tool_registry import ToolRegistry, ToolRisk, ToolSchema
from terminux_engine.package_runtime.action_registry import ActionRegistry
from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.loader import RuntimePackage


ActionContextFactory = Callable[[str, dict[str, Any]], PackageContext]


def register_package_action_tools(
    registry: ToolRegistry,
    packages: Iterable[RuntimePackage],
    action_registry: ActionRegistry,
    context_factory: ActionContextFactory,
) -> list[str]:
    """將有 description 的 package actions 註冊為 Agent tools。

    action handler 保持由既有 ActionRegistry 解析；executor 只負責把 Agent
    keyword arguments 轉成既有 action ``query`` JSON 字串。
    """
    registered: list[str] = []
    for package in packages:
        for action_name, action_spec in package.manifest.action_specs.items():
            if not action_spec.description:
                continue
            if registry.has_tool(action_name):
                raise ValueError(f"Tool already registered: {action_name}")

            schema = ToolSchema(
                name=action_name,
                description=action_spec.description,
                parameters=_to_tool_parameters(action_spec.parameters),
            )
            # LLM-backed action 的耗時遠超過 registry 的 5 秒預設。
            # 值只從 manifest 讀——Common 不依 action 名稱或 provider 推導。
            # 未宣告時傳 None，registry 沿用既有預設（backward compatible）。
            timeout_kwargs = ({"timeout_sec": action_spec.timeout_sec}
                              if action_spec.timeout_sec is not None else {})
            registry.register(
                action_name,
                _create_action_executor(
                    action_name,
                    action_registry=action_registry,
                    context_factory=context_factory,
                ),
                schema,
                **timeout_kwargs,
                # package action 的 handler 可能寫 store、檔案或通知；manifest
                # description 不是安全分類，所以所有動態 Agent action 從嚴視為
                # DANGEROUS，必經同一 runtime gate。
                risk=ToolRisk.DANGEROUS,
            )
            registered.append(action_name)
    return registered


def _create_action_executor(
    action_name: str,
    *,
    action_registry: ActionRegistry,
    context_factory: ActionContextFactory,
) -> Callable[..., Any]:
    """建立直接呼叫既有 ActionRegistry 的 tool executor。"""

    def _executor(**kwargs: Any) -> Any:
        params = {
            "action": action_name,
            "args": kwargs,
        }
        ctx = context_factory(action_name, params)
        query = json.dumps(kwargs, ensure_ascii=False, sort_keys=True)
        return action_registry.call(action_name, ctx, query=query)

    return _executor


def _to_tool_parameters(
    parameters: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """將 manifest parameters 轉成 ToolSchema 使用的 JSON Schema。"""
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for name, parameter_schema in parameters.items():
        schema = dict(parameter_schema)
        if schema.pop("required", False):
            required.append(name)
        properties[name] = schema

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        result["required"] = required
    return result
