# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
import logging
import re
from collections import Counter
from pathlib import Path

from terminux_engine.isvn.coordinate import resolve_coordinate
from terminux_engine.lineage.attach import attach_lineage
from terminux_engine.lineage.branch_tree import check_sibling_divergence
from terminux_engine.lineage.ledger import default_ledger_path
from terminux_engine.lineage.models import CheckResult, Finding, JobIntent
from terminux_engine.lineage.rule_loader import LineageRule, load_lineage_rules
from terminux_engine.lineage.snapshot import build_snapshot
from terminux_engine.lineage.types import ANCHOR_KEYS, SUMMARY_ROLES
from terminux_engine.resource_paths import bundled_knowledge_path

_log = logging.getLogger(__name__)


def get_default_rules_path() -> Path:
    """取得發行套件內的唯讀 lineage 規則。"""
    return bundled_knowledge_path("lineage_rules.yaml")


def _rules_by_id(rules: list[LineageRule]) -> dict[str, LineageRule]:
    return {r.rule_id: r for r in rules}


def check_synthesis_preflight(
    intent: JobIntent,
    project_root: str | Path,
    ledger_path: str | Path | None = None,
    rules_path: str | Path | None = None,
) -> CheckResult:
    """
    對 intent 的所有 declared_inputs 做 lineage 一致性檢查，回傳 CheckResult。
    rules_path：若 None，使用 get_default_rules_path()。
    """
    if ledger_path is None:
        ledger_path = default_ledger_path(project_root)

    # 載入規則
    _rp = Path(rules_path) if rules_path is not None else get_default_rules_path()
    rules = _rules_by_id(load_lineage_rules(_rp))

    rule_same_anchor = rules.get("synthesis_inputs_same_anchor")
    rule_must_have_cksum = rules.get("summary_must_have_source_rtl_checksum")
    rule_missing_is_insuff = rules.get("missing_anchor_is_insufficient_evidence")

    # Step 1：build + attach 所有 inputs
    snaps = []
    isvn_project = Path.home().parent.name
    for d in intent.declared_inputs:
        raw = build_snapshot(d["path"], role=d["role"])
        try:
            coordinate = resolve_coordinate(raw.path, isvn_project)
        except Exception as exc:
            _log.warning("isvn coordinate resolution failed for %s: %s", raw.path, exc)
        else:
            if coordinate is not None and coordinate.resolved_by == "file_query":
                raw = raw.model_copy(update={"isvn_coordinate": coordinate})
        attached = attach_lineage(raw, ledger_path)
        snaps.append(attached)

    # Step 2：target anchors（直接取 root input 的 checksum）
    target_rtl = next((s.checksum_sha256 for s in snaps if s.role == "rtl"), None)
    target_sdc = next((s.checksum_sha256 for s in snaps if s.role == "sdc"), None)
    target_upf = next((s.checksum_sha256 for s in snaps if s.role == "upf"), None)

    findings: list[Finding] = []
    overall_status: str = "CONSISTENT"

    # Step 3：逐 snap 判定
    for snap in snaps:
        if snap.role in SUMMARY_ROLES:
            rtl_anchors = snap.lineage_anchors.get("rtl", [])
            if not rtl_anchors:
                # 缺 anchor：依 missing_anchor_is_insufficient_evidence 規則處理
                if rule_missing_is_insuff is None or rule_missing_is_insuff.enabled:
                    base_msg = (rule_must_have_cksum.message
                                if rule_must_have_cksum and rule_must_have_cksum.enabled
                                else "summary artifact has no recorded rtl anchor")
                    # 在 rule message 後附 What/Which/Why/How 補強
                    msg = _enrich_missing_anchor_message(base_msg, snap)
                    findings.append(Finding(
                        severity="medium",
                        status="INSUFFICIENT_EVIDENCE",
                        role=snap.role,
                        path=snap.path,
                        message=msg,
                        category="MISSING_CHECKSUM",
                        suggestion=(
                            "Check whether the producing job recorded "
                            "source_rtl_checksum; if not, update the producer "
                            "tool/script or re-run the upstream step with "
                            "current ledger configuration."
                        ),
                        context=_finding_context(snap),
                    ))
                    if overall_status == "CONSISTENT":
                        overall_status = "INSUFFICIENT_EVIDENCE"
                # disabled 時沿用既有行為（INSUFFICIENT_EVIDENCE），同上
                else:
                    msg = _enrich_missing_anchor_message(
                        "summary artifact has no recorded rtl anchor", snap,
                    )
                    findings.append(Finding(
                        severity="medium",
                        status="INSUFFICIENT_EVIDENCE",
                        role=snap.role,
                        path=snap.path,
                        message=msg,
                        category="MISSING_CHECKSUM",
                        suggestion=(
                            "Check whether the producing job recorded "
                            "source_rtl_checksum; if not, update the producer "
                            "tool/script or re-run the upstream step."
                        ),
                        context=_finding_context(snap),
                    ))
                    if overall_status == "CONSISTENT":
                        overall_status = "INSUFFICIENT_EVIDENCE"
            elif target_rtl is not None and target_rtl not in rtl_anchors:
                # anchor 不一致：依 synthesis_inputs_same_anchor 規則處理
                if rule_same_anchor is None or rule_same_anchor.enabled:
                    base_msg = (rule_same_anchor.message
                                if rule_same_anchor
                                else "summary artifact rtl anchor does not match synthesis rtl input")
                    # 附上具體 checksum + 操作建議
                    msg = _enrich_anchor_mismatch_message(
                        base_msg, snap, rtl_anchors, target_rtl,
                    )
                    findings.append(Finding(
                        severity="high",
                        status="MIXED_LINEAGE",
                        role=snap.role,
                        path=snap.path,
                        message=msg,
                        expected_rtl_checksum=target_rtl,
                        observed_rtl_anchors=rtl_anchors,
                        category="ANCHOR_MISMATCH",
                        suggestion=(
                            f"Replace '{snap.path}' with an artifact produced "
                            f"from the same RTL branch (matching expected "
                            f"checksum {target_rtl[:16]}...), or re-run the "
                            f"upstream step using the current RTL."
                        ),
                        context=_finding_context(snap),
                    ))
                    overall_status = "MIXED_LINEAGE"
                # rule disabled → 不執行 anchor 一致性檢查，略過此 snap

        # role in ('rtl', 'sdc', 'upf') → skip（自身為 root anchor）
        # 其他 role → skip

    # ─── Step 3.5：sibling divergence（topology check）─────────────────────
    # sibling divergence 規則 2.4
    input_cksums = [s.checksum_sha256 for s in snaps]
    sd_status, divergences, unmapped = check_sibling_divergence(
        input_cksums, tree_path=None,
    )

    rule_no_sib = rules.get("inputs_no_sibling_divergence")
    sib_enabled = (rule_no_sib is None) or rule_no_sib.enabled

    if sib_enabled and sd_status == "MIXED_LINEAGE":
        sib_base = (
            rule_no_sib.message if rule_no_sib
            else "inputs diverge at sibling branches under common ancestor"
        )
        for d in divergences:
            path_a = next(
                (s.path for s in snaps if s.checksum_sha256 == d["input_a_checksum"]),
                "",
            )
            path_b = next(
                (s.path for s in snaps if s.checksum_sha256 == d["input_b_checksum"]),
                "",
            )
            # 附上 LCA + divergent paths + actionable hint
            sib_msg = (
                f"{sib_base} "
                f"Inputs '{path_a}' and '{path_b}' branch off from common ancestor "
                f"'{d['lca_branch_id']}' but follow divergent paths. "
                f"To fix: re-align these inputs to the same branch by re-running "
                f"one of them from the common ancestor, or replace the stale one "
                f"with a version from the same branch as the other."
            )
            findings.append(Finding(
                severity="high",
                status="MIXED_LINEAGE",
                message=sib_msg,
                kind="sibling_divergence",
                lca_branch_id=d["lca_branch_id"],
                divergent_inputs=[path_a, path_b],
                category="SIBLING_DIVERGENCE",
                suggestion=(
                    "Re-align divergent inputs by re-running one from the "
                    "common ancestor, or replace the stale artifact."
                ),
                context={
                    "lca_branch_id": d["lca_branch_id"],
                    "input_a": path_a,
                    "input_b": path_b,
                },
            ))
        overall_status = "MIXED_LINEAGE"

    # ─── Step 3.6：path-based branch detection ─────────────
    # 純看路徑字串、零 ledger 依賴的 heuristic。彌補 checksum-based 檢查在
    # 缺 source_rtl_checksum 時完全放棄分析的盲區。與 sibling
    # divergence（checksum topology）互不干擾、可並存 emit。
    path_findings = _detect_path_based_branch_mismatch(snaps)
    if path_findings:
        findings.extend(path_findings)
        overall_status = "MIXED_LINEAGE"

    # ─── Step 3.5b：unverified topology（input 無 branch_node）─────────────
    # info-level、不升級 overall_status；提示使用者 N 個 inputs 中有 M 個無法 topology 驗證
    for ck in unmapped:
        path = next((s.path for s in snaps if s.checksum_sha256 == ck), "")
        findings.append(Finding(
            severity="info",
            status="CONSISTENT",
            message="input has no branch_node; topology check skipped",
            path=path,
            kind="unverified_topology",
            category="UNVERIFIED_TOPOLOGY",
            context={"input": path} if path else {},
        ))

    # Step 4：若無 medium/high 嚴重度 finding 且 overall 為 CONSISTENT，補 closing
    has_severe = any(f.severity in ("medium", "high") for f in findings)
    if not has_severe and overall_status == "CONSISTENT":
        findings.append(Finding(
            severity="info",
            status="CONSISTENT",
            message="all FE synthesis inputs align with the same rtl anchor",
            category="ALL_INPUTS_ALIGNED",
        ))

    return CheckResult(
        run_file=intent.run_file,
        checked_stage=intent.stage,
        target_anchors={"rtl": target_rtl, "sdc": target_sdc, "upf": target_upf},
        status=overall_status,  # type: ignore[arg-type]
        findings=findings,
        summary=_build_check_summary(overall_status, findings),  # type: ignore[arg-type]
    )


