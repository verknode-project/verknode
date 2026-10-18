"""Mermaid diagrams from tables: edges (from, to, label) as a flowchart, rows as a timeline or a pie.

The code goes to a String column - for Mermaid Display, Text Display (```mermaid fences), a Markdown file
or an LLM prompt. (Mermaid Display draws edge tables itself, too.)
"""
from __future__ import annotations

import re

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

DIRECTIONS = ["LR", "TD", "RL", "BT"]
SHAPES = {"Rectangle": '["{}"]', "Rounded": '("{}")', "Stadium": '(["{}"])', "Circle": '(("{}"))',
          "Rhombus": '{{"{}"}}', "Hexagon": '{{{{"{}"}}}}'}


def mm_text(v) -> str:
    """A label for Mermaid: in quotes, without what breaks them."""
    return re.sub(r"\s+", " ", str(v)).replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;").strip()


def edges_to_mermaid(df: pl.DataFrame, src: str, dst: str, label: str | None = None, direction: str = "LR",
                     shape: str = "Rounded", group: str | None = None) -> str:
    """A flowchart of the edges of a table (a row: from -> to, with a label); group: subgraphs of the
    to nodes by a column (e.g. the team of a person who reports to a manager)."""
    for c in (src, dst, label, group):
        if c and c not in df.columns:
            raise InvalidSettingsError(f"There is no column {c!r}")
    ids: dict[str, str] = {}
    lines = [f"flowchart {direction if direction in DIRECTIONS else 'LR'}"]
    box = SHAPES.get(shape, SHAPES["Rounded"])

    def node(v):
        key = str(v)
        if key not in ids:
            ids[key] = f"n{len(ids)}"
        return ids[key]

    groups: dict[str, list[str]] = {}
    edges = []
    for r in df.iter_rows(named=True):
        a, b = r[src], r[dst]
        if a is None:
            continue
        na = node(a)
        nb = node(b) if b is not None else None
        if group and r.get(group) is not None:  # (the row's group: of its to node - the from node if none)
            members = groups.setdefault(str(r[group]), [])
            if (nb or na) not in members:
                members.append(nb or na)
        if nb is None:
            continue
        lab = r.get(label) if label else None
        edges.append(f"  {na} -->|\"{mm_text(lab)}\"| {nb}" if lab not in (None, "") else f"  {na} --> {nb}")
    if len(ids) > 500:
        raise InvalidSettingsError(f"{len(ids)} nodes are too many for a diagram (500 at most)")
    lines += [f"  {i}{box.format(mm_text(k))}" for k, i in ids.items()]
    in_group = set()
    for gi, (g, members) in enumerate(groups.items()):
        members = [m for m in members if m not in in_group]
        in_group |= set(members)
        lines += [f'  subgraph g{gi}["{mm_text(g)}"]', *[f"    {m}" for m in members], "  end"]
    return "\n".join(lines + edges)


class MermaidDiagram(NodeModel):
    factory = "verknode.diagrams.MermaidDiagram"
    name = "Mermaid Diagram"
    category = "Views/Diagrams"
    icon = "⧉"
    short_description = "Mermaid code from a table: a flowchart of edges, a pie, a timeline."
    description = (
        "Flowchart: one edge per row (from, to, optional label), the to nodes grouped into boxes by a column if wanted. "
        "Pie: a slice per row (label, value). Timeline: events per period (period, event). The output is one "
        "row with the Mermaid code - for Mermaid Display, a Markdown file or an LLM."
    )
    in_ports = [Port(TABLE, "Table")]
    out_ports = [Port(TABLE, "Diagram", "One row: the Mermaid code")]
    fields = [
        F("kind", "enum", "Diagram", "Flowchart", options=["Flowchart", "Pie", "Timeline"]),
        F("source", "column", "From / label / period column", None, port=1),
        F("target", "column", "To / value / event column", None, port=1),
        F("label", "column", "Edge label column", None, "Flowchart. Empty: none.", port=1),
        F("group", "column", "Group column", None, "Flowchart: boxes of the to nodes. Empty: none.", port=1),
        F("direction", "enum", "Direction", "LR", "Flowchart: left to right, top down ...", options=DIRECTIONS),
        F("shape", "enum", "Node shape", "Rounded", options=list(SHAPES)),
        F("title", "string", "Title", "", "Pie, timeline."),
        F("column", "string", "Output column", "mermaid"),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema({s.get("column") or "mermaid": pl.String})]

    def execute(self, inputs, s, ctx):
        df, kind = inputs[0], s.get("kind") or "Flowchart"
        src, dst = s.get("source") or (df.columns[0] if df.width else None), s.get("target") or (df.columns[1] if df.width > 1 else None)
        if not src or not dst:
            raise InvalidSettingsError("Pick the two columns")
        title = (s.get("title") or "").strip()
        if kind == "Flowchart":
            code = edges_to_mermaid(df, src, dst, s.get("label"), s.get("direction") or "LR", s.get("shape") or "Rounded",
                                    s.get("group"))
        elif kind == "Pie":
            lines = ["pie showData"] + ([f"  title {mm_text(title)}"] if title else [])
            for r in df.select(src, dst).iter_rows():
                try:
                    lines.append(f'  "{mm_text(r[0])}" : {float(r[1])}')
                except (TypeError, ValueError):
                    pass
            code = "\n".join(lines)
        else:
            lines = ["timeline"] + ([f"  title {mm_text(title)}"] if title else [])
            for period, sub in df.group_by(src, maintain_order=True):
                events = " : ".join(mm_text(v).replace(":", "-") for v in sub[dst].to_list() if v is not None)
                lines.append(f"  {mm_text(period[0]).replace(':', '-')} : {events}")
            code = "\n".join(lines)
        return [pl.DataFrame({s.get("column") or "mermaid": [code]})]
