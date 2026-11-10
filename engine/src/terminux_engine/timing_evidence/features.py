# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""單一 setup path 的診斷特徵：純函式、無 I/O、不含任何工具或 library 知識。

每個特徵都有三種狀態：

- `derived`：值可由 path 證據直接算出，並附上推導假設（封閉的假設碼）；
- `not_derivable`：缺欄位、分母非正、逐級和不一致等，附原因碼，**不以 0 代替**；
- `ambiguous`：值可算，但在報告解析度下排名或身分無法唯一判定；候選全部保留。

原因碼與假設碼都是封閉詞彙（`FEATURE_REASON_CODES`、`ASSUMPTION_CODES`），對外投影
只接受這些值。數值以報告宣告的解析度做定點運算（`Decimal`），避免浮點累加造成假差異。
特徵只描述延遲組成，不判定 root cause，也不提出修正。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from terminux_engine.timing_evidence.model import (
    CLOCK_PROPAGATED,
    ENDPOINT_REGISTER,
    LOGIC_ROLES,
    ROLE_BUFFER,
    ROLE_INVERTER,
    ROLE_SEQUENTIAL_LAUNCH,
    ROLE_UNKNOWN,
    PathStage,
    TimingPathEvidence,
)

FEATURES_SCHEMA_VERSION = "terminux.timing_path_features.v1"

STATUS_DERIVED = "derived"
STATUS_NOT_DERIVABLE = "not_derivable"
STATUS_AMBIGUOUS = "ambiguous"
FEATURE_STATUSES = frozenset({STATUS_DERIVED, STATUS_NOT_DERIVABLE, STATUS_AMBIGUOUS})

FEATURE_DATA_PATH_DELAY = "data_path_delay"
FEATURE_MAX_STAGE = "max_stage_contribution"
FEATURE_CELL_VS_NET = "cell_vs_net_delay"
FEATURE_LOGIC_DEPTH = "logic_depth"
FEATURE_CLOCK_SKEW = "path_clock_skew"
FEATURE_IDS = (
    FEATURE_DATA_PATH_DELAY, FEATURE_MAX_STAGE, FEATURE_CELL_VS_NET, FEATURE_LOGIC_DEPTH,
    FEATURE_CLOCK_SKEW,
)

# 原因碼（穩定字串；測試與文件直接引用）
REASON_STARTPOINT_IS_PORT = "startpoint_is_port_v1_out_of_scope"
REASON_LAUNCH_CLOCK_ARRIVAL_MISSING = "launch_clock_arrival_missing"
REASON_ENDPOINT_ARRIVAL_MISSING = "endpoint_arrival_missing"
REASON_NON_POSITIVE_DENOMINATOR = "non_positive_denominator"
REASON_STAGES_INCOMPLETE = "stages_incomplete"
REASON_NO_STAGES = "no_stages"
REASON_STAGE_INCREMENT_MISSING = "stage_increment_missing"
REASON_NEGATIVE_INCREMENT = "negative_increment"
REASON_SUM_INCONSISTENT = "sum_inconsistent"
REASON_DELTA_T_UNAVAILABLE = "delta_t_unavailable"
REASON_RANKING_TIE = "ranking_tie_at_resolution"
REASON_RANKING_DIFFERS = "ranking_differs_between_delay_column_and_delta_t"
REASON_ROLE_UNKNOWN = "role_unknown"
REASON_CLOCK_NOT_PROPAGATED = "clock_not_propagated"
REASON_CLOCK_ARRIVAL_MISSING = "clock_arrival_missing"
REASON_CRPR_MISSING = "crpr_missing"
REASON_NO_COMBINATIONAL_STAGE = "no_combinational_stage"

