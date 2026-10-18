"""Workflow agents: build the workflow you describe in a chat.

Workflow Agent Chat is one node that carries the whole chat: run it, and it shows a chat on the canvas,
waits for your message, works on it, answers, and waits again - until End chat. The Workflow Agent is
the same agent for a chat loop of your own:

    Chat Loop Start -> Workflow Agent -> Concatenate -> Recursive Loop End

For each message (AgentSession.turn) the model works with tools - find node types, read their help,
add, configure, connect, disconnect and remove nodes, run and preview - on a copy of the workflow: the
copy runs in a file sandbox (engine.sandbox: files are copied in when used, nothing real is written).
What it did is tested (and shown to the model again, to fix) before the same edits are made to the
workflow, while the chat goes on (Workflow.live_edits; one undo step per message). The agent keeps to
its own group (by default "Built by agent #<its id>") and never touches other nodes. Edits that undo
what was there before the message - removing nodes or connections, replacing a connection, overwriting
files when what was built runs - wait for the user's OK (Yes / No in the chat).
"""
from __future__ import annotations

import difflib
import html
import json
import os
import re
import time

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE
from jupyterlab_verknode.engine.node import CanceledExecutionError

from .chat import _check_conversation
from .llm import chat_tools, server, server_fields, tool_message

ANSWER = {"role": pl.String, "content": pl.String, "model": pl.String, "seconds": pl.Float64, "tokens": pl.Int64,
          "confirm": pl.Boolean}
CONVERSATION = {"role": pl.String, "content": pl.String}
SUMMARY = "\n\n**Changes**"  # (where the agent's own report starts in a reply: not shown to the model again)
CONFIRM = "\n\n**Waiting for your OK**"
CLAIM = re.compile(r"\b(I|I've|I have)\s+(now\s+)?(added|removed|deleted|changed|updated|connected|disconnected|"
                   r"configured|created|built|set|renamed|replaced|cleared)\b|\bhas been (added|removed|deleted|"
                   r"changed|updated|cleared|created)\b", re.I)
YES = re.compile(r"\s*(y|yes|yeah|yep|ok|okay|sure|go ahead|do it|apply|confirm|confirmed|please do|yes,? (please|do it|go ahead|apply)?)[.! ]*", re.I)
NO = re.compile(r"\s*(n|no|nope|cancel|don'?t|do not|stop|abort|keep (it|them)|never ?mind)[.! ]*", re.I)


def _short(v, n=300):
    text = json.dumps(v, default=str)
    return text if len(text) <= n else text[:n] + "…"


def default_group(me) -> str:
    return f"Built by agent #{me}"


def _unquote(v):
    """A structured setting the model wrote as a JSON string ("{\"include\": ...}") -> its value."""
    if isinstance(v, str) and v.strip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def columns_setting(v):
    """A column selection as the model writes it -> {"include", "exclude", "enforce"}: a list of names, or
    names to include without an enforce that includes them, are those columns."""
    if isinstance(v, str):
        v = [v]
    if isinstance(v, list):
        return {"include": [str(c) for c in v], "exclude": [], "enforce": "inclusion"}
    if isinstance(v, dict):
        inc, exc = list(v.get("include") or []), list(v.get("exclude") or [])
        enforce = v.get("enforce")
        if enforce not in ("inclusion", "exclusion") or (enforce == "exclusion" and inc and not exc):
            enforce = "exclusion" if exc and not inc else "inclusion"
        return {"include": inc, "exclude": exc, "enforce": enforce}
    return v


FORMATS = """Setting formats: column = a column name. columns = {"enforce": "inclusion", "include": [names]} for \
just these columns, or {"enforce": "exclusion", "exclude": [names]} for all columns but these (a list of names \
also works). records = a list of objects with the keys shown. enum = one of the options. Leave out the \
settings whose defaults are right."""


