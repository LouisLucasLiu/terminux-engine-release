# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""固定的 setup repair 驗收判定。

`gcd.setup_repair_acceptance.v1`。**deterministic**：輸入只有兩個 run 各自
在正式 flow 內寫出的證據檔（setup endpoint JSON、core JSON、ECO diff、
兩組等價 proof）與既有 DQE 比較結果。不讀 LLM 敘述、不猜方向。

程序成功、DQE 寫入成功、等價成功與 timing 成功**分列**。任何一項未達標就
`repair_status = NOT_REPAIRED`；必要資料缺失或工具檢查未完成則
`UNVERIFIABLE`——都不會被寫成 REPAIRED。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POLICY_ID = "gcd.setup_repair_acceptance.v1"
POLICY_VERSION = "v1"

REPAIRED = "REPAIRED"
NOT_REPAIRED = "NOT_REPAIRED"
UNVERIFIABLE = "UNVERIFIABLE"

#: 必要品質項。方向與門檻沿既有 DQE 規則。
REQUIRED_QUALITY_METRICS: tuple[str, ...] = (
    "orfs_hold_violation_count",
    "orfs_route_drc_violation_count",
    "orfs_max_slew_violation_count",
    "orfs_max_cap_violation_count",
    "orfs_max_fanout_violation_count",
)

R_PROBLEM_ABSENT = "baseline_has_no_setup_violation"
R_EXECUTION = "execution_incomplete"
R_INPUTS = "input_or_diff_not_approved"
R_NO_NETLIST_CHANGE = "no_real_netlist_change"
R_COVERAGE = "coverage_reduced_or_missing"
R_STILL_VIOLATING = "setup_groups_still_violating"
R_PROOF = "equivalence_not_proven"
R_QUALITY = "required_quality_regressed"
R_AREA = "core_area_increased"
R_EVIDENCE_MISSING = "required_evidence_missing"
R_TARGET_SLACK_NOT_MET = "target_slack_not_met"
R_OK = "ok"


@dataclass(frozen=True)
class GroupVerdict:
    group: str
    analysed_endpoints: int
    violating_endpoints: int
    tns: float
    wns: float

    def to_json_dict(self) -> dict[str, Any]:
        return {"group": self.group, "analysed_endpoints": self.analysed_endpoints,
                "violating_endpoints": self.violating_endpoints,
                "tns": self.tns, "wns": self.wns}


@dataclass(frozen=True)
class RepairVerdict:
    policy_id: str
    policy_version: str
    repair_status: str
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    baseline_groups: tuple[GroupVerdict, ...] = ()
    candidate_groups: tuple[GroupVerdict, ...] = ()
    eco_diff: dict[str, Any] = field(default_factory=dict)
    proofs: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    core_area: dict[str, Any] = field(default_factory=dict)
    #: 固定用語。不得改寫成 signoff 或「已完成第一波修復」。
    statement: str = ("本次固定要求下的 setup repair 判定；不是多 corner "
                      "signoff，也不宣稱整體 timing 已完成")
    target_slack_ns: float | None = None
    actual_slack_ns: float | None = None
    target_slack_met: bool | None = None
    timing_repaired: bool | None = None

    def to_json_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "policy_id": self.policy_id, "policy_version": self.policy_version,
            "repair_status": self.repair_status,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "baseline_groups": [g.to_json_dict() for g in self.baseline_groups],
            "candidate_groups": [g.to_json_dict() for g in self.candidate_groups],
            "eco_diff": dict(self.eco_diff), "proofs": dict(self.proofs),
            "quality": dict(self.quality), "core_area": dict(self.core_area),
            "statement": self.statement,
        }
        if self.target_slack_ns is not None:
            out["target_slack_ns"] = self.target_slack_ns
        if self.actual_slack_ns is not None:
            out["actual_slack_ns"] = self.actual_slack_ns
        if self.target_slack_met is not None:
            out["target_slack_met"] = self.target_slack_met
        if self.timing_repaired is not None:
            out["timing_repaired"] = self.timing_repaired
        return out


