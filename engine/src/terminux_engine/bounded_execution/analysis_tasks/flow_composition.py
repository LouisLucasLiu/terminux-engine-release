# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""flow task 的 production composition helper。

把散在 Package／Kernel／Core 的零件組成 `FlowController` 的依賴。放在這裡
而不是 `jsonrpc_server`，是為了讓 daemon 的組裝點保持可讀，並且讓聚焦檢查
可以用同一份組裝邏輯（只換掉外部資源）。

它**不放寬**任何邊界：binding 仍是 host-built 物件，profile 仍每次重驗，
Package 仍不碰 Docker。
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from terminux_engine.bounded_execution.analysis_tasks import flow_models as F
from terminux_engine.bounded_execution.analysis_tasks import flow_worker as W
from terminux_engine.bounded_execution.analysis_tasks.flow_controller import (
    FlowController, FlowControllerDeps, FlowDraftError,
)

_LOG = logging.getLogger(__name__)

#: 派送本 flow step 期間，host 注入 package services 的內容。
_CURRENT_FLOW_BINDING: Any = None


def load_manifest_for(profile: Any) -> Any:
    """讀回並驗 baseline input manifest 的**原始 bytes** hash。"""
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    return FI.load_manifest(profile.input_manifest_path,
                            expected_sha256=profile.input_manifest_sha256)


def derive_candidate_for(profile: Any, manifest: Any, *,
                         candidate_value: str | None = None) -> tuple[bytes, str, str]:
    """由已驗證的 manifest 推導候選 config bytes 與**預期** tree hash。

    只讀 design config 一個檔案，其餘由 manifest inventory 推導——
    不重算 3.2 GB。
    """
    import hashlib

    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    # EAP proposal 的 SETUP_SLACK_MARGIN 值由 Domain 提出，不能回填 Profile
    # 的固定 candidate。schema 11 setup-margin Profile 的 baseline config 刻意
    # 沒有該 assignment；它以已驗證的 candidate suffix 表示唯一允許的變更。
    # 因此 EAP DRAFT 必須以同一份 suffix 的單一 numeric token 形成預期 hash。
    if candidate_value is not None and getattr(profile, "is_setup_repair", False):
        return derive_eap_setup_margin_candidate(profile, manifest, candidate_value)

    config_path = Path(manifest.snapshot_root) / profile.design_config
    original = config_path.read_bytes()
    actual_sha = hashlib.sha256(original).hexdigest()
    if actual_sha != manifest.config_sha256:
        raise FlowDraftError(
            "TASK_REVALIDATION_FAILED",
            "the design config bytes do not match the input manifest")
    candidate_bytes = FI.compute_candidate_config(
        original, name=profile.parameter.name,
        baseline_value=profile.parameter.baseline,
        candidate_value=candidate_value or profile.parameter.candidate)
    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    expected_tree_hash = FI.expected_candidate_tree_hash(
        manifest, candidate_config_sha256=candidate_sha,
        candidate_config_size=len(candidate_bytes))
    return candidate_bytes, expected_tree_hash, candidate_sha


def derive_eap_setup_margin_candidate(
        profile: Any, manifest: Any, candidate_value: str) -> tuple[bytes, str, str]:
    """以已驗證 setup-margin suffix 的單一 token 形成 EAP 動態候選。

    這是 ORFS schema 11 setup-margin Profile 的既有工程表示法：baseline 不帶
    `SETUP_SLACK_MARGIN` assignment，候選才在隔離副本末端附加一行。通過
    Package validator 的 proposal value 取代該受驗證 suffix 的固定 token；
    其餘 bytes、來源 manifest 與 helper hash 都不變。
    """
    import hashlib

    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    recipe = profile.repair_recipe
    if recipe is None or recipe.method.variable != profile.parameter.name:
        raise FlowDraftError("TASK_REVALIDATION_FAILED",
                             "the setup-margin Profile has no matching recipe")
    config_path = Path(manifest.snapshot_root) / profile.design_config
    original = config_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != manifest.config_sha256:
        raise FlowDraftError(
            "TASK_REVALIDATION_FAILED",
            "the design config bytes do not match the input manifest")
    suffix = _eap_setup_margin_suffix(profile, recipe, candidate_value)
    candidate_bytes = original + suffix
    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    expected_tree_hash = FI.expected_candidate_tree_hash(
        manifest, candidate_config_sha256=candidate_sha,
        candidate_config_size=len(candidate_bytes))
    return candidate_bytes, expected_tree_hash, candidate_sha


