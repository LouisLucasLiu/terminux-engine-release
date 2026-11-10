# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Scaffold v1 — 安全、確定性的離線 Python after-package 產生器。

只產生 v1 唯一支援的 profile：Python `after` package（`__init__.py`／
`package.yaml`／`after.py`／`README.md`）。不啟動 daemon、不 import
package handler、不覆寫既有檔案。寫入前後皆重用
`package_runtime.validator::validate_tree()` 做驗證，不解析其 CLI
文字輸出、不修改該模組。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.package_runtime import validator as pv
from terminux_engine.package_runtime.version import API_VERSION, SCHEMA_VERSION

DEFAULT_VERSION = "0.1.0"
_SCAFFOLD_FILES = ("__init__.py", "package.yaml", "after.py", "README.md")


class ScaffoldInternalError(Exception):
    """Scaffold CLI usage／無法完成的前置條件錯誤，對應 exit code 2。"""


@dataclass(frozen=True)
class ScaffoldResult:
    """單次 scaffold 呼叫的結果，直接對應輸出 JSON schema。"""

    schema_version: int
    status: str  # "CREATED" | "DRY_RUN" | "REFUSED"
    package_id: str
    target: str
    files: tuple[str, ...]
    validation_errors: int
    validation_warnings: int
    refusal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "package_id": self.package_id,
            "target": self.target,
            "files": list(self.files),
            "validation": {
                "errors": self.validation_errors,
                "warnings": self.validation_warnings,
            },
        }


# ─── package id / tool / version 合法性（filesystem 安全層，非 manifest contract）──


def _reject_package_id(package_id: str) -> str | None:
    """回傳拒絕原因；合法時回傳 None。只做 filesystem 安全檢查，不重造 manifest contract。"""
    if not package_id:
        return "package id must not be empty"
    if Path(package_id).is_absolute():
        return "package id must not be an absolute path"
    if package_id in (".", ".."):
        return "package id must not be '.' or '..'"
    if "/" in package_id or os.sep in package_id or (os.altsep and os.altsep in package_id):
        return "package id must not contain path separators"
    return None


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _yaml_scalar(value: str) -> str:
    """借用 PyYAML 自己的 quoting 判斷，取得單一 scalar 的正確 YAML 表示。"""
    dumped = yaml.safe_dump({"v": value}, default_flow_style=False, allow_unicode=True)
    return dumped[len("v: "):].rstrip("\n")


def _render_manifest(package_id: str, version: str, tools: list[str]) -> str:
    lines = [
        "package:",
        f"  id: {_yaml_scalar(package_id)}",
        f"  version: {_yaml_scalar(version)}",
        f"  api: {_yaml_scalar(API_VERSION)}",
        "",
        f"schema: {SCHEMA_VERSION}",
        "",
        "when:",
        "  tools:",
    ]
    for tool in tools:
        lines.append(f"    - {_yaml_scalar(tool)}")
    lines.extend([
        "",
        "phases:",
        "  after: after.run",
        "",
    ])
    return "\n".join(lines)


def _render_after_py(package_id: str) -> str:
    return (
        f'"""{package_id} after-phase package（scaffold 產生的 placeholder，待實作）。"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "from terminux_engine.package_runtime.context import PackageContext\n"
        "from terminux_engine.package_runtime.result import PhaseResult\n"
        "\n"
        "\n"
        "def run(ctx: PackageContext) -> PhaseResult:\n"
        '    """Placeholder：尚未實作任何治理判斷，一律回傳 noop。"""\n'
        "    return PhaseResult(\n"
        '        status="noop",\n'
        "        message=(\n"
        f'            "{package_id} after-phase 尚未實作，這是 scaffold 產生的"\n'
        '            "placeholder，不代表任何治理判斷已完成。"\n'
        "        ),\n"
        "    )\n"
    )


