#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""**FIXTURE** — corpus replay executable（qualification 專用），不是真的 ORFS。

它只替換 docker process 邊界（正式 adapter 仍組 argv、掛載、labels、cidfile、
artifact 探索、receipt／manifest／DQE 投影與 readiness 判定）。

行為完全由輸入決定：

* corpus 由 **label 帶進來的 manifest 身分**選取：method_identity、
  extension_version 與 manifest sha256 都必須相符（Package 的 `corpus_replay`
  逐檔驗 hash）。不看 design 名稱、參數名稱、檔名或候選值。
* 生效的 `CORE_UTILIZATION` 由**唯讀掛載的 config.mk** 讀出；它必須等於本 slot
  corpus 宣告的 `method_value`，否則直接失敗（materialized config 與 corpus 不符）。
* per-slot CORE 證據由 Package 的 `core_utilization_evidence` 產生，帶本次
  task／slot／run 身分；corpus 內的 task／grant／run／receipt 身分一律不被複製。
* 輸出只寫進 container 的 `/work`（＝本 slot 的 task-owned output root）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from terminux_engine.packages.orfs_run_intelligence import corpus_replay as CR
from terminux_engine.packages.orfs_run_intelligence import (
    core_utilization_evidence as CU,
)

CONTAINER_FLOW = "/OpenROAD-flow-scripts/flow"
CONTAINER_WORK = "/work"
FIXTURE_MARKER = "TERMINUX_FIXTURE_NOT_A_REAL_EDA_RUN"
IMAGE_IDENTITY = "sha256:" + "f" * 64


