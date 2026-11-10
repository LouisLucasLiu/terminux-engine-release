# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS report／log 的 deterministic parser（orfs_dqe，無 LLM）。

每個 parser 的格式依據都來自 ORFS source 或真實 sample，並記在 docstring：

- `6_finish.rpt`：由 `flow/scripts/report_metrics.tcl` 的
  `proc report_metrics {stage when ...}` 產生，檔名來自
  `set filename $::env(REPORTS_DIR)/${stage}_[string map {" " "_"} $when].rpt`
  （stage=6、when=finish）；每個 section 都是固定的四段
  `====` 分隔線／`"$when <name>"` title／`----` 分隔線／內容。
  Violation count 的值行也是逐字寫死的，例如
  `report_puts "setup violation count [sta::endpoint_violation_count max]"`。
  實測 sample（`rt2_lineage_prod`）有 31 個 section。
- `5_route_drc.rpt`：由 `flow/scripts/detail_route.tcl` 的
  `detailed_route -output_drc` 產生。ORFS 自己的兩支工具
  （`flow/util/genReport.py:298`、`flow/util/viewDrc.py:34`）都以
  「含 `violation type:` 的行數」計算 violation 數量，因此空檔＝0 violations。
- `6_report.log`：ORFS `flow/util/genReport.py` 的
  `REGEX_ERROR = ^\\[error ?(\\w+-\\d+)?\\]`／
  `REGEX_WARNING = ^\\[warning ?(\\w+-\\d+)?\\]`（皆 case-insensitive、
  行首錨定），錨定行首正是為了不把散文中的 "error" 誤判成 fatal marker。

所有 parser 在找不到值時回傳 `None`，**絕不回傳 0**。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

#: ORFS `genReport.py` 的 marker regex，逐字沿用（含 case-insensitive）。
ORFS_ERROR_MARKER = re.compile(r"^\[error ?(\w+-\d+)?\]", re.IGNORECASE)
ORFS_WARNING_MARKER = re.compile(r"^\[warning ?(\w+-\d+)?\]", re.IGNORECASE)

#: ORFS `genReport.py` / `viewDrc.py` 共用的 DRC violation marker。
ORFS_DRC_VIOLATION_MARKER = "violation type:"

_SECTION_RULE = re.compile(r"^={20,}$")
_UNDERLINE_RULE = re.compile(r"^-{20,}$")

#: 單一檔案的讀取上限。ORFS finish report 實測 33 KB、log 3.8 KB；上限
#: 純粹是防呆，避免 package 意外讀進 GDS 之類的大型二進位 artifact。
MAX_TEXT_BYTES = 8 * 1024 * 1024


class ParseError(RuntimeError):
    """檔案結構損壞到無法 deterministic 解析。"""


@dataclass(frozen=True)
class ScalarExtraction:
    """一次 scalar 抽取的結果與其可重現位置。"""

    value: float | int | None
    section: str | None
    line_start: int | None
    line_end: int | None
    excerpt_hash: str | None
    problem: str | None = None


@dataclass(frozen=True)
class CountExtraction:
    """一次 marker 計數的結果。"""

    count: int | None
    line_start: int | None
    line_end: int | None
    excerpt_hash: str | None
    problem: str | None = None


