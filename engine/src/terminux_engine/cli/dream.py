# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""terminux-dream CLI — Daily cron orchestration 的薄包裝。

Usage:
    terminux-dream --user-id USER --date DATE
    terminux-dream --user-id USER --backfill --days N
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 參數。"""
    parser = argparse.ArgumentParser(
        prog="terminux-dream",
        description="Daily cron：整理 events 成 period_digest + daily_digest。",
    )
    parser.add_argument(
        "--user-id", required=True, help="使用者 ID",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--date", dest="date_str",
        help="日期（yesterday / today / YYYY-MM-DD）",
    )
    group.add_argument(
        "--backfill", action="store_true", default=False,
        help="啟用 backfill 模式（與 --date 互斥）",
    )

    parser.add_argument(
        "--days", type=int, default=1,
        help="backfill 天數（需配 --backfill，N >= 1）",
    )

    args = parser.parse_args(argv)

    # 驗證 --days 只在 --backfill 時有意義
    if args.backfill and args.days < 1:
        parser.error("--days must be >= 1")

    return args


def _resolve_dates(args: argparse.Namespace) -> list[str]:
    """將 CLI 參數解析為日期字串列表。"""
    today = date.today()
    yesterday = today - timedelta(days=1)

    if args.backfill:
        # 從昨天往前 N 天
        return [
            (yesterday - timedelta(days=i)).isoformat()
            for i in range(args.days)
        ]

    # --date 模式
    raw = args.date_str
    if raw == "yesterday":
        return [yesterday.isoformat()]
    elif raw == "today":
        return [today.isoformat()]
    else:
        # 驗證 YYYY-MM-DD 格式
        try:
            date.fromisoformat(raw)
        except ValueError:
            print(f"Error: invalid date format: {raw}", file=sys.stderr)
            sys.exit(2)
        return [raw]


def _init_infrastructure() -> tuple:
    """初始化 EventStore + LLMRouter（與 daemon 相同方式）。

    回傳 (event_store, llm_router)。
    """
    from terminux_engine.store.event_store import EventStore

    # EventStore
    event_store_path = os.environ.get(
        "TERMINUX_EVENT_STORE_PATH",
        os.path.expanduser("~/.terminux/ecc/event_store.db"),
    )
    event_store = EventStore(event_store_path)

    # LLMRouter
    llm_router = None
    try:
        from terminux_engine.llm.llm_router import LLMRouter
        from terminux_engine.llm.router_config import load_router_config

        from terminux_engine.llm.task_scope import contain_router_config, profile_containment_active

        router_config = contain_router_config(load_router_config())
        if profile_containment_active():
            # cron 程序沒有 daemon 的 log handler；以 stderr 一行公告遠端 provider 已收斂。
            print("TERM_PROFILE_CONTAINMENT disabled=remote_router_providers", file=sys.stderr)
        llm_router = LLMRouter(router_config)
    except Exception as exc:
        print(f"Warning: LLMRouter initialization failed: {exc}", file=sys.stderr)
        # 使用不可用的 router — run_daily_cron 仍可跑（LLM 失敗 → llm_summary=""）
        llm_router = _NoOpLLMRouter()

    return event_store, llm_router


class _NoOpLLMRouter:
    """LLM 不可用時的 fallback，讓 digest 仍可產出（llm_summary=""）。"""

    def generate(self, prompt: str, **kwargs: object) -> str:
        return ""


def main(argv: list[str] | None = None) -> int:
    """CLI 主進入點。回傳 exit code（0=成功、1=部分失敗、2=參數錯誤）。"""
    args = _parse_args(argv)
    dates = _resolve_dates(args)

    from terminux_engine.ipc.daemon_owner import check_local_database_access
    owner_check = check_local_database_access()
    if owner_check:
        return owner_check

    from terminux_engine.memory.dreaming import Dreamer, run_daily_cron

    event_store, llm_router = _init_infrastructure()
    dreamer = Dreamer(event_store=event_store, llm_router=llm_router)

    has_errors = False
    try:
        for date_str in dates:
            result = run_daily_cron(
                event_store=event_store,
                llm_router=llm_router,
                user_id=args.user_id,
                date_str=date_str,
                dreamer=dreamer,
            )

            # 輸出結果
            daily_status = "ok" if result.daily_digest_created else "FAIL"
            print(
                f"[{result.date}] periods={result.periods_processed} "
                f"appended={result.full_digests_appended} "
                f"upgraded={result.immediate_digests_upgraded} "
                f"daily={daily_status} errors={len(result.errors)}"
            )

            if result.errors:
                has_errors = True
                for err in result.errors:
                    print(f"  ERROR: {err}")
    finally:
        dreamer.close()

    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