def _render_readme(package_id: str, version: str, tools: list[str]) -> str:
    tools_list = "\n".join(f"- `{tool}`" for tool in tools)
    return (
        f"# Package: {package_id}\n"
        "\n"
        f"- **id**: `{package_id}`\n"
        f"- **version**: `{version}`\n"
        f"- **matched tools** (`when.tools`):\n{tools_list}\n"
        "\n"
        "## 目前狀態\n"
        "\n"
        "本 package 由 `terminux-package-new`（Package Scaffold v1）產生，"
        "**只是一個安全的 noop placeholder，不是完整實作**：\n"
        "\n"
        "- v1 scaffold 只支援 runtime `after` phase（`phases.after`）。"
        "`before`/`main` 目前 schema 合法但 runtime 不執行，本 scaffold 不產生。\n"
        "- `after.py::run()` 目前只回傳 `PhaseResult(status=\"noop\", ...)`，"
        "**必須由開發者替換**成真正的治理判斷，不得直接視為完成。\n"
        "- 本 scaffold 不產生任何 `actions`、prompt phase、permissions 自動推導、"
        "rules/templates 資料夾或 test harness——這些皆留待後續階段。\n"
        "\n"
        "## 驗證\n"
        "\n"
        "修改 `package.yaml`／`after.py` 後，請執行：\n"
        "\n"
        "```sh\n"
        "# 先啟用安裝了 terminux-engine 的 Python 環境\n"
        "terminux-package-validate --root <PACKAGES_ROOT>\n"
        "```\n"
        "\n"
        "確認沒有 ERROR（WARNING 不阻塞，但建議一併檢視）。\n"
        "\n"
        "## 已知限制（明確聲明，避免誤解）\n"
        "\n"
        "- `permissions` 目前只是宣告資料，**runtime 不會強制執行**"
        "（見 `package_runtime/permissions.py`）。\n"
        "- `ReportService` 目前只是純記憶體登記，**不會**自動持久化或顯示報告——"
        "若需要真正保存結果，必須自行處理（例如 `packages/orfs_dqe/after.py` 以 "
        "`_be_dqe_common.db_writer` 寫入 DQE DB）。\n"
        "- 新增或修改本 package 後，production daemon 的 package inventory"
        "**只會在正式 lifecycle 重啟後刷新**；本 scaffold 不負責、也不會嘗試重啟 daemon。\n"
    )


# ─── 主要邏輯 ─────────────────────────────────────────────────────────────────


def _scoped_counts(report: pv.ValidationReport, *, prefix: str | None) -> tuple[int, int]:
    if prefix is None:
        return report.error_count, report.warning_count
    findings = [f for f in report.findings if f.manifest_path.startswith(prefix)]
    errors = sum(1 for f in findings if f.severity == "ERROR")
    warnings = sum(1 for f in findings if f.severity == "WARNING")
    return errors, warnings