class Edit:
    """Makes edits to a workflow (the test copy, or the workflow itself), only to the nodes it may change."""

    def __init__(self, wf, editable: set, group_id, me: int, hidden=(), group_name: str | None = None):
        self.wf, self.editable, self.group_id, self.me = wf, set(editable), group_id, me
        self.hidden = set(hidden)  # (nodes it must not use at all: itself, the chat loop it runs in)
        self.group_name = group_name  # (the group to make for the first new node)
        self.suggested: set[str] = set()  # (node types offered for unknown ones)
        self.refs: dict[str, int] = {}
        self.idmap: dict[int, int] = {}  # (replaying: the id a node had on the copy -> its id here)
        self.added: list[int] = []
        self.touched: set[int] = set()
        self.log: list[dict] = []

    def node(self, ref, write: bool = False):
        if isinstance(ref, str) and ref in self.refs:
            nid = self.refs[ref]
        else:
            try:
                nid = int(str(ref).strip().lstrip("#"))
            except ValueError:
                raise ValueError(f"unknown node {ref!r} (give the node's id, a number)") from None
            nid = self.idmap.get(nid, nid)
        if nid not in self.wf.nodes or nid == self.me or nid in self.hidden:
            raise ValueError(f"there is no node #{ref} you can use (see the workflow for the ids)")
        if write and nid not in self.editable:
            raise ValueError(f"node #{nid} is not yours (not in your group): you may only connect from it, "
                             "not change, remove or connect into it")
        return nid

    def type_of(self, name):
        # ("CSV Reader (IO/Read)", as find_node_types lists it: the name)
        m = re.fullmatch(r"\s*(.+?)\s*\(([^()]*)\)\s*", str(name or ""))
        if m and any(t["name"] == m.group(1) and t["category"] == m.group(2) for t in self.wf.node_types()):
            name = m.group(1)
        for t in self.wf.node_types():
            if name in (t["name"], t["factory"]):
                return t["factory"]
        low = str(name).lower().strip()
        for t in self.wf.node_types():
            if t["name"].lower() == low:
                return t["factory"]
        names = [t["name"] for t in self.wf.node_types()]
        close = difflib.get_close_matches(str(name), names, n=4, cutoff=0.4)
        close += [n for n in names if low and low in n.lower() and n not in close][:4]
        self.suggested.update(close)
        raise ValueError(f"unknown node type {name!r}" + (f" - did you mean: {', '.join(close)}?" if close else
                                                          " (find_node_types finds types)"))

    def _settings(self, nid, settings):
        if isinstance(settings, str):
            try:
                settings = json.loads(settings) if settings.strip() else {}
            except ValueError:
                raise ValueError("settings must be a JSON object") from None
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        nc = self.wf.nodes[nid]
        keys = {f.key for f in nc.model.fields if f.type != "widget"}
        unknown = [k for k in settings if k not in keys]
        if unknown:
            raise ValueError(f"{nc.name} has no setting(s) {unknown}; its settings are {sorted(keys)}")
        types = {f.key: f.type for f in nc.model.fields}
        settings = {k: _unquote(v) if types[k] in ("columns", "records", "multienum", "varlist") else v
                    for k, v in settings.items()}
        settings = {k: columns_setting(v) if types[k] == "columns" else v for k, v in settings.items()}
        if settings:
            self.wf.set_settings(nid, settings, nc.var_settings)

    def _place(self, after):
        nodes = [self.wf.nodes[n] for n in self.editable if n in self.wf.nodes]
        if not nodes and after is None:  # (the first node: below everything)
            everything = [n for n in self.wf.nodes.values() if n.parent is None]
            low = max((n.y + ((n.widget_layout or {}).get("h") or 0) for n in everything), default=0)
            return min((n.x for n in everything), default=0), low + 200
        if after is not None:
            a = self.wf.nodes[after]
            x, y = a.x + 150, a.y
        else:
            x = max((n.x for n in nodes), default=0) + 150
            y = min((n.y for n in nodes), default=80)
        taken = {(round(n.x / 40), round(n.y / 40)) for n in self.wf.nodes.values()}
        while (round(x / 40), round(y / 40)) in taken:
            y += 120
        return x, y

    def apply(self, ops: list) -> list[str]:
        """Make the edits; the errors (one per failed edit)."""
        errors = []
        for i, op in enumerate(ops, 1):
            kind = (op or {}).get("op") if isinstance(op, dict) else None
            try:
                self.one(op)
            except Exception as e:  # noqa: BLE001 - any failure is reported
                errors.append(f"edit {i} ({kind}): {e}")
        return errors

    def one(self, op) -> int | None:
        """One edit ({"op": "add" | "configure" | "connect" | "disconnect" | "remove" | "label", ...});
        the node it made or changed."""
        wf, kind = self.wf, op.get("op")
        if kind == "add":
            factory = self.type_of(op.get("type"))
            after = self.node(op["after"]) if op.get("after") not in (None, "", 0) else None
            x, y = self._place(after)
            parent = wf.nodes[after].parent if after is not None else None
            nid = wf.add_node(factory, x, y, parent)
            if op.get("id") is not None and int(op["id"]) != nid:
                self.idmap[int(op["id"])] = nid
            self.added.append(nid)
            self.editable.add(nid)
            self.touched.add(nid)
            if op.get("ref"):
                self.refs[str(op["ref"])] = nid
            wf.set_annotation(nid, str(op.get("label") or wf.nodes[nid].name))
            if self.group_id is not None and any(g["id"] == self.group_id for g in wf.groups):
                g = wf.group(self.group_id)
                wf.update_group(self.group_id, nodes=list(g["nodes"]) + [nid])
            elif self.group_name:
                self.group_id = wf.add_group([nid], self.group_name, "#3d9be9")
            try:
                self._settings(nid, op.get("settings") or {})
            except Exception:
                wf.remove_nodes([nid])  # (all or nothing)
                self.added.remove(nid)
                self.editable.discard(nid)
                raise
            self.log.append(f"added #{nid} {wf.nodes[nid].name} \"{wf.nodes[nid].annotation}\"")
            return nid
        if kind == "configure":
            nid = self.node(op.get("node"), True)
            self._settings(nid, op.get("settings") or {})
            if op.get("label"):
                wf.set_annotation(nid, str(op["label"]))
            self.touched.add(nid)
            self.log.append(f"configured #{nid} {_short(op.get('settings') or {}, 160)}")
            return nid
        if kind in ("connect", "disconnect"):
            src = self.node(op.get("from"))
            dst = self.node(op.get("to"), True)
            sp, dp = int(op.get("from_port") or 1), int(op.get("to_port") or 1)
            if kind == "connect":
                wf.connect(src, sp, dst, dp)
            else:
                if not any(c.key() == (src, sp, dst, dp) for c in wf.connections):
                    raise ValueError(f"there is no connection #{src}:{sp} -> #{dst}:{dp}")
                wf.disconnect(src, sp, dst, dp)
            self.touched.add(dst)
            self.log.append(f"{kind}ed #{src}:{sp} -> #{dst}:{dp}")
            return dst
        if kind == "remove":
            nid = self.node(op.get("node"), True)
            name = f"#{nid} {wf.nodes[nid].name} \"{wf.nodes[nid].annotation}\""
            wf.remove_nodes([nid])
            self.editable.discard(nid)
            self.touched.discard(nid)
            self.log.append(f"removed {name}")
            return None
        if kind == "label":
            nid = self.node(op.get("node"), True)
            wf.set_annotation(nid, str(op.get("text") or ""))
            return nid
        raise ValueError(f"unknown edit {kind!r}")


# ---------------------------------------------------------------------------------------------------
# what the model sees
# ---------------------------------------------------------------------------------------------------
def _columns(spec, n=40) -> str | None:
    if isinstance(spec, (pl.Schema, dict)):
        items = list(spec.items())
        return ", ".join(f"{c}:{t}" for c, t in items[:n]) + (f", … ({len(items)} columns)" if len(items) > n else "")
    return None


def node_line(wf, nid, analysis=None, settings=True) -> str:
    nc = wf.nodes[nid]
    a = (analysis or wf.analyze()).get(nid) or {}
    ins = ", ".join(f"{i}:{p.name} ({p.type}{', optional' if p.optional else ''})"
                    for i, p in enumerate(nc.model.in_ports, 1)) or "-"
    outs = ", ".join(f"{i}:{p.name} ({p.type})" for i, p in enumerate(nc.model.out_ports, 1)) or "-"
    line = f"#{nid} [{nc.name}] \"{nc.annotation}\" in: {ins}; out: {outs}"
    if settings:
        line += f"\n    settings: {_short(nc.settings, 500)}"
        bound = {k: b["used"] for k, b in (nc.var_settings or {}).items() if b.get("used")}
        if bound:
            line += f"\n    settings from workflow variables: {_short(bound, 200)}"
    if nc.message and nc.message[0] == "ERROR":
        line += f"\n    ERROR: {nc.message[1][:300]}"
    elif not a.get("ok"):
        line += "\n    NOT READY: " + why_not_run(wf, nid)[:300]
    else:
        for i, spec in enumerate((a.get("specs") or [])[1:], 1):
            cols = _columns(spec)
            if cols is not None:
                line += f"\n    output {i} columns: {cols}"
        rows = next((o.height for o in (nc.outputs or [])[1:] if isinstance(o, pl.DataFrame)), None)
        if rows is not None:
            line += f" ({rows} rows when run)"
    return line


def describe(wf, editable: set, me: int, hidden=(), group: str | None = None) -> str:
    """The workflow as the model sees it (without the nodes it must not use)."""
    analysis = wf.analyze()
    groups = {n: g["text"] for g in wf.groups for n in g["nodes"]}
    mine, others = [], []
    for nid in wf.topo_order():
        nc = wf.nodes[nid]
        if nid == me or nid in hidden or getattr(nc.model, "boundary", False):
            continue
        if nid in editable:
            mine.append("  * " + node_line(wf, nid, analysis))
        else:
            where = f" (group \"{groups[nid]}\")" if nid in groups else ""
            others.append("    " + node_line(wf, nid, analysis, settings=len(wf.nodes) < 40) + where)
    out = [f"Your nodes (group \"{group}\"; you may change them):" if group else "Nodes you may change:"]
    out += mine or ["  (none yet)"]
    if others:
        out += ["Other nodes (read only: you may connect from their outputs into your nodes):"] + others
    seen = set(editable) | {n for n in wf.nodes if n not in hidden and n != me}
    conns = [f"  #{c.source_id}:{c.source_port} -> #{c.dest_id}:{c.dest_port}" for c in wf.connections
             if c.source_id in seen and c.dest_id in seen]
    out += ["Connections (from:port -> to:port):"] + (conns or ["  (none)"])
    return "\n".join(out)


