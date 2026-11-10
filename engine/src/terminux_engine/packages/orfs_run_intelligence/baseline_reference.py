# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""method-neutral 的 baseline functional／formal reference（Package 擁有）。

這一份是 **production** 的 baseline 參考身分：它只由一次真實 baseline run 的
輸出與該 run 的執行身分組出，並把「後續 candidate 要拿來比對的東西」固定下來。

method-neutral 的意思是：`CORE_UTILIZATION` 與 `SETUP_SLACK_MARGIN` 共用同一份
component 與同一組身分欄位（input／RTL／SDC／scripts／image／工具／artifact／
receipt／manifest／DQE／result event）。**method 專屬的證據不會被改名冒充**：
setup-margin 既有的 `reference_path`／`post_cts_*` 欄位原樣保留在它自己的
verification reference 內，本模組只另外產生一份獨立的 reference 記錄。

artifact 以**邏輯角色**管理（`synthesized_netlist`／`final_netlist`）。角色到
檔名的 allowlist 與它的版本由本 Package 固定，Profile 以 `version`＋`digest`
釘住（因此進入 task digest）。解析規則是**精確相對路徑比對**：不 glob、不模糊
搜尋、不「挑最近的檔」；路徑必須落在本次 task-owned output root 內，且不得經
symlink 逃離該 root。allowlist 內恰好一個存在才採用，兩個都在是
`ARTIFACT_ROLE_AMBIGUOUS`，一個都沒有是
`BASELINE_REFERENCE_ARTIFACT_NAME_MISMATCH`——兩者都 fail closed。

任何一項宣告要求的輸入缺席、讀不到或 hash 對不上，一律拋出——不補值、不改寫、
也不從別的 run 搬。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: reference 的種類與 schema 身分。
REFERENCE_KIND = "orfs.baseline_functional_reference"
SCHEMA_VERSION = "orfs.baseline_functional_reference.v2"
PACKAGE_ID = "orfs_run_intelligence"
PACKAGE_VERSION = "eap3.orfs.v1"
#: 落在 task-owned reference 區的檔名。
REFERENCE_FILENAME = "terminux_baseline_functional_reference.json"

# ── artifact role mapping（Package 固定、Profile 釘住、進 task digest）──

#: 角色 → 這個角色**允許**的精確相對檔名（相對於本次 run 的 results 目錄）。
#: `1_synth.v` 與 `1_2_yosys.v` 是同一個合成輸出在不同 ORFS revision 的命名；
#: 記錄裡寫下實際採用的那一個，不改名冒充、也不同時接受兩個。
ARTIFACT_ROLE_MAPPING_VERSION = "orfs.artifact_roles.v1"
ARTIFACT_ROLE_MAPPING: dict[str, tuple[str, ...]] = {
    "final_netlist": ("6_final.v",),
    "synthesized_netlist": ("1_synth.v", "1_2_yosys.v"),
}
#: proof kind → 它需要的 artifact 角色。宣告了某個 proof kind，它的輸入角色
#: 就必須全部到位，否則 reference 不成立。
PROOF_KIND_INPUT_ROLES: dict[str, tuple[str, ...]] = {
    "formal_equivalence": ("synthesized_netlist", "final_netlist"),
}
#: 要記錄身分的工具（`terminux_tool_binaries.txt` 由 adapter 於執行當下寫出）。
REQUIRED_TOOL_IDENTITIES: tuple[str, ...] = ("yosys",)

#: 向後相容的唯讀視圖（舊呼叫端用的 (role, filenames) 序列）。
FUNCTIONAL_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (role, names) for role, names in ARTIFACT_ROLE_MAPPING.items())

# ── 拒絕碼 ────────────────────────────────────────────────────────────

