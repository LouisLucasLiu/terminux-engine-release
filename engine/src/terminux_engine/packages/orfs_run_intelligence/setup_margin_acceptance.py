# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`aes.setup_slack_margin_acceptance.v1`。

與 `gcd.setup_repair_acceptance.v1` 的**每一條工程條件完全相同**——問題確實
存在、真實 netlist 變化、兩組等價、全部 setup 歸零、品質零違規、core 面積不
增大、input relation 成立。一條都沒有放寬。

本 policy 只多加兩關：

1. **proof 工具身分**：兩組 pair 的 proof 必須是 envelope schema v2，
   且 `proof_tool_identity` 與 qualification 身分指向**同一個 executable**。
   同一個 container image 內的另一個 binary 不接受。
2. **方法確實被消費**：`SETUP_SLACK_MARGIN` 必須真的進到 `repair_timing` 的
   argv，且該次呼叫確實做了工作。只有「log 裡出現字串」不算——還要有
   pinned source 的 env→argv 映射與兩側 netlist 的真實差異。

`REPAIRED` 只有在上述全部成立時才給出；方法沒被消費、或身分不成立，一律
`UNVERIFIABLE`，不因為 timing 數字好看就放行。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from terminux_engine.packages.orfs_run_intelligence.proof_tool_identity import (
    ProofToolIdentity, evaluate as evaluate_tool_identity,
)
from terminux_engine.packages.orfs_run_intelligence.repair_acceptance import (
    NOT_REPAIRED, REPAIRED, UNVERIFIABLE, RepairVerdict, evaluate_repair,
)
from terminux_engine.packages.orfs_run_intelligence.transition_miter_proof import (
    ENVELOPE_SCHEMA_VERSION, METHOD as MITER_METHOD,
)

POLICY_ID = "aes.setup_slack_margin_acceptance.v1"
POLICY_VERSION = "v1"
#: profile 綁定的固定 proof policy 身分。
PROOF_POLICY_ID = "yosys_transition_miter.v1"

#: 本 policy 才有的 reason code。
R_PROOF_METHOD = "mapped_proof_method_not_established"
R_PROOF_TOOL_IDENTITY = "mapped_proof_tool_identity_not_established"
R_METHOD_NOT_CONSUMED = "method_not_consumed_by_the_tool"

#: 兩組必須存在的 pair。
PAIR_LABELS: tuple[str, ...] = ("baseline_candidate_post_cts",
                                "baseline_candidate_final")


def _topology_change(baseline: Any, candidate: Any) -> dict[str, Any]:
    """由兩側 post-CTS 拓樸算出真實 netlist 變化。

    與 v0.5／v0.8 相同的做法：用工具寫出的原始計數，不讀顯示字串。
    """
    if not baseline or not candidate:
        return {}
    change: dict[str, Any] = {}
    for key in ("instances", "nets", "iterms"):
        try:
            base = int(baseline[key])
            cand = int(candidate[key])
        except (KeyError, TypeError, ValueError):
            return {}
        change[f"{key}_baseline"] = base
        change[f"{key}_candidate"] = cand
        change[f"{key[:-1] if key.endswith('s') else key}_delta"] = cand - base
    base_masters = dict(baseline.get("masters") or {})
    cand_masters = dict(candidate.get("masters") or {})
    changed = sorted(
        f"{name}:{base_masters.get(name, 0)}->{cand_masters.get(name, 0)}"
        for name in set(base_masters) | set(cand_masters)
        if base_masters.get(name, 0) != cand_masters.get(name, 0))
    change["changed_masters"] = changed
    change["has_real_change"] = bool(
        changed or any(v for k, v in change.items() if k.endswith("_delta")))
    # `evaluate_repair` 讀的是這三個鍵。
    change["instance_delta"] = change.get("instance_delta", 0)
    change["net_delta"] = change.get("net_delta", 0)
    change["iterm_delta"] = change.get("iterm_delta", 0)
    return change


def _proof_failures(proofs: dict[str, Any]) -> list[str]:
    """兩組 pair 的方法身分與工具身分。"""
    problems: list[str] = []
    for label in PAIR_LABELS:
        proof = proofs.get(label)
        if not proof:
            problems.append(f"the {label} pair proof is missing")
            continue
        method = proof.get("method")
        if method not in (PROOF_POLICY_ID, MITER_METHOD):
            problems.append(
                f"the {label} pair proof was produced by {method!r}, not the "
                f"approved {PROOF_POLICY_ID!r}")
            continue
        try:
            schema = int(proof.get("envelope_schema_version") or 1)
        except (TypeError, ValueError):
            problems.append(
                f"the {label} pair proof has an unreadable "
                f"envelope_schema_version")
            continue
        if schema != ENVELOPE_SCHEMA_VERSION:
            # parametric setup repair 是**新**的 proof：只接受 schema v2。舊 schema 在這裡
            # 不是相容，而是缺少工具身分。
            problems.append(
                f"the {label} pair proof uses envelope schema {schema}; this "
                f"round requires schema {ENVELOPE_SCHEMA_VERSION} with a "
                "proof tool identity")
            continue
        raw = proof.get("proof_tool_identity")
        qualified_raw = proof.get("qualified_tool_identity")
        identity = (ProofToolIdentity.from_json_dict(raw)
                    if isinstance(raw, dict) else None)
        qualified = (ProofToolIdentity.from_json_dict(qualified_raw)
                     if isinstance(qualified_raw, dict) else None)
        accepted, code, detail = evaluate_tool_identity(identity, qualified)
        if not accepted:
            problems.append(
                f"the {label} pair proof tool identity is not established "
                f"({code}: {', '.join(detail) or 'no detail'}); a different "
                "binary inside the same container image is not accepted")
            continue
        if str(proof.get("status") or "") != "equivalent":
            problems.append(
                f"the {label} pair proof status is "
                f"{proof.get('status') or 'unknown'!r}")
            continue
        try:
            bits = int(proof.get("state_bits") or 0)
        except (TypeError, ValueError):
            bits = 0
        if bits <= 0:
            problems.append(
                f"the {label} pair proof records no canonical state bits")
        free = [str(x) for x in (proof.get("miter_free_inputs") or [])]
        if not any(name.endswith("r5_S") for name in free):
            problems.append(
                f"the {label} pair proof does not show the canonical state bits "
                "as free miter inputs, so it does not cover all states")
    return problems


