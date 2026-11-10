# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import copy
from pathlib import Path

from terminux_engine.lineage.models import ArtifactSnapshot
from terminux_engine.lineage.types import ANCHOR_KEYS, ROOT_ANCHOR_ROLES
from terminux_engine.lineage import ledger as ledger_mod


def attach_lineage(
    snap: ArtifactSnapshot,
    ledger_path: str | Path,
) -> ArtifactSnapshot:
    """
    查 ledger，找 snap.path 對應的 producer commit，填入 lineage 欄位。
    不修改傳入的 snap，回傳新物件。
    """
    commit, out_snap = ledger_mod.find_latest_commit_by_output_path(
        ledger_path, snap.path
    )

    if commit is not None and out_snap is not None:
        return snap.model_copy(update={
            "lineage_source": "producer_commit",
            "producer_job_id": commit.job_id,
            "producer_stage": commit.stage,
            "lineage_anchors": copy.deepcopy(out_snap.lineage_anchors),
        })

    # 找不到 producer — 根據 role 決定 anchor
    if snap.role in ROOT_ANCHOR_ROLES:
        anchors = {k: ([snap.checksum_sha256] if k == snap.role else []) for k in ANCHOR_KEYS}
    else:
        anchors = {k: [] for k in ANCHOR_KEYS}

    return snap.model_copy(update={
        "lineage_source": "root_observation",
        "producer_job_id": None,
        "producer_stage": None,
        "lineage_anchors": anchors,
    })


def merge_anchor_maps(snapshots: list[ArtifactSnapshot]) -> dict[str, list[str]]:
    """合併多個 snapshot 的 lineage_anchors，每個 key 取 union（sorted + deduplicated）。"""
    merged: dict[str, set[str]] = {k: set() for k in ANCHOR_KEYS}
    for snap in snapshots:
        for key in ANCHOR_KEYS:
            merged[key].update(snap.lineage_anchors.get(key, []))
    return {k: sorted(merged[k]) for k in ANCHOR_KEYS}
