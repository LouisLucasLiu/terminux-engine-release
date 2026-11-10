# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""逐段 `O_NOFOLLOW` 開啟的安全讀檔，供報告與 receipt 讀取共用。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

#: 呼叫端以 reason code 建立自己的例外型別；本模組不決定錯誤類別。
ErrorFactory = Callable[[str], BaseException]

REASON_PATH_INVALID = "PATH_INVALID"
REASON_FILE_INVALID = "FILE_INVALID"
REASON_TOO_LARGE = "TOO_LARGE"
REASON_UNAVAILABLE = "UNAVAILABLE"
REASON_NOT_OWNED = "NOT_OWNED"


def read_regular_file(path: Path, limit: int, error: ErrorFactory, *,
                      required_owner_uid: int | None = None) -> bytes:
    """只讀取絕對路徑下、沿途不經 symlink 的普通檔，且大小不得超過 `limit`。

    路徑含 NUL、相對路徑或 `..` → `PATH_INVALID`；非普通檔或宣告大小超限 →
    `FILE_INVALID`；實際讀到超過 `limit` → `TOO_LARGE`；任何 OS 錯誤（含 symlink）
    → `UNAVAILABLE`。錯誤以 `error(reason)` 建立後拋出。

    `required_owner_uid` 不為 None 時，以已開啟檔案的 `fstat` 比對擁有者，不符 →
    `NOT_OWNED`。這只防止誤用他人的檔案，存取權仍由 OS 權限決定。
    """
    if "\x00" in os.fspath(path) or not path.is_absolute() or ".." in path.parts:
        raise error(REASON_PATH_INVALID)
    parts = path.parts[1:]
    if not parts:
        raise error(REASON_PATH_INVALID)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise error(REASON_FILE_INVALID)
            if required_owner_uid is not None and info.st_uid != required_owner_uid:
                raise error(REASON_NOT_OWNED)
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(file_fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if not remaining:
                raise error(REASON_TOO_LARGE)
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise error(REASON_UNAVAILABLE) from exc
    finally:
        os.close(fd)


__all__ = ["read_regular_file", "REASON_FILE_INVALID", "REASON_NOT_OWNED", "REASON_PATH_INVALID",
           "REASON_TOO_LARGE", "REASON_UNAVAILABLE"]
