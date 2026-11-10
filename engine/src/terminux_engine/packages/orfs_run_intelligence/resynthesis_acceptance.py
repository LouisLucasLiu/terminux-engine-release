# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""速度導向重合成的固定驗收判定。

`gcd.setup_resynthesis_acceptance.v1`。與 `gcd.setup_repair_acceptance.v1`
共用「setup 全零／品質不退／core 不增大／必要證據齊全」那一套，但三處不同：

1. 沒有 ECO diff——真實 netlist 變化改由**兩側 post-resize 拓樸**證明。
2. 兩組等價換成本版指定的 pair（post-resize 對 post-resize、final 對 final）。
3. 多兩項本版才有的必要條件：兩側實際消費的 ABC 策略必須不同，且
   candidate 必須沿用 baseline 的整數 DBU 邊界。

一樣 deterministic：只讀正式 flow 寫出的證據與既有 DQE 比較，不讀模型文字。
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

POLICY_ID = "gcd.setup_resynthesis_acceptance.v1"
POLICY_VERSION = "v1"

#: 本版才有的 reason code。
R_STRATEGY = "synthesis_strategy_not_consumed"
R_GEOMETRY = "physical_bounds_not_identical"
#: 合成 delay target 的方法符合性。**先驗它，再判 timing。**
R_TARGET = "synthesis_delay_target_not_established"


@dataclass(frozen=True)
class ResynthesisVerdict:
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
    strategy: dict[str, Any] = field(default_factory=dict)
    synthesis_target: dict[str, Any] = field(default_factory=dict)
    statement: str = ("本次固定要求下的速度導向重合成判定；不是多 corner "
                      "signoff，也不宣稱整體 timing 已完成")

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
            "strategy": dict(self.strategy),
            "synthesis_target": dict(self.synthesis_target),
            "statement": self.statement,
        }


def _topology_change(baseline: Any, candidate: Any) -> dict[str, Any]:
    """兩側 post-resize 拓樸的實際差異。只看 cell／connectivity。"""
    if not baseline or not candidate:
        return {}
    out: dict[str, Any] = {}
    for key in ("instances", "nets", "iterms"):
        try:
            b, c = int(baseline[key]), int(candidate[key])
        except (KeyError, TypeError, ValueError):
            return {}
        out[f"{key}_baseline"] = b
        out[f"{key}_candidate"] = c
        out[f"{key}_delta"] = c - b
    bm = baseline.get("masters") or {}
    cm = candidate.get("masters") or {}
    changed = []
    for master in sorted(set(bm) | set(cm)):
        if bm.get(master, 0) != cm.get(master, 0):
            changed.append(f"{master}:{bm.get(master, 0)}->{cm.get(master, 0)}")
    out["changed_masters"] = changed
    out["has_real_change"] = bool(
        changed or any(out[f"{k}_delta"] for k in ("instances", "nets", "iterms")))
    return out


def _box(payload: Any, key: str) -> dict[str, int] | None:
    box = (payload or {}).get(key)
    if not isinstance(box, dict):
        return None
    try:
        return {c: int(box[c]) for c in ("llx", "lly", "urx", "ury")}
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_resynthesis(
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
    strategy_evidence: Any,
    target_evidence: Any = None,
    execution_ok: bool,
    inputs_approved: bool,
    inputs_reason: str | None = None,
) -> ResynthesisVerdict:
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

    base_groups = _groups(baseline_setup)
    cand_groups = _groups(candidate_setup)

    base_violations = sum(g.violating_endpoints for g in base_groups)
    if base_groups and base_violations == 0:
        fail(R_PROBLEM_ABSENT,
             "the new baseline already has no setup violation, so this "
             "resynthesis cannot be said to have repaired one")

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

    # ── 真實 netlist 變化：兩側 post-resize 拓樸的實際差異 ──
    change = _topology_change(baseline_post_resize_topology,
                             candidate_post_resize_topology)
    if change and not change.get("has_real_change"):
        fail(R_NO_NETLIST_CHANGE,
             "the two runs produced the same post-resize cell and connectivity "
             "topology; changing the config alone is not a repair")

    # ── 兩組指定 pair 的等價 ──
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

    # ── 實際消費的合成策略必須不同 ──
    strategy = dict(strategy_evidence or {})
    base_script = strategy.get("baseline_abc_script")
    cand_script = strategy.get("candidate_abc_script")
    if not base_script or not cand_script:
        missing("the consumed ABC strategy is not recorded for both slots")
    elif base_script == cand_script:
        fail(R_STRATEGY,
             f"both runs used the same ABC script ({base_script}); the "
             "candidate did not actually take the speed mapping")

    # ── 合成 delay target 的方法符合性──
    #
    # 這一項在 timing 之前判。只有「實收值等於核准目標、而且那支 script 真的
    # 會消費它」才算成立；只看到 `-D 250` 不夠。無法建立 → UNVERIFIABLE，
    # 不能因為後面 timing／proof 都 PASS 就放行。
    target = dict(target_evidence or {})
    if not target:
        missing("the synthesis delay target evidence is missing for both slots")
    else:
        for slot in ("baseline", "candidate"):
            row = target.get(slot) or {}
            if not row:
                missing(f"no synthesis delay target evidence for the {slot} run")
                continue
            if slot == "candidate" and not row.get("consumed"):
                unverifiable = True
                fail(R_TARGET,
                     f"the candidate speed mapping did not consume the approved "
                     f"delay target: {row.get('reason')}")
            elif slot == "baseline" and not row.get("consumed"):
                # area 分支不消費 target 是可證實的分支行為，如實列出；
                # 它**不能**替 candidate 豁免。
                target.setdefault("notes", []).append(
                    "not_consumed_by_area_branch")

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

    # ── 實體邊界必須完全相同（整數 DBU，不看 micron 顯示值）──
    geometry: dict[str, Any] = {}
    base_core_box, cand_core_box = _box(baseline_core, "core_dbu"), _box(candidate_core, "core_dbu")
    base_die_box, cand_die_box = _box(baseline_core, "die_dbu"), _box(candidate_core, "die_dbu")
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
    common = dict(
        baseline_groups=base_groups, candidate_groups=cand_groups,
        netlist_change=change, proofs=proofs, quality=quality,
        core_area=core, geometry=geometry, strategy=strategy,
        synthesis_target=target)
    if codes:
        return ResynthesisVerdict(
            policy_id=POLICY_ID, policy_version=POLICY_VERSION,
            repair_status=UNVERIFIABLE if unverifiable else NOT_REPAIRED,
            reason_codes=tuple(codes), reasons=tuple(reasons), **common)
    return ResynthesisVerdict(
        policy_id=POLICY_ID, policy_version=POLICY_VERSION,
        repair_status=REPAIRED, reason_codes=(R_OK,),
        reasons=("all fixed resynthesis acceptance conditions hold",), **common)


__all__ = ["POLICY_ID", "R_GEOMETRY", "R_STRATEGY", "ResynthesisVerdict",
           "evaluate_resynthesis"]
