# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Terminux CLI 的版本與授權資訊輸出。"""

from __future__ import annotations

import sys

from terminux_engine._version import __version__

VERSION_STRING = f"Terminux {__version__}"

VERSION_OUTPUT = f"""Terminux {__version__}
Copyright 2026 Liu, Hsien-Ming (Louis Liu)
License: PolyForm Shield 1.0.0 <https://polyformproject.org/licenses/shield/1.0.0/>"""


def _check_help_arg(usage: str, argv: list[str] | None = None) -> None:
    """沒有 argparse 的入口也必須在執行任何操作前處理 help。"""
    raw_args = sys.argv[1:] if argv is None else argv
    if "--help" in raw_args or "-h" in raw_args:
        print(usage)
        raise SystemExit(0)


def check_version_arg(argv: list[str] | None = None) -> bool:
    """檢查命令列引數是否包含 --version；若有則輸出標準授權版本資訊並退出。"""
    raw_args = sys.argv[1:] if argv is None else argv
    if "--version" in raw_args:
        print(VERSION_OUTPUT)
        raise SystemExit(0)
    return False


def check_version_arg_package_new(argv: list[str] | None = None) -> bool:
    """相容性保留；terminux-package-new 現在與其他 scripts 統一使用 check_version_arg。"""
    return check_version_arg(argv)
