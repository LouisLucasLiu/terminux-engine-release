# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""實際有效設定與參數被消費的證據。

兩份證據，都不是「解讀 log」：

1. `terminux_effective_settings.txt`——由 adapter 在 EDA stage **使用設定
   之前**，用 ORFS 自己的 `print-%` target 產生。
2. `objects/<platform>/<design>/<variant>/vars`——ORFS 自己的 `vars` target
   產物，完整變數快照。

以及參數確實**被消費**的證據：floorplan 產物的 die／core area。只寫出
config 或讓 argv 帶該字串都不算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EFFECTIVE_SETTINGS_FILENAME = "terminux_effective_settings.txt"
#: RC-4：adapter 在 `source env.sh` 之後寫出的實際 EDA binary 身分。
TOOL_BINARIES_FILENAME = "terminux_tool_binaries.txt"
#: 一個工具完整身分必須具備的欄位。缺任何一個就標為 incomplete，不補值。
TOOL_BINARY_REQUIRED_FIELDS: tuple[str, ...] = ("realpath", "sha256", "version")
_TOOL_BINARY_LINE = re.compile(
    r"^(?P<tool>[a-z][a-z0-9_]*)\.(?P<field>[a-z0-9_]+):\s*(?P<value>.*)$")
_SHA256_TOKEN = re.compile(r"^[0-9a-f]{64}$")

_PRINT_LINE = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*):\s*(?P<value>.*)$")
#: ORFS `vars` 檔的形式：`VAR=value`（generate-vars.sh）。
_VARS_LINE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
#: floorplan **實際使用**這個參數的證據。三條都是 ORFS／OpenROAD 自己寫的：
#:
#: 1. `initialize_floorplan -utilization <N>`：工具實際收到的值。
#: 2. `Defining die area using utilization: <N>%`：工具據此決定 die area。
#: 3. `Core area: <X> um^2`：結果。
#:
#: 只寫出 config 或讓 argv 帶該字串都不算——必須看到工具**收到並用了**它。
_FLOORPLAN_INVOCATION = re.compile(
    r"initialize_floorplan\b[^\n]*?-utilization\s+(?P<value>[0-9]+(?:\.[0-9]+)?)")
_FLOORPLAN_DIE_AREA = re.compile(
    r"Defining die area using utilization:\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*%")
_FLOORPLAN_CORE_AREA = re.compile(
    r"Core area:\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*um\^2")


@dataclass(frozen=True)
class EffectiveSettings:
    """一次 execution 實際生效的設定。"""

    values: dict[str, str] = field(default_factory=dict)
    source: str = "unavailable"
    vars_file: str | None = None
    #: 讀不到的項目一律列出來，**不補值**。
    missing: tuple[str, ...] = ()

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "values": dict(self.values), "source": self.source,
            "vars_file": self.vars_file, "missing": list(self.missing),
        }


def read_effective_settings(
    output_root: str | Path, *, platform: str, design: str, variant: str,
    required: tuple[str, ...] = ("CORE_UTILIZATION", "DESIGN_CONFIG"),
) -> EffectiveSettings:
    """讀回本次 execution 的有效設定。

    優先用 adapter 產生的 `print-%` 輸出；再用 ORFS 自己的 `vars` 檔補齊。
    兩者都讀不到就誠實回 `unavailable`，不從 log 猜。
    """
    root = Path(output_root)
    values: dict[str, str] = {}
    sources: list[str] = []

    settings_file = root / EFFECTIVE_SETTINGS_FILENAME
    if settings_file.is_file():
        for line in settings_file.read_text(errors="replace").splitlines():
            match = _PRINT_LINE.match(line.strip())
            if match:
                values[match.group("name")] = match.group("value").strip()
        if values:
            sources.append("make_print")

    vars_file = root / "objects" / platform / design / variant / "vars"
    vars_path: str | None = None
    if vars_file.is_file():
        vars_path = str(vars_file)
        for line in vars_file.read_text(errors="replace").splitlines():
            match = _VARS_LINE.match(line.strip())
            if match:
                values.setdefault(match.group("name"), match.group("value").strip())
        sources.append("orfs_vars")

    missing = tuple(name for name in required if name not in values)
    return EffectiveSettings(
        values=values, source="+".join(sources) if sources else "unavailable",
        vars_file=vars_path, missing=missing,
    )


