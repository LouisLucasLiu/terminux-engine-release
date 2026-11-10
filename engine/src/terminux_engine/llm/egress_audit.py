# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Profile 模式外送請求的本機決策紀錄（metadata＋雜湊，不含內容）。

與 `agent_audit.emit` 刻意分離：那個寫入器依設計吞掉所有例外，無法承載
「寫不進去就不外送」的語意。本模組在 `structural_c2` 下是最終 Gate 的一部分：
`record()` 失敗即拋 `EgressAuditUnavailable`，呼叫端據此拒絕送出。

紀錄內容：request／turn／session 前綴、policy 身分（revision＝policy 檔 SHA256）、
目的地 host 與 model、stage、決策與原因碼、本 turn 的請求序號、各類別位元組數，
以及依 `TERMINUX_EGRESS_AUDIT_DIGEST` 選定的 payload 完整性欄。不記錄內容、
Authorization、路徑、env 值、金鑰本體、完整 session_id 或 hostname。

payload 完整性欄的四種模式（必要的外送紀錄只接受後三者，且必須明示設定）：
- `metadata_only`：不記任何 payload 雜湊；只適用於不需要外送紀錄的情境。
- `plain_sha`：`payload_sha256`，任何人可重算（完整性用途；短輸入可被離線枚舉確認）。
- `keyed_ephemeral`：`payload_hmac`＋`key_id`，金鑰在 daemon 程序啟動後於記憶體產生、
  永不落盤、重啟即換；同一 daemon 期間可比對「同一 payload」，跨重啟無法關聯。
- `keyed_persisted`：金鑰來自管理端提供的 keyring 檔（`egress_audit_keyring`），daemon 只讀。
  每筆紀錄另附整筆的 `record_mac`、管理端指定的 `key_id`、`key_commitment`、MAC 演算法與
  版本；持有 keyring 的驗證端（`egress_audit_verify`）可在 daemon 重啟後重算每筆紀錄的 MAC。
  keyring 在程序內第一次使用時固定；之後檔案內容改變即拒絕外送，輪替需重啟 daemon。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from terminux_engine.ipc.request_context import try_get_current_request_context
from terminux_engine.llm import agent_audit
from terminux_engine.llm.egress_audit_keyring import AuditKey, Keyring, KeyringError, load_keyring
from terminux_engine.llm.egress_policy import EgressPolicy

DIGEST_ENV = "TERMINUX_EGRESS_AUDIT_DIGEST"
DIGEST_METADATA_ONLY = "metadata_only"
DIGEST_PLAIN_SHA = "plain_sha"
DIGEST_KEYED_EPHEMERAL = "keyed_ephemeral"
DIGEST_KEYED_PERSISTED = "keyed_persisted"
DIGEST_MODES = frozenset({DIGEST_METADATA_ONLY, DIGEST_PLAIN_SHA, DIGEST_KEYED_EPHEMERAL, DIGEST_KEYED_PERSISTED})
#: 會為最終 payload 產生雜湊或認證碼的模式；必要的外送紀錄只接受這些，且必須明示設定。
HASHING_DIGEST_MODES = frozenset({DIGEST_PLAIN_SHA, DIGEST_KEYED_EPHEMERAL, DIGEST_KEYED_PERSISTED})
#: 保存天數（管理決定；超過者於 daemon 啟動 sweep 時刪除並留一筆 `audit_purged`）。
RETENTION_DAYS = 90
AUDIT_DIRECTORY_NAME = "egress_audit"
STAGES = frozenset({"activate", "track_message", "final_gate", "transport"})
#: `keyed_persisted` 紀錄的 MAC 身分；驗證端只接受完全相同的版本與演算法字串。
MAC_VERSION = "terminux.egress_audit.mac.v1"
MAC_ALGORITHM = "hmac-sha256"
#: 定義域分離：payload HMAC 與整筆紀錄 MAC 使用不同前綴，兩者不可互相替代。
_PAYLOAD_DOMAIN = b"terminux.egress_audit.payload.v1\x00"
_RECORD_DOMAIN = b"terminux.egress_audit.record.v1\x00"
_SESSION_PREFIX_CHARS = 8
_MAX_TEXT = 160
_KEY_BYTES = 32

