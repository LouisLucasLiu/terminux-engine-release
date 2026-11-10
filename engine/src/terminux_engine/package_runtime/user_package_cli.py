# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""User package CLI：install、uninstall、status／activate。

- `terminux-package-install <src_dir> [--replace]`
- `terminux-package-uninstall <package_id>`
- `terminux-package-status [package_id] [--activate]`

Install／uninstall 只改 `~/.terminux/packages`，不碰 daemon；啟用一律透過
`terminux-package-status --activate`（daemon 的 `extensions.reload`，同時載入
`terminux-tool` 登記的使用者工具）。daemon 連不上時狀態是 `UNKNOWN_DAEMON_OFFLINE`，
不推測為已載入，也不啟動或重啟 daemon。

`DISPATCH_READY` 只代表設定已就緒（Package 已載入、`when.tools` 的工具已在 daemon
目前的 activation 中、使用者工具的 wrapper 會被 PATH 選到）；是否真的觸發要以實際
執行後的 dispatch 紀錄為準。

Exit code：0 成功；1 拒絕、未啟用或 daemon 無法使用；2 非預期的內部錯誤。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from terminux_engine.package_runtime import user_packages as up
from terminux_engine.tool_identity import user_catalog as uc
from terminux_engine.tool_identity.catalog import ORIGIN_USER, CatalogError, ToolCatalog, load_catalog

STATE_LOADED = "LOADED"
STATE_NOT_LOADED = "NOT_LOADED"
STATE_GENERATION_MISMATCH = "GENERATION_MISMATCH"
STATE_REJECTED = "REJECTED"
STATE_INVALID = "INVALID"
STATE_UNKNOWN_DAEMON_OFFLINE = "UNKNOWN_DAEMON_OFFLINE"
STATE_UNKNOWN_DAEMON_ERROR = "UNKNOWN_DAEMON_ERROR"
STATE_LOADED_NOT_INSTALLED = "LOADED_NOT_INSTALLED"

_ACTIVATE_HINT = "run `terminux-package-status --activate` to load it into the running daemon"


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_refusal(prog: str, error: up.UserPackageError, fmt: str) -> None:
    if fmt == "json":
        _print_json({"status": "REFUSED", "error": error.to_dict()})
        return
    print(f"REFUSED {error.code}: {error.message}")
    for detail in error.details:
        print(f"  - {detail}")


def _short(digest: str | None) -> str:
    return digest[:19] if digest else "-"


# ── install ──────────────────────────────────────────────────────────


