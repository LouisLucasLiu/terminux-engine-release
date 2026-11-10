# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Report Parser — 依據 playbook parsing rules 萃取結構化指標。

從 STA timing report 檔案或 stdout lines 中，
使用 ParsingRule 定義的 regex 萃取 MetricEntry。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from terminux_engine.ecc.ecc_object import MetricEntry
from terminux_engine.ecc.playbook import ParsingRule


# ─── 內部共用邏輯 ─────────────────────────────────────────────────────────────

def _extract_metrics(
    text: str,
    parsing_rules: list[ParsingRule],
    corner: str | None,
    timestamp: float,
    source_report: str,
) -> list[MetricEntry]:
    """對文字內容做 regex 萃取，回傳 MetricEntry 清單。"""
    results: list[MetricEntry] = []
    for rule in parsing_rules:
        m = re.search(rule.pattern, text)
        if m is None:
            continue
        raw = m.group(1)
        if rule.data_type == "int":
            value = float(int(float(raw)))
        else:
            value = float(raw)
        results.append(
            MetricEntry(
                metric_name=rule.metric_name,
                value=value,
                corner=corner,
                timestamp=timestamp,
                source_report=source_report,
            )
        )
    return results


# ─── Public API ───────────────────────────────────────────────────────────────

def parse_report(
    report_path: str,
    parsing_rules: list[ParsingRule],
    corner: str | None = None,
    timestamp: float | None = None,
) -> list[MetricEntry]:
    """
    從 report 檔案中萃取指標。

    若檔案不存在，raise FileNotFoundError。
    每條匹配的 parsing rule 產生一筆 MetricEntry。
    """
    p = Path(report_path)
    if not p.exists():
        raise FileNotFoundError(f"Report file not found: {report_path}")
    text = p.read_text(encoding="utf-8")
    ts = timestamp if timestamp is not None else time.time()
    return _extract_metrics(text, parsing_rules, corner, ts, report_path)


def parse_stdout(
    stdout_lines: list[str],
    parsing_rules: list[ParsingRule],
    corner: str | None = None,
    timestamp: float | None = None,
    source_label: str = "stdout",
) -> list[MetricEntry]:
    """
    從 stdout lines 中萃取指標。

    將 stdout_lines join 成單一字串後做 regex 萃取。
    source_report 使用 source_label。
    """
    text = "\n".join(stdout_lines)
    ts = timestamp if timestamp is not None else time.time()
    return _extract_metrics(text, parsing_rules, corner, ts, source_label)
