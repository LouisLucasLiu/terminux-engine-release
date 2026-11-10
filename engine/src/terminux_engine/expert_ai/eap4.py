# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""同一 Engineering Work 的可替換多 Expert 協作。

本模組只擴充 Engineering Work 的角色選擇與回覆整合；work、budget、Package、FlowTask、
grant、Kernel 與 evidence reader 仍由既有元件負責。Expert 不能直接派送其他
Expert，也不能取得新的執行權限。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from terminux_engine.expert_ai.eap3 import (
    EngineeringWorkService,
    GLOBAL_DECISIONS,
    TERMINAL_STATUSES,
    WorkError,
    canonical_json,
    digest_json,
)
from terminux_engine.expert_ai.eap3_extensions import ProviderBindingRouter


EAP4_SCHEMA_VERSION = "multi_expert_collaboration.v1"
EXPERT_PURPOSES = frozenset({
    "proposal", "proposal_revision", "proposal_review",
    "evidence_review", "execution_analysis", "result_review",
})
REVIEW_KINDS = frozenset({"REVIEW", "NEED_EVIDENCE", "UNSUPPORTED"})


@dataclass(frozen=True)
class ExpertCapabilityDescriptor:
    """可選 Expert 的固定、可觀測描述。"""

    expert_id: str
    capability: str
    purposes: tuple[str, ...]
    responsibility: str
    input_schema_version: str
    output_schema_version: str
    binding_id: str
    adapter_version: str
    implementation_version: str
    evidence_requirements: tuple[str, ...] = ()
    selection_keywords: tuple[str, ...] = ()
    required: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "capability": self.capability,
            "purposes": list(self.purposes),
            "responsibility": self.responsibility,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "binding_id": self.binding_id,
            "adapter_version": self.adapter_version,
            "implementation_version": self.implementation_version,
            "evidence_requirements": list(self.evidence_requirements),
            "selection_keywords": list(self.selection_keywords),
            "required": self.required,
        }

    @property
    def descriptor_digest(self) -> str:
        return digest_json(self.to_json_dict())


