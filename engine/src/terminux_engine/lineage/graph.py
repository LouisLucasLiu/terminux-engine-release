# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""Lineage Graph 視覺化 — DAG 建構 + Mermaid 生成 + 互動式 HTML 輸出。

從 ledger 的 producer-consumer 關係建構 DAG，輸出互動式 HTML。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from terminux_engine._version import __version__
from terminux_engine.lineage.mermaid_cdn import MERMAID_SCRIPT_TAG
from terminux_engine.lineage.models import ArtifactSnapshot, JobCommit

# 依 trace_root 著色（Tableau 10 colorblind-safe 去紅，紅色保留給 failure marker）
_TRACE_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
]


def _trace_root_to_color_index(trace_root: str) -> int:
    """Stable hash → palette index。空字串視為無 session（caller 不該呼叫）。"""
    return int(hashlib.md5(trace_root.encode()).hexdigest()[:8], 16) % len(_TRACE_PALETTE)


# ─── DAG Model ───────────────────────────────────────────────────────────────

@dataclass
class GraphNode:
    """DAG 中的 node。"""

    node_id: str
    """job_{job_id} 或 art_{checksum[:12]}"""
    node_type: str
    """'job' 或 'artifact'"""
    label: str
    """顯示標籤"""
    block_name: str | None = None
    """所屬 block（用於 subgraph 分組）"""
    trace_root: str = ""
    """JobCommit 的 trace_root（artifact node 留空）。多 session graph 著色用。"""


@dataclass
class GraphEdge:
    """DAG 中的 edge。"""

    source: str
    """來源 node_id"""
    target: str
    """目標 node_id"""


@dataclass
class DAG:
    """完整的 lineage DAG。"""

    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)


# ─── DAG 建構 ────────────────────────────────────────────────────────────────

def _art_node_id(checksum: str) -> str:
    """Artifact node id（用 checksum 前 12 碼去重）。"""
    return f"art_{checksum[:12]}"


def _job_node_id(job_id: str) -> str:
    """Job node id。"""
    return f"job_{job_id}"


def _ts_short(ts: float) -> str:
    """timestamp → 短格式 MM-DD HH:MM。"""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _basename(path: str) -> str:
    """取得檔案名稱。"""
    return Path(path).name


def _ensure_artifact_node(
    dag: DAG,
    snap: ArtifactSnapshot,
    block_name: str | None,
) -> str:
    """確保 artifact node 存在（去重用 checksum），回傳 node_id。"""
    nid = _art_node_id(snap.checksum_sha256)
    if nid not in dag.nodes:
        label = f"{_basename(snap.path)}\\n{snap.role}\\n{snap.checksum_sha256[:8]}"
        dag.nodes[nid] = GraphNode(
            node_id=nid,
            node_type="artifact",
            label=label,
            block_name=block_name,
        )
    return nid


def build_dag_from_commits(commits: list[JobCommit]) -> DAG:
    """從所有 JobCommit 建構 DAG。

    - 每個 JobCommit → 一個 Job node
    - 每個 ArtifactSnapshot → 一個 Artifact node（checksum 去重）
    - input artifact → job edge
    - job → output artifact edge
    """
    dag = DAG()

    for commit in commits:
        # Job node
        jid = _job_node_id(commit.job_id)
        job_label = (
            f"{commit.tool_name}\\n"
            f"{commit.stage or ''}\\n"
            f"{_ts_short(commit.started_at)}"
        )
        dag.nodes[jid] = GraphNode(
            node_id=jid,
            node_type="job",
            label=job_label,
            block_name=commit.block_name,
            trace_root=commit.trace_root,
        )

        # Input artifacts → job
        for inp in commit.input_snapshots:
            aid = _ensure_artifact_node(dag, inp, commit.block_name)
            dag.edges.append(GraphEdge(source=aid, target=jid))

        # Job → output artifacts
        for out in commit.output_snapshots:
            aid = _ensure_artifact_node(dag, out, commit.block_name)
            dag.edges.append(GraphEdge(source=jid, target=aid))

    return dag


# ─── 上游回溯 ────────────────────────────────────────────────────────────────