def _consumption_failures(consumption: Any, host_consumption: Any, *,
                          variable: str, expected_value: str) -> list[str]:
    """方法是否真的被工具消費。兩個來源都必須成立：

    1. **in-flow**：hook 在 `erase_non_stage_variables cts` **之後**讀到的
       env 值——那就是 `repair_timing` 當下實際持有的值；
    2. **host**：run 結束後解析完整 CTS log，確認 `repair_timing` 真的收到
       `-setup_margin <值>`。

    刻意不採用 hook 內對 log 的觀察：hook 跑在 CTS 中途，OpenROAD 的 log 還
    沒 flush，那個布林恆為假（parametric setup repair attempt8 就是這樣被誤判）。

    值一律用**數值**比較：Make／Tcl 會把 `0.10` 正規化成 `0.1`，用字串比會
    在語義相同時誤判不符。
    """
    problems: list[str] = []
    if not consumption:
        problems.append(f"the in-flow {variable} consumption evidence is missing")
    else:
        observed = consumption.get("observed_value")
        try:
            same = float(observed) == float(expected_value)
        except (TypeError, ValueError):
            same = str(observed) == str(expected_value)
        if not same:
            problems.append(
                f"the tool held {variable}={observed!r} at the consumption "
                f"point but the approved candidate value is {expected_value!r}")
    if host_consumption is None:
        problems.append(
            f"the host-side {variable} consumption evidence is missing")
        return problems
    view = (host_consumption.to_json_dict()
            if hasattr(host_consumption, "to_json_dict") else dict(host_consumption))
    if not view.get("consumed"):
        problems.append(
            f"{variable} was not consumed by repair_timing: "
            f"{view.get('reason') or 'no reason recorded'}")
    return problems


def evaluate_setup_margin(
    *,
    baseline_post_cts_topology: Any,
    candidate_post_cts_topology: Any,
    post_cts_pair_proof: Any,
    method_consumption: Any,
    host_method_consumption: Any,
    method_variable: str,
    method_candidate_value: str,
    target_slack_ns: float | None = None,
    actual_slack_ns: float | None = None,
    **kwargs: Any,
) -> RepairVerdict:
    """先跑既有的全部工程條件，再加上 proof 身分與方法消費兩關。"""
    change = _topology_change(baseline_post_cts_topology,
                              candidate_post_cts_topology)
    verdict = evaluate_repair(
        eco_diff=change or None,
        eco_proof=post_cts_pair_proof,
        target_slack_ns=target_slack_ns,
        actual_slack_ns=actual_slack_ns,
        **kwargs)

    proofs = {
        "baseline_candidate_post_cts": dict(post_cts_pair_proof or {}),
        "baseline_candidate_final": dict(kwargs.get("final_pair_proof") or {}),
    }
    extra_codes: list[str] = []
    extra_reasons: list[str] = []

    proof_problems = _proof_failures(proofs)
    if proof_problems:
        extra_codes.append(
            R_PROOF_TOOL_IDENTITY if any("tool identity" in p
                                         for p in proof_problems)
            else R_PROOF_METHOD)
        extra_reasons.extend(proof_problems)

    consumption_problems = _consumption_failures(
        method_consumption, host_method_consumption, variable=method_variable,
        expected_value=method_candidate_value)
    if consumption_problems:
        extra_codes.append(R_METHOD_NOT_CONSUMED)
        extra_reasons.extend(consumption_problems)

    if not extra_codes:
        return replace(verdict, policy_id=POLICY_ID,
                       policy_version=POLICY_VERSION, proofs=proofs,
                       eco_diff=change)

    codes = tuple(dict.fromkeys(
        [c for c in verdict.reason_codes if c != "ok"] + extra_codes))
    reasons = tuple(
        [r for r in verdict.reasons
         if r != "every fixed acceptance condition is satisfied"]
        + extra_reasons)
    # 走到這裡代表 proof 身分或方法消費**至少有一項不成立**，也就是證據不足；
    # 一律 UNVERIFIABLE，不因為 timing 數字好看就放行，也不降級成
    # NOT_REPAIRED（那會把「證據不足」講成「修了但沒修好」）。
    return replace(verdict, policy_id=POLICY_ID, policy_version=POLICY_VERSION,
                   repair_status=UNVERIFIABLE, reason_codes=codes,
                   reasons=reasons, proofs=proofs, eco_diff=change)


__all__ = [
    "POLICY_ID", "POLICY_VERSION", "PROOF_POLICY_ID", "PAIR_LABELS",
    "REPAIRED", "NOT_REPAIRED", "UNVERIFIABLE",
    "R_PROOF_METHOD", "R_PROOF_TOOL_IDENTITY", "R_METHOD_NOT_CONSUMED",
    "evaluate_setup_margin",
]
