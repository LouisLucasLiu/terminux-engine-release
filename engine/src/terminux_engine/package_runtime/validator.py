# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Contract Validator v1 — offline manifest tree validation。

不啟動 daemon、不 import／執行任何 package handler 程式碼。重用
production `manifest.py::parse_manifest()` 做 manifest 語意驗證；本模組
只新增 parser 本身看不到的「跨 package」與「entrypoint 存在性（static
分析）」檢查。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.package_runtime import identity
from terminux_engine.package_runtime.entrypoint import (
    EntrypointPathEscapeError,
    EntrypointSourceMissingError,
    EntrypointSymlinkSourceError,
    MalformedEntrypointError,
)
from terminux_engine.package_runtime.entrypoint import (
    resolve_entrypoint as resolve_canonical_entrypoint,
)
from terminux_engine.package_runtime.errors import DuplicateManifestKeyError, ManifestError
from terminux_engine.package_runtime.manifest import (
    PackageManifest,
    _DuplicateManifestKeyLoader,
    parse_manifest,
)

SCHEMA_VERSION = 1

_KNOWN_PERMISSION_SERVICES = frozenset({"file", "store", "notify", "report", "llm"})
_UNSUPPORTED_RUNTIME_PHASES = frozenset({"before", "main"})
_SEVERITY_RANK = {"ERROR": 0, "WARNING": 1}


class ValidatorInternalError(Exception):
    """Validator 自身（非 manifest 內容）的非預期錯誤，對應 CLI exit code 2。"""


@dataclass(frozen=True)
class Finding:
    """單一驗證結果。"""

    severity: str  # "ERROR" | "WARNING"
    code: str
    manifest_path: str  # POSIX 相對路徑（相對於 scan root），deterministic 輸出用
    package_id: str | None
    field: str | None
    message: str

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.manifest_path,
            _SEVERITY_RANK.get(self.severity, 99),
            self.code,
            self.field or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "manifest_path": self.manifest_path,
            "package_id": self.package_id,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationReport:
    """整棵 package tree 的驗證結果。"""

    schema_version: int
    valid: bool
    package_count: int
    error_count: int
    warning_count: int
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "valid": self.valid,
            "package_count": self.package_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "findings": [f.to_dict() for f in self.findings],
        }


# ─── Duplicate YAML mapping key 偵測 ─────────────────────────────────────────
# 重用 production manifest.py 的 `_DuplicateManifestKeyLoader`（唯一一套
# duplicate-key-safe YAML loader，production `load_manifest()` 與本
# validator 共用同一個 class，不自製第二套 parser）。


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_within(candidate: Path, base: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


# ─── Entrypoint static 存在性檢查（不 import） ──────────────────────────────


def _check_entrypoint(
    entrypoint: str,
    *,
    package_dir: Path,
    root: Path,
    package_id: str,
    manifest_rel: str,
    field: str,
    findings: list[Finding],
) -> None:
    """驗證 `module.function` entrypoint 是否存在，純 static（AST），不 import。

    Grammar 與 filesystem containment（path escape／symlink source／
    regular-file 檢查）全部委派給 canonical resolver（`entrypoint.py`），
    與 production `manifest.py`/`loader.py` 共用同一套規則，不在此重寫
    路徑邏輯。本函式只負責把 resolver 的例外映射成對應 Finding，並在
    resolver 成功後做 AST-only 的函式存在性檢查（resolver 本身不 import、
    不判斷函式是否存在）。
    """
    try:
        resolved = resolve_canonical_entrypoint(package_dir, entrypoint)
    except MalformedEntrypointError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="MALFORMED_ENTRYPOINT",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=field,
            message=str(exc),
        ))
        return
    except EntrypointSymlinkSourceError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="HANDLER_SYMLINK_SOURCE",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=field,
            message=str(exc),
        ))
        return
    except EntrypointSourceMissingError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="MISSING_HANDLER_MODULE",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=field,
            message=str(exc),
        ))
        return
    except EntrypointPathEscapeError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="HANDLER_PATH_ESCAPE",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=field,
            message=str(exc),
        ))
        return

    module_path = resolved.source_path
    func_name = resolved.ref.function_name

    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        findings.append(Finding(
            severity="ERROR",
            code="HANDLER_MODULE_SYNTAX_ERROR",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=field,
            message=f"entrypoint module has a syntax error: {resolved.ref.module_name}.py: {exc}",
        ))
        return

    found = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name
        for node in tree.body
    )
    if not found:
        findings.append(Finding(
            severity="ERROR",
            code="MISSING_HANDLER_FUNCTION",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=field,
            message=(
                f"entrypoint function not found at module top level: "
                f"{func_name} (entrypoint={entrypoint!r})"
            ),
        ))


