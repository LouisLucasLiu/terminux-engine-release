# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`terminux-tool`：使用者自行登記 EDA executable（不需管理員權限）。

- `terminux-tool register <id> [--executable <name> ...]`
- `terminux-tool unregister <id>`
- `terminux-tool list`

只改動本 UID 的 `<home>/.terminux/tools/user_tool_catalog.yaml` 與
`<home>/.terminux/bin/` 內指向 `_terminux_wrapper` 的 symlink；不執行被登記的
程式、不碰出貨 catalog，也不通知 daemon。登記後由
`terminux-package-status --activate` 讓執行中的 daemon 載入。

登記順序：驗證 → 建立 wrapper link → 原子寫入 overlay；寫入失敗時移除本次
建立的 link。移除順序：原子寫入 overlay → 移除確定屬於該工具的 link。

Exit code：0 成功；1 拒絕；2 非預期的內部錯誤。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminux_engine.tool_identity import user_catalog as uc
from terminux_engine.tool_identity.catalog import CatalogError, ToolCatalog, load_catalog

ALREADY_REGISTERED = "USER_TOOL_ALREADY_REGISTERED"
NOT_REGISTERED = "USER_TOOL_NOT_REGISTERED"
PATH_OCCUPIED = "USER_TOOL_EXECUTABLE_PATH_OCCUPIED"
BIN_UNSAFE = "USER_TOOL_BIN_DIR_UNSAFE"
BUSY = "USER_TOOL_CATALOG_BUSY"
WRITE_FAILED = "USER_TOOL_CATALOG_WRITE_FAILED"

LINK_CREATED = "CREATED"
LINK_EXISTING = "EXISTING"
LINK_PENDING = "PENDING_WRAPPER_SETUP"
LINK_REMOVED = "REMOVED"
LINK_ABSENT = "ABSENT"
LINK_KEPT_NOT_OURS = "KEPT_NOT_A_TERMINUX_WRAPPER_LINK"
LINK_KEPT_SHARED = "KEPT_SHARED_NAME"
LINK_KEPT_BIN_UNSAFE = "KEPT_BIN_DIR_UNSAFE"

_LOCK_TIMEOUT_SEC = 10.0
_ACTIVATE_HINT = "run `terminux-package-status --activate` to load it into the running daemon"


@dataclass(frozen=True)
class ToolChangeResult:
    status: str  # REGISTERED | UNCHANGED | UNREGISTERED
    tool_id: str
    executables: tuple[str, ...]
    catalog_path: str
    digest: str
    links: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "tool_id": self.tool_id, "executables": list(self.executables),
            "catalog_path": self.catalog_path, "digest": self.digest, "links": dict(self.links),
            "warnings": list(self.warnings),
        }


# ── 鎖、讀寫 ───────────────────────────────────────────────────────────


@contextmanager
def _catalog_lock(tools_dir: Path) -> Iterator[None]:
    """`<tools_dir>/.lock` 的 flock，避免同時 register／unregister 遺失更新。"""
    path = tools_dir / ".lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise uc.UserToolError(uc.CATALOG_UNSAFE, f"{path} is not a private regular file")
        deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise uc.UserToolError(BUSY, "another terminux-tool command is in progress") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_entries(path: Path) -> list[tuple[str, tuple[str, ...]]] | None:
    """讀取目前 overlay 的原始項目；檔案無效時拒絕任何修改（不覆寫使用者的檔案）。"""
    data = uc.read_user_catalog_bytes(path)
    if data is None:
        return None
    try:
        raw = uc.parse_user_catalog(data.decode("utf-8"), source=str(path))
    except UnicodeDecodeError:
        raise uc.UserToolError(uc.CATALOG_INVALID, f"{path} is not UTF-8 text") from None
    entries: list[tuple[str, tuple[str, ...]]] = []
    for raw_id, entry in raw:
        try:
            entries.append(uc._entry_executables(raw_id, entry))
        except uc.UserToolError as exc:
            raise uc.UserToolError(
                uc.CATALOG_INVALID,
                f"{path} contains an invalid entry ({exc.code}: {exc.message}); "
                "fix or remove that entry by hand before using terminux-tool") from None
    return entries


def _write_catalog(path: Path, entries: list[tuple[str, tuple[str, ...]]]) -> str:
    """暫存檔＋`os.replace` 原子寫入，權限 0600；回傳新內容的 digest。"""
    data = uc.render_user_catalog(entries).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".user_tool_catalog.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise uc.UserToolError(WRITE_FAILED, f"cannot write {path}: {exc}") from None
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return uc.bytes_digest(data)


def _load_builtin() -> ToolCatalog:
    try:
        return load_catalog()
    except CatalogError as exc:
        raise uc.UserToolError(uc.BUILTIN_UNAVAILABLE, f"the shipped tool catalog could not be loaded: {exc}") from None