def catalog(wf) -> str:
    by: dict[str, list] = {}
    for t in wf.node_types():
        if t["name"].startswith("Metanode"):
            continue
        top = "/".join(t["category"].split("/")[:2])
        by.setdefault(top, []).append(t["name"])
    return "\n".join(f"{c}: {', '.join(sorted(n))}" for c, n in sorted(by.items()))


def type_specs(wf, names) -> str:
    out = []
    wanted = {str(n).lower() for n in names}
    for t in wf.node_types():
        if t["name"].lower() not in wanted:
            continue
        ins = ", ".join(f"{p['index']}:{p['name']} ({p['type']}{', optional' if p.get('optional') else ''})"
                        for p in t["inPorts"] if p["index"] > 0) or "none"
        outs = ", ".join(f"{p['index']}:{p['name']} ({p['type']})" for p in t["outPorts"] if p["index"] > 0) or "none"
        fields = []
        for f in t["fields"]:
            if f.get("type") in ("widget",):
                continue
            extra = f" options={f['options']}" if f.get("options") else ""
            sub = f" each item: {{{', '.join(x['key'] + ': ' + x['type'] for x in f.get('fields') or [])}}}" if f.get("fields") else ""
            fields.append(f"    {f['key']} ({f['type']}) default={_short(f.get('default'), 120)}{extra}{sub}"
                          + (f" - {f['description'][:200]}" if f.get("description") else ""))
        desc = html.unescape(re.sub(r"<[^>]+>", "", t.get("description") or ""))[:5000]  # (e.g. expression functions)
        out.append(f"[{t['name']}] {t['shortDescription']}\n  {desc}\n  in ports: {ins}\n  out ports: {outs}\n"
                   "  settings:\n" + ("\n".join(fields) or "    (none)"))
    return "\n\n".join(out)


def search_types(wf, query="", limit=12) -> str:
    """The node types that match the words of a query best (name, category, descriptions)."""
    words = [w for w in re.findall(r"\w+", str(query).lower()) if len(w) > 1]
    scored = []
    for t in wf.node_types():
        if t["name"].startswith("Metanode"):
            continue
        name, short = t["name"].lower(), f"{t['category']} {t['shortDescription']}".lower()
        long = (t.get("description") or "").lower()
        # (the name counts most, then the category and short description, then the description)
        score = sum(6 * (w in name) + 3 * (w in short) + min(long.count(w), 2) for w in words)
        score += 4 * sum(1 for a, b in zip(words, words[1:]) if f"{a} {b}" in f"{name} {short}")
        if score:
            scored.append((score, t))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return "No node type matches; try other words."
    return "\n".join(f"- {t['name']} ({t['category']}): {t['shortDescription']}" for _, t in scored[:limit])


def clone(wf):
    """A copy of the workflow (with the results of its nodes), for trying things out."""
    from jupyterlab_verknode.engine import persistence

    copy = persistence.load_workflow(persistence.save_workflow(wf), wf.base_dir)
    copy.path, copy.node_paths = wf.path, list(wf.node_paths)
    if copy.node_paths:
        copy.reload_nodes(reason="test copy")
    copy.next_id = wf.next_id  # (the same ids for the same edits: the copy's results fit the workflow)
    copy.next_group_id = wf.next_group_id
    return copy


def run_sandboxed(copy, ids, s):
    """Run these nodes of a copy in a file sandbox: (report rows, problems, file changes)."""
    from jupyterlab_verknode.engine.sandbox import Sandbox

    report, problems, files = [], [], []
    if not ids:
        return report, problems, files
    box = Sandbox(float(s.get("sandbox_mb") or 500), bool(s.get("allow_unsafe"))) if s.get("sandbox", True) else None
    copy.sandbox = box  # (None: the sandbox is off - the real files)
    try:
        copy.reset(ids)
        copy.execute(ids)
        time.sleep(0.05)
        copy.wait()
        for nid in copy.topo_order(set(ids)):
            nc = copy.nodes[nid]
            msg = nc.message
            state = "executed" if nc.executed else ("skipped" if msg and "test sandbox" in msg[1] else
                                                     "failed" if msg and msg[0] == "ERROR" else "not run")
            rows = next((o.height for o in (nc.outputs or [])[1:] if isinstance(o, pl.DataFrame)), None)
            report.append({"id": nid, "name": nc.name, "label": nc.annotation, "state": state,
                           "message": msg[1] if msg else None, "rows": rows})
            if state == "failed":
                problems.append(f"node #{nid} [{nc.name}] \"{nc.annotation}\" failed: {msg[1]}")
            elif state == "not run":
                why = msg[1] if msg else why_not_run(copy, nid)
                report[-1]["message"] = why
                problems.append(f"node #{nid} [{nc.name}] \"{nc.annotation}\" did not run: {why}")
            elif msg and msg[0] == "WARNING" and state == "executed":
                problems.append(f"node #{nid} [{nc.name}] \"{nc.annotation}\" warns: {msg[1]}")
        files = box.changes() if box is not None else []
    finally:
        if box is not None:
            box.cleanup()
        copy.sandbox = None
    return report, problems, files


def why_not_run(wf, nid) -> str:
    """Why a node is not ready: its own problem, or the first node before it that is not."""
    analysis = wf.analyze()
    nc = wf.nodes[nid]
    missing = [i for i, p in enumerate(nc.model.in_ports, 1) if not p.optional and i not in wf.in_conns(nid)]
    if missing:
        return f"its input port(s) {missing} are not connected"
    if (analysis.get(nid) or {}).get("message"):
        return analysis[nid]["message"]
    for a in wf.topo_order(wf.ancestors(nid)):
        if not (analysis.get(a) or {}).get("ok"):
            up = wf.nodes[a]
            return (f"node #{a} [{up.name}] \"{up.annotation}\" before it is not ready: "
                    + why_not_run(wf, a) if a != nid else "")
    return "a node before it did not run"


def transfer_results(test, wf, idmap: dict, skip=()) -> int:
    """(The sandbox is off.) The results of the test run go to the workflow's nodes that are the same
    (settings and connections) and have not run: they need not run again. How many nodes got theirs."""
    from jupyterlab_verknode.engine.workflow import Workflow

    n = 0
    with wf._lock:
        for cid in test.topo_order():
            tc, rid = test.nodes[cid], idmap.get(cid, cid)
            rc = wf.nodes.get(rid)
            if rc is None or rid in skip or rc.executed or not tc.executed or rc.factory != tc.factory:
                continue
            if Workflow._config(tc)[2] != Workflow._config(rc)[2]:
                continue
            tin = {(idmap.get(c.source_id, c.source_id), c.source_port, c.dest_port) for c in test.connections
                   if c.dest_id == cid}
            rin = {(c.source_id, c.source_port, c.dest_port) for c in wf.connections if c.dest_id == rid}
            if tin != rin or any(not wf.nodes[src].executed for src, _, _ in rin):
                continue
            for k in Workflow._STATE:
                setattr(rc, k, getattr(tc, k))
            n += 1
    if n:
        wf._changed(dirty=False)
    return n