_BASE_REASONS = frozenset({
    REASON_STARTPOINT_IS_PORT, REASON_LAUNCH_CLOCK_ARRIVAL_MISSING, REASON_ENDPOINT_ARRIVAL_MISSING,
    REASON_NON_POSITIVE_DENOMINATOR, REASON_STAGES_INCOMPLETE, REASON_NO_STAGES,
    REASON_STAGE_INCREMENT_MISSING, REASON_NEGATIVE_INCREMENT, REASON_SUM_INCONSISTENT,
    REASON_ROLE_UNKNOWN, REASON_CLOCK_NOT_PROPAGATED, REASON_CLOCK_ARRIVAL_MISSING,
    REASON_CRPR_MISSING, REASON_NO_COMBINATIONAL_STAGE,
})
_RANKING_REASONS = (REASON_DELTA_T_UNAVAILABLE, REASON_RANKING_TIE, REASON_RANKING_DIFFERS)
#: 對外投影接受的全部原因碼（含最大 stage 子結果的前綴形式）。
FEATURE_REASON_CODES = _BASE_REASONS | frozenset(
    f"{prefix}:{reason}" for prefix in ("max_stage", "max_combinational_stage")
    for reason in _RANKING_REASONS)

# 假設碼（封閉；對外投影只送碼，不送散文）
ASSUMPTION_DISPLAY_RESOLUTION = "values_as_displayed_increments_below_half_resolution_show_zero"
ASSUMPTION_DATA_PATH_EXCLUDES_CLOCK_SETUP_EXTERNAL = "data_path_excludes_launch_clock_network_setup_and_external_delay"
ASSUMPTION_STAGE_IS_CELL_ARC = "stage_is_one_cell_arc_launch_clock_to_q_included_in_max_stage_only"
ASSUMPTION_SHARE_OF_DATA_PATH = "share_is_displayed_increment_over_data_path_delay"
ASSUMPTION_NET_ZERO_IS_DISPLAY = "displayed_net_zero_means_below_half_resolution_not_zero_wire_delay"
ASSUMPTION_NET_UPPER_BOUND = "net_upper_bound_adds_half_resolution_per_net_arc_bound_not_measurement"
ASSUMPTION_LOGIC_DEPTH_DEFINITION = "logic_depth_counts_cell_arcs_between_launch_output_and_endpoint_data_pin"
ASSUMPTION_ROLES_FROM_ADAPTER = "buffer_inverter_roles_from_tool_adapter_unknown_role_withholds_exclusions"
ASSUMPTION_SKEW_DEFINITION = "skew_is_capture_latency_minus_launch_latency_plus_crpr_for_this_path_only"
ASSUMPTION_CODES = frozenset({
    ASSUMPTION_DISPLAY_RESOLUTION, ASSUMPTION_DATA_PATH_EXCLUDES_CLOCK_SETUP_EXTERNAL,
    ASSUMPTION_STAGE_IS_CELL_ARC, ASSUMPTION_SHARE_OF_DATA_PATH, ASSUMPTION_NET_ZERO_IS_DISPLAY,
    ASSUMPTION_NET_UPPER_BOUND, ASSUMPTION_LOGIC_DEPTH_DEFINITION, ASSUMPTION_ROLES_FROM_ADAPTER,
    ASSUMPTION_SKEW_DEFINITION,
})

#: 比例的小數位數（0.3 ＝ 30%）。
_SHARE_PLACES = 3


@dataclass(frozen=True)
class Feature:
    """一個特徵的結果。`value` 只含純量與純量的小清單，可直接 JSON 序列化。"""

    feature_id: str
    status: str
    value: dict[str, Any]
    reasons: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id, "status": self.status,
            "value": dict(self.value), "reasons": list(self.reasons),
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True)
class FeatureSet:
    data_path_delay: Feature
    max_stage_contribution: Feature
    cell_vs_net_delay: Feature
    logic_depth: Feature
    path_clock_skew: Feature

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FEATURES_SCHEMA_VERSION,
            FEATURE_DATA_PATH_DELAY: self.data_path_delay.to_json_dict(),
            FEATURE_MAX_STAGE: self.max_stage_contribution.to_json_dict(),
            FEATURE_CELL_VS_NET: self.cell_vs_net_delay.to_json_dict(),
            FEATURE_LOGIC_DEPTH: self.logic_depth.to_json_dict(),
            FEATURE_CLOCK_SKEW: self.path_clock_skew.to_json_dict(),
        }


