# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Capability Contract v1。

**不是**第二份 action registry。identity、description、input schema 與 risk
一律從呼叫端傳進來的既有 Tool Registry entry 取得；Package 只能**補充**
planning 用的 domain metadata，不能改名、改 schema，也不能降低 risk
（設計規則）。

Common 這一層完全不認識任何工具名稱、檔名或 metric：
`preconditions`／`evidence_produced`／`side_effects` 對它而言只是要原樣搬運
與做結構驗證的不透明字串。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

CAPABILITY_SCHEMA_VERSION = 1

RISK_SAFE = "safe"
RISK_DANGEROUS = "dangerous"
_RISKS = (RISK_SAFE, RISK_DANGEROUS)

IDEMPOTENCY_VALUES = ("read_only", "idempotent", "non_idempotent", "unknown")
AVAILABILITY_STATES = ("available", "unavailable", "validation_deferred")

#: 永遠不得進入 plan-only catalog 的 capability。這是硬編碼的**排除**清單，
#: 不是能力清單——它只會讓 catalog 變小，不會讓任何東西變成可用。
FORBIDDEN_CAPABILITY_IDS: frozenset[str] = frozenset({"run_command"})

#: Package／tool owner 只允許補充這些欄位。其餘一律忽略並記成 conflict。
_METADATA_FIELDS: frozenset[str] = frozenset({
    "side_effects", "preconditions", "evidence_produced", "stop_signals",
    "idempotency", "availability",
})

#: 這些欄位由 authority 決定，Package 若嘗試提供就是 conflict。
_AUTHORITY_ONLY_FIELDS: frozenset[str] = frozenset({
    "capability_id", "description", "input_schema", "risk", "approval_required",
})

_MAX_LIST_ITEMS = 12
_MAX_ITEM_CHARS = 400


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            out.append(text[:_MAX_ITEM_CHARS])
        if len(out) >= _MAX_LIST_ITEMS:
            break
    return out


@dataclass(frozen=True)
class Capability:
    """一個 capability 的 normalized planning view。"""

    schema_version: int
    capability_id: str
    description: str
    input_schema: dict[str, Any]
    risk: str
    approval_required: bool
    side_effects: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    evidence_produced: tuple[str, ...] = ()
    stop_signals: tuple[str, ...] = ()
    idempotency: str = "unknown"
    availability: dict[str, Any] = field(default_factory=lambda: {"state": "available",
                                                                 "limitations": []})
    provenance: dict[str, Any] = field(default_factory=dict)
    conflicts: tuple[dict[str, Any], ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk,
            "approval_required": self.approval_required,
            "side_effects": list(self.side_effects),
            "preconditions": list(self.preconditions),
            "evidence_produced": list(self.evidence_produced),
            "stop_signals": list(self.stop_signals),
            "idempotency": self.idempotency,
            "availability": dict(self.availability),
            "provenance": dict(self.provenance),
            "conflicts": [dict(c) for c in self.conflicts],
        }


@dataclass(frozen=True)
class CapabilitySnapshot:
    """一次 planning 使用的 bounded catalog，帶 canonical identity。"""

    schema_version: int
    snapshot_id: str
    capabilities: tuple[Capability, ...]
    excluded: tuple[dict[str, Any], ...]
    provenance: dict[str, Any]

    def get(self, capability_id: str) -> Capability | None:
        for c in self.capabilities:
            if c.capability_id == capability_id:
                return c
        return None

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(c.capability_id for c in self.capabilities)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "capability_count": len(self.capabilities),
            "capabilities": [c.to_json_dict() for c in self.capabilities],
            "excluded": [dict(e) for e in self.excluded],
            "provenance": dict(self.provenance),
        }


def build_capability_snapshot(
    *,
    registry_entries: Iterable[dict[str, Any]],
    exposed_capability_ids: Iterable[str],
    metadata_provider: Callable[[str], dict[str, Any] | None] | None = None,
    registry_snapshot_ref: str = "",
) -> CapabilitySnapshot:
    """把既有 registry entries normalize 成 bounded capability snapshot。

    `registry_entries` 每一筆至少要有 `capability_id`、`description`、
    `input_schema`、`risk`——它們**必須**由呼叫端從既有 Tool Registry
    取得，本函式不會、也無法自行發現任何 capability。

    `exposed_capability_ids` 是這個 plan profile 決定要暴露的集合；不在
    registry entries 裡的 id 一律被排除（provider 無權新增 action）。
    """
    exposed = list(dict.fromkeys(exposed_capability_ids))
    by_id = {e["capability_id"]: e for e in registry_entries if e.get("capability_id")}

    capabilities: list[Capability] = []
    excluded: list[dict[str, Any]] = []

    for capability_id in exposed:
        if capability_id in FORBIDDEN_CAPABILITY_IDS:
            excluded.append({"capability_id": capability_id, "reason": "forbidden_capability"})
            continue
        entry = by_id.get(capability_id)
        if entry is None:
            excluded.append({"capability_id": capability_id,
                             "reason": "not_present_in_tool_registry"})
            continue
        capabilities.append(_normalize(entry, metadata_provider, registry_snapshot_ref))

    for capability_id in sorted(set(by_id) - set(exposed)):
        excluded.append({
            "capability_id": capability_id,
            "reason": ("forbidden_capability" if capability_id in FORBIDDEN_CAPABILITY_IDS
                       else "not_exposed_by_plan_profile"),
        })

    identity = _canonical({
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "capabilities": [c.to_json_dict() for c in capabilities],
        "registry_snapshot_ref": registry_snapshot_ref,
    })
    return CapabilitySnapshot(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        snapshot_id=f"cap_{_sha256(identity)[:32]}",
        capabilities=tuple(capabilities),
        excluded=tuple(excluded),
        provenance={
            "assembler": "terminux_engine.planning.capability.build_capability_snapshot",
            "registry_snapshot_ref": registry_snapshot_ref,
            "authority": {
                "capability_id": "tool_registry",
                "description": "tool_registry",
                "input_schema": "tool_registry",
                "risk": "tool_registry_risk_classification",
                "approval_required": "derived_from_risk",
                "side_effects": "authority_base + owner_metadata (additive only)",
                "preconditions": "owner_metadata",
                "evidence_produced": "owner_metadata",
                "stop_signals": "owner_metadata",
                "idempotency": "owner_metadata (default unknown)",
                "availability": "owner_metadata (default available)",
            },
            "forbidden_capability_ids": sorted(FORBIDDEN_CAPABILITY_IDS),
        },
    )


