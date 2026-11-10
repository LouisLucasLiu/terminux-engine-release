# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""`gcd.single_instance_buffer_swap.v1` 的 host 端來源核驗與執行綁定。

proposal 階段（`eap_extension.compile_plan()`）要回答兩件事，而且都**不能**
信任 provider／proposal 自稱的內容：

1. 這次要改的來源候選是不是 host 已登記、內容沒變的那一份——以 proposal
   宣告的 `source_candidate.netlist_sha256` 對照 host 登記的 netlist 實際
   bytes 的 sha256。
2. 目標 instance 在那一份來源裡現在是什麼 cell、接到哪些 net——由 host
   直接解析那一份已核對 digest 的 netlist 得到（`verified_pre_swap_snapshot`），
   再用既有 `check_source_drift()` 比對 proposal 宣稱的 `expected_cell`。

這只是 proposal 階段「對既有證據」的核驗；**工具執行前**仍由 single-instance ECO 既有的
Operation 1 Tcl 對**實際載入的設計**再核對一次 instance／cell（並比對本模組
提供的 pin→net），兩者分開、都不能省（見 `single_instance_eco_adapter.py`）。

host 執行綁定（docker 執行檔、image digest、flow mount、容器內路徑、來源候選
登記表）來自 host 自己的設定檔（環境變數 `HOST_CONFIG_ENV` 指定），沿用
`constraint_mode_demo.eap_extension.compile_plan()` 以 host 環境設定解析工具
位置的既有做法——proposal 不能提供這些值。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from terminux_engine.packages.orfs_run_intelligence import single_instance_eco as SIE

HOST_CONFIG_ENV = "TERMINUX_SINGLE_INSTANCE_ECO_HOST_CONFIG"
HOST_CONFIG_SCHEMA = "gcd.single_instance_buffer_swap.host_binding.v1"
#: host-native 綁定：站點安裝的 OpenROAD 與元件庫以 realpath ＋ SHA-256 釘住，
#: flow scripts／platform／來源結果都是主機路徑。與上面的 container 綁定是兩個
#: 獨立 schema；同一個設定檔只能是其中一種。
NATIVE_HOST_CONFIG_SCHEMA = "gcd.single_instance_buffer_swap.host_binding.native.v1"
BINDING_MODE_CONTAINER = "container"
BINDING_MODE_HOST_NATIVE = "host_native"
SNAPSHOT_PARSER = "verilog_instance_scan.v1"

_REQUIRED_BINDING_KEYS = frozenset({
    "schema_version", "docker_executable", "docker_image", "flow_scripts_mount",
    "platform_dir_container", "lib_files_container", "source_candidates"})
_REQUIRED_CANDIDATE_KEYS = frozenset({
    "results_dir_host", "results_dir_container", "design_file", "sdc_file",
    "netlist_file"})
_REQUIRED_NATIVE_BINDING_KEYS = frozenset({
    "schema_version", "openroad", "lib_files", "flow_dir", "platform_dir",
    "source_candidates"})
_REQUIRED_NATIVE_CANDIDATE_KEYS = frozenset({
    "results_dir", "design_file", "sdc_file", "netlist_file"})
#: 會寫進 shell／Tcl 的主機路徑只接受不需要跳脫的字元；其他一律拒絕，不猜跳脫規則。
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./+-]*$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_NON_INSTANCE_KEYWORDS = frozenset({
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "assign",
    "supply0", "supply1", "tri", "parameter", "localparam"})
#: 一個 instance statement：`CELL name (...);`，可跨多行；名稱可為一般識別字
#: 或 Verilog escaped identifier（`\name[0] `）。
_INSTANCE_RE = re.compile(
    r"^\s*([A-Za-z_][\w$]*)\s+(\\\S+|[A-Za-z_][\w$]*)\s*\((.*?)\);",
    re.MULTILINE | re.DOTALL)
_PIN_RE = re.compile(r"\.([A-Za-z_][\w$]*)\s*\(\s*([^()]*?)\s*\)")


