# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""每個 session 的外送上下文 epoch：可外送歷史只能來自目前這一個 epoch。

epoch 在「先前有效的授權被撤銷」時結束（下一個 epoch 從空的可外送歷史開始）：

- `/unbind-run` 移除綁定，或 `/bind-run` 替換掉既有綁定；
- 先前有效的綁定不再有效：Profile／policy 檔變更使綁定失效、綁定 Run 的權威列被判污染
  （`run_binding.session_binding_state` 的觀察結果撤銷了上次觀察中的有效項目，見 `revokes`）；
- 本 turn 的 evidence 被最終 Gate 拒絕（`EVIDENCE_*`）；
- Profile 或外送 policy 權威在任何讀取點讀不到、無法驗證，或在 turn 內被換成另一份；
- turn 之間 Profile 改變，或外送 policy 的授權範圍改變（`structural_c2` 的任何 revision 改變；
  `exact_digest` 除 digest 白名單以外的任何欄位改變——只增刪白名單不算）；
- session 結束或連線中斷（registry revoke）。

只新增綁定（例如尚未綁定時第一次 `/bind-run`）不撤銷任何授權，epoch 不結束：未綁定期間的
對話不含任何綁定 Run 的 evidence，可以留在可外送歷史中。失效的綁定之後恢復有效（例如 policy
檔改回相同 bytes）不會讓已結束的 epoch 復活。

epoch 識別只存在 daemon 記憶體：程序內全域遞增、不重複使用，daemon 重啟即全部消失
（對話緩衝區同樣只存在記憶體）。這裡只決定「哪些回合仍可外送」；本機顯示歷史與已送出
的 bytes 不受影響，也無法召回。
"""
from __future__ import annotations

import itertools
import secrets
import threading
from dataclasses import dataclass
from typing import Any

REASON_FIRST_USE = "first_use"
REASON_RUN_BOUND = "run_bound"
REASON_RUN_UNBOUND = "run_unbound"
REASON_BINDING_STATE_CHANGED = "binding_state_changed"
REASON_RUN_REPLACED = "run_replaced"
REASON_EVIDENCE_DENIED = "evidence_denied"
REASON_REVOKED_AT_GATE = "revoked_at_final_gate"
REASON_AUTHORITY_UNAVAILABLE = "authority_unavailable"

_lock = threading.Lock()
#: 同一 daemon 程序內不同 epoch 的識別永不相同；前綴避免與前一個程序的值混淆。
_process_nonce = secrets.token_hex(4)
_counter = itertools.count(1)


@dataclass
class _State:
    epoch: str
    #: 上次觀察到的綁定狀態；None 表示本 epoch 尚未觀察過（下一次觀察只記錄、不推進）。
    fingerprint: tuple[Any, ...] | None
    reason: str


_states: dict[str, _State] = {}


def _new_epoch_id() -> str:
    return f"ctx-{_process_nonce}-{next(_counter)}"


def current(session_id: str) -> str:
    """目前 epoch；session 第一次使用時建立。"""
    with _lock:
        state = _states.get(session_id)
        if state is None:
            state = _states[session_id] = _State(_new_epoch_id(), None, REASON_FIRST_USE)
        return state.epoch


def peek(session_id: str) -> str | None:
    """目前 epoch；不存在時回傳 None（不建立）。最終 Gate 以此判斷 turn 期間是否已被撤銷。"""
    with _lock:
        state = _states.get(session_id)
        return state.epoch if state is not None else None


def advance(session_id: str, reason: str) -> str:
    """結束目前 epoch 並開始新的一個；回傳新 epoch。"""
    with _lock:
        state = _State(_new_epoch_id(), None, reason)
        _states[session_id] = state
        return state.epoch


def revokes(previous: tuple[Any, ...] | None, current: tuple[Any, ...]) -> bool:
    """`previous` 中每個有效（state=`valid`）的綁定，在 `current` 是否仍以相同 run／attempt／
    content hash 維持有效；任一不再有效即為撤銷。指紋項目為 `(run_id, attempt, content_hash, state)`。"""
    if not previous:
        return False
    still_valid = {(run, attempt, digest) for run, attempt, digest, state in current if state == "valid"}
    return any(state == "valid" and (run, attempt, digest) not in still_valid
               for run, attempt, digest, state in previous)


def advance_if_current(session_id: str, epoch: str, reason: str) -> bool:
    """只有目前 epoch 仍是 `epoch` 時才結束它；比對與替換在同一次取鎖內完成。回傳是否推進。"""
    with _lock:
        state = _states.get(session_id)
        if state is None or state.epoch != epoch:
            return False
        _states[session_id] = _State(_new_epoch_id(), None, reason)
        return True


def observe(session_id: str, fingerprint: tuple[Any, ...]) -> tuple[str, bool]:
    """以 turn 開始時的綁定狀態更新 epoch；先前有效的綁定被撤銷（`revokes`）才推進。

    回傳 `(epoch, advanced)`。本 epoch 第一次觀察只記錄狀態（`unbind`／替換已在改變當下推進過
    epoch）；沒有撤銷時只更新記錄的狀態（例如新增綁定），epoch 不變。
    """
    with _lock:
        state = _states.get(session_id)
        if state is None:
            state = _states[session_id] = _State(_new_epoch_id(), fingerprint, REASON_FIRST_USE)
            return state.epoch, False
        if state.fingerprint is None:
            state.fingerprint = fingerprint
            return state.epoch, False
        if not revokes(state.fingerprint, fingerprint):
            state.fingerprint = fingerprint
            return state.epoch, False
        state = _State(_new_epoch_id(), fingerprint, REASON_BINDING_STATE_CHANGED)
        _states[session_id] = state
        return state.epoch, True


def last_reason(session_id: str) -> str | None:
    """目前 epoch 的開始原因（診斷用，不含任何內容）。"""
    with _lock:
        state = _states.get(session_id)
        return state.reason if state is not None else None


def forget(session_id: str, peer_uid: int | None = None) -> None:
    """session 結束／連線中斷：移除該 session 的 epoch；之後的同 id 請求不會對上舊 epoch。"""
    with _lock:
        _states.pop(session_id, None)


def clear() -> None:
    """daemon 關閉或測試重設。"""
    with _lock:
        _states.clear()


__all__ = [
    "REASON_AUTHORITY_UNAVAILABLE", "REASON_BINDING_STATE_CHANGED", "REASON_EVIDENCE_DENIED", "REASON_FIRST_USE",
    "REASON_REVOKED_AT_GATE", "REASON_RUN_BOUND",
    "REASON_RUN_REPLACED", "REASON_RUN_UNBOUND", "advance", "advance_if_current", "clear", "current", "forget",
    "last_reason", "observe",
    "peek", "revokes",
]
