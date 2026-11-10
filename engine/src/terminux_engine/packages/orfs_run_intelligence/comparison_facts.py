# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Deterministic run-comparison facts。

方向（improved／degraded）由**這裡**的 ORFS domain policy 定義，Common
不得以數值正負猜測。LLM 只能引用這些 pair，不得重算或覆寫
。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.packages.orfs_run_intelligence.comparison import ComparisonDecision

IMPROVED = "improved"
DEGRADED = "degraded"
UNCHANGED = "unchanged"
NOT_COMPARABLE = "not_comparable"

#: metric_id → 方向語意。`higher_is_better` / `lower_is_better` 是 ORFS
#: signoff 語意，不是通則：WNS／TNS 是負值 slack，越接近 0 越好，所以
#: 數值「變大」才是改善；violation count 則越小越好。
_DIRECTION: dict[str, str] = {
    "orfs_setup_wns": "higher_is_better",
    "orfs_setup_tns": "higher_is_better",
    "orfs_setup_violation_count": "lower_is_better",
    "orfs_hold_violation_count": "lower_is_better",
    "orfs_max_slew_violation_count": "lower_is_better",
    "orfs_max_fanout_violation_count": "lower_is_better",
    "orfs_max_cap_violation_count": "lower_is_better",
    "orfs_route_drc_violation_count": "lower_is_better",
    "orfs_log_error_markers": "lower_is_better",
    "orfs_log_warning_markers": "lower_is_better",
}

#: 這些 check 沒有有意義的數值方向（它們是 pass/fail 型 gate）。
_NON_DIRECTIONAL: frozenset[str] = frozenset({
    "orfs_process_exit_code",
    "orfs_manifest_integrity",
    "orfs_artifact_completeness",
    "orfs_final_deliverables",
})

#: 浮點比較容差。ORFS report 的 WNS／TNS 印到小數第二位，因此 1e-9 只用來
#: 吸收 float 表示誤差，不用來掩蓋真實差異。
FLOAT_TOLERANCE = 1e-9

#: 這兩個 semantic status 代表「不知道」，任何跨 run 的狀態比較都不成立。
_UNRESOLVED_STATUSES = frozenset({"unknown", "legacy_ambiguous"})


#: Status 層比較的結果。刻意與 `comparison`（數值方向）分開：一側是
#: `legacy_ambiguous` 時，值與單位仍然可比，但「狀態有沒有變好」不成立。
STATUS_COMPARABLE = "comparable"
STATUS_UNRESOLVED = "unresolved"
STATUS_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class MetricComparison:
    metric_id: str
    unit: str | None
    baseline_value: Any
    candidate_value: Any
    delta: float | None
    baseline_status: str | None
    candidate_status: str | None
    comparison: str
    status_comparison: str = STATUS_NOT_APPLICABLE
    status_reason: str | None = None
    reason: str | None = None
    evidence_ref_ids: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "unit": self.unit,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "delta": self.delta,
            "baseline_status": self.baseline_status,
            "candidate_status": self.candidate_status,
            "comparison": self.comparison,
            "status_comparison": self.status_comparison,
            "status_reason": self.status_reason,
            "reason": self.reason,
            "evidence_ref_ids": list(self.evidence_ref_ids),
        }


def _numeric(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw))
    except ValueError:
        return None


def _row_view(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["DQE_ID"]: r for r in rows}


