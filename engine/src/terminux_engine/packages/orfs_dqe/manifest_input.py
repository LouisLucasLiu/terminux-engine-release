# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Manifest-driven input。

`orfs_dqe` 的 artifact 來源**只有** artifact lineage 的 immutable manifest
。本模組負責：

1. 從 `TimelineEvent.metadata["artifact_lineage"]` 取得 manifest identity；
2. 讀回 durable manifest 並驗證 identity／canonical SHA／schema／producer／
   output boundary；
3. 依 `adapter_role` 選取 input observation。

**不做** cwd fallback scan、不重新掃描 ORFS results/reports/logs tree、
不因檔名相同就跳過 role。整段重用 artifact lineage 既有的 canonical serialization 與
manifest model，不另做一套 hash 或 schema 解析。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.lineage.artifact_manifest import (
    AVAILABILITY_PRESENT,
    MANIFEST_SCHEMA_VERSION,
    STABILITY_STABLE,
    ArtifactManifest,
    ArtifactObservation,
)
from terminux_engine.lineage.artifact_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
)

LINEAGE_METADATA_KEY = "artifact_lineage"
ORCHESTRATION_METADATA_KEY = "orchestration"

#: Kernel 建立 TimelineEvent 時填入的 source（見 `orchestration/kernel.py`
#: `_build_timeline_event()`）。只有這個 source 的 ORFS event 才被視為
#: 「應該帶有 manifest」的 production event。
KERNEL_EVENT_SOURCE = "orchestration.kernel"


class ManifestInputError(RuntimeError):
    """Manifest 無法取得或無法信任——evaluator 層級失敗，不是品質問題。"""


class ManifestNotApplicable(RuntimeError):
    """這個 event 不該由本 package 評估（非 production source 等）。"""


@dataclass(frozen=True)
class ManifestInput:
    """通過完整 integrity 驗證後的 manifest 輸入。"""

    manifest: ArtifactManifest
    manifest_reference: str
    manifest_sha256: str
    capture_status: str
    producer: dict[str, Any]

    def observations_by_role(self) -> dict[str, list[ArtifactObservation]]:
        grouped: dict[str, list[ArtifactObservation]] = {}
        for observation in self.manifest.observations:
            grouped.setdefault(observation.adapter_role, []).append(observation)
        return grouped


def lineage_metadata(event: Any) -> dict[str, Any] | None:
    raw = (event.metadata or {}).get(LINEAGE_METADATA_KEY)
    return raw if isinstance(raw, dict) else None


