# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""已停止 daemon 時的明確路徑冷備份；不宣稱線上一致性。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from terminux_engine.ipc.agent_socket import format_agent_socket_path, format_lineage_socket_path
from terminux_engine.ipc.daemon_owner import owner_present


class BackupError(ValueError):
    """來源、socket 或還原前置條件不符合時拒絕。"""


def _require_stopped(sockets: tuple[Path, Path]) -> None:
    if owner_present():
        raise BackupError("daemon owner.lock exists; stop and reconcile work first")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise BackupError(f"symlink is not allowed: {root}")
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise BackupError(f"source is not a regular file or directory: {root}")
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"symlink is not allowed: {path}")
        if path.is_file():
            result.append(path)
        elif not path.is_dir():
            raise BackupError(f"special file is not allowed: {path}")
    return result


def _validated_sources(destination: Path, sources: dict[str, Path]) -> dict[str, Path]:
    if not destination.is_absolute() or destination.exists():
        raise BackupError("destination must be an absent absolute path")
    if not sources:
        raise BackupError("at least one explicit source is required")
    result: dict[str, Path] = {}
    for label, supplied in sources.items():
        if not label or not label.replace("_", "").replace("-", "").isalnum():
            raise BackupError("source labels must contain only letters, digits, _ or -")
        if not supplied.is_absolute():
            raise BackupError(f"source must be absolute: {label}")
        source = supplied.resolve(strict=True)
        if source != supplied or destination.is_relative_to(source):
            raise BackupError(f"source is a symlink or contains destination: {label}")
        _files(source)
        result[label] = source
    paths = list(result.values())
    for index, left in enumerate(paths):
        if any(left == right or left.is_relative_to(right)
               or right.is_relative_to(left) for right in paths[index + 1:]):
            raise BackupError("source paths must not overlap")
    return result


