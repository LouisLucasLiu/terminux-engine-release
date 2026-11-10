# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""lineage.jsonl rotation + gzip backup + purge。

超過 max_bytes 時 gzip 壓縮 → unlink 原檔；wrapper 下次 >> 建新 inode，
JsonlIngestor 的 sidecar inode 偵測自然 reset offset。
"""
from __future__ import annotations

import gzip
import logging
import shutil
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class LogSweeper:
    """lineage.jsonl rotation + gzip 備份 + purge（背景 polling thread）。"""
    def __init__(
        self,
        jsonl_path: Path | str,
        max_bytes: int = 10 * 1024 * 1024,
        max_backups: int = 3,
        check_interval: float = 300.0,
    ):
        self._jsonl_path = Path(jsonl_path)
        self._max_bytes = max_bytes
        self._max_backups = max_backups
        self._check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def sweep_once(self) -> bool:
        """輪替舊檔與各主機 JSONL；每個來源獨立保留備份。"""
        paths = [self._jsonl_path]
        try:
            paths.extend(sorted(self._jsonl_path.parent.glob("lineage.*.jsonl")))
        except OSError as exc:
            logger.warning("glob lineage JSONL files failed: %s", exc)
        rotated = False
        for path in dict.fromkeys(paths):
            if self._sweep_path(path):
                rotated = True
        return rotated

    def _sweep_path(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning("failed to stat %s: %s", path, exc)
            return False
        if size < self._max_bytes:
            return False
        rotated = self._rotate(path)
        self._purge_old_backups(path)
        return rotated

    def start(self) -> None:
        """啟動背景 polling thread（冪等）。daemon=True 避免阻擋 process exit。"""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name="terminux-log-sweeper",
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
                self.sweep_once()
            except Exception as exc:
                logger.error("sweep_once failed unexpectedly: %s", exc)
            if self._stop_event.wait(timeout=self._check_interval):
                break

    def _rotate(self, path: Path) -> bool:
        """gzip 壓縮 → unlink 原檔。失敗回 False 並清理不完整的 gz。"""
        ts = int(time.time())
        gz_path = path.with_name(f"{path.name}.{ts}.gz")
        try:
            with path.open("rb") as f_in:
                with gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
        except OSError as exc:
            logger.warning("rotation gzip failed: %s", exc)
            try:
                gz_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("rotation unlink failed: %s", exc)
            return False
        try:
            gz_size = gz_path.stat().st_size
        except OSError:
            gz_size = -1
        logger.info("Rotated %s → %s (%d bytes)", path, gz_path, gz_size)
        return True

    def _purge_old_backups(self, path: Path) -> None:
        """按 mtime 排序，保留最新 max_backups 個 .gz，刪除更舊的。"""
        try:
            backups = sorted(
                path.parent.glob(f"{path.name}.*.gz"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            logger.warning("purge glob/stat failed: %s", exc)
            return
        for old in backups[self._max_backups:]:
            try:
                old.unlink()
                logger.info("Purged old backup: %s", old)
            except OSError as exc:
                logger.warning("failed to purge %s: %s", old, exc)