@dataclass(frozen=True)
class ParameterConsumption:
    """參數是否真的被 floorplan 消費。"""

    consumed: bool
    reason: str
    evidence_files: tuple[str, ...] = ()
    observed: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "consumed": self.consumed, "reason": self.reason,
            "evidence_files": list(self.evidence_files),
            "observed": dict(self.observed),
        }


#: `ABC_AREA` 的消費證據在 synth 階段，不在 floorplan。
_ABC_SCRIPT_LINE = re.compile(r"Using ABC (area|speed) script\.")
_ABC_CLOCK_LINE = re.compile(r"Setting clock period to (\S+)")
_ABC_SCRIPT_BY_VALUE = {"1": "area", "0": "speed"}


#: 有**消費點規則**的參數。其他變數只能觀察有效設定，消費證據如實標為
#: 未驗證（差異是資訊，不是禁令——不因為沒規則就拒絕執行）。
CONSUMPTION_RULES: dict[str, str] = {
    "CORE_UTILIZATION": "floorplan",
    "ABC_AREA": "synth_abc_script",
    "SETUP_SLACK_MARGIN": "cts_repair_timing_argv",
}


def unverified_consumption_evidence(
    output_root: str | Path, *, platform: str, design: str, variant: str,
    parameter_name: str, expected_value: str | None = None,
) -> ParameterConsumption:
    """沒有消費點規則的參數：只回報有效設定裡看到的值，**不**宣稱被消費。"""
    settings = read_effective_settings(
        output_root, platform=platform, design=design, variant=variant,
        required=(parameter_name,))
    observed = {"effective_value": settings.get(parameter_name) or "",
                "settings_source": settings.source}
    return ParameterConsumption(
        consumed=False,
        reason=(f"no consumption-point rule is registered for {parameter_name}; "
                "only the effective setting was observed, so method consumption "
                "is unverified (execution is not blocked by this)"),
        evidence_files=tuple(f for f in (settings.vars_file,) if f),
        observed=observed)


def consumption_evidence_for_parameter(
    parameter_name: str, output_root: str | Path, *, platform: str, design: str,
    variant: str, expected_value: str | None = None,
) -> ParameterConsumption:
    """依**這個 task** 的參數名稱選消費證據來源（不再讀 daemon-global profile）。"""
    if parameter_name == "SETUP_SLACK_MARGIN":
        return setup_margin_consumption_evidence(
            output_root, platform=platform, design=design, variant=variant,
            expected_value=expected_value)
    if parameter_name == "ABC_AREA":
        return abc_strategy_consumption_evidence(
            output_root, platform=platform, design=design, variant=variant,
            expected_value=expected_value)
    if parameter_name == "CORE_UTILIZATION":
        return floorplan_consumption_evidence(
            output_root, platform=platform, design=design, variant=variant,
            expected_value=expected_value)
    return unverified_consumption_evidence(
        output_root, platform=platform, design=design, variant=variant,
        parameter_name=parameter_name, expected_value=expected_value)


def consumption_evidence_for_profile(profile_guard: "Any"):
    """**Deprecated**：舊版用 daemon-global profile 決定參數名稱。保留給
    舊 check script；production 走 `consumption_evidence_for_parameter`。"""
    def _evidence(output_root, *, platform, design, variant,
                  expected_value=None, parameter_name=None):
        name = parameter_name
        if name is None:
            try:
                name = profile_guard.current().parameter.name
            except Exception:  # noqa: BLE001 - 讀不到就沿用原本的來源
                name = "CORE_UTILIZATION"
        return consumption_evidence_for_parameter(
            name, output_root, platform=platform, design=design,
            variant=variant, expected_value=expected_value)

    return _evidence


_SETUP_MARGIN_ARG = re.compile(r"-setup_margin\s+(\S+)")
_REPAIR_TIMING_LINE = re.compile(r"^.*\brepair_timing\b.*$", re.MULTILINE)


