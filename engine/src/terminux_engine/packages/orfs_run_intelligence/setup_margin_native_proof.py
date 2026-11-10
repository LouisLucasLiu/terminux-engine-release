# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""host-native setup-margin proof 契約（`aes.setup_slack_margin.v1` 的 native 版本）。

container 版本（`method_extensions.SETUP_MARGIN`）的 proof 工具身分以 image ID 為
必要欄位（`proof_tool_identity.REQUIRED_FIELDS`）；host-native run 沒有 image，沿用它
只會一律 `proof_tool_identity_incomplete`。本模組是**另一份**有版本的契約，container
版本的 descriptor、canonical bytes、digest 與 reader 都不改：

- proof 方法不變：Profile 釘住的 in-flow helper 以 canonical transition miter
  （`yosys_transition_miter.v1`，envelope schema v2）證明兩組 mapped pair；
- 工具身分由 Profile `execution.native_tool_binding.tools.FORMAL_YOSYS` 的 realpath ＋
  SHA-256 授權；版本字串只記錄，不是身分；
- 身分 schema、descriptor、acceptance policy 與 digest 都有自己的版本與 domain tag，
  container 版本的身分（帶 image ID）一律不接受。

工具身分在四個時點核對：選用 Profile 時（qualified identity 檔 ↔ pin ↔ 主機）、
派送前與 exec 前（native flow adapter 的 pin 核對）、判定時（主機上的 binary 必須
仍是 pin）。proof 的輸入（gold／gate netlist、liberty）在判定時重新雜湊；helper 腳本
由 Profile 的 sha256 宣告與 input inventory 綁定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from terminux_engine.orchestration.orfs_flow_task_adapter import NATIVE_PROOF_TOOL_ENV
from terminux_engine.orchestration.native_tool_identity import (
    FilePin,
    NativeToolIdentityError,
    parse_pin,
    sha256_file,
    verify_pin,
)

DESCRIPTOR_SCHEMA = "orfs.setup_margin.native_proof_descriptor.v1"
IDENTITY_SCHEMA = "orfs.setup_margin.native_proof_tool_identity.v1"
ACCEPTANCE_POLICY_ID = "aes.setup_slack_margin_acceptance.native.v1"
ACCEPTANCE_POLICY_VERSION = "v1"
EXECUTION_MODE = "host_native"
#: Profile 授權 proof binary 的 pin 名稱（與 formal pair native 契約相同）。
BINDING_TOOL_NAME = "FORMAL_YOSYS"
#: native flow adapter 匯出給 in-flow helper 的 proof binary 路徑。
PROOF_TOOL_ENV = NATIVE_PROOF_TOOL_ENV
#: proof 方法與 envelope 版本沿用 canonical transition miter。
PROOF_POLICY_ID = "yosys_transition_miter.v1"
ENVELOPE_SCHEMA_VERSION = 2
PAIR_LABELS: tuple[str, ...] = ("baseline_candidate_post_cts", "baseline_candidate_final")

#: 每一組 pair proof 的判定。四者互斥。
PROOF_PASS = "PROOF_PASS"
PROOF_FAIL = "PROOF_FAIL"
UNVERIFIABLE = "UNVERIFIABLE"
NOT_EXECUTED = "NOT_EXECUTED"
PAIR_STATUSES: tuple[str, ...] = (PROOF_PASS, PROOF_FAIL, UNVERIFIABLE, NOT_EXECUTED)

#: 本契約才有的 reason code。
R_NATIVE_PROOF_NOT_EXECUTED = "native_proof_not_executed"
R_NATIVE_PROOF_IDENTITY = "native_proof_tool_identity_not_established"
R_NATIVE_PROOF_INPUTS = "native_proof_inputs_not_established"
R_NATIVE_PROOF_TOOL_CHANGED = "native_proof_tool_changed_after_proof"