def markdown_table(df: pl.DataFrame, rows: int, cols: int = 7) -> str:
    shown = df.columns[:cols]
    head = "| " + " | ".join(shown) + " |\n|" + "---|" * len(shown)
    body = []
    for r in df.head(rows).iter_rows(named=True):
        cells = []
        for c in shown:
            v = r[c]
            v = f"{v:.6g}" if isinstance(v, float) else ("" if v is None else str(v))
            cells.append(v.replace("|", "/").replace("\n", " ")[:60])
        body.append("| " + " | ".join(cells) + " |")
    more = f"\n\n({df.height} rows" + (f", {df.width} columns" if df.width > len(shown) else "") + ")"
    return head + "\n" + "\n".join(body) + more


# ---------------------------------------------------------------------------------------------------
# the agent
# ---------------------------------------------------------------------------------------------------
def _fn(name, description, props=None, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}


NODE = {"type": "integer", "description": "a node id (the number after #)"}
TOOLS = [
    _fn("find_node_types", "Search the node types by what they do (words in their name or description).",
        {"query": {"type": "string", "description": "e.g. 'group by sum', 'csv', 'bar chart', 'join'"}}, ["query"]),
    _fn("node_type_help", "The ports and settings (with their formats and defaults) of node types. Read it before "
        "adding or configuring a node type you have not used in this chat.",
        {"types": {"type": "array", "items": {"type": "string"}, "description": "exact node type names"}}, ["types"]),
    _fn("list_files", "Files in the workflow's folder (paths are relative to it).",
        {"folder": {"type": "string", "description": "a sub folder (default: the workflow's folder)"}}),
    _fn("show_workflow", "The workflow as it is now (your nodes, their settings, output columns and problems)."),
    _fn("add_node", "Add a node to your group. Returns its id, its ports and (when it is ready) its output columns.",
        {"type": {"type": "string", "description": "exact node type name"},
         "label": {"type": "string", "description": "a short label saying what it does here"},
         "settings": {"type": "object", "description": "only the settings that differ from the defaults"},
         "after": {"type": "integer", "description": "place it to the right of this node (optional)"}},
        ["type", "label"]),
    _fn("configure_node", "Change settings (and/or the label) of one of your nodes.",
        {"node": NODE, "settings": {"type": "object", "description": "only the settings that change"},
         "label": {"type": "string"}}, ["node"]),
    _fn("connect", "Connect an output port to an input port of one of your nodes (ports count from 1). "
        "A connection already into that input is replaced.",
        {"from_node": NODE, "from_port": {"type": "integer"}, "to_node": NODE, "to_port": {"type": "integer"}},
        ["from_node", "to_node"]),
    _fn("disconnect", "Remove a connection into one of your nodes.",
        {"from_node": NODE, "from_port": {"type": "integer"}, "to_node": NODE, "to_port": {"type": "integer"}},
        ["from_node", "to_node"]),
    _fn("remove_node", "Remove one of your nodes (with its connections).", {"node": NODE}, ["node"]),
    _fn("run", "Run your nodes (on a test copy, in a file sandbox) and get each node's state, rows and errors."),
    _fn("preview", "Run up to a node (on the test copy) and show the first rows of its output table.",
        {"node": NODE, "port": {"type": "integer", "description": "output port (default 1)"},
         "rows": {"type": "integer", "description": "how many rows (default 10)"}}, ["node"]),
]

SYSTEM = """You are the workflow agent of verknode, a visual data-workflow editor (like KNIME). A workflow is \
made of nodes (readers, filters, joins, aggregations, charts, ...) whose ports are connected: tables flow \
along the connections. You build and change the workflow as the user asks in the chat, with the tools.

How to work:
1. Read the workflow (it is given with each message). Reuse the nodes that are there: build on them.
2. Pick node types from the list below. Call node_type_help for a type before you use it (its settings \
have exact names and formats). When no type you know does a step, search with find_node_types by what it \
should do (e.g. "week", "round date", "pivot", "remove duplicates") before you try a workaround.
3. Build a step at a time: add_node (with the settings it needs), then connect it. Each result tells you the \
node's output columns or what is wrong: fix problems before going on.
4. When done, call preview on the last node and check that the result is exactly what the user asked for \
(right rows, columns and numbers). Fix it if not.
5. Then answer the user briefly, in one to three sentences: what the result shows, or what you changed. Do \
not write a list of the changes or a table: the user gets those from verknode, below your answer.

Rules:
- The workflow changes only through your tool calls. Never say you did something you did not do with a tool.
- You may only change, connect into or remove your own nodes (marked *); new nodes join your group. \
Other nodes are read only: connect from their outputs if you need their data. Never try to change them.
- Only remove or rewire what the user asks you to. Removing nodes or connections that were there before this \
message is shown to the user, who must agree before it happens: say so in your answer.
- Ports count from 1. A node needs its (non optional) input ports connected before it is ready.
- File paths are relative to the workflow's folder (list_files shows the files). Do not write files unless asked.
- Everything you do is tried on a copy, in a file sandbox; it is made in the workflow when you have answered.
- If the user only asks a question, answer it without changing the workflow. If what they ask is unclear, ask \
them (briefly) instead of guessing."""


ASK_OK = ("\n(This undoes what was there before this message: it is made in the workflow only when the user "
          "agrees - they will be asked. In your answer, say what you would change and that it waits for their OK; "
          "do not say it is done.)")


def _is_yes(text) -> bool:
    return bool(YES.fullmatch(text or ""))


def _is_no(text) -> bool:
    return bool(NO.fullmatch(text or ""))