def verify_inputs_for(*, manifest_path: str, expected_sha256: str, root: str) -> Any:
    """worker 的完整逐檔驗證。慢，永遠在短鎖之外。"""
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    manifest = FI.load_manifest(manifest_path, expected_sha256=expected_sha256)
    return FI.verify_inventory(manifest, root)


def build_variant_writer(profile_for_task: Any, task_lookup: Callable[[str], Any]) -> Callable[[Any], Any]:
    """Package 的受控寫入 helper。

    它只依 host binding 工作，並在寫完後驗證實際 tree hash 等於 create 時
    綁進 task digest 的預期值。
    """
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    def _write(binding: Any) -> Any:
        profile = profile_for_task(binding.task_id)
        task = task_lookup(binding.task_id)
        if task is None:
            raise RuntimeError("no live task for this binding")
        manifest = FI.load_manifest(profile.input_manifest_path,
                                    expected_sha256=profile.input_manifest_sha256)
        slot = binding.slot
        # 每個候選都從**baseline 副本**出發，各自獨立的目錄與 manifest；
        # candidate_2 不沿用 candidate_1 已改過的 config。
        entry = (task.candidate_entry(slot)
                 if hasattr(task, "candidate_entry") else None)
        candidate_value = (str(entry["value"]) if entry
                           else profile.parameter.candidate)
        expected_tree_hash = (str(entry["expected_tree_hash"]) if entry
                              else task.expected_candidate_tree_hash)
        # EAP setup-margin task 與 Profile 的舊固定 repair task 共用同一個
        # 已驗證 suffix 規則；差別只有值來自已核准 proposal，而非 Profile。
        if getattr(task, "template_id", "") == F.EAP_SETUP_MARGIN_TEMPLATE_ID:
            recipe = profile.repair_recipe
            if recipe is None:
                raise RuntimeError("the EAP setup-margin Profile has no recipe")
            result = FI.prepare_repair_snapshot(
                source_manifest=manifest,
                source_root=profile.input_snapshot_root,
                destination_root=profile.candidate_source_root(binding.task_id, slot),
                manifest_path=str(profile.candidate_source_root(binding.task_id, slot).parent /
                                  f"{binding.task_id}_{slot}_manifest.json"),
                slot=slot, helpers=(),
                config_suffix=_eap_setup_margin_suffix(profile, recipe, candidate_value),
                expected_tree_hash=expected_tree_hash)
            return F.VariantInputRef(
                task_id=binding.task_id, candidate_root=str(result.root),
                manifest_path=str(result.manifest_path),
                manifest_sha256=result.manifest_sha256, tree_hash=result.tree_hash,
                config_sha256=result.config_sha256)
        candidate_root = profile.candidate_source_root(binding.task_id, slot)
        result = FI.materialize_candidate(
            baseline_manifest=manifest,
            baseline_root=profile.input_snapshot_root,
            candidate_root=candidate_root,
            manifest_path=str(candidate_root.parent /
                              f"{binding.task_id}_{slot}_manifest.json"),
            name=profile.parameter.name,
            baseline_value=profile.parameter.baseline,
            candidate_value=candidate_value,
            expected_tree_hash=expected_tree_hash,
        )
        return F.VariantInputRef(
            task_id=binding.task_id, candidate_root=str(result.candidate_root),
            manifest_path=str(result.manifest_path),
            manifest_sha256=result.manifest_sha256, tree_hash=result.tree_hash,
            config_sha256=result.config_sha256)

    return _write


def derive_repair_candidate(profile: Any, manifest: Any) -> tuple[bytes, str, str]:
    """候選與 baseline 的唯一差異是已核准的 config 追加內容。

    在 create 階段先算出預期的 config bytes／sha256／tree hash，全部綁進
    task digest；s3 實際寫完後必須算出同一個 tree hash。
    """
    import hashlib

    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    recipe = profile.repair_recipe
    if recipe is None:
        raise FlowDraftError("TASK_REVALIDATION_FAILED",
                             "this profile carries no approved repair recipe")
    config_path = Path(manifest.snapshot_root) / profile.design_config
    original = config_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != manifest.config_sha256:
        raise FlowDraftError(
            "TASK_REVALIDATION_FAILED",
            "the design config bytes do not match the input manifest")
    suffix = _approved_candidate_suffix(profile, recipe)
    candidate_bytes = original + suffix
    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    expected_tree_hash = FI.expected_candidate_tree_hash(
        manifest, candidate_config_sha256=candidate_sha,
        candidate_config_size=len(candidate_bytes))
    return candidate_bytes, expected_tree_hash, candidate_sha


