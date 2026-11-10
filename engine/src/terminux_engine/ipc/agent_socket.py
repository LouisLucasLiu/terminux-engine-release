# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""per-user agent socket 的**唯一**路徑規則。

Terminux agent socket（IPCServer；session.init／chat／task control）固定為：

    <目前執行帳號的 home>/.terminux/run/<nodename>/terminux.sock

規則（每一條都是 fail-closed）：

1. home 由 process 自己的 effective UID 查系統帳號資料（`pwd.getpwuid`）
   取得——**不讀 `$HOME`**，避免 `sudo -u` 之類切換帳號後繼承別人的 HOME
   而連錯 socket。TERMINUX_HOME_DIR 可明確指定本 UID 的私有基底目錄。
   uid 不由 request、member_id、project 或任何 caller 提供。
2. home 必須存在、是目錄、owner 為目前 UID。不要求 home 本身是 0700。
3. `<home>/.terminux/` 由 daemon 建立（0700）或重用：必須是目錄、不是
   symlink、owner 為目前 UID、group／other 不可寫。既有目錄不遞迴 chmod／
   chown。client 端只檢查、不建立。
4. socket 路徑上若已有**非 socket** 的東西（普通檔、symlink、目錄…）一律
   報錯，不刪除、不覆寫、不跟隨 symlink。已是 socket 時交給 bind 判定
   （`TERM_SOCKET_CONFLICT`），並提供 `describe_socket_path()` 的診斷
   （live／unresponsive／owner），**不自動刪除**。
5. 長度在 bind／connect **之前**檢查（沿用 `validate_unix_socket_path`）。
6. 不依賴 `/run/user/<uid>`、logind login session 或 `XDG_RUNTIME_DIR`。
   沒有 `/tmp/terminux.sock` 或舊 `/run/user/<uid>/terminux/terminux.sock`
   fallback；偵測到 legacy socket 只給 migration warning，**不刪除、
   不接管、不阻擋**。

`TERMINUX_CORE_SOCK` 仍然存在，語義是**明確的隔離部署覆寫**（hermetic
測試、isolated daemon harness、C++ 整合測試）：它不是 fallback——未設定時
一律走 per-user 規則；設定時必須是絕對路徑、不得是任一 legacy 路徑、且其父
目錄必須是本 UID 擁有的 0700 目錄。

