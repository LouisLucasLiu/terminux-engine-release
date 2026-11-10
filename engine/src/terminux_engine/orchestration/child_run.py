# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Daemon 派生的 BE ECO orchestration child entry point。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.checkpoint import _validate_json_native
from terminux_engine.orchestration.builtin_tools import get_default_registry
from terminux_engine.orchestration.kernel import OrchestrationKernel
from terminux_engine.orchestration.ledger import OrchestrationLedger
from terminux_engine.orchestration.runner_factory import (
    build_tool_registry,
    build_domain_runner_and_engine,
    is_supported_workflow,
    unsupported_workflow_error,
)
from terminux_engine.orchestration.ipc_llm import IpcRequestError, JsonRpcIpcClient


_LOG = logging.getLogger(__name__)

_BE_ECO_RUNNER_MODULE = "terminux_engine.orchestration.be_eco_runner"

_BE_ECO_SUPPORTED_LANGUAGES = frozenset({"zh-Hant", "en", "zh-Hans"})
_BE_ECO_DEFAULT_LANGUAGE = "zh-Hant"


def _resolve_be_eco_language(parent_daemon_socket: str) -> str:
    """每次 BE ECO child spawn 皆須以既有 explicit endpoint
    呼叫 `preferences.get_language`；transport／RPC failure 在 kernel 執行
    前 fail closed；missing/null/empty/invalid language 則 fallback
    `zh-Hant`。不對 endpoint 做 env/default fallback。"""
    client = JsonRpcIpcClient(parent_daemon_socket)
    try:
        result = client.call("preferences.get_language", {})
    except (IpcRequestError, OSError, ValueError) as exc:
        raise ValueError(
            "BE ECO child failed to resolve daemon language preference"
        ) from exc
    language = result.get("language")
    if isinstance(language, str) and language in _BE_ECO_SUPPORTED_LANGUAGES:
        return language
    return _BE_ECO_DEFAULT_LANGUAGE


def _is_be_eco_runner(runner: Any) -> bool:
    """runner 是否為 `BeEcoRunner`，不為此載入 be_eco。

    BeEcoRunner 的實例存在，代表它的模組已經載入；模組未載入（含未安裝 be_eco）
    時不可能是 BeEcoRunner。
    """
    module = sys.modules.get(_BE_ECO_RUNNER_MODULE)
    return module is not None and isinstance(runner, module.BeEcoRunner)


