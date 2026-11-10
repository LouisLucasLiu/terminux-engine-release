# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Workstream D：flow task 的 lineage 寫入——用**既有** DLE（`lineage.ledger`）。

不建第二套 lineage DB。每一次 governed flow run（一個 slot）在既有的
`JobCommit` ledger（`~/.terminux/lineage/ledger.jsonl`）留兩筆：

1. `status="intent"`：EDA **launch 之前**，記下 planned／approved 的內容——
   Profile snapshot、設計輸入（design config、RTL、SDC、liberty）、方法、
   proof 定義、toolchain 身分、命令。寫不進去就在 side effect 前停止。
2. `status="committed"`：run 完成證據到齊之後，加上 output artifacts 與
   實際消費的設定。launch 後寫入失敗只標記，不刪除、不重跑。

三層由 `evidence` 內的 `layer` 欄位與 task EventStore 交叉對應：
`planned`（DRAFT snapshot）、`approved`（grant）、`executed`（本 commit）。
`JobCommit.evidence` 的值型別是 `dict[str, str]`，所以這裡一律字串化。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from terminux_engine.lineage.ledger import append_job_commit, load_job_commits
from terminux_engine.lineage.models import ArtifactSnapshot, JobCommit
from terminux_engine.lineage.snapshot import build_snapshot

FLOW_TOOL_NAME = "orfs"
STATUS_INTENT = "intent"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"

#: flow task 用到的 artifact roles。`rtl`／`sdc`／`upf` 沿用既有 ANCHOR_KEYS。
ROLE_PROFILE = "profile"
ROLE_DESIGN_CONFIG = "design_config"
ROLE_RTL = "rtl"
ROLE_SDC = "sdc"
ROLE_LIBERTY = "liberty"
ROLE_METHOD = "method"
ROLE_PROOF_DEFINITION = "proof_definition"
ROLE_OUTPUT = "output"

#: evidence kinds（非檔案身分）。
EVIDENCE_TASK = "flow_task"
EVIDENCE_TOOLCHAIN = "toolchain"
EVIDENCE_METHOD = "method"
EVIDENCE_PROOF_TOOL = "proof_tool"
EVIDENCE_EXECUTION = "execution"
EVIDENCE_EFFECTIVE = "effective_setting"

CONTAINER_FLOW_PATH = "/OpenROAD-flow-scripts/flow"


class FlowLineageError(RuntimeError):
    """lineage 記錄無法建立或寫入。"""


def default_flow_ledger_path() -> Path:
    """既有 daemon 慣例：`~/.terminux/lineage/ledger.jsonl`。"""
    from terminux_engine.lineage.ledger import default_ledger_path

    return default_ledger_path(Path.home() / ".terminux")


def _stringify(values: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        out[str(key)] = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True)
    return out


def _synthetic_snapshot(role: str, logical_path: str, checksum: str) -> ArtifactSnapshot:
    """沒有實體檔案（例如 inline profile）時的身分快照。"""
    return ArtifactSnapshot(
        role=role, path=logical_path, checksum_sha256=checksum,
        size_bytes=0, modified_time_epoch=0.0, lineage_source="root_observation")


def _file_snapshot(role: str, path: Path) -> ArtifactSnapshot | None:
    try:
        return build_snapshot(path, role)
    except (FileNotFoundError, OSError):
        return None


def container_path_to_host(path: str, input_root: str | Path) -> Path | None:
    """把 container 內 `/OpenROAD-flow-scripts/flow/...` 對回 host 的 input snapshot。"""
    if not path:
        return None
    if path.startswith(CONTAINER_FLOW_PATH + "/"):
        return Path(input_root) / "flow" / path[len(CONTAINER_FLOW_PATH) + 1:]
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return None


def _split_paths(value: str | None) -> list[str]:
    if not value:
        return []
    return [token for token in value.replace("\n", " ").split(" ") if token]


@dataclass(frozen=True)
class FlowRunIdentity:
    """一個 slot 的 run 身分（來自 task／binding，host 建立）。"""

    task_id: str
    task_digest: str
    grant_id: str
    slot: str
    run_id: str
    profile_id: str
    profile_digest: str
    design_id: str
    platform_id: str
    docker_image_id: str | None
    member_id: str
    project_id: str
    command: str
    #: host-native run 的工具身分（Profile 授權的 binding digest）。
    native_tool_binding_sha256: str | None = None


def profile_snapshot(profile: Any, profile_source: dict[str, Any] | None) -> ArtifactSnapshot:
    """Profile 的 lineage 身分：有實體檔就用檔案 hash，inline 就用 canonical digest。"""
    source = profile_source or {}
    resolved = source.get("resolved_path")
    if resolved:
        snap = _file_snapshot(ROLE_PROFILE, Path(str(resolved)))
        if snap is not None:
            return snap
    return _synthetic_snapshot(
        ROLE_PROFILE, f"profile://{getattr(profile, 'profile_id', 'unknown')}",
        str(getattr(profile, "profile_digest", "")))


