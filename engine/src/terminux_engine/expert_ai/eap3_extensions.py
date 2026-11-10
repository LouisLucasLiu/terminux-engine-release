# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Engineering Work 的 provider binding、Package 組裝與 Runtime bridge。"""

from __future__ import annotations

import json
import hashlib
import importlib
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.expert_ai.eap2 import build_flow_task_proposal
from terminux_engine.expert_ai.eap3 import (
    PackageCapability,
    WorkError,
    WorkPackageRegistry,
    canonical_json,
    digest_json,
)
from terminux_engine.orchestration.subprocess_executor import SubprocessExecutor


class ProviderBindingRouter:
    """依設定把 role／capability 路由到 provider，並正規化不同回覆外形。"""

    def __init__(self, bindings: list[dict[str, Any]], *, fixture_only: bool = False) -> None:
        self._bindings = [self._validate_binding(row, fixture_only=fixture_only)
                          for row in bindings]

    @classmethod
    def from_file(cls, path: str | Path, *, fixture_only: bool = False) -> "ProviderBindingRouter":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("bindings"), list):
            raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "bindings array is required")
        return cls(payload["bindings"], fixture_only=fixture_only)

    def call(
        self, *, role: str, capability: str, purpose: str,
        request: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        binding = next((row for row in self._bindings
                        if row["role"] == role and capability in row["capabilities"]), None)
        if binding is None:
            raise WorkError("EXPERT_BINDING_NOT_FOUND", f"{role}/{capability}")
        canonical_request = {
            "request_id": request_id, "role": role,
            "capability": capability, "purpose": purpose, "request": request,
        }
        if binding["adapter"] == "openai_chat":
            body_payload = {
                "model": binding["model"], "stream": False,
                "max_tokens": binding["max_output_tokens"],
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": (
                        "You are the Terminux " + role + " engineering role. "
                        "Return exactly one JSON object matching the requested purpose; "
                        "never invent grants, bindings, task ids, or execution authority. "
                        + self._response_contract(role, purpose, request))},
                    {"role": "user", "content": canonical_json(canonical_request)},
                ],
            }
        else:
            body_payload = {
                "model": binding["model"], **canonical_request,
                "max_output_tokens": binding["max_output_tokens"],
            }
        body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        credential_ref = binding.get("credentials_env")
        if credential_ref:
            # 與 daemon 既有 provider 共用安全 credential file；只有該模組
            # 的 allowlist 名稱可從 0600 檔案載入，不把 secret 複製進環境。
            from terminux_engine.llm.credential_file import secret
            token = secret(str(credential_ref))
            if not token:
                raise WorkError("PROVIDER_CREDENTIAL_MISSING", credential_ref)
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(binding["endpoint"], data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=binding["timeout_sec"]) as response:
                raw = response.read(binding["max_response_bytes"] + 1)
        except urllib.error.HTTPError as exc:
            raise WorkError("PROVIDER_HTTP_ERROR", f"status={exc.code}") from exc
        except TimeoutError as exc:
            raise WorkError("PROVIDER_TIMEOUT", type(exc).__name__) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise WorkError("PROVIDER_TIMEOUT", type(exc.reason).__name__) from exc
            raise WorkError("PROVIDER_TRANSPORT_ERROR", type(exc.reason).__name__) from exc
        if len(raw) > binding["max_response_bytes"]:
            raise WorkError("PROVIDER_OUTPUT_TRUNCATED", "response exceeds configured byte cap")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            error = WorkError("PROVIDER_RESPONSE_JSON_INVALID", type(exc).__name__)
            error.provider_metadata = self._provider_metadata(binding, raw, None)
            error.response_diagnostics = {"wire_format": "invalid_json"}
            raise error from exc
        provider_metadata = self._provider_metadata(binding, raw, payload)
        try:
            if binding["adapter"] == "canonical":
                result = payload["response"]
            elif binding["adapter"] == "nested_text":
                nested = payload["data"]["message"]
                try:
                    result = json.loads(nested)
                except (TypeError, ValueError) as exc:
                    error = WorkError(
                        "PROVIDER_RESPONSE_JSON_INVALID", type(exc).__name__)
                    error.provider_metadata = provider_metadata
                    error.response_diagnostics = self._content_diagnostics(nested)
                    raise error from exc
            elif binding["adapter"] == "openai_chat":
                choice = payload["choices"][0]
                finish_reason = choice.get("finish_reason")
                if finish_reason in {"length", "max_tokens"}:
                    error = WorkError(
                        "PROVIDER_OUTPUT_TRUNCATED",
                        f"provider finish_reason={finish_reason}")
                    error.provider_metadata = provider_metadata
                    raise error
                content = choice["message"]["content"]
                try:
                    result = json.loads(content)
                except (TypeError, ValueError) as exc:
                    error = WorkError(
                        "PROVIDER_RESPONSE_JSON_INVALID", type(exc).__name__)
                    error.provider_metadata = provider_metadata
                    error.response_diagnostics = self._content_diagnostics(content)
                    raise error from exc
            else:  # pragma: no cover - 設定驗證已阻擋
                raise KeyError("adapter")
        except (KeyError, TypeError, IndexError) as exc:
            error = WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED", type(exc).__name__)
            error.provider_metadata = provider_metadata
            error.response_diagnostics = {
                "provider_payload_type": type(payload).__name__,
                "provider_payload_fields": (
                    sorted(str(key) for key in payload)
                    if isinstance(payload, dict) else []),
            }
            raise error from exc
        if not isinstance(result, dict):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "normalized response must be an object")
        try:
            self._validate_response(role, purpose, result)
        except WorkError as exc:
            # 未通過的 normalized response 不能成為合法結果，但上層需要其
            # digest／欄位與 finish_reason，才能留下具體診斷並有界修正。
            exc.rejected_response = result
            exc.provider_metadata = provider_metadata
            raise
        return {**result, "_terminux_provider_call": provider_metadata}

    @staticmethod
    def _provider_metadata(
        binding: dict[str, Any], raw: bytes, payload: Any,
    ) -> dict[str, Any]:
        finish_reason = payload.get("finish_reason") \
            if isinstance(payload, dict) else None
        if binding["adapter"] == "openai_chat" and isinstance(payload, dict):
            choices = payload.get("choices")
            if (isinstance(choices, list) and choices
                    and isinstance(choices[0], dict)):
                finish_reason = choices[0].get("finish_reason")
        endpoint = urllib.parse.urlparse(binding["endpoint"])
        return {
            "binding_id": binding["binding_id"], "model": binding["model"],
            "adapter": binding["adapter"],
            "endpoint_origin": f"{endpoint.scheme}://{endpoint.netloc}",
            "response_bytes": len(raw), "finish_reason": finish_reason,
        }

    @staticmethod
    def _content_diagnostics(content: Any) -> dict[str, Any]:
        return {
            "normalized_content_type": type(content).__name__,
            "normalized_content_chars": len(content)
            if isinstance(content, (str, bytes, list, dict)) else None,
            "normalized_content_digest": digest_json(content),
        }

    @staticmethod
    def _response_contract(
        role: str, purpose: str, request: dict[str, Any],
    ) -> str:
        """把 logical purpose 轉成可機器驗證的回覆合約。

        這只規定 response shape；候選值仍由 Domain 提出，可用邊界
        來自 parent authorization，實際合法性仍由 Package validator 決定。
        """
        if role == "global" and purpose == "plan":
            return (
                'Return exactly one JSON object with a top-level kind. '
                'For this ready engineering work return {"kind":"DELEGATE",'
                '"question":"<specific engineering question>"}. When '
                'available_experts is supplied, you may add expert_requests with '
                'call_key, expert_id, purpose, question, and depends_on, using only '
                'the listed compatible purposes. Do not submit or execute a proposal '
                'that has not yet been produced by an Expert.')
        if role == "global" and purpose == "domain_response":
            return (
                'Return exactly one JSON object with a top-level kind. '
                'Return one of SUBMIT_PROPOSAL, REQUEST_REVISION, COLLECT_EVIDENCE, '
                'WAIT_USER, or STOP in the top-level kind, with a non-empty message. '
                'Use SUBMIT_PROPOSAL when the validated Domain proposal should become '
                'a reviewable execution draft.')
        if role == "global" and purpose == "result_decision":
            return (
                'Return exactly one JSON object with a top-level kind of STOP, '
                'REQUEST_REVISION, COLLECT_EVIDENCE, '
                'or WAIT_USER, with message and reason_code. If the single authorized '
                'attempt has a trustworthy result, STOP and report criteria_met as a boolean. '
                'The supplied history.candidate_disposition lists every attempt in this '
                'Work: each entry has attempt_id and state (QUALIFIED, NOT_QUALIFIED, '
                'EVIDENCE_INSUFFICIENT, or IN_PROGRESS). retained_attempt_id is only the '
                'first attempt that qualified, not a claim that it is the best one; '
                'additional_qualified_attempt_ids lists other qualified attempts without '
                'ranking them; no_eligible_candidate is true only when every closed '
                'attempt failed to qualify. To adopt a specific earlier attempt as the '
                'formal result instead of the most recent one, set kind STOP, boolean '
                'criteria_met true, and string selected_attempt_id equal to that '
                'attempt_id (only valid together with kind STOP); omit selected_attempt_id '
                'to let the runtime evaluate the most recent attempt as before. Selecting '
                'an attempt does not by itself mean criteria are met: the runtime '
                'independently verifies that attempt\'s own persisted engineering '
                'evidence and will reject a selection that does not exist in this Work, '
                'is still executing, or does not qualify.')
        if role == "global" and purpose == "expert_integration":
            proposal = request.get("latest_proposal") or {}
            reviews = [row for row in request.get("expert_responses") or []
                       if row.get("purpose") == "proposal_review"]
            return (
                'Integrate the supplied versioned Expert responses. Return one JSON '
                'object with top-level kind and non-empty message; kind must be one of '
                'SUBMIT_PROPOSAL, REQUEST_REVISION, COLLECT_EVIDENCE, WAIT_USER, STOP. '
                'REQUEST_REVISION also requires string target_expert_id. '
                'The authoritative latest proposal digest is '
                + json.dumps(proposal.get("proposal_digest"))
                + '; proposal_review response digests are '
                + json.dumps([row.get("response_digest") for row in reviews])
                + '. Base the decision on those supplied records. Runtime, not your '
                'response, binds the exact proposal and Expert response digests. '
                'Do not return or alter authority, scope, identity or digest fields. '
                'Never merge conflicting changes or invent evidence.')
        if role == "global" and purpose == "multi_expert_result_decision":
            return (
                'Return exactly one JSON object with top-level kind STOP, '
                'REQUEST_REVISION, COLLECT_EVIDENCE, or WAIT_USER, a non-empty '
                'message, reason_code, and boolean criteria_met. Base the decision '
                'on the supplied objective, criteria, stop conditions, remaining '
                'budget, outcome, and Expert analyses. authorization_context is '
                'authoritative for whether the current authorization is active, the '
                'remaining capacity per purpose and the allowed change bounds. If '
                'criteria are unmet, explain whether the evidence and remaining '
                'authorization support a concrete revision; unmet criteria alone do '
                'not determine the next action. '
                'Preserve each Expert '
                'caveat and do not treat review as proof or overwrite Package '
                'acceptance. Runtime binds the exact outcome and Expert response '
                'digests; do not return or alter authority, scope, identity or '
                'digest fields.')
        if role == "domain" and purpose in {"proposal", "proposal_revision"}:
            payload = request.get("payload") or {}
            package = request.get("package") or payload.get("package") or {}
            contract = package.get("proposal_contract")
            if isinstance(contract, dict):
                return (
                    'Return {"kind":"PROPOSAL","message":"...","proposal":{...}}. '
                    'The proposal must follow this Package-owned contract exactly: '
                    + canonical_json(contract) + '. Candidate values remain your engineering '
                    'judgment inside the supplied authorization boundary; never invent one.')
            raise WorkError(
                "PROVIDER_REQUEST_CONTRACT_MISSING",
                "Package proposal_contract is required for Domain proposal requests")
        if role == "domain" and purpose in {"proposal_review", "evidence_review"}:
            payload = request.get("payload") or {}
            reviewed = payload.get("review_of_proposal_digest")
            return (
                'Review only the supplied proposal and evidence. Return either '
                '{"kind":"REVIEW","message":"...",'
                '"review_of_proposal_digest":' + json.dumps(reviewed) + ','
                '"disposition":"ACCEPT|REQUEST_REVISION|NEED_EVIDENCE|STOP"}, '
                'or NEED_EVIDENCE/UNSUPPORTED with a non-empty message. Preserve the '
                'exact review_of_proposal_digest; do not invent proof or authority.')
        if role == "domain" and purpose == "execution_analysis":
            return (
                'Return {"kind":"EXECUTION_ANALYSIS","message":"...",'
                '"criteria_met":<boolean>,"recommendation":"stop or revise"}. '
                'Base the statement only on the supplied verified attempt evidence; '
                'state uncertainty instead of inventing measurements.')
        if role == "domain" and purpose == "result_review":
            return (
                'Return {"kind":"RESULT_REVIEW","message":"...",'
                '"supported":<boolean>}. Review only the supplied outcome and prior '
                'analyses; do not replace Package acceptance or invent evidence.')
        return "Return a top-level kind and fields appropriate to the supplied purpose."

    @staticmethod
    def _validate_response(role: str, purpose: str, result: dict[str, Any]) -> None:
        """驗證 role response 外層合約；Package 仍負責 proposal 工程格式。"""
        allowed: dict[tuple[str, str], frozenset[str]] = {
            ("global", "plan"): frozenset({"DELEGATE", "WAIT_USER", "STOP"}),
            ("global", "domain_response"): frozenset({
                "SUBMIT_PROPOSAL", "REQUEST_REVISION", "COLLECT_EVIDENCE",
                "WAIT_USER", "STOP"}),
            ("global", "result_decision"): frozenset({
                "STOP", "REQUEST_REVISION", "COLLECT_EVIDENCE", "WAIT_USER"}),
            ("global", "expert_integration"): frozenset({
                "SUBMIT_PROPOSAL", "REQUEST_REVISION", "COLLECT_EVIDENCE",
                "WAIT_USER", "STOP"}),
            ("global", "multi_expert_result_decision"): frozenset({
                "STOP", "REQUEST_REVISION", "COLLECT_EVIDENCE", "WAIT_USER"}),
            ("domain", "proposal"): frozenset({
                "PROPOSAL", "NEED_EVIDENCE", "NO_PROPOSAL", "UNSUPPORTED"}),
            ("domain", "proposal_revision"): frozenset({
                "PROPOSAL", "NEED_EVIDENCE", "NO_PROPOSAL", "UNSUPPORTED"}),
            ("domain", "execution_analysis"): frozenset({"EXECUTION_ANALYSIS"}),
            ("domain", "proposal_review"): frozenset({
                "REVIEW", "NEED_EVIDENCE", "UNSUPPORTED"}),
            ("domain", "evidence_review"): frozenset({
                "REVIEW", "NEED_EVIDENCE", "UNSUPPORTED"}),
            ("domain", "result_review"): frozenset({"RESULT_REVIEW"}),
        }
        expected = allowed.get((role, purpose))
        kind = result.get("kind")
        if expected is not None and kind not in expected:
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                f"kind must be one of {sorted(expected)}")
        if kind == "DELEGATE" and not isinstance(result.get("question"), str):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED", "DELEGATE requires question")
        if kind == "PROPOSAL" and not isinstance(result.get("proposal"), dict):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED", "PROPOSAL requires proposal object")
        if kind == "EXECUTION_ANALYSIS" and not isinstance(
                result.get("criteria_met"), bool):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "EXECUTION_ANALYSIS requires boolean criteria_met")
        if kind == "REVIEW" and (
                result.get("disposition") not in {
                    "ACCEPT", "REQUEST_REVISION", "NEED_EVIDENCE", "STOP"}
                or not isinstance(result.get("review_of_proposal_digest"), str)):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "REVIEW requires target digest and valid disposition")
        if kind == "RESULT_REVIEW" and not isinstance(
                result.get("supported"), bool):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "RESULT_REVIEW requires boolean supported")
        if purpose == "expert_integration" and kind == "REQUEST_REVISION" \
                and not isinstance(result.get("target_expert_id"), str):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "REQUEST_REVISION requires target_expert_id")
        if role == "global" and purpose in {
                "plan", "domain_response", "result_decision",
                "expert_integration", "multi_expert_result_decision"} and (
                not isinstance(result.get("message"), str)
                or not result["message"].strip()) and not (
                    purpose == "plan" and kind == "DELEGATE"):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                f"{purpose} requires non-empty message")
        if purpose == "multi_expert_result_decision" and not isinstance(
                result.get("criteria_met"), bool):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "multi-Expert result decision requires boolean criteria_met")
        if role == "global" and purpose in {
                "result_decision", "multi_expert_result_decision"} and (
                not isinstance(result.get("reason_code"), str)
                or not result["reason_code"].strip()):
            raise WorkError(
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                f"{purpose} requires non-empty reason_code")
        if "selected_attempt_id" in result:
            # `selected_attempt_id` 只在 `global`/`result_decision` 的 STOP
            # 上有意義（host 端只在那個分支讀它）；其他 kind／purpose 帶著
            # 它出現是不允許的決策組合，一律在這裡明確拒絕，不留給下游
            # 靜默忽略或誤用。出現時型別也必須是非空字串——host 端的
            # `_resolve_selected_attempt()` 仍會核對它是否真的指向本 Work
            # 一個已關閉的 attempt，這裡只做格式層的守門。
            if not (role == "global" and purpose == "result_decision" and kind == "STOP"):
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED",
                    "selected_attempt_id is only valid on a global "
                    "result_decision STOP")
            selected = result["selected_attempt_id"]
            if not isinstance(selected, str) or not selected.strip():
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED",
                    "selected_attempt_id must be a non-empty string when provided")

    @staticmethod
    def _validate_binding(row: Any, *, fixture_only: bool) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "binding must be an object")
        required = {"binding_id", "role", "capabilities", "endpoint", "model",
                    "timeout_sec", "max_output_tokens", "adapter"}
        if not required.issubset(row):
            raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "binding fields are incomplete")
        if row["role"] not in {"global", "domain"} or row["adapter"] not in {
                "canonical", "nested_text", "openai_chat"}:
            raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "unknown role or adapter")
        parsed = urllib.parse.urlparse(str(row["endpoint"]))
        if parsed.scheme not in {"http", "https"}:
            raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "endpoint must be HTTP(S)")
        if fixture_only and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise WorkError("FIXTURE_NETWORK_SCOPE_INVALID", "fixture endpoint must be loopback")
        normalized = dict(row)
        normalized["timeout_sec"] = float(row["timeout_sec"])
        normalized["max_output_tokens"] = int(row["max_output_tokens"])
        normalized["max_response_bytes"] = int(row.get("max_response_bytes", 262144))
        normalized["capabilities"] = tuple(str(item) for item in row["capabilities"])
        return normalized


