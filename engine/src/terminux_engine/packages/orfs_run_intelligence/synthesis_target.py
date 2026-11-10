# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""合成 delay target 的**實收與實際消費**判定。

這不是「多一個報告欄位」。它回答一個會讓派工停止的問題：這一次 synthesis
真的用到了原 SDC 要求換算出來的 delay 目標嗎？

判定分三層，任何一層不成立都不算 consumed：

1. **實收**：Yosys 的 `abc` pass 有沒有拿到 `-D <value>`，值是多少。
2. **單位**：那個值換算後是否等於 SDC 週期對應的 ps。
3. **消費**：pinned 的 `abc` pass 只在 script 字串含 `{D}` 時代入
   `delay_target`（見 `tools/yosys/passes/techmap/abc.cc:1083-1088` 與它自己
   的 help）。ORFS 一律傳 `-script <檔案>`，字串是 `source <檔案>`，因此
   **只有那個 script 檔裡真的有 `{D}` 才可能被消費**。

第 3 層是刻意存在的：只看到 `-D 250` 就宣告「已使用 250 ps」是錯的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: `abc` pass 的呼叫行。`-script` 之後是 script 檔路徑。
_ABC_CALL = re.compile(r"\babc(?:_new)?\s+(?P<args>-[^\n]*)")
_D_OPTION = re.compile(r"(?:^|\s)-D\s+(?P<value>\S+)")
_SCRIPT_OPTION = re.compile(r"(?:^|\s)-script\s+(?P<path>\S+)")

NOT_RECEIVED = "target_not_received"
UNIT_MISMATCH = "target_unit_mismatch"
NOT_CONSUMED = "target_not_consumed_by_script"
UNKNOWN = "target_evidence_unknown"
OK = "target_consumed"


@dataclass(frozen=True)
class SynthesisTargetEvidence:
    """一個 slot 的合成 delay target 事實。"""

    slot: str
    status: str
    reason: str
    #: `abc` pass 實際收到的 `-D` 值（原字串，不改寫）。
    received_value: str | None = None
    expected_ps: str | None = None
    unit_consistent: bool | None = None
    script_path: str | None = None
    #: 那支 script 裡 `{D}` 出現幾次。0 表示 target 不可能被消費。
    script_placeholder_count: int | None = None
    consumed: bool = False
    evidence_files: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot, "status": self.status, "reason": self.reason,
            "received_value": self.received_value,
            "expected_ps": self.expected_ps,
            "unit_consistent": self.unit_consistent,
            "script_path": self.script_path,
            "script_placeholder_count": self.script_placeholder_count,
            "consumed": self.consumed,
            "evidence_files": list(self.evidence_files),
            "notes": list(self.notes),
        }


def expected_ps_from_sdc_period(period: str, *, unit: str) -> str | None:
    """SDC 週期 → ps。`unit` 必須是已驗證的時間單位，缺就回 `None`。

    用整數／精確十進位運算，避免漏乘或二次換算成 250000。
    """
    factors = {"ps": 1, "ns": 1000, "us": 1_000_000, "s": 1_000_000_000_000}
    factor = factors.get((unit or "").strip().lower())
    if factor is None:
        return None
    try:
        from decimal import Decimal

        value = Decimal(str(period).strip()) * factor
    except Exception:  # noqa: BLE001 - 解析不出來就是不知道
        return None
    if value != value.to_integral_value():
        return format(value.normalize(), "f")
    return str(int(value))