def derive_resynthesis_candidate(profile: Any, manifest: Any) -> tuple[bytes, str, str]:
    """候選相對 baseline 只有兩處差異。

    第一處（`ABC_AREA` 的數字 token）在 create 時就能算出來；第二處
    （沿用 baseline 邊界的 geometry 段）要等 s1 量到實際 core/die 才存在。
    因此這裡回傳的**不是** tree hash，而是把「已固定的部分」綁進 task 身分的
    rule digest：recipe id、geometry policy、以及只做 token 改寫後的 config
    bytes。實際的 tree hash 由 s3 materialize 之後產生，並經 s3→s4 的
    step binding 傳遞。
    """
    import hashlib

    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    recipe = profile.repair_recipe
    if recipe is None:
        raise FlowDraftError("TASK_REVALIDATION_FAILED",
                             "this profile carries no approved recipe")
    config_path = Path(manifest.snapshot_root) / profile.design_config
    original = config_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != manifest.config_sha256:
        raise FlowDraftError(
            "TASK_REVALIDATION_FAILED",
            "the design config bytes do not match the input manifest")
    swapped = FI.compute_candidate_config(
        original, name=recipe.strategy_variable,
        baseline_value=recipe.baseline_value,
        candidate_value=recipe.candidate_value)
    rule_digest = hashlib.sha256(b"\n".join([
        b"gcd.speed_resynthesis.rule.v1",
        recipe.recipe_id.encode(),
        recipe.geometry_policy_id.encode(),
        recipe.strategy_variable.encode(),
        recipe.baseline_value.encode(), recipe.candidate_value.encode(),
        hashlib.sha256(swapped).hexdigest().encode(),
    ])).hexdigest()
    return swapped, rule_digest, hashlib.sha256(swapped).hexdigest()


def derive_prefix_borrow_candidate(profile: Any, manifest: Any) -> tuple[bytes, str, str]:
    """候選相對 baseline 只有兩處**已宣告**差異。

    第一處是那份固定 RTL patch（create 時就完全確定）；第二處是沿用 baseline
    邊界的 geometry 段（要等 s1 量到實際 core/die 才存在）。因此這裡回傳的
    也是 rule digest，不是 tree hash——實際 tree hash 由 s3 產生並經
    step binding 傳到 s4。config 本身**不改任何 token**（兩側 ABC_AREA=1）。
    """
    import hashlib

    recipe = profile.repair_recipe
    if recipe is None:
        raise FlowDraftError("TASK_REVALIDATION_FAILED",
                             "this profile carries no approved recipe")
    patch = getattr(recipe, "rtl_patch", None)
    if patch is None:
        raise FlowDraftError("TASK_REVALIDATION_FAILED",
                             "this profile carries no approved RTL patch")
    config_path = Path(manifest.snapshot_root) / profile.design_config
    original = config_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != manifest.config_sha256:
        raise FlowDraftError(
            "TASK_REVALIDATION_FAILED",
            "the design config bytes do not match the input manifest")
    # 原 RTL 必須就是 patch 宣告的 before——create 階段就先擋掉不符的來源。
    rtl_path = Path(manifest.snapshot_root) / patch.path
    if not rtl_path.is_file():
        raise FlowDraftError("TASK_REVALIDATION_FAILED",
                             f"the RTL to patch is missing: {patch.path}")
    actual = hashlib.sha256(rtl_path.read_bytes()).hexdigest()
    if actual != patch.before_sha256:
        raise FlowDraftError(
            "TASK_REVALIDATION_FAILED",
            f"the source RTL hashes to {actual} but the approved patch applies "
            f"to {patch.before_sha256}")
    rule_digest = hashlib.sha256(b"\n".join([
        b"gcd.prefix_borrow.rule.v1",
        recipe.recipe_id.encode(),
        recipe.geometry_policy_id.encode(),
        recipe.comparison_policy_id.encode(),
        patch.path.encode(),
        patch.before_sha256.encode(), patch.after_sha256.encode(),
        patch.patch_sha256.encode(),
        "|".join(patch.modules).encode(),
        hashlib.sha256(original).hexdigest().encode(),
    ])).hexdigest()
    return original, rule_digest, hashlib.sha256(original).hexdigest()