_ENTRYPOINT_CAUSE_CODES = {
    MalformedEntrypointError: "MALFORMED_ENTRYPOINT",
    EntrypointSymlinkSourceError: "HANDLER_SYMLINK_SOURCE",
    EntrypointSourceMissingError: "MISSING_HANDLER_MODULE",
    EntrypointPathEscapeError: "HANDLER_PATH_ESCAPE",
}


def _finding_for_manifest_error(
    exc: ManifestError, *, manifest_rel: str, package_id: str | None
) -> Finding:
    """把 `parse_manifest()` 的 `ManifestError` 映射成 Finding。

    manifest.py 現在對 entrypoint 做完整 grammar/containment 檢查，會在
    `ManifestError.__cause__` 保留原始的 `EntrypointError` 子型別（見
    `manifest.py::_validate_python_entrypoint()` 的 `raise ... from exc`）；
    有這個 cause 時映射成對應的具體 reason code（與 `_check_entrypoint()`
    使用同一份對照表），維持「malformed／source missing／path escape／
    symlink source」互相區分，不混成單一 `INVALID_MANIFEST`。其餘（schema、
    必填欄位、prompt、namespace 等）manifest 錯誤維持既有的
    `INVALID_MANIFEST` 通用 code，行為不變。
    """
    cause = exc.__cause__
    code = _ENTRYPOINT_CAUSE_CODES.get(type(cause), "INVALID_MANIFEST")
    return Finding(
        severity="ERROR",
        code=code,
        manifest_path=manifest_rel,
        package_id=package_id,
        field=None,
        message=str(exc),
    )


# ─── 單一 manifest 驗證 ──────────────────────────────────────────────────────


