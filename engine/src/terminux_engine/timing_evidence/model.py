# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`TimingPathEvidence`：一條 reported setup path 的 tool-neutral 資料模型。

形狀固定、可 JSON 序列化、有版本，且每一筆都綁定來源（run／attempt／manifest／
report content SHA／section／行號／excerpt hash）。缺值一律 `None`，不以 0 補位。

所有可能進入對外投影的分類欄位都是**封閉詞彙**（本模組的常數）：stage kind／role／
cell function、endpoint kind、單位、rounding、completeness problem code。自由文字
（instance／net／pin／startpoint／endpoint／clock／group 名稱、section 標題、cell type）
只存在本機 record，由 `projection` 排除在外。

`from_json_dict()` 是從 EventStore 讀回時的唯一入口：封閉鍵集合、型別、數值有限、
字串長度與 stage 數上限全部在這裡檢查，任何不符都拋 `TimingEvidenceError`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "terminux.timing_path_evidence.v1"
RECORD_TYPE = "timing_path_evidence"

#: 單一 path 可攜帶的 stage 上限（cell arc 與 net arc 合計）。超過即 fail closed。
MAX_STAGES = 64
#: 任何自由字串欄位的長度上限（instance／net 名稱只存在本機 record，仍受此限）。
MAX_TEXT_CHARS = 256

KIND_CELL = "cell"
KIND_NET = "net"
STAGE_KINDS = frozenset({KIND_CELL, KIND_NET})

ROLE_SEQUENTIAL_LAUNCH = "sequential_launch"
ROLE_COMBINATIONAL = "combinational"
ROLE_BUFFER = "buffer"
ROLE_INVERTER = "inverter"
ROLE_UNKNOWN = "unknown"
STAGE_ROLES = frozenset({
    ROLE_SEQUENTIAL_LAUNCH, ROLE_COMBINATIONAL, ROLE_BUFFER, ROLE_INVERTER, ROLE_UNKNOWN,
})
#: 計入 logic depth 的 role（Q 與 D 之間的 cell arc）。
LOGIC_ROLES = frozenset({ROLE_COMBINATIONAL, ROLE_BUFFER, ROLE_INVERTER, ROLE_UNKNOWN})

#: cell 的邏輯功能（tool-neutral 封閉詞彙）。adapter 依 library 知識對應；這是對外
#: 投影中唯一可描述 cell 的欄位，library 的 cell type 字串不外送。
CELL_FUNCTION_FLOP = "flop"
CELL_FUNCTION_LATCH = "latch"
CELL_FUNCTION_BUFFER = "buffer"
CELL_FUNCTION_INVERTER = "inverter"
CELL_FUNCTION_OTHER = "other"
CELL_FUNCTIONS = frozenset({
    CELL_FUNCTION_FLOP, CELL_FUNCTION_LATCH, CELL_FUNCTION_BUFFER, CELL_FUNCTION_INVERTER,
    "nand", "nor", "and", "or", "xor", "xnor", "aoi", "oai", "mux", "adder", "clock_gate",
    CELL_FUNCTION_OTHER,
})

ENDPOINT_REGISTER = "register"
ENDPOINT_INPUT_PORT = "input_port"
ENDPOINT_OUTPUT_PORT = "output_port"
ENDPOINT_UNKNOWN = "unknown"
ENDPOINT_KINDS = frozenset({
    ENDPOINT_REGISTER, ENDPOINT_INPUT_PORT, ENDPOINT_OUTPUT_PORT, ENDPOINT_UNKNOWN,
})

ANALYSIS_SETUP = "setup"
ANALYSES = frozenset({ANALYSIS_SETUP})

SELECTION_WORST_REPORTED_IN_SECTION = "worst_reported_in_section"
SELECTIONS = frozenset({SELECTION_WORST_REPORTED_IN_SECTION})

#: 對外以種類代替 report section 標題（標題是 tool 文字，只存本機）。
SECTION_KIND_SETUP_PATH_REPORT = "setup_path_report"
SECTION_KINDS = frozenset({SECTION_KIND_SETUP_PATH_REPORT})

CLOCK_PROPAGATED = "propagated"
CLOCK_IDEAL = "ideal"
CLOCK_PROPAGATIONS = frozenset({CLOCK_PROPAGATED, CLOCK_IDEAL})

UNIT_TIMES = frozenset({"ns", "ps", "s"})
UNIT_CAPS = frozenset({"fF", "pF", "F"})

ROUNDING_FIXED_DECIMAL = "fixed_decimal"
ROUNDINGS = frozenset({ROUNDING_FIXED_DECIMAL})