def input_snapshots_for(
    profile: Any, *, input_root: str | Path, profile_source: dict[str, Any] | None,
    effective_values: dict[str, str] | None = None,
) -> list[ArtifactSnapshot]:
    """planned／executed 兩層共用的輸入快照。

    launch 前只有 profile／design config／helper 可取；run 之後由有效設定
    （ORFS 自己的 `vars`）補上 RTL、SDC、liberty 的實際檔案。
    """
    root = Path(input_root)
    snaps: list[ArtifactSnapshot] = [profile_snapshot(profile, profile_source)]
    config = _file_snapshot(ROLE_DESIGN_CONFIG, root / str(profile.design_config))
    if config is not None:
        snaps.append(config)
    recipe = getattr(profile, "repair_recipe", None)
    for helper in (getattr(recipe, "helpers", None) or ()):
        snap = _file_snapshot(ROLE_PROOF_DEFINITION, root / str(helper.relative_path))
        if snap is not None:
            snaps.append(snap)
    values = effective_values or {}
    for role, key in ((ROLE_RTL, "VERILOG_FILES"), (ROLE_SDC, "SDC_FILE"),
                      (ROLE_LIBERTY, "LIB_FILES")):
        for token in _split_paths(values.get(key)):
            host_path = container_path_to_host(token, root)
            if host_path is None:
                continue
            snap = _file_snapshot(role, host_path)
            if snap is not None:
                snaps.append(snap)
    return snaps


def _task_evidence(identity: FlowRunIdentity, *, layer: str) -> dict[str, str]:
    return _stringify({
        "kind": EVIDENCE_TASK, "layer": layer,
        "task_id": identity.task_id, "task_digest": identity.task_digest,
        "grant_id": identity.grant_id, "slot": identity.slot,
        "run_id": identity.run_id, "profile_id": identity.profile_id,
        "profile_digest": identity.profile_digest,
        "member_id": identity.member_id, "project_id": identity.project_id,
    })


def _method_evidence(profile: Any, slot: str, value: str | None) -> dict[str, str]:
    parameter = getattr(profile, "parameter", None)
    return _stringify({
        "kind": EVIDENCE_METHOD, "slot": slot,
        "variable": getattr(parameter, "name", None),
        "value": value,
        "recipe_id": getattr(getattr(profile, "repair_recipe", None), "recipe_id", None),
        "template_id": getattr(profile, "template_id", None),
    })


def _toolchain_evidence(profile: Any, identity: FlowRunIdentity) -> dict[str, str]:
    execution = getattr(profile, "execution", None)
    return _stringify({
        "kind": EVIDENCE_TOOLCHAIN,
        "docker_image_id": identity.docker_image_id,
        "runner": getattr(execution, "runner", "docker"),
        "docker_executable": getattr(execution, "docker_executable", None),
        "host_env_script": getattr(execution, "host_env_script", None),
        "host_make_executable": getattr(execution, "host_make_executable", None),
        **({"native_tool_binding_sha256": identity.native_tool_binding_sha256}
           if identity.native_tool_binding_sha256 else {}),
    })


def _proof_evidence(profile: Any) -> dict[str, str] | None:
    recipe = getattr(profile, "repair_recipe", None)
    policy = getattr(recipe, "proof_policy_id", None)
    execution = getattr(profile, "execution", None)
    proof_tool = getattr(execution, "proof_tool", None)
    if policy is None and proof_tool is None:
        return None
    return _stringify({
        "kind": EVIDENCE_PROOF_TOOL, "proof_policy_id": policy,
        "qualified_tool_identity_path":
            getattr(recipe, "qualified_tool_identity_path", None),
        "user_declared_executable": getattr(proof_tool, "executable_realpath", None),
        "user_declared_version_sha256": getattr(proof_tool, "version_sha256", None),
    })


def build_intent_commit(
    identity: FlowRunIdentity, profile: Any, *, input_root: str | Path,
    profile_source: dict[str, Any] | None, parameter_value: str | None,
    now: float | None = None,
) -> JobCommit:
    """EDA launch **之前**的 planned／approved 記錄。"""
    stamp = time.time() if now is None else now
    profile_snap = profile_snapshot(profile, profile_source)
    evidence = [
        _task_evidence(identity, layer="approved"),
        _method_evidence(profile, identity.slot, parameter_value),
        _toolchain_evidence(profile, identity),
    ]
    proof = _proof_evidence(profile)
    if proof is not None:
        evidence.append(proof)
    return JobCommit(
        job_id=f"{identity.task_id}:{identity.slot}:{STATUS_INTENT}",
        tool_name=FLOW_TOOL_NAME, stage=identity.slot, status=STATUS_INTENT,
        started_at=stamp, ended_at=stamp,
        run_file=profile_snap.path, run_file_checksum_sha256=profile_snap.checksum_sha256,
        input_snapshots=input_snapshots_for(
            profile, input_root=input_root, profile_source=profile_source),
        output_snapshots=[], producer_relations=[], evidence=evidence,
        block_name=identity.design_id,
    )


