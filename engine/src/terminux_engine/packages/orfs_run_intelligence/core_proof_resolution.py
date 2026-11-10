# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""由 durable 事件找回 CORE 比較要用的那一份 pair-proof envelope。

為什麼需要它
------------

CORE 的 ORFS 執行與 formal proof 是**兩個** FlowTask：工具不同、額度不同、
失敗模式也不同。比較要成立，就必須在判定的當下，從已持久化的事件裡重新找回
「這一次 Work／attempt、這一對 netlist」的 proof envelope。

因此這裡刻意**不接受**：

* 記憶體裡的 live 物件——daemon 重啟後就沒有了；
* 呼叫端（harness 或 user）傳進來的 envelope 路徑——那等於讓外部決定比較結果。

唯一的輸入是 durable step result，唯一的比對依據是 durable identity。

`resolve_pair_proof()` 在找到**多於一份**互相矛盾的候選時拒絕，而不是挑最新
的一份：同一對輸入上出現兩個不同結論，代表證據本身有問題。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 相依 proof task 的 step result 標記。
PROOF_BUSINESS_CLASS = "formal_pair_proof"

#: 本版 launch identity 綁定的欄位。
IDENTITY_FIELDS: tuple[str, ...] = (
    "work_id", "attempt_id", "proof_descriptor_sha256",
    "baseline_sha256", "candidate_sha256",
    "input_manifest_sha256", "input_tree_hash")
#: 本版新增的欄位；舊資料沒有它們。
IDENTITY_FIELDS_ADDED: tuple[str, ...] = (
    "baseline_sha256", "candidate_sha256",
    "input_manifest_sha256", "input_tree_hash")

CURRENT_IDENTITY = "CURRENT"
LEGACY_IDENTITY = "LEGACY"


class PairProofResolutionError(RuntimeError):
    """找不到、有歧義或形狀錯誤。一律不放行比較。"""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


def classify_proof_identity(identity: Any) -> str:
    """這份 launch identity 是本版的，還是缺欄位的舊資料。

    舊資料**明確**判成 `LEGACY`，不補預設值：補值會讓兩種不同的輸入算出同一個
    digest，去重就會把它們當成同一次 launch。
    """
    if not isinstance(identity, dict):
        return LEGACY_IDENTITY
    if any(identity.get(name) in (None, "") for name in IDENTITY_FIELDS_ADDED):
        return LEGACY_IDENTITY
    return CURRENT_IDENTITY


def _envelope_of(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("business_class") not in (None, PROOF_BUSINESS_CLASS):
        return None
    envelope = payload.get("proof_envelope")
    return envelope if isinstance(envelope, dict) else None


def _matches(envelope: dict[str, Any], *, work_id: str, attempt_id: str,
             descriptor_sha256: str, baseline_netlist_sha256: str,
             candidate_netlist_sha256: str) -> bool:
    task = envelope.get("task") or {}
    tool_result = envelope.get("tool_result") or {}
    baseline = (tool_result.get("baseline") or {}).get("sha256")
    candidate = (tool_result.get("candidate") or {}).get("sha256")
    return (str(task.get("work_id") or "") == str(work_id)
            and str(task.get("attempt_id") or "") == str(attempt_id)
            and str(envelope.get("proof_descriptor_sha256") or "") == str(descriptor_sha256)
            and str(baseline or "") == str(baseline_netlist_sha256)
            and str(candidate or "") == str(candidate_netlist_sha256))


def resolve_pair_proof(
    step_results: Iterable[Any], *, work_id: str, attempt_id: str,
    descriptor_sha256: str, baseline_netlist_sha256: str,
    candidate_netlist_sha256: str,
) -> tuple[dict[str, Any], bool]:
    """回傳 `(envelope, qualification_only)`；找不到或有歧義就拋。

    `step_results` 是 durable step result 的 payload（`full_result`）。它們由
    EventStore 讀回，因此重啟之後仍然找得到同一份 envelope。
    """
    matched: list[tuple[dict[str, Any], bool]] = []
    for payload in step_results:
        envelope = _envelope_of(payload)
        if envelope is None:
            continue
        if not _matches(envelope, work_id=work_id, attempt_id=attempt_id,
                        descriptor_sha256=descriptor_sha256,
                        baseline_netlist_sha256=baseline_netlist_sha256,
                        candidate_netlist_sha256=candidate_netlist_sha256):
            continue
        matched.append((envelope, bool(payload.get("qualification_only", True))))

    if not matched:
        raise PairProofResolutionError(
            "no durable pair-proof envelope covers this Work, attempt and "
            "netlist pair; the CORE comparison is not released",
            work_id=work_id, attempt_id=attempt_id,
            baseline_sha256=baseline_netlist_sha256,
            candidate_sha256=candidate_netlist_sha256)

    digests = {envelope.get("durable_envelope_sha256") for envelope, _ in matched}
    if len(digests) != 1:
        raise PairProofResolutionError(
            "more than one distinct pair-proof envelope covers the same netlist "
            "pair; the evidence is ambiguous and the comparison is not released",
            envelopes=sorted(str(row) for row in digests))
    classifications = {envelope.get("classification") for envelope, _ in matched}
    if len(classifications) != 1:
        raise PairProofResolutionError(
            "the durable proof records disagree on the classification",
            classifications=sorted(str(row) for row in classifications))
    return matched[0]


def step_result_payloads(event_store: Any, *, member_id: str,
                         event_type: str) -> list[dict[str, Any]]:
    """把 scope 內的 step result payload 取出來，只回 `full_result` 的部分。"""
    payloads: list[dict[str, Any]] = []
    for event in event_store.query(user_id=member_id, event_type=event_type,
                                   order="asc"):
        envelope = (event.payload or {}).get("envelope")
        if not isinstance(envelope, dict):
            continue
        full = envelope.get("full_result")
        if isinstance(full, dict):
            payloads.append(full)
    return payloads


__all__ = [
    "CURRENT_IDENTITY", "IDENTITY_FIELDS", "IDENTITY_FIELDS_ADDED",
    "LEGACY_IDENTITY", "PROOF_BUSINESS_CLASS", "PairProofResolutionError",
    "classify_proof_identity", "resolve_pair_proof", "step_result_payloads",
]
