# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Doctor / CI Integration v1。

編排既有三項工具（Validator／Scaffold-produced tree／Local Test
Harness），提供一個統一、deterministic 的 package health check 與
repository CI 可直接呼叫的穩定入口。**不建立第四套 manifest／case／
assertion 判斷邏輯**——Stage 1 只呼叫 `validator.validate_tree()` 與
`test_harness.parse_case_file()`；Stage 2 只呼叫
`test_harness.run_case()`，本模組完全不重新解析 YAML、不重新定義
assertion 語意。
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminux_engine.package_runtime import test_harness as th
from terminux_engine.package_runtime import validator as pv

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = th.DEFAULT_TIMEOUT
CASE_DIR_NAME = "package-tests"


class DoctorInternalError(Exception):
    """CLI usage／無法啟動的錯誤，對應 exit code 2。"""


@dataclass(frozen=True)
class Diagnostic:
    severity: str  # "ERROR" | "WARNING"
    code: str
    package_id: str | None
    case_name: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "package_id": self.package_id,
            "case_name": self.case_name,
            "message": self.message,
        }


@dataclass(frozen=True)
class CaseEntry:
    package_id: str
    name: str
    case_path: str  # normalized "<ROOT>/..."
    status: str  # "PASS" | "FAIL" | "DISCOVERED_NOT_EXECUTED"
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "name": self.name,
            "case_path": self.case_path,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class PackageEntry:
    package_id: str
    valid: bool
    error_count: int
    warning_count: int
    case_count: int
    executed_case_count: int
    passed_case_count: int
    failed_case_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "valid": self.valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "case_count": self.case_count,
            "executed_case_count": self.executed_case_count,
            "passed_case_count": self.passed_case_count,
            "failed_case_count": self.failed_case_count,
        }


