# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import sys


if len(sys.argv) > 1 and sys.argv[1] == "purge":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="python -m terminux_engine purge",
        description="歸檔後清除指定日期區間的 EventStore 事件（建議 daemon 停止時執行）。",
    )
    parser.add_argument("--from", dest="from_date", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="結束日期 YYYY-MM-DD（含當日）")
    parser.add_argument("--force", action="store_true", help="先歸檔再刪除事件")
    args = parser.parse_args(sys.argv[2:])

    from terminux_engine.ipc.daemon_owner import check_local_database_access
    owner_check = check_local_database_access()
    if owner_check:
        sys.exit(owner_check)

    from terminux_engine.store.purge import purge_events_in_range

    db_path = str(Path.home() / ".terminux" / "ecc" / "event_store.db")
    try:
        result = purge_events_in_range(
            db_path,
            args.from_date,
            args.to_date,
            force=args.force,
        )
    except PermissionError as exc:
        print(f"拒絕執行 purge --force：{exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        parser.error(str(exc))

    if args.force:
        print(
            f"已刪除 {result['deleted']} 筆事件；"
            f"archive：{result['archive_path']}；"
            f"範圍：{result['range']['from']} 至 {result['range']['to']}。"
        )
    else:
        print(
            f"Dry-run：將影響 {result['would_delete']} 筆事件；"
            f"範圍：{result['range']['from']} 至 {result['range']['to']}；"
            "資料庫未變更。"
        )
    sys.exit(0)

from terminux_engine.main import main

main()
