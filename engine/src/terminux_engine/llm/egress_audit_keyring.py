# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""外送 audit 的持久 HMAC keyring：管理端提供、daemon 只讀，供 `keyed_persisted` 模式使用。

keyring 是管理者放在 daemon HOME 之外的 JSON 檔，路徑由 `TERMINUX_EGRESS_AUDIT_KEYRING_FILE`
明示給出（與外送 policy 檔、憑證檔相同的設定方式）。daemon 從不產生、改寫、輪替或刪除
金鑰；任何形狀或存取條件不成立都以 `KeyringError` 回報，由呼叫端拒絕外送。

存取條件（全部在同一個描述符上以 `fstat` 檢查，不以路徑重新查詢）：絕對路徑、沿途每個
目錄以 `O_NOFOLLOW` 開啟、父目錄為本人擁有且 group／other 無任何位元、檔案為本人擁有的
0600 或 0400 普通檔、硬連結數 1、大小不超過 `MAX_KEYRING_BYTES`。

檔案形狀::

    {"v": 1, "keys": [
        {"key_id": "k-2026-11-01-a", "algorithm": "hmac-sha256", "status": "active",
         "secret_hex": "<64..256 個 hex 字元>"},
        {"key_id": "k-2026-10-01-a", "algorithm": "hmac-sha256", "status": "verify_only",
         "secret_hex": "..."}]}

恰好一把 `active` 用於新紀錄；`verify_only` 只供驗證舊紀錄；`revoked` 仍可辨識舊紀錄
但驗證工具必須標示。`key_id` 由管理端指定、不可重複，不是由金鑰導出。本模組不記錄、
不回傳、不比較任何金鑰值的前綴或長度以外的資訊；`AuditKey` 的 repr 不含金鑰。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any

KEYRING_ENV = "TERMINUX_EGRESS_AUDIT_KEYRING_FILE"
KEYRING_VERSION = 1
ALGORITHM_HMAC_SHA256 = "hmac-sha256"
STATUS_ACTIVE = "active"
STATUS_VERIFY_ONLY = "verify_only"
STATUS_REVOKED = "revoked"
KEY_STATUSES = frozenset({STATUS_ACTIVE, STATUS_VERIFY_ONLY, STATUS_REVOKED})
MIN_SECRET_BYTES = 32
MAX_SECRET_BYTES = 128
MAX_KEYS = 64
MAX_KEYRING_BYTES = 65536
_ALLOWED_FILE_MODES = frozenset({0o600, 0o400})
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_COMMITMENT_DOMAIN = b"terminux.egress_audit.key_commitment.v1"
_COMMITMENT_HEX = 16

REASON_NOT_CONFIGURED = "KEYRING_NOT_CONFIGURED"
REASON_PATH_INVALID = "KEYRING_PATH_INVALID"
REASON_UNAVAILABLE = "KEYRING_UNAVAILABLE"
REASON_DIRECTORY_INSECURE = "KEYRING_DIRECTORY_INSECURE"
REASON_FILE_INSECURE = "KEYRING_FILE_INSECURE"
REASON_TOO_LARGE = "KEYRING_TOO_LARGE"
REASON_INVALID = "KEYRING_INVALID"
REASON_NO_ACTIVE_KEY = "KEYRING_NO_ACTIVE_KEY"
REASON_WEAK_KEY = "KEYRING_WEAK_KEY"


