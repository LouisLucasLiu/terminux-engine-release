# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS finish report 的單一 worst reported setup path 解析（tool adapter）。

格式依據（與 `parsers.py` 相同的來源）：ORFS `flow/scripts/report_metrics.tcl` 以
`report_checks -path_delay max -fields {slew cap input net fanout} -format full_clock_expanded`
寫出 section `finish report_checks -path_delay max`；沒有 `-group_path_count`、
`-endpoint_count`、`-digits`，因此 OpenSTA 預設**每個 path group 只列 1 條** path，
所有時間顯示到 0.01 ns。OpenSTA full 格式的列語意：

- 輸出 pin 列（`inst/Z (CELL)`，含 fanout／cap）：Delay 欄是 **cell arc**；
- 輸入 pin 列（`inst/A (CELL)`，只有 slew）：Delay 欄是 **net arc**（driver → load）；
- `name (net)` 列沒有數值，只標示 net 名稱；
- `clock X (rise edge)`／`clock source latency`／`clock network delay (ideal)`／
  `clock reconvergence pessimism`／`library setup time`／`output external delay` 是
  path 層級的列，不是 data path stage。

Data path 的逐級資料必須是連續的 cell／net 交替鏈，且相鄰 arrival 與 increment 在報告
解析度內一致；任何不認識的列、缺列或鏈不連續都讓 `stages_complete=False`（附封閉問題碼），
特徵層據此拒絕推導，不會以少一級的 path 給出看似完整的結果。

本模組只負責 ORFS／OpenSTA 特有的文字格式、pin／net 列辨識、Nangate45 cell role／
function 對應與單位／解析度宣告；tool-neutral 的資料模型與特徵演算法在
`terminux_engine.timing_evidence`。解析失敗時回傳明確的原因碼，**不**回傳部分 stage
冒充完整 path。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from terminux_engine.package_runtime.result import RecordRequest
from terminux_engine.packages.orfs_dqe import parsers
from terminux_engine.packages.orfs_dqe.manifest_input import (
    ManifestInput,
    orchestration_metadata,
    select_role,
)
from terminux_engine.packages.orfs_dqe.models import DQE_NAMESPACE
from terminux_engine.timing_evidence import features as feature_module
from terminux_engine.timing_evidence.model import (
    ANALYSIS_SETUP,
    CELL_FUNCTION_BUFFER,
    CELL_FUNCTION_FLOP,
    CELL_FUNCTION_INVERTER,
    CELL_FUNCTION_LATCH,
    CELL_FUNCTION_OTHER,
    CLOCK_IDEAL,
    CLOCK_PROPAGATED,
    ENDPOINT_INPUT_PORT,
    ENDPOINT_OUTPUT_PORT,
    ENDPOINT_REGISTER,
    ENDPOINT_UNKNOWN,
    KIND_CELL,
    KIND_NET,
    MAX_STAGES,
    PROBLEM_ARRIVAL_CHAIN_INCONSISTENT,
    PROBLEM_ENDPOINT_ARRIVAL_MISMATCH,
    PROBLEM_ENDPOINT_ROW_MISSING,
    PROBLEM_FIRST_STAGE_INVALID,
    PROBLEM_NEGATIVE_INCREMENT,
    PROBLEM_SLACK_TIE,
    PROBLEM_STAGE_SEQUENCE_BROKEN,
    PROBLEM_STARTPOINT_ROW_MISSING,
    PROBLEM_UNRECOGNIZED_ROW,
    RECORD_TYPE,
    ROLE_BUFFER,
    ROLE_COMBINATIONAL,
    ROLE_INVERTER,
    ROLE_SEQUENTIAL_LAUNCH,
    ROLE_UNKNOWN,
    ROUNDING_FIXED_DECIMAL,
    SECTION_KIND_SETUP_PATH_REPORT,
    SELECTION_WORST_REPORTED_IN_SECTION,
    PathCompleteness,
    PathEndpoints,
    PathScope,
    PathSource,
    PathStage,
    PathTiming,
    TimingEvidenceError,
    TimingPathEvidence,
)
from terminux_engine.timing_evidence.projection import outbound_section, validate_section

