# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""來源 snapshot、input manifest 與唯一允許的 diff。

這是 Package 的**正式 snapshot preparer**：CLI 與 host 都呼叫這裡，不人工
編造 manifest。它只做隔離資料寫入，**不執行 Make／Tcl／EDA**，也不改原 repo。

三個責任：

1. `prepare_source_snapshot()`：把 source worktree 封存成固定副本並產生
   `InputManifest v1`。先掃描、複製、再複掃比對——期間變動即拒絕，不混合
   不同時點的檔案。
2. `verify_inventory()`：worker 在每次 EDA 派送前／完成後的完整逐檔比對
   （包含 link target 與非 config 檔案）。慢，因此永遠在短鎖之外執行。
3. `compute_candidate_config()` / `materialize_candidate()`：唯一允許的
   工程 diff。

**唯一允許 diff**（不可放寬）：原行

    export CORE_UTILIZATION ?= 55

只把數字 token `55` 換成 `35`。`export`、`?=`、空白、換行、註解與其餘所有
bytes 一律保留；**不改成 `=`／`:=`／`override`，不附加第二條定義**。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

MANIFEST_SCHEMA_VERSION = 1

#: 固定排除範圍：版本控制內部與既有輸出樹。其餘一律納入（含 `tools/`）。
#:
#: 兩類語義不同，與 rsync 的 pattern 規則一致：
#:
#: - `.git`（不含斜線）＝**任意深度**。`tools/` 底下的 submodule 各自有
#:   `.git`，它們同樣是版本控制內部，不是工程檔。
#: - 含斜線者＝**錨定在 source root**，只排掉頂層那一個既有輸出樹。
SNAPSHOT_EXCLUDE_ANY_DEPTH: tuple[str, ...] = (".git",)
SNAPSHOT_EXCLUDE_ROOTED: tuple[str, ...] = (
    "flow/logs", "flow/reports", "flow/results", "flow/objects",
)
SNAPSHOT_EXCLUDES: tuple[str, ...] = (
    SNAPSHOT_EXCLUDE_ANY_DEPTH + SNAPSHOT_EXCLUDE_ROOTED
)

KIND_FILE = "file"
KIND_LINK = "link"
KIND_DIR = "dir"

_READ_CHUNK = 1 << 20


