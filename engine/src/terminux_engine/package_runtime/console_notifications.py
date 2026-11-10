# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Vendor-neutral console notification sink（orfs_dqe）。

把 Package Runtime 已經產生的 `NotificationRequest` 呈現在一個文字串流上
（standalone CLI 的 stdout）。這是 **delivery／presentation**，不是任何
資料的 authority——DQE 的 durable authority 仍是既有 DQE DB。

**Vendor neutrality**：本模組只認識 `level`／`title`／`message`／`actions`
四個通用欄位。它不知道 package id、EDA vendor、metric、policy 或 artifact
檔名；輸出裡若出現任何 domain 字樣，都只可能來自 package 自己 request 的
內容。任何 CLI（未來的商用 EDA composition root）都可以注入同一個 sink。

**Header forgery 防護**：package 的 `message` 可能含換行或控制字元。
每一行輸出都被強制前綴——header 行是 `[<prefix>][<level>] <title>`，
message 的每一行都縮排兩格。因此 package 內容永遠無法偽造出第二個
header 行。C0 控制字元（`\\t` 除外）在輸出前被移除。
"""

from __future__ import annotations

import sys
from typing import Any, Callable, TextIO

from terminux_engine.package_runtime.result import NotificationRequest

DEFAULT_PREFIX = "Terminux"

#: message 縮排：確保 package 內容不可能被讀成新的 header 行。
_CONTINUATION_INDENT = "  "


def _sanitize(text: str) -> str:
    """移除 C0 控制字元（保留 `\\n` 與 `\\t`），避免終端跳脫序列注入。"""
    return "".join(
        ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 0x20
    ).replace("\x7f", "")


def format_notification(
    notification: NotificationRequest, *, prefix: str = DEFAULT_PREFIX
) -> list[str]:
    """把一則 notification 轉成要輸出的行（純函式，不做 I/O）。

    第一行永遠是 header；其餘行永遠縮排。回傳的每個元素都不含換行。
    """
    level = _sanitize(str(notification.level)).strip() or "info"
    title = _sanitize(notification.title).replace("\n", " ").strip()
    lines = [f"[{prefix}][{level}] {title}".rstrip()]
    for raw in _sanitize(notification.message).splitlines():
        lines.append(f"{_CONTINUATION_INDENT}{raw}".rstrip())
    if notification.actions:
        # 只做安全的文字呈現，**不執行** action。
        labels = ", ".join(
            _sanitize(str(action.get("label") or action.get("id") or "action"))
            .replace("\n", " ")
            .strip()
            for action in notification.actions
        )
        lines.append(f"{_CONTINUATION_INDENT}(actions available: {labels})")
    return lines


class ConsoleNotificationSink:
    """可注入 `NotifyService(sink=...)` 的文字串流 sink。

    每一則 notification 恰好輸出一次，且 deterministic。
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        prefix: str = DEFAULT_PREFIX,
    ) -> None:
        self._stream = stream
        self._prefix = prefix
        #: 供呼叫端（CLI／測試）取得本 process 實際輸出過的行。
        self.emitted_lines: list[str] = []
        self.notification_count = 0

    @property
    def stream(self) -> TextIO:
        # 延遲解析，讓測試可以 monkeypatch `sys.stdout`。
        return self._stream if self._stream is not None else sys.stdout

    def __call__(self, notification: NotificationRequest) -> None:
        lines = format_notification(notification, prefix=self._prefix)
        self.notification_count += 1
        self.emitted_lines.extend(lines)
        stream = self.stream
        stream.write("\n".join(lines) + "\n")
        flush: Callable[[], Any] | None = getattr(stream, "flush", None)
        if flush is not None:
            flush()


__all__ = [
    "ConsoleNotificationSink",
    "DEFAULT_PREFIX",
    "format_notification",
]