#: adapter 可回報的完整性問題碼（封閉）。
PROBLEM_SLACK_TIE = "slack_tie_first_block_selected"
PROBLEM_STARTPOINT_ROW_MISSING = "startpoint_row_missing"
PROBLEM_ENDPOINT_ROW_MISSING = "endpoint_row_missing"
PROBLEM_ENDPOINT_ARRIVAL_MISMATCH = "endpoint_arrival_mismatch"
PROBLEM_STAGE_SEQUENCE_BROKEN = "stage_sequence_broken"
PROBLEM_UNRECOGNIZED_ROW = "unrecognized_row"
PROBLEM_ARRIVAL_CHAIN_INCONSISTENT = "arrival_chain_inconsistent"
PROBLEM_NEGATIVE_INCREMENT = "negative_increment"
PROBLEM_FIRST_STAGE_INVALID = "first_stage_invalid"
COMPLETENESS_PROBLEMS = frozenset({
    PROBLEM_SLACK_TIE, PROBLEM_STARTPOINT_ROW_MISSING, PROBLEM_ENDPOINT_ROW_MISSING,
    PROBLEM_ENDPOINT_ARRIVAL_MISMATCH, PROBLEM_STAGE_SEQUENCE_BROKEN, PROBLEM_UNRECOGNIZED_ROW,
    PROBLEM_ARRIVAL_CHAIN_INCONSISTENT, PROBLEM_NEGATIVE_INCREMENT, PROBLEM_FIRST_STAGE_INVALID,
})


