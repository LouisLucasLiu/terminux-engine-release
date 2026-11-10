# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""依執行收據與 artifact manifest 顯示同一次 ORFS 的正式報告，或定位其中的 timing path stage。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.safe_file import read_regular_file

_ROLES = frozenset({"orfs.finish_report", "orfs.route_drc_report"})
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_REPORT_BYTES = 16 * 1024 * 1024
_LOCATOR_ROLE = "orfs.finish_report"
#: `str.splitlines` 視為換行、但檢視器不計為一行的字元；出現時兩邊的行號可能不同。
_LINE_DRIFT = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_VIEW_PATH = "/usr/bin:/bin"
_VIEW_ENV_KEEP = ("TERM", "COLUMNS", "LINES", "LANG", "LC_ALL", "LC_CTYPE")


class ReportUnavailable(ValueError):
    """報告來源、身分或檔案驗證未通過。"""


@dataclass(frozen=True)
class StageLocation:
    """一個 stage 在已驗證 report 內的位置；不含 instance、net 或 pin 名稱。"""

    report_sha256: str
    excerpt_hash: str
    index: int
    kind: str
    role: str | None
    fanout: int | None
    cap: float | None
    line: int


def _read_regular(path: Path, limit: int, *, owner_uid: int | None = None) -> bytes:
    """沿用共用的逐段 `O_NOFOLLOW` 讀檔；錯誤碼維持 `REPORT_*` 前綴不變。"""
    return read_regular_file(path, limit, lambda reason: ReportUnavailable(f"REPORT_{reason}"),
                             required_owner_uid=owner_uid)


def _document(path: Path, *, owner_uid: int | None = None) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular(path, _MAX_JSON_BYTES, owner_uid=owner_uid))
    except ReportUnavailable:
        raise
    except (UnicodeError, ValueError) as exc:
        raise ReportUnavailable("REPORT_DOCUMENT_INVALID") from exc
    if not isinstance(value, dict):
        raise ReportUnavailable("REPORT_DOCUMENT_INVALID")
    return value


def report_bytes(receipt_path: Path, run_id: str, attempt: int, role: str) -> bytes:
    """只回傳收據與 manifest 均釘住、位於該次 reports root 的檔案。"""
    return _verified_report(receipt_path, run_id, attempt, role)


