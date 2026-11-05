"""Walkthrough: a document that explains the workflow, read section by section like a slide show.

The document is Markdown (the node's "document" setting). Each "## " heading starts a section; links with
vk: addresses refer to the parts of the workflow - a node, a group, a node's settings, its canvas widget, an
output table, its view - and run buttons run part or all of it. Stepping to a section shows what it is about
(its "show" line). The editor draws it: on the canvas as the node's widget, or in a dialog, a browser window
or a JupyterLab tab of its own; and edits it there.

check() finds the references that point at nothing; outline() is the workflow as a writer needs it; the node
drafts the document with an LLM (a local model or a server's API) when it is empty. The MCP tools
get_walkthrough / write_walkthrough (agentapi.py) let other agents read and write it.
"""
from __future__ import annotations

import html
import re

from verknode.node import F, InvalidSettingsError, NodeModel

from .llm import LLMNode, chat, llm_port, server_for, server_fields

FORMAT = """A walkthrough is Markdown. Each "## " heading starts a section (a step of the walkthrough); text \
before the first one is the opening section, and a "# " heading at the top is the title. Sections are short: \
a few sentences, a list, maybe a table.

Refer to the parts of the workflow with links whose address starts with vk: - the reader shows them on the \
canvas when clicked:
- [the reader](vk:node/3) - node #3: selects it, brings it into view; hovering shows its output data
- [the cleaning steps](vk:group/1) - group 1: its nodes
- [its settings](vk:settings/3) - opens node #3's settings; vk:settings/3/path goes to the setting "path"
- [the chart](vk:widget/7) - shows node #7's canvas widget (a chart, table, slider, ...)
- [the result](vk:output/5) - opens the table of node #5's output 1; vk:output/5/2 is output 2
- [the report](vk:view/8) - opens node #8's view
- [Run the import](vk:run/3) - a button: runs node #3 (and what it needs); vk:run/3,5 runs several, \
vk:run/group/1 a group, vk:run/all the whole workflow

A line <!-- show: group/1, widget/7 --> in a section says what to show when the reader steps to it (any of \
node/, group/, settings/, widget/, output/, view/; no run/). #3 in the text is a reference to node #3, too. \
Use only the node ids and group ids the workflow has."""

_REF = re.compile(r"\]\(\s*vk:([^)\s]+)\s*\)")
_SHOW = re.compile(r"<!--\s*show:\s*(.*?)\s*-->", re.I | re.S)
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _prose(text: str) -> str:
    """The text without its code (a reference written as an example in `code` is not one)."""
    return _CODE.sub(lambda m: " " * len(m.group()), text)
KINDS = ("node", "group", "settings", "widget", "output", "view", "run")


def parse_ref(ref: str) -> dict | None:
    """A vk: address (without "vk:") as {"kind", ...}, or None when it is not one."""
    parts = [p for p in ref.strip().removeprefix("vk:").split("/") if p != ""]
    if not parts or parts[0] not in KINDS:
        return None
    kind, rest = parts[0], parts[1:]
    try:
        if kind == "run":
            if rest == ["all"]:
                return {"kind": "run", "all": True}
            if len(rest) == 2 and rest[0] == "group":
                return {"kind": "run", "group": int(rest[1])}
            if len(rest) == 1:
                return {"kind": "run", "nodes": [int(x) for x in rest[0].split(",") if x.strip()]}
            return None
        if kind == "group":
            return {"kind": "group", "group": int(rest[0])} if len(rest) == 1 else None
        out = {"kind": kind, "node": int(rest[0])}
        if kind == "settings" and len(rest) == 2:
            out["field"] = rest[1]
        elif kind == "output" and len(rest) == 2:
            out["port"] = int(rest[1])
        elif len(rest) != 1:
            return None
        return out
    except (ValueError, IndexError):
        return None


def sections(text: str) -> list[dict]:
    """The sections: {"title", "body", "show": [addresses]}; the first is the opening (before any "## ")."""
    text = text or ""
    title = ""
    m = re.match(r"\s*#\s+(.+)\n?", text)
    if m:
        title, text = m.group(1).strip(), text[m.end():]
    out = []
    for i, part in enumerate(re.split(r"^##\s+", text, flags=re.M)):
        if i == 0:
            head, body = title, part
        else:
            head, _, body = part.partition("\n")
        show = [a.strip() for s in _SHOW.findall(_prose(body)) for a in s.split(",") if a.strip()]
        if i == 0 and not body.strip() and not show:
            continue
        out.append({"title": head.strip(), "body": body.strip("\n"), "show": show})
    return out


