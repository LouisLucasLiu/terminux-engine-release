# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Package Local Test Harness v1。

在不啟動 daemon 的情況下，用 production manifest loader、entrypoint
loader、`PackageDispatcher`／`DispatchEngine`（`EngineMode.STANDALONE`）
與 disposable services，對單一 package 的 Python `after` phase 執行
一個或多個 deterministic case，驗證 `PhaseResult` 與其副作用 requests。

**重要信任邊界（不得誇大）**：Package phase 是受信任的 developer Python
code。Child process 提供的是 timeout／crash-SystemExit 隔離／module
state 隔離／環境變數最小化，**不是** security sandbox。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.package_runtime import validator as pv
from terminux_engine.package_runtime.loader import PackageLoader
from terminux_engine.package_runtime.manifest import load_manifest

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 10.0
FIXED_TIMESTAMP = "1970-01-01T00:00:00+00:00"
DEFAULT_TERMINAL_ID = "package-test"
DEFAULT_SOURCE = "test_harness"
DEFAULT_USER = "package-test"
DEFAULT_HOST = "localhost"

REASON_CODES = frozenset({
    "ROOT_VALIDATION_FAILED",
    "PACKAGE_NOT_FOUND",
    "PACKAGE_ID_AMBIGUOUS",
    "AFTER_PHASE_MISSING",
    "UNSUPPORTED_PHASE_KIND",
    "CASE_SCHEMA_INVALID",
    "CASE_PATH_ESCAPE",
    "CASE_TIMEOUT",
    "PHASE_EXECUTION_ERROR",
    "PHASE_STATUS_MISMATCH",
    "MESSAGE_MISMATCH",
    "FACTS_MISMATCH",
    "EVIDENCE_MISMATCH",
    "RECORDS_MISMATCH",
    "NOTIFICATIONS_MISMATCH",
    "REPORTS_MISMATCH",
    "WORKSPACE_MISMATCH",
    "INTERNAL_ERROR",
    "PACKAGE_NOT_DISPATCHED",
    "CASE_DQE_DB_PATH_OUTSIDE_WORKSPACE",
    "CASE_DQE_DB_PATH_INVALID",
    "CASE_DQE_DB_PATH_SYMLINK_ESCAPE",
})

_HOST_GENERATED_EVENT_FIELDS = frozenset({"event_id", "cwd", "user", "host", "timestamp"})
_VALID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


class HarnessInternalError(Exception):
    """CLI usage／harness 無法啟動的錯誤，對應 exit code 2。"""


