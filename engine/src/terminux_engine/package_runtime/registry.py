# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package registry 與 structural matching。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from terminux_engine.package_runtime import identity
from terminux_engine.package_runtime.errors import DuplicatePackageIdError, ManifestError
from terminux_engine.package_runtime.loader import PackageLoader, RuntimePackage
from terminux_engine.package_runtime.manifest import PackageManifest, load_manifest
from terminux_engine.package_runtime.result import TimelineEvent

_log = logging.getLogger(__name__)


@dataclass
class PackageRegistry:
    """已載入 package 的 registry。"""

    packages: list[RuntimePackage] = field(default_factory=list)

    def __post_init__(self) -> None:
        """建構時（含直接傳入 list 的呼叫端）也要拒絕 duplicate package id。

        不只保護 `register()` 這條路徑——`PackageRegistry(some_list)` 這種
        直接用 dataclass constructor 傳入既有 list 的既有慣例（見
        `orchestration/runner_factory.py` 等）同樣受到保護，不需要每個
        呼叫端各自小心。
        """
        seen: dict[str, RuntimePackage] = {}
        for package in self.packages:
            existing = seen.get(package.id)
            if existing is not None:
                raise DuplicatePackageIdError(
                    f"duplicate package id {package.id!r}: already present from "
                    f"{existing.package_dir.name!r}, cannot also include "
                    f"{package.package_dir.name!r}"
                )
            seen[package.id] = package

    def register(self, package: RuntimePackage) -> None:
        """註冊 package。第二次註冊相同 id 會 raise，不覆寫既有 entry。"""
        existing = self.by_id(package.id)
        if existing is not None:
            raise DuplicatePackageIdError(
                f"duplicate package id {package.id!r}: already registered from "
                f"{existing.package_dir.name!r}, cannot also register "
                f"{package.package_dir.name!r}"
            )
        self.packages.append(package)

    def match(self, event: TimelineEvent) -> list[RuntimePackage]:
        """依 manifest structural pattern 過濾 package。

        v1 只支援 when.tools。空 tools 表示不限制 tool。
        """
        matched: list[RuntimePackage] = []
        for package in self.packages:
            tools = package.manifest.when_tools
            if tools and event.tool_name not in tools:
                continue
            matched.append(package)
        return matched

    def by_id(self, package_id: str) -> RuntimePackage | None:
        """依 package id 查詢。"""
        for package in self.packages:
            if package.id == package_id:
                return package
        return None


def discover_and_register_packages(package_dirs: Sequence[Path]) -> PackageRegistry:
    """Two-pass 安全組裝：Pass 1 發現＋驗證全部 manifest（零 import），
    Pass 2 才真正建立 `PackageRegistry`。

    - **Pass 1**：對每個目錄呼叫 `load_manifest()`（已包含 duplicate YAML
      key 偵測與 entrypoint containment），個別 manifest 解析失敗沿用既有
      容錯政策（記 log、跳過該 package，不影響其他 package——與既有
      `PackageLoader.load_many()` 的既有政策一致，本函式未改變這件事）。
      成功解析的 manifest 全部收集後，呼叫
      `identity.validate_registry_uniqueness()` 做跨 package 的
      duplicate package/action id 檢查——**這一步是 all-or-nothing**：
      只要偵測到任何一筆 duplicate，就整批拒絕（`raise`），不嘗試判斷
      「哪一個是合法的、哪一個是重複的」贏家（沒有這種贏家判斷邏輯）。
    - **Pass 2**：只有 Pass 1 完全通過才會執行——重新載入每個已驗證的
      package（`PackageLoader.load()` 一樣不 import handler，這裡只是
      建立 `RuntimePackage` 物件），逐一 `register()`。個別 package 在
      這一步理論上不應該再失敗（Pass 1 已驗證過），若因為極端的
      same-UID race condition失敗，沿用相同的容錯政策（跳過、記 log），
      不中止整批。

    呼叫端（daemon startup）若要在偵測到 duplicate 時降級為「零 package
    可用但 daemon 繼續運作」，應自行 catch
    `DuplicatePackageIdError`/`DuplicatePackageActionIdError` 並決定
    fallback——本函式本身不做這個政策決定，只負責誠實回報衝突。
    """
    manifests: list[tuple[str, PackageManifest]] = []
    ordered_dirs = sorted(package_dirs, key=lambda p: p.name)

    for package_dir in ordered_dirs:
        label = package_dir.name
        try:
            manifest = load_manifest(package_dir / "package.yaml")
        except ManifestError as exc:
            _log.warning("Package manifest failed: %s: %s", label, exc)
            continue
        manifests.append((label, manifest))

    # Pass 1b：cross-package uniqueness，all-or-nothing。
    identity.validate_registry_uniqueness(manifests)

    # Pass 2：只有 Pass 1 全部通過才執行。
    registry = PackageRegistry()
    loader = PackageLoader()
    validated_labels = {label for label, _ in manifests}
    for package_dir in ordered_dirs:
        if package_dir.name not in validated_labels:
            continue
        try:
            package = loader.load(package_dir)
        except Exception as exc:
            _log.warning("Package load failed in pass 2: %s: %s", package_dir.name, exc)
            continue
        registry.register(package)

    return registry
