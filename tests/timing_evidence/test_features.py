# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`timing_evidence.features`：單一 setup path 的診斷特徵（tool-neutral）。

基準資料是真實 ORFS GCD／Nangate45 finish report 的 core_clock worst reported setup path
（來源 SHA-256 `f6dcc72a…`，第 132–220 行；逐級表見 `orfs_dqe_timing_path_fixture.py` 的說明）。這裡直接以模型物件建構 path，不經 parser，讓特徵演算法可以獨立驗證。

所有期望值都是離線手算：data path 0.40 ns、CK→Q 0.12（30%）、最大組合 arc 0.04（10%，排名
有歧義）、cell 0.39／net 0.00／殘差 0.01、logic depth 15／14／12、skew 0.00。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from terminux_engine.timing_evidence import features as F
from terminux_engine.timing_evidence.model import (
    CLOCK_IDEAL,
    CLOCK_PROPAGATED,
    ENDPOINT_INPUT_PORT,
    ENDPOINT_REGISTER,
    KIND_CELL,
    KIND_NET,
    ROLE_BUFFER,
    ROLE_COMBINATIONAL,
    ROLE_INVERTER,
    ROLE_SEQUENTIAL_LAUNCH,
    ROLE_UNKNOWN,
    ROUNDING_FIXED_DECIMAL,
    SECTION_KIND_SETUP_PATH_REPORT,
    PathCompleteness,
    PathEndpoints,
    PathScope,
    PathSource,
    PathStage,
    PathTiming,
    TimingPathEvidence,
)

