# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""limited production activation 的**唯一**正式 gate 與 kill switch。

為什麼需要這個模組
------------------

parametric setup repair 產出的 `orfs_limited_activation.json` 當時只被當作**文件**驗證：產品端
`grep` 不到任何消費點。既有的 `production_activation_gate()`
是 source-level、經 code review 的常數，刻意不讀 env——那對「要不要有 bounded
execution」是對的，但它無法表達「這一個受限的 ORFS repair profile 現在開著／
關著」，也沒有任何可即時關閉的手段。

這裡補上的正是後者，並且**不建立第二套 registry**：

* 它不是新的授權來源。scope、profile digest、input manifest、image、proof
  binary 全部沿用既有 registry 與 flow profile 的值；policy 只宣告「這一組
  身分被允許」，比對不符就拒絕。
* 它不放寬任何既有限制。既有的 proposal → explicit approval → one-shot claim
  → allowlist → per-task budget 全部照舊。這個 gate 只會**再**擋一層。
* 它不是 lifecycle state。沒有 update、沒有 delete、不記錄執行歷程。

不可放寬的邊界
--------------

1. **預設 disabled**。沒有設定 policy ⇒ flow 的 create／start／launch 全部拒絕。
2. **路徑只能由 daemon startup composition 決定**（`TERMINUX_LIMITED_ACTIVATION_POLICY_FILE`）。
   caller、request、Package metadata、LLM 輸出都不能指定 policy 位置。
3. **絕對路徑、owner/mode 合格、digest 啟動時釘住**。之後每個授權點重讀並比對；
   缺失、不可讀、不合法、digest 改變一律拒絕。
4. **kill switch 只能關，不能開**。一旦在這個 process 內觀察到 disabled（或任何
   policy 失效），就**永久上鎖**；把檔案改回 enabled 也不會解鎖，必須重啟 daemon
   並重新驗證 policy。這樣「關掉」不會被檔案的來回編輯繞過，也讓重新啟用一定
   經過啟動時的完整驗證。
5. **get／cancel 永遠保留**。這個 gate 只擋「新工作與下一次派送」；讀回與取消
   不經過它，disabled、policy drift 或 provider 異常都不會讓使用者失去停止與
   讀取的能力。已落盤的證據一律不刪。
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from terminux_engine.bounded_execution.analysis_tasks.canonical import sha256_payload
from terminux_engine.bounded_execution.proposal_intake import (
    ProposalIntake, ProposalIntakeDecision, ProposalIntakeError,
    build_proposal_intake, intersect_proposal_intake,
)

SCHEMA_VERSION = 2

#: 唯一的設定來源。只讀 daemon 啟動環境。
POLICY_ENV = "TERMINUX_LIMITED_ACTIVATION_POLICY_FILE"

STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
_STATES = frozenset({STATE_ENABLED, STATE_DISABLED})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "policy_id", "state", "scope", "allowed_execution",
    "budgets", "kill_switch", "qualification", "proposal_intake",
})
_QUALIFICATION_FIELDS = frozenset({"allow_fixture_corpus"})
#: v3 起 `allowed_execution` 是**可選的 deployment restriction**，
#: 不再是產品引擎的 mandatory profile registry。v2 檔案照常可讀（等同帶
#: restriction 的 v3）。
#: `qualification` 同樣可選；**沒有宣告就是不允許** fixture corpus（fail closed）。
#: `proposal_intake` 也一樣可選；**沒有宣告就是不接受**手動 proposal。
_OPTIONAL_TOP_LEVEL_FIELDS = frozenset({
    "allowed_execution", "qualification", "proposal_intake"})
SUPPORTED_SCHEMA_VERSIONS = frozenset({2, 3})
_SCOPE_FIELDS = frozenset({"allowed_members", "allowed_projects"})
_EXECUTION_FIELDS = frozenset({
    "profile_id", "profile_digest", "template_id", "recipe_id",
    "acceptance_policy_id", "proof_policy_id", "docker_image_id",
    "input_manifest_sha256", "proof_binary_identity",
})
#: 工具身分恰好擇一：舊 Profile 釘 image，host-native Profile 釘 binding digest。
_EXECUTION_TOOL_FIELDS = frozenset({"docker_image_id", "native_tool_binding_sha256"})
_PROOF_FIELDS = frozenset({
    "executable_realpath", "executable_sha256", "version_sha256",
})
_BUDGET_FIELDS = frozenset({
    "max_concurrent_tasks", "task_deadline_sec", "run_timeout_sec",
    "max_eda_launches_per_task", "max_body_llm_dispatches_per_task",
    "runtime_retries",
})
_KILL_SWITCH_FIELDS = frozenset({"action", "stops", "preserves"})