def orchestration_metadata(event: Any) -> dict[str, Any]:
    raw = (event.metadata or {}).get(ORCHESTRATION_METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def is_kernel_production_event(event: Any) -> bool:
    """只有 Kernel 產生的 event 才是「應該帶 manifest」的 production event。

    Shell hook／daemon 重放等其他 source 的 `tool_name=orfs` event 沒有
    Kernel-managed 的 artifact capture，對它們評估 DQE 只會產生重複或
    無證據的結果，因此一律 `noop`。
    """
    return getattr(event, "source", None) == KERNEL_EVENT_SOURCE


def load_manifest_input(event: Any) -> ManifestInput:
    """讀取並完整驗證 manifest；任何不一致都 fail closed。"""
    if not is_kernel_production_event(event):
        raise ManifestNotApplicable(
            f"event source {getattr(event, 'source', None)!r} is not a Kernel production event"
        )

    lineage = lineage_metadata(event)
    if lineage is None:
        raise ManifestInputError(
            "kernel production event carries no artifact_lineage metadata"
        )

    manifest_id = lineage.get("artifact_manifest_id")
    reference = lineage.get("artifact_manifest_reference")
    expected_sha = lineage.get("artifact_manifest_sha256")
    capture_status = lineage.get("artifact_capture_status")

    if not manifest_id or not reference or not expected_sha:
        raise ManifestInputError(
            "artifact_lineage metadata is incomplete: "
            f"id={manifest_id!r} reference={reference!r} sha256={expected_sha!r}"
        )

    path = Path(str(reference))
    if not path.is_file():
        raise ManifestInputError(f"manifest reference is not a regular file: {path}")

    payload_bytes = path.read_bytes()
    actual_sha = manifest_sha256(payload_bytes)
    if actual_sha != expected_sha:
        raise ManifestInputError(
            "manifest SHA-256 mismatch (tampered or stale reference): "
            f"expected={expected_sha} actual={actual_sha}"
        )

    import json

    try:
        manifest = ArtifactManifest.from_json_dict(json.loads(payload_bytes.decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 - 任何解析失敗都是 evaluator 失敗
        raise ManifestInputError(f"manifest is unreadable: {exc!r}") from exc

    # Round-trip：確認我們解讀出來的物件與磁碟上的 canonical bytes 等價。
    if manifest_sha256(canonical_manifest_bytes(manifest)) != actual_sha:
        raise ManifestInputError(
            "manifest does not round-trip to its persisted canonical bytes"
        )

    if manifest.manifest_id != manifest_id:
        raise ManifestInputError(
            f"manifest id mismatch: metadata={manifest_id} manifest={manifest.manifest_id}"
        )

    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestInputError(
            f"unsupported manifest schema_version: {manifest.schema_version}"
        )

    _verify_producer(manifest, event)
    _verify_boundaries(manifest)

    return ManifestInput(
        manifest=manifest,
        manifest_reference=str(path),
        manifest_sha256=actual_sha,
        capture_status=str(capture_status) if capture_status else "unknown",
        producer=manifest.producer.to_json_dict(),
    )


def _verify_producer(manifest: ArtifactManifest, event: Any) -> None:
    """Manifest 的 producer 必須與 event 攜帶的 orchestration identity 相同。"""
    orchestration = orchestration_metadata(event)
    if not orchestration:
        raise ManifestInputError("event carries no orchestration metadata to cross-check")

    producer = manifest.producer
    mismatches: list[str] = []
    if orchestration.get("run_id") != producer.run_id:
        mismatches.append(f"run_id({orchestration.get('run_id')}!={producer.run_id})")
    if orchestration.get("step_id") != producer.step_id:
        mismatches.append(f"step_id({orchestration.get('step_id')}!={producer.step_id})")
    if orchestration.get("attempt") != producer.attempt:
        mismatches.append(f"attempt({orchestration.get('attempt')}!={producer.attempt})")
    event_tool_name = getattr(event, "tool_name", None)
    if event_tool_name != producer.event_tool_name:
        mismatches.append(
            f"event_tool_name({event_tool_name}!={producer.event_tool_name})"
        )
    if mismatches:
        raise ManifestInputError(
            "manifest producer does not match event identity: " + ", ".join(mismatches)
        )


def _verify_boundaries(manifest: ArtifactManifest) -> None:
    """每個已解析的 observation 必須落在 manifest 宣告的 output boundary 內。"""
    boundaries = [Path(b) for b in manifest.output_boundaries]
    if not boundaries:
        raise ManifestInputError("manifest declares no output boundaries")
    for observation in manifest.observations:
        if observation.resolved_path is None:
            continue
        resolved = Path(observation.resolved_path)
        if not any(_is_within(resolved, boundary) for boundary in boundaries):
            raise ManifestInputError(
                f"observation {observation.observation_id} escapes declared output "
                f"boundaries: {observation.resolved_path}"
            )


def _is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class RoleSelection:
    """一個 required role 的選取結果。"""

    role: str
    observation: ArtifactObservation | None
    problem: str | None = None

    @property
    def usable(self) -> bool:
        """可以拿來 parse 的條件：唯一、present 且 stable。"""
        return (
            self.problem is None
            and self.observation is not None
            and self.observation.availability == AVAILABILITY_PRESENT
            and self.observation.stability == STABILITY_STABLE
            and self.observation.resolved_path is not None
        )


def select_role(manifest_input: ManifestInput, role: str) -> RoleSelection:
    """依 `adapter_role` 選一個 observation；重複視為 ambiguous，不任選第一筆。"""
    matches = manifest_input.observations_by_role().get(role, [])
    if not matches:
        return RoleSelection(role=role, observation=None, problem="role_missing")
    if len(matches) > 1:
        return RoleSelection(
            role=role,
            observation=None,
            problem=f"role_ambiguous:{len(matches)}_observations",
        )
    observation = matches[0]
    if observation.availability != AVAILABILITY_PRESENT:
        return RoleSelection(
            role=role,
            observation=observation,
            problem=f"availability:{observation.availability}",
        )
    if observation.stability != STABILITY_STABLE:
        return RoleSelection(
            role=role,
            observation=observation,
            problem=f"stability:{observation.stability}",
        )
    if observation.resolved_path is None:
        return RoleSelection(role=role, observation=observation, problem="unresolved_path")
    return RoleSelection(role=role, observation=observation)


def unknown_roles(manifest_input: ManifestInput, known_roles: list[str]) -> list[str]:
    """Manifest 中有、但 policy 不認得的 role——記錄但不讓 v1 崩潰。"""
    known = set(known_roles)
    return sorted(
        {
            observation.adapter_role
            for observation in manifest_input.manifest.observations
            if observation.adapter_role not in known
        }
    )


__all__ = [
    "KERNEL_EVENT_SOURCE",
    "LINEAGE_METADATA_KEY",
    "ManifestInput",
    "ManifestInputError",
    "ManifestNotApplicable",
    "RoleSelection",
    "is_kernel_production_event",
    "lineage_metadata",
    "load_manifest_input",
    "orchestration_metadata",
    "select_role",
    "unknown_roles",
]