class ExpertCapabilityCatalog:
    """daemon 啟動時固定的 Expert capability catalog。"""

    def __init__(
        self, descriptors: tuple[ExpertCapabilityDescriptor, ...], *,
        global_binding_id: str,
    ) -> None:
        if not isinstance(global_binding_id, str) or not global_binding_id:
            raise WorkError("EXPERT_CATALOG_INVALID", "global_binding_id is required")
        self.global_binding_id = global_binding_id
        self._descriptors: dict[str, ExpertCapabilityDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.expert_id in self._descriptors:
                raise WorkError("EXPERT_ID_DUPLICATE", descriptor.expert_id)
            if not descriptor.purposes or any(
                    purpose not in EXPERT_PURPOSES for purpose in descriptor.purposes):
                raise WorkError("EXPERT_PURPOSE_INVALID", descriptor.expert_id)
            self._descriptors[descriptor.expert_id] = descriptor
        if not self._descriptors:
            raise WorkError("EXPERT_CATALOG_INVALID", "at least one Expert is required")

    @classmethod
    def from_file(cls, path: str | Path) -> "ExpertCapabilityCatalog":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise WorkError("EXPERT_CATALOG_INVALID", "schema_version 1 is required")
        rows = payload.get("experts")
        if not isinstance(rows, list):
            raise WorkError("EXPERT_CATALOG_INVALID", "experts array is required")
        descriptors: list[ExpertCapabilityDescriptor] = []
        required = {
            "expert_id", "capability", "purposes", "responsibility",
            "input_schema_version", "output_schema_version", "binding_id",
            "adapter_version", "implementation_version",
        }
        for row in rows:
            if not isinstance(row, dict) or not required.issubset(row):
                raise WorkError("EXPERT_CATALOG_INVALID", "Expert fields are incomplete")
            sequence_fields = (
                "purposes", "evidence_requirements", "selection_keywords")
            if any(not isinstance(row.get(name, []), list) for name in sequence_fields):
                raise WorkError("EXPERT_CATALOG_INVALID", "Expert arrays are invalid")
            descriptors.append(ExpertCapabilityDescriptor(
                expert_id=str(row["expert_id"]),
                capability=str(row["capability"]),
                purposes=tuple(str(value) for value in row["purposes"]),
                responsibility=str(row["responsibility"]),
                input_schema_version=str(row["input_schema_version"]),
                output_schema_version=str(row["output_schema_version"]),
                binding_id=str(row["binding_id"]),
                adapter_version=str(row["adapter_version"]),
                implementation_version=str(row["implementation_version"]),
                evidence_requirements=tuple(
                    str(value) for value in row.get("evidence_requirements", [])),
                selection_keywords=tuple(
                    str(value).casefold() for value in row.get(
                        "selection_keywords", [])),
                required=bool(row.get("required", False)),
            ))
        return cls(tuple(descriptors),
                   global_binding_id=str(payload.get("global_binding_id") or ""))

    def get(self, expert_id: str) -> ExpertCapabilityDescriptor:
        descriptor = self._descriptors.get(expert_id)
        if descriptor is None:
            raise WorkError("EXPERT_NOT_AVAILABLE", expert_id)
        return descriptor

    def descriptors(self) -> list[dict[str, Any]]:
        return [self._descriptors[key].to_json_dict()
                for key in sorted(self._descriptors)]

    def select(
        self, *, capability: str, purpose: str, objective: str,
        allowed_experts: tuple[str, ...] = (),
    ) -> ExpertCapabilityDescriptor:
        allowed = set(allowed_experts)
        candidates = [
            descriptor for descriptor in self._descriptors.values()
            if descriptor.capability == capability
            and purpose in descriptor.purposes
            and (not allowed or descriptor.expert_id in allowed)
        ]
        if not candidates:
            raise WorkError(
                "EXPERT_CAPABILITY_NOT_AVAILABLE", f"{capability}/{purpose}")
        text = objective.casefold()
        candidates.sort(key=lambda descriptor: (
            -sum(keyword in text for keyword in descriptor.selection_keywords),
            not descriptor.required, descriptor.expert_id))
        return candidates[0]

    def supporting(
        self, *, purpose: str, allowed_experts: tuple[str, ...] = (),
    ) -> list[ExpertCapabilityDescriptor]:
        allowed = set(allowed_experts)
        return sorted([
            descriptor for descriptor in self._descriptors.values()
            if purpose in descriptor.purposes
            and (not allowed or descriptor.expert_id in allowed)
        ], key=lambda descriptor: (not descriptor.required, descriptor.expert_id))


class ExpertBindingRouter:
    """依 Expert descriptor 的 binding_id 路由，沿用既有 provider adapter。"""

    def __init__(
        self, *, catalog: ExpertCapabilityCatalog,
        bindings: list[dict[str, Any]], fixture_only: bool = False,
    ) -> None:
        self._catalog = catalog
        by_id: dict[str, dict[str, Any]] = {}
        for row in bindings:
            if not isinstance(row, dict) or not isinstance(row.get("binding_id"), str):
                raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "binding_id is required")
            if row["binding_id"] in by_id:
                raise WorkError("PROVIDER_BINDING_ID_DUPLICATE", row["binding_id"])
            by_id[row["binding_id"]] = row
        needed = {catalog.global_binding_id}
        needed.update(row["binding_id"] for row in catalog.descriptors())
        missing = sorted(needed - set(by_id))
        if missing:
            raise WorkError("EXPERT_BINDING_NOT_FOUND", ",".join(missing))
        self._routers = {
            binding_id: ProviderBindingRouter([by_id[binding_id]],
                                               fixture_only=fixture_only)
            for binding_id in needed
        }

    @classmethod
    def from_files(
        cls, *, catalog: ExpertCapabilityCatalog,
        binding_path: str | Path, fixture_only: bool = False,
    ) -> "ExpertBindingRouter":
        payload = yaml.safe_load(Path(binding_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("bindings"), list):
            raise WorkError("PROVIDER_BINDING_CONFIG_INVALID", "bindings array is required")
        return cls(catalog=catalog, bindings=payload["bindings"],
                   fixture_only=fixture_only)

    def call(
        self, *, role: str, capability: str, purpose: str,
        request: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        if role == "global":
            binding_id = self._catalog.global_binding_id
        else:
            context = request.get("expert_context") or {}
            expert_id = context.get("expert_id")
            descriptor = self._catalog.get(str(expert_id or ""))
            if descriptor.capability != capability or purpose not in descriptor.purposes:
                raise WorkError(
                    "EXPERT_PURPOSE_NOT_SUPPORTED", f"{descriptor.expert_id}/{purpose}")
            if context.get("descriptor_digest") != descriptor.descriptor_digest:
                raise WorkError(
                    "EXPERT_DESCRIPTOR_MISMATCH", descriptor.expert_id)
            binding_id = descriptor.binding_id
        response = self._routers[binding_id].call(
            role=role, capability=capability, purpose=purpose,
            request=request, request_id=request_id)
        metadata = response.get("_terminux_provider_call")
        if isinstance(metadata, dict):
            metadata["configured_binding_id"] = binding_id
        return response


class MultiExpertWorkService(EngineeringWorkService):
    """在既有 Engineering Work 狀態機內加入多 Expert 選擇、依賴與整合。"""

    def __init__(self, *, expert_catalog: ExpertCapabilityCatalog, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._expert_catalog = expert_catalog

    def create(
        self, *, objective: str, profile_id: str, package_id: str,
        constraints: list[str] | None = None, criteria: list[str] | None = None,
        block: str | None = None, method: str | None = None,
        source_root: str | None = None,
        baseline_reference: dict[str, Any] | None = None,
        request_id: str | None = None,
        preferred_experts: list[str] | None = None,
        required_reviews: list[str] | None = None,
    ) -> dict[str, Any]:
        preferences = list(preferred_experts or [])
        reviews = list(required_reviews or ["proposal_review", "result_review"])
        if any(not isinstance(item, str) or not item for item in preferences):
            return self._error("EXPERT_PREFERENCE_INVALID", "expert ids must be non-empty")
        if len(set(preferences)) != len(preferences):
            return self._error("EXPERT_PREFERENCE_INVALID", "expert ids must be unique")
        if any(item not in {"proposal_review", "result_review"} for item in reviews):
            return self._error("EXPERT_REVIEW_INVALID", "unsupported review purpose")
        try:
            for expert_id in preferences:
                self._expert_catalog.get(expert_id)
        except WorkError as exc:
            return self._error(exc.code, exc.message)
        stable_request = request_id
        if stable_request is None:
            context = self._scope_provider()
            stable_request = "eap4-work-create:" + digest_json({
                "session": context.session_id, "profile_id": profile_id,
                "package_id": package_id, "objective": objective,
                "source_root": source_root, "preferred_experts": preferences,
                "required_reviews": reviews,
            })
        created = super().create(
            objective=objective, profile_id=profile_id, package_id=package_id,
            constraints=constraints, criteria=criteria, block=block, method=method,
            source_root=source_root, baseline_reference=baseline_reference,
            request_id=stable_request)
        if created.get("status") != "ok":
            return created
        with self._lock:
            work = self._load_controlled(created["work"]["work_id"])
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            existing = work.get("collaboration")
            requested = {
                "preferred_experts": preferences,
                "required_reviews": reviews,
            }
            if isinstance(existing, dict):
                if existing.get("requested") != requested:
                    return self._error(
                        "EAP4_CREATE_REQUEST_CONFLICT",
                        "idempotent request has different Expert preferences")
                return {"status": "ok", "idempotent": True,
                        "work": self._public(work)}
            work["collaboration"] = {
                "schema_version": EAP4_SCHEMA_VERSION,
                "requested": requested,
                "catalog": self._expert_catalog.descriptors(),
                "catalog_digest": digest_json(self._expert_catalog.descriptors()),
                "selected_experts": [], "plans": [], "responses": [],
                "reviews": [], "conflicts": [], "integrations": [],
                "format_corrections_used": 0,
            }
            self._append(work, "EAP4_COLLABORATION_CREATED", {
                "catalog_digest": work["collaboration"]["catalog_digest"],
                **requested,
            })
            return {"status": "ok", "idempotent": bool(created.get("idempotent")),
                    "work": self._public(work)}

    def preview_authorization(self, work_id: str, **kwargs: Any) -> dict[str, Any]:
        dispatch_limits = kwargs.pop("dispatch_limits", None)
        if dispatch_limits is not None:
            expected = {"mainline", "format_correction",
                        "package_conformance_revision"}
            if not isinstance(dispatch_limits, dict) or set(dispatch_limits) != expected \
                    or any(type(value) is not int or value < 0
                           for value in dispatch_limits.values()):
                return self._error("EAP4_DISPATCH_LIMITS_INVALID",
                                   "three nonnegative dispatch class limits are required")
        result = super().preview_authorization(work_id, **kwargs)
        if result.get("status") != "ok":
            return result
        with self._lock:
            work = self._load_controlled(work_id)
            if isinstance(work, WorkError):
                return self._error(work.code, work.message)
            collaboration = work.get("collaboration")
            if not isinstance(collaboration, dict):
                return result
            requested = collaboration["requested"]
            allowed = requested["preferred_experts"] or [
                row["expert_id"] for row in collaboration["catalog"]]
            preview = work["authorization_preview"]
            preview["multi_expert"] = {
                "schema_version": EAP4_SCHEMA_VERSION,
                "allowed_experts": allowed,
                "required_reviews": list(requested["required_reviews"]),
                "catalog_digest": collaboration["catalog_digest"],
                "descriptor_digests": {
                    expert_id: self._expert_catalog.get(expert_id).descriptor_digest
                    for expert_id in allowed},
            }
            if dispatch_limits is not None:
                preview["multi_expert"]["dispatch_limits"] = dict(dispatch_limits)
            preview["authorization_digest"] = digest_json({
                key: value for key, value in preview.items()
                if key != "authorization_digest"})
            self._append(work, "EAP4_AUTHORIZATION_PREVIEWED", {
                "authorization_digest": preview["authorization_digest"],
                "allowed_experts": allowed,
                "required_reviews": requested["required_reviews"],
            })
            return {"status": "ok", "authorization": json.loads(json.dumps(preview)),
                    "execution_allowed": False}

    def _dispatch_class_remaining(self, work: dict[str, Any],
                                  dispatch_class: str) -> int | None:
        limits = (((work.get("authorization") or {}).get("multi_expert") or {})
                  .get("dispatch_limits"))
        if not isinstance(limits, dict):
            return None
        used = sum((row.get("dispatch_class") or "mainline") == dispatch_class
                   for row in work.get("role_calls") or [])
        return int(limits[dispatch_class]) - used

    def _class_can_spend(self, work: dict[str, Any], dispatch_class: str,
                         count: int = 1) -> bool:
        remaining = self._dispatch_class_remaining(work, dispatch_class)
        return remaining is None or remaining >= count

    def _call_role(self, work: dict[str, Any], role: str, purpose: str,
                   payload: dict[str, Any], *,
                   dispatch_class: str = "mainline") -> dict[str, Any]:
        if not self._class_can_spend(work, dispatch_class):
            self._budget_wait(work, f"{dispatch_class} provider dispatch sublimit exhausted")
            return {"kind": "STOP", "reason_code": "PROVIDER_SUBLIMIT_EXHAUSTED",
                    "message": "dispatch sublimit exhausted"}
        response = super()._call_role(
            work, role, purpose, payload, dispatch_class=dispatch_class)
        if role == "global" and work["status"] not in {
                "WAITING_USER", "UNCONFIRMED"}:
            self._reject_conflicting_global_claims(work, purpose, payload, response)
        return response

    def _reject_conflicting_global_claims(
        self, work: dict[str, Any], purpose: str,
        payload: dict[str, Any], response: dict[str, Any],
    ) -> None:
        """模型可決策，但不可更動既有權威或證據關聯。"""
        authority_fields = set(response) & {
            "authorization_id", "scope", "member_id", "project_id",
            "task_id", "grant_id", "host_binding", "decision_id",
            "revision", "expert_plan_digest", "proposal_id", "attempt_id",
            "work_id", "authorization_digest"}
        expected: dict[str, Any] = {}
        if purpose == "expert_integration":
            latest = payload.get("latest_proposal") or {}
            reviews = [row for row in payload.get("expert_responses") or []
                       if row.get("purpose") == "proposal_review"]
            expected = {
                "proposal_digest": latest.get("proposal_digest"),
                "review_response_digest": (reviews[-1] if reviews else {}).get(
                    "response_digest"),
                "based_on_expert_response_digests": payload.get(
                    "expert_response_digests"),
                "based_on_domain_response_digest": latest.get("response_digest"),
            }
        elif purpose == "multi_expert_result_decision":
            digests = payload.get("expert_response_digests") or []
            expected = {
                "based_on_expert_response_digests": digests,
                "based_on_domain_response_digest": digest_json({
                    "expert_response_digests": digests}),
                "outcome_digest": (payload.get("attempt") or {}).get(
                    "outcome_digest"),
            }
        elif purpose == "result_decision":
            expected = {"based_on_domain_response_digest": payload.get(
                "domain_response_digest")}
        mismatched = sorted(
            {key for key, value in expected.items()
             if key in response and response[key] != value}
            | {key for key in response if (key.endswith("_digest")
                 or key.endswith("_digests")) and key not in expected
                 and key not in authority_fields})
        if not authority_fields and not mismatched:
            return
        is_integration = purpose == "expert_integration"
        reason = ("GLOBAL_INTEGRATION_AUTHORITY_REJECTED" if is_integration
                  else "GLOBAL_DECISION_AUTHORITY_REJECTED") if authority_fields else (
                  "GLOBAL_INTEGRATION_LINEAGE_MISMATCH" if is_integration
                  else "GLOBAL_DECISION_LINEAGE_MISMATCH")
        fields = sorted(authority_fields) if authority_fields else mismatched
        work["status"] = "UNCONFIRMED"
        work["stop_reason"] = reason
        work["pending"] = {"kind": reason, "fields": fields}
        self._append(work, "GLOBAL_DECISION_CLAIM_REJECTED", {
            "purpose": purpose, "response_digest": digest_json(response),
            "fields": fields})

    def _maybe_correct_role_format(
        self, work: dict[str, Any], role: str, purpose: str,
        payload: dict[str, Any], call: dict[str, Any], error: Exception,
    ) -> dict[str, Any] | None:
        if role != "global" or purpose not in {
                "plan", "domain_response", "result_decision",
                "expert_integration", "multi_expert_result_decision"} \
                or call.get("dispatch_class") != "mainline":
            return None
        if work["control_generation"] != call["control_generation"] \
                or work["status"] in TERMINAL_STATUSES:
            return None
        rejected = getattr(error, "rejected_response", None)
        if isinstance(rejected, dict):
            self._reject_conflicting_global_claims(work, purpose, payload, rejected)
            if work["status"] == "UNCONFIRMED":
                return None
        code = call.get("error_code")
        message = str(call.get("error_message") or "")
        correctable = (
            code == "PROVIDER_RESPONSE_JSON_INVALID"
            or (code == "PROVIDER_RESPONSE_VALIDATION_FAILED"
                and (message.startswith("kind must be one of")
                     or message in {"normalized response must be an object",
                                    f"{purpose} requires non-empty message",
                                    "REQUEST_REVISION requires target_expert_id",
                                    "DELEGATE requires question",
                                    "multi-Expert result decision requires boolean criteria_met",
                                    f"{purpose} requires non-empty reason_code"})))
        collaboration = work.get("collaboration") or {}
        remaining = self._dispatch_class_remaining(work, "format_correction")
        if not correctable or (remaining is None and int(
                collaboration.get("format_corrections_used", 0)) >= 1):
            return None
        if not self._class_can_spend(work, "format_correction") \
                or not self._can_spend(work, {"provider_dispatches": 1}):
            return None
        collaboration["format_corrections_used"] = int(
            collaboration.get("format_corrections_used", 0)) + 1
        correction = {
            "attempt": collaboration["format_corrections_used"],
            "error_code": code, "error_message": message,
            "rejected_response_digest": call.get("rejected_response_digest"),
            "rejected_response_fields": call.get("rejected_response_fields"),
            "rejected_response_kind": call.get("rejected_response_kind"),
            "instruction": (
                "修正 JSON／最外層決策格式；保留工程判斷，不自創或改寫 proposal、"
                "review、outcome、scope、digest 與授權資料。只回傳此階段的合法決策欄位。"),
            "allowed_kinds": sorted({
                "plan": {"DELEGATE", "WAIT_USER", "STOP"},
                "domain_response": {"SUBMIT_PROPOSAL", "REQUEST_REVISION",
                                    "COLLECT_EVIDENCE", "WAIT_USER", "STOP"},
                "result_decision": {"STOP", "REQUEST_REVISION",
                                    "COLLECT_EVIDENCE", "WAIT_USER"},
                "expert_integration": {"SUBMIT_PROPOSAL", "REQUEST_REVISION",
                                       "COLLECT_EVIDENCE", "WAIT_USER", "STOP"},
                "multi_expert_result_decision": {"STOP", "REQUEST_REVISION",
                                                 "COLLECT_EVIDENCE", "WAIT_USER"},
            }[purpose]),
            "proposal_digest": (payload.get("latest_proposal") or {}).get(
                "proposal_digest"),
            "expert_response_digests": list(
                payload.get("expert_response_digests") or []),
            "outcome_digest": (payload.get("attempt") or {}).get(
                "outcome_digest"),
        }
        self._append(work, "GLOBAL_FORMAT_CORRECTION_REQUIRED", {
            "request_id": call["request_id"], "error_code": code,
            "format_corrections_used": collaboration["format_corrections_used"],
        })
        return self._call_role(
            work, role, purpose, {**payload, "format_correction": correction},
            dispatch_class="format_correction")

    def _plan_or_delegate(self, work: dict[str, Any]) -> None:
        if not self._catalog_matches_work(work):
            self._stop_for_catalog_mismatch(work)
            return
        pending = work.get("pending") or {}
        kind = pending.get("kind")
        if kind == "EAP4_EXPERT_SEQUENCE":
            self._advance_expert_sequence(work)
            return
        if kind == "EAP4_GLOBAL_INTEGRATION":
            self._integrate_expert_responses(work)
            return
        if not isinstance(work.get("collaboration"), dict) or kind == "EXECUTE_APPROVED":
            super()._plan_or_delegate(work)
            return
        if kind == "DOMAIN_REVISION" and work["attempts"]:
            prior_call = next((row for row in reversed(work["role_calls"])
                               if row.get("purpose") in {"proposal", "proposal_revision"}
                               and row.get("expert_id")), None)
            expert_id = (prior_call or {}).get("expert_id")
            descriptor = (self._expert_catalog.get(expert_id)
                          if expert_id else None)
            if descriptor is None or "proposal_revision" not in descriptor.purposes:
                work["status"] = "WAITING_USER"
                work["pending"] = {"kind": "EXPERT_CAPABILITY_MISSING",
                                   "reason_code": "PROPOSAL_REVISION_EXPERT_MISSING"}
                return
            attempt = work["attempts"][-1]
            response = self._call_expert(
                work, descriptor, "proposal_revision", {
                    "revision_request": pending,
                    "prior_proposal": pending.get("prior_proposal"),
                    "attempt": attempt,
                    "expert_analyses": attempt.get("expert_analyses") or [],
                    "history": self._history_context(work),
                    "objective": work["objective"], "criteria": work["criteria"],
                    "constraints": work["constraints"],
                    "package": work["package"],
                    "requested_method": work.get("requested_method"),
                    "authorization_scope": {
                        "allowed_methods": list((work.get("authorization") or {}).get(
                            "allowed_methods") or ()),
                        "allowed_changes": (work.get("authorization") or {}).get(
                            "allowed_changes"),
                    },
                }, call_key=f"result-revision-{attempt['attempt_id']}-{work['revision']}",
                parent_digest=attempt.get("outcome_digest"))
            if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
                return
            work["pending"] = None
            self._consume_domain(work, response)
            return
        if kind == "GLOBAL_AFTER_DOMAIN" and work["attempts"]:
            attempt = work["attempts"][-1]
            decision = self._call_role(work, "global", "domain_response", {
                "domain_response": pending["domain_response"],
                "domain_response_digest": pending["domain_response_digest"],
                "attempt": attempt,
                "expert_analyses": attempt.get("expert_analyses") or [],
                "objective": work["objective"], "criteria": work["criteria"],
                "constraints": work["constraints"],
                "stop_conditions": (work.get("authorization") or {}).get(
                    "stop_conditions") or [],
                "history": self._history_context(work),
            })
            if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
                return
            work["pending"] = None
            self._consume_global(work, decision, pending["domain_response_digest"])
            return
        if kind in {"DOMAIN_REVISION", "GLOBAL_AFTER_DOMAIN"}:
            super()._plan_or_delegate(work)
            return
        collaboration = work["collaboration"]
        allowed = (((work.get("authorization") or {}).get("multi_expert") or {}).get(
            "allowed_experts") or collaboration["requested"]["preferred_experts"])
        available = [row for row in collaboration["catalog"]
                     if not allowed or row["expert_id"] in allowed]
        decision = self._call_role(work, "global", "plan", {
            "objective": work["objective"], "criteria": work["criteria"],
            "constraints": work["constraints"],
            "requested_method": work.get("requested_method"),
            "package": work["package"], "baseline": work["baseline"],
            "history": self._history_context(work),
            "available_experts": available,
            "required_reviews": collaboration["requested"]["required_reviews"],
        })
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        self._consume_global(work, decision, None)

    def _consume_global(
        self, work: dict[str, Any], decision: dict[str, Any],
        based_on_domain_digest: str | None,
    ) -> None:
        if decision.get("kind") != "DELEGATE" or not isinstance(
                work.get("collaboration"), dict):
            super()._consume_global(work, decision, based_on_domain_digest)
            return
        try:
            plan = self._resolve_expert_plan(work, decision)
        except WorkError as exc:
            work["status"] = "WAITING_USER"
            work["pending"] = {"kind": "EXPERT_CAPABILITY_MISSING",
                               "reason_code": exc.code, "message": exc.message,
                               "available": self._expert_catalog.descriptors()}
            self._append(work, "EXPERT_SELECTION_REJECTED", work["pending"])
            return
        stored = self._store_global_decision(
            work, decision, based_on_domain_digest,
            based_on_expert_digests=[])
        collaboration = work["collaboration"]
        collaboration["plans"].append(plan)
        for request in plan["requests"]:
            descriptor = self._expert_catalog.get(request["expert_id"])
            if descriptor.expert_id not in collaboration["selected_experts"]:
                collaboration["selected_experts"].append(descriptor.expert_id)
        work["pending"] = {
            "kind": "EAP4_EXPERT_SEQUENCE", "plan": plan, "index": 0,
            "responses": [], "global_decision_id": stored["decision_id"],
        }
        work["status"] = "ANALYZING"
        self._append(work, "EAP4_EXPERT_PLAN_SELECTED", {
            "plan_digest": plan["plan_digest"],
            "expert_ids": [row["expert_id"] for row in plan["requests"]],
        })

    def _resolve_expert_plan(
        self, work: dict[str, Any], decision: dict[str, Any],
    ) -> dict[str, Any]:
        collaboration = work["collaboration"]
        authorization = work.get("authorization") or {}
        multi = authorization.get("multi_expert") or {}
        allowed = tuple(multi.get("allowed_experts") or
                        collaboration["requested"]["preferred_experts"])
        raw = decision.get("expert_requests")
        requests: list[dict[str, Any]] = []
        if isinstance(raw, list) and raw:
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise WorkError("EXPERT_PLAN_INVALID", "request must be an object")
                expert_id = str(item.get("expert_id") or "")
                purpose = str(item.get("purpose") or "")
                descriptor = self._expert_catalog.get(expert_id)
                if allowed and expert_id not in allowed:
                    raise WorkError("EXPERT_OUT_OF_SCOPE", expert_id)
                writes_proposal = purpose in {"proposal", "proposal_revision"}
                if purpose not in descriptor.purposes or (
                        writes_proposal
                        and descriptor.capability != work["package"]["capability"]):
                    raise WorkError("EXPERT_PURPOSE_NOT_SUPPORTED", f"{expert_id}/{purpose}")
                depends = list(item.get("depends_on") or [])
                requests.append({
                    "call_key": str(item.get("call_key") or f"expert-{index + 1}"),
                    "expert_id": expert_id, "purpose": purpose,
                    "question": str(item.get("question") or decision.get("question") or ""),
                    "depends_on": depends,
                })
        else:
            primary = self._expert_catalog.select(
                capability=work["package"]["capability"], purpose="proposal",
                objective=work["objective"], allowed_experts=allowed)
            requests.append({"call_key": "proposal", "expert_id": primary.expert_id,
                             "purpose": "proposal", "question": decision.get("question") or "",
                             "depends_on": []})
            if "proposal_review" in collaboration["requested"]["required_reviews"]:
                reviewers = [row for row in self._expert_catalog.supporting(
                    purpose="proposal_review", allowed_experts=allowed)
                    if row.expert_id != primary.expert_id]
                if not reviewers:
                    raise WorkError(
                        "EXPERT_CAPABILITY_NOT_AVAILABLE", "proposal_review")
                requests.append({"call_key": "proposal-review",
                                 "expert_id": reviewers[0].expert_id,
                                 "purpose": "proposal_review",
                                 "question": "審查方案與比較依據",
                                 "depends_on": ["proposal"]})
        if not any(row["purpose"] in {"proposal", "proposal_revision"}
                   for row in requests):
            raise WorkError("EXPERT_PLAN_INVALID", "one proposal Expert is required")
        required_reviews = collaboration["requested"]["required_reviews"]
        if "proposal_review" in required_reviews and not any(
                row["purpose"] == "proposal_review" for row in requests):
            proposal_request = next(row for row in requests
                                    if row["purpose"] in {
                                        "proposal", "proposal_revision"})
            reviewers = [descriptor for descriptor in self._expert_catalog.supporting(
                purpose="proposal_review", allowed_experts=allowed)
                if descriptor.expert_id != proposal_request["expert_id"]]
            if not reviewers:
                raise WorkError(
                    "EXPERT_CAPABILITY_NOT_AVAILABLE", "proposal_review")
            requests.append({
                "call_key": "required-proposal-review",
                "expert_id": reviewers[0].expert_id,
                "purpose": "proposal_review",
                "question": "完成 parent authorization 要求的方案審查",
                "depends_on": [proposal_request["call_key"]],
            })
        keys: set[str] = set()
        for request in requests:
            if not request["call_key"] or request["call_key"] in keys:
                raise WorkError("EXPERT_PLAN_INVALID", "call_key must be unique")
            if any(dependency not in keys for dependency in request["depends_on"]):
                raise WorkError("EXPERT_DEPENDENCY_INVALID", request["call_key"])
            keys.add(request["call_key"])
        normalized = {"schema_version": "expert_plan.v1",
                      "revision": work["revision"], "requests": requests}
        normalized["plan_digest"] = digest_json(normalized)
        return normalized

    def _advance_expert_sequence(self, work: dict[str, Any]) -> None:
        pending = work["pending"]
        plan = pending["plan"]
        index = int(pending["index"])
        if index >= len(plan["requests"]):
            work["pending"] = {
                "kind": "EAP4_GLOBAL_INTEGRATION",
                "responses": pending["responses"],
                "plan_digest": plan["plan_digest"],
            }
            self._append(work, "EAP4_EXPERT_SEQUENCE_COMPLETED", {
                "plan_digest": plan["plan_digest"],
                "response_digests": [row["response_digest"]
                                     for row in pending["responses"]],
            })
            # Engineering Work 的 no-progress gate 只觀察狀態、revision、role call 與
            # attempt 數；同一 ANALYZING 狀態內只改 pending 會被視為停滯。
            # 因此在這個確定性邊界直接進 Global integration，讓本次
            # transition 留下實際 role call，而不是要求外部 continue。
            self._integrate_expert_responses(work)
            return
        request = plan["requests"][index]
        dependencies = [row for row in pending["responses"]
                        if row["call_key"] in request["depends_on"]]
        descriptor = self._expert_catalog.get(request["expert_id"])
        latest = work["proposals"][-1] if work["proposals"] else None
        payload = {
            "question": request["question"], "objective": work["objective"],
            "criteria": work["criteria"], "constraints": work["constraints"],
            "package": work["package"], "evidence": self._evidence_context(work),
            "history": self._history_context(work),
            "dependencies": dependencies,
            "reviewed_proposal": latest,
            "review_of_proposal_digest": (
                latest.get("proposal_digest") if isinstance(latest, dict) else None),
            "authorization_scope": {
                "allowed_methods": list((work.get("authorization") or {}).get(
                    "allowed_methods") or ()),
                "allowed_changes": (work.get("authorization") or {}).get(
                    "allowed_changes"),
            },
        }
        response = self._call_expert(
            work, descriptor, request["purpose"], payload,
            call_key=request["call_key"],
            parent_digest=(latest or {}).get("proposal_digest"),
            dispatch_class=str(request.get("dispatch_class") or "mainline"))
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        response_digest = digest_json(response)
        package_revision_required = False
        record = {
            "call_key": request["call_key"], "expert_id": descriptor.expert_id,
            "purpose": request["purpose"], "response": response,
            "response_digest": response_digest,
            "review_of_proposal_digest": payload["review_of_proposal_digest"],
        }
        if request["purpose"] in {"proposal", "proposal_revision"}:
            self._consume_domain(work, response)
            if work["status"] in TERMINAL_STATUSES | {"WAITING_USER", "UNCONFIRMED"}:
                return
            if response.get("kind") == "PROPOSAL" and work["proposals"]:
                proposal_record = work["proposals"][-1]
                record["response"] = proposal_record["response"]
                record["proposal_digest"] = proposal_record["proposal_digest"]
                record["response_digest"] = proposal_record["response_digest"]
                if (proposal_record.get("validation") or {}).get("status") != "PASSED":
                    record["validation"] = proposal_record["validation"]
                    package_revision_required = True
            elif response.get("kind") == "NEED_EVIDENCE" and work["evidence_sets"]:
                record["collected_evidence_digest"] = digest_json(
                    work["evidence_sets"][-1])
            work["pending"] = pending
            work["status"] = "ANALYZING"
        elif request["purpose"] in {"proposal_review", "evidence_review"}:
            expected = payload["review_of_proposal_digest"]
            if response.get("kind") == "REVIEW" and (
                    not isinstance(expected, str)
                    or response.get("review_of_proposal_digest") != expected):
                work["status"] = "UNCONFIRMED"
                work["stop_reason"] = "EXPERT_REVIEW_TARGET_MISMATCH"
                self._append(work, "EXPERT_REVIEW_REJECTED", {
                    "expert_id": descriptor.expert_id,
                    "expected": expected,
                    "actual": response.get("review_of_proposal_digest")})
                return
            work["collaboration"]["reviews"].append(record)
            alternative = response.get("alternative")
            if isinstance(alternative, dict):
                conflict = {
                    "review_response_digest": response_digest,
                    "review_of_proposal_digest": expected,
                    "alternative_digest": digest_json(alternative),
                    "resolution": "UNRESOLVED_NO_MERGE",
                }
                work["collaboration"]["conflicts"].append(conflict)
                self._append(work, "EXPERT_CONFLICT_RECORDED", conflict)
            if response.get("kind") == "NEED_EVIDENCE":
                self._collect(work, response.get("request") or {})
                if work["status"] in TERMINAL_STATUSES | {
                        "WAITING_USER", "UNCONFIRMED"}:
                    return
                if work["evidence_sets"]:
                    collected = work["evidence_sets"][-1]
                    record["collected_evidence_digest"] = digest_json(collected)
                    record["response"] = {**response,
                                          "collected_evidence": collected}
                    record["response_digest"] = digest_json(record["response"])
                work["pending"] = pending
                work["status"] = "ANALYZING"
        pending["responses"].append(record)
        work["collaboration"]["responses"].append(record)
        pending["index"] = index + 1
        self._append(work, "EXPERT_RESPONSE_ACCEPTED", {
            "call_key": request["call_key"], "expert_id": descriptor.expert_id,
            "purpose": request["purpose"],
            "response_digest": record["response_digest"],
            "proposal_digest": record.get("proposal_digest"),
        })
        if package_revision_required:
            self._schedule_package_revision(work, descriptor, record)

    def _schedule_package_revision(
        self, work: dict[str, Any], descriptor: ExpertCapabilityDescriptor,
        rejected: dict[str, Any],
    ) -> None:
        """把 Package 拒絕原因完整交回原 Expert，再審查修正後版本。"""
        if "proposal_revision" not in descriptor.purposes:
            work["status"] = "WAITING_USER"
            work["pending"] = {
                "kind": "EXPERT_CAPABILITY_MISSING",
                "reason_code": "PACKAGE_PROPOSAL_REVISION_EXPERT_MISSING",
                "expert_id": descriptor.expert_id,
            }
            self._append(work, "EAP4_PACKAGE_REVISION_UNAVAILABLE", work["pending"])
            return
        correction_key = f"package-revision-{len(work['proposals']) + 1}"
        validation = rejected.get("validation") or {}
        requests = [{
            "call_key": correction_key,
            "expert_id": descriptor.expert_id,
            "purpose": "proposal_revision",
            "dispatch_class": "package_conformance_revision",
            "question": canonical_json({
                "instruction": "依 Package 正式格式修正；不可猜值、刪欄位或擴張授權",
                "reason_code": validation.get("reason_code"),
                "message": validation.get("message"),
                "validation": validation,
                "package_contract": work["package"].get("proposal_contract"),
                "requested_method": work.get("requested_method"),
                "authorization_scope": {
                    "allowed_methods": list((work.get("authorization") or {}).get(
                        "allowed_methods") or ()),
                    "allowed_changes": (work.get("authorization") or {}).get(
                        "allowed_changes"),
                },
                "rejected_proposal_digest": rejected.get("proposal_digest"),
            }),
            "depends_on": [],
        }]
        selected = work["collaboration"]["selected_experts"]
        reviewer = next((self._expert_catalog.get(expert_id)
                         for expert_id in selected
                         if expert_id != descriptor.expert_id
                         and "proposal_review" in self._expert_catalog.get(
                             expert_id).purposes), None)
        if reviewer is not None:
            requests.append({
                "call_key": f"package-review-{len(work['proposals']) + 1}",
                "expert_id": reviewer.expert_id,
                "purpose": "proposal_review",
                "question": "審查 Package 格式修正後的方案",
                "depends_on": [correction_key],
            })
        plan = {"schema_version": "expert_plan.v1", "revision": work["revision"],
                "requests": requests,
                "package_revision_of": rejected.get("proposal_digest")}
        plan["plan_digest"] = digest_json(plan)
        required_dispatches = len(requests) + 1
        if (not self._can_spend(
                work, {"provider_dispatches": required_dispatches})
                or not self._class_can_spend(
                    work, "package_conformance_revision")
                or not self._class_can_spend(
                    work, "mainline", len(requests))):
            self._budget_wait(
                work,
                "package revision requires an authorized revision dispatch "
                "including review and integration budget and sublimits",
            )
            self._append(work, "EAP4_PACKAGE_REVISION_DEFERRED_BUDGET", {
                "expert_id": descriptor.expert_id,
                "rejected_proposal_digest": rejected.get("proposal_digest"),
                "required_provider_dispatches": required_dispatches,
            })
            return
        work["collaboration"]["plans"].append(plan)
        work["pending"] = {"kind": "EAP4_EXPERT_SEQUENCE", "plan": plan,
                           "index": 0, "responses": []}
        work["status"] = "ANALYZING"
        self._append(work, "EAP4_PACKAGE_REVISION_REQUIRED", {
            "plan_digest": plan["plan_digest"],
            "expert_id": descriptor.expert_id,
            "rejected_proposal_digest": rejected.get("proposal_digest"),
            "reason_code": validation.get("reason_code"),
        })

    def _integrate_expert_responses(self, work: dict[str, Any]) -> None:
        pending = work["pending"]
        responses = pending["responses"]
        response_digests = [row["response_digest"] for row in responses]
        latest = work["proposals"][-1] if work["proposals"] else None
        decision = self._call_role(work, "global", "expert_integration", {
            "objective": work["objective"], "criteria": work["criteria"],
            "expert_responses": responses,
            "expert_response_digests": response_digests,
            "latest_proposal": latest,
            "conflicts": work["collaboration"]["conflicts"],
            "history": self._history_context(work),
        })
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        if decision.get("kind") not in GLOBAL_DECISIONS - {"DELEGATE", "EXECUTE"}:
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "GLOBAL_EXPERT_INTEGRATION_INVALID"
            self._append(work, "GLOBAL_INVALID", {
                "response_digest": digest_json(decision)})
            return
        authority_fields = set(decision) & {
            "authorization_id", "scope", "member_id", "project_id",
            "task_id", "grant_id", "host_binding"}
        if authority_fields:
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "GLOBAL_INTEGRATION_AUTHORITY_REJECTED"
            work["pending"] = {"kind": work["stop_reason"],
                               "fields": sorted(authority_fields)}
            self._append(work, "GLOBAL_INTEGRATION_AUTHORITY_REJECTED", {
                "response_digest": digest_json(decision),
                "fields": sorted(authority_fields)})
            return
        latest_review = next((row for row in reversed(responses)
                              if row.get("purpose") == "proposal_review"), None)
        claimed = {
            "proposal_digest": (latest or {}).get("proposal_digest"),
            "review_response_digest": (latest_review or {}).get("response_digest"),
            "based_on_expert_response_digests": response_digests,
            "based_on_domain_response_digest": (latest or {}).get("response_digest"),
        }
        mismatched = sorted(key for key, expected in claimed.items()
                            if key in decision and decision[key] != expected)
        if mismatched:
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "GLOBAL_INTEGRATION_LINEAGE_MISMATCH"
            work["pending"] = {"kind": work["stop_reason"],
                               "fields": mismatched}
            self._append(work, "GLOBAL_INTEGRATION_LINEAGE_REJECTED", {
                "response_digest": digest_json(decision),
                "fields": mismatched})
            return
        decision = {**decision,
                    "based_on_expert_response_digests": response_digests,
                    "expert_plan_digest": pending["plan_digest"]}
        proposal_digest = ((latest or {}).get("response_digest")
                           if isinstance(latest, dict) else None)
        if decision["kind"] == "REQUEST_REVISION":
            stored = self._store_global_decision(
                work, decision, proposal_digest,
                based_on_expert_digests=response_digests)
            self._schedule_revision_sequence(work, decision, stored)
            return
        work["collaboration"]["integrations"].append({
            "decision_digest": digest_json(decision),
            "response_digests": response_digests,
        })
        work["pending"] = None
        super()._consume_global(work, decision, proposal_digest)

    def _schedule_revision_sequence(
        self, work: dict[str, Any], decision: dict[str, Any],
        stored: dict[str, Any],
    ) -> None:
        # 有用途子上限的 plan-only qualification 不把工程修訂偽裝成格式修正。
        limits = (((work.get("authorization") or {}).get("multi_expert") or {})
                  .get("dispatch_limits"))
        if isinstance(limits, dict):
            work["status"] = "WAITING_USER"
            work["pending"] = {
                "kind": "ENGINEERING_REVISION_NOT_AUTHORIZED",
                "reason_code": "ENGINEERING_REVISION_NOT_AUTHORIZED",
                "decision_id": stored["decision_id"],
            }
            self._append(work, "EAP4_REVISION_DEFERRED_SCOPE", work["pending"])
            return
        selected = work["collaboration"]["selected_experts"]
        target = str(decision.get("target_expert_id") or "")
        if not target:
            target = next((expert_id for expert_id in selected
                           if "proposal_revision" in self._expert_catalog.get(
                               expert_id).purposes), "")
        descriptor = self._expert_catalog.get(target)
        if "proposal_revision" not in descriptor.purposes:
            work["status"] = "WAITING_USER"
            work["pending"] = {"kind": "EXPERT_CAPABILITY_MISSING",
                               "reason_code": "PROPOSAL_REVISION_EXPERT_MISSING"}
            return
        requests = [{
            "call_key": f"revision-{work['revision']}-{len(work['proposals']) + 1}",
            "expert_id": target, "purpose": "proposal_revision",
            "question": str(decision.get("message") or "修訂方案"),
            "depends_on": [],
        }]
        for reviewer_id in selected:
            reviewer = self._expert_catalog.get(reviewer_id)
            if reviewer_id != target and "proposal_review" in reviewer.purposes:
                requests.append({
                    "call_key": f"review-{work['revision']}-{len(work['proposals']) + 1}",
                    "expert_id": reviewer_id, "purpose": "proposal_review",
                    "question": "審查修訂方案", "depends_on": [requests[0]["call_key"]],
                })
                break
        plan = {"schema_version": "expert_plan.v1", "revision": work["revision"],
                "requests": requests, "revision_of_decision_id": stored["decision_id"]}
        plan["plan_digest"] = digest_json(plan)
        required_dispatches = len(requests) + 1
        if not self._can_spend(
                work, {"provider_dispatches": required_dispatches}):
            self._budget_wait(
                work,
                "revision requires "
                f"{required_dispatches} dispatches including re-review and integration",
            )
            self._append(work, "EAP4_REVISION_DEFERRED_BUDGET", {
                "decision_id": stored["decision_id"],
                "target_expert_id": target,
                "required_provider_dispatches": required_dispatches,
            })
            return
        work["collaboration"]["plans"].append(plan)
        work["pending"] = {"kind": "EAP4_EXPERT_SEQUENCE", "plan": plan,
                           "index": 0, "responses": [],
                           "global_decision_id": stored["decision_id"]}
        work["status"] = "ANALYZING"
        self._append(work, "EAP4_REVISION_SEQUENCE_REQUIRED", {
            "decision_id": stored["decision_id"],
            "plan_digest": plan["plan_digest"], "target_expert_id": target})

    def _submit_latest(self, work: dict[str, Any]) -> None:
        if (isinstance(work.get("collaboration"), dict) and not work["attempts"]
                and (work.get("authorization") or {}).get("mode") != "PLAN_ONLY"):
            required = len(self._result_experts(work)) + 1
            if not self._can_spend(work, {"provider_dispatches": required}):
                self._budget_wait(
                    work, f"attempt requires {required} multi-Expert result dispatches")
                return
        super()._submit_latest(work)

    def _analyze_result(self, work: dict[str, Any]) -> None:
        if not isinstance(work.get("collaboration"), dict):
            super()._analyze_result(work)
            return
        if not self._catalog_matches_work(work):
            self._stop_for_catalog_mismatch(work)
            return
        attempt = work["attempts"][-1]
        responses: list[dict[str, Any]] = []
        for descriptor, purpose in self._result_experts(work):
            payload = {
                "attempt": attempt, "history": self._history_context(work),
                "criteria": work["criteria"], "prior_analyses": responses,
                "outcome_digest": attempt.get("outcome_digest"),
            }
            response = self._call_expert(
                work, descriptor, purpose, payload,
                call_key=f"result-{attempt['attempt_id']}-{descriptor.expert_id}",
                parent_digest=attempt.get("outcome_digest"))
            if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
                return
            responses.append({
                "expert_id": descriptor.expert_id, "purpose": purpose,
                "response": response, "response_digest": digest_json(response),
                "outcome_digest": attempt.get("outcome_digest"),
            })
        digests = [row["response_digest"] for row in responses]
        attempt["expert_analyses"] = responses
        attempt["expert_analysis_digests"] = digests
        self._append(work, "EAP4_RESULT_ANALYSES", {
            "attempt_id": attempt["attempt_id"], "response_digests": digests})
        decision = self._call_role(work, "global", "multi_expert_result_decision", {
            "attempt": attempt, "expert_analyses": responses,
            "expert_response_digests": digests,
            "objective": work["objective"],
            "criteria": work["criteria"],
            "constraints": work["constraints"],
            "stop_conditions": (work.get("authorization") or {}).get(
                "stop_conditions") or [],
            "authorization_context": self._result_authorization_context(
                work, attempt, responses),
            "history": self._history_context(work),
        })
        if work["status"] in {"WAITING_USER", "UNCONFIRMED"}:
            return
        decision = {**decision, "based_on_expert_response_digests": digests}
        collection_digest = digest_json({"expert_response_digests": digests})
        attempt["domain_analysis"] = responses[0]["response"]
        attempt["domain_analysis_digest"] = responses[0]["response_digest"]
        attempt["expert_analysis_collection_digest"] = collection_digest
        self._consume_global(work, decision, collection_digest)

    def _result_authorization_context(
        self, work: dict[str, Any], attempt: dict[str, Any],
        responses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """結果決策所需的現行授權事實：有效性、各用途剩餘額度與變更上限。

        數值全部由 Runtime 狀態計算，不由模型推導；`after_this_decision` 已扣除
        本次 Global 結果決策自身的一次主線 dispatch。
        """
        authorization = work.get("authorization") or {}
        budget = self._prepare_public_budget(work)
        remaining = dict(budget["remaining"])
        dispatch_limits = ((authorization.get("multi_expert") or {})
                           .get("dispatch_limits") or {})
        classes = {name: self._dispatch_class_remaining(work, name)
                   for name in dispatch_limits}
        after = dict(remaining)
        after["provider_dispatches"] = remaining.get("provider_dispatches", 0) - 1
        after_classes = dict(classes)
        if after_classes.get("mainline") is not None:
            after_classes["mainline"] -= 1
        changes = authorization.get("allowed_changes")
        plan = attempt.get("execution_plan") or {}
        executed = (((plan.get("canonical_proposal") or {}).get("changes")) or {})
        candidate = executed.get("candidate")
        at_bound: dict[str, Any] = {}
        if isinstance(changes, dict) and changes.get("kind") == "timing_parameter_bounds":
            try:
                value = float(candidate)
                at_bound = {"at_maximum": value >= float(changes["maximum"]),
                            "at_minimum": value <= float(changes["minimum"])}
            except (TypeError, ValueError, KeyError):
                at_bound = {"at_maximum": None, "at_minimum": None}
        task_budget = (((attempt.get("outcome") or {}).get("evidence") or {})
                       .get("flow_task") or {})
        narration_limit = _find_int(task_budget, "body_llm_dispatches")
        narration_used = _find_int(task_budget, "body_llm_dispatches_used")
        return {
            "authorization_id": authorization.get("authorization_id"),
            "authorization_digest": authorization.get("authorization_digest"),
            "mode": authorization.get("mode"),
            "status": ("ACTIVE" if authorization and work["status"] not in TERMINAL_STATUSES
                       and not self._active_time_exhausted(work) else "INACTIVE"),
            "remaining_before_this_decision": remaining,
            "remaining_after_this_decision": after,
            "dispatch_class_remaining_after_this_decision": after_classes,
            "flow_narration": {
                "per_candidate_task_limit": narration_limit,
                "used_by_this_attempt": narration_used,
                "available_to_a_new_candidate_task": narration_limit,
            },
            "allowed_methods": list(authorization.get("allowed_methods") or ()),
            "allowed_changes": changes,
            "executed_change": executed,
            "executed_change_position": at_bound,
            "expert_revision_signals": [
                {"expert_id": row.get("expert_id"), "purpose": row.get("purpose"),
                 "kind": (row.get("response") or {}).get("kind"),
                 "criteria_met": (row.get("response") or {}).get("criteria_met"),
                 "recommendation": (row.get("response") or {}).get("recommendation"),
                 "has_revision_proposal": isinstance(
                     (row.get("response") or {}).get("proposal"), dict)}
                for row in responses],
        }

    def _result_experts(
        self, work: dict[str, Any],
    ) -> list[tuple[ExpertCapabilityDescriptor, str]]:
        collaboration = work["collaboration"]
        selected = tuple(collaboration["selected_experts"] or
                         collaboration["requested"]["preferred_experts"])
        result: list[tuple[ExpertCapabilityDescriptor, str]] = []
        for expert_id in selected:
            descriptor = self._expert_catalog.get(expert_id)
            if "execution_analysis" in descriptor.purposes:
                result.append((descriptor, "execution_analysis"))
            elif "result_review" in descriptor.purposes:
                result.append((descriptor, "result_review"))
        required = collaboration["requested"]["required_reviews"]
        if "result_review" in required and not any(
                purpose == "result_review" for _, purpose in result):
            allowed = tuple(((work.get("authorization") or {}).get(
                "multi_expert") or {}).get("allowed_experts") or selected)
            reviewers = self._expert_catalog.supporting(
                purpose="result_review", allowed_experts=allowed)
            if not reviewers:
                raise WorkError(
                    "EXPERT_CAPABILITY_NOT_AVAILABLE", "result_review")
            result.append((reviewers[0], "result_review"))
        if not result:
            raise WorkError(
                "EXPERT_CAPABILITY_NOT_AVAILABLE", "execution_analysis")
        return result

    def _call_expert(
        self, work: dict[str, Any], descriptor: ExpertCapabilityDescriptor,
        purpose: str, payload: dict[str, Any], *, call_key: str,
        parent_digest: str | None, dispatch_class: str = "mainline",
    ) -> dict[str, Any]:
        if not self._class_can_spend(work, dispatch_class):
            self._budget_wait(work, f"{dispatch_class} dispatch sublimit exhausted")
            return {"kind": "UNSUPPORTED", "message": "dispatch sublimit exhausted"}
        if not self._can_spend(work, {"provider_dispatches": 1}):
            self._budget_wait(work, f"one {descriptor.expert_id} dispatch is required")
            return {"kind": "UNSUPPORTED", "message": "provider budget exhausted"}
        authorization = work.get("authorization") or {}
        multi = authorization.get("multi_expert") or {}
        allowed = multi.get("allowed_experts") or []
        if allowed and descriptor.expert_id not in allowed:
            work["status"] = "WAITING_USER"
            work["pending"] = {"kind": "AUTHORIZATION_DELTA",
                               "reason_code": "EXPERT_OUT_OF_SCOPE",
                               "expert_id": descriptor.expert_id}
            return {"kind": "UNSUPPORTED", "message": "Expert out of scope"}
        expected_descriptor = (multi.get("descriptor_digests") or {}).get(
            descriptor.expert_id)
        if expected_descriptor not in {None, descriptor.descriptor_digest}:
            work["status"] = "WAITING_USER"
            work["pending"] = {"kind": "EXPERT_VERSION_INCOMPATIBLE",
                               "expert_id": descriptor.expert_id,
                               "expected_descriptor_digest": expected_descriptor,
                               "actual_descriptor_digest": descriptor.descriptor_digest}
            self._append(work, "EXPERT_VERSION_REJECTED", work["pending"])
            return {"kind": "UNSUPPORTED", "message": "Expert version changed"}
        self._reserve(work, {"provider_dispatches": 1})
        request_id = self._new_request(work, f"expert_{descriptor.expert_id}_{purpose}")
        generation = work["control_generation"]
        evidence_refs = self._input_evidence_refs(work)
        request = {
            "expert_context": {
                **descriptor.to_json_dict(),
                "descriptor_digest": descriptor.descriptor_digest,
                "work_id": work["work_id"], "work_revision": work["revision"],
            },
            "input_evidence_refs": evidence_refs,
            "parent_digest": parent_digest,
            "payload": payload,
        }
        call = {
            "request_id": request_id, "call_key": call_key,
            "role": "domain", "expert_id": descriptor.expert_id,
            "capability": descriptor.capability, "purpose": purpose,
            "revision": work["revision"], "control_generation": generation,
            "binding_id": descriptor.binding_id,
            "input_schema_version": descriptor.input_schema_version,
            "output_schema_version": descriptor.output_schema_version,
            "adapter_version": descriptor.adapter_version,
            "implementation_version": descriptor.implementation_version,
            "descriptor_digest": descriptor.descriptor_digest,
            "input_evidence_refs": evidence_refs,
            "parent_digest": parent_digest,
            "request_digest": digest_json(request), "status": "DISPATCHED",
            "dispatch_class": dispatch_class,
            "started_at": time.time(),
        }
        work["role_calls"].append(call)
        self._append(work, "EXPERT_DISPATCHED", call)
        response: dict[str, Any] | None = None
        provider: dict[str, Any] | None = None
        try:
            response = self._experts.call(
                role="domain", capability=descriptor.capability, purpose=purpose,
                request=request, request_id=request_id)
            if not isinstance(response, dict):
                raise WorkError(
                    "PROVIDER_RESPONSE_INVALID", "provider result must be an object")
            provider = response.pop("_terminux_provider_call", None)
            self._validate_expert_response(purpose, response)
        except Exception as exc:  # noqa: BLE001 - provider failure is durable work state
            self._complete_reservation(work, {"provider_dispatches": 1})
            rejected = getattr(exc, "rejected_response", None)
            if not isinstance(rejected, dict) and isinstance(response, dict):
                rejected = response
            provider_failure = getattr(exc, "provider_metadata", None)
            if not isinstance(provider_failure, dict) and isinstance(provider, dict):
                provider_failure = provider
            response_diagnostics = getattr(exc, "response_diagnostics", None)
            call.update({"status": "FAILED", "error_type": type(exc).__name__,
                         "error_code": str(getattr(exc, "code", type(exc).__name__)),
                         "error_message": str(getattr(exc, "message", str(exc))),
                         "finished_at": time.time()})
            if isinstance(provider_failure, dict):
                call["provider"] = provider_failure
            if isinstance(response_diagnostics, dict):
                call["response_diagnostics"] = response_diagnostics
            if isinstance(rejected, dict):
                call.update({
                    "rejected_response": rejected,
                    "rejected_response_digest": digest_json(rejected),
                    "rejected_response_fields": sorted(str(key) for key in rejected),
                    "rejected_response_kind": rejected.get("kind"),
                })
            correction_errors = {
                "PROVIDER_RESPONSE_VALIDATION_FAILED",
                "PROVIDER_RESPONSE_JSON_INVALID",
            }
            collaboration = work.get("collaboration") or {}
            if (purpose in {"proposal", "proposal_revision"}
                    and dispatch_class == "mainline"
                    and call["error_code"] in correction_errors
                    and int(collaboration.get("format_corrections_used", 0)) < 1
                    and self._class_can_spend(work, "format_correction")
                    and (not isinstance(multi.get("dispatch_limits"), dict)
                         or (self._class_can_spend(work, "mainline", 2)
                             and self._can_spend(
                                 work, {"provider_dispatches": 3})))
                    and self._can_spend(work, {"provider_dispatches": 1})):
                collaboration["format_corrections_used"] = int(
                    collaboration.get("format_corrections_used", 0)) + 1
                correction = {
                    "attempt": collaboration["format_corrections_used"],
                    "error_code": call["error_code"],
                    "error_message": call["error_message"],
                    "instruction": (
                        "Return only one JSON object matching the same purpose and "
                        "Package contract. Preserve engineering content; do not invent "
                        "authority, candidate values, or evidence."),
                    "package_contract": work["package"].get("proposal_contract"),
                    "rejected_response": rejected,
                    "rejected_response_digest": call.get(
                        "rejected_response_digest"),
                    "authorization_scope": {
                        "allowed_methods": list((work.get("authorization") or {}).get(
                            "allowed_methods") or ()),
                        "allowed_changes": (work.get("authorization") or {}).get(
                            "allowed_changes"),
                    },
                }
                self._append(work, "EXPERT_FORMAT_CORRECTION_REQUIRED", {
                    "request_id": request_id,
                    "expert_id": descriptor.expert_id,
                    "purpose": purpose,
                    "error_code": call["error_code"],
                    "format_corrections_used": collaboration[
                        "format_corrections_used"],
                })
                return self._call_expert(
                    work, descriptor, purpose,
                    {**payload, "format_correction": correction},
                    call_key=call_key + "-format-correction",
                    parent_digest=parent_digest,
                    dispatch_class="format_correction")
            work["status"] = "UNCONFIRMED"
            work["stop_reason"] = "PROVIDER_UNAVAILABLE"
            self._append(work, "EXPERT_FAILED", {
                "request_id": request_id, "expert_id": descriptor.expert_id,
                "error_code": call["error_code"]})
            return {"kind": "UNSUPPORTED", "message": "provider call failed"}
        self._complete_reservation(work, {"provider_dispatches": 1})
        call.update({"status": "COMPLETED", "response_digest": digest_json(response),
                     "finished_at": time.time()})
        if isinstance(provider, dict):
            call["provider"] = provider
            if provider.get("configured_binding_id") != descriptor.binding_id:
                work["status"] = "UNCONFIRMED"
                work["stop_reason"] = "EXPERT_BINDING_MISMATCH"
                self._append(work, "EXPERT_BINDING_REJECTED", {
                    "request_id": request_id, "expert_id": descriptor.expert_id})
                return {"kind": "UNSUPPORTED", "message": "binding mismatch"}
        if work["control_generation"] != generation \
                or work["status"] in TERMINAL_STATUSES:
            ignored = {"request_id": request_id,
                       "response_digest": call["response_digest"],
                       "ignored": True, "observed_generation": generation,
                       "received_at": time.time()}
            work["late_responses"].append(ignored)
            self._append(work, "EXPERT_RESPONSE_IGNORED", ignored)
            return {"kind": "UNSUPPORTED", "message": "stale response"}
        self._append(work, "EXPERT_COMPLETED", {
            "request_id": request_id, "expert_id": descriptor.expert_id,
            "response_digest": call["response_digest"]})
        return response

    @staticmethod
    def _validate_expert_response(purpose: str, response: dict[str, Any]) -> None:
        kind = response.get("kind")
        if purpose in {"proposal", "proposal_revision"}:
            if kind not in {"PROPOSAL", "NEED_EVIDENCE", "NO_PROPOSAL", "UNSUPPORTED"}:
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED", "invalid proposal response")
            if kind == "PROPOSAL" and not isinstance(response.get("proposal"), dict):
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED", "proposal object is required")
        elif purpose in {"proposal_review", "evidence_review"}:
            if kind not in REVIEW_KINDS:
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED", "invalid review response")
            if kind == "REVIEW" and response.get("disposition") not in {
                    "ACCEPT", "REQUEST_REVISION", "NEED_EVIDENCE", "STOP"}:
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED", "invalid review disposition")
        elif purpose == "execution_analysis":
            if kind != "EXECUTION_ANALYSIS" or not isinstance(
                    response.get("criteria_met"), bool):
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED", "invalid execution analysis")
        elif purpose == "result_review":
            if kind != "RESULT_REVIEW" or not isinstance(
                    response.get("supported"), bool):
                raise WorkError(
                    "PROVIDER_RESPONSE_VALIDATION_FAILED", "invalid result review")

    def _store_global_decision(
        self, work: dict[str, Any], decision: dict[str, Any],
        based_on_domain_digest: str | None, *,
        based_on_expert_digests: list[str],
    ) -> dict[str, Any]:
        stored = {
            **decision, "decision_id": "gdec_" + uuid.uuid4().hex,
            "revision": work["revision"],
            "based_on_domain_response_digest": based_on_domain_digest,
            "based_on_expert_response_digests": list(based_on_expert_digests),
        }
        work["decisions"].append(stored)
        self._append(work, "GLOBAL_DECISION", stored)
        return stored

    @staticmethod
    def _input_evidence_refs(work: dict[str, Any]) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        baseline = work.get("baseline") or {}
        reference = baseline.get("reference")
        if isinstance(reference, dict):
            references.append({
                "kind": "baseline", "digest": digest_json(reference),
                "task_id": reference.get("task_id")
                    or (reference.get("execution_import") or {}).get(
                        "source_task_id"),
                "run_id": reference.get("run_id")
                    or (reference.get("execution_import") or {}).get(
                        "run", {}).get("run_id"),
            })
        for evidence in work.get("evidence_sets") or []:
            references.append({"kind": "evidence_set",
                               "digest": digest_json(evidence)})
        return references

    def _catalog_matches_work(self, work: dict[str, Any]) -> bool:
        collaboration = work.get("collaboration")
        if not isinstance(collaboration, dict):
            return True
        return collaboration.get("catalog_digest") == digest_json(
            self._expert_catalog.descriptors())

    def _stop_for_catalog_mismatch(self, work: dict[str, Any]) -> None:
        work["status"] = "WAITING_USER"
        work["pending"] = {
            "kind": "EXPERT_VERSION_INCOMPATIBLE",
            "expected_catalog_digest": (work.get("collaboration") or {}).get(
                "catalog_digest"),
            "actual_catalog_digest": digest_json(
                self._expert_catalog.descriptors()),
            "message": "work revision is pinned to an incompatible Expert catalog",
        }
        self._append(work, "EXPERT_CATALOG_VERSION_REJECTED", work["pending"])


__all__ = [
    "EAP4_SCHEMA_VERSION", "EXPERT_PURPOSES", "ExpertBindingRouter",
    "ExpertCapabilityCatalog", "ExpertCapabilityDescriptor",
    "MultiExpertWorkService",
]


def _find_int(node: Any, key: str) -> int | None:
    """在巢狀 dict／list 中取第一個整數欄位；找不到回 None，不補值。"""
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        for child in node.values():
            found = _find_int(child, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_int(child, key)
            if found is not None:
                return found
    return None