# ── 定點運算 helpers ───────────────────────────────────────────────────


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def _quantum(resolution: float) -> Decimal:
    return _dec(resolution)


def _q(value: Decimal, resolution: float) -> float:
    """量化到報告解析度後轉回 float（輸出用）。"""
    return float(value.quantize(_quantum(resolution), rounding=ROUND_HALF_UP))


def _share(numerator: Decimal, denominator: Decimal) -> float:
    return float((numerator / denominator).quantize(Decimal(1).scaleb(-_SHARE_PLACES),
                                                    rounding=ROUND_HALF_UP))


# ── 共用前提 ───────────────────────────────────────────────────────────


def _data_path_delay(path: TimingPathEvidence) -> tuple[Decimal | None, list[str]]:
    """F0：endpoint data arrival − startpoint clock arrival。回傳 (值, 原因碼)。"""
    reasons: list[str] = []
    if path.endpoints.startpoint_kind != ENDPOINT_REGISTER:
        reasons.append(REASON_STARTPOINT_IS_PORT)
    if path.timing.launch_clock_arrival is None:
        reasons.append(REASON_LAUNCH_CLOCK_ARRIVAL_MISSING)
    if path.timing.data_arrival is None:
        reasons.append(REASON_ENDPOINT_ARRIVAL_MISSING)
    if reasons:
        return None, reasons
    value = _dec(path.timing.data_arrival) - _dec(path.timing.launch_clock_arrival)
    if value <= 0:
        return None, [REASON_NON_POSITIVE_DENOMINATOR]
    return value, []


def _stage_preconditions(path: TimingPathEvidence) -> list[str]:
    reasons: list[str] = []
    if not path.completeness.stages_complete:
        reasons.append(REASON_STAGES_INCOMPLETE)
    if not path.stages:
        reasons.append(REASON_NO_STAGES)
    if any(stage.incr is None for stage in path.stages):
        reasons.append(REASON_STAGE_INCREMENT_MISSING)
    elif any(stage.incr < 0 for stage in path.stages):
        reasons.append(REASON_NEGATIVE_INCREMENT)
    return reasons


def _delta_t(path: TimingPathEvidence) -> dict[int, Decimal] | None:
    """以相鄰 arrival 的差重算每個 stage 的增量；任何 arrival 缺席即 None。"""
    previous = path.timing.launch_clock_arrival
    if previous is None:
        return None
    result: dict[int, Decimal] = {}
    for stage in path.stages:
        if stage.arrival is None:
            return None
        result[stage.index] = _dec(stage.arrival) - _dec(previous)
        previous = stage.arrival
    return result


def _stage_summary(stage: PathStage) -> dict[str, Any]:
    return {
        "stage_index": stage.index, "line": stage.line, "cell_function": stage.cell_function,
        "role": stage.role,
    }


# ── 特徵 ───────────────────────────────────────────────────────────────


def feature_data_path_delay(path: TimingPathEvidence) -> Feature:
    value, reasons = _data_path_delay(path)
    if value is None:
        return Feature(FEATURE_DATA_PATH_DELAY, STATUS_NOT_DERIVABLE, {}, tuple(reasons))
    return Feature(
        FEATURE_DATA_PATH_DELAY, STATUS_DERIVED,
        {
            "value": _q(value, path.timing.resolution), "unit": path.timing.unit_time,
            "launch_clock_arrival": path.timing.launch_clock_arrival,
            "data_arrival": path.timing.data_arrival,
        },
        assumptions=(ASSUMPTION_DATA_PATH_EXCLUDES_CLOCK_SETUP_EXTERNAL, ASSUMPTION_DISPLAY_RESOLUTION),
    )