#: ORFS `report_metrics.tcl` 寫出的 section 名稱（stage 6 ＝ finish）。
SECTION_TITLE = "finish report_checks -path_delay max"
#: `report_metrics.tcl` 沒有給 `-group_path_count`：OpenSTA 預設每個 group 1 條。
PATHS_PER_GROUP_DECLARED = 1
#: Nangate45 liberty：`time_unit : "1ns"`、`capacitive_load_unit (1,ff)`；報告無 `-digits`
#: → 兩位小數。
UNIT_TIME = "ns"
UNIT_CAP = "fF"
RESOLUTION = 0.01
ADAPTER_ROLE = "orfs.finish_report"
RECORD_SCHEMA_VERSION = 1

#: Nangate45 cell 名稱前綴 → (role, cell_function)。只在本 adapter 內；Platform 不得依名稱猜測。
#: 來源：NangateOpenCellLibrary_typical.lib 的 cell 命名。
_CELL_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("CLKBUF_", ROLE_BUFFER, CELL_FUNCTION_BUFFER), ("BUF_", ROLE_BUFFER, CELL_FUNCTION_BUFFER),
    ("TBUF_", ROLE_BUFFER, CELL_FUNCTION_BUFFER),
    ("INV_", ROLE_INVERTER, CELL_FUNCTION_INVERTER), ("TINV_", ROLE_INVERTER, CELL_FUNCTION_INVERTER),
    ("CLKGATE", ROLE_UNKNOWN, "clock_gate"),
    ("SDFF", ROLE_UNKNOWN, CELL_FUNCTION_FLOP), ("DFF", ROLE_UNKNOWN, CELL_FUNCTION_FLOP),
    ("DLH", ROLE_UNKNOWN, CELL_FUNCTION_LATCH), ("DLL", ROLE_UNKNOWN, CELL_FUNCTION_LATCH),
    ("NAND", ROLE_COMBINATIONAL, "nand"), ("NOR", ROLE_COMBINATIONAL, "nor"),
    ("XNOR", ROLE_COMBINATIONAL, "xnor"), ("XOR", ROLE_COMBINATIONAL, "xor"),
    ("AND", ROLE_COMBINATIONAL, "and"), ("OR", ROLE_COMBINATIONAL, "or"),
    ("AOI", ROLE_COMBINATIONAL, "aoi"), ("OAI", ROLE_COMBINATIONAL, "oai"),
    ("MUX", ROLE_COMBINATIONAL, "mux"),
    ("HA_", ROLE_COMBINATIONAL, "adder"), ("FA_", ROLE_COMBINATIONAL, "adder"),
)

_NUMBER = re.compile(r"^[-+]?\d+(\.\d+)?$")
_PIN_DESC = re.compile(r"^(?P<instance>\S+)/(?P<pin>\S+) \((?P<cell>[^()\s]+)\)$")
_PORT_DESC = re.compile(r"^(?P<name>\S+) \((?P<direction>in|out|inout)\)$")
_NET_DESC = re.compile(r"^(?P<name>\S+) \(net\)$")
_CLOCK_EDGE = re.compile(r"^clock (?P<clock>\S+) \((?P<edge>rise|fall) edge\)$")
_CLOCKED_BY = re.compile(r"clocked by (?P<clock>\S+)\)?$")
#: data phase 可忽略的 path 層級列（不是 stage，也不是未知列）。
_PATH_LEVEL_ROWS = frozenset({
    "clock source latency", "clock network delay (ideal)", "clock network delay (propagated)",
    "input external delay", "output external delay",
})

# 原因碼（整條 path 無法解析時）
PROBLEM_SECTION_NOT_FOUND = "section_not_found"
PROBLEM_NO_PATH_BLOCKS = "no_path_blocks"
PROBLEM_NO_SETUP_PATH = "no_setup_path_with_slack"
PROBLEM_TOO_MANY_STAGES = "too_many_stages"
PROBLEM_HEADER_INCOMPLETE = "path_header_incomplete"
PROBLEM_REPORT_UNREADABLE = "finish_report_unreadable"
PROBLEM_ROLE_UNUSABLE = "finish_report_role_unusable"