def install_main(argv: list[str] | None = None) -> int:
    """`terminux-package-install`：驗證並安裝一份不可變 snapshot（不會自動啟用）。"""
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(
        prog="terminux-package-install",
        description="Install a user Package into ~/.terminux/packages (copy snapshot).")
    parser.add_argument("source_dir", help="Package directory containing package.yaml")
    parser.add_argument("--replace", action="store_true",
                        help="replace an installed Package with the same id")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        result = up.install_package(args.source_dir, replace=args.replace)
    except up.UserPackageError as exc:
        _print_refusal(parser.prog, exc, args.format)
        return 1
    except Exception as exc:
        print(f"{parser.prog}: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        _print_json(result.to_dict())
        return 0
    if result.status == "UNCHANGED":
        print(f"UNCHANGED {result.package_id} {result.version}: identical content is already "
              f"installed (generation {result.generation})")
        return 0
    verb = "Replaced" if result.status == "REPLACED" else "Installed"
    print(f"{verb} user package {result.package_id} {result.version}")
    if result.previous is not None:
        print(f"  previous:   {result.previous.version} (generation {result.previous.generation})")
    print(f"  generation: {result.generation}")
    print(f"  digest:     {result.digest}")
    print(f"  location:   {result.package_dir}")
    print(f"WARNING: {up.TRUST_WARNING}")
    print(f"Not active yet: {_ACTIVATE_HINT}.")
    print("If the daemon is not running, the Package is loaded the next time Terminux starts.")
    return 0


# ── uninstall ────────────────────────────────────────────────────────


def uninstall_main(argv: list[str] | None = None) -> int:
    """`terminux-package-uninstall`：移除安裝紀錄；daemon 在下次啟用時卸載。"""
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(
        prog="terminux-package-uninstall",
        description="Uninstall a user Package from ~/.terminux/packages.")
    parser.add_argument("package_id")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        result = up.uninstall_package(args.package_id)
    except up.UserPackageError as exc:
        _print_refusal(parser.prog, exc, args.format)
        return 1
    except Exception as exc:
        print(f"{parser.prog}: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        _print_json(result.to_dict())
        return 0
    previous = (f" {result.previous.version} (generation {result.previous.generation})"
                if result.previous else "")
    print(f"Uninstalled user package {result.package_id}{previous}")
    print("The running daemon keeps it loaded until you run "
          "`terminux-package-status --activate`.")
    return 0


# ── status / activate ────────────────────────────────────────────────


def _query_daemon(activate: bool) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """回傳 (payload, unavailable_state, reason)。

    連不上 → `UNKNOWN_DAEMON_OFFLINE`；daemon 回 JSON-RPC error（拒絕、舊版、內部錯誤）
    → `UNKNOWN_DAEMON_ERROR`。兩者都不視為已載入。
    """
    from terminux_engine.orchestration.daemon_client import (
        DaemonRpcError,
        DaemonTransportError,
        OrchestrationDaemonClient,
    )

    method = "extensions.reload" if activate else "extensions.status"
    try:
        return OrchestrationDaemonClient().call(method, {}), None, None
    except DaemonTransportError as exc:
        return None, STATE_UNKNOWN_DAEMON_OFFLINE, f"daemon not reachable: {exc}"
    except DaemonRpcError as exc:
        return None, STATE_UNKNOWN_DAEMON_ERROR, (
            f"daemon returned an error for {method}: {exc.message} (code {exc.code})")


def _merge(local: list[up.LocalPackageStatus], user_scope: dict[str, Any] | None,
           unavailable_state: str | None) -> list[dict[str, Any]]:
    loaded = {item["package_id"]: item for item in (user_scope or {}).get("packages", [])}
    records = {item["package_id"]: item for item in (user_scope or {}).get("records", [])}
    rows: list[dict[str, Any]] = []
    for status in local:
        row = status.to_dict()
        daemon_loaded = loaded.get(status.package_id)
        record = records.get(status.package_id)
        row["loaded_generation"] = daemon_loaded["generation"] if daemon_loaded else None
        row["loaded_digest"] = daemon_loaded["digest"] if daemon_loaded else None
        row["daemon_reason"] = record.get("reason") if record else None
        row["daemon_message"] = record.get("message") if record else None
        if unavailable_state is not None:
            row["loaded"] = None
            row["state"] = unavailable_state
        elif daemon_loaded and (daemon_loaded["generation"], daemon_loaded["digest"]) == (
                status.generation, status.digest):
            row["loaded"] = True
            row["state"] = STATE_LOADED
        elif record and record.get("status") == up.RECORD_REJECTED:
            row["loaded"] = False
            row["state"] = STATE_REJECTED
        elif not status.valid:
            row["loaded"] = False
            row["state"] = STATE_INVALID
        elif daemon_loaded:
            row["loaded"] = False
            row["state"] = STATE_GENERATION_MISMATCH
        else:
            row["loaded"] = False
            row["state"] = STATE_NOT_LOADED
        rows.append(row)
    local_ids = {status.package_id for status in local}
    for package_id, item in sorted(loaded.items()):
        if package_id in local_ids:
            continue
        rows.append({
            "package_id": package_id, "installed": False, "valid": False, "tools": [],
            "version": item["version"], "generation": None, "digest": None,
            "reason": None, "message": None, "details": [],
            "loaded": True, "loaded_generation": item["generation"], "loaded_digest": item["digest"],
            "daemon_reason": None, "daemon_message": None, "state": STATE_LOADED_NOT_INSTALLED,
        })
    return rows


# ── 工具就緒狀態（TOOL_REGISTERED／WRAPPER_READY／DISPATCH_READY） ─────────


def _local_tool_view() -> tuple[ToolCatalog | None, uc.UserCatalogView, Any]:
    try:
        builtin: ToolCatalog | None = load_catalog()
    except CatalogError:
        builtin = None
    view = uc.read_user_catalog_view(builtin)
    try:
        home = uc.resolve_user_home()
    except uc.UserToolError as exc:
        home = exc
    return builtin, view, home


def _daemon_origin(tool: str, builtin: ToolCatalog | None, daemon_tools: dict[str, Any] | None) -> str | None:
    """daemon 目前 activation 中這個工具的來源；不認得回傳 None。"""
    if daemon_tools is not None and not daemon_tools.get("builtin_catalog_loaded", True):
        return None
    if builtin is not None and builtin.origin_of(tool) is not None:
        return "builtin"
    for item in (daemon_tools or {}).get("tools", []):
        if item.get("tool_id") == tool:
            return ORIGIN_USER
    return None


def _daemon_executables(tool: str, daemon_tools: dict[str, Any] | None) -> list[str] | None:
    for item in (daemon_tools or {}).get("tools", []):
        if item.get("tool_id") == tool:
            return list(item.get("executables", []))
    return None


def _tool_rows(tools: list[str], builtin: ToolCatalog | None, view: uc.UserCatalogView, home: Any,
               daemon_tools: dict[str, Any] | None, online: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tool in tools:
        local_origin = view.catalog.origin_of(tool) if view.catalog is not None else None
        local_exes = list(view.catalog.executables_for(tool)) if view.catalog is not None else []
        daemon_origin = _daemon_origin(tool, builtin, daemon_tools) if online else None
        row: dict[str, Any] = {
            "tool": tool,
            "local_origin": local_origin,
            "daemon_origin": daemon_origin,
            "registered": (daemon_origin is not None) if online else None,
            "pending_activation": online and (
                local_origin != daemon_origin
                or (local_origin == ORIGIN_USER and local_exes != _daemon_executables(tool, daemon_tools))),
            "wrapper": [],
            "wrapper_ready": None,
            # 磁碟上的 overlay 無效時，daemon 保留上一份 activation；重新啟用也不會成功。
            "local_catalog_invalid": view.state == uc.STATE_INVALID_BUILTIN_ONLY,
        }
        if ORIGIN_USER in (local_origin, daemon_origin):
            if isinstance(home, Exception):
                row["wrapper"] = [{"executable": tool, "ready": False, "reason": getattr(home, "code", "HOME"),
                                   "link": None, "first_on_path": None, "real_executable": None}]
            else:
                exes = local_exes or _daemon_executables(tool, daemon_tools) or [tool]
                row["wrapper"] = [uc.wrapper_readiness(name, home=home) for name in exes]
            row["wrapper_ready"] = bool(row["wrapper"]) and all(item["ready"] for item in row["wrapper"])
        rows.append(row)
    return rows


def _dispatch_readiness(row: dict[str, Any], online: bool) -> tuple[bool, str | None]:
    """第一個阻擋原因；全部通過時為 (True, None)。只是設定檢查，不是 LIVE_VERIFIED。"""
    if not online:
        return False, row["state"]
    if row["state"] != STATE_LOADED:
        return False, f"PACKAGE_{row['state']}"
    for tool in row["tool_readiness"]:
        if not tool["registered"]:
            if tool["local_origin"] is not None:
                return False, f"TOOL_REGISTRATION_PENDING_ACTIVATION:{tool['tool']}"
            return False, f"TOOL_NOT_REGISTERED:{tool['tool']}"
        if tool["pending_activation"]:
            if tool["local_catalog_invalid"]:
                return False, f"USER_TOOL_CATALOG_INVALID_ON_DISK:{tool['tool']}"
            return False, f"TOOL_REGISTRATION_PENDING_ACTIVATION:{tool['tool']}"
        if tool["daemon_origin"] == ORIGIN_USER and not tool["wrapper_ready"]:
            reasons = ",".join(f"{item['executable']}={item['reason']}" for item in tool["wrapper"]
                               if not item["ready"])
            return False, f"WRAPPER_NOT_READY:{tool['tool']}:{reasons}"
    return True, None


def _apply_tool_readiness(rows: list[dict[str, Any]], builtin: ToolCatalog | None, view: uc.UserCatalogView,
                          home: Any, daemon_tools: dict[str, Any] | None, online: bool) -> None:
    for row in rows:
        row["tool_readiness"] = _tool_rows(list(row.get("tools") or []), builtin, view, home,
                                           daemon_tools, online)
        if online and row["tool_readiness"]:
            row["tool_registered"] = all(tool["registered"] for tool in row["tool_readiness"])
        else:
            row["tool_registered"] = None
        user_tools = [tool for tool in row["tool_readiness"] if tool["wrapper_ready"] is not None]
        row["wrapper_ready"] = all(tool["wrapper_ready"] for tool in user_tools) if user_tools else None
        row["dispatch_ready"], row["dispatch_blocked_by"] = _dispatch_readiness(row, online)


def _user_tools_summary(view: uc.UserCatalogView, daemon_tools: dict[str, Any] | None, online: bool,
                        activation_id: int | None) -> dict[str, Any]:
    local_fingerprint = uc.catalog_fingerprint(view.catalog) if view.catalog is not None else None
    daemon_fingerprint = daemon_tools.get("fingerprint") if daemon_tools else None
    return {
        "local": {
            "path": str(view.path) if view.path is not None else None,
            "state": view.state,
            "digest": view.digest,
            "fingerprint": local_fingerprint,
            "error": view.error,
            "records": [record.to_dict() for record in view.records],
        },
        "daemon": daemon_tools,
        "activation_id": activation_id if online else None,
        "pending_activation": (online and daemon_tools is not None
                               and daemon_fingerprint != local_fingerprint),
    }


def _format_tools_text(summary: dict[str, Any], online: bool) -> list[str]:
    local = summary["local"]
    lines = [f"User tool catalog: {local['path'] or '(home not resolvable)'}"]
    text = f"  on disk: {local['state']}"
    if local["digest"]:
        text += f" digest={local['digest'][:19]}"
    lines.append(text)
    if local["error"]:
        lines.append(f"  on disk error: {local['error']['code']}: {local['error']['message']}")
    for record in local["records"]:
        if record["status"] != uc.STATUS_ACCEPTED:
            lines.append(f"  rejected: {record['tool_id']} {record['reason']}: {record['message']}")
    daemon = summary["daemon"]
    if not online:
        lines.append("  daemon: unknown (daemon not available)")
    elif daemon is None:
        lines.append("  daemon: no user tool scope (user extensions disabled or not supported)")
    else:
        text = f"  daemon: activation #{summary['activation_id']} {daemon['state']}"
        if daemon.get("digest"):
            text += f" digest={daemon['digest'][:19]}"
        lines.append(text)
        if daemon.get("error"):
            lines.append(f"  daemon error: {daemon['error']['code']}: {daemon['error']['message']}")
        if summary["pending_activation"] and local["error"]:
            lines.append("  The catalog on disk is invalid; the daemon keeps the user tools of activation "
                         f"#{summary['activation_id']}. Fix or restore the file, then activate again.")
        elif summary["pending_activation"]:
            lines.append(f"  PENDING_ACTIVATION: the daemon has not loaded the catalog on disk; {_ACTIVATE_HINT}")
    return lines


def _format_status_text(root: str | None, payload: dict[str, Any] | None, unavailable_state: str | None,
                        reason: str | None, rows: list[dict[str, Any]], activate: bool) -> list[str]:
    lines = [f"User package root: {root or '(not created yet)'}"]
    if unavailable_state is not None:
        lines.append(f"Daemon: {unavailable_state} ({reason})")
        if activate and unavailable_state == STATE_UNKNOWN_DAEMON_OFFLINE:
            lines.append("Activation: NOT_PERFORMED (no running daemon; nothing was started or restarted)")
        elif activate:
            lines.append("Activation: UNKNOWN (the daemon returned an error; check its state before retrying)")
    else:
        assert payload is not None
        scope = payload["scopes"]["user_packages"]
        lines.append(f"Daemon: ONLINE (user package activation #{scope['activation_id']}"
                     + (", user packages disabled" if scope["disabled"] else "") + ")")
        lines.append(f"Built-in packages loaded: {len(payload.get('builtin_packages', []))}")
        if activate:
            reload_info = payload.get("reload", {})
            text = f"Activation: {reload_info.get('status')}"
            if reload_info.get("error"):
                text += f" ({reload_info['error'].get('code')}: {reload_info['error'].get('message')})"
            lines.append(text)
        elif scope.get("error"):
            lines.append(f"Activation error: {scope['error'].get('code')}: {scope['error'].get('message')}")
    if not rows:
        lines.append("No user packages installed.")
    for row in rows:
        lines.append(f"{row['package_id']} {row.get('version') or '-'}")
        if row["installed"]:
            lines.append(f"  INSTALLED  yes  generation={row['generation']} digest={_short(row['digest'])}")
        else:
            lines.append("  INSTALLED  no")
        if row["installed"]:
            if row["valid"]:
                lines.append("  VALID      yes")
            else:
                lines.append(f"  VALID      no   {row['reason']}: {row['message']}")
        if row["state"] in (STATE_UNKNOWN_DAEMON_OFFLINE, STATE_UNKNOWN_DAEMON_ERROR):
            lines.append(f"  LOADED     {row['state']}")
        elif row["loaded"]:
            lines.append(f"  LOADED     yes  generation={row['loaded_generation']}")
        elif row["loaded_generation"]:
            lines.append(f"  LOADED     no   daemon runs generation={row['loaded_generation']}")
        else:
            lines.append("  LOADED     no")
        if row.get("daemon_reason"):
            lines.append(f"  REJECTED   {row['daemon_reason']}: {row['daemon_message']}")
        lines.append(f"  STATE      {row['state']}")
        for tool in row.get("tool_readiness", []):
            origin = tool["daemon_origin"] or tool["local_origin"] or "unknown"
            if tool["registered"] is None:
                registered = f"UNKNOWN_DAEMON (on disk: {'yes' if tool['local_origin'] else 'no'})"
            elif tool["registered"]:
                registered = "yes"
                if tool["pending_activation"]:
                    registered += (" (on-disk catalog invalid; daemon keeps the previous activation)"
                                   if tool["local_catalog_invalid"] else " (PENDING_ACTIVATION: disk differs)")
            else:
                registered = "no" + (" (registered on disk; not active yet)" if tool["local_origin"] else "")
            lines.append(f"  TOOL       {tool['tool']} origin={origin} TOOL_REGISTERED {registered}")
            for wrapper in tool["wrapper"]:
                text = f"  WRAPPER    {wrapper['executable']}: {wrapper['reason']}"
                if wrapper.get("real_executable"):
                    text += f" (wrapper first on PATH; real executable {wrapper['real_executable']})"
                lines.append(text)
        if "dispatch_ready" in row:
            if row["dispatch_ready"]:
                lines.append("  DISPATCH_READY yes (configuration only; not a live run)")
            else:
                lines.append(f"  DISPATCH_READY no  ({row['dispatch_blocked_by']})")
        if row["state"] in (STATE_NOT_LOADED, STATE_GENERATION_MISMATCH, STATE_LOADED_NOT_INSTALLED):
            lines.append(f"  next:      {_ACTIVATE_HINT}")
        elif (row.get("dispatch_blocked_by") or "").startswith("TOOL_REGISTRATION_PENDING_ACTIVATION"):
            lines.append(f"  next:      {_ACTIVATE_HINT}")
        elif (row.get("dispatch_blocked_by") or "").startswith("USER_TOOL_CATALOG_INVALID_ON_DISK"):
            lines.append("  next:      fix or restore ~/.terminux/tools/user_tool_catalog.yaml "
                         "(see the error below), then activate again")
    return lines


def status_main(argv: list[str] | None = None) -> int:
    """`terminux-package-status`：本機安裝狀態＋daemon 實際載入的 generation。"""
    from terminux_engine.version_cli import check_version_arg
    check_version_arg(argv)
    parser = argparse.ArgumentParser(
        prog="terminux-package-status",
        description="Show installed user Packages and what the running daemon has loaded.")
    parser.add_argument("package_id", nargs="?", default=None)
    parser.add_argument("--activate", action="store_true",
                        help="ask the running daemon to reload user Packages (no restart)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        try:
            root = up.resolve_user_packages_root(create=False)
            local = up.inspect_installed(root)
        except up.UserPackageError as exc:
            if exc.code != up.ROOT_MISSING:
                _print_refusal(parser.prog, exc, args.format)
                return 1
            root, local = None, []
        payload, unavailable_state, reason = _query_daemon(args.activate)
        user_scope = payload["scopes"]["user_packages"] if payload is not None else None
        rows = _merge(local, user_scope, unavailable_state)
        online = unavailable_state is None and payload is not None
        daemon_tools = payload["scopes"].get("user_tools") if payload is not None else None
        builtin, tool_view, home = _local_tool_view()
        _apply_tool_readiness(rows, builtin, tool_view, home, daemon_tools, online)
        tools_summary = _user_tools_summary(
            tool_view, daemon_tools, online, payload.get("activation_id") if payload else None)
    except Exception as exc:
        print(f"{parser.prog}: unexpected internal error: {exc}", file=sys.stderr)
        return 2

    if args.package_id is not None:
        rows = [row for row in rows if row["package_id"] == args.package_id]

    if args.format == "json":
        _print_json({
            "root": str(root) if root else None,
            "daemon": {
                "online": unavailable_state is None,
                "state": unavailable_state or "ONLINE",
                "reason": reason,
                "user_packages": user_scope,
                "builtin_packages": payload.get("builtin_packages") if payload else None,
                "reload": payload.get("reload") if payload else None,
                "activation_id": payload.get("activation_id") if payload else None,
            },
            "user_tools": tools_summary,
            "packages": rows,
        })
    else:
        print("\n".join(_format_status_text(str(root) if root else None, payload,
                                            unavailable_state, reason, rows, args.activate)))
        print("\n".join(_format_tools_text(tools_summary, online)))

    if not args.activate:
        return 0
    if unavailable_state is not None or payload is None:
        return 1
    if payload.get("reload", {}).get("status") != "ACTIVATED":
        return 1
    return 0 if all(row["state"] == STATE_LOADED for row in rows if row["installed"]) else 1


def main(argv: list[str] | None = None) -> int:
    """`python -m terminux_engine.package_runtime.user_package_cli <install|uninstall|status> ...`"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    commands = {"install": install_main, "uninstall": uninstall_main, "status": status_main}
    if not arguments or arguments[0] not in commands:
        print("usage: python -m terminux_engine.package_runtime.user_package_cli "
              "{install,uninstall,status} ...", file=sys.stderr)
        return 2
    return commands[arguments[0]](arguments[1:])


if __name__ == "__main__":
    raise SystemExit(main())