def _max_over(stages: tuple[PathStage, ...], delta_t: dict[int, Decimal] | None,
              denominator: Decimal, resolution: float) -> tuple[dict[str, Any], bool, list[str]]:
    """在一組 stage 上找 Delay 欄最大者；回傳 (結果, 是否歧義, 原因)。"""
    by_delay = max(_dec(stage.incr) for stage in stages)  # type: ignore[arg-type]
    ties = [stage for stage in stages if _dec(stage.incr) == by_delay]  # type: ignore[arg-type]
    reasons: list[str] = []
    ambiguous = False
    delta_candidates: list[int] = []
    if delta_t is None:
        ambiguous = True
        reasons.append(REASON_DELTA_T_UNAVAILABLE)
    else:
        peak = max(delta_t[stage.index] for stage in stages)
        delta_candidates = [stage.index for stage in stages if delta_t[stage.index] == peak]
        if set(delta_candidates) != {stage.index for stage in ties}:
            ambiguous = True
            reasons.append(REASON_RANKING_DIFFERS)
    if len(ties) > 1:
        ambiguous = True
        reasons.append(REASON_RANKING_TIE)
    leader = ties[0]
    result = {
        "value": _q(by_delay, resolution), "share": _share(by_delay, denominator),
        **_stage_summary(leader),
        "ties": [stage.index for stage in ties],
        "delta_t_candidates": delta_candidates,
        "ranking_ambiguous": ambiguous,
    }
    return result, ambiguous, reasons


def feature_max_stage_contribution(path: TimingPathEvidence) -> Feature:
    denominator, reasons = _data_path_delay(path)
    reasons = reasons + _stage_preconditions(path)
    cells = path.cell_stages
    if not cells and REASON_NO_STAGES not in reasons:
        reasons.append(REASON_NO_STAGES)
    if denominator is None or reasons:
        return Feature(FEATURE_MAX_STAGE, STATUS_NOT_DERIVABLE, {}, tuple(dict.fromkeys(reasons)))
    resolution = path.timing.resolution
    delta_t = _delta_t(path)
    overall, overall_ambiguous, overall_reasons = _max_over(cells, delta_t, denominator, resolution)
    combinational_stages = tuple(stage for stage in cells if stage.role != ROLE_SEQUENTIAL_LAUNCH)
    value: dict[str, Any] = {
        "unit": path.timing.unit_time, "data_path_delay": _q(denominator, resolution),
        "max_stage": overall,
    }
    ambiguous = overall_ambiguous
    all_reasons = [f"max_stage:{reason}" for reason in overall_reasons]
    if combinational_stages:
        combinational, comb_ambiguous, comb_reasons = _max_over(
            combinational_stages, delta_t, denominator, resolution)
        value["max_combinational_stage"] = combinational
        ambiguous = ambiguous or comb_ambiguous
        all_reasons.extend(f"max_combinational_stage:{reason}" for reason in comb_reasons)
    else:
        value["max_combinational_stage"] = None
        all_reasons.append(REASON_NO_COMBINATIONAL_STAGE)
    nets = path.net_stages
    if nets:
        peak = max(_dec(stage.incr) for stage in nets)  # type: ignore[arg-type]
        net_ties = [stage.index for stage in nets if _dec(stage.incr) == peak]  # type: ignore[arg-type]
        value["max_net_arc"] = {
            "value": _q(peak, resolution), "share": _share(peak, denominator),
            "stage_index": net_ties[0] if len(net_ties) == 1 else None,
            "tie_count": len(net_ties),
        }
    else:
        value["max_net_arc"] = None
    return Feature(
        FEATURE_MAX_STAGE, STATUS_AMBIGUOUS if ambiguous else STATUS_DERIVED, value,
        tuple(all_reasons),
        assumptions=(ASSUMPTION_STAGE_IS_CELL_ARC, ASSUMPTION_SHARE_OF_DATA_PATH,
                     ASSUMPTION_DISPLAY_RESOLUTION),
    )