class AgentSession:
    """The agent's work on one message: a tool-calling loop on a test copy of the workflow; the edits it
    made are then made to the workflow - at once, or after the user's OK when they undo what was there.

    turn(message, history) -> (reply, waiting for an OK). A pending change (self.pending) is applied by
    the next turn when the message is yes; any other message drops it."""

    def __init__(self, ctx, s: dict, wf, me: int, hidden=(), status=None, after_change=None):
        self.ctx, self.s, self.wf, self.me = ctx, s, wf, me
        self.hidden = set(hidden) | {me}
        self.status = status or (lambda text: None)
        self.after_change = after_change or (lambda: None)  # (e.g. run what was built when the chat ends)
        self.group = (s.get("group") or "").strip() or default_group(me)
        self.pending: dict | None = None
        self.srv = server(s)
        self.tokens = 0

    # -- the scope ------------------------------------------------------------------------------
    def scope(self, wf):
        """(the nodes it may change, the group's id)."""
        for g in wf.groups:
            if g["text"].strip().lower() == self.group.lower():
                return {n for n in g["nodes"] if n not in self.hidden and n in wf.nodes}, g["id"]
        return set(), None

    # -- one message ----------------------------------------------------------------------------
    def turn(self, message: str, history: list) -> tuple[str, bool]:
        """The reply to a message, and whether it waits for an OK. self.nodes: the nodes of the workflow
        the reply changed (or, waiting for the OK, would change) - for Show changes."""
        self.nodes: list[int] = []
        pending, self.pending = self.pending, None
        if pending is not None:
            if _is_yes(message):
                return self._apply(pending, confirmed=True), False
            if _is_no(message):
                return "OK - I did not change anything.", False
            # (something else: the change is dropped, the message is a new request)
        return self._work(message, history)

    def _work(self, message, history) -> tuple[str, bool]:
        s, wf = self.s, self.wf
        t0 = time.time()
        test = clone(wf)
        editable, gid = self.scope(test)
        self.orig_nodes = set(test.nodes)
        self.orig_conns = {c.key() for c in test.connections}
        self.test = test
        self.edit = Edit(test, editable, gid, self.me, self.hidden, self.group)
        self.ops: list[dict] = []
        self.destructive: list[str] = []
        self.helped: set[str] = set()
        self.dirty = False  # (edited since the last run)
        self.last_run = None  # (report, problems, files) of the last run
        guidance = (s.get("guidance") or "").strip()
        system = SYSTEM + ("\n\nMore instructions:\n" + guidance if guidance else "") + \
            f"\n\nNode types (by category):\n{catalog(wf)}"
        state = describe(test, self.edit.editable, self.me, self.hidden, self.group)
        mentioned = [int(m) for m in dict.fromkeys(re.findall(r"@#(\d+)", message))]
        mentioned = [n for n in mentioned if n in test.nodes and n not in self.hidden]
        if mentioned:  # (the nodes the user selected on the canvas and added to the message)
            state += "\n\nThe nodes I mean (@#id in my message, selected on the canvas):\n" + "\n".join(
                ("  * " if n in self.edit.editable else "    (read only) ") + node_line(test, n) for n in mentioned)
        messages = [{"role": "system", "content": system}] + history + [
            {"role": "user", "content": f"The workflow now:\n{state}\n\n" + ("" if s.get("sandbox", True) else
             "(The sandbox is OFF: your runs read and write the real files and run scripts - do not write, move "
             "or delete files unless I ask.)\n\n") + f"My message: {message}"}]
        steps, max_steps = 0, max(4, int(s.get("steps") or 40))
        fixes, max_fixes = 0, max(0, int(s.get("rounds") or 3))
        checked = not s.get("check", True)
        text, nudges, claimed = "", 0, False
        seen: dict = {}  # (tool calls so far: repeating one that cannot help is stopped)
        while True:
            self.ctx.check_canceled()
            if steps >= max_steps:
                text = (text + "\n\n" if text else "") + f"(I stopped after {max_steps} steps.)"
                break
            steps += 1
            self.status("Thinking…" if steps == 1 else f"Working… (step {steps})")
            r = chat_tools(self.srv, messages, s, TOOLS)
            messages.append(r["message"])
            if r["calls"]:
                for call in r["calls"]:
                    self.ctx.check_canceled()
                    key = (call["name"], json.dumps(call["args"], sort_keys=True, default=str))
                    seen[key] = seen.get(key, 0) + 1
                    reads = ("run", "preview", "show_workflow", "find_node_types", "node_type_help", "list_files")
                    if seen[key] > 2 and call["name"] in reads and not self.dirty:
                        result = ("You called this already, and nothing has changed since: the result is the same. "
                                  "Change something (e.g. fix the problem it showed, connect from another node), "
                                  "or answer me.")
                    elif seen[key] > 2 and call["name"] not in reads:
                        result = ("You made exactly this change already: making it again changes nothing. If the "
                                  "result is still wrong, try another way (node_type_help shows what the node can "
                                  "do), or tell me what you could not do.")
                    else:
                        result = self._tool(call["name"], call["args"])
                    messages.append(tool_message(self.srv, call, result))
                continue
            text = (r["text"] or "").strip()
            if not text and nudges < 2:
                # (it stopped without a word, e.g. after looking things up: it goes on)
                nudges += 1
                messages.append({"role": "user", "content": "Go on: do what I asked with the tools, then answer me "
                                 "(or answer me if there is nothing to build)."})
                continue
            if not self.ops and not claimed and CLAIM.search(text):
                # (it says it changed something, but no tool did: it has to do it)
                claimed = True
                messages.append({"role": "user", "content": "(verknode: no tool was called, so nothing has changed "
                                 "- the workflow changes only through tool calls.) Do it now: call the tools "
                                 f"(add_node, configure_node, connect, disconnect, remove_node) for my message "
                                 f"\"{message}\". Answer only after that. If there is nothing to change, say so."})
                continue
            # the model has answered: test what it built, and show it the result once
            if not self.ops:
                break
            if self.dirty or self.last_run is None:
                self.status("Testing…")
                self._run()
            report, problems, files = self.last_run
            if problems and fixes < max_fixes:
                fixes += 1
                messages.append({"role": "user", "content": "Before you answer: testing what you built gave these "
                                 "problems:\n- " + "\n- ".join(problems[:10]) + "\nFix them with the tools, then answer."})
                continue
            if not checked and not problems:
                checked = True
                result = self._result_text(rows=10)
                if result:
                    messages.append({"role": "user", "content": "(verknode: before you answer, check the result.) "
                                     f"This is what your nodes give now:\n{result}\n\nIs it exactly what I asked for "
                                     f"(\"{message}\")? Check each column: the right rows and grouping (e.g. per week "
                                     "is not per day), the right calculation (sum, count, mean; a percent is x 100), "
                                     "and no values counted twice (a value a join repeats on many rows must not be "
                                     "summed). If something is wrong, fix it with the tools. Then answer my message "
                                     "(about the result - not about this check)."})
                    continue
            break
        reply = text or ("Done." if self.ops else "I have no answer to that.")
        self.ctx.log(f"Agent: {len(self.ops)} edit(s), {steps} step(s) in {time.time() - t0:.0f} s")
        if not self.ops:
            return reply, False
        if self.dirty or self.last_run is None:  # (e.g. it ran out of steps while editing)
            self.status("Testing…")
            self._run()
        report, problems, files = self.last_run
        plan = {"ops": self.ops, "log": list(self.edit.log), "reply": reply, "problems": problems, "test": self.test,
                "preview": self._result_text(rows=int(s.get("preview_rows") or 5), markdown=True), "report": report}
        warnings = list(self.destructive)
        if problems:
            warnings.append("leave these problems (the test still shows them): " + "; ".join(problems[:4]))
        warnings += [f"when what was built runs, it would {'overwrite' if f['change'] == 'modified' else 'delete'} "
                     f"{os.path.relpath(f['path'], wf.base_dir or '.')}" for f in files if f["change"] in ("modified", "deleted")]
        if warnings:
            self.pending = plan
            self.nodes = sorted({int(op[k]) for op in self.ops for k in ("node", "from", "to")
                                 if isinstance(op.get(k), int) and op[k] in wf.nodes and op[k] not in self.hidden})
            return (reply + CONFIRM + " - this would:\n" + "\n".join(f"- {w}" for w in warnings) +
                    "\n\nShall I go ahead? (Yes / No)" + SUMMARY + " it would make\n" +
                    "\n".join(f"- {line}" for line in plan["log"])), True
        return self._apply(plan), False

    def _apply(self, plan, confirmed=False) -> str:
        wf = self.wf
        editable, gid = self.scope(wf)
        with wf.live_edits(), wf.batch():  # (one undo step per message)
            live = Edit(wf, editable, gid, self.me, self.hidden, self.group)
            errors = live.apply(plan["ops"])
        self.nodes = sorted(n for n in set(live.added) | live.touched if n in wf.nodes)
        sandboxed = self.s.get("sandbox", True)
        moved = 0 if sandboxed else transfer_results(plan["test"], wf, live.idmap, self.hidden)
        if live.log:
            self.after_change()
        lines = [("Done." if confirmed else plan["reply"])]
        if errors:
            lines.append("\n**Some changes failed** (the workflow changed meanwhile?):\n" +
                         "\n".join(f"- {e}" for e in errors[:5]))
        lines.append(SUMMARY + "\n" + "\n".join(f"- {line}" for line in live.log))
        ran = [r for r in plan["report"] if r["state"] == "executed"]
        skipped = [r for r in plan["report"] if r["state"] == "skipped"]
        test = (f"\n**Test** (on a copy, in a sandbox): {len(ran)} node(s) ran" if sandboxed else
                f"\n**Run** (sandbox off - the real files): {len(ran)} node(s) ran"
                + (f", their results are in the workflow ({moved} node(s))" if moved else "")) + \
            (f", {len(skipped)} not run there (scripts and the like)" if skipped else "")
        if plan["problems"]:
            test += "; problems:\n" + "\n".join(f"- {p}" for p in plan["problems"][:5])
        lines.append(test)
        if plan["preview"]:
            lines.append("\n" + plan["preview"])
        return "\n".join(lines)

    # -- tools ----------------------------------------------------------------------------------
    def _tool(self, name, args) -> str:
        self.ctx.log(f"Agent tool {name} {_short(args, 200)}")
        try:
            fn = getattr(self, f"t_{name}", None) if isinstance(name, str) else None
            if fn is None:
                return f"Error: there is no tool {name!r}. The tools are: {', '.join(t['name'] for t in TOOLS)}"
            if "_unreadable" in args:
                return "Error: the arguments were not valid JSON"
            return fn(**args)
        except TypeError as e:
            return f"Error: wrong arguments for {name}: {e}"
        except CanceledExecutionError:
            raise
        except Exception as e:  # noqa: BLE001 - errors go back to the model
            return f"Error: {e}"

    def _edit(self, op) -> int | None:
        nid = self.edit.one(op)
        self.ops.append(op)
        self.dirty = True
        return nid

    def t_find_node_types(self, query=""):
        return search_types(self.test, query)

    def t_node_type_help(self, types=()):
        if isinstance(types, str):
            types = [types]
        known = {t["name"].lower(): t["name"] for t in self.test.node_types()}
        out, unknown = [], []
        for name in types:
            if str(name).lower() in known:
                self.helped.add(known[str(name).lower()])
                out.append(known[str(name).lower()])
            else:
                close = difflib.get_close_matches(str(name), list(known.values()), n=3, cutoff=0.4)
                unknown.append(f"no node type {name!r}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        text = type_specs(self.test, out) + ("\n\n" if out and unknown else "") + "; ".join(unknown)
        return (text + ("\n\n" + FORMATS if out else "")) or "No types given."

    def t_list_files(self, folder=""):
        base = os.path.abspath(self.wf.base_dir or ".")
        root = os.path.abspath(os.path.join(base, folder or ""))
        if not (root == base or root.startswith(base + os.sep)) or not os.path.isdir(root):
            return f"Error: no folder {folder!r} in the workflow's folder"
        out = []
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith((".", "__")))
            for f in sorted(files):
                if not f.startswith("."):
                    p = os.path.join(d, f)
                    out.append(f"{os.path.relpath(p, base)} ({os.path.getsize(p):,} bytes)")
            if len(out) > 150:
                break
        return "\n".join(out[:150]) or "(no files)"

    def t_show_workflow(self):
        return describe(self.test, self.edit.editable, self.me, self.hidden, self.group)

    def _after_edit(self, nid) -> str:
        if nid is None or nid not in self.test.nodes:
            return ""
        out = node_line(self.test, nid)
        downstream = [c.dest_id for c in self.test.connections if c.source_id == nid and c.dest_id in self.edit.editable]
        for d in downstream[:3]:
            out += "\nNext: " + node_line(self.test, d, settings=False)
        return out

    def t_add_node(self, type="", label="", settings=None, after=None, **extra):
        if extra:
            settings = {**(settings or {}), **extra}  # (settings given as arguments)
        op = {"op": "add", "type": type, "label": label, "settings": settings or {}, "after": after}
        nid = self.edit.one(op)
        op["id"] = nid
        self.ops.append(op)
        self.dirty = True
        name = self.test.nodes[nid].name
        hint = "" if name in self.helped else f"\n(node_type_help {name!r} shows its settings)"
        return f"Added. {self._after_edit(nid)}{hint}"

    def t_configure_node(self, node=None, settings=None, label=None):
        nid = self._edit({"op": "configure", "node": node, "settings": settings or {}, "label": label})
        return f"Configured. {self._after_edit(nid)}"

    def _conn(self, kind, from_node, from_port, to_node, to_port):
        src, dst = self.edit.node(from_node), self.edit.node(to_node, True)
        sp, dp = int(from_port or 1), int(to_port or 1)
        before = [c.key() for c in self.test.connections if c.dest_id == dst and c.dest_port == dp]
        op = {"op": kind, "from": src, "from_port": sp, "to": dst, "to_port": dp}
        self._edit(op)
        n = len(self.destructive)
        if kind == "disconnect" and (src, sp, dst, dp) in self.orig_conns:
            self.destructive.append(f"remove the connection {self._name(src)}:{sp} -> {self._name(dst)}:{dp}")
        if kind == "connect":
            for k in before:
                if k in self.orig_conns and k != (src, sp, dst, dp):
                    self.destructive.append(f"replace the connection {self._name(k[0])}:{k[1]} -> {self._name(dst)}:{dp} "
                                            f"by one from {self._name(src)}:{sp}")
        return self._after_edit(dst) + (ASK_OK if len(self.destructive) > n else "")

    def _name(self, nid):
        nc = self.test.nodes.get(nid) or self.wf.nodes.get(nid)
        return f"#{nid} \"{nc.annotation}\"" if nc else f"#{nid}"

    def t_connect(self, from_node=None, to_node=None, from_port=1, to_port=1):
        return "Connected. " + self._conn("connect", from_node, from_port, to_node, to_port)

    def t_disconnect(self, from_node=None, to_node=None, from_port=1, to_port=1):
        return "Disconnected. " + self._conn("disconnect", from_node, from_port, to_node, to_port)

    def t_remove_node(self, node=None):
        nid = self.edit.node(node, True)
        name = self._name(nid)
        self._edit({"op": "remove", "node": nid})
        if nid in self.orig_nodes:
            self.destructive.append(f"remove the node {name} [{self.wf.nodes[nid].name if nid in self.wf.nodes else ''}]")
            return f"Removed {name} on the copy.{ASK_OK}"
        return f"Removed {name}."

    def _run(self, ids=None):
        ids = sorted(n for n in (ids if ids is not None else self.edit.editable) if n in self.test.nodes)
        self.last_run = run_sandboxed(self.test, ids, self.s)
        self.dirty = False
        return self.last_run

    def t_run(self):
        if not self.edit.editable:
            return "You have no nodes yet."
        self.status("Testing…")
        report, problems, files = self._run()
        lines = [f"#{r['id']} [{r['name']}] \"{r['label']}\": {r['state']}" + (f", {r['rows']} rows" if r["rows"] is not None else "")
                 + (f" - {r['message'][:300]}" if r["message"] and r["state"] != "executed" else "") for r in report]
        if files:
            lines.append("Files it would write: " + ", ".join(f"{f['path']} ({f['change']})" for f in files[:8]))
        return "\n".join(lines) + ("\nProblems:\n- " + "\n- ".join(problems) if problems else "\nNo problems.")

    def t_preview(self, node=None, port=1, rows=10):
        nid = self.edit.node(node)
        nc = self.test.nodes[nid]
        if not nc.executed or self.dirty:
            self.status("Testing…")
            mine = (self.test.ancestors(nid) | {nid}) & (self.edit.editable | {nid})
            self._run(mine if not self.dirty else None)
        if not nc.executed:
            return f"#{nid} did not run: {nc.message[1] if nc.message else 'a node before it did not run'}"
        outs = nc.outputs or []
        port = int(port or 1)
        if port >= len(outs):
            return f"#{nid} has {len(outs) - 1} output port(s)"
        o = outs[port]
        if isinstance(o, pl.DataFrame):
            return f"#{nid} output {port}: {o.height} rows; columns {_columns(o.schema)}\n" + \
                markdown_table(o, max(1, min(int(rows or 10), 50)), cols=20)
        return f"#{nid} output {port}: {type(o).__name__} " + _short(getattr(o, "summary", lambda: {})(), 400)

    def _result_text(self, rows=5, markdown=False) -> str:
        """The first rows of the result: the last table of your nodes that ran."""
        report = self.last_run[0] if self.last_run else []
        for r in reversed([r for r in report if r["state"] == "executed"]):
            nc = self.test.nodes.get(r["id"])
            df = next((o for o in (nc.outputs or [])[1:] if isinstance(o, pl.DataFrame)), None) if nc else None
            if df is not None and df.width:
                title = f"**{nc.annotation}**" if markdown else f"#{nc.id} \"{nc.annotation}\""
                return f"{title}\n\n" + markdown_table(df, max(1, rows), cols=7 if markdown else 20)
        return ""


