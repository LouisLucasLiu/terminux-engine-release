# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Unix domain socket 的 fail-closed 綁定、路徑長度守衛與安全清理。

AF_UNIX 的 `sun_path` 有硬性長度上限（Linux 為 108 bytes，含結尾
NUL）。之前的失敗模式是「路徑過長」只在 `bind()`／`connect()` 當下變成一個
不可診斷的 `OSError`，或更糟——被上層誤讀成 daemon 不存在。本模組因此提供：

1. **`validate_unix_socket_path()`**：在 bind／connect **之前**檢查長度，
   超限就丟明確、可診斷的 `SocketPathTooLongError`（含實際長度與上限）。
   明確指定的 path **絕不**被暗中改寫或截短。
2. **`runtime_socket_dir()`**：自動產生的 socket 一律放在與一般工作暫存目錄
   分離的 runtime directory（優先 `XDG_RUNTIME_DIR`／`/run/user/<uid>`），
   並檢查 owner、mode `0700`、非 symlink、確實是目錄——不安全即 fail closed。
   這樣 socket 路徑長度不再受 `TMPDIR`／`HOME`／cwd 影響。
3. **`allocate_runtime_socket_path()`**：回傳**唯一一份**權威 path，
   server 與 client 共用同一個值，不各自重新推導。
4. 清理只移除本次建立且 identity 可核對的 socket 與 instance 目錄；
   不遞迴清理 `/tmp`、`HOME` 或使用者目錄。
