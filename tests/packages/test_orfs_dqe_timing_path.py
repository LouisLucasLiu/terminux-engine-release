# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`orfs_dqe.timing_path`：從真實 ORFS finish report 摘錄解析單一 worst reported setup path。

資料來源與手算基準見同目錄的 `orfs_dqe_timing_path_fixture.py`。這些測試只用靜態摘錄，
不執行任何 EDA 工具，也不建立 receipt 或 EventStore 投影。
"""

from __future__ import annotations

import hashlib

import pytest

from terminux_engine.packages.orfs_dqe import parsers, timing_path as T
from terminux_engine.timing_evidence import features as F
from terminux_engine.timing_evidence import model as M
from terminux_engine.timing_evidence.model import (
    CLOCK_IDEAL,
    CLOCK_PROPAGATED,
    ENDPOINT_OUTPUT_PORT,
    ENDPOINT_REGISTER,
    KIND_CELL,
    KIND_NET,
    ROLE_BUFFER,
    ROLE_COMBINATIONAL,
    ROLE_INVERTER,
    ROLE_SEQUENTIAL_LAUNCH,
    ROLE_UNKNOWN,
)
from tests.packages import orfs_dqe_timing_path_fixture as X
from tests.timing_evidence.test_features import GCD_STAGES

SECTION_LINES = X.FINISH_REPORT_PATH_SECTION.splitlines()


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _core_clock_only() -> list[str]:
    """只保留 core_clock block 的 section（header ＋ 來源第 132–220 行）。"""
    end = X.excerpt_line(X.CORE_CLOCK_BLOCK_LINES[1])
    return SECTION_LINES[:end]


def _vclk_only() -> list[str]:
    header = SECTION_LINES[:X.excerpt_line(X.CORE_CLOCK_BLOCK_LINES[0]) - 1]
    start, end = (X.excerpt_line(n) for n in X.VCLK_BLOCK_LINES)
    return header + SECTION_LINES[start - 1:end]


def _without_line(lines: list[str], source_line_no: int) -> list[str]:
    target = X.excerpt_line(source_line_no)
    return [line for n, line in enumerate(lines, start=1) if n != target]


def _evidence(parsed: T.ParsedPath):
    return T.build_evidence(parsed, run_id="run_" + "1" * 32, attempt=1, step_id="orfs-gcd",
                            artifact_manifest_id="am_" + "0" * 32, artifact_manifest_sha256="0" * 64,
                            artifact_instance_id=None, report_content_sha256=X.SOURCE_REPORT_SHA256)


# ── 選取與範圍 ──────────────────────────────────────────────────────────────

def test_selects_worst_reported_setup_path_in_section() -> None:
    parsed = T.parse_worst_setup_path(SECTION_LINES)
    assert parsed.timing.slack == pytest.approx(-0.18)   # core_clock（−0.18）而非 vclk（−0.17）
    assert parsed.scope.path_group == "core_clock"
    assert parsed.scope.path_group_index == 1 and parsed.scope.paths_in_section == 2
    assert parsed.scope.paths_per_group_declared == 1
    assert parsed.scope.selection == "worst_reported_in_section"
    assert parsed.scope.global_worst_claim is False
    assert parsed.scope.corner is None  # 報告沒有 Corner 列，不猜
    assert parsed.section == T.SECTION_TITLE and parsed.section_kind == "setup_path_report"


def test_endpoints_and_clocks_are_identified_from_the_header() -> None:
    parsed = T.parse_worst_setup_path(SECTION_LINES)
    assert parsed.endpoints.startpoint_kind == ENDPOINT_REGISTER
    assert parsed.endpoints.endpoint_kind == ENDPOINT_REGISTER
    assert parsed.endpoints.launch_clock == parsed.endpoints.capture_clock == "core_clock"
    # 物件名稱只保留在本機證據；對外投影不含（見 timing_evidence tests）。
    assert parsed.endpoints.startpoint_name == "dpath.a_reg.out[2]$_DFFE_PP_"
    assert parsed.endpoints.endpoint_name == "dpath.a_reg.out[5]$_DFFE_PP_"


def test_path_level_timing_values_match_the_report() -> None:
    timing = T.parse_worst_setup_path(SECTION_LINES).timing
    assert (timing.unit_time, timing.unit_cap, timing.resolution) == ("ns", "fF", 0.01)
    assert timing.data_arrival == pytest.approx(0.47)
    assert timing.data_required == pytest.approx(0.29)
    assert timing.launch_clock_edge == 0.0 and timing.launch_clock_arrival == pytest.approx(0.07)
    assert timing.capture_clock_edge == pytest.approx(0.25)
    assert timing.capture_clock_arrival == pytest.approx(0.32)
    assert timing.crpr == 0.0 and timing.setup_time == pytest.approx(-0.03)
    assert timing.clock_uncertainty is None and timing.external_delay is None  # 報告沒有這些列
    assert timing.clock_propagation == CLOCK_PROPAGATED


# ── 逐級資料 ────────────────────────────────────────────────────────────────

def test_stage_table_matches_the_hand_derived_reference() -> None:
    parsed = T.parse_worst_setup_path(SECTION_LINES)
    assert parsed.completeness.stages_complete and parsed.completeness.problems == ()
    assert len(parsed.stages) == len(GCD_STAGES) == 32
    for stage, expected in zip(parsed.stages, GCD_STAGES):
        (index, kind, role, function, cell_type, incr, arrival, fanout, cap, slew,
         source_line) = expected
        assert (stage.index, stage.kind, stage.role, stage.cell_function, stage.cell_type) == (
            index, kind, role, function, cell_type)
        assert stage.incr == pytest.approx(incr) and stage.arrival == pytest.approx(arrival)
        assert stage.fanout == fanout
        assert (stage.cap is None) == (cap is None) and (cap is None or stage.cap == pytest.approx(cap))
        assert stage.slew == pytest.approx(slew)
        assert X.source_line(stage.line) == source_line


def test_cell_and_net_rows_are_told_apart_by_column_count() -> None:
    parsed = T.parse_worst_setup_path(SECTION_LINES)
    cells = [s for s in parsed.stages if s.kind == KIND_CELL]
    nets = [s for s in parsed.stages if s.kind == KIND_NET]
    assert len(cells) == len(nets) == 16
    assert cells[0].role == ROLE_SEQUENTIAL_LAUNCH and cells[0].pin == "Q"
    assert cells[0].instance == "dpath.a_reg.out[2]$_DFFE_PP_"
    assert nets[0].net == "dpath.a_lt_b$in0[2]" and nets[0].instance == "_517_" and nets[0].pin == "A"
    assert nets[-1].instance == "dpath.a_reg.out[5]$_DFFE_PP_" and nets[-1].pin == "D"
    assert all(s.cell_type is None and s.role is None and s.cell_function is None and s.fanout is None
               for s in nets)


def test_provenance_points_at_the_block_lines_and_hashes_the_excerpt() -> None:
    parsed = T.parse_worst_setup_path(SECTION_LINES)
    assert X.source_line(parsed.line_start) == X.CORE_CLOCK_BLOCK_LINES[0]
    assert X.source_line(parsed.line_end) == X.CORE_CLOCK_BLOCK_LINES[1]
    block = SECTION_LINES[parsed.line_start - 1:parsed.line_end]
    assert block[0].startswith("Startpoint:") and block[-1].strip().endswith("slack (VIOLATED)")
    expected = hashlib.sha256("\n".join(line.rstrip() for line in block).encode("utf-8")).hexdigest()
    assert parsed.excerpt_hash == expected


# ── 與特徵層銜接 ────────────────────────────────────────────────────────────

def test_features_from_parsed_path_reproduce_the_hand_calculation() -> None:
    features = F.compute_features(_evidence(T.parse_worst_setup_path(SECTION_LINES)))
    assert features.data_path_delay.value["value"] == pytest.approx(0.40)
    top = features.max_stage_contribution.value
    assert top["max_stage"]["value"] == pytest.approx(0.12) and top["max_stage"]["share"] == pytest.approx(0.3)
    assert top["max_combinational_stage"]["value"] == pytest.approx(0.04)
    assert top["max_combinational_stage"]["cell_function"] == "aoi"
    assert top["max_combinational_stage"]["ranking_ambiguous"] is True
    assert features.max_stage_contribution.status == F.STATUS_AMBIGUOUS
    cell_net = features.cell_vs_net_delay.value
    assert cell_net["cell_sum"] == pytest.approx(0.39) and cell_net["net_sum_shown"] == 0.0
    assert cell_net["residual"] == pytest.approx(0.01)
    depth = features.logic_depth.value
    assert (depth["logic_depth"], depth["depth_excl_buffers"], depth["depth_excl_buffers_inverters"]) == (15, 14, 12)
    assert features.path_clock_skew.status == F.STATUS_DERIVED
    assert features.path_clock_skew.value["value"] == 0.0


# ── Nangate45 role／function 對應（adapter 知識）─────────────────────────────────

@pytest.mark.parametrize("cell_type,role,function", [
    ("BUF_X8", ROLE_BUFFER, "buffer"), ("CLKBUF_X3", ROLE_BUFFER, "buffer"),
    ("INV_X2", ROLE_INVERTER, "inverter"),
    ("NAND2_X4", ROLE_COMBINATIONAL, "nand"), ("NOR2_X4", ROLE_COMBINATIONAL, "nor"),
    ("AOI21_X4", ROLE_COMBINATIONAL, "aoi"), ("OAI22_X1", ROLE_COMBINATIONAL, "oai"),
    ("XNOR2_X2", ROLE_COMBINATIONAL, "xnor"), ("XOR2_X1", ROLE_COMBINATIONAL, "xor"),
    ("AND2_X1", ROLE_COMBINATIONAL, "and"), ("OR2_X1", ROLE_COMBINATIONAL, "or"),
    ("MUX2_X1", ROLE_COMBINATIONAL, "mux"), ("HA_X1", ROLE_COMBINATIONAL, "adder"),
    ("DFF_X1", ROLE_UNKNOWN, "flop"), ("SDFF_X1", ROLE_UNKNOWN, "flop"),
    ("DLH_X1", ROLE_UNKNOWN, "latch"), ("CLKGATETST_X1", ROLE_UNKNOWN, "clock_gate"),
    ("sky130_fd_sc_hd__nand2_1", ROLE_UNKNOWN, "other"), (None, ROLE_UNKNOWN, "other"),
    ("", ROLE_UNKNOWN, "other"),
])
def test_cell_classification_is_explicit_and_closed(cell_type: str | None, role: str, function: str) -> None:
    assert T.classify_cell(cell_type) == (role, function)
    assert T.role_for_cell(cell_type) == role
    assert function in M.CELL_FUNCTIONS


# ── 第二條 path（output port、ideal virtual clock、external delay）───────────────

def test_output_port_endpoint_with_ideal_clock_is_parsed_but_limits_features() -> None:
    parsed = T.parse_worst_setup_path(_vclk_only())
    assert parsed.scope.paths_in_section == 1 and parsed.scope.path_group == "vclk_core_clock"
    assert parsed.endpoints.endpoint_kind == ENDPOINT_OUTPUT_PORT
    assert parsed.endpoints.capture_clock == "vclk_core_clock"
    assert parsed.timing.external_delay == pytest.approx(-0.05)
    assert parsed.timing.clock_propagation == CLOCK_IDEAL
    assert parsed.timing.capture_clock_arrival == pytest.approx(0.32)
    assert parsed.timing.setup_time is None  # port endpoint 沒有 library setup 列
    assert parsed.stages[-1].kind == KIND_NET and parsed.stages[-1].arrival == pytest.approx(0.44)
    assert parsed.completeness.stages_complete, parsed.completeness.problems
    features = F.compute_features(_evidence(parsed))
    assert features.data_path_delay.value["value"] == pytest.approx(0.37)
    assert features.path_clock_skew.status == F.STATUS_NOT_DERIVABLE
    assert F.REASON_CLOCK_NOT_PROPAGATED in features.path_clock_skew.reasons


def test_single_block_section_keeps_identical_stage_values() -> None:
    whole = T.parse_worst_setup_path(SECTION_LINES)
    alone = T.parse_worst_setup_path(_core_clock_only())
    assert alone.scope.paths_in_section == 1
    assert [(s.kind, s.incr, s.arrival) for s in alone.stages] == [(s.kind, s.incr, s.arrival) for s in whole.stages]
    assert alone.excerpt_hash == whole.excerpt_hash


# ── 畸形報告：fail closed，不給部分結果 ───────────────────────────────────────

def test_missing_section_is_a_parse_error() -> None:
    lines = _lines("==========================================================================\n"
                   "finish report_wns\n"
                   "--------------------------------------------------------------------------\n"
                   "wns max -0.18\n")
    with pytest.raises(T.PathParseError) as excinfo:
        T.parse_worst_setup_path(lines)
    assert excinfo.value.code == T.PROBLEM_SECTION_NOT_FOUND


def test_section_without_any_path_block_is_rejected() -> None:
    header = SECTION_LINES[:4]
    with pytest.raises(T.PathParseError) as excinfo:
        T.parse_worst_setup_path(header + ["No paths found."])
    assert excinfo.value.code == T.PROBLEM_NO_PATH_BLOCKS


def test_block_truncated_before_slack_is_not_a_setup_path() -> None:
    truncated = [line for line in _core_clock_only() if "slack" not in line]
    with pytest.raises(T.PathParseError) as excinfo:
        T.parse_worst_setup_path(truncated)
    assert excinfo.value.code == T.PROBLEM_NO_SETUP_PATH


def test_hold_only_block_is_not_selected() -> None:
    lines = [line.replace("Path Type: max", "Path Type: min") for line in _core_clock_only()]
    with pytest.raises(T.PathParseError) as excinfo:
        T.parse_worst_setup_path(lines)
    assert excinfo.value.code == T.PROBLEM_NO_SETUP_PATH


def test_missing_launch_clock_pin_row_marks_the_path_incomplete() -> None:
    parsed = T.parse_worst_setup_path(_without_line(_core_clock_only(), X.CORE_CLOCK_KEY_LINES["launch_ck"]))
    assert parsed.timing.launch_clock_arrival is None
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_STARTPOINT_ROW_MISSING in parsed.completeness.problems
    # 缺值不是 0：特徵層必須回 not_derivable。
    assert F.compute_features(_evidence(parsed)).data_path_delay.status == F.STATUS_NOT_DERIVABLE


def test_missing_endpoint_pin_row_marks_the_path_incomplete() -> None:
    parsed = T.parse_worst_setup_path(_without_line(_core_clock_only(), X.CORE_CLOCK_KEY_LINES["endpoint_d"]))
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_ENDPOINT_ROW_MISSING in parsed.completeness.problems


# ── B1：中段缺列、未知列、鏈不連續都不得判為完整 ────────────────────────────────

def test_missing_middle_output_row_breaks_the_chain_and_blocks_logic_depth() -> None:
    """刪掉 AOI21_X4 的輸出列（來源第 182 行），endpoint 與 data arrival 都還在。"""
    parsed = T.parse_worst_setup_path(_without_line(_core_clock_only(), X.CORE_CLOCK_KEY_LINES["aoi21"]))
    assert parsed.timing.data_arrival == pytest.approx(0.47)      # 終點仍在
    assert len(parsed.stages) == 31
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_STAGE_SEQUENCE_BROKEN in parsed.completeness.problems
    features = F.compute_features(_evidence(parsed))
    assert features.logic_depth.status == F.STATUS_NOT_DERIVABLE   # 不是誤報 14
    assert features.max_stage_contribution.status == F.STATUS_NOT_DERIVABLE
    assert features.cell_vs_net_delay.status == F.STATUS_NOT_DERIVABLE


def test_missing_middle_input_row_breaks_the_chain() -> None:
    """刪掉 `_840_/B1` 的輸入列（net arc，來源第 181 行）：兩個 cell 列相鄰。"""
    parsed = T.parse_worst_setup_path(_without_line(_core_clock_only(), 181))
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_STAGE_SEQUENCE_BROKEN in parsed.completeness.problems


def test_duplicated_middle_row_breaks_the_chain() -> None:
    lines = _core_clock_only()
    target = X.excerpt_line(X.CORE_CLOCK_KEY_LINES["aoi21"])
    lines = lines[:target] + [lines[target - 1]] + lines[target:]
    parsed = T.parse_worst_setup_path(lines)
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_STAGE_SEQUENCE_BROKEN in parsed.completeness.problems


def test_unrecognized_row_inside_the_data_path_is_flagged() -> None:
    lines = _core_clock_only()
    target = X.excerpt_line(X.CORE_CLOCK_KEY_LINES["aoi21"])
    lines = lines[:target] + ["                          annotation row not from OpenSTA"] + lines[target:]
    parsed = T.parse_worst_setup_path(lines)
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_UNRECOGNIZED_ROW in parsed.completeness.problems


def test_arrival_jump_without_matching_increment_is_flagged() -> None:
    lines = _core_clock_only()
    target = X.excerpt_line(X.CORE_CLOCK_KEY_LINES["buf_x8"])
    assert "0.41" in lines[target - 1]
    lines[target - 1] = lines[target - 1].replace("0.41", "0.61")   # arrival 跳 0.20，incr 仍 0.03
    parsed = T.parse_worst_setup_path(lines)
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_ARRIVAL_CHAIN_INCONSISTENT in parsed.completeness.problems


def test_negative_increment_is_flagged() -> None:
    lines = _core_clock_only()
    target = X.excerpt_line(X.CORE_CLOCK_KEY_LINES["aoi21"])
    lines[target - 1] = lines[target - 1].replace("    0.04    0.38", "   -0.04    0.38")
    parsed = T.parse_worst_setup_path(lines)
    assert not parsed.completeness.stages_complete
    assert M.PROBLEM_NEGATIVE_INCREMENT in parsed.completeness.problems


def test_intact_excerpt_has_no_completeness_problems() -> None:
    for lines in (SECTION_LINES, _core_clock_only(), _vclk_only()):
        parsed = T.parse_worst_setup_path(lines)
        assert parsed.completeness.stages_complete and parsed.completeness.problems == ()
        assert all(p in M.COMPLETENESS_PROBLEMS for p in parsed.completeness.problems)


# ── C1：缺 CRPR 列 ────────────────────────────────────────────────────────────

def test_removed_pessimism_row_yields_none_and_not_derivable_skew() -> None:
    lines = [line for line in _core_clock_only() if "clock reconvergence pessimism" not in line]
    parsed = T.parse_worst_setup_path(lines)
    assert parsed.timing.crpr is None
    assert parsed.completeness.stages_complete  # data path 本身仍完整
    skew = F.compute_features(_evidence(parsed)).path_clock_skew
    assert skew.status == F.STATUS_NOT_DERIVABLE and skew.reasons == (F.REASON_CRPR_MISSING,)


def test_too_many_stages_fails_closed() -> None:
    base = _core_clock_only()
    insert_at = X.excerpt_line(X.CORE_CLOCK_KEY_LINES["aoi21"])
    extra: list[str] = []
    for i in range(40):
        extra.append(f"                  0.01    0.00    0.35 v _x{i}_/A1 (NAND2_X1)")
        extra.append(f"     1    4.00    0.01    0.00    0.35 ^ _x{i}_/ZN (NAND2_X1)")
    lines = base[:insert_at] + extra + base[insert_at:]
    with pytest.raises(T.PathParseError) as excinfo:
        T.parse_worst_setup_path(lines)
    assert excinfo.value.code == T.PROBLEM_TOO_MANY_STAGES


def test_structurally_broken_report_raises_parsers_error() -> None:
    doubled = SECTION_LINES + SECTION_LINES  # 重複 section title → split_finish_sections 拒絕
    with pytest.raises(parsers.ParseError):
        T.parse_worst_setup_path(doubled)
