# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Z3-2：JSONL → SessionEvent ingest pipeline。

定時讀舊 lineage.jsonl 與各主機 lineage.*.jsonl 寫 event_store。每檔獨立
sidecar offset + inode 偵測
+ deterministic event_id 為 dedup 安全網。tool→tool_name rename 對齊 Z1-3。
session_id 用 trace_root（與 ECC session_id 不同命名空間，intentional）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

from terminux_engine.store.event_store import EventStore, SessionEvent

logger = logging.getLogger(__name__)

_EVENT_TYPE_TOOL_EXEC = "tool_exec"
_DEFAULT_LAYER = "wrapper"
_DEFAULT_TERMINAL_ID = "default"


class JsonlIngestor:
    """JSONL → event_store ingest pipeline（背景 polling thread）。"""

    def __init__(
        self,
        jsonl_path: Path | str,
        event_store: EventStore,
        user_id: str,
        poll_interval: float = 30.0,
        sidecar_suffix: str = ".offset",
    ):
        self._jsonl_path = Path(jsonl_path)
        self._sidecar_suffix = sidecar_suffix
        self._event_store = event_store
        self._user_id = user_id
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def ingest_once(self) -> int:
        """讀舊檔與所有主機檔的新增資料。回傳實際寫入筆數。不 raise。"""
        paths = [self._jsonl_path]
        try:
            paths.extend(sorted(self._jsonl_path.parent.glob("lineage.*.jsonl")))
        except OSError as exc:
            logger.warning("glob lineage JSONL files failed: %s", exc)
        return sum(self._ingest_path(path) for path in dict.fromkeys(paths))

    def _ingest_path(self, jsonl_path: Path) -> int:
        try:
            with jsonl_path.open("rb") as f:
                # 以已開啟檔案的 inode 決定 offset，避免輪替發生在 stat/open 之間。
                stat = os.fstat(f.fileno())
                current_inode = stat.st_ino
                offset = self._resume_offset(jsonl_path, current_inode, stat.st_size)
                if offset >= stat.st_size:
                    return 0
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            logger.warning("read lineage JSONL failed (%s): %s", jsonl_path, exc)
            return 0
        if not data:
            return 0

        # partial last line 留待下次：找最後一個 \n 切 complete 段
        if data.endswith(b"\n"):
            complete = data
        else:
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                return 0
            complete = data[: last_nl + 1]

        bytes_consumed = len(complete)
        new_events = 0
        for line in complete.split(b"\n"):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("malformed JSONL line: %s", exc)
                continue
            event = self._jsonl_to_session_event(raw)
            if event is None:
                continue
            try:
                self._event_store.emit(event)
                new_events += 1
            except sqlite3.IntegrityError:
                pass  # PRIMARY KEY 衝突 = 已 ingest（dedup 安全網）
            except Exception as exc:
                logger.warning("event_store.emit failed: %s", exc)

        self._save_offset(jsonl_path, offset + bytes_consumed, current_inode)
        return new_events

    def start(self) -> None:
        """啟動背景 polling thread（冪等）。daemon=True 避免阻擋 process exit。"""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name="terminux-jsonl-ingestor",
        )
        self._thread.start()

    def signal_stop(self) -> None:
        """非阻塞發出停止信號。"""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """等待背景 thread 結束。"""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None

    @property
    def is_alive(self) -> bool:
        """背景 thread 是否仍在執行。"""
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float | None = None) -> None:
        """停止背景 thread（blocking join）。"""
        self.signal_stop()
        self.join(timeout=timeout)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.ingest_once()
            except Exception as exc:
                logger.error("ingest_once failed unexpectedly: %s", exc)
            if self._stop_event.wait(timeout=self._poll_interval):
                break

    def _sidecar_path(self, jsonl_path: Path) -> Path:
        return jsonl_path.with_name(jsonl_path.name + self._sidecar_suffix)

    def _resume_offset(self, jsonl_path: Path, current_inode: int, file_size: int) -> int:
        """三條 reset 路徑：sidecar 缺 / inode 變 / offset > size。"""
        sidecar_path = self._sidecar_path(jsonl_path)
        if not sidecar_path.exists():
            return 0
        try:
            state = json.loads(sidecar_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("sidecar unreadable, resetting: %s", exc)
            return 0
        if state.get("inode") != current_inode:
            return 0
        offset = int(state.get("offset", 0))
        return 0 if offset > file_size else offset

    def _save_offset(self, jsonl_path: Path, offset: int, inode: int) -> None:
        """tmp + replace 原子化寫入。失敗只 log。"""
        sidecar_path = self._sidecar_path(jsonl_path)
        tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps({"inode": inode, "offset": offset}))
            tmp.replace(sidecar_path)
        except OSError as exc:
            logger.warning("save offset failed: %s", exc)

    def _jsonl_to_session_event(self, raw: dict) -> SessionEvent | None:
        """JSONL dict → SessionEvent；缺 required 欄位或構造失敗回 None。"""
        ts = raw.get("ts")
        trace_self = raw.get("trace_self", "")
        trace_root = raw.get("trace_root", "")
        if not isinstance(ts, (int, float)) or not trace_self or not trace_root:
            logger.warning("JSONL event missing required fields (ts/trace_*)")
            return None

        # event_type 從 JSONL 讀；舊 wrapper 寫 "tool_exec"，向後相容
        event_type = raw.get("event") or _EVENT_TYPE_TOOL_EXEC

        payload = {
            "tool_name": raw.get("tool"),  # tool → tool_name rename
            "argv": raw.get("argv", []),
            "cwd": raw.get("cwd", ""),
            "pid": raw.get("pid"),
            "schema_version": raw.get("v", 1),
        }
        # queue_submit 特有欄位（其他 event type 不會有 → 不污染 payload）
        if raw.get("queue_system"):
            payload["queue_system"] = raw["queue_system"]
        try:
            return SessionEvent(
                event_id=self._make_event_id(trace_self, ts, event_type),
                session_id=trace_root,  # = trace_root（intentional）
                terminal_id=_DEFAULT_TERMINAL_ID,
                user_id=self._user_id,
                project_id=None,
                timestamp=float(ts),
                event_type=event_type,
                payload=payload,
                block_name=None,
                trace_root=trace_root,
                trace_queue=raw.get("trace_queue", ""),
                trace_job=raw.get("trace_job", ""),
                trace_parent=raw.get("trace_parent", ""),
                trace_self=trace_self,
                layer=raw.get("layer", _DEFAULT_LAYER),
            )
        except Exception as exc:
            logger.warning("SessionEvent construction failed: %s", exc)
            return None

    @staticmethod
    def _make_event_id(trace_self: str, ts: float, event_type: str) -> str:
        """deterministic event_id = sha256(trace_self:ts:event_type)[:32]。"""
        return hashlib.sha256(
            f"{trace_self}:{ts}:{event_type}".encode()
        ).hexdigest()[:32]
