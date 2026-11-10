# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""server 明確設定的分析存取範圍。

一份由 **daemon 管理者**放在檔案系統上的唯讀 JSON policy，把 canonical
member、project、指定資料來源與**兩筆特定 run**明確連結起來。

它表達的是「現在允許這個 member 在這個 project 分析這兩筆資料」，
**不是**補填 run 原始的 project／design 歸屬，也不是執行輸入證據。

邊界（不可放寬）：

- 只能由啟動環境的 `TERMINUX_ANALYSIS_ACCESS_POLICY_FILE` 指定**絕對路徑**。
  沒有寬鬆預設；不從 cwd、workspace 或 action arguments 自動發現 policy。
- provider 與 IPC **沒有**寫入入口；Agent 沒有修改 policy 的 tool。
- 這份 policy 不授予 EDA 執行、其他 Package action、其他 run 或其他
  member 的權限；`/task-start` 的一次任務授權仍必須執行。
- 它**不宣稱**能抵抗同一 Linux 帳號直接修改 host 程式或設定檔的人，也
  **不把檔案 owner 當作 run/project 歸屬證據**。

analysis task 不提供 hot reload：daemon 啟動時載入一次，之後每次 create／start／
每步派送前確認檔案仍在、仍可解析、且 digest 與啟動時一致。變更要重啟
daemon 才生效，舊 task 仍不能續跑。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks.canonical import sha256_payload

#: 唯一的設定來源。只讀 daemon 啟動環境。
POLICY_ENV = "TERMINUX_ANALYSIS_ACCESS_POLICY_FILE"

SCHEMA_VERSION = 1

#: 唯一允許的固定範本。
FIXED_TEMPLATE_ID = "orfs.explain_compare.v1"

#: 恰好兩筆不同的 run，對應固定兩步分析。
REQUIRED_RUN_COUNT = 2

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "policy_id", "member_id", "project_id", "template_id",
    "dqe_db_path", "artifact_root", "allowed_runs",
})
_RUN_FIELDS = frozenset({"run_id", "manifest_sha256"})

# ── 拒絕碼 ────────────────────────────────────────────────────────────

POLICY_NOT_CONFIGURED = "ANALYSIS_ACCESS_POLICY_NOT_CONFIGURED"
POLICY_UNREADABLE = "ANALYSIS_ACCESS_POLICY_UNREADABLE"
POLICY_INVALID = "ANALYSIS_ACCESS_POLICY_INVALID"
POLICY_CHANGED = "ANALYSIS_ACCESS_POLICY_CHANGED"


