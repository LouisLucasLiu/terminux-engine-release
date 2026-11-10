# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""離線、唯讀的外送 audit 驗證工具：以管理端 keyring 重算 `keyed_persisted` 紀錄的 MAC。

    python -m terminux_engine.llm.egress_audit_verify --keyring <file> <audit 目錄或日檔>...

每筆紀錄恰好得到一個狀態；工具不重建 payload、不接受 payload 反查、不輸出任何金鑰值或
`payload_hmac`。歷史的 `keyed_ephemeral` 紀錄不會因為持有 keyring 而被改判為可驗證。

- `VERIFIED`：以紀錄所指的金鑰重算 `record_mac` 相符，且該金鑰仍為 active 或 verify_only。
- `VERIFIED_REVOKED_KEY`：MAC 相符，但該金鑰已標 `revoked`；呼叫端須另行判定其可信度。
- `TAMPERED`：金鑰與承諾相符，但 `record_mac` 不符或缺失（任一欄位被改動、增刪或重排）。
- `MISSING_KEY`：keyring 內沒有該 `key_id`（退役金鑰已被刪除，紀錄不可驗證）。
- `KEY_MISMATCH`：有同名 `key_id`，但 `key_commitment` 不符（同 ID 不同金鑰）。
- `LEGACY_EPHEMERAL_UNVERIFIABLE`：`keyed_ephemeral` 紀錄，金鑰已隨該 daemon 程序消失。
- `UNKEYED_NO_MAC`：`plain_sha`／`metadata_only` 或沒有 MAC 欄位的舊格式紀錄。
- `UNSUPPORTED_VERSION`：紀錄版本、MAC 版本或演算法字串不是本工具所知。
- `MALFORMED`：不是 JSON 物件，或 `keyed_persisted` 紀錄缺少／形狀不符的身分欄位。

沒有 MAC 的狀態（`UNKEYED_NO_MAC`、`LEGACY_EPHEMERAL_UNVERIFIABLE`）預設不算失敗；驗證一個應全為
`keyed_persisted` 的目錄時加 `--require-sealed`，使它們一併計為失敗——否則把一筆紀錄改標為無鑰模式並
刪掉 MAC 欄位、或插入一筆無鑰紀錄，都只會得到「無法驗證」而不是失敗。
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from terminux_engine.llm.egress_audit import (
    DIGEST_KEYED_EPHEMERAL,
    DIGEST_KEYED_PERSISTED,
    MAC_ALGORITHM,
    MAC_VERSION,
    record_mac,
)
from terminux_engine.llm.egress_audit_keyring import STATUS_REVOKED, Keyring, KeyringError, load_keyring

STATUS_VERIFIED = "VERIFIED"
STATUS_VERIFIED_REVOKED_KEY = "VERIFIED_REVOKED_KEY"
STATUS_TAMPERED = "TAMPERED"
STATUS_MISSING_KEY = "MISSING_KEY"
STATUS_KEY_MISMATCH = "KEY_MISMATCH"
STATUS_LEGACY_EPHEMERAL = "LEGACY_EPHEMERAL_UNVERIFIABLE"
STATUS_UNKEYED = "UNKEYED_NO_MAC"
STATUS_UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
STATUS_MALFORMED = "MALFORMED"
#: 代表紀錄或 keyring 有問題、而不只是「無法驗證的舊格式」的狀態；CLI 以此決定結束碼。
FAILURE_STATUSES = frozenset({STATUS_TAMPERED, STATUS_MISSING_KEY, STATUS_KEY_MISMATCH,
                              STATUS_UNSUPPORTED_VERSION, STATUS_MALFORMED})
#: 沒有 MAC 可驗的狀態：預設只是「無法驗證」，`--require-sealed` 時一併視為失敗（防止把
#: `keyed_persisted` 紀錄改標為無鑰模式、或插入無鑰紀錄來躲過驗證）。
UNSEALED_STATUSES = frozenset({STATUS_UNKEYED, STATUS_LEGACY_EPHEMERAL})
_RECORD_VERSION = 1
_REQUEST_ID_CHARS = 12
_MAC_HEX = re.compile(r"[0-9a-f]{64}")
_COMMITMENT_HEX = re.compile(r"[0-9a-f]{16}")


@dataclass(frozen=True)
class RecordVerification:
    """單筆紀錄的驗證結果；只攜帶可公開的識別欄位。"""

    file: str
    line: int
    status: str
    event: str | None = None
    key_id: str | None = None
    request_id_prefix: str | None = None
    detail: str = ""


