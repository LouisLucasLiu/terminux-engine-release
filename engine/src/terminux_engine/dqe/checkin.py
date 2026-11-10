# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE temporary job checkin workflow。

本模組是 check-in 的 side-effect 邊界：Python 可讀 DQE_DB、寫 archived package、
並透過外部 isvn CLI 做 checkin。C++ 不得繞過此模組直接碰 DB / isvn。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_DEFAULT_DB_PATH = Path.home() / ".terminux" / "dqe" / "dqe.sqlite"
_VERSION_COLUMNS = {
    "id",
    "ProjectCode",
    "DqeDbNum",
    "Phase",
    "Block",
    "Stage",
    "Flow",
    "Version",
    "DQE_ID",
    "VALUE",
    "UNIT",
    "STATUS",
    "DATE_TIME",
    "Reporter",
    "PATH",
    "ATTACHMENT",
    "EXECUTABLE",
    "job_key",
}
_META_COLUMNS = {"DQE_ID", "TITLE", "DESCRIPTION", "REFERENCE"}
_DQE_SQL = """
SELECT
    v.id,
    v.ProjectCode,
    v.DqeDbNum,
    v.Phase,
    v.Block,
    v.Stage,
    v.Flow,
    v.Version,
    v.DQE_ID,
    v.VALUE,
    v.UNIT,
    v.STATUS,
    v.DATE_TIME,
    v.Reporter,
    v.PATH,
    v.ATTACHMENT,
    v.EXECUTABLE,
    v.job_key,
    m.TITLE,
    m.DESCRIPTION,
    m.REFERENCE
FROM Version_Dqe_Tab v
LEFT JOIN Dqe_Meta_Tab m ON v.DQE_ID = m.DQE_ID
WHERE v.job_key = ?
ORDER BY v.DQE_ID, v.id
"""


@dataclass
class CheckinRequest:
    job_key: str
    workspace_root: str
    target_version_path: str
    db_path: str | None = None
    dry_run: bool = False


@dataclass
class CheckinFileResult:
    relpath: str
    kind: str
    managed_before: bool
    edited: bool
    written: bool
    checked_in: bool
    message: str = ""


@dataclass
class CheckinResult:
    job_key: str
    target_workspace: str
    target_version_path: str
    target_dir: str
    last_attempt_at: str
    last_result: str
    last_message: str
    package_json: str
    dry_run: bool
    partial: bool
    files: list[CheckinFileResult]
    failure_code: str | None = None
    partial_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    command: list[str] | None = None


@dataclass
class _DqeRow:
    row_id: int
    project_code: str
    dqe_db_num: str
    phase: str
    block: str
    stage: str
    flow: str
    version: str
    dqe_id: str
    value: str
    unit: str
    status: str
    date_time: str
    reporter: str
    path: str
    attachment: bytes | None
    executable: bytes | None
    job_key: str
    title: str
    description: str
    reference: str


@dataclass
class _PlannedFile:
    relpath: str
    kind: str
    content: bytes


@dataclass
class _PackagePlan:
    package_json: str
    payload: dict[str, Any]
    files: list[_PlannedFile]


