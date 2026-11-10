# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""合成**實際讀取了核准 RTL** 的三層證據。

這不是「多一個報告欄位」。它回答一個會讓判定停止的問題：這一次 synthesis
真的把已核准的那份 RTL bytes 讀進去了嗎？

判定分三層，任何一層不成立都不算成立：

1. **路徑**：synth log 有沒有 `Executing Verilog-2005 frontend: <path>`，
   而且其中一筆的 basename 就是 recipe 宣告的那個 RTL。
2. **內容**：該 slot 私有 source 內那個檔案的 SHA-256，是否等於本 slot 應有的
   值（baseline=`before_sha256`，candidate=`after_sha256`）。
3. **範圍**：recipe 宣告的兩個 module 名是否真的出現在 elaborate 結果裡。

只看到「檔案 hash 對」不夠——那不證明這一次 run 讀了它；只看到 log 有讀檔也
不夠——那不證明讀到的是核准的 bytes。兩邊都要，加上 module 範圍。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: yosys 的 Verilog frontend 行。pinned 格式見真實 log。
_FRONTEND = re.compile(
    r"Executing Verilog-2005 frontend:\s*(?P<path>\S+)")

NOT_READ = "rtl_not_read_by_this_run"
CONTENT_MISMATCH = "rtl_content_mismatch"
MODULES_MISSING = "rtl_modules_not_elaborated"
UNKNOWN = "rtl_evidence_unknown"
OK = "rtl_source_established"


@dataclass(frozen=True)
class RtlSourceEvidence:
    """一個 slot 的 RTL 來源事實。"""

    slot: str
    status: str
    reason: str
    #: 這一次 run 實際讀進去的 design RTL 路徑（容器內字面值）。
    read_path: str | None = None
    #: 本 slot 私有 source 內該檔案的實際 SHA-256。
    actual_sha256: str | None = None
    expected_sha256: str | None = None
    modules_expected: tuple[str, ...] = ()
    modules_seen: tuple[str, ...] = ()
    established: bool = False
    evidence_files: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot, "status": self.status, "reason": self.reason,
            "read_path": self.read_path,
            "actual_sha256": self.actual_sha256,
            "expected_sha256": self.expected_sha256,
            "modules_expected": list(self.modules_expected),
            "modules_seen": list(self.modules_seen),
            "established": self.established,
            "evidence_files": list(self.evidence_files),
            "notes": list(self.notes),
        }


def _synth_logs(output_root: Path, platform: str, design: str,
                variant: str) -> list[Path]:
    logs = output_root / "logs" / platform / design / variant
    names = ("1_1_yosys_canonicalize.log", "1_2_yosys.log", "1_synth.log")
    return [logs / n for n in names if (logs / n).is_file()]


def read_rtl_source_evidence(
    output_root: str | Path,
    *,
    platform: str,
    design: str,
    variant: str,
    slot: str,
    rtl_relative_path: str,
    expected_sha256: str | None,
    modules: tuple[str, ...] = (),
    source_root: str | Path | None = None,
) -> RtlSourceEvidence:
    """由 synth log ＋ 該 slot 私有 source 判定三層條件。"""
    root = Path(output_root)
    wanted = Path(rtl_relative_path).name
    files: list[str] = []
    notes: list[str] = []

    read_path: str | None = None
    seen_modules: set[str] = set()
    for log in _synth_logs(root, platform, design, variant):
        text = log.read_text(errors="replace")
        files.append(str(log))
        if read_path is None:
            for match in _FRONTEND.finditer(text):
                candidate = match.group("path")
                if Path(candidate).name == wanted:
                    read_path = candidate
                    break
        for module in modules:
            if module in text:
                seen_modules.add(module)

    if not files:
        return RtlSourceEvidence(
            slot=slot, status=UNKNOWN,
            reason="no synth log is available for this run",
            expected_sha256=expected_sha256, modules_expected=tuple(modules))
    if read_path is None:
        return RtlSourceEvidence(
            slot=slot, status=NOT_READ,
            reason=(f"no synth log records reading {wanted!r} through the "
                    "Verilog frontend"),
            expected_sha256=expected_sha256, modules_expected=tuple(modules),
            evidence_files=tuple(files))

    actual: str | None = None
    if source_root is not None:
        local = Path(source_root) / rtl_relative_path
        if local.is_file():
            actual = hashlib.sha256(local.read_bytes()).hexdigest()
            files.append(str(local))
        else:
            notes.append(f"the slot source RTL was not readable at {local}")
    else:
        notes.append("no slot source root was provided; content not verified")

    if actual is None or expected_sha256 is None:
        return RtlSourceEvidence(
            slot=slot, status=UNKNOWN,
            reason="cannot verify which RTL bytes this run consumed",
            read_path=read_path, actual_sha256=actual,
            expected_sha256=expected_sha256, modules_expected=tuple(modules),
            modules_seen=tuple(sorted(seen_modules)),
            evidence_files=tuple(files), notes=tuple(notes))
    if actual != expected_sha256:
        return RtlSourceEvidence(
            slot=slot, status=CONTENT_MISMATCH,
            reason=(f"the RTL this slot carries hashes to {actual} but the "
                    f"approved value for {slot} is {expected_sha256}"),
            read_path=read_path, actual_sha256=actual,
            expected_sha256=expected_sha256, modules_expected=tuple(modules),
            modules_seen=tuple(sorted(seen_modules)),
            evidence_files=tuple(files), notes=tuple(notes))
    if modules and set(modules) - seen_modules:
        return RtlSourceEvidence(
            slot=slot, status=MODULES_MISSING,
            reason=("the elaborated design does not mention "
                    f"{sorted(set(modules) - seen_modules)}"),
            read_path=read_path, actual_sha256=actual,
            expected_sha256=expected_sha256, modules_expected=tuple(modules),
            modules_seen=tuple(sorted(seen_modules)),
            evidence_files=tuple(files), notes=tuple(notes))
    return RtlSourceEvidence(
        slot=slot, status=OK,
        reason=(f"this run read {read_path} and the slot source hashes to the "
                f"approved value; the declared modules are elaborated"),
        read_path=read_path, actual_sha256=actual,
        expected_sha256=expected_sha256, modules_expected=tuple(modules),
        modules_seen=tuple(sorted(seen_modules)), established=True,
        evidence_files=tuple(files), notes=tuple(notes))


__all__ = ["CONTENT_MISMATCH", "MODULES_MISSING", "NOT_READ", "OK", "UNKNOWN",
           "RtlSourceEvidence", "read_rtl_source_evidence"]