def setup_margin_consumption_evidence(
    output_root: str | Path, *, platform: str, design: str, variant: str,
    expected_value: str | None = None,
) -> "ParameterConsumption":
    """從 CTS log 證明 `repair_timing` **實際收到** `-setup_margin`。

    `scripts/util.tcl:45` 的 `log_cmd repair_timing {*}$additional_args` 會把
    真正送出去的 argv 印進 log，而 `append_env_var` 只在值非空時才 append
    這個旗標。因此「log 裡的 argv」＋「pinned source 的映射」才是消費證據；
    `make print-SETUP_SLACK_MARGIN` 只是設定值，不算。

    baseline 的值是 ORFS 預設 0——**預期它不出現在 argv 裡**，這同樣是可
    驗證的消費事實，不是「查不到」。
    """
    root = Path(output_root)
    logs = root / "logs" / platform / design / variant
    observed: dict[str, str] = {}
    files: list[str] = []
    ran = False
    seen: str | None = None
    if logs.is_dir():
        for name in ("4_1_cts.log", "4_cts.log"):
            path = logs / name
            if not path.is_file():
                continue
            files.append(str(path))
            text = path.read_text(errors="replace")
            for line in _REPAIR_TIMING_LINE.findall(text):
                ran = True
                found = _SETUP_MARGIN_ARG.search(line)
                if found:
                    seen = found.group(1)
            break
    observed["repair_timing_ran"] = "true" if ran else "false"
    observed["setup_margin_in_argv"] = seen if seen is not None else ""
    if not files:
        return ParameterConsumption(
            consumed=False,
            reason="no CTS log records the repair_timing invocation",
            evidence_files=(), observed=observed)
    if not ran:
        return ParameterConsumption(
            consumed=False,
            reason="the CTS log records no repair_timing invocation, so this "
                   "parameter had nothing to be consumed by",
            evidence_files=tuple(files), observed=observed)
    if expected_value is None:
        return ParameterConsumption(
            consumed=True, reason="the CTS repair_timing invocation was recorded",
            evidence_files=tuple(files), observed=observed)
    want = str(expected_value).strip()
    # 實測（parametric setup repair attempt3 的 baseline）：ORFS 會把這個變數的值明確帶進環境，
    # 因此 `append_env_var` **一律**會 append 旗標——連預設的 0 也會出現成
    # `-setup_margin 0`。所以判斷依據是「工具收到的值」，不是「旗標在不在」。
    if seen is None:
        return ParameterConsumption(
            consumed=False,
            reason=(f"SETUP_SLACK_MARGIN={want} never reached repair_timing: "
                    "the CTS log shows no -setup_margin flag at all"),
            evidence_files=tuple(files), observed=observed)
    try:
        matches = float(seen) == float(want)
    except ValueError:
        matches = seen == want
    if not matches:
        return ParameterConsumption(
            consumed=False,
            reason=(f"repair_timing received -setup_margin {seen} but the "
                    f"approved value for this slot is {want}"),
            evidence_files=tuple(files), observed=observed)
    return ParameterConsumption(
        consumed=True,
        reason=("the CTS repair_timing invocation actually received "
                f"-setup_margin {seen}"),
        evidence_files=tuple(files), observed=observed)


def abc_strategy_consumption_evidence(
    output_root: str | Path, *, platform: str, design: str, variant: str,
    expected_value: str | None = None,
) -> "ParameterConsumption":
    """從 synth log 證明**實際選用的 ABC script** 與 `ABC_AREA` 一致。

    pinned `synth_preamble.tcl` 在分支之後自己印出「Using ABC area/speed
    script.」。那一行才是消費證據；`make print-ABC_AREA` 只是設定值。
    """
    root = Path(output_root)
    logs = root / "logs" / platform / design / variant
    observed: dict[str, str] = {}
    files: list[str] = []
    if logs.is_dir():
        for name in ("1_1_yosys_canonicalize.log", "1_synth.log", "1_2_yosys.log"):
            path = logs / name
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
            script = _ABC_SCRIPT_LINE.search(text)
            clock = _ABC_CLOCK_LINE.search(text)
            if script is None:
                continue
            files.append(str(path))
            observed["abc_script_kind"] = script.group(1)
            observed["abc_script"] = f"abc_{script.group(1)}.script"
            if clock is not None:
                observed["abc_clock_period_value"] = clock.group(1)
            break
    if not observed:
        return ParameterConsumption(
            consumed=False,
            reason="no synth log records which ABC script was used",
            evidence_files=tuple(files), observed={})
    if expected_value is not None:
        want = _ABC_SCRIPT_BY_VALUE.get(str(expected_value).strip())
        if want is None:
            return ParameterConsumption(
                consumed=False,
                reason=f"ABC_AREA={expected_value!r} is not a fixed value of "
                       "this template",
                evidence_files=tuple(files), observed=observed)
        if observed["abc_script_kind"] != want:
            return ParameterConsumption(
                consumed=False,
                reason=(f"ABC_AREA={expected_value} should select the {want} "
                        f"script but synthesis used the "
                        f"{observed['abc_script_kind']} script"),
                evidence_files=tuple(files), observed=observed)
    return ParameterConsumption(
        consumed=True,
        reason="synthesis recorded the ABC script it actually used",
        evidence_files=tuple(files), observed=observed)


