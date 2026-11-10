# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import logging
from pathlib import Path

from pydantic import ValidationError

from terminux_engine.lineage.models import JobCommit, ArtifactSnapshot
from terminux_engine.lineage.types import LEDGER_FILENAME, LEDGER_DIR

_log = logging.getLogger(__name__)


def default_ledger_path(project_root: str | Path) -> Path:
    return Path(project_root) / LEDGER_DIR / LEDGER_FILENAME


def append_job_commit(ledger_path: str | Path, commit: JobCommit) -> None:
    """追加一筆 commit（目錄不存在則建立）。一行一筆 JSON。"""
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(commit.model_dump_json() + "\n")


def load_job_commits(ledger_path: str | Path) -> list[JobCommit]:
    """載入所有 commits。ledger 不存在時回傳空 list，不拋 exception。

    Defensive parsing：~/.terminux/lineage.jsonl 路徑可能被 wrapper 端的
    tool_exec event log 共用（schema 完全不同）。遇到無法解析為 JobCommit
    的行，記 debug log 後跳過，**不**讓整個 lineage check 因為單一不相容 line
    全盤崩潰回 Internal error。
    """
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return []
    commits: list[JobCommit] = []
    skipped = 0
    with ledger_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                commits.append(JobCommit.model_validate_json(line))
            except ValidationError:
                skipped += 1
    if skipped > 0:
        _log.debug(
            "load_job_commits: skipped %d non-JobCommit line(s) in %s "
            "(likely wrapper tool_exec events sharing the path)",
            skipped, ledger_path,
        )
    return commits


def find_latest_commit_by_output_path(
    ledger_path: str | Path,
    abs_path: str,
) -> tuple[JobCommit, ArtifactSnapshot] | tuple[None, None]:
    """從 ledger 尾端優先搜尋，找 output_snapshots[i].path == abs_path。"""
    commits = load_job_commits(ledger_path)
    for commit in reversed(commits):
        for snap in commit.output_snapshots:
            if snap.path == abs_path:
                return commit, snap
    return None, None


def find_latest_commit_by_output_checksum(
    ledger_path: str | Path,
    checksum_sha256: str,
) -> tuple[JobCommit, ArtifactSnapshot] | tuple[None, None]:
    """從 ledger 尾端優先搜尋，找 output_snapshots[i].checksum_sha256 == checksum_sha256。"""
    commits = load_job_commits(ledger_path)
    for commit in reversed(commits):
        for snap in commit.output_snapshots:
            if snap.checksum_sha256 == checksum_sha256:
                return commit, snap
    return None, None