_lock = threading.Lock()
_key: bytes | None = None
_pinned_keyring: Keyring | None = None


class EgressAuditUnavailable(RuntimeError):
    """audit 無法寫入或設定無效；在需要 audit 的模式下代表不得外送。"""


def digest_mode() -> str:
    """讀取 payload 完整性欄模式；未設定＝`metadata_only`，未知值視為設定無效。"""
    raw = os.environ.get(DIGEST_ENV, "").strip().lower()
    if not raw:
        return DIGEST_METADATA_ONLY
    if raw not in DIGEST_MODES:
        raise EgressAuditUnavailable(f"invalid {DIGEST_ENV}")
    return raw


def require_hashing_mode() -> str:
    """必要外送紀錄的前提：明示設定一種會記錄 payload 雜湊的模式，且金鑰可用。

    未設定、空白、`metadata_only` 或未知值都視為不可外送；本函式不替管理端選擇
    模式，也不建立任何持久金鑰。`keyed_ephemeral` 會在此先取得程序金鑰，
    `keyed_persisted` 會在此先讀取並固定 keyring；取得失敗同樣拒絕。
    """
    raw = os.environ.get(DIGEST_ENV)
    if raw is None or not raw.strip():
        raise EgressAuditUnavailable(f"{DIGEST_ENV} must be set explicitly")
    mode = digest_mode()
    if mode not in HASHING_DIGEST_MODES:
        raise EgressAuditUnavailable("audit digest mode does not record a payload hash")
    if mode == DIGEST_KEYED_EPHEMERAL:
        _process_key()
    elif mode == DIGEST_KEYED_PERSISTED:
        _persistent_keyring()
    return mode


def configure_key(key: bytes | None) -> str | None:
    """以外部提供的金鑰取代程序金鑰（測試用合成金鑰）；None 表示重設程序內的金鑰狀態。

    重設同時放掉已固定的 keyring，讓下一次使用重新讀檔（模擬 daemon 重啟）。
    回傳該金鑰的公開識別；與目前的 digest 模式無關，因此也可用於重設。
    """
    global _key, _pinned_keyring
    if key is not None and len(key) < 16:
        raise EgressAuditUnavailable("audit key too short")
    with _lock:
        _key = key
        if key is None:
            _pinned_keyring = None
    return hashlib.sha256(key).hexdigest()[:8] if key is not None else None


def _process_key() -> bytes:
    global _key
    with _lock:
        if _key is None:
            try:
                generated = os.urandom(_KEY_BYTES)
            except (OSError, NotImplementedError) as exc:
                raise EgressAuditUnavailable("audit key could not be generated") from exc
            if not isinstance(generated, bytes) or len(generated) < 16:
                raise EgressAuditUnavailable("audit key unusable")
            _key = generated
        if len(_key) < 16:
            raise EgressAuditUnavailable("audit key unusable")
        return _key


def _persistent_keyring() -> Keyring:
    """讀取管理端 keyring 並與程序內第一次固定的版本比對；任何不一致都拒絕。

    每次呼叫都重新讀檔：檔案在程序期間被替換（即使換成另一份合法 keyring）
    代表金鑰身分在外送途中改變，不得沿用舊的或改用新的；管理者輪替後須重啟 daemon。
    """
    global _pinned_keyring
    try:
        loaded = load_keyring()
    except KeyringError as exc:
        raise EgressAuditUnavailable(f"audit keyring {exc.reason}") from exc
    with _lock:
        if _pinned_keyring is None:
            _pinned_keyring = loaded
        elif _pinned_keyring.sha256 != loaded.sha256:
            raise EgressAuditUnavailable("audit keyring changed since first use")
        return _pinned_keyring


def _active_key() -> AuditKey:
    return _persistent_keyring().active