_DEFAULT_PACKAGE_EXTENSIONS = (
    "terminux_engine.packages.orfs_run_intelligence.eap_extension:capability",
)


def build_package_registry(extension_specs: tuple[str, ...] | None = None) -> WorkPackageRegistry:
    """依 ``module:factory`` 掛載 Package；不修改 work service／Core／Kernel。"""
    if extension_specs is None:
        configured = os.environ.get("TERMINUX_EAP3_PACKAGE_EXTENSIONS", "").strip()
        extension_specs = (tuple(item.strip() for item in configured.split(",") if item.strip())
                           if configured else _DEFAULT_PACKAGE_EXTENSIONS)
    packages: list[PackageCapability] = []
    for spec in extension_specs:
        module_name, separator, factory_name = spec.partition(":")
        if not separator or not module_name or not factory_name:
            raise WorkError("PACKAGE_EXTENSION_SPEC_INVALID", spec)
        factory = getattr(importlib.import_module(module_name), factory_name, None)
        if not callable(factory):
            raise WorkError("PACKAGE_EXTENSION_FACTORY_INVALID", spec)
        package = factory()
        if not isinstance(package, PackageCapability):
            raise WorkError("PACKAGE_EXTENSION_RESULT_INVALID", spec)
        packages.append(package)
    return WorkPackageRegistry(tuple(packages))