#: allowlist 內兩個（含以上）名稱同時存在——不猜哪一個才是本次的合成輸出。
ARTIFACT_ROLE_AMBIGUOUS = "ARTIFACT_ROLE_AMBIGUOUS"
#: allowlist 內一個名稱都不存在。
ARTIFACT_NAME_MISMATCH = "BASELINE_REFERENCE_ARTIFACT_NAME_MISMATCH"
#: 候選路徑（或它的 symlink 目標）落在 task-owned output root 之外。
ARTIFACT_ROLE_PATH_ESCAPE = "ARTIFACT_ROLE_PATH_ESCAPES_OUTPUT_ROOT"
#: Profile 釘住的 role mapping 版本或 digest 與本 Package 的不符。
ARTIFACT_ROLE_MAPPING_UNSUPPORTED = "ARTIFACT_ROLE_MAPPING_UNSUPPORTED"
#: 採用的 artifact 不在本次 run 的 artifact manifest 內，或 hash 不符。
ARTIFACT_NOT_IN_MANIFEST = "BASELINE_REFERENCE_ARTIFACT_NOT_IN_MANIFEST"
#: 宣告要求的輸入（design config／RTL／SDC／scripts／工具／lineage）缺席。
REFERENCE_INPUT_MISSING = "BASELINE_REFERENCE_INPUT_MISSING"
#: fixture（QUALIFICATION_ONLY）永遠不得產生 production reference。
FIXTURE_NOT_PRODUCTION = "BASELINE_REFERENCE_FIXTURE_NOT_PRODUCTION"
#: 宣告的 proof kind 本 Package 不認得，或它的輸入角色不完整。
PROOF_KIND_UNSUPPORTED = "BASELINE_REFERENCE_PROOF_KIND_UNSUPPORTED"


