# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import re
import time
from pathlib import Path

from terminux_engine.lineage.models import JobIntent
from terminux_engine.lineage.snapshot import build_snapshot

_SET_RE = re.compile(r'^\s*set\s+(\S+)\s+(.+?)\s*$')


def parse_runfile(
    runfile_path: str | Path,
    command: str,
) -> JobIntent:
    """
    解析 fe_lineage_trial 格式的 *.tcl，回傳 JobIntent。
    intent_id = f"intent_{stage}_{design}_{int(time.time())}"
    所有 path resolve 為絕對路徑字串，不檢查存在性。
    """
    runfile_path = Path(runfile_path).resolve()
    base_dir = runfile_path.parent

    stage = "unknown_stage"
    tool_name = "unknown_tool"
    design_name = "top"
    declared_inputs: list[dict[str, str]] = []
    declared_outputs: list[dict[str, str]] = []

    with runfile_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _SET_RE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            # 去掉首尾雙引號
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]

            if key == "STAGE_NAME":
                stage = value
            elif key == "TOOL_NAME":
                tool_name = value
            elif key == "DESIGN_NAME":
                design_name = value
            elif key.startswith("INPUT_"):
                role = key[len("INPUT_"):].lower()
                abs_path = str((base_dir / value).resolve())
                declared_inputs.append({"role": role, "path": abs_path})
            elif key.startswith("OUTPUT_"):
                role = key[len("OUTPUT_"):].lower()
                abs_path = str((base_dir / value).resolve())
                declared_outputs.append({"role": role, "path": abs_path})
            # 其餘 key 靜默忽略

    snap = build_snapshot(runfile_path, role="run_file")
    intent_id = f"intent_{stage}_{design_name}_{int(time.time())}"

    return JobIntent(
        intent_id=intent_id,
        tool_name=tool_name,
        stage=stage,
        command=command,
        run_file=str(runfile_path),
        run_file_checksum_sha256=snap.checksum_sha256,
        design_name=design_name,
        declared_inputs=declared_inputs,
        declared_outputs=declared_outputs,
    )