# ─── Finding.message 充實 helper ───────────────────────────────
#
# 目標：把 yaml rule.message 與動態 context（path / role / checksum / actionable
# hint）拼接，讓人類看 NotificationBar 與 Agent 看 chat prompt 都能直接行動。
# Pattern：rule.message 仍保留在 Finding.message 開頭（既有 test 用 `in` 斷言
# 仍 pass），後面附加 What/Which/Why/How 區塊。


def _enrich_missing_anchor_message(base_msg: str, snap) -> str:
    """summary artifact 缺 rtl anchor → 補 path / role / 修正建議。"""
    producer = getattr(snap, "producer_job_id", None) or "(unknown)"
    return (
        f"{base_msg} "
        f"Artifact '{snap.path}' (role: {snap.role}) does not have a recorded "
        f"rtl anchor. This artifact was produced by job '{producer}'. "
        f"Check: was this artifact produced by a job that recorded source RTL "
        f"checksum? If not, the producing tool or script may need to be updated "
        f"to include RTL checksum in its output, or re-run the upstream step "
        f"with current ledger configuration."
    )


def _enrich_anchor_mismatch_message(
    base_msg: str, snap, observed_anchors: list[str], expected_rtl: str,
) -> str:
    """MIXED_LINEAGE → 補 path / role / actual + expected checksum / 修正建議。"""
    producer = getattr(snap, "producer_job_id", None) or "(unknown)"
    observed_preview = ", ".join(observed_anchors[:3]) if observed_anchors else "(none)"
    return (
        f"{base_msg} "
        f"Artifact '{snap.path}' (role: {snap.role}) has source_rtl_checksum "
        f"in [{observed_preview}] which belongs to a different RTL version "
        f"than the synthesis target (expected: {expected_rtl}). This artifact "
        f"was produced by job '{producer}'. To fix: either replace this "
        f"artifact with one produced from the same RTL branch (matching the "
        f"expected checksum), or re-run the upstream step that produced it "
        f"using the current RTL."
    )


