# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""每個 session 可見的 Run 綁定：receipt 只是定位器，EventStore 投影才是授權依據。

綁定成立的條件全部由 host 驗證：Profile 內有管理端明示的 `run_binding` 規則、
receipt 檔為本人可讀的絕對路徑普通檔、`sha256(檔案 bytes)` 等於 EventStore 投影的
`source_receipt_sha256`、投影的 content hash 可重算一致、`run_id`／`attempt` 相符、
receipt 的 runner／platform／design／年齡落在規則內、session 綁定數未超過上限。
任何一項不成立就拒絕，沒有部分綁定。

綁定只存在 daemon 記憶體，隨 `session.end`／連線中斷的 registry revoke 清除，daemon
重啟即消失；沒有時間後備 TTL（管理值未定前不提供）。綁定記下當時的 Profile 與
policy 檔 SHA256，兩者任一改變即失效（下一個 turn 起不再列入有效 Run）。

移除、替換或失效任何先前有效的綁定，都會結束本 session 的外送上下文 epoch
（`llm.context_epoch`）：前一個 epoch 的回合（含由 evidence 衍生的回答）不再進入可外送歷史。
只新增綁定不結束 epoch。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.ipc.request_context import CanonicalRequestContext, get_current_request_context
from terminux_engine.llm import context_epoch
from terminux_engine.llm.task_scope import TaskProfile, policy_identity
from terminux_engine.orchestration.safe_file import read_regular_file

MAX_RECEIPT_BYTES = 2 * 1024 * 1024

CODE_DISABLED = "RUN_BIND_DISABLED"
CODE_USAGE = "RUN_BIND_USAGE"
CODE_UNSUPPORTED = "RUN_BIND_UNSUPPORTED"
CODE_RECEIPT_UNREADABLE = "RUN_BIND_RECEIPT_UNREADABLE"
CODE_RECEIPT_INVALID = "RUN_BIND_RECEIPT_INVALID"
CODE_NOT_PROJECTED = "RUN_BIND_NOT_PROJECTED"
CODE_AMBIGUOUS = "RUN_BIND_AMBIGUOUS"
CODE_ATTEMPT_MISMATCH = "RUN_BIND_ATTEMPT_MISMATCH"
CODE_OUTSIDE_POLICY = "RUN_BIND_OUTSIDE_POLICY"
CODE_LIMIT = "RUN_BIND_LIMIT"
CODE_AUTHORITY_UNAVAILABLE = "RUN_BIND_AUTHORITY_UNAVAILABLE"
CODE_FOREIGN_OR_UNTRUSTED = "RUN_BIND_FOREIGN_OR_UNTRUSTED"
CODE_POLICY_UNAVAILABLE = "RUN_BIND_POLICY_UNAVAILABLE"
CODE_BOUND = "RUN_BOUND"
CODE_UNBOUND = "RUN_UNBOUND"
CODE_NOT_BOUND = "RUN_NOT_BOUND"

#: `session_binding_state` 的單一綁定狀態值。
STATE_VALID = "valid"
STATE_STALE = "stale_profile_or_policy"
STATE_TAINTED = "tainted"
STATE_UNAVAILABLE = "authority_unavailable"
#: `~policy`（revision）身分列的狀態：只供 turn 內比對「turn 開始時與之後讀到的是否同一份」，
#: 不參與跨 turn 的撤銷判定（`context_epoch.revokes` 只看 `valid` 列）；跨 turn 改看 `~policy_scope`。
STATE_TURN_IDENTITY = "turn_identity"
#: epoch 指紋中代表 Profile 與 policy 身分的列（不是 Run）；最終 Gate 只比對綁定列。
IDENTITY_ROW_PREFIX = "~"


class _NoAuthority:
    """沒有可用的 EventStore 時的 projector：每次核對都得到 `RUN_BIND_AUTHORITY_UNAVAILABLE`。"""


