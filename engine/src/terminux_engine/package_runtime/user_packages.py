# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""User-installed package：安裝根目錄、不可變 generation、探索與手動啟用。

目錄結構（全部由本 UID 擁有）::

    <home>/.terminux/packages/                 0700
        .lock                                  install／uninstall／GC 互斥
        .staging/                              同一檔案系統上的暫存區
        <package_id>/                          0700
            current.json                       0600，指向目前安裝的 generation
            generations/<generation>/          不可變 snapshot（目錄 0500、檔案 0400）

設計約束：

- `after.py` 每次 dispatch 都會從磁碟重新 import，因此已發布的 generation
  目錄內容永不改寫。更新是「新增一個 generation＋原子替換 `current.json`」，
  in-flight dispatch 持有的 `RuntimePackage` 仍指向舊 generation。
- 只有 daemon 端的 `UserPackageManager` 會刪除 generation，而且只刪除既不是
  `current.json` 指向、也不被目前啟用或仍有 lease 的 activation 引用的目錄。
- User package 是同 UID 的 Python 程式碼，沒有 sandbox；這裡的檢查只確保
  格式正確、輸入有界，以及使用者 package 無法影響內建 package。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from terminux_engine.ipc import agent_socket
from terminux_engine.package_runtime import validator as package_validator
from terminux_engine.package_runtime.errors import ManifestError, PackageRuntimeError
from terminux_engine.package_runtime.loader import RuntimePackage
from terminux_engine.package_runtime.manifest import PackageManifest, load_manifest

_log = logging.getLogger(__name__)

USER_PACKAGES_DIR_NAME = "packages"
POINTER_FILE_NAME = "current.json"
GENERATIONS_DIR_NAME = "generations"
POINTER_SCHEMA = 1
DISABLE_ENV = "TERMINUX_DISABLE_USER_PACKAGES"

BUILTIN_PACKAGES_DIR = Path(__file__).resolve().parents[1] / "packages"

MAX_PACKAGE_FILES = 256
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 8
_MAX_POINTER_BYTES = 64 * 1024
_LOCK_TIMEOUT_SEC = 10.0

# 安裝時略過的開發產物；其餘檔案（含隱藏檔）都會進入 snapshot 與 digest。
_EXCLUDED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".git"})
_EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")

PACKAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

TRUST_WARNING = (
    "This Package runs Python under your account in the Terminux daemon; it is not "
    "sandboxed. Validation checks Package structure, not whether the code is safe. "
    "Install only code you trust."
)

# ── 機器可讀的原因碼 ───────────────────────────────────────────────────
ROOT_MISSING = "USER_PACKAGE_ROOT_MISSING"
ROOT_INVALID = "USER_PACKAGE_ROOT_INVALID"
ROOT_BUSY = "USER_PACKAGE_ROOT_BUSY"
PATH_UNSAFE = "USER_PACKAGE_PATH_UNSAFE"
ID_INVALID = "USER_PACKAGE_ID_INVALID"
ID_MISMATCH = "USER_PACKAGE_ID_MISMATCH"
ID_SHADOWS_BUILTIN = "USER_PACKAGE_ID_SHADOWS_BUILTIN"
DUPLICATE_ID = "USER_PACKAGE_DUPLICATE_ID"
MANIFEST_INVALID = "USER_PACKAGE_MANIFEST_INVALID"
V1_ACTIONS_NOT_ALLOWED = "USER_PACKAGE_V1_ACTIONS_NOT_ALLOWED"
V1_PHASE_NOT_ALLOWED = "USER_PACKAGE_V1_PHASE_NOT_ALLOWED"
V1_PROMPT_PHASE_NOT_ALLOWED = "USER_PACKAGE_V1_PROMPT_PHASE_NOT_ALLOWED"
V1_AFTER_REQUIRED = "USER_PACKAGE_V1_AFTER_REQUIRED"
V1_WHEN_TOOLS_REQUIRED = "USER_PACKAGE_V1_WHEN_TOOLS_REQUIRED"
SOURCE_INVALID = "USER_PACKAGE_SOURCE_INVALID"
SOURCE_SYMLINK = "USER_PACKAGE_SOURCE_SYMLINK"
SOURCE_SPECIAL_FILE = "USER_PACKAGE_SOURCE_SPECIAL_FILE"
SOURCE_TOO_LARGE = "USER_PACKAGE_SOURCE_TOO_LARGE"
ALREADY_INSTALLED = "USER_PACKAGE_ALREADY_INSTALLED"
NOT_INSTALLED = "USER_PACKAGE_NOT_INSTALLED"
POINTER_INVALID = "USER_PACKAGE_POINTER_INVALID"
GENERATION_MISSING = "USER_PACKAGE_GENERATION_MISSING"
DIGEST_MISMATCH = "USER_PACKAGE_DIGEST_MISMATCH"
DISCOVERY_FAILED = "USER_PACKAGE_DISCOVERY_FAILED"
ACTIVATION_FAILED = "USER_PACKAGE_ACTIVATION_FAILED"
GC_FAILED = "USER_PACKAGE_GC_FAILED"

# Record 狀態
RECORD_LOADED = "LOADED"
RECORD_REJECTED = "REJECTED"
RECORD_RETAINED_PREVIOUS = "RETAINED_PREVIOUS"


