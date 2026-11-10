# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""使用者工具 overlay：`<home>/.terminux/tools/user_tool_catalog.yaml`。

格式沿用出貨 catalog 的 schema v1::

    schema_version: 1
    tools:
      demo_eda:
        executables: [demo_eda]

合併規則（effective catalog）：

- 出貨 catalog 是權威。它載入失敗時沿用既有 fail-closed 行為（effective
  catalog 為 None），overlay 不參與。
- 使用者項目只能**附加**。id 或 executable 撞到出貨 catalog、出貨
  `tool_signatures.yaml` 的 pattern、內建 Package 的 id 或 `when.tools` 名稱、
  wrapper／queue 保留名稱或 wrapper 自己依賴的指令時，只拒絕那一筆，其他項目
  與內建工具照常。
- 多筆使用者項目用到同一個名稱時全部拒絕，不挑選贏家。
- 整個檔案 malformed（YAML、schema、型別、大小）時整份 overlay 不採用；
  內建工具不受影響。

本模組只處理 executable identity，不處理 dispatch、Package 或 execution
policy。路徑解析與 `package_runtime.user_packages` 相同：home 由
`ipc.agent_socket.home_for_uid()` 依 effective UID 取得（支援
`TERMINUX_HOME_DIR`，不讀 `$HOME`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.ipc import agent_socket
from terminux_engine.resource_paths import bundled_knowledge_path
from terminux_engine.tool_identity.catalog import (
    ORIGIN_BUILTIN,
    ORIGIN_USER,
    CatalogError,
    ToolCatalog,
    _DuplicateKeyLoader,
)

USER_TOOLS_DIR_NAME = "tools"
USER_CATALOG_FILE_NAME = "user_tool_catalog.yaml"
USER_CATALOG_SCHEMA = 1
MAX_USER_CATALOG_BYTES = 64 * 1024
MAX_USER_TOOLS = 64
MAX_EXECUTABLES_PER_TOOL = 8

#: id 與 executable 共用的名稱規則：不可能形成路徑、不含空白或引號，
#: 第一個字元不可為 `_`、`.` 或 `-`。
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_RESERVED_PREFIX = "terminux"

#: wrapper 本身（`terminux-wrapper-entry`／`terminux-wrapper-lib.sh`）以 PATH
#: 查找的外部指令，以及 shell／Python 直譯器。登記成 wrapper 會讓 wrapper
#: 呼叫到自己而無限遞迴，因此保留。
WRAPPER_RUNTIME_COMMANDS = frozenset({
    "basename", "bash", "date", "dirname", "env", "hostname", "mkdir", "pwd", "python",
    "python3", "setsid", "sh", "stat",
})
#: `~/.terminux/bin` 內由 Terminux 自己管理的檔名。
WRAPPER_INTERNAL_NAMES = frozenset({
    "srun", "terminux-notify", "terminux-tcsh-postcmd",
})
WRAPPER_TARGET = "_terminux_wrapper"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "resources" / "scripts"
_BUILTIN_PACKAGES_DIR = Path(__file__).resolve().parents[1] / "packages"
_RESERVED_LIST_FILES = ("wrapped_tools.txt", "queue_tools.txt")

STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"

# Overlay 整體狀態（daemon activation 與 CLI 共用）。
STATE_ABSENT = "ABSENT"
STATE_LOADED = "LOADED"
STATE_INVALID_BUILTIN_ONLY = "INVALID_BUILTIN_ONLY"
STATE_BUILTIN_UNAVAILABLE = "BUILTIN_UNAVAILABLE"

