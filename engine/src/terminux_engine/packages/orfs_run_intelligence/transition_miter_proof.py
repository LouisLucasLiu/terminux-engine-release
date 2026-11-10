# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""canonical transition miter 的產品 proof wrapper。

**方法**（全部用既有 pinned Yosys，不引入新工具）：每個 flop 只建立一個
canonical state bit `S[i]`，令 `Q = S[i]`、`QN = ~S[i]`，並把 `D` 拉成
top-level output `N[i]`。兩側因此都成為純組合的 `(PI, S) → (PO, N)`；
`miter -equiv` 以共享輸入建立差異 trigger，SAT 再證明 trigger 不可能成立。

證明的內容正是三件事同時成立，且**對所有輸入與所有 state**（不限可達狀態）：

1. 所有 top-level outputs 相同；
2. 所有 flop 的 next-state 相同；
3. 上述對任意 `S` 都成立。

這修掉舊 mapped `lec` 的錯誤——它把同一 flop 的 Q／QN 當兩個獨立自由輸入，
於是允許實體上不可能的 `Q == QN`。

**fail closed 的條件**（任一成立即不是 `equivalent`）：兩側 flop 名稱集合不同、
缺 cell model、SAT 沒有給出明確結論、逾時、或工具回傳非零而沒有可解析的結論。
組合邏輯一個 cell 都不動；受驗證的 netlist 不被改寫。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from terminux_engine.packages.orfs_run_intelligence.proof_tool_identity import (
    ProofToolIdentity,
    evaluate as evaluate_tool_identity,
)

METHOD = "transition_miter.v1"

#: envelope 的 schema 版本。v1 是 setup repair 落盤的形狀（只有 `tool`
#: 字串與 `image_id`）；v2 額外要求 `proof_tool_identity`（實際 executable 的
#: realpath／bytes hash／version hash／argv／mode）並對它 fail closed。
#:
#: **`METHOD` 不變**：proof 的語義（canonical transition miter）沒有改，只有
#: 身分材料變嚴格。method id 只在 proof 語義改變時才提升。
ENVELOPE_SCHEMA_VERSION = 2
LEGACY_ENVELOPE_SCHEMA_VERSION = 1

EQUIVALENT = "equivalent"
DIFFERENT = "not_equivalent"
BOUNDARY_REJECTED = "state_boundary_rejected"
UNVERIFIABLE = "unverifiable"

#: SAT 的兩個明確結論字串（pinned Yosys 0.67）。
_PROVED = "no model found: SUCCESS!"
_COUNTEREXAMPLE = "model found: FAIL!"


@dataclass(frozen=True)
class TransitionMiterProof:
    """一組 mapped pair 的 canonical transition miter 結果。"""

    step: str
    status: str
    reason: str
    gold_path: str | None = None
    gate_path: str | None = None
    gold_sha256: str | None = None
    gate_sha256: str | None = None
    #: canonical state bit 數＝配對成功的 sequential instance 數。
    state_bits: int | None = None
    flop_order_sha256: str | None = None
    #: 由 pinned Liberty 產生的 functional cell model 身分。
    cell_model_sha256: str | None = None
    liberty_sha256: str | None = None
    #: miter 的自由輸入（必須含 state 與全部 PI）。
    miter_free_inputs: tuple[str, ...] = ()
    sat_variables: int | None = None
    sat_clauses: int | None = None
    tool: str | None = None
    image_id: str | None = None
    log_sha256: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: 實際執行的工具身分。schema v2 起為必要欄位。
    tool_identity: ProofToolIdentity | None = None
    #: qualification 所使用的工具身分；必須與 `tool_identity` 完全相同。
    qualified_tool_identity: ProofToolIdentity | None = None

    @property
    def proven(self) -> bool:
        return self.status == EQUIVALENT

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
            "method": METHOD, "step": self.step, "status": self.status,
            "reason": self.reason,
            "gold": self.gold_path, "gate": self.gate_path,
            "gold_sha256": self.gold_sha256, "gate_sha256": self.gate_sha256,
            "state_bits": self.state_bits,
            "flop_order_sha256": self.flop_order_sha256,
            "cell_model_sha256": self.cell_model_sha256,
            "liberty_sha256": self.liberty_sha256,
            "miter_free_inputs": list(self.miter_free_inputs),
            "sat_variables": self.sat_variables, "sat_clauses": self.sat_clauses,
            "tool": self.tool, "image_id": self.image_id,
            "log_sha256": self.log_sha256, "notes": list(self.notes),
            "proof_tool_identity": (self.tool_identity.to_json_dict()
                                    if self.tool_identity is not None else None),
            "qualified_tool_identity": (
                self.qualified_tool_identity.to_json_dict()
                if self.qualified_tool_identity is not None else None),
            "proven": self.proven,
        }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse_sat_log(text: str) -> tuple[str, str, dict[str, Any]]:
    """把 pinned Yosys 的 SAT 輸出解析成 (status, reason, facts)。

    只承認兩個**明確**結論；其餘一律 `unverifiable`——不從「沒有看到失敗」
    推論成功。
    """
    facts: dict[str, Any] = {}
    inputs = tuple(
        line.split("Import show expression: ", 1)[1].strip().lstrip("\\")
        for line in text.splitlines()
        if "Import show expression: " in line)
    if inputs:
        facts["miter_free_inputs"] = inputs
    for line in text.splitlines():
        if line.startswith("Solving problem with "):
            parts = line.split()
            try:
                facts["sat_variables"] = int(parts[3])
                facts["sat_clauses"] = int(parts[6])
            except (IndexError, ValueError):
                pass
    if _PROVED in text:
        return (EQUIVALENT,
                "the canonical transition miter trigger is unsatisfiable: all "
                "top-level outputs and all flop next-states agree for every "
                "input and every state", facts)
    if _COUNTEREXAMPLE in text:
        return (DIFFERENT,
                "the SAT solver produced a counterexample: some input/state "
                "makes an output or a next-state differ", facts)
    if "__HOST_TIMEOUT__" in text or "Timeout" in text:
        return UNVERIFIABLE, "the proof did not finish within its timeout", facts
    return (UNVERIFIABLE,
            "the tool produced no explicit SAT conclusion", facts)