class InputPreparationError(RuntimeError):
    """snapshot 準備或驗證失敗。一律 fail-closed。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


SOURCE_CHANGED_DURING_SNAPSHOT = "SOURCE_CHANGED_DURING_SNAPSHOT"
SYMLINK_ESCAPES_SOURCE = "SYMLINK_ESCAPES_SOURCE"
INVENTORY_DRIFT = "INPUT_INVENTORY_DRIFT"
MANIFEST_INVALID = "INPUT_MANIFEST_INVALID"
CONFIG_NOT_AS_EXPECTED = "DESIGN_CONFIG_NOT_AS_EXPECTED"


# ── Inventory ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InputEntry:
    """一個輸入項目的身分。

    `sha256` 只對一般檔案有值；symlink 記 `link_target`（**不跟隨**它去
    hash 目標內容——link 本身就是輸入的一部分）。
    """

    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None
    link_target: str | None = None
    mode: int | None = None

    def identity_tuple(self) -> tuple[Any, ...]:
        return (self.path, self.kind, self.size, self.sha256, self.link_target,
                self.mode)

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "kind": self.kind}
        if self.size is not None:
            payload["size"] = self.size
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        if self.link_target is not None:
            payload["link_target"] = self.link_target
        if self.mode is not None:
            payload["mode"] = self.mode
        return payload

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "InputEntry":
        return cls(
            path=str(payload["path"]), kind=str(payload["kind"]),
            size=payload.get("size"), sha256=payload.get("sha256"),
            link_target=payload.get("link_target"), mode=payload.get("mode"),
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(relative: str) -> bool:
    """套用固定排除範圍。語義必須與 `_copy_tree()` 用的 rsync pattern 一致，
    否則掃描與副本會對不起來（複掃比對就會失敗）。"""
    parts = relative.split("/")
    if any(part in SNAPSHOT_EXCLUDE_ANY_DEPTH for part in parts):
        return True
    return any(relative == ex or relative.startswith(ex + "/")
               for ex in SNAPSHOT_EXCLUDE_ROOTED)


def _walk(root: Path) -> Iterator[tuple[str, Path]]:
    """深度優先走訪，套用固定排除範圍。回傳 (relative_posix, absolute)。"""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            raise InputPreparationError(
                INVENTORY_DRIFT, f"source directory unreadable: {current}: {exc}") from exc
        for entry in entries:
            absolute = Path(entry.path)
            relative = absolute.relative_to(root).as_posix()
            if _excluded(relative):
                continue
            yield relative, absolute
            if entry.is_dir(follow_symlinks=False):
                stack.append(absolute)


def scan_inventory(root: Path, *, hash_files: bool = True) -> tuple[InputEntry, ...]:
    """掃描一棵樹的完整 inventory。

    symlink 一律檢查是否逃出 `root`——**未知外部來源不能無條件跟隨**
   。逃出即拒絕整份 snapshot，不自行決定要不要納入。
    """
    root = root.resolve()
    entries: list[InputEntry] = []
    for relative, absolute in _walk(root):
        if absolute.is_symlink():
            target = os.readlink(absolute)
            resolved = Path(os.path.realpath(absolute))
            if os.path.isabs(target) or not _within(resolved, root):
                raise InputPreparationError(
                    SYMLINK_ESCAPES_SOURCE,
                    f"symlink escapes the fixed source boundary: "
                    f"{relative} -> {target}")
            entries.append(InputEntry(path=relative, kind=KIND_LINK,
                                      link_target=target))
            continue
        if absolute.is_dir():
            entries.append(InputEntry(path=relative, kind=KIND_DIR))
            continue
        try:
            stat = absolute.stat()
        except OSError as exc:
            raise InputPreparationError(
                INVENTORY_DRIFT, f"input disappeared while scanning: {relative}") from exc
        entries.append(InputEntry(
            path=relative, kind=KIND_FILE, size=stat.st_size,
            sha256=_sha256_file(absolute) if hash_files else None,
            mode=stat.st_mode & 0o777,
        ))
    entries.sort(key=lambda e: e.path)
    return tuple(entries)


def _within(inner: Path, outer: Path) -> bool:
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


def inventory_tree_hash(entries: tuple[InputEntry, ...]) -> str:
    """整棵樹的身分。逐項 canonical 編碼後 hash。

    這讓「只換掉一個檔案」的預期 hash 可以在**不重讀所有檔案**的情況下
    推導出來（create 的要求）。
    """
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(json.dumps(entry.to_json_dict(), sort_keys=True,
                                 ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# ── Manifest ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InputManifest:
    """`InputManifest v1`。

    `effective_settings` 在 snapshot 階段是空的——**預先 snapshot 與 runtime
    有效設定分開標示**，不把後者說成核准前已觀察的事實。
    """

    schema_version: int
    slot: str
    source_root: str
    snapshot_root: str
    design_config: str
    design_id: str
    platform_id: str
    #: snapshot 準備時宣告的 image；host-native Profile 沒有 image，記為 `None`。
    docker_image_id: str | None
    source_revision: str | None
    source_dirty: bool | None
    entries: tuple[InputEntry, ...]
    tree_hash: str
    config_sha256: str

    def entry(self, relative_path: str) -> InputEntry | None:
        return next((e for e in self.entries if e.path == relative_path), None)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "slot": self.slot,
            "source_root": self.source_root,
            "snapshot_root": self.snapshot_root,
            "design_config": self.design_config,
            "design_id": self.design_id,
            "platform_id": self.platform_id,
            "docker_image_id": self.docker_image_id,
            "source_revision": self.source_revision,
            "source_dirty": self.source_dirty,
            "tree_hash": self.tree_hash,
            "config_sha256": self.config_sha256,
            "entry_count": len(self.entries),
            "entries": [e.to_json_dict() for e in self.entries],
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "InputManifest":
        try:
            entries = tuple(InputEntry.from_json_dict(e) for e in payload["entries"])
            manifest = cls(
                schema_version=int(payload["schema_version"]),
                slot=str(payload["slot"]),
                source_root=str(payload["source_root"]),
                snapshot_root=str(payload["snapshot_root"]),
                design_config=str(payload["design_config"]),
                design_id=str(payload["design_id"]),
                platform_id=str(payload["platform_id"]),
                docker_image_id=(None if payload["docker_image_id"] is None
                                 else str(payload["docker_image_id"])),
                source_revision=payload.get("source_revision"),
                source_dirty=payload.get("source_dirty"),
                entries=entries,
                tree_hash=str(payload["tree_hash"]),
                config_sha256=str(payload["config_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputPreparationError(
                MANIFEST_INVALID, f"input manifest is malformed: {exc}") from exc
        if int(payload.get("entry_count", len(entries))) != len(entries):
            raise InputPreparationError(
                MANIFEST_INVALID, "input manifest entry_count does not match entries")
        if inventory_tree_hash(entries) != manifest.tree_hash:
            raise InputPreparationError(
                MANIFEST_INVALID, "input manifest tree_hash does not recompute")
        return manifest


def manifest_bytes(manifest: InputManifest) -> bytes:
    return json.dumps(manifest.to_json_dict(), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def load_manifest(path: str | Path, *, expected_sha256: str | None = None) -> InputManifest:
    """讀回 manifest 並驗**原始 bytes** hash。

    `expected_sha256` 來自 profile；不符即拒絕——改寫 manifest 之後不能
    假稱原始證據未變。
    """
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise InputPreparationError(
            MANIFEST_INVALID, f"input manifest unreadable: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and actual != expected_sha256:
        raise InputPreparationError(
            MANIFEST_INVALID,
            "input manifest bytes do not match the hash declared by the profile")
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise InputPreparationError(
            MANIFEST_INVALID, f"input manifest is not valid JSON: {exc}") from exc
    return InputManifest.from_json_dict(document)


# ── Snapshot preparer ────────────────────────────────────────────────


def _source_revision(root: Path) -> tuple[str | None, bool | None]:
    """記錄來源 revision 與 dirty 狀態。取不到就誠實回 None，不猜。"""
    try:
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False)
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if rev.returncode != 0:
        return None, None
    dirty = None if status.returncode != 0 else bool(status.stdout.strip())
    return rev.stdout.strip() or None, dirty


def _copy_tree(source: Path, destination: Path) -> str:
    """複製 source tree。

    優先用檔案系統的 reflink／copy-on-write（`cp --reflink=auto -a`）；
    不支援就退回一般複製。兩者都**不**在原 repo 與副本之間建立共同可寫的
    hardlink——候選的修改不會回寫其他副本。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise InputPreparationError(
            INVENTORY_DRIFT, f"snapshot destination already exists: {destination}")
    excluded_args: list[str] = []
    for ex in SNAPSHOT_EXCLUDE_ANY_DEPTH:
        excluded_args.append(f"--exclude={ex}")
    for ex in SNAPSHOT_EXCLUDE_ROOTED:
        # 前導斜線讓 rsync 把 pattern 錨定在 transfer root。
        excluded_args.append(f"--exclude=/{ex}")
    # `cp` 沒有 --exclude；用 rsync 保持排除範圍與 symlink 語義一致。
    if shutil.which("rsync"):
        result = subprocess.run(
            ["rsync", "-a", "--links", "--no-inc-recursive", *excluded_args,
             str(source) + "/", str(destination) + "/"],
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise InputPreparationError(
                INVENTORY_DRIFT,
                f"source snapshot copy failed: {result.stderr.strip()[:400]}")
        return "rsync"
    # 沒有 rsync 就用 Python 複製，語義相同但較慢。
    destination.mkdir(parents=True)
    for relative, absolute in _walk(source.resolve()):
        target = destination / relative
        if absolute.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(absolute), target)
        elif absolute.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(absolute, target, follow_symlinks=False)
    return "python"


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: InputManifest
    copy_method: str