def _groups(payload: Any) -> tuple[GroupVerdict, ...]:
    rows = (payload or {}).get("groups") or []
    out = []
    for row in rows:
        try:
            out.append(GroupVerdict(
                group=str(row["group"]),
                analysed_endpoints=int(row["analysed_endpoints"]),
                violating_endpoints=int(row["violating_endpoints"]),
                tns=float(row["tns"]), wns=float(row["wns"])))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def _group_slack_unusable_reason(
    payload: Any, groups: tuple[GroupVerdict, ...], coverage_reduced: bool,
) -> str | None:
    """宣告 target 時，group WNS 只有在來源可確認且涵蓋完整時才可作為餘量證據。"""
    if not isinstance(payload, dict):
        return "setup evidence is not an object"
    if payload.get("time_unit") != "ns":
        return f"setup evidence time_unit is {payload.get('time_unit')!r}, not 'ns'"
    rows = payload.get("groups")
    if not isinstance(rows, list) or len(rows) != len(groups):
        return "some setup group rows could not be parsed"
    if payload.get("counts_agree") is not True:
        return "enumerated and global setup violation counts are not confirmed to agree"
    if coverage_reduced:
        return "candidate setup coverage is reduced relative to the baseline"
    return None


def evaluate_repair(
    *,
    baseline_setup: Any,
    candidate_setup: Any,
    baseline_core: Any,
    candidate_core: Any,
    eco_diff: Any,
    eco_proof: Any,
    final_pair_proof: Any,
    metric_comparisons: Any,
    execution_ok: bool,
    inputs_approved: bool,
    inputs_reason: str | None = None,
    target_slack_ns: float | None = None,
    actual_slack_ns: float | None = None,
) -> RepairVerdict:
    """逐項判定固定的品質 gate。"""
    codes: list[str] = []
    reasons: list[str] = []
    unverifiable = False

    def fail(code: str, reason: str) -> None:
        if code not in codes:
            codes.append(code)
        reasons.append(reason)

    def missing(reason: str) -> None:
        nonlocal unverifiable
        unverifiable = True
        fail(R_EVIDENCE_MISSING, reason)

    # ── 執行與允許差異 ──
    if not execution_ok:
        fail(R_EXECUTION, "at least one of the two runs did not complete")
    if not inputs_approved:
        fail(R_INPUTS, inputs_reason or "the input identity or diff is not approved")

    # ── 必要證據 ──
    for label, payload in (("baseline setup evidence", baseline_setup),
                           ("candidate setup evidence", candidate_setup),
                           ("ECO topology diff", eco_diff),
                           ("pre/post ECO equivalence proof", eco_proof),
                           ("baseline/candidate final equivalence proof",
                            final_pair_proof)):
        if not payload:
            missing(f"{label} is missing")

    base_groups = _groups(baseline_setup)
    cand_groups = _groups(candidate_setup)

    # ── 問題確實存在 ──
    base_violations = sum(g.violating_endpoints for g in base_groups)
    if base_groups and base_violations == 0:
        fail(R_PROBLEM_ABSENT,
             "the new baseline already has no setup violation, so this ECO "
             "cannot be said to have repaired one")

    # ── coverage 未減少 ──
    coverage_reduced = False
    if base_groups and cand_groups:
        base_by = {g.group: g for g in base_groups}
        cand_by = {g.group: g for g in cand_groups}
        for name, g in base_by.items():
            other = cand_by.get(name)
            if other is None:
                coverage_reduced = True
                fail(R_COVERAGE, f"clock group {name!r} disappeared on the candidate")
            elif other.analysed_endpoints < g.analysed_endpoints:
                coverage_reduced = True
                fail(R_COVERAGE,
                     f"clock group {name!r} analysed {other.analysed_endpoints} "
                     f"endpoints on the candidate vs {g.analysed_endpoints} on "
                     "the baseline")
        for payload, side in ((baseline_setup, "baseline"),
                              (candidate_setup, "candidate")):
            if payload and payload.get("counts_agree") is False:
                coverage_reduced = True
                fail(R_COVERAGE,
                     f"the {side} enumerated violation count disagrees with the "
                     "global count")

    # ── 兩個 group 都必須零違規 ──
    timing_repaired = True
    if cand_groups:
        for g in cand_groups:
            # 用工具原始數值判定，不用顯示字串。
            if g.violating_endpoints != 0 or g.tns < 0.0 or g.wns < 0.0:
                timing_repaired = False
                fail(R_STILL_VIOLATING,
                     f"clock group {g.group!r} still has "
                     f"{g.violating_endpoints} violating endpoints "
                     f"(wns={g.wns!r}, tns={g.tns!r})")
    elif candidate_setup:
        timing_repaired = False
        missing("the candidate setup evidence lists no clock group")
    else:
        timing_repaired = False

    # ── 真實 netlist 變更 ──
    if eco_diff:
        deltas = [eco_diff.get(k) for k in
                  ("instance_delta", "net_delta", "iterm_delta")]
        changed_masters = eco_diff.get("changed_masters") or []
        if all((d in (0, "0", None)) for d in deltas) and not changed_masters:
            fail(R_NO_NETLIST_CHANGE,
                 "the ECO produced no cell or connectivity change")

    # ── 兩組等價證明 ──
    for label, proof in (("pre/post ECO", eco_proof),
                         ("baseline/candidate final", final_pair_proof)):
        if not proof:
            continue
        status = str(proof.get("status") or "")
        if status != "equivalent":
            if status in ("checker_unavailable", "no_log", "unknown", ""):
                unverifiable = True
            fail(R_PROOF, f"{label} equivalence is {status or 'unknown'}")

    # ── 必要品質項 ──
    quality: dict[str, Any] = {}
    index = {}
    for entry in metric_comparisons or ():
        row = entry if isinstance(entry, dict) else entry.to_json_dict()
        if row.get("metric_id"):
            index[str(row["metric_id"])] = row
    for metric_id in REQUIRED_QUALITY_METRICS:
        row = index.get(metric_id)
        if row is None:
            missing(f"required quality metric {metric_id} is absent")
            continue
        quality[metric_id] = {
            "baseline": row.get("baseline_value"),
            "candidate": row.get("candidate_value"),
            "comparison": row.get("comparison"),
            "baseline_status": row.get("baseline_status"),
            "candidate_status": row.get("candidate_status"),
        }
        if row.get("comparison") == "degraded":
            fail(R_QUALITY, f"{metric_id} regressed against the baseline")
        elif row.get("comparison") == "not_comparable":
            unverifiable = True
            fail(R_QUALITY, f"{metric_id} could not be compared: {row.get('reason')}")
        try:
            if float(row.get("candidate_value")) != 0.0:
                fail(R_QUALITY,
                     f"{metric_id} is {row.get('candidate_value')} on the "
                     "candidate; this round requires it to stay at 0")
        except (TypeError, ValueError):
            missing(f"{metric_id} has no numeric candidate value")

    # ── core 面積不得增大 ──
    core: dict[str, Any] = {}
    try:
        base_area = float((baseline_core or {})["core_area_um2"])
        cand_area = float((candidate_core or {})["core_area_um2"])
        core = {"baseline_core_area_um2": base_area,
                "candidate_core_area_um2": cand_area,
                "delta_um2": cand_area - base_area}
        # 用實際 DB 邊界的數值比較；只有顯示精度差不能當變更容許量。
        if cand_area > base_area:
            fail(R_AREA,
                 f"the candidate core area {cand_area} exceeds the baseline "
                 f"{base_area}")
    except (KeyError, TypeError, ValueError):
        missing("the core area evidence is missing or not numeric")

    proofs = {
        "pre_post_eco": dict(eco_proof or {}),
        "baseline_candidate_final": dict(final_pair_proof or {}),
    }

    # ── 目標 setup 餘量判定 ──
    extracted_slack: float | None = actual_slack_ns
    if extracted_slack is None and metric_comparisons:
        for entry in metric_comparisons:
            row = (entry if isinstance(entry, dict)
                   else (entry.to_json_dict() if hasattr(entry, "to_json_dict") else {}))
            if row.get("metric_id") in ("orfs_critical_path_slack", "critical_path_slack"):
                val = row.get("candidate_value")
                try:
                    if val is not None:
                        extracted_slack = float(val)
                        break
                except (TypeError, ValueError):
                    pass
    # final group WNS 來源：pinned `setup_evidence.tcl`（final stage；每個受約束
    # endpoint 的 worst max path；逐 target clock group 取最小值；`%.17g` 未截斷；
    # 工具 UI 單位）。正式流程的 DQE policy 為 v1，不含 critical path slack，
    # 因此這是宣告 target 時實際使用的 worst setup slack。
    slack_unusable_reason: str | None = None
    if extracted_slack is None and cand_groups:
        if target_slack_ns is not None:
            slack_unusable_reason = _group_slack_unusable_reason(
                candidate_setup, cand_groups, coverage_reduced)
        if slack_unusable_reason is None:
            try:
                extracted_slack = min(g.wns for g in cand_groups)
            except Exception:
                pass

    target_slack_met: bool | None = None
    target_slack_unmet = False
    if target_slack_ns is not None:
        if extracted_slack is None:
            missing("target_slack_ns was requested but candidate slack evidence is unavailable"
                    + (f" ({slack_unusable_reason})" if slack_unusable_reason else ""))
            target_slack_met = False
        else:
            target_slack_met = (extracted_slack >= target_slack_ns)
            if not target_slack_met:
                target_slack_unmet = True

    if codes:
        status = UNVERIFIABLE if unverifiable else NOT_REPAIRED
        if target_slack_unmet:
            codes.append(R_TARGET_SLACK_NOT_MET)
            reasons.append(
                f"target slack threshold {target_slack_ns:.6f} ns not met (actual={extracted_slack:.6f} ns)"
            )
        return RepairVerdict(
            policy_id=POLICY_ID, policy_version=POLICY_VERSION,
            repair_status=status, reason_codes=tuple(codes),
            reasons=tuple(reasons), baseline_groups=base_groups,
            candidate_groups=cand_groups, eco_diff=dict(eco_diff or {}),
            proofs=proofs, quality=quality, core_area=core,
            target_slack_ns=target_slack_ns, actual_slack_ns=extracted_slack,
            target_slack_met=target_slack_met, timing_repaired=timing_repaired)

    if target_slack_unmet:
        # 硬約束全部通過、timing 已修復（零違規），但指定目標餘量尚未達成。
        # 維持 REPAIRED 以保留修復網表並標記 timing_repaired=True，
        # 但輸出 target_slack_met=False 與 target_slack_not_met reason_code，
        # 供上層 Global / continuation / reader 明確識別目標未達標，不當作 criteria_met。
        return RepairVerdict(
            policy_id=POLICY_ID, policy_version=POLICY_VERSION,
            repair_status=REPAIRED,
            reason_codes=(R_TARGET_SLACK_NOT_MET,),
            reasons=(f"timing repaired (0 setup violations) but target slack {target_slack_ns:.6f} ns not met (actual={extracted_slack:.6f} ns)",),
            baseline_groups=base_groups, candidate_groups=cand_groups,
            eco_diff=dict(eco_diff or {}), proofs=proofs, quality=quality,
            core_area=core,
            target_slack_ns=target_slack_ns, actual_slack_ns=extracted_slack,
            target_slack_met=target_slack_met, timing_repaired=timing_repaired)

    return RepairVerdict(
        policy_id=POLICY_ID, policy_version=POLICY_VERSION,
        repair_status=REPAIRED, reason_codes=(R_OK,),
        reasons=("every fixed acceptance condition is satisfied",),
        baseline_groups=base_groups, candidate_groups=cand_groups,
        eco_diff=dict(eco_diff or {}), proofs=proofs, quality=quality,
        core_area=core,
        target_slack_ns=target_slack_ns, actual_slack_ns=extracted_slack,
        target_slack_met=target_slack_met, timing_repaired=timing_repaired)


__all__ = [
    "POLICY_ID", "POLICY_VERSION", "REPAIRED", "NOT_REPAIRED", "UNVERIFIABLE",
    "REQUIRED_QUALITY_METRICS", "GroupVerdict", "RepairVerdict",
    "evaluate_repair",
    "R_AREA", "R_COVERAGE", "R_EVIDENCE_MISSING", "R_EXECUTION", "R_INPUTS",
    "R_NO_NETLIST_CHANGE", "R_OK", "R_PROBLEM_ABSENT", "R_PROOF", "R_QUALITY",
    "R_STILL_VIOLATING", "R_TARGET_SLACK_NOT_MET",
]
