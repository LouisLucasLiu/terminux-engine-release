# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from pydantic import BaseModel, Field

from terminux_engine.isvn.coordinate import DesignCoordinate

from .types import ANCHOR_KEYS, LineageSource, LineageStatus, FindingSeverity


class ArtifactSnapshot(BaseModel):
    role: str
    path: str
    checksum_sha256: str
    size_bytes: int
    modified_time_epoch: float
    producer_job_id: str | None = None
    producer_stage: str | None = None
    lineage_anchors: dict[str, list[str]] = Field(
        default_factory=lambda: {k: [] for k in ANCHOR_KEYS}
    )
    lineage_source: LineageSource = 'root_observation'
    isvn_coordinate: DesignCoordinate | None = None


class JobIntent(BaseModel):
    intent_id: str
    tool_name: str
    stage: str
    command: str
    run_file: str
    run_file_checksum_sha256: str
    design_name: str
    declared_inputs: list[dict[str, str]]   # [{"role": ..., "path": ...}]
    declared_outputs: list[dict[str, str]]


class ProducerRelation(BaseModel):
    output_role: str
    output_path: str
    produced_from_input_roles: list[str]
    producer_stage: str
    lineage_anchors: dict[str, list[str]] = Field(default_factory=dict)


class JobCommit(BaseModel):
    job_id: str
    tool_name: str
    stage: str
    status: str = 'committed'
    started_at: float
    ended_at: float
    run_file: str
    run_file_checksum_sha256: str
    input_snapshots: list[ArtifactSnapshot]
    output_snapshots: list[ArtifactSnapshot]
    producer_relations: list[ProducerRelation]
    evidence: list[dict[str, str]]
    block_name: str | None = None
    """IC block 名稱。由 block_name_extractor 提取，可能為 None。"""

    # ─── trace chain ─────────────────────────────────────
    # 與 SessionEvent 對稱，方便 grep 與 daemon 端 ingest 時欄位對齊。
    # 不加 layer 欄位：JobCommit 永遠是 EDA tool / wrapper 層，無多層語意。
    # 格式驗證由生產端負責（同 SessionEvent 哲學）。
    trace_root: str = ""
    """sess_{uuid_hex}。空字串 = trace 不可用（legacy JobCommit 或 Z6-1 caller 未提供）。"""
    trace_queue: str = ""
    """submit_{random_hex}。Workstation-only / MVP 階段固定空字串。"""
    trace_job: str = ""
    """slurm_job_{job_id}。Workstation-only / MVP 階段固定空字串。"""
    trace_parent: str = ""
    """上一層的 trace_self。"""
    trace_self: str = ""
    """inv_{epoch_ns}_{pid}。空字串 = trace 不可用。"""


class Finding(BaseModel):
    severity: FindingSeverity
    status: LineageStatus
    message: str
    role: str | None = None
    path: str | None = None
    expected_rtl_checksum: str | None = None
    observed_rtl_anchors: list[str] = []

    # ── Sibling Divergence─────
    kind: str | None = None
    """Finding 子類別。None=legacy（既有 anchor finding 不主動填）。
    新增：anchor_mismatch / sibling_divergence / unverified_topology。"""
    lca_branch_id: str | None = None
    """sibling_divergence 才填；最深共祖 BranchNode.branch_id。"""
    divergent_inputs: list[str] = []
    """sibling_divergence 才填；引發此 divergence 的兩 input path。"""

    # ── 加性結構化（不破壞既有 9 欄位）───────────
    category: str | None = None
    """已知 category 值（不用 enum，允許未來擴展）：
       MISSING_CHECKSUM    — summary artifact 缺 source_rtl_checksum
       ANCHOR_MISMATCH     — source_rtl_checksum 和 target RTL 不一致
       SIBLING_DIVERGENCE  — input 在 sibling branch 分歧
       UNVERIFIED_TOPOLOGY — input 無 branch_node，topology 無法驗證
       ALL_INPUTS_ALIGNED  — 全部 input 通過檢查（closing finding）
       RUNFILE_NOT_FOUND   — runfile 路徑不存在（preexec_api 早 return）
       UNKNOWN             — fallback
       flow task change dimensions（加性，見 lineage/change_dimensions.py）：
       LIBRARY_OR_CORNER_CHANGED     — liberty／platform 改變
       METHOD_CHANGED                — design config／method／profile 改變（預期的受控變更）
       TOOLCHAIN_CHANGED             — image／runner／EDA executable 改變
       PROOF_BASIS_CHANGED           — proof 定義或 proof tool 改變
       OUTPUT_ONLY_CHANGED           — 只有輸出不同
       UNKNOWN_OR_INCOMPLETE_LINEAGE — lineage 不完整，比較無法確認
    """
    suggestion: str | None = None
    """可執行的修正建議。已預先在 message 內 concat（12-9h3）；本欄位是
    結構化版本供 consumer 直接取用，不必再 string parse。"""
    context: dict[str, str] = Field(default_factory=dict)
    """額外 context（command / cwd / runfile_path / producer_job_id 等）。
    Consumer 可直接讀，不必再從 message 解析。"""


class CheckResult(BaseModel):
    run_file: str
    checked_stage: str
    target_anchors: dict[str, str | None]   # {"rtl": sha256|None, "sdc": ..., "upf": ...}
    status: LineageStatus
    findings: list[Finding]

    # ── 一句話摘要（給 NotificationBar / prompt 用）─
    summary: str = ""
    """整體檢查的一句話結論。空字串 = 12-9h3a 之前的舊 CheckResult。"""
