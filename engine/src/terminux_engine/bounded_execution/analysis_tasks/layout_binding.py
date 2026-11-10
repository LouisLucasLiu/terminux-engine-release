# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""candidate 沿用 baseline 實體邊界的固定綁定。

`gcd.exact_baseline_bounds.v1`：候選的 floorplan 不重新由 utilization 推導，
而是直接使用**本 task s1 量到的整數 DBU 邊界**。這不是第二個待搜尋參數——
規則在授權時就固定，host 只是在 s1 結果通過驗證後把實際數值填進去。

換算一律走 `Fraction`：DBU 是整數，`db_units_per_micron` 也是整數，兩者相除
可以得到**精確**的十進位。用 float 或由面積反推長寬都會讓 candidate 拿到與
baseline 不同的邊界，那就不再是「相同實體邊界」。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

POLICY_ID = "gcd.exact_baseline_bounds.v1"

_CORNERS = ("llx", "lly", "urx", "ury")


class LayoutBindingError(RuntimeError):
    """s1 的 geometry 證據不足以建立綁定。**不猜、不用舊數字補。**"""


def _exact_micron(dbu: int, units: int) -> str:
    """整數 DBU → 精確十進位字串（不四捨五入、不留多餘的 0）。"""
    value = Fraction(int(dbu), int(units))
    # 分母只含 2 與 5 時才有有限小數；DBU/micron 一律是 10 的倍數，成立。
    denominator = value.denominator
    residue = denominator
    for factor in (2, 5):
        while residue % factor == 0:
            residue //= factor
    if residue != 1:
        raise LayoutBindingError(
            f"{dbu}/{units} has no exact decimal representation")
    digits = 0
    scaled = value
    while scaled.denominator != 1:
        scaled *= 10
        digits += 1
    text = f"{int(scaled)}"
    if digits == 0:
        return text
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    text = text.rjust(digits + 1, "0")
    out = f"{text[:-digits]}.{text[-digits:]}".rstrip("0").rstrip(".")
    return ("-" if negative else "") + (out or "0")


@dataclass(frozen=True)
class LayoutBinding:
    """s1 量到的邊界。整數 DBU 是權威值，micron 只是換算後的呈現。"""

    policy_id: str
    run_id: str
    slot: str
    db_units_per_micron: int
    core_dbu: dict[str, int]
    die_dbu: dict[str, int]
    evidence_path: str
    evidence_sha256: str

    def core_micron(self) -> tuple[str, str, str, str]:
        return tuple(_exact_micron(self.core_dbu[c], self.db_units_per_micron)
                     for c in _CORNERS)  # type: ignore[return-value]

    def die_micron(self) -> tuple[str, str, str, str]:
        return tuple(_exact_micron(self.die_dbu[c], self.db_units_per_micron)
                     for c in _CORNERS)  # type: ignore[return-value]

    def config_section(self) -> bytes:
        """append 到 candidate config 末端的固定段。

        `CORE_UTILIZATION` 必須先移除：pinned `floorplan.tcl` 對四種
        floorplan 方法做互斥檢查，同時看得到 utilization 與 die/core area
        會直接 `exit 1`。
        """
        die = " ".join(self.die_micron())
        core = " ".join(self.core_micron())
        return (
            "\n"
            "# ── Terminux R5 v0.5：候選沿用本 task baseline 的實體邊界 ──────\n"
            f"# policy={self.policy_id}；來源 run={self.run_id} slot={self.slot}\n"
            f"# 整數 DBU（權威值）：core={self.core_dbu} die={self.die_dbu}\n"
            f"# db_units_per_micron={self.db_units_per_micron}\n"
            "# 這不是新的待搜尋參數，而是「與 baseline 相同邊界」這條固定規則\n"
            "# 在本 task 的實際值。utilization 必須移除：pinned floorplan.tcl\n"
            "# 的四種方法互斥，同時存在會直接失敗。\n"
            "undefine CORE_UTILIZATION\n"
            "unexport CORE_UTILIZATION\n"
            f"export DIE_AREA = {die}\n"
            f"export CORE_AREA = {core}\n"
        ).encode("utf-8")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id, "run_id": self.run_id,
            "slot": self.slot,
            "db_units_per_micron": self.db_units_per_micron,
            "core_dbu": dict(self.core_dbu), "die_dbu": dict(self.die_dbu),
            "core_micron": list(self.core_micron()),
            "die_micron": list(self.die_micron()),
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "floorplan_mode": "explicit_baseline_bounds",
        }


def build_layout_binding(*, payload: Any, run_id: str, slot: str,
                         evidence_path: str, evidence_sha256: str) -> LayoutBinding:
    """由 s1 的 core evidence 建立綁定。缺任何一個整數座標就拒絕。"""
    if not isinstance(payload, dict):
        raise LayoutBindingError("the baseline core evidence is unreadable")
    units = payload.get("db_units_per_micron")
    if not isinstance(units, int) or units <= 0:
        raise LayoutBindingError("db_units_per_micron is missing or not a "
                                 "positive integer")
    boxes: dict[str, dict[str, int]] = {}
    for key in ("core_dbu", "die_dbu"):
        box = payload.get(key)
        if not isinstance(box, dict):
            raise LayoutBindingError(
                f"{key} is missing; the micron values must not be used to "
                "reconstruct the boundary")
        parsed: dict[str, int] = {}
        for corner in _CORNERS:
            value = box.get(corner)
            if isinstance(value, bool) or not isinstance(value, int):
                raise LayoutBindingError(f"{key}.{corner} must be an integer DBU")
            parsed[corner] = value
        if parsed["urx"] <= parsed["llx"] or parsed["ury"] <= parsed["lly"]:
            raise LayoutBindingError(f"{key} is not a positive area")
        boxes[key] = parsed
    core, die = boxes["core_dbu"], boxes["die_dbu"]
    if (core["llx"] < die["llx"] or core["lly"] < die["lly"]
            or core["urx"] > die["urx"] or core["ury"] > die["ury"]):
        raise LayoutBindingError("the core area is not inside the die area")
    return LayoutBinding(
        policy_id=POLICY_ID, run_id=run_id, slot=slot,
        db_units_per_micron=units, core_dbu=core, die_dbu=die,
        evidence_path=evidence_path, evidence_sha256=evidence_sha256)


__all__ = ["POLICY_ID", "LayoutBinding", "LayoutBindingError",
           "build_layout_binding"]