def durable_input_verdict(
*, work: dict[str, Any], reference: dict[str, Any],
) -> dict[str, Any] | None:
    """判定被重用的 baseline 與本次 Work 是否為**同一份工程輸入**。

    `input_digest` 是每個 Work 各自 `prepare_input` 產生的 snapshot 身分：
    來源 Work 與重用它的 Work 在構造上不會相同。直接要求相等，等於讓任何
    跨 Work 的 durable 重用都不成立，而失敗又會落到 baseline 重新收集。

    分兩層，兩層都不略過 digest：

    1. `input_digest` 相同 ⇒ 同一次準備，直接成立。
    2. 不同 ⇒ 改用**已釘住的工程輸入身分**判等：input manifest sha256 必須
       相同；兩側都有 tree hash 時也必須相同。任一側缺少可比對的欄位一律
       拒絕（fail closed），不把「缺欄位」當成相容。

    兩側各自的 digest 都保留在拒絕理由裡；不把來源 digest 寫進本次 Work。
    RTL／SDC／design config／tool identity 由 caller 既有的 `expected` 比對
    負責，參數差異由 durable profile 的 parameter identity 負責。
    """
    target_input = work.get("input") or {}
    source_digest = reference.get("input_digest")
    target_digest = target_input.get("input_digest")
    if source_digest is not None and source_digest == target_digest:
        return None

    imported = reference.get("execution_import")
    if not isinstance(imported, dict):
        return {"status": "REJECTED", "reason_code": "BASELINE_IMPORT_INVALID"}

    detail = {"source_input_digest": source_digest,
              "target_input_digest": target_digest}
    source_manifest = imported.get("input_manifest_sha256")
    target_manifest = (target_input.get("manifest_digest")
                       or target_input.get("input_manifest_digest"))
    if not (isinstance(source_manifest, str) and source_manifest
            and isinstance(target_manifest, str) and target_manifest):
        return {"status": "REJECTED",
                "reason_code": "BASELINE_INPUT_MANIFEST_UNAVAILABLE", **detail}
    detail["source_input_manifest_sha256"] = source_manifest
    detail["target_input_manifest_sha256"] = target_manifest
    if source_manifest != target_manifest:
        return {"status": "REJECTED", "reason_code": "BASELINE_INPUT_MISMATCH",
                **detail}

    source_tree = imported.get("input_tree_hash")
    target_tree = target_input.get("tree_hash")
    if (isinstance(source_tree, str) and source_tree
            and isinstance(target_tree, str) and target_tree):
        detail["source_input_tree_hash"] = source_tree
        detail["target_input_tree_hash"] = target_tree
        if source_tree != target_tree:
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_INPUT_TREE_MISMATCH", **detail}
    return None


#: durable 重用時要逐項比對的工程內容角色。來源取自 durable production
#: reference，目標從 Profile 釘住的 input snapshot 以**相同相對路徑**重建。
_ENGINEERING_CONTENT_ROLES: tuple[str, ...] = ("design_config", "rtl", "sdc")


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def durable_engineering_verdict(
    *, reference: dict[str, Any], target_snapshot_root: str | None,
    target_tool_identity: str | None,
) -> dict[str, Any] | None:
    """比對 durable baseline 與本次 Work 的**工程內容**，而不是 Profile digest。

    `config_digest`／`constraints_digest` 在兩側都只是 `profile_digest` 的
    佔位值（見 `service.py` 的 environment 組裝），拿它們判等既沒有工程意義，
    對 durable 重用也永遠不可能相等——target Profile 必須帶 durable pin，
    digest 在構造上就不同。歷史 reference 的原值一律保留，這裡不改寫它，
    也不像 plan_only 那樣把檢查 pop 掉。

    改為：來源身分（source profile digest 是否符合 pin）由
    `durable_baseline` 負責；這裡只回答「工程內容是不是同一份」——
    design config、RTL、SDC 逐檔比對內容 sha256，scripts 比對 revision
    digest，tool identity 比對 image。比對以**相對路徑**進行，純部署路徑差異
    不算漂移；任何會影響工程結果的差異都保留。

    資料不足一律明確拒絕，不補值、不猜測。
    """
    imported = reference.get("execution_import")
    if not isinstance(imported, dict):
        return {"status": "REJECTED", "reason_code": "BASELINE_IMPORT_INVALID"}
    verification = imported.get("verification_reference")
    record = (verification or {}).get("production_reference") \
        if isinstance(verification, dict) else None
    source_input = (record or {}).get("input") if isinstance(record, dict) else None
    if not isinstance(source_input, dict):
        return {"status": "REJECTED",
                "reason_code": "BASELINE_ENGINEERING_RECORD_UNAVAILABLE",
                "detail": {"field": "production_reference.input",
                           "provenance": "durable baseline reference"}}
    source_root = source_input.get("snapshot_root")
    if not isinstance(source_root, str) or not source_root:
        return {"status": "REJECTED",
                "reason_code": "BASELINE_ENGINEERING_RECORD_UNAVAILABLE",
                "detail": {"field": "production_reference.input.snapshot_root",
                           "provenance": "durable baseline reference"}}
    if not isinstance(target_snapshot_root, str) or not target_snapshot_root:
        return {"status": "REJECTED",
                "reason_code": "BASELINE_TARGET_INPUT_ROOT_UNAVAILABLE",
                "detail": {"field": "profile.input_snapshot_root",
                           "provenance": "target Profile"}}

    source_base = Path(source_root)
    target_base = Path(target_snapshot_root)
    for role in _ENGINEERING_CONTENT_ROLES:
        entry = source_input.get(role)
        if not isinstance(entry, dict):
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_ENGINEERING_RECORD_UNAVAILABLE",
                    "detail": {"field": f"production_reference.input.{role}",
                               "provenance": "durable baseline reference"}}
        source_sha = entry.get("sha256")
        source_path = entry.get("path")
        if not isinstance(source_sha, str) or not source_sha \
                or not isinstance(source_path, str) or not source_path:
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_ENGINEERING_RECORD_UNAVAILABLE",
                    "detail": {"field": f"production_reference.input.{role}",
                               "provenance": "durable baseline reference"}}
        try:
            relative = Path(source_path).relative_to(source_base)
        except ValueError:
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_ENGINEERING_PATH_OUTSIDE_SNAPSHOT",
                    "detail": {"field": f"{role}.path", "source": source_path,
                               "source_snapshot_root": source_root,
                               "provenance": "durable baseline reference"}}
        candidate = target_base / relative
        if not candidate.is_file():
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_TARGET_ENGINEERING_INPUT_MISSING",
                    "detail": {"field": role, "relative_path": str(relative),
                               "target": str(candidate),
                               "provenance": "target Profile input snapshot"}}
        target_sha = _sha256_file(candidate)
        if target_sha != source_sha:
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_" + role.upper() + "_CONTENT_MISMATCH",
                    "detail": {"field": role, "relative_path": str(relative),
                               "source": source_sha, "target": target_sha,
                               "source_provenance": "durable production reference",
                               "target_provenance": "target Profile input snapshot"}}

    image_block = (record or {}).get("image") or {}
    image = image_block.get("docker_image_id")
    native_binding = image_block.get("native_tool_binding_sha256")
    if isinstance(native_binding, str) and native_binding:
        # host-native reference：工具身分是 Profile 授權的 binding digest，
        # 與 target Profile 的 `tool_identity_ref` 用同一種表示比對。
        image = f"native-binding:sha256:{native_binding}"
    if not isinstance(image, str) or not image:
        return {"status": "REJECTED",
                "reason_code": "BASELINE_ENGINEERING_RECORD_UNAVAILABLE",
                "detail": {"field": "production_reference.image.docker_image_id",
                           "provenance": "durable baseline reference"}}
    if not isinstance(target_tool_identity, str) or not target_tool_identity:
        return {"status": "REJECTED",
                "reason_code": "BASELINE_TARGET_TOOL_IDENTITY_UNAVAILABLE",
                "detail": {"field": "environment.tool_identity",
                           "provenance": "target Work environment"}}
    if image != target_tool_identity:
        return {"status": "REJECTED", "reason_code": "BASELINE_TOOL_IDENTITY_MISMATCH",
                "detail": {"field": "tool_identity", "source": image,
                           "target": target_tool_identity,
                           "source_provenance": "durable production reference",
                           "target_provenance": "target Work environment"}}
    return None


ENGINEERING_RESULT_SCHEMA = "terminux.work_engineering_result.v1"
#: Package 產出的判定欄位。Work 層**只複製，不解讀**：它不認識 formal 語意，
#: 也不替 Package 決定什麼算成功。這些名稱在 FlowTask view 裡是既有欄位。
_VERDICT_FACTS = ("selection_eligible", "comparable", "proof_status",
                  "engineering_verdict", "protected_metric_regressions",
                  "qualification_only", "evidence_origin",
                  "target_slack_ns", "actual_slack_ns", "target_slack_met",
                  "timing_repaired",
                  # CORE_UTILIZATION 專有：面積判定摘要與逐指標 timing 判定
                  # 分開保留（`setup_timing` 巢狀在 `core_utilization_
                  # acceptance` 底下，`_lifted_fact` 的兩層查找可以找到）；
                  # 「完整驗收結果」與「timing 是否改善」不合併成同一個事實。
                  # 其他方法沒有這兩個欄位時 `_lifted_fact` 回 `None`。
                  "core_area", "setup_timing")