# 錯誤／拒絕原因碼。
ROOT_MISSING = "USER_TOOL_ROOT_MISSING"
ROOT_INVALID = "USER_TOOL_ROOT_INVALID"
CATALOG_INVALID = "USER_TOOL_CATALOG_INVALID"
CATALOG_UNSAFE = "USER_TOOL_CATALOG_UNSAFE"
ENTRY_INVALID = "USER_TOOL_ENTRY_INVALID"
NAME_INVALID = "USER_TOOL_NAME_INVALID"
SHADOWS_BUILTIN = "USER_TOOL_SHADOWS_BUILTIN"
USED_BY_BUILTIN_PACKAGE = "USER_TOOL_USED_BY_BUILTIN_PACKAGE"
MATCHES_SIGNATURE = "USER_TOOL_MATCHES_BUILTIN_SIGNATURE"
RESERVED_NAME = "USER_TOOL_RESERVED_NAME"
DUPLICATE = "USER_TOOL_DUPLICATE"
SIGNATURES_UNAVAILABLE = "USER_TOOL_SIGNATURES_UNAVAILABLE"
BUILTIN_PACKAGES_UNREADABLE = "USER_TOOL_BUILTIN_PACKAGES_UNREADABLE"
BUILTIN_UNAVAILABLE = "TOOL_CATALOG_BUILTIN_UNAVAILABLE"

# Wrapper readiness 原因碼（只讀檢查，CLI 用）。
WRAPPER_READY = "WRAPPER_READY"
WRAPPER_NOT_INSTALLED = "WRAPPER_NOT_INSTALLED"
WRAPPER_LINK_MISSING = "WRAPPER_LINK_MISSING"
WRAPPER_LINK_OCCUPIED = "WRAPPER_LINK_OCCUPIED"
WRAPPER_NOT_FIRST_ON_PATH = "WRAPPER_NOT_FIRST_ON_PATH"
REAL_EXECUTABLE_NOT_FOUND = "REAL_EXECUTABLE_NOT_FOUND"
HOME_MISMATCH = "HOME_MISMATCH"
NOTIFY_HELPER_MISSING = "NOTIFY_HELPER_MISSING"
WRAPPER_NOT_READABLE = "WRAPPER_NOT_READABLE"