class CheckinDomainError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IsvnCli:
    """外部 isvn CLI wrapper。所有命令都使用 shell=False。"""

    def __init__(
        self,
        executable: str = "isvn",
        timeout_sec: int = 30,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._executable = executable
        self._timeout_sec = timeout_sec
        self._runner = runner or subprocess.run

    def status(self, target_dir: str) -> CommandResult:
        return self._run(["status"], target_dir)

    def managed_relpaths(self, target_dir: str) -> set[str]:
        result = self.status(target_dir)
        if not result.ok:
            raise CheckinDomainError("isvn_status_failed", _command_message("isvn status failed", result))
        return _parse_status_managed_relpaths(result.stdout)

    def edit(self, target_dir: str, relpath: str) -> CommandResult:
        return self._run(["edit", relpath], target_dir)

    def checkin(self, target_dir: str, relpath: str, version_path: str) -> CommandResult:
        return self._run(["checkin", relpath, "--to", version_path], target_dir)

    def _run(self, args: list[str], cwd: str) -> CommandResult:
        cmd = [self._executable, *args]
        try:
            completed = self._runner(
                cmd,
                shell=False,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except FileNotFoundError as exc:
            raise CheckinDomainError("isvn_not_found", "isvn command not found") from exc
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                ok=False,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "isvn command timed out",
                returncode=-1,
                command=cmd,
            )
        return CommandResult(
            ok=completed.returncode == 0,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
            command=cmd,
        )


class DqeCheckinService:
    def __init__(self, isvn_cli: IsvnCli | None = None) -> None:
        self._isvn = isvn_cli or IsvnCli()

    def checkin(self, request: CheckinRequest) -> CheckinResult:
        files: list[CheckinFileResult] = []
        package_json = f"dqe_{request.job_key}.json"
        attempt_at = _now_iso()
        workspace = request.workspace_root or os.environ.get("ISVN_WORKSPACE", "")
        target_dir = _target_dir_string(workspace, request.target_version_path)

        try:
            _validate_basic_request(request, workspace)
            # dry-run 也必須先驗證既有 target version；絕不以 preview 建立 version。
            workspace_path, target_path = _validate_target_paths(workspace, request.target_version_path)
            rows = _read_dqe_rows(Path(request.db_path or _DEFAULT_DB_PATH), request.job_key)
            plan = _build_package_plan(request.job_key, rows)
            package_json = plan.package_json
            if request.dry_run:
                # preview 也要以唯讀 status 告知既有 target 檔案；不可改寫或建立 version。
                managed = self._isvn.managed_relpaths(str(target_path))
                with tempfile.TemporaryDirectory(prefix="dqe_checkin_dry_run_") as tmp:
                    _write_plan(Path(tmp), plan, files_by_relpath=None)
                return CheckinResult(
                    job_key=request.job_key,
                    target_workspace=str(workspace_path),
                    target_version_path=request.target_version_path,
                    target_dir=str(target_path),
                    last_attempt_at=attempt_at,
                    last_result="success_new",
                    last_message=f"Dry run built package plan for {request.job_key}",
                    package_json=package_json,
                    dry_run=True,
                    partial=False,
                    files=[
                        CheckinFileResult(
                            f.relpath,
                            f.kind,
                            _normalize_relpath(f.relpath) in managed,
                            False,
                            True,
                            False,
                        )
                        for f in plan.files
                    ],
                )
            managed = self._isvn.managed_relpaths(str(target_path))
            file_results = [
                CheckinFileResult(
                    relpath=f.relpath,
                    kind=f.kind,
                    managed_before=_normalize_relpath(f.relpath) in managed,
                    edited=False,
                    written=False,
                    checked_in=False,
                )
                for f in plan.files
            ]
            files = file_results
            by_relpath = {item.relpath: item for item in file_results}

            for item in file_results:
                if item.managed_before:
                    edit_result = self._isvn.edit(str(target_path), item.relpath)
                    if not edit_result.ok:
                        item.message = _command_message("isvn edit failed", edit_result)
                        return _failed_result(
                            request,
                            workspace_path,
                            target_path,
                            attempt_at,
                            package_json,
                            files,
                            "isvn_edit_failed",
                            item.message,
                        )
                    item.edited = True

            try:
                _write_plan(target_path, plan, files_by_relpath=by_relpath)
            except OSError as exc:
                return _failed_result(
                    request,
                    workspace_path,
                    target_path,
                    attempt_at,
                    package_json,
                    files,
                    "package_write_failed",
                    str(exc),
                )

            for item in file_results:
                checkin_result = self._isvn.checkin(str(target_path), item.relpath, request.target_version_path)
                if not checkin_result.ok:
                    item.message = _command_message("isvn checkin failed", checkin_result)
                    partial = any(f.checked_in for f in file_results)
                    return _failed_result(
                        request,
                        workspace_path,
                        target_path,
                        attempt_at,
                        package_json,
                        files,
                        "partial_checkin_failed" if partial else "isvn_checkin_failed",
                        _partial_message(item.message) if partial else item.message,
                        partial=partial,
                    )
                item.checked_in = True

            result_kind = "success_update" if any(f.managed_before for f in file_results) else "success_new"
            return CheckinResult(
                job_key=request.job_key,
                target_workspace=str(workspace_path),
                target_version_path=request.target_version_path,
                target_dir=str(target_path),
                last_attempt_at=attempt_at,
                last_result=result_kind,
                last_message=f"Checked in {len(file_results)} files to {request.target_version_path}",
                package_json=package_json,
                dry_run=False,
                partial=False,
                files=file_results,
            )
        except CheckinDomainError as exc:
            return CheckinResult(
                job_key=request.job_key,
                target_workspace=workspace,
                target_version_path=request.target_version_path,
                target_dir=target_dir,
                last_attempt_at=attempt_at,
                last_result="failed",
                last_message=str(exc),
                package_json=package_json,
                dry_run=request.dry_run,
                partial=False,
                files=files,
                failure_code=exc.code,
            )
        except Exception as exc:  # 受控結構化回傳，避免 GUI 收 traceback
            return CheckinResult(
                job_key=request.job_key,
                target_workspace=workspace,
                target_version_path=request.target_version_path,
                target_dir=target_dir,
                last_attempt_at=attempt_at,
                last_result="failed",
                last_message=str(exc),
                package_json=package_json,
                dry_run=request.dry_run,
                partial=False,
                files=files,
                failure_code="internal_error",
            )


def checkin_job(request: CheckinRequest, isvn_cli: IsvnCli | None = None) -> CheckinResult:
    return DqeCheckinService(isvn_cli=isvn_cli).checkin(request)


def _read_dqe_rows(db_path: Path, job_key: str) -> list[_DqeRow]:
    if not db_path.exists():
        raise CheckinDomainError("db_missing", f"DQE_DB does not exist: {db_path}")
    conn = _connect_read_only(db_path)
    try:
        _verify_schema(conn)
        rows = conn.execute(_DQE_SQL, (job_key,)).fetchall()
    except sqlite3.Error as exc:
        raise CheckinDomainError("db_query_failed", str(exc)) from exc
    finally:
        conn.close()
    if not rows:
        raise CheckinDomainError("job_not_found", f"DQE job_key not found: {job_key}")
    return [_row_from_sql(row) for row in rows]


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return sqlite3.connect(str(db_path))


def _verify_schema(conn: sqlite3.Connection) -> None:
    version_cols = _table_columns(conn, "Version_Dqe_Tab")
    meta_cols = _table_columns(conn, "Dqe_Meta_Tab")
    missing_version = sorted(_VERSION_COLUMNS - version_cols)
    missing_meta = sorted(_META_COLUMNS - meta_cols)
    if missing_version or missing_meta:
        raise CheckinDomainError(
            "db_schema_mismatch",
            f"DQE_DB schema mismatch: missing Version={missing_version}, Meta={missing_meta}",
        )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _row_from_sql(row: sqlite3.Row | tuple[Any, ...]) -> _DqeRow:
    return _DqeRow(
        row_id=int(row[0]),
        project_code=_text(row[1]),
        dqe_db_num=_text(row[2]),
        phase=_text(row[3]),
        block=_text(row[4]),
        stage=_text(row[5]),
        flow=_text(row[6]),
        version=_text(row[7]),
        dqe_id=_text(row[8]),
        value=_text(row[9]),
        unit=_text(row[10]),
        status=_text(row[11]),
        date_time=_text(row[12]),
        reporter=_text(row[13]),
        path=_text(row[14]),
        attachment=row[15],
        executable=row[16],
        job_key=_text(row[17]),
        title=_text(row[18]),
        description=_text(row[19]),
        reference=_text(row[20]),
    )


def _build_package_plan(job_key: str, rows: list[_DqeRow]) -> _PackagePlan:
    package_json = f"dqe_{_safe_token(job_key)}.json"
    first = rows[0]
    payload: dict[str, Any] = {
        "job_key": job_key,
        "address": {
            "project": first.project_code,
            "phase": first.phase,
            "block": first.block,
            "flow": first.flow,
            "stage": first.stage,
            "version": first.version,
        },
        "DQES": [],
    }
    planned: list[_PlannedFile] = []
    used_names: set[str] = set()

    for row in rows:
        item = {
            "DQE_ID": row.dqe_id,
            "TITLE": row.title,
            "VALUE": row.value,
            "UNIT": row.unit,
            "STATUS": row.status,
            "DESCRIPTION": row.description,
            "REFERENCE": row.reference,
            "DATE_TIME": row.date_time,
            "PATH": row.path,
            "ATTACHMENT": "",
            "EXECUTABLE": "",
        }
        if row.attachment is not None:
            relpath = _attachment_relpath(row, "attachment", used_names)
            item["ATTACHMENT"] = relpath
            planned.append(_PlannedFile(relpath, "attachment", bytes(row.attachment)))
        if row.executable is not None:
            relpath = _attachment_relpath(row, "executable", used_names)
            item["EXECUTABLE"] = relpath
            planned.append(_PlannedFile(relpath, "executable", bytes(row.executable)))
        payload["DQES"].append(item)

    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    planned.append(_PlannedFile(package_json, "json", json_bytes))
    return _PackagePlan(package_json=package_json, payload=payload, files=planned)


def _attachment_relpath(row: _DqeRow, kind: str, used_names: set[str]) -> str:
    base = _safe_token(row.dqe_id)
    if kind == "attachment":
        suffix = Path(Path(row.path).name).suffix.lstrip(".") if row.path else ""
        ext = _safe_token(suffix) if suffix else "bin"
        filename = f"{base}_attachment.{ext}"
    else:
        filename = f"{base}_executable.sh"
    if filename in used_names:
        stem = filename.rsplit(".", 1)[0]
        ext = filename.rsplit(".", 1)[1] if "." in filename else ""
        filename = f"{stem}_{row.row_id}.{ext}" if ext else f"{stem}_{row.row_id}"
    used_names.add(filename)
    return f"attachments/{filename}"


def _write_plan(
    target_dir: Path,
    plan: _PackagePlan,
    *,
    files_by_relpath: dict[str, CheckinFileResult] | None,
) -> None:
    for item in plan.files:
        destination = _safe_destination(target_dir, item.relpath)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.content)
        if files_by_relpath is not None and item.relpath in files_by_relpath:
            files_by_relpath[item.relpath].written = True