def trace_upstream(
    commits: list[JobCommit],
    target_checksum: str,
) -> list[JobCommit]:
    """從某 artifact 的 checksum 開始，回溯所有上游 producer chain。

    回傳涉及的 JobCommit 子集（包含 root producer）。
    循環依賴防護：visited set。
    """
    # 建立 output checksum → JobCommit 的 index
    output_to_job: dict[str, JobCommit] = {}
    for c in commits:
        for out in c.output_snapshots:
            output_to_job[out.checksum_sha256] = c

    result: list[JobCommit] = []
    visited_jobs: set[str] = set()

    queue: deque[str] = deque()
    queue.append(target_checksum)
    visited_checksums: set[str] = {target_checksum}

    while queue:
        checksum = queue.popleft()
        job = output_to_job.get(checksum)
        if job is None:
            continue
        if job.job_id in visited_jobs:
            continue
        visited_jobs.add(job.job_id)
        result.append(job)

        # 將該 job 的 input checksums 加入 queue
        for inp in job.input_snapshots:
            if inp.checksum_sha256 not in visited_checksums:
                visited_checksums.add(inp.checksum_sha256)
                queue.append(inp.checksum_sha256)

    return result


# ─── Mermaid 生成 ────────────────────────────────────────────────────────────

def _escape_mermaid(text: str) -> str:
    """Mermaid label 內的特殊字元轉義。"""
    # 移除 " 避免破壞 Mermaid syntax
    text = text.replace('"', "'")
    # 移除可能引起問題的字元
    text = text.replace("(", "").replace(")", "")
    text = text.replace("[", "").replace("]", "")
    text = text.replace("{", "").replace("}", "")
    return text


def _build_mermaid_from_dag(dag: DAG) -> str:
    """從 DAG 生成 Mermaid flowchart 文字。

    - 按 block_name 分組為 subgraph
    - Job node：菱形 {{ }}
    - Artifact node：矩形 [ ]
    - ≥2 個 distinct trace_root → 為 job node 上色（artifact 不上色）
    """
    lines: list[str] = ["flowchart LR"]

    # 收集 job node 的 distinct trace_root（空字串視為無 session）
    distinct_roots = sorted({
        n.trace_root for n in dag.nodes.values()
        if n.node_type == "job" and n.trace_root
    })
    color_by_root: dict[str, int] = {}
    if len(distinct_roots) >= 2:
        for root in distinct_roots:
            color_by_root[root] = _trace_root_to_color_index(root)
        # 只 emit graph 中實際用到的 classDef（避免污染未用的 palette）
        used_indices = sorted(set(color_by_root.values()))
        for idx in used_indices:
            color = _TRACE_PALETTE[idx]
            lines.append(f"    classDef session_{idx} fill:{color},stroke:#333,color:#fff")

    # 按 block_name 分組 nodes
    groups: dict[str, list[GraphNode]] = {}
    for node in dag.nodes.values():
        key = node.block_name or "__unknown__"
        groups.setdefault(key, []).append(node)

    # 生成 subgraphs
    for block_name, nodes in sorted(groups.items()):
        if block_name == "__unknown__":
            display_name = "Unknown Block"
        else:
            display_name = f"Block: {block_name}"

        sg_id = re.sub(r"[^a-zA-Z0-9_]", "_", block_name)
        lines.append(f'    subgraph {sg_id} ["{_escape_mermaid(display_name)}"]')

        for node in nodes:
            label = _escape_mermaid(node.label)
            if node.node_type == "job":
                cls_suffix = ""
                if node.trace_root and node.trace_root in color_by_root:
                    cls_suffix = f":::session_{color_by_root[node.trace_root]}"
                lines.append(f'        {node.node_id}{{"{label}"}}{cls_suffix}')
            else:
                lines.append(f'        {node.node_id}["{label}"]')

        lines.append("    end")

    # 生成 edges
    for edge in dag.edges:
        lines.append(f"    {edge.source} --> {edge.target}")

    return "\n".join(lines)


def build_mermaid_full(commits: list[JobCommit]) -> str:
    """從所有 JobCommit 建構完整 Mermaid flowchart。"""
    dag = build_dag_from_commits(commits)
    return _build_mermaid_from_dag(dag)


def build_mermaid_trace(commits: list[JobCommit], target_checksum: str) -> str:
    """從 target artifact 回溯，建構子集 Mermaid flowchart。"""
    subset = trace_upstream(commits, target_checksum)
    if not subset:
        return "flowchart LR\n    empty[\"No upstream found\"]"
    dag = build_dag_from_commits(subset)
    return _build_mermaid_from_dag(dag)


# ─── 收集所有 output artifacts ───────────────────────────────────────────────

@dataclass
class ArtifactInfo:
    """用於 HTML 下拉選單的 artifact 資訊。"""

    checksum: str
    label: str
    timestamp: float