class UserPackageError(PackageRuntimeError):
    """User package 操作失敗；`code` 為穩定的原因碼，`details` 為補充說明。"""

    def __init__(self, code: str, message: str, *, details: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": list(self.details)}


# ── 基本檢查 ───────────────────────────────────────────────────────────


def user_packages_disabled() -> bool:
    """`TERMINUX_DISABLE_USER_PACKAGES` 只停用 user package；不影響內建 package。"""
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes")


def check_package_id(package_id: str) -> None:
    """User package id 只允許小寫英數與底線，也因此不可能形成路徑。"""
    if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
        raise UserPackageError(
            ID_INVALID,
            f"invalid user package id {package_id!r}: must match {PACKAGE_ID_RE.pattern}",
        )


def _check_private_path(path: Path, *, expect_dir: bool, forbidden_mode: int = 0o022) -> os.stat_result:
    """lstat 檢查：不可為 symlink、型別正確、owner 為本 UID、group／other 不可寫。"""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UserPackageError(PATH_UNSAFE, f"cannot inspect {path}: {exc}") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise UserPackageError(PATH_UNSAFE, f"{path} is a symlink")
    if expect_dir and not stat.S_ISDIR(metadata.st_mode):
        raise UserPackageError(PATH_UNSAFE, f"{path} is not a directory")
    if not expect_dir and not stat.S_ISREG(metadata.st_mode):
        raise UserPackageError(PATH_UNSAFE, f"{path} is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise UserPackageError(
            PATH_UNSAFE, f"{path} is owned by uid {metadata.st_uid}, expected {os.geteuid()}")
    if stat.S_IMODE(metadata.st_mode) & forbidden_mode:
        raise UserPackageError(
            PATH_UNSAFE,
            f"{path} has mode {stat.S_IMODE(metadata.st_mode):04o}; "
            "it must not be writable by group or other")
    return metadata


def _ensure_private_dir(path: Path) -> Path:
    """建立（0700）或重用一個本 UID 私有目錄。"""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise UserPackageError(PATH_UNSAFE, f"cannot create {path}: {exc}") from None
    _check_private_path(path, expect_dir=True, forbidden_mode=0o077)
    return path


def resolve_user_packages_root(*, create: bool = False) -> Path:
    """解析 `<home>/.terminux/packages`。

    home 由 `ipc.agent_socket.home_for_uid()` 依 effective UID 解析（支援
    `TERMINUX_HOME_DIR` 隔離覆寫，不讀 `$HOME`），`.terminux` 的檢查與 daemon
    socket 目錄相同；`packages` 本身必須是 0700。只有安裝流程會 `create`。
    """
    uid = agent_socket.current_uid()
    try:
        home = agent_socket.home_for_uid(uid)
        if agent_socket.HOME_OVERRIDE_ENV not in os.environ:
            agent_socket._check_home(home, uid=uid, expected=str(home))
    except KeyError as exc:
        raise UserPackageError(ROOT_INVALID, f"no home directory for uid {uid}: {exc}") from None
    except agent_socket.AgentSocketError as exc:
        raise UserPackageError(ROOT_INVALID, str(exc)) from None

    terminux_dir = home / agent_socket.AGENT_SOCKET_DIR_NAME
    root = terminux_dir / USER_PACKAGES_DIR_NAME
    for directory, forbidden in ((terminux_dir, 0o022), (root, 0o077)):
        if create and not os.path.lexists(directory):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise UserPackageError(ROOT_INVALID, f"cannot create {directory}: {exc}") from None
        try:
            agent_socket._check_directory(
                directory, uid=uid, expected=str(root),
                missing_hint="no user package has been installed yet",
                forbidden_mode=forbidden)
        except agent_socket.AgentSocketError as exc:
            code = ROOT_MISSING if exc.code == agent_socket.RUNTIME_DIR_MISSING else ROOT_INVALID
            raise UserPackageError(code, str(exc)) from None
    return root


def builtin_package_ids(builtin_dir: Path = BUILTIN_PACKAGES_DIR) -> frozenset[str]:
    """內建 package 的目錄名稱與 manifest id；不依賴內建 registry 是否組裝成功。"""
    ids: set[str] = set()
    if not builtin_dir.is_dir():
        return frozenset()
    for entry in builtin_dir.iterdir():
        manifest_path = entry / "package.yaml"
        if not manifest_path.is_file():
            continue
        ids.add(entry.name)
        try:
            ids.add(load_manifest(manifest_path).package_id)
        except ManifestError:
            continue
    return frozenset(ids)


# ── Snapshot 複製與 digest ────────────────────────────────────────────


@dataclass
class _Budget:
    files: int = 0
    total_bytes: int = 0


def _iter_tree(root: Path, *, skip_excluded: bool) -> Iterator[tuple[str, Path, os.stat_result]]:
    """依名稱排序走訪，lstat 每個項目；symlink、特殊檔與超出深度一律拒絕。"""

    def walk(directory: Path, prefix: str, depth: int) -> Iterator[tuple[str, Path, os.stat_result]]:
        if depth > MAX_DIRECTORY_DEPTH:
            raise UserPackageError(
                SOURCE_TOO_LARGE, f"directory nesting exceeds {MAX_DIRECTORY_DEPTH} levels at {prefix or '.'}")
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise UserPackageError(SOURCE_INVALID, f"cannot list {directory}: {exc}") from None
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise UserPackageError(SOURCE_INVALID, f"cannot inspect {relative}: {exc}") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise UserPackageError(SOURCE_SYMLINK, f"symlinks are not allowed: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                if skip_excluded and entry.name in _EXCLUDED_DIR_NAMES:
                    continue
                yield relative, Path(entry.path), metadata
                yield from walk(Path(entry.path), f"{relative}/", depth + 1)
            elif stat.S_ISREG(metadata.st_mode):
                if skip_excluded and entry.name.endswith(_EXCLUDED_FILE_SUFFIXES):
                    continue
                yield relative, Path(entry.path), metadata
            else:
                raise UserPackageError(
                    SOURCE_SPECIAL_FILE, f"only regular files and directories are allowed: {relative}")

    yield from walk(root, "", 0)


def _read_bounded(path: Path, budget: _Budget, relative: str) -> Iterator[bytes]:
    """O_NOFOLLOW 開檔後以 fstat 再確認，邊讀邊累計大小上限。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise UserPackageError(SOURCE_INVALID, f"cannot open {relative}: {exc}") from None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UserPackageError(SOURCE_SPECIAL_FILE, f"not a regular file: {relative}")
        budget.files += 1
        if budget.files > MAX_PACKAGE_FILES:
            raise UserPackageError(SOURCE_TOO_LARGE, f"package has more than {MAX_PACKAGE_FILES} files")
        file_bytes = 0
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            file_bytes += len(chunk)
            budget.total_bytes += len(chunk)
            if file_bytes > MAX_FILE_BYTES:
                raise UserPackageError(
                    SOURCE_TOO_LARGE, f"{relative} is larger than {MAX_FILE_BYTES} bytes")
            if budget.total_bytes > MAX_PACKAGE_BYTES:
                raise UserPackageError(
                    SOURCE_TOO_LARGE, f"package is larger than {MAX_PACKAGE_BYTES} bytes")
            yield chunk


def _copy_snapshot(source: Path, destination: Path) -> None:
    """把 source 複製成全新的私有目錄樹；destination 必須不存在。"""
    budget = _Budget()
    destination.mkdir(mode=0o700)
    for relative, path, metadata in _iter_tree(source, skip_excluded=True):
        target = destination / relative
        if stat.S_ISDIR(metadata.st_mode):
            budget.files += 1
            if budget.files > MAX_PACKAGE_FILES:
                raise UserPackageError(
                    SOURCE_TOO_LARGE, f"package has more than {MAX_PACKAGE_FILES} files and directories")
            target.mkdir(mode=0o700)
            continue
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "wb") as output:
            for chunk in _read_bounded(path, budget, relative):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())


def compute_tree_digest(root: Path) -> str:
    """Package 內容 digest：依相對路徑排序，涵蓋每個檔案的路徑、大小與 sha256。"""
    budget = _Budget()
    tree = hashlib.sha256(b"terminux-user-package-tree-v1\0")
    for relative, path, metadata in _iter_tree(root, skip_excluded=False):
        if stat.S_ISDIR(metadata.st_mode):
            continue
        content = hashlib.sha256()
        size = 0
        for chunk in _read_bounded(path, budget, relative):
            content.update(chunk)
            size += len(chunk)
        tree.update(relative.encode("utf-8", "surrogateescape") + b"\0"
                    + str(size).encode() + b"\0" + content.hexdigest().encode() + b"\n")
    return f"sha256:{tree.hexdigest()}"


def _freeze_contents(root: Path) -> None:
    """已發布的 generation 內容設為唯讀（目錄 0500、檔案 0400）；最上層由呼叫端處理。"""
    for _relative, path, metadata in sorted(
            _iter_tree(root, skip_excluded=False), key=lambda item: item[0].count("/"), reverse=True):
        os.chmod(path, 0o500 if stat.S_ISDIR(metadata.st_mode) else 0o400, follow_symlinks=False)


def _remove_tree(path: Path) -> None:
    """移除本模組建立的目錄樹；先恢復目錄寫入權限，且絕不跟隨 symlink。"""
    try:
        top = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(top.st_mode) or not stat.S_ISDIR(top.st_mode):
        path.unlink()
        return
    for current, directories, _files in os.walk(path, topdown=True, followlinks=False):
        os.chmod(current, 0o700, follow_symlinks=False)
        for name in directories:
            child = Path(current) / name
            if not child.is_symlink():
                os.chmod(child, 0o700, follow_symlinks=False)
    shutil.rmtree(path)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ── Pointer（current.json） ───────────────────────────────────────────


@dataclass(frozen=True)
class InstalledPointer:
    """`current.json`：目前安裝的 generation。"""

    package_id: str
    version: str
    generation: str
    digest: str
    installed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POINTER_SCHEMA,
            "package_id": self.package_id,
            "version": self.version,
            "generation": self.generation,
            "digest": self.digest,
            "installed_at": self.installed_at,
        }


def read_pointer(package_dir: Path) -> InstalledPointer | None:
    """讀取 `current.json`；不存在回傳 `None`，格式錯誤 raise `POINTER_INVALID`。"""
    path = package_dir / POINTER_FILE_NAME
    try:
        _check_private_path(path, expect_dir=False)
    except FileNotFoundError:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(_MAX_POINTER_BYTES + 1)
    except OSError as exc:
        raise UserPackageError(POINTER_INVALID, f"cannot read {path}: {exc}") from None
    if len(raw) > _MAX_POINTER_BYTES:
        raise UserPackageError(POINTER_INVALID, f"{path} is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserPackageError(POINTER_INVALID, f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != POINTER_SCHEMA:
        raise UserPackageError(POINTER_INVALID, f"{path} has an unsupported schema")
    values = {key: data.get(key) for key in
              ("package_id", "version", "generation", "digest", "installed_at")}
    if not all(isinstance(value, str) and value for value in values.values()):
        raise UserPackageError(POINTER_INVALID, f"{path} is missing required fields")
    if not GENERATION_RE.fullmatch(values["generation"]):
        raise UserPackageError(POINTER_INVALID, f"{path} names an invalid generation")
    if not _DIGEST_RE.fullmatch(values["digest"]):
        raise UserPackageError(POINTER_INVALID, f"{path} has an invalid digest")
    return InstalledPointer(**values)


def _write_pointer(package_dir: Path, pointer: InstalledPointer) -> None:
    """同目錄暫存檔＋fsync＋`os.replace`：reader 只會看到舊或新的完整內容。"""
    payload = (json.dumps(pointer.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".current-", suffix=".tmp", dir=package_dir)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, package_dir / POINTER_FILE_NAME)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(package_dir)


def _new_generation_name(digest: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{digest.split(':', 1)[1][:12]}"


# ── 互斥鎖 ─────────────────────────────────────────────────────────────


@contextmanager
def _root_lock(root: Path, *, blocking: bool) -> Iterator[bool]:
    """`<root>/.lock` 的 flock。blocking=False 時拿不到鎖就 yield False。"""
    path = root / ".lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise UserPackageError(PATH_UNSAFE, f"{path} is not a private regular file")
        deadline = time.monotonic() + (_LOCK_TIMEOUT_SEC if blocking else 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    if blocking:
                        raise UserPackageError(
                            ROOT_BUSY, "another install, uninstall or cleanup is in progress") from None
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── V1 政策 ────────────────────────────────────────────────────────────


def v1_policy_violations(manifest: PackageManifest) -> list[tuple[str, str]]:
    """V1 user package 只允許 Python `after` phase，且必須宣告 `when.tools`。"""
    violations: list[tuple[str, str]] = []
    if manifest.actions:
        violations.append((V1_ACTIONS_NOT_ALLOWED,
                           "user packages cannot declare actions in V1: "
                           + ", ".join(sorted(manifest.actions))))
    if manifest.prompt_phases:
        violations.append((V1_PROMPT_PHASE_NOT_ALLOWED,
                           "user packages cannot declare prompt phases in V1: "
                           + ", ".join(sorted(manifest.prompt_phases))))
    other_phases = sorted(set(manifest.phases) - {"after"})
    if other_phases:
        violations.append((V1_PHASE_NOT_ALLOWED,
                           "user packages may only declare the Python 'after' phase in V1: "
                           + ", ".join(other_phases)))
    if "after" not in manifest.phases:
        violations.append((V1_AFTER_REQUIRED, "user packages must declare a Python 'after' phase"))
    if not manifest.when_tools:
        violations.append((V1_WHEN_TOOLS_REQUIRED,
                           "user packages must list at least one tool in when.tools"))
    return violations


def _validate_candidate(package_dir: Path, *, expected_id: str,
                        builtin_ids: frozenset[str]) -> PackageManifest:
    """安裝與探索共用：validator＋id 一致性＋內建衝突＋V1 政策。"""
    if expected_id in builtin_ids:
        raise UserPackageError(
            ID_SHADOWS_BUILTIN,
            f"package id {expected_id!r} is used by a built-in package; choose a different id")
    try:
        report, manifest = package_validator.validate_package_dir(package_dir)
    except package_validator.ValidatorInternalError as exc:
        raise UserPackageError(MANIFEST_INVALID, str(exc)) from None
    findings = tuple(
        f"[{finding.severity}] {finding.code}"
        + (f" ({finding.field})" if finding.field else "")
        + f": {finding.message}"
        for finding in report.findings
    )
    violations = v1_policy_violations(manifest) if manifest is not None else []
    if violations:
        code, message = violations[0]
        raise UserPackageError(
            code, message,
            details=tuple(f"{c}: {m}" for c, m in violations[1:]) + findings)
    if not report.valid or manifest is None:
        errors = [finding for finding in report.findings if finding.severity == "ERROR"]
        summary = errors[0].message if errors else "validation failed"
        raise UserPackageError(MANIFEST_INVALID, summary, details=findings)
    if manifest.package_id != expected_id:
        raise UserPackageError(
            ID_MISMATCH,
            f"manifest package.id {manifest.package_id!r} does not match {expected_id!r}")
    return manifest


# ── 安裝／移除 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallResult:
    """單次安裝的結果。`status` 為 INSTALLED／REPLACED／UNCHANGED。"""

    status: str
    package_id: str
    version: str
    generation: str
    digest: str
    package_dir: Path
    previous: InstalledPointer | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "package_id": self.package_id,
            "version": self.version,
            "generation": self.generation,
            "digest": self.digest,
            "package_dir": str(self.package_dir),
            "previous": self.previous.to_dict() if self.previous else None,
            "warnings": list(self.warnings),
            "activated": False,
            "trust_warning": TRUST_WARNING,
        }


def _peek_manifest_id(package_dir: Path) -> str:
    try:
        return load_manifest(package_dir / "package.yaml").package_id
    except ManifestError as exc:
        raise UserPackageError(MANIFEST_INVALID, str(exc)) from None


def install_package(
    source_dir: str | Path,
    *,
    replace: bool = False,
    root: Path | None = None,
    builtin_ids: frozenset[str] | None = None,
) -> InstallResult:
    """把 source_dir 驗證後複製成新的不可變 generation，並原子切換 `current.json`。

    驗證對象是 staging 內的複本（不是 source），因此 source 在複製期間被改動
    也不會讓未驗證的內容被發布。任何失敗都不會改動既有的 `current.json`。
    本函式不啟用 daemon 內的 package；啟用由 `extensions.reload` 負責。
    """
    source = Path(source_dir)
    try:
        source_metadata = source.lstat()
    except OSError as exc:
        raise UserPackageError(SOURCE_INVALID, f"cannot read source directory {source}: {exc}") from None
    if stat.S_ISLNK(source_metadata.st_mode):
        raise UserPackageError(SOURCE_SYMLINK, f"source directory must not be a symlink: {source}")
    if not stat.S_ISDIR(source_metadata.st_mode):
        raise UserPackageError(SOURCE_INVALID, f"source is not a directory: {source}")

    root = root if root is not None else resolve_user_packages_root(create=True)
    builtin = builtin_ids if builtin_ids is not None else builtin_package_ids()

    with _root_lock(root, blocking=True):
        staging_parent = _ensure_private_dir(root / ".staging")
        work = Path(tempfile.mkdtemp(prefix="install-", dir=staging_parent))
        try:
            candidate = work / "candidate"
            _copy_snapshot(source, candidate)
            package_id = _peek_manifest_id(candidate)
            check_package_id(package_id)
            staged = work / package_id
            candidate.rename(staged)
            manifest = _validate_candidate(staged, expected_id=package_id, builtin_ids=builtin)
            digest = compute_tree_digest(staged)

            package_dir = root / package_id
            previous: InstalledPointer | None = None
            if os.path.lexists(package_dir):
                _check_private_path(package_dir, expect_dir=True)
                try:
                    previous = read_pointer(package_dir)
                except UserPackageError:
                    if not replace:
                        raise
                    previous = None
            if previous is not None and not replace:
                raise UserPackageError(
                    ALREADY_INSTALLED,
                    f"{package_id} {previous.version} is already installed "
                    f"(generation {previous.generation}); use --replace to update it")
            if previous is not None and previous.digest == digest:
                return InstallResult(
                    status="UNCHANGED", package_id=package_id, version=previous.version,
                    generation=previous.generation, digest=digest, package_dir=package_dir,
                    previous=previous)

            _ensure_private_dir(package_dir)
            generations = _ensure_private_dir(package_dir / GENERATIONS_DIR_NAME)
            generation = _new_generation_name(digest)
            while os.path.lexists(generations / generation):
                time.sleep(0.001)
                generation = _new_generation_name(digest)
            _freeze_contents(staged)
            # 跨目錄 rename 目錄需要對該目錄本身有寫入權限，因此最上層在 rename 後才設唯讀。
            os.rename(staged, generations / generation)
            os.chmod(generations / generation, 0o500)
            _fsync_dir(generations)
            pointer = InstalledPointer(
                package_id=package_id, version=manifest.version, generation=generation,
                digest=digest, installed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            _write_pointer(package_dir, pointer)
            return InstallResult(
                status="REPLACED" if previous is not None else "INSTALLED",
                package_id=package_id, version=manifest.version, generation=generation,
                digest=digest, package_dir=package_dir, previous=previous)
        finally:
            _remove_tree(work)


@dataclass(frozen=True)
class UninstallResult:
    """移除結果：`current.json` 已刪除；generation 由 daemon 在不再使用後清理。"""

    package_id: str
    previous: InstalledPointer | None

    def to_dict(self) -> dict[str, Any]:
        return {"status": "UNINSTALLED", "package_id": self.package_id,
                "previous": self.previous.to_dict() if self.previous else None,
                "activated": False}


def uninstall_package(package_id: str, *, root: Path | None = None) -> UninstallResult:
    """移除 `current.json`。不直接刪除 generation，避免刪掉 daemon 正在執行的程式碼。"""
    check_package_id(package_id)
    try:
        root = root if root is not None else resolve_user_packages_root(create=False)
    except UserPackageError as exc:
        if exc.code == ROOT_MISSING:
            raise UserPackageError(NOT_INSTALLED, f"{package_id} is not installed") from None
        raise
    with _root_lock(root, blocking=True):
        package_dir = root / package_id
        try:
            _check_private_path(package_dir, expect_dir=True)
        except FileNotFoundError:
            raise UserPackageError(NOT_INSTALLED, f"{package_id} is not installed") from None
        try:
            pointer = read_pointer(package_dir)
        except UserPackageError as exc:
            # 損壞的 pointer 也允許移除，讓使用者能回到乾淨狀態。
            try:
                (package_dir / POINTER_FILE_NAME).unlink()
            except OSError as unlink_error:
                raise UserPackageError(
                    POINTER_INVALID, f"{exc.message}; cannot remove it: {unlink_error}") from None
            _fsync_dir(package_dir)
            return UninstallResult(package_id=package_id, previous=None)
        if pointer is None:
            raise UserPackageError(NOT_INSTALLED, f"{package_id} is not installed")
        (package_dir / POINTER_FILE_NAME).unlink()
        _fsync_dir(package_dir)
        return UninstallResult(package_id=package_id, previous=pointer)


# ── 探索 ──────────────────────────────────────────────────────────────


@dataclass(eq=False)
class UserRuntimePackage(RuntimePackage):
    """指向單一不可變 generation 的 user package。"""

    generation: str = ""
    digest: str = ""

    def runtime_module_name(self, module_name: str) -> str:
        """與內建 package 分開的 namespace，且每個 generation 各自獨立。"""
        safe_generation = self.generation.replace("-", "_")
        return f"terminux_user_package.g{safe_generation}.{self.id}.{module_name}"


def _forget_generation_modules(generation: str) -> None:
    """移除已刪除 generation 在 `sys.modules` 留下的 handler module。"""
    prefix = f"terminux_user_package.g{generation.replace('-', '_')}."
    for name in [name for name in sys.modules if name.startswith(prefix)]:
        sys.modules.pop(name, None)


@dataclass(frozen=True)
class UserPackageRecord:
    """單一 user package 在一次 activation 中的結果。"""

    package_id: str
    status: str
    reason: str | None = None
    message: str | None = None
    version: str | None = None
    generation: str | None = None
    digest: str | None = None
    details: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "version": self.version,
            "generation": self.generation,
            "digest": self.digest,
            "details": list(self.details),
        }


def _load_installed(root: Path, package_id: str, builtin_ids: frozenset[str]) -> UserRuntimePackage | None:
    check_package_id(package_id)
    package_dir = root / package_id
    _check_private_path(package_dir, expect_dir=True)
    pointer = read_pointer(package_dir)
    if pointer is None:
        return None
    if pointer.package_id != package_id:
        raise UserPackageError(
            POINTER_INVALID, f"current.json names {pointer.package_id!r}, expected {package_id!r}")
    if package_id in builtin_ids:
        raise UserPackageError(
            ID_SHADOWS_BUILTIN,
            f"package id {package_id!r} is used by a built-in package; the built-in package wins")
    generation_dir = package_dir / GENERATIONS_DIR_NAME / pointer.generation
    try:
        _check_private_path(package_dir / GENERATIONS_DIR_NAME, expect_dir=True)
        _check_private_path(generation_dir, expect_dir=True)
    except FileNotFoundError:
        raise UserPackageError(
            GENERATION_MISSING, f"installed generation {pointer.generation} is missing") from None
    digest = compute_tree_digest(generation_dir)
    if digest != pointer.digest:
        raise UserPackageError(
            DIGEST_MISMATCH,
            f"generation {pointer.generation} content does not match its recorded digest; "
            "reinstall the package")
    manifest = _validate_candidate(generation_dir, expected_id=package_id, builtin_ids=builtin_ids)
    return UserRuntimePackage(
        package_dir=generation_dir, manifest=manifest,
        generation=pointer.generation, digest=pointer.digest)


def discover_user_packages(
    root: Path, *, builtin_ids: frozenset[str],
) -> tuple[list[UserRuntimePackage], list[UserPackageRecord]]:
    """逐一探索 user package；單一 package 的任何錯誤只拒絕它自己。

    沒有 `current.json` 的目錄（已移除、等待清理）不列入。重複 id 在目前的
    目錄結構下不會出現（id＝目錄名稱），仍保留防線：全部同 id 的項目都拒絕，
    不挑選贏家。
    """
    packages: list[UserRuntimePackage] = []
    records: list[UserPackageRecord] = []
    try:
        with os.scandir(root) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise UserPackageError(ROOT_INVALID, f"cannot list {root}: {exc}") from None
    for name in names:
        if name.startswith("."):
            continue
        try:
            package = _load_installed(root, name, builtin_ids)
        except UserPackageError as exc:
            records.append(UserPackageRecord(
                package_id=name, status=RECORD_REJECTED, reason=exc.code,
                message=exc.message, details=exc.details))
            continue
        except Exception as exc:
            _log.warning("User package discovery failed: %s: %s", name, exc)
            records.append(UserPackageRecord(
                package_id=name, status=RECORD_REJECTED, reason=DISCOVERY_FAILED,
                message=f"{type(exc).__name__}: {exc}"))
            continue
        if package is not None:
            packages.append(package)

    counts: dict[str, int] = {}
    for package in packages:
        counts[package.id] = counts.get(package.id, 0) + 1
    duplicated = {package_id for package_id, count in counts.items() if count > 1}
    accepted: list[UserRuntimePackage] = []
    for package in packages:
        if package.id in duplicated:
            records.append(UserPackageRecord(
                package_id=package.id, status=RECORD_REJECTED, reason=DUPLICATE_ID,
                message=f"package id {package.id!r} is installed more than once",
                version=package.version, generation=package.generation, digest=package.digest))
            continue
        accepted.append(package)
        records.append(UserPackageRecord(
            package_id=package.id, status=RECORD_LOADED, version=package.version,
            generation=package.generation, digest=package.digest))
    records.sort(key=lambda record: (record.package_id, record.status))
    return accepted, records


# ── Daemon 端的 activation ────────────────────────────────────────────


@dataclass(frozen=True)
class UserPackageActivation:
    """一次 activation 的不可變結果；dispatch 透過 lease 取得並全程使用同一份。"""

    activation_id: int
    activated_at: float
    disabled: bool
    packages: tuple[UserRuntimePackage, ...] = ()
    records: tuple[UserPackageRecord, ...] = ()
    error: dict[str, Any] | None = None
    # 同一次 activation 的使用者工具 scope（`tool_identity.user_catalog.UserToolScope`）。
    # 與 packages 一起發布，dispatch 不會看到「新 package＋舊 catalog」的組合。
    # None 表示沒有提供 tool scope builder，或 user extensions 已停用。
    tools: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "activation_id": self.activation_id,
            "activated_at": self.activated_at,
            "disabled": self.disabled,
            "error": self.error,
            "packages": [
                {"package_id": package.id, "version": package.version,
                 "generation": package.generation, "digest": package.digest}
                for package in self.packages
            ],
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True)
class GarbageReport:
    """清理結果。`skipped` 非 None 表示本次沒有執行清理。"""

    removed: tuple[str, ...] = ()
    in_use: tuple[str, ...] = ()
    skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"removed": list(self.removed), "in_use": list(self.in_use), "skipped": self.skipped}


@dataclass(frozen=True)
class ReloadOutcome:
    """`reload()` 的結果。失敗時 `activation` 是保留下來的前一次 activation。"""

    status: str  # "ACTIVATED" | "FAILED_PREVIOUS_RETAINED"
    activation: UserPackageActivation
    error: dict[str, Any] | None = None
    garbage: GarbageReport = field(default_factory=GarbageReport)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "error": self.error, "garbage": self.garbage.to_dict()}


class UserPackageManager:
    """管理 daemon 內目前啟用的 user package activation。

    - `reload()` 先完整建出 candidate，成功後才以單一 reference 發布；建構失敗時
      保留上一份 activation。
    - 單一 package 在 reload 時被拒絕，而上一份 activation 已載入它時，保留上一份
      的 generation（狀態 `RETAINED_PREVIOUS`），不因壞掉的新內容而讓可用的版本消失。
      已移除（沒有 `current.json`）的 package 則會正常卸載。
    - `lease()` 讓一次 dispatch 全程固定使用同一份 activation；被替換但仍有 lease
      的 activation 所引用的 generation 不會被清理。
    """

    def __init__(
        self,
        *,
        root_resolver: Callable[[], Path],
        builtin_ids: Callable[[], frozenset[str]],
        tool_scope_builder: Callable[[Any], Any] | None = None,
    ) -> None:
        self._root_resolver = root_resolver
        self._builtin_ids = builtin_ids
        # 以上一份 activation 的 tool scope 為參數建出下一份；拋例外時整次 reload 失敗。
        self._tool_scope_builder = tool_scope_builder
        self._state_lock = threading.Lock()
        self._reload_lock = threading.Lock()
        self._active: UserPackageActivation | None = None
        self._leases: dict[int, int] = {}
        self._retired: dict[int, UserPackageActivation] = {}
        self._counter = 0

    def active(self) -> UserPackageActivation | None:
        """目前發布中的 activation；尚未初始化時為 `None`。"""
        with self._state_lock:
            return self._active

    def ensure_active(self) -> UserPackageActivation:
        """第一次使用時做初始 activation（對應 daemon 啟動後的第一次載入）。"""
        current = self.active()
        if current is not None:
            return current
        with self._reload_lock:
            current = self.active()
            if current is not None:
                return current
            return self._reload_locked().activation

    def _next_id(self) -> int:
        with self._state_lock:
            self._counter += 1
            return self._counter

    def _build(self, previous: UserPackageActivation | None) -> UserPackageActivation:
        if user_packages_disabled():
            return UserPackageActivation(
                activation_id=self._next_id(), activated_at=time.time(), disabled=True)
        tools = (self._tool_scope_builder(previous.tools if previous is not None else None)
                 if self._tool_scope_builder is not None else None)
        try:
            root = self._root_resolver()
        except UserPackageError as exc:
            if exc.code == ROOT_MISSING:
                return UserPackageActivation(
                    activation_id=self._next_id(), activated_at=time.time(), disabled=False,
                    tools=tools)
            raise
        packages, records = discover_user_packages(root, builtin_ids=self._builtin_ids())
        loaded_ids = {package.id for package in packages}
        if previous is not None:
            previous_by_id = {package.id: package for package in previous.packages}
            merged_records: list[UserPackageRecord] = []
            for record in records:
                old = previous_by_id.get(record.package_id)
                if (record.status == RECORD_REJECTED and old is not None
                        and record.package_id not in loaded_ids
                        and record.reason != ID_SHADOWS_BUILTIN):
                    packages.append(old)
                    loaded_ids.add(old.id)
                    merged_records.append(UserPackageRecord(
                        package_id=record.package_id, status=RECORD_RETAINED_PREVIOUS,
                        reason=record.reason, message=record.message, version=old.version,
                        generation=old.generation, digest=old.digest, details=record.details))
                else:
                    merged_records.append(record)
            records = merged_records
        packages.sort(key=lambda package: package.id)
        return UserPackageActivation(
            activation_id=self._next_id(), activated_at=time.time(), disabled=False,
            packages=tuple(packages), records=tuple(records), tools=tools)

    def reload(self) -> ReloadOutcome:
        """重新探索並發布新的 activation；失敗時保留上一份。"""
        with self._reload_lock:
            return self._reload_locked()

    def _reload_locked(self) -> ReloadOutcome:
        previous = self.active()
        try:
            candidate = self._build(previous)
        except Exception as exc:
            # 帶穩定原因碼的錯誤（user package 或 tool scope builder）原樣回報。
            error = (exc.to_dict() if isinstance(getattr(exc, "code", None), str) and hasattr(exc, "to_dict")
                     else {"code": ACTIVATION_FAILED, "message": f"{type(exc).__name__}: {exc}",
                           "details": []})
            _log.warning("User package activation failed; previous activation kept: %s", error)
            if previous is None:
                # 尚未有任何 activation：發布一份空的，並記下錯誤，避免每次 dispatch 重試。
                # 工具 scope 仍盡量建出（只含內建工具時不會失敗），讓內建工具照常運作。
                tools = None
                if self._tool_scope_builder is not None:
                    try:
                        tools = self._tool_scope_builder(None)
                    except Exception as tool_exc:
                        _log.warning("User tool scope unavailable: %s", tool_exc)
                previous = UserPackageActivation(
                    activation_id=self._next_id(), activated_at=time.time(),
                    disabled=False, error=error, tools=tools)
                with self._state_lock:
                    self._active = previous
            return ReloadOutcome(status="FAILED_PREVIOUS_RETAINED", activation=previous, error=error)
        with self._state_lock:
            old = self._active
            self._active = candidate
            if old is not None and self._leases.get(old.activation_id, 0) > 0:
                self._retired[old.activation_id] = old
        try:
            garbage = self.collect_garbage()
        except Exception as exc:
            # 新 activation 已經發布；清理失敗只影響磁碟空間，下次 reload 再試。
            _log.warning("User package cleanup failed: %s", exc)
            garbage = GarbageReport(skipped=f"{GC_FAILED}: {type(exc).__name__}: {exc}")
        return ReloadOutcome(status="ACTIVATED", activation=candidate, garbage=garbage)

    def acquire(self) -> UserPackageActivation:
        """取得目前 activation 並登記一個 lease；必須以 `release()` 歸還。

        給需要跨執行緒持有同一份 activation 的呼叫端（例如：request handler
        以它驗證工具身分，再交給 dispatch 執行緒使用）。
        """
        self.ensure_active()
        with self._state_lock:
            activation = self._active
            assert activation is not None
            self._leases[activation.activation_id] = self._leases.get(activation.activation_id, 0) + 1
        return activation

    def release(self, activation: UserPackageActivation) -> None:
        """歸還 `acquire()` 取得的 lease。"""
        with self._state_lock:
            remaining = self._leases.get(activation.activation_id, 0) - 1
            if remaining > 0:
                self._leases[activation.activation_id] = remaining
            else:
                self._leases.pop(activation.activation_id, None)
                self._retired.pop(activation.activation_id, None)

    @contextmanager
    def lease(self) -> Iterator[UserPackageActivation]:
        """在整個 dispatch 期間固定同一份 activation。"""
        activation = self.acquire()
        try:
            yield activation
        finally:
            self.release(activation)

    def _referenced_generations(self) -> tuple[set[Path], set[Path]]:
        with self._state_lock:
            active = self._active
            retired = list(self._retired.values())
        current = {package.package_dir.resolve() for package in (active.packages if active else ())}
        in_use = {package.package_dir.resolve() for activation in retired for package in activation.packages}
        return current, in_use - current

    def collect_garbage(self) -> GarbageReport:
        """刪除不再被引用的 generation 與 staging 殘留；拿不到鎖時略過。"""
        if user_packages_disabled():
            return GarbageReport(skipped="USER_PACKAGES_DISABLED")
        try:
            root = self._root_resolver()
        except UserPackageError as exc:
            return GarbageReport(skipped=exc.code)
        current, in_use = self._referenced_generations()
        removed: list[str] = []
        with _root_lock(root, blocking=False) as locked:
            if not locked:
                return GarbageReport(skipped=ROOT_BUSY, in_use=tuple(sorted(str(p) for p in in_use)))
            # 持有鎖時沒有安裝在進行，staging 內的東西都是中斷安裝的殘留。
            staging = root / ".staging"
            if staging.is_dir() and not staging.is_symlink():
                for leftover in staging.iterdir():
                    _remove_tree(leftover)
            for entry in sorted(root.iterdir()):
                if entry.name.startswith(".") or not PACKAGE_ID_RE.fullmatch(entry.name):
                    continue
                try:
                    _check_private_path(entry, expect_dir=True)
                    pointer = read_pointer(entry)
                except (UserPackageError, FileNotFoundError):
                    continue  # 狀態不明時不刪任何東西
                generations = entry / GENERATIONS_DIR_NAME
                if generations.is_dir() and not generations.is_symlink():
                    for generation in sorted(generations.iterdir()):
                        if not GENERATION_RE.fullmatch(generation.name):
                            continue
                        if pointer is not None and generation.name == pointer.generation:
                            continue
                        resolved = generation.resolve()
                        if resolved in current or resolved in in_use:
                            continue
                        _remove_tree(generation)
                        _forget_generation_modules(generation.name)
                        removed.append(f"{entry.name}/{generation.name}")
                if pointer is None:
                    try:
                        if generations.is_dir() and not any(generations.iterdir()):
                            generations.rmdir()
                        if not any(entry.iterdir()):
                            entry.rmdir()
                    except OSError:
                        pass
        return GarbageReport(removed=tuple(removed), in_use=tuple(sorted(str(p) for p in in_use)))


# ── 本機狀態（CLI 用，不需要 daemon） ──────────────────────────────────


@dataclass(frozen=True)
class LocalPackageStatus:
    """安裝目錄內單一 package 的本機狀態。"""

    package_id: str
    installed: bool
    valid: bool
    version: str | None = None
    generation: str | None = None
    digest: str | None = None
    reason: str | None = None
    message: str | None = None
    details: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "installed": self.installed,
            "valid": self.valid,
            "tools": list(self.tools),
            "version": self.version,
            "generation": self.generation,
            "digest": self.digest,
            "reason": self.reason,
            "message": self.message,
            "details": list(self.details),
        }


def inspect_installed(root: Path, *, builtin_ids: frozenset[str] | None = None) -> list[LocalPackageStatus]:
    """列出已安裝的 user package，並以 daemon 相同的規則檢查是否有效。"""
    builtin = builtin_ids if builtin_ids is not None else builtin_package_ids()
    statuses: list[LocalPackageStatus] = []
    with os.scandir(root) as iterator:
        names = sorted(entry.name for entry in iterator)
    for name in names:
        if name.startswith("."):
            continue
        pointer: InstalledPointer | None = None
        try:
            check_package_id(name)
            _check_private_path(root / name, expect_dir=True)
            pointer = read_pointer(root / name)
            if pointer is None:
                continue
            package = _load_installed(root, name, builtin)
        except UserPackageError as exc:
            statuses.append(LocalPackageStatus(
                package_id=name, installed=pointer is not None, valid=False,
                version=pointer.version if pointer else None,
                generation=pointer.generation if pointer else None,
                digest=pointer.digest if pointer else None,
                reason=exc.code, message=exc.message, details=exc.details))
            continue
        except FileNotFoundError:
            continue
        statuses.append(LocalPackageStatus(
            package_id=name, installed=True, valid=True, version=pointer.version,
            generation=pointer.generation, digest=pointer.digest,
            tools=tuple(package.manifest.when_tools) if package is not None else ()))
    return statuses