def _safe_destination(root: Path, relpath: str) -> Path:
    if Path(relpath).is_absolute() or ".." in Path(relpath).parts:
        raise OSError(f"unsafe package relpath: {relpath}")
    destination = (root / relpath).resolve()
    common = os.path.commonpath([str(root.resolve()), str(destination)])
    if common != str(root.resolve()):
        raise OSError(f"package relpath escapes target dir: {relpath}")
    return destination


def _validate_basic_request(request: CheckinRequest, workspace: str) -> None:
    if not request.job_key:
        raise CheckinDomainError("invalid_request", "job_key is required")
    if not workspace:
        raise CheckinDomainError("target_workspace_missing", "workspace_root is required")
    _validate_relative_version_path(request.target_version_path)


def _validate_target_paths(workspace: str, target_version_path: str) -> tuple[Path, Path]:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise CheckinDomainError("target_workspace_missing", f"workspace_root does not exist: {workspace}")
    target_path = (workspace_path / target_version_path).resolve()
    common = os.path.commonpath([str(workspace_path), str(target_path)])
    if common != str(workspace_path):
        raise CheckinDomainError("path_escape_rejected", "target_version_path escapes workspace_root")
    if not target_path.is_dir():
        raise CheckinDomainError("target_version_dir_missing", f"target version directory does not exist: {target_path}")
    return workspace_path, target_path