def _verified_report(receipt_path: Path, run_id: str, attempt: int, role: str, *,
                     owner_uid: int | None = None) -> bytes:
    """`owner_uid` 不為 None 時，receipt、manifest 與 report 都必須屬於該 UID。"""
    if role not in _ROLES or not run_id or attempt < 1:
        raise ReportUnavailable("REPORT_REQUEST_INVALID")
    receipt = _document(receipt_path, owner_uid=owner_uid)
    if receipt.get("run_id") != run_id or receipt.get("attempt") != attempt:
        raise ReportUnavailable("REPORT_RUN_MISMATCH")
    metadata = receipt.get("adapter_metadata")
    if not isinstance(metadata, dict) or metadata.get("runner") != "host_native":
        raise ReportUnavailable("REPORT_SOURCE_INVALID")
    work_home = metadata.get("work_home")
    platform = metadata.get("platform")
    design = metadata.get("design")
    variant = metadata.get("variant")
    if not isinstance(work_home, str) or not all(
        isinstance(part, str) and part and part not in {".", ".."} and "/" not in part
        for part in (platform, design, variant)
    ):
        raise ReportUnavailable("REPORT_SOURCE_INVALID")
    report_root = Path(work_home) / "reports" / platform / design / variant
    roots = receipt.get("output_roots")
    if (not report_root.is_absolute() or ".." in report_root.parts
            or not isinstance(roots, list) or str(report_root) not in roots):
        raise ReportUnavailable("REPORT_ROOT_MISMATCH")
    manifest_path = receipt.get("artifact_manifest_reference")
    manifest_digest = receipt.get("artifact_manifest_sha256")
    if not isinstance(manifest_path, str) or not isinstance(manifest_digest, str):
        raise ReportUnavailable("REPORT_MANIFEST_INVALID")
    manifest_data = _read_regular(Path(manifest_path), _MAX_JSON_BYTES, owner_uid=owner_uid)
    if hashlib.sha256(manifest_data).hexdigest() != manifest_digest:
        raise ReportUnavailable("REPORT_MANIFEST_MISMATCH")
    try:
        manifest = json.loads(manifest_data)
    except (UnicodeError, ValueError) as exc:
        raise ReportUnavailable("REPORT_MANIFEST_INVALID") from exc
    if (not isinstance(manifest, dict) or manifest.get("manifest_id") != receipt.get("artifact_manifest_id")
            or manifest.get("capture_status") != "complete"):
        raise ReportUnavailable("REPORT_MANIFEST_MISMATCH")
    producer = manifest.get("producer")
    if (not isinstance(producer, dict) or producer.get("run_id") != run_id
            or producer.get("attempt") != attempt or producer.get("step_id") != receipt.get("step_id")):
        raise ReportUnavailable("REPORT_RUN_MISMATCH")
    observations = manifest.get("observations")
    if not isinstance(observations, list):
        raise ReportUnavailable("REPORT_MANIFEST_INVALID")
    matching = [item for item in observations if isinstance(item, dict) and item.get("adapter_role") == role]
    if len(matching) != 1:
        raise ReportUnavailable("REPORT_ROLE_UNAVAILABLE")
    item = matching[0]
    path = item.get("resolved_path")
    identity = item.get("content_identity")
    if (item.get("availability") != "present" or item.get("file_type") != "regular"
            or not isinstance(path, str) or not isinstance(identity, dict)
            or identity.get("algorithm") != "sha256" or not isinstance(identity.get("digest"), str)):
        raise ReportUnavailable("REPORT_ROLE_UNAVAILABLE")
    target = Path(path)
    if target.suffix != ".rpt" or target.parent != report_root:
        raise ReportUnavailable("REPORT_ROOT_MISMATCH")
    item_producer = item.get("producer")
    if (not isinstance(item_producer, dict) or item_producer.get("run_id") != run_id
            or item_producer.get("attempt") != attempt):
        raise ReportUnavailable("REPORT_RUN_MISMATCH")
    content = _read_regular(target, _MAX_REPORT_BYTES, owner_uid=owner_uid)
    if hashlib.sha256(content).hexdigest() != identity["digest"]:
        raise ReportUnavailable("REPORT_CONTENT_MISMATCH")
    if not content:
        raise ReportUnavailable("REPORT_EMPTY")
    return content


def locate_stage(content: bytes, stage_index: int, expect_line: int | None = None) -> StageLocation:
    """以產生 timing path evidence 的同一 parser，把 1-based stage index 換算成 report 實體行號。"""
    from terminux_engine.packages.orfs_dqe import parsers, timing_path

    if len(content) > parsers.MAX_TEXT_BYTES:
        raise ReportUnavailable("REPORT_PARSE_LIMIT_EXCEEDED")
    if b"\x00" in content:
        raise ReportUnavailable("REPORT_PARSE_REJECTED")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportUnavailable("REPORT_PARSE_REJECTED") from exc
    if _LINE_DRIFT.search(text):
        raise ReportUnavailable("REPORT_PARSE_REJECTED")
    lines = text.splitlines()
    try:
        parsed = timing_path.parse_worst_setup_path(lines)
    except Exception as exc:  # noqa: BLE001 - parser 訊息可能含 report 內容，一律收斂成封閉碼
        raise ReportUnavailable("REPORT_PARSE_REJECTED") from exc
    # slack 同值時 parser 取第一條；這裡沒有 record 可佐證，不把它當成已確定的 path。
    if timing_path.PROBLEM_SLACK_TIE in parsed.completeness.problems:
        raise ReportUnavailable("REPORT_PATH_AMBIGUOUS")
    if not parsed.completeness.stages_complete:
        raise ReportUnavailable("REPORT_PATH_INCOMPLETE")
    matches = [stage for stage in parsed.stages if stage.index == stage_index]
    if len(matches) != 1:
        raise ReportUnavailable("REPORT_STAGE_OUT_OF_RANGE")
    stage = matches[0]
    line = stage.line
    if ((stage.kind == timing_path.KIND_CELL) != (stage.role is not None)
            or line is None or not parsed.line_start <= line <= parsed.line_end
            or line > len(lines) or not lines[line - 1].strip()):
        raise ReportUnavailable("REPORT_STAGE_INVALID")
    if expect_line is not None and expect_line != line:
        raise ReportUnavailable("REPORT_LINE_MISMATCH")
    return StageLocation(
        report_sha256=hashlib.sha256(content).hexdigest(), excerpt_hash=parsed.excerpt_hash,
        index=stage.index, kind=stage.kind, role=stage.role, fanout=stage.fanout, cap=stage.cap,
        line=line,
    )


