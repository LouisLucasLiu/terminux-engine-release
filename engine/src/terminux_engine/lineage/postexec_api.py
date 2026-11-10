# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from terminux_engine.execution_trace.trace_id import TraceChain
from terminux_engine.ipc.agent_socket import local_nodename
from terminux_engine.lineage.attach import attach_lineage, merge_anchor_maps
from terminux_engine.lineage.ledger import append_job_commit
from terminux_engine.lineage.models import ArtifactSnapshot, JobCommit, ProducerRelation
from terminux_engine.lineage.runfile_parser import parse_runfile
from terminux_engine.lineage.snapshot import build_snapshot
from terminux_engine.lineage.tool_identifier import identify_tool
from terminux_engine.lineage.trace_lookup import lookup_trace_for_post_exec
from terminux_engine.lineage.types import OUTPUT_SKIP_ROLES
from terminux_engine.resource_paths import bundled_knowledge_path


def _get_default_tool_signatures_path() -> Path:
    return bundled_knowledge_path("tool_signatures.yaml")


def _resolve_runfile(command: str, cwd: Path) -> tuple[str | None, str | None]:
    """
    從 command 提取 -f 後的 runfile，resolve 為絕對路徑。
    回傳 (runfile_abs_str, error_status) 其中一個為 None。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    runfile_str: str | None = None
    for i, tok in enumerate(tokens):
        if tok == "-f" and i + 1 < len(tokens):
            runfile_str = tokens[i + 1]
            break

    if runfile_str is None:
        return None, "INSUFFICIENT_EVIDENCE"

    runfile_abs = Path(runfile_str)
    if not runfile_abs.is_absolute():
        runfile_abs = (cwd / runfile_str).resolve()

    if not runfile_abs.exists():
        return str(runfile_abs), "INSUFFICIENT_EVIDENCE"

    return str(runfile_abs), None


# ─── Recorder ─────────────────────────────────────────────────────────────────

def _record_tool_run(
    tool_name: str,
    runfile_path: str | Path,
    ledger_path: Path,
    *,
    trace_chain: TraceChain | None = None,
) -> JobCommit:
    """
    把一次已結束的工具執行寫成一筆 JobCommit，append 到 ledger。

    只讀取 runfile 宣告的輸入與輸出，不建立、不修改任何輸出檔或目錄：
    - 輸入必須存在，不存在時拋出 FileNotFoundError；
    - 輸出只記錄執行後確實存在、可讀取的一般檔案；不存在、是目錄或其他特殊檔案
      時略過（FIFO 不開啟，避免阻塞）。
    """
    runfile_path = Path(runfile_path).resolve()
    intent = parse_runfile(runfile_path, command=f"{tool_name} -f {runfile_path}")
    stage = intent.stage

    input_snaps = [
        attach_lineage(build_snapshot(d["path"], role=d["role"]), ledger_path)
        for d in intent.declared_inputs
    ]
    combined_anchors = merge_anchor_maps(input_snaps)

    output_snaps: list[ArtifactSnapshot] = []
    for d in intent.declared_outputs:
        role = d["role"]
        out_path = Path(d["path"])
        if role in OUTPUT_SKIP_ROLES or not out_path.is_file():
            continue
        try:
            raw_out = build_snapshot(out_path, role=role)
        except OSError:
            # 檢查後才消失或沒有讀取權限：略過，不讓單一輸出中斷整筆記錄。
            continue
        output_snaps.append(raw_out.model_copy(update={
            "lineage_anchors": {k: list(v) for k, v in combined_anchors.items()},
            "lineage_source": "derived_from_inputs",
            "producer_job_id": None,
            "producer_stage": stage,
        }))

    ts = time.time()
    trace_kwargs = trace_chain.model_dump() if trace_chain is not None else {}
    commit = JobCommit(
        job_id=f"{stage}_{intent.design_name}_{int(ts)}",
        tool_name=tool_name,
        stage=stage,
        started_at=ts,
        ended_at=time.time(),
        run_file=str(runfile_path),
        run_file_checksum_sha256=intent.run_file_checksum_sha256,
        input_snapshots=input_snaps,
        output_snapshots=output_snaps,
        producer_relations=[
            ProducerRelation(
                output_role=s.role,
                output_path=s.path,
                produced_from_input_roles=[i.role for i in input_snaps],
                producer_stage=stage,
                lineage_anchors={k: list(v) for k, v in combined_anchors.items()},
            )
            for s in output_snaps
        ],
        evidence=[
            {"type": "run_file_parse", "source": str(runfile_path), "confidence": "high"},
            {"type": "post_run_file_scan", "source": "local_filesystem", "confidence": "high"},
        ],
        **trace_kwargs,
    )
    append_job_commit(ledger_path, commit)
    return commit


# ─── Result model ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PostExecResult:
    status: str            # "COMMITTED" | "SKIPPED" | "INSUFFICIENT_EVIDENCE"
    tool_name: str | None
    runfile_path: str | None
    job_id: str | None
    message: str


# ─── Public API ───────────────────────────────────────────────────────────────

def handle_post_exec(
    command: str,
    cwd: str | Path,
    exit_code: int,
    ledger_path: str | Path,
    *,
    tool_signatures_path: str | Path | None = None,
    caller_pid: int | None = None,
    nodename: str | None = None,
    post_exec_ts: float | None = None,
) -> PostExecResult:
    """
    統一 post-exec 落帳入口。
    exit_code != 0 時不呼叫 recorder，直接回 SKIPPED。
    recorder 只讀取 runfile 宣告的輸入與輸出並寫入 ledger，不建立、不修改任何輸出檔。

    caller_pid 為 shell hook 傳來的 shell PID，作為 ancestor lookup
    的 fallback_pid。None（預設）時 lookup_trace_for_post_exec 不走 ancestor
    fallback，與既有行為完全一致（向後相容）。
    """
    cwd = Path(cwd)
    ledger_path = Path(ledger_path)
    sigs_path = (
        Path(tool_signatures_path)
        if tool_signatures_path is not None
        else _get_default_tool_signatures_path()
    )

    # ── Step 1：辨識 tool ──────────────────────────────────────────────────
    tool_match = identify_tool(command, sigs_path)
    if tool_match is None:
        return PostExecResult(
            status="SKIPPED",
            tool_name=None,
            runfile_path=None,
            job_id=None,
            message="Tool not recognized by lineage signatures; commit skipped.",
        )

    tool_name = tool_match.tool_name

    # ── Step 2 & 3：定位 runfile ───────────────────────────────────────────
    runfile_abs, err_status = _resolve_runfile(command, cwd)
    if err_status is not None:
        return PostExecResult(
            status=err_status,
            tool_name=tool_name,
            runfile_path=runfile_abs,  # 可能為 None 或解析後但不存在的路徑
            job_id=None,
            message="No valid runfile found; lineage commit skipped.",
        )

    # ── Step 4：檢查 exit_code ────────────────────────────────────────────
    if exit_code != 0:
        return PostExecResult(
            status="SKIPPED",
            tool_name=tool_name,
            runfile_path=runfile_abs,
            job_id=None,
            message=f"Tool exited with code {exit_code}; lineage commit skipped.",
        )

    # ── Step 5：反查 wrapper trace chain──────────────────
    # 從 request nodename 對應的 JSONL 尾部用
    # (tool_name + cwd + ts + argv) 反查
    # wrapper 在 pre-exec 時寫入的 trace chain。失敗回 None → JobCommit 五個
    # trace 欄位走 default 空字串，不影響 lineage commit 流程。
    trace_nodename = nodename if isinstance(nodename, str) else local_nodename()
    trace_nodename = trace_nodename or local_nodename()
    if (trace_nodename in {"", ".", ".."}
            or any(not (char.isascii() and (char.isalnum() or char in "._-"))
                   for char in trace_nodename)):
        trace_nodename = local_nodename()
    trace_chain = lookup_trace_for_post_exec(
        tool_name=tool_name,
        command=command,
        cwd=str(cwd),
        post_exec_ts=post_exec_ts if post_exec_ts is not None else time.time(),
        jsonl_path=Path.home() / ".terminux" / f"lineage.{trace_nodename}.jsonl",
        fallback_pid=caller_pid,  # JSONL miss → ancestor walk
    )

    # ── Step 6：記錄到 ledger（只讀取輸入與輸出）────────────────────────────
    commit = _record_tool_run(
        tool_name,
        runfile_abs,
        ledger_path,
        trace_chain=trace_chain,
    )

    return PostExecResult(
        status="COMMITTED",
        tool_name=tool_name,
        runfile_path=runfile_abs,
        job_id=commit.job_id,
        message=f"Lineage committed: job_id={commit.job_id}",
    )