def key_id() -> str | None:
    """目前金鑰的公開識別：ephemeral 為 SHA256 前 8 字元，persisted 為管理端指定的 `key_id`；非 keyed 模式回 None。"""
    mode = digest_mode()
    if mode == DIGEST_KEYED_EPHEMERAL:
        return hashlib.sha256(_process_key()).hexdigest()[:8]
    if mode == DIGEST_KEYED_PERSISTED:
        return _active_key().key_id
    return None


def audit_directory(*, create: bool = False) -> Path:
    """`host_runtime_directory()/egress_audit`；目錄必須為本人 0700 且不是 symlink。"""
    from terminux_engine.ipc.agent_socket import AgentSocketError, host_runtime_directory

    try:
        base = host_runtime_directory(create_dir=create)
    except AgentSocketError as exc:
        raise EgressAuditUnavailable(f"runtime directory unavailable: {exc.__class__.__name__}") from exc
    directory = base / AUDIT_DIRECTORY_NAME
    if create and not os.path.lexists(directory):
        try:
            directory.mkdir(mode=0o700)
        except OSError as exc:
            raise EgressAuditUnavailable("audit directory could not be created") from exc
    try:
        info = os.lstat(directory)
    except OSError as exc:
        raise EgressAuditUnavailable("audit directory missing") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise EgressAuditUnavailable("audit directory owner or mode invalid")
    return directory


def _bounded(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_TEXT]