# ── wrapper link ───────────────────────────────────────────────────────


def _wrapper_installed(bin_dir: Path) -> bool:
    """`terminux-setup-wrappers` 已安裝 `_terminux_wrapper`，且 bin 目錄安全。"""
    try:
        directory = bin_dir.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) & 0o022):
        raise uc.UserToolError(
            BIN_UNSAFE, f"{bin_dir} must be a directory owned by you and not writable by group or other")
    try:
        wrapper = (bin_dir / uc.WRAPPER_TARGET).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(wrapper.st_mode) and wrapper.st_uid == os.geteuid()


def _bin_dir_safe(bin_dir: Path) -> bool:
    """bin 目錄是本人擁有、非 symlink、group／other 不可寫的目錄。"""
    try:
        metadata = bin_dir.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and not stat.S_IMODE(metadata.st_mode) & 0o022)


def _create_links(bin_dir: Path, executables: tuple[str, ...]) -> dict[str, str]:
    """建立缺少的 link；任何一個名稱被其他檔案占用就整批不做（已建立的會移除）。"""
    if not _wrapper_installed(bin_dir):
        return {name: LINK_PENDING for name in executables}
    states = {name: uc.link_state(bin_dir, name) for name in executables}
    occupied = sorted(name for name, state in states.items() if state == "OCCUPIED")
    if occupied:
        raise uc.UserToolError(
            PATH_OCCUPIED,
            f"{', '.join(str(bin_dir / name) for name in occupied)} already exists and is not a "
            "Terminux wrapper link; it was left untouched")
    created: list[str] = []
    links: dict[str, str] = {}
    try:
        for name in executables:
            if states[name] == "LINKED":
                links[name] = LINK_EXISTING
                continue
            os.symlink(uc.WRAPPER_TARGET, bin_dir / name)
            created.append(name)
            links[name] = LINK_CREATED
    except OSError as exc:
        _remove_created(bin_dir, created)
        raise uc.UserToolError(PATH_OCCUPIED, f"cannot create wrapper link in {bin_dir}: {exc}") from None
    return links


def _remove_created(bin_dir: Path, names: list[str]) -> None:
    for name in names:
        if uc.link_state(bin_dir, name) == "LINKED":
            try:
                os.unlink(bin_dir / name)
            except OSError:
                pass


def _packages_using(tool_id: str) -> tuple[list[str], str | None]:
    """已安裝的 user package 中，`when.tools` 含此工具者（只讀，盡力而為）。"""
    try:
        from terminux_engine.package_runtime import user_packages as up
        from terminux_engine.package_runtime.manifest import load_manifest

        root = up.resolve_user_packages_root(create=False)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "USER_PACKAGE_ROOT_MISSING":
            return [], None
        return [], f"installed packages could not be inspected: {exc}"
    users: list[str] = []
    try:
        for entry in sorted(os.scandir(root), key=lambda item: item.name):
            if entry.name.startswith(".") or not up.PACKAGE_ID_RE.fullmatch(entry.name):
                continue
            try:
                pointer = up.read_pointer(Path(entry.path))
                if pointer is None:
                    continue
                manifest = load_manifest(
                    Path(entry.path) / up.GENERATIONS_DIR_NAME / pointer.generation / "package.yaml")
            except Exception:
                continue
            if tool_id in manifest.when_tools:
                users.append(entry.name)
    except OSError as exc:
        return users, f"installed packages could not be inspected: {exc}"
    return users, None


# ── 操作 ───────────────────────────────────────────────────────────────