def floorplan_consumption_evidence(
    output_root: str | Path, *, platform: str, design: str, variant: str,
    expected_value: str | None = None,
) -> ParameterConsumption:
    """從 floorplan 的實際紀錄證明參數**被消費**。

    證據是 OpenROAD 自己寫進 floorplan log 的三件事：它收到的
    `-utilization` 值、它據此決定 die area、以及產生的 core area。
    給了 `expected_value` 時，收到的值必須相符——否則就算 floorplan 跑過，
    也不能說「這個參數生效了」。

    **不**接受「config 裡寫了」或「argv 帶了字串」當證據。
    """
    root = Path(output_root)
    logs = root / "logs" / platform / design / variant
    results = root / "results" / platform / design / variant

    observed: dict[str, str] = {}
    files: list[str] = []
    for candidate in sorted(logs.glob("2_*floorplan*.log")) if logs.is_dir() else []:
        text = candidate.read_text(errors="replace")
        invocation = _FLOORPLAN_INVOCATION.search(text)
        die_area = _FLOORPLAN_DIE_AREA.search(text)
        core_area = _FLOORPLAN_CORE_AREA.search(text)
        if invocation is None and die_area is None:
            continue
        if invocation is not None:
            observed["tool_received_utilization"] = invocation.group("value")
        if die_area is not None:
            observed["die_area_from_utilization_pct"] = die_area.group("value")
        if core_area is not None:
            observed["core_area_um2"] = core_area.group("value")
        files.append(str(candidate))
        break

    floorplan_odb = results / "2_floorplan.odb"
    if floorplan_odb.is_file():
        files.append(str(floorplan_odb))
        observed["floorplan_odb_bytes"] = str(floorplan_odb.stat().st_size)

    if not files:
        return ParameterConsumption(
            consumed=False,
            reason=("no floorplan artifact or log was found under this slot's "
                    "output root; parameter consumption is not proven"))
    received = observed.get("tool_received_utilization")
    if received is None:
        return ParameterConsumption(
            consumed=False,
            reason=("the floorplan log does not record the utilization the tool "
                    "actually received"),
            evidence_files=tuple(files), observed=observed)
    if expected_value is not None and _as_number(received) != _as_number(expected_value):
        return ParameterConsumption(
            consumed=False,
            reason=(f"the floorplan tool received utilization {received!r}, but "
                    f"{expected_value!r} was expected"),
            evidence_files=tuple(files), observed=observed)
    return ParameterConsumption(
        consumed=True,
        reason=("the floorplan stage recorded the utilization it received and "
                "derived the die/core area from it"),
        evidence_files=tuple(files), observed=observed)


def read_tool_binaries(output_root: str | Path) -> dict[str, Any]:
    """讀回本次 execution 實際解析到的 EDA binary 身分。

    檔案不存在時回 `unavailable`；某工具欄位不全或 hash 形狀不對時列入
    `incomplete`。這裡不推論、不補 image 內其他 binary 的資訊。
    """
    path = Path(output_root) / TOOL_BINARIES_FILENAME
    if not path.is_file():
        return {"source": "unavailable", "tools": {}, "incomplete": []}
    tools: dict[str, dict[str, str]] = {}
    for line in path.read_text(errors="replace").splitlines():
        match = _TOOL_BINARY_LINE.match(line.strip())
        if match:
            tools.setdefault(match.group("tool"), {})[match.group("field")] = (
                match.group("value").strip())
    incomplete = sorted(
        name for name, fields in tools.items()
        if any(not fields.get(key) for key in TOOL_BINARY_REQUIRED_FIELDS)
        or not _SHA256_TOKEN.match(fields.get("sha256", "")))
    return {"source": "adapter_tool_binaries", "tools": tools,
            "incomplete": incomplete}


def _as_number(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = [
    "EFFECTIVE_SETTINGS_FILENAME",
    "TOOL_BINARIES_FILENAME",
    "EffectiveSettings",
    "ParameterConsumption",
    "floorplan_consumption_evidence",
    "read_effective_settings",
    "read_tool_binaries",
]