def scaffold_package(
    package_id: str,
    *,
    root: str | Path,
    tools: list[str],
    version: str = DEFAULT_VERSION,
    dry_run: bool = False,
) -> ScaffoldResult:
    """建立（或 dry-run 模擬建立）一個 Python after package。"""
    root_path = Path(root)
    if not root_path.exists():
        raise ScaffoldInternalError(f"root does not exist: {root_path}")
    if not root_path.is_dir():
        raise ScaffoldInternalError(f"root is not a directory: {root_path}")
    try:
        root_resolved = root_path.resolve()
    except OSError as exc:
        raise ScaffoldInternalError(f"cannot resolve root: {exc}") from exc

    id_reason = _reject_package_id(package_id)
    if id_reason is not None:
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target="",
            files=(),
            validation_errors=0,
            validation_warnings=0,
            refusal_reason=id_reason,
        )

    version = version if version else ""
    if not version:
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target="",
            files=(),
            validation_errors=0,
            validation_warnings=0,
            refusal_reason="version must not be empty",
        )

    deduped_tools = _dedupe_preserve_order(tools)
    if not deduped_tools:
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target="",
            files=(),
            validation_errors=0,
            validation_warnings=0,
            refusal_reason="at least one --tool is required",
        )
    for tool in deduped_tools:
        if not tool:
            return ScaffoldResult(
                schema_version=SCHEMA_VERSION,
                status="REFUSED",
                package_id=package_id,
                target="",
                files=(),
                validation_errors=0,
                validation_warnings=0,
                refusal_reason="tool name must not be empty",
            )

    target = root_path / package_id
    try:
        target_resolved = target.resolve()
    except OSError as exc:
        raise ScaffoldInternalError(f"cannot resolve target: {exc}") from exc
    if target_resolved.parent != root_resolved:
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target=str(target_resolved),
            files=(),
            validation_errors=0,
            validation_warnings=0,
            refusal_reason="package id escapes root directory",
        )

    if os.path.lexists(target):
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target=str(target_resolved),
            files=(),
            validation_errors=0,
            validation_warnings=0,
            refusal_reason="target already exists",
        )

    try:
        pre_report = pv.validate_tree(root_path)
    except pv.ValidatorInternalError as exc:
        raise ScaffoldInternalError(f"pre-write validation failed: {exc}") from exc

    if pre_report.error_count > 0:
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target=str(target_resolved),
            files=(),
            validation_errors=pre_report.error_count,
            validation_warnings=pre_report.warning_count,
            refusal_reason="existing package root already has validation errors",
        )

    if dry_run:
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="DRY_RUN",
            package_id=package_id,
            target=str(target_resolved),
            files=_SCAFFOLD_FILES,
            validation_errors=pre_report.error_count,
            validation_warnings=pre_report.warning_count,
        )

    manifest_text = _render_manifest(package_id, version, deduped_tools)
    after_text = _render_after_py(package_id)
    readme_text = _render_readme(package_id, version, deduped_tools)

    try:
        target.mkdir()
        (target / "__init__.py").write_text("", encoding="utf-8")
        (target / "package.yaml").write_text(manifest_text, encoding="utf-8")
        (target / "after.py").write_text(after_text, encoding="utf-8")
        (target / "README.md").write_text(readme_text, encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ScaffoldInternalError(f"failed to write scaffold files: {exc}") from exc

    try:
        post_report = pv.validate_tree(root_path)
    except pv.ValidatorInternalError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ScaffoldInternalError(f"post-write validation failed: {exc}") from exc

    scoped_errors, scoped_warnings = _scoped_counts(post_report, prefix=f"{package_id}/")
    if scoped_errors > 0:
        shutil.rmtree(target, ignore_errors=True)
        return ScaffoldResult(
            schema_version=SCHEMA_VERSION,
            status="REFUSED",
            package_id=package_id,
            target=str(target_resolved),
            files=(),
            validation_errors=scoped_errors,
            validation_warnings=scoped_warnings,
            refusal_reason="generated package failed post-write validation",
        )

    return ScaffoldResult(
        schema_version=SCHEMA_VERSION,
        status="CREATED",
        package_id=package_id,
        target=str(target_resolved),
        files=_SCAFFOLD_FILES,
        validation_errors=scoped_errors,
        validation_warnings=scoped_warnings,
    )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _format_text(result: ScaffoldResult) -> str:
    lines = [f"Package Scaffold v1 — status={result.status} package_id={result.package_id}"]
    if result.status == "REFUSED":
        lines.append(f"refused: {result.refusal_reason}")
    else:
        lines.append(f"target: {result.target}")
        for name in result.files:
            lines.append(f"  + {name}")
    lines.append(
        f"validation: {result.validation_errors} error(s), "
        f"{result.validation_warnings} warning(s)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-package-new")
    parser.add_argument("package_id")
    parser.add_argument("--root", required=True)
    parser.add_argument("--tool", action="append", default=None)
    parser.add_argument("--package-version", default=DEFAULT_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    tools = args.tool if args.tool is not None else []

    try:
        result = scaffold_package(
            args.package_id,
            root=args.root,
            tools=tools,
            version=args.package_version,
            dry_run=args.dry_run,
        )
    except ScaffoldInternalError as exc:
        print(f"terminux-package-new: internal error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 真正未預期的錯誤，不得誤報成 REFUSED
        print(f"terminux-package-new: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_format_text(result))

    return 0 if result.status in ("CREATED", "DRY_RUN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
