# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""從 client 的 Python prefix 與 PATH 尋找本機執行檔。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_client_binary(name: str, override_variable: str,
                        *, excluded: tuple[Path, ...] = ()) -> str | None:
    override = os.environ.get(override_variable)
    if override:
        return override
    prefix = Path(sys.prefix)
    for directory in (prefix / "bin", prefix.parent / "bin"):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    excluded_realpaths = {os.path.realpath(path) for path in excluded}
    for directory in os.get_exec_path():
        candidate = Path(directory or os.curdir) / name
        try:
            if (not candidate.is_file() or not os.access(candidate, os.X_OK)
                    or os.path.realpath(candidate) in excluded_realpaths):
                continue
            # PATH 可能先遇到啟動 wrapper；略過後繼續找，避免遞迴呼叫 launcher。
            with candidate.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    return str(candidate.absolute())
        except OSError:
            continue
    return None