def feature_cell_vs_net_delay(path: TimingPathEvidence) -> Feature:
    denominator, reasons = _data_path_delay(path)
    reasons = reasons + _stage_preconditions(path)
    if denominator is None or reasons:
        return Feature(FEATURE_CELL_VS_NET, STATUS_NOT_DERIVABLE, {}, tuple(dict.fromkeys(reasons)))
    resolution = path.timing.resolution
    quantum = _quantum(resolution)
    cells, nets = path.cell_stages, path.net_stages
    cell_sum = sum((_dec(stage.incr) for stage in cells), Decimal(0))  # type: ignore[arg-type]
    net_sum = sum((_dec(stage.incr) for stage in nets), Decimal(0))  # type: ignore[arg-type]
    residual = denominator - (cell_sum + net_sum)
    tolerance = quantum / 2 * (len(cells) + len(nets))
    if abs(residual) > tolerance:
        return Feature(
            FEATURE_CELL_VS_NET, STATUS_NOT_DERIVABLE,
            {"residual": float(residual), "tolerance": float(tolerance)},
            (REASON_SUM_INCONSISTENT,),
        )
    # 每個顯示為 0 的 net arc 實際可能接近半個解析度：上限 ＝ 顯示值 ＋ n × resolution／2。
    net_upper = net_sum + quantum / 2 * len(nets)
    return Feature(
        FEATURE_CELL_VS_NET, STATUS_DERIVED,
        {
            "unit": path.timing.unit_time, "resolution": resolution,
            "data_path_delay": _q(denominator, resolution),
            "cell_count": len(cells), "net_count": len(nets),
            "cell_sum": _q(cell_sum, resolution), "net_sum_shown": _q(net_sum, resolution),
            "residual": _q(residual, resolution), "tolerance": float(tolerance),
            "cell_share_shown": _share(cell_sum, denominator),
            "net_share_shown": _share(net_sum, denominator),
            "net_sum_upper_bound": _q(net_upper, resolution),
            "net_share_upper_bound": _share(net_upper, denominator),
        },
        assumptions=(ASSUMPTION_NET_ZERO_IS_DISPLAY, ASSUMPTION_NET_UPPER_BOUND,
                     ASSUMPTION_DISPLAY_RESOLUTION),
    )