class KeyringError(ValueError):
    """keyring 無法讀取或形狀／存取條件不成立；`reason` 是穩定的對外字串。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, repr=False)
class AuditKey:
    key_id: str
    algorithm: str
    status: str
    secret: bytes

    def __repr__(self) -> str:
        return f"AuditKey(key_id={self.key_id!r}, algorithm={self.algorithm!r}, status={self.status!r})"

    @property
    def commitment(self) -> str:
        """公開的金鑰承諾：驗證端可確認手上的金鑰就是當時用的那把，旁觀者無法由此反推金鑰。"""
        return hmac.new(self.secret, _COMMITMENT_DOMAIN, hashlib.sha256).hexdigest()[:_COMMITMENT_HEX]


@dataclass(frozen=True, repr=False)
class Keyring:
    """一份已驗證形狀的 keyring；`sha256` 是檔案 bytes 的雜湊，用於偵測程序期間的替換。"""

    sha256: str
    keys: tuple[AuditKey, ...]

    def __repr__(self) -> str:
        return f"Keyring(sha256={self.sha256[:16]!r}, keys={[key.key_id for key in self.keys]!r})"

    @property
    def active(self) -> AuditKey:
        return next(key for key in self.keys if key.status == STATUS_ACTIVE)

    def by_id(self, key_id: str) -> AuditKey | None:
        return next((key for key in self.keys if key.key_id == key_id), None)


def configured_path() -> str | None:
    """keyring 路徑；未設定、空字串或純空白一律視為未設定。"""
    raw = os.environ.get(KEYRING_ENV)
    if raw is None:
        return None
    path = raw.strip()
    return path or None


def _secure_directory(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise KeyringError(REASON_DIRECTORY_INSECURE)


def read_keyring_bytes(path: str) -> bytes:
    """以逐段 `O_NOFOLLOW` 開啟並在描述符上檢查父目錄與檔案的擁有者、模式與大小。"""
    if not isinstance(path, str) or "\x00" in path or not path.startswith("/") or path.startswith("//"):
        raise KeyringError(REASON_PATH_INVALID)
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise KeyringError(REASON_PATH_INVALID)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _secure_directory(fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) not in _ALLOWED_FILE_MODES or info.st_nlink != 1):
                raise KeyringError(REASON_FILE_INSECURE)
            if info.st_size > MAX_KEYRING_BYTES:
                raise KeyringError(REASON_TOO_LARGE)
            chunks: list[bytes] = []
            remaining = MAX_KEYRING_BYTES + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining <= 0:
                raise KeyringError(REASON_TOO_LARGE)
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    except OSError as error:
        raise KeyringError(REASON_UNAVAILABLE) from error
    finally:
        os.close(fd)


def _parse_secret(value: Any) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]+", value) or len(value) % 2:
        raise KeyringError(REASON_INVALID)
    secret = bytes.fromhex(value)
    if not MIN_SECRET_BYTES <= len(secret) <= MAX_SECRET_BYTES:
        raise KeyringError(REASON_WEAK_KEY)
    # 全部相同位元組（含全零）是常見的佔位值，不當作金鑰。
    if len(set(secret)) < 2:
        raise KeyringError(REASON_WEAK_KEY)
    return secret


def _parse_key(entry: Any) -> AuditKey:
    if not isinstance(entry, dict) or set(entry) != {"key_id", "algorithm", "status", "secret_hex"}:
        raise KeyringError(REASON_INVALID)
    key_id = entry["key_id"]
    if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
        raise KeyringError(REASON_INVALID)
    if entry["algorithm"] != ALGORITHM_HMAC_SHA256:
        raise KeyringError(REASON_INVALID)
    status = entry["status"]
    if not isinstance(status, str) or status not in KEY_STATUSES:
        raise KeyringError(REASON_INVALID)
    return AuditKey(key_id, ALGORITHM_HMAC_SHA256, status, _parse_secret(entry["secret_hex"]))


def parse_keyring(raw: bytes) -> Keyring:
    """驗證 keyring bytes 的形狀：版本、鍵集合、金鑰條目、唯一 `key_id`、恰好一把 `active`。"""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise KeyringError(REASON_INVALID) from error
    if not isinstance(document, dict) or set(document) != {"v", "keys"} or document["v"] != KEYRING_VERSION:
        raise KeyringError(REASON_INVALID)
    entries = document["keys"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_KEYS:
        raise KeyringError(REASON_INVALID)
    keys = tuple(_parse_key(entry) for entry in entries)
    if len({key.key_id for key in keys}) != len(keys):
        raise KeyringError(REASON_INVALID)
    if sum(1 for key in keys if key.status == STATUS_ACTIVE) != 1:
        raise KeyringError(REASON_NO_ACTIVE_KEY)
    return Keyring(hashlib.sha256(raw).hexdigest(), keys)


def load_keyring(path: str | None = None) -> Keyring:
    """讀檔並驗證；`path` 省略時取 `TERMINUX_EGRESS_AUDIT_KEYRING_FILE`，未設定即 `KEYRING_NOT_CONFIGURED`。"""
    if path is None:
        path = configured_path()
    if path is None:
        raise KeyringError(REASON_NOT_CONFIGURED)
    return parse_keyring(read_keyring_bytes(path))


__all__ = [
    "ALGORITHM_HMAC_SHA256", "AuditKey", "KEYRING_ENV", "KEYRING_VERSION", "KEY_STATUSES", "Keyring",
    "KeyringError", "MAX_KEYRING_BYTES", "MAX_KEYS", "MAX_SECRET_BYTES", "MIN_SECRET_BYTES",
    "REASON_DIRECTORY_INSECURE", "REASON_FILE_INSECURE", "REASON_INVALID", "REASON_NO_ACTIVE_KEY",
    "REASON_NOT_CONFIGURED", "REASON_PATH_INVALID", "REASON_TOO_LARGE", "REASON_UNAVAILABLE",
    "REASON_WEAK_KEY", "STATUS_ACTIVE", "STATUS_REVOKED", "STATUS_VERIFY_ONLY", "configured_path",
    "load_keyring", "parse_keyring", "read_keyring_bytes",
]