def prepare_source_snapshot(
    *,
    source_root: str | Path,
    snapshot_root: str | Path,
    manifest_path: str | Path,
    design_config: str,
    design_id: str,
    platform_id: str,
    docker_image_id: str | None,
    slot: str = "baseline",
    pinned_manifest: InputManifest | None = None,
) -> SnapshotResult:
    """封存一份固定來源副本並產生 manifest。

    順序固定：**掃描 → 複製 → 複掃來源 → 比對**。期間來源變動即拒絕，
    不混合不同時點的檔案。這裡不執行 Make／Tcl／EDA。

    RC-4：`pinned_manifest` 是 Profile 已固定的來源 manifest。給了就要求副本
    inventory 與它完全相同；來源本身不是 git worktree（例如唯讀 snapshot）時，
    沿用 pinned manifest 保存的 revision／dirty，而不是記成未知。來源是 git
    worktree 且 revision 不同則拒絕。
    """
    source = Path(source_root).resolve()
    destination = Path(snapshot_root)
    if not source.is_dir():
        raise InputPreparationError(
            INVENTORY_DRIFT, f"source_root is not a directory: {source}")

    before = scan_inventory(source)
    copy_method = _copy_tree(source, destination)
    after = scan_inventory(source)
    if inventory_tree_hash(before) != inventory_tree_hash(after):
        raise InputPreparationError(
            SOURCE_CHANGED_DURING_SNAPSHOT,
            "the source tree changed while the snapshot was being taken; "
            "no snapshot may mix files from different points in time")

    # 副本本身也要有自己的 inventory——它才是 execution 實際讀的東西。
    copied = scan_inventory(destination.resolve())
    if inventory_tree_hash(copied) != inventory_tree_hash(before):
        raise InputPreparationError(
            INVENTORY_DRIFT,
            "the snapshot copy does not match the source inventory it was taken from")

    config_entry = next((e for e in copied if e.path == design_config), None)
    if config_entry is None or config_entry.sha256 is None:
        raise InputPreparationError(
            CONFIG_NOT_AS_EXPECTED,
            f"design_config not found in the snapshot: {design_config}")

    revision, dirty = _source_revision(source)
    if pinned_manifest is not None:
        if inventory_tree_hash(copied) != pinned_manifest.tree_hash:
            raise InputPreparationError(
                INVENTORY_DRIFT,
                "the source tree does not match the pinned Profile input manifest")
        if revision is None:
            revision, dirty = (pinned_manifest.source_revision,
                               pinned_manifest.source_dirty)
        elif (pinned_manifest.source_revision is not None
              and (revision, dirty) != (pinned_manifest.source_revision,
                                        pinned_manifest.source_dirty)):
            raise InputPreparationError(
                INVENTORY_DRIFT,
                "the source revision does not match the pinned Profile input manifest")
    manifest = InputManifest(
        schema_version=MANIFEST_SCHEMA_VERSION, slot=slot,
        source_root=str(source), snapshot_root=str(destination.resolve()),
        design_config=design_config, design_id=design_id, platform_id=platform_id,
        docker_image_id=docker_image_id, source_revision=revision,
        source_dirty=dirty, entries=copied,
        tree_hash=inventory_tree_hash(copied), config_sha256=config_entry.sha256,
    )
    raw = manifest_bytes(manifest)
    target = Path(manifest_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return SnapshotResult(
        snapshot_root=destination.resolve(), manifest_path=target,
        manifest_sha256=hashlib.sha256(raw).hexdigest(), manifest=manifest,
        copy_method=copy_method,
    )


# ── 完整逐檔驗證（worker 用，永遠在短鎖之外）─────────────────────────


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    missing: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.ok:
            return f"verified {self.checked} entries"
        parts = []
        if self.missing:
            parts.append(f"missing={list(self.missing[:5])}")
        if self.changed:
            parts.append(f"changed={list(self.changed[:5])}")
        if self.unexpected:
            parts.append(f"unexpected={list(self.unexpected[:5])}")
        return "input inventory drift: " + ", ".join(parts)


def verify_inventory(manifest: InputManifest, root: str | Path) -> VerifyResult:
    """完整逐檔比對實際 inventory 與 manifest。

    包含 link target 與非 config 檔案。這是慢速工作，呼叫端必須在
    短鎖之外執行，並在 claim 前重驗授權／取消／期限與**本次驗證結果的關聯**。
    """
    actual = {e.path: e for e in scan_inventory(Path(root).resolve())}
    expected = {e.path: e for e in manifest.entries}
    missing = tuple(sorted(set(expected) - set(actual)))
    unexpected = tuple(sorted(set(actual) - set(expected)))
    changed = tuple(sorted(
        path for path in set(expected) & set(actual)
        if expected[path].identity_tuple() != actual[path].identity_tuple()
    ))
    return VerifyResult(
        ok=not (missing or unexpected or changed),
        checked=len(expected), missing=missing, changed=changed,
        unexpected=unexpected,
    )


__all__ = [
    "CONFIG_NOT_AS_EXPECTED",
    "INVENTORY_DRIFT",
    "KIND_DIR",
    "KIND_FILE",
    "KIND_LINK",
    "MANIFEST_INVALID",
    "MANIFEST_SCHEMA_VERSION",
    "SNAPSHOT_EXCLUDES",
    "SNAPSHOT_EXCLUDE_ANY_DEPTH",
    "SNAPSHOT_EXCLUDE_ROOTED",
    "SOURCE_CHANGED_DURING_SNAPSHOT",
    "SYMLINK_ESCAPES_SOURCE",
    "InputEntry",
    "InputManifest",
    "InputPreparationError",
    "SnapshotResult",
    "VerifyResult",
    "inventory_tree_hash",
    "load_manifest",
    "manifest_bytes",
    "prepare_source_snapshot",
    "scan_inventory",
    "verify_inventory",
]


# ── 唯一允許的工程 diff ─────────────────────────────────────────

CANDIDATE_DIFF_REJECTED = "CANDIDATE_DIFF_REJECTED"

#: 只匹配「一行之內」的固定形式，且把數字 token 單獨切出來。
#:
#: 刻意逐段捕捉：`export`、空白、變數名、空白、指派運算子、空白、數字、
#: 行尾殘餘。替換時只換 group `value`，其餘 group 原樣寫回——因此
#: `export`、`?=`、原空白與註解都不會被改動。
_ASSIGNMENT_RE_TEMPLATE = (
    r"(?P<prefix>^[ \t]*(?:export[ \t]+)?)"
    r"(?P<name>{name})"
    r"(?P<gap1>[ \t]*)"
    r"(?P<op>\?=|:=|::=|\+=|=)"
    r"(?P<gap2>[ \t]*)"
    # Package validator 接受非負十進位 token；工程層必須使用相同表示法，否則
    # 已驗證的 `0.10` proposal 會在 candidate binding 時被錯誤拒絕。
    r"(?P<value>[0-9]+(?:\.[0-9]+)?)"
    r"(?P<suffix>[ \t]*(?:#[^\n]*)?)$"
)


@dataclass(frozen=True)
class ConfigAssignment:
    """config 內某個變數的一次指派。"""

    line_number: int
    operator: str
    value: str
    raw_line: str


def find_assignments(config_text: str, name: str) -> tuple[ConfigAssignment, ...]:
    """找出 config 內對 `name` 的所有數值指派。

    **不解析 Make 語義**——它只認直接的字面指派。任何條件展開、include
    來源或其他寫法都不會被這裡認出來，因此下面的唯一性檢查會失敗並拒絕，
    而不是猜一個。
    """
    pattern = re.compile(_ASSIGNMENT_RE_TEMPLATE.format(name=re.escape(name)),
                         re.MULTILINE)
    found: list[ConfigAssignment] = []
    for match in pattern.finditer(config_text):
        found.append(ConfigAssignment(
            line_number=config_text.count("\n", 0, match.start()) + 1,
            operator=match.group("op"), value=match.group("value"),
            raw_line=match.group(0),
        ))
    return tuple(found)


def _mentions_name_elsewhere(config_text: str, name: str,
                             allowed_line: int) -> tuple[str, ...]:
    """找出同一份 config 內其他提到這個變數的行。

    有第二處提及就不能證明「唯一定義」，一律報出來讓呼叫端阻止候選執行
    （不自行選另一個修改方法／參數）。
    """
    others: list[str] = []
    for index, line in enumerate(config_text.splitlines(), start=1):
        if index == allowed_line:
            continue
        stripped = line.split("#", 1)[0]
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", stripped):
            others.append(f"line {index}: {line.strip()[:120]}")
    return tuple(others)


def compute_candidate_config(
    config_bytes: bytes, *, name: str, baseline_value: str, candidate_value: str,
) -> bytes:
    """由 baseline config bytes 算出唯一允許的 candidate config bytes。

    規則（不可放寬）：

    - 必須**恰好一處**對 `name` 的數值指派，且其值等於 `baseline_value`。
    - config 內不得有第二處提及這個變數。
    - **只**替換那個數字 token；`export`、指派運算子、空白、換行、註解與
      其餘所有 bytes 原樣保留。
    - 不改成 `=`／`:=`／`override`，不附加第二條定義。

    任一條不成立就拋出——由呼叫端阻止候選執行，不自行改用別的寫法。
    """
    try:
        text = config_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED, f"design config is not UTF-8: {exc}") from exc

    assignments = find_assignments(text, name)
    if len(assignments) != 1:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED,
            f"expected exactly one numeric assignment of {name}, found "
            f"{len(assignments)}: "
            f"{[a.raw_line.strip() for a in assignments]}")
    assignment = assignments[0]
    if assignment.value != baseline_value:
        raise InputPreparationError(
            CONFIG_NOT_AS_EXPECTED,
            f"{name} is {assignment.value!r} in the design config, but the "
            f"profile declares baseline {baseline_value!r}")

    others = _mentions_name_elsewhere(text, name, assignment.line_number)
    if others:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED,
            f"{name} is mentioned more than once in the design config; the "
            f"single-definition requirement cannot be proven: {list(others)}")

    pattern = re.compile(_ASSIGNMENT_RE_TEMPLATE.format(name=re.escape(name)),
                         re.MULTILINE)

    def _swap(match: re.Match[str]) -> str:
        return (match.group("prefix") + match.group("name") + match.group("gap1")
                + match.group("op") + match.group("gap2") + candidate_value
                + match.group("suffix"))

    new_text, count = pattern.subn(_swap, text)
    if count != 1:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED,
            f"the candidate rewrite touched {count} lines; exactly one was required")
    new_bytes = new_text.encode("utf-8")

    # 事後複驗：除了那一個數字 token，其餘 bytes 必須完全相同。
    _assert_single_token_diff(config_bytes, new_bytes, baseline_value,
                              candidate_value, name)
    return new_bytes