class CaseLoadError(Exception):
    """Case 檔案格式或路徑問題，對應一個 stable reason code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─── Case model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Case:
    name: str
    source_path: Path
    workspace_files: dict[str, str]
    event_command: str
    event_argv: list[str]
    event_tool_name: str
    event_source: str
    event_terminal_id: str
    event_exit_code: int | None
    event_stdout_tail: list[str]
    event_metadata: dict[str, Any]
    timeout: float
    expect_status: str
    expect_message: str | None
    expect_message_contains: str | None
    expect_facts_contains: dict[str, Any]
    expect_evidence_contains: dict[str, Any]
    expect_records: list[dict[str, Any]]
    expect_notifications: list[dict[str, Any]]
    expect_reports: list[dict[str, Any]]
    expect_files_exist: list[str]
    expect_files_absent: list[str]
    expect_file_text: dict[str, str]
    expect_file_text_contains: dict[str, str]


def _check_relative_safe(path_str: Any, *, label: str) -> str:
    if not isinstance(path_str, str) or not path_str:
        raise CaseLoadError("CASE_SCHEMA_INVALID", f"{label} must be a non-empty string")
    p = Path(path_str)
    if p.is_absolute():
        raise CaseLoadError("CASE_PATH_ESCAPE", f"{label} must not be an absolute path: {path_str}")
    if ".." in p.parts:
        raise CaseLoadError("CASE_PATH_ESCAPE", f"{label} must not contain '..': {path_str}")
    return path_str


def _normalize_case_name(name: str) -> str:
    if not name or any(ch not in _VALID_NAME_CHARS for ch in name):
        raise CaseLoadError(
            "CASE_SCHEMA_INVALID",
            f"case name must be non-empty and use only [a-zA-Z0-9_-]: {name!r}",
        )
    return name


def parse_case_file(path: Path, *, default_timeout: float) -> Case:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseLoadError("CASE_SCHEMA_INVALID", f"cannot read case file: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CaseLoadError("CASE_SCHEMA_INVALID", f"malformed case YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "case root must be a mapping")

    if raw.get("schema") != SCHEMA_VERSION:
        raise CaseLoadError("CASE_SCHEMA_INVALID", f"unsupported case schema: {raw.get('schema')!r}")

    name = raw.get("name")
    if not isinstance(name, str):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "case name is required")
    name = _normalize_case_name(name)

    workspace_raw = raw.get("workspace") or {}
    if not isinstance(workspace_raw, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "workspace must be a mapping")
    files_raw = workspace_raw.get("files") or {}
    if not isinstance(files_raw, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "workspace.files must be a mapping")
    workspace_files: dict[str, str] = {}
    for rel_path, content in files_raw.items():
        _check_relative_safe(rel_path, label="workspace.files key")
        if not isinstance(content, str):
            raise CaseLoadError("CASE_SCHEMA_INVALID", f"workspace.files[{rel_path!r}] must be text")
        workspace_files[rel_path] = content

    event_raw = raw.get("event")
    if not isinstance(event_raw, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event is required")
    overridden = _HOST_GENERATED_EVENT_FIELDS & set(event_raw.keys())
    if overridden:
        raise CaseLoadError(
            "CASE_SCHEMA_INVALID",
            f"event fields are host-generated and must not be set by case: {sorted(overridden)}",
        )
    command = event_raw.get("command")
    if not isinstance(command, str) or not command:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.command is required")
    tool_name = event_raw.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.tool_name is required")
    argv = event_raw.get("argv", [command])
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.argv must be a list of strings")
    exit_code = event_raw.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.exit_code must be an integer")
    stdout_tail = event_raw.get("stdout_tail", [])
    if not isinstance(stdout_tail, list) or not all(isinstance(s, str) for s in stdout_tail):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.stdout_tail must be a list of strings")
    metadata = event_raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.metadata must be a mapping")
    event_source = event_raw.get("source", DEFAULT_SOURCE)
    if not isinstance(event_source, str) or not event_source:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.source must be a non-empty string")
    terminal_id = event_raw.get("terminal_id", DEFAULT_TERMINAL_ID)
    if not isinstance(terminal_id, str) or not terminal_id:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "event.terminal_id must be a non-empty string")

    timeout_raw = raw.get("timeout", default_timeout)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "timeout must be numeric") from exc
    if timeout <= 0:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "timeout must be positive")

    expect_raw = raw.get("expect")
    if not isinstance(expect_raw, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "expect is required")
    status = expect_raw.get("status")
    if status not in ("noop", "ok", "warn", "fail"):
        raise CaseLoadError("CASE_SCHEMA_INVALID", f"expect.status must be one of noop/ok/warn/fail, got {status!r}")
    message = expect_raw.get("message")
    message_contains = expect_raw.get("message_contains")
    if message is not None and message_contains is not None:
        raise CaseLoadError("CASE_SCHEMA_INVALID", "expect.message and expect.message_contains are mutually exclusive")
    if message is not None and not isinstance(message, str):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "expect.message must be a string")
    if message_contains is not None and not isinstance(message_contains, str):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "expect.message_contains must be a string")

    facts_contains = expect_raw.get("facts_contains", {})
    evidence_contains = expect_raw.get("evidence_contains", {})
    if not isinstance(facts_contains, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "expect.facts_contains must be a mapping")
    if not isinstance(evidence_contains, dict):
        raise CaseLoadError("CASE_SCHEMA_INVALID", "expect.evidence_contains must be a mapping")

    records = expect_raw.get("records", [])
    notifications = expect_raw.get("notifications", [])
    reports = expect_raw.get("reports", [])
    for field_name, value in (("records", records), ("notifications", notifications), ("reports", reports)):
        if not isinstance(value, list):
            raise CaseLoadError("CASE_SCHEMA_INVALID", f"expect.{field_name} must be a list")

    files_exist = expect_raw.get("files_exist", [])
    files_absent = expect_raw.get("files_absent", [])
    for field_name, value in (("files_exist", files_exist), ("files_absent", files_absent)):
        if not isinstance(value, list):
            raise CaseLoadError("CASE_SCHEMA_INVALID", f"expect.{field_name} must be a list")
        for p in value:
            _check_relative_safe(p, label=f"expect.{field_name} entry")

    file_text = expect_raw.get("file_text", {})
    file_text_contains = expect_raw.get("file_text_contains", {})
    for field_name, value in (("file_text", file_text), ("file_text_contains", file_text_contains)):
        if not isinstance(value, dict):
            raise CaseLoadError("CASE_SCHEMA_INVALID", f"expect.{field_name} must be a mapping")
        for p in value:
            _check_relative_safe(p, label=f"expect.{field_name} key")

    return Case(
        name=name,
        source_path=path,
        workspace_files=workspace_files,
        event_command=command,
        event_argv=list(argv),
        event_tool_name=tool_name,
        event_source=event_source,
        event_terminal_id=terminal_id,
        event_exit_code=exit_code,
        event_stdout_tail=list(stdout_tail),
        event_metadata=dict(metadata),
        timeout=timeout,
        expect_status=status,
        expect_message=message,
        expect_message_contains=message_contains,
        expect_facts_contains=facts_contains,
        expect_evidence_contains=evidence_contains,
        expect_records=records,
        expect_notifications=notifications,
        expect_reports=reports,
        expect_files_exist=list(files_exist),
        expect_files_absent=list(files_absent),
        expect_file_text=dict(file_text),
        expect_file_text_contains=dict(file_text_contains),
    )


# ─── Recursive subset comparison ─────────────────────────────────────────────


def _contains(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and _contains(value, actual[key]) for key, value in expected.items())
    return expected == actual


# ─── Package lookup ───────────────────────────────────────────────────────────


def find_package_dir(root: Path, package_id: str) -> Path:
    """在（已確認乾淨的）root 內找唯一宣告 package_id 的目錄。"""
    matches: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        manifest_path = entry / "package.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_manifest(manifest_path)
        except Exception:
            continue
        if manifest.package_id == package_id:
            matches.append(entry)
    if not matches:
        raise CaseLoadError("PACKAGE_NOT_FOUND", f"no package with id {package_id!r} found under {root}")
    if len(matches) > 1:
        raise CaseLoadError(
            "PACKAGE_ID_AMBIGUOUS",
            f"multiple package directories declare id {package_id!r}: {[str(m) for m in matches]}",
        )
    return matches[0]


# ─── Workspace diff ───────────────────────────────────────────────────────────


def _snapshot_workspace(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file():
            rel = path.relative_to(workspace).as_posix()
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _diff_workspace(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    diff: list[dict[str, str]] = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            diff.append({"path": f"<WORKSPACE>/{rel}", "change": "added"})
        elif rel not in after:
            diff.append({"path": f"<WORKSPACE>/{rel}", "change": "removed"})
        elif before[rel] != after[rel]:
            diff.append({"path": f"<WORKSPACE>/{rel}", "change": "modified"})
    return diff


# ─── Assertion evaluation (pure) ──────────────────────────────────────────────


@dataclass(frozen=True)
class CaseOutcome:
    name: str
    status: str  # "PASS" | "FAIL"
    reason_codes: tuple[str, ...]
    phase_status: str | None
    records: tuple[dict[str, Any], ...]
    notifications: tuple[dict[str, Any], ...]
    reports: tuple[dict[str, Any], ...]
    workspace_diff: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "phase_status": self.phase_status,
            "records": [dict(r) for r in self.records],
            "notifications": [dict(n) for n in self.notifications],
            "reports": [dict(r) for r in self.reports],
            "workspace_diff": [dict(d) for d in self.workspace_diff],
        }


def _fail(name: str, code: str, *, facts: dict[str, Any] | None = None, **extra: Any) -> CaseOutcome:
    return CaseOutcome(
        name=name,
        status="FAIL",
        reason_codes=(code,),
        phase_status=extra.get("phase_status"),
        records=tuple(extra.get("records", ())),
        notifications=tuple(extra.get("notifications", ())),
        reports=tuple(extra.get("reports", ())),
        workspace_diff=tuple(extra.get("workspace_diff", ())),
    )


def evaluate_case(
    case: Case,
    *,
    worker_facts: dict[str, Any] | None,
    workspace_diff: list[dict[str, str]],
    workspace_after: Path,
) -> CaseOutcome:
    """純函式：對 worker 回報的執行事實比對 case 的 expect 區塊。"""
    if worker_facts is None:
        return _fail(case.name, "PHASE_EXECUTION_ERROR", workspace_diff=workspace_diff)

    if worker_facts.get("not_dispatched"):
        return _fail(case.name, "PACKAGE_NOT_DISPATCHED", workspace_diff=workspace_diff)

    phase_status = worker_facts.get("phase_status")
    records = worker_facts.get("records", [])
    notifications = worker_facts.get("notifications", [])
    reports = worker_facts.get("reports", [])

    reason_codes: list[str] = []

    if phase_status == "error" or phase_status is None:
        reason_codes.append("PHASE_EXECUTION_ERROR")
    elif phase_status != case.expect_status:
        reason_codes.append("PHASE_STATUS_MISMATCH")

    if not reason_codes:
        message = worker_facts.get("message")
        if case.expect_message is not None and message != case.expect_message:
            reason_codes.append("MESSAGE_MISMATCH")
        if case.expect_message_contains is not None and (
            message is None or case.expect_message_contains not in message
        ):
            reason_codes.append("MESSAGE_MISMATCH")

        facts = worker_facts.get("facts", {})
        if not _contains(case.expect_facts_contains, facts):
            reason_codes.append("FACTS_MISMATCH")

        evidence = worker_facts.get("evidence", {})
        if not _contains(case.expect_evidence_contains, evidence):
            reason_codes.append("EVIDENCE_MISMATCH")

        if not _records_match(case.expect_records, records):
            reason_codes.append("RECORDS_MISMATCH")

        if not _notifications_match(case.expect_notifications, notifications):
            reason_codes.append("NOTIFICATIONS_MISMATCH")

        if not _reports_match(case.expect_reports, reports):
            reason_codes.append("REPORTS_MISMATCH")

        if not _workspace_matches(case, workspace_after):
            reason_codes.append("WORKSPACE_MISMATCH")

    status = "PASS" if not reason_codes else "FAIL"
    return CaseOutcome(
        name=case.name,
        status=status,
        reason_codes=tuple(reason_codes),
        phase_status=phase_status,
        records=tuple(records),
        notifications=tuple(notifications),
        reports=tuple(reports),
        workspace_diff=tuple(workspace_diff),
    )


def _records_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    if len(expected) != len(actual):
        return False
    for exp, act in zip(expected, actual):
        for field_name in ("namespace", "record_type", "key"):
            if field_name in exp and exp[field_name] != act.get(field_name):
                return False
        if "payload_contains" in exp and not _contains(exp["payload_contains"], act.get("payload", {})):
            return False
    return True


def _notifications_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    if len(expected) != len(actual):
        return False
    for exp, act in zip(expected, actual):
        for field_name in ("level", "title", "message"):
            if field_name in exp and exp[field_name] != act.get(field_name):
                return False
        if "message_contains" in exp and exp["message_contains"] not in (act.get("message") or ""):
            return False
        if "actions" in exp:
            actual_names = [a.get("name") for a in act.get("actions", [])]
            if list(exp["actions"]) != actual_names:
                return False
    return True


def _reports_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    if len(expected) != len(actual):
        return False
    for exp, act in zip(expected, actual):
        for field_name in ("report_id", "title", "report_path", "template", "output_name"):
            if field_name in exp and exp[field_name] != act.get(field_name):
                return False
        if "data_contains" in exp and not _contains(exp["data_contains"], act.get("data", {})):
            return False
    return True


def _workspace_matches(case: Case, workspace: Path) -> bool:
    for rel in case.expect_files_exist:
        if not (workspace / rel).is_file():
            return False
    for rel in case.expect_files_absent:
        if (workspace / rel).exists():
            return False
    for rel, expected_text in case.expect_file_text.items():
        target = workspace / rel
        if not target.is_file() or target.read_text(encoding="utf-8", errors="replace") != expected_text:
            return False
    for rel, needle in case.expect_file_text_contains.items():
        target = workspace / rel
        if not target.is_file() or needle not in target.read_text(encoding="utf-8", errors="replace"):
            return False
    return True


# ─── Per-case orchestration (parent side) ─────────────────────────────────────


def _child_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # DQE Test Isolation Enforcement v1：child 永遠 deny production DQE DB
        # 的 default fallback（見 terminux_engine.dqe.db_policy）。worker 也
        # 一律注入 per-case disposable manager（見 run_case() 的 dqe_db_path／
        # _harness_worker.py），所以正常執行不會走到這個 policy——這是
        # defense-in-depth，不是主要機制。host 的 TERMINUX_DQE_DB_PATH 本來
        # 就不在上面白名單內，不會被繼承。
        "TERMINUX_DQE_DEFAULT_DB_POLICY": "deny",
    }


def _validate_case_dqe_db_path(case: Case, workspace: Path) -> str | None:
    """驗證 case 若自行提供 `event.metadata['dqe_db_path']`，是否安全地落在
    該 case 的 disposable workspace 內。

    這個值目前**不會**被實際使用（`services.dqe_db_manager` 一律優先注入，
    見 run_case()），但仍在 handler 執行前驗證——防止未來重構意外移除
    services 注入時，這個欄位變成可逃出 workspace 的寫入路徑。回傳 reason
    code（驗證失敗）或 None（安全，或 case 根本沒有提供這個欄位）。
    """
    value = case.event_metadata.get("dqe_db_path")
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(ord(ch) < 0x20 for ch in value):
        return "CASE_DQE_DB_PATH_INVALID"

    candidate = Path(value)
    target = candidate if candidate.is_absolute() else workspace / candidate

    # 不解析 symlink 的純字面 containment 檢查（抓 plain ".." 或 absolute escape）。
    normalized = Path(os.path.normpath(str(target)))
    normalized_workspace = Path(os.path.normpath(str(workspace)))
    try:
        normalized.relative_to(normalized_workspace)
        plain_contained = True
    except ValueError:
        plain_contained = False

    try:
        resolved_target = target.resolve()
        resolved_workspace = workspace.resolve()
    except (OSError, RuntimeError):
        return "CASE_DQE_DB_PATH_INVALID"

    try:
        resolved_target.relative_to(resolved_workspace)
    except ValueError:
        # 字面上像是落在 workspace 內，但解析 symlink 後逃出——symlink escape；
        # 否則是一開始就在 workspace 外（absolute path 或 ".." traversal）。
        return "CASE_DQE_DB_PATH_SYMLINK_ESCAPE" if plain_contained else "CASE_DQE_DB_PATH_OUTSIDE_WORKSPACE"

    if resolved_target.exists() and resolved_target.is_dir():
        return "CASE_DQE_DB_PATH_INVALID"
    return None


def run_case(root: Path, package_id: str, case_path: Path, *, default_timeout: float) -> CaseOutcome:
    """對單一 case 執行完整流程（每個 case 皆從乾淨狀態獨立開始）。"""
    try:
        case = parse_case_file(case_path, default_timeout=default_timeout)
    except CaseLoadError as exc:
        return _fail(case_path.stem, exc.code)

    try:
        report = pv.validate_tree(root)
    except pv.ValidatorInternalError as exc:
        return _fail(case.name, "ROOT_VALIDATION_FAILED")
    if report.error_count > 0:
        return _fail(case.name, "ROOT_VALIDATION_FAILED")

    try:
        package_dir = find_package_dir(root, package_id)
    except CaseLoadError as exc:
        return _fail(case.name, exc.code)

    manifest = load_manifest(package_dir / "package.yaml")
    if not manifest.has_phase("after"):
        return _fail(case.name, "AFTER_PHASE_MISSING")
    if manifest.is_prompt_phase("after"):
        return _fail(case.name, "UNSUPPORTED_PHASE_KIND")

    work_root = Path(tempfile.mkdtemp(prefix="terminux_package_test_"))
    try:
        workspace = work_root / "workspace"
        workspace.mkdir()
        for rel_path, content in case.workspace_files.items():
            target = workspace / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        before_snapshot = _snapshot_workspace(workspace)

        dqe_db_path_reason = _validate_case_dqe_db_path(case, workspace)
        if dqe_db_path_reason is not None:
            return _fail(case.name, dqe_db_path_reason)

        store_db = work_root / "store.db"
        # DQE Test Isolation Enforcement v1：無條件建立 per-case disposable
        # DQE DB，透過 services.dqe_db_manager 注入（見 _harness_worker.py），
        # 不判斷 package 是否真的會寫 DQE DB。case 完全不需要記得提供
        # dqe_db_path。
        dqe_db_path = work_root / ".terminux-test" / "dqe.sqlite"

        normalized_name = case.name
        event_spec = {
            "event_id": f"test:{package_id}:{normalized_name}",
            "trace_id": None,
            "command": case.event_command,
            "argv": case.event_argv,
            "cwd": str(workspace),
            "tool_name": case.event_tool_name,
            "user": DEFAULT_USER,
            "host": DEFAULT_HOST,
            "terminal_id": case.event_terminal_id,
            "source": case.event_source,
            "timestamp": FIXED_TIMESTAMP,
            "exit_code": case.event_exit_code,
            "stdout_tail": case.event_stdout_tail,
            "metadata": case.event_metadata,
        }
        spec = {
            "package_id": package_id,
            "package_dir": str(package_dir.resolve()),
            "store_db": str(store_db),
            "dqe_db_path": str(dqe_db_path),
            "event": event_spec,
        }
        spec_path = work_root / "spec.json"
        result_path = work_root / "result.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        worker_module = "terminux_engine.package_runtime._harness_worker"
        try:
            proc = subprocess.run(
                [sys.executable, "-u", "-m", worker_module, str(spec_path), str(result_path)],
                env=_child_env(),
                cwd=str(workspace),
                timeout=case.timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            after_snapshot = _snapshot_workspace(workspace)
            diff = _diff_workspace(before_snapshot, after_snapshot)
            return _fail(case.name, "CASE_TIMEOUT", workspace_diff=diff)

        worker_facts: dict[str, Any] | None = None
        if proc.returncode == 0 and result_path.is_file():
            try:
                worker_facts = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                worker_facts = None

        after_snapshot = _snapshot_workspace(workspace)
        diff = _diff_workspace(before_snapshot, after_snapshot)

        return evaluate_case(
            case,
            worker_facts=worker_facts,
            workspace_diff=diff,
            workspace_after=workspace,
        )
    finally:
        import shutil

        shutil.rmtree(work_root, ignore_errors=True)


# ─── CLI ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HarnessReport:
    package_id: str
    cases: tuple[CaseOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        passed = sum(1 for c in self.cases if c.status == "PASS")
        return {
            "schema_version": SCHEMA_VERSION,
            "package_id": self.package_id,
            "summary": {
                "total": len(self.cases),
                "passed": passed,
                "failed": len(self.cases) - passed,
            },
            "cases": [c.to_dict() for c in self.cases],
        }


def _format_text(report: HarnessReport) -> str:
    lines: list[str] = []
    for case in report.cases:
        if case.status == "PASS":
            lines.append(f"PASS {report.package_id}::{case.name}")
        else:
            codes = ",".join(case.reason_codes)
            lines.append(f"FAIL {report.package_id}::{case.name} [{codes}]")
    total = len(report.cases)
    passed = sum(1 for c in report.cases if c.status == "PASS")
    failed = total - passed
    lines.append(f"{total} cases: {passed} passed, {failed} failed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(prog="terminux-package-test")
    parser.add_argument("--root", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    if not args.case:
        print("terminux-package-test: at least one --case is required", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("terminux-package-test: --timeout must be positive", file=sys.stderr)
        return 2

    root_path = Path(args.root)
    if not root_path.is_dir():
        print(f"terminux-package-test: root is not a directory: {root_path}", file=sys.stderr)
        return 2

    try:
        cases: list[CaseOutcome] = []
        for case_arg in args.case:
            case_path = Path(case_arg)
            if not case_path.is_file():
                print(f"terminux-package-test: internal error: case file not found: {case_path}", file=sys.stderr)
                return 2
            cases.append(run_case(root_path, args.package, case_path, default_timeout=args.timeout))
    except Exception as exc:  # 真正未預期的錯誤，不得誤報成 case FAIL
        print(f"terminux-package-test: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    report = HarnessReport(package_id=args.package, cases=tuple(cases))

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_format_text(report))

    return 0 if all(c.status == "PASS" for c in report.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