def feature_logic_depth(path: TimingPathEvidence) -> Feature:
    reasons = [r for r in _stage_preconditions(path)
               if r in {REASON_STAGES_INCOMPLETE, REASON_NO_STAGES}]
    logic = tuple(stage for stage in path.cell_stages if stage.role in LOGIC_ROLES)
    if reasons:
        return Feature(FEATURE_LOGIC_DEPTH, STATUS_NOT_DERIVABLE, {}, tuple(reasons))
    unknown = sum(1 for stage in logic if stage.role == ROLE_UNKNOWN)
    buffers = sum(1 for stage in logic if stage.role == ROLE_BUFFER)
    inverters = sum(1 for stage in logic if stage.role == ROLE_INVERTER)
    histogram: dict[str, int] = {}
    for stage in logic:
        key = stage.cell_function or "other"
        histogram[key] = histogram.get(key, 0) + 1
    value: dict[str, Any] = {
        "logic_depth": len(logic),
        "role_unknown_count": unknown,
        "buffer_count": None if unknown else buffers,
        "inverter_count": None if unknown else inverters,
        "depth_excl_buffers": None if unknown else len(logic) - buffers,
        "depth_excl_buffers_inverters": None if unknown else len(logic) - buffers - inverters,
        "cell_function_histogram": [
            {"cell_function": function, "count": count}
            for function, count in sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }
    return Feature(
        FEATURE_LOGIC_DEPTH, STATUS_DERIVED, value,
        (REASON_ROLE_UNKNOWN,) if unknown else (),
        assumptions=(ASSUMPTION_LOGIC_DEPTH_DEFINITION, ASSUMPTION_ROLES_FROM_ADAPTER),
    )


def feature_path_clock_skew(path: TimingPathEvidence) -> Feature:
    timing = path.timing
    reasons: list[str] = []
    if timing.clock_propagation != CLOCK_PROPAGATED:
        reasons.append(REASON_CLOCK_NOT_PROPAGATED)
    if None in (timing.launch_clock_edge, timing.launch_clock_arrival,
                timing.capture_clock_edge, timing.capture_clock_arrival):
        reasons.append(REASON_CLOCK_ARRIVAL_MISSING)
    if timing.crpr is None:
        # 報告沒有 CRPR 列不等於 0：沒有證據就不推導。
        reasons.append(REASON_CRPR_MISSING)
    if reasons:
        return Feature(FEATURE_CLOCK_SKEW, STATUS_NOT_DERIVABLE, {}, tuple(reasons))
    launch_latency = _dec(timing.launch_clock_arrival) - _dec(timing.launch_clock_edge)
    capture_latency = _dec(timing.capture_clock_arrival) - _dec(timing.capture_clock_edge)
    crpr = _dec(timing.crpr)
    skew = capture_latency - launch_latency + crpr
    return Feature(
        FEATURE_CLOCK_SKEW, STATUS_DERIVED,
        {
            "unit": timing.unit_time, "value": _q(skew, timing.resolution),
            "launch_latency": _q(launch_latency, timing.resolution),
            "capture_latency": _q(capture_latency, timing.resolution),
            "crpr": _q(crpr, timing.resolution),
        },
        assumptions=(ASSUMPTION_SKEW_DEFINITION, ASSUMPTION_DISPLAY_RESOLUTION),
    )


def compute_features(path: TimingPathEvidence) -> FeatureSet:
    """對一條 path 計算全部特徵；任何特徵的缺值都不影響其他特徵。"""
    return FeatureSet(
        data_path_delay=feature_data_path_delay(path),
        max_stage_contribution=feature_max_stage_contribution(path),
        cell_vs_net_delay=feature_cell_vs_net_delay(path),
        logic_depth=feature_logic_depth(path),
        path_clock_skew=feature_path_clock_skew(path),
    )


__all__ = [
    "ASSUMPTION_CODES", "FEATURES_SCHEMA_VERSION", "FEATURE_CELL_VS_NET", "FEATURE_CLOCK_SKEW",
    "FEATURE_DATA_PATH_DELAY", "FEATURE_IDS", "FEATURE_LOGIC_DEPTH", "FEATURE_MAX_STAGE",
    "FEATURE_REASON_CODES", "FEATURE_STATUSES", "Feature", "FeatureSet",
    "REASON_CLOCK_ARRIVAL_MISSING", "REASON_CLOCK_NOT_PROPAGATED", "REASON_CRPR_MISSING",
    "REASON_DELTA_T_UNAVAILABLE", "REASON_ENDPOINT_ARRIVAL_MISSING",
    "REASON_LAUNCH_CLOCK_ARRIVAL_MISSING", "REASON_NEGATIVE_INCREMENT",
    "REASON_NON_POSITIVE_DENOMINATOR", "REASON_NO_COMBINATIONAL_STAGE", "REASON_NO_STAGES",
    "REASON_RANKING_DIFFERS", "REASON_RANKING_TIE", "REASON_ROLE_UNKNOWN",
    "REASON_STAGES_INCOMPLETE", "REASON_STAGE_INCREMENT_MISSING", "REASON_STARTPOINT_IS_PORT",
    "REASON_SUM_INCONSISTENT", "STATUS_AMBIGUOUS", "STATUS_DERIVED", "STATUS_NOT_DERIVABLE",
    "compute_features", "feature_cell_vs_net_delay", "feature_data_path_delay",
    "feature_logic_depth", "feature_max_stage_contribution", "feature_path_clock_skew",
]
