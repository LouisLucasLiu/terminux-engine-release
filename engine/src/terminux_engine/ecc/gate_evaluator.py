# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Quality Gate Evaluator — 依 playbook 門檻對 ECC Object 指標做 deterministic 評估。

純比較運算，不做 AI 解釋。
"""

from __future__ import annotations

import time

from terminux_engine.ecc.ecc_object import ECCObject, GateResult, MetricEntry
from terminux_engine.ecc.playbook import QualityGate


# ─── 內部函式 ─────────────────────────────────────────────────────────────────

def _compare(actual: float, operator: str, threshold: float) -> bool:
    """依 operator 比較 actual 與 threshold。"""
    if operator == ">=":
        return actual >= threshold
    if operator == "<=":
        return actual <= threshold
    if operator == "==":
        return actual == threshold
    if operator == ">":
        return actual > threshold
    if operator == "<":
        return actual < threshold
    return False


def _find_latest_metric(
    metrics: list[MetricEntry],
    metric_name: str,
    corner: str | None = None,
) -> MetricEntry | None:
    """從 metric_records 中找到指定 metric_name（+ optional corner）的最新一筆。"""
    best: MetricEntry | None = None
    for m in metrics:
        if m.metric_name != metric_name:
            continue
        if corner is not None and m.corner != corner:
            continue
        if best is None or m.timestamp > best.timestamp:
            best = m
    return best


def _find_metrics_by_corner(
    metrics: list[MetricEntry],
    metric_name: str,
) -> dict[str, MetricEntry]:
    """
    從 metric_records 中找到指定 metric_name 的所有 corner 的最新一筆。
    回傳 {corner: MetricEntry}。只包含 corner 不為 None 的 entries。
    """
    latest: dict[str, MetricEntry] = {}
    for m in metrics:
        if m.metric_name != metric_name or m.corner is None:
            continue
        prev = latest.get(m.corner)
        if prev is None or m.timestamp > prev.timestamp:
            latest[m.corner] = m
    return latest


# ─── Public API ───────────────────────────────────────────────────────────────

def evaluate_gates(
    ecc: ECCObject,
    gates: list[QualityGate],
    corner: str | None = None,
) -> list[GateResult]:
    """
    依 playbook quality gates 評估 ECC Object 中的指標。

    若找不到對應 metric，該 gate 不產生 GateResult。
    """
    now = time.time()
    stage_id = ecc.current_stage_id or ""
    results: list[GateResult] = []

    for gate in gates:
        if gate.scope == "ALL_CORNERS":
            by_corner = _find_metrics_by_corner(ecc.metric_records, gate.metric)
            for c, m in by_corner.items():
                passed = _compare(m.value, gate.operator, gate.threshold)
                results.append(GateResult(
                    gate_id=gate.id,
                    metric_name=gate.metric,
                    actual_value=m.value,
                    threshold=gate.threshold,
                    operator=gate.operator,
                    passed=passed,
                    corner=c,
                    timestamp=now,
                    stage_id=stage_id,
                ))
        else:
            m = _find_latest_metric(ecc.metric_records, gate.metric, corner=corner)
            if m is None:
                continue
            passed = _compare(m.value, gate.operator, gate.threshold)
            results.append(GateResult(
                gate_id=gate.id,
                metric_name=gate.metric,
                actual_value=m.value,
                threshold=gate.threshold,
                operator=gate.operator,
                passed=passed,
                corner=m.corner,
                timestamp=now,
                stage_id=stage_id,
            ))

    return results
