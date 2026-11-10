# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""固定的 deterministic 候選選擇規則。

`gcd.setup_area_bounded.v1`。**不讀 LLM 敘述算分**——輸入只有

* 既有 comparability policy 的 `SelectionEligibility`（派生自 identity），
* 既有 `comparison_facts` 產生的逐指標比較（方向由 ORFS domain policy 決定，
  不是這裡猜的），
* 既有 floorplan consumption 證據裡的 `core_area_um2`。

這裡**不新增** action、不新增 LLM turn、不自行定義任何指標方向。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POLICY_ID = "gcd.setup_area_bounded.v1"
POLICY_VERSION = "v1"

#: 本 policy 直接判定的 setup 指標。方向仍取自 `comparison_facts`。
SETUP_WNS = "orfs_setup_wns"
SETUP_TNS = "orfs_setup_tns"
SETUP_VIOLATIONS = "orfs_setup_violation_count"
SETUP_METRICS: tuple[str, ...] = (SETUP_WNS, SETUP_TNS, SETUP_VIOLATIONS)

#: DQE semantic status 的嚴重度序。只用於「有沒有變差」，不用於算分。
_STATUS_SEVERITY: dict[str, int] = {"pass": 0, "warn": 1, "fail": 2}

REASON_OK = "ok"
REASON_NOT_SELECTION_ELIGIBLE = "not_selection_eligible"
REASON_IDENTITY_MISMATCH = "identity_mismatch"
REASON_METRIC_MISSING = "required_metric_missing"
REASON_METRIC_NOT_COMPARABLE = "required_metric_not_comparable"
REASON_AREA_MISSING = "core_area_missing"
REASON_AREA_OVER_LIMIT = "core_area_over_limit"
REASON_SETUP_REGRESSED = "setup_regressed"
REASON_NO_SETUP_IMPROVEMENT = "no_setup_improvement"
REASON_QUALITY_REGRESSED = "non_setup_quality_regressed"
REASON_QUALITY_UNDECIDABLE = "non_setup_quality_undecidable"


@dataclass(frozen=True)
class CandidateAssessment:
    """一個候選的資格判定。`eligible=False` 時一定帶 reason codes。"""

    slot: str
    parameter_value: str
    run_id: str
    input_manifest_sha256: str
    eligible: bool
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    setup_wns: float | None = None
    setup_tns: float | None = None
    setup_violations: float | None = None
    core_area_um2: float | None = None
    area_ratio: float | None = None
    improved_metrics: tuple[str, ...] = ()
    order_index: int = 0

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot, "parameter_value": self.parameter_value,
            "run_id": self.run_id,
            "input_manifest_sha256": self.input_manifest_sha256,
            "selection_eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "setup_wns": self.setup_wns, "setup_tns": self.setup_tns,
            "setup_violation_count": self.setup_violations,
            "core_area_um2": self.core_area_um2, "area_ratio": self.area_ratio,
            "improved_metrics": list(self.improved_metrics),
            "order_index": self.order_index,
        }


@dataclass(frozen=True)
class SelectionResult:
    """選擇結果紀錄。**沒有**新的 execution authority。"""

    policy_id: str
    policy_version: str
    area_ratio_limit: float
    baseline_slot: str
    baseline_run_id: str
    baseline_core_area_um2: float | None
    baseline_setup: dict[str, float | None]
    candidates: tuple[CandidateAssessment, ...]
    selected_slot: str
    selected_run_id: str
    ordering: tuple[str, ...]
    ordering_basis: tuple[str, ...] = field(default=(
        "setup_wns desc", "setup_tns desc", "setup_violation_count asc",
        "core_area_um2 asc", "profile candidate order"))
    #: 固定用語。不得改寫成「整體最佳」或「timing 已修復」。
    statement: str = ("本次 setup timing／面積與已觀察品質規則下的選擇")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id, "policy_version": self.policy_version,
            "area_ratio_limit": self.area_ratio_limit,
            "baseline_slot": self.baseline_slot,
            "baseline_run_id": self.baseline_run_id,
            "baseline_core_area_um2": self.baseline_core_area_um2,
            "baseline_setup": dict(self.baseline_setup),
            "candidates": [c.to_json_dict() for c in self.candidates],
            "selected_slot": self.selected_slot,
            "selected_run_id": self.selected_run_id,
            "ordering": list(self.ordering),
            "ordering_basis": list(self.ordering_basis),
            "statement": self.statement,
        }