def _assert_single_token_diff(
    original: bytes, updated: bytes, baseline_value: str, candidate_value: str,
    name: str,
) -> None:
    """獨立於替換邏輯的複驗：兩份 bytes 只能差在一個數字 token。"""
    original_lines = original.split(b"\n")
    updated_lines = updated.split(b"\n")
    if len(original_lines) != len(updated_lines):
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED, "the candidate config changed the line count")
    differing = [i for i, (a, b) in enumerate(zip(original_lines, updated_lines))
                 if a != b]
    if len(differing) != 1:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED,
            f"the candidate config differs on {len(differing)} lines; exactly one "
            "was required")
    index = differing[0]
    before, after = original_lines[index], updated_lines[index]
    expected = before.replace(baseline_value.encode(), candidate_value.encode(), 1)
    if after != expected:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED,
            "the candidate line differs by more than the numeric token")
    # 指派運算子必須原封不動。
    for operator in (b"?=", b":=", b"::=", b"+=", b"="):
        if (operator in before) != (operator in after):
            raise InputPreparationError(
                CANDIDATE_DIFF_REJECTED,
                f"the assignment operator changed for {name}")


def expected_candidate_tree_hash(
    manifest: InputManifest, *, candidate_config_sha256: str,
    candidate_config_size: int,
) -> str:
    """由已驗證的 manifest inventory 推導候選樹的預期 hash。

    只替換 config 那一個 entry 的 size/hash，**不需要讀完所有 source 檔案**
    （create 不重算 3.2 GB）。
    """
    swapped: list[InputEntry] = []
    replaced = 0
    for entry in manifest.entries:
        if entry.path == manifest.design_config:
            swapped.append(InputEntry(
                path=entry.path, kind=entry.kind, size=candidate_config_size,
                sha256=candidate_config_sha256, link_target=entry.link_target,
                mode=entry.mode))
            replaced += 1
        else:
            swapped.append(entry)
    if replaced != 1:
        raise InputPreparationError(
            MANIFEST_INVALID,
            "the manifest does not contain exactly one design_config entry")
    return inventory_tree_hash(tuple(swapped))


