# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Branch Tree — Lineage Graph Foundation.

Branch tree 是一個 DAG（有向無環圖），記錄每個 stage job 的 input/output 關係，
支援同一 stage 重跑多次、多個 upstream summary 合併等情境。

儲存位置：~/.terminux/lineage/branch_tree.jsonl（或 TERMINUX_BRANCH_TREE_PATH 環境變數）
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ─── 資料結構 ──────────────────────────────────────────────────────────────────

@dataclass
class ArtifactRef:
    path: str
    checksum: str


@dataclass
class BranchNode:
    branch_id: str
    parent_ids: list[str]
    job_id: str
    tool_name: str
    inputs: list[ArtifactRef]
    outputs: list[ArtifactRef]


# ─── 公開 API ──────────────────────────────────────────────────────────────────

def compute_branch_id(tool_name: str, inputs: list[ArtifactRef]) -> str:
    """計算 branch_id（基於 tool_name + 所有 input checksums 的排序組合）。"""
    keys = sorted(
        f"{tool_name}:{inp.checksum}"
        for inp in inputs
    )
    return hashlib.sha256("|".join(keys).encode()).hexdigest()


def get_branch_tree_path() -> Path:
    """回傳 branch_tree.jsonl 的路徑（env var 優先，否則 default）。"""
    env = os.environ.get("TERMINUX_BRANCH_TREE_PATH", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".terminux" / "lineage" / "branch_tree.jsonl"


def append_branch_node(node: BranchNode, tree_path: str | Path | None = None) -> None:
    """Append 一筆 branch node 到 branch_tree.jsonl。"""
    p = Path(tree_path) if tree_path is not None else get_branch_tree_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "branch_id": node.branch_id,
        "parent_ids": node.parent_ids,
        "job_id": node.job_id,
        "tool_name": node.tool_name,
        "inputs": [{"path": a.path, "checksum": a.checksum} for a in node.inputs],
        "outputs": [{"path": a.path, "checksum": a.checksum} for a in node.outputs],
    }
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_branch_nodes(tree_path: str | Path | None = None) -> list[BranchNode]:
    """載入所有 branch nodes。檔案不存在時回傳空 list。"""
    p = Path(tree_path) if tree_path is not None else get_branch_tree_path()
    if not p.exists():
        return []
    nodes: list[BranchNode] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("branch_tree: 無法解析 JSON 行: %s", line[:80])
                continue
            nodes.append(BranchNode(
                branch_id=d["branch_id"],
                parent_ids=d.get("parent_ids", []),
                job_id=d["job_id"],
                tool_name=d["tool_name"],
                inputs=[ArtifactRef(path=a["path"], checksum=a["checksum"]) for a in d.get("inputs", [])],
                outputs=[ArtifactRef(path=a["path"], checksum=a["checksum"]) for a in d.get("outputs", [])],
            ))
    return nodes


def find_branch_by_output_checksum(
    checksum: str,
    tree_path: str | Path | None = None,
) -> BranchNode | None:
    """找到 outputs 裡包含此 checksum 的最新 branch node。"""
    nodes = load_branch_nodes(tree_path)
    # 反向搜尋，優先回傳最新的
    for node in reversed(nodes):
        for out in node.outputs:
            if out.checksum == checksum:
                return node
    return None


def find_root_branch(
    branch_id: str,
    all_nodes: list[BranchNode],
) -> BranchNode | None:
    """從指定 branch 往上追 parent，回傳 root branch（parent_ids = []）。"""
    by_id: dict[str, BranchNode] = {}
    for n in all_nodes:
        by_id[n.branch_id] = n

    current = by_id.get(branch_id)
    if current is None:
        return None

    # BFS/DFS 往上追，找到第一個 root
    visited: set[str] = set()
    stack = [current]
    roots: list[BranchNode] = []

    while stack:
        node = stack.pop()
        if node.branch_id in visited:
            continue
        visited.add(node.branch_id)

        if not node.parent_ids:
            roots.append(node)
        else:
            for pid in node.parent_ids:
                parent = by_id.get(pid)
                if parent is not None:
                    stack.append(parent)

    # 回傳第一個找到的 root（多個 root 的情況由 check_lineage_consistency 處理）
    return roots[0] if roots else None


