# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Orchestration production 命令列入口（daemon-only 八項命令）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

import sys

from terminux_engine.orchestration.daemon_client import (
    DaemonRpcError,
    DaemonTransportError,
    OrchestrationDaemonClient,
)
from terminux_engine.orchestration.session_bootstrap import persistent_cli_session

class _CliUsageError(RuntimeError):
    """本地 argument／config 驗證失敗；CLI 層以 exit 2 呈現，不外露 traceback。"""


def _parse_config_json(raw: str | None) -> dict[str, Any]:
    """`--config` 省略時回傳 `{}`；提供時只接受可 decode 為 JSON object 的字串。"""
    if raw is None:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _CliUsageError(f"--config must be valid JSON: {error}") from error
    if type(decoded) is not dict:
        raise _CliUsageError("--config must decode to a JSON object")
    return decoded


def _build_orchestration_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """依 subcommand 組出 (JSON-RPC method, params)；只送使用者實際提供的 optional key。"""

    command = args.command
    if command == "start":
        config = _parse_config_json(args.config)
        return "orchestration.start", {"workflow": args.workflow, "config": config}
    if command == "status":
        return "orchestration.status", {"run_id": args.run_id}
    if command == "list":
        params: dict[str, Any] = {}
        if args.workflow_type is not None:
            params["workflow_type"] = args.workflow_type
        if args.status is not None:
            params["status"] = args.status
        if args.pending_review:
            params["pending_review"] = True
        if args.pending_review and args.status not in (None, "abandoned"):
            raise _CliUsageError(
                "--pending-review requires --status to be omitted or 'abandoned'"
            )
        if args.limit is not None:
            if args.limit < 1 or args.limit > 100:
                raise _CliUsageError("--limit must be between 1 and 100")
            params["limit"] = args.limit
        return "orchestration.list_runs", params
    if command == "pause":
        return "orchestration.pause", {"run_id": args.run_id}
    if command == "resume":
        return "orchestration.resume", {"run_id": args.run_id}
    if command == "cancel":
        params = {"run_id": args.run_id}
        if args.immediate:
            params["immediate"] = True
        return "orchestration.cancel", params
    if command == "retry":
        params = {"run_id": args.run_id}
        if args.step_id is not None:
            params["step_id"] = args.step_id
        return "orchestration.retry", params
    if command == "abandon":
        return "orchestration.abandon", {"run_id": args.run_id}
    raise AssertionError(f"unhandled orchestration command: {command}")


def _exit_code_for_rpc_error(error: DaemonRpcError) -> int:
    if error.code == -32602:
        return 2
    if error.code == -32004:
        return 3
    return 1  # -32603 或其他 daemon error


def _run_orchestration_command(args: argparse.Namespace) -> int:
    """daemon-only：本地 validation → JSON-RPC call → 印出 inner result 或錯誤。"""

    try:
        method, params = _build_orchestration_request(args)
    except _CliUsageError as error:
        print(str(error), file=sys.stderr)
        return 2

    # session scope isolation：orchestration.* 是 context-required
    # method，`persistent_cli_session()` 在整個 daemon call 期間持有
    # core-socket 連線（session id 綁定該連線生命週期，見
    # session_bootstrap.py docstring：daemon 不可達或環境沒有 IPCServer 時
    # session_id 為 None，之後的 daemon call 會因缺 context 收到明確的
    # SESSION_CONTEXT_REQUIRED 錯誤，不在此處靜默失敗）。
    with persistent_cli_session() as session:
        client = OrchestrationDaemonClient(session_id=session.session_id)
        try:
            result = client.call(method, params)
        except DaemonRpcError as error:
            print(f"orchestration daemon error {error.code}: {error.message}", file=sys.stderr)
            return _exit_code_for_rpc_error(error)
        except DaemonTransportError as error:
            print(str(error), file=sys.stderr)
            return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析 G-3 daemon-only 八項 orchestration 子命令。"""
    parser = argparse.ArgumentParser(prog="terminux-orchestration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("workflow")
    start.add_argument("--config", default=None)

    status = subparsers.add_parser("status")
    status.add_argument("run_id")

    list_runs = subparsers.add_parser("list")
    list_runs.add_argument("--workflow-type", dest="workflow_type", default=None)
    list_runs.add_argument("--status", dest="status", default=None)
    list_runs.add_argument("--pending-review", dest="pending_review", action="store_true")
    list_runs.add_argument("--limit", type=int, default=None)

    pause = subparsers.add_parser("pause")
    pause.add_argument("run_id")

    resume = subparsers.add_parser("resume")
    resume.add_argument("run_id")

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("run_id")
    cancel.add_argument("--immediate", action="store_true")

    retry = subparsers.add_parser("retry")
    retry.add_argument("run_id")
    retry.add_argument("--step-id", dest="step_id", default=None)

    abandon = subparsers.add_parser("abandon")
    abandon.add_argument("run_id")

    args = parser.parse_args(argv)
    return _run_orchestration_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
