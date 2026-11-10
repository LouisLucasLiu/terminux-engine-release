# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime — shared, domain-blind package/action identity uniqueness checker。

Single source of truth for「這個 package tree 內是否有 duplicate
package id 或 duplicate action id」，由 offline validator 與 production
registry assembly（`registry.py::discover_and_register_packages()`）
共用，不各自維護一套 set/dict 判斷。

只比對已成功 parse 的 `PackageManifest` 物件的 identity 欄位
（`package_id`／`actions` 的 key），不碰檔案系統、不 import handler。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from terminux_engine.package_runtime.errors import (
    DuplicatePackageActionIdError,
    DuplicatePackageIdError,
)
from terminux_engine.package_runtime.manifest import PackageManifest

DUPLICATE_PACKAGE_ID = "DUPLICATE_PACKAGE_ID"
DUPLICATE_ACTION_ID = "DUPLICATE_ACTION_ID"


@dataclass(frozen=True)
class PackageIdentityEntry:
    """單一已驗證 manifest 的 identity 摘要。"""

    package_id: str
    manifest_label: str
    action_ids: tuple[str, ...]


@dataclass(frozen=True)
class IdentityCollision:
    """一筆 duplicate identity 衝突。"""

    kind: str  # DUPLICATE_PACKAGE_ID | DUPLICATE_ACTION_ID
    identity: str
    first_owner: str
    second_owner: str


@dataclass(frozen=True)
class PackageIdentityIndex:
    """整個 package tree 的 identity 索引結果。"""

    entries: tuple[PackageIdentityEntry, ...]
    collisions: tuple[IdentityCollision, ...]

    @property
    def is_unique(self) -> bool:
        """回傳 True 表示沒有任何 duplicate package/action id。"""
        return not self.collisions


def build_package_identity_index(
    manifests: Sequence[tuple[str, PackageManifest]],
) -> PackageIdentityIndex:
    """建立 identity index 並偵測 collision。

    `manifests`：`(label, manifest)` pair 的序列，`label` 是穩定、
    deterministic 的擁有者識別（例如 manifest 相對路徑或 package 目錄
    名稱），用於診斷訊息，不得是隨機或機器特定的絕對路徑。

    為求輸出與 collision 判定完全 deterministic（不依賴呼叫端傳入順序
    或 filesystem iteration order），本函式內部一律先依 `label` 排序
    再處理。即使兩份 manifest 內容 byte-identical，仍視為 duplicate。
    """
    ordered = sorted(manifests, key=lambda pair: pair[0])

    entries: list[PackageIdentityEntry] = []
    collisions: list[IdentityCollision] = []
    seen_package_ids: dict[str, str] = {}
    seen_action_ids: dict[str, str] = {}

    for label, manifest in ordered:
        action_ids = tuple(sorted(manifest.actions))
        entries.append(PackageIdentityEntry(
            package_id=manifest.package_id,
            manifest_label=label,
            action_ids=action_ids,
        ))

        if manifest.package_id in seen_package_ids:
            collisions.append(IdentityCollision(
                kind=DUPLICATE_PACKAGE_ID,
                identity=manifest.package_id,
                first_owner=seen_package_ids[manifest.package_id],
                second_owner=label,
            ))
        else:
            seen_package_ids[manifest.package_id] = label

        for action_id in action_ids:
            if action_id in seen_action_ids:
                collisions.append(IdentityCollision(
                    kind=DUPLICATE_ACTION_ID,
                    identity=action_id,
                    first_owner=seen_action_ids[action_id],
                    second_owner=label,
                ))
            else:
                seen_action_ids[action_id] = label

    return PackageIdentityIndex(entries=tuple(entries), collisions=tuple(collisions))


def validate_registry_uniqueness(manifests: Sequence[tuple[str, PackageManifest]]) -> None:
    """`build_package_identity_index()` 的 raise 版本。

    有任何 collision 時，依 deterministic 順序（`build_package_identity_index()`
    已排序）raise **第一筆** collision 對應的例外。呼叫端（production
    two-pass assembly）預期在偵測到第一個 collision 就整批拒絕，不需要
    完整清單——需要完整清單（例如 offline validator 要一次回報所有
    問題）的呼叫端應直接使用 `build_package_identity_index()`。
    """
    index = build_package_identity_index(manifests)
    if not index.collisions:
        return
    collision = index.collisions[0]
    message = (
        f"duplicate {('package id' if collision.kind == DUPLICATE_PACKAGE_ID else 'action id')} "
        f"{collision.identity!r}: already registered from {collision.first_owner!r}, "
        f"cannot also register from {collision.second_owner!r}"
    )
    if collision.kind == DUPLICATE_PACKAGE_ID:
        raise DuplicatePackageIdError(message)
    raise DuplicatePackageActionIdError(message)
