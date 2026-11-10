# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Evidence Bundle 的 `timing_path` section：對外投影與封閉 schema 驗證。

投影是 `TimingPathEvidence` ＋ `FeatureSet` 的最小化版本：**不含** instance／net／pin
名稱、startpoint／endpoint 名稱、clock／path group 名稱、library cell type、report section
標題、step id、adapter role、檔案路徑與原始 report 文字。只保留 stage index、封閉詞彙
（kind／role／cell function／reason／assumption／unit）、數值、行號與來源雜湊。

每一個可能是字串的欄位都只接受**封閉詞彙或固定格式**（run id、sha256）：沒有任何欄位
接受自由文字，因此 record 寫入者無法藉任何欄位夾帶名稱或指令。`validate_section()` 是
同一份 schema 的封閉驗證：bundle 組裝端用它自我檢查，外送 Gate 用它獨立拒絕。
"""

from __future__ import annotations

import math
import re
from typing import Any

from terminux_engine.timing_evidence.features import (
    ASSUMPTION_CODES,
    FEATURES_SCHEMA_VERSION,
    FEATURE_IDS,
    FEATURE_REASON_CODES,
    FEATURE_STATUSES,
    FeatureSet,
)
from terminux_engine.timing_evidence.model import (
    ANALYSES,
    CELL_FUNCTIONS,
    CLOCK_PROPAGATIONS,
    COMPLETENESS_PROBLEMS,
    ENDPOINT_KINDS,
    MAX_STAGES,
    ROUNDINGS,
    SCHEMA_VERSION,
    SECTION_KINDS,
    SELECTIONS,
    STAGE_KINDS,
    STAGE_ROLES,
    UNIT_CAPS,
    UNIT_TIMES,
    TimingPathEvidence,
)

SECTION_KEY = "timing_path"
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
SECTION_STATUSES = frozenset({STATUS_AVAILABLE, STATUS_UNAVAILABLE})

#: section 的最大序列化位元組數；超過視為無效（防止超大 payload）。
MAX_SECTION_BYTES = 64 * 1024
_MAX_REASONS = 16
_MAX_ASSUMPTIONS = 8
_MAX_HISTOGRAM = 32

#: `unavailable` 的原因碼（封閉）。bundle 端把任何不在此集合內的 record 原因一律改寫為
#: `record_unavailable`，所以 record 寫入者不能透過 reason 夾帶文字。
REASON_RECORD_UNAVAILABLE = "record_unavailable"
REASON_CODES = frozenset({
    # bundle 組裝端
    "execution_evidence_unavailable", "receipt_projection_untrusted",
    "artifact_manifest_not_verified", "receipt_identity_incomplete", "no_record",
    "record_identity_mismatch", "ambiguous_records",
    REASON_RECORD_UNAVAILABLE, "record_unreadable", "projection_invalid",
    # report 內容驗證（verification）
    "manifest_unreadable", "manifest_identity_mismatch", "observation_missing",
    "observation_not_stable", "report_outside_boundary", "report_unreadable",
    "report_content_mismatch", "reparser_unavailable", "report_reparse_failed",
    "record_content_mismatch",
    # adapter（after hook）回報的解析失敗
    "section_not_found", "no_path_blocks", "no_setup_path_with_slack", "too_many_stages",
    "path_header_incomplete", "finish_report_unreadable", "finish_report_role_unusable",
    "finish_report_structure_invalid", "orchestration_identity_missing", "evidence_invalid",
    "internal_error",
})

_RUN_ID = re.compile(r"^run_[A-Za-z0-9_]{1,64}$")
_MANIFEST_ID = re.compile(r"^am_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

SECTION_KEYS = frozenset({
    "status", "reason", "schema_version", "source", "scope", "endpoints", "timing", "stages",
    "completeness", "features",
})
SOURCE_KEYS = frozenset({
    "run_id", "attempt", "artifact_manifest_id", "artifact_manifest_sha256",
    "report_content_sha256", "section_kind", "line_start", "line_end", "excerpt_hash",
})
SCOPE_KEYS = frozenset({
    "analysis", "path_group_index", "paths_in_section", "paths_per_group_declared", "selection",
    "corner_known", "global_worst_claim",
})
ENDPOINT_KEYS = frozenset({"startpoint_kind", "endpoint_kind", "same_launch_capture_clock"})
TIMING_KEYS = frozenset({
    "unit_time", "unit_cap", "resolution", "data_arrival", "data_required", "slack",
    "launch_clock_edge", "launch_clock_arrival", "capture_clock_edge", "capture_clock_arrival",
    "crpr", "setup_time", "clock_uncertainty", "external_delay", "clock_propagation",
})
STAGE_KEYS = frozenset({
    "index", "kind", "role", "cell_function", "incr", "arrival", "fanout", "cap", "slew", "line",
})
COMPLETENESS_KEYS = frozenset({"stages_complete", "stage_count", "rounding", "problems"})
FEATURE_KEYS = frozenset({"feature_id", "status", "value", "reasons", "assumptions"})
STAGE_REF_KEYS = frozenset({
    "value", "share", "stage_index", "line", "cell_function", "role", "ties",
    "delta_t_candidates", "ranking_ambiguous",
})
NET_REF_KEYS = frozenset({"value", "share", "stage_index", "tie_count"})
HISTOGRAM_KEYS = frozenset({"cell_function", "count"})

_FEATURE_VALUE_KEYS: dict[str, frozenset[str]] = {
    "data_path_delay": frozenset({"value", "unit", "launch_clock_arrival", "data_arrival"}),
    "max_stage_contribution": frozenset({
        "unit", "data_path_delay", "max_stage", "max_combinational_stage", "max_net_arc"}),
    "cell_vs_net_delay": frozenset({
        "unit", "resolution", "data_path_delay", "cell_count", "net_count", "cell_sum",
        "net_sum_shown", "residual", "tolerance", "cell_share_shown", "net_share_shown",
        "net_sum_upper_bound", "net_share_upper_bound"}),
    "logic_depth": frozenset({
        "logic_depth", "role_unknown_count", "buffer_count", "inverter_count",
        "depth_excl_buffers", "depth_excl_buffers_inverters", "cell_function_histogram"}),
    "path_clock_skew": frozenset({"unit", "value", "launch_latency", "capture_latency", "crpr"}),
}
_NOT_DERIVABLE_VALUE_KEYS = frozenset({"residual", "tolerance"})


class SectionInvalid(ValueError):
    """`timing_path` section 不符合封閉 schema；`code` 是穩定原因碼。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ── 投影 ───────────────────────────────────────────────────────────────