@dataclass(frozen=True)
class DoctorReport:
    root_display: str
    mode: str  # "CHECK_ONLY" | "RUN_CASES"
    packages: tuple[PackageEntry, ...]
    cases: tuple[CaseEntry, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def status(self) -> str:
        return "PASS" if self.exit_code == 0 else "FAIL"

    @property
    def exit_code(self) -> int:
        if any(d.severity == "ERROR" for d in self.diagnostics):
            return 1
        if any(c.status == "FAIL" for c in self.cases):
            return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        executed = [c for c in self.cases if c.status in ("PASS", "FAIL")]
        passed = [c for c in executed if c.status == "PASS"]
        failed = [c for c in executed if c.status == "FAIL"]
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "root": self.root_display,
            "mode": self.mode,
            "summary": {
                "packages": len(self.packages),
                "valid_packages": sum(1 for p in self.packages if p.valid),
                "invalid_packages": sum(1 for p in self.packages if not p.valid),
                "discovered_cases": len(self.cases),
                "executed_cases": len(executed),
                "passed_cases": len(passed),
                "failed_cases": len(failed),
                "warnings": sum(1 for d in self.diagnostics if d.severity == "WARNING"),
                "errors": sum(1 for d in self.diagnostics if d.severity == "ERROR"),
            },
            "packages": [p.to_dict() for p in self.packages],
            "cases": [c.to_dict() for c in self.cases],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _normalize(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
        return f"<ROOT>/{rel}"
    except ValueError:
        return "<ROOT>"


def _discover_package_dirs(root: Path) -> list[Path]:
    return [
        entry
        for entry in sorted(root.iterdir(), key=lambda p: p.name)
        if entry.is_dir() and (entry / "package.yaml").is_file()
    ]


def _discover_case_files(package_dir: Path) -> list[Path]:
    case_dir = package_dir / CASE_DIR_NAME
    if not case_dir.is_dir():
        return []
    files = [p for p in case_dir.glob("*.yaml") if p.is_file()]
    files += [p for p in case_dir.glob("*.yml") if p.is_file()]
    return sorted(files, key=lambda p: p.name)


def run_doctor(
    root: Path,
    *,
    run_cases: bool,
    require_cases: bool,
    timeout: float,
) -> DoctorReport:
    """執行完整 Stage 1（＋視旗標決定的 Stage 2），回傳結構化報告。"""
    diagnostics: list[Diagnostic] = []
    package_entries: list[PackageEntry] = []
    case_entries: list[CaseEntry] = []

    try:
        report = pv.validate_tree(root)
    except pv.ValidatorInternalError as exc:
        raise DoctorInternalError(f"root validation failed: {exc}") from exc

    if report.error_count > 0:
        diagnostics.append(Diagnostic(
            severity="ERROR",
            code="PACKAGE_TREE_INVALID",
            package_id=None,
            case_name=None,
            message=f"{report.error_count} validation error(s) found in package tree",
        ))
    for finding in report.findings:
        if finding.severity == "WARNING":
            diagnostics.append(Diagnostic(
                severity="WARNING",
                code="PACKAGE_VALIDATION_WARNING",
                package_id=finding.package_id,
                case_name=None,
                message=f"{finding.code}: {finding.message}",
            ))

    findings_by_dir: dict[str, list[pv.Finding]] = {}
    for finding in report.findings:
        prefix = finding.manifest_path.split("/", 1)[0]
        findings_by_dir.setdefault(prefix, []).append(finding)

    package_dirs = _discover_package_dirs(root)

    stage1_case_map: dict[str, list[tuple[Path, th.Case | None, th.CaseLoadError | None]]] = {}

    for pkg_dir in package_dirs:
        package_id = pkg_dir.name
        pkg_findings = findings_by_dir.get(package_id, [])
        error_count = sum(1 for f in pkg_findings if f.severity == "ERROR")
        warning_count = sum(1 for f in pkg_findings if f.severity == "WARNING")
        valid = error_count == 0

        case_files = _discover_case_files(pkg_dir)
        parsed_entries: list[tuple[Path, th.Case | None, th.CaseLoadError | None]] = []
        seen_names: dict[str, Path] = {}
        for case_path in case_files:
            try:
                case = th.parse_case_file(case_path, default_timeout=timeout)
            except th.CaseLoadError as exc:
                parsed_entries.append((case_path, None, exc))
                diagnostics.append(Diagnostic(
                    severity="ERROR",
                    code=exc.code,
                    package_id=package_id,
                    case_name=case_path.stem,
                    message=exc.message,
                ))
                continue
            if case.name in seen_names:
                diagnostics.append(Diagnostic(
                    severity="ERROR",
                    code="DUPLICATE_CASE_NAME",
                    package_id=package_id,
                    case_name=case.name,
                    message=(
                        f"case name {case.name!r} already used by "
                        f"{seen_names[case.name].name!r} in the same package"
                    ),
                ))
                parsed_entries.append((case_path, None, None))
                continue
            seen_names[case.name] = case_path
            parsed_entries.append((case_path, case, None))

        valid_case_count = sum(1 for _, c, e in parsed_entries if c is not None)
        stage1_case_map[package_id] = parsed_entries

        if valid_case_count == 0:
            if require_cases:
                diagnostics.append(Diagnostic(
                    severity="ERROR",
                    code="PACKAGE_CASES_REQUIRED",
                    package_id=package_id,
                    case_name=None,
                    message=f"package {package_id!r} has no local test cases (--require-cases)",
                ))
            else:
                diagnostics.append(Diagnostic(
                    severity="WARNING",
                    code="NO_LOCAL_CASES",
                    package_id=package_id,
                    case_name=None,
                    message=f"package {package_id!r} has no local test cases under {CASE_DIR_NAME}/",
                ))

        package_entries.append(PackageEntry(
            package_id=package_id,
            valid=valid,
            error_count=error_count,
            warning_count=warning_count,
            case_count=valid_case_count,
            executed_case_count=0,
            passed_case_count=0,
            failed_case_count=0,
        ))

    stage1_has_error = any(d.severity == "ERROR" for d in diagnostics)
    execute = run_cases and not stage1_has_error

    updated_packages: list[PackageEntry] = []
    for pkg_entry in package_entries:
        entries = stage1_case_map.get(pkg_entry.package_id, [])
        executed_count = 0
        passed_count = 0
        failed_count = 0
        for case_path, case, _err in entries:
            if case is None:
                continue
            display_path = _normalize(case_path, root)
            if execute and pkg_entry.case_count > 0:
                outcome = th.run_case(root, pkg_entry.package_id, case_path, default_timeout=timeout)
                executed_count += 1
                if outcome.status == "PASS":
                    passed_count += 1
                else:
                    failed_count += 1
                    code = "CASE_TIMEOUT" if "CASE_TIMEOUT" in outcome.reason_codes else "CASE_EXECUTION_FAILED"
                    diagnostics.append(Diagnostic(
                        severity="ERROR",
                        code=code,
                        package_id=pkg_entry.package_id,
                        case_name=case.name,
                        message=f"case failed: {', '.join(outcome.reason_codes)}",
                    ))
                case_entries.append(CaseEntry(
                    package_id=pkg_entry.package_id,
                    name=case.name,
                    case_path=display_path,
                    status=outcome.status,
                    reason_codes=outcome.reason_codes,
                ))
            else:
                if not stage1_has_error:
                    diagnostics.append(Diagnostic(
                        severity="WARNING",
                        code="CASE_DISCOVERED_NOT_EXECUTED",
                        package_id=pkg_entry.package_id,
                        case_name=case.name,
                        message="case discovered but not executed (pass --run-cases to execute)",
                    ))
                case_entries.append(CaseEntry(
                    package_id=pkg_entry.package_id,
                    name=case.name,
                    case_path=display_path,
                    status="DISCOVERED_NOT_EXECUTED",
                    reason_codes=(),
                ))

        updated_packages.append(PackageEntry(
            package_id=pkg_entry.package_id,
            valid=pkg_entry.valid,
            error_count=pkg_entry.error_count,
            warning_count=pkg_entry.warning_count,
            case_count=pkg_entry.case_count,
            executed_case_count=executed_count,
            passed_case_count=passed_count,
            failed_case_count=failed_count,
        ))

    mode = "RUN_CASES" if run_cases else "CHECK_ONLY"
    return DoctorReport(
        root_display="<ROOT>",
        mode=mode,
        packages=tuple(updated_packages),
        cases=tuple(case_entries),
        diagnostics=tuple(diagnostics),
    )


# ─── Text output ───────────────────────────────────────────────────────────


def format_text(report: DoctorReport) -> str:
    lines = ["Package Doctor v1", ""]
    cases_required_pkgs = {
        d.package_id for d in report.diagnostics if d.code == "PACKAGE_CASES_REQUIRED"
    }
    error_codes_by_pkg: dict[str, list[str]] = {}
    for d in report.diagnostics:
        if d.severity == "ERROR" and d.package_id:
            error_codes_by_pkg.setdefault(d.package_id, []).append(d.code)

    for pkg in report.packages:
        pkg_errors = error_codes_by_pkg.get(pkg.package_id, [])
        if not pkg.valid:
            tag, detail = "FAIL", "[INVALID]"
        elif pkg.failed_case_count > 0:
            tag = "FAIL"
            detail = f"[{pkg.case_count} cases, {pkg.passed_case_count} passed]"
        elif pkg.case_count == 0 and pkg.package_id in cases_required_pkgs:
            tag, detail = "FAIL", "[PACKAGE_CASES_REQUIRED]"
        elif [c for c in pkg_errors if c not in ("PACKAGE_CASES_REQUIRED",)]:
            tag, detail = "FAIL", f"[{pkg_errors[0]}]"
        elif pkg.case_count == 0:
            tag, detail = "WARN", "[NO_LOCAL_CASES]"
        elif pkg.executed_case_count > 0:
            tag = "PASS"
            detail = f"[{pkg.case_count} cases, {pkg.passed_case_count} passed]"
        else:
            tag, detail = "PASS", f"[{pkg.case_count} cases]"
        lines.append(f"{tag} {pkg.package_id} {detail}")

    total_packages = len(report.packages)
    valid_packages = sum(1 for p in report.packages if p.valid)
    lines.append("")
    lines.append(f"{total_packages} packages: {valid_packages} valid, {total_packages - valid_packages} invalid")

    executed = [c for c in report.cases if c.status in ("PASS", "FAIL")]
    passed = sum(1 for c in executed if c.status == "PASS")
    lines.append(f"{len(executed)} cases: {passed} passed, {len(executed) - passed} failed")

    warning_count = sum(1 for d in report.diagnostics if d.severity == "WARNING")
    lines.append(f"{warning_count} warnings")
    return "\n".join(lines)


# ─── JUnit XML ─────────────────────────────────────────────────────────────


def build_junit_xml(report: DoctorReport) -> ET.Element:
    total = 1 + len(report.cases)
    failures = sum(1 for d in report.diagnostics if d.code == "PACKAGE_TREE_INVALID")
    failures += sum(1 for c in report.cases if c.status == "FAIL")
    skipped = sum(1 for c in report.cases if c.status == "DISCOVERED_NOT_EXECUTED")

    suite = ET.Element("testsuite", {
        "name": "package_doctor",
        "tests": str(total),
        "failures": str(failures),
        "errors": "0",
        "skipped": str(skipped),
        "time": "0",
    })

    tree_case = ET.SubElement(suite, "testcase", {
        "classname": "package_doctor.tree",
        "name": "validate_tree",
        "time": "0",
    })
    tree_diag = [d for d in report.diagnostics if d.code == "PACKAGE_TREE_INVALID"]
    if tree_diag:
        failure = ET.SubElement(tree_case, "failure", {"message": tree_diag[0].message})
        failure.text = tree_diag[0].message

    for case in report.cases:
        tc = ET.SubElement(suite, "testcase", {
            "classname": f"package_doctor.{case.package_id}",
            "name": case.name,
            "time": "0",
        })
        if case.status == "FAIL":
            message = ", ".join(case.reason_codes) or "case failed"
            failure = ET.SubElement(tc, "failure", {"message": message})
            failure.text = message
        elif case.status == "DISCOVERED_NOT_EXECUTED":
            ET.SubElement(tc, "skipped", {"message": "discovered but not executed"})

    return suite


def write_junit_xml(report: DoctorReport, output_path: Path) -> None:
    root_el = build_junit_xml(report)
    tree = ET.ElementTree(root_el)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-package-doctor")
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-cases", action="store_true")
    parser.add_argument("--require-cases", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--junitxml", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        print("terminux-package-doctor: --timeout must be positive", file=sys.stderr)
        return 2

    root_path = Path(args.root)
    if not root_path.is_dir():
        print(f"terminux-package-doctor: root is not a directory: {root_path}", file=sys.stderr)
        return 2

    junit_path: Path | None = None
    if args.junitxml is not None:
        junit_path = Path(args.junitxml)
        if not junit_path.parent.is_dir():
            print(
                f"terminux-package-doctor: --junitxml parent directory does not exist: {junit_path.parent}",
                file=sys.stderr,
            )
            return 2

    try:
        report = run_doctor(
            root_path,
            run_cases=args.run_cases,
            require_cases=args.require_cases,
            timeout=args.timeout,
        )
    except DoctorInternalError as exc:
        print(f"terminux-package-doctor: internal error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 真正未預期的錯誤，不得誤報成 diagnostic
        print(f"terminux-package-doctor: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    if junit_path is not None:
        try:
            write_junit_xml(report, junit_path)
        except OSError as exc:
            print(f"terminux-package-doctor: failed to write JUnit XML: {exc}", file=sys.stderr)
            return 2

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text(report))

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