def approved_rtl_patch_bytes(profile: Any, recipe: Any) -> Any:
    """由**已驗證的 input tree** 與已核准 patch 產生候選 RTL bytes。

    patch 檔本身也在 snapshot 內（hash 已在 input manifest），因此這裡不從
    repo、網路或任意路徑取內容；套用方式是嚴格逐行比對，不用 `patch(1)` 的
    fuzzy 行為。
    """
    import hashlib

    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    patch = recipe.rtl_patch
    source = Path(profile.input_snapshot_root) / patch.path
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != patch.before_sha256:
        raise RuntimeError(
            "the snapshot RTL does not match the approved patch input hash")
    patch_file = (Path(profile.input_snapshot_root) / recipe.helper_dir
                  / "gcd_prefix_borrow.patch")
    if not patch_file.is_file():
        raise RuntimeError(f"the approved RTL patch is missing: {patch_file}")
    raw = patch_file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != patch.patch_sha256:
        raise RuntimeError(
            "the RTL patch bytes do not match the hash bound into the task digest")
    patched = _apply_unified_diff(original, raw)
    after = hashlib.sha256(patched).hexdigest()
    if after != patch.after_sha256:
        raise RuntimeError(
            f"applying the approved patch produced {after}, not the approved "
            f"{patch.after_sha256}")
    return FI.ApprovedSourcePatch(
        relative_path=patch.path, before_sha256=patch.before_sha256,
        after_sha256=patch.after_sha256, content=patched)


def _apply_unified_diff(original: bytes, diff: bytes) -> bytes:
    """套用 unified diff。**嚴格模式**：context 必須逐行完全相符。

    刻意不用 `patch(1)`：它會做 fuzzy 比對與偏移搜尋，那正是這裡要禁止的。
    """
    src = original.split(b"\n")
    out: list[bytes] = []
    pos = 0
    lines = diff.split(b"\n")
    i = 0
    saw_hunk = False
    while i < len(lines):
        line = lines[i]
        if line.startswith(b"--- ") or line.startswith(b"+++ "):
            i += 1
            continue
        if line.startswith(b"@@"):
            saw_hunk = True
            head = line.split(b"@@")[1].strip()
            old_part = head.split(b" ")[0]          # -601,11
            start = int(old_part[1:].split(b",")[0]) - 1
            if start < pos:
                raise RuntimeError("the patch hunks are not in ascending order")
            out.extend(src[pos:start])
            pos = start
            i += 1
            while i < len(lines) and not lines[i].startswith(b"@@"):
                body = lines[i]
                if body.startswith(b"-"):
                    if src[pos] != body[1:]:
                        raise RuntimeError(
                            f"patch context mismatch at source line {pos + 1}")
                    pos += 1
                elif body.startswith(b"+"):
                    out.append(body[1:])
                elif body.startswith(b" ") or body == b"":
                    expected = body[1:] if body else b""
                    if pos >= len(src) or src[pos] != expected:
                        # diff 尾端的空行不是 context，遇到就結束這個 hunk。
                        if body == b"":
                            i += 1
                            continue
                        raise RuntimeError(
                            f"patch context mismatch at source line {pos + 1}")
                    out.append(src[pos])
                    pos += 1
                else:
                    raise RuntimeError(f"unsupported patch line: {body[:40]!r}")
                i += 1
            continue
        i += 1
    if not saw_hunk:
        raise RuntimeError("the patch contains no hunk")
    out.extend(src[pos:])
    return b"\n".join(out)