# ─── path-based branch detection helpers ──────────────────────


# 抓 path segment 形如 /branch_A/ /branch_main/ /br_xyz/ 等
# 用 negative lookahead 防 'branches' 等更長的字串：必須是完整 segment
_BRANCH_TAG_PATTERN = re.compile(r"/(branch_[A-Za-z0-9_-]+|br_[A-Za-z0-9_-]+)/")


def _extract_branch_tag(path: str) -> str | None:
    """從 artifact path 找出 branch tag。失敗回 None。

    支援的 pattern：
        /…/branch_<id>/…  → branch_<id>
        /…/br_<id>/…       → br_<id>

    Heuristic，不保證 100% 抓對 — 真實 IC 專案的目錄命名沒統一標準。
    之後可考慮擴展，或從 playbook 帶入 pattern。
    """
    if not path:
        return None
    # 確保起始 '/' 與結尾 '/' 都能匹配（normalize）
    normalized = path if path.startswith("/") else "/" + path
    if not normalized.endswith("/"):
        normalized = normalized + "/"
    m = _BRANCH_TAG_PATTERN.search(normalized)
    return m.group(1) if m else None


def _detect_path_based_branch_mismatch(snaps: list) -> list[Finding]:
    """對 input snaps 跑 path-based branch detection。

    流程：
    1. 對每個 snap 抽 branch_tag（None / branch_A / branch_B / ...）
    2. 把有 tag 的拿出來，數 majority tag
    3. tag != majority 的 snap → emit BRANCH_MISMATCH Finding
    4. 若全部 snap 同 tag 或 < 2 個 distinct tag → 不 emit（單 branch OK）
    """
    tagged: list[tuple] = []  # (snap, tag)
    for snap in snaps:
        tag = _extract_branch_tag(getattr(snap, "path", "") or "")
        if tag:
            tagged.append((snap, tag))
    if len(tagged) < 2:
        return []
    counts = Counter(tag for _, tag in tagged)
    distinct = set(counts.keys())
    if len(distinct) < 2:
        return []

    # majority = 最多 artifact 所在的 tag；tie 時取字典序最小（deterministic）
    max_n = max(counts.values())
    majority_candidates = sorted(t for t, n in counts.items() if n == max_n)
    majority_tag = majority_candidates[0]

    findings: list[Finding] = []
    for snap, tag in tagged:
        if tag == majority_tag:
            continue
        role = getattr(snap, "role", None)
        path = getattr(snap, "path", "") or ""
        msg = (
            f"Path-based branch detection: {role} at '{path}' appears to be "
            f"from '{tag}', while majority of inputs are from '{majority_tag}'."
        )
        sugg = (
            f"This artifact appears to come from '{tag}' based on its path, "
            f"but other key inputs are from '{majority_tag}'. Replace with a "
            f"version from '{majority_tag}', or confirm this cross-branch "
            f"usage is approved."
        )
        findings.append(Finding(
            severity="high",
            status="MIXED_LINEAGE",
            role=role,
            path=path,
            message=msg,
            kind="path_branch_mismatch",
            category="BRANCH_MISMATCH",
            suggestion=sugg,
            context={
                "detection_method": "path_based",
                "branch_tag": tag,
                "majority_branch": majority_tag,
                "artifact_path": path,
                "artifact_role": role or "",
            },
        ))
    return findings


# ─── 結構化 context / summary helper ──────────────────────────


def _finding_context(snap) -> dict[str, str]:
    """從 ArtifactSnapshot 抽出 context dict 給 Finding.context。"""
    out: dict[str, str] = {}
    if getattr(snap, "path", None):
        out["artifact_path"] = snap.path
    if getattr(snap, "role", None):
        out["artifact_role"] = snap.role
    pid = getattr(snap, "producer_job_id", None)
    if pid:
        out["producer_job_id"] = pid
    pstage = getattr(snap, "producer_stage", None)
    if pstage:
        out["producer_stage"] = pstage
    return out


def _build_check_summary(status: str, findings: list[Finding]) -> str:
    """一句話描述 overall 檢查結果，給 NotificationBar / agent_prompt 顯示。"""
    severe = [f for f in findings if f.severity in ("medium", "high")]
    if status == "MIXED_LINEAGE":
        n = len(severe)
        return f"血緣不一致：{n} 個 input 與 synthesis target 不匹配"
    if status == "INSUFFICIENT_EVIDENCE":
        n = len(severe)
        return f"證據不足：{n} 個 input 缺少必要的血緣資訊"
    return "所有 synthesis 輸入血緣一致"
