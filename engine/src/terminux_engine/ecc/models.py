# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ── Enums ────────────────────────────────────────────────

class CommandType(str, Enum):
    FILE_EDIT   = "file_edit"
    FILE_OP     = "file_op"
    EDA_TOOL    = "eda_tool"
    DFA_OP      = "dfa_op"
    JOB_SUBMIT  = "job_submit"
    OTHER       = "other"


class NotifyType(str, Enum):
    INLINE  = "inline"
    SIDEBAR = "sidebar"
    EMAIL   = "email"


class ProvenanceSource(str, Enum):
    DFA_TICKET = "dfa_ticket"
    DVC        = "dvc"
    RUNSET     = "runset"
    PLUGIN     = "plugin"


class QueryOrder(str, Enum):
    ASC  = "asc"
    DESC = "desc"


# ── Core Event ───────────────────────────────────────────

@dataclass
class ShellEvent:
    # 識別
    event_id:           str           # UUID v4
    session_id:         str           # UUID v4
    member_id:          str           # Linux account name，非空
    project_id:         str           # 非空
    timestamp:          datetime      # UTC

    # 情境
    work_directory:     str           # 以 / 開頭
    block:              str | None    # 由 Layer 1 解析，可為 None

    # 命令
    command:            str           # 完整命令字串，非空
    command_type:       CommandType

    # 操作對象
    # command.submit 的 targets（shell parsing）與
    # command.complete 的 targets_confirmed（fd 監看）合併後存入
    targets:            list[str]     # 可為空 list

    # 輸出
    stdout_summary:     str | None
    stderr_summary:     str | None
    exit_code:          int | None
    duration_ms:        int | None    # ≥ 0，None 表示尚未完成

    # 命令執行時的環境變數快照（由 C++ 在 command.submit 時傳入）
    env_snapshot:       dict[str, str] = field(default_factory=dict)

    # Layer 1 提取的結構化資料
    # 例：{ "tool_signature": "primetime", "wns": -0.023, "tns": -1.245 }
    extracted_data:     dict[str, Any] = field(default_factory=dict)

    # 偵測到的輸出檔案路徑
    artifacts_detected: list[str]      = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id must not be empty")
        if not self.member_id:
            raise ValueError("member_id must not be empty")
        if not self.project_id:
            raise ValueError("project_id must not be empty")
        if not self.command:
            raise ValueError("command must not be empty")
        if not self.work_directory.startswith("/"):
            raise ValueError("work_directory must be an absolute path")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")

    def to_dict(self) -> dict:
        """
        將 ShellEvent 序列化為 dict。
        datetime → ISO 8601 字串；CommandType → .value 字串。
        """
        return {
            "event_id":           self.event_id,
            "session_id":         self.session_id,
            "member_id":          self.member_id,
            "project_id":         self.project_id,
            "timestamp":          self.timestamp.isoformat(),
            "work_directory":     self.work_directory,
            "block":              self.block,
            "command":            self.command,
            "command_type":       self.command_type.value,
            "targets":            list(self.targets),
            "stdout_summary":     self.stdout_summary,
            "stderr_summary":     self.stderr_summary,
            "exit_code":          self.exit_code,
            "duration_ms":        self.duration_ms,
            "env_snapshot":       dict(self.env_snapshot),
            "extracted_data":     dict(self.extracted_data),
            "artifacts_detected": list(self.artifacts_detected),
        }


# ── Provenance ───────────────────────────────────────────

@dataclass
class ProvenanceSpec:
    step:       str               # 流程步驟名稱，非空
    member_id:  str               # 非空
    data:       dict[str, Any]    # 規範資料，不可為空 dict
    source:     ProvenanceSource
    written_at: datetime          # UTC

    def __post_init__(self) -> None:
        if not self.step:
            raise ValueError("step must not be empty")
        if not self.member_id:
            raise ValueError("member_id must not be empty")
        if not self.data:
            raise ValueError("data must not be empty")


@dataclass
class ProvenanceObs:
    step:        str               # 非空
    member_id:   str               # 非空
    data:        dict[str, Any]    # 實際觀察資料
    event_id:    str               # 關聯的 ShellEvent.event_id，非空
    observed_at: datetime          # UTC

    def __post_init__(self) -> None:
        if not self.step:
            raise ValueError("step must not be empty")
        if not self.member_id:
            raise ValueError("member_id must not be empty")
        if not self.event_id:
            raise ValueError("event_id must not be empty")


@dataclass
class ProvenanceMismatch:
    key:      str         # 錯配的資料項目名稱
    spec:     Any         # 規範值
    observed: Any         # 實際觀察值


# ── Query ────────────────────────────────────────────────

@dataclass
class EventQuery:
    # 所有欄位皆選填，None 表示不過濾
    member_id:       str | None         = None
    project_id:      str | None         = None
    session_id:      str | None         = None
    block:           str | None         = None
    command_type:    CommandType | None = None
    tool_signature:  str | None         = None  # 從 extracted_data 過濾
    after:           datetime | None    = None
    before:          datetime | None    = None
    exit_code:       int | None         = None
    limit:           int | None         = None  # None 表示不限筆數
    order:           QueryOrder         = QueryOrder.DESC

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be >= 0")
        if self.after is not None and self.before is not None:
            if self.after > self.before:
                raise ValueError("after must not be greater than before")


# ── Notification ─────────────────────────────────────────

@dataclass
class Notification:
    session_id:  str           # 目標 terminal 的 session_id
    notify_type: NotifyType
    title:       str           # 非空
    content:     str           # 非空
    detail:      str | None    # sidebar 時使用，可為 None
    timestamp:   datetime      # UTC

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title must not be empty")
        if not self.content:
            raise ValueError("content must not be empty")