def build_prefix_borrow_writer(profile_for_task: Any,
                               task_lookup: Callable[[str], Any],
                               live_lookup: Callable[[str], Any] | None = None
                               ) -> Callable[[Any], Any]:
    """v0.8 的 candidate materializer：固定 RTL patch ＋ 已驗證的 geometry 段。

    config 只多 geometry 段，**不改任何 token**；RTL 只套那一份已核准 patch。
    """
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    def _write(binding: Any) -> Any:
        profile = profile_for_task(binding.task_id)
        recipe = profile.repair_recipe
        task = task_lookup(binding.task_id)
        if task is None:
            raise RuntimeError("no live task for this binding")
        live = live_lookup(binding.task_id) if live_lookup is not None else None
        layout = (getattr(binding, "layout_binding", None)
                  or getattr(live, "layout_binding", None))
        if layout is None:
            raise RuntimeError(
                "the candidate cannot be materialized before the baseline "
                "geometry has been verified")
        manifest = FI.load_manifest(profile.input_manifest_path,
                                    expected_sha256=profile.input_manifest_sha256)
        config_path = Path(manifest.snapshot_root) / profile.design_config
        original = config_path.read_bytes()
        candidate_bytes = original + layout.config_section()
        candidate_root = profile.candidate_source_root(binding.task_id, binding.slot)
        result = FI.prepare_repair_snapshot(
            source_manifest=manifest,
            source_root=profile.input_snapshot_root,
            destination_root=candidate_root,
            manifest_path=str(candidate_root.parent /
                              f"{binding.task_id}_{binding.slot}_manifest.json"),
            slot=binding.slot,
            helpers=(),
            config_replacement=candidate_bytes,
            config_suffix=b"",
            source_patches=(approved_rtl_patch_bytes(profile, recipe),),
            expected_tree_hash=None)
        return F.VariantInputRef(
            task_id=binding.task_id, candidate_root=str(result.root),
            manifest_path=str(result.manifest_path),
            manifest_sha256=result.manifest_sha256, tree_hash=result.tree_hash,
            config_sha256=result.config_sha256)

    return _write


def build_resynthesis_writer(profile_for_task: Any,
                             task_lookup: Callable[[str], Any],
                             live_lookup: Callable[[str], Any] | None = None
                             ) -> Callable[[Any], Any]:
    """v5 的 candidate materializer：token 改寫 ＋ 已驗證的 geometry 段。

    geometry 只能來自 host 在 s1 之後派生並綁進 step binding 的
    `layout_binding`；模型、client、任意磁碟路徑都不能提供或覆寫它。
    """
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    def _write(binding: Any) -> Any:
        profile = profile_for_task(binding.task_id)
        recipe = profile.repair_recipe
        task = task_lookup(binding.task_id)
        if task is None:
            raise RuntimeError("no live task for this binding")
        # geometry 只能來自 host 在 s1 之後派生的 live binding。
        live = live_lookup(binding.task_id) if live_lookup is not None else None
        layout = (getattr(binding, "layout_binding", None)
                  or getattr(live, "layout_binding", None))
        if layout is None:
            raise RuntimeError(
                "the candidate cannot be materialized before the baseline "
                "geometry has been verified")
        manifest = FI.load_manifest(profile.input_manifest_path,
                                    expected_sha256=profile.input_manifest_sha256)
        config_path = Path(manifest.snapshot_root) / profile.design_config
        original = config_path.read_bytes()
        swapped = FI.compute_candidate_config(
            original, name=recipe.strategy_variable,
            baseline_value=recipe.baseline_value,
            candidate_value=recipe.candidate_value)
        # 唯一允許的兩處差異：token 改寫，再加上固定規則產生的 geometry 段。
        candidate_bytes = swapped + layout.config_section()
        candidate_root = profile.candidate_source_root(binding.task_id, binding.slot)
        result = FI.prepare_repair_snapshot(
            source_manifest=manifest,
            source_root=profile.input_snapshot_root,
            destination_root=candidate_root,
            manifest_path=str(candidate_root.parent /
                              f"{binding.task_id}_{binding.slot}_manifest.json"),
            slot=binding.slot,
            helpers=(),
            # 這裡不是「append 到原檔」，而是整份取代：token 改寫在檔案中間。
            config_replacement=candidate_bytes,
            config_suffix=b"",
            expected_tree_hash=None)
        return F.VariantInputRef(
            task_id=binding.task_id, candidate_root=str(result.root),
            manifest_path=str(result.manifest_path),
            manifest_sha256=result.manifest_sha256, tree_hash=result.tree_hash,
            config_sha256=result.config_sha256)

    return _write


