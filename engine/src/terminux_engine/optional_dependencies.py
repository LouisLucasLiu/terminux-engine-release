# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""選用功能的延遲 import 與可操作的安裝錯誤。"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


class MissingOptionalDependencyError(RuntimeError):
    """選用相依未安裝或無法匯入。"""


def import_optional(module_name: str, extra: str) -> ModuleType:
    """按需載入已宣告相依，保留原因並指出需安裝的 extra。"""
    try:
        return import_module(module_name)
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            f"無法載入選用相依 {module_name}；"
            f"請安裝：pip install 'terminux-engine[{extra}]'"
        ) from exc