#: container 身分欄位。出現在 native 身分裡就代表來源混用，一律拒絕。
_CONTAINER_IDENTITY_FIELDS = frozenset({"image_id", "repo_digest", "image_tag",
                                        "image_tag_display_only"})
_HASH_FIELDS: tuple[str, ...] = (
    "version_sha256", "wrapper_sha256", "generator_sha256", "cell_model_sha256",
    "liberty_sha256", "gold_sha256", "gate_sha256")
_IDENTITY_FIELDS = frozenset({
    "schema", "execution_mode", "executable_realpath", "executable_sha256",
    "version_output", "argv", "mode", "digest", *_HASH_FIELDS})
#: 任何 native 身分（含 qualified identity 檔）都必須有的欄位。
_BASE_REQUIRED: tuple[str, ...] = ("executable_realpath", "executable_sha256", "argv", "mode")
#: 一次 proof 的身分另外必須帶的輸入身分：產生 miter 的 helper、cell model 與
#: liberty、兩側 netlist。
_PROOF_REQUIRED: tuple[str, ...] = (
    "generator_sha256", "cell_model_sha256", "liberty_sha256", "gold_sha256", "gate_sha256")
#: envelope 頂層欄位 ↔ 身分欄位；兩處都記錄，必須一致。
_ENVELOPE_HASHES: tuple[tuple[str, str], ...] = (
    ("gold_sha256", "gold_sha256"), ("gate_sha256", "gate_sha256"),
    ("liberty_sha256", "liberty_sha256"), ("cell_model_sha256", "cell_model_sha256"))
#: 判定時重新雜湊的 proof 輸入（envelope 的路徑欄位 → 雜湊欄位）。
_REHASHED_INPUTS: tuple[tuple[str, str], ...] = (
    ("gold", "gold_sha256"), ("gate", "gate_sha256"), ("liberty", "liberty_sha256"))


