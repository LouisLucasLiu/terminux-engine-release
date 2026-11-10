# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Engineering Work 正式 CLI：維持 terminux session，再呼叫 lineage work RPC。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from terminux_engine.orchestration.daemon_client import OrchestrationDaemonClient
from terminux_engine.ipc.agent_socket import client_agent_socket, client_lineage_socket
from terminux_engine.release.support_export import summarize_work


class SessionConnection:
    """持有 agent socket connection，避免 session 在 lineage 呼叫前被撤銷。"""

    def __init__(self, path: str) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(path)
        self._stream = self._socket.makefile("rwb")

    def init(self, *, project_id: str | None) -> str:
        params: dict[str, Any] = {"member_id": getpass.getuser(),
                                  "shell": os.environ.get("SHELL", "unknown"),
                                  "work_directory": os.getcwd()}
        if project_id is not None:
            params["project_id"] = project_id
        result = self.call("session.init", params)
        return str(result["session_id"])

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid4().hex
        self._stream.write((json.dumps({"jsonrpc": "2.0", "id": request_id,
                                        "method": method, "params": params}) + "\n").encode())
        self._stream.flush()
        response = json.loads(self._stream.readline().decode())
        if "error" in response:
            raise RuntimeError(response["error"].get("message", "agent socket error"))
        return response["result"]

    def close(self) -> None:
        self._stream.close()
        self._socket.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terminux-work")
    parser.add_argument("--agent-socket")
    parser.add_argument("--lineage-socket")
    parser.add_argument("--project")
    parser.add_argument("--profile-path",
                        help="在這次 session 先選用 Profile，再執行 work 操作")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("profile_id")
    create.add_argument("package_id")
    create.add_argument("objective")
    create.add_argument("--block")
    create.add_argument("--method")
    create.add_argument(
        "--source-root",
        help="選用 Profile 限制內的來源；平台於核准後建立 snapshot／manifest")
    create.add_argument("--criterion", action="append", default=[])
    create.add_argument("--constraint", action="append", default=[])
    create.add_argument(
        "--expert", action="append", default=[],
        help="限制本 work 可選的 Expert；可重複指定")
    create.add_argument(
        "--review", action="append", default=[],
        choices=("proposal_review", "result_review"),
        help="要求的協作審查；可重複指定")
    saved = sub.add_parser("create-from-saved-proposal")
    saved.add_argument("profile_id")
    saved.add_argument("source_work_id")
    saved.add_argument("source_proposal_id")
    saved.add_argument("expected_proposal_digest")
    saved.add_argument("objective")
    saved.add_argument("--source-root")
    saved.add_argument("--criterion", action="append", default=[])
    saved.add_argument("--constraint", action="append", default=[])
    saved.add_argument("--request-id")
    authorize = sub.add_parser("authorize")
    authorize.add_argument("work_id")
    authorize.add_argument("--mode", choices=("PLAN_ONLY", "PER_PROPOSAL", "BOUNDED_EXPLORATION"),
                           default="BOUNDED_EXPLORATION")
    authorize.add_argument("--attempts", type=int, default=2)
    authorize.add_argument("--provider-dispatches", type=int, default=12)
    authorize.add_argument("--tool-launches", type=int, default=5)
    # opt-in sub-budget：未指定就完全不進 limits，既有 authorization digest 不變。
    authorize.add_argument("--formal-proof-launches", type=int, default=None)
    authorize.add_argument("--candidate-materializations", type=int, default=2)
    # baseline collection 的用途額度。不給就是 0：Work 不得自行收集 baseline。
    authorize.add_argument("--baseline-tool-launches", type=int, default=None)
    authorize.add_argument("--allowed-method", action="append")
    authorize.add_argument("--allowed-change-json")
    authorize.add_argument("--writable-root", action="append")
    authorize.add_argument("--stop-condition", action="append")
    for name in ("approve", "continue", "get", "config", "export", "pause", "resume", "cancel", "takeover"):
        command = sub.add_parser(name)
        command.add_argument("work_id")
    sub.add_parser("list")
    answer = sub.add_parser("answer")
    answer.add_argument("work_id")
    answer.add_argument("message")
    answer.add_argument("--objective")
    answer.add_argument("--method")
    return parser