def _labels(argv: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token == "--label" and index + 1 < len(argv):
            key, _, value = argv[index + 1].partition("=")
            found[key] = value
    return found


def _mounts(argv: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token == "-v" and index + 1 < len(argv):
            parts = argv[index + 1].split(":")
            if len(parts) >= 2:
                found[parts[1]] = parts[0]
    return found


def _script_value(script: str, name: str) -> str:
    match = re.search(rf"{name}=(?:'([^']*)'|(\S+))", script)
    if match is None:
        raise SystemExit(f"fixture corpus replay: {name} not found in the container script")
    return match.group(1) or match.group(2)


def _config_value(config: Path, name: str) -> str:
    match = re.search(rf"^\s*export\s+{name}\s*\??=\s*([^\s#]+)\s*$",
                      config.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
    if match is None:
        raise SystemExit(f"fixture corpus replay: no {name} in {config}")
    return match.group(1)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    from terminux_engine.version_cli import _check_help_arg, check_version_arg
    _check_help_arg(
        "usage: terminux-corpus-replay [-h] [--help] [--version]\n"
        "       terminux-corpus-replay image inspect IMAGE\n"
        "       terminux-corpus-replay run [docker 相容參數]\n\n"
        "受控 corpus replay fixture，並非真實 EDA 執行工具。\n"
        "  -h, --help  顯示用法後退出\n"
        "  --version   顯示版本後退出",
        argv[1:],
    )
    check_version_arg(argv[1:] if len(argv) > 1 else [])
    if len(argv) < 2 or argv[1] == "image":
        if "inspect" in argv:
            print(IMAGE_IDENTITY + "\t[]")
        return 0
    if argv[1] != "run":
        return 0

    labels = _labels(argv)
    mounts = _mounts(argv)
    # Profile 宣告的受控延遲：只給 timeout 行為的 qualification 用。
    for token in argv:
        if token.startswith("--terminux-fixture-sleep="):
            import time as _time

            _time.sleep(float(token.split("=", 1)[1]))
    flow_dir = Path(mounts[CONTAINER_FLOW])
    work = Path(mounts[CONTAINER_WORK])
    script = argv[-1] if argv[-3:-1] == ["bash", "-c"] else ""
    variant = _script_value(script, "FLOW_VARIANT")
    design_config = _script_value(script, "DESIGN_CONFIG")
    platform, design = Path(design_config).parts[1], Path(design_config).parts[2]

    task_id = labels["terminux.task_id"]
    slot = labels["terminux.slot"]
    run_id = labels["terminux.run_id"]
    method_identity = labels["terminux.method_extension.method_identity"]
    extension_version = labels["terminux.method_extension.extension_version"]
    manifest_path = labels["terminux.fixture_corpus.manifest_path"]
    manifest_sha256 = labels["terminux.fixture_corpus.manifest_sha256"]

    # 1. 依 method／extension／slot 身分選 corpus，逐檔驗 hash，只寫本 slot 輸出區。
    try:
        replayed = CR.replay(
            manifest_path=manifest_path, method_identity=method_identity,
            extension_version=extension_version, slot=slot, output_root=work,
            expected_sha256=manifest_sha256,
            substitutions={"platform": platform, "design": design, "variant": variant})
    except CR.CorpusReplayError as exc:
        print(f"fixture corpus replay rejected: {exc}", file=sys.stderr)
        return 91

    # 2. 生效值來自唯讀掛載的 materialized config，必須與 corpus 宣告相符。
    config = flow_dir / Path(design_config).relative_to("flow") if str(
        design_config).startswith("flow/") else flow_dir / design_config
    utilization = _config_value(config, "CORE_UTILIZATION")
    declared = str(replayed.get("method_value"))
    if utilization != declared:
        print("fixture corpus replay rejected: the materialized CORE_UTILIZATION "
              f"({utilization}) differs from the corpus slot value ({declared})",
              file=sys.stderr)
        return 92

    reports = work / "reports" / platform / design / variant
    logs = work / "logs" / platform / design / variant
    objects = work / "objects" / platform / design / variant
    objects.mkdir(parents=True, exist_ok=True)

    # 3. adapter 會讀的有效設定與 ORFS vars 快照（值來自 config，不是參數餵入）。
    (work / "terminux_effective_settings.txt").write_text(
        "# terminux effective settings (fixture corpus replay)\n"
        f"CORE_UTILIZATION: {utilization}\n"
        f"DESIGN_CONFIG: {design_config}\n"
        f"FLOW_VARIANT: {variant}\n"
        f"PLATFORM: {platform}\nDESIGN_NAME: {design}\n"
        f"WORK_HOME: {work}\n", encoding="utf-8")
    (objects / "vars").write_text(
        f"CORE_UTILIZATION={utilization}\nFLOW_VARIANT={variant}\n", encoding="utf-8")
    (work / "terminux_tool_binaries.txt").write_text(
        "# terminux tool binaries (fixture corpus replay)\n"
        "openroad.status: unavailable\nyosys.status: unavailable\n", encoding="utf-8")
    (work / FIXTURE_MARKER).write_text(
        "這個目錄是 fixture corpus replay 的輸出，不是真實 EDA 執行結果。\n"
        f"method={method_identity} slot={slot} run={run_id}\n", encoding="utf-8")

    # 4. Package 定義的 per-slot CORE 證據。執行身分（receipt／manifest／DQE／
    #    result event）不在這裡，由 host 判定時補上。
    evidence = dict(replayed.get("evidence") or {})
    floorplan_log = logs / str(evidence.get("floorplan_log") or "2_1_floorplan.log")
    # corpus replay 只能走 fixture 入口；真實 ORFS builder 不在此路徑上。
    payload = CU.build_fixture_slot_evidence(
        method_identity=method_identity, extension_version=extension_version,
        slot=slot, task_id=task_id, run_id=run_id, core_utilization=utilization,
        materialized_config=config, floorplan_log=floorplan_log,
        scripts_digest=_tree_digest(flow_dir / "scripts")
        if (flow_dir / "scripts").is_dir() else "",
        image_digest=IMAGE_IDENTITY,
        tool_digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        rtl_digest=str(evidence.get("rtl_digest") or ""),
        sdc_digest=str(evidence.get("sdc_digest") or ""),
        corner=str(evidence.get("corner") or ""),
        library=str(evidence.get("library") or ""),
        proof_references=tuple(reports / str(name)
                               for name in evidence.get("proof_references") or ()),
        proof_status=str(evidence.get("proof_status") or "unverifiable"),
        proof_kinds=tuple(evidence.get("proof_kinds") or ()),
        corpus={"adapter_schema": replayed["schema_version"],
                "manifest_sha256": replayed["manifest_sha256"],
                "files": len(replayed["files"])})
    target = reports / CU.EVIDENCE_FILENAME_TEMPLATE.format(slot=slot)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                      encoding="utf-8")
    return 0


def cli() -> int:
    """console script 入口：`terminux-corpus-replay`（安裝版由 site-packages 提供）。"""
    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(cli())