def _display(value: object) -> str:
    return "-" if value is None else str(value)


def _integer_argument(text: str) -> int:
    if not re.fullmatch(r"-?[0-9]{1,18}", text):
        raise ReportUnavailable("REPORT_REQUEST_INVALID")
    return int(text)


def _terminal_ready() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "") not in {"", "dumb"}


def _view_environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in _VIEW_ENV_KEEP if name in os.environ}
    env.update(PATH=_VIEW_PATH, LESSSECURE="1", LESSHISTFILE="-")
    return env


def _view(content: bytes, line: int) -> None:
    """只把已驗證的 bytes 經 stdin 交給 `less`，不讓它依路徑重新開檔。"""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        result = subprocess.run(["less", "-N", f"+{line}", "-"], input=content,
                                env=_view_environment(), shell=False, check=False)
    except OSError as exc:
        raise ReportUnavailable("REPORT_VIEW_UNAVAILABLE") from exc
    finally:
        signal.signal(signal.SIGINT, previous)
    if result.returncode != 0:
        raise ReportUnavailable("REPORT_VIEW_FAILED")


def _run_locator(receipt_path: Path, args: argparse.Namespace) -> int:
    stage_index = _integer_argument(args.stage)
    expect_line = None if args.expect_line is None else _integer_argument(args.expect_line)
    if args.role != _LOCATOR_ROLE:
        raise ReportUnavailable("REPORT_STAGE_ROLE_INVALID")
    if args.view and not _terminal_ready():
        raise ReportUnavailable("REPORT_VIEW_REQUIRES_TTY")
    content = _verified_report(receipt_path, args.run_id, args.attempt, args.role, owner_uid=os.geteuid())
    location = locate_stage(content, stage_index, expect_line)
    print(f"VERIFIED run_id={args.run_id} attempt={args.attempt} role={args.role}")
    print(f"report_sha256={location.report_sha256} excerpt_hash={location.excerpt_hash}")
    print(f"stage={location.index} kind={location.kind} role={_display(location.role)} "
          f"fanout={_display(location.fanout)} cap={_display(location.cap)} line={location.line}", flush=True)
    if args.view:
        _view(content, location.line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a report verified against the ORFS receipt and artifact manifest")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--role", choices=sorted(_ROLES), default="orfs.finish_report")
    parser.add_argument("--stage", metavar="N", help="locate this 1-based timing path stage instead of printing the report")
    parser.add_argument("--expect-line", metavar="L", help="require the stage to be on this report line (needs --stage)")
    parser.add_argument("--view", action="store_true", help="open the verified report at the stage line (needs --stage and a terminal)")
    args = parser.parse_args(argv)
    try:
        receipt_path = args.receipt if args.receipt.is_absolute() else Path.cwd() / args.receipt
        if args.stage is not None:
            return _run_locator(receipt_path, args)
        if args.expect_line is not None or args.view:
            raise ReportUnavailable("REPORT_REQUEST_INVALID")
        content = report_bytes(receipt_path, args.run_id, args.attempt, args.role)
    except ReportUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