#: kill switch **必須**保留的能力。policy 不得宣稱關掉它們。
REQUIRED_PRESERVED = ("agent.flow_task.get", "agent.flow_task.cancel")

# ── 拒絕碼 ────────────────────────────────────────────────────────────

NOT_CONFIGURED = "LIMITED_ACTIVATION_NOT_CONFIGURED"
UNREADABLE = "LIMITED_ACTIVATION_POLICY_UNREADABLE"
INVALID = "LIMITED_ACTIVATION_POLICY_INVALID"
INSECURE = "LIMITED_ACTIVATION_POLICY_INSECURE"
CHANGED = "LIMITED_ACTIVATION_POLICY_CHANGED"
DISABLED = "LIMITED_ACTIVATION_DISABLED"
LATCHED = "LIMITED_ACTIVATION_LATCHED_OFF"
SCOPE_MISMATCH = "LIMITED_ACTIVATION_SCOPE_NOT_ALLOWED"
IDENTITY_MISMATCH = "LIMITED_ACTIVATION_IDENTITY_NOT_ALLOWED"
#: Profile 宣告了 qualification fixture corpus，但這份 policy 不允許。
FIXTURE_NOT_ALLOWED = "LIMITED_ACTIVATION_FIXTURE_CORPUS_NOT_ALLOWED"