"""
from __future__ import annotations

import errno
import os
import secrets
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

#: Linux `sockaddr_un.sun_path` 為 108 bytes，最後一個 byte 必須是 NUL，
#: 因此可用的路徑 bytes 為 107。其他平台較小者以此為保守上限。
AF_UNIX_PATH_MAX_BYTES = 107

#: 自動產生 socket 時，instance 目錄名的固定前綴。
_RUNTIME_DIR_NAME = "terminux"


class SocketPathTooLongError(ValueError):
    """socket path 超過平台 `sun_path` 上限。

    刻意**不是** `OSError`：它在 bind／connect 之前就能判定，屬於呼叫端傳入
    的設定錯誤，不是 kernel 回報的 I/O 失敗。訊息帶實際長度與上限，讓
    operator 可以直接診斷，不必反推 errno。
    """

    def __init__(self, path: str | Path, *, limit: int = AF_UNIX_PATH_MAX_BYTES) -> None:
        encoded = os.fspath(path).encode("utf-8", "surrogateescape")
        self.path = os.fspath(path)
        self.length = len(encoded)
        self.limit = limit
        super().__init__(
            f"TERM_SOCKET_PATH_TOO_LONG: unix socket path is {self.length} bytes "
            f"but the platform limit is {limit} bytes: {self.path}")


class RuntimeSocketDirectoryError(RuntimeError):
    """runtime directory 不存在、不是目錄、不屬於本使用者或權限不是 0700。"""


def validate_unix_socket_path(path: str | Path) -> Path:
    """在 bind／connect **之前**驗證長度；回傳原 path，不改寫。

    明確由使用者或管理者指定的 path 一律原樣使用——超限時報錯，
    **不**自動搬到別處，也不截短。
    """
    if len(os.fspath(path).encode("utf-8", "surrogateescape")) > AF_UNIX_PATH_MAX_BYTES:
        raise SocketPathTooLongError(path)
    return Path(path)


def _assert_safe_directory(directory: Path) -> None:
    """owner／mode／symlink／非目錄一律 fail closed。"""
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_UNAVAILABLE: {directory}: {exc}") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_IS_SYMLINK: {directory}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_NOT_A_DIRECTORY: {directory}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_WRONG_OWNER: {directory} is owned by uid "
            f"{metadata.st_uid}, expected {os.geteuid()}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_TOO_PERMISSIVE: {directory} has mode "
            f"{stat.S_IMODE(metadata.st_mode):04o}, expected 0700")


def _runtime_base() -> Path:
    """挑一個**短且安全**的 runtime base，與 TMPDIR／HOME／cwd 無關。

    順序：`XDG_RUNTIME_DIR` → `/run/user/<uid>` → `/tmp/terminux-<uid>`。
    前兩者由 OS 以 0700 建立；最後的 fallback 由本函式自己建立成 0700，
    並同樣做 owner／mode 檢查（不是「找不到就放寬」）。
    """
    for candidate in (os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.geteuid()}"):
        if not candidate:
            continue
        directory = Path(candidate)
        try:
            _assert_safe_directory(directory)
        except RuntimeSocketDirectoryError:
            continue
        return directory
    fallback = Path(f"/tmp/{_RUNTIME_DIR_NAME}-{os.geteuid()}")
    try:
        fallback.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_UNAVAILABLE: {fallback}: {exc}") from None
    _assert_safe_directory(fallback)
    return fallback


def runtime_socket_dir(*, base: str | Path | None = None) -> Path:
    """回傳可放自動產生 socket 的 runtime directory（已通過安全檢查）。

    `base` 只給測試與呼叫端明確覆寫用；一樣要通過同一組 owner／mode 檢查。
    """
    directory = Path(base) if base is not None else _runtime_base()
    if base is not None:
        _assert_safe_directory(directory)
    sockets = directory / _RUNTIME_DIR_NAME if base is None else directory
    if sockets != directory:
        try:
            sockets.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise RuntimeSocketDirectoryError(
                f"TERM_RUNTIME_DIR_UNAVAILABLE: {sockets}: {exc}") from None
        _assert_safe_directory(sockets)
    return sockets


@dataclass(frozen=True)
class RuntimeSocketPath:
    """自動配置出來的 socket path 與其 instance 目錄。

    `path` 是**唯一**權威值：server bind 它、client 連它，兩邊不各自推導。
    """

    path: Path
    instance_dir: Path

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return os.fspath(self.path)


def allocate_runtime_socket_path(
    name: str = "sock",
    *,
    base: str | Path | None = None,
) -> RuntimeSocketPath:
    """在安全 runtime directory 內配置一個**保證長度合法**的 socket path。

    路徑長度只取決於 runtime base 與固定短名，因此與 `TMPDIR`／`HOME`／
    cwd 的長度完全無關。仍然在回傳前做一次長度驗證：若連 runtime base 自己
    就過長（例如 operator 指定了很深的 `XDG_RUNTIME_DIR`），也要明確報錯，
    而不是留到 bind 才失敗。
    """
    sockets = runtime_socket_dir(base=base)
    instance = sockets / secrets.token_hex(4)
    try:
        instance.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise RuntimeSocketDirectoryError(
            f"TERM_RUNTIME_DIR_UNAVAILABLE: {instance}: {exc}") from None
    _assert_safe_directory(instance)
    # socket 檔名保持短（呼叫端給的 name 只取前 16 字元，避免把長度問題
    # 從目錄搬到檔名）。
    path = instance / f"{name[:16]}.sock"
    validate_unix_socket_path(path)
    return RuntimeSocketPath(path=path, instance_dir=instance)


def release_runtime_socket_path(allocated: RuntimeSocketPath) -> bool:
    """只移除本次配置的 socket 與其 instance 目錄。

    不遞迴刪除 runtime base，也不碰 instance 目錄裡不是自己建立的東西：
    只有目錄變空之後才 `rmdir`。
    """
    removed = False
    try:
        metadata = allocated.path.lstat()
    except OSError:
        metadata = None
    if metadata is not None and stat.S_ISSOCK(metadata.st_mode) \
            and metadata.st_uid == os.geteuid():
        try:
            allocated.path.unlink()
            removed = True
        except OSError:
            pass
    try:
        allocated.instance_dir.rmdir()
    except OSError:
        pass
    return removed


@dataclass(frozen=True)
class BoundUnixSocket:
    """本 process 成功 bind 後取得的 socket 身分。"""

    path: Path
    device: int
    inode: int
    owner_uid: int


def bind_owned_unix_socket(
    server_socket: socket.socket,
    socket_path: str | Path,
) -> BoundUnixSocket:
    """Fail-closed bind，並記錄本 process 建立的 socket 身分。

    既存 endpoint 一律保留，讓 operator 先驗證並人工隔離；不可在 startup
    階段猜測 stale socket 或直接 unlink。
    """
    # 長度在 bind **之前**就判定，錯誤明確可診斷，不會變成
    # 一個看不出原因的 `TERM_SOCKET_BIND_FAILED`。
    path = validate_unix_socket_path(socket_path)
    if os.path.lexists(path):
        raise OSError(errno.EADDRINUSE, "TERM_SOCKET_CONFLICT")

    try:
        server_socket.bind(os.fspath(path))
    except OSError as exc:
        raise OSError(exc.errno or errno.EIO, "TERM_SOCKET_BIND_FAILED") from None

    try:
        metadata = path.lstat()
    except OSError:
        raise OSError(errno.EIO, "TERM_SOCKET_IDENTITY_UNAVAILABLE") from None

    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise OSError(errno.EIO, "TERM_SOCKET_IDENTITY_MISMATCH")

    return BoundUnixSocket(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
    )


def unlink_owned_unix_socket(identity: BoundUnixSocket) -> bool:
    """只移除仍是本 process 建立之 socket；任何替換皆保留。"""
    try:
        metadata = identity.path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False

    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_dev != identity.device
        or metadata.st_ino != identity.inode
        or metadata.st_uid != identity.owner_uid
    ):
        return False

    try:
        identity.path.unlink()
    except OSError:
        return False
    return True