def _normalize(
    entry: dict[str, Any],
    metadata_provider: Callable[[str], dict[str, Any] | None] | None,
    registry_snapshot_ref: str,
) -> Capability:
    capability_id = entry["capability_id"]
    risk = str(entry.get("risk") or "").strip().lower()
    if risk not in _RISKS:
        # 未知 risk 一律當成最嚴格的——絕不因為缺資料而放寬。
        risk = RISK_DANGEROUS
    approval_required = risk == RISK_DANGEROUS

    metadata: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    metadata_source_hash = None
    if metadata_provider is not None:
        try:
            raw = metadata_provider(capability_id)
        except Exception as exc:  # noqa: BLE001 - metadata 缺失不得讓 planning 崩潰
            raw = None
            conflicts.append({"field": "*", "reason": f"metadata provider failed: {exc!r}"})
        if isinstance(raw, dict):
            metadata_source_hash = _sha256(_canonical(raw))
            for key in sorted(raw):
                if key in _AUTHORITY_ONLY_FIELDS:
                    conflicts.append({
                        "field": key, "reason": "owner metadata may not set an authority field",
                        "authority_value": entry.get(
                            "risk" if key == "risk" else key,
                            approval_required if key == "approval_required" else None),
                        "ignored_owner_value": raw[key],
                    })
                elif key in _METADATA_FIELDS:
                    metadata[key] = raw[key]
                else:
                    conflicts.append({"field": key, "reason": "unknown planning metadata field"})

    idempotency = str(metadata.get("idempotency") or "unknown")
    if idempotency not in IDEMPOTENCY_VALUES:
        conflicts.append({"field": "idempotency", "reason": "unknown value",
                          "ignored_owner_value": metadata.get("idempotency")})
        idempotency = "unknown"

    availability_raw = metadata.get("availability")
    if isinstance(availability_raw, dict):
        state = str(availability_raw.get("state") or "available")
        if state not in AVAILABILITY_STATES:
            conflicts.append({"field": "availability.state", "reason": "unknown value",
                              "ignored_owner_value": state})
            state = "available"
        availability = {"state": state,
                        "limitations": _clean_list(availability_raw.get("limitations"))}
    else:
        availability = {"state": "available", "limitations": []}

    # Side effects：authority base 是必須保留的下限，owner 只能**增加**。
    authority_side_effects = _clean_list(entry.get("side_effects"))
    if risk == RISK_DANGEROUS and not authority_side_effects:
        authority_side_effects = [
            "classified DANGEROUS by the tool registry; treat as having side effects"
        ]
    owner_side_effects = _clean_list(metadata.get("side_effects"))
    side_effects = list(dict.fromkeys(authority_side_effects + owner_side_effects))

    return Capability(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        capability_id=capability_id,
        description=str(entry.get("description") or "").strip(),
        input_schema=dict(entry.get("input_schema") or {}),
        risk=risk,
        approval_required=approval_required,
        side_effects=tuple(side_effects),
        preconditions=tuple(_clean_list(metadata.get("preconditions"))),
        evidence_produced=tuple(_clean_list(metadata.get("evidence_produced"))),
        stop_signals=tuple(_clean_list(metadata.get("stop_signals"))),
        idempotency=idempotency,
        availability=availability,
        provenance={
            "tool_registry_snapshot": registry_snapshot_ref,
            "package_id": entry.get("package_id"),
            "package_version": entry.get("package_version"),
            "metadata_source_hash": metadata_source_hash,
        },
        conflicts=tuple(conflicts),
    )


__all__ = [
    "AVAILABILITY_STATES",
    "CAPABILITY_SCHEMA_VERSION",
    "Capability",
    "CapabilitySnapshot",
    "FORBIDDEN_CAPABILITY_IDS",
    "IDEMPOTENCY_VALUES",
    "RISK_DANGEROUS",
    "RISK_SAFE",
    "build_capability_snapshot",
]