def _lifted_fact(payload: Any, key: str) -> Any:
    """在 verdict payload 的頂層或下一層找出一個已知欄位；找不到回 `None`。

    Package 的 acceptance 結果有時直接放在 `repair`，有時放在它底下的一個
    method-specific 區塊（例如 `core_utilization_acceptance`）。這裡只做**兩層**
    查找，不做遞迴搜尋——超過兩層就不是既定形狀，寧可回 `None` 讓上層看見缺口。
    """
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload[key]
    for value in payload.values():
        if isinstance(value, dict) and key in value:
            return value[key]
    return None


def engineering_result(task_view: dict[str, Any]) -> dict[str, Any]:
    """由已終結的 FlowTask view 取出**有界**的工程結果摘要。

    這是 Work 層唯一該讀的工程事實來源：Expert／Global 的結果回饋、修訂請求的
    續作 guard 與交接報告都用它，不必各自去翻整份 view，也不必相信 provider
    自己宣稱的數字。

    只複製 Package 已經算好的判定欄位，不重新判定、不補值、不翻譯語意。欄位
    缺席就是 `None`——「查無紀錄」由讀的人處理，不在這裡猜成通過或失敗。
    """
    if not isinstance(task_view, dict):
        return {"schema_version": ENGINEERING_RESULT_SCHEMA, "available": False,
                "reason": "task view is unavailable"}
    repair = task_view.get("repair") if isinstance(task_view.get("repair"), dict) else {}
    comparison = (task_view.get("comparison")
                  if isinstance(task_view.get("comparison"), dict) else {})
    steps = {str(row.get("step_id")): row.get("status")
             for row in (task_view.get("steps") or [])
             if isinstance(row, dict) and row.get("step_id")}
    summary: dict[str, Any] = {
        "schema_version": ENGINEERING_RESULT_SCHEMA,
        "available": bool(repair),
        "task_id": task_view.get("task_id"),
        "task_digest": task_view.get("task_digest"),
        "terminal_status": task_view.get("status"),
        "terminal_reason": task_view.get("terminal_reason"),
        "steps": steps,
        "repair_status": repair.get("repair_status"),
        "engineering_resolution": repair.get("engineering_resolution"),
        "comparison_status": repair.get("comparison_status") or comparison.get("status"),
        "claim_scope": repair.get("claim_scope"),
        # 限制與拒絕理由原樣保留：它們是 Expert 判斷「還能不能續作」的依據。
        "reason_codes": list(repair.get("reason_codes") or ()),
        "limitations": list(repair.get("reasons") or ()),
        "production_readiness": (repair.get("production_readiness")
                                 if isinstance(repair.get("production_readiness"), dict)
                                 else None),
    }
    for name in _VERDICT_FACTS:
        summary[name] = _lifted_fact(repair, name)
    # proof 分類是獨立一塊：`UNPROVEN` 與 `NOT_EQUIVALENT` 都是合法結果，
    # 兩者都不得被讀成「沒有證據」。
    summary["proof"] = {"proof_status": summary.get("proof_status"),
                        "required_proof_kinds": list(
                            ((summary.get("production_readiness") or {}).get(
                                "required_proof_kinds")) or ())}
    return summary


