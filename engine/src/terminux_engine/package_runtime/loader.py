# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package loader。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from terminux_engine.package_runtime.context import PackageContext
from terminux_engine.package_runtime.entrypoint import (
    EntrypointError,
    ResolvedEntrypoint,
)
from terminux_engine.package_runtime.entrypoint import (
    resolve_entrypoint as resolve_canonical_entrypoint,
)
from terminux_engine.package_runtime.errors import ManifestError, PackageLoadError
from terminux_engine.package_runtime.manifest import PackageManifest, load_manifest
from terminux_engine.package_runtime.result import PhaseResult

_log = logging.getLogger(__name__)


@dataclass
class RuntimePackage:
    """Loaded package instance。"""

    package_dir: Path
    manifest: PackageManifest

    @property
    def id(self) -> str:
        """回傳 package id。"""
        return self.manifest.package_id

    @property
    def version(self) -> str:
        """回傳 package version。"""
        return self.manifest.version

    def has_phase(self, phase: str) -> bool:
        """檢查 package 是否宣告 phase。"""
        return self.manifest.has_phase(phase)

    def call_phase(self, phase: str, ctx: PackageContext) -> PhaseResult:
        """呼叫 package phase entrypoint。"""
        entrypoint = self.manifest.phase_entrypoint(phase)
        func = self.resolve_entrypoint(entrypoint)
        return func(ctx)

    def resolve_action(self, action_name: str) -> Callable[..., Any]:
        """解析 manifest 宣告的 action。"""
        entrypoint = self.manifest.actions[action_name]
        return self.resolve_entrypoint(entrypoint)

    def resolve_entrypoint(self, entrypoint: str) -> Callable[..., Any]:
        """解析 module.function entrypoint。

        每次呼叫都重新透過 canonical resolver（`entrypoint.py`）做完整
        grammar + containment 檢查——不信任 manifest 階段留下的任何快取
        路徑或字串，等於「即使 parser 與 loader 之間存在時間窗，loader
        也是重新驗證一次」。
        """
        try:
            resolved = resolve_canonical_entrypoint(self.package_dir, entrypoint)
        except EntrypointError as exc:
            raise PackageLoadError(f"invalid entrypoint {entrypoint!r}: {exc}") from exc
        module = self._load_module(resolved)
        func = getattr(module, resolved.ref.function_name, None)
        if not callable(func):
            raise PackageLoadError(f"entrypoint is not callable: {entrypoint}")
        return func

    def runtime_module_name(self, module_name: str) -> str:
        """回傳 handler module 在 `sys.modules` 內的名稱；子類別可改用獨立 namespace。"""
        safe_id = self.id.replace("-", "_")
        return f"terminux_package_runtime.{safe_id}.{module_name}"

    def _load_module(self, resolved: ResolvedEntrypoint) -> ModuleType:
        module_path = resolved.source_path
        runtime_module_name = self.runtime_module_name(resolved.ref.module_name)
        spec = importlib.util.spec_from_file_location(runtime_module_name, module_path)
        if spec is None or spec.loader is None:
            raise PackageLoadError(f"cannot import module: {module_path}")
        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules[runtime_module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(runtime_module_name, None)
            raise PackageLoadError(f"module import failed: {module_path}: {exc}") from exc
        return module


class PackageLoader:
    """載入 package directory。"""

    def load(self, package_dir: str | Path) -> RuntimePackage:
        """載入單一 package。"""
        root = Path(package_dir)
        manifest_path = root / "package.yaml"
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            raise
        except Exception as exc:
            raise PackageLoadError(f"failed to load package: {root}: {exc}") from exc
        return RuntimePackage(package_dir=root, manifest=manifest)

    def load_many(self, package_dirs: list[str | Path]) -> list[RuntimePackage]:
        """載入多個 package；失敗者記錄 log 後跳過。"""
        packages: list[RuntimePackage] = []
        for package_dir in package_dirs:
            try:
                packages.append(self.load(package_dir))
            except Exception as exc:
                _log.warning("Package load failed: %s", exc)
        return packages