class UserToolError(ValueError):
    """overlay 路徑、檔案或單一項目不符規則時拋出，帶穩定的原因碼。"""

    def __init__(self, code: str, message: str, *, details: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": list(self.details)}


@dataclass(frozen=True)
class UserToolRecord:
    """單一使用者項目的合併結果。"""

    tool_id: str
    executables: tuple[str, ...]
    status: str
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "executables": list(self.executables),
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class ReservedNames:
    """使用者項目不可使用的名稱，以及出貨 signatures 的 pattern。"""

    names: frozenset[str]
    signature_names: frozenset[str]
    signature_patterns: tuple[re.Pattern[str], ...]
    signature_error: str | None = None
    # 內建 Package 的 id 與 `when.tools` 名稱 → Package id。使用者工具若用到這些
    # 名稱，wrapper 事件就會觸發內建 Package，因此一律拒絕。
    builtin_package_names: dict[str, str] = field(default_factory=dict)
    # 任何內建 manifest 無法完整讀取時，保留表可能不完整：所有使用者項目 fail closed。
    builtin_package_error: str | None = None


# ── 名稱規則與保留名稱 ─────────────────────────────────────────────────


def check_tool_name(name: object) -> str:
    """回傳合法名稱；不合法時拋 `NAME_INVALID`。"""
    if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
        raise UserToolError(
            NAME_INVALID,
            f"invalid tool name {name!r}: must match {TOOL_NAME_RE.pattern} "
            "(a plain command name, not a path)")
    if name.lower().startswith(_RESERVED_PREFIX):
        raise UserToolError(NAME_INVALID, f"tool name {name!r} uses the reserved prefix 'terminux'")
    return name


def _read_name_list(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].replace(" ", "").strip()
        if name:
            names.add(name)
    return names


def _builtin_package_names(packages_dir: Path = _BUILTIN_PACKAGES_DIR) -> tuple[dict[str, str], tuple[str, ...]]:
    """內建 Package 的 id 與 `when.tools`，回傳 (名稱 → Package id, 錯誤)。

    與內建 registry 相同：子目錄內有 `package.yaml` 才算 Package，並以同一個
    `load_manifest()` 解析（只讀 manifest，不 import Package 程式碼）。任何一份
    讀不到、格式不合、目錄無法列出或其他例外都列入錯誤，不默默略過。
    """
    from terminux_engine.package_runtime.manifest import load_manifest

    names: dict[str, str] = {}
    errors: list[str] = []
    try:
        entries = sorted(packages_dir.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        return names, (f"{packages_dir}: {type(exc).__name__}: {exc}",)
    for entry in entries:
        manifest_path = entry / "package.yaml"
        try:
            if not entry.is_dir():
                continue
            os.stat(manifest_path)
        except FileNotFoundError:
            continue  # 沒有 manifest 的目錄不是 Package（與 registry 相同）
        except OSError as exc:
            errors.append(f"{entry.name}/package.yaml: {type(exc).__name__}: {exc}")
            continue
        names.setdefault(entry.name, entry.name)
        try:
            manifest = load_manifest(manifest_path)
        except Exception as exc:
            errors.append(f"{entry.name}/package.yaml: {type(exc).__name__}: {exc}")
            continue
        names.setdefault(manifest.package_id, manifest.package_id)
        for tool in manifest.when_tools:
            names.setdefault(tool, manifest.package_id)
    return names, tuple(errors)


def load_reserved_names() -> ReservedNames:
    """讀取 wrapper／queue 保留清單、出貨 `tool_signatures.yaml` 與內建 Package 使用的名稱。

    signatures 讀不到時不猜測：`signature_error` 會讓所有使用者項目被拒絕
    （fail closed），內建工具不受影響。
    """
    names = set(WRAPPER_RUNTIME_COMMANDS) | set(WRAPPER_INTERNAL_NAMES)
    for filename in _RESERVED_LIST_FILES:
        path = _SCRIPTS_DIR / filename
        if path.is_file():
            names |= _read_name_list(path)
    patterns: list[re.Pattern[str]] = []
    signature_names: set[str] = set()
    error: str | None = None
    try:
        data = yaml.safe_load(bundled_knowledge_path("tool_signatures.yaml").read_text(encoding="utf-8"))
        for tool in data["tools"]:
            signature_names.add(str(tool["name"]))
            for pattern in tool["patterns"]:
                patterns.append(re.compile(pattern))
    except Exception as exc:  # 任何讀取／格式問題都視為無法比對
        error = f"{type(exc).__name__}: {exc}"
    try:
        package_names, package_errors = _builtin_package_names()
    except Exception as exc:
        package_names, package_errors = {}, (f"{type(exc).__name__}: {exc}",)
    return ReservedNames(
        names=frozenset(names), signature_names=frozenset(signature_names),
        signature_patterns=tuple(patterns), signature_error=error,
        builtin_package_names=package_names,
        builtin_package_error="; ".join(package_errors) if package_errors else None)


def _signature_hit(name: str, reserved: ReservedNames) -> bool:
    if name in reserved.signature_names:
        return True
    # Qt 路徑以整行指令比對 pattern；檢查單獨名稱與「名稱＋參數」兩種形式。
    return any(pattern.search(name) or pattern.search(f"{name} ")
               for pattern in reserved.signature_patterns)


# ── 解析與合併 ─────────────────────────────────────────────────────────


def parse_user_catalog(text: str, *, source: str) -> list[tuple[Any, Any]]:
    """檔案層級驗證；回傳 (tool_id, entry) 原始項目清單（保持檔案順序）。

    任何檔案層級問題都拋 `CATALOG_INVALID`。單一項目的內容在合併時才檢查，
    讓一筆錯誤只影響它自己。
    """
    try:
        data = yaml.load(text, Loader=_DuplicateKeyLoader)
    except CatalogError as exc:
        raise UserToolError(CATALOG_INVALID, f"{source}: {exc}") from None
    except yaml.YAMLError as exc:
        raise UserToolError(CATALOG_INVALID, f"{source}: invalid YAML: {exc}") from None
    except (TypeError, ValueError, RecursionError) as exc:
        # 例如不可 hash 的 key、無效日期、過深的巢狀；一律視為檔案無效。
        raise UserToolError(CATALOG_INVALID, f"{source}: unreadable YAML: {type(exc).__name__}: {exc}") from None
    if not isinstance(data, dict):
        raise UserToolError(CATALOG_INVALID, f"{source}: the root must be a mapping")
    unknown = sorted(str(key) for key in data if key not in ("schema_version", "tools"))
    if unknown:
        raise UserToolError(CATALOG_INVALID, f"{source}: unknown top-level keys: {', '.join(unknown)}")
    if data.get("schema_version") != USER_CATALOG_SCHEMA:
        raise UserToolError(
            CATALOG_INVALID,
            f"{source}: unsupported schema_version={data.get('schema_version')!r} "
            f"(expected {USER_CATALOG_SCHEMA})")
    tools = data.get("tools")
    if tools is None:
        tools = {}
    if not isinstance(tools, dict):
        raise UserToolError(CATALOG_INVALID, f"{source}: 'tools' must be a mapping")
    if len(tools) > MAX_USER_TOOLS:
        raise UserToolError(
            CATALOG_INVALID, f"{source}: {len(tools)} tools exceed the limit of {MAX_USER_TOOLS}")
    return list(tools.items())


def _entry_executables(tool_id: Any, entry: Any) -> tuple[str, tuple[str, ...]]:
    """單一項目的形狀與名稱檢查。"""
    tool_id = check_tool_name(tool_id)
    if not isinstance(entry, dict) or set(entry) != {"executables"}:
        raise UserToolError(
            ENTRY_INVALID, f"tool {tool_id!r} must be a mapping with exactly one key 'executables'")
    executables = entry["executables"]
    if (not isinstance(executables, list) or not executables
            or len(executables) > MAX_EXECUTABLES_PER_TOOL):
        raise UserToolError(
            ENTRY_INVALID,
            f"tool {tool_id!r}: 'executables' must be a list of 1-{MAX_EXECUTABLES_PER_TOOL} names")
    names = tuple(check_tool_name(name) for name in executables)
    if len(set(names)) != len(names):
        raise UserToolError(ENTRY_INVALID, f"tool {tool_id!r} lists the same executable twice")
    return tool_id, names


def _conflict(name: str, builtin: ToolCatalog, reserved: ReservedNames) -> UserToolError | None:
    if name in builtin.canonical_ids() or builtin.resolve(name) is not None:
        return UserToolError(
            SHADOWS_BUILTIN, f"{name!r} is a built-in Terminux tool; the built-in definition wins")
    if name in reserved.builtin_package_names:
        return UserToolError(
            USED_BY_BUILTIN_PACKAGE,
            f"{name!r} is used by the built-in package {reserved.builtin_package_names[name]!r}; "
            "a user tool with this name would trigger it")
    if name in reserved.names:
        return UserToolError(
            RESERVED_NAME, f"{name!r} is reserved by the Terminux wrapper or queue integration")
    if _signature_hit(name, reserved):
        return UserToolError(
            MATCHES_SIGNATURE,
            f"{name!r} is recognized by the shipped tool signatures; it cannot become a user tool")
    return None


def compose_effective_catalog(
    builtin: ToolCatalog,
    raw_entries: list[tuple[Any, Any]],
    reserved: ReservedNames,
) -> tuple[ToolCatalog, tuple[UserToolRecord, ...]]:
    """出貨 catalog＋通過檢查的使用者項目。出貨部分原封不動排在前面。"""
    records: list[UserToolRecord] = []
    candidates: list[tuple[str, tuple[str, ...]]] = []
    for raw_id, entry in raw_entries:
        display_id = raw_id if isinstance(raw_id, str) else repr(raw_id)
        try:
            tool_id, executables = _entry_executables(raw_id, entry)
        except UserToolError as exc:
            records.append(UserToolRecord(
                tool_id=display_id, executables=(), status=STATUS_REJECTED,
                reason=exc.code, message=exc.message))
            continue
        if reserved.signature_error is not None:
            records.append(UserToolRecord(
                tool_id=tool_id, executables=executables, status=STATUS_REJECTED,
                reason=SIGNATURES_UNAVAILABLE,
                message=f"shipped tool signatures could not be read: {reserved.signature_error}"))
            continue
        if reserved.builtin_package_error is not None:
            records.append(UserToolRecord(
                tool_id=tool_id, executables=executables, status=STATUS_REJECTED,
                reason=BUILTIN_PACKAGES_UNREADABLE,
                message=("built-in package manifests could not be read completely, so name conflicts "
                         f"cannot be ruled out: {reserved.builtin_package_error}")))
            continue
        problem = None
        for name in (tool_id, *executables):
            problem = _conflict(name, builtin, reserved)
            if problem is not None:
                break
        if problem is not None:
            records.append(UserToolRecord(
                tool_id=tool_id, executables=executables, status=STATUS_REJECTED,
                reason=problem.code, message=problem.message))
            continue
        candidates.append((tool_id, executables))

    # id 與 executable 視為同一個命名空間：同一個名稱出現在兩筆以上就全部拒絕。
    owners: dict[str, set[str]] = {}
    for tool_id, executables in candidates:
        for name in {tool_id, *executables}:
            owners.setdefault(name, set()).add(tool_id)
    canonical_by_executable = {name: builtin.resolve(name) or "" for name in builtin.executables()}
    executables_by_canonical = {tool_id: builtin.executables_for(tool_id) for tool_id in builtin.canonical_ids()}
    origin_by_canonical = {tool_id: ORIGIN_BUILTIN for tool_id in builtin.canonical_ids()}
    for tool_id, executables in candidates:
        clashes = sorted(name for name in {tool_id, *executables} if len(owners[name]) > 1)
        if clashes:
            others = sorted({other for name in clashes for other in owners[name]} - {tool_id})
            records.append(UserToolRecord(
                tool_id=tool_id, executables=executables, status=STATUS_REJECTED, reason=DUPLICATE,
                message=f"name(s) {', '.join(clashes)} also used by user tool(s) {', '.join(others)}"))
            continue
        for name in executables:
            canonical_by_executable[name] = tool_id
        executables_by_canonical[tool_id] = executables
        origin_by_canonical[tool_id] = ORIGIN_USER
        records.append(UserToolRecord(tool_id=tool_id, executables=executables, status=STATUS_ACCEPTED))
    catalog = ToolCatalog(
        _canonical_by_executable=canonical_by_executable,
        _executables_by_canonical=executables_by_canonical,
        _origin_by_canonical=origin_by_canonical,
    )
    return catalog, tuple(records)


def builtin_only(builtin: ToolCatalog) -> ToolCatalog:
    """只含出貨項目、origin 全部是 builtin 的 effective catalog。"""
    return compose_effective_catalog(builtin, [], ReservedNames(frozenset(), frozenset(), ()))[0]


def catalog_fingerprint(catalog: ToolCatalog) -> str:
    """effective catalog 內容的指紋，用來比對 CLI 與 daemon 是否看到同一份設定。"""
    items = [[tool_id, catalog.origin_of(tool_id), list(catalog.executables_for(tool_id))]
             for tool_id in catalog.canonical_ids()]
    encoded = json.dumps(items, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def render_user_catalog(entries: list[tuple[str, tuple[str, ...]]]) -> str:
    """`terminux-tool` 寫出的標準格式；名稱已通過 `TOOL_NAME_RE`，不需要引號。"""
    lines = [
        "# Managed by terminux-tool (register / unregister).",
        f"schema_version: {USER_CATALOG_SCHEMA}",
    ]
    if not entries:
        lines.append("tools: {}")
    else:
        lines.append("tools:")
        for tool_id, executables in entries:
            lines.append(f"  {tool_id}:")
            lines.append(f"    executables: [{', '.join(executables)}]")
    return "\n".join(lines) + "\n"


# ── 路徑與安全讀取 ─────────────────────────────────────────────────────


def resolve_user_home() -> Path:
    """與 user package root 相同的 home 解析（不讀 `$HOME`）。"""
    uid = agent_socket.current_uid()
    try:
        home = agent_socket.home_for_uid(uid)
        if agent_socket.HOME_OVERRIDE_ENV not in os.environ:
            agent_socket._check_home(home, uid=uid, expected=str(home))
    except KeyError as exc:
        raise UserToolError(ROOT_INVALID, f"no home directory for uid {uid}: {exc}") from None
    except agent_socket.AgentSocketError as exc:
        raise UserToolError(ROOT_INVALID, str(exc)) from None
    return home


def resolve_user_tools_dir(*, create: bool = False) -> Path:
    """解析 `<home>/.terminux/tools`（0700、本 UID、非 symlink）。只有登記流程會 `create`。"""
    uid = agent_socket.current_uid()
    terminux_dir = resolve_user_home() / agent_socket.AGENT_SOCKET_DIR_NAME
    tools_dir = terminux_dir / USER_TOOLS_DIR_NAME
    for directory, forbidden in ((terminux_dir, 0o022), (tools_dir, 0o077)):
        if create and not os.path.lexists(directory):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise UserToolError(ROOT_INVALID, f"cannot create {directory}: {exc}") from None
        try:
            agent_socket._check_directory(
                directory, uid=uid, expected=str(tools_dir),
                missing_hint="no user tool has been registered yet", forbidden_mode=forbidden)
        except agent_socket.AgentSocketError as exc:
            code = ROOT_MISSING if exc.code == agent_socket.RUNTIME_DIR_MISSING else ROOT_INVALID
            raise UserToolError(code, str(exc)) from None
    return tools_dir


def read_user_catalog_bytes(path: Path) -> bytes | None:
    """以 O_NOFOLLOW 讀取 overlay；不存在回傳 None。

    必須是本 UID 的一般檔案、group／other 不可寫、大小有上限。
    """
    try:
        # O_NONBLOCK：路徑若被換成 FIFO，open 不會卡住；隨後的 S_ISREG 檢查會拒絕它。
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UserToolError(CATALOG_UNSAFE, f"cannot open {path}: {exc}") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise UserToolError(CATALOG_UNSAFE, f"{path} is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise UserToolError(
                CATALOG_UNSAFE, f"{path} is owned by uid {metadata.st_uid}, expected {os.geteuid()}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise UserToolError(
                CATALOG_UNSAFE,
                f"{path} has mode {stat.S_IMODE(metadata.st_mode):04o}; "
                "it must not be writable by group or other")
        if metadata.st_size > MAX_USER_CATALOG_BYTES:
            raise UserToolError(
                CATALOG_INVALID, f"{path} is larger than {MAX_USER_CATALOG_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_USER_CATALOG_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_USER_CATALOG_BYTES:
            raise UserToolError(
                CATALOG_INVALID, f"{path} is larger than {MAX_USER_CATALOG_BYTES} bytes")
        return data
    finally:
        os.close(fd)


def bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class UserCatalogView:
    """磁碟上 overlay 的一次讀取結果（CLI 與 daemon 共用）。"""

    state: str  # ABSENT | LOADED | INVALID_BUILTIN_ONLY | BUILTIN_UNAVAILABLE
    path: Path | None
    catalog: ToolCatalog | None
    records: tuple[UserToolRecord, ...] = ()
    digest: str | None = None
    error: dict[str, Any] | None = None
    raw_entries: tuple[tuple[Any, Any], ...] = ()


def read_user_catalog_view(builtin: ToolCatalog | None, *, reserved: ReservedNames | None = None) -> UserCatalogView:
    """讀取並合併 overlay。overlay 任何問題都不會讓內建工具失效。"""
    if builtin is None:
        return UserCatalogView(
            state=STATE_BUILTIN_UNAVAILABLE, path=None, catalog=None,
            error={"code": BUILTIN_UNAVAILABLE,
                   "message": "the shipped tool catalog could not be loaded; dispatch stays fail-closed",
                   "details": []})
    path: Path | None = None
    try:
        # 尚未登記過（目錄不存在）時仍回報預期位置。
        path = (resolve_user_home() / agent_socket.AGENT_SOCKET_DIR_NAME / USER_TOOLS_DIR_NAME
                / USER_CATALOG_FILE_NAME)
        resolve_user_tools_dir(create=False)
        data = read_user_catalog_bytes(path)
        if data is None:
            return UserCatalogView(state=STATE_ABSENT, path=path, catalog=builtin_only(builtin))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise UserToolError(CATALOG_INVALID, f"{path} is not UTF-8 text") from None
        raw = parse_user_catalog(text, source=str(path))
        catalog, records = compose_effective_catalog(
            builtin, raw, reserved if reserved is not None else load_reserved_names())
        return UserCatalogView(
            state=STATE_LOADED, path=path, catalog=catalog, records=records,
            digest=bytes_digest(data), raw_entries=tuple(raw))
    except UserToolError as exc:
        if exc.code == ROOT_MISSING:
            return UserCatalogView(state=STATE_ABSENT, path=path, catalog=builtin_only(builtin))
        return UserCatalogView(
            state=STATE_INVALID_BUILTIN_ONLY, path=path, catalog=builtin_only(builtin),
            error=exc.to_dict())
    except Exception as exc:
        # overlay 的任何非預期問題都不得影響內建工具。
        return UserCatalogView(
            state=STATE_INVALID_BUILTIN_ONLY, path=path, catalog=builtin_only(builtin),
            error={"code": CATALOG_INVALID, "message": f"{type(exc).__name__}: {exc}", "details": []})


# ── Daemon activation 的工具 scope ─────────────────────────────────────


@dataclass(frozen=True)
class UserToolScope:
    """一次 extensions activation 中的 effective tool catalog（不可變）。"""

    state: str
    catalog: ToolCatalog | None
    path: str | None = None
    digest: str | None = None
    records: tuple[UserToolRecord, ...] = ()
    error: dict[str, Any] | None = None
    fingerprint: str | None = field(default=None)

    def user_tools(self) -> list[tuple[str, tuple[str, ...]]]:
        return [(record.tool_id, record.executables) for record in self.records
                if record.status == STATUS_ACCEPTED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "path": self.path,
            "digest": self.digest,
            "fingerprint": self.fingerprint,
            "error": self.error,
            "builtin_catalog_loaded": self.catalog is not None,
            "tools": [{"tool_id": tool_id, "executables": list(executables), "origin": ORIGIN_USER}
                      for tool_id, executables in self.user_tools()],
            "records": [record.to_dict() for record in self.records],
        }


def build_user_tool_scope(builtin: ToolCatalog | None, previous: UserToolScope | None) -> UserToolScope:
    """建出下一份工具 scope。

    overlay 整份無效、而上一份 activation 已成功載入過 overlay 時拋
    `UserToolError`，讓呼叫端拒絕整次 reload 並保留上一份 activation；
    沒有可保留的 overlay 時改用只含內建工具的 catalog，並附上診斷。
    """
    view = read_user_catalog_view(builtin)
    if (view.state == STATE_INVALID_BUILTIN_ONLY and previous is not None
            and previous.state == STATE_LOADED and view.error is not None):
        raise UserToolError(
            view.error["code"], view.error["message"], details=tuple(view.error.get("details", ())))
    return UserToolScope(
        state=view.state, catalog=view.catalog,
        path=str(view.path) if view.path is not None else None,
        digest=view.digest, records=view.records, error=view.error,
        fingerprint=catalog_fingerprint(view.catalog) if view.catalog is not None else None)


# ── Wrapper readiness（只讀檢查） ──────────────────────────────────────


def wrapper_bin_dir(home: Path) -> Path:
    return home / agent_socket.AGENT_SOCKET_DIR_NAME / "bin"


def link_state(bin_dir: Path, executable: str) -> str:
    """`LINKED`（本 UID 擁有、指向 `_terminux_wrapper` 的 symlink）、`MISSING` 或 `OCCUPIED`。"""
    target = bin_dir / executable
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return "MISSING"
    if (stat.S_ISLNK(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and os.readlink(target) == WRAPPER_TARGET):
        return "LINKED"
    return "OCCUPIED"


def _file_id(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.stat()
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino


def _accessible(path: Path, mode: int) -> bool:
    try:
        if os.access in os.supports_effective_ids:
            return os.access(path, mode, effective_ids=True)
        return os.access(path, mode)
    except OSError:
        return False


def wrapper_readiness(executable: str, *, home: Path, path_env: str | None = None,
                      env_home: str | None = None) -> dict[str, Any]:
    """以呼叫端的 PATH 檢查這個 executable 會不會經過 Terminux wrapper。

    規則與 `terminux_find_real_tool` 相同：以 device:inode 判斷是不是 wrapper
    本身。這是設定層級的檢查，不執行任何程式。
    """
    bin_dir = wrapper_bin_dir(home)
    wrapper = bin_dir / WRAPPER_TARGET
    wrapper_lib = bin_dir / "_wrapper_lib.sh"
    result: dict[str, Any] = {
        "executable": executable, "link": link_state(bin_dir, executable),
        "first_on_path": None, "real_executable": None, "ready": False, "reason": None,
    }
    wrapper_id = _file_id(wrapper)
    env_home_value = os.environ.get("HOME") if env_home is None else env_home
    if (wrapper_id is None or not wrapper.is_file() or wrapper.is_symlink()
            or not wrapper_lib.is_file()):
        result["reason"] = WRAPPER_NOT_INSTALLED
        return result
    # 與實際載入方式一致：bash 執行 `_terminux_wrapper`（需可讀＋可執行），再 `source`
    # `_wrapper_lib.sh`（需可讀）。以 effective UID 判斷。
    if not (_accessible(wrapper, os.R_OK | os.X_OK) and _accessible(wrapper_lib, os.R_OK)):
        result["reason"] = WRAPPER_NOT_READABLE
        return result
    if result["link"] == "MISSING":
        result["reason"] = WRAPPER_LINK_MISSING
        return result
    if result["link"] == "OCCUPIED":
        result["reason"] = WRAPPER_LINK_OCCUPIED
        return result
    for directory in (path_env if path_env is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / executable
        if not os.access(candidate, os.X_OK) or candidate.is_dir():
            continue
        candidate_id = _file_id(candidate)
        if candidate_id is None:
            continue
        if result["first_on_path"] is None:
            result["first_on_path"] = str(candidate)
            if candidate_id != wrapper_id:
                result["reason"] = WRAPPER_NOT_FIRST_ON_PATH
                return result
            continue
        if candidate_id != wrapper_id:
            result["real_executable"] = str(candidate)
            break
    if result["first_on_path"] is None:
        result["reason"] = WRAPPER_NOT_FIRST_ON_PATH
        return result
    if result["real_executable"] is None:
        result["reason"] = REAL_EXECUTABLE_NOT_FOUND
        return result
    # wrapper 以 `$HOME/.terminux/bin/terminux-notify` 送事件；$HOME 必須是同一個 home。
    if not env_home_value or _file_id(Path(env_home_value)) != _file_id(home):
        result["reason"] = HOME_MISMATCH
        return result
    if not os.access(bin_dir / "terminux-notify", os.X_OK):
        result["reason"] = NOTIFY_HELPER_MISSING
        return result
    result["ready"] = True
    result["reason"] = WRAPPER_READY
    return result