def _payload_fields(payload: bytes | None) -> dict[str, Any]:
    mode = digest_mode()
    fields: dict[str, Any] = {"digest_mode": mode, "payload_bytes": len(payload) if payload is not None else None}
    if payload is None or mode == DIGEST_METADATA_ONLY:
        return fields
    if mode == DIGEST_PLAIN_SHA:
        fields["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        return fields
    if mode == DIGEST_KEYED_PERSISTED:
        key = _active_key()
        fields["payload_hmac"] = hmac.new(key.secret, _PAYLOAD_DOMAIN + payload, hashlib.sha256).hexdigest()
        fields["key_id"] = key.key_id
        return fields
    fields["payload_hmac"] = hmac.new(_process_key(), payload, hashlib.sha256).hexdigest()
    fields["key_id"] = key_id()
    return fields


def canonical_record_bytes(record: dict[str, Any]) -> bytes:
    """整筆紀錄 MAC 的輸入：排除 `record_mac` 後以排序鍵、無空白的 JSON 序列化。"""
    body = {key: value for key, value in record.items() if key != "record_mac"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def record_mac(record: dict[str, Any], secret: bytes) -> str:
    """`HMAC-SHA256(secret, record domain ‖ canonical record)`；寫入端與驗證端共用同一定義。"""
    return hmac.new(secret, _RECORD_DOMAIN + canonical_record_bytes(record), hashlib.sha256).hexdigest()


def _seal_record(record: dict[str, Any]) -> dict[str, Any]:
    """`keyed_persisted` 下為每筆紀錄（含 `audit_purged`）附上金鑰身分與整筆 MAC。"""
    if digest_mode() != DIGEST_KEYED_PERSISTED:
        return record
    key = _active_key()
    if record.get("key_id", key.key_id) != key.key_id:
        raise EgressAuditUnavailable("audit record key does not match active key")
    record["key_id"] = key.key_id
    record["key_commitment"] = key.commitment
    record["mac_algorithm"] = MAC_ALGORITHM
    record["mac_version"] = MAC_VERSION
    record["record_mac"] = record_mac(record, key.secret)
    return record


def build_record(*, stage: str, decision: str, reason_code: str | None, request_index: int,
                 endpoint_host: str | None, model: str | None, policy: EgressPolicy | None,
                 payload: bytes | None, classes: Iterable[tuple[str, int]] = (),
                 runs: Iterable[tuple[str, int]] = (), provider: str = "openai_compatible",
                 retry_of: str | None = None, event: str = "egress_decision") -> dict[str, Any]:
    """組出一筆紀錄；不含任何內容、路徑、env 值或金鑰。"""
    if stage not in STAGES:
        raise EgressAuditUnavailable("invalid audit stage")
    context = try_get_current_request_context()
    session_prefix = context.session_id[:_SESSION_PREFIX_CHARS] if context is not None else None
    record: dict[str, Any] = {
        "v": 1,
        "event": event,
        "request_id": uuid.uuid4().hex,
        "turn_id": agent_audit.current_turn_id(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "uid": os.getuid(),
        "session_id_prefix": session_prefix,
        "runs": [{"run_id": _bounded(run), "attempt": attempt} for run, attempt in runs],
        "provider": provider,
        "destination_host": _bounded(endpoint_host),
        "model": _bounded(model),
        "policy_id": policy.policy_id if policy is not None else None,
        "policy_revision": policy.sha256 if policy is not None else None,
        "policy_version": policy.policy_version if policy is not None else None,
        "approval_mode": policy.approval_mode if policy is not None else None,
        "stage": stage,
        "decision": decision,
        "reason_code": _bounded(reason_code),
        "retry_of": retry_of,
        "request_index": request_index,
        "classes": [{"class": _bounded(name), "bytes": size} for name, size in classes],
    }
    record.update(_payload_fields(payload))
    return record


def write_record(record: dict[str, Any]) -> dict[str, Any]:
    """以 0600、`O_APPEND|O_NOFOLLOW` 寫入當日檔並 fsync 檔案與目錄；任何失敗拋 `EgressAuditUnavailable`。

    `keyed_persisted` 下先附上整筆 MAC 再寫入；金鑰不可用時在開檔之前就拒絕。
    """
    record = _seal_record(record)
    directory = audit_directory(create=True)
    name = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise EgressAuditUnavailable("audit directory could not be opened") from exc
    try:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            raise EgressAuditUnavailable("audit file could not be opened") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise EgressAuditUnavailable("audit file owner or mode invalid")
            data = line.encode("utf-8")
            with _lock:
                written = os.write(fd, data)
                if written != len(data):
                    raise EgressAuditUnavailable("audit write incomplete")
                os.fsync(fd)
                # 新建日檔的目錄項也要落盤：fsync 檔案本身不保證目錄項已持久。
                os.fsync(dir_fd)
        except OSError as exc:
            raise EgressAuditUnavailable("audit write failed") from exc
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return record


def record(**kwargs: Any) -> dict[str, Any]:
    """`build_record` 後立即寫入；回傳已寫入的紀錄。"""
    return write_record(build_record(**kwargs))


def sweep(*, now: float | None = None) -> list[str]:
    """刪除超過 `RETENTION_DAYS` 的日檔並留一筆 `audit_purged`；回傳刪除的檔名。

    `keyed_persisted` 下先確認 keyring 可用：purge 紀錄必須能加上 MAC，否則不刪任何檔。
    """
    directory = audit_directory(create=True)
    if digest_mode() == DIGEST_KEYED_PERSISTED:
        _active_key()
    cutoff = (now if now is not None else time.time()) - RETENTION_DAYS * 86400
    removed: list[str] = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".jsonl" or entry.is_symlink() or not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed.append(entry.name)
        except OSError:
            continue
    if removed:
        write_record({
            "v": 1, "event": "audit_purged",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "uid": os.getuid(), "removed_files": removed, "retention_days": RETENTION_DAYS,
        })
    return removed


__all__ = [
    "AUDIT_DIRECTORY_NAME", "DIGEST_ENV", "DIGEST_KEYED_EPHEMERAL", "DIGEST_KEYED_PERSISTED", "DIGEST_METADATA_ONLY",
    "DIGEST_MODES", "DIGEST_PLAIN_SHA", "EgressAuditUnavailable", "HASHING_DIGEST_MODES", "MAC_ALGORITHM",
    "MAC_VERSION", "RETENTION_DAYS", "STAGES",
    "audit_directory", "build_record", "canonical_record_bytes", "configure_key", "digest_mode", "key_id", "record",
    "record_mac", "require_hashing_mode", "sweep", "write_record",
]