class LimitedActivationError(RuntimeError):
    """policy 缺席、不合法、漂移、已關閉，或身分不符。一律 fail-closed。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── 模型 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProofBinaryIdentity:
    executable_realpath: str
    executable_sha256: str
    version_sha256: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "executable_realpath": self.executable_realpath,
            "executable_sha256": self.executable_sha256,
            "version_sha256": self.version_sha256,
        }


@dataclass(frozen=True)
class AllowedExecution:
    """唯一允許執行的那一組身分。任何一項不符即拒絕。"""

    profile_id: str
    profile_digest: str
    template_id: str
    recipe_id: str
    acceptance_policy_id: str
    proof_policy_id: str
    docker_image_id: str | None
    input_manifest_sha256: str
    proof_binary_identity: ProofBinaryIdentity
    native_tool_binding_sha256: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        if self.native_tool_binding_sha256 is not None:
            return {
                "profile_id": self.profile_id,
                "profile_digest": self.profile_digest,
                "template_id": self.template_id,
                "recipe_id": self.recipe_id,
                "acceptance_policy_id": self.acceptance_policy_id,
                "proof_policy_id": self.proof_policy_id,
                "native_tool_binding_sha256": self.native_tool_binding_sha256,
                "input_manifest_sha256": self.input_manifest_sha256,
                "proof_binary_identity": self.proof_binary_identity.to_json_dict(),
            }
        return {
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "template_id": self.template_id,
            "recipe_id": self.recipe_id,
            "acceptance_policy_id": self.acceptance_policy_id,
            "proof_policy_id": self.proof_policy_id,
            "docker_image_id": self.docker_image_id,
            "input_manifest_sha256": self.input_manifest_sha256,
            "proof_binary_identity": self.proof_binary_identity.to_json_dict(),
        }


@dataclass(frozen=True)
class ActivationBudgets:
    max_concurrent_tasks: int
    task_deadline_sec: int
    run_timeout_sec: int
    max_eda_launches_per_task: int
    max_body_llm_dispatches_per_task: int
    runtime_retries: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "task_deadline_sec": self.task_deadline_sec,
            "run_timeout_sec": self.run_timeout_sec,
            "max_eda_launches_per_task": self.max_eda_launches_per_task,
            "max_body_llm_dispatches_per_task":
                self.max_body_llm_dispatches_per_task,
            "runtime_retries": self.runtime_retries,
        }


@dataclass(frozen=True)
class KillSwitch:
    action: str
    stops: tuple[str, ...]
    preserves: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {"action": self.action, "stops": list(self.stops),
                "preserves": list(self.preserves)}


@dataclass(frozen=True)
class LimitedActivationPolicy:
    """不可變的已載入 policy。"""

    schema_version: int
    policy_id: str
    state: str
    allowed_members: tuple[str, ...]
    allowed_projects: tuple[str, ...]
    #: `None` ⇒ 沒有 deployment restriction，任何通過 scope 的 task-scoped
    #: profile 都可以由 user 以新 DRAFT 核准執行。
    allowed_execution: AllowedExecution | None
    budgets: ActivationBudgets
    kill_switch: KillSwitch
    policy_digest: str
    #: 只有明確宣告 `qualification.allow_fixture_corpus` 的 policy 才允許
    #: fixture corpus replay；production／live policy 一律拒絕。
    allows_fixture_corpus: bool = False
    #: Phase A：這台 daemon 允許的手動 proposal intake。`None` ⇒ 一律不收。
    proposal_intake: ProposalIntake | None = None
    #: `state` **以外**全部內容的 digest。drift 比對用這一個。
    #:
    #: 為什麼要分兩個：如果 drift 比對含 `state`，那麼 operator 把
    #: `enabled` 改成 `disabled`（正是 kill switch 的正常操作）會先被判成
    #: 「policy 被竄改」。兩件事必須分得開——按下開關是刻意的、要回報
    #: `DISABLED`；改動 scope／identity／budget 才是竄改，回報 `CHANGED`。
    #: 兩者都會 latch，所以分開不會讓任何一邊變寬鬆。
    identity_digest: str = ""

    @property
    def enabled(self) -> bool:
        return self.state == STATE_ENABLED

    def matches_scope(self, member_id: str, project_id: str) -> bool:
        return (member_id in self.allowed_members
                and project_id in self.allowed_projects)

    def matches_profile(self, profile: Any) -> tuple[bool, str | None]:
        """profile 的身分必須與 policy 允許的那一組**完全**相符。

        比對 profile_id、profile_digest、template_id、recipe_id、
        acceptance/proof policy、image ID 與 input manifest hash。訊息只說
        「哪一類不符」，不回傳 policy 允許的值。

        沒有 `allowed_execution`（v3 無 restriction）就一律相符——
        policy 控制的是 AI side effect 是否啟用與 scope，不是工程 Profile
        allowlist。
        """
        wanted = self.allowed_execution
        if wanted is None:
            return True, None
        checks: tuple[tuple[str, Any, Any], ...] = (
            ("profile_id", getattr(profile, "profile_id", None), wanted.profile_id),
            ("profile_digest", getattr(profile, "profile_digest", None),
             wanted.profile_digest),
            ("template_id", getattr(profile, "template_id", None), wanted.template_id),
            ("docker_image_id", getattr(profile, "docker_image_id", None),
             wanted.docker_image_id),
            ("native tool binding",
             getattr(getattr(profile, "native_tool_binding", None), "digest", None),
             wanted.native_tool_binding_sha256),
            ("input_manifest_sha256", getattr(profile, "input_manifest_sha256", None),
             wanted.input_manifest_sha256),
        )
        for name, actual, expected in checks:
            if actual != expected:
                return False, f"the flow profile {name} is not the activated one"
        recipe = getattr(profile, "repair_recipe", None)
        if getattr(recipe, "recipe_id", None) != wanted.recipe_id:
            return False, "the flow profile recipe is not the activated one"
        if getattr(recipe, "acceptance_policy_id", None) != wanted.acceptance_policy_id:
            return False, "the flow profile acceptance policy is not the activated one"
        if getattr(recipe, "proof_policy_id", None) != wanted.proof_policy_id:
            return False, "the flow profile proof policy is not the activated one"
        return True, None

    def _identity_material(self) -> dict[str, Any]:
        """drift 比對的材料：`state` 以外的全部內容。"""
        material = self._digest_material()
        material.pop("state", None)
        return material

    def _digest_material(self) -> dict[str, Any]:
        """digest 材料**不含**檔案位置——policy 搬位置不改變它的身分。"""
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "state": self.state,
            "scope": {
                "allowed_members": list(self.allowed_members),
                "allowed_projects": list(self.allowed_projects),
            },
            **({"allowed_execution": self.allowed_execution.to_json_dict()}
               if self.allowed_execution is not None else {}),
            "budgets": self.budgets.to_json_dict(),
            "kill_switch": self.kill_switch.to_json_dict(),
            **({"qualification": {"allow_fixture_corpus": True}}
               if self.allows_fixture_corpus else {}),
            **({"proposal_intake": self.proposal_intake.to_json_dict()}
               if self.proposal_intake is not None else {}),
        }

    @property
    def has_deployment_restriction(self) -> bool:
        return self.allowed_execution is not None

    def to_json_dict(self) -> dict[str, Any]:
        payload = self._digest_material()
        payload["policy_digest"] = self.policy_digest
        payload["identity_digest"] = self.identity_digest
        return payload

    def public_summary(self) -> dict[str, Any]:
        """可外露的摘要。不含路徑、hash 或 allowlist 內容。"""
        return {
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "schema_version": self.schema_version,
            "state": self.state,
            "deployment_restriction": self.allowed_execution is not None,
            "allows_fixture_corpus": self.allows_fixture_corpus,
            "accepts_manual_proposals": self.proposal_intake is not None,
            **({"activated_profile_id": self.allowed_execution.profile_id,
                "activated_template_id": self.allowed_execution.template_id}
               if self.allowed_execution is not None else {}),
            "max_concurrent_tasks": self.budgets.max_concurrent_tasks,
        }


# ── 載入與驗證 ───────────────────────────────────────────────────────


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise LimitedActivationError(
                INVALID, f"duplicate JSON key in policy: {key!r}")
        seen.add(key)
    return dict(pairs)


def _assert_secure_file(path: Path) -> None:
    """policy 檔本身必須不可被別人改：非 symlink、屬於本 uid、group/other 不可寫。

    這**不宣稱**能抵抗同一個 Linux 帳號的持有者——那個人本來就能改產品程式。
    它擋的是「別的帳號或寬鬆權限讓 policy 被第三方改寫」。
    """
    try:
        info = path.lstat()
    except OSError as exc:
        raise LimitedActivationError(
            UNREADABLE, f"the activation policy could not be inspected: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise LimitedActivationError(
            INSECURE, "the activation policy must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise LimitedActivationError(
            INSECURE, "the activation policy must be a regular file")
    if info.st_uid != os.getuid():
        raise LimitedActivationError(
            INSECURE, "the activation policy must be owned by the daemon user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise LimitedActivationError(
            INSECURE, "the activation policy must not be group- or world-writable")


def load_policy(path: str | Path) -> LimitedActivationPolicy:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise LimitedActivationError(
            INVALID, f"{POLICY_ENV} must be an absolute path: {path!r}")
    _assert_secure_file(candidate)
    try:
        raw = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise LimitedActivationError(
            UNREADABLE, f"the activation policy could not be read: {exc}") from exc
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except LimitedActivationError:
        raise
    except ValueError as exc:
        raise LimitedActivationError(
            INVALID, f"the activation policy is not valid JSON: {exc}") from exc
    return build_policy(document)


def _require_section(document: dict[str, Any], name: str,
                     fields: frozenset[str]) -> dict[str, Any]:
    section = document[name]
    if not isinstance(section, dict):
        raise LimitedActivationError(INVALID, f"{name} must be an object")
    unknown = sorted(set(section) - fields)
    if unknown:
        raise LimitedActivationError(INVALID, f"unknown {name} field(s): {unknown}")
    missing = sorted(fields - set(section))
    if missing:
        raise LimitedActivationError(INVALID, f"missing {name} field(s): {missing}")
    return section


def _require_id_list(value: Any, name: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(v, str) or not v.strip() for v in value)):
        raise LimitedActivationError(
            INVALID, f"{name} must be a non-empty list of non-empty strings")
    if len(set(value)) != len(value):
        raise LimitedActivationError(INVALID, f"{name} contains duplicates")
    return tuple(value)


def build_policy(document: Any) -> LimitedActivationPolicy:
    """驗證一份已解析的 policy。未知欄位、缺欄位、格式不符一律拒絕。"""
    if not isinstance(document, dict):
        raise LimitedActivationError(
            INVALID, "the activation policy must be a JSON object")
    unknown = sorted(set(document) - _TOP_LEVEL_FIELDS)
    if unknown:
        raise LimitedActivationError(INVALID, f"unknown policy field(s): {unknown}")
    schema_version = document.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise LimitedActivationError(
            INVALID,
            f"unsupported policy schema_version: {schema_version!r}")
    # v2：allowed_execution 必填（歷史語意）；v3：可選。
    required = (_TOP_LEVEL_FIELDS - {"qualification", "proposal_intake"}
                if schema_version == 2
                else _TOP_LEVEL_FIELDS - _OPTIONAL_TOP_LEVEL_FIELDS)
    missing = sorted(required - set(document))
    if missing:
        raise LimitedActivationError(INVALID, f"missing policy field(s): {missing}")
    policy_id = document["policy_id"]
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise LimitedActivationError(INVALID, "policy_id must be a non-empty string")
    state = document["state"]
    if state not in _STATES:
        raise LimitedActivationError(
            INVALID, f"state must be one of {sorted(_STATES)}; got {state!r}")

    scope = _require_section(document, "scope", _SCOPE_FIELDS)
    members = _require_id_list(scope["allowed_members"], "scope.allowed_members")
    projects = _require_id_list(scope["allowed_projects"], "scope.allowed_projects")

    allowed_execution: AllowedExecution | None = None
    if "allowed_execution" in document:
        allowed_execution = _build_allowed_execution(document)

    budgets_raw = _require_section(document, "budgets", _BUDGET_FIELDS)
    budget_values: dict[str, int] = {}
    for field_name in sorted(_BUDGET_FIELDS):
        value = budgets_raw[field_name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LimitedActivationError(
                INVALID, f"budgets.{field_name} must be a non-negative integer")
        budget_values[field_name] = value
    if budget_values["max_concurrent_tasks"] < 1:
        raise LimitedActivationError(
            INVALID, "budgets.max_concurrent_tasks must be at least 1")
    if budget_values["max_eda_launches_per_task"] < 1:
        raise LimitedActivationError(
            INVALID, "budgets.max_eda_launches_per_task must be at least 1")

    kill_raw = _require_section(document, "kill_switch", _KILL_SWITCH_FIELDS)
    action = kill_raw["action"]
    if not isinstance(action, str) or not action.strip():
        raise LimitedActivationError(
            INVALID, "kill_switch.action must be a non-empty string")
    stops = _require_id_list(kill_raw["stops"], "kill_switch.stops")
    preserves = _require_id_list(kill_raw["preserves"], "kill_switch.preserves")
    for required in REQUIRED_PRESERVED:
        if required not in preserves:
            raise LimitedActivationError(
                INVALID,
                f"kill_switch.preserves must include {required!r}; the kill "
                f"switch may never remove the ability to read or stop work")
    overlap = sorted(set(stops) & set(preserves))
    if overlap:
        raise LimitedActivationError(
            INVALID, f"kill_switch lists the same capability as both stopped "
                     f"and preserved: {overlap}")

    allows_fixture = False
    if "qualification" in document:
        if schema_version == 2:
            raise LimitedActivationError(
                INVALID, "qualification requires policy schema_version 3")
        section = _require_section(document, "qualification", _QUALIFICATION_FIELDS)
        value = section["allow_fixture_corpus"]
        if not isinstance(value, bool):
            raise LimitedActivationError(
                INVALID, "qualification.allow_fixture_corpus must be a boolean")
        allows_fixture = value

    proposal_intake: ProposalIntake | None = None
    if "proposal_intake" in document:
        if schema_version == 2:
            raise LimitedActivationError(
                INVALID, "proposal_intake requires policy schema_version 3")
        try:
            proposal_intake = build_proposal_intake(
                document["proposal_intake"], where="policy.proposal_intake")
        except ProposalIntakeError as exc:
            raise LimitedActivationError(INVALID, exc.message) from exc

    policy = LimitedActivationPolicy(
        schema_version=int(schema_version),
        policy_id=policy_id,
        state=state,
        allowed_members=members,
        allowed_projects=projects,
        allowed_execution=allowed_execution,
        budgets=ActivationBudgets(**budget_values),
        kill_switch=KillSwitch(action=action, stops=stops, preserves=preserves),
        policy_digest="", allows_fixture_corpus=allows_fixture,
        proposal_intake=proposal_intake,
    )
    return replace(
        policy,
        policy_digest=sha256_payload(policy._digest_material()),
        identity_digest=sha256_payload(policy._identity_material()),
    )


def _build_allowed_execution(document: dict[str, Any]) -> AllowedExecution:
    """`allowed_execution`（deployment restriction）的驗證。v2 必填，v3 可選。"""
    raw_execution = document.get("allowed_execution")
    present_tools = (set(raw_execution) & _EXECUTION_TOOL_FIELDS
                     if isinstance(raw_execution, dict) else set())
    if isinstance(raw_execution, dict) and len(present_tools) != 1:
        raise LimitedActivationError(
            INVALID,
            "allowed_execution must pin exactly one tool identity "
            f"({sorted(_EXECUTION_TOOL_FIELDS)})")
    execution = _require_section(
        document, "allowed_execution",
        (_EXECUTION_FIELDS - _EXECUTION_TOOL_FIELDS) | present_tools)
    for field_name in ("profile_id", "template_id", "recipe_id",
                       "acceptance_policy_id", "proof_policy_id"):
        value = execution[field_name]
        if not isinstance(value, str) or not value.strip():
            raise LimitedActivationError(
                INVALID, f"allowed_execution.{field_name} must be a non-empty string")
    for field_name in ("profile_digest", "input_manifest_sha256"):
        value = execution[field_name]
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            raise LimitedActivationError(
                INVALID,
                f"allowed_execution.{field_name} must be 64 lowercase hex chars")
    image_id = execution.get("docker_image_id")
    native_binding = execution.get("native_tool_binding_sha256")
    if "docker_image_id" in execution and (
            not isinstance(image_id, str) or not _IMAGE_ID_RE.match(image_id)):
        raise LimitedActivationError(
            INVALID, "allowed_execution.docker_image_id must be 'sha256:<64 hex>'")
    if "native_tool_binding_sha256" in execution and (
            not isinstance(native_binding, str) or not _SHA256_RE.match(native_binding)):
        raise LimitedActivationError(
            INVALID, "allowed_execution.native_tool_binding_sha256 must be 64 lowercase hex")

    proof_raw = execution["proof_binary_identity"]
    if not isinstance(proof_raw, dict):
        raise LimitedActivationError(
            INVALID, "allowed_execution.proof_binary_identity must be an object")
    extra = sorted(set(proof_raw) - _PROOF_FIELDS)
    if extra:
        raise LimitedActivationError(
            INVALID, f"unknown proof_binary_identity field(s): {extra}")
    absent = sorted(_PROOF_FIELDS - set(proof_raw))
    if absent:
        raise LimitedActivationError(
            INVALID, f"missing proof_binary_identity field(s): {absent}")
    realpath = proof_raw["executable_realpath"]
    if not isinstance(realpath, str) or not Path(realpath).is_absolute():
        raise LimitedActivationError(
            INVALID, "proof_binary_identity.executable_realpath must be absolute")
    for field_name in ("executable_sha256", "version_sha256"):
        value = proof_raw[field_name]
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            raise LimitedActivationError(
                INVALID,
                f"proof_binary_identity.{field_name} must be 64 lowercase hex chars")
    return AllowedExecution(
        profile_id=execution["profile_id"],
        profile_digest=execution["profile_digest"],
        template_id=execution["template_id"],
        recipe_id=execution["recipe_id"],
        acceptance_policy_id=execution["acceptance_policy_id"],
        proof_policy_id=execution["proof_policy_id"],
        docker_image_id=image_id,
        native_tool_binding_sha256=native_binding,
        input_manifest_sha256=execution["input_manifest_sha256"],
        proof_binary_identity=ProofBinaryIdentity(
            executable_realpath=realpath,
            executable_sha256=proof_raw["executable_sha256"],
            version_sha256=proof_raw["version_sha256"],
        ),
    )


def configured_policy_path() -> str | None:
    value = os.environ.get(POLICY_ENV, "").strip()
    return value or None


# ── 啟動時載入一次，之後只驗「沒有變」＋ latching kill switch ─────────


class LimitedActivationGuard:
    """持有啟動時載入的 policy，並在每個授權點確認它仍然有效。

    `current()` 重讀檔案並比對 digest：

    * 檔案不見、讀不到、解析失敗、digest 改變 ⇒ 拋（停止新工作與派送）。
    * `state` 不是 `enabled` ⇒ 拋 `DISABLED`。
    * **任何**一次上述失敗都會 latch：之後即使檔案恢復成合法的 enabled，
      這個 process 仍然拒絕（`LATCHED`）。要重新啟用必須重啟 daemon，
      讓 policy 走完啟動時的完整驗證。

    latch 只影響「新工作與下一次派送」。`get`／`cancel` 不經過這裡。
    """

    def __init__(self, policy: LimitedActivationPolicy, path: str) -> None:
        self._policy = policy
        self._path = path
        self._lock = threading.Lock()
        self._latched: LimitedActivationError | None = None
        if not policy.enabled:
            # 啟動時就是 disabled：直接上鎖，語義與執行中被關掉一致。
            self._latched = LimitedActivationError(
                DISABLED, "the limited activation policy is disabled")

    @property
    def startup_policy(self) -> LimitedActivationPolicy:
        return self._policy

    @property
    def path(self) -> str:
        return self._path

    @property
    def latched_off(self) -> bool:
        with self._lock:
            return self._latched is not None

    @property
    def latched_reason(self) -> str | None:
        with self._lock:
            return None if self._latched is None else self._latched.code

    def _latch(self, error: LimitedActivationError) -> LimitedActivationError:
        with self._lock:
            if self._latched is None:
                self._latched = error
        return error

    def current(self) -> LimitedActivationPolicy:
        with self._lock:
            latched = self._latched
        if latched is not None:
            raise LimitedActivationError(
                LATCHED,
                "limited activation was switched off in this daemon instance "
                f"({latched.code}); restart the daemon to re-validate the policy")
        try:
            reloaded = load_policy(self._path)
        except LimitedActivationError as exc:
            raise self._latch(exc) from exc
        # 先看「開關」再看「竄改」：把 state 改成 disabled 是正常的 kill
        # switch 操作，必須回報 DISABLED 而不是 CHANGED。
        if not reloaded.enabled:
            raise self._latch(LimitedActivationError(
                DISABLED, "the limited activation policy is disabled"))
        if reloaded.identity_digest != self._policy.identity_digest:
            raise self._latch(LimitedActivationError(
                CHANGED,
                "the limited activation policy changed since daemon startup; "
                "restart the daemon to apply it"))
        return self._policy


def load_guard_from_env() -> LimitedActivationGuard | None:
    """daemon 啟動時呼叫一次。未設定回 None——**沒有寬鬆預設**（flow 全拒）。"""
    path = configured_policy_path()
    if path is None:
        return None
    return LimitedActivationGuard(load_policy(path), path)


# ── 唯一的 gate ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LimitedActivationGate:
    """flow 路徑的正式 gate：既有 base gate **加上** limited activation policy。

    `enabled` 沿用既有慣例（`getattr(gate, "enabled", False)`），因此既有的
    check point 不需要新增第二種判斷方式。
    """

    base: Any
    guard: LimitedActivationGuard | None

    @property
    def enabled(self) -> bool:
        if not getattr(self.base, "enabled", False):
            return False
        if self.guard is None:
            return False
        try:
            self.guard.current()
        except LimitedActivationError:
            return False
        return True

    @property
    def reason(self) -> str:
        denial = self.denial()
        if denial is None:
            policy = self.guard.startup_policy  # type: ignore[union-attr]
            return (f"limited_activation:{policy.policy_id}:"
                    f"{policy.policy_digest}")
        return f"{denial[0]}: {denial[1]}"

    def denial(self) -> tuple[str, str] | None:
        """回傳 `(code, message)`；`None` 表示目前允許新工作／派送。"""
        if not getattr(self.base, "enabled", False):
            return ("BOUNDED_EXECUTION_VALIDATION_REQUIRED",
                    "bounded execution is not activated on this daemon")
        if self.guard is None:
            return (NOT_CONFIGURED,
                    "this daemon has no limited activation policy; "
                    "limited production execution is disabled by default")
        try:
            self.guard.current()
        except LimitedActivationError as exc:
            return (exc.code, exc.message)
        return None

    def check(self, *, member_id: str, project_id: str,
              profile: Any = None) -> tuple[str, str] | None:
        """create／start／每次 launch 前的**同一個**檢查。

        順序固定：base gate → policy 有效 → scope → profile 身分。
        任何一項不成立即回傳拒絕，不繼續往下猜。
        """
        denial = self.denial()
        if denial is not None:
            return denial
        policy = self.guard.current()  # type: ignore[union-attr]
        if not policy.matches_scope(member_id, project_id):
            return (SCOPE_MISMATCH,
                    "the limited activation policy does not cover this "
                    "member/project")
        if profile is not None:
            ok, reason = policy.matches_profile(profile)
            if not ok:
                return (IDENTITY_MISMATCH,
                        reason or "the flow profile is not the activated one")
            # fixture corpus 只有明確允許 qualification 的 policy 能用；
            # production／live policy 在 Work／child 建立前就拒絕。
            if (getattr(profile, "is_qualification_fixture", False)
                    and not policy.allows_fixture_corpus):
                return (FIXTURE_NOT_ALLOWED,
                        "this profile declares a QUALIFICATION_ONLY fixture corpus, "
                        "but the activation policy does not allow fixture corpus "
                        "replay")
        return None

    def proposal_intake_decision(self, profile: Any) -> ProposalIntakeDecision:
        """Phase A：這個 Profile 在這台 daemon 上可收哪些手動 proposal。

        順序與 `check()` 一致：base gate → policy 有效 → 兩側 intake 交集。
        gate 本身不放行（policy 缺席／disabled／漂移／latched）時，理由碼原樣
        回傳——手動 proposal 不繞過 kill switch。這個判定**不**建立授權、
        grant 或 task，它只決定「這筆 proposal 可不可以被收下並保存」。
        """
        denial = self.denial()
        if denial is not None:
            return ProposalIntakeDecision.denied(denial[0])
        policy = self.guard.current()  # type: ignore[union-attr]
        return intersect_proposal_intake(
            getattr(profile, "proposal_intake", None), policy.proposal_intake)

    def blocked_response(self) -> dict[str, object]:
        """沿用既有 base gate 的 blocked 形狀，只換掉 reason。"""
        denial = self.denial()
        response = dict(self.base.blocked_response())
        if denial is not None:
            response["error_code"] = denial[0]
            response["activation_reason"] = denial[1]
        return response

    def public_summary(self) -> dict[str, Any]:
        denial = self.denial()
        summary: dict[str, Any] = {
            "limited_activation_enabled": denial is None,
            "policy_configured": self.guard is not None,
        }
        if denial is not None:
            summary["denial_code"] = denial[0]
        if self.guard is not None:
            summary["policy"] = self.guard.startup_policy.public_summary()
            summary["latched_off"] = self.guard.latched_off
        return summary


__all__ = [
    "CHANGED",
    "DISABLED",
    "FIXTURE_NOT_ALLOWED",
    "IDENTITY_MISMATCH",
    "INSECURE",
    "INVALID",
    "LATCHED",
    "NOT_CONFIGURED",
    "POLICY_ENV",
    "REQUIRED_PRESERVED",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SCOPE_MISMATCH",
    "STATE_DISABLED",
    "STATE_ENABLED",
    "UNREADABLE",
    "ActivationBudgets",
    "AllowedExecution",
    "KillSwitch",
    "LimitedActivationError",
    "LimitedActivationGate",
    "LimitedActivationGuard",
    "LimitedActivationPolicy",
    "ProofBinaryIdentity",
    "ProposalIntake",
    "ProposalIntakeDecision",
    "build_policy",
    "configured_policy_path",
    "load_guard_from_env",
    "load_policy",
]
