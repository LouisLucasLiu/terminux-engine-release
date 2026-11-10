# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""On-demand attachment payload。"""

from __future__ import annotations

import base64
from dataclasses import dataclass


@dataclass
class AttachmentPayload:
    """明確載入 attachment 後的 payload；JSON 輸出使用 base64。"""

    attachment_ref: str
    filename: str
    mime_type: str | None
    size_bytes: int
    content: bytes

    def to_json_safe(self) -> dict[str, object]:
        return {
            "attachment_ref": self.attachment_ref,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "content": base64.b64encode(self.content).decode("ascii"),
            "encoding": "base64",
        }

