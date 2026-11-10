# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime exception types。"""

from __future__ import annotations


class PackageRuntimeError(Exception):
    """Package Runtime 的基底錯誤。"""


class ManifestError(PackageRuntimeError):
    """package.yaml validation 失敗。"""


class PackageLoadError(PackageRuntimeError):
    """package 載入失敗。"""


class ActionRegistryError(PackageRuntimeError):
    """action 註冊或解析失敗。"""


class DuplicatePackageIdError(PackageRuntimeError):
    """package id 與已註冊的另一個 package 衝突。"""


class DuplicatePackageActionIdError(PackageRuntimeError):
    """action id 與已註冊的另一個 action 衝突。"""


class DuplicateManifestKeyError(PackageRuntimeError):
    """package.yaml 內同一個 mapping 出現重複 key。"""