def _as_float(value: Any) -> float | None:
    """沿正式 parser 的原精度。不用顯示用的四捨五入字串比較。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _comparison_index(comparisons: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for entry in comparisons or ():
        item = entry if isinstance(entry, dict) else entry.to_json_dict()
        metric_id = item.get("metric_id")
        if metric_id:
            index[str(metric_id)] = item
    return index


def assess_candidate(
    *,
    slot: str,
    parameter_value: str,
    run_id: str,
    input_manifest_sha256: str,
    order_index: int,
    eligibility: Any,
    metric_comparisons: Any,
    baseline_core_area_um2: Any,
    candidate_core_area_um2: Any,
    area_ratio_limit: float,
    identity_ok: bool = True,
    identity_reason: str | None = None,
) -> CandidateAssessment:
    """逐項判定五條選擇規則。任何一條不成立就不具選擇資格。"""
    codes: list[str] = []
    reasons: list[str] = []
    index = _comparison_index(metric_comparisons)

    def _fail(code: str, reason: str) -> None:
        if code not in codes:
            codes.append(code)
        reasons.append(reason)

    # 1. 證據品質與身分。`eligibility` 可能是 dataclass 或它的 JSON 形式。
    if eligibility is None:
        eligible_flag, detail_reasons = False, ("no comparison eligibility was recorded",)
    elif isinstance(eligibility, dict):
        eligible_flag = bool(eligibility.get("selection_eligible"))
        detail_reasons = tuple(eligibility.get("reasons") or ())
    else:
        eligible_flag = bool(getattr(eligibility, "eligible", False))
        detail_reasons = tuple(getattr(eligibility, "reasons", ()) or ())
    if not eligible_flag:
        detail = ", ".join(detail_reasons)
        _fail(REASON_NOT_SELECTION_ELIGIBLE,
              f"the comparison is not selection-eligible: {detail}")
    if not identity_ok:
        _fail(REASON_IDENTITY_MISMATCH,
              identity_reason or "the run identity does not match this task slot")

    # 2. 三個 setup 指標必須是同定義／同單位的有效數值。
    setup_values: dict[str, float | None] = {}
    for metric_id in SETUP_METRICS:
        item = index.get(metric_id)
        if item is None:
            _fail(REASON_METRIC_MISSING, f"{metric_id} is missing from the comparison")
            setup_values[metric_id] = None
            continue
        if item.get("comparison") == "not_comparable":
            _fail(REASON_METRIC_NOT_COMPARABLE,
                  f"{metric_id} is not comparable: {item.get('reason')}")
        value = _as_float(item.get("candidate_value"))
        base_value = _as_float(item.get("baseline_value"))
        if value is None or base_value is None:
            _fail(REASON_METRIC_MISSING,
                  f"{metric_id} has no numeric value on at least one side")
        setup_values[metric_id] = value

    # 3. core area 上限（單位固定 um²，不用 die area／cell area 代替）。
    base_area = _as_float(baseline_core_area_um2)
    cand_area = _as_float(candidate_core_area_um2)
    ratio: float | None = None
    if base_area is None or cand_area is None or base_area <= 0:
        _fail(REASON_AREA_MISSING,
              "the floorplan core area is unavailable on at least one side")
    else:
        ratio = cand_area / base_area
        if ratio > area_ratio_limit:
            _fail(REASON_AREA_OVER_LIMIT,
                  f"core area ratio {ratio:.4f} exceeds the limit "
                  f"{area_ratio_limit}")

    # 4. setup 三項都不得變差，且至少一項嚴格改善。
    improved: list[str] = []
    for metric_id in SETUP_METRICS:
        item = index.get(metric_id)
        if item is None:
            continue
        outcome = item.get("comparison")
        if outcome == "degraded":
            _fail(REASON_SETUP_REGRESSED, f"{metric_id} regressed against the baseline")
        elif outcome == "improved":
            improved.append(metric_id)
    if not improved:
        _fail(REASON_NO_SETUP_IMPROVEMENT,
              "no setup metric improved; the baseline is not replaced")

    # 5. 非 setup 的既有品質項不得新增違規或惡化。方向與門檻沿既有規則。
    for metric_id, item in sorted(index.items()):
        if metric_id in SETUP_METRICS:
            continue
        if item.get("comparison") == "degraded":
            _fail(REASON_QUALITY_REGRESSED,
                  f"{metric_id} regressed against the baseline")
            continue
        if item.get("comparison") == "not_comparable":
            _fail(REASON_QUALITY_UNDECIDABLE,
                  f"{metric_id} could not be compared: {item.get('reason')}")
            continue
        base_status = item.get("baseline_status")
        cand_status = item.get("candidate_status")
        if item.get("status_comparison") != "comparable":
            _fail(REASON_QUALITY_UNDECIDABLE,
                  f"{metric_id} status could not be compared: "
                  f"{item.get('status_reason')}")
            continue
        base_rank = _STATUS_SEVERITY.get(str(base_status))
        cand_rank = _STATUS_SEVERITY.get(str(cand_status))
        if base_rank is None or cand_rank is None:
            _fail(REASON_QUALITY_UNDECIDABLE,
                  f"{metric_id} has an unknown semantic status "
                  f"({base_status} → {cand_status})")
        elif cand_rank > base_rank:
            _fail(REASON_QUALITY_REGRESSED,
                  f"{metric_id} status worsened: {base_status} → {cand_status}")

    return CandidateAssessment(
        slot=slot, parameter_value=parameter_value, run_id=run_id,
        input_manifest_sha256=input_manifest_sha256,
        eligible=not codes,
        reason_codes=tuple(codes) if codes else (REASON_OK,),
        reasons=tuple(reasons) if reasons else (
            "every selection rule is satisfied",),
        setup_wns=setup_values.get(SETUP_WNS),
        setup_tns=setup_values.get(SETUP_TNS),
        setup_violations=setup_values.get(SETUP_VIOLATIONS),
        core_area_um2=cand_area, area_ratio=ratio,
        improved_metrics=tuple(improved), order_index=order_index)


def select_best(
    *,
    baseline_slot: str,
    baseline_run_id: str,
    baseline_core_area_um2: Any,
    baseline_setup: dict[str, Any],
    assessments: tuple[CandidateAssessment, ...],
    area_ratio_limit: float,
) -> SelectionResult:
    """合格候選排序後取第一名；沒有合格改善就保留 baseline。"""
    eligible = [a for a in assessments if a.eligible]

    def _key(a: CandidateAssessment) -> tuple[Any, ...]:
        # setup WNS 較大 → TNS 較大 → 違規較少 → core area 較小 →
        # profile 候選序列（穩定，不用 run_id 之類的偶然值打破同分）。
        return (
            -(a.setup_wns if a.setup_wns is not None else float("-inf")),
            -(a.setup_tns if a.setup_tns is not None else float("-inf")),
            a.setup_violations if a.setup_violations is not None else float("inf"),
            a.core_area_um2 if a.core_area_um2 is not None else float("inf"),
            a.order_index,
        )

    ordered = sorted(eligible, key=_key)
    if ordered:
        winner = ordered[0]
        selected_slot, selected_run = winner.slot, winner.run_id
    else:
        selected_slot, selected_run = baseline_slot, baseline_run_id

    return SelectionResult(
        policy_id=POLICY_ID, policy_version=POLICY_VERSION,
        area_ratio_limit=float(area_ratio_limit),
        baseline_slot=baseline_slot, baseline_run_id=baseline_run_id,
        baseline_core_area_um2=_as_float(baseline_core_area_um2),
        baseline_setup={k: _as_float(v) for k, v in baseline_setup.items()},
        candidates=tuple(assessments),
        selected_slot=selected_slot, selected_run_id=selected_run,
        ordering=tuple(a.slot for a in ordered))


__all__ = [
    "POLICY_ID", "POLICY_VERSION", "SETUP_METRICS",
    "REASON_AREA_MISSING", "REASON_AREA_OVER_LIMIT", "REASON_IDENTITY_MISMATCH",
    "REASON_METRIC_MISSING", "REASON_METRIC_NOT_COMPARABLE",
    "REASON_NOT_SELECTION_ELIGIBLE", "REASON_NO_SETUP_IMPROVEMENT",
    "REASON_OK", "REASON_QUALITY_REGRESSED", "REASON_QUALITY_UNDECIDABLE",
    "REASON_SETUP_REGRESSED",
    "CandidateAssessment", "SelectionResult", "assess_candidate", "select_best",
]
