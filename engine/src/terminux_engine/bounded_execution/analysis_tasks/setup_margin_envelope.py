# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""RC-4：setup-margin proof envelope 的 fail-closed 身分核對。

Profile 宣告的 helper hash、baseline 共同 config suffix hash、candidate suffix
hash 與 qualified tool identity，過去只做格式檢查就進 digest。這裡把它們逐一
對到**已驗證 input manifest** 與實際 bytes；任一項缺少或不符即拒絕，不以
「profile 有寫」代替「來源真的是這份」。

本模組只核對身分，不解讀 timing、不執行工具，也不修改任何檔案。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: 拒絕原因碼。呼叫端以既有 task error 包裝後回報。
ENVELOPE_INVALID = "SETUP_MARGIN_ENVELOPE_INVALID"
#: candidate suffix 範本在 helper 目錄內的固定檔名。
CANDIDATE_SUFFIX_FILENAME = "config_candidate.mk"
#: 範本值驗證用的探測值；只拿來確認範本恰有一處可替換的數字 token。
_PROBE_VALUE = "0.0123456789"


class SetupMarginEnvelopeError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ENVELOPE_INVALID
        self.message = message


def is_setup_margin_recipe(recipe: Any) -> bool:
    """只有帶 `method` 與 qualified identity 的 setup-margin recipe 才核對。"""
    return (recipe is not None and hasattr(recipe, "method")
            and hasattr(recipe, "candidate_config_suffix_sha256")
            and hasattr(recipe, "qualified_tool_identity_path"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_verified_file(manifest: Any, root: Path, relative_path: str,
                        declared_sha256: str, label: str) -> bytes:
    """manifest entry、實際 bytes 與宣告 hash 三者必須完全一致。"""
    entry = manifest.entry(relative_path)
    if entry is None or entry.kind != "file" or not entry.sha256:
        raise SetupMarginEnvelopeError(
            f"{label} {relative_path!r} is not a file in the input manifest")
    if entry.sha256 != declared_sha256:
        raise SetupMarginEnvelopeError(
            f"{label} {relative_path!r} is {entry.sha256} in the input manifest "
            f"but the Profile declares {declared_sha256}")
    path = root / relative_path
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SetupMarginEnvelopeError(
            f"{label} {relative_path!r} is unreadable: {exc}") from exc
    actual = _sha256_bytes(data)
    if actual != declared_sha256:
        raise SetupMarginEnvelopeError(
            f"{label} {relative_path!r} bytes hash to {actual}, not the declared "
            f"{declared_sha256}")
    return data


def _config_suffix_offset(config: bytes, declared_sha256: str) -> int | None:
    """回傳 design config 末端 bytes 等於宣告 hash 的起點；找不到回 None。

    只接受行首起點（檔頭或換行之後），避免把半行內容當成共同 suffix。
    """
    for offset in range(len(config) - 1, -1, -1):
        if offset and config[offset - 1:offset] != b"\n":
            continue
        if _sha256_bytes(config[offset:]) == declared_sha256:
            return offset
    return None


def verify_setup_margin_envelope(
    recipe: Any, *, manifest: Any, snapshot_root: str | Path,
    parameter_name: str, baseline_value: str, native_tool: Any = None,
) -> dict[str, Any]:
    """核對 envelope；成功回傳可保存的身分摘要，失敗拋 `SetupMarginEnvelopeError`。

    `native_tool` 是 host-native Profile 授權的 `FORMAL_YOSYS` pin：有值時 qualified
    identity 檔依 native 契約（`setup_margin_native_proof`）核對；`None` 時沿用
    container 契約，行為與摘要格式不變。
    """
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI
    from terminux_engine.packages.orfs_run_intelligence.proof_tool_identity import (
        ProofToolIdentity,
    )

    root = Path(snapshot_root)
    method = recipe.method
    if method.variable != parameter_name:
        raise SetupMarginEnvelopeError(
            f"the recipe method variable {method.variable!r} is not the Profile "
            f"parameter {parameter_name!r}")
    if str(method.baseline_value) != str(baseline_value):
        raise SetupMarginEnvelopeError(
            "the recipe baseline value does not match the Profile baseline")

    helpers: list[dict[str, str]] = []
    declared = {helper.relative_path: helper.sha256 for helper in recipe.helpers}
    for relative_path, sha256 in sorted(declared.items()):
        _read_verified_file(manifest, root, relative_path, sha256, "helper")
        helpers.append({"relative_path": relative_path, "sha256": sha256})

    suffix_path = f"{recipe.helper_dir.rstrip('/')}/{CANDIDATE_SUFFIX_FILENAME}"
    if declared.get(suffix_path) != recipe.candidate_config_suffix_sha256:
        raise SetupMarginEnvelopeError(
            f"the candidate config suffix {suffix_path!r} must be a declared helper "
            "with the candidate_config_suffix_sha256 hash")
    suffix = _read_verified_file(manifest, root, suffix_path,
                                 recipe.candidate_config_suffix_sha256,
                                 "candidate config suffix")
    try:
        FI.compute_candidate_config(
            suffix, name=method.variable, baseline_value=str(method.candidate_value),
            candidate_value=_PROBE_VALUE)
    except FI.InputPreparationError as exc:
        raise SetupMarginEnvelopeError(
            "the candidate config suffix is not a single replaceable assignment of "
            f"the approved template value: {getattr(exc, 'message', exc)}") from exc

    config = _read_verified_file(manifest, root, manifest.design_config,
                                 manifest.config_sha256, "design config")
    if FI.find_assignments(config.decode("utf-8", errors="replace"), method.variable):
        raise SetupMarginEnvelopeError(
            f"the baseline design config already assigns {method.variable}; the "
            "baseline must use the ORFS default")
    offset = _config_suffix_offset(config, recipe.config_suffix_sha256)
    if offset is None:
        raise SetupMarginEnvelopeError(
            "no trailing section of the design config matches config_suffix_sha256")

    identity_path = Path(recipe.qualified_tool_identity_path)
    if native_tool is not None:
        from terminux_engine.packages.orfs_run_intelligence import (
            setup_margin_native_proof as SMN,
        )

        try:
            qualified = SMN.verify_qualified_identity_file(
                identity_path,
                declared_sha256=getattr(recipe, "qualified_tool_identity_sha256", None),
                pin=native_tool)
        except SMN.NativeProofIdentityError as exc:
            raise SetupMarginEnvelopeError(exc.message) from exc
        return {
            "helpers": helpers,
            "candidate_config_suffix": {"relative_path": suffix_path,
                                        "sha256": recipe.candidate_config_suffix_sha256,
                                        "template_value": str(method.candidate_value)},
            "config_suffix": {"relative_path": manifest.design_config,
                              "sha256": recipe.config_suffix_sha256,
                              "offset": offset,
                              "design_config_sha256": manifest.config_sha256},
            "qualified_tool_identity": qualified,
            "execution_mode": SMN.EXECUTION_MODE,
        }
    try:
        identity_raw = identity_path.read_bytes()
    except OSError as exc:
        raise SetupMarginEnvelopeError(
            f"the qualified tool identity is unreadable: {exc}") from exc
    identity_sha256 = _sha256_bytes(identity_raw)
    declared_identity = getattr(recipe, "qualified_tool_identity_sha256", None)
    if declared_identity is not None and identity_sha256 != declared_identity:
        raise SetupMarginEnvelopeError(
            f"the qualified tool identity hashes to {identity_sha256}, not the "
            f"declared {declared_identity}")
    try:
        payload = json.loads(identity_raw.decode("utf-8"))
        identity = ProofToolIdentity.from_json_dict(payload)
    except (ValueError, TypeError, AttributeError) as exc:
        raise SetupMarginEnvelopeError(
            f"the qualified tool identity is malformed: {exc}") from exc
    missing = identity.missing_fields()
    if missing:
        raise SetupMarginEnvelopeError(
            f"the qualified tool identity is incomplete: {list(missing)}")
    if payload.get("digest") != identity.digest:
        raise SetupMarginEnvelopeError(
            "the qualified tool identity digest does not recompute")

    return {
        "helpers": helpers,
        "candidate_config_suffix": {"relative_path": suffix_path,
                                    "sha256": recipe.candidate_config_suffix_sha256,
                                    "template_value": str(method.candidate_value)},
        "config_suffix": {"relative_path": manifest.design_config,
                          "sha256": recipe.config_suffix_sha256,
                          "offset": offset,
                          "design_config_sha256": manifest.config_sha256},
        "qualified_tool_identity": {"path": str(identity_path),
                                    "sha256": identity_sha256,
                                    "digest": identity.digest,
                                    "declared_sha256": declared_identity},
    }


__all__ = [
    "ENVELOPE_INVALID",
    "SetupMarginEnvelopeError",
    "is_setup_margin_recipe",
    "verify_setup_margin_envelope",
]
