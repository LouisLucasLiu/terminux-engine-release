# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""CLI：供 shell/wrapper installation 讀取 catalog（不把 catalog 內容複製進
shell array，改由 wrapper 端呼叫本 CLI 取得 executable basename 清單）。

Usage:
    python3 -m terminux_engine.tool_identity --list-executables
        逐行印出 catalog 內所有 executable basename（含 alias），
        deterministic 順序（依 catalog 檔案內宣告順序）。
    python3 -m terminux_engine.tool_identity --list-executables --include-user
        先印出 catalog 內容，再附加本 UID 使用者工具 overlay
        （`~/.terminux/tools/user_tool_catalog.yaml`）中通過檢查的 executable。
        overlay 有問題只在 stderr 說明，不影響內建清單與 exit code。

Exit code 非 0 代表 catalog malformed／找不到檔案，stderr 印出原因；
呼叫端（shell script）應視為 fatal error，不得靜默忽略後 fallback 到
hidden hardcoded list。
"""

from __future__ import annotations

import argparse
import sys

from terminux_engine.tool_identity.catalog import CatalogError, ToolCatalog, load_catalog


def _print_user_executables(catalog: ToolCatalog) -> None:
    """附加使用者工具；overlay 問題只寫 stderr，內建清單已完整印出。"""
    from terminux_engine.tool_identity.user_catalog import (
        STATUS_ACCEPTED,
        read_user_catalog_view,
    )

    try:
        view = read_user_catalog_view(catalog)
    except Exception as exc:  # 內建清單已完整印出；使用者部分出錯只說明，不改 exit code
        print(f"terminux_engine.tool_identity: user tool catalog ignored: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return
    if view.error is not None:
        print(f"terminux_engine.tool_identity: user tool catalog ignored: "
              f"{view.error['code']}: {view.error['message']}", file=sys.stderr)
    for record in view.records:
        if record.status == STATUS_ACCEPTED:
            for name in record.executables:
                print(name)
        else:
            print(f"terminux_engine.tool_identity: user tool {record.tool_id!r} skipped: "
                  f"{record.reason}: {record.message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m terminux_engine.tool_identity")
    parser.add_argument(
        "--list-executables",
        action="store_true",
        help="逐行印出 catalog 內所有 executable basename（含 alias）",
    )
    parser.add_argument(
        "--include-user",
        action="store_true",
        help="附加使用者工具 overlay 中通過檢查的 executable（需搭配 --list-executables）",
    )
    parser.add_argument(
        "--catalog-path",
        default=None,
        help="覆寫 catalog 檔案路徑（預設用 repo 內建路徑）",
    )
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog_path)
    except CatalogError as exc:
        print(f"terminux_engine.tool_identity: {exc}", file=sys.stderr)
        return 1

    if args.list_executables:
        for name in catalog.executables():
            print(name)
        if args.include_user:
            _print_user_executables(catalog)
        return 0

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