def build_committed_commit(
    intent: JobCommit, identity: FlowRunIdentity, profile: Any, *,
    input_root: str | Path, profile_source: dict[str, Any] | None,
    effective_values: dict[str, str] | None, output_paths: Iterable[str | Path],
    parameter_value: str | None, consumption: dict[str, Any] | None,
    started_at: float, ended_at: float | None = None, status: str = STATUS_COMMITTED,
) -> JobCommit:
    """run 完成之後的 executed 記錄。輸入以有效設定補齊實際 RTL／SDC／lib。"""
    outputs: list[ArtifactSnapshot] = []
    for path in output_paths:
        snap = _file_snapshot(ROLE_OUTPUT, Path(path))
        if snap is not None:
            snap.producer_job_id = intent.job_id
            snap.producer_stage = identity.slot
            outputs.append(snap)
    evidence = [
        _task_evidence(identity, layer="executed"),
        _method_evidence(profile, identity.slot, parameter_value),
        _toolchain_evidence(profile, identity),
    ]
    proof = _proof_evidence(profile)
    if proof is not None:
        evidence.append(proof)
    if consumption:
        evidence.append(_stringify({"kind": "consumption", **consumption}))
    for key in ("SDC_FILE", "PLATFORM", "DESIGN_NAME", "FLOW_VARIANT"):
        if effective_values and key in effective_values:
            evidence.append(_stringify({"kind": EVIDENCE_EFFECTIVE, "name": key,
                                        "value": effective_values[key]}))
    return JobCommit(
        job_id=f"{identity.task_id}:{identity.slot}:{status}",
        tool_name=FLOW_TOOL_NAME, stage=identity.slot, status=status,
        started_at=started_at, ended_at=time.time() if ended_at is None else ended_at,
        run_file=intent.run_file, run_file_checksum_sha256=intent.run_file_checksum_sha256,
        input_snapshots=input_snapshots_for(
            profile, input_root=input_root, profile_source=profile_source,
            effective_values=effective_values),
        output_snapshots=outputs, producer_relations=[], evidence=evidence,
        block_name=identity.design_id,
    )


def append_flow_commit(ledger_path: str | Path, commit: JobCommit) -> Path:
    """寫進既有 ledger。任何失敗都轉成 `FlowLineageError`，由呼叫端決定
    是「launch 前停止」還是「launch 後標記不可確認」。"""
    try:
        append_job_commit(ledger_path, commit)
    except OSError as exc:
        raise FlowLineageError(f"lineage ledger write failed: {exc}") from exc
    return Path(ledger_path)


def find_flow_commits(ledger_path: str | Path, *, task_id: str,
                      slot: str | None = None, status: str | None = None) -> list[JobCommit]:
    """依 task／slot／status 找回本 task 的 commit（舊紀錄照常可讀）。"""
    found: list[JobCommit] = []
    for commit in load_job_commits(ledger_path):
        if commit.tool_name != FLOW_TOOL_NAME:
            continue
        if slot is not None and commit.stage != slot:
            continue
        if status is not None and commit.status != status:
            continue
        for item in commit.evidence:
            if item.get("kind") == EVIDENCE_TASK and item.get("task_id") == task_id:
                found.append(commit)
                break
    return found


def commit_reference(commit: JobCommit) -> dict[str, Any]:
    """寫回 task EventStore／view 的最小引用。"""
    return {
        "job_id": commit.job_id, "status": commit.status, "stage": commit.stage,
        "run_file": commit.run_file, "run_file_sha256": commit.run_file_checksum_sha256,
        "inputs": [{"role": s.role, "path": s.path, "sha256": s.checksum_sha256}
                   for s in commit.input_snapshots],
        "outputs": len(commit.output_snapshots),
        "content_sha256": hashlib.sha256(
            commit.model_dump_json().encode("utf-8")).hexdigest(),
    }


__all__ = [
    "EVIDENCE_EFFECTIVE",
    "EVIDENCE_EXECUTION",
    "EVIDENCE_METHOD",
    "EVIDENCE_PROOF_TOOL",
    "EVIDENCE_TASK",
    "EVIDENCE_TOOLCHAIN",
    "FLOW_TOOL_NAME",
    "ROLE_DESIGN_CONFIG",
    "ROLE_LIBERTY",
    "ROLE_METHOD",
    "ROLE_OUTPUT",
    "ROLE_PROFILE",
    "ROLE_PROOF_DEFINITION",
    "ROLE_RTL",
    "ROLE_SDC",
    "STATUS_COMMITTED",
    "STATUS_FAILED",
    "STATUS_INTENT",
    "FlowLineageError",
    "FlowRunIdentity",
    "append_flow_commit",
    "build_committed_commit",
    "build_intent_commit",
    "commit_reference",
    "container_path_to_host",
    "default_flow_ledger_path",
    "find_flow_commits",
    "input_snapshots_for",
    "profile_snapshot",
]