class _HeartbeatThread:
    """獨立於 kernel checkpoint 的 child heartbeat writer。"""

    def __init__(
        self,
        ledger: OrchestrationLedger,
        run_id: str,
        *,
        interval_seconds: float = 15.0,
    ) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name=f"orchestration-heartbeat-{run_id}",
        )

    def start(self) -> None:
        """同步寫入首次 heartbeat 後啟動週期 writer。"""
        self._write_heartbeat()
        self._thread.start()

    def stop(self) -> None:
        """停止 writer 並等待其結束。"""
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval_seconds)

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self._write_heartbeat()

    def _write_heartbeat(self) -> None:
        try:
            run = self._ledger.get_run(self._run_id)
            if run is None or run.status != "running":
                return
            # Retry child 必須先經 conditional claim 寫入自己的 durable PID，
            # 不能沿用 normal child 的 child_pid 為 NULL 相容行為取得 ownership。
            if run.parent_run_id is not None and run.child_pid != os.getpid():
                return
            # Resume child 在 claim 前仍是 paused，或已由其他 child 取得 PID 時不得寫入。
            if run.parent_run_id is None and run.child_pid is not None and run.child_pid != os.getpid():
                return
            self._ledger.update_heartbeat(self._run_id)
        except Exception:
            _LOG.exception("Orchestration heartbeat write failed: run_id=%s", self._run_id)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="terminux-orchestration-child")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--config-json")
    parser.add_argument(
        "--parent-daemon-socket", dest="parent_daemon_socket", default=None
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--retry", action="store_true")
    return parser.parse_args(argv)


def _artifact_capture_for(workflow: str, *, artifact_root: str | None = None) -> Any:
    """依 workflow 決定是否注入 artifact capture。"""
    from terminux_engine.orchestration.orfs_single_run_runner import (
        WORKFLOW_TYPE as ORFS_SINGLE_RUN_WORKFLOW,
    )

    if workflow not in {ORFS_SINGLE_RUN_WORKFLOW, "package_single_tool"}:
        return None
    try:
        from terminux_engine.lineage.artifact_capture import ArtifactCaptureService

        return ArtifactCaptureService.with_default_root(artifact_root)
    except Exception:  # noqa: BLE001 - capture 不可用不得升級成執行失敗
        _LOG.warning("artifact capture service unavailable for %s", workflow)
        return None


def _project_run_receipt(kernel: Any, outcome: Any, workflow: str) -> None:
    """把本次執行的 receipt 投影進 EventStore。

    daemon child 路徑原本不投影 receipt（只有 `orfs_cli` 有），
    因此 `receipt_projection_reader` 即使接上也讀不到東西。這裡沿用同一組
    正式 helper（`build_receipt` ＋ `project_execution_receipt`），
    **不新建第二套 receipt 格式**。

    投影失敗只記 log：它是 evidence，不是 lifecycle authority，不得把它的
    失敗升級成執行失敗。
    """
    from terminux_engine.orchestration.orfs_single_run_runner import (
        WORKFLOW_TYPE as ORFS_SINGLE_RUN_WORKFLOW,
    )

    if workflow not in {ORFS_SINGLE_RUN_WORKFLOW, "package_single_tool"} \
            or outcome is None or not outcome.steps:
        return
    try:
        import getpass
        import os

        from terminux_engine.execution_receipts.wiring import project_execution_receipt
        from terminux_engine.orchestration.execution_contract import build_receipt
        from terminux_engine.store.event_store import EventStore

        step_outcome = outcome.steps[0]
        tool_id = getattr(step_outcome, "tool_id", None) or "orfs"
        receipt = build_receipt(
            run_id=outcome.run_id, step_outcome=step_outcome,
            event_tool_name=kernel._tool_registry.event_tool_name(tool_id),
            adapter_id=kernel._tool_registry.adapter_id(tool_id),
            adapter_version=kernel._tool_registry.adapter_version(tool_id),
        )
        store_path = os.environ.get(
            "TERMINUX_EVENT_STORE_PATH",
            os.path.expanduser("~/.terminux/ecc/event_store.db"))
        project_execution_receipt(
            receipt, event_store=EventStore(store_path),
            session_id="orchestration-child", user_id=getpass.getuser(),
            project_id=None,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("execution receipt projection failed: run_id=%s",
                       getattr(outcome, "run_id", None))


def _build_kernel(
    ledger: OrchestrationLedger,
    *,
    run_id: str,
    workflow: str,
    config: dict[str, Any],
    parent_daemon_socket: str | None,
    shutdown_requested: threading.Event,
) -> OrchestrationKernel:
    """依 workflow 建立正式 domain runner；unsupported workflow 在 kernel 前 fail closed。

    workflow selection 集中於
    `runner_factory.build_domain_runner_and_engine()`；本函式只負責
    workflow-agnostic 的 language preflight 與 socket 檢查，不再硬寫
    `BeEcoRunner(...)`。
    """

    if not is_supported_workflow(workflow):
        raise unsupported_workflow_error(workflow)

    if not parent_daemon_socket:
        raise ValueError(
            "orchestration child requires --parent-daemon-socket to reach the parent daemon"
        )

    # 每次建構 kernel 前，先以同一 explicit endpoint 完成
    # language preflight；transport／RPC failure 在此 fail closed（不建立
    # kernel），故必須發生在下方 OrchestrationKernel 建構之前。此規則對
    # `eda_convergence` 同樣適用（domain-neutral，非 BE ECO 專屬行為）。
    language = _resolve_be_eco_language(parent_daemon_socket)

    runner, engine = build_domain_runner_and_engine(
        workflow=workflow,
        config=config,
        parent_daemon_socket=parent_daemon_socket,
        language=language,
        orchestration_run_id=run_id,
    )
    return OrchestrationKernel(
        engine=engine,
        ledger=ledger,
        # daemon child 路徑原本沒有 artifact capture（只有
        # `orfs_cli` 有），因此新 run 不會有 output manifest。本 workflow
        # 的完成證據需要它，故在此注入既有的 capture service；其他
        # workflow 維持 None，行為不變。
        artifact_capture_service=_artifact_capture_for(
            workflow, artifact_root=(config.get("artifact_root")
                                     if workflow in {"orfs_single_run", "package_single_tool"}
                                     else None)),
        # tool registry 也依 workflow 決定——`orfs_single_run` 需要
        # ORFS tool，而 `builtin_tools` 的 registry 沒有它。既有兩個
        # workflow 仍拿到同一份 default registry，行為不變。
        # 把 ledger 內這筆 run 的 metadata 交給 registry builder，
        # 讓 host 綁進 grant 的 `agent_task.execution`／identity 能到達 adapter。
        tool_registry=build_tool_registry(
            workflow=workflow, config=config, run_id=run_id,
            run_metadata=_run_metadata_for(ledger, run_id)),
        runner=runner,
        shutdown_requested=shutdown_requested,
    )


def _run_metadata_for(ledger: OrchestrationLedger, run_id: str) -> dict[str, Any] | None:
    """讀回 ledger 內這筆 run 的 metadata（host 建立、不可變）。讀不到回 None。"""
    try:
        run = ledger.get_run(run_id)
    except Exception:  # noqa: BLE001 - 沒有 metadata 就是沒有 agent_task 子樹
        return None
    metadata = getattr(run, "metadata", None) if run is not None else None
    return dict(metadata) if isinstance(metadata, dict) else None


def _workflow_config_from_metadata(
    ledger: OrchestrationLedger,
    run_id: str,
) -> dict[str, Any]:
    """restore mode 只讀取 run 持久化的正式 workflow config。"""

    run = ledger.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    if type(run.metadata) is not dict:
        raise ValueError(f"run lacks orchestration metadata: {run_id}")
    orchestration = run.metadata.get("orchestration")
    if type(orchestration) is not dict:
        raise ValueError(f"run lacks orchestration metadata: {run_id}")
    schema_version = orchestration.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError(f"unsupported orchestration metadata schema: {run_id}")
    workflow_config = orchestration.get("workflow_config")
    if type(workflow_config) is not dict:
        raise ValueError(f"run workflow_config is invalid: {run_id}")
    _validate_json_native(workflow_config, path="$.orchestration.workflow_config")
    return workflow_config


def _write_be_eco_run_result(
    workspace_root: Path,
    run_id: str,
    evidence: dict[str, Any],
) -> None:
    """best-effort atomic write；使用 sibling temporary
    file + atomic replace，不得產生半份正式結果；write failure 只 log，
    不得改寫已完成的 orchestration terminal transition。"""
    target = workspace_root / f"be_eco_run_result.{run_id}.json"
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(evidence, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except Exception:
        _LOG.exception("Orchestration BE ECO run evidence write failed: run_id=%s", run_id)
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def run_child(argv: list[str] | None = None) -> int:
    """執行一次 child BE ECO kernel run。"""
    args = _parse_args(argv)
    ledger = OrchestrationLedger(Path(args.ledger_path))
    heartbeat = _HeartbeatThread(ledger, args.run_id)
    shutdown_requested = threading.Event()

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        shutdown_requested.set()

    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        if args.resume or args.retry:
            config = _workflow_config_from_metadata(ledger, args.run_id)
        else:
            if args.config_json is None:
                # task-owned descriptor config 超過 argv 上限時，唯一允許的
                # fallback 是同一筆 host-created ledger metadata；不讀 env、
                # 不接受 caller path，也不另建 config store。
                config = _workflow_config_from_metadata(ledger, args.run_id)
            else:
                config = json.loads(args.config_json)
                if not isinstance(config, dict):
                    raise ValueError("--config-json must encode an object")
        heartbeat.start()
        kernel = _build_kernel(
            ledger,
            run_id=args.run_id,
            workflow=args.workflow,
            config=config,
            parent_daemon_socket=args.parent_daemon_socket,
            shutdown_requested=shutdown_requested,
        )
        if args.resume:
            outcome = kernel.run_from_checkpoint(
                run_id=args.run_id,
                mode="resume",
                child_pid=os.getpid(),
            )
        elif args.retry:
            outcome = kernel.run_from_checkpoint(
                run_id=args.run_id,
                mode="retry",
                child_pid=os.getpid(),
            )
        else:
            outcome = kernel.run(config, run_id=args.run_id)
        # 本 workflow 的完成證據需要 durable receipt projection。
        _project_run_receipt(kernel, outcome, args.workflow)
        # 只在 kernel 真正到達 terminal（outcome 非 None）時
        # best-effort 寫 evidence result file；paused／claim-loser 等非
        # terminal exit（outcome 為 None）不具備 authoritative terminal
        # status/reason，不得捏造，故不寫。
        if outcome is not None and _is_be_eco_runner(kernel._runner):
            try:
                evidence = kernel._runner.collect_run_evidence(
                    outcome.run_id,
                    terminal_status=outcome.final_status,
                    terminal_reason=outcome.terminal_reason,
                )
                _write_be_eco_run_result(
                    Path(config["workspace"]), outcome.run_id, evidence
                )
            except Exception:
                _LOG.exception(
                    "Orchestration BE ECO run evidence aggregation failed: run_id=%s",
                    args.run_id,
                )
        return 0
    finally:
        heartbeat.stop()
        signal.signal(signal.SIGTERM, previous_handler)
        ledger.close()


def main(argv: list[str] | None = None) -> int:
    """child command-line main。"""
    try:
        return run_child(argv)
    except SystemExit:
        return 0
    except Exception:
        _LOG.exception("Orchestration child failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