def _history(msgs, memory):
    """The earlier messages for the model: the agent's replies without the reports it added."""
    out = []
    for m in msgs[-max(0, int(memory or 20)):] if memory else []:
        content = m["content"].split(SUMMARY)[0].split(CONFIRM)[0].split("\n\n**Some changes failed**")[0]
        out.append({"role": m["role"], "content": content})
    while out and out[0]["role"] == "assistant":
        out = out[1:]
    return out


AGENT_FIELDS = [
    F("group", "string", "Build in group", "",
      "The group annotation it builds in (made on its first node): it changes only the nodes in it. "
      "Empty: \"Built by agent #<its id>\" - a group of its own."),
    F("guidance", "code", "Guidance", "", "More instructions for the agent (e.g. house rules, where data is)."),
    F("steps", "int", "Tool calls per message at most", 40),
    F("rounds", "int", "Rounds to fix failures", 3, "How often failures of the test are given back to the model."),
    F("check", "bool", "Check the result", True, "The model sees the first rows of what it built and fixes it "
      "if they are not what was asked for (when it did not look itself)."),
    F("memory", "int", "Memory (messages)", 20),
    F("preview_rows", "int", "Rows shown in replies", 5),
    F("sandbox", "bool", "Test in a sandbox", True,
      "Off: the agent's test runs work on the real files (they can write, overwrite and delete them) and run "
      "scripts, and their results go straight into this workflow. Also the checkbox on the chat."),
    F("allow_unsafe", "bool", "Run script and custom nodes in tests", False, "They can reach files outside the sandbox."),
    F("sandbox_mb", "int", "Sandbox size (MB)", 500),
    F("run_when_done", "bool", "Run what was built when the chat ends", True),
]