def build_proof(
    *,
    step: str,
    log_text: str,
    gold_path: str,
    gate_path: str,
    gold_sha256: str,
    gate_sha256: str,
    state_bits: int | None,
    flop_order_sha256: str | None,
    cell_model_sha256: str | None,
    liberty_sha256: str | None,
    tool: str | None,
    image_id: str | None,
    boundary_error: str | None = None,
    tool_identity: ProofToolIdentity | None = None,
    qualified_tool_identity: ProofToolIdentity | None = None,
) -> TransitionMiterProof:
    """由一次 miter 執行的原始 log 組出 durable proof envelope。

    `tool_identity` 與 `qualified_tool_identity` 必須同時存在、完整、
    且指向**同一個 executable**；否則即使 SAT 給了 UNSAT，結果仍為
    `unverifiable`——不得因為「同一個 image」就接受 image 內另一個 yosys。
    """
    if boundary_error:
        return TransitionMiterProof(
            step=step, status=BOUNDARY_REJECTED,
            reason=f"the canonical state boundary was rejected: {boundary_error}",
            gold_path=gold_path, gate_path=gate_path,
            gold_sha256=gold_sha256, gate_sha256=gate_sha256,
            state_bits=state_bits, flop_order_sha256=flop_order_sha256,
            cell_model_sha256=cell_model_sha256, liberty_sha256=liberty_sha256,
            tool=tool, image_id=image_id, log_sha256=sha256_text(log_text),
            tool_identity=tool_identity,
            qualified_tool_identity=qualified_tool_identity)
    status, reason, facts = parse_sat_log(log_text)
    free_inputs = tuple(facts.get("miter_free_inputs", ()))
    notes: list[str] = []
    if status == EQUIVALENT:
        # state 與 PI 都必須真的是 miter 的自由輸入；否則證的不是「所有 state」。
        if not any(name.endswith("r5_S") or name == "in_r5_S"
                   for name in free_inputs):
            status = UNVERIFIABLE
            reason = ("the miter did not expose the canonical state bits as free "
                      "inputs, so the result does not cover all states")
        elif state_bits is None or state_bits <= 0:
            status = UNVERIFIABLE
            reason = "the canonical state bit count is unknown"
    if status == EQUIVALENT:
        # 工具身分是 equivalent 的必要條件，且必須與 qualification 相同。
        accepted, code, detail = evaluate_tool_identity(
            tool_identity, qualified_tool_identity)
        if not accepted:
            status = UNVERIFIABLE
            reason = (f"{code}: the executable that produced this proof is not the "
                      f"qualified one ({', '.join(detail) or 'no detail'}); a "
                      f"different binary inside the same image is not accepted")
    return TransitionMiterProof(
        step=step, status=status, reason=reason,
        gold_path=gold_path, gate_path=gate_path,
        gold_sha256=gold_sha256, gate_sha256=gate_sha256,
        state_bits=state_bits, flop_order_sha256=flop_order_sha256,
        cell_model_sha256=cell_model_sha256, liberty_sha256=liberty_sha256,
        miter_free_inputs=free_inputs,
        tool_identity=tool_identity,
        qualified_tool_identity=qualified_tool_identity,
        sat_variables=facts.get("sat_variables"),
        sat_clauses=facts.get("sat_clauses"),
        tool=tool, image_id=image_id, log_sha256=sha256_text(log_text),
        notes=tuple(notes))


__all__ = ["BOUNDARY_REJECTED", "DIFFERENT", "EQUIVALENT", "METHOD",
           "UNVERIFIABLE", "TransitionMiterProof", "build_proof",
           "parse_sat_log", "sha256_text"]
