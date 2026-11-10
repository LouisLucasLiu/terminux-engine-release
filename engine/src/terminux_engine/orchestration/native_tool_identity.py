# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""host-native 工具身分與子行程環境邊界（flow task、formal pair、ECO 共用）。

站點（CAD／IT）安裝的工具以「授權時解析的 realpath ＋ 內容 SHA-256」釘住。
每次派送前重新解析與重算；路徑、內容或可執行性任一不符即 fail closed，
不會因為站點更新工具而默默接受新的 binary。

身分分三層，彼此不混用：

- `declared`：授權（Profile／host binding）宣告的 pin；
- `resolved_before_run`：派送前實際重算、與 pin 相符的結果；
- `executed_confirmed`：工具真的以該 binary 執行——本模組**無法**證明，
  一律記為 `NOT_VERIFIED`。

子行程環境：共同 executor 會把 `PreparedExecution.env` 疊在呼叫端環境上，
因此 host-native argv 一律以 `env -i` 開頭，只帶入 allowlist。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.orfs_native_adapter import ENV_ALLOWLIST

IDENTITY_DECLARED = "declared"
IDENTITY_RESOLVED_BEFORE_RUN = "resolved_before_run"
EXECUTED_IDENTITY_NOT_VERIFIED = "NOT_VERIFIED"

ENV_EXECUTABLE = Path("/usr/bin/env")
BASH_EXECUTABLE = Path("/bin/bash")
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: 失敗碼。全部 fail closed：沒有任何一種會降級成「只比對版本字串」。
TOOL_PIN_INVALID = "NATIVE_TOOL_PIN_INVALID"
TOOL_MISSING = "NATIVE_TOOL_MISSING"
TOOL_REALPATH_CHANGED = "NATIVE_TOOL_REALPATH_CHANGED"
TOOL_DIGEST_MISMATCH = "NATIVE_TOOL_DIGEST_MISMATCH"
TOOL_NOT_EXECUTABLE = "NATIVE_TOOL_NOT_EXECUTABLE"