#: (index, kind, role, cell_function, cell_type, incr, arrival, fanout, cap, slew, source line)
GCD_STAGES: tuple[tuple, ...] = (
    (1, KIND_CELL, ROLE_SEQUENTIAL_LAUNCH, "flop", "DFF_X1", 0.12, 0.19, 4, 13.62, 0.03, 152),
    (2, KIND_NET, None, None, None, 0.00, 0.19, None, None, 0.03, 154),
    (3, KIND_CELL, ROLE_INVERTER, "inverter", "INV_X2", 0.02, 0.21, 2, 9.42, 0.01, 155),
    (4, KIND_NET, None, None, None, 0.00, 0.21, None, None, 0.01, 157),
    (5, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.02, 0.22, 1, 7.52, 0.01, 158),
    (6, KIND_NET, None, None, None, 0.00, 0.22, None, None, 0.01, 160),
    (7, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.01, 0.24, 2, 9.09, 0.01, 161),
    (8, KIND_NET, None, None, None, 0.00, 0.24, None, None, 0.01, 163),
    (9, KIND_CELL, ROLE_COMBINATIONAL, "nor", "NOR2_X4", 0.02, 0.26, 2, 8.42, 0.02, 164),
    (10, KIND_NET, None, None, None, 0.00, 0.26, None, None, 0.02, 166),
    (11, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.01, 0.28, 1, 5.86, 0.01, 167),
    (12, KIND_NET, None, None, None, 0.00, 0.28, None, None, 0.01, 169),
    (13, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.02, 0.29, 3, 11.11, 0.01, 170),
    (14, KIND_NET, None, None, None, 0.00, 0.29, None, None, 0.01, 172),
    (15, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.01, 0.31, 1, 7.21, 0.01, 173),
    (16, KIND_NET, None, None, None, 0.00, 0.31, None, None, 0.01, 175),
    (17, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.02, 0.32, 2, 9.96, 0.01, 176),
    (18, KIND_NET, None, None, None, 0.00, 0.32, None, None, 0.01, 178),
    (19, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X4", 0.02, 0.34, 5, 22.36, 0.01, 179),
    (20, KIND_NET, None, None, None, 0.00, 0.35, None, None, 0.01, 181),
    (21, KIND_CELL, ROLE_COMBINATIONAL, "aoi", "AOI21_X4", 0.04, 0.38, 3, 12.54, 0.03, 182),
    (22, KIND_NET, None, None, None, 0.00, 0.38, None, None, 0.03, 184),
    (23, KIND_CELL, ROLE_BUFFER, "buffer", "BUF_X8", 0.03, 0.41, 7, 36.05, 0.01, 185),
    (24, KIND_NET, None, None, None, 0.00, 0.42, None, None, 0.01, 187),
    (25, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X2", 0.01, 0.43, 1, 4.17, 0.01, 188),
    (26, KIND_NET, None, None, None, 0.00, 0.43, None, None, 0.01, 190),
    (27, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND3_X2", 0.02, 0.45, 1, 3.85, 0.01, 191),
    (28, KIND_NET, None, None, None, 0.00, 0.45, None, None, 0.01, 193),
    (29, KIND_CELL, ROLE_COMBINATIONAL, "nand", "NAND2_X2", 0.01, 0.46, 1, 1.95, 0.01, 194),
    (30, KIND_NET, None, None, None, 0.00, 0.46, None, None, 0.01, 196),
    (31, KIND_CELL, ROLE_INVERTER, "inverter", "INV_X1", 0.01, 0.47, 1, 2.45, 0.01, 197),
    (32, KIND_NET, None, None, None, 0.00, 0.47, None, None, 0.01, 199),
)

SHA = "f6dcc72a3b150fa26ae20067e36b6af3e67ff48a20fe0fb598ac3eb6ac86fcae"
MANIFEST_ID = "am_" + "0" * 32


def stage(row: tuple) -> PathStage:
    index, kind, role, function, cell_type, incr, arrival, fanout, cap, slew, line = row
    return PathStage(index=index, kind=kind, role=role, cell_function=function, cell_type=cell_type,
                     incr=incr, arrival=arrival, fanout=fanout, cap=cap, slew=slew, line=line,
                     instance=None, net=None, pin=None)


def gcd_path(**timing_overrides) -> TimingPathEvidence:
    timing = PathTiming(
        unit_time="ns", unit_cap="fF", resolution=0.01,
        data_arrival=0.47, data_required=0.29, slack=-0.18,
        launch_clock_edge=0.00, launch_clock_arrival=0.07,
        capture_clock_edge=0.25, capture_clock_arrival=0.32,
        crpr=0.00, setup_time=-0.03, clock_uncertainty=None, external_delay=None,
        clock_propagation=CLOCK_PROPAGATED,
    )
    timing = replace(timing, **timing_overrides)
    return TimingPathEvidence(
        source=PathSource(run_id="run_" + "a" * 32, attempt=1, step_id="orfs-gcd",
                          artifact_manifest_id=MANIFEST_ID, artifact_manifest_sha256="b" * 64,
                          artifact_instance_id=None, adapter_role="orfs.finish_report",
                          report_content_sha256=SHA,
                          section="finish report_checks -path_delay max",
                          section_kind=SECTION_KIND_SETUP_PATH_REPORT,
                          line_start=132, line_end=220, excerpt_hash="c" * 64),
        scope=PathScope(analysis="setup", path_group="core_clock", path_group_index=1,
                        paths_in_section=2, paths_per_group_declared=1,
                        selection="worst_reported_in_section", corner=None),
        endpoints=PathEndpoints(startpoint_kind=ENDPOINT_REGISTER, endpoint_kind=ENDPOINT_REGISTER,
                                launch_clock="core_clock", capture_clock="core_clock",
                                startpoint_name=None, endpoint_name=None),
        timing=timing,
        stages=tuple(stage(row) for row in GCD_STAGES),
        completeness=PathCompleteness(stages_complete=True, stage_count=len(GCD_STAGES),
                                      rounding=ROUNDING_FIXED_DECIMAL),
    )


def with_stages(path: TimingPathEvidence, stages: tuple[PathStage, ...], *,
                complete: bool = True, problems: tuple[str, ...] = ()) -> TimingPathEvidence:
    return replace(path, stages=stages,
                   completeness=PathCompleteness(stages_complete=complete, stage_count=len(stages),
                                                 rounding=ROUNDING_FIXED_DECIMAL, problems=problems))


# ── 正向：真實 GCD 值 ───────────────────────────────────────────────────────────

def test_data_path_delay_is_endpoint_arrival_minus_launch_clock_arrival() -> None:
    feature = F.feature_data_path_delay(gcd_path())
    assert feature.status == F.STATUS_DERIVED
    assert feature.value["value"] == pytest.approx(0.40)
    assert feature.value["unit"] == "ns"
    assert set(feature.assumptions) <= F.ASSUMPTION_CODES


def test_max_stage_is_launch_clock_to_q_and_combinational_max_is_ambiguous() -> None:
    feature = F.feature_max_stage_contribution(gcd_path())
    # 最大 stage（含 CK→Q）明確；最大組合 stage 的身分在 0.01 ns 解析度下有歧義。
    assert feature.status == F.STATUS_AMBIGUOUS
    top = feature.value["max_stage"]
    assert top["value"] == pytest.approx(0.12) and top["share"] == pytest.approx(0.30)
    assert top["stage_index"] == 1 and top["line"] == 152 and top["cell_function"] == "flop"
    assert top["role"] == ROLE_SEQUENTIAL_LAUNCH and top["ties"] == [1]
    assert top["ranking_ambiguous"] is False
    comb = feature.value["max_combinational_stage"]
    assert comb["value"] == pytest.approx(0.04) and comb["share"] == pytest.approx(0.10)
    assert comb["stage_index"] == 21 and comb["line"] == 182 and comb["cell_function"] == "aoi"
    assert comb["ties"] == [21]
    # 以相鄰 Time 欄差計算，AOI21_X4（0.38−0.35）與 BUF_X8（0.41−0.38）都是 0.03。
    assert comb["delta_t_candidates"] == [21, 23]
    assert comb["ranking_ambiguous"] is True
    assert "max_combinational_stage:" + F.REASON_RANKING_DIFFERS in feature.reasons
    assert set(feature.reasons) <= F.FEATURE_REASON_CODES
    net = feature.value["max_net_arc"]
    assert net["value"] == 0.0 and net["tie_count"] == 16 and net["stage_index"] is None


def test_cell_vs_net_delay_uses_displayed_values_and_reports_residual_and_bound() -> None:
    feature = F.feature_cell_vs_net_delay(gcd_path())
    assert feature.status == F.STATUS_DERIVED
    value = feature.value
    assert value["cell_sum"] == pytest.approx(0.39)
    assert value["net_sum_shown"] == 0.0
    assert value["residual"] == pytest.approx(0.01)
    assert value["tolerance"] == pytest.approx(0.16)  # 0.5 × 0.01 × 32
    assert value["cell_count"] == 16 and value["net_count"] == 16
    assert value["cell_share_shown"] == pytest.approx(0.975)
    # 16 個顯示 0.00 的 net arc，每個最多接近 0.005 → 上限 0.08 ns（20%）。
    assert value["net_sum_upper_bound"] == pytest.approx(0.08)
    assert value["net_share_upper_bound"] == pytest.approx(0.20)
    assert F.ASSUMPTION_NET_ZERO_IS_DISPLAY in feature.assumptions


def test_logic_depth_counts_q_to_d_cell_arcs_with_and_without_buffers_inverters() -> None:
    feature = F.feature_logic_depth(gcd_path())
    assert feature.status == F.STATUS_DERIVED
    value = feature.value
    assert value["logic_depth"] == 15
    assert value["buffer_count"] == 1 and value["inverter_count"] == 2
    assert value["depth_excl_buffers"] == 14
    assert value["depth_excl_buffers_inverters"] == 12
    assert value["role_unknown_count"] == 0
    assert value["cell_function_histogram"][0] == {"cell_function": "nand", "count": 10}
    assert sum(item["count"] for item in value["cell_function_histogram"]) == 15


def test_path_clock_skew_is_zero_within_resolution() -> None:
    feature = F.feature_path_clock_skew(gcd_path())
    assert feature.status == F.STATUS_DERIVED
    assert feature.value["value"] == 0.0
    assert feature.value["launch_latency"] == pytest.approx(0.07)
    assert feature.value["capture_latency"] == pytest.approx(0.07)
    assert feature.value["crpr"] == 0.0


def test_compute_features_is_deterministic_and_json_serializable() -> None:
    import json

    first = F.compute_features(gcd_path()).to_json_dict()
    second = F.compute_features(gcd_path()).to_json_dict()
    assert first == second
    assert first["schema_version"] == F.FEATURES_SCHEMA_VERSION
    assert set(first) == set(F.FEATURE_IDS) | {"schema_version"}
    json.dumps(first, allow_nan=False)


def test_cross_check_period_min_equals_data_path_plus_setup_minus_skew() -> None:
    path = gcd_path()
    data_path = F.feature_data_path_delay(path).value["value"]
    skew = F.feature_path_clock_skew(path).value["value"]
    # report_clock_min_period 的 core_clock period_min 0.43 ＝ 0.40 ＋ 0.03 − 0.00。
    assert data_path - path.timing.setup_time - skew == pytest.approx(0.43)


# ── 負向：缺值、分母、不完整、殘差、role、clock ───────────────────────────────

def test_port_startpoint_is_out_of_scope_not_zero() -> None:
    path = replace(gcd_path(), endpoints=replace(gcd_path().endpoints,
                                                 startpoint_kind=ENDPOINT_INPUT_PORT))
    for feature in (F.feature_data_path_delay(path), F.feature_max_stage_contribution(path),
                    F.feature_cell_vs_net_delay(path)):
        assert feature.status == F.STATUS_NOT_DERIVABLE
        assert F.REASON_STARTPOINT_IS_PORT in feature.reasons
        assert "value" not in feature.value


def test_missing_launch_clock_arrival_is_not_derivable() -> None:
    path = gcd_path(launch_clock_arrival=None)
    feature = F.feature_data_path_delay(path)
    assert feature.status == F.STATUS_NOT_DERIVABLE
    assert feature.reasons == (F.REASON_LAUNCH_CLOCK_ARRIVAL_MISSING,)
    assert F.feature_max_stage_contribution(path).status == F.STATUS_NOT_DERIVABLE


def test_missing_endpoint_arrival_is_not_derivable() -> None:
    feature = F.feature_data_path_delay(gcd_path(data_arrival=None))
    assert feature.status == F.STATUS_NOT_DERIVABLE
    assert feature.reasons == (F.REASON_ENDPOINT_ARRIVAL_MISSING,)


def test_non_positive_denominator_is_rejected() -> None:
    feature = F.feature_data_path_delay(gcd_path(data_arrival=0.07))
    assert feature.status == F.STATUS_NOT_DERIVABLE
    assert feature.reasons == (F.REASON_NON_POSITIVE_DENOMINATOR,)
    assert F.feature_cell_vs_net_delay(gcd_path(data_arrival=0.05)).status == F.STATUS_NOT_DERIVABLE


def test_incomplete_stage_list_blocks_stage_features_but_not_path_delay() -> None:
    base = gcd_path()
    path = with_stages(base, base.stages[:10], complete=False, problems=("endpoint_row_missing",))
    assert F.feature_data_path_delay(path).status == F.STATUS_DERIVED
    for feature in (F.feature_max_stage_contribution(path), F.feature_cell_vs_net_delay(path),
                    F.feature_logic_depth(path)):
        assert feature.status == F.STATUS_NOT_DERIVABLE
        assert F.REASON_STAGES_INCOMPLETE in feature.reasons


def test_incomplete_flag_alone_blocks_logic_depth_even_with_all_stages_present() -> None:
    """parser 判定鏈不連續時，即使 stage 數看起來完整，logic depth 也不得推導（B1）。"""
    base = gcd_path()
    path = with_stages(base, base.stages, complete=False, problems=("stage_sequence_broken",))
    feature = F.feature_logic_depth(path)
    assert feature.status == F.STATUS_NOT_DERIVABLE and "logic_depth" not in feature.value


def test_empty_stage_list_is_no_stages() -> None:
    path = with_stages(gcd_path(), ())
    assert F.feature_logic_depth(path).reasons == (F.REASON_NO_STAGES,)
    assert F.REASON_NO_STAGES in F.feature_max_stage_contribution(path).reasons


def test_missing_or_negative_increment_is_not_derivable() -> None:
    base = gcd_path()
    missing = with_stages(base, (replace(base.stages[0], incr=None),) + base.stages[1:])
    assert F.REASON_STAGE_INCREMENT_MISSING in F.feature_cell_vs_net_delay(missing).reasons
    negative = with_stages(base, (replace(base.stages[0], incr=-0.12),) + base.stages[1:])
    assert F.REASON_NEGATIVE_INCREMENT in F.feature_max_stage_contribution(negative).reasons


def test_sum_inconsistent_with_data_path_delay_is_rejected() -> None:
    # 把 data arrival 推到 1.47：cell＋net 顯示和 0.39 與 1.40 的差遠超過捨入容許值。
    feature = F.feature_cell_vs_net_delay(gcd_path(data_arrival=1.47))
    assert feature.status == F.STATUS_NOT_DERIVABLE
    assert feature.reasons == (F.REASON_SUM_INCONSISTENT,)
    assert set(feature.value) == {"residual", "tolerance"}


def test_unknown_role_withholds_buffer_and_inverter_counts() -> None:
    base = gcd_path()
    stages = tuple(replace(s, role=ROLE_UNKNOWN, cell_function="other") if s.index == 23 else s
                   for s in base.stages)
    feature = F.feature_logic_depth(with_stages(base, stages))
    assert feature.status == F.STATUS_DERIVED
    assert feature.value["logic_depth"] == 15
    assert feature.value["role_unknown_count"] == 1
    assert feature.value["buffer_count"] is None and feature.value["depth_excl_buffers"] is None
    assert feature.reasons == (F.REASON_ROLE_UNKNOWN,)


def test_ideal_or_missing_clock_makes_skew_not_derivable() -> None:
    ideal = F.feature_path_clock_skew(gcd_path(clock_propagation=CLOCK_IDEAL))
    assert ideal.status == F.STATUS_NOT_DERIVABLE and F.REASON_CLOCK_NOT_PROPAGATED in ideal.reasons
    missing = F.feature_path_clock_skew(gcd_path(capture_clock_arrival=None))
    assert missing.status == F.STATUS_NOT_DERIVABLE
    assert F.REASON_CLOCK_ARRIVAL_MISSING in missing.reasons


def test_missing_crpr_row_is_not_derivable_not_zero() -> None:
    """C1：報告沒有 CRPR 列時不得以 0 代入；有明確 0.00 時才是 derived 0。"""
    missing = F.feature_path_clock_skew(gcd_path(crpr=None))
    assert missing.status == F.STATUS_NOT_DERIVABLE
    assert missing.reasons == (F.REASON_CRPR_MISSING,)
    assert "value" not in missing.value
    assert F.feature_path_clock_skew(gcd_path(crpr=0.0)).status == F.STATUS_DERIVED


def test_unambiguous_ranking_when_delta_t_agrees_with_delay_column() -> None:
    # 把 BUF_X8 的 arrival 從 0.41 改成 0.40：Δt 變成 0.02，AOI21_X4 成為唯一候選。
    base = gcd_path()
    stages = tuple(replace(s, arrival=0.40) if s.index == 23 else s for s in base.stages)
    feature = F.feature_max_stage_contribution(with_stages(base, stages))
    assert feature.status == F.STATUS_DERIVED
    comb = feature.value["max_combinational_stage"]
    assert comb["delta_t_candidates"] == [21] and comb["ranking_ambiguous"] is False


def test_missing_arrival_makes_ranking_check_unavailable_and_ambiguous() -> None:
    base = gcd_path()
    stages = tuple(replace(s, arrival=None) if s.index == 2 else s for s in base.stages)
    feature = F.feature_max_stage_contribution(with_stages(base, stages))
    assert feature.status == F.STATUS_AMBIGUOUS
    assert "max_stage:" + F.REASON_DELTA_T_UNAVAILABLE in feature.reasons
    assert feature.value["max_stage"]["value"] == pytest.approx(0.12)


def test_all_emitted_reasons_and_assumptions_are_in_the_closed_vocabularies() -> None:
    variants = [gcd_path(), gcd_path(crpr=None), gcd_path(clock_propagation=CLOCK_IDEAL),
                gcd_path(data_arrival=1.47), gcd_path(launch_clock_arrival=None)]
    base = gcd_path()
    variants.append(with_stages(base, base.stages[:4], complete=False, problems=("endpoint_row_missing",)))
    for path in variants:
        for feature in F.compute_features(path).to_json_dict().values():
            if not isinstance(feature, dict):
                continue
            assert set(feature["reasons"]) <= F.FEATURE_REASON_CODES
            assert set(feature["assumptions"]) <= F.ASSUMPTION_CODES
