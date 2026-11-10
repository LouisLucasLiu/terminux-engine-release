# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""最低限度的 tool identity 自動化檢查。

只驗證 orchestration `ToolRegistry` 自己內部的一致性（`event_tool_name`
是否為合法 canonical string、`expected_packages` 是否確實存在、其
`when.tools` 是否真的包含這個 `event_tool_name`）——**不**嘗試把
`tool_signatures.yaml`、`canonical_eda_tool_catalog.yaml`、
`orchestration.ToolRegistry`、Package manifest 四個各自獨立的 registry
合併成一個（這四者刻意維持獨立）。
"""

from __future__ import annotations

import re
from pathlib import Path

from terminux_engine.orchestration.tool_registry import ToolDefinition
from terminux_engine.package_runtime.manifest import load_manifest

_CANONICAL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolIdentityValidationError(ValueError):
    """一個 `ToolDefinition` 未滿足最低限度 identity 一致性要求。"""


def validate_tool_identity(definition: ToolDefinition, *, packages_root: Path) -> None:
    """驗證單一 `ToolDefinition` 的 identity 一致性；違反時 fail-closed。

    `packages_root`：production 呼叫端應傳入
    `engine/src/terminux_engine/packages/`；test-only registration（例如
    vendor-neutral execution contract 的 ORFS dispatch fixture）應傳入其專屬的 disposable fixture
    package root，兩者不得混用（見模組 docstring）。
    """
    if not definition.event_tool_name:
        raise ToolIdentityValidationError(
            f"tool {definition.tool_id!r}: event_tool_name must not be empty"
        )
    if not _CANONICAL_ID_RE.match(definition.event_tool_name):
        raise ToolIdentityValidationError(
            f"tool {definition.tool_id!r}: event_tool_name {definition.event_tool_name!r} "
            "is not a canonical identifier (expected lowercase snake_case)"
        )

    for package_id in definition.expected_packages:
        package_dir = packages_root / package_id
        manifest_path = package_dir / "package.yaml"
        if not manifest_path.is_file():
            raise ToolIdentityValidationError(
                f"tool {definition.tool_id!r}: expected_packages entry "
                f"{package_id!r} not found under {packages_root}"
            )
        manifest = load_manifest(manifest_path)
        if definition.event_tool_name not in manifest.when_tools:
            raise ToolIdentityValidationError(
                f"tool {definition.tool_id!r}: package {package_id!r} when.tools "
                f"{manifest.when_tools!r} does not include event_tool_name "
                f"{definition.event_tool_name!r}"
            )