#: production 入口在 EventStore 不可用時傳入，讓核對 fail-closed 而不是被略過。
AUTHORITY_UNAVAILABLE = _NoAuthority()


class ReceiptUnreadable(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Binding:
    session_id: str
    member_id: str
    run_id: str
    attempt: int
    profile_sha256: str
    policy_sha256: str
    bound_at: float
    #: 綁定當下權威投影列的 content hash；之後的 evidence 必須與它一致。
    content_hash: str = ""
    #: 綁定所用 receipt 檔 bytes 的 SHA256；turn 開始時據此重新核對權威列。
    receipt_sha256: str = ""


@dataclass(frozen=True)
class BindResult:
    ok: bool
    code: str
    message: str
    run_id: str | None = None
    attempt: int | None = None


_lock = threading.Lock()
_bindings: dict[str, list[Binding]] = {}
#: 已判定權威列不再一致的綁定（session, member, run, attempt, content_hash）。
#: 只增不減：同一綁定一旦被判污染，之後的每次觀察都維持 tainted。
_tainted: set[tuple[str, str, str, int, str]] = set()


def _result(code: str, detail: str, *, ok: bool = False, run_id: str | None = None,
            attempt: int | None = None) -> BindResult:
    return BindResult(ok, code, f"{code}: {detail}", run_id, attempt)


def _candidate(document: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    run_id = document.get("run_id")
    attempt = document.get("attempt")
    metadata = document.get("adapter_metadata")
    if (not isinstance(run_id, str) or not run_id.startswith("run_")
            or not run_id.replace("_", "").isalnum() or type(attempt) is not int or attempt < 1
            or not isinstance(metadata, dict)):
        raise ReceiptUnreadable(CODE_RECEIPT_INVALID)
    return run_id, attempt, metadata


def read_receipt(path_text: str) -> tuple[bytes, dict[str, Any]]:
    """以逐段 `O_NOFOLLOW` 讀取 receipt；回傳原始 bytes 與解析後的文件。"""
    if "\x00" in path_text or not path_text.startswith("/"):
        raise ReceiptUnreadable(CODE_RECEIPT_UNREADABLE)
    raw = read_regular_file(Path(path_text), MAX_RECEIPT_BYTES,
                            lambda reason: ReceiptUnreadable(CODE_RECEIPT_UNREADABLE))
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ReceiptUnreadable(CODE_RECEIPT_INVALID) from exc
    if not isinstance(document, dict):
        raise ReceiptUnreadable(CODE_RECEIPT_INVALID)
    return raw, document


def _projection_row(projector: Any, run_id: str, attempt: int, receipt_sha256: str,
                    member_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """從 EventStore 投影找出唯一一筆同 run／attempt、本人寫入且 content hash 自洽的紀錄。

    同一個 run_id 下只要有任何一列不是本人（`user_id != member_id`）或重算的
    content hash 不一致，整個 Run 即拒絕（`RUN_BIND_FOREIGN_OR_UNTRUSTED`）：
    對應 Evidence Resolver 的 `foreign_user_receipt_count`／`untrusted_receipt_count`
    必須為 0。projector 無法提供寫入身分時視為權威不可用，不放行。
    """
    from terminux_engine.execution_receipts.projection import content_hash_of

    query = getattr(projector, "query_rows_by_run_id", None)
    if query is None:
        return None, CODE_AUTHORITY_UNAVAILABLE
    try:
        entries = list(query(run_id))
    except Exception:  # noqa: BLE001 - invalidation authority 或 store 讀不到都不得靜默放行
        return None, CODE_AUTHORITY_UNAVAILABLE
    if not entries:
        return None, CODE_NOT_PROJECTED
    rows: list[dict[str, Any]] = []
    for entry in entries:
        payload = getattr(entry, "payload", None)
        if (not isinstance(payload, dict) or getattr(entry, "user_id", None) != member_id
                or content_hash_of(payload) != payload.get("content_hash")):
            return None, CODE_FOREIGN_OR_UNTRUSTED
        rows.append(payload)
    matching = [row for row in rows if row.get("attempt") == attempt]
    if not matching:
        return None, CODE_ATTEMPT_MISMATCH
    if len(matching) > 1:
        return None, CODE_AMBIGUOUS
    row = matching[0]
    if row.get("source_receipt_sha256") != receipt_sha256:
        return None, CODE_RECEIPT_INVALID
    return row, None


def bind(*, receipt_path: str, profile: TaskProfile, projector: Any,
         context: CanonicalRequestContext | None = None, now: float | None = None) -> BindResult:
    """驗證並綁定一個 Run 到目前 session；每個失敗都有固定碼且不改變任何狀態。"""
    policy = profile.run_binding
    if policy is None:
        return _result(CODE_DISABLED, "this administrator profile does not permit run binding.")
    identity = policy_identity()
    if identity is None:
        return _result(CODE_POLICY_UNAVAILABLE, "the outbound policy is not readable; binding is refused.")
    context = context if context is not None else get_current_request_context()
    if receipt_path.strip() == "last":
        return _result(CODE_UNSUPPORTED, "'last' is not supported; give an absolute receipt path.")
    try:
        raw, document = read_receipt(receipt_path.strip())
        run_id, attempt, metadata = _candidate(document)
    except ReceiptUnreadable as exc:
        return _result(exc.reason, "the receipt could not be read as a valid execution receipt.")
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    if projector is None:
        return _result(CODE_AUTHORITY_UNAVAILABLE, "the EventStore projection is not available.")
    row, failure = _projection_row(projector, run_id, attempt, receipt_sha256, context.member_id)
    if failure is not None or row is None:
        return _result(failure or CODE_NOT_PROJECTED,
                       f"{run_id} attempt {attempt} is not an authoritative receipt for this account.",
                       run_id=run_id, attempt=attempt)
    runner = metadata.get("runner")
    design = f"{metadata.get('platform')}/{metadata.get('design')}"
    finished_at = row.get("finished_at")
    current_time = now if now is not None else time.time()
    if (runner != policy.require_runner or design not in policy.allowed_designs
            or not isinstance(finished_at, (int, float)) or isinstance(finished_at, bool)
            or current_time - float(finished_at) > policy.max_age_sec or finished_at > current_time + 300):
        return _result(CODE_OUTSIDE_POLICY,
                       f"{run_id} attempt {attempt} is outside the run binding rules of this profile.",
                       run_id=run_id, attempt=attempt)
    binding = Binding(context.session_id, context.member_id, run_id, attempt,
                      profile.profile_sha256, identity.sha256, current_time,
                      content_hash=str(row.get("content_hash") or ""), receipt_sha256=receipt_sha256)
    with _lock:
        items = _bindings.get(context.session_id, [])
        # Profile 或 policy 已變更的同 member 綁定不再有效：直接以新綁定取代，不占上限。
        others = [item for item in items if item.member_id != context.member_id]
        existing = [item for item in items if item.member_id == context.member_id
                    and item.profile_sha256 == profile.profile_sha256
                    and item.policy_sha256 == identity.sha256]
        for item in existing:
            if item.run_id == run_id and item.attempt == attempt and item.content_hash == binding.content_hash:
                _bindings[context.session_id] = others + existing
                return _result(CODE_BOUND, f"{run_id} attempt {attempt} is already bound to this session.",
                               ok=True, run_id=run_id, attempt=attempt)
        kept = [item for item in existing if not (item.run_id == run_id and item.attempt == attempt)]
        if len(kept) >= policy.max_runs_per_session:
            return _result(CODE_LIMIT, "this session already holds the maximum number of bound runs; "
                           "use /unbind-run first.", run_id=run_id, attempt=attempt)
        # 同一 member 先前的綁定（含 Profile／policy 已變更者）若沒有原樣保留，就是被替換或移除。
        replaced = len(kept) != len([item for item in items if item.member_id == context.member_id])
        _bindings[context.session_id] = others + kept + [binding]
        # epoch 推進與綁定變更在同一把鎖內完成：任何持鎖讀取都看到兩者一致的狀態。
        if replaced:
            context_epoch.advance(context.session_id, context_epoch.REASON_RUN_REPLACED)
    return _result(CODE_BOUND, f"{run_id} attempt {attempt} is now bound to this session "
                   f"({runner} {design}).", ok=True, run_id=run_id, attempt=attempt)


def unbind(*, context: CanonicalRequestContext | None = None) -> BindResult:
    """移除目前 session 的全部綁定。"""
    context = context if context is not None else get_current_request_context()
    with _lock:
        items = _bindings.get(context.session_id, [])
        existing = [item for item in items if item.member_id == context.member_id]
        remaining = [item for item in items if item.member_id != context.member_id]
        if remaining:
            _bindings[context.session_id] = remaining
        else:
            _bindings.pop(context.session_id, None)
        if existing:
            context_epoch.advance(context.session_id, context_epoch.REASON_RUN_UNBOUND)
    if not existing:
        return _result(CODE_NOT_BOUND, "no run is bound to this session.")
    runs = ", ".join(f"{item.run_id} attempt {item.attempt}" for item in existing)
    return _result(CODE_UNBOUND, f"{runs} unbound from this session.", ok=True)


def revoke_session(session_id: str, peer_uid: int | None = None) -> None:
    """session 結束／連線中斷的 registry 通知；同一 session 的綁定全部消失。"""
    with _lock:
        _bindings.pop(session_id, None)
        for key in [key for key in _tainted if key[0] == session_id]:
            _tainted.discard(key)
        context_epoch.forget(session_id)


def clear() -> None:
    """daemon 關閉或測試重設。"""
    with _lock:
        _bindings.clear()
        _tainted.clear()
        context_epoch.clear()


def revalidate_authority(profile: TaskProfile, projector: Any, *,
                         context: CanonicalRequestContext | None = None) -> frozenset[tuple[str, str, str, int, str]]:
    """以 EventStore 投影重新核對本 session、同 member 每個綁定的權威列；回傳本次讀不到權威的綁定。

    條件與 `bind` 相同：該 Run 全部列為本人寫入、content hash 自洽、receipt SHA256 與釘選 content hash
    相符。外人列、不自洽或不一致 → 永久記為 tainted；權威讀不到（`RUN_BIND_AUTHORITY_UNAVAILABLE`，
    包括 `AUTHORITY_UNAVAILABLE` projector）→ 只在本次核對回傳為不可用，不持久。`projector` 為 None
    時不核對（只供單元測試；production 入口一律傳入 projector 或 `AUTHORITY_UNAVAILABLE`）。
    讀取 EventStore 在鎖外進行。
    """
    if projector is None:
        return frozenset()
    try:
        context = context if context is not None else get_current_request_context()
    except Exception:  # noqa: BLE001 - 無 request context 時沒有任何綁定
        return frozenset()
    with _lock:
        items = [item for item in _bindings.get(context.session_id, ()) if item.member_id == context.member_id]
    unavailable: set[tuple[str, str, str, int, str]] = set()
    for item in items:
        key = (item.session_id, item.member_id, item.run_id, item.attempt, item.content_hash)
        with _lock:
            if key in _tainted:
                continue
        row, failure = _projection_row(projector, item.run_id, item.attempt, item.receipt_sha256, item.member_id)
        if failure == CODE_AUTHORITY_UNAVAILABLE:
            unavailable.add(key)
        elif failure is not None or row is None or str(row.get("content_hash") or "") != item.content_hash:
            with _lock:
                _tainted.add(key)
    return frozenset(unavailable)


def _policy_scope_key(identity: Any) -> str | None:
    """跨 turn 撤銷判定所用的 policy 身分；讀不到時為 None。

    `structural_c2`：revision 本身，任何改變都結束 epoch。`exact_digest`：白名單以外的全部欄位——
    白名單逐份核准實際送出的 bytes，管理端只增刪白名單不是外送上下文授權的改變；目的地、model、
    模式、資料類別或其他欄位改變仍結束 epoch。turn 內的任何 revision 改變另由 `~policy` 列拒絕。
    """
    if identity is None:
        return None
    return identity.sha256 if identity.structural else "exact_digest:" + identity.context_scope


def _state_rows_locked(session_id: str, member_id: str, profile: TaskProfile, identity: Any,
                       unavailable: frozenset[tuple[str, str, str, int, str]]) -> tuple[tuple[str, int, str, str], ...]:
    """呼叫端必須持有 `_lock`。前三列是 Profile、policy revision 與 policy 授權範圍，其後是本 member 的綁定。"""
    policy_sha = identity.sha256 if identity is not None else None
    scope = _policy_scope_key(identity)
    rows: list[tuple[str, int, str, str]] = []
    for item in _bindings.get(session_id, ()):
        if item.member_id != member_id:
            continue
        key = (item.session_id, item.member_id, item.run_id, item.attempt, item.content_hash)
        if key in _tainted:
            state = STATE_TAINTED
        elif (item.profile_sha256 != profile.profile_sha256 or policy_sha is None
              or item.policy_sha256 != policy_sha or not item.content_hash):
            state = STATE_STALE
        elif key in unavailable:
            state = STATE_UNAVAILABLE
        else:
            state = STATE_VALID
        rows.append((item.run_id, item.attempt, item.content_hash, state))
    identity_rows = ((IDENTITY_ROW_PREFIX + "profile", 0, profile.profile_sha256, STATE_VALID),
                     (IDENTITY_ROW_PREFIX + "policy", 0, policy_sha or "",
                      STATE_TURN_IDENTITY if policy_sha is not None else STATE_UNAVAILABLE),
                     (IDENTITY_ROW_PREFIX + "policy_scope", 0, scope or "",
                      STATE_VALID if scope is not None else STATE_UNAVAILABLE))
    return identity_rows + tuple(sorted(rows))


def epoch_snapshot(profile: TaskProfile, *, context: CanonicalRequestContext | None = None, observe: bool = False,
                   unavailable: frozenset[tuple[str, str, str, int, str]] = frozenset(),
                   ) -> tuple[str | None, tuple[tuple[str, int, str, str], ...]]:
    """在同一把鎖內取得本 session 的外送上下文 epoch 與狀態指紋。

    指紋＝Profile 與 policy 身分列，加上每個綁定的 `(run_id, attempt, content_hash, state)`。
    `bind`／`unbind`／`revoke_session` 也在這把鎖內推進或遺忘 epoch，因此回傳的兩者彼此一致：不可能看到
    「舊 epoch＋解除再綁定之後的綁定集合」。`observe=True`（turn 開始）時以這份指紋更新 epoch
    （`context_epoch.observe`）；否則只讀取（`context_epoch.peek`，最終 Gate）。`unavailable` 是本次核對
    讀不到權威的綁定（`revalidate_authority` 的回傳值）。policy 檔在鎖外讀取。
    """
    try:
        context = context if context is not None else get_current_request_context()
    except Exception:  # noqa: BLE001 - 無 request context 時沒有任何綁定
        return None, ()
    identity = policy_identity()
    with _lock:
        rows = _state_rows_locked(context.session_id, context.member_id, profile, identity, unavailable)
        if observe:
            epoch, _ = context_epoch.observe(context.session_id, rows)
        else:
            epoch = context_epoch.peek(context.session_id)
    return epoch, rows


def retire_epoch_if_current(session_id: str, epoch: str, reason: str) -> None:
    """若本 session 目前仍是 `epoch`，在綁定鎖內結束它（最終 Gate 偵測到撤銷時呼叫）。

    在綁定鎖內呼叫 `context_epoch.advance_if_current`：比對與推進在 epoch 鎖的同一次取得內完成，
    epoch 已被其他操作（包括不經綁定鎖的推進）推進時不再覆寫。
    """
    with _lock:
        context_epoch.advance_if_current(session_id, epoch, reason)


def retire_session_epoch(session_id: str, reason: str) -> bool:
    """無條件結束本 session 目前的 epoch（若存在）；回傳是否結束。

    用於 Profile／policy 權威讀不到或在 turn 內改變：此時不論目前是哪一個 epoch，先前的回合都不得
    再外送。在綁定鎖內推進，與 `bind`／`unbind` 的推進互斥。呼叫端不得持有本模組的鎖。
    """
    with _lock:
        if context_epoch.peek(session_id) is None:
            return False
        context_epoch.advance(session_id, reason)
        return True


def binding_rows(rows: tuple[tuple[str, int, str, str], ...]) -> tuple[tuple[str, int, str, str], ...]:
    """指紋中屬於 Run 綁定的列（去掉 Profile／policy 身分列）。"""
    return tuple(row for row in rows if not row[0].startswith(IDENTITY_ROW_PREFIX))


def session_binding_state(profile: TaskProfile, *, projector: Any = None,
                          context: CanonicalRequestContext | None = None) -> tuple[tuple[str, int, str, str], ...]:
    """本 session、同 member 全部綁定的 `(run_id, attempt, content_hash, state)`，依 run 排序。

    `state`：`valid`、`stale_profile_or_policy`、`tainted`、`authority_unavailable`。給定 `projector` 時先重新
    核對權威列。結果只作為 epoch 判定的指紋，不改變綁定本身。
    """
    unavailable = revalidate_authority(profile, projector, context=context)
    return binding_rows(epoch_snapshot(profile, context=context, unavailable=unavailable)[1])


def bound_runs_for_current_session(profile: TaskProfile) -> tuple[tuple[str, int], ...]:
    """目前 session、同 member、Profile 與 policy 皆未變更的綁定。"""
    return tuple((run, attempt) for run, attempt, _ in bound_pins_for_current_session(profile))


def bound_pins_for_current_session(profile: TaskProfile) -> tuple[tuple[str, int, str], ...]:
    """同 `bound_runs_for_current_session`，另附綁定當下權威列的 content hash。"""
    try:
        context = get_current_request_context()
    except Exception:  # noqa: BLE001 - 無 request context 時沒有任何綁定可見
        return ()
    identity = policy_identity()
    policy_sha = identity.sha256 if identity is not None else None
    with _lock:
        items = list(_bindings.get(context.session_id, ()))
    return tuple(
        (item.run_id, item.attempt, item.content_hash) for item in items
        if item.member_id == context.member_id and item.profile_sha256 == profile.profile_sha256
        and policy_sha is not None and item.policy_sha256 == policy_sha and item.content_hash
    )


def bindings_for_session(session_id: str) -> tuple[Binding, ...]:
    """診斷／測試用唯讀檢視。"""
    with _lock:
        return tuple(_bindings.get(session_id, ()))


__all__ = [
    "AUTHORITY_UNAVAILABLE", "BindResult", "Binding", "ReceiptUnreadable", "STATE_STALE", "STATE_TAINTED",
    "STATE_UNAVAILABLE", "STATE_VALID", "bind", "binding_rows",
    "bindings_for_session", "bound_pins_for_current_session", "bound_runs_for_current_session", "clear",
    "epoch_snapshot", "read_receipt", "retire_session_epoch", "revalidate_authority", "revoke_session",
    "session_binding_state", "unbind",
]