AGENT_DESCRIPTION = (
    "Tell it what to build: \"read agent/sales.csv\", \"keep the rows with amount over 100\", \"sum the revenue "
    "per region as a bar chart\". For each message the model works with tools - it finds node types and reads "
    "their help, adds, configures and connects nodes, runs and previews them - on a copy of the workflow, which "
    "runs in a file <b>sandbox</b> (files are copied in when used, nothing real is written; scripts and custom "
    "nodes are not run there unless allowed). What it built is tested, and the model fixes failures and checks "
    "the first rows of the result. Then the same edits are made to the workflow, while the chat goes on: the "
    "reply lists the changes and shows the first rows. The agent changes only the nodes of its group "
    "(<b>Build in group</b>; by default a group of its own) - other nodes it can only read from - and each "
    "message is one undo step. Removing nodes or connections that were there, or overwriting files, waits for "
    "your OK (Yes / No in the chat). When the chat ends, what was built runs for real. Check what it builds: "
    "models make mistakes."
)


def _run_at_end(ctx, s, group):
    """Once per run: when the run ends, run what was built (the group's nodes)."""
    sw = ctx.script_workflow()
    what = f"run what {group} built"
    if not s.get("run_when_done", True) or any(w == what for _, w, _ in (sw._wf._after_run or [])):
        return
    sw._record(("execute", _GroupNodes(sw, group)), what)


class _GroupNodes(list):
    """The nodes of a group (when the run ends), for Workflow._apply_script_edits."""

    def __init__(self, sw, group):
        super().__init__()
        self.sw, self.group = sw, group

    def __iter__(self):
        from jupyterlab_verknode.engine.workflow_edits import NodeRef

        wf = self.sw._wf
        ids = next((g["nodes"] for g in wf.groups if g["text"].strip().lower() == (self.group or "").lower()), [])
        return iter([NodeRef(self.sw, n) for n in ids if n in wf.nodes])

    def __bool__(self):
        return True