@dataclass(frozen=True)
class VariantResult:
    candidate_root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: InputManifest
    config_sha256: str
    tree_hash: str
    copy_method: str


def materialize_candidate(
    *,
    baseline_manifest: InputManifest,
    baseline_root: str | Path,
    candidate_root: str | Path,
    manifest_path: str | Path,
    name: str,
    baseline_value: str,
    candidate_value: str,
    expected_tree_hash: str,
) -> VariantResult:
    """建立候選 input snapshot 並套用唯一允許的 diff（s3）。

    實際 tree hash 必須等於 create 時綁進 task digest 的預期值；不符即拒絕，
    不接受「差不多」。
    """
    source = Path(baseline_root).resolve()
    destination = Path(candidate_root)
    copy_method = _copy_tree(source, destination)

    config_path = destination / baseline_manifest.design_config
    original = config_path.read_bytes()
    updated = compute_candidate_config(
        original, name=name, baseline_value=baseline_value,
        candidate_value=candidate_value)
    config_path.write_bytes(updated)

    entries = scan_inventory(destination.resolve())
    tree_hash = inventory_tree_hash(entries)
    if tree_hash != expected_tree_hash:
        raise InputPreparationError(
            CANDIDATE_DIFF_REJECTED,
            "the materialized candidate tree hash does not match the value bound "
            "into the task digest at create time")

    config_sha = hashlib.sha256(updated).hexdigest()
    manifest = InputManifest(
        schema_version=MANIFEST_SCHEMA_VERSION, slot="candidate",
        source_root=str(source), snapshot_root=str(destination.resolve()),
        design_config=baseline_manifest.design_config,
        design_id=baseline_manifest.design_id,
        platform_id=baseline_manifest.platform_id,
        docker_image_id=baseline_manifest.docker_image_id,
        source_revision=baseline_manifest.source_revision,
        source_dirty=baseline_manifest.source_dirty,
        entries=entries, tree_hash=tree_hash, config_sha256=config_sha,
    )
    raw = manifest_bytes(manifest)
    target = Path(manifest_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return VariantResult(
        candidate_root=destination.resolve(), manifest_path=target,
        manifest_sha256=hashlib.sha256(raw).hexdigest(), manifest=manifest,
        config_sha256=config_sha, tree_hash=tree_hash, copy_method=copy_method,
    )


__all__ += [
    "CANDIDATE_DIFF_REJECTED",
    "ConfigAssignment",
    "VariantResult",
    "compute_candidate_config",
    "expected_candidate_tree_hash",
    "find_assignments",
    "materialize_candidate",
]


# ── 固定 ECO helper 的受控寫入 ──────────────────────────────────

REPAIR_HELPER_DIR = "flow/designs/{platform}/{design}/terminux_r5"
REPAIR_DIFF_REJECTED = "REPAIR_DIFF_REJECTED"


@dataclass(frozen=True)
class ApprovedSourcePatch:
    """一份已核准的 source 內容取代。

    `before_sha256`／`after_sha256` 由 host 事先凍結並綁進 task digest。
    套用時**兩端都驗**：原檔必須正好是 before，寫完必須正好是 after。
    不做 fuzzy apply、不自動合併、不容許多改一個 byte。
    """

    relative_path: str
    before_sha256: str
    after_sha256: str
    content: bytes


@dataclass(frozen=True)
class RepairHelperFile:
    """一個已核准的固定 helper 檔案。bytes 與 hash 由 server 事先固定。"""

    relative_path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class RepairSnapshotResult:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: "InputManifest"
    tree_hash: str
    config_sha256: str
    copy_method: str
    added_paths: tuple[str, ...]
    config_appended_sha256: str
    #: 本次實際套用的已核准 source 取代（含前後 hash）。
    patched_sources: tuple[dict[str, str], ...] = ()


def _apply_source_patches(
    destination: Path, patches: tuple["ApprovedSourcePatch", ...],
) -> tuple[dict[str, str], ...]:
    """套用已核准的 source 取代。前後 hash 都必須完全相符。"""
    applied: list[dict[str, str]] = []
    for patch in patches:
        target = destination / patch.relative_path
        if not target.is_file():
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                f"the source to patch does not exist: {patch.relative_path!r}")
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        if before != patch.before_sha256:
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                f"{patch.relative_path!r} hashes to {before} but the approved "
                f"patch applies to {patch.before_sha256}")
        after = hashlib.sha256(patch.content).hexdigest()
        if after != patch.after_sha256:
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                f"the patched bytes for {patch.relative_path!r} hash to {after} "
                f"but the approved result is {patch.after_sha256}")
        target.write_bytes(patch.content)
        written = hashlib.sha256(target.read_bytes()).hexdigest()
        if written != patch.after_sha256:
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                f"{patch.relative_path!r} did not end up at the approved hash")
        applied.append({"relative_path": patch.relative_path,
                        "before_sha256": patch.before_sha256,
                        "after_sha256": patch.after_sha256})
    return tuple(applied)


