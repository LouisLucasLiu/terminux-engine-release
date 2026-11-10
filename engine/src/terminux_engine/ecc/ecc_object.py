# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""ECC Object 資料模型定義。

Execution-Context-and-Content Object 及其所有子結構，
使用 Pydantic v2 BaseModel。純資料模型，不含 I/O 或業務邏輯。
"""

from __future__ import annotations

import uuid
import time
from datetime import datetime, timezone

from pydantic import BaseModel


# ─── 工具函式 ─────────────────────────────────────────────────────────────────

def make_correlation_key(session_id: str, updated_at: float) -> str:
    """由 session_id + ISO timestamp 組成 correlation_key。"""
    iso = datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat()
    return f"{session_id}_{iso}"


# ─── 子結構 ───────────────────────────────────────────────────────────────────

class CommandRecord(BaseModel):
    """記錄一筆使用者輸入的 command。"""

    timestamp: float
    command: str
    cwd: str
    tool_name: str | None = None
    preexec_result: str = "allow"
    """pre-exec 判定結果：allow / warn / block"""
    preexec_message: str = ""
    """pre-exec 警告/拒絕訊息（warn/block 時帶內容；allow 為空字串）。
    chat query 在 query 模糊時優先解釋這條訊息，避免 LLM 抓歷史 profile 數據作答。"""
    preexec_findings: list[dict] = []
    """checker 的 findings 序列化版（含 role / path / expected_rtl_checksum /
    observed_rtl_anchors）。chat query 用這個告訴 LLM 具體哪個 input 出問題，
    不只是泛泛的「synthesis inputs must share same anchor」。"""
    postexec_summary: str | None = None
    """post-exec 後由 AI 生成的一句話摘要"""


class EventRecord(BaseModel):
    """記錄一筆系統事件。"""

    timestamp: float
    event_type: str
    """事件類型，例如：stage_transition / gate_evaluation / lineage_check / warning_issued"""
    detail: str
    """事件描述"""
    metadata: dict[str, str] = {}
    """附加資訊"""


class MetricEntry(BaseModel):
    """從 EDA tool 報告中萃取的單一指標。"""

    metric_name: str
    """指標名稱，例如：wns_ns / tns_ns / drc_total_violations"""
    value: float
    """指標數值"""
    corner: str | None = None
    """製程角名稱（STA 專用），例如：ss_0p72v_m40c"""
    timestamp: float
    """萃取時間"""
    source_report: str
    """來源報告路徑"""


class ArtifactEntry(BaseModel):
    """產出工件的索引條目。"""

    path: str
    checksum_sha256: str
    size_bytes: int
    generated_at: float
    corner: str | None = None
    """若為 corner-specific 的產物"""
    tool_name: str | None = None
    """產生此工件的工具名稱"""


class GateResult(BaseModel):
    """一次品質門檻評估的結果。"""

    gate_id: str
    """門檻識別碼，例如：GATE_WNS_TRIAL"""
    metric_name: str
    actual_value: float
    threshold: float
    operator: str
    """比較運算子：>= / <= / == / > / <"""
    passed: bool
    corner: str | None = None
    timestamp: float
    stage_id: str
    """評估時所處的階段"""


class CornerCoverage(BaseModel):
    """STA corner 覆蓋追蹤狀態。"""

    required_corners: list[str]
    """playbook 定義的必須覆蓋的 corner 清單"""
    completed_pass: list[str] = []
    """已完成且通過的 corner"""
    completed_fail: list[str] = []
    """已完成但未通過的 corner"""

    @property
    def pending(self) -> list[str]:
        """尚未執行的 corner。"""
        done = set(self.completed_pass) | set(self.completed_fail)
        return [c for c in self.required_corners if c not in done]

    @property
    def coverage_ratio(self) -> float:
        """覆蓋率：(completed_pass + completed_fail) / required_corners。"""
        total = len(self.required_corners)
        if total == 0:
            return 0.0
        done = len(self.completed_pass) + len(self.completed_fail)
        return done / total

    @property
    def is_complete(self) -> bool:
        """是否全部 corner 都已執行。"""
        return len(self.pending) == 0 and len(self.required_corners) > 0

    @property
    def is_all_pass(self) -> bool:
        """是否全部 corner 都已通過。"""
        return self.is_complete and len(self.completed_fail) == 0

    def record_corner(self, corner: str, passed: bool) -> None:
        """記錄一個 corner 的執行結果。若已記錄則更新狀態。"""
        # 先從兩個清單移除（支援 fail→pass 更新）
        if corner in self.completed_pass:
            self.completed_pass.remove(corner)
        if corner in self.completed_fail:
            self.completed_fail.remove(corner)
        # 加入對應清單
        if passed:
            self.completed_pass.append(corner)
        else:
            self.completed_fail.append(corner)


class RootInput(BaseModel):
    """一個 root input 的資訊。"""

    role: str
    """角色，例如：rtl / sdc / upf"""
    path: str
    checksum_sha256: str
    traced_from: str | None = None
    """從哪個 downstream artifact 追溯而來"""


class LineageSummary(BaseModel):
    """血緣追溯結果摘要。"""

    synthesis_roots: list[RootInput]
    """追溯到的所有 root inputs"""
    trace_complete: bool
    """追溯是否完整（所有 input 都能追到 root）"""
    ai_judgment: str | None = None
    """AI 判定結果：CONSISTENT / MIXED_LINEAGE / INSUFFICIENT_EVIDENCE"""
    ai_explanation: str | None = None
    """AI 生成的判定說明"""


# ─── SessionSummary ──────────────────────────────────────────────────────────

class SessionSummary(BaseModel):
    """跨 session 記憶用的精簡摘要。"""

    session_id: str
    created_at: float
    last_stage_id: str | None = None
    corners_passed: int = 0
    corners_failed: int = 0
    corners_total: int = 0
    gate_pass_count: int = 0
    gate_fail_count: int = 0
    command_count: int = 0
    final_wns: float | None = None
    """最後一筆 wns_ns metric 的值"""
    final_tns: float | None = None
    """最後一筆 tns_ns metric 的值"""

    @classmethod
    def from_ecc(cls, ecc: "ECCObject") -> "SessionSummary":
        """從 ECCObject 產生精簡摘要。"""
        # 最新 wns_ns / tns_ns
        final_wns: float | None = None
        final_tns: float | None = None
        for m in reversed(ecc.metric_records):
            if final_wns is None and m.metric_name == "wns_ns":
                final_wns = m.value
            if final_tns is None and m.metric_name == "tns_ns":
                final_tns = m.value
            if final_wns is not None and final_tns is not None:
                break

        # gate 統計
        gate_pass = sum(1 for g in ecc.gate_results if g.passed)
        gate_fail = sum(1 for g in ecc.gate_results if not g.passed)

        # corner coverage
        cc = ecc.corner_coverage
        corners_passed = len(cc.completed_pass) if cc else 0
        corners_failed = len(cc.completed_fail) if cc else 0
        corners_total = len(cc.required_corners) if cc else 0

        return cls(
            session_id=ecc.session_id,
            created_at=ecc.created_at,
            last_stage_id=ecc.current_stage_id,
            corners_passed=corners_passed,
            corners_failed=corners_failed,
            corners_total=corners_total,
            gate_pass_count=gate_pass,
            gate_fail_count=gate_fail,
            command_count=len(ecc.command_history),
            final_wns=final_wns,
            final_tns=final_tns,
        )


# ─── SessionContinuity ────────────────────────────────────────────────────────

class SessionContinuity(BaseModel):
    """跨 session 連續性記憶（比 SessionSummary 更豐富的版本）。

    包含 corner 名稱、metric 趨勢、最近命令等細節，
    讓 AI 能在新 session 延續上一次的脈絡。
    """

    session_id: str
    created_at: float
    last_stage_id: str | None = None
    corners_passed: list[str] = []
    """通過的 corner 名稱清單"""
    corners_failed: list[str] = []
    """失敗的 corner 名稱清單"""
    corners_pending: list[str] = []
    """尚未執行的 corner 名稱清單"""
    metric_trends: dict[str, list[float]] = {}
    """各 metric 的最近趨勢（每個 key 最多 3 筆，最新在前）"""
    recent_commands: list[str] = []
    """最後 5 條命令"""
    failed_gates: list[str] = []
    """失敗的 gate ID 清單"""
    last_ai_suggestion: str | None = None
    """上一次 AI 給的建議"""

    @classmethod
    def from_ecc(cls, ecc: "ECCObject") -> "SessionContinuity":
        """從 ECCObject 產生連續性記憶。"""
        # corner coverage
        cc = ecc.corner_coverage
        corners_passed = list(cc.completed_pass) if cc else []
        corners_failed = list(cc.completed_fail) if cc else []
        corners_pending = list(cc.pending) if cc else []

        # metric trends：按 metric_name 分組，取最近 3 筆（最新在前）
        metric_groups: dict[str, list[tuple[float, float]]] = {}
        for m in ecc.metric_records:
            metric_groups.setdefault(m.metric_name, []).append((m.timestamp, m.value))
        metric_trends: dict[str, list[float]] = {}
        for name, entries in metric_groups.items():
            entries.sort(key=lambda x: x[0], reverse=True)
            metric_trends[name] = [v for _, v in entries[:3]]

        # recent commands（最後 5 筆）
        recent_commands = [c.command for c in ecc.command_history[-5:]]

        # failed gates
        failed_gates = list({g.gate_id for g in ecc.gate_results if not g.passed})

        return cls(
            session_id=ecc.session_id,
            created_at=ecc.created_at,
            last_stage_id=ecc.current_stage_id,
            corners_passed=corners_passed,
            corners_failed=corners_failed,
            corners_pending=corners_pending,
            metric_trends=metric_trends,
            recent_commands=recent_commands,
            failed_gates=failed_gates,
        )


# ─── 主結構 ───────────────────────────────────────────────────────────────────

class ECCObject(BaseModel):
    """
    Execution-Context-and-Content Object.

    核心結構要件：
    (A) 識別與關聯欄位（必要）
    (B) 執行情境欄位（至少包含其一）
    (C) 工作內容欄位（至少包含其一）
    (D) 階段關聯欄位（當啟用階段感知時為必要）
    """

    # --- (A) 識別與關聯 ---
    session_id: str
    """唯一識別該交互會話。格式：sess-{uuid4().hex[:8]}"""

    created_at: float
    """ECC Object 建立時間（epoch seconds）"""

    updated_at: float
    """最後一次更新時間（epoch seconds）"""

    correlation_key: str
    """由 session_id + timestamp 組成，用於增量更新與歷史溯源。
    格式：{session_id}_{iso_timestamp}"""

    # --- (B) 執行情境 ---
    work_directory: str = ""
    """當前工作目錄"""

    command_history: list[CommandRecord] = []
    """累積式命令歷史"""

    current_stage_id: str | None = None
    """當前 playbook 階段識別碼"""

    event_history: list[EventRecord] = []
    """累積式事件歷史"""

    # --- (C) 工作內容 ---
    metric_records: list[MetricEntry] = []
    """從報告中萃取的結構化指標。累積式，不覆蓋。"""

    artifact_index: list[ArtifactEntry] = []
    """產出工件索引。累積式。"""

    lineage_summary: LineageSummary | None = None
    """血緣追溯結果"""

    # --- (D) 階段關聯 ---
    stage_entered_at: float | None = None
    """進入當前階段的時間"""

    applicable_gate_ids: list[str] = []
    """當前階段適用的品質門檻 ID"""

    gate_results: list[GateResult] = []
    """品質門檻評估結果。累積式。"""

    corner_coverage: CornerCoverage | None = None
    """STA corner 覆蓋狀態（STA 場景專用）"""

    # ─── classmethod ──────────────────────────────────────────────────────

    @classmethod
    def create_new(cls, work_directory: str = "") -> ECCObject:
        """建立全新的 ECCObject，自動生成 session_id、時間戳、correlation_key。"""
        now = time.time()
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        return cls(
            session_id=sid,
            created_at=now,
            updated_at=now,
            correlation_key=make_correlation_key(sid, now),
            work_directory=work_directory,
        )

    # ─── 內部方法 ─────────────────────────────────────────────────────────

    def _refresh_updated(self) -> None:
        """內部方法：更新 updated_at 與 correlation_key。"""
        self.updated_at = time.time()
        self.correlation_key = make_correlation_key(self.session_id, self.updated_at)

    # ─── 累積式更新方法 ──────────────────────────────────────────────────

    def append_command(self, record: CommandRecord) -> None:
        """追加一筆 command 到 command_history，並更新 updated_at 與 correlation_key。"""
        self.command_history.append(record)
        self._refresh_updated()

    def append_event(self, event: EventRecord) -> None:
        """追加一筆 event 到 event_history，並更新 updated_at 與 correlation_key。"""
        self.event_history.append(event)
        self._refresh_updated()

    def append_metric(self, metric: MetricEntry) -> None:
        """追加一筆 metric 到 metric_records，並更新 updated_at 與 correlation_key。"""
        self.metric_records.append(metric)
        self._refresh_updated()

    def append_artifact(self, artifact: ArtifactEntry) -> None:
        """追加一筆 artifact 到 artifact_index，並更新 updated_at 與 correlation_key。"""
        self.artifact_index.append(artifact)
        self._refresh_updated()

    def append_gate_result(self, result: GateResult) -> None:
        """追加一筆 gate result 到 gate_results，並更新 updated_at 與 correlation_key。"""
        self.gate_results.append(result)
        self._refresh_updated()

    def update_stage(self, stage_id: str, gate_ids: list[str], entered_at: float) -> None:
        """更新當前階段資訊，同時追加一筆 stage transition event。"""
        self.current_stage_id = stage_id
        self.applicable_gate_ids = gate_ids
        self.stage_entered_at = entered_at
        self.event_history.append(
            EventRecord(
                timestamp=entered_at,
                event_type="stage_transition",
                detail=f"Entered stage: {stage_id}",
                metadata={"stage_id": stage_id},
            )
        )
        self._refresh_updated()
