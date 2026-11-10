# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Canonical EDA Tool Catalog v1 — loader 與 canonicalization API。

只處理 EDA executable identity facts（canonical id + 真正的 executable
alias），不處理 execution policy、SAFE/DANGEROUS 分類、dispatch owner 或
approval 邏輯——這些屬於 Agent ToolRegistry／PackageRegistry／
ActionRegistry／Orchestration ToolRegistry，本模組不得與其合併。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.resource_paths import bundled_knowledge_path


#: Effective catalog 中每個 canonical id 的來源。出貨 catalog 一律是 builtin；
#: 使用者 overlay（`user_catalog.py`）只能附加 user 項目，不能改寫 builtin。
ORIGIN_BUILTIN = "builtin"
ORIGIN_USER = "user"


class CatalogError(ValueError):
    """Catalog 檔案 malformed、duplicate id 或 alias collision 時拋出。"""


class _DuplicateKeyLoader(yaml.SafeLoader):
    """偵測 YAML mapping 內重複 key（PyYAML 預設靜默保留最後一個，不報錯）。"""


def _construct_mapping_no_duplicates(
    loader: yaml.SafeLoader, node: yaml.MappingNode
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise CatalogError(f"duplicate key in catalog YAML: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


@dataclass(frozen=True)
class ToolCatalog:
    """Canonical EDA tool identity 的唯讀視圖。

    Consumer 不得修改回傳值——`_canonical_by_executable` 與
    `_ids` 皆為 frozen dataclass 欄位，且回傳給外部的集合一律是新建的
    immutable tuple，避免 consumer 意外修改共用狀態影響其他 consumer。
    """

    _canonical_by_executable: dict[str, str]
    _executables_by_canonical: dict[str, tuple[str, ...]]
    # 沒有列出的 canonical id 一律視為 builtin；只有 effective catalog 會放入 user。
    _origin_by_canonical: dict[str, str] = field(default_factory=dict)

    def canonical_ids(self) -> tuple[str, ...]:
        """列舉所有 canonical tool id（deterministic：依 catalog 檔案內宣告順序）。"""
        return tuple(self._executables_by_canonical.keys())

    def executables(self) -> tuple[str, ...]:
        """列舉所有 executable basename（含 alias，deterministic 順序）。"""
        return tuple(self._canonical_by_executable.keys())

    def executables_for(self, canonical_id: str) -> tuple[str, ...]:
        """指定 canonical id 底下的 executable/alias 清單；不存在回傳空 tuple。"""
        return self._executables_by_canonical.get(canonical_id, ())

    def origin_of(self, canonical_id: str) -> str | None:
        """canonical id 的來源（`builtin`／`user`）；不在 catalog 內回傳 None。"""
        if canonical_id not in self._executables_by_canonical:
            return None
        return self._origin_by_canonical.get(canonical_id, ORIGIN_BUILTIN)

    def resolve(self, command_token: str) -> str | None:
        """把一個原始 command token（可能含絕對路徑、前後空白）解析成 canonical id。

        規則：
        - 先 strip 前後空白；空字串 → None
        - 絕對/相對路徑取 basename 後比對（`os.path.basename`）
        - Linux case-sensitive exact match，不做大小寫正規化
        - 找不到對應 executable/alias → None（明確 unknown，非 exception）
        """
        token = command_token.strip()
        if not token:
            return None
        basename = os.path.basename(token)
        if not basename:
            return None
        return self._canonical_by_executable.get(basename)


def default_catalog_path() -> Path:
    """取得發行套件內的唯讀工具 catalog。"""
    return bundled_knowledge_path("canonical_eda_tool_catalog.yaml")


def load_catalog(path: str | Path | None = None) -> ToolCatalog:
    """載入並驗證 catalog 檔案，回傳唯讀 `ToolCatalog`。

    Malformed schema、duplicate canonical id、duplicate/colliding alias
    一律 raise `CatalogError`（fail closed），不 fallback 到任何 hidden
    hardcoded list。
    """
    resolved_path = Path(path) if path is not None else default_catalog_path()

    if not resolved_path.is_file():
        raise CatalogError(f"catalog file not found: {resolved_path}")

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"cannot read catalog file {resolved_path}: {exc}") from exc

    try:
        data = yaml.load(raw_text, Loader=_DuplicateKeyLoader)
    except CatalogError:
        raise
    except yaml.YAMLError as exc:
        raise CatalogError(f"invalid YAML in {resolved_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise CatalogError(f"catalog root must be a mapping: {resolved_path}")

    schema_version = data.get("schema_version")
    if schema_version != 1:
        raise CatalogError(
            f"unsupported catalog schema_version={schema_version!r} (expected 1): "
            f"{resolved_path}"
        )

    tools = data.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise CatalogError(f"catalog 'tools' must be a non-empty mapping: {resolved_path}")

    canonical_by_executable: dict[str, str] = {}
    executables_by_canonical: dict[str, tuple[str, ...]] = {}

    for canonical_id, entry in tools.items():
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise CatalogError(f"invalid canonical id: {canonical_id!r}")
        if not isinstance(entry, dict):
            raise CatalogError(f"tool entry for {canonical_id!r} must be a mapping")

        executables = entry.get("executables")
        if not isinstance(executables, list) or not executables:
            raise CatalogError(
                f"tool entry {canonical_id!r} must have a non-empty 'executables' list"
            )

        seen_in_entry: set[str] = set()
        entry_executables: list[str] = []
        for raw_name in executables:
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise CatalogError(
                    f"tool entry {canonical_id!r} has an invalid executable name: {raw_name!r}"
                )
            name = raw_name.strip()
            if name in seen_in_entry:
                raise CatalogError(
                    f"duplicate executable/alias {name!r} within tool entry {canonical_id!r}"
                )
            seen_in_entry.add(name)
            entry_executables.append(name)

            if name in canonical_by_executable:
                other_id = canonical_by_executable[name]
                raise CatalogError(
                    f"executable/alias {name!r} collides across canonical ids "
                    f"{other_id!r} and {canonical_id!r}"
                )
            canonical_by_executable[name] = canonical_id

        if canonical_id in executables_by_canonical:
            raise CatalogError(f"duplicate canonical id: {canonical_id!r}")
        executables_by_canonical[canonical_id] = tuple(entry_executables)

    return ToolCatalog(
        _canonical_by_executable=canonical_by_executable,
        _executables_by_canonical=executables_by_canonical,
    )