def _approved_candidate_suffix(profile: Any, recipe: Any) -> bytes:
    """candidate 唯一追加的那一段 config，bytes 取自**已驗證的 input tree**。

    它就放在 baseline snapshot 的 helper 目錄裡（ORFS 不會 source `.mk`
    檔案，所以它是惰性資料），因此它的 hash 已經在 input manifest 內，
    而且必須等於 recipe 綁進 task digest 的那個 hash。
    """
    import hashlib

    path = (Path(profile.input_snapshot_root) / recipe.helper_dir
            / "config_candidate.mk")
    if not path.is_file():
        raise RuntimeError(
            f"the approved candidate config suffix is missing: {path}")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != recipe.candidate_config_suffix_sha256:
        raise RuntimeError(
            "the candidate config suffix does not match the hash bound into "
            "the task digest")
    return data


def _eap_setup_margin_suffix(profile: Any, recipe: Any, candidate_value: str) -> bytes:
    """把 Profile 已驗證 suffix 的唯一設定值換成已核准 proposal candidate。"""
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    return FI.compute_candidate_config(
        _approved_candidate_suffix(profile, recipe), name=profile.parameter.name,
        baseline_value=recipe.method.candidate_value, candidate_value=candidate_value)


def build_repair_writer(profile_for_task: Any, task_lookup: Callable[[str], Any],
                        live_lookup: Callable[[str], Any] | None = None
                        ) -> Callable[[Any], Any]:
    """把已核准的固定 ECO helper／config bytes 寫入隔離 candidate source。

    bytes 由 server 在 profile 準備時固定，hash 綁進 profile digest 與
    task digest；這裡只負責複製並驗證結果 tree hash。
    """
    from terminux_engine.packages.orfs_run_intelligence import flow_inputs as FI

    prefix_borrow = build_prefix_borrow_writer(profile_for_task, task_lookup,
                                              live_lookup)
    resynthesis = build_resynthesis_writer(profile_for_task, task_lookup,
                                           live_lookup)

    def _write(binding: Any) -> Any:
        profile = profile_for_task(binding.task_id)
        recipe = profile.repair_recipe
        if recipe is None:
            raise RuntimeError("this profile carries no approved repair recipe")
        # v5 的候選差異是「合成策略 token ＋ 已驗證 geometry」，不是 ECO
        # hook 的 config 追加段，因此走另一條 materializer。
        if getattr(profile, "template_id", "") in F.PREFIX_BORROW_TEMPLATES:
            return prefix_borrow(binding)
        if getattr(profile, "template_id", "") in F.RESYNTHESIS_TEMPLATES:
            return resynthesis(binding)
        task = task_lookup(binding.task_id)
        if task is None:
            raise RuntimeError("no live task for this binding")
        manifest = FI.load_manifest(profile.input_manifest_path,
                                    expected_sha256=profile.input_manifest_sha256)
        entry = (task.candidate_entry(binding.slot)
                 if hasattr(task, "candidate_entry") else None)
        expected_tree_hash = (str(entry["expected_tree_hash"]) if entry
                              else task.expected_candidate_tree_hash)
        candidate_root = profile.candidate_source_root(binding.task_id, binding.slot)
        result = FI.prepare_repair_snapshot(
            source_manifest=manifest,
            source_root=profile.input_snapshot_root,
            destination_root=candidate_root,
            manifest_path=str(candidate_root.parent /
                              f"{binding.task_id}_{binding.slot}_manifest.json"),
            slot=binding.slot,
            # baseline 已經含共同 helper，candidate 只追加那一行 hook 設定。
            helpers=(),
            config_suffix=_approved_candidate_suffix(profile, recipe),
            expected_tree_hash=expected_tree_hash)
        return F.VariantInputRef(
            task_id=binding.task_id, candidate_root=str(result.root),
            manifest_path=str(result.manifest_path),
            manifest_sha256=result.manifest_sha256, tree_hash=result.tree_hash,
            config_sha256=result.config_sha256)

    return _write


def _derive_candidate_for_template(profile: Any, manifest: Any):
    """依 template 選候選推導方式。v1～v4 不變。"""
    if getattr(profile, "template_id", "") in F.PREFIX_BORROW_TEMPLATES:
        return derive_prefix_borrow_candidate(profile, manifest)
    if getattr(profile, "template_id", "") in F.RESYNTHESIS_TEMPLATES:
        return derive_resynthesis_candidate(profile, manifest)
    return derive_repair_candidate(profile, manifest)