def _validate_single_manifest(
    manifest_path: Path,
    *,
    package_dir: Path,
    root: Path,
    seen_resolved_manifest_paths: dict[Path, str],
) -> tuple[list[Finding], PackageManifest | None]:
    """驗證單一 package 的 manifest，回傳 (findings, manifest_or_None)。

    Duplicate package/action id 的跨 package 檢查**不在這裡做**——那需要
    看到全部已成功解析的 manifest，由呼叫端（`validate_tree()`）在收集
    完所有 manifest 後，透過共用的 `identity.build_package_identity_index()`
    一次做（與 production `registry.py::discover_and_register_packages()`
    共用同一套判斷，不維護兩份 set/dict 邏輯）。
    """
    findings: list[Finding] = []
    manifest_rel = _rel(manifest_path, root)

    resolved = manifest_path.resolve()
    if resolved in seen_resolved_manifest_paths:
        findings.append(Finding(
            severity="ERROR",
            code="DUPLICATE_MANIFEST_PATH",
            manifest_path=manifest_rel,
            package_id=None,
            field=None,
            message=(
                f"manifest resolves to the same file as "
                f"{seen_resolved_manifest_paths[resolved]!r} (symlink/alias?)"
            ),
        ))
        return findings, None
    seen_resolved_manifest_paths[resolved] = manifest_rel

    if not _is_within(package_dir, root):
        findings.append(Finding(
            severity="ERROR",
            code="PACKAGE_DIR_PATH_ESCAPE",
            manifest_path=manifest_rel,
            package_id=None,
            field=None,
            message=f"package directory escapes scan root: {package_dir}",
        ))
        return findings, None

    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="UNREADABLE_PACKAGE_MANIFEST",
            manifest_path=manifest_rel,
            package_id=None,
            field=None,
            message=f"cannot read manifest file: {exc}",
        ))
        return findings, None

    try:
        raw = yaml.load(raw_text, Loader=_DuplicateManifestKeyLoader)
    except DuplicateManifestKeyError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="DUPLICATE_MANIFEST_KEY",
            manifest_path=manifest_rel,
            package_id=None,
            field=None,
            message=str(exc),
        ))
        return findings, None
    except yaml.YAMLError as exc:
        findings.append(Finding(
            severity="ERROR",
            code="MALFORMED_YAML",
            manifest_path=manifest_rel,
            package_id=None,
            field=None,
            message=str(exc),
        ))
        return findings, None

    if not isinstance(raw, dict):
        findings.append(Finding(
            severity="ERROR",
            code="ROOT_NOT_MAPPING",
            manifest_path=manifest_rel,
            package_id=None,
            field=None,
            message="manifest root must be a mapping",
        ))
        return findings, None

    # 重用 production parser 做 manifest 語意驗證（schema/API/required
    # fields/entrypoint 格式/prompt path 存在性與 containment/action
    # namespace 等，皆由 parse_manifest() 負責，本函式不重新實作）。
    try:
        manifest: PackageManifest = parse_manifest(raw, source_path=manifest_path)
    except ManifestError as exc:
        findings.append(_finding_for_manifest_error(
            exc,
            manifest_rel=manifest_rel,
            package_id=(raw.get("package") or {}).get("id") if isinstance(raw.get("package"), dict) else None,
        ))
        return findings, None

    package_id = manifest.package_id

    # ── before/main：schema 合法但 runtime 未接線，一律 ERROR ──────────────
    all_phase_names = set(manifest.phases) | set(manifest.prompt_phases)
    for phase_name in sorted(all_phase_names & _UNSUPPORTED_RUNTIME_PHASES):
        findings.append(Finding(
            severity="ERROR",
            code="UNSUPPORTED_RUNTIME_PHASE",
            manifest_path=manifest_rel,
            package_id=package_id,
            field=f"phases.{phase_name}",
            message=(
                f"phase {phase_name!r} is schema-valid but not wired into the "
                f"runtime dispatcher (only 'after' is executed); declaring it "
                f"has no effect at runtime"
            ),
        ))

    # ── after phase entrypoint 存在性（static，不 import）───────────────────
    if "after" in manifest.phases:
        _check_entrypoint(
            manifest.phases["after"],
            package_dir=package_dir,
            root=root,
            package_id=package_id,
            manifest_rel=manifest_rel,
            field="phases.after",
            findings=findings,
        )

    # ── actions：entrypoint 存在性（duplicate action id 由呼叫端跨 manifest
    # 統一處理，見函式 docstring）───────────────────────────────────────────
    for action_name, handler in sorted(manifest.actions.items()):
        _check_entrypoint(
            handler,
            package_dir=package_dir,
            root=root,
            package_id=package_id,
            manifest_rel=manifest_rel,
            field=f"actions.{action_name}",
            findings=findings,
        )

    # ── permissions：語法已由 parse_manifest 檢查（必須是 mapping）；這裡
    # 只加「已知 service 名稱」與「不強制」提醒 ──────────────────────────
    services = manifest.permissions.get("services") if isinstance(manifest.permissions, dict) else None
    if isinstance(services, dict) and services:
        for service_name in sorted(services):
            if service_name not in _KNOWN_PERMISSION_SERVICES:
                findings.append(Finding(
                    severity="WARNING",
                    code="UNKNOWN_PERMISSION_SERVICE",
                    manifest_path=manifest_rel,
                    package_id=package_id,
                    field=f"permissions.services.{service_name}",
                    message=f"unknown permission service name: {service_name!r}",
                ))
        findings.append(Finding(
            severity="WARNING",
            code="DECLARED_PERMISSIONS_NOT_RUNTIME_ENFORCED",
            manifest_path=manifest_rel,
            package_id=package_id,
            field="permissions",
            message=(
                "permissions are declared but the runtime does not enforce them "
                "(v1: declaration only, see package_runtime/permissions.py)"
            ),
        ))

    # ── when.tools：voltus catalog gap 提醒（既有 gap，不得由 validator 補入 catalog）
    if "voltus" in manifest.when_tools:
        findings.append(Finding(
            severity="WARNING",
            code="VOLTUS_CATALOG_GAP",
            manifest_path=manifest_rel,
            package_id=package_id,
            field="when.tools",
            message=(
                "'voltus' is declared as a trigger but is not wrapped by the "
                "shell wrapper and is not in the Canonical EDA Tool Catalog v1 "
                "(known, accepted gap — see shell_hook_tool_registry_single_"
                "source_decision.md)"
            ),
        ))

    return findings, manifest


# ─── Tree-level 掃描 ─────────────────────────────────────────────────────────