class SourceVerificationError(ValueError):
    """host 來源核驗失敗；`compile_plan()` 以此拒絕，不產生任何可派發的計畫。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_host_binding(path: str | None = None) -> dict[str, Any]:
    """讀取 host 自己的 ECO 執行綁定；未設定或格式不符一律拒絕。"""
    config_path = path or os.environ.get(HOST_CONFIG_ENV, "").strip()
    if not config_path:
        raise SourceVerificationError(
            "HOST_BINDING_UNAVAILABLE",
            f"{HOST_CONFIG_ENV} is not configured on this host")
    try:
        binding = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceVerificationError("HOST_BINDING_UNREADABLE", str(exc)) from exc
    if (not isinstance(binding, dict) or set(binding) != _REQUIRED_BINDING_KEYS
            or binding.get("schema_version") != HOST_CONFIG_SCHEMA):
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "host binding has unexpected fields")
    docker = Path(str(binding["docker_executable"]))
    if not docker.is_absolute() or not docker.is_file():
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "docker_executable must be an absolute file")
    if not SIE.is_digest_pinned_image(str(binding["docker_image"])):
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "docker_image must be digest pinned")
    mount = Path(str(binding["flow_scripts_mount"]))
    if not mount.is_absolute() or not mount.is_dir():
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "flow_scripts_mount must be an absolute directory")
    candidates = binding["source_candidates"]
    if not isinstance(candidates, dict) or not candidates:
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "source_candidates registry is empty")
    for reference, entry in candidates.items():
        if not isinstance(entry, dict) or set(entry) != _REQUIRED_CANDIDATE_KEYS:
            raise SourceVerificationError(
                "HOST_BINDING_INVALID", f"source candidate {reference!r} is malformed")
    return binding


def _read_binding_document(path: str | None) -> dict[str, Any]:
    config_path = path or os.environ.get(HOST_CONFIG_ENV, "").strip()
    if not config_path:
        raise SourceVerificationError(
            "HOST_BINDING_UNAVAILABLE",
            f"{HOST_CONFIG_ENV} is not configured on this host")
    try:
        binding = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceVerificationError("HOST_BINDING_UNREADABLE", str(exc)) from exc
    if not isinstance(binding, dict):
        raise SourceVerificationError("HOST_BINDING_INVALID", "host binding is not an object")
    return binding


def configured_binding_mode(path: str | None = None) -> str:
    """這台主機設定的 ECO 綁定是 container 還是 host-native；未知 schema 拒絕。"""
    schema = _read_binding_document(path).get("schema_version")
    if schema == HOST_CONFIG_SCHEMA:
        return BINDING_MODE_CONTAINER
    if schema == NATIVE_HOST_CONFIG_SCHEMA:
        return BINDING_MODE_HOST_NATIVE
    raise SourceVerificationError("HOST_BINDING_INVALID", "host binding schema is unknown")


def _safe_directory(value: Any, field: str) -> str:
    text = str(value or "")
    if (not _SAFE_PATH_RE.fullmatch(text) or os.path.normpath(text) != text
            or not Path(text).is_dir()):
        raise SourceVerificationError(
            "HOST_BINDING_INVALID", f"{field} must be an existing absolute directory "
            "with a plain path")
    return text


def load_native_host_binding(path: str | None = None) -> dict[str, Any]:
    """讀取 host-native ECO 綁定；欄位、路徑或 pin 任一不符一律拒絕。

    只做形狀與存在性核對；工具內容在派送前（`single_instance_eco_native`）
    重新核對 realpath 與 SHA-256。
    """
    from terminux_engine.orchestration.native_tool_identity import (
        NativeToolIdentityError,
        parse_pin,
    )

    binding = _read_binding_document(path)
    if (set(binding) != _REQUIRED_NATIVE_BINDING_KEYS
            or binding.get("schema_version") != NATIVE_HOST_CONFIG_SCHEMA):
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "native host binding has unexpected fields")
    try:
        for name in ("openroad", "lib_files"):
            pin = parse_pin(binding[name], where=name)
            if not _SAFE_PATH_RE.fullmatch(pin.realpath):
                raise SourceVerificationError(
                    "HOST_BINDING_INVALID", f"{name}.realpath must be a plain path")
    except NativeToolIdentityError as exc:
        raise SourceVerificationError("HOST_BINDING_INVALID", exc.message) from exc
    _safe_directory(binding["flow_dir"], "flow_dir")
    _safe_directory(Path(binding["flow_dir"]) / "scripts", "flow_dir/scripts")
    _safe_directory(binding["platform_dir"], "platform_dir")
    candidates = binding["source_candidates"]
    if not isinstance(candidates, dict) or not candidates:
        raise SourceVerificationError("HOST_BINDING_INVALID",
                                      "source_candidates registry is empty")
    for reference, entry in candidates.items():
        if (not isinstance(entry, dict) or set(entry) != _REQUIRED_NATIVE_CANDIDATE_KEYS
                or any(not _SAFE_NAME_RE.fullmatch(str(entry[key])) for key in (
                    "design_file", "sdc_file", "netlist_file"))):
            raise SourceVerificationError(
                "HOST_BINDING_INVALID", f"source candidate {reference!r} is malformed")
    return binding


def parse_netlist_instance(text: str, instance: str) -> dict[str, Any] | None:
    """在 structural Verilog 裡找出 `instance` 的 cell 與 pin→net。

    找不到回 `None`；同名 instance 出現多次視為來源不明確，也回 `None`
    （不猜哪一個才是）。
    """
    matches = [match for match in _INSTANCE_RE.finditer(text)
               if match.group(1) not in _NON_INSTANCE_KEYWORDS
               and match.group(2) == instance]
    if len(matches) != 1:
        return None
    cell, _name, body = matches[0].groups()
    pin_nets = sorted(f"{pin}={net}" for pin, net in _PIN_RE.findall(body))
    return {"cell": cell, "pin_nets": pin_nets}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def verify_source_candidate(
    *, changes: dict[str, Any], binding: dict[str, Any],
) -> dict[str, Any]:
    """由 host 登記的來源證據核驗 proposal 宣告的來源候選與目標 instance。

    成功時回傳 `package_tool.single_instance_eco` 需要的 host 綁定欄位；任何
    一項不成立都 raise `SourceVerificationError`（compile 階段拒絕，零派發）。
    """
    source = changes.get("source_candidate") or {}
    reference = source.get("candidate_reference")
    entry = binding["source_candidates"].get(reference)
    if entry is None:
        raise SourceVerificationError(
            "SOURCE_CANDIDATE_UNREGISTERED",
            f"source candidate {reference!r} is not registered on this host")
    results_host = Path(entry["results_dir_host"])
    mount = Path(binding["flow_scripts_mount"])
    # host 核對的那一份 ODB 必須就是容器實際載入的那一份：host 路徑相對
    # flow mount 的位置，要等於容器路徑相對容器 flow 的位置。
    try:
        host_relative = results_host.resolve().relative_to(mount.resolve())
        container_relative = Path(entry["results_dir_container"]).relative_to(
            SIE.CONTAINER_FLOW_PATH)
    except ValueError as exc:
        raise SourceVerificationError(
            "SOURCE_LOCATION_INVALID",
            "source results are not inside the mounted flow tree") from exc
    if host_relative != container_relative:
        raise SourceVerificationError(
            "SOURCE_LOCATION_INVALID",
            "host and container source result locations differ")
    snapshot, odb = _verified_snapshot(changes=changes, results_host=results_host,
                                       entry=entry, reference=reference)
    return {
        "verified_pre_swap_snapshot": snapshot,
        "docker_image": binding["docker_image"],
        "docker_executable": binding["docker_executable"],
        "flow_scripts_mount": binding["flow_scripts_mount"],
        "platform_dir_container": binding["platform_dir_container"],
        "lib_files_container": binding["lib_files_container"],
        "source_results_dir_container": entry["results_dir_container"],
        "source_design_file": entry["design_file"],
        "source_sdc_file": entry["sdc_file"],
        "source_odb_host_path": str(odb.resolve()),
        "source_odb_sha256": _sha256(odb),
    }


def verify_native_source_candidate(
    *, changes: dict[str, Any], binding: dict[str, Any],
) -> dict[str, Any]:
    """host-native 版的來源核驗：規則與 `verify_source_candidate()` 相同，
    回傳的是主機路徑與工具 pin，沒有 container 欄位。"""
    source = changes.get("source_candidate") or {}
    reference = source.get("candidate_reference")
    entry = binding["source_candidates"].get(reference)
    if entry is None:
        raise SourceVerificationError(
            "SOURCE_CANDIDATE_UNREGISTERED",
            f"source candidate {reference!r} is not registered on this host")
    results_host = Path(_safe_directory(entry["results_dir"], "results_dir"))
    snapshot, odb = _verified_snapshot(changes=changes, results_host=results_host,
                                       entry=entry, reference=reference)
    return {
        "verified_pre_swap_snapshot": snapshot,
        "native_tool": dict(binding["openroad"]),
        "lib_files": dict(binding["lib_files"]),
        "flow_dir": binding["flow_dir"],
        "platform_dir": binding["platform_dir"],
        "source_results_dir": str(results_host),
        "source_design_file": entry["design_file"],
        "source_sdc_file": entry["sdc_file"],
        "source_odb_host_path": str(odb.resolve()),
        "source_odb_sha256": _sha256(odb),
    }


def _verified_snapshot(*, changes: dict[str, Any], results_host: Path,
                       entry: dict[str, Any], reference: Any
                       ) -> tuple[dict[str, Any], Path]:
    """核對 host 登記的 netlist／ODB／SDC，回傳由 netlist 解析出的 instance 快照。"""
    declared_digest = (changes.get("source_candidate") or {}).get("netlist_sha256")
    netlist = results_host / entry["netlist_file"]
    odb = results_host / entry["design_file"]
    sdc = results_host / entry["sdc_file"]
    for path in (netlist, odb, sdc):
        if not _within(path, results_host) or not path.is_file():
            raise SourceVerificationError(
                "SOURCE_ARTIFACT_MISSING", f"source artifact is unavailable: {path.name}")
    netlist_digest = _sha256(netlist)
    if netlist_digest != declared_digest:
        raise SourceVerificationError(
            "SOURCE_NETLIST_DIGEST_MISMATCH",
            "the declared source netlist digest differs from the host evidence")
    target = changes.get("target_instance")
    parsed = parse_netlist_instance(
        netlist.read_text(encoding="utf-8", errors="replace"), str(target))
    if parsed is None:
        raise SourceVerificationError(
            SIE.REASON_INSTANCE_NOT_FOUND,
            f"instance {target!r} is not uniquely present in the source netlist")
    snapshot = {
        "instances": {target: {"cell": parsed["cell"]}},
        "target_pin_net": parsed["pin_nets"],
        "provenance": {"candidate_reference": reference,
                       "netlist_file": entry["netlist_file"],
                       "netlist_sha256": netlist_digest,
                       "parser": SNAPSHOT_PARSER},
    }
    ok, code, reason = SIE.check_source_drift(
        expected_cell=str(changes.get("expected_cell")),
        observed_instance_snapshot=snapshot, target_instance=str(target))
    if not ok:
        raise SourceVerificationError(str(code), str(reason))
    return snapshot, odb


__all__ = ["BINDING_MODE_CONTAINER", "BINDING_MODE_HOST_NATIVE", "HOST_CONFIG_ENV",
           "HOST_CONFIG_SCHEMA", "NATIVE_HOST_CONFIG_SCHEMA", "SNAPSHOT_PARSER",
           "SourceVerificationError", "configured_binding_mode", "load_host_binding",
           "load_native_host_binding", "parse_netlist_instance",
           "verify_native_source_candidate", "verify_source_candidate"]
