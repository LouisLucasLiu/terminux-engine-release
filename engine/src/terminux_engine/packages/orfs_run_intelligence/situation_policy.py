# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ORFS run-level situation domain policy。

**只有這裡**知道：哪些 DQE check 算 blocker、metric 方向的工程意義、
readiness 怎麼判、以及哪些 report section 值得節錄。
`terminux_engine.situation` 完全不參與這些判斷。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POLICY_ID = "orfs_run_situation"
POLICY_VERSION = "v1"

READINESS_NOT_READY = "not_ready"
READINESS_INSUFFICIENT = "insufficient_evidence"
READINESS_CONDITIONAL = "conditionally_ready"

#: 這些 semantic status 代表「不知道」，不能拿來判 readiness。
_UNRESOLVED = frozenset({"unknown", "legacy_ambiguous"})


@dataclass(frozen=True)
class ReadinessDecision:
    status: str
    basis: tuple[str, ...]
    blockers: tuple[dict[str, Any], ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {"status": self.status, "basis": list(self.basis),
                "blockers": [dict(b) for b in self.blockers],
                "authoritative": False, "formal_signoff_gate": "not_evaluated",
                "policy_id": POLICY_ID, "policy_version": POLICY_VERSION}


def decide_readiness(
    *,
    current_rows: list[dict[str, Any]],
    required_sources_available: bool,
    missing_reasons: tuple[str, ...] = (),
) -> ReadinessDecision:
    """situation assessment readiness policy。

    優先序刻意是：**先看證據夠不夠，再看有沒有 fail**——證據不足時說
    `not_ready` 會過度斷言，說 `conditionally_ready` 則是危險的樂觀。
    """
    if not required_sources_available or not current_rows:
        return ReadinessDecision(
            READINESS_INSUFFICIENT,
            ("required DQE rows or artifact identity/verification is unavailable",)
            + missing_reasons,
            (),
        )

    blockers = tuple(
        {"check_id": r["DQE_ID"], "semantic_status": r["semantic_status"],
         "value": r.get("VALUE"), "unit": r.get("UNIT") or None}
        for r in current_rows if r.get("semantic_status") == "fail"
    )
    if blockers:
        return ReadinessDecision(
            READINESS_NOT_READY,
            (f"the current run has {len(blockers)} failing DQE check(s)",),
            blockers,
        )

    unresolved = [r["DQE_ID"] for r in current_rows
                  if r.get("semantic_status") in _UNRESOLVED]
    basis = ["the current run has no failing DQE check"]
    if unresolved:
        basis.append(
            f"{len(unresolved)} check(s) have an unresolved semantic status and are not "
            f"counted either way")
    return ReadinessDecision(READINESS_CONDITIONAL, tuple(basis), ())


#: check_id → 這個 check 在 situation 敘述中的角色。純 domain 知識。
CHECK_ROLE: dict[str, str] = {
    "orfs_setup_wns": "setup_timing",
    "orfs_setup_tns": "setup_timing",
    "orfs_setup_violation_count": "setup_timing",
    "orfs_hold_violation_count": "hold_timing",
    "orfs_max_slew_violation_count": "design_rule",
    "orfs_max_fanout_violation_count": "design_rule",
    "orfs_max_cap_violation_count": "design_rule",
    "orfs_route_drc_violation_count": "routing",
    "orfs_log_error_markers": "tool_log",
    "orfs_log_warning_markers": "tool_log",
    "orfs_process_exit_code": "execution",
    "orfs_manifest_integrity": "provenance",
    "orfs_artifact_completeness": "provenance",
    "orfs_final_deliverables": "provenance",
}

#: 這些 check 的 report section 值得放進 situation context。
SITUATION_SECTIONS: dict[str, tuple[str, str]] = {
    "orfs_setup_wns": ("orfs.finish_report", "finish report_wns"),
    "orfs_setup_tns": ("orfs.finish_report", "finish report_tns"),
    "orfs_setup_violation_count": ("orfs.finish_report", "finish setup_violation_count"),
    "orfs_hold_violation_count": ("orfs.finish_report", "finish hold_violation_count"),
    "orfs_route_drc_violation_count": ("orfs.route_drc_report", "violation type: markers"),
}


def describe_blocker(blocker: dict[str, Any]) -> str:
    unit = f" {blocker['unit']}" if blocker.get("unit") else ""
    value = blocker.get("value")
    role = CHECK_ROLE.get(blocker["check_id"], "quality")
    return (f"DQE check {blocker['check_id']} ({role}) is failing"
            + (f" with observed value {value}{unit}" if value not in (None, "") else "")
            + ".")


__all__ = [
    "CHECK_ROLE",
    "POLICY_ID",
    "POLICY_VERSION",
    "READINESS_CONDITIONAL",
    "READINESS_INSUFFICIENT",
    "READINESS_NOT_READY",
    "ReadinessDecision",
    "SITUATION_SECTIONS",
    "decide_readiness",
    "describe_blocker",
]