class NativeProofIdentityError(ValueError):
    """native proof 身分不成立；`code` 為固定 reason code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class NativeProofToolIdentity:
    """產生 setup-margin proof 的站點 binary 與 proof 輸入身分。

    digest 以 `IDENTITY_SCHEMA` 為 domain tag 計算，與 container 版本
    （`ProofToolIdentity.digest`）的材料格式不同，兩者不可能相同。
    `version_output` 只記錄，不進 digest；`version_sha256` 進 digest 但不是比對依據。
    """

    executable_realpath: str | None = None
    executable_sha256: str | None = None
    version_output: str | None = None
    version_sha256: str | None = None
    argv: tuple[str, ...] = ()
    mode: str | None = None
    wrapper_sha256: str | None = None
    generator_sha256: str | None = None
    cell_model_sha256: str | None = None
    liberty_sha256: str | None = None
    gold_sha256: str | None = None
    gate_sha256: str | None = None

    def canonical_material(self) -> dict[str, Any]:
        return {"schema": IDENTITY_SCHEMA, "execution_mode": EXECUTION_MODE,
                "executable_realpath": self.executable_realpath,
                "executable_sha256": self.executable_sha256,
                "argv": list(self.argv), "mode": self.mode,
                **{name: getattr(self, name) for name in _HASH_FIELDS}}

    @property
    def digest(self) -> str:
        text = _canonical(self.canonical_material())
        return hashlib.sha256(f"{IDENTITY_SCHEMA}\n{text}".encode("utf-8")).hexdigest()

    def missing_fields(self, *, proof: bool) -> tuple[str, ...]:
        names = _BASE_REQUIRED + (_PROOF_REQUIRED if proof else ())
        return tuple(name for name in names
                     if getattr(self, name) in (None, "", ()))

    def to_json_dict(self) -> dict[str, Any]:
        return {**self.canonical_material(), "version_output": self.version_output,
                "digest": self.digest}

    @classmethod
    def from_json_dict(cls, payload: Any) -> "NativeProofToolIdentity":
        """嚴格讀回：schema、執行模式與欄位集合都必須相符；container 欄位拒絕。"""
        if not isinstance(payload, dict):
            raise NativeProofIdentityError(R_NATIVE_PROOF_IDENTITY,
                                           "the native proof tool identity is not an object")
        mixed = sorted(_CONTAINER_IDENTITY_FIELDS & set(payload))
        if mixed:
            raise NativeProofIdentityError(
                R_NATIVE_PROOF_IDENTITY,
                f"a container identity ({', '.join(mixed)}) is not a host-native proof "
                "tool identity")
        if payload.get("schema") != IDENTITY_SCHEMA:
            raise NativeProofIdentityError(
                R_NATIVE_PROOF_IDENTITY,
                f"the proof tool identity schema is {payload.get('schema')!r}, not "
                f"{IDENTITY_SCHEMA!r}")
        if payload.get("execution_mode") != EXECUTION_MODE:
            raise NativeProofIdentityError(R_NATIVE_PROOF_IDENTITY,
                                           "the proof tool identity is not host-native")
        unknown = sorted(set(payload) - _IDENTITY_FIELDS)
        if unknown:
            raise NativeProofIdentityError(
                R_NATIVE_PROOF_IDENTITY, f"unknown proof tool identity field(s): {unknown}")
        argv = payload.get("argv")
        if argv is None:
            argv = []
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise NativeProofIdentityError(R_NATIVE_PROOF_IDENTITY,
                                           "the proof tool argv must be a list of strings")
        for name in ("executable_realpath", "executable_sha256", "version_output", "mode",
                     *_HASH_FIELDS):
            value = payload.get(name)
            if value is not None and not isinstance(value, str):
                raise NativeProofIdentityError(
                    R_NATIVE_PROOF_IDENTITY, f"proof tool identity {name} must be a string")
        identity = cls(
            executable_realpath=payload.get("executable_realpath"),
            executable_sha256=payload.get("executable_sha256"),
            version_output=payload.get("version_output"),
            version_sha256=payload.get("version_sha256"),
            argv=tuple(argv), mode=payload.get("mode"),
            **{name: payload.get(name) for name in _HASH_FIELDS if name != "version_sha256"})
        if "digest" in payload and payload["digest"] != identity.digest:
            raise NativeProofIdentityError(R_NATIVE_PROOF_IDENTITY,
                                           "the proof tool identity digest does not recompute")
        return identity


def _pin(pin: Any) -> FilePin:
    if isinstance(pin, FilePin):
        return pin
    try:
        return parse_pin(pin, where=BINDING_TOOL_NAME)
    except NativeToolIdentityError as exc:
        raise NativeProofIdentityError(R_NATIVE_PROOF_IDENTITY, f"{exc.code}: {exc.message}") from exc


def proof_descriptor() -> dict[str, Any]:
    """Package 固定的 host-native setup-margin proof 契約；由 native extension pin 進 plan。

    不含站點路徑或雜湊：binary 身分屬於 Profile 授權並進 Profile digest。
    """
    return {
        "descriptor_schema": DESCRIPTOR_SCHEMA,
        "execution_mode": EXECUTION_MODE,
        "method_identity": "aes.setup_slack_margin.v1",
        "proof_policy_id": PROOF_POLICY_ID,
        "proof_producer": "profile_pinned_in_flow_helper",
        "pairs": list(PAIR_LABELS),
        "envelope": {"method": PROOF_POLICY_ID,
                     "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
                     "tool_identity_schema": IDENTITY_SCHEMA},
        "acceptance_policy_id": ACCEPTANCE_POLICY_ID,
        "pair_statuses": list(PAIR_STATUSES),
        "tool_binding": {
            "source": "profile.execution.native_tool_binding.tools." + BINDING_TOOL_NAME,
            "identity": ["realpath", "sha256"],
            "flow_environment_variable": PROOF_TOOL_ENV,
            "verified": ["profile_selection", "before_dispatch", "in_process_before_exec",
                         "acceptance"],
            "version_string_is_identity": False,
        },
        "qualified_identity": {
            "profile_fields": ["repair_recipe.qualified_tool_identity_path",
                               "repair_recipe.qualified_tool_identity_sha256"],
            "file_sha256_required": True,
            "must_equal_tool_binding": True,
        },
        "trusted_inputs": {
            "profile": ["repair_recipe.helpers[].sha256",
                        "repair_recipe.config_suffix_sha256",
                        "repair_recipe.candidate_config_suffix_sha256",
                        "repair_recipe.qualified_tool_identity_sha256",
                        "input_manifest_sha256"],
            "proof_identity": ["executable_realpath", "executable_sha256", "argv", "mode",
                               *_PROOF_REQUIRED, "wrapper_sha256"],
            "rehashed_at_acceptance": [path for path, _ in _REHASHED_INPUTS],
        },
        "trust_model": {
            "container_equivalent": False,
            "network_isolation": "NOT_ENFORCED",
            "filesystem_isolation": "NOT_ENFORCED",
            "process_identity": "the operating system account that runs the daemon",
            "proof_environment": ("the native flow environment: cleared to an allowlist, "
                                  "then the Profile-pinned site env script"),
            "assumed_trusted": [
                "the site-administered Yosys installation pinned by realpath and SHA-256",
                "shared libraries loaded by that binary (not content-pinned)",
                "the Profile-pinned in-flow helper scripts that write the proof envelopes",
                "the operating system account and host kernel",
            ],
            "enforced": [
                "binary realpath and SHA-256 rechecked at Profile selection, before "
                "dispatch, before exec and at acceptance",
                "the proof identity must name exactly the authorized binary",
                "the miter generator must be one of the Profile-pinned helpers",
                "gold, gate and liberty re-hashed at acceptance",
                "a container identity (image ID) is never accepted as host-native",
            ],
            "allowed_write_paths": ["the task slot output root (ORFS WORK_HOME)"],
            "not_enforced": [
                "network access is not blocked",
                "writes outside the slot output root by the tool are not prevented",
                "Yosys plugins and environment variables are controlled by the pinned "
                "helpers and env script, not inspected separately",
            ],
        },
    }


def descriptor_sha256() -> str:
    text = _canonical(proof_descriptor())
    return hashlib.sha256(f"{DESCRIPTOR_SCHEMA}\n{text}".encode("utf-8")).hexdigest()


def verify_qualified_identity_file(path: str | Path, *, declared_sha256: str | None,
                                   pin: Any, check_host: bool = True) -> dict[str, Any]:
    """核對 qualified identity 檔：bytes、schema、digest 與授權 pin。

    native 契約要求 Profile 宣告檔案的 SHA-256；檔案必須指向 `FORMAL_YOSYS` pin
    的同一個 binary。`check_host` 時另外要求該 binary 此刻在主機上仍符合 pin
    （Profile 選用時）；驗收時由 `evaluate_native_setup_margin()` 自己記錄這項核對。
    """
    tool = _pin(pin)
    if not isinstance(declared_sha256, str) or not declared_sha256:
        raise NativeProofIdentityError(
            R_NATIVE_PROOF_IDENTITY,
            "a host-native setup-margin Profile must declare qualified_tool_identity_sha256")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise NativeProofIdentityError(
            R_NATIVE_PROOF_IDENTITY, f"the qualified tool identity is unreadable: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != declared_sha256:
        raise NativeProofIdentityError(
            R_NATIVE_PROOF_IDENTITY,
            f"the qualified tool identity hashes to {actual}, not the declared "
            f"{declared_sha256}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NativeProofIdentityError(
            R_NATIVE_PROOF_IDENTITY, f"the qualified tool identity is malformed: {exc}") from exc
    if not isinstance(payload, dict) or "digest" not in payload:
        raise NativeProofIdentityError(R_NATIVE_PROOF_IDENTITY,
                                       "the qualified tool identity has no digest")
    identity = NativeProofToolIdentity.from_json_dict(payload)
    missing = identity.missing_fields(proof=False)
    if missing:
        raise NativeProofIdentityError(
            R_NATIVE_PROOF_IDENTITY, f"the qualified tool identity is incomplete: {list(missing)}")
    if (identity.executable_realpath, identity.executable_sha256) != (tool.realpath, tool.sha256):
        raise NativeProofIdentityError(
            R_NATIVE_PROOF_IDENTITY,
            "the qualified tool identity does not name the FORMAL_YOSYS binary authorized "
            "by the Profile")
    if check_host:
        try:
            verify_pin(tool, where=BINDING_TOOL_NAME)
        except NativeToolIdentityError as exc:
            raise NativeProofIdentityError(
                R_NATIVE_PROOF_IDENTITY, f"{exc.code}: {exc.message}") from exc
    return {"path": str(path), "sha256": actual, "digest": identity.digest,
            "declared_sha256": declared_sha256, "schema": IDENTITY_SCHEMA,
            "execution_mode": EXECUTION_MODE, "tool": tool.to_json_dict()}


@dataclass(frozen=True)
class PairJudgement:
    """一組 pair proof 的 native 判定。"""

    label: str
    status: str
    reasons: tuple[str, ...] = ()
    reason_code: str | None = None
    identity_digest: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {"label": self.label, "status": self.status, "reasons": list(self.reasons),
                "reason_code": self.reason_code, "identity_digest": self.identity_digest}


INPUT_MATCHES = "MATCHES"


def _rehash_observation(path_value: Any, expected: Any) -> str:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        return "is not recorded as an absolute host path"
    path = Path(path_value)
    if not path.is_file():
        return f"{path_value} no longer exists"
    try:
        observed = sha256_file(path)
    except OSError as exc:
        return f"{path_value} is unreadable: {exc}"
    if observed != expected:
        return f"{path_value} now hashes to {observed}, not the recorded {expected}"
    return INPUT_MATCHES


def observe_pair_inputs(proof: Any) -> dict[str, str]:
    """判定當下在主機上重新雜湊 proof 輸入；回傳每個輸入的觀測（`MATCHES` 或問題）。"""
    if not isinstance(proof, dict):
        return {}
    return {path_name: _rehash_observation(proof.get(path_name), proof.get(hash_name))
            for path_name, hash_name in _REHASHED_INPUTS}


def observe_tool(tool: Any) -> dict[str, Any]:
    """判定當下授權的 proof binary 是否仍符合 pin。"""
    authorized = _pin(tool)
    try:
        verify_pin(authorized, where=BINDING_TOOL_NAME)
    except NativeToolIdentityError as exc:
        return {"status": "CHANGED", "code": exc.code, "message": exc.message,
                **authorized.to_json_dict()}
    return {"status": INPUT_MATCHES, **authorized.to_json_dict()}


def evaluate_pair(label: str, proof: Any, *, tool: Any, qualified_digest: str | None,
                  helper_sha256: frozenset[str] | set[str],
                  input_observations: Any) -> PairJudgement:
    """依 native 契約判定一組 pair proof；不呼叫工具、不讀主機檔案。

    `input_observations` 是判定時記錄的輸入重新雜湊結果（`observe_pair_inputs()`）。

    - 沒有 envelope ⇒ `NOT_EXECUTED`；
    - 方法、envelope 版本、工具身分、qualified 身分、miter 產生器或 proof 輸入任一
      不成立 ⇒ `UNVERIFIABLE`；
    - 身分成立且工具明確給出 `equivalent`（含所有 state 為自由輸入）⇒ `PROOF_PASS`；
      明確 `not_equivalent` ⇒ `PROOF_FAIL`；其他結論 ⇒ `UNVERIFIABLE`。
    """
    authorized = _pin(tool)
    if not isinstance(proof, dict) or not proof:
        return PairJudgement(label, NOT_EXECUTED, (f"the {label} pair proof is missing",),
                             R_NATIVE_PROOF_NOT_EXECUTED)

    def unverifiable(code: str, reason: str, digest: str | None = None) -> PairJudgement:
        return PairJudgement(label, UNVERIFIABLE, (reason,), code, digest)

    if proof.get("method") != PROOF_POLICY_ID:
        return unverifiable(R_NATIVE_PROOF_IDENTITY,
                            f"the {label} pair proof was produced by {proof.get('method')!r}, "
                            f"not {PROOF_POLICY_ID!r}")
    if proof.get("envelope_schema_version") != ENVELOPE_SCHEMA_VERSION:
        return unverifiable(R_NATIVE_PROOF_IDENTITY,
                            f"the {label} pair proof envelope schema is "
                            f"{proof.get('envelope_schema_version')!r}, not "
                            f"{ENVELOPE_SCHEMA_VERSION}")
    try:
        identity = NativeProofToolIdentity.from_json_dict(proof.get("proof_tool_identity"))
        qualified = NativeProofToolIdentity.from_json_dict(proof.get("qualified_tool_identity"))
    except NativeProofIdentityError as exc:
        return unverifiable(exc.code, f"the {label} pair proof: {exc.message}")
    missing = identity.missing_fields(proof=True)
    if missing:
        return unverifiable(R_NATIVE_PROOF_IDENTITY,
                            f"the {label} pair proof tool identity lacks {list(missing)}",
                            identity.digest)
    if (identity.executable_realpath, identity.executable_sha256) != (
            authorized.realpath, authorized.sha256):
        return unverifiable(R_NATIVE_PROOF_IDENTITY,
                            f"the {label} pair proof was produced by "
                            f"{identity.executable_realpath!r}, not the authorized "
                            f"{BINDING_TOOL_NAME} binary", identity.digest)
    if qualified_digest is None or qualified.digest != qualified_digest:
        return unverifiable(R_NATIVE_PROOF_IDENTITY,
                            f"the {label} pair proof does not carry the qualified identity "
                            "verified for this Profile", identity.digest)
    unpinned = [name for name in ("generator_sha256", "wrapper_sha256")
                if getattr(identity, name) is not None
                and getattr(identity, name) not in helper_sha256]
    if unpinned:
        return unverifiable(R_NATIVE_PROOF_INPUTS,
                            f"the {label} pair proof {', '.join(unpinned)} is not a "
                            "Profile-pinned helper", identity.digest)
    for envelope_name, identity_name in _ENVELOPE_HASHES:
        if proof.get(envelope_name) != getattr(identity, identity_name):
            return unverifiable(R_NATIVE_PROOF_INPUTS,
                                f"the {label} pair proof {envelope_name} differs from its "
                                "tool identity", identity.digest)
    observations = input_observations if isinstance(input_observations, dict) else {}
    for path_name, _hash_name in _REHASHED_INPUTS:
        observed = observations.get(path_name)
        if observed != INPUT_MATCHES:
            return unverifiable(R_NATIVE_PROOF_INPUTS,
                                f"the {label} pair proof {path_name} "
                                f"{observed or 'was not re-hashed at acceptance'}",
                                identity.digest)
    status = str(proof.get("status") or "")
    if status == "not_equivalent":
        return PairJudgement(label, PROOF_FAIL,
                             (f"the {label} pair proof found a counterexample",),
                             None, identity.digest)
    if status != "equivalent":
        return unverifiable(R_NATIVE_PROOF_IDENTITY,
                            f"the {label} pair proof status is {status or 'unknown'!r}",
                            identity.digest)
    try:
        bits = int(proof.get("state_bits") or 0)
    except (TypeError, ValueError):
        bits = 0
    free = [str(item) for item in (proof.get("miter_free_inputs") or [])]
    if bits <= 0 or not any(name.endswith("r5_S") for name in free):
        return unverifiable(R_NATIVE_PROOF_INPUTS,
                            f"the {label} pair proof does not cover all canonical states",
                            identity.digest)
    return PairJudgement(label, PROOF_PASS, (), None, identity.digest)


@dataclass(frozen=True)
class NativeSetupMarginResult:
    """native setup-margin 驗收：既有 RepairVerdict ＋ 每組 pair 的 native 判定。"""

    verdict: Any
    pairs: tuple[PairJudgement, ...]
    binding: dict[str, Any] = field(default_factory=dict)
    host_observations: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        payload = dict(self.verdict.to_json_dict())
        payload["native_proof"] = {
            "descriptor_schema": DESCRIPTOR_SCHEMA,
            "descriptor_sha256": descriptor_sha256(),
            "execution_mode": EXECUTION_MODE,
            "pairs": {pair.label: pair.to_json_dict() for pair in self.pairs},
            "binding": dict(self.binding),
            # 判定當下的主機觀測；歷史 replay 以它重算，不重新讀取主機現況。
            "host_observations": dict(self.host_observations),
            # 工具身分由 helper 在 run 內自行記錄；本契約只能證明它與授權 pin
            # 一致、且 binary 在判定時未被替換，不能證明實際執行的 bytes。
            "executed_identity": "HELPER_REPORTED_NOT_INDEPENDENTLY_VERIFIED",
        }
        return payload


def evaluate_native_setup_margin(
    *,
    tool: Any,
    qualified_digest: str | None,
    helper_sha256: frozenset[str] | set[str],
    baseline_post_cts_topology: Any,
    candidate_post_cts_topology: Any,
    post_cts_pair_proof: Any,
    final_pair_proof: Any,
    method_consumption: Any,
    host_method_consumption: Any,
    method_variable: str,
    method_candidate_value: str,
    host_observations: Any = None,
    target_slack_ns: float | None = None,
    actual_slack_ns: float | None = None,
    **kwargs: Any,
) -> NativeSetupMarginResult:
    """工程條件與 container 版本完全相同；proof 身分改用 native 契約。

    `host_observations` 為 `None` 時在主機上核對 binary 與重新雜湊 proof 輸入並記錄；
    歷史 replay 傳入當時記錄的觀測，不重讀主機現況。任何 pair 不是
    `PROOF_PASS`／`PROOF_FAIL`、方法沒被消費、或授權的 binary 在判定時已不符 ⇒
    `UNVERIFIABLE`。`PROOF_FAIL` 由既有工程條件判成 `NOT_REPAIRED`。
    """
    from terminux_engine.packages.orfs_run_intelligence import repair_acceptance as RA
    # 與 container 版本共用同一組工程條件（拓樸變化與方法消費），不另寫一份。
    from terminux_engine.packages.orfs_run_intelligence.setup_margin_acceptance import (
        R_METHOD_NOT_CONSUMED,
        _consumption_failures,
        _topology_change,
    )

    authorized = _pin(tool)
    change = _topology_change(baseline_post_cts_topology, candidate_post_cts_topology)
    verdict = RA.evaluate_repair(
        eco_diff=change or None, eco_proof=post_cts_pair_proof,
        final_pair_proof=final_pair_proof, target_slack_ns=target_slack_ns,
        actual_slack_ns=actual_slack_ns, **kwargs)
    proofs = {"baseline_candidate_post_cts": dict(post_cts_pair_proof or {}),
              "baseline_candidate_final": dict(final_pair_proof or {})}
    if host_observations is None:
        observations: dict[str, Any] = {
            "tool_check": observe_tool(authorized),
            "pairs": {label: observe_pair_inputs(proofs[label] or None)
                      for label in PAIR_LABELS}}
    else:
        observations = dict(host_observations) if isinstance(host_observations, dict) else {}
    pair_observations = observations.get("pairs")
    pair_observations = pair_observations if isinstance(pair_observations, dict) else {}
    pairs = tuple(evaluate_pair(label, proofs[label] or None, tool=authorized,
                                qualified_digest=qualified_digest,
                                helper_sha256=helper_sha256,
                                input_observations=pair_observations.get(label))
                  for label in PAIR_LABELS)

    extra_codes: list[str] = []
    extra_reasons: list[str] = []
    for pair in pairs:
        if pair.status in (UNVERIFIABLE, NOT_EXECUTED):
            extra_codes.append(pair.reason_code or R_NATIVE_PROOF_IDENTITY)
            extra_reasons.extend(pair.reasons)
    tool_check = observations.get("tool_check")
    if (not isinstance(tool_check, dict) or tool_check.get("status") != INPUT_MATCHES
            or (tool_check.get("realpath"), tool_check.get("sha256"))
            != (authorized.realpath, authorized.sha256)):
        extra_codes.append(R_NATIVE_PROOF_TOOL_CHANGED)
        detail = tool_check.get("message") if isinstance(tool_check, dict) else None
        extra_reasons.append("the authorized proof binary did not match its pin at "
                             f"acceptance: {detail or 'no matching observation'}")
    consumption_problems = _consumption_failures(
        method_consumption, host_method_consumption, variable=method_variable,
        expected_value=method_candidate_value)
    if consumption_problems:
        extra_codes.append(R_METHOD_NOT_CONSUMED)
        extra_reasons.extend(consumption_problems)

    binding = {"tool": authorized.to_json_dict(), "qualified_digest": qualified_digest,
               "helper_sha256": sorted(helper_sha256)}
    if not extra_codes:
        verdict = replace(verdict, policy_id=ACCEPTANCE_POLICY_ID,
                          policy_version=ACCEPTANCE_POLICY_VERSION, proofs=proofs,
                          eco_diff=change)
        return NativeSetupMarginResult(verdict, pairs, binding, observations)
    codes = tuple(dict.fromkeys(
        [code for code in verdict.reason_codes if code != "ok"] + extra_codes))
    reasons = tuple(
        [reason for reason in verdict.reasons
         if reason != "every fixed acceptance condition is satisfied"] + extra_reasons)
    verdict = replace(verdict, policy_id=ACCEPTANCE_POLICY_ID,
                      policy_version=ACCEPTANCE_POLICY_VERSION, repair_status=RA.UNVERIFIABLE,
                      reason_codes=codes, reasons=reasons, proofs=proofs, eco_diff=change)
    return NativeSetupMarginResult(verdict, pairs, binding, observations)


__all__ = [
    "ACCEPTANCE_POLICY_ID", "ACCEPTANCE_POLICY_VERSION", "BINDING_TOOL_NAME",
    "DESCRIPTOR_SCHEMA", "ENVELOPE_SCHEMA_VERSION", "EXECUTION_MODE", "IDENTITY_SCHEMA",
    "NOT_EXECUTED", "NativeProofIdentityError", "NativeProofToolIdentity",
    "NativeSetupMarginResult", "PAIR_LABELS", "PAIR_STATUSES", "PROOF_FAIL", "PROOF_PASS",
    "PROOF_POLICY_ID", "PROOF_TOOL_ENV", "PairJudgement", "R_NATIVE_PROOF_IDENTITY",
    "R_NATIVE_PROOF_INPUTS", "R_NATIVE_PROOF_NOT_EXECUTED", "R_NATIVE_PROOF_TOOL_CHANGED",
    "UNVERIFIABLE", "descriptor_sha256", "evaluate_native_setup_margin", "evaluate_pair",
    "observe_pair_inputs", "observe_tool", "proof_descriptor",
    "verify_qualified_identity_file",
]