def create_backup(destination: Path, sources: dict[str, Path],
                  *, sockets: tuple[Path, Path]) -> dict[str, Any]:
    """逐一路徑複製並驗 hash，最後才將 staging 原子改名。"""
    _require_stopped(sockets)
    destination = Path(destination)
    checked = _validated_sources(destination, sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".terminux-backup-", dir=destination.parent) as scratch:
        stage = Path(scratch)
        stage.chmod(0o700)
        manifest: dict[str, Any] = {"schema_version": "terminux.cold_backup.v1", "sources": []}
        for label, source in checked.items():
            copied = stage / "data" / label
            copied.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copy2(source, copied)
            else:
                shutil.copytree(source, copied, symlinks=False)
            entries = []
            for original in _files(source):
                relative = original.relative_to(source) if source.is_dir() else Path(".")
                clone = copied / relative if source.is_dir() else copied
                original_hash = _hash(original)
                if _hash(clone) != original_hash:
                    raise BackupError(f"source changed while copying: {label}/{relative}")
                entries.append({"relative_path": relative.as_posix(),
                                "sha256": original_hash, "bytes": original.stat().st_size})
            manifest["sources"].append({"label": label, "original_path": str(source),
                                        "kind": "file" if source.is_file() else "directory",
                                        "entries": entries})
        (stage / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            raise BackupError("destination appeared during backup")
        stage.rename(destination)
    return verify_backup(destination)


def verify_backup(backup: Path) -> dict[str, Any]:
    """核對備份清單、檔案集合、大小及 SHA-256；不開啟 daemon。"""
    backup = Path(backup)
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError("manifest is missing or unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "terminux.cold_backup.v1":
        raise BackupError("backup schema is unsupported")
    if not isinstance(manifest.get("sources"), list):
        raise BackupError("backup source list is invalid")
    labels: set[str] = set()
    total = 0
    for source in manifest["sources"]:
        if not isinstance(source, dict):
            raise BackupError("backup source entry is invalid")
        label = source.get("label")
        if not isinstance(label, str) or label in labels or not label.replace("_", "").replace("-", "").isalnum():
            raise BackupError("backup source label is invalid")
        labels.add(label)
        root = backup / "data" / label
        kind = source.get("kind")
        if kind not in {"file", "directory"}:
            raise BackupError("backup source kind is invalid")
        if (kind == "file" and not root.is_file()) or (kind == "directory" and not root.is_dir()):
            raise BackupError(f"backup source is missing: {label}")
        entries = source.get("entries")
        if not isinstance(entries, list):
            raise BackupError("backup file list is invalid")
        expected: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise BackupError("backup file entry is invalid")
            relative = entry.get("relative_path")
            if not isinstance(relative, str):
                raise BackupError("backup relative path is invalid")
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise BackupError("backup relative path escapes source")
            file_path = root / relative_path if kind == "directory" else root
            if not file_path.is_file() or file_path.is_symlink():
                raise BackupError(f"backup file is missing: {label}/{relative}")
            if _hash(file_path) != entry.get("sha256") or file_path.stat().st_size != entry.get("bytes"):
                raise BackupError(f"backup file checksum mismatch: {label}/{relative}")
            expected.add(relative)
            total += 1
        actual = {path.relative_to(root).as_posix() if kind == "directory" else "."
                  for path in _files(root)}
        if actual != expected:
            raise BackupError(f"backup file inventory mismatch: {label}")
    actual_labels = {path.name for path in (backup / "data").iterdir()}
    if actual_labels != labels:
        raise BackupError("backup source inventory mismatch")
    return {"status": "verified", "schema_version": manifest["schema_version"],
            "source_count": len(labels), "file_count": total}


def restore_backup(backup: Path, targets: dict[str, Path],
                   *, sockets: tuple[Path, Path]) -> dict[str, Any]:
    """只還原至清單所記的明確原路徑；所有目標必須不存在。"""
    _require_stopped(sockets)
    verified = verify_backup(backup)
    manifest = json.loads((Path(backup) / "manifest.json").read_text(encoding="utf-8"))
    rows = {row["label"]: row for row in manifest["sources"]}
    if set(targets) != set(rows):
        raise BackupError("restore targets must exactly match backup labels")
    for label, supplied in targets.items():
        if not supplied.is_absolute() or str(supplied) != rows[label]["original_path"]:
            raise BackupError(f"restore target differs from original path: {label}")
        if supplied.exists() or supplied.is_symlink():
            raise BackupError(f"restore target already exists: {label}")
    staged: list[tuple[Path, Path]] = []
    for label, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".terminux-restore-", dir=target.parent))
        payload = stage / "payload"
        source = Path(backup) / "data" / label
        if rows[label]["kind"] == "file":
            shutil.copy2(source, payload)
        else:
            shutil.copytree(source, payload)
        for entry in rows[label]["entries"]:
            relative = Path(entry["relative_path"])
            copied = payload / relative if rows[label]["kind"] == "directory" else payload
            if _hash(copied) != entry["sha256"]:
                raise BackupError(f"restore staging checksum mismatch: {label}/{relative}")
        staged.append((payload, target))
    for payload, target in staged:
        if target.exists() or target.is_symlink():
            raise BackupError(f"restore target appeared during staging: {target}")
    for payload, target in staged:
        payload.rename(target)
        payload.parent.rmdir()
    return {**verified, "status": "restored"}


def _paths(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        label, separator, path = value.partition("=")
        if not separator or not label or not path or label in result:
            raise BackupError("paths must be unique LABEL=/absolute/path entries")
        result[label] = Path(path)
    return result


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-cold-backup")
    parser.add_argument("--agent-socket", type=Path)
    parser.add_argument("--lineage-socket", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("destination", type=Path)
    create.add_argument("--source", action="append", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--target", action="append", required=True)
    args = parser.parse_args(argv)
    sockets = (
        args.agent_socket or format_agent_socket_path(os.geteuid()),
        args.lineage_socket or format_lineage_socket_path(os.geteuid()),
    )
    try:
        if args.command == "create":
            result = create_backup(args.destination, _paths(args.source), sockets=sockets)
        elif args.command == "verify":
            result = verify_backup(args.backup)
        else:
            result = restore_backup(args.backup, _paths(args.target), sockets=sockets)
    except (BackupError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
