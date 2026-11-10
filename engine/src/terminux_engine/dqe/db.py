# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_NA = "_NA_"
_MAX_BLOB_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_DB_PATH = Path.home() / ".terminux" / "dqe" / "dqe.sqlite"

_DDL = """
CREATE TABLE IF NOT EXISTS Root_Dqe_Tab (
    id         INTEGER PRIMARY KEY,
    DqeDbNum   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS Project_Dqe_Tab (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ProjectCode TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS Phase_Dqe_Tab (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    Phase TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS Block_Dqe_Tab (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    Block TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS Flow_Dqe_Tab (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Flow TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS Version_Dqe_Tab (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ProjectCode TEXT    NOT NULL,
    DqeDbNum    TEXT    NOT NULL,
    Phase       TEXT    NOT NULL,
    Block       TEXT    NOT NULL,
    Stage       TEXT    NOT NULL,
    Flow        TEXT    NOT NULL,
    Version     TEXT    NOT NULL,
    DQE_ID      TEXT    NOT NULL,
    VALUE       TEXT,
    UNIT        TEXT,
    STATUS      TEXT,
    DATE_TIME   TEXT    NOT NULL,
    Reporter    TEXT,
    PATH        TEXT,
    ATTACHMENT  BLOB,
    EXECUTABLE  BLOB,
    job_key     TEXT    NOT NULL,
    eco_run_id    TEXT,
    eco_iteration INTEGER,
    orchestration_run_id TEXT,
    step_id              TEXT,
    attempt              INTEGER,
    dispatch_key         TEXT,
    source_event_id      TEXT,
    package_id           TEXT,
    semantic_status      TEXT
);

CREATE INDEX IF NOT EXISTS idx_version_job_key  ON Version_Dqe_Tab(job_key);
CREATE INDEX IF NOT EXISTS idx_version_datetime ON Version_Dqe_Tab(DATE_TIME);
CREATE INDEX IF NOT EXISTS idx_eco_run ON Version_Dqe_Tab(eco_run_id, eco_iteration, Flow);

CREATE TABLE IF NOT EXISTS Dqe_Meta_Tab (
    DQE_ID      TEXT PRIMARY KEY,
    TITLE       TEXT,
    DESCRIPTION TEXT,
    REFERENCE   TEXT
);
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO Version_Dqe_Tab
    (ProjectCode, DqeDbNum, Phase, Block, Stage, Flow, Version,
     DQE_ID, VALUE, UNIT, STATUS, DATE_TIME,
     Reporter, PATH, ATTACHMENT, EXECUTABLE, job_key, eco_run_id, eco_iteration,
     orchestration_run_id, step_id, attempt, dispatch_key, source_event_id, package_id,
     semantic_status)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


@dataclass
class DqeRecord:
    """一筆 DQE 指標記錄。"""

    dqe_id: str
    value: str | None = None
    unit: str | None = None
    status: str | None = None
    reporter: str | None = None
    path: str | None = None
    attachment: bytes | None = None
    executable: bytes | None = None
    orchestration_run_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    dispatch_key: str | None = None
    source_event_id: str | None = None
    package_id: str | None = None
    #: DQE 的 domain-neutral exact semantic status
    #: （`pass`／`warn`／`fail`／`unknown`）。既有的 `status` 欄位保留為
    #: legacy presentation status（`OK`／`WARNING`／`ERROR`），舊 reader
    #: 完全不受影響；新 reader 應優先讀這一欄。`None` 代表寫入端未提供
    #: exact status（既有 package 的行為），此時只能從 legacy status 推導，
    #: 且 `WARNING` 無法區分 warn 與 unknown。
    semantic_status: str | None = None


class DqeDbManager:
    """DQE 共用資料庫管家。

    職責：建庫（lazy）、原子批次寫入、定期清洗。
    不負責解析 EDA report、不寫 EventStore、不做遠端 push/pull。
    """

    def __init__(
        self,
        db_path: Path = _DEFAULT_DB_PATH,
        *,
        busy_timeout: int = 5000,
        max_retries: int = 3,
    ) -> None:
        self._db_path = db_path
        self.busy_timeout = busy_timeout
        self.max_retries = max_retries
        self._seq_lock = threading.Lock()
        self._last_ts: str = ""
        self._seq: int = 0
        # 維持既有 _insert_records 可覆寫介面，同時安全傳遞本次批次的 ECO 標籤。
        self._eco_tags: ContextVar[tuple[str | None, int | None]] = ContextVar(
            "dqe_eco_tags", default=(None, None)
        )

    @property
    def db_path(self) -> Path:
        """目前這個 manager 實際使用的 DB 路徑（唯讀）。

        analysis access policy 需要比對「server 實際選定的資料
        來源」與 policy 宣告的來源；沒有公開 accessor 就只能去讀私有欄位。
        這是純讀取，不改變任何解析行為。
        """
        return self._db_path

    # ── 內部 ──────────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=self.busy_timeout / 1000)
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout}")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> str:
        """確認 schema 存在，回傳 DqeDbNum。第一次建庫時自動初始化。"""
        try:
            row = conn.execute(
                "SELECT DqeDbNum FROM Root_Dqe_Tab WHERE id = 1"
            ).fetchone()
            if row is not None:
                self._ensure_version_dqe_columns(conn)
                return row[0]
        except sqlite3.OperationalError:
            pass

        # 第一次：建所有表，插入 Root 記錄
        conn.executescript(_DDL)
        self._ensure_version_dqe_columns(conn)
        dqe_db_num = datetime.now().strftime("%Y%m%d_%H%M%S")
        conn.execute(
            "INSERT OR IGNORE INTO Root_Dqe_Tab (id, DqeDbNum) VALUES (1, ?)",
            (dqe_db_num,),
        )
        conn.commit()
        return dqe_db_num

    def _ensure_version_dqe_columns(self, conn: sqlite3.Connection) -> None:
        """為既有 DQE DB 補上 ECO 與 orchestration 欄位及索引。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(Version_Dqe_Tab)")}
        if "eco_run_id" not in columns:
            conn.execute("ALTER TABLE Version_Dqe_Tab ADD COLUMN eco_run_id TEXT")
        if "eco_iteration" not in columns:
            conn.execute("ALTER TABLE Version_Dqe_Tab ADD COLUMN eco_iteration INTEGER")
        orchestration_columns = {
            "orchestration_run_id": "TEXT",
            "step_id": "TEXT",
            "attempt": "INTEGER",
            "dispatch_key": "TEXT",
            "source_event_id": "TEXT",
            "package_id": "TEXT",
            # additive、nullable，舊 DB 自動補上；
            # 不改既有欄位、不重寫既有 rows。
            "semantic_status": "TEXT",
        }
        for column, column_type in orchestration_columns.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE Version_Dqe_Tab ADD COLUMN {column} {column_type}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eco_run "
            "ON Version_Dqe_Tab(eco_run_id, eco_iteration, Flow)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_version_dqe_dispatch_unique "
            "ON Version_Dqe_Tab(dispatch_key, package_id, DQE_ID)"
        )

    def _gen_job_key(self) -> str:
        """生成 YYYYMMDD_HHMMSS_NNN 格式的 job_key；同秒多次呼叫以序號區分。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._seq_lock:
            if ts != self._last_ts:
                self._last_ts = ts
                self._seq = 0
            self._seq += 1
            seq = self._seq
        return f"{ts}_{seq:03d}"

    def _insert_records(
        self,
        conn: sqlite3.Connection,
        records: list[DqeRecord],
        project_code: str,
        dqe_db_num: str,
        phase: str,
        block: str,
        stage: str,
        flow: str,
        version: str,
        now_str: str,
        reporter: str | None,
        job_key: str,
    ) -> None:
        """插入 Version_Dqe_Tab 各列。提取為獨立方法以方便測試注入失敗。"""
        eco_run_id, eco_iteration = self._eco_tags.get()
        for rec in records:
            conn.execute(
                _INSERT_SQL,
                (
                    project_code, dqe_db_num, phase, block, stage, flow, version,
                    rec.dqe_id, rec.value, rec.unit, rec.status, now_str,
                    rec.reporter or reporter, rec.path,
                    rec.attachment, rec.executable, job_key, eco_run_id, eco_iteration,
                    rec.orchestration_run_id, rec.step_id, rec.attempt,
                    rec.dispatch_key or None, rec.source_event_id, rec.package_id,
                    rec.semantic_status,
                ),
            )

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def store_dqe_batch(
        self,
        records: list[DqeRecord],
        *,
        project_code: str = _NA,
        phase: str = _NA,
        block: str = _NA,
        stage: str = _NA,
        flow: str = _NA,
        version: str = _NA,
        reporter: str | None = None,
        eco_run_id: str | None = None,
        eco_iteration: int | None = None,
    ) -> str:
        """原子寫入一組 DQE，回傳 job_key。失敗時整組 rollback。"""
        job_key, _rows_inserted = self.store_dqe_batch_with_result(
            records,
            project_code=project_code,
            phase=phase,
            block=block,
            stage=stage,
            flow=flow,
            version=version,
            reporter=reporter,
            eco_run_id=eco_run_id,
            eco_iteration=eco_iteration,
        )
        return job_key

    def store_dqe_batch_with_result(
        self,
        records: list[DqeRecord],
        *,
        project_code: str = _NA,
        phase: str = _NA,
        block: str = _NA,
        stage: str = _NA,
        flow: str = _NA,
        version: str = _NA,
        reporter: str | None = None,
        eco_run_id: str | None = None,
        eco_iteration: int | None = None,
    ) -> tuple[str, int]:
        """原子寫入一組 DQE，回傳 job_key 與實際新增列數。"""
        for rec in records:
            if rec.attachment and len(rec.attachment) > _MAX_BLOB_BYTES:
                raise ValueError(
                    f"DqeRecord({rec.dqe_id}).attachment 超過 5 MB 上限"
                )
            if rec.executable and len(rec.executable) > _MAX_BLOB_BYTES:
                raise ValueError(
                    f"DqeRecord({rec.dqe_id}).executable 超過 5 MB 上限"
                )

        job_key = self._gen_job_key()
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")

        for attempt in range(self.max_retries):
            conn = self._connect()
            try:
                dqe_db_num = self._ensure_schema(conn)
                eco_token = self._eco_tags.set((eco_run_id, eco_iteration))
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO Project_Dqe_Tab(ProjectCode) VALUES (?)",
                        (project_code,),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO Phase_Dqe_Tab(Phase) VALUES (?)",
                        (phase,),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO Block_Dqe_Tab(Block) VALUES (?)",
                        (block,),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO Flow_Dqe_Tab(Flow) VALUES (?)",
                        (flow,),
                    )
                    changes_before_insert = conn.total_changes
                    self._insert_records(
                        conn, records, project_code, dqe_db_num,
                        phase, block, stage, flow, version,
                        now_str, reporter, job_key,
                    )
                    rows_inserted = conn.total_changes - changes_before_insert
                return job_key, rows_inserted
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or attempt >= self.max_retries - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
            finally:
                if "eco_token" in locals():
                    self._eco_tags.reset(eco_token)
                conn.close()
        raise RuntimeError("unreachable")

    def upsert_dqe_meta(
        self,
        dqe_id: str,
        title: str,
        description: str,
        reference: str = "",
    ) -> None:
        """寫入 DQE metadata；相同 DQE_ID 更新標題、說明與參考資料。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn:
                conn.execute(
                    "INSERT INTO Dqe_Meta_Tab"
                    "(DQE_ID, TITLE, DESCRIPTION, REFERENCE) VALUES (?,?,?,?) "
                    "ON CONFLICT(DQE_ID) DO UPDATE SET "
                    "TITLE=excluded.TITLE, DESCRIPTION=excluded.DESCRIPTION, "
                    "REFERENCE=excluded.REFERENCE",
                    (dqe_id, title, description, reference),
                )
        finally:
            conn.close()

    # ── Read API（explain_run；read-only、vendor-neutral）────────────

    #: Legacy presentation status → exact semantic status 的 deterministic
    #: 還原表。`WARNING` **刻意不在表內**：舊寫入端把 `warn` 與 `unknown`
    #: 都壓成 `WARNING`，沒有任何欄位能區分兩者，因此只能回報
    #: `legacy_ambiguous`，不得猜測。
    _LEGACY_STATUS_TO_SEMANTIC = {"OK": "pass", "ERROR": "fail"}

    LEGACY_AMBIGUOUS = "legacy_ambiguous"

    def read_records_by_orchestration_run(
        self,
        orchestration_run_id: str,
        *,
        package_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """依 orchestration run 讀回 DQE rows（唯讀，不建立 schema）。

        每一列都帶三個 status 欄位：

        - ``status``：既有的 legacy presentation status，原樣回傳。
        - ``semantic_status``：exact semantic status。寫入端有提供時直接
          回傳；沒有時依 ``_LEGACY_STATUS_TO_SEMANTIC`` deterministic 還原，
          無法還原（``WARNING``）則為 ``legacy_ambiguous``。
        - ``semantic_status_source``：``recorded`` / ``derived_from_legacy``
          / ``legacy_ambiguous``，讓上層知道這個值有多可信。

        本方法不理解任何 EDA vendor、tool 或 check 語意；它只做欄位讀取與
        一張固定的 legacy 對照表。
        """
        if not self._db_path.exists():
            return []
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(Version_Dqe_Tab)")}
            has_semantic = "semantic_status" in columns
            sql = (
                # `rowid` 而不是 `id`：`id INTEGER PRIMARY KEY` 本來就是
                # rowid 的別名，但舊／精簡 schema 不一定有 `id` 欄位，直接選
                # `id` 會讓整段查詢失敗而被誤判成「讀不到 DQE」。
                "SELECT rowid AS _row_id, DQE_ID, VALUE, UNIT, STATUS, DATE_TIME, PATH, job_key, Flow, Block, "
                "orchestration_run_id, step_id, attempt, dispatch_key, source_event_id, package_id"
                + (", semantic_status" if has_semantic else "")
                + " FROM Version_Dqe_Tab WHERE orchestration_run_id = ?"
            )
            params: list[Any] = [orchestration_run_id]
            if package_id is not None:
                sql += " AND package_id = ?"
                params.append(package_id)
            sql += " ORDER BY DQE_ID"
            rows = [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

        # 正式 DQE evidence 讀取預設排除已作廢的 row。原始 row 仍在
        # 表裡供 forensic 直接查詢；這裡只是不讓它進入任何產品判定。`id`
        # 只用於比對，不外流到回傳結構（既有欄位契約不變）。
        from terminux_engine.evidence_integrity.invalidation import (
            load_invalidation_set,
        )

        invalidated = load_invalidation_set()
        db_identity = str(self._db_path.resolve())
        kept: list[dict[str, Any]] = []
        for row in rows:
            row_id = row.pop("_row_id", None)
            if invalidated.run_invalidated(row.get("orchestration_run_id")):
                continue
            if invalidated.dqe_row_invalidated(
                    db_identity=db_identity, row_id=row_id):
                continue
            kept.append(row)
        rows = kept

        for row in rows:
            recorded = row.get("semantic_status")
            if isinstance(recorded, str) and recorded:
                row["semantic_status_source"] = "recorded"
                continue
            legacy = row.get("STATUS")
            derived = self._LEGACY_STATUS_TO_SEMANTIC.get(legacy) if isinstance(legacy, str) else None
            if derived is not None:
                row["semantic_status"] = derived
                row["semantic_status_source"] = "derived_from_legacy"
            else:
                row["semantic_status"] = self.LEGACY_AMBIGUOUS
                row["semantic_status_source"] = "legacy_ambiguous"
        return rows

    def clean(self, retention_days: int = 14) -> int:
        """刪除超過 retention_days 天的記錄，回傳刪除筆數。"""
        if not self._db_path.exists():
            return 0

        cutoff_str = (
            datetime.now() - timedelta(days=retention_days)
        ).strftime("%Y%m%d_%H%M%S")

        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM Version_Dqe_Tab WHERE DATE_TIME < ?",
                    (cutoff_str,),
                )
                deleted = cur.rowcount
        finally:
            conn.close()

        logger.info("DQE clean: 刪除 %d 筆（cutoff=%s）", deleted, cutoff_str)
        return deleted

    def vacuum(self) -> None:
        """VACUUM 回收刪除後的碎片空間。"""
        if not self._db_path.exists():
            return

        conn = self._connect()
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

        logger.info("DQE VACUUM 完成")