def build_service_binder(
    orchestration_service_getter: Callable[[], Any],
    variant_writer: Callable[[Any], Any],
    repair_writer: Callable[[Any], Any] | None = None,
    profile_for_task: Callable[[str], Any] | None = None,
) -> Callable[..., Any]:
    """回傳 `(binding, on_run_created) -> contextmanager`。

    binding 只在派送這一步期間存在於 contextvar 內；步驟結束即 reset，
    不會洩漏到下一個 task 或別的 package 呼叫。
    """
    from terminux_engine.bounded_execution.analysis_tasks import flow_binding_context as BC
    from terminux_engine.orchestration.flow_execution_service import (
        FlowExecutionService,
    )

    @contextlib.contextmanager
    def _bind(binding: Any, on_run_created: Any = None,
              *, task_id: str | None = None,
              imported_run_dqe_overrides: dict[str, str] | None = None,
              allow_llm: bool | None = None,
              formal_proof: dict | None = None,
              ) -> Iterator[None]:
        # s2／s5 是純分析 action，沒有 EDA `TaskExecutionBinding`；但 DQE
        # 仍必須固定到同一個已核准 task 的 Profile。絕不可偽造 run binding
        # 或從 action arguments 取 task id。
        scoped_task_id = task_id or getattr(binding, "task_id", None)
        profile = (profile_for_task(scoped_task_id)
                   if profile_for_task is not None and isinstance(scoped_task_id, str)
                   else None)
        evidence_facts: dict[str, Any] = {}
        dqe_db_path = getattr(profile, "dqe_db_path", None)
        if isinstance(dqe_db_path, str) and dqe_db_path:
            evidence_facts["dqe_db_path"] = dqe_db_path

        # ── manual zero-provider 或零 dispatch 注入 ──
        allow_llm_flag = allow_llm
        if profile is not None and allow_llm_flag is None:
            limits = getattr(profile, "limits", None)
            body_llm = getattr(limits, "body_llm_dispatches", None) if limits is not None else None
            if body_llm is None and isinstance(limits, dict):
                body_llm = limits.get("body_llm_dispatches")
            if body_llm == 0:
                allow_llm_flag = False

        if allow_llm_flag is False:
            evidence_facts["allow_llm"] = False
            evidence_facts["body_llm_dispatches"] = 0

        # formal proof 事實由 controller 依 durable envelope 推導後傳入；
        # selection eligibility 用它決定能不能選用 candidate，呼叫端不得提供。
        if isinstance(formal_proof, dict):
            evidence_facts["formal_proof"] = dict(formal_proof)

        # Per-run DQE 覆寫：imported baseline run 的 DQE 仍屬來源 DB。
        # 完整的 ``{run_id: source_dqe_db_path}`` mapping 由 controller
        # 在 import 成立後組出（它知道哪個 run_id 是 imported 的），
        # 透過參數傳入。
        run_overrides = dict(imported_run_dqe_overrides or {})

        # 同時把 source DQE 路徑存入 evidence_facts 供 host 端唯讀查閱。
        if run_overrides:
            first_path = next(iter(run_overrides.values()), None)
            if isinstance(first_path, str) and first_path:
                evidence_facts["source_dqe_db_path"] = first_path

        execution = FlowExecutionService(
            orchestration_service_getter(),
            on_run_created=on_run_created or (lambda _ref: None),
        )
        token = BC.set_current_flow_context(
            BC.FlowBindingContext(binding=binding, execution=execution,
                                  variant_writer=variant_writer,
                                  repair_writer=repair_writer,
                                  evidence_facts=evidence_facts,
                                  imported_run_dqe_overrides=run_overrides))
        try:
            yield
        finally:
            BC.reset_current_flow_context(token)

    return _bind