def _find_all_roots(
    branch_id: str,
    by_id: dict[str, BranchNode],
) -> list[BranchNode]:
    """從指定 branch 往上追所有 parent，收集全部 root branches。"""
    visited: set[str] = set()
    stack = [branch_id]
    roots: list[BranchNode] = []

    while stack:
        bid = stack.pop()
        if bid in visited:
            continue
        visited.add(bid)
        node = by_id.get(bid)
        if node is None:
            continue
        if not node.parent_ids:
            roots.append(node)
        else:
            for pid in node.parent_ids:
                stack.append(pid)

    return roots


def check_lineage_consistency(
    upstream_output_checksums: list[str],
    tree_path: str | Path | None = None,
) -> tuple[str, str]:
    """
    對所有 upstream summary checksums 做 preflight 判定。

    回傳 (status, message)：
    - ("CONSISTENT", "...")
    - ("MIXED_LINEAGE", "...")
    - ("INSUFFICIENT_EVIDENCE", "...")  ← 找不到 branch node 時

    判定邏輯：
    1. 對每個 checksum，找到對應的 branch node
    2. 往上追到所有 root branches
    3. 收集所有 root 的 inputs（path + checksum）
    4. 若有同一個 path 但不同 checksum → MIXED_LINEAGE
    5. 否則 → CONSISTENT
    """
    nodes = load_branch_nodes(tree_path)
    if not nodes:
        return ("INSUFFICIENT_EVIDENCE", "branch tree is empty")

    by_id: dict[str, BranchNode] = {}
    for n in nodes:
        by_id[n.branch_id] = n

    # 對每個 upstream checksum 找 branch node
    all_root_inputs: dict[str, set[str]] = {}  # path → set of checksums

    for cksum in upstream_output_checksums:
        # 找包含此 output checksum 的 branch node（反向搜尋）
        branch_node: BranchNode | None = None
        for node in reversed(nodes):
            for out in node.outputs:
                if out.checksum == cksum:
                    branch_node = node
                    break
            if branch_node is not None:
                break

        if branch_node is None:
            return (
                "INSUFFICIENT_EVIDENCE",
                f"no branch node found for output checksum {cksum[:16]}...",
            )

        # 往上追到所有 root branches
        roots = _find_all_roots(branch_node.branch_id, by_id)
        if not roots:
            return (
                "INSUFFICIENT_EVIDENCE",
                f"cannot trace root for branch {branch_node.branch_id[:16]}...",
            )

        for root in roots:
            for inp in root.inputs:
                all_root_inputs.setdefault(inp.path, set())
                all_root_inputs[inp.path].add(inp.checksum)

    # 判定：同一 path 有多個不同 checksum → MIXED_LINEAGE
    conflicting_paths: list[str] = []
    for path, checksums in all_root_inputs.items():
        if len(checksums) > 1:
            conflicting_paths.append(path)

    if conflicting_paths:
        paths_str = ", ".join(conflicting_paths)
        return (
            "MIXED_LINEAGE",
            f"root inputs have conflicting checksums for: {paths_str}",
        )

    return ("CONSISTENT", "all upstream summaries trace back to consistent root inputs")


# ─── Sibling Divergence───────────────

def find_ancestors(
    branch_id: str,
    by_id: dict[str, BranchNode],
) -> set[str]:
    """回傳 branch_id 自身 + 所有祖先 branch_id 集合（DAG-safe）。
    branch_id 不在 by_id 時回傳空集合。"""
    visited: set[str] = set()
    stack: list[str] = [branch_id]
    while stack:
        bid = stack.pop()
        if bid in visited:
            continue
        if bid not in by_id:
            continue
        visited.add(bid)
        stack.extend(by_id[bid].parent_ids)
    return visited


