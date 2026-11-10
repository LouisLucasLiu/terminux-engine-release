# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""固定 RTL patch（prefix-borrow）的 deterministic 修復判定。

`gcd.prefix_borrow_acceptance.v1`。與 v5 的差別只有兩點：

* **方法符合性**改成「合成實際讀取了核准 RTL」（三層鏈，見
  `rtl_source_evidence.py`），而不是「候選換了 ABC script」。
* mapper delay target **不是**本 recipe 的門檻，結果明列
  `synthesis_delay_target_requirement=NOT_APPLICABLE_TO_THIS_RECIPE`；
  但原本「target 未被使用」的觀察照實保留，不刪、不改寫。

其餘每一條工程條件（問題存在、真實 netlist 變化、兩組等價、全部 setup 歸零、
品質零違規、exact geometry、core area 不增大）與 v5 完全相同——**沒有一條被
放寬**。這一點是刻意的：換方法不等於降門檻。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from terminux_engine.packages.orfs_run_intelligence.repair_acceptance import (
    NOT_REPAIRED, R_AREA, R_COVERAGE, R_EVIDENCE_MISSING, R_EXECUTION, R_INPUTS,
    R_NO_NETLIST_CHANGE, R_OK, R_PROBLEM_ABSENT, R_PROOF, R_QUALITY,
    R_STILL_VIOLATING, REPAIRED, REQUIRED_QUALITY_METRICS, UNVERIFIABLE,
    GroupVerdict, _groups,
)
from terminux_engine.packages.orfs_run_intelligence.resynthesis_acceptance import (
    R_GEOMETRY, _box, _topology_change,
)

POLICY_ID = "gcd.prefix_borrow_acceptance.v1"
POLICY_VERSION = "v1"

#: 本 recipe 才有的 reason code：候選實際合成的不是核准的 RTL。
R_RTL = "approved_rtl_not_synthesized"

#: 本 recipe 對 mapper delay target 的固定立場。由 template／recipe 決定，
#: **不是**遇到 target=false 才自動切換。
TARGET_REQUIREMENT = "NOT_APPLICABLE_TO_THIS_RECIPE"


@dataclass(frozen=True)
class PrefixBorrowVerdict:
    policy_id: str
    policy_version: str
    repair_status: str
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    baseline_groups: tuple[GroupVerdict, ...] = ()
    candidate_groups: tuple[GroupVerdict, ...] = ()
    netlist_change: dict[str, Any] = field(default_factory=dict)
    proofs: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    core_area: dict[str, Any] = field(default_factory=dict)
    geometry: dict[str, Any] = field(default_factory=dict)
    rtl_source: dict[str, Any] = field(default_factory=dict)
    synthesis_target: dict[str, Any] = field(default_factory=dict)
    synthesis_delay_target_requirement: str = TARGET_REQUIREMENT
    statement: str = ("本次固定 RTL 算術結構修改的判定；不是多 corner signoff，"
                      "也不宣稱整體 timing 已完成")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id, "policy_version": self.policy_version,
            "repair_status": self.repair_status,
            "reason_codes": list(self.reason_codes), "reasons": list(self.reasons),
            "baseline_groups": [g.to_json_dict() for g in self.baseline_groups],
            "candidate_groups": [g.to_json_dict() for g in self.candidate_groups],
            "netlist_change": dict(self.netlist_change),
            "proofs": dict(self.proofs), "quality": dict(self.quality),
            "core_area": dict(self.core_area), "geometry": dict(self.geometry),
            "rtl_source": dict(self.rtl_source),
            "synthesis_target": dict(self.synthesis_target),
            "synthesis_delay_target_requirement":
                self.synthesis_delay_target_requirement,
            "statement": self.statement,
        }