class WorkflowAgentChat(NodeModel):
    factory = "verknode.llm.WorkflowAgentChat"
    name = "Workflow Agent Chat"
    category = "AI/LLM"
    icon = "🛠💬"
    node_type = "Manipulator"
    sandbox_unsafe = True  # (changes the workflow, waits for the canvas)
    needs_canvas = True  # (not in headless runs: export)
    renderer = "chat"
    width, height = 420, 520
    short_description = "A chat on the canvas with an agent that builds the workflow you describe (one node)."
    description = (
        "One node that carries the whole chat: run it (or Begin chat on it), and it shows a chat on the canvas, "
        "waits for your message, builds, answers, and waits for the next message - until End chat (Cancel stops "
        "it too). The conversation stays: running the node again, or Resume chat, goes on with it; New chat "
        "starts again. Its output is the conversation: connect it to the input of another Workflow Agent Chat "
        "to go on from this chat there (the new chat starts with it, and builds in a group of its own). "
        "Tab in the message box adds the nodes selected on the canvas (as @#id); Show highlights the agent's "
        "nodes, Show changes under a reply what it changed. " + AGENT_DESCRIPTION
    )
    in_ports = [Port(TABLE, "Conversation", "A chat to go on from (role and content, e.g. of another Workflow "
                     "Agent Chat): optional", optional=True)]
    out_ports = [Port(TABLE, "Conversation", "role and content: what was said")]
    fields = [
        F("title", "string", "Title", "Workflow agent", "Shown above the chat on the canvas."),
        F("greeting", "string", "Greeting",
          "Hi! Tell me what to build - e.g. \"read sales.csv\", then what to do with it. I try each step on a copy "
          "first, and ask before removing anything that is there.", "The first message (empty: none)."),
        F("placeholder", "string", "Placeholder", "Describe what to build…"),
        *AGENT_FIELDS,
        *server_fields(),
    ]

    def configure(self, in_specs, s, ctx):
        if in_specs and in_specs[0] is not None:
            _check_conversation(in_specs[0])
        return [pl.Schema(CONVERSATION)]

    keeps_display_on_reset = True  # (the conversation stays - reset, canceled, saved - to resume it)

    def keeps_display(self, old: dict, new: dict) -> bool:
        return True  # (a setting changed: the conversation stays on the chat)

    def widget(self, s: dict, display) -> dict:
        # (agent: Begin / Resume chat, and the Sandbox checkbox - the setting)
        data = {**(display or {}), "agent": True, "sandbox": bool(s.get("sandbox", True))}
        return {"type": "output", "renderer": self.renderer, "title": s.get("title") or "", "width": self.width,
                "height": self.height, "data": data, "interactive": False, "selection": None}

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=CONVERSATION)]
        sw = ctx.script_workflow()
        wf, me = sw._wf, sw._me
        s = dict(s)
        s["sandbox"] = bool(s.get("sandbox", True))
        msgs: list[dict] = []
        # it goes on with the conversation it shows (Resume chat, or just running it) - New chat: afresh
        got = [x for x in ctx.received() if isinstance(x, dict)]
        resume = next((x["resume"] for x in reversed(got) if x.get("resume")), None)
        if resume is None and not any(x.get("new") for x in got):
            resume = (ctx.previous_display or {}).get("messages")
        if isinstance(resume, list) and any(isinstance(m, dict) and m.get("role") == "user" for m in resume):
            msgs = [{k: m[k] for k in ("role", "content", "meta", "nodes") if k in m} for m in resume
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
            ctx.log(f"Chat resumed ({len(msgs)} messages)")
        earlier = inputs[0] if inputs and inputs[0] is not None else None
        if not msgs and earlier is not None and earlier.height:
            # (a new chat goes on from the chat it gets)
            msgs = [{"role": r, "content": c or "", "meta": "the chat before"} for r, c in
                    zip(earlier["role"].to_list(), earlier["content"].to_list()) if r in ("user", "assistant")]
            msgs.append({"role": "assistant", "content": "I have the chat above (from the node before) - what next? "
                         "I build in a group of my own, and can use what was built there."})
            ctx.log(f"Chat goes on from the chat before ({len(msgs) - 1} messages)")
        greeting = (s.get("greeting") or "").strip()
        if greeting and not msgs:
            msgs.append({"role": "assistant", "content": greeting})
        base = {"placeholder": s.get("placeholder") or "", "title": s.get("title") or ""}

        def show(**kw):
            group_nodes = sorted(session.scope(wf)[0]) if session is not None else []
            ctx.set_display({**base, "messages": list(msgs), "group": session.group if session is not None else "",
                             "group_nodes": group_nodes, **kw})

        session = None

        def set_sandbox(value):
            # (the checkbox while the chat runs: it is the setting at once - without running the chat again)
            s["sandbox"] = value
            with wf._lock:
                nc = wf.nodes.get(me)
                if nc is not None:
                    nc.settings = {**nc.settings, "sandbox": value}
            wf._changed()

        session = AgentSession(ctx, s, wf, me, status=lambda text: show(waiting=False, status=text),
                               after_change=lambda: _run_at_end(ctx, s, session.group))
        model = session.srv["model"]
        try:
            while True:
                show(waiting=True)
                got = ctx.wait_for_input() or {}
                if got.get("end"):
                    show(waiting=False, ended=True)
                    ctx.log("Chat ended")
                    break
                if "sandbox" in got and "text" not in got:
                    set_sandbox(bool(got["sandbox"]))
                    ctx.log(f"Sandbox {'on' if s['sandbox'] else 'OFF: the agent works on the real files'}")
                    continue
                text = str(got.get("text") or "").strip()
                if not text:
                    continue
                msgs.append({"role": "user", "content": text})
                show(waiting=False, status="Thinking…")
                t0 = time.time()
                history = _history([m for m in msgs[:-1] if m["role"] in ("user", "assistant")], s.get("memory", 20))
                try:
                    reply, confirm = session.turn(text, history)
                except CanceledExecutionError:
                    raise
                except Exception as e:  # noqa: BLE001 - the chat goes on
                    reply, confirm = f"**Something went wrong:** {e}", False
                    session.pending = None
                msgs.append({"role": "assistant", "content": reply, "meta": f"{model} · {time.time() - t0:.1f} s",
                             **({"confirm": True} if confirm else {}),
                             **({"nodes": session.nodes} if getattr(session, "nodes", None) else {})})
        except CanceledExecutionError:
            show(waiting=False)
            raise
        return [pl.DataFrame({"role": [m["role"] for m in msgs], "content": [m["content"] for m in msgs]},
                             schema=CONVERSATION)]


class WorkflowAgent(NodeModel):
    factory = "verknode.llm.WorkflowAgent"
    name = "Workflow Agent"
    category = "AI/LLM"
    icon = "🛠"
    node_type = "Manipulator"
    sandbox_unsafe = True  # (changes the workflow)
    short_description = "The workflow agent for a chat loop of your own (else: Workflow Agent Chat)."
    description = (
        "Put it in a chat loop instead of an LLM Chat (Chat Loop Start -> Workflow Agent -> Concatenate -> "
        "Recursive Loop End); Workflow Agent Chat is the same in one node. It never touches the chat loop it is "
        "in. " + AGENT_DESCRIPTION
    )
    in_ports = [Port(TABLE, "Conversation", "role and content columns (e.g. of a Chat Loop Start)")]
    out_ports = [Port(TABLE, "Answer", "One row: role, content, model, seconds, tokens, confirm - for the conversation")]
    fields = [*AGENT_FIELDS, *server_fields()]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0]
        missing = [c for c in ("role", "content") if c not in spec]
        if missing:
            raise InvalidSettingsError(f"The conversation needs the columns role and content (missing: {', '.join(missing)})")
        return [pl.Schema(ANSWER)]

    def execute(self, inputs, s, ctx):
        conv = inputs[0]
        if ctx.dry:
            return [pl.DataFrame(schema=ANSWER)]
        msgs = [{"role": r, "content": c or ""} for r, c in zip(conv["role"].to_list(), conv["content"].to_list())
                if r in ("user", "assistant")]
        if not msgs or msgs[-1]["role"] != "user":
            return [pl.DataFrame(schema=ANSWER)]  # (e.g. the chat was ended)
        t0 = time.time()
        sw = ctx.script_workflow()
        wf, me = sw._wf, sw._me
        hidden = wf.ancestors(me) | wf.descendants(me)  # (the chat loop it runs in)
        session = AgentSession(ctx, s, wf, me, hidden, after_change=lambda: _run_at_end(ctx, s, session.group))
        # (a change waiting for the OK, from the message before: kept on the node's model)
        pending = getattr(self, "_pending", None)
        if pending is not None and len(msgs) >= 2 and msgs[-2]["content"] == pending[0]:
            session.pending = pending[1]
        self._pending = None
        reply, confirm = session.turn(msgs[-1]["content"], _history(msgs[:-1], s.get("memory", 20)))
        if confirm:
            self._pending = (reply, session.pending)
        return [pl.DataFrame({"role": ["assistant"], "content": [reply], "model": [session.srv["model"]],
                              "seconds": [time.time() - t0], "tokens": [None], "confirm": [confirm]}, schema=ANSWER)]