def _apply_repair_helpers(
    destination: Path, manifest: "InputManifest",
    helpers: tuple[RepairHelperFile, ...], config_suffix: bytes,
    config_replacement: bytes | None = None,
) -> tuple[tuple[str, ...], str]:
    """把已核准的 helper bytes 與 config 追加內容寫進隔離 source。

    **只**新增 `terminux_r5/` 下的檔案，並在 design config 末端追加固定內容。
    不改任何既有 stage script、RTL、SDC 或 platform 檔案。
    """
    added: list[str] = []
    for helper in helpers:
        actual = hashlib.sha256(helper.content).hexdigest()
        if actual != helper.sha256:
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                f"approved helper {helper.relative_path!r} does not match its "
                "fixed hash")
        target = destination / helper.relative_path
        if target.exists():
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                f"{helper.relative_path!r} already exists in the source tree; "
                "this round only adds new files")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(helper.content)
        added.append(helper.relative_path)

    config_path = destination / manifest.design_config
    original = config_path.read_bytes()
    if config_replacement is not None:
        # 候選的差異不是「末端追加」——`ABC_AREA` 的數字 token 在
        # 檔案中間。呼叫端已用 `compute_candidate_config()` 證明過那是**唯一
        # 一處**單 token 改寫，這裡只負責寫入並回報實際 hash。
        if config_suffix:
            raise InputPreparationError(
                REPAIR_DIFF_REJECTED,
                "config_replacement and config_suffix are mutually exclusive")
        config_path.write_bytes(config_replacement)
        return tuple(added), hashlib.sha256(config_replacement).hexdigest()
    # 只擋「同一段內容被套用兩次」。候選本來就是從**已經帶有共同 setup repair 設定**
    # 的 baseline 衍生的，所以不能用「含有 TERMINUX_R5」當判準。
    if config_suffix and config_suffix in original:
        raise InputPreparationError(
            REPAIR_DIFF_REJECTED,
            "the design config already carries this approved suffix")
    # 只在末端追加；既有每一行的 bytes 原樣保留。
    updated = original + config_suffix
    if not updated.startswith(original):
        raise InputPreparationError(
            REPAIR_DIFF_REJECTED, "the design config prefix was not preserved")
    config_path.write_bytes(updated)
    return tuple(added), hashlib.sha256(updated).hexdigest()