class AccessPolicyError(RuntimeError):
    """policy 缺席、不可讀或不合法。一律 fail-closed，不降級。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── 模型 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AllowedRun:
    run_id: str
    #: 既有 manifest **原始檔案 bytes** 的 SHA-256。與 manifest 內部驗證
    #: 分別執行——改寫 manifest 之後不能假稱原始證據未變。
    manifest_sha256: str

    def to_json_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True)
class AnalysisAccessPolicy:
    """不可變的已載入 policy。"""

    schema_version: int
    policy_id: str
    member_id: str
    project_id: str
    template_id: str
    dqe_db_path: str
    artifact_root: str
    allowed_runs: tuple[AllowedRun, ...]
    policy_digest: str

    # ── 比對 ────────────────────────────────────────────────────────

    def matches_scope(self, member_id: str, project_id: str) -> bool:
        """member 與 session project 都必須**精確**相符。"""
        return self.member_id == member_id and self.project_id == project_id

    def allowed_run(self, run_id: str) -> AllowedRun | None:
        return next((r for r in self.allowed_runs if r.run_id == run_id), None)

    @property
    def allowed_run_ids(self) -> tuple[str, ...]:
        return tuple(r.run_id for r in self.allowed_runs)

    def matches_sources(self, *, dqe_db_path: str | None,
                        artifact_root: str | None) -> tuple[bool, str | None]:
        """實際 server 資料來源必須與 policy 相符（realpath 比對）。

        任一邊解析不出來就是**不相符**——不猜、不放行。
        """
        if not dqe_db_path or not artifact_root:
            return False, "the server data sources could not be resolved"
        if _realpath(dqe_db_path) != _realpath(self.dqe_db_path):
            return False, "the resolved DQE source does not match the policy"
        if _realpath(artifact_root) != _realpath(self.artifact_root):
            return False, "the resolved artifact root does not match the policy"
        return True, None

    def to_json_dict(self) -> dict[str, Any]:
        payload = self._digest_material()
        payload["policy_digest"] = self.policy_digest
        return payload

    def _digest_material(self) -> dict[str, Any]:
        """digest 材料**不含**檔案位置——policy 搬個位置不改變它的身分。"""
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "member_id": self.member_id,
            "project_id": self.project_id,
            "template_id": self.template_id,
            "dqe_db_path": self.dqe_db_path,
            "artifact_root": self.artifact_root,
            "allowed_runs": [r.to_json_dict() for r in self.allowed_runs],
        }

    def public_summary(self) -> dict[str, Any]:
        """可外露的摘要。不含檔案路徑以外的任何額外內部狀態。"""
        return {
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "project_id": self.project_id,
            "template_id": self.template_id,
            "allowed_run_count": len(self.allowed_runs),
        }


def _realpath(value: str) -> str:
    return str(Path(value).expanduser().resolve())


# ── 載入 ─────────────────────────────────────────────────────────────


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise AccessPolicyError(
                POLICY_INVALID, f"duplicate JSON key in policy: {key!r}")
        seen.add(key)
    return dict(pairs)


def load_policy(path: str | Path) -> AnalysisAccessPolicy:
    """從絕對路徑載入並完整驗證一份 policy。"""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise AccessPolicyError(
            POLICY_INVALID, f"{POLICY_ENV} must be an absolute path: {path!r}")
    try:
        raw = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise AccessPolicyError(
            POLICY_UNREADABLE, f"the access policy could not be read: {exc}") from exc
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except AccessPolicyError:
        raise
    except ValueError as exc:
        raise AccessPolicyError(
            POLICY_INVALID, f"the access policy is not valid JSON: {exc}") from exc
    return build_policy(document)


def build_policy(document: Any) -> AnalysisAccessPolicy:
    """驗證一份已解析的 policy document。未知欄位一律拒絕。"""
    if not isinstance(document, dict):
        raise AccessPolicyError(POLICY_INVALID, "the access policy must be a JSON object")

    unknown = sorted(set(document) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise AccessPolicyError(POLICY_INVALID, f"unknown policy field(s): {unknown}")
    missing = sorted(_TOP_LEVEL_FIELDS - set(document))
    if missing:
        raise AccessPolicyError(POLICY_INVALID, f"missing policy field(s): {missing}")

    if document["schema_version"] != SCHEMA_VERSION:
        raise AccessPolicyError(
            POLICY_INVALID,
            f"unsupported policy schema_version: {document['schema_version']!r}")

    for field in ("policy_id", "member_id", "project_id", "template_id"):
        value = document[field]
        if not isinstance(value, str) or not value.strip():
            raise AccessPolicyError(
                POLICY_INVALID, f"policy field {field!r} must be a non-empty string")

    if document["template_id"] != FIXED_TEMPLATE_ID:
        raise AccessPolicyError(
            POLICY_INVALID,
            f"policy template_id must be {FIXED_TEMPLATE_ID!r}, "
            f"got {document['template_id']!r}")

    for field in ("dqe_db_path", "artifact_root"):
        value = document[field]
        if not isinstance(value, str) or not value:
            raise AccessPolicyError(
                POLICY_INVALID, f"policy field {field!r} must be a non-empty string")
        if not Path(value).is_absolute():
            raise AccessPolicyError(
                POLICY_INVALID, f"policy field {field!r} must be an absolute path")

    runs_raw = document["allowed_runs"]
    if not isinstance(runs_raw, list) or len(runs_raw) != REQUIRED_RUN_COUNT:
        raise AccessPolicyError(
            POLICY_INVALID,
            f"allowed_runs must list exactly {REQUIRED_RUN_COUNT} runs")

    runs: list[AllowedRun] = []
    seen_runs: set[str] = set()
    for entry in runs_raw:
        if not isinstance(entry, dict):
            raise AccessPolicyError(POLICY_INVALID, "each allowed_runs entry must be an object")
        extra = sorted(set(entry) - _RUN_FIELDS)
        if extra:
            raise AccessPolicyError(POLICY_INVALID, f"unknown allowed_runs field(s): {extra}")
        absent = sorted(_RUN_FIELDS - set(entry))
        if absent:
            raise AccessPolicyError(POLICY_INVALID, f"missing allowed_runs field(s): {absent}")
        run_id = entry["run_id"]
        digest = entry["manifest_sha256"]
        if not isinstance(run_id, str) or not run_id:
            raise AccessPolicyError(POLICY_INVALID, "allowed_runs.run_id must be a non-empty string")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise AccessPolicyError(
                POLICY_INVALID,
                f"allowed_runs.manifest_sha256 must be 64 lowercase hex chars "
                f"(run {run_id!r})")
        if run_id in seen_runs:
            raise AccessPolicyError(POLICY_INVALID, f"duplicate run in policy: {run_id!r}")
        seen_runs.add(run_id)
        runs.append(AllowedRun(run_id=run_id, manifest_sha256=digest))

    policy = AnalysisAccessPolicy(
        schema_version=SCHEMA_VERSION,
        policy_id=document["policy_id"],
        member_id=document["member_id"],
        project_id=document["project_id"],
        template_id=document["template_id"],
        dqe_db_path=document["dqe_db_path"],
        artifact_root=document["artifact_root"],
        allowed_runs=tuple(runs),
        policy_digest="",
    )
    from dataclasses import replace

    return replace(policy, policy_digest=sha256_payload(policy._digest_material()))


def configured_policy_path() -> str | None:
    value = os.environ.get(POLICY_ENV, "").strip()
    return value or None


def load_policy_from_env() -> AnalysisAccessPolicy:
    path = configured_policy_path()
    if path is None:
        raise AccessPolicyError(
            POLICY_NOT_CONFIGURED,
            f"{POLICY_ENV} is not set; this daemon has no analysis access policy")
    return load_policy(path)


# ── 啟動時載入一次，之後只驗「沒有變」（無 hot reload）─────────


class PolicyGuard:
    """持有啟動時載入的 policy，並在每個授權點確認它沒有變。

    `current()` 會重新讀檔並比對 digest：

    - 檔案不見了、讀不到、解析失敗 ⇒ 拋出（停止新授權／派送）。
    - digest 與啟動時不同 ⇒ 拋出 `POLICY_CHANGED`。**不**採用新內容——
      要變更就重啟 daemon，舊 task 仍不能續跑。
    """

    def __init__(self, policy: AnalysisAccessPolicy, path: str) -> None:
        self._policy = policy
        self._path = path

    @property
    def startup_policy(self) -> AnalysisAccessPolicy:
        return self._policy

    @property
    def path(self) -> str:
        return self._path

    def current(self) -> AnalysisAccessPolicy:
        reloaded = load_policy(self._path)
        if reloaded.policy_digest != self._policy.policy_digest:
            raise AccessPolicyError(
                POLICY_CHANGED,
                "the analysis access policy changed since daemon startup; "
                "restart the daemon to apply it (no task may use the new policy)")
        return self._policy


def load_guard_from_env() -> PolicyGuard | None:
    """daemon 啟動時呼叫一次。未設定回 None——沒有寬鬆預設。"""
    path = configured_policy_path()
    if path is None:
        return None
    return PolicyGuard(load_policy(path), path)


__all__ = [
    "FIXED_TEMPLATE_ID",
    "POLICY_CHANGED",
    "POLICY_ENV",
    "POLICY_INVALID",
    "POLICY_NOT_CONFIGURED",
    "POLICY_UNREADABLE",
    "REQUIRED_RUN_COUNT",
    "SCHEMA_VERSION",
    "AccessPolicyError",
    "AllowedRun",
    "AnalysisAccessPolicy",
    "PolicyGuard",
    "build_policy",
    "configured_policy_path",
    "load_guard_from_env",
    "load_policy",
    "load_policy_from_env",
]


# ── 正式 resolver（注入 validate_task_runs）─────────────────────────


class PolicyProjectAccessResolver:
    """由 server policy 支持的正式 project access resolver。

    它符合 `analysis_task_template.ProjectAccessResolver` 的呼叫慣例，
    production composition 用它取代原本的 `None`。

    每次呼叫都重新確認 policy 檔案仍在且 digest 與啟動時一致（無 hot
    reload）；任何一項不成立就是 `UNVERIFIABLE`，停止新授權／派送。

    只回三種判定：

    - `DENIED`：**明確不在**政策範圍（member/project 不符、run 不在
      allowlist、來源不符）。
    - `UNVERIFIABLE`：缺政策、policy 變動、無法解析或無法確定。
    - `ALLOWED`：全部條件成立，並附上 policy 宣告的 manifest hash 供
      run/manifest 段比對。

    **不會**降級成 member-only 或「ALLOWED＋unproven」。
    """

    def __init__(self, guard: PolicyGuard) -> None:
        self._guard = guard

    def __call__(
        self, *, member_id: str, project_id: str, run_id: str,
        dqe_db_path: str | None = None, artifact_root: str | None = None,
    ) -> Any:
        from terminux_engine.packages.orfs_run_intelligence.analysis_task_template import (
            PROJECT_ACCESS_UNVERIFIABLE, ProjectAccessVerdict,
        )

        try:
            policy = self._guard.current()
        except AccessPolicyError as exc:
            return ProjectAccessVerdict(
                decision="UNVERIFIABLE", reason_code=exc.code, message=exc.message)

        if not policy.matches_scope(member_id, project_id):
            # 明確不在政策範圍。訊息不透露 policy 允許的是誰。
            return ProjectAccessVerdict(
                decision="DENIED", reason_code="ANALYSIS_ACCESS_NOT_IN_POLICY",
                message=("the analysis access policy does not cover this "
                         "member/project"))

        allowed = policy.allowed_run(run_id)
        if allowed is None:
            return ProjectAccessVerdict(
                decision="DENIED", reason_code="ANALYSIS_ACCESS_RUN_NOT_IN_POLICY",
                message="this run is not in the analysis access policy allowlist")

        ok, reason = policy.matches_sources(
            dqe_db_path=dqe_db_path, artifact_root=artifact_root)
        if not ok:
            return ProjectAccessVerdict(
                decision="UNVERIFIABLE", reason_code=PROJECT_ACCESS_UNVERIFIABLE,
                message=reason or "the server data sources could not be verified")

        return ProjectAccessVerdict(
            decision="ALLOWED", reason_code="ANALYSIS_ACCESS_POLICY_MATCH",
            message=("the server analysis access policy authorizes this "
                     "member/project to analyze this run"),
            evidence=(
                f"policy:{policy.policy_id}",
                f"policy_digest:{policy.policy_digest}",
                f"dqe_source:{policy.dqe_db_path}",
                f"artifact_root:{policy.artifact_root}",
            ),
            expected_manifest_sha256=allowed.manifest_sha256,
        )


__all__.append("PolicyProjectAccessResolver")