def excerpt_hash(text: str) -> str:
    """短 excerpt 的 SHA-256——EventStore 只存 hash，不存整段文字。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> list[str]:
    """讀取文字 artifact，回傳行清單（不含換行）。"""
    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ParseError(f"file too large for text parsing: {path} ({size} bytes)")
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ParseError(f"file is not text (contains NUL bytes): {path}")
    return raw.decode("utf-8", errors="replace").splitlines()


def split_finish_sections(lines: list[str]) -> dict[str, tuple[int, int, list[str]]]:
    """把 `6_finish.rpt` 拆成 `{section_title: (start_line, end_line, body)}`。

    結構（1-indexed 行號）：

    ```
    ==========================   <- rule
    finish report_wns            <- title
    --------------------------   <- underline
    wns max -0.18                <- body（到下一個 rule 之前）
    ```

    重複 section title 視為結構異常（`ParseError`），不任選一個。
    """
    sections: dict[str, tuple[int, int, list[str]]] = {}
    index = 0
    total = len(lines)
    while index < total:
        if not _SECTION_RULE.match(lines[index].strip()):
            index += 1
            continue
        if index + 2 >= total:
            break
        title = lines[index + 1].strip()
        if not _UNDERLINE_RULE.match(lines[index + 2].strip()):
            index += 1
            continue
        body_start = index + 3
        body_end = body_start
        while body_end < total and not _SECTION_RULE.match(lines[body_end].strip()):
            body_end += 1
        if title in sections:
            raise ParseError(f"duplicate section title in finish report: {title!r}")
        sections[title] = (
            body_start + 1,  # 1-indexed
            body_end,
            lines[body_start:body_end],
        )
        index = body_end
    return sections


def extract_finish_scalar(
    lines: list[str], *, section: str, pattern: str
) -> ScalarExtraction:
    """從指定 section 以 regex 抽一個數值。找不到→`None`＋problem。"""
    try:
        sections = split_finish_sections(lines)
    except ParseError as exc:
        return ScalarExtraction(None, section, None, None, None, problem=repr(exc))

    if section not in sections:
        return ScalarExtraction(
            None, section, None, None, None, problem="section_not_found"
        )

    start_line, _end_line, body = sections[section]
    compiled = re.compile(pattern)
    for offset, line in enumerate(body):
        match = compiled.match(line.strip())
        if match is None:
            continue
        raw = match.group("value")
        value = _to_number(raw)
        if value is None:
            return ScalarExtraction(
                None,
                section,
                start_line + offset,
                start_line + offset,
                excerpt_hash(line.strip()),
                problem=f"unparsable_number:{raw!r}",
            )
        return ScalarExtraction(
            value,
            section,
            start_line + offset,
            start_line + offset,
            excerpt_hash(line.strip()),
        )
    return ScalarExtraction(
        None, section, start_line, start_line, None, problem="pattern_not_matched"
    )


def count_drc_violations(lines: list[str]) -> CountExtraction:
    """依 ORFS 自己的規則計算 DRC violations。

    ORFS `flow/util/genReport.py:298` 與 `flow/util/viewDrc.py:34` 都以
    `violation type:` 這個 marker 逐筆計數；`viewDrc.py` 進一步顯示每筆
    violation 是「`violation type:` / `srcs:` / `bbox = ...`」的三行重複
    結構。空檔案沒有任何 marker，因此 violation 數為 0——這是 ORFS
    自身的語意，不是本 package 對「空檔」的推測。
    """
    matched = [
        index + 1 for index, line in enumerate(lines) if ORFS_DRC_VIOLATION_MARKER in line
    ]
    if not matched:
        return CountExtraction(0, None, None, None)
    return CountExtraction(
        len(matched),
        matched[0],
        matched[-1],
        excerpt_hash(lines[matched[0] - 1].strip()),
    )


def count_log_markers(lines: list[str], *, kind: str) -> CountExtraction:
    """計算 ORFS log 中的 `[ERROR ...]`／`[WARNING ...]` marker 數。"""
    if kind == "error":
        marker = ORFS_ERROR_MARKER
    elif kind == "warning":
        marker = ORFS_WARNING_MARKER
    else:  # pragma: no cover - policy 只會用上面兩種
        return CountExtraction(None, None, None, None, problem=f"unknown_kind:{kind}")

    matched = [index + 1 for index, line in enumerate(lines) if marker.match(line.strip())]
    if not matched:
        return CountExtraction(0, None, None, None)
    return CountExtraction(
        len(matched),
        matched[0],
        matched[-1],
        excerpt_hash(lines[matched[0] - 1].strip()),
    )


def _to_number(raw: str) -> float | int | None:
    text = raw.strip()
    try:
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        return float(text)
    except ValueError:
        return None


__all__ = [
    "CountExtraction",
    "MAX_TEXT_BYTES",
    "ORFS_DRC_VIOLATION_MARKER",
    "ORFS_ERROR_MARKER",
    "ORFS_WARNING_MARKER",
    "ParseError",
    "ScalarExtraction",
    "count_drc_violations",
    "count_log_markers",
    "excerpt_hash",
    "extract_finish_scalar",
    "read_text",
    "split_finish_sections",
]