def _identity(record: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    event = record.get("event")
    key_id = record.get("key_id")
    request_id = record.get("request_id")
    return (event if isinstance(event, str) else None,
            key_id if isinstance(key_id, str) else None,
            request_id[:_REQUEST_ID_CHARS] if isinstance(request_id, str) else None)


def verify_record(record: Any, keyring: Keyring) -> tuple[str, str]:
    """回傳 `(status, detail)`；不修改 `record`。"""
    if not isinstance(record, dict):
        return STATUS_MALFORMED, "not_an_object"
    if record.get("v") != _RECORD_VERSION:
        return STATUS_UNSUPPORTED_VERSION, "record_version"
    mode = record.get("digest_mode")
    sealed = "record_mac" in record or mode == DIGEST_KEYED_PERSISTED
    if not sealed:
        if mode == DIGEST_KEYED_EPHEMERAL:
            return STATUS_LEGACY_EPHEMERAL, "ephemeral_key_not_recoverable"
        return STATUS_UNKEYED, f"digest_mode={mode}" if isinstance(mode, str) else "no_digest_mode"
    if record.get("mac_version") != MAC_VERSION or record.get("mac_algorithm") != MAC_ALGORITHM:
        return STATUS_UNSUPPORTED_VERSION, "mac_version_or_algorithm"
    key_id = record.get("key_id")
    commitment = record.get("key_commitment")
    # 先以固定形狀（小寫 hex）過濾，再做常數時間比較：非 ASCII 或長度不符都不得進到 compare_digest。
    if not isinstance(key_id, str) or not isinstance(commitment, str) or not _COMMITMENT_HEX.fullmatch(commitment):
        return STATUS_MALFORMED, "missing_or_malformed_key_identity"
    key = keyring.by_id(key_id)
    if key is None:
        return STATUS_MISSING_KEY, "key_id_not_in_keyring"
    if not hmac.compare_digest(key.commitment.encode("ascii"), commitment.encode("ascii")):
        return STATUS_KEY_MISMATCH, "key_commitment_differs"
    claimed = record.get("record_mac")
    if not isinstance(claimed, str) or not _MAC_HEX.fullmatch(claimed):
        return STATUS_TAMPERED, "record_mac_missing_or_malformed"
    if not hmac.compare_digest(record_mac(record, key.secret).encode("ascii"), claimed.encode("ascii")):
        return STATUS_TAMPERED, "record_mac_mismatch"
    if key.status == STATUS_REVOKED:
        return STATUS_VERIFIED_REVOKED_KEY, "key_revoked"
    return STATUS_VERIFIED, ""


def verify_lines(lines: Iterable[str], keyring: Keyring, *, file: str = "") -> list[RecordVerification]:
    results: list[RecordVerification] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            results.append(RecordVerification(file, number, STATUS_MALFORMED, detail="invalid_json"))
            continue
        status, detail = verify_record(record, keyring)
        event, key_id, request_prefix = _identity(record) if isinstance(record, dict) else (None, None, None)
        results.append(RecordVerification(file, number, status, event, key_id, request_prefix, detail))
    return results


def verify_file(path: Path, keyring: Keyring) -> list[RecordVerification]:
    """逐行驗證一個日檔；檔名只以最後一段出現在結果中。"""
    with open(path, "rb") as stream:
        text = stream.read().decode("utf-8", errors="replace")
    return verify_lines(text.splitlines(), keyring, file=path.name)


def verify_path(path: Path, keyring: Keyring) -> list[RecordVerification]:
    """目錄＝其中所有 `*.jsonl`（不跟 symlink）；檔案＝該檔。"""
    if path.is_dir():
        results: list[RecordVerification] = []
        for entry in sorted(path.iterdir()):
            if entry.suffix == ".jsonl" and not entry.is_symlink() and entry.is_file():
                results.extend(verify_file(entry, keyring))
        return results
    return verify_file(path, keyring)


def summarize(results: Iterable[RecordVerification]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return dict(sorted(counts.items()))


def failing(results: Iterable[RecordVerification], *, require_sealed: bool = False) -> list[RecordVerification]:
    """回傳應使結束碼為 1 的結果；`require_sealed` 時沒有 MAC 的紀錄也算。"""
    bad = FAILURE_STATUSES | (UNSEALED_STATUSES if require_sealed else frozenset())
    return [result for result in results if result.status in bad]


def main(argv: list[str] | None = None) -> int:
    """結束碼：0＝沒有失敗狀態；1＝任何 `FAILURE_STATUSES`（`--require-sealed` 時含無 MAC 紀錄）；2＝keyring 或參數錯誤。"""
    parser = argparse.ArgumentParser(prog="python -m terminux_engine.llm.egress_audit_verify",
                                     description="以管理端 keyring 離線驗證外送 audit 紀錄的 MAC。")
    parser.add_argument("--keyring", required=True, help="keyring 檔絕對路徑（本人 0600／0400，父目錄 0700）")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出每筆結果與統計")
    parser.add_argument("--require-sealed", action="store_true",
                        help="驗證 keyed_persisted 目錄時使用：任何沒有 MAC 的紀錄（UNKEYED_NO_MAC、"
                             "LEGACY_EPHEMERAL_UNVERIFIABLE）也視為失敗")
    parser.add_argument("paths", nargs="+", help="audit 目錄或日檔")
    args = parser.parse_args(argv)
    try:
        keyring = load_keyring(args.keyring)
    except KeyringError as exc:
        print(f"keyring error: {exc.reason}", file=sys.stderr)
        return 2
    results: list[RecordVerification] = []
    for raw in args.paths:
        path = Path(raw)
        if not path.exists():
            print(f"path not found: {path.name}", file=sys.stderr)
            return 2
        results.extend(verify_path(path, keyring))
    summary = summarize(results)
    failed = failing(results, require_sealed=args.require_sealed)
    if args.json:
        print(json.dumps({"results": [asdict(r) for r in results], "summary": summary,
                          "require_sealed": args.require_sealed, "failed": len(failed)}, ensure_ascii=False))
    else:
        for result in results:
            print(f"{result.status:28} {result.file}:{result.line} event={result.event} key_id={result.key_id} "
                  f"request={result.request_id_prefix} {result.detail}".rstrip())
        print("summary " + " ".join(f"{status}={count}" for status, count in summary.items())
              + f" require_sealed={args.require_sealed} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "FAILURE_STATUSES", "RecordVerification", "UNSEALED_STATUSES", "failing", "STATUS_KEY_MISMATCH", "STATUS_LEGACY_EPHEMERAL", "STATUS_MALFORMED",
    "STATUS_MISSING_KEY", "STATUS_TAMPERED", "STATUS_UNKEYED", "STATUS_UNSUPPORTED_VERSION", "STATUS_VERIFIED",
    "STATUS_VERIFIED_REVOKED_KEY", "main", "summarize", "verify_file", "verify_lines", "verify_path", "verify_record",
]
