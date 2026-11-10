# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""DQE Viewer source adapter base contract。"""

from __future__ import annotations


class SourceAdapter:
    """Local Python adapter contract。"""

    def list_groups(self, query):
        raise NotImplementedError

    def load_group(self, group_ref: str):
        raise NotImplementedError

    def load_attachment(self, attachment_ref: str, max_bytes: int | None = None):
        raise NotImplementedError