class TimingEvidenceError(ValueError):
    """JSON 形狀或數值不符合本模型；`code` 是穩定的原因碼。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ── 欄位驗證 helpers ───────────────────────────────────────────────────


def _mapping(value: Any, keys: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TimingEvidenceError(code)
    return value


def _text(value: Any, code: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_CHARS:
        raise TimingEvidenceError(code)
    return value


def _number(value: Any, code: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TimingEvidenceError(code)
    return float(value)


def _integer(value: Any, code: str, *, optional: bool = False, minimum: int = 0) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < minimum:
        raise TimingEvidenceError(code)
    return value


def _choice(value: Any, choices: frozenset[str], code: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or value not in choices:
        raise TimingEvidenceError(code)
    return value


def _boolean(value: Any, code: str) -> bool:
    if type(value) is not bool:
        raise TimingEvidenceError(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise TimingEvidenceError(code)
    return value


# ── 模型 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathSource:
    """這條 path 來自哪一次執行的哪一份 artifact 的哪幾行。"""

    run_id: str
    attempt: int
    step_id: str | None
    artifact_manifest_id: str
    artifact_manifest_sha256: str
    artifact_instance_id: str | None
    adapter_role: str
    report_content_sha256: str | None
    section: str
    section_kind: str
    line_start: int
    line_end: int
    excerpt_hash: str

    _KEYS = frozenset({
        "run_id", "attempt", "step_id", "artifact_manifest_id", "artifact_manifest_sha256",
        "artifact_instance_id", "adapter_role", "report_content_sha256", "section", "section_kind",
        "line_start", "line_end", "excerpt_hash",
    })

    def to_json_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in sorted(self._KEYS)}

    @classmethod
    def from_json_dict(cls, value: Any) -> "PathSource":
        doc = _mapping(value, cls._KEYS, "source_keys")
        line_start = _integer(doc["line_start"], "source_line", minimum=1)
        line_end = _integer(doc["line_end"], "source_line", minimum=1)
        if line_end < line_start:
            raise TimingEvidenceError("source_line")
        return cls(
            run_id=_text(doc["run_id"], "source_run_id"),
            attempt=_integer(doc["attempt"], "source_attempt", minimum=1),
            step_id=_text(doc["step_id"], "source_step_id", optional=True),
            artifact_manifest_id=_text(doc["artifact_manifest_id"], "source_manifest"),
            artifact_manifest_sha256=_sha256(doc["artifact_manifest_sha256"], "source_manifest"),
            artifact_instance_id=_text(doc["artifact_instance_id"], "source_instance", optional=True),
            adapter_role=_text(doc["adapter_role"], "source_role"),
            report_content_sha256=(None if doc["report_content_sha256"] is None
                                   else _sha256(doc["report_content_sha256"], "source_content")),
            section=_text(doc["section"], "source_section"),
            section_kind=_choice(doc["section_kind"], SECTION_KINDS, "source_section_kind"),
            line_start=line_start, line_end=line_end,
            excerpt_hash=_sha256(doc["excerpt_hash"], "source_excerpt"),
        )


@dataclass(frozen=True)
class PathScope:
    """這條 path 的分析範圍。`global_worst_claim` 永遠為 False：它只是 section 內列出的那一條。"""

    analysis: str
    path_group: str | None
    path_group_index: int
    paths_in_section: int
    paths_per_group_declared: int | None
    selection: str
    corner: str | None
    global_worst_claim: bool = False

    _KEYS = frozenset({
        "analysis", "path_group", "path_group_index", "paths_in_section",
        "paths_per_group_declared", "selection", "corner", "global_worst_claim",
    })

    def __post_init__(self) -> None:
        if self.global_worst_claim is not False:
            raise TimingEvidenceError("global_worst_claim")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "analysis": self.analysis, "path_group": self.path_group,
            "path_group_index": self.path_group_index, "paths_in_section": self.paths_in_section,
            "paths_per_group_declared": self.paths_per_group_declared,
            "selection": self.selection, "corner": self.corner,
            "global_worst_claim": False,
        }

    @classmethod
    def from_json_dict(cls, value: Any) -> "PathScope":
        doc = _mapping(value, cls._KEYS, "scope_keys")
        if _boolean(doc["global_worst_claim"], "global_worst_claim") is not False:
            raise TimingEvidenceError("global_worst_claim")
        return cls(
            analysis=_choice(doc["analysis"], ANALYSES, "scope_analysis"),
            path_group=_text(doc["path_group"], "scope_group", optional=True),
            path_group_index=_integer(doc["path_group_index"], "scope_group_index", minimum=1),
            paths_in_section=_integer(doc["paths_in_section"], "scope_paths", minimum=1),
            paths_per_group_declared=_integer(doc["paths_per_group_declared"], "scope_declared",
                                              optional=True, minimum=1),
            selection=_choice(doc["selection"], SELECTIONS, "scope_selection"),
            corner=_text(doc["corner"], "scope_corner", optional=True),
        )


@dataclass(frozen=True)
class PathEndpoints:
    """起訖點的種類與時脈。物件名稱只存在本機 record，對外投影不含。"""

    startpoint_kind: str
    endpoint_kind: str
    launch_clock: str | None
    capture_clock: str | None
    startpoint_name: str | None
    endpoint_name: str | None

    _KEYS = frozenset({
        "startpoint_kind", "endpoint_kind", "launch_clock", "capture_clock",
        "startpoint_name", "endpoint_name",
    })

    def to_json_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in sorted(self._KEYS)}

    @classmethod
    def from_json_dict(cls, value: Any) -> "PathEndpoints":
        doc = _mapping(value, cls._KEYS, "endpoints_keys")
        return cls(
            startpoint_kind=_choice(doc["startpoint_kind"], ENDPOINT_KINDS, "endpoint_kind"),
            endpoint_kind=_choice(doc["endpoint_kind"], ENDPOINT_KINDS, "endpoint_kind"),
            launch_clock=_text(doc["launch_clock"], "endpoint_clock", optional=True),
            capture_clock=_text(doc["capture_clock"], "endpoint_clock", optional=True),
            startpoint_name=_text(doc["startpoint_name"], "endpoint_name", optional=True),
            endpoint_name=_text(doc["endpoint_name"], "endpoint_name", optional=True),
        )


@dataclass(frozen=True)
class PathTiming:
    """Path 層級的時間值。單位與解析度由 adapter 宣告；報告沒有的列為 `None`，不填 0。"""

    unit_time: str
    unit_cap: str | None
    resolution: float
    data_arrival: float | None
    data_required: float | None
    slack: float | None
    launch_clock_edge: float | None
    launch_clock_arrival: float | None
    capture_clock_edge: float | None
    capture_clock_arrival: float | None
    crpr: float | None
    setup_time: float | None
    clock_uncertainty: float | None
    external_delay: float | None
    clock_propagation: str | None

    _KEYS = frozenset({
        "unit_time", "unit_cap", "resolution", "data_arrival", "data_required", "slack",
        "launch_clock_edge", "launch_clock_arrival", "capture_clock_edge",
        "capture_clock_arrival", "crpr", "setup_time", "clock_uncertainty", "external_delay",
        "clock_propagation",
    })
    _NUMBERS = (
        "data_arrival", "data_required", "slack", "launch_clock_edge", "launch_clock_arrival",
        "capture_clock_edge", "capture_clock_arrival", "crpr", "setup_time", "clock_uncertainty",
        "external_delay",
    )

    def to_json_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in sorted(self._KEYS)}

    @classmethod
    def from_json_dict(cls, value: Any) -> "PathTiming":
        doc = _mapping(value, cls._KEYS, "timing_keys")
        resolution = _number(doc["resolution"], "timing_resolution")
        if resolution is None or resolution <= 0:
            raise TimingEvidenceError("timing_resolution")
        numbers = {key: _number(doc[key], "timing_value", optional=True) for key in cls._NUMBERS}
        return cls(
            unit_time=_choice(doc["unit_time"], UNIT_TIMES, "timing_unit"),
            unit_cap=_choice(doc["unit_cap"], UNIT_CAPS, "timing_unit", optional=True),
            resolution=resolution,
            clock_propagation=_choice(doc["clock_propagation"], CLOCK_PROPAGATIONS,
                                      "timing_propagation", optional=True),
            **numbers,
        )


@dataclass(frozen=True)
class PathStage:
    """Data path 上的一個 arc：`cell`（input pin → output pin）或 `net`（driver → load）。

    `role` 與 `cell_function` 只對 cell 有意義，且由 adapter 依 library 知識給定（封閉詞彙）；
    `cell_type` 是 library 原始名稱，只存本機。本模型不推測任何分類。
    """

    index: int
    kind: str
    role: str | None
    cell_function: str | None
    cell_type: str | None
    incr: float | None
    arrival: float | None
    fanout: int | None
    cap: float | None
    slew: float | None
    line: int | None
    instance: str | None
    net: str | None
    pin: str | None

    _KEYS = frozenset({
        "index", "kind", "role", "cell_function", "cell_type", "incr", "arrival", "fanout", "cap",
        "slew", "line", "instance", "net", "pin",
    })

    def to_json_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in sorted(self._KEYS)}

    @classmethod
    def from_json_dict(cls, value: Any) -> "PathStage":
        doc = _mapping(value, cls._KEYS, "stage_keys")
        kind = _choice(doc["kind"], STAGE_KINDS, "stage_kind")
        role = _choice(doc["role"], STAGE_ROLES, "stage_role", optional=True)
        function = _choice(doc["cell_function"], CELL_FUNCTIONS, "stage_cell_function", optional=True)
        if (kind == KIND_CELL) != (role is not None) or (kind == KIND_CELL) != (function is not None):
            raise TimingEvidenceError("stage_role")
        return cls(
            index=_integer(doc["index"], "stage_index", minimum=1),
            kind=kind, role=role, cell_function=function,
            cell_type=_text(doc["cell_type"], "stage_cell_type", optional=True),
            incr=_number(doc["incr"], "stage_value", optional=True),
            arrival=_number(doc["arrival"], "stage_value", optional=True),
            fanout=_integer(doc["fanout"], "stage_fanout", optional=True),
            cap=_number(doc["cap"], "stage_value", optional=True),
            slew=_number(doc["slew"], "stage_value", optional=True),
            line=_integer(doc["line"], "stage_line", optional=True, minimum=1),
            instance=_text(doc["instance"], "stage_name", optional=True),
            net=_text(doc["net"], "stage_name", optional=True),
            pin=_text(doc["pin"], "stage_name", optional=True),
        )


@dataclass(frozen=True)
class PathCompleteness:
    """逐級資料是否完整；不完整時 `problems`（封閉碼）說明原因，特徵演算法據此拒絕計算。"""

    stages_complete: bool
    stage_count: int
    rounding: str
    problems: tuple[str, ...] = ()

    _KEYS = frozenset({"stages_complete", "stage_count", "rounding", "problems"})

    def __post_init__(self) -> None:
        if self.rounding not in ROUNDINGS or any(p not in COMPLETENESS_PROBLEMS for p in self.problems):
            raise TimingEvidenceError("completeness_problems")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "stages_complete": self.stages_complete, "stage_count": self.stage_count,
            "rounding": self.rounding, "problems": list(self.problems),
        }

    @classmethod
    def from_json_dict(cls, value: Any) -> "PathCompleteness":
        doc = _mapping(value, cls._KEYS, "completeness_keys")
        problems = doc["problems"]
        if (not isinstance(problems, list) or len(problems) > 16
                or not all(isinstance(p, str) and p in COMPLETENESS_PROBLEMS for p in problems)):
            raise TimingEvidenceError("completeness_problems")
        return cls(
            stages_complete=_boolean(doc["stages_complete"], "completeness_flag"),
            stage_count=_integer(doc["stage_count"], "completeness_count"),
            rounding=_choice(doc["rounding"], ROUNDINGS, "completeness_rounding"),
            problems=tuple(problems),
        )


@dataclass(frozen=True)
class TimingPathEvidence:
    """一條 reported setup path 的完整證據。"""

    source: PathSource
    scope: PathScope
    endpoints: PathEndpoints
    timing: PathTiming
    stages: tuple[PathStage, ...]
    completeness: PathCompleteness
    schema_version: str = SCHEMA_VERSION

    _KEYS = frozenset({
        "schema_version", "source", "scope", "endpoints", "timing", "stages", "completeness",
    })

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise TimingEvidenceError("schema_version")
        if len(self.stages) > MAX_STAGES:
            raise TimingEvidenceError("too_many_stages")
        if [stage.index for stage in self.stages] != list(range(1, len(self.stages) + 1)):
            raise TimingEvidenceError("stage_index")

    @property
    def cell_stages(self) -> tuple[PathStage, ...]:
        return tuple(stage for stage in self.stages if stage.kind == KIND_CELL)

    @property
    def net_stages(self) -> tuple[PathStage, ...]:
        return tuple(stage for stage in self.stages if stage.kind == KIND_NET)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source.to_json_dict(),
            "scope": self.scope.to_json_dict(),
            "endpoints": self.endpoints.to_json_dict(),
            "timing": self.timing.to_json_dict(),
            "stages": [stage.to_json_dict() for stage in self.stages],
            "completeness": self.completeness.to_json_dict(),
        }

    @classmethod
    def from_json_dict(cls, value: Any) -> "TimingPathEvidence":
        doc = _mapping(value, cls._KEYS, "evidence_keys")
        if doc["schema_version"] != SCHEMA_VERSION:
            raise TimingEvidenceError("schema_version")
        stages = doc["stages"]
        if not isinstance(stages, list) or len(stages) > MAX_STAGES:
            raise TimingEvidenceError("too_many_stages")
        return cls(
            source=PathSource.from_json_dict(doc["source"]),
            scope=PathScope.from_json_dict(doc["scope"]),
            endpoints=PathEndpoints.from_json_dict(doc["endpoints"]),
            timing=PathTiming.from_json_dict(doc["timing"]),
            stages=tuple(PathStage.from_json_dict(item) for item in stages),
            completeness=PathCompleteness.from_json_dict(doc["completeness"]),
        )


__all__ = [
    "ANALYSES", "ANALYSIS_SETUP", "CELL_FUNCTIONS", "CELL_FUNCTION_BUFFER", "CELL_FUNCTION_FLOP",
    "CELL_FUNCTION_INVERTER", "CELL_FUNCTION_LATCH", "CELL_FUNCTION_OTHER", "CLOCK_IDEAL",
    "CLOCK_PROPAGATED", "CLOCK_PROPAGATIONS", "COMPLETENESS_PROBLEMS", "ENDPOINT_INPUT_PORT",
    "ENDPOINT_KINDS", "ENDPOINT_OUTPUT_PORT", "ENDPOINT_REGISTER", "ENDPOINT_UNKNOWN", "KIND_CELL",
    "KIND_NET", "LOGIC_ROLES", "MAX_STAGES", "MAX_TEXT_CHARS", "PROBLEM_ARRIVAL_CHAIN_INCONSISTENT",
    "PROBLEM_ENDPOINT_ARRIVAL_MISMATCH", "PROBLEM_ENDPOINT_ROW_MISSING", "PROBLEM_FIRST_STAGE_INVALID",
    "PROBLEM_NEGATIVE_INCREMENT", "PROBLEM_SLACK_TIE", "PROBLEM_STAGE_SEQUENCE_BROKEN",
    "PROBLEM_STARTPOINT_ROW_MISSING", "PROBLEM_UNRECOGNIZED_ROW", "PathCompleteness",
    "PathEndpoints", "PathScope", "PathSource", "PathStage", "PathTiming", "RECORD_TYPE",
    "ROLE_BUFFER", "ROLE_COMBINATIONAL", "ROLE_INVERTER", "ROLE_SEQUENTIAL_LAUNCH", "ROLE_UNKNOWN",
    "ROUNDINGS", "ROUNDING_FIXED_DECIMAL", "SCHEMA_VERSION", "SECTION_KINDS",
    "SECTION_KIND_SETUP_PATH_REPORT", "SELECTIONS", "SELECTION_WORST_REPORTED_IN_SECTION",
    "STAGE_KINDS", "STAGE_ROLES", "TimingEvidenceError", "TimingPathEvidence", "UNIT_CAPS",
    "UNIT_TIMES",
]