def _remove_links(bin_dir: Path, names: tuple[str, ...],
                  remaining: list[tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    """只移除這個工具自己的 wrapper link；內建／保留名稱與其他使用者工具的一律保留。"""
    if not _bin_dir_safe(bin_dir):
        return {name: (LINK_ABSENT if not os.path.lexists(bin_dir) else LINK_KEPT_BIN_UNSAFE)
                for name in names}
    protected: set[str] = set()
    try:
        builtin = load_catalog()
        protected |= set(builtin.executables()) | set(builtin.canonical_ids())
    except CatalogError:
        protected |= set(names)  # 無法確認時一律保留
    reserved = uc.load_reserved_names()
    protected |= set(reserved.names) | set(reserved.builtin_package_names)
    if reserved.builtin_package_error is not None:
        protected |= set(names)  # 內建名稱無法完整確認時一律保留 link
    for _, exes in remaining:
        protected |= set(exes)
    links: dict[str, str] = {}
    for name in names:
        if name in protected:
            links[name] = LINK_KEPT_SHARED
            continue
        state = uc.link_state(bin_dir, name)
        if state == "MISSING":
            links[name] = LINK_ABSENT
        elif state == "LINKED":
            try:
                os.unlink(bin_dir / name)
                links[name] = LINK_REMOVED
            except OSError:
                links[name] = LINK_KEPT_NOT_OURS
        else:
            links[name] = LINK_KEPT_NOT_OURS
    return links


def register_tool(tool_id: str, executables: list[str] | None = None) -> ToolChangeResult:
    """登記一個使用者工具；executables 省略時等於 `[tool_id]`。"""
    names = tuple(executables) if executables else (tool_id,)
    uc.check_tool_name(tool_id)
    for name in names:
        uc.check_tool_name(name)
    if len(set(names)) != len(names):
        raise uc.UserToolError(uc.ENTRY_INVALID, "the same executable is listed twice")
    if len(names) > uc.MAX_EXECUTABLES_PER_TOOL:
        raise uc.UserToolError(uc.ENTRY_INVALID, f"at most {uc.MAX_EXECUTABLES_PER_TOOL} executables per tool")
    builtin = _load_builtin()
    home = uc.resolve_user_home()
    tools_dir = uc.resolve_user_tools_dir(create=True)
    path = tools_dir / uc.USER_CATALOG_FILE_NAME
    bin_dir = uc.wrapper_bin_dir(home)
    with _catalog_lock(tools_dir):
        entries = _read_entries(path) or []
        existing = dict(entries)
        if tool_id in existing and existing[tool_id] != names:
            raise uc.UserToolError(
                ALREADY_REGISTERED,
                f"{tool_id!r} is already registered with executables {list(existing[tool_id])}; "
                f"run `terminux-tool unregister {tool_id}` first to change it")
        candidate = entries if tool_id in existing else [*entries, (tool_id, names)]
        _, records = uc.compose_effective_catalog(
            builtin, [(tid, {"executables": list(exes)}) for tid, exes in candidate],
            uc.load_reserved_names())
        record = next(r for r in records if r.tool_id == tool_id)
        if record.status != uc.STATUS_ACCEPTED:
            raise uc.UserToolError(record.reason or uc.ENTRY_INVALID, record.message or "rejected")
        links = _create_links(bin_dir, names)
        if tool_id in existing:
            data = uc.read_user_catalog_bytes(path) or b""
            return ToolChangeResult(
                status="UNCHANGED", tool_id=tool_id, executables=names, catalog_path=str(path),
                digest=uc.bytes_digest(data), links=links)
        try:
            digest = _write_catalog(path, candidate)
        except Exception:
            _remove_created(bin_dir, [name for name, state in links.items() if state == LINK_CREATED])
            raise
    return ToolChangeResult(
        status="REGISTERED", tool_id=tool_id, executables=names, catalog_path=str(path),
        digest=digest, links=links)


def unregister_tool(tool_id: str) -> ToolChangeResult:
    """移除使用者工具與它自己的 wrapper link；不刪除任何 Package。"""
    uc.check_tool_name(tool_id)
    home = uc.resolve_user_home()
    try:
        tools_dir = uc.resolve_user_tools_dir(create=False)
    except uc.UserToolError as exc:
        if exc.code == uc.ROOT_MISSING:
            raise uc.UserToolError(NOT_REGISTERED, f"{tool_id!r} is not a registered user tool") from None
        raise
    path = tools_dir / uc.USER_CATALOG_FILE_NAME
    bin_dir = uc.wrapper_bin_dir(home)
    with _catalog_lock(tools_dir):
        entries = _read_entries(path) or []
        existing = dict(entries)
        if tool_id not in existing:
            raise uc.UserToolError(NOT_REGISTERED, f"{tool_id!r} is not a registered user tool")
        names = existing[tool_id]
        remaining = [(tid, exes) for tid, exes in entries if tid != tool_id]
        digest = _write_catalog(path, remaining)
        # link 也在同一個鎖內處理，避免與同名的 register 交錯。
        links = _remove_links(bin_dir, names, remaining)
    users, inspect_error = _packages_using(tool_id)
    warnings: list[str] = []
    if users:
        warnings.append(
            f"installed user package(s) {', '.join(users)} still list {tool_id!r} in when.tools; "
            "they stay installed but will not be triggered by this tool")
    if inspect_error:
        warnings.append(inspect_error)
    return ToolChangeResult(
        status="UNREGISTERED", tool_id=tool_id, executables=names, catalog_path=str(path),
        digest=digest, links=links, warnings=tuple(warnings))


def list_tools() -> dict[str, Any]:
    """本機觀點：內建工具、使用者工具（含拒絕原因）與每個 executable 的 wrapper 狀態。"""
    try:
        builtin: ToolCatalog | None = load_catalog()
        builtin_error = None
    except CatalogError as exc:
        builtin, builtin_error = None, str(exc)
    view = uc.read_user_catalog_view(builtin)
    try:
        home: Path | None = uc.resolve_user_home()
    except uc.UserToolError:
        home = None
    user_rows: list[dict[str, Any]] = []
    for record in view.records:
        row = record.to_dict()
        row["origin"] = "user"
        row["wrapper"] = ([uc.wrapper_readiness(name, home=home) for name in record.executables]
                          if home is not None and record.status == uc.STATUS_ACCEPTED else [])
        user_rows.append(row)
    return {
        "builtin": {
            "loaded": builtin is not None,
            "error": builtin_error,
            "tools": [{"tool_id": tid, "executables": list(builtin.executables_for(tid)), "origin": "builtin"}
                      for tid in builtin.canonical_ids()] if builtin is not None else [],
        },
        "user_catalog": {
            "path": str(view.path) if view.path is not None else None,
            "state": view.state,
            "digest": view.digest,
            "error": view.error,
            "fingerprint": uc.catalog_fingerprint(view.catalog) if view.catalog is not None else None,
        },
        "user_tools": user_rows,
    }


# ── CLI ────────────────────────────────────────────────────────────────


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _refuse(error: uc.UserToolError, fmt: str) -> int:
    if fmt == "json":
        _print_json({"status": "REFUSED", "error": error.to_dict()})
    else:
        print(f"REFUSED {error.code}: {error.message}")
        for detail in error.details:
            print(f"  - {detail}")
    return 1


def _print_change(result: ToolChangeResult) -> None:
    verb = {"REGISTERED": "Registered", "UNCHANGED": "Already registered",
            "UNREGISTERED": "Unregistered"}[result.status]
    print(f"{verb} user tool {result.tool_id} (executables: {', '.join(result.executables)})")
    print(f"  catalog: {result.catalog_path}")
    print(f"  digest:  {result.digest}")
    for name, state in result.links.items():
        print(f"  wrapper link {name}: {state}")
    if any(state == LINK_PENDING for state in result.links.values()):
        print("  The Terminux wrapper is not installed yet; links are created the next time "
              "Terminux starts (terminux-setup-wrappers).")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    print(f"Not active yet: {_ACTIVATE_HINT}.")


def _print_list(payload: dict[str, Any]) -> None:
    builtin = payload["builtin"]
    if builtin["loaded"]:
        names = ", ".join(item["tool_id"] for item in builtin["tools"])
        print(f"Built-in tools ({len(builtin['tools'])}, origin=builtin): {names}")
    else:
        print(f"Built-in tools: UNAVAILABLE ({builtin['error']})")
    catalog = payload["user_catalog"]
    print(f"User tool catalog: {catalog['path'] or '(home directory could not be resolved)'}")
    print(f"  state: {catalog['state']}" + (f"  digest={catalog['digest']}" if catalog["digest"] else ""))
    if catalog["error"]:
        print(f"  error: {catalog['error']['code']}: {catalog['error']['message']}")
        print("  Built-in tools keep working; user tools from this file are not used.")
    if not payload["user_tools"]:
        print("No user tools registered.")
    for row in payload["user_tools"]:
        print(f"{row['tool_id']} (origin=user) executables: {', '.join(row['executables']) or '-'}")
        if row["status"] == uc.STATUS_ACCEPTED:
            print("  STATUS   ACCEPTED")
        else:
            print(f"  STATUS   REJECTED {row['reason']}: {row['message']}")
        for wrapper in row["wrapper"]:
            text = f"  WRAPPER  {wrapper['executable']}: {wrapper['reason']}"
            if wrapper["real_executable"]:
                text += f" (real executable: {wrapper['real_executable']})"
            print(text)
    print("To see what the running daemon has loaded: terminux-package-status")


def main(argv: list[str] | None = None) -> int:
    """`terminux-tool {register,unregister,list}`。"""
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(
        prog="terminux-tool", description="Register your own EDA executables with Terminux.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="register a user tool")
    register.add_argument("tool_id")
    register.add_argument("--executable", action="append", default=None, metavar="NAME",
                          help="executable name to wrap (repeatable; default: the tool id)")
    unregister = commands.add_parser("unregister", help="remove a user tool")
    unregister.add_argument("tool_id")
    commands.add_parser("list", help="show built-in and user tools")
    for sub in (register, unregister, commands.choices["list"]):
        sub.add_argument("--format", choices=("text", "json"), default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            payload = list_tools()
            if args.format == "json":
                _print_json(payload)
            else:
                _print_list(payload)
            return 0
        if args.command == "register":
            result = register_tool(args.tool_id, args.executable)
        else:
            result = unregister_tool(args.tool_id)
    except uc.UserToolError as exc:
        return _refuse(exc, args.format)
    except Exception as exc:
        print(f"terminux-tool: unexpected internal error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        _print_json(result.to_dict())
    else:
        _print_change(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