def collect_output_artifacts(commits: list[JobCommit]) -> list[ArtifactInfo]:
    """收集所有 output artifacts（去重），按 timestamp 降序。"""
    seen: dict[str, ArtifactInfo] = {}
    for c in commits:
        for out in c.output_snapshots:
            cs = out.checksum_sha256
            if cs not in seen:
                seen[cs] = ArtifactInfo(
                    checksum=cs,
                    label=f"{_basename(out.path)} ({out.role}) [{cs[:8]}]",
                    timestamp=c.ended_at,
                )
            else:
                # 保留最新的 timestamp
                if c.ended_at > seen[cs].timestamp:
                    seen[cs].timestamp = c.ended_at
    # 按 timestamp 降序
    return sorted(seen.values(), key=lambda a: a.timestamp, reverse=True)


# ─── HTML 輸出 ───────────────────────────────────────────────────────────────

_MAX_TRACE_GRAPHS = 10

_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="generator" content="{generator}">
    <title>Terminux Lineage Graph</title>
    {mermaid_script}
    <script type="module">
        const mermaid = window.mermaid;
        mermaid.initialize({{
            startOnLoad: false,
            theme: 'default',
            flowchart: {{ curve: 'basis' }},
            securityLevel: 'loose',
        }});
        window.mermaidAPI = mermaid;

        const graphs = {graphs_json};

        let renderCounter = 0;

        async function renderGraph(key) {{
            const container = document.getElementById('graph-container');
            const code = graphs[key];
            if (!code) {{
                container.innerHTML = '<p>No graph data for this selection.</p>';
                return;
            }}
            try {{
                renderCounter++;
                const id = 'mermaid-svg-' + renderCounter;
                const {{ svg }} = await mermaid.render(id, code);
                container.innerHTML = svg;
            }} catch (e) {{
                container.innerHTML = '<pre>Mermaid render error: ' + e.message + '</pre>';
            }}
        }}

        // 初始 render
        document.addEventListener('DOMContentLoaded', () => {{
            renderGraph('full');

            document.getElementById('view-mode').addEventListener('change', (e) => {{
                const mode = e.target.value;
                const artifactSelect = document.getElementById('artifact-select');
                if (mode === 'full') {{
                    artifactSelect.style.display = 'none';
                    renderGraph('full');
                }} else {{
                    artifactSelect.style.display = 'inline-block';
                    const key = artifactSelect.value;
                    if (key) renderGraph(key);
                }}
            }});

            document.getElementById('artifact-select').addEventListener('change', (e) => {{
                renderGraph(e.target.value);
            }});
        }});
    </script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 20px;
            background: #f8f9fa;
        }}
        h1 {{
            color: #333;
            margin-bottom: 16px;
        }}
        #controls {{
            margin-bottom: 16px;
            padding: 12px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        select {{
            padding: 6px 12px;
            font-size: 14px;
            border: 1px solid #ccc;
            border-radius: 4px;
            margin-right: 8px;
        }}
        #graph-container {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            overflow-x: auto;
        }}
        #graph-container svg {{
            max-width: 100%;
        }}
    </style>
</head>
<body>
    <h1>Terminux Lineage Graph</h1>

    <div id="controls">
        <select id="view-mode">
            <option value="full">Full Graph</option>
            <option value="trace">Trace from artifact</option>
        </select>
        <select id="artifact-select" style="display:none">
            {artifact_options}
        </select>
    </div>

    <div id="graph-container">
        <p>Loading...</p>
    </div>
</body>
</html>
"""


def render_lineage_html(commits: list[JobCommit]) -> str:
    """從 JobCommit list 生成互動式 HTML。

    包含 full graph + top-N most recent output artifacts 的 trace graph。
    """
    # 建立 graphs dict
    graphs: dict[str, str] = {}

    # Full graph
    graphs["full"] = build_mermaid_full(commits)

    # Trace graphs（最新 N 個 output artifacts）
    artifacts = collect_output_artifacts(commits)
    trace_artifacts = artifacts[:_MAX_TRACE_GRAPHS]

    for art in trace_artifacts:
        key = f"trace_{art.checksum[:12]}"
        graphs[key] = build_mermaid_trace(commits, art.checksum)

    # 產生 artifact select options
    options_html = ""
    for art in trace_artifacts:
        key = f"trace_{art.checksum[:12]}"
        label_escaped = html.escape(art.label)
        options_html += f'            <option value="{key}">{label_escaped}</option>\n'

    graphs_json = json.dumps(graphs, ensure_ascii=False)

    return _HTML_TEMPLATE.format(
        generator=f"Terminux {__version__}",
        mermaid_script=MERMAID_SCRIPT_TAG,
        graphs_json=graphs_json,
        artifact_options=options_html,
    )