class BaselineReferenceError(RuntimeError):
    """reference 無法由本次 run 的真實輸出組出。一律 fail closed。"""

    def __init__(self, message: str, *, code: str = REFERENCE_INPUT_MISSING) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReferenceInputs:
    """host 由已核准 Profile／binding／run evidence 提供的輸入。"""

    method_identity: str
    extension_version: str
    task_id: str
    task_digest: str
    run_id: str
    slot: str
    profile_id: str
    profile_digest: str
    input_snapshot_root: str
    input_manifest_sha256: str
    input_tree_hash: str
    design_config_relpath: str
    rtl_relpath: str
    sdc_relpath: str
    output_root: str
    platform_id: str
    design_id: str
    variant: str
    #: 舊 Profile 的 image；host-native run 為 `None`，改由
    #: `native_tool_binding_sha256` 表示工具身分。
    docker_image_id: str | None
    reference_dir: str
    #: 本次 run 的執行身分（receipt／artifact manifest／DQE／result event）。
    run_identity: dict[str, Any]
    #: 本次 run 的 artifact manifest 內容：`resolved_path → sha256`。採用的每一個
    #: artifact 都必須以同一個檔案與同一個 hash 出現在裡面。
    artifact_manifest_entries: dict[str, str] = field(default_factory=dict)
    #: method extension 宣告的 proof kind（決定哪些角色必須到位）。
    required_proof_kinds: tuple[str, ...] = ()
    #: Profile 釘住的 role mapping（`{"version": ..., "digest": ...}`）。
    #: `None` ⇒ 未釘住，採用本 Package 現行 mapping 並如實記錄。
    artifact_role_mapping: dict[str, Any] | None = None
    #: `True` ⇒ 這不是 production 執行，不得產生 production reference。
    qualification_fixture: bool = False
    native_tool_binding_sha256: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _file_identity(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise BaselineReferenceError(f"{label} is missing: {path}",
                                     code=REFERENCE_INPUT_MISSING)
    return {"path": str(path), "sha256": _sha256(path),
            "bytes": path.stat().st_size}


def _tree_digest(root: Path, label: str) -> dict[str, Any]:
    if not root.is_dir():
        raise BaselineReferenceError(f"{label} is missing: {root}",
                                     code=REFERENCE_INPUT_MISSING)
    combined = hashlib.sha256()
    count = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        combined.update(str(path.relative_to(root)).encode("utf-8"))
        combined.update(_sha256(path).encode("ascii"))
        count += 1
    if not count:
        raise BaselineReferenceError(f"{label} has no files: {root}",
                                     code=REFERENCE_INPUT_MISSING)
    return {"root": str(root), "files": count, "digest": combined.hexdigest()}


# ── role mapping 的身分與解析 ─────────────────────────────────────────

def artifact_role_mapping_document() -> dict[str, Any]:
    """本 Package 固定的 role → allowlist 文件（canonical 形狀）。"""
    return {"version": ARTIFACT_ROLE_MAPPING_VERSION,
            "roles": {role: list(names)
                      for role, names in sorted(ARTIFACT_ROLE_MAPPING.items())}}


def artifact_role_mapping_digest() -> str:
    """role mapping 文件的 canonical SHA-256（Profile 用它釘住 allowlist）。"""
    return hashlib.sha256(_canonical(artifact_role_mapping_document())).hexdigest()


def resolve_role_mapping(pinned: Any) -> dict[str, Any]:
    """核對 Profile 釘住的 role mapping；不符就拒絕，不降級沿用預設。"""
    document = artifact_role_mapping_document()
    digest = artifact_role_mapping_digest()
    current = {"version": document["version"], "digest": digest}
    if pinned is None:
        return {**current, "pinned_by_profile": False}
    if not isinstance(pinned, dict) or set(pinned) != {"version", "digest"}:
        raise BaselineReferenceError(
            "the pinned artifact role mapping must declare exactly "
            "{'version', 'digest'}", code=ARTIFACT_ROLE_MAPPING_UNSUPPORTED)
    if pinned["version"] != current["version"]:
        raise BaselineReferenceError(
            f"the pinned artifact role mapping version {pinned['version']!r} is "
            f"not the one this package provides ({current['version']!r})",
            code=ARTIFACT_ROLE_MAPPING_UNSUPPORTED)
    if pinned["digest"] != digest:
        raise BaselineReferenceError(
            "the pinned artifact role mapping digest does not match this "
            "package's allowlist", code=ARTIFACT_ROLE_MAPPING_UNSUPPORTED)
    return {**current, "pinned_by_profile": True}


@dataclass(frozen=True)
class ResolvedArtifact:
    """一個邏輯角色在本次 run 實際採用的檔案。"""

    role: str
    filename: str
    relative_path: str
    path: Path
    resolved_path: Path


def resolve_artifact_role(role: str, *, results_root: str | Path,
                          output_root: str | Path) -> ResolvedArtifact:
    """在 task-owned output root 內以**精確相對路徑**解析一個 artifact 角色。

    不使用 glob、不模糊比對、不依 mtime 挑檔。allowlist 內恰好一個存在才採用。
    """
    allowed = ARTIFACT_ROLE_MAPPING.get(role)
    if not allowed:
        raise BaselineReferenceError(
            f"{role!r} is not a known artifact role",
            code=ARTIFACT_ROLE_MAPPING_UNSUPPORTED)
    root = Path(output_root).resolve()
    base = Path(results_root)
    try:
        base.resolve().relative_to(root)
    except ValueError:
        raise BaselineReferenceError(
            f"the results directory {base} is not inside the task-owned output "
            f"root {root}", code=ARTIFACT_ROLE_PATH_ESCAPE) from None

    present: list[ResolvedArtifact] = []
    for name in allowed:
        candidate = base / name
        if not candidate.exists():
            # 不存在（含指向不存在目標的 symlink）就是不存在；不另尋替代檔名。
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise BaselineReferenceError(
                f"the {role} candidate {candidate} resolves to {resolved}, "
                f"outside the task-owned output root {root}",
                code=ARTIFACT_ROLE_PATH_ESCAPE) from None
        if not resolved.is_file():
            continue
        present.append(ResolvedArtifact(
            role=role, filename=name,
            relative_path=str(candidate.relative_to(root)),
            path=candidate, resolved_path=resolved))

    if len(present) > 1:
        raise BaselineReferenceError(
            f"the {role} role is ambiguous: "
            f"{[row.relative_path for row in present]} are all present; this run "
            f"must produce exactly one of {list(allowed)}",
            code=ARTIFACT_ROLE_AMBIGUOUS)
    if not present:
        raise BaselineReferenceError(
            f"baseline {role} is missing: none of "
            f"{[str(base / name) for name in allowed]} exists",
            code=ARTIFACT_NAME_MISMATCH)
    return present[0]


def _manifest_lookup(entries: Any) -> dict[str, str]:
    """把 host 提供的 artifact manifest 內容正規化成 `realpath → sha256`。"""
    lookup: dict[str, str] = {}
    for path, digest in dict(entries or {}).items():
        if not path or not digest:
            continue
        lookup[str(Path(str(path)).resolve())] = str(digest)
    return lookup


def read_tool_identities(output_root: str | Path) -> dict[str, dict[str, str]]:
    """讀回 adapter 在執行當下寫出的工具身分（realpath／sha256／version）。"""
    path = Path(output_root) / "terminux_tool_binaries.txt"
    if not path.is_file():
        raise BaselineReferenceError(f"tool identity evidence is missing: {path}",
                                     code=REFERENCE_INPUT_MISSING)
    identities: dict[str, dict[str, str]] = {}
    for line in path.read_text(errors="replace").splitlines():
        if ":" not in line or line.startswith("#"):
            continue
        name, _, value = line.partition(":")
        tool, _, field_name = name.strip().partition(".")
        if tool and field_name:
            identities.setdefault(tool, {})[field_name] = value.strip()
    return identities


def build_reference(inputs: ReferenceInputs) -> dict[str, Any]:
    """由一次真實 baseline run 的輸出組出 production reference 並落盤。"""
    if inputs.qualification_fixture:
        raise BaselineReferenceError(
            "a QUALIFICATION_ONLY fixture profile must not produce a production "
            "baseline functional reference", code=FIXTURE_NOT_PRODUCTION)
    mapping = resolve_role_mapping(inputs.artifact_role_mapping)
    proof_kinds = tuple(inputs.required_proof_kinds)
    if not proof_kinds:
        raise BaselineReferenceError(
            "the method declares no required proof kind; a production baseline "
            "reference has nothing to attest", code=PROOF_KIND_UNSUPPORTED)
    unknown = [kind for kind in proof_kinds if kind not in PROOF_KIND_INPUT_ROLES]
    if unknown:
        raise BaselineReferenceError(
            f"this package cannot provide inputs for proof kind(s) {unknown}",
            code=PROOF_KIND_UNSUPPORTED)

    snapshot = Path(inputs.input_snapshot_root)
    results = (Path(inputs.output_root) / "results" / inputs.platform_id
               / inputs.design_id / inputs.variant)
    reference_dir = Path(inputs.reference_dir)
    reference_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries = _manifest_lookup(inputs.artifact_manifest_entries)
    if not manifest_entries:
        raise BaselineReferenceError(
            "this run carries no artifact manifest content to attest the "
            "baseline artifacts against", code=ARTIFACT_NOT_IN_MANIFEST)

    artifacts: dict[str, Any] = {}
    for role in sorted(ARTIFACT_ROLE_MAPPING):
        found = resolve_artifact_role(role, results_root=results,
                                      output_root=inputs.output_root)
        identity = _file_identity(found.path, f"baseline {role}")
        attested = manifest_entries.get(str(found.resolved_path))
        if attested is None:
            raise BaselineReferenceError(
                f"the baseline {role} {found.relative_path} is not recorded in "
                f"this run's artifact manifest", code=ARTIFACT_NOT_IN_MANIFEST)
        if attested != identity["sha256"]:
            raise BaselineReferenceError(
                f"the baseline {role} {found.relative_path} does not match the "
                f"hash recorded in this run's artifact manifest",
                code=ARTIFACT_NOT_IN_MANIFEST)
        target = reference_dir / f"baseline_{found.filename}"
        shutil.copyfile(found.path, target)
        copied = _sha256(target)
        if copied != identity["sha256"]:
            raise BaselineReferenceError(
                f"the copied baseline {role} does not match its source hash",
                code=REFERENCE_INPUT_MISSING)
        artifacts[role] = {
            "role": role, "filename": found.filename,
            "relative_path": found.relative_path,
            "resolved_path": str(found.resolved_path),
            "source": identity, "reference_path": str(target), "sha256": copied,
            "artifact_manifest_attested": True,
            "artifact_manifest_sha256": attested,
        }

    tools = read_tool_identities(inputs.output_root)
    tool_identity: dict[str, Any] = {}
    for name in REQUIRED_TOOL_IDENTITIES:
        row = tools.get(name) or {}
        if not row.get("sha256") or not row.get("realpath"):
            raise BaselineReferenceError(
                f"the {name} binary identity was not recorded by this run",
                code=REFERENCE_INPUT_MISSING)
        tool_identity[name] = {"realpath": row.get("realpath"),
                               "sha256": row.get("sha256"),
                               "version": row.get("version")}

    run_identity = dict(inputs.run_identity)
    required_lineage = ("receipt_id", "receipt_content_hash", "artifact_manifest_id",
                        "dqe_dispatch_key", "result_event_id", "result_sha256")
    missing = [key for key in required_lineage if not run_identity.get(key)]
    if missing:
        raise BaselineReferenceError(
            f"this run has no complete evidence lineage: missing {missing}",
            code=REFERENCE_INPUT_MISSING)
    if run_identity.get("run_id") != inputs.run_id:
        raise BaselineReferenceError(
            "the run identity does not belong to this run",
            code=REFERENCE_INPUT_MISSING)

    reference = {
        "schema_version": SCHEMA_VERSION,
        "reference_kind": REFERENCE_KIND,
        "package": {"package_id": PACKAGE_ID, "version": PACKAGE_VERSION},
        "method": {"method_identity": inputs.method_identity,
                   "extension_version": inputs.extension_version},
        "baseline": {"task_id": inputs.task_id, "task_digest": inputs.task_digest,
                     "run_id": inputs.run_id, "slot": inputs.slot,
                     "profile_id": inputs.profile_id,
                     "profile_digest": inputs.profile_digest},
        "input": {"snapshot_root": str(snapshot),
                  "manifest_sha256": inputs.input_manifest_sha256,
                  "tree_hash": inputs.input_tree_hash,
                  "design_config": _file_identity(
                      snapshot / inputs.design_config_relpath, "design config"),
                  "rtl": _file_identity(snapshot / inputs.rtl_relpath, "RTL source"),
                  "sdc": _file_identity(snapshot / inputs.sdc_relpath, "SDC source")},
        "scripts": _tree_digest(snapshot / "flow" / "scripts", "ORFS scripts"),
        "artifact_role_mapping": {**mapping,
                                  "roles": artifact_role_mapping_document()["roles"]},
        "required_proof_kinds": list(proof_kinds),
        "proof_inputs": {
            kind: {role: artifacts[role]["sha256"]
                   for role in PROOF_KIND_INPUT_ROLES[kind]}
            for kind in proof_kinds},
        "formal_inputs": {role: body["sha256"] for role, body in artifacts.items()},
        "image": ({"docker_image_id": inputs.docker_image_id}
                  if inputs.native_tool_binding_sha256 is None
                  else {"docker_image_id": None,
                        "native_tool_binding_sha256": inputs.native_tool_binding_sha256}),
        "tools": tool_identity,
        "artifacts": artifacts,
        "run_identity": run_identity,
        "provenance": {"fixture": False, "openroad_launched": True,
                       "qualification_only": False,
                       "production_readiness": "ESTABLISHED",
                       "evidence_origin": "ORFS"},
    }
    reference["reference_digest"] = hashlib.sha256(
        _canonical(reference)).hexdigest()
    target = reference_dir / REFERENCE_FILENAME
    target.write_text(json.dumps(reference, ensure_ascii=False, sort_keys=True,
                                 indent=1), encoding="utf-8")
    reference["record_path"] = str(target)
    return reference


def _verify_role_mapping(reference: dict[str, Any], expected: dict[str, Any],
                         problems: list[str]) -> None:
    """role mapping 的版本／digest／allowlist 必須與本 Package 現行一致。"""
    mapping = reference.get("artifact_role_mapping") or {}
    document = artifact_role_mapping_document()
    digest = artifact_role_mapping_digest()
    if mapping.get("version") != document["version"]:
        problems.append("the reference artifact role mapping version is not the "
                        "one this package provides")
    if mapping.get("digest") != digest:
        problems.append("the reference artifact role mapping digest does not "
                        "match this package's allowlist")
    if mapping.get("roles") != document["roles"]:
        problems.append("the reference artifact role allowlist differs from this "
                        "package's allowlist")
    wanted = expected.get("artifact_role_mapping")
    if wanted is not None:
        for key in ("version", "digest"):
            if wanted.get(key) is not None and mapping.get(key) != wanted[key]:
                problems.append(f"the reference artifact role mapping {key} does "
                                f"not match the one this task pinned")

    artifacts = reference.get("artifacts") or {}
    for role, allowed in sorted(ARTIFACT_ROLE_MAPPING.items()):
        body = artifacts.get(role) or {}
        relative = str(body.get("relative_path") or "")
        if not relative:
            problems.append(f"the reference does not record which file the "
                            f"{role} role resolved to")
        elif Path(relative).name not in allowed:
            problems.append(f"the reference {role} file name is outside this "
                            f"package's allowlist")
        if not body.get("artifact_manifest_attested"):
            problems.append(f"the reference {role} was never attested against "
                            f"the run's artifact manifest")
        if not body.get("sha256"):
            problems.append(f"the reference {role} carries no hash")


def _verify_proof_kinds(reference: dict[str, Any], expected: dict[str, Any],
                        problems: list[str]) -> None:
    """宣告的 proof kind 與它們的輸入角色都必須到位。"""
    declared = list(reference.get("required_proof_kinds") or [])
    if not declared:
        problems.append("the reference declares no required proof kind")
    wanted = expected.get("required_proof_kinds")
    if wanted is not None and list(wanted) != declared:
        problems.append("the reference proof kinds differ from the ones this "
                        "method declares")
    formal = reference.get("formal_inputs") or {}
    proof_inputs = reference.get("proof_inputs") or {}
    artifacts = reference.get("artifacts") or {}
    for kind in declared:
        roles = PROOF_KIND_INPUT_ROLES.get(kind)
        if roles is None:
            problems.append(f"the reference declares the unsupported proof kind "
                            f"{kind!r}")
            continue
        recorded = proof_inputs.get(kind) or {}
        for role in roles:
            digest = (artifacts.get(role) or {}).get("sha256")
            if not digest or not formal.get(role):
                problems.append(f"the reference lacks the {role} input required "
                                f"by proof kind {kind!r}")
            elif recorded.get(role) != digest:
                problems.append(f"the reference {role} input for proof kind "
                                f"{kind!r} does not match its artifact hash")


def verify_reference(reference: Any, *, expected: dict[str, Any]) -> list[str]:
    """核對保存的 reference 是否仍屬於這個 task／run、輸入與工具身分。

    回傳問題清單（空清單＝通過）。呼叫端一律在授權 candidate **之前**呼叫它，
    有任何一項不符就拒絕。
    """
    problems: list[str] = []
    if not isinstance(reference, dict):
        return ["the baseline functional reference is missing"]
    if (reference.get("schema_version") != SCHEMA_VERSION
            or reference.get("reference_kind") != REFERENCE_KIND):
        return ["the baseline functional reference schema is unsupported"]
    provenance = reference.get("provenance") or {}
    if (provenance.get("fixture") or provenance.get("qualification_only")
            or not provenance.get("openroad_launched")):
        problems.append("a fixture reference may never be used as a production "
                        "baseline reference")
    _verify_role_mapping(reference, expected, problems)
    _verify_proof_kinds(reference, expected, problems)
    baseline = reference.get("baseline") or {}
    for key in ("task_id", "run_id"):
        if expected.get(key) is not None and baseline.get(key) != expected[key]:
            problems.append(f"the reference {key} does not match this baseline")
    method = reference.get("method") or {}
    if (expected.get("method_identity") is not None
            and method.get("method_identity") != expected["method_identity"]):
        problems.append("the reference was produced for another method")
    source_input = reference.get("input") or {}
    for key, label in (("manifest_sha256", "input manifest"),
                       ("tree_hash", "input tree")):
        if (expected.get(key) is not None
                and source_input.get(key) != expected[key]):
            problems.append(f"the reference {label} differs from this task")
    for key, label in (("rtl_sha256", "rtl"), ("sdc_sha256", "sdc"),
                       ("design_config_sha256", "design_config")):
        wanted = expected.get(key)
        if wanted is not None and (source_input.get(label) or {}).get(
                "sha256") != wanted:
            problems.append(f"the reference {label} hash differs from this task")
    if (expected.get("scripts_digest") is not None
            and (reference.get("scripts") or {}).get("digest")
            != expected["scripts_digest"]):
        problems.append("the reference ORFS scripts revision differs from this task")
    if (expected.get("docker_image_id") is not None
            and (reference.get("image") or {}).get("docker_image_id")
            != expected["docker_image_id"]):
        problems.append("the reference image identity differs from this task")
    # host-native：工具身分是 binding digest。舊（image）reference 沒有這個值，
    # 因此不會被當成同一組工具重用。
    if (expected.get("native_tool_binding_sha256") is not None
            and (reference.get("image") or {}).get("native_tool_binding_sha256")
            != expected["native_tool_binding_sha256"]):
        problems.append("the reference native tool binding differs from this task")
    tools = reference.get("tools") or {}
    for name in REQUIRED_TOOL_IDENTITIES:
        row = tools.get(name) or {}
        if not row.get("sha256"):
            problems.append(f"the reference has no {name} binary identity")
        wanted = (expected.get("tool_sha256") or {}).get(name)
        if wanted is not None and row.get("sha256") != wanted:
            problems.append(f"the reference {name} binary identity differs")
    run_identity = reference.get("run_identity") or {}
    for key in ("receipt_id", "receipt_content_hash", "artifact_manifest_id",
                "dqe_dispatch_key", "result_event_id"):
        if not run_identity.get(key):
            problems.append(f"the reference evidence lineage lacks {key}")
        wanted = (expected.get("run_identity") or {}).get(key)
        if wanted is not None and run_identity.get(key) != wanted:
            problems.append(f"the reference {key} does not match this run")
    # input 與 scripts 也要重新核對，來源漂移必須看得出來。
    for label in ("design_config", "rtl", "sdc"):
        body = (reference.get("input") or {}).get(label) or {}
        path = Path(str(body.get("path") or ""))
        if not path.is_file():
            problems.append(f"the reference {label} source is no longer readable")
        elif _sha256(path) != body.get("sha256"):
            problems.append(f"the reference {label} source hash no longer matches")
    scripts = reference.get("scripts") or {}
    scripts_root = Path(str(scripts.get("root") or ""))
    if not scripts_root.is_dir():
        problems.append("the reference ORFS scripts tree is no longer readable")
    else:
        try:
            current = _tree_digest(scripts_root, "ORFS scripts")
        except BaselineReferenceError:
            problems.append("the reference ORFS scripts tree is no longer readable")
        else:
            if current["digest"] != scripts.get("digest"):
                problems.append("the reference ORFS scripts revision no longer matches")
    for role, body in (reference.get("artifacts") or {}).items():
        path = Path(str((body or {}).get("reference_path") or ""))
        if not path.is_file() or _sha256(path) != (body or {}).get("sha256"):
            problems.append(f"the retained baseline {role} no longer matches its hash")
    return problems


__all__ = ["ARTIFACT_NAME_MISMATCH", "ARTIFACT_NOT_IN_MANIFEST",
           "ARTIFACT_ROLE_AMBIGUOUS", "ARTIFACT_ROLE_MAPPING",
           "ARTIFACT_ROLE_MAPPING_UNSUPPORTED", "ARTIFACT_ROLE_MAPPING_VERSION",
           "ARTIFACT_ROLE_PATH_ESCAPE", "BaselineReferenceError",
           "FIXTURE_NOT_PRODUCTION", "FUNCTIONAL_ARTIFACTS", "PACKAGE_ID",
           "PACKAGE_VERSION", "PROOF_KIND_INPUT_ROLES", "PROOF_KIND_UNSUPPORTED",
           "REFERENCE_FILENAME", "REFERENCE_INPUT_MISSING", "REFERENCE_KIND",
           "REQUIRED_TOOL_IDENTITIES", "SCHEMA_VERSION", "ReferenceInputs",
           "ResolvedArtifact", "artifact_role_mapping_digest",
           "artifact_role_mapping_document", "build_reference",
           "read_tool_identities", "resolve_artifact_role", "resolve_role_mapping",
           "verify_reference"]
