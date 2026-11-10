# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
"""View B：Execution DAG。從 event_store 查 trace_parent → trace_self 邊，
渲染 process 層級 Mermaid（terminal → shell → wrapper → tool）。"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from terminux_engine._version import __version__
from terminux_engine.lineage.mermaid_cdn import MERMAID_SCRIPT_TAG
from terminux_engine.store.event_store import EventStore, SessionEvent

_LAYER_SHAPE_OPEN = {"terminal": "([", "shell": "[", "wrapper": "{{", "queue_submit": "([", "bash_env": "["}
_LAYER_SHAPE_CLOSE = {"terminal": "])", "shell": "]", "wrapper": "}}", "queue_submit": "])", "bash_env": "]"}
_DEFAULT_OPEN = "["
_DEFAULT_CLOSE = "]"


@dataclass
class _NodeInfo:
    """單一 process 節點（per trace_self 去重後）。"""
    trace_self: str
    trace_parent: str
    tool_name: str
    layer: str
    timestamp: float
    pid: str
    argv: str


def _safe_node_id(trace_self: str) -> str:
    """trace_self → Mermaid-safe node id（移除非字母數字）。"""
    cleaned = "".join(c if c.isalnum() else "_" for c in trace_self)
    return f"N_{cleaned}" if cleaned else "N_empty"


def _escape_mermaid_label(text: str) -> str:
    """Mermaid label 特殊字元轉義（複用 graph.py 同樣 pattern）。"""
    return (text.replace('"', "'").replace("(", "").replace(")", "")
                .replace("[", "").replace("]", "").replace("{", "").replace("}", ""))


def _events_to_nodes(events: list[SessionEvent]) -> dict[str, _NodeInfo]:
    """按 trace_self 去重，取最早 timestamp 的 event 作為該 process 的代表。"""
    nodes: dict[str, _NodeInfo] = {}
    for ev in events:
        if not ev.trace_self:
            continue
        existing = nodes.get(ev.trace_self)
        if existing is not None and existing.timestamp <= ev.timestamp:
            continue
        payload = ev.payload or {}
        tool_name = payload.get("tool_name") or ev.event_type
        argv_val = payload.get("argv", [])
        argv_str = json.dumps(argv_val, ensure_ascii=False) if argv_val else ""
        nodes[ev.trace_self] = _NodeInfo(
            trace_self=ev.trace_self,
            trace_parent=ev.trace_parent or "",
            tool_name=tool_name,
            layer=ev.layer or "wrapper",
            timestamp=ev.timestamp,
            pid=str(payload.get("pid", "")),
            argv=argv_str,
        )
    return nodes


def _render_mermaid(nodes: dict[str, _NodeInfo]) -> str:
    """nodes → Mermaid flowchart 字串。"""
    lines: list[str] = ["flowchart TD"]
    if not nodes:
        return "\n".join(lines)

    base_ts = min(n.timestamp for n in nodes.values())
    node_ids: dict[str, str] = {ts: _safe_node_id(ts) for ts in nodes}

    # nodes
    for ts, info in sorted(nodes.items(), key=lambda kv: kv[1].timestamp):
        nid = node_ids[ts]
        rel = info.timestamp - base_ts
        label = _escape_mermaid_label(f"{info.tool_name}<br/>[+{rel:.1f}s]")
        opener = _LAYER_SHAPE_OPEN.get(info.layer, _DEFAULT_OPEN)
        closer = _LAYER_SHAPE_CLOSE.get(info.layer, _DEFAULT_CLOSE)
        lines.append(f'    {nid}{opener}"{label}"{closer}')

    # edges：parent → self（parent 必須也在 nodes 內，否則邊指向圖外）
    for ts, info in nodes.items():
        if info.trace_parent and info.trace_parent in nodes:
            src = node_ids[info.trace_parent]
            dst = node_ids[ts]
            lines.append(f"    {src} --> {dst}")

    # tooltips（click N callback "..."），含 PID + trace_self + argv
    for ts, info in nodes.items():
        nid = node_ids[ts]
        tooltip = _escape_mermaid_label(
            f"pid={info.pid}, trace_self={info.trace_self}"
            + (f", argv={info.argv}" if info.argv else "")
        )
        lines.append(f'    click {nid} callback "{tooltip}"')

    return "\n".join(lines)


def build_exec_dag(event_store: EventStore, trace_root: str) -> str:
    """從 event_store 查 trace_root 的所有事件，建構 execution DAG Mermaid。

    Wrapper 事件透過 Z3-2 ingestor 設 session_id=trace_root，所以用 session_id 過濾。
    空 trace_root 或無事件 → 回傳空 flowchart（只有 header）。
    """
    if not trace_root:
        return "flowchart TD"
    events = event_store.query(session_id=trace_root)
    nodes = _events_to_nodes(events)
    return _render_mermaid(nodes)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="generator" content="{generator}">
<title>{title}</title>
{mermaid_script}
<script>
  window.callback = function(label) {{ alert(label); }};
  mermaid.initialize({{ startOnLoad: true, securityLevel: 'loose' }});
</script>
<style>
  body {{ font-family: sans-serif; margin: 1em; }}
  .mermaid {{ background: #fafafa; padding: 1em; border: 1px solid #ddd; }}
</style>
</head>
<body>
<h1>{title}</h1>
<pre class="mermaid">
{mermaid_str}
</pre>
</body>
</html>
"""


def render_exec_dag_html(mermaid_str: str, title: str = "Execution Trace DAG") -> str:
    """包裝 Mermaid 字串為完整 HTML（含 mermaid.js CDN + click callback）。"""
    return _HTML_TEMPLATE.format(
        generator=f"Terminux {__version__}",
        mermaid_script=MERMAID_SCRIPT_TAG,
        title=html.escape(title),
        mermaid_str=mermaid_str,
    )