def validate_tree(root: str | Path) -> ValidationReport:
    """驗證整個 package tree（deterministic，唯讀，不 import handler）。"""
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValidatorInternalError(f"scan root is not a directory: {root_path}")

    all_findings: list[Finding] = []
    seen_resolved_manifest_paths: dict[Path, str] = {}
    collected_manifests: list[tuple[str, PackageManifest]] = []
    package_count = 0

    try:
        entries = sorted(root_path.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise ValidatorInternalError(f"cannot list scan root: {exc}") from exc

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError as exc:
            all_findings.append(Finding(
                severity="ERROR",
                code="UNREADABLE_PACKAGE_DIRECTORY",
                manifest_path=_rel(entry, root_path),
                package_id=None,
                field=None,
                message=f"cannot stat package directory: {exc}",
            ))
            continue

        manifest_path = entry / "package.yaml"
        if not manifest_path.is_file():
            continue  # 非 package 目錄（例如 __pycache__），沿用 production 慣例跳過

        package_count += 1
        findings, manifest = _validate_single_manifest(
            manifest_path,
            package_dir=entry,
            root=root_path,
            seen_resolved_manifest_paths=seen_resolved_manifest_paths,
        )
        all_findings.extend(findings)
        if manifest is not None:
            collected_manifests.append((_rel(manifest_path, root_path), manifest))

    # Duplicate package/action id：跨全部已成功解析的 manifest 一次檢查，
    # 與 production `registry.py::discover_and_register_packages()` 共用
    # 同一個 identity index 判斷（`package_runtime/identity.py`），不維護
    # 兩份 set/dict 邏輯。
    manifest_by_label = {label: manifest for label, manifest in collected_manifests}
    index = identity.build_package_identity_index(collected_manifests)
    for collision in index.collisions:
        kind_label = "package id" if collision.kind == identity.DUPLICATE_PACKAGE_ID else "action id"
        field = "package.id" if collision.kind == identity.DUPLICATE_PACKAGE_ID else f"actions.{collision.identity}"
        owner_manifest = manifest_by_label.get(collision.second_owner)
        collision_package_id = (
            collision.identity if collision.kind == identity.DUPLICATE_PACKAGE_ID
            else (owner_manifest.package_id if owner_manifest is not None else None)
        )
        all_findings.append(Finding(
            severity="ERROR",
            code=collision.kind,
            manifest_path=collision.second_owner,
            package_id=collision_package_id,
            field=field,
            message=(
                f"duplicate {kind_label} {collision.identity!r} already declared by "
                f"{collision.first_owner!r}"
            ),
        ))

    all_findings.sort(key=lambda f: f.sort_key())
    error_count = sum(1 for f in all_findings if f.severity == "ERROR")
    warning_count = sum(1 for f in all_findings if f.severity == "WARNING")

    return ValidationReport(
        schema_version=SCHEMA_VERSION,
        valid=error_count == 0,
        package_count=package_count,
        error_count=error_count,
        warning_count=warning_count,
        findings=tuple(all_findings),
    )


def validate_package_dir(package_dir: str | Path) -> tuple[ValidationReport, PackageManifest | None]:
    """驗證單一 package 目錄（不掃描兄弟目錄、不 import handler）。

    與 `validate_tree()` 共用同一個 `_validate_single_manifest()`；只在目錄名稱
    不是 package id 的情境使用（例如 user package 的不可變 generation 目錄）。
    回傳 report 與成功解析的 manifest（解析失敗時為 `None`）。
    """
    package_path = Path(package_dir)
    if not package_path.is_dir():
        raise ValidatorInternalError(f"package directory is not a directory: {package_path}")
    manifest_path = package_path / "package.yaml"
    if not manifest_path.is_file():
        finding = Finding(
            severity="ERROR",
            code="MISSING_PACKAGE_MANIFEST",
            manifest_path="package.yaml",
            package_id=None,
            field=None,
            message="package.yaml not found in package directory",
        )
        return ValidationReport(
            schema_version=SCHEMA_VERSION, valid=False, package_count=0,
            error_count=1, warning_count=0, findings=(finding,),
        ), None
    findings, manifest = _validate_single_manifest(
        manifest_path,
        package_dir=package_path,
        root=package_path.parent,
        seen_resolved_manifest_paths={},
    )
    findings.sort(key=lambda f: f.sort_key())
    error_count = sum(1 for f in findings if f.severity == "ERROR")
    warning_count = sum(1 for f in findings if f.severity == "WARNING")
    return ValidationReport(
        schema_version=SCHEMA_VERSION,
        valid=error_count == 0,
        package_count=1,
        error_count=error_count,
        warning_count=warning_count,
        findings=tuple(findings),
    ), manifest


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _default_root() -> Path:
    # 預設驗證已安裝套件的內建 Package，不依賴開發 checkout。
    return Path(__file__).resolve().parents[1] / "packages"


def _format_text(report: ValidationReport) -> str:
    lines: list[str] = []
    lines.append(
        f"Package Contract Validator v1 — {report.package_count} package(s) scanned, "
        f"{report.error_count} error(s), {report.warning_count} warning(s)"
    )
    for f in report.findings:
        pkg = f.package_id or "-"
        fld = f.field or "-"
        lines.append(f"[{f.severity}] {f.manifest_path} pkg={pkg} field={fld} {f.code}: {f.message}")
    lines.append("VALID" if report.valid else "INVALID")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-package-validate")
    parser.add_argument("--root", default=None, help="package tree root（預設 production packages 目錄）")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root is not None else _default_root()

    try:
        report = validate_tree(root)
    except ValidatorInternalError as exc:
        print(f"terminux-package-validate: internal error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 真正未預期的錯誤，不得誤報成 manifest invalid
        print(f"terminux-package-validate: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_format_text(report))

    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
