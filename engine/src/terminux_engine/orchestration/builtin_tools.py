# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""內建 EDA tool 的 orchestration registry。

工具定義由選用模組 `builtin_tool_definitions` 提供；模組不在這次安裝中時沒有內建工具。
有定義的工具也只有在執行檔存在、且對應的 DQE package 已安裝時才註冊；缺少任一項的工具
略過並記 log。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from terminux_engine.orchestration.tool_registry import ToolDefinition, ToolRegistry


_LOG = logging.getLogger(__name__)

#: 提供內建工具定義的選用模組。
_DEFINITIONS_MODULE = "terminux_engine.orchestration.builtin_tool_definitions"


def _load_definitions() -> ModuleType | None:
    """載入選用的工具定義模組；只有模組本身不存在時才視為沒有內建工具。"""
    try:
        return importlib.import_module(_DEFINITIONS_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != _DEFINITIONS_MODULE:
            raise
        return None


_DEFINITIONS = _load_definitions()

BUILTIN_TOOL_DEFINITIONS: list[ToolDefinition] = (
    list(_DEFINITIONS.BUILTIN_TOOL_DEFINITIONS) if _DEFINITIONS is not None else []
)


def __getattr__(name: str) -> Any:
    """選用模組中的個別工具定義（例如 `MMMC_SUM`）也可從本模組取得。"""
    if _DEFINITIONS is not None and not name.startswith("_") and hasattr(_DEFINITIONS, name):
        return getattr(_DEFINITIONS, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _executable_available(tool_definition: ToolDefinition) -> bool:
    return any(Path(entry).is_file() and os.access(entry, os.X_OK)
               for entry in tool_definition.executables)


def _package_installed(package_id: str) -> bool:
    try:
        return importlib.util.find_spec(f"terminux_engine.packages.{package_id}") is not None
    except ModuleNotFoundError:
        return False


def get_default_registry() -> ToolRegistry:
    """建立並回傳 registry，只含執行檔與 DQE package 都存在的內建工具。"""
    registry = ToolRegistry()
    for tool_definition in BUILTIN_TOOL_DEFINITIONS:
        missing_packages = [package_id for package_id in tool_definition.expected_packages
                            if not _package_installed(package_id)]
        if not _executable_available(tool_definition) or missing_packages:
            _LOG.info("builtin tool %s skipped: executable available=%s, missing packages=%s",
                      tool_definition.tool_id, _executable_available(tool_definition), missing_packages)
            continue
        registry.register(tool_definition)
    return registry