def check(wf, text: str) -> list[str]:
    """What is wrong with a walkthrough of this workflow: references to what is not there, and the like."""
    if not (text or "").strip():
        return ["The document is empty"]
    secs = sections(text)
    groups = {g["id"]: g for g in wf.groups}
    problems = []
    for n, sec in enumerate(secs, 1):
        where = f"section {n}" + (f" \"{sec['title']}\"" if sec["title"] else "")
        refs = [("link", r) for r in _REF.findall(_prose(sec["body"]))] + [("show", r) for r in sec["show"]]
        for how, r in refs:
            ref = parse_ref(r)
            if ref is None:
                problems.append(f"{where}: vk:{r} is not a reference (vk:node/3, vk:group/1, vk:settings/3, "
                                "vk:widget/3, vk:output/3, vk:view/3, vk:run/3)")
                continue
            if how == "show" and ref["kind"] == "run":
                problems.append(f"{where}: show: {r} - a section cannot run anything when it is shown")
            ids = ref.get("nodes") or ([ref["node"]] if "node" in ref else [])
            for nid in ids:
                if nid not in wf.nodes:
                    problems.append(f"{where}: vk:{r} - there is no node #{nid}")
            if "group" in ref and ref["group"] not in groups:
                problems.append(f"{where}: vk:{r} - there is no group {ref['group']}")
            nc = wf.nodes.get(ref.get("node"))
            if nc is None:
                continue
            if ref["kind"] == "settings" and "field" in ref and ref["field"] not in {f.key for f in nc.model.fields}:
                problems.append(f"{where}: vk:{r} - #{nc.id} [{nc.name}] has no setting {ref['field']!r}")
            if ref["kind"] == "widget" and not hasattr(nc.model, "widget"):
                problems.append(f"{where}: vk:{r} - #{nc.id} [{nc.name}] has no canvas widget")
            if ref["kind"] == "output" and not 1 <= ref.get("port", 1) <= len(nc.model.out_ports):
                problems.append(f"{where}: vk:{r} - #{nc.id} [{nc.name}] has {len(nc.model.out_ports)} output(s)")
            if ref["kind"] == "view" and not getattr(nc.model, "has_view", False):
                problems.append(f"{where}: vk:{r} - #{nc.id} [{nc.name}] has no view")
    return problems


def outline(wf, me: int | None = None) -> str:
    """The workflow for a writer: its nodes (ids, types, labels, settings, output columns), groups, canvas
    widgets and connections."""
    from .agent import describe

    # (first the ids at a glance: small models mix them up in the long listing)
    ids = ["The ids to refer to (copy them exactly):"] + [
        f"  node #{n} = {nc.name} \"{nc.annotation}\"" for n, nc in sorted(wf.nodes.items()) if n != me]
    ids += [f"  group {g['id']} = \"{g['text']}\" (nodes {', '.join(f'#{n}' for n in g['nodes'])})" for g in wf.groups]
    text = "\n".join(ids) + "\n\n" + describe(wf, set(), -1 if me is None else me).replace(
        "Nodes you may change:\n  (none yet)\n", "")
    text = text.replace("Other nodes (read only: you may connect from their outputs into your nodes):", "Nodes:")
    if wf.groups:
        text += "\nGroups (vk:group/<id>):\n" + "\n".join(
            f"  group {g['id']} \"{g['text']}\": " + ", ".join(f"#{n}" for n in g["nodes"])
            + (f"\n    note: {' '.join(g['note'].split())[:600]}" if g.get("note") else "") for g in wf.groups)
    widgets = [f"#{n} [{nc.name}] \"{nc.annotation}\"" for n, nc in wf.nodes.items()
               if n != me and hasattr(nc.model, "widget")]
    if widgets:
        text += "\nNodes with a canvas widget (vk:widget/<id>):\n  " + "\n  ".join(widgets)
    return text


SYSTEM = """You write walkthroughs of verknode workflows. verknode is a visual data-workflow editor (like \
KNIME): nodes (readers, filters, joins, aggregations, charts, ...) are connected, and tables flow along the \
connections. A walkthrough explains a workflow to someone who opens it for the first time, step by step: \
what it is for, where the data comes from, what each part does and why, what the results show, and how to \
try it. Follow the order of the workflow, a group or a few nodes per section. Refer to everything you \
mention with a vk: link, give each section a show line, and add run buttons where trying a step helps. \
Write only about what the workflow below has; do not invent numbers. Before you use an id, look it up in \
the list of ids: the link text names what the id is (its label or type).

""" + FORMAT + "\n\nAnswer with the document only (Markdown, no code fence around it)."


def _unfence(text: str) -> str:
    m = re.fullmatch(r"\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*", text or "", re.S)
    return (m.group(1) if m else text or "").strip() + "\n"