class FlowTaskWorkRuntime:
    """把 Engineering Work attempt 轉入既有 FlowTask service。"""

    def __init__(self, flow_service: Any) -> None:
        self._flow = flow_service

    def supports_plan(self, execution_plan: dict[str, Any]) -> bool:
        """正式 Runtime 僅接已編譯成 FlowTask proposal 的 Package 計畫。"""
        if not (execution_plan.get("runtime") == "flow_task"
                and isinstance(execution_plan.get("flow_task_proposal"), dict)):
            return False
        # authority 只有 flow_task_proposal.method_extension；頂層若保留，必須
        # 是逐位元相同的副本。Runtime 只核對一致性，不解讀或補造 descriptor。
        flow_pin = execution_plan["flow_task_proposal"].get("method_extension")
        mirror = execution_plan.get("method_extension")
        if flow_pin is None:  # 歷史 setup-margin plan 保持原格式與可讀性。
            return mirror is None
        try:
            from terminux_engine.packages.orfs_run_intelligence.method_extensions import resolve_pinned
            resolve_pinned(flow_pin)
            if mirror is not None:
                resolve_pinned(mirror)
        except ValueError:
            return False
        return mirror is None or digest_json(mirror) == digest_json(flow_pin)

    def prepare_input(self, *, work: dict[str, Any], request_id: str) -> dict[str, Any]:
        environment = work.get("environment") or {}
        if (work.get("package") or {}).get("execution_kind") == "local_tool":
            return self._prepare_local_tool_input(
                work=work, environment=environment, request_id=request_id)
        requested_source = work.get("requested_source_root")
        if isinstance(requested_source, str):
            prepared = self._flow.prepare_eap_work_input(
                profile_id=work["profile_id"], work_id=work["work_id"],
                source_root=requested_source)
            if prepared.get("status") != "ok":
                return {"status": "FAILED", "recoverable": True,
                        "reason_code": prepared.get(
                            "error_code", "INPUT_PREPARATION_FAILED"),
                        "detail": prepared, "request_id": request_id}
            return {"status": "READY", "request_id": request_id,
                    "profile_id": prepared["profile_id"],
                    "input_digest": prepared["input_digest"],
                    "manifest_digest": prepared["manifest_digest"],
                    "manifest_path": prepared["manifest_path"],
                    "snapshot_root": prepared["snapshot_root"],
                    "tree_hash": prepared.get("tree_hash"),
                    "copy_method": prepared.get("copy_method"),
                    "environment_patch": {
                        "input_snapshot_root": prepared["snapshot_root"],
                        "input_manifest_digest": prepared["manifest_digest"]}}
        reference = (work.get("baseline") or {}).get("reference")
        authorization = work.get("authorization") or {}
        if authorization.get("mode") == "PLAN_ONLY" and isinstance(reference, dict):
            # 純提案只讀已由 Profile 選取流程驗證的固定 input；不為了分析
            # 再複製 3+ GiB source，也不產生任何 execution import 權限。
            input_digest = environment.get("input_manifest_digest")
            snapshot_root = environment.get("input_snapshot_root")
            manifest_path = environment.get("input_manifest_path")
            imported = reference.get("execution_import") or {}
            try:
                from terminux_engine.packages.orfs_run_intelligence.flow_inputs import (
                    InputPreparationError, load_manifest, verify_inventory,
                )
                manifest = load_manifest(
                    str(manifest_path), expected_sha256=str(input_digest))
                inventory = verify_inventory(manifest, str(snapshot_root))
            except (InputPreparationError, OSError, ValueError) as exc:
                return {"status": "FAILED", "recoverable": True,
                        "reason_code": "PLAN_ONLY_INPUT_REFERENCE_INVALID",
                        "error_type": type(exc).__name__, "request_id": request_id}
            if (not isinstance(input_digest, str)
                    or not isinstance(snapshot_root, str)
                    or not isinstance(manifest_path, str)
                    or not inventory.ok
                    or manifest.tree_hash != imported.get("input_tree_hash")):
                return {"status": "FAILED", "recoverable": True,
                        "reason_code": "PLAN_ONLY_INPUT_REFERENCE_MISMATCH",
                        "request_id": request_id}
            return {"status": "READY", "request_id": request_id,
                    "profile_id": work["profile_id"],
                    "input_digest": reference.get("input_digest"),
                    "manifest_digest": input_digest,
                    "manifest_path": manifest_path,
                    "snapshot_root": snapshot_root,
                    "historical_input_tree_hash": manifest.tree_hash,
                    "read_only": True}
        identity = {"profile_id": work["profile_id"], "project_id": work["scope"]["project_id"],
                    "block": work["scope"]["block"], "environment": environment}
        return {"status": "READY", "input_digest": digest_json(identity),
                "manifest_digest": environment.get("input_manifest_digest"),
                "request_id": request_id}

    @staticmethod
    def _prepare_local_tool_input(
        *, work: dict[str, Any], environment: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        """依 Profile 固定來源建立小型、可驗證的 task-owned input snapshot。"""
        source_value = environment.get("source_root")
        execution_value = environment.get("execution_root")
        if (not isinstance(source_value, str) or not Path(source_value).is_absolute()
                or not isinstance(execution_value, str)
                or not Path(execution_value).is_absolute()):
            return {"status": "FAILED", "reason_code": "INPUT_SCOPE_INVALID",
                    "recoverable": False, "request_id": request_id}
        source = Path(source_value).resolve()
        execution_root = Path(execution_value).resolve()
        destination = execution_root / work["work_id"] / "input_snapshot"
        allowed = [Path(item).resolve() for item in (
            (work.get("authorization") or {}).get("writable_roots") or [])]
        if (not source.is_dir() or destination.is_relative_to(source)
                or not allowed or not any(
                destination.is_relative_to(root) for root in allowed)):
            return {"status": "FAILED", "reason_code": "INPUT_SCOPE_INVALID",
                    "recoverable": False, "request_id": request_id}
        if destination.exists():
            return {"status": "FAILED", "reason_code": "INPUT_SNAPSHOT_ALREADY_EXISTS",
                    "recoverable": False, "request_id": request_id}
        files = sorted(path for path in source.rglob("*") if path.is_file())
        if (not files or len(files) > 10000
                or sum(path.stat().st_size for path in files) > 64 * 1024 * 1024
                or any(path.is_symlink() for path in source.rglob("*"))):
            return {"status": "FAILED", "reason_code": "INPUT_TREE_INVALID",
                    "recoverable": False, "request_id": request_id}
        before = [{"path": str(path.relative_to(source)),
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in files]
        shutil.copytree(source, destination)
        after_source = [{"path": row["path"], "sha256": hashlib.sha256(
            (source / row["path"]).read_bytes()).hexdigest()} for row in before]
        after_copy = [{"path": row["path"], "sha256": hashlib.sha256(
            (destination / row["path"]).read_bytes()).hexdigest()} for row in before]
        if before != after_source or before != after_copy:
            return {"status": "FAILED", "reason_code": "INPUT_CHANGED_DURING_COPY",
                    "recoverable": False, "request_id": request_id}
        manifest = {"schema_version": "package.input_manifest.v1", "files": before}
        manifest_path = destination / "manifest.json"
        manifest_bytes = (canonical_json(manifest) + "\n").encode("utf-8")
        manifest_path.write_bytes(manifest_bytes)
        tree_hash = digest_json(before)
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        return {"status": "READY", "request_id": request_id,
                "profile_id": work["profile_id"], "input_digest": tree_hash,
                "tree_hash": tree_hash, "manifest_digest": manifest_digest,
                "manifest_path": str(manifest_path), "snapshot_root": str(destination),
                "environment_patch": {
                    "input_snapshot_root": str(destination),
                    "input_manifest_path": str(manifest_path),
                    "input_manifest_digest": manifest_digest}}

    def durable_baseline_reference(self, work: dict[str, Any]) -> dict[str, Any] | None:
        """Profile 釘住 durable source 時，取回 Work 層的 baseline reference。

        回 `None` 代表「這個 Work 沒有宣告 durable 重用」——照常走 baseline
        collection。只有釘選存在、而且 durable 紀錄自己過得了關，才會回一份
        reference；任何不成立都回 `None`，由既有流程決定要不要收集 baseline。
        """
        from terminux_engine.bounded_execution.analysis_tasks import (
            durable_baseline as DB,
        )

        profile_id = work.get("execution_profile_id") or work.get("profile_id")
        flow = getattr(self, "_flow", None)
        resolver = getattr(flow, "profile_for_profile_id", None)
        if not profile_id or not callable(resolver):
            # fixture runtime 沒有 FlowTask service，也就沒有 Profile 釘選。
            return None
        profile = resolver(str(profile_id))
        if profile is None:
            return None
        try:
            source = DB.DurableBaselineSource.from_profile(profile)
            if source is None:
                return None
            return DB.reconstruct_work_baseline_reference(source, expected_import={})
        except DB.DurableBaselineError:
            return None

    def validate_baseline(
        self, *, work: dict[str, Any], reference: dict[str, Any],
        requirements: dict[str, Any],
    ) -> dict[str, Any]:
        required = ("scope", "input_digest", "tool_identity", "config_digest",
                    "constraints_digest", "receipt", "manifest", "dqe", "policy",
                    "execution_import")
        if any(key not in reference for key in required):
            return {"status": "REJECTED", "reason_code": "BASELINE_INCOMPLETE"}
        plan_only = (work.get("authorization") or {}).get("mode") == "PLAN_ONLY"
        reference_scope = reference["scope"]
        if reference_scope != work["scope"]:
            same_read_scope = plan_only and all(
                reference_scope.get(key) == work["scope"].get(key)
                for key in ("member_id", "project_id", "block"))
            # 跨 daemon 的 durable 重用一定跨 session：來源 Work 與本次 Work
            # 的 `origin_session_id` 本來就不同。其餘欄位仍須完全相同。
            compared = (set(reference_scope) | set(work["scope"])) - {
                "origin_session_id"}
            durable_scope = all(
                reference_scope.get(key) == work["scope"].get(key)
                for key in compared)
            if not same_read_scope and not durable_scope:
                differing = sorted(
                    key for key in compared
                    if reference_scope.get(key) != work["scope"].get(key))
                return {"status": "REJECTED", "reason_code": "BASELINE_SCOPE_MISMATCH",
                        "detail": {"differing": differing,
                                   "source": {k: reference_scope.get(k) for k in differing},
                                   "target": {k: work["scope"].get(k) for k in differing},
                                   "source_provenance": "durable baseline reference",
                                   "target_provenance": "this Work scope"}}
        input_verdict = durable_input_verdict(work=work, reference=reference)
        if input_verdict is not None:
            return input_verdict
        expected = {
            "tool_identity": requirements.get("tool_identity"),
            "config_digest": requirements.get("design_config_digest"),
            "constraints_digest": requirements.get("constraints_digest"),
        }
        # durable 重用：`config_digest`／`constraints_digest` 兩側都只是
        # `profile_digest` 的佔位值，對跨 Work 重用既無工程意義也不可能相等。
        # 以**工程內容**逐項比對取代它——不是略過檢查，是換成更強的檢查；
        # 歷史 reference 的原值原樣保留。來源身分（profile digest 是否符合
        # pin）另由 `durable_baseline` 把關。
        environment = work.get("environment") or {}
        durable_record = ((reference.get("execution_import") or {}).get(
            "verification_reference") or {}).get("production_reference")
        if isinstance(durable_record, dict):
            engineering = durable_engineering_verdict(
                reference=reference,
                target_snapshot_root=environment.get("input_snapshot_root"),
                target_tool_identity=environment.get("tool_identity"))
            if engineering is not None:
                return engineering
            expected.pop("config_digest")
            expected.pop("constraints_digest")
        if plan_only:
            # Profile digest 會包含 profile/input 路徑；跨 session 的唯讀
            # analysis 以完整 input tree 與 tool identity 判等，不把路徑差異
            # 誤當工程 config/constraint 變更。
            expected.pop("config_digest")
            expected.pop("constraints_digest")
        for key, value in expected.items():
            if value is not None and reference.get(key) != value:
                return {"status": "REJECTED",
                        "reason_code": "BASELINE_" + key.upper() + "_MISMATCH",
                        "detail": {"field": key, "source": reference.get(key),
                                   "target": value,
                                   "source_provenance": "durable baseline reference",
                                   "target_provenance": "package baseline requirements"}}
        for key in ("receipt", "manifest", "dqe", "policy"):
            if not isinstance(reference[key], dict) or not reference[key].get("complete"):
                return {"status": "REJECTED", "reason_code": f"BASELINE_{key.upper()}_INVALID"}
        if reference.get("content_digest") != digest_json(reference.get("content")):
            return {"status": "REJECTED", "reason_code": "BASELINE_CONTENT_CHANGED"}
        imported = reference.get("execution_import")
        if not isinstance(imported, dict):
            return {"status": "REJECTED", "reason_code": "BASELINE_IMPORT_INVALID"}
        if plan_only and (work.get("input") or {}).get(
                "historical_input_tree_hash") != imported.get("input_tree_hash"):
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_INPUT_TREE_MISMATCH"}
        content = reference.get("content") or {}
        evidence = content.get("completed_run_evidence") or {}
        imported_run = imported.get("run") or {}
        evidence_run_id = evidence.get("run_id")
        dqe_dispatch_key = evidence.get("dqe_dispatch_key")
        if not (
                evidence.get("state") == "ready"
                and isinstance(evidence_run_id, str) and evidence_run_id
                and imported_run.get("run_id") == evidence_run_id
                and imported_run.get("task_id") == imported.get("source_task_id")
                and imported_run.get("slot") == "baseline"
                and reference["receipt"].get("receipt_id")
                == evidence.get("receipt_id")
                and reference["receipt"].get("content_hash")
                == evidence.get("receipt_content_hash")
                and reference["manifest"].get("manifest_id")
                == evidence.get("artifact_manifest_id")
                and reference["manifest"].get("sha256")
                == evidence.get("artifact_manifest_sha256")
                and reference["dqe"].get("row_count")
                == evidence.get("dqe_row_count")
                and reference["dqe"].get("dispatch_key") == dqe_dispatch_key
                and isinstance(dqe_dispatch_key, str)
                and dqe_dispatch_key.startswith(evidence_run_id + ":")
                and content.get("input_manifest_sha256")
                == imported.get("input_manifest_sha256")
                and content.get("input_tree_hash") == imported.get("input_tree_hash")
                and content.get("output_root") == imported.get("output_root")):
            return {"status": "REJECTED",
                    "reason_code": "BASELINE_RUN_EVIDENCE_MISMATCH"}
        if plan_only:
            if not self._historical_baseline_is_readable(reference, imported):
                return {"status": "REJECTED",
                        "reason_code": "BASELINE_HISTORICAL_EVIDENCE_MISMATCH"}
            return {"status": "REUSED_VALIDATED", "reference": reference,
                    "requirements_digest": digest_json(requirements),
                    "validation_mode": "HISTORICAL_READ_ONLY_PLAN_ONLY"}
        exported = self._flow.export_eap_baseline_import(
            task_id=str(imported.get("source_task_id", "")),
            task_digest=str(imported.get("source_task_digest", "")),
            profile_id=work.get("execution_profile_id") or work.get("profile_id"))
        if exported.get("status") != "ok" or exported.get("baseline_import") != imported:
            return {"status": "REJECTED", "reason_code": "BASELINE_IMPORT_CHANGED"}
        return {"status": "REUSED_VALIDATED", "reference": reference,
                "requirements_digest": digest_json(requirements)}

    def _historical_baseline_is_readable(
        self, reference: dict[str, Any], imported: dict[str, Any],
    ) -> bool:
        """用正式唯讀 reader 重驗歷史證據，不產生 host execution import。"""
        source_task_id = str(imported.get("source_task_id") or "")
        source_task_digest = str(imported.get("source_task_digest") or "")
        result = self._flow.get(source_task_id, include_results=True)
        task = result.get("task") or {}
        if (result.get("status") != "ok" or task.get("status") != "COMPLETED"
                or task.get("task_id") != source_task_id
                or task.get("task_digest") != source_task_digest):
            return False
        rows = result.get("results") or []
        verified = [row for row in rows if row.get("step_id") == "s1"
                    and row.get("status") == "SUCCEEDED"
                    and row.get("evidence_status") == "verified"]
        if len(verified) != 1:
            return False
        step = next((row for row in task.get("steps") or []
                     if row.get("step_id") == "s1"), {})
        content = reference.get("content") or {}
        expected = content.get("completed_run_evidence") or {}
        full = ((verified[0].get("envelope") or {}).get("full_result") or {})
        observed = full.get("completed_run_evidence") or {}
        evidence_keys = (
            "state", "run_id", "slot", "run_status", "process_exit_code",
            "receipt_id", "receipt_content_hash", "artifact_manifest_id",
            "artifact_manifest_sha256", "dqe_row_count", "dqe_dispatch_key",
        )
        return bool(
            step.get("result_event_id") == imported.get("result_event_id")
            == content.get("result_event_id")
            and step.get("result_sha256") == imported.get("result_sha256")
            == content.get("result_sha256")
            and all(observed.get(key) == expected.get(key) for key in evidence_keys)
            and observed.get("state") == "ready"
            and full.get("input_manifest_sha256")
            == imported.get("input_manifest_sha256")
            == content.get("input_manifest_sha256")
            and full.get("input_tree_hash") == imported.get("input_tree_hash")
            == content.get("input_tree_hash")
            and full.get("output_root") == imported.get("output_root")
            == content.get("output_root"))

    def collect_evidence(
        self, *, work: dict[str, Any], request: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        if request.get("kind") != "BASELINE_ONLY":
            return {"status": "INCOMPLETE", "reason_code": "EVIDENCE_KIND_UNSUPPORTED",
                    "needs_user": False, "request_id": request_id}
        authorization = work.get("authorization") or {}
        source = {
            "work_id": work["work_id"], "request_id": request_id,
            "parent_authorization_id": authorization.get("authorization_id"),
            "parent_authorization_digest": authorization.get("authorization_digest"),
        }
        created = self._flow.create_eap_baseline_task(
            question=f"Engineering Work baseline for {work['work_id']}",
            profile_id=self._execution_profile_id(work), request=source)
        task = created.get("task") or {}
        if created.get("status") != "ok":
            return {"status": "INCOMPLETE", "reason_code": "BASELINE_DRAFT_FAILED",
                    "detail": created, "request_id": request_id}
        started = self._flow.start_flow_task(task["task_id"], task["task_digest"])
        started_task = started.get("task") or {}
        if started.get("status") != "ok" or not started.get("accepted"):
            return {"status": "INCOMPLETE", "reason_code": "BASELINE_GRANT_FAILED",
                    "detail": started, "request_id": request_id,
                    "task_id": task["task_id"], "task_digest": task["task_digest"]}
        collection = {"status": started_task.get("status", "RUNNING"),
                "source": "controlled_baseline_task", "request_id": request_id,
                "task_id": task["task_id"], "task_digest": task["task_digest"],
                "parent_authorization_id": authorization.get("authorization_id")}
        if collection["status"] not in {"RUNNING", "QUEUED"}:
            return self.poll_evidence(work=work, collection=collection)
        return collection

    def poll_evidence(
        self, *, work: dict[str, Any], collection: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._flow.get(collection["task_id"], include_results=True)
        task = result.get("task") or {}
        status = task.get("status")
        if status in {"RUNNING", "QUEUED", "DRAFT"}:
            return {**collection, "status": status}
        if result.get("status") != "ok" or status != "COMPLETED":
            return {**collection, "status": "INCOMPLETE",
                    "reason_code": "BASELINE_TASK_" + str(status or "UNAVAILABLE"),
                    "task": task}
        results = result.get("results") or []
        verified = [row for row in results
                    if row.get("step_id") == "s1"
                    and row.get("status") == "SUCCEEDED"
                    and row.get("evidence_status") == "verified"]
        if len(verified) != 1:
            return {**collection, "status": "INCOMPLETE",
                    "reason_code": "BASELINE_RESULT_NOT_VERIFIED", "task": task}
        envelope = verified[0].get("envelope") or {}
        full = envelope.get("full_result") or {}
        evidence = full.get("completed_run_evidence") or {}
        if evidence.get("state") != "ready":
            return {**collection, "status": "INCOMPLETE",
                    "reason_code": "BASELINE_ENGINEERING_EVIDENCE_NOT_READY",
                    "task": task}
        exported = self._flow.export_eap_baseline_import(
            task_id=task["task_id"], task_digest=task["task_digest"])
        baseline_import = exported.get("baseline_import")
        if exported.get("status") != "ok" or not isinstance(baseline_import, dict):
            return {**collection, "status": "INCOMPLETE",
                    "reason_code": "BASELINE_IMPORT_NOT_VERIFIED",
                    "detail": exported, "task": task}
        imported_run = baseline_import.get("run") or {}
        evidence_run_id = evidence.get("run_id")
        dqe_dispatch_key = evidence.get("dqe_dispatch_key")
        task_step = next((row for row in task.get("steps") or []
                          if row.get("step_id") == "s1"), {})
        result_event_id = (task_step.get("result_event_id")
                           or verified[0].get("result_event_id"))
        result_sha256 = (task_step.get("result_sha256")
                         or verified[0].get("result_sha256"))
        result_reference_consistent = (
            (not verified[0].get("result_event_id")
             or verified[0].get("result_event_id") == result_event_id)
            and (not verified[0].get("result_sha256")
                 or verified[0].get("result_sha256") == result_sha256))
        identity_matches = (
            isinstance(evidence_run_id, str) and evidence_run_id
            and isinstance(result_event_id, str) and result_event_id
            and isinstance(result_sha256, str) and result_sha256
            and imported_run.get("run_id") == evidence_run_id
            and imported_run.get("task_id") == task.get("task_id")
            and imported_run.get("slot") == "baseline"
            and isinstance(dqe_dispatch_key, str)
            and dqe_dispatch_key.startswith(evidence_run_id + ":")
            and result_reference_consistent
            and result_event_id == baseline_import.get("result_event_id")
            and result_sha256 == baseline_import.get("result_sha256")
            and full.get("input_manifest_sha256")
            == baseline_import.get("input_manifest_sha256")
            and full.get("input_tree_hash") == baseline_import.get("input_tree_hash")
            and full.get("output_root") == baseline_import.get("output_root")
        )
        if not identity_matches:
            return {**collection, "status": "INCOMPLETE",
                    "reason_code": "BASELINE_RUN_EVIDENCE_MISMATCH", "task": task}
        content = {
            "task_id": task.get("task_id"), "task_digest": task.get("task_digest"),
            "run_id": evidence.get("run_id"), "result_event_id": result_event_id,
            "result_sha256": result_sha256,
            "completed_run_evidence": evidence,
            "input_manifest_sha256": full.get("input_manifest_sha256"),
            "input_tree_hash": full.get("input_tree_hash"),
            "output_root": full.get("output_root"),
            "baseline_import": baseline_import,
        }
        environment = work.get("environment") or {}
        reference = {
            "scope": work["scope"], "input_digest": work["input"]["input_digest"],
            "tool_identity": environment.get("tool_identity"),
            "config_digest": environment.get("design_config_digest"),
            "constraints_digest": environment.get("constraints_digest"),
            "receipt": {"complete": bool(evidence.get("receipt_id")),
                        "receipt_id": evidence.get("receipt_id"),
                        "content_hash": evidence.get("receipt_content_hash")},
            "manifest": {"complete": bool(evidence.get("artifact_manifest_id")),
                         "manifest_id": evidence.get("artifact_manifest_id"),
                         "sha256": evidence.get("artifact_manifest_sha256")},
            "dqe": {"complete": int(evidence.get("dqe_row_count") or 0) > 0,
                    "row_count": int(evidence.get("dqe_row_count") or 0),
                    "dispatch_key": evidence.get("dqe_dispatch_key")},
            "policy": {"complete": True,
                       "id": "flow-task-run-evidence-ready.v1"},
            "execution_import": baseline_import,
            "content": content, "content_digest": digest_json(content),
        }
        if not all(reference[key]["complete"]
                   for key in ("receipt", "manifest", "dqe", "policy")):
            return {**collection, "status": "INCOMPLETE",
                    "reason_code": "BASELINE_REFERENCE_INCOMPLETE", "task": task}
        return {**collection, "status": "COMPLETE", "task": task,
                "reference": reference, "result": verified[0]}

    def execute(
        self, *, work: dict[str, Any], proposal: dict[str, Any],
        execution_plan: dict[str, Any], authorization: dict[str, Any],
        attempt_id: str, request_id: str,
    ) -> dict[str, Any]:
        flow_proposal = json.loads(json.dumps(execution_plan["flow_task_proposal"]))
        if isinstance(flow_proposal.get("package_tool"), dict):
            flow_proposal["canonical_proposal"] = json.loads(json.dumps(proposal))
            flow_proposal["authorization_writable_roots"] = list(
                authorization.get("writable_roots") or [])
        flow_proposal["work_source"] = {
            "work_id": work["work_id"], "work_revision": work["revision"],
            "attempt_id": attempt_id, "parent_authorization_id": authorization["authorization_id"],
            "parent_authorization_digest": authorization["authorization_digest"],
        }
        if isinstance(work.get("execution_seed"), dict):
            flow_proposal["work_source"]["saved_proposal"] = {
                key: json.loads(json.dumps(work["execution_seed"][key]))
                for key in (
                    "source_work_id", "source_proposal_id",
                    "source_proposal_digest", "canonical_proposal_digest",
                    "source_response_digest", "domain_response_digest",
                    "global_decision_id",
                    "global_decision_digest", "baseline_strategy",
                ) if key in work["execution_seed"]}
        create = (self._flow.create_package_flow_task
                  if isinstance(flow_proposal.get("package_tool"), dict)
                  else self._flow.create_eap_flow_task)
        created = create(
            question=f"Engineering Work {work['work_id']} attempt {attempt_id}",
            profile_id=self._execution_profile_id(work), proposal=flow_proposal)
        task = created.get("task") or {}
        if created.get("status") != "ok":
            return {"status": "FAILED", "reason_code": "CHILD_DRAFT_FAILED", "detail": created}
        started = self._flow.start_flow_task(task["task_id"], task["task_digest"])
        started_task = started.get("task") or {}
        if started.get("status") != "ok" or not started.get("accepted"):
            return {"status": "FAILED", "reason_code": "CHILD_GRANT_FAILED",
                    "task_id": task["task_id"], "task_digest": task["task_digest"],
                    "detail": started}
        return {"status": started_task.get("status", "RUNNING"),
                "task_id": task["task_id"], "task_digest": task["task_digest"],
                # grant identity 由 FlowTask view 直接給；舊形狀（巢狀 grant
                # 物件）仍然接受，兩者都取不到才是 `None`。
                "grant_id": (started_task.get("grant_id")
                             or (started_task.get("grant") or {}).get("grant_id")),
                "parent_authorization_id": authorization["authorization_id"]}

    def poll(self, *, work: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
        result = self._flow.get(attempt["task_id"], include_results=True)
        task = result.get("task") or {}
        status = task.get("status")
        if status in {"RUNNING", "QUEUED", "DRAFT", "WAITING_DEPENDENCY"}:
            # WAITING_DEPENDENCY：FlowTask 正在等待 dependent proof 完成；
            # 父 FlowTask 尚未終結，attempt 不得提前結算。
            return {"status": status, "task_id": attempt["task_id"]}
        return {"status": status or "UNCONFIRMED", "task_id": attempt["task_id"],
                "task_digest": attempt.get("task_digest"), "results": result.get("results"),
                # 有界的工程結果摘要。Expert／Global 與續作 guard 都讀它，而不是
                # 去翻整份 FlowTask view；原始 view 仍然保留在 evidence 內。
                "engineering_result": engineering_result(task),
                "evidence": {"flow_task": result}}

    def cancel(self, *, work: dict[str, Any]) -> dict[str, Any]:
        collection = (work.get("baseline") or {}).get("collection")
        if isinstance(collection, dict) and collection.get("task_id") \
                and collection.get("task_digest"):
            result = self._flow.cancel_flow_task(
                collection["task_id"], collection["task_digest"])
            terminal = (result.get("task") or {}).get("status") in {
                "CANCELLED", "FAILED", "BLOCKED", "COMPLETED"}
            return {"children_terminal": terminal,
                    "cancelled": [collection["task_id"]], "result": result}
        active = next((row for row in reversed(work["attempts"])
                       if row.get("status") in {"RUNNING", "QUEUED"}), None)
        if active is None:
            return {"children_terminal": True, "cancelled": []}
        result = self._flow.cancel_flow_task(active["task_id"], active["task_digest"])
        terminal = (result.get("task") or {}).get("status") in {"CANCELLED", "FAILED", "BLOCKED"}
        return {"children_terminal": terminal, "cancelled": [active["task_id"]], "result": result}

    def _execution_profile_id(self, work: dict[str, Any]) -> str:
        profile_id = str(work.get("execution_profile_id") or work["profile_id"])
        if profile_id == work["profile_id"]:
            return profile_id
        scope = self._flow.eap_profile_scope(profile_id)
        if isinstance(scope, dict) and scope.get("status") == "ok":
            return profile_id
        source = work.get("requested_source_root")
        if not isinstance(source, str):
            raise WorkError("WORK_INPUT_REATTACH_FAILED",
                            "prepared execution Profile is unavailable")
        restored = self._flow.prepare_eap_work_input(
            profile_id=work["profile_id"], work_id=work["work_id"],
            source_root=source)
        if restored.get("status") != "ok" or restored.get("profile_id") != profile_id:
            raise WorkError("WORK_INPUT_REATTACH_FAILED",
                            str(restored.get("error_code") or "prepared input unavailable"))
        return profile_id


class FixtureProcessRuntime(FlowTaskWorkRuntime):
    """只供 isolated qualification 的本機 child runtime。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config)
        self._executor = SubprocessExecutor()
        self._results: dict[str, dict[str, Any]] = {}

    def supports_plan(self, execution_plan: dict[str, Any]) -> bool:
        """fixture 的單工具路徑只證明 Kernel／child，沒有正式 FlowTask。"""
        return execution_plan.get("runtime") in {"single_tool", "flow_task"}

    @classmethod
    def from_file(cls, path: str | Path) -> "FixtureProcessRuntime":
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("fixture") is not True:
            raise WorkError("FIXTURE_RUNTIME_CONFIG_INVALID", "fixture: true is required")
        return cls(config)

    def prepare_input(self, *, work: dict[str, Any], request_id: str) -> dict[str, Any]:
        source = Path(self._config["source_root"]).resolve()
        if not source.is_dir():
            return {"status": "FAILED", "recoverable": True,
                    "reason_code": "SOURCE_NOT_FOUND"}
        snapshot = self._task_root(
            work["work_id"], "input_snapshot_" + request_id.rsplit(":", 1)[-1])
        self._assert_authorized_root(snapshot, work["authorization"])
        records = []
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            content = path.read_bytes()
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            records.append({"path": str(relative),
                            "sha256": digest_json(content.hex())})
        manifest = {"schema_version": "eap3.input_manifest.v1", "files": records}
        self._after_snapshot_copy(source, snapshot)
        # source 與 snapshot 都再讀一次；任一側改動都不能宣稱固定成功。
        verify = [{"path": row["path"], "sha256": digest_json(
            (source / row["path"]).read_bytes().hex())} for row in records]
        copied = [{"path": row["path"], "sha256": digest_json(
            (snapshot / row["path"]).read_bytes().hex())} for row in records]
        if verify != records or copied != records:
            return {"status": "FAILED", "recoverable": True,
                    "reason_code": "SOURCE_CHANGED_DURING_PREPARATION",
                    "partial_snapshot_root": str(snapshot)}
        manifest_path = snapshot / "manifest.json"
        manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        return {"status": "READY", "request_id": request_id,
                "input_digest": digest_json(manifest), "manifest": manifest,
                "manifest_digest": digest_json(manifest),
                "snapshot_root": str(snapshot), "manifest_path": str(manifest_path)}

    def _after_snapshot_copy(self, source: Path, snapshot: Path) -> None:
        """測試插入點：正式 fixture runtime 不修改 source。"""
        return

    def collect_evidence(
        self, *, work: dict[str, Any], request: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        self._assert_evidence_sink(work)
        if request.get("kind") != "BASELINE_ONLY" and not request.get("requires_tool"):
            available = list(self._config.get("existing_evidence") or [])
            return {"status": "COMPLETE", "request_id": request_id,
                    "source": "existing", "items": available,
                    "reference": {"kind": "existing", "digest": digest_json(available)}}
        root = self._task_root(work["work_id"], "baseline_" + request_id.rsplit(":", 1)[-1])
        self._assert_authorized_root(root, work["authorization"])
        task_id = "fixture_evidence_task_" + request_id.rsplit(":", 1)[-1]
        task_digest = digest_json({"work_id": work["work_id"], "request": request,
                                   "input": work["input"]["input_digest"]})
        authorization = self._child_authorization(work, task_id, task_digest, "baseline")
        result = self._run_child(root, mode="baseline", candidate=None,
                                 authorization=authorization)
        reference = self._baseline_reference(work, result)
        return {"status": "COMPLETE" if result["receipt"]["exit_code"] == 0 else "INCOMPLETE",
                "request_id": request_id, "source": "controlled_task",
                "reference": reference, "receipt": result["receipt"],
                "manifest": result["manifest"], "dqe": result["dqe"],
                "task_id": task_id, "task_digest": task_digest,
                "grant": authorization}

    def poll_evidence(
        self, *, work: dict[str, Any], collection: dict[str, Any],
    ) -> dict[str, Any]:
        # qualification fixture 的 child 同步收束；不應留下待輪詢工作。
        return {**collection, "status": "INCOMPLETE",
                "reason_code": "FIXTURE_EVIDENCE_POLL_UNEXPECTED"}

    def execute(
        self, *, work: dict[str, Any], proposal: dict[str, Any],
        execution_plan: dict[str, Any], authorization: dict[str, Any],
        attempt_id: str, request_id: str,
    ) -> dict[str, Any]:
        root = self._task_root(work["work_id"], attempt_id)
        self._assert_authorized_root(root, authorization)
        self._assert_evidence_sink(work)
        candidate_dir = root / "candidate"
        candidate_dir.mkdir(parents=True, exist_ok=False)
        canonical = execution_plan["canonical_proposal"]
        candidate_file = candidate_dir / "approved_change.json"
        candidate_file.write_text(canonical_json(canonical) + "\n", encoding="utf-8")
        materialized_digest = digest_json(json.loads(candidate_file.read_text(encoding="utf-8")))
        if materialized_digest != digest_json(canonical):
            return {"status": "FAILED", "reason_code": "MATERIALIZED_PROPOSAL_MISMATCH"}
        candidate = canonical.get("changes", {}).get("candidate")
        if candidate is None:
            candidate = canonical.get("changes", {}).get("mode")
        task_id = "fixture_task_" + attempt_id.removeprefix("attempt_")
        task_digest = digest_json({"work_id": work["work_id"], "attempt_id": attempt_id,
                                   "plan": execution_plan, "authorization": authorization["authorization_digest"]})
        child_authorization = self._child_authorization(
            work, task_id, task_digest, "candidate")
        result = self._run_child(root, mode="candidate", candidate=str(candidate),
                                 authorization=child_authorization)
        grant_id = child_authorization["grant"]["grant_id"]
        outcome = {"status": "SUCCEEDED" if result["receipt"]["exit_code"] == 0 else "FAILED",
                   "task_id": task_id, "task_digest": task_digest, "grant_id": grant_id,
                   "host_binding": {**child_authorization["binding"],
                                    "task_owned_root": str(root)},
                   "materialization": {"path": str(candidate_file),
                                       "proposal_digest": materialized_digest,
                                       "count": 1},
                   "receipt": result["receipt"], "manifest": result["manifest"],
                   "dqe": result["dqe"], "formal": result["formal"],
                   "acceptance": result["acceptance"],
                   "evidence": {"artifact_root": str(root / "artifacts"),
                                "sink": str(self._config["evidence_root"])}}
        self._results[attempt_id] = outcome
        return outcome

    def poll(self, *, work: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
        return self._results.get(attempt["attempt_id"], {"status": "UNCONFIRMED"})

    def cancel(self, *, work: dict[str, Any]) -> dict[str, Any]:
        # 此 fixture 的 child 是同步且在 execute 回傳前已由 SubprocessExecutor
        # 收束；不存在未受控背景程序。
        return {"children_terminal": True, "cancelled": []}

    def validate_baseline(
        self, *, work: dict[str, Any], reference: dict[str, Any],
        requirements: dict[str, Any],
    ) -> dict[str, Any]:
        required = ("scope", "input_digest", "tool_identity", "config_digest",
                    "constraints_digest", "receipt", "manifest", "dqe", "policy",
                    "content", "content_digest")
        if any(key not in reference for key in required):
            return {"status": "REJECTED", "reason_code": "BASELINE_INCOMPLETE"}
        if reference["scope"] != work["scope"]:
            return {"status": "REJECTED", "reason_code": "BASELINE_SCOPE_MISMATCH"}
        if reference["input_digest"] != (work.get("input") or {}).get("input_digest"):
            return {"status": "REJECTED", "reason_code": "BASELINE_INPUT_MISMATCH"}
        environment = work.get("environment") or {}
        expected = {"tool_identity": environment.get("tool_identity", "fixture-tool/v1"),
                    "config_digest": environment.get("design_config_digest", "fixture-config"),
                    "constraints_digest": environment.get("constraints_digest", "fixture-constraints")}
        for key, value in expected.items():
            if reference.get(key) != value:
                return {"status": "REJECTED",
                        "reason_code": "BASELINE_" + key.upper() + "_MISMATCH"}
        if reference["content_digest"] != digest_json(reference["content"]):
            return {"status": "REJECTED", "reason_code": "BASELINE_CONTENT_CHANGED"}
        if any(not isinstance(reference[key], dict) or not reference[key].get("complete")
               for key in ("receipt", "manifest", "dqe", "policy")):
            return {"status": "REJECTED", "reason_code": "BASELINE_PARTIAL"}
        return {"status": "REUSED_VALIDATED", "reference": reference,
                "requirements_digest": digest_json(requirements)}

    def _task_root(self, work_id: str, child_id: str) -> Path:
        execution_root = Path(self._config["execution_root"]).resolve()
        root = (execution_root / work_id / child_id).resolve()
        if execution_root not in root.parents:
            raise WorkError("FIXTURE_TASK_ROOT_INVALID", str(root))
        root.mkdir(parents=True, exist_ok=False)
        return root

    @staticmethod
    def _assert_authorized_root(root: Path, authorization: dict[str, Any]) -> None:
        allowed = [Path(item).resolve() for item in authorization.get("writable_roots") or []]
        if not any(root == item or root.is_relative_to(item) for item in allowed):
            raise WorkError("WRITABLE_ROOT_OUT_OF_SCOPE", str(root))

    def _assert_evidence_sink(self, work: dict[str, Any]) -> None:
        configured = Path(self._config["evidence_root"]).resolve()
        environment = work.get("environment") or {}
        dqe_parent = Path(environment.get("dqe_db_path", configured / "dqe.sqlite")).resolve().parent
        artifacts = Path(environment.get("artifact_root", configured / "artifacts")).resolve()
        if not all(path == configured or path.is_relative_to(configured)
                   for path in (dqe_parent, artifacts)):
            raise WorkError("EVIDENCE_SINK_MISMATCH",
                            "fixture runtime must use the Profile-fixed evidence sink")

    def _run_child(
        self, root: Path, *, mode: str, candidate: str | None,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """經既有 OrchestrationKernel／ToolRegistry 啟動本機 fixture。"""
        from terminux_engine.orchestration.kernel import OrchestrationKernel
        from terminux_engine.orchestration.ledger import OrchestrationLedger
        from terminux_engine.orchestration.outcome import DispatchOutcome, PackageOutcome
        from terminux_engine.orchestration.single_tool_runner import SingleToolRunner
        from terminux_engine.orchestration.tool_registry import ToolDefinition, ToolRegistry

        artifacts = root / "artifacts"
        artifacts.mkdir(parents=True)

        def command(parameters: dict[str, Any]) -> list[str]:
            argv = ["--mode", str(parameters["mode"]), "--output", str(parameters["output"])]
            if parameters.get("candidate") is not None:
                argv.extend(["--candidate", str(parameters["candidate"])])
            return argv

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            tool_id="eap3_fixture_tool",
            executables=[str(Path(self._config["executable"]).resolve())],
            event_tool_name="eap3_fixture_tool", expected_packages=["fixture_dqe"],
            default_timeout=float(self._config.get("timeout_sec", 30)),
            command_builder=command))

        class EvidenceEngine:
            def dispatch_sync(self, event: Any) -> DispatchOutcome:
                return DispatchOutcome(
                    event_id=event.event_id, tool_name=event.tool_name, source=event.source,
                    started_at=time.time(), finished_at=time.time(),
                    packages=[PackageOutcome(
                        "fixture_dqe", "1", "ok", "fixture evidence persisted",
                        time.time(), time.time(), evidence={"dqe_write": {"status": "written"},
                                                          "authorization": authorization})],
                    skipped_no_after=[])

        parameters = {"mode": mode, "output": str(artifacts), "candidate": candidate}
        kernel = OrchestrationKernel(
            engine=EvidenceEngine(), ledger=OrchestrationLedger(root / "kernel_ledger.sqlite"),
            tool_registry=registry, runner=SingleToolRunner())
        outcome = kernel.run({"tool_id": "eap3_fixture_tool", "parameters": parameters,
                              "cwd": str(root), "step_id": mode})
        if outcome is None or not outcome.steps or outcome.steps[0].subprocess_result is None:
            raise WorkError("FIXTURE_KERNEL_OUTCOME_MISSING", "Kernel returned no subprocess result")
        result = outcome.steps[0].subprocess_result
        argv = outcome.steps[0].prepared_execution.argv if outcome.steps[0].prepared_execution else []
        files = [{"path": str(path), "sha256": digest_json(path.read_bytes().hex())}
                 for path in sorted(item for item in artifacts.rglob("*") if item.is_file())]
        receipt = {"complete": not result.timed_out, "exit_code": result.exit_code,
                   "started_at": result.started_at, "finished_at": result.finished_at,
                   "argv_digest": digest_json(argv), "fixture": True}
        manifest = {"complete": True, "files": files, "digest": digest_json(files)}
        metrics_path = artifacts / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
        dqe = {"complete": bool(metrics), "metrics": metrics,
               "verdict": "PASS" if metrics.get("violations", 1) == 0 else "VALID_NOT_TARGET"}
        formal = {"complete": True, "equivalent": bool(metrics.get("equivalent", False))}
        acceptance = {"complete": True, "passed": bool(
            receipt["exit_code"] == 0 and dqe["complete"] and formal["equivalent"]),
            "criteria_met": bool(metrics.get("criteria_met", False))}
        sink = Path(self._config["evidence_root"]).resolve() / root.parent.name / root.name
        sink.mkdir(parents=True, exist_ok=True)
        (sink / "evidence.json").write_text(canonical_json({
            "receipt": receipt, "manifest": manifest, "dqe": dqe,
            "formal": formal, "acceptance": acceptance}) + "\n", encoding="utf-8")
        return {"receipt": {**receipt, "kernel_run_id": outcome.run_id,
                            "kernel_status": outcome.final_status},
                "manifest": manifest, "dqe": dqe,
                "formal": formal, "acceptance": acceptance}

    @staticmethod
    def _child_authorization(
        work: dict[str, Any], task_id: str, task_digest: str, step_id: str,
    ) -> dict[str, Any]:
        """使用既有 grant／step binding 型別建立每輪獨立授權。"""
        from terminux_engine.bounded_execution.analysis_tasks.models import (
            TaskAuthorizationGrant, TaskScope, TaskStepBinding,
        )
        from terminux_engine.bounded_execution.analysis_tasks.canonical import now_ms, monotonic_s

        scope_data = work["scope"]
        scope = TaskScope(
            member_id=scope_data["member_id"], project_id=scope_data["project_id"],
            origin_session_id=scope_data["origin_session_id"], workspace="",
            project_source=scope_data["project_source"])
        now = now_ms()
        mono = monotonic_s()
        grant = TaskAuthorizationGrant(
            grant_id="grant_" + digest_json({"task": task_id, "digest": task_digest})[:24],
            task_id=task_id, task_digest=task_digest,
            daemon_instance_id=work["daemon_instance_id"], scope=scope,
            approved_by_session_id=scope.origin_session_id, approved_at_ms=now,
            expires_at_ms=now + 600_000, expires_at_monotonic=mono + 600,
            deadline_at_ms=now + 600_000, deadline_at_monotonic=mono + 600,
            body_llm_dispatches=0)
        binding = TaskStepBinding(
            task_id=task_id, step_id=step_id, grant_id=grant.grant_id,
            capability_id="eap3.fixture.execute", arguments_sha256=task_digest,
            capability_contract_sha256=digest_json({"fixture": True}),
            catalog_snapshot_id="eap3-fixture-catalog-v1", risk="dangerous", scope=scope,
            authorization_source="task")
        return {"grant": grant.to_event_payload(), "binding": binding.to_json_dict()}

    def _baseline_reference(self, work: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        content = {"receipt": result["receipt"], "manifest": result["manifest"],
                   "dqe": result["dqe"]}
        environment = work.get("environment") or {}
        execution_import = {
            "schema_version": "fixture.baseline_import.v1",
            "source_task_id": "fixture-baseline",
            "source_task_digest": digest_json(content),
            "source_profile_id": work.get("execution_profile_id") or work["profile_id"],
            "source_scope": work["scope"],
            "fixture_only": True,
        }
        execution_import["import_digest"] = digest_json(execution_import)
        return {"scope": work["scope"], "input_digest": work["input"]["input_digest"],
                "tool_identity": environment.get("tool_identity", "fixture-tool/v1"),
                "config_digest": environment.get("design_config_digest", "fixture-config"),
                "constraints_digest": environment.get("constraints_digest", "fixture-constraints"),
                "receipt": {"complete": result["receipt"]["complete"]},
                "manifest": {"complete": result["manifest"]["complete"]},
                "dqe": {"complete": result["dqe"]["complete"]},
                "policy": {"complete": True, "id": "fixture-policy-v1"},
                "execution_import": execution_import,
                "content": content, "content_digest": digest_json(content)}


__all__ = [
    "ENGINEERING_RESULT_SCHEMA", "FixtureProcessRuntime", "FlowTaskWorkRuntime",
    "ProviderBindingRouter", "build_package_registry", "engineering_result",
]
