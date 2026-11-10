# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ECC Object 的事件累積管理器。

持有一個 ECCObject instance，接收 pre-exec / post-exec / lineage 事件，
in-place 更新 ECC Object，並透過 ECCStore 持久化。
"""

from __future__ import annotations

import getpass
import logging
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terminux_engine.store.event_store import EventStore

from terminux_engine.ecc.ecc_object import (
    CommandRecord,
    CornerCoverage,
    ECCObject,
    EventRecord,
    GateResult,
    LineageSummary,
)
from terminux_engine.ecc.playbook import (
    Playbook,
    StageSpec,
    get_parsing_rules,
    identify_stage,
)
from terminux_engine.ecc.report_parser import parse_report, parse_stdout

if TYPE_CHECKING:
    from terminux_engine.ecc.store import ECCStore

logger = logging.getLogger(__name__)


class ECCAccumulator:
    """ECC Object 的事件累積管理器。"""

    DEDUP_WINDOW_SEC: float = 0.5  # 500ms 去重窗口

    def __init__(
        self,
        playbook: Playbook,
        store: ECCStore | None = None,
        session_id: str | None = None,
        event_store: EventStore | None = None,
        user_id: str = "",
        project_id: str | None = None,
    ) -> None:
        self._playbook = playbook
        self._store = store
        self._event_store = event_store
        self._user_id = user_id or getpass.getuser()
        self._project_id = project_id
        self._current_block_name: str | None = None

        # 去重 guard（qt_terminal lineage.pre/post_exec 與 shell_hook 同命令同時間只處理一次）
        self._last_pre_exec_cmd: str | None = None
        self._last_pre_exec_time: float = 0.0
        self._last_post_exec_cmd: str | None = None
        self._last_post_exec_time: float = 0.0

        # 嘗試從 store 恢復 session（store 為 None 時跳過）
        restored: ECCObject | None = None
        if session_id is not None and store is not None:
            restored = store.load_session(session_id)

        if restored is not None:
            self._ecc = restored
        else:
            self._ecc = ECCObject.create_new()

    # ─── helpers ──────────────────────────────────────────────────────────

    def _find_stage_spec(self, stage_id: str) -> StageSpec | None:
        """從 playbook.stage_specs 中找到指定 stage_id 的 StageSpec。"""
        for s in self._playbook.stage_specs:
            if s.id == stage_id:
                return s
        return None

    # ─── dedup guard ───────────────────────────────────────────────────────

    def _is_duplicate(self, phase: str, command: str, now: float) -> bool:
        """判斷 pre_exec / post_exec 是否為重複觸發（500ms 窗口）。

        空字串 command 不去重（保守策略）。
        """
        if not command:
            return False

        if phase == "pre_exec":
            if (self._last_pre_exec_cmd == command
                    and now - self._last_pre_exec_time < self.DEDUP_WINDOW_SEC):
                return True
            self._last_pre_exec_cmd = command
            self._last_pre_exec_time = now
            return False
        elif phase == "post_exec":
            if (self._last_post_exec_cmd == command
                    and now - self._last_post_exec_time < self.DEDUP_WINDOW_SEC):
                return True
            self._last_post_exec_cmd = command
            self._last_post_exec_time = now
            return False
        return False

    # ─── event-sourcing helpers ────────────────────────────────────────────

    def _emit(self, event_type: str, payload: dict, corner: str | None = None) -> None:
        """發送一筆 SessionEvent 到 EventStore（若存在）。失敗靜默記錄 warning。"""
        if self._event_store is None:
            return
        try:
            from terminux_engine.store.event_store import SessionEvent
            self._event_store.emit(SessionEvent(
                event_id=uuid.uuid4().hex,
                session_id=self._ecc.session_id,
                user_id=self._user_id,
                project_id=self._project_id,
                timestamp=time.time(),
                event_type=event_type,
                payload=payload,
                block_name=self._current_block_name,
            ))
        except Exception as exc:
            logger.warning("EventStore emit failed (%s): %s", event_type, exc)

    def emit_session_start(self, llm_backend: str = "") -> None:
        """發送 session_start 事件（由 jsonrpc_server._init_ecc 呼叫一次）。"""
        self._emit("session_start", {
            "playbook": self._playbook.id,
            "llm_backend": llm_backend,
            "playbook_id": getattr(self._playbook, "id", "unknown") or "unknown",
            "playbook_version": getattr(self._playbook, "version", "unknown") or "unknown",
        })

    def record_gate_results(self, gate_results: list[GateResult]) -> None:
        """記錄 gate 評估結果，emit gate_eval events。"""
        for g in gate_results:
            self._emit("gate_eval", {
                "gate_id": g.gate_id,
                "passed": g.passed,
                "actual": g.actual_value,
                "threshold": g.threshold,
                "corner": g.corner,
            }, corner=g.corner)

    # ─── public API ───────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """回傳目前 ECC Object 的 session_id。"""
        return self._ecc.session_id

    @property
    def current_block_name(self) -> str | None:
        """回傳目前的 block_name（供 jsonrpc_server 讀取）。"""
        return self._current_block_name

    def update_block_name(self, block_name: str) -> None:
        """更新目前的 block_name（由 jsonrpc_server 呼叫）。"""
        self._current_block_name = block_name

    def get_current(self) -> ECCObject:
        """回傳目前的 ECC Object（不做 save）。"""
        return self._ecc

    def on_pre_exec(
        self,
        command: str,
        cwd: str,
        tool_name: str | None = None,
        source: str = "",
        preexec_result: str = "allow",
        preexec_message: str = "",
        preexec_findings: list[dict] | None = None,
    ) -> ECCObject:
        """處理 pre-exec 事件。"""
        now = time.time()

        # 去重 guard：同命令 500ms 內重複觸發 → 跳過
        if self._is_duplicate("pre_exec", command, now):
            logger.debug("Dedup: skipping duplicate pre_exec for %r (source=%s)", command, source)
            return self._ecc

        ecc = self._ecc

        # stage 識別
        if tool_name is not None:
            stage = identify_stage(self._playbook, cwd, tool_name)
            if stage is not None and stage.id != ecc.current_stage_id:
                old_stage = ecc.current_stage_id
                gate_ids = [g.id for g in stage.quality_gates]
                ecc.update_stage(stage.id, gate_ids, now)
                # 初始化 corner coverage（若 stage 有 corner_list 且尚未初始化）
                if (
                    ecc.corner_coverage is None
                    and stage.required_reports
                    and stage.required_reports[0].corner_list
                ):
                    ecc.corner_coverage = CornerCoverage(
                        required_corners=list(stage.required_reports[0].corner_list),
                    )
                # emit stage_change event
                self._emit("stage_change", {
                    "from_stage": old_stage,
                    "to_stage": stage.id,
                })

        # 更新 work_directory
        ecc.work_directory = cwd

        # 記錄 command（preexec_result/message/findings 由 caller 帶入，反映 lineage check 真實 decision）
        record = CommandRecord(
            timestamp=now,
            command=command,
            cwd=cwd,
            tool_name=tool_name,
            preexec_result=preexec_result,
            preexec_message=preexec_message,
            preexec_findings=list(preexec_findings) if preexec_findings else [],
        )
        ecc.append_command(record)

        # emit command event
        self._emit("command", {
            "command": command,
            "cwd": cwd,
            "tool_name": tool_name,
        })

        return ecc

    def on_post_exec(
        self,
        stdout_lines: list[str],
        report_paths: list[str],
        exit_code: int,
        corner: str | None = None,
        cwd: str | None = None,
        tool_name: str | None = None,
        command: str = "",
        source: str = "",
    ) -> ECCObject:
        """處理 post-exec 事件。"""
        now = time.time()

        # 去重 guard：同命令 500ms 內重複觸發 → 跳過
        dedup_cmd = command or ""
        if dedup_cmd and self._is_duplicate("post_exec", dedup_cmd, now):
            logger.debug("Dedup: skipping duplicate post_exec for %r (source=%s)", dedup_cmd, source)
            return self._ecc

        ecc = self._ecc

        # Stage identification fallback：若 pre_exec 未設定 stage，嘗試從 cwd + tool_name 識別
        if ecc.current_stage_id is None and cwd is not None and tool_name is not None:
            stage = identify_stage(self._playbook, cwd, tool_name)
            if stage is not None:
                gate_ids = [g.id for g in stage.quality_gates]
                ecc.update_stage(stage.id, gate_ids, now)
                if (
                    ecc.corner_coverage is None
                    and stage.required_reports
                    and stage.required_reports[0].corner_list
                ):
                    ecc.corner_coverage = CornerCoverage(
                        required_corners=list(stage.required_reports[0].corner_list),
                    )
                logger.info(
                    "Post-exec stage fallback: %s (cwd=%s, tool=%s)",
                    stage.id, cwd, tool_name,
                )

        # 取得 parsing rules
        rules = []
        if ecc.current_stage_id is not None:
            stage_spec = self._find_stage_spec(ecc.current_stage_id)
            if stage_spec is not None:
                rules = get_parsing_rules(stage_spec)

        # 解析 stdout
        if rules:
            for m in parse_stdout(stdout_lines, rules, corner=corner, timestamp=now):
                ecc.append_metric(m)
                # emit metric event
                self._emit("metric", {
                    "metric_name": m.metric_name,
                    "value": m.value,
                    "corner": m.corner,
                    "source": "stdout",
                })

        # 解析 report 檔案
        if rules:
            for rp in report_paths:
                try:
                    for m in parse_report(rp, rules, corner=corner, timestamp=now):
                        ecc.append_metric(m)
                        # emit metric event
                        self._emit("metric", {
                            "metric_name": m.metric_name,
                            "value": m.value,
                            "corner": m.corner,
                            "source": rp,
                        })
                except FileNotFoundError:
                    logger.warning("Report file not found, skipping: %s", rp)

        # 更新 corner coverage
        if corner is not None and ecc.corner_coverage is not None:
            passed = exit_code == 0
            ecc.corner_coverage.record_corner(corner, passed)
            # emit corner_update event
            cc = ecc.corner_coverage
            self._emit("corner_update", {
                "corner": corner,
                "status": "pass" if passed else "fail",
                "completed": len(cc.completed_pass) + len(cc.completed_fail),
                "total": len(cc.required_corners),
            })

        # 追加 post_exec event
        ecc.append_event(EventRecord(
            timestamp=now,
            event_type="post_exec",
            detail=f"exit_code={exit_code}",
            metadata={"exit_code": str(exit_code), "corner": corner or ""},
        ))

        return ecc

    def on_lineage_trace(self, lineage_summary: LineageSummary) -> ECCObject:
        """將 lineage 追溯結果寫入 ECC Object。"""
        ecc = self._ecc
        ecc.lineage_summary = lineage_summary

        ecc.append_event(EventRecord(
            timestamp=time.time(),
            event_type="lineage_check",
            detail=f"judgment={lineage_summary.ai_judgment or 'none'}",
            metadata={"trace_complete": str(lineage_summary.trace_complete)},
        ))

        return ecc