def read_synthesis_target_evidence(
    output_root: str | Path, *, platform: str, design: str, variant: str,
    slot: str, expected_ps: str | None,
    script_root: str | Path | None = None,
) -> SynthesisTargetEvidence:
    """由 synth log 與**實際被 source 的 script 檔**判定三層條件。"""
    root = Path(output_root)
    logs = root / "logs" / platform / design / variant
    files: list[str] = []
    call_args = None
    for name in ("1_2_yosys.log", "1_1_yosys_canonicalize.log", "1_synth.log"):
        path = logs / name
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        match = _ABC_CALL.search(text)
        if match is None:
            continue
        files.append(str(path))
        call_args = match.group("args")
        break
    if call_args is None:
        return SynthesisTargetEvidence(
            slot=slot, status=UNKNOWN,
            reason="no synth log records the abc invocation",
            expected_ps=expected_ps, evidence_files=tuple(files))

    d_match = _D_OPTION.search(call_args)
    script_match = _SCRIPT_OPTION.search(call_args)
    received = d_match.group("value") if d_match else None
    script_path = script_match.group("path") if script_match else None

    notes: list[str] = []
    placeholder_count: int | None = None
    if script_path is not None:
        # 容器內的路徑要映回 host 的 snapshot 才讀得到。
        # 容器內的路徑要映回 host 的 snapshot 才讀得到；但在沒有容器的
        # 環境裡那個路徑本身就是 host 路徑，所以先照字面試一次。
        literal = Path(script_path)
        local = literal
        if not literal.is_file() and script_root is not None:
            local = Path(script_root) / literal.name
        if local.is_file():
            placeholder_count = local.read_text(errors="replace").count("{D}")
            files.append(str(local))
        else:
            notes.append(f"the ABC script itself was not readable at {local}")

    if received is None:
        return SynthesisTargetEvidence(
            slot=slot, status=NOT_RECEIVED,
            reason="the abc pass was invoked without -D",
            expected_ps=expected_ps, script_path=script_path,
            script_placeholder_count=placeholder_count,
            evidence_files=tuple(files), notes=tuple(notes))

    unit_consistent = (expected_ps is not None
                       and str(received).strip() == str(expected_ps).strip())
    if placeholder_count is None:
        return SynthesisTargetEvidence(
            slot=slot, status=UNKNOWN,
            reason="cannot tell whether the ABC script consumes the target",
            received_value=received, expected_ps=expected_ps,
            unit_consistent=unit_consistent, script_path=script_path,
            evidence_files=tuple(files), notes=tuple(notes))
    if placeholder_count == 0:
        # pinned 的 `abc` pass 只代換 script 字串裡的 `{D}`；用 `-script <檔案>`
        # 時字串是 `source <檔案>`，所以這裡是 0 就代表**不可能**被消費。
        return SynthesisTargetEvidence(
            slot=slot, status=NOT_CONSUMED,
            reason=("the ABC script contains no {D} placeholder, so the pinned "
                    "abc pass cannot pass the delay target to it"),
            received_value=received, expected_ps=expected_ps,
            unit_consistent=unit_consistent, script_path=script_path,
            script_placeholder_count=0, evidence_files=tuple(files),
            notes=tuple(notes))
    if not unit_consistent:
        return SynthesisTargetEvidence(
            slot=slot, status=UNIT_MISMATCH,
            reason=(f"the abc pass received -D {received} but the approved "
                    f"target is {expected_ps} ps"),
            received_value=received, expected_ps=expected_ps,
            unit_consistent=False, script_path=script_path,
            script_placeholder_count=placeholder_count,
            evidence_files=tuple(files), notes=tuple(notes))
    return SynthesisTargetEvidence(
        slot=slot, status=OK,
        reason=(f"the abc pass received -D {received} ps and the script "
                f"substitutes it {placeholder_count} time(s)"),
        received_value=received, expected_ps=expected_ps, unit_consistent=True,
        script_path=script_path, script_placeholder_count=placeholder_count,
        consumed=True, evidence_files=tuple(files), notes=tuple(notes))


__all__ = ["NOT_CONSUMED", "NOT_RECEIVED", "OK", "UNIT_MISMATCH", "UNKNOWN",
           "SynthesisTargetEvidence", "expected_ps_from_sdc_period",
           "read_synthesis_target_evidence"]