def draft(srv: dict, s: dict, wf, me: int | None = None, guidance: str = "", log=None) -> tuple[str, list[str]]:
    """A walkthrough of the workflow, written by the model (one more try when references are wrong):
    (document, problems left)."""
    ask = f"The workflow:\n{outline(wf, me)}\n\n" + (f"What to explain, for whom: {guidance}\n\n" if guidance else "") + \
        "Write the walkthrough."
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ask}]
    text = _unfence(chat(srv, messages, s)["text"])
    problems = check(wf, text)
    if problems:
        if log:
            log(f"Walkthrough draft: {len(problems)} wrong reference(s), asking for a fix")
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "These references are wrong:\n- " + "\n- ".join(problems[:15]) +
                      "\nWrite the whole document again, with only ids that the workflow has."}]
        fixed = _unfence(chat(srv, messages, s)["text"])
        left = check(wf, fixed)
        if len(left) <= len(problems):
            text, problems = fixed, left
    return text, problems


class Walkthrough(LLMNode, NodeModel):
    factory = "verknode.docs.Walkthrough"
    name = "Walkthrough"
    category = "Widgets/Documents"
    icon = "▸☰"
    node_type = "Visualizer"

    @staticmethod
    def sandbox_unsafe(s: dict) -> bool:
        """Only a draft changes something (the node's own document); checking a document is safe."""
        return bool(s.get("draft", True)) and not (s.get("document") or "").strip()

    needs_canvas = True  # (not in headless runs)
    documentation = True  # (exports leave it out: it explains the workflow, it makes no data)
    width, height = 420, 340
    short_description = "A document that walks through the workflow, section by section, pointing at its parts."
    description = (
        "A walkthrough of the workflow: Markdown, read a section at a time like a slide show - on the canvas "
        "(its widget), or in a dialog, a browser window or a JupyterLab tab (⇱ on the widget). Its links point at "
        "the parts of the workflow: clicked, they show a node, a group, a node's settings, its canvas widget, an "
        "output table or a view, and run buttons run part or all of the workflow; stepping to a section shows "
        "what it is about. Edit it on the widget (✎: insert references to the nodes selected on the canvas), "
        "or here. Empty, it is drafted by the LLM of its Model settings or LLM port when it runs (✨ Draft on "
        "the widget). Agents write it over MCP (write_walkthrough).\n\n" + html.escape(FORMAT)
    )
    in_ports = [llm_port("The model that drafts the document when it is empty (optional: else the Model settings)")]
    out_ports = []
    fields = [
        F("title", "string", "Title", "", "Shown on top (empty: the document's \"# \" heading, or Walkthrough)."),
        F("document", "code", "Document", "", "Markdown: \"## \" starts a section; vk: links refer to the "
          "workflow (see the Description).", language="markdown"),
        F("draft", "bool", "Draft it with the LLM when it is empty", True,
          "When the node runs with an empty document, the model writes one from the workflow.", section="Drafting"),
        F("guidance", "string", "What to explain, for whom", "",
          "e.g. \"for new analysts; explain the cleaning rules\"", section="Drafting"),
        *server_fields(temperature=0.3, max_tokens=4096),
    ]

    def widget(self, s: dict, display) -> dict:
        data = {"title": s.get("title") or "", "document": s.get("document") or "",
                "draft": bool(s.get("draft", True)), **({"problems": display["problems"]}
                                                       if isinstance(display, dict) and display.get("problems") else {})}
        return {"type": "output", "renderer": "walkthrough", "title": "", "width": self.width,
                "height": self.height, "data": data, "interactive": False, "selection": None}

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return []
        sw = ctx.script_workflow() if ctx.script_workflow else None
        wf, me = (sw._wf, sw._me) if sw is not None else (None, None)
        doc = (s.get("document") or "").strip()
        if not doc and s.get("draft", True):
            if wf is None:
                raise InvalidSettingsError("Drafting needs the workflow (an open editor or a script workflow)")
            ctx.log("Drafting the walkthrough…")
            try:
                srv = server_for(s, inputs)
                text, problems = draft(srv, s, wf, me, (s.get("guidance") or "").strip(), ctx.log)
            except InvalidSettingsError as e:
                raise InvalidSettingsError(f"No draft: {e} - or write the document (✎ on the widget)") from None
            with wf._lock:
                nc = wf.nodes.get(me)
                if nc is not None:
                    nc.settings = {**nc.settings, "document": text}
            wf._changed()
            ctx.log(f"Drafted the walkthrough: {len(sections(text))} section(s)")
            doc = text
        if wf is not None and doc:
            problems = check(wf, doc)
            ctx.set_display({"problems": problems})
            if problems:
                ctx.warn(f"{len(problems)} reference(s) point at nothing: " + "; ".join(problems[:3]))
        return []
