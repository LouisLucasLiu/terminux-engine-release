# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Runtime — canonical Python entrypoint grammar 與 resolution。

Domain-blind、single source of truth：`manifest.py`（parser）、
`loader.py`（production loader）、`validator.py`（offline validator）
三者共用本模組做 `module.function` entrypoint 的語法檢查與檔案系統
containment 檢查，不各自重寫 regex 或路徑拼接邏輯。

Grammar：`<module>.<function>`，兩側各自必須是合法 Python identifier
（`str.isidentifier()`），恰好一個 `.`。Target 固定為
`<package_root>/<module>.py`，必須存在、是 regular file、不是
symlink、resolve 後 parent 精確等於 resolved package root。

本模組**不 import** 任何 package 程式碼，只做 `Path` 操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from terminux_engine.package_runtime.errors import PackageRuntimeError


class EntrypointError(PackageRuntimeError):
    """Entrypoint grammar／resolution 失敗的共同基底。"""


class MalformedEntrypointError(EntrypointError):
    """Entrypoint 字串不符合 `<module>.<function>` grammar。"""


class EntrypointSourceMissingError(EntrypointError):
    """Entrypoint module 檔案不存在，或存在但不是 regular file。"""


class EntrypointPathEscapeError(EntrypointError):
    """Entrypoint module resolve 後的路徑跳脫了 package root。"""


class EntrypointSymlinkSourceError(EntrypointError):
    """Entrypoint module 本身是 symlink（無論 symlink 目標是否落在 root 內）。"""


@dataclass(frozen=True)
class EntrypointRef:
    """解析後、尚未接觸檔案系統的 entrypoint 語法結構。"""

    original: str
    module_name: str
    function_name: str


@dataclass(frozen=True)
class ResolvedEntrypoint:
    """通過 grammar 與 filesystem containment 驗證的 entrypoint。"""

    ref: EntrypointRef
    source_path: Path


def parse_entrypoint(value: str) -> EntrypointRef:
    """純 grammar 檢查，不碰檔案系統。

    `str.isidentifier()` 天然拒絕空字串、前後/內部空白、`/`、`\\`、
    `..`、絕對路徑、NUL/control character、URI-like（含 `:`）與
    Windows drive-like（含 `:`/`\\`）等全部非法形式，不需要另外維護
    一份字元黑名單。
    """
    if not isinstance(value, str) or not value:
        raise MalformedEntrypointError(
            f"entrypoint must be a non-empty string: {value!r}"
        )
    parts = value.split(".")
    if len(parts) != 2:
        raise MalformedEntrypointError(
            f"entrypoint must be exactly module.function (one dot): {value!r}"
        )
    module_name, function_name = parts
    if not module_name.isidentifier() or not function_name.isidentifier():
        raise MalformedEntrypointError(
            f"entrypoint module and function must be valid Python identifiers: {value!r}"
        )
    return EntrypointRef(original=value, module_name=module_name, function_name=function_name)


def resolve_entrypoint(package_root: Path, value: str) -> ResolvedEntrypoint:
    """完整 grammar + filesystem containment 驗證，回傳 canonical source path。

    不 import package 程式碼。`package_root` 必須是呼叫端已知、可信的
    package 目錄（不是從 entrypoint 字串本身推導）。
    """
    ref = parse_entrypoint(value)
    resolved_root = package_root.resolve()
    candidate = package_root / f"{ref.module_name}.py"

    if candidate.is_symlink():
        raise EntrypointSymlinkSourceError(
            f"entrypoint module must not be a symlink: {candidate}"
        )
    if not candidate.exists():
        raise EntrypointSourceMissingError(
            f"entrypoint module file not found: {candidate}"
        )
    if not candidate.is_file():
        raise EntrypointSourceMissingError(
            f"entrypoint module is not a regular file: {candidate}"
        )

    resolved_candidate = candidate.resolve()
    if resolved_candidate.parent != resolved_root:
        raise EntrypointPathEscapeError(
            f"entrypoint module resolves outside the package root: {value!r}"
        )

    return ResolvedEntrypoint(ref=ref, source_path=resolved_candidate)