def main(argv: list[str] | None = None) -> int:
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    args = _parser().parse_args(argv)
    agent_socket = args.agent_socket or str(client_agent_socket().path)
    session = SessionConnection(agent_socket)
    try:
        session_id = session.init(project_id=args.project)
        client = OrchestrationDaemonClient(socket_path=args.lineage_socket or str(client_lineage_socket().path),
                                            session_id=session_id)
        if args.profile_path:
            selected = client.call("agent.flow_task.select_profile",
                                   {"profile_path": args.profile_path})
            if selected.get("status") != "ok":
                raise RuntimeError(selected.get("message", "Profile selection failed"))
        if args.command == "create":
            method = "agent.eap3.work.create"
            params = {"profile_id": args.profile_id, "package_id": args.package_id,
                      "objective": args.objective, "criteria": args.criterion,
                      "constraints": args.constraint}
            if args.block:
                params["block"] = args.block
            if args.method:
                params["method"] = args.method
            if args.source_root:
                params["source_root"] = str(Path(args.source_root).resolve())
            if args.expert:
                params["preferred_experts"] = args.expert
            if args.review:
                params["required_reviews"] = args.review
        elif args.command == "create-from-saved-proposal":
            method = "agent.eap3.work.create_from_saved_proposal"
            params = {
                "profile_id": args.profile_id,
                "source_work_id": args.source_work_id,
                "source_proposal_id": args.source_proposal_id,
                "expected_proposal_digest": args.expected_proposal_digest,
                "objective": args.objective,
                "criteria": args.criterion,
                "constraints": args.constraint,
            }
            if args.source_root:
                params["source_root"] = str(Path(args.source_root).resolve())
            if args.request_id:
                params["request_id"] = args.request_id
        elif args.command == "authorize":
            method = "agent.eap3.work.authorization_preview"
            params = {"work_id": args.work_id, "mode": args.mode,
                      "limits": {"engineering_attempts": args.attempts,
                                 "provider_dispatches": args.provider_dispatches,
                                 "tool_launches": args.tool_launches,
                                 "candidate_materializations": args.candidate_materializations}}
            if args.formal_proof_launches is not None:
                params["limits"]["formal_proof_launches"] = args.formal_proof_launches
            if args.baseline_tool_launches is not None:
                params["limits"]["baseline_tool_launches"] = args.baseline_tool_launches
            if args.allowed_method:
                params["allowed_methods"] = args.allowed_method
            if args.allowed_change_json:
                params["allowed_changes"] = json.loads(args.allowed_change_json)
            if args.writable_root:
                params["writable_roots"] = args.writable_root
            if args.stop_condition:
                params["stop_conditions"] = args.stop_condition
        elif args.command == "list":
            method, params = "agent.eap3.work.list", {}
        elif args.command == "export":
            method, params = "agent.eap3.work.get", {"work_id": args.work_id}
        elif args.command == "config":
            method, params = "agent.eap3.work.effective_config", {"work_id": args.work_id}
        elif args.command == "answer":
            method = "agent.eap3.work.answer"
            params = {"work_id": args.work_id, "message": args.message}
            if args.objective:
                params["objective"] = args.objective
            if args.method:
                params["method"] = args.method
        else:
            method = "agent.eap3.work." + args.command
            params = {"work_id": args.work_id}
        result = client.call(method, params)
        if args.command == "export":
            if result.get("status") != "ok" or not isinstance(result.get("work"), dict):
                print(json.dumps({"status": "error", "error_code": result.get("error_code")},
                                 ensure_ascii=False, indent=2))
                return 2
            result = summarize_work(result["work"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["SessionConnection", "main"]