def _validate_relative_version_path(target_version_path: str) -> None:
    if not target_version_path:
        raise CheckinDomainError("target_version_dir_missing", "target_version_path is required")
    path = Path(target_version_path)
    if path.is_absolute() or ".." in path.parts:
        raise CheckinDomainError("path_escape_rejected", "target_version_path must be a safe relative path")


def _target_dir_string(workspace: str, version_path: str) -> str:
    if not workspace or not version_path:
        return ""
    return str(Path(workspace).expanduser() / version_path)


def _parse_status_managed_relpaths(output: str) -> set[str]:
    managed: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        token = stripped.split()[-1]
        normalized = _normalize_relpath(token)
        if normalized:
            managed.add(normalized)
    return managed


def _normalize_relpath(path: str) -> str:
    if not path:
        return ""
    normalized = os.path.normpath(path).replace("\\", "/")
    if normalized == "." or normalized.startswith("../") or normalized == ".." or os.path.isabs(normalized):
        return ""
    return normalized


def _failed_result(
    request: CheckinRequest,
    workspace_path: Path,
    target_path: Path,
    attempt_at: str,
    package_json: str,
    files: list[CheckinFileResult],
    failure_code: str,
    message: str,
    *,
    partial: bool = False,
) -> CheckinResult:
    warning = (
        "Some files may already have been checked into isvn; retry the same job to converge."
        if partial else None
    )
    return CheckinResult(
        job_key=request.job_key,
        target_workspace=str(workspace_path),
        target_version_path=request.target_version_path,
        target_dir=str(target_path),
        last_attempt_at=attempt_at,
        last_result="failed",
        last_message=message,
        package_json=package_json,
        dry_run=request.dry_run,
        partial=partial,
        files=files,
        failure_code=failure_code,
        partial_warning=warning,
    )


def _partial_message(message: str) -> str:
    return f"{message}; some files may already have been checked into isvn"


def _command_message(prefix: str, result: CommandResult) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if len(detail) > 500:
        detail = detail[:500] + "..."
    return f"{prefix}: {detail}" if detail else prefix


def _safe_token(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "")
    text = text.strip("._")
    return text or "unnamed"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "CheckinRequest",
    "CheckinFileResult",
    "CheckinResult",
    "CommandResult",
    "DqeCheckinService",
    "IsvnCli",
    "checkin_job",
]