錯誤碼沿用既有的 `TERM_RUNTIME_DIR_*`（此處「runtime dir」指 socket 所在的
`~/.terminux` 目錄），只新增 home 解析與 socket 路徑被占用兩類。
"""

from __future__ import annotations

import logging
import os
import pwd
import socket
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from terminux_engine.ipc.socket_lifecycle import (
    SocketPathTooLongError,
    validate_unix_socket_path,
)

#: home 下的 socket 目錄與檔名。
AGENT_SOCKET_DIR_NAME = ".terminux"
AGENT_SOCKET_FILE_NAME = "terminux.sock"
#: 舊的機器全域路徑。只用來偵測與警告，永遠不會被解析成可用路徑。
LEGACY_AGENT_SOCKET_PATH = "/tmp/terminux.sock"
#: 舊版的 per-user runtime dir 路徑樣板（已退役）。同樣只偵測與警告。
LEGACY_RUNTIME_DIR_SOCKET_TEMPLATE = "/run/user/{uid}/terminux/terminux.sock"
#: 明確的隔離部署覆寫。不是 fallback（見模組 docstring）。
OVERRIDE_ENV = "TERMINUX_CORE_SOCK"
HOME_OVERRIDE_ENV = "TERMINUX_HOME_DIR"
LEGACY_OVERRIDE_IGNORED = "TERM_LEGACY_SOCKET_OVERRIDE_IGNORED"
_legacy_override_warned: set[str] = set()
_legacy_override_lock = threading.Lock()

# ── 錯誤碼 ──────────────────────────────────────────────────────────
# `TERM_RUNTIME_DIR_*` 沿用既有定義：指 `<home>/.terminux`（socket 目錄）。
RUNTIME_DIR_MISSING = "TERM_RUNTIME_DIR_MISSING"
RUNTIME_DIR_NOT_A_DIRECTORY = "TERM_RUNTIME_DIR_NOT_A_DIRECTORY"
RUNTIME_DIR_IS_SYMLINK = "TERM_RUNTIME_DIR_IS_SYMLINK"
RUNTIME_DIR_WRONG_OWNER = "TERM_RUNTIME_DIR_WRONG_OWNER"
RUNTIME_DIR_TOO_PERMISSIVE = "TERM_RUNTIME_DIR_TOO_PERMISSIVE"
RUNTIME_DIR_UNAVAILABLE = "TERM_RUNTIME_DIR_UNAVAILABLE"
# home 解析／檢查，與 socket 路徑被非 socket 占用。
HOME_DIR_UNRESOLVED = "TERM_HOME_DIR_UNRESOLVED"
HOME_DIR_UNAVAILABLE = "TERM_HOME_DIR_UNAVAILABLE"
SOCKET_PATH_OCCUPIED = "TERM_AGENT_SOCKET_PATH_OCCUPIED"
SOCKET_PATH_TOO_LONG = "TERM_SOCKET_PATH_TOO_LONG"
OVERRIDE_INVALID = "TERM_AGENT_SOCKET_OVERRIDE_INVALID"
LEGACY_PATH_REJECTED = "TERM_AGENT_SOCKET_LEGACY_PATH_REJECTED"

SOURCE_PER_USER = "per_user"
SOURCE_OVERRIDE = "override"

#: 既有 `.terminux` 目錄不允許的 mode 位元：group／other 可寫。
_SOCKET_DIR_FORBIDDEN_MODE = 0o022
#: 覆寫路徑的父目錄沿用既有的嚴格規則：group／other 任何位元都不行。
_OVERRIDE_PARENT_FORBIDDEN_MODE = 0o077


class AgentSocketError(RuntimeError):
    """socket 路徑無法解析。訊息一律含目前 UID 與預期路徑，不含其他帳號的資訊。"""

    def __init__(self, code: str, message: str, *, uid: int, expected_path: str) -> None:
        super().__init__(f"{code}: {message} (uid={uid}, expected={expected_path})")
        self.code = code
        self.message = message
        self.uid = uid
        self.expected_path = expected_path

    def to_json_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "uid": self.uid,
                "expected_path": self.expected_path}


@dataclass(frozen=True)
class AgentSocketResolution:
    """解析結果。`source` 說明路徑是 per-user 規則還是明確覆寫。"""

    path: Path
    uid: int
    source: str

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return os.fspath(self.path)


# ── 純函式：uid → home → 路徑 ───────────────────────────────────────


def current_uid() -> int:
    """唯一的 UID 來源：process effective UID。"""
    return os.geteuid()


def local_nodename() -> str:
    """取得一致的主機身分；覆寫值也必須是單一安全的目錄名稱。"""
    value = os.environ.get("TERMINUX_NODENAME") or socket.gethostname()
    if (value in {".", ".."} or not value
            or any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in value)):
        raise ValueError("TERM_NODENAME_INVALID")
    return value


def _lookup_home(uid: int) -> Path:
    """以 uid 查 passwd 取 home。模組層級 seam，只給**測試**改道用。

    找不到帳號或 `pw_dir` 為空 ⇒ `KeyError`（由呼叫端轉成
    `TERM_HOME_DIR_UNRESOLVED`）。
    """
    entry = pwd.getpwuid(uid)  # KeyError：沒有這個 uid
    if not entry.pw_dir:
        raise KeyError(f"uid {uid} has no home directory in the account database")
    return Path(entry.pw_dir)


def home_for_uid(uid: int) -> Path:
    """解析私有基底覆寫，否則查系統帳號資料；不讀 `$HOME`。"""
    if HOME_OVERRIDE_ENV in os.environ:
        raw = os.environ[HOME_OVERRIDE_ENV]
        home = Path(raw)
        if not home.is_absolute():
            raise AgentSocketError(OVERRIDE_INVALID,
                                   f"{HOME_OVERRIDE_ENV} must be an absolute path",
                                   uid=uid, expected_path=raw)
        _check_directory(home, uid=uid, expected=raw,
                         missing_hint=f"{HOME_OVERRIDE_ENV} must already exist",
                         forbidden_mode=_OVERRIDE_PARENT_FORBIDDEN_MODE)
        return home
    return _lookup_home(uid)


def format_agent_socket_path(uid: int) -> Path:
    """純字串規則（含一次 passwd 查詢），不碰檔案系統。

    給文件、錯誤訊息與 C++ 端對照用。home 解析不到時回傳一個可辨識的
    佔位路徑，讓錯誤訊息仍能說明「預期形狀」。
    """
    try:
        home = home_for_uid(uid)
    except KeyError:
        home = Path(f"<home of uid {uid}>")
    return home / AGENT_SOCKET_DIR_NAME / "run" / local_nodename() / AGENT_SOCKET_FILE_NAME


def format_lineage_socket_path(uid: int) -> Path:
    """與 Core 使用同一主機目錄的 lineage 路徑。"""
    return format_agent_socket_path(uid).with_name("lineage.sock")


def legacy_runtime_dir_socket_path(uid: int) -> str:
    """舊版的 `/run/user/<uid>/terminux/terminux.sock`（只用來偵測與拒絕覆寫）。"""
    return LEGACY_RUNTIME_DIR_SOCKET_TEMPLATE.format(uid=uid)


# ── 檢查 ───────────────────────────────────────────────────────────


def _check_home(home: Path, *, uid: int, expected: str) -> None:
    """home：存在、是目錄（允許 symlink 指向目錄）、owner 為本 uid。"""
    try:
        metadata = home.stat()
    except FileNotFoundError:
        raise AgentSocketError(
            HOME_DIR_UNAVAILABLE, f"home directory {home} does not exist",
            uid=uid, expected_path=expected) from None
    except OSError as exc:
        raise AgentSocketError(
            HOME_DIR_UNAVAILABLE, f"home directory {home} could not be inspected: {exc}",
            uid=uid, expected_path=expected) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise AgentSocketError(
            HOME_DIR_UNAVAILABLE, f"home directory {home} is not a directory",
            uid=uid, expected_path=expected)
    if metadata.st_uid != uid:
        raise AgentSocketError(
            HOME_DIR_UNAVAILABLE,
            f"home directory {home} is owned by uid {metadata.st_uid}, expected {uid}",
            uid=uid, expected_path=expected)


def _check_directory(directory: Path, *, uid: int, expected: str,
                     missing_hint: str, forbidden_mode: int) -> None:
    """socket 目錄：owner／mode／symlink／非目錄一律 fail closed，並給結構化錯誤碼。"""
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        raise AgentSocketError(
            RUNTIME_DIR_MISSING, f"{directory} does not exist; {missing_hint}",
            uid=uid, expected_path=expected) from None
    except OSError as exc:
        raise AgentSocketError(
            RUNTIME_DIR_UNAVAILABLE, f"{directory} could not be inspected: {exc}",
            uid=uid, expected_path=expected) from None
    if stat.S_ISLNK(metadata.st_mode):
        raise AgentSocketError(
            RUNTIME_DIR_IS_SYMLINK, f"{directory} is a symlink",
            uid=uid, expected_path=expected)
    if not stat.S_ISDIR(metadata.st_mode):
        raise AgentSocketError(
            RUNTIME_DIR_NOT_A_DIRECTORY, f"{directory} is not a directory",
            uid=uid, expected_path=expected)
    if metadata.st_uid != uid:
        raise AgentSocketError(
            RUNTIME_DIR_WRONG_OWNER,
            f"{directory} is owned by uid {metadata.st_uid}, expected {uid}",
            uid=uid, expected_path=expected)
    if stat.S_IMODE(metadata.st_mode) & forbidden_mode:
        raise AgentSocketError(
            RUNTIME_DIR_TOO_PERMISSIVE,
            f"{directory} has mode {stat.S_IMODE(metadata.st_mode):04o}; "
            f"it must not be writable by group or other"
            + (" (expected 0700)" if forbidden_mode == _OVERRIDE_PARENT_FORBIDDEN_MODE else ""),
            uid=uid, expected_path=expected)


def _validate_length(path: Path, *, uid: int, expected: str) -> Path:
    try:
        return validate_unix_socket_path(path)
    except SocketPathTooLongError as exc:
        raise AgentSocketError(
            SOCKET_PATH_TOO_LONG,
            f"{exc.path} is {exc.length} bytes; the platform limit is {exc.limit}",
            uid=uid, expected_path=expected) from None


# ── socket 路徑本身的診斷（不刪、不改、不跟隨 symlink） ───────────────


@dataclass(frozen=True)
class SocketPathDiagnostic:
    """socket 路徑上目前有什麼。`kind` 之一：

    - ``absent``：沒有東西（daemon 可以 bind；client 表示 daemon 未啟動）
    - ``socket_live``：是 socket 且接受連線（另一個 daemon 正在服務）
    - ``socket_unresponsive``：是 socket 但連不上（可能 stale；**不自動刪**）
    - ``symlink``／``regular_file``／``directory``／``other``：被非 socket 占用
    """

    kind: str
    owner_uid: int | None
    detail: str


def describe_socket_path(path: Path, *, probe_timeout: float = 0.5) -> SocketPathDiagnostic:
    """只 lstat ＋（是 socket 時）一次短暫 connect 探測。永不 unlink。"""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return SocketPathDiagnostic("absent", None, f"{path} does not exist")
    except OSError as exc:
        return SocketPathDiagnostic("other", None, f"{path} could not be inspected: {exc}")
    owner = metadata.st_uid
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return SocketPathDiagnostic(
            "symlink", owner, f"{path} is a symlink to {target!s} (owner uid {owner}); "
                              "not followed, not removed")
    if stat.S_ISREG(mode):
        return SocketPathDiagnostic(
            "regular_file", owner, f"{path} is a regular file (owner uid {owner}); not removed")
    if stat.S_ISDIR(mode):
        return SocketPathDiagnostic(
            "directory", owner, f"{path} is a directory (owner uid {owner}); not removed")
    if not stat.S_ISSOCK(mode):
        return SocketPathDiagnostic(
            "other", owner, f"{path} is not a socket (mode {stat.S_IFMT(mode):o}); not removed")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(probe_timeout)
    try:
        probe.connect(os.fspath(path))
    except OSError as exc:
        return SocketPathDiagnostic(
            "socket_unresponsive", owner,
            f"{path} is a socket (owner uid {owner}) that does not accept connections "
            f"({exc.strerror or exc}); it may be stale from a daemon that did not shut "
            "down cleanly. Verify no daemon is using it before removing it yourself")
    finally:
        probe.close()
    return SocketPathDiagnostic(
        "socket_live", owner,
        f"{path} is a socket (owner uid {owner}) that accepts connections; another "
        "daemon for this uid is already running")


def _refuse_non_socket_occupant(path: Path, *, uid: int, expected: str) -> None:
    """路徑上有非 socket 的東西 ⇒ 報錯，不刪、不覆寫、不跟隨。"""
    if _is_socket_or_absent(path):
        return
    diagnostic = describe_socket_path(path, probe_timeout=0.0)
    raise AgentSocketError(
        SOCKET_PATH_OCCUPIED, diagnostic.detail, uid=uid, expected_path=expected)


def _is_socket_or_absent(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.lstat().st_mode)
    except FileNotFoundError:
        return True
    except OSError:
        return False


# ── 解析 ───────────────────────────────────────────────────────────


def _resolve_override(raw: str, *, uid: int, env_name: str = OVERRIDE_ENV,
                      filename: str = AGENT_SOCKET_FILE_NAME) -> AgentSocketResolution:
    expected = os.fspath(format_agent_socket_path(uid).with_name(filename))
    _validate_length(Path(raw), uid=uid, expected=expected)
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise AgentSocketError(
            OVERRIDE_INVALID, f"{env_name} must be an absolute path: {raw!r}",
            uid=uid, expected_path=expected)
    normalized = Path(os.path.normpath(raw))
    if os.fspath(normalized) in (LEGACY_AGENT_SOCKET_PATH,
                                 legacy_runtime_dir_socket_path(uid)):
        raise AgentSocketError(
            LEGACY_PATH_REJECTED,
            f"{env_name} may not point at a retired socket path "
            f"({os.fspath(normalized)}); the agent socket lives under the user's home",
            uid=uid, expected_path=expected)
    # 覆寫的父目錄必須是本 uid 私有的 0700 目錄：擋掉「把 socket 放到
    # 別人也能寫的目錄」這種形狀。
    _check_directory(normalized.parent, uid=uid, expected=expected,
                     missing_hint=f"the parent directory named by {env_name} "
                                  "must already exist",
                     forbidden_mode=_OVERRIDE_PARENT_FORBIDDEN_MODE)
    _refuse_non_socket_occupant(normalized, uid=uid, expected=expected)
    return AgentSocketResolution(
        path=_validate_length(normalized, uid=uid, expected=expected),
        uid=uid, source=SOURCE_OVERRIDE)


def _resolve_socket(filename: str, env_name: str | None, *, create_dir: bool,
                    socket_endpoint: bool = True) -> AgentSocketResolution:
    uid = current_uid()
    if HOME_OVERRIDE_ENV in os.environ:
        home_for_uid(uid)
    override = os.environ.get(env_name, "").strip() if env_name else ""
    if override and env_name in {OVERRIDE_ENV, "TERMINUX_SOCK"}:
        try:
            old_default = home_for_uid(uid) / AGENT_SOCKET_DIR_NAME / filename
        except KeyError:
            old_default = None
        if old_default is not None and Path(os.path.normpath(override)) == old_default:
            with _legacy_override_lock:
                if env_name not in _legacy_override_warned:
                    logging.getLogger(__name__).warning(
                        "%s: ignored %s=%s", LEGACY_OVERRIDE_IGNORED, env_name, override)
                    _legacy_override_warned.add(env_name)
            override = ""
    if override:
        return _resolve_override(override, uid=uid, env_name=env_name, filename=filename)

    expected = os.fspath(format_agent_socket_path(uid).with_name(filename))
    if socket_endpoint:
        _validate_length(Path(expected), uid=uid, expected=expected)
    try:
        home = home_for_uid(uid)
    except KeyError as exc:
        raise AgentSocketError(
            HOME_DIR_UNRESOLVED,
            f"no home directory is recorded for uid {uid} in the account database: {exc}",
            uid=uid, expected_path=expected) from None
    _check_home(home, uid=uid, expected=expected)

    terminux = home / AGENT_SOCKET_DIR_NAME
    sockets = terminux / "run" / local_nodename()
    for directory in (terminux, terminux / "run", sockets):
        if create_dir and not os.path.lexists(directory):
            try:
                directory.mkdir(mode=0o700)
            except OSError as exc:
                raise AgentSocketError(RUNTIME_DIR_UNAVAILABLE, str(exc),
                                       uid=uid, expected_path=expected) from None
        _check_directory(directory, uid=uid, expected=expected,
                         missing_hint="daemon or relay has not created the host directory",
                         forbidden_mode=(_SOCKET_DIR_FORBIDDEN_MODE if directory == terminux
                                         else _OVERRIDE_PARENT_FORBIDDEN_MODE))
    path = sockets / filename
    if socket_endpoint:
        _validate_length(path, uid=uid, expected=expected)
        _refuse_non_socket_occupant(path, uid=uid, expected=expected)
    return AgentSocketResolution(path=path, uid=uid, source=SOURCE_PER_USER)


def resolve_agent_socket_path(*, create_dir: bool = False) -> AgentSocketResolution:
    """解析本機 Core 路徑；只有 daemon／relay 可要求建立私有目錄。"""
    return _resolve_socket(AGENT_SOCKET_FILE_NAME, OVERRIDE_ENV, create_dir=create_dir)


def host_runtime_directory(*, create_dir: bool = False) -> Path:
    """取得不受 socket 覆寫影響的私有主機目錄，供 pid／log 使用。"""
    # 一般檔案不受 AF_UNIX 長度或 socket occupant 限制；仍驗證完整目錄的安全性。
    return _resolve_socket(AGENT_SOCKET_FILE_NAME, None, create_dir=create_dir,
                           socket_endpoint=False).path.parent


def resolve_lineage_socket_path(*, create_dir: bool = False) -> AgentSocketResolution:
    """解析本機 lineage 路徑，TERMINUX_SOCK 的明確覆寫優先。"""
    return _resolve_socket("lineage.sock", "TERMINUX_SOCK", create_dir=create_dir)


def daemon_lineage_socket() -> AgentSocketResolution:
    """daemon／relay 建立並解析本機 lineage endpoint。"""
    return resolve_lineage_socket_path(create_dir=True)


def client_lineage_socket() -> AgentSocketResolution:
    """client 只讀解析本機 lineage endpoint。"""
    return resolve_lineage_socket_path()


def shell_lineage_socket_path() -> str:
    """shell 啟用判斷使用；runtime 尚不存在時回空字串，不輸出 traceback。"""
    try:
        return str(client_lineage_socket().path)
    except (AgentSocketError, ValueError):
        return ""


def daemon_agent_socket() -> AgentSocketResolution:
    """daemon 啟動時呼叫：建立私有主機目錄並回傳要 bind 的路徑。"""
    return resolve_agent_socket_path(create_dir=True)


def client_agent_socket() -> AgentSocketResolution:
    """client（CLI／GUI／bootstrap）呼叫：只解析，不建立任何目錄。"""
    return resolve_agent_socket_path(create_dir=False)


# ── legacy 偵測（只警告） ──────────────────────────────────────────


def legacy_socket_paths(uid: int | None = None) -> tuple[str, ...]:
    """所有退役路徑：機器全域的 `/tmp` 與舊版的 `/run/user/<uid>`。"""
    return (LEGACY_AGENT_SOCKET_PATH,
            legacy_runtime_dir_socket_path(current_uid() if uid is None else uid))


def legacy_socket_present() -> bool:
    """任一 legacy 路徑是否還在。只 `lstat`，不連線、不刪除。"""
    return bool(_present_legacy_paths())


def _present_legacy_paths() -> list[str]:
    present: list[str] = []
    for candidate in legacy_socket_paths():
        try:
            os.lstat(candidate)
        except OSError:
            continue
        present.append(candidate)
    return present


def legacy_migration_warning() -> str | None:
    """有 legacy socket 就回一句 migration 提示；沒有回 `None`。"""
    present = _present_legacy_paths()
    if not present:
        return None
    return ("TERM_LEGACY_AGENT_SOCKET_PRESENT: " + ", ".join(present) + " still "
            "exist(s); those paths are no longer used, not removed and not taken over. "
            "Stop the old daemon that created them and remove the files yourself if "
            "they are stale. New clients connect only to the home socket.")


__all__ = [
    "AGENT_SOCKET_DIR_NAME",
    "AGENT_SOCKET_FILE_NAME",
    "HOME_DIR_UNAVAILABLE",
    "HOME_DIR_UNRESOLVED",
    "LEGACY_AGENT_SOCKET_PATH",
    "LEGACY_PATH_REJECTED",
    "LEGACY_RUNTIME_DIR_SOCKET_TEMPLATE",
    "OVERRIDE_ENV",
    "OVERRIDE_INVALID",
    "RUNTIME_DIR_IS_SYMLINK",
    "RUNTIME_DIR_MISSING",
    "RUNTIME_DIR_NOT_A_DIRECTORY",
    "RUNTIME_DIR_TOO_PERMISSIVE",
    "RUNTIME_DIR_UNAVAILABLE",
    "RUNTIME_DIR_WRONG_OWNER",
    "SOCKET_PATH_OCCUPIED",
    "SOCKET_PATH_TOO_LONG",
    "SOURCE_OVERRIDE",
    "SOURCE_PER_USER",
    "AgentSocketError",
    "AgentSocketResolution",
    "SocketPathDiagnostic",
    "client_agent_socket",
    "client_lineage_socket",
    "current_uid",
    "daemon_agent_socket",
    "daemon_lineage_socket",
    "describe_socket_path",
    "format_agent_socket_path",
    "format_lineage_socket_path",
    "home_for_uid",
    "host_runtime_directory",
    "local_nodename",
    "legacy_migration_warning",
    "legacy_runtime_dir_socket_path",
    "legacy_socket_paths",
    "legacy_socket_present",
    "resolve_agent_socket_path",
    "resolve_lineage_socket_path",
    "shell_lineage_socket_path",
]