class NativeToolIdentityError(ValueError):
    """工具身分與授權的 pin 不符；派送不得繼續。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FilePin:
    """一個以 realpath ＋ SHA-256 釘住的檔案（工具或 env script）。"""

    realpath: str
    sha256: str

    def to_json_dict(self) -> dict[str, str]:
        return {"realpath": self.realpath, "sha256": self.sha256}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_pin(node: Any, *, where: str) -> FilePin:
    """驗證 `{"realpath": <abs>, "sha256": <64 hex>}`；多一個欄位也拒絕。"""
    if not isinstance(node, dict) or set(node) != {"realpath", "sha256"}:
        raise NativeToolIdentityError(
            TOOL_PIN_INVALID, f"{where} requires exactly realpath and sha256")
    realpath, digest = node["realpath"], node["sha256"]
    if (not isinstance(realpath, str) or not realpath
            or not Path(realpath).is_absolute() or "\0" in realpath or "\n" in realpath):
        raise NativeToolIdentityError(TOOL_PIN_INVALID, f"{where}.realpath must be absolute")
    if os.path.normpath(realpath) != realpath:
        raise NativeToolIdentityError(
            TOOL_PIN_INVALID, f"{where}.realpath must be a normalized path")
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        raise NativeToolIdentityError(
            TOOL_PIN_INVALID, f"{where}.sha256 must be 64 lowercase hex characters")
    return FilePin(realpath=realpath, sha256=digest)


def verify_pin(pin: FilePin, *, where: str, executable: bool = True) -> dict[str, Any]:
    """派送前重新解析 realpath 並重算內容雜湊；任何不符都拋出。

    回傳 `resolved_before_run` 的觀測結果（與 pin 相同，另附大小）。
    """
    path = Path(pin.realpath)
    if not path.is_file():
        raise NativeToolIdentityError(TOOL_MISSING, f"{where} is missing: {pin.realpath}")
    resolved = os.path.realpath(path)
    if resolved != pin.realpath:
        raise NativeToolIdentityError(
            TOOL_REALPATH_CHANGED,
            f"{where} now resolves to {resolved}, not the authorized {pin.realpath}")
    if executable and not os.access(path, os.X_OK):
        raise NativeToolIdentityError(TOOL_NOT_EXECUTABLE, f"{where} is not executable")
    observed = sha256_file(path)
    if observed != pin.sha256:
        raise NativeToolIdentityError(
            TOOL_DIGEST_MISMATCH,
            f"{where} content digest {observed} differs from the authorized {pin.sha256}")
    return {"status": IDENTITY_RESOLVED_BEFORE_RUN, "realpath": resolved,
            "sha256": observed, "size": path.stat().st_size}


def allowlisted_env() -> dict[str, str]:
    """從呼叫端環境只取 allowlist 內的名稱。"""
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.setdefault("PATH", _DEFAULT_PATH)
    return env


def strict_env_argv(argv: list[str], *, extra_env: dict[str, str] | None = None) -> list[str]:
    """`env -i <allowlist> [extra] <argv>`：子行程不繼承呼叫端環境。

    `extra_env` 由 adapter 自己組裝（例如固定的工具變數），不接受 caller 值。
    """
    env = allowlisted_env()
    for name, value in (extra_env or {}).items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) or "\0" in value:
            raise NativeToolIdentityError(TOOL_PIN_INVALID, f"invalid environment entry {name!r}")
        env[name] = value
    return [str(ENV_EXECUTABLE), "-i",
            *(f"{name}={value}" for name, value in sorted(env.items())), *argv]


#: native 子行程的 run 標記與 process group 紀錄檔。取消時只認同時具備
#: 「pgid 紀錄」與「環境中的本 run 標記」的 process group，避免 pid 重用時誤殺。
RUN_MARKER_ENV = "TERMINUX_NATIVE_RUN_ID"
PGID_RECORD_NAME = "terminux_native_process_group"


def pgid_record_line(output_root: str | Path) -> str:
    """script 第一行：記下本 process group（launcher→env→bash 同一個 pid）。"""
    import shlex

    target = shlex.quote(str(Path(output_root) / PGID_RECORD_NAME))
    return f"mkdir -p {shlex.quote(str(output_root))} && echo \"$$\" > {target}"


def _group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # `)` 之後依序是 state、ppid、pgrp。殭屍不算仍在執行。
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            members.append(int(entry.name))
    return members


def _carries_marker(pid: int, run_id: str) -> bool:
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.geteuid():
            return False
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    return f"{RUN_MARKER_ENV}={run_id}".encode() in environ


def release_native_process_group(output_root: str | Path, run_id: str, *,
                                 grace_seconds: float = 5.0,
                                 deadline_seconds: float = 20.0) -> dict[str, Any]:
    """停止本 run 的 native process group，並確認它已不存在。

    只在 pgid 紀錄存在、且 group 內有屬於本使用者並帶本 run 標記的成員時才送
    signal；先 SIGTERM，逾 `grace_seconds` 改 SIGKILL，`deadline_seconds` 內
    無法確認就回報未確認（`ok: False`），不拋例外。
    """
    import signal
    import time

    record = Path(output_root) / PGID_RECORD_NAME
    try:
        pgid = int(record.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return {"attempted": False, "ok": True, "resource_kind": "native_process_group",
                "reason": "no_process_group_record"}
    members = _group_members(pgid)
    if not members:
        return {"attempted": True, "ok": True, "resource_kind": "native_process_group",
                "pgid": pgid, "reason": "confirmed_absent"}
    if not any(_carries_marker(pid, run_id) for pid in members):
        return {"attempted": True, "ok": False, "resource_kind": "native_process_group",
                "pgid": pgid, "reason": "identity_mismatch"}
    started = time.monotonic()
    sent = signal.SIGTERM
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        return {"attempted": True, "ok": False, "resource_kind": "native_process_group",
                "pgid": pgid, "reason": "not_permitted"}
    while time.monotonic() - started < deadline_seconds:
        if not _group_members(pgid):
            return {"attempted": True, "ok": True, "resource_kind": "native_process_group",
                    "pgid": pgid, "reason": "confirmed_absent", "signal": sent.name}
        if sent is signal.SIGTERM and time.monotonic() - started >= grace_seconds:
            sent = signal.SIGKILL
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    return {"attempted": True, "ok": False, "resource_kind": "native_process_group",
            "pgid": pgid, "reason": "deadline_exceeded", "signal": sent.name}


def pin_check_script(pins: dict[str, FilePin], *, exit_code: int) -> str:
    """bash 片段：執行任何工具之前，在子行程內再核對一次全部 pin。

    派送前的 `verify_pin` 與實際 exec 之間仍有時間差；這段在同一個 process
    group 內、緊接在使用工具之前重新解析 realpath 並重算 SHA-256，不符即以
    `exit_code` 結束，不執行任何工具。
    """
    import shlex

    lines = [
        "native_pin_check() {",
        '  if [ ! -f "$2" ]; then echo "TERMINUX_NATIVE_TOOL_MISSING $1" >&2; '
        f"exit {exit_code}; fi",
        '  if [ "$(readlink -f -- "$2")" != "$2" ]; then '
        'echo "TERMINUX_NATIVE_TOOL_REALPATH_CHANGED $1" >&2; '
        f"exit {exit_code}; fi",
        '  if [ "$(sha256sum -- "$2" | cut -d" " -f1)" != "$3" ]; then '
        'echo "TERMINUX_NATIVE_TOOL_DIGEST_MISMATCH $1" >&2; '
        f"exit {exit_code}; fi",
        "}",
    ]
    for name in sorted(pins):
        pin = pins[name]
        lines.append(f"native_pin_check {name} {shlex.quote(pin.realpath)} "
                     f"{shlex.quote(pin.sha256)}")
    return "\n".join(lines)


def identity_layers(declared: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    """receipt／evidence 內的三層身分；`executed_confirmed` 永遠不宣稱已證明。"""
    return {IDENTITY_DECLARED: declared, IDENTITY_RESOLVED_BEFORE_RUN: resolved,
            "executed_confirmed": EXECUTED_IDENTITY_NOT_VERIFIED}


__all__ = [
    "BASH_EXECUTABLE", "ENV_EXECUTABLE", "EXECUTED_IDENTITY_NOT_VERIFIED", "FilePin",
    "IDENTITY_DECLARED", "IDENTITY_RESOLVED_BEFORE_RUN", "NativeToolIdentityError",
    "PGID_RECORD_NAME", "RUN_MARKER_ENV",
    "TOOL_DIGEST_MISMATCH", "TOOL_MISSING", "TOOL_NOT_EXECUTABLE", "TOOL_PIN_INVALID",
    "TOOL_REALPATH_CHANGED", "allowlisted_env", "identity_layers", "parse_pin",
    "pgid_record_line", "pin_check_script", "release_native_process_group",
    "sha256_file", "strict_env_argv", "verify_pin",
]