def find_lca(
    branch_a: str,
    branch_b: str,
    by_id: dict[str, BranchNode],
) -> BranchNode | None:
    """回傳兩個 branch 在 DAG 中的最深共祖（deepest common ancestor）。
    無共祖回 None；多 LCA 取首個（IC flow tree 罕見）。"""
    a_anc = find_ancestors(branch_a, by_id)
    b_anc = find_ancestors(branch_b, by_id)
    common = a_anc & b_anc
    if not common:
        return None

    # 對 common 中每個候選 c，若無其他 c' ∈ common 滿足「c 是 c' 的祖先」
    # （即 c' 比 c 深），則 c 是 deepest。
    anc_cache: dict[str, set[str]] = {c: find_ancestors(c, by_id) for c in common}
    for c in common:
        is_deepest = True
        for other in common:
            if other == c:
                continue
            # c ∈ ancestors(other) → c 是 other 的祖先 → other 比 c 深
            if c in anc_cache[other]:
                is_deepest = False
                break
        if is_deepest:
            return by_id.get(c)

    # defensive：理論上 common 非空時必有 deepest
    return None


def _child_of_lca_on_path_to(
    target_branch_id: str,
    lca_branch_id: str,
    by_id: dict[str, BranchNode],
) -> str | None:
    """從 target 上溯，回傳 parent_ids 包含 lca 的那個 branch_id（從 LCA 往
    target 走的下一步）。多路徑時回首個發現者。"""
    visited: set[str] = set()
    stack: list[str] = [target_branch_id]
    while stack:
        bid = stack.pop()
        if bid in visited or bid not in by_id:
            continue
        visited.add(bid)
        node = by_id[bid]
        if lca_branch_id in node.parent_ids:
            return bid
        stack.extend(node.parent_ids)
    return None


def check_sibling_divergence(
    input_checksums: list[str],
    tree_path: str | Path | None = None,
) -> tuple[str, list[dict], list[str]]:
    """檢查 inputs 是否分屬 sibling 分支。

    回傳 (status, divergences, unmapped_checksums)：
      status: "CONSISTENT" | "MIXED_LINEAGE"
      divergences: list of dict（見 spec 2.3 SiblingDivergence）
      unmapped_checksums: 在 branch_tree 找不到對應 BranchNode 的 input cksum
    """
    nodes = load_branch_nodes(tree_path)
    if not nodes:
        return ("CONSISTENT", [], list(input_checksums))

    by_id: dict[str, BranchNode] = {n.branch_id: n for n in nodes}

    # 1. 將 input checksum 對映到包含此 cksum 為 output 的最新 BranchNode
    cksum_to_branch: dict[str, str] = {}
    unmapped: list[str] = []
    for ck in input_checksums:
        found_id: str | None = None
        for n in reversed(nodes):
            if any(o.checksum == ck for o in n.outputs):
                found_id = n.branch_id
                break
        if found_id is None:
            unmapped.append(ck)
        else:
            cksum_to_branch[ck] = found_id

    # 2. Pairwise topology check
    mapped: list[tuple[str, str]] = list(cksum_to_branch.items())
    divergences: list[dict] = []
    for i in range(len(mapped)):
        for j in range(i + 1, len(mapped)):
            ck_a, br_a = mapped[i]
            ck_b, br_b = mapped[j]
            # 同 BranchNode（同 tool run 多 outputs）→ 同分支，跳過
            if br_a == br_b:
                continue
            anc_a = find_ancestors(br_a, by_id)
            anc_b = find_ancestors(br_b, by_id)
            # 直系（一個是另一個祖先）→ linear，跳過
            if br_a in anc_b or br_b in anc_a:
                continue
            common = anc_a & anc_b
            # 完全獨立 root → 由 check_lineage_consistency 負責，跳過
            if not common:
                continue
            lca_node = find_lca(br_a, br_b, by_id)
            if lca_node is None:
                # defensive：common 非空但找不到 LCA 不該發生
                continue
            child_a = _child_of_lca_on_path_to(br_a, lca_node.branch_id, by_id) or ""
            child_b = _child_of_lca_on_path_to(br_b, lca_node.branch_id, by_id) or ""
            divergences.append({
                "input_a_checksum": ck_a,
                "input_b_checksum": ck_b,
                "lca_branch_id":    lca_node.branch_id,
                "lca_tool":         lca_node.tool_name,
                "child_on_a_path":  child_a,
                "child_on_b_path":  child_b,
            })

    status = "MIXED_LINEAGE" if divergences else "CONSISTENT"
    return (status, divergences, unmapped)