def normalize_reason(reason: Any) -> str:
    """把任何來源的原因字串收斂到封閉詞彙：取 `:` 前的碼，不在集合內一律 `record_unavailable`。"""
    if not isinstance(reason, str):
        return REASON_RECORD_UNAVAILABLE
    code = reason.split(":", 1)[0]
    return code if code in REASON_CODES else REASON_RECORD_UNAVAILABLE


def unavailable_section(reason: str) -> dict[str, Any]:
    """沒有可用 path 證據時的固定形狀；`reason` 必在封閉詞彙內。"""
    return {
        "status": STATUS_UNAVAILABLE, "reason": normalize_reason(reason),
        "schema_version": SCHEMA_VERSION,
        "source": None, "scope": None, "endpoints": None, "timing": None, "stages": None,
        "completeness": None, "features": None,
    }


def outbound_section(evidence: TimingPathEvidence, features: FeatureSet) -> dict[str, Any]:
    """可進入 Evidence Bundle 的最小化投影（不含任何名稱、路徑或自由文字）。"""
    source = evidence.source
    scope = evidence.scope
    endpoints = evidence.endpoints
    return {
        "status": STATUS_AVAILABLE, "reason": None, "schema_version": SCHEMA_VERSION,
        "source": {
            "run_id": source.run_id, "attempt": source.attempt,
            "artifact_manifest_id": source.artifact_manifest_id,
            "artifact_manifest_sha256": source.artifact_manifest_sha256,
            "report_content_sha256": source.report_content_sha256,
            "section_kind": source.section_kind, "line_start": source.line_start,
            "line_end": source.line_end, "excerpt_hash": source.excerpt_hash,
        },
        "scope": {
            "analysis": scope.analysis, "path_group_index": scope.path_group_index,
            "paths_in_section": scope.paths_in_section,
            "paths_per_group_declared": scope.paths_per_group_declared,
            "selection": scope.selection, "corner_known": scope.corner is not None,
            "global_worst_claim": False,
        },
        "endpoints": {
            "startpoint_kind": endpoints.startpoint_kind, "endpoint_kind": endpoints.endpoint_kind,
            "same_launch_capture_clock": (
                None if endpoints.launch_clock is None or endpoints.capture_clock is None
                else endpoints.launch_clock == endpoints.capture_clock),
        },
        "timing": evidence.timing.to_json_dict(),
        "stages": [
            {
                "index": stage.index, "kind": stage.kind, "role": stage.role,
                "cell_function": stage.cell_function, "incr": stage.incr, "arrival": stage.arrival,
                "fanout": stage.fanout, "cap": stage.cap, "slew": stage.slew, "line": stage.line,
            }
            for stage in evidence.stages
        ],
        "completeness": evidence.completeness.to_json_dict(),
        "features": features.to_json_dict(),
    }