def evaluate_prefix_borrow(
    *,
    baseline_setup: Any,
    candidate_setup: Any,
    baseline_core: Any,
    candidate_core: Any,
    baseline_post_resize_topology: Any,
    candidate_post_resize_topology: Any,
    post_resize_pair_proof: Any,
    final_pair_proof: Any,
    metric_comparisons: Any,
    rtl_evidence: Any,
    target_evidence: Any = None,
    execution_ok: bool,
    inputs_approved: bool,
    inputs_reason: str | None = None,
) -> PrefixBorrowVerdict:
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

    if not execution_ok:
        fail(R_EXECUTION, "at least one of the two runs did not complete")
    if not inputs_approved:
        fail(R_INPUTS, inputs_reason or "the input identity or diff is not approved")

    for label, payload in (
            ("baseline setup evidence", baseline_setup),
            ("candidate setup evidence", candidate_setup),
            ("baseline post-resize topology", baseline_post_resize_topology),
            ("candidate post-resize topology", candidate_post_resize_topology),
            ("post-resize equivalence proof", post_resize_pair_proof),
            ("final equivalence proof", final_pair_proof)):
        if not payload:
            missing(f"{label} is missing")

    # ── 方法符合性：合成實際讀取核准 RTL。**在 timing 之前判。** ──
    rtl = dict(rtl_evidence or {})
    if not rtl:
        missing("the RTL source evidence is missing for both slots")
    else:
        for slot in ("baseline", "candidate"):
            row = rtl.get(slot) or {}
            if not row:
                missing(f"no RTL source evidence for the {slot} run")
                continue
            if not row.get("established"):
                unverifiable = True
                fail(R_RTL,
                     f"the {slot} run did not establish that it synthesized the "
                     f"approved RTL: {row.get('reason')}")

    base_groups = _groups(baseline_setup)
    cand_groups = _groups(candidate_setup)

    base_violations = sum(g.violating_endpoints for g in base_groups)
    if base_groups and base_violations == 0:
        fail(R_PROBLEM_ABSENT,
             "the new baseline already has no setup violation, so this RTL "
             "change cannot be said to have repaired one")

    if base_groups and cand_groups:
        base_by = {g.group: g for g in base_groups}
        cand_by = {g.group: g for g in cand_groups}
        for name, g in base_by.items():
            other = cand_by.get(name)
            if other is None:
                fail(R_COVERAGE, f"clock group {name!r} disappeared on the candidate")
            elif other.analysed_endpoints < g.analysed_endpoints:
                fail(R_COVERAGE,
                     f"clock group {name!r} analysed {other.analysed_endpoints} "
                     f"endpoints on the candidate vs {g.analysed_endpoints} on "
                     "the baseline")
        for payload, side in ((baseline_setup, "baseline"),
                              (candidate_setup, "candidate")):
            if payload and payload.get("counts_agree") is False:
                fail(R_COVERAGE,
                     f"the {side} enumerated violation count disagrees with the "
                     "global count")

    if cand_groups:
        for g in cand_groups:
            if g.violating_endpoints != 0 or g.tns < 0.0 or g.wns < 0.0:
                fail(R_STILL_VIOLATING,
                     f"clock group {g.group!r} still has "
                     f"{g.violating_endpoints} violating endpoints "
                     f"(wns={g.wns!r}, tns={g.tns!r})")
    elif candidate_setup:
        missing("the candidate setup evidence lists no clock group")

    change = _topology_change(baseline_post_resize_topology,
                              candidate_post_resize_topology)
    if change and not change.get("has_real_change"):
        fail(R_NO_NETLIST_CHANGE,
             "the two runs produced the same post-resize cell and connectivity "
             "topology; synthesis mapped the new expression onto the same "
             "network, so this is no effective design change")

    for label, proof in (("baseline/candidate post-resize", post_resize_pair_proof),
                         ("baseline/candidate final", final_pair_proof)):
        if not proof:
            continue
        status = str(proof.get("status") or "")
        if status != "equivalent":
            if status in ("checker_unavailable", "no_log", "unknown",
                          "unverifiable", ""):
                unverifiable = True
            fail(R_PROOF, f"{label} equivalence is {status or 'unknown'}")

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

    geometry: dict[str, Any] = {}
    base_core_box = _box(baseline_core, "core_dbu")
    cand_core_box = _box(candidate_core, "core_dbu")
    base_die_box = _box(baseline_core, "die_dbu")
    cand_die_box = _box(candidate_core, "die_dbu")
    if not all((base_core_box, cand_core_box, base_die_box, cand_die_box)):
        missing("integer DBU core/die bounds are missing on at least one slot")
    else:
        geometry = {"baseline_core_dbu": base_core_box,
                    "candidate_core_dbu": cand_core_box,
                    "baseline_die_dbu": base_die_box,
                    "candidate_die_dbu": cand_die_box,
                    "identical": (base_core_box == cand_core_box
                                  and base_die_box == cand_die_box)}
        if not geometry["identical"]:
            fail(R_GEOMETRY,
                 "the candidate did not run on the same integer DBU core/die "
                 "bounds as the baseline")

    core: dict[str, Any] = {}
    try:
        base_area = float((baseline_core or {})["core_area_um2"])
        cand_area = float((candidate_core or {})["core_area_um2"])
        core = {"baseline_core_area_um2": base_area,
                "candidate_core_area_um2": cand_area,
                "delta_um2": cand_area - base_area}
        if cand_area > base_area:
            fail(R_AREA,
                 f"the candidate core area {cand_area} exceeds the baseline "
                 f"{base_area}")
    except (KeyError, TypeError, ValueError):
        missing("the core area evidence is missing or not numeric")

    proofs = {
        "baseline_candidate_post_resize": dict(post_resize_pair_proof or {}),
        "baseline_candidate_final": dict(final_pair_proof or {}),
    }
    # target 觀察照實保留，但**不參與**本 recipe 的通過與否。
    target = dict(target_evidence or {})
    common = dict(
        baseline_groups=base_groups, candidate_groups=cand_groups,
        netlist_change=change, proofs=proofs, quality=quality,
        core_area=core, geometry=geometry, rtl_source=rtl,
        synthesis_target=target)
    if codes:
        return PrefixBorrowVerdict(
            policy_id=POLICY_ID, policy_version=POLICY_VERSION,
            repair_status=UNVERIFIABLE if unverifiable else NOT_REPAIRED,
            reason_codes=tuple(codes), reasons=tuple(reasons), **common)
    return PrefixBorrowVerdict(
        policy_id=POLICY_ID, policy_version=POLICY_VERSION,
        repair_status=REPAIRED, reason_codes=(R_OK,),
        reasons=("all fixed prefix-borrow acceptance conditions hold",), **common)


__all__ = ["NOT_REPAIRED", "POLICY_ID", "REPAIRED", "R_RTL", "TARGET_REQUIREMENT",
           "UNVERIFIABLE", "PrefixBorrowVerdict", "evaluate_prefix_borrow"]