class PathParseError(RuntimeError):
    """整條 path 無法 deterministic 解析；`code` 是穩定原因碼。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Row:
    line: int
    numbers: tuple[float, ...]
    edge: str | None
    description: str


@dataclass
class _Block:
    """一個 `Startpoint:` 到下一個 `Startpoint:` 之前的 path 區塊。"""

    line_start: int
    line_end: int
    text_lines: list[str]
    startpoint: str | None = None
    startpoint_kind: str = ENDPOINT_UNKNOWN
    launch_clock: str | None = None
    endpoint: str | None = None
    endpoint_kind: str = ENDPOINT_UNKNOWN
    capture_clock: str | None = None
    path_group: str | None = None
    path_type: str | None = None
    corner: str | None = None
    rows: list[_Row] = field(default_factory=list)
    slack: float | None = None


@dataclass(frozen=True)
class ParsedPath:
    """解析完成的 path（尚未綁定 run／manifest 來源）。"""

    scope: PathScope
    endpoints: PathEndpoints
    timing: PathTiming
    stages: tuple[PathStage, ...]
    completeness: PathCompleteness
    section: str
    section_kind: str
    line_start: int
    line_end: int
    excerpt_hash: str


# ── 公開 API ───────────────────────────────────────────────────────────


def classify_cell(cell_type: str | None) -> tuple[str, str]:
    """Nangate45 cell 名稱 → (role, cell_function)。不認得的前綴回 (`unknown`, `other`)，不猜。"""
    if not cell_type:
        return ROLE_UNKNOWN, CELL_FUNCTION_OTHER
    upper = cell_type.upper()
    for prefix, role, function in _CELL_PREFIXES:
        if upper.startswith(prefix):
            return role, function
    return ROLE_UNKNOWN, CELL_FUNCTION_OTHER


def role_for_cell(cell_type: str | None) -> str:
    """Nangate45 cell 名稱 → role（`classify_cell` 的第一個元素）。"""
    return classify_cell(cell_type)[0]


def parse_worst_setup_path(lines: list[str], *, section: str = SECTION_TITLE) -> ParsedPath:
    """從 finish report 的 `section` 選出 slack 最小的 setup path 並正規化為逐級資料。

    選取規則：section 內所有 `Path Type: max` 且有 slack 的 block 中 slack 最小者；
    同值時取先出現者並記錄 `slack_tie_first_block_selected`。這是「section 內列出的
    最差 path」，不是全設計的 worst path（`global_worst_claim=False`）。
    """
    sections = parsers.split_finish_sections(lines)
    if section not in sections:
        raise PathParseError(PROBLEM_SECTION_NOT_FOUND)
    start_line, _end_line, body = sections[section]
    blocks = _split_blocks(body, start_line)
    if not blocks:
        raise PathParseError(PROBLEM_NO_PATH_BLOCKS)
    candidates = [block for block in blocks if block.path_type == "max" and block.slack is not None]
    if not candidates:
        raise PathParseError(PROBLEM_NO_SETUP_PATH)
    worst = min(block.slack for block in candidates)  # type: ignore[type-var]
    selected = [block for block in candidates if block.slack == worst]
    chosen = selected[0]
    problems: list[str] = []
    if len(selected) > 1:
        problems.append(PROBLEM_SLACK_TIE)
    group_index = 1 + [block is chosen for block in blocks].index(True)
    return _normalize(chosen, section=section, paths_in_section=len(blocks),
                      group_index=group_index, problems=problems)


def build_evidence(parsed: ParsedPath, *, run_id: str, attempt: int, step_id: str | None,
                   artifact_manifest_id: str, artifact_manifest_sha256: str,
                   artifact_instance_id: str | None,
                   report_content_sha256: str | None) -> TimingPathEvidence:
    """把解析結果綁定到來源身分。"""
    return TimingPathEvidence(
        source=PathSource(
            run_id=run_id, attempt=attempt, step_id=step_id,
            artifact_manifest_id=artifact_manifest_id,
            artifact_manifest_sha256=artifact_manifest_sha256,
            artifact_instance_id=artifact_instance_id, adapter_role=ADAPTER_ROLE,
            report_content_sha256=report_content_sha256, section=parsed.section,
            section_kind=parsed.section_kind, line_start=parsed.line_start,
            line_end=parsed.line_end, excerpt_hash=parsed.excerpt_hash,
        ),
        scope=parsed.scope, endpoints=parsed.endpoints, timing=parsed.timing,
        stages=parsed.stages, completeness=parsed.completeness,
    )


def collect_record(manifest_input: ManifestInput, event: Any) -> RecordRequest:
    """after hook 用：從 manifest 選取 finish report、解析、計算特徵，包成一筆 record。

    **永不拋出**。任何失敗都回傳 `status="unavailable"` 的 record（附原因碼），讓下游有
    明確的不可用訊號；DQE 判定與既有 record 完全不受影響。
    """
    orchestration = orchestration_metadata(event)
    manifest_id = manifest_input.manifest.manifest_id
    base: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION, "record_type": RECORD_TYPE,
        "source": {
            "run_id": orchestration.get("run_id"), "attempt": orchestration.get("attempt"),
            "step_id": orchestration.get("step_id"),
            "artifact_manifest_id": manifest_id,
            "artifact_manifest_sha256": manifest_input.manifest_sha256,
            "adapter_role": ADAPTER_ROLE,
        },
    }
    try:
        payload = _collect_payload(manifest_input, orchestration, base)
    except Exception as exc:  # noqa: BLE001 - 不可用訊號必須留下，不得讓 after hook 失敗
        payload = {**base, "status": "unavailable",
                   "reason": f"internal_error:{type(exc).__name__.lower()}",
                   "evidence": None, "features": None}
    status = payload["status"]
    return RecordRequest(
        namespace=DQE_NAMESPACE, record_type=RECORD_TYPE,
        key=f"orfs-tpe:{manifest_id}:v{RECORD_SCHEMA_VERSION}",
        payload=payload,
        display_message=(
            f"ORFS timing path evidence {status}"
            + (f": {payload['reason']}" if payload.get("reason") else "")
        ),
        index_tags={
            "manifest_id": manifest_id, "status": status,
            "run_id": str(orchestration.get("run_id") or ""),
        },
    )


def _collect_payload(manifest_input: ManifestInput, orchestration: dict[str, Any],
                     base: dict[str, Any]) -> dict[str, Any]:
    selection = select_role(manifest_input, ADAPTER_ROLE)
    if not selection.usable or selection.observation is None:
        return {**base, "status": "unavailable",
                "reason": f"{PROBLEM_ROLE_UNUSABLE}:{selection.problem or 'unknown'}",
                "evidence": None, "features": None}
    observation = selection.observation
    try:
        lines = parsers.read_text(Path(str(observation.resolved_path)))
    except (OSError, parsers.ParseError):
        return {**base, "status": "unavailable", "reason": PROBLEM_REPORT_UNREADABLE,
                "evidence": None, "features": None}
    try:
        parsed = parse_worst_setup_path(lines)
    except (PathParseError, parsers.ParseError) as exc:
        code = exc.code if isinstance(exc, PathParseError) else "finish_report_structure_invalid"
        return {**base, "status": "unavailable", "reason": code, "evidence": None, "features": None}
    run_id = orchestration.get("run_id")
    attempt = orchestration.get("attempt")
    if not isinstance(run_id, str) or type(attempt) is not int:
        return {**base, "status": "unavailable", "reason": "orchestration_identity_missing",
                "evidence": None, "features": None}
    content = observation.content_identity
    try:
        evidence = build_evidence(
            parsed, run_id=run_id, attempt=attempt,
            step_id=(str(orchestration["step_id"]) if orchestration.get("step_id") is not None else None),
            artifact_manifest_id=manifest_input.manifest.manifest_id,
            artifact_manifest_sha256=manifest_input.manifest_sha256,
            artifact_instance_id=observation.artifact_instance_id,
            report_content_sha256=content.digest if content is not None else None,
        )
        features = feature_module.compute_features(evidence)
        # 自我檢查：這份證據的對外投影必須通過封閉 schema，否則不當作可用。
        validate_section(outbound_section(evidence, features))
    except (TimingEvidenceError, ValueError) as exc:
        return {**base, "status": "unavailable",
                "reason": f"evidence_invalid:{getattr(exc, 'code', 'value_error')}",
                "evidence": None, "features": None}
    return {**base, "status": "available", "reason": None,
            "evidence": evidence.to_json_dict(), "features": features.to_json_dict()}


# ── 解析細節 ───────────────────────────────────────────────────────────


def _split_blocks(body: list[str], start_line: int) -> list[_Block]:
    starts = [i for i, line in enumerate(body) if line.strip().startswith("Startpoint:")]
    blocks: list[_Block] = []
    for position, begin in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(body)
        chunk = body[begin:end]
        # 去掉尾端空白列，讓 line_end 指向 slack 列。
        while chunk and not chunk[-1].strip():
            chunk.pop()
        block = _Block(line_start=start_line + begin, line_end=start_line + begin + len(chunk) - 1,
                       text_lines=[line.rstrip() for line in chunk])
        _parse_block(block, start_line + begin)
        blocks.append(block)
    return blocks


def _parse_block(block: _Block, first_line: int) -> None:
    pending_header: str | None = None
    for offset, raw in enumerate(block.text_lines):
        line_no = first_line + offset
        text = raw.strip()
        if not text or set(text) <= {"-"}:
            continue
        if text.startswith("Startpoint:"):
            block.startpoint, kind, clock = _endpoint_header(text[len("Startpoint:"):].strip())
            block.startpoint_kind, block.launch_clock = kind, clock
            pending_header = "start"
            continue
        if text.startswith("Endpoint:"):
            block.endpoint, kind, clock = _endpoint_header(text[len("Endpoint:"):].strip())
            block.endpoint_kind, block.capture_clock = kind, clock
            pending_header = "end"
            continue
        if text.startswith("(") and pending_header is not None:
            kind, clock = _kind_and_clock(text)
            if pending_header == "start":
                block.startpoint_kind = kind if kind != ENDPOINT_UNKNOWN else block.startpoint_kind
                block.launch_clock = clock or block.launch_clock
            else:
                block.endpoint_kind = kind if kind != ENDPOINT_UNKNOWN else block.endpoint_kind
                block.capture_clock = clock or block.capture_clock
            continue
        pending_header = None
        if text.startswith("Path Group:"):
            block.path_group = text[len("Path Group:"):].strip() or None
            continue
        if text.startswith("Path Type:"):
            block.path_type = text[len("Path Type:"):].strip() or None
            continue
        if text.startswith("Corner:"):
            block.corner = text[len("Corner:"):].strip() or None
            continue
        if text.startswith("Fanout") and "Description" in text:
            continue
        row = _tokenize(text, line_no)
        if row is None:
            continue
        block.rows.append(row)
        if row.description.startswith("slack") and row.numbers:
            block.slack = row.numbers[-1]


def _endpoint_header(rest: str) -> tuple[str | None, str, str | None]:
    """`Startpoint: name (kind clocked by clk)` 或只有 `name`（kind 在下一列）。"""
    name, _, tail = rest.partition(" ")
    kind, clock = _kind_and_clock(tail) if tail else (ENDPOINT_UNKNOWN, None)
    return (name or None), kind, clock


def _kind_and_clock(text: str) -> tuple[str, str | None]:
    lowered = text.lower()
    if "flip-flop" in lowered or "latch" in lowered:
        kind = ENDPOINT_REGISTER
    elif "input port" in lowered:
        kind = ENDPOINT_INPUT_PORT
    elif "output port" in lowered:
        kind = ENDPOINT_OUTPUT_PORT
    else:
        kind = ENDPOINT_UNKNOWN
    match = _CLOCKED_BY.search(text)
    clock = match.group("clock").rstrip(")") if match else None
    return kind, clock


def _tokenize(text: str, line_no: int) -> _Row | None:
    tokens = text.split()
    numbers: list[float] = []
    index = 0
    while index < len(tokens) and _NUMBER.match(tokens[index]):
        numbers.append(float(tokens[index]))
        index += 1
    edge = None
    if index < len(tokens) and tokens[index] in {"^", "v"}:
        edge = tokens[index]
        index += 1
    description = " ".join(tokens[index:])
    if not description:
        return None
    return _Row(line=line_no, numbers=tuple(numbers), edge=edge, description=description)


def _normalize(block: _Block, *, section: str, paths_in_section: int, group_index: int,
               problems: list[str]) -> ParsedPath:
    if block.startpoint is None or block.endpoint is None or block.path_type is None:
        raise PathParseError(PROBLEM_HEADER_INCOMPLETE)
    timing: dict[str, Any] = {
        "data_arrival": None, "data_required": None, "slack": block.slack,
        "launch_clock_edge": None, "launch_clock_arrival": None,
        "capture_clock_edge": None, "capture_clock_arrival": None,
        "crpr": None, "setup_time": None, "clock_uncertainty": None, "external_delay": None,
    }
    propagated = False
    ideal = False
    stages: list[PathStage] = []
    phase = "launch_clock"  # → data → required
    pending_net: str | None = None
    endpoint_arrival: float | None = None
    completeness_problems = list(problems)

    def note(problem: str) -> None:
        if problem not in completeness_problems:
            completeness_problems.append(problem)

    for row in block.rows:
        desc = row.description
        edge_match = _CLOCK_EDGE.match(desc)
        if edge_match and row.numbers:
            key = "launch_clock_edge" if phase != "required" else "capture_clock_edge"
            timing[key] = row.numbers[-1]
            continue
        if desc == "data arrival time" and row.numbers:
            if phase in {"launch_clock", "data"}:
                timing["data_arrival"] = row.numbers[-1]
                phase = "required"
            continue
        if desc == "data required time" and row.numbers:
            timing["data_required"] = row.numbers[-1]
            continue
        if desc.startswith("slack"):
            continue
        if desc == "clock network delay (ideal)":
            ideal = True
            if phase == "required" and row.numbers:
                timing["capture_clock_arrival"] = row.numbers[-1]
            elif row.numbers:
                timing["launch_clock_arrival"] = row.numbers[-1]
            continue
        if desc == "clock reconvergence pessimism" and len(row.numbers) >= 2:
            timing["crpr"] = row.numbers[-2]
            continue
        if desc == "library setup time" and len(row.numbers) >= 2:
            timing["setup_time"] = row.numbers[-2]
            continue
        if desc == "clock uncertainty" and len(row.numbers) >= 2:
            timing["clock_uncertainty"] = row.numbers[-2]
            continue
        if desc in {"output external delay", "input external delay"} and len(row.numbers) >= 2:
            timing["external_delay"] = row.numbers[-2]
            continue
        if desc in _PATH_LEVEL_ROWS:
            continue
        net_match = _NET_DESC.match(desc)
        if net_match:
            pending_net = net_match.group("name")
            continue
        pin_match = _PIN_DESC.match(desc)
        port_match = _PORT_DESC.match(desc)
        if pin_match is None and port_match is None:
            if phase == "data":
                # data path 中出現不認識的列：不能假裝鏈是完整的。
                note(PROBLEM_UNRECOGNIZED_ROW)
            continue
        instance = pin_match.group("instance") if pin_match else port_match.group("name")  # type: ignore[union-attr]
        pin = pin_match.group("pin") if pin_match else None
        cell = pin_match.group("cell") if pin_match else None
        is_output_row = len(row.numbers) >= 5
        if phase == "launch_clock":
            if instance == block.startpoint:
                if block.startpoint_kind == ENDPOINT_REGISTER and not is_output_row:
                    # 起點 register 的 CK pin 列：launch clock 到達時間。
                    timing["launch_clock_arrival"] = row.numbers[-1] if row.numbers else None
                    continue
                if block.startpoint_kind == ENDPOINT_REGISTER and is_output_row:
                    phase = "data"
                    stages.append(_cell_stage(row, len(stages) + 1, cell, instance, pin,
                                              role=ROLE_SEQUENTIAL_LAUNCH,
                                              function=classify_cell(cell)[1]))
                    continue
                if block.startpoint_kind != ENDPOINT_REGISTER:
                    # port 起點：port 列本身不是 cell arc，之後的列進入 data path。
                    phase = "data"
                    continue
            if pin_match is not None:
                propagated = True
            continue
        if phase == "data":
            expected_kind = KIND_NET if (stages and stages[-1].kind == KIND_CELL) else KIND_CELL
            if not stages and block.startpoint_kind != ENDPOINT_REGISTER:
                expected_kind = KIND_NET  # port 起點後第一個 arc 是 port → 第一個 cell 的 net arc
            actual_kind = KIND_CELL if is_output_row else KIND_NET
            if actual_kind != expected_kind:
                note(PROBLEM_STAGE_SEQUENCE_BROKEN)
            if instance == block.endpoint and not is_output_row:
                stage = _net_stage(row, len(stages) + 1, instance, pin, pending_net)
                stages.append(stage)
                endpoint_arrival = stage.arrival
                pending_net = None
                continue
            if is_output_row:
                role, function = classify_cell(cell)
                stages.append(_cell_stage(row, len(stages) + 1, cell, instance, pin,
                                          role=role, function=function))
            else:
                stages.append(_net_stage(row, len(stages) + 1, instance, pin, pending_net))
                pending_net = None
            if len(stages) > MAX_STAGES:
                raise PathParseError(PROBLEM_TOO_MANY_STAGES)
            continue
        # required phase
        if instance == block.endpoint and not is_output_row and row.numbers:
            timing["capture_clock_arrival"] = row.numbers[-1]
        if pin_match is not None:
            propagated = True

    if block.startpoint_kind == ENDPOINT_REGISTER and timing["launch_clock_arrival"] is None:
        note(PROBLEM_STARTPOINT_ROW_MISSING)
    if not stages or endpoint_arrival is None:
        note(PROBLEM_ENDPOINT_ROW_MISSING)
    elif timing["data_arrival"] is not None and endpoint_arrival != timing["data_arrival"]:
        note(PROBLEM_ENDPOINT_ARRIVAL_MISMATCH)
    if stages:
        first = stages[0]
        if block.startpoint_kind == ENDPOINT_REGISTER and first.role != ROLE_SEQUENTIAL_LAUNCH:
            note(PROBLEM_FIRST_STAGE_INVALID)
        if stages[-1].kind != KIND_NET:
            note(PROBLEM_STAGE_SEQUENCE_BROKEN)
        _check_arrival_chain(stages, timing["launch_clock_arrival"], note)
    if len(stages) > MAX_STAGES:
        raise PathParseError(PROBLEM_TOO_MANY_STAGES)

    stages_complete = not completeness_problems or completeness_problems == [PROBLEM_SLACK_TIE]
    text = "\n".join(block.text_lines)
    return ParsedPath(
        scope=PathScope(
            analysis=ANALYSIS_SETUP, path_group=block.path_group, path_group_index=group_index,
            paths_in_section=paths_in_section, paths_per_group_declared=PATHS_PER_GROUP_DECLARED,
            selection=SELECTION_WORST_REPORTED_IN_SECTION, corner=block.corner,
        ),
        endpoints=PathEndpoints(
            startpoint_kind=block.startpoint_kind, endpoint_kind=block.endpoint_kind,
            launch_clock=block.launch_clock, capture_clock=block.capture_clock,
            startpoint_name=block.startpoint, endpoint_name=block.endpoint,
        ),
        timing=PathTiming(
            unit_time=UNIT_TIME, unit_cap=UNIT_CAP, resolution=RESOLUTION,
            clock_propagation=(CLOCK_IDEAL if ideal else CLOCK_PROPAGATED if propagated else None),
            **timing,
        ),
        stages=tuple(stages),
        completeness=PathCompleteness(
            stages_complete=stages_complete, stage_count=len(stages),
            rounding=ROUNDING_FIXED_DECIMAL, problems=tuple(completeness_problems),
        ),
        section=section, section_kind=SECTION_KIND_SETUP_PATH_REPORT,
        line_start=block.line_start, line_end=block.line_end,
        excerpt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _check_arrival_chain(stages: list[PathStage], launch_arrival: float | None, note: Any) -> None:
    """相鄰 arrival 必須等於前一個 arrival ＋ 本級 increment（容許一個解析度的捨入）。"""
    tolerance = Decimal(repr(RESOLUTION)) + Decimal("0.000001")
    previous = launch_arrival
    for stage in stages:
        if stage.incr is None or stage.arrival is None:
            note(PROBLEM_ARRIVAL_CHAIN_INCONSISTENT)
            return
        if stage.incr < 0:
            note(PROBLEM_NEGATIVE_INCREMENT)
        if previous is not None:
            expected = Decimal(repr(previous)) + Decimal(repr(stage.incr))
            if abs(expected - Decimal(repr(stage.arrival))) > tolerance:
                note(PROBLEM_ARRIVAL_CHAIN_INCONSISTENT)
        previous = stage.arrival


def _cell_stage(row: _Row, index: int, cell: str | None, instance: str | None, pin: str | None,
                *, role: str, function: str) -> PathStage:
    fanout, cap, slew, delay, arrival = row.numbers[-5:]
    return PathStage(
        index=index, kind=KIND_CELL, role=role, cell_function=function, cell_type=cell, incr=delay,
        arrival=arrival, fanout=int(fanout), cap=cap, slew=slew, line=row.line, instance=instance,
        net=None, pin=pin,
    )


def _net_stage(row: _Row, index: int, instance: str | None, pin: str | None,
               net: str | None) -> PathStage:
    if len(row.numbers) >= 3:
        slew, delay, arrival = row.numbers[-3:]
    elif len(row.numbers) == 2:
        slew, (delay, arrival) = None, row.numbers
    else:
        slew, delay, arrival = None, None, (row.numbers[-1] if row.numbers else None)
    # net arc 的 cell 欄位是 load pin 所屬 cell，不是這個 arc 的 cell；不寫入 cell_type。
    return PathStage(
        index=index, kind=KIND_NET, role=None, cell_function=None, cell_type=None, incr=delay,
        arrival=arrival, fanout=None, cap=None, slew=slew, line=row.line, instance=instance, net=net,
        pin=pin,
    )


def canonical_payload(payload: dict[str, Any]) -> str:
    """record payload 的 canonical JSON（bundle 端用來判斷重複 record 是否相同）。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ADAPTER_ROLE", "PATHS_PER_GROUP_DECLARED", "PROBLEM_HEADER_INCOMPLETE", "PROBLEM_NO_PATH_BLOCKS",
    "PROBLEM_NO_SETUP_PATH", "PROBLEM_REPORT_UNREADABLE", "PROBLEM_ROLE_UNUSABLE",
    "PROBLEM_SECTION_NOT_FOUND", "PROBLEM_TOO_MANY_STAGES", "ParsedPath", "PathParseError",
    "RECORD_SCHEMA_VERSION", "RESOLUTION", "SECTION_TITLE", "UNIT_CAP", "UNIT_TIME",
    "build_evidence", "canonical_payload", "classify_cell", "collect_record",
    "parse_worst_setup_path", "role_for_cell",
]
