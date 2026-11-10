# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`gcd.prefix_borrow_miter_acceptance.v1`。

與 v0.8 的 `gcd.prefix_borrow_acceptance.v1` **每一條工程條件完全相同**——
問題存在、真實 netlist 變化、兩組等價、全部 setup 歸零、品質零違規、
exact geometry、core area 不增大、RTL 來源證據。一條都沒有放寬。

**唯一的差別**：兩組 mapped pair 的 proof 必須出自固定的
`yosys_transition_miter.v1`，並且該 proof 自身必須是 `equivalent`、
canonical state bit 數 > 0、且 state 真的是 miter 的自由輸入。

會需要這一條，是因為前兩個 mapped checker 都曾以「看起來像 PASS」的輸出通過：
v0.9 的舊 `lec` 把同一 flop 的 Q／QN 當兩個自由輸入，v0.10 的 `sec` 對六類植入
錯誤全部錯判 equivalent。所以這裡**不只看 status，還看 proof 的方法身分與它是否
真的涵蓋所有 state**。
"""

from __future__ import annotations

from typing import Any

from terminux_engine.packages.orfs_run_intelligence.prefix_borrow_acceptance import (
    PrefixBorrowVerdict, evaluate_prefix_borrow,
)
from terminux_engine.packages.orfs_run_intelligence.proof_tool_identity import (
    ProofToolIdentity, evaluate as evaluate_tool_identity,
)
from terminux_engine.packages.orfs_run_intelligence.transition_miter_proof import (
    ENVELOPE_SCHEMA_VERSION, EQUIVALENT, LEGACY_ENVELOPE_SCHEMA_VERSION, METHOD,
)

POLICY_ID = "gcd.prefix_borrow_miter_acceptance.v1"
POLICY_VERSION = "v1"
#: profile 綁定的固定 proof policy 身分。
PROOF_POLICY_ID = "yosys_transition_miter.v1"

#: 本 policy 才有的 reason code。
R_PROOF_METHOD = "mapped_proof_method_not_established"
#: 工具身分不成立。與方法身分分開記，因為病因不同——
#: 一個是「用錯方法」，一個是「用了未經 qualification 的 binary」。
R_PROOF_TOOL_IDENTITY = "mapped_proof_tool_identity_not_established"

NOT_REPAIRED = "NOT_REPAIRED"
REPAIRED = "REPAIRED"
UNVERIFIABLE = "UNVERIFIABLE"


def _tool_identity_failures(label: str, proof: dict[str, Any]) -> list[str]:
    """schema v2 起，工具身分是必要條件。

    舊 envelope（沒有 `envelope_schema_version`，或為 1）維持原判定，不回頭
    套新規則——setup repair 的歷史結論不因此改變。
    """
    try:
        schema = int(proof.get("envelope_schema_version") or
                     LEGACY_ENVELOPE_SCHEMA_VERSION)
    except (TypeError, ValueError):
        return [f"the {label} pair proof has an unreadable "
                f"envelope_schema_version "
                f"{proof.get('envelope_schema_version')!r}"]
    if schema < ENVELOPE_SCHEMA_VERSION:
        return []
    if schema > ENVELOPE_SCHEMA_VERSION:
        # 未知的**較新** schema 不做寬鬆預設：本 policy 不知道它的規則。
        return [f"the {label} pair proof uses envelope schema {schema}, which this "
                f"policy version does not know how to check"]
    raw = proof.get("proof_tool_identity")
    qualified_raw = proof.get("qualified_tool_identity")
    identity = (ProofToolIdentity.from_json_dict(raw)
                if isinstance(raw, dict) else None)
    qualified = (ProofToolIdentity.from_json_dict(qualified_raw)
                 if isinstance(qualified_raw, dict) else None)
    accepted, code, detail = evaluate_tool_identity(identity, qualified)
    if accepted:
        return []
    return [f"the {label} pair proof tool identity is not established ({code}: "
            f"{', '.join(detail) or 'no detail'}); a different binary inside the "
            f"same container image is not accepted"]


def _proof_method_failures(proofs: dict[str, Any]) -> list[str]:
    """檢查兩組 pair 的 proof 是否真的出自固定方法且涵蓋所有 state。"""
    problems: list[str] = []
    # key 名沿 v0.8 verdict 的 `proofs` 欄位，不另立一套。
    for label in ("baseline_candidate_post_resize", "baseline_candidate_final"):
        proof = proofs.get(label)
        if not proof:
            problems.append(f"the {label} pair proof is missing")
            continue
        method = proof.get("method")
        if method not in (PROOF_POLICY_ID, METHOD):
            problems.append(
                f"the {label} pair proof was produced by {method!r}, not the "
                f"approved {PROOF_POLICY_ID!r}")
            continue
        if str(proof.get("status") or "") != EQUIVALENT:
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
        problems.extend(_tool_identity_failures(label, proof))
    return problems


def evaluate_prefix_borrow_miter(**kwargs: Any) -> PrefixBorrowVerdict:
    """先跑 v0.8 的全部條件，再加上 proof 方法身分這一關。

    方法身分不成立時一律 `UNVERIFIABLE`——**不因為 timing 好就放行**，也不從
    「status 寫著 equivalent」推論方法正確。
    """
    verdict = evaluate_prefix_borrow(**kwargs)
    payload = verdict.to_json_dict()
    problems = _proof_method_failures(payload.get("proofs") or {})

    codes = list(payload["reason_codes"])
    reasons = list(payload["reasons"])
    status = payload["repair_status"]
    if problems:
        if R_PROOF_METHOD not in codes:
            codes.append(R_PROOF_METHOD)
        reasons.extend(problems)
        status = UNVERIFIABLE
    elif status == REPAIRED:
        codes, reasons = ["ok"], [
            "all fixed prefix-borrow acceptance conditions hold and both mapped "
            f"pairs are proven by {PROOF_POLICY_ID}"]

    return PrefixBorrowVerdict(
        policy_id=POLICY_ID, policy_version=POLICY_VERSION,
        repair_status=status, reason_codes=tuple(codes), reasons=tuple(reasons),
        baseline_groups=verdict.baseline_groups,
        candidate_groups=verdict.candidate_groups,
        netlist_change=verdict.netlist_change, proofs=verdict.proofs,
        quality=verdict.quality, core_area=verdict.core_area,
        geometry=verdict.geometry, rtl_source=verdict.rtl_source,
        synthesis_target=verdict.synthesis_target,
        statement=("本次固定 RTL 算術結構修改的判定；mapped pair 的等價由 "
                   "canonical transition miter 證明。不是多 corner signoff，"
                   "也不宣稱整體 timing 已完成"))


__all__ = ["NOT_REPAIRED", "POLICY_ID", "PROOF_POLICY_ID", "REPAIRED",
           "R_PROOF_METHOD", "UNVERIFIABLE", "evaluate_prefix_borrow_miter"]