# ── 驗證 ───────────────────────────────────────────────────────────────


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise SectionInvalid(code)


def _mapping(value: Any, keys: frozenset[str], code: str) -> dict[str, Any]:
    _need(isinstance(value, dict) and set(value) == keys, code)
    return value


def _number(value: Any, code: str, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    _need(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), code)


def _integer(value: Any, code: str, *, optional: bool = True, minimum: int = 0) -> None:
    if value is None and optional:
        return
    _need(type(value) is int and value >= minimum, code)


def _pattern(value: Any, pattern: re.Pattern[str], code: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    _need(isinstance(value, str) and pattern.match(value) is not None, code)


def _choice(value: Any, choices: frozenset[str], code: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    _need(isinstance(value, str) and value in choices, code)


def _boolean(value: Any, code: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    _need(type(value) is bool, code)


def _choice_list(value: Any, choices: frozenset[str], limit: int, code: str) -> None:
    _need(isinstance(value, list) and len(value) <= limit
          and all(isinstance(v, str) and v in choices for v in value), code)


def _index_list(value: Any, code: str) -> None:
    _need(isinstance(value, list) and len(value) <= MAX_STAGES
          and all(type(v) is int and 1 <= v <= MAX_STAGES for v in value), code)


def _stage_ref(value: Any, code: str) -> None:
    ref = _mapping(value, STAGE_REF_KEYS, code)
    _number(ref["value"], code, optional=False)
    _number(ref["share"], code, optional=False)
    _integer(ref["stage_index"], code, optional=False, minimum=1)
    _integer(ref["line"], code, minimum=1)
    _choice(ref["cell_function"], CELL_FUNCTIONS, code, optional=True)
    _choice(ref["role"], STAGE_ROLES, code, optional=True)
    _index_list(ref["ties"], code)
    _index_list(ref["delta_t_candidates"], code)
    _boolean(ref["ranking_ambiguous"], code)


def _feature_value(feature_id: str, status: str, value: Any) -> None:
    code = f"feature_value:{feature_id}"
    _need(isinstance(value, dict), code)
    if status == "not_derivable":
        _need(set(value) <= _NOT_DERIVABLE_VALUE_KEYS, code)
        for item in value.values():
            _number(item, code, optional=False)
        return
    _need(set(value) == _FEATURE_VALUE_KEYS[feature_id], code)
    if feature_id == "data_path_delay":
        _number(value["value"], code, optional=False)
        _choice(value["unit"], UNIT_TIMES, code)
        _number(value["launch_clock_arrival"], code)
        _number(value["data_arrival"], code)
    elif feature_id == "max_stage_contribution":
        _choice(value["unit"], UNIT_TIMES, code)
        _number(value["data_path_delay"], code, optional=False)
        _stage_ref(value["max_stage"], code)
        if value["max_combinational_stage"] is not None:
            _stage_ref(value["max_combinational_stage"], code)
        if value["max_net_arc"] is not None:
            net = _mapping(value["max_net_arc"], NET_REF_KEYS, code)
            _number(net["value"], code, optional=False)
            _number(net["share"], code, optional=False)
            _integer(net["stage_index"], code, minimum=1)
            _integer(net["tie_count"], code, optional=False, minimum=1)
    elif feature_id == "cell_vs_net_delay":
        _choice(value["unit"], UNIT_TIMES, code)
        for key in ("resolution", "data_path_delay", "cell_sum", "net_sum_shown", "residual",
                    "tolerance", "cell_share_shown", "net_share_shown", "net_sum_upper_bound",
                    "net_share_upper_bound"):
            _number(value[key], code, optional=False)
        _integer(value["cell_count"], code, optional=False)
        _integer(value["net_count"], code, optional=False)
    elif feature_id == "logic_depth":
        _integer(value["logic_depth"], code, optional=False)
        _integer(value["role_unknown_count"], code, optional=False)
        for key in ("buffer_count", "inverter_count", "depth_excl_buffers",
                    "depth_excl_buffers_inverters"):
            _integer(value[key], code)
        histogram = value["cell_function_histogram"]
        _need(isinstance(histogram, list) and len(histogram) <= _MAX_HISTOGRAM, code)
        for item in histogram:
            entry = _mapping(item, HISTOGRAM_KEYS, code)
            _choice(entry["cell_function"], CELL_FUNCTIONS, code)
            _integer(entry["count"], code, optional=False, minimum=1)
    else:  # path_clock_skew
        _choice(value["unit"], UNIT_TIMES, code)
        for key in ("value", "launch_latency", "capture_latency", "crpr"):
            _number(value[key], code, optional=False)


def validate_section(section: Any) -> None:
    """封閉驗證一份 `timing_path` section；任何不符合即 `SectionInvalid`。"""
    doc = _mapping(section, SECTION_KEYS, "section_keys")
    _choice(doc["status"], SECTION_STATUSES, "section_status")
    _need(doc["schema_version"] == SCHEMA_VERSION, "section_schema_version")
    if doc["status"] == STATUS_UNAVAILABLE:
        _choice(doc["reason"], REASON_CODES, "section_reason")
        for key in ("source", "scope", "endpoints", "timing", "stages", "completeness", "features"):
            _need(doc[key] is None, "section_unavailable_payload")
        return
    _need(doc["reason"] is None, "section_reason")

    source = _mapping(doc["source"], SOURCE_KEYS, "source_keys")
    _pattern(source["run_id"], _RUN_ID, "source_run_id")
    _integer(source["attempt"], "source_attempt", optional=False, minimum=1)
    _pattern(source["artifact_manifest_id"], _MANIFEST_ID, "source_manifest")
    _pattern(source["artifact_manifest_sha256"], _SHA256, "source_manifest")
    _pattern(source["report_content_sha256"], _SHA256, "source_content")
    _choice(source["section_kind"], SECTION_KINDS, "source_section_kind")
    _integer(source["line_start"], "source_line", optional=False, minimum=1)
    _integer(source["line_end"], "source_line", optional=False, minimum=1)
    _need(source["line_end"] >= source["line_start"], "source_line")
    _pattern(source["excerpt_hash"], _SHA256, "source_excerpt")

    scope = _mapping(doc["scope"], SCOPE_KEYS, "scope_keys")
    _choice(scope["analysis"], ANALYSES, "scope_analysis")
    _integer(scope["path_group_index"], "scope_group_index", optional=False, minimum=1)
    _integer(scope["paths_in_section"], "scope_paths", optional=False, minimum=1)
    _integer(scope["paths_per_group_declared"], "scope_declared", minimum=1)
    _choice(scope["selection"], SELECTIONS, "scope_selection")
    _boolean(scope["corner_known"], "scope_corner")
    _need(scope["global_worst_claim"] is False, "global_worst_claim")

    endpoints = _mapping(doc["endpoints"], ENDPOINT_KEYS, "endpoints_keys")
    _choice(endpoints["startpoint_kind"], ENDPOINT_KINDS, "endpoint_kind")
    _choice(endpoints["endpoint_kind"], ENDPOINT_KINDS, "endpoint_kind")
    _boolean(endpoints["same_launch_capture_clock"], "endpoint_clock", optional=True)

    timing = _mapping(doc["timing"], TIMING_KEYS, "timing_keys")
    _choice(timing["unit_time"], UNIT_TIMES, "timing_unit")
    _choice(timing["unit_cap"], UNIT_CAPS, "timing_unit", optional=True)
    _number(timing["resolution"], "timing_resolution", optional=False)
    _need(timing["resolution"] > 0, "timing_resolution")
    for key in TIMING_KEYS - {"unit_time", "unit_cap", "resolution", "clock_propagation"}:
        _number(timing[key], "timing_value")
    _choice(timing["clock_propagation"], CLOCK_PROPAGATIONS, "timing_propagation", optional=True)

    stages = doc["stages"]
    _need(isinstance(stages, list) and len(stages) <= MAX_STAGES, "too_many_stages")
    for position, item in enumerate(stages, start=1):
        stage = _mapping(item, STAGE_KEYS, "stage_keys")
        _need(stage["index"] == position, "stage_index")
        _choice(stage["kind"], STAGE_KINDS, "stage_kind")
        _choice(stage["role"], STAGE_ROLES, "stage_role", optional=True)
        _choice(stage["cell_function"], CELL_FUNCTIONS, "stage_cell_function", optional=True)
        _need((stage["kind"] == "cell") == (stage["role"] is not None), "stage_role")
        _need((stage["kind"] == "cell") == (stage["cell_function"] is not None), "stage_cell_function")
        for key in ("incr", "arrival", "cap", "slew"):
            _number(stage[key], "stage_value")
        _integer(stage["fanout"], "stage_fanout")
        _integer(stage["line"], "stage_line", minimum=1)

    completeness = _mapping(doc["completeness"], COMPLETENESS_KEYS, "completeness_keys")
    _boolean(completeness["stages_complete"], "completeness_flag")
    _integer(completeness["stage_count"], "completeness_count", optional=False)
    _choice(completeness["rounding"], ROUNDINGS, "completeness_rounding")
    _choice_list(completeness["problems"], COMPLETENESS_PROBLEMS, _MAX_REASONS, "completeness_problems")

    features = doc["features"]
    _need(isinstance(features, dict) and set(features) == set(FEATURE_IDS) | {"schema_version"},
          "features_keys")
    _need(features["schema_version"] == FEATURES_SCHEMA_VERSION, "features_schema_version")
    for feature_id in FEATURE_IDS:
        feature = _mapping(features[feature_id], FEATURE_KEYS, f"feature_keys:{feature_id}")
        _need(feature["feature_id"] == feature_id, f"feature_id:{feature_id}")
        _choice(feature["status"], FEATURE_STATUSES, f"feature_status:{feature_id}")
        _choice_list(feature["reasons"], FEATURE_REASON_CODES, _MAX_REASONS,
                     f"feature_reasons:{feature_id}")
        _choice_list(feature["assumptions"], ASSUMPTION_CODES, _MAX_ASSUMPTIONS,
                     f"feature_assumptions:{feature_id}")
        _feature_value(feature_id, feature["status"], feature["value"])


__all__ = [
    "COMPLETENESS_KEYS", "ENDPOINT_KEYS", "FEATURE_KEYS", "MAX_SECTION_BYTES", "REASON_CODES",
    "REASON_RECORD_UNAVAILABLE", "SCOPE_KEYS", "SECTION_KEY", "SECTION_KEYS", "SECTION_STATUSES",
    "SOURCE_KEYS", "STAGE_KEYS", "STATUS_AVAILABLE", "STATUS_UNAVAILABLE", "SectionInvalid",
    "TIMING_KEYS", "normalize_reason", "outbound_section", "unavailable_section", "validate_section",
]