def compare_metrics(
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    decision: ComparisonDecision,
    *,
    evidence_ref_ids: dict[str, tuple[str, ...]] | None = None,
) -> list[MetricComparison]:
    """為每個 check 建立一筆 deterministic pair。

    只要 pair 不可比較（缺一側、單位不同、狀態未定、policy 判 not_comparable）
    就不產生有方向的結論——回 `not_comparable` 並寫明理由。
    """
    refs = evidence_ref_ids or {}
    base, cand = _row_view(baseline_rows), _row_view(candidate_rows)
    directional_allowed = decision.allows_directional_conclusions
    results: list[MetricComparison] = []

    for metric_id in sorted(set(base) | set(cand)):
        b, c = base.get(metric_id), cand.get(metric_id)
        b_unit = (b or {}).get("UNIT") or None
        c_unit = (c or {}).get("UNIT") or None
        b_val = (b or {}).get("VALUE")
        c_val = (c or {}).get("VALUE")
        b_status = (b or {}).get("semantic_status")
        c_status = (c or {}).get("semantic_status")
        ref_ids = refs.get(metric_id, ())

        # Status 層與數值層分開判定：`legacy_ambiguous`／`unknown` 只讓
        # 狀態比較失效，不會讓可比的數值失去方向。
        if b_status is None or c_status is None:
            status_cmp, status_reason = STATUS_NOT_APPLICABLE, "status missing on one side"
        elif b_status in _UNRESOLVED_STATUSES or c_status in _UNRESOLVED_STATUSES:
            status_cmp = STATUS_UNRESOLVED
            status_reason = (
                f"semantic status is unresolved on at least one side "
                f"(baseline={b_status}, candidate={c_status}); that DQE row predates "
                f"exact semantic status or the check could not be evaluated")
        else:
            status_cmp, status_reason = STATUS_COMPARABLE, None

        def emit(comparison: str, delta: float | None = None, reason: str | None = None):
            results.append(MetricComparison(
                metric_id=metric_id, unit=b_unit or c_unit,
                baseline_value=b_val, candidate_value=c_val, delta=delta,
                baseline_status=b_status, candidate_status=c_status,
                comparison=comparison, status_comparison=status_cmp,
                status_reason=status_reason, reason=reason, evidence_ref_ids=ref_ids,
            ))

        if b is None or c is None:
            emit(NOT_COMPARABLE, reason=(
                f"metric is present only on the "
                f"{'candidate' if b is None else 'baseline'} run"))
            continue
        if not directional_allowed:
            emit(NOT_COMPARABLE, reason=(
                f"the run pair is {decision.status}; no directional conclusion is allowed"))
            continue
        if b_unit != c_unit:
            emit(NOT_COMPARABLE, reason=(
                f"unit mismatch: baseline {b_unit!r} vs candidate {c_unit!r}"))
            continue
        if metric_id in _NON_DIRECTIONAL:
            emit(UNCHANGED if b_val == c_val else NOT_COMPARABLE,
                 reason=None if b_val == c_val else
                 "this check is a pass/fail gate with no meaningful numeric direction")
            continue

        bn, cn = _numeric(b_val), _numeric(c_val)
        if bn is None or cn is None:
            emit(UNCHANGED if b_val == c_val else NOT_COMPARABLE,
                 reason=None if b_val == c_val else "value is not numeric on at least one side")
            continue

        delta = cn - bn
        direction = _DIRECTION.get(metric_id)
        if direction is None:
            emit(NOT_COMPARABLE, delta=delta,
                 reason="no ORFS direction policy is declared for this metric")
            continue
        if abs(delta) <= FLOAT_TOLERANCE:
            emit(UNCHANGED, delta=0.0)
            continue
        better = delta > 0 if direction == "higher_is_better" else delta < 0
        emit(IMPROVED if better else DEGRADED, delta=delta)

    return results


def summarize(comparisons: list[MetricComparison]) -> dict[str, Any]:
    counts = {IMPROVED: 0, DEGRADED: 0, UNCHANGED: 0, NOT_COMPARABLE: 0}
    status_counts = {STATUS_COMPARABLE: 0, STATUS_UNRESOLVED: 0, STATUS_NOT_APPLICABLE: 0}
    for c in comparisons:
        counts[c.comparison] = counts.get(c.comparison, 0) + 1
        status_counts[c.status_comparison] = status_counts.get(c.status_comparison, 0) + 1
    return {"value_direction": counts, "status_comparison": status_counts}


__all__ = [
    "DEGRADED",
    "STATUS_COMPARABLE",
    "STATUS_NOT_APPLICABLE",
    "STATUS_UNRESOLVED",
    "FLOAT_TOLERANCE",
    "IMPROVED",
    "MetricComparison",
    "NOT_COMPARABLE",
    "UNCHANGED",
    "compare_metrics",
    "summarize",
]