def prepare_repair_snapshot(
    *,
    source_manifest: "InputManifest",
    source_root: str | Path,
    destination_root: str | Path,
    manifest_path: str | Path,
    slot: str,
    helpers: tuple[RepairHelperFile, ...],
    config_suffix: bytes,
    expected_tree_hash: str | None = None,
    config_replacement: bytes | None = None,
    source_patches: tuple["ApprovedSourcePatch", ...] = (),
) -> RepairSnapshotResult:
    """由已保存的來源 snapshot 建立 setup repair 的 slot source。

    `source_revision`／`source_dirty`／`docker_image_id` 由來源 manifest
    繼承——這一份是**副本的副本**，本身沒有 git worktree，因此不重新推導
    revision，也不填空值假裝未知。
    """
    source = Path(source_root).resolve()
    destination = Path(destination_root)
    copy_method = _copy_tree(source, destination)
    added, config_sha = _apply_repair_helpers(
        destination, source_manifest, helpers, config_suffix,
        config_replacement=config_replacement)
    # 已核准的 source 取代（本批只有那一份 RTL）。前後 hash 都驗。
    patched = _apply_source_patches(destination, source_patches)

    entries = scan_inventory(destination.resolve())
    tree_hash = inventory_tree_hash(entries)
    if expected_tree_hash is not None and tree_hash != expected_tree_hash:
        raise InputPreparationError(
            REPAIR_DIFF_REJECTED,
            "the materialized repair tree hash does not match the value bound "
            "into the task digest at create time")

    manifest = InputManifest(
        schema_version=MANIFEST_SCHEMA_VERSION, slot=slot,
        source_root=str(source), snapshot_root=str(destination.resolve()),
        design_config=source_manifest.design_config,
        design_id=source_manifest.design_id,
        platform_id=source_manifest.platform_id,
        docker_image_id=source_manifest.docker_image_id,
        source_revision=source_manifest.source_revision,
        source_dirty=source_manifest.source_dirty,
        entries=entries, tree_hash=tree_hash, config_sha256=config_sha,
    )
    raw = manifest_bytes(manifest)
    target = Path(manifest_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return RepairSnapshotResult(
        root=destination.resolve(), manifest_path=target,
        manifest_sha256=hashlib.sha256(raw).hexdigest(), manifest=manifest,
        tree_hash=tree_hash, config_sha256=config_sha, copy_method=copy_method,
        added_paths=added, config_appended_sha256=config_sha,
        patched_sources=patched,
    )


__all__ += [
    "REPAIR_DIFF_REJECTED",
    "REPAIR_HELPER_DIR",
    "RepairHelperFile",
    "RepairSnapshotResult",
    "prepare_repair_snapshot",
]