def build_preflight(profile_for_task: Any) -> Callable[[Any], tuple[bool, str | None]]:
    """派工前核對工具／profile／來源條件。不呼叫 LLM。

    profile 取自 task-owned snapshot；runner 由 profile 的 `execution`
    決定（host-native 或 fixture corpus replay），各自核對它需要的東西。舊
    container 模式與舊 host runner 一律拒絕，不呼叫 docker。
    """
    import shutil
    import subprocess

    def _check(task: Any) -> tuple[bool, str | None]:
        profile = profile_for_task(task.task_id)
        if profile is None:
            return False, "the task-owned profile snapshot is missing"
        if profile.profile_digest != task.profile_digest:
            return False, "the task-owned profile snapshot does not match the task"
        if not Path(profile.input_snapshot_root).is_dir():
            return False, "the input snapshot is missing"
        if not Path(profile.input_manifest_path).is_file():
            return False, "the input manifest is missing"
        execution = getattr(profile, "execution", None)
        binding = getattr(execution, "native_tool_binding", None)
        if binding is not None:
            # host-native：不檢查 image；逐一重新核對授權的工具 pin。
            from terminux_engine.orchestration.native_tool_identity import (
                NativeToolIdentityError,
                verify_pin,
            )

            try:
                verify_pin(binding.env_script, where="env_script", executable=False)
                for name in sorted(binding.tools):
                    verify_pin(binding.tools[name], where=name)
            except NativeToolIdentityError as exc:
                return False, f"the native tool binding does not match this host: {exc}"
            return True, None
        from terminux_engine.orchestration import legacy_docker_execution as LDE

        denial = LDE.flow_task_execution_denial(execution)
        if denial is not None:
            # 舊 container 模式或舊 host runner：不查 image、不呼叫 docker、
            # 不碰 Profile 的 host env script。
            return False, denial.message
        # 只剩 fixture corpus replay：replay CLI 模擬 docker CLI 邊界。
        docker = getattr(execution, "docker_executable", None) or "docker"
        if shutil.which(docker) is None and not Path(docker).is_file():
            return False, f"docker is not available on this host: {docker}"
        try:
            result = subprocess.run(
                [docker, "image", "inspect", profile.docker_image_id,
                 "--format", "{{.Id}}"],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"the pinned image could not be inspected: {exc}"
        if result.returncode != 0:
            return False, ("the pinned image is not present locally; this batch "
                           "never downloads or builds images")
        return True, None

    return _check


def build_cancel_run(orchestration_service_getter: Callable[[], Any]) -> Callable[[Any], dict[str, Any]]:
    """對已綁定 run 做 ownership-first cancel。

    身份來自 Kernel 的 run 記錄；**不做模糊比對、不停止其他容器**。
    無法確認停止時如實回報，由呼叫端標 OUTCOME_UNCONFIRMED。
    """
    def _cancel(run_ref: Any) -> dict[str, Any]:
        service = orchestration_service_getter()
        if service is None:
            return {"run_id": run_ref.run_id, "slot": run_ref.slot,
                    "requested": False, "confirmed": False,
                    "reason": "the orchestration service is not configured"}
        try:
            result = service.cancel_request(run_ref.run_id, immediate=True)
        except Exception as exc:  # noqa: BLE001
            return {"run_id": run_ref.run_id, "slot": run_ref.slot,
                    "requested": False, "confirmed": False, "reason": repr(exc)}
        return {"run_id": run_ref.run_id, "slot": run_ref.slot,
                "requested": True,
                # ack 不等於容器已停止。
                "confirmed": False,
                "ack": bool(result.get("ack")) if isinstance(result, dict) else None,
                "reason": ("cancel requested; container stop is confirmed "
                           "separately by the adapter")}

    return _cancel


def build_flow_controller(
    *,
    profile_for_task: Any,
    step_deps: W.FlowStepDeps,
    orchestration_service_getter: Callable[[], Any],
    task_lookup: Callable[[str], Any],
) -> FlowController:
    variant_writer = build_variant_writer(profile_for_task, task_lookup)

    def start_package_run(binding: Any, on_run_created: Any) -> str:
        service = orchestration_service_getter()
        if service is None:
            raise RuntimeError("orchestration service is unavailable")
        return service.start_task_run(binding, F.SLOT_CANDIDATE, on_run_created)

    return FlowController(FlowControllerDeps(
        profile_for_task=profile_for_task,
        step_deps=step_deps,
        load_manifest=load_manifest_for,
        derive_candidate=derive_candidate_for,
        cancel_run=build_cancel_run(orchestration_service_getter),
        variant_writer=variant_writer,
        preflight_check=build_preflight(profile_for_task),
        derive_repair_candidate=_derive_candidate_for_template,
        start_package_run=start_package_run,
    ))


__all__ = [
    "build_cancel_run",
    "build_flow_controller",
    "build_preflight",
    "build_service_binder",
    "build_variant_writer",
    "derive_candidate_for",
    "load_manifest_for",
    "verify_inputs_for",
]
