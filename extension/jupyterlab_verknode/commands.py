"""Python equivalents of workflow edits (shown in the editor console) and script export.

The commands use the ``verknode`` client API, e.g.::

    import verknode
    wf = verknode.open("notebooks/examples/demo.vkf")
    n = wf.add_node("CSV Reader", x=100, y=100, path="data.csv")
    wf.connect(n, wf.node(3))
    wf.execute()
"""
from __future__ import annotations

import copy
import os
import pprint

from .engine.workflow import GROUP_COLORS, Workflow


def _lit(value) -> str:
    """Python literal; long structures are pretty printed."""
    text = repr(value)
    if len(text) > 100 and isinstance(value, (dict, list)):
        text = pprint.pformat(value, width=96, sort_dicts=False)
    return text


def _kwargs(settings: dict) -> str:
    parts = []
    for k, v in settings.items():
        parts.append(f"{k}={_lit(v)}" if k.isidentifier() else f"**{{{k!r}: {_lit(v)}}}")
    return ", ".join(parts)


# add_node(node_type, x, y, settings=None, **kwargs): settings named like these
# parameters must go into the settings dict
_ADD_NODE_PARAMS = {"node_type", "x", "y", "settings"}


def _add_node_settings(settings: dict) -> str:
    """Trailing add_node arguments for node settings."""
    if not settings:
        return ""
    plain = {k: v for k, v in settings.items() if k.isidentifier() and k not in _ADD_NODE_PARAMS}
    rest = {k: v for k, v in settings.items() if k not in plain}
    out = f", {_kwargs(plain)}" if plain else ""
    if rest:
        out += f", settings={_lit(rest)}"
    return out


def _ports(src_port: int, dst_port: int) -> str:
    out = ""
    if src_port != 1:
        out += f", src_port={src_port}"
    if dst_port != 1:
        out += f", dst_port={dst_port}"
    return out


def _changed_settings(nc, before: dict | None = None) -> dict:
    base = before if before is not None else nc.model.default_settings()
    return {k: v for k, v in nc.settings.items() if base.get(k) != v}


def _binding_lines(target: str, before: dict, after: dict) -> list[str]:
    lines = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key) or {}, after.get(key) or {}
        if old.get("used") != new.get("used"):
            lines.append(f"{target}.bind({key!r}, {new['used']!r})" if new.get("used")
                         else f"{target}.unbind({key!r})")
        if old.get("exposed") != new.get("exposed"):
            lines.append(f"{target}.expose({key!r}, {new.get('exposed')!r})")
    return lines


class Recorder:
    """Captures workflow state before an operation to describe it afterwards."""

    def __init__(self, wf: Workflow, op: str, args: dict):
        self.wf, self.op, self.args = wf, op, args
        self.before = None
        self.groups_before = {g["id"] for g in wf.groups}
        if op == "set_settings":
            nc = wf.nodes.get(int(args["id"]))
            if nc is not None:
                self.before = (copy.deepcopy(nc.settings), copy.deepcopy(nc.var_settings))

    def lines(self, result) -> list[str]:
        wf, op, a = self.wf, self.op, self.args
        node = lambda nid: f"wf.node({int(nid)})"  # noqa: E731
        if op == "add_node":
            nid = result["id"]
            inside = f", metanode={int(a['parent'])}" if a.get("parent") is not None else ""
            return [f"wf.add_node({wf.nodes[nid].name!r}, x={round(a.get('x', 0))}, y={round(a.get('y', 0))}{inside})"
                    f"  # -> node {nid}"]
        if op == "remove_nodes":
            return [f"wf.remove({', '.join(str(int(i)) for i in a['ids'])})"]
        if op in ("connect", "disconnect"):
            return [f"wf.{op}({_endpoints(wf, int(a['sourceID']), int(a['sourcePort']), int(a['destID']), int(a['destPort']))})"]
        if op == "rewire":
            out = [f"{node(p['id'])}.move({round(p['x'])}, {round(p['y'])})" for p in a.get("positions") or []
                   if int(p["id"]) in wf.nodes]
            if a.get("delete"):
                out.append(f"wf.remove({', '.join(str(int(i)) for i in a['delete'])})")
            out += [f"wf.disconnect({_endpoints(wf, int(c['sourceID']), int(c['sourcePort']), int(c['destID']), int(c['destPort']))})"
                    for c in a.get("remove", [])]
            out += [f"wf.connect({_endpoints(wf, int(c['sourceID']), int(c['sourcePort']), int(c['destID']), int(c['destPort']))})"
                    for c in a.get("add", [])]
            return out
        if op == "collapse":
            name = f", name={a['name']!r}" if a.get("name") else ""
            return [f"wf.collapse({', '.join(str(int(i)) for i in a['ids'])}{name})  # -> metanode {result['id']}"]
        if op == "expand":
            return [f"wf.metanode({int(a['id'])}).expand()"]
        if op == "rename_metanode":
            return [f"wf.metanode({int(a['id'])}).name = {a['name']!r}"]
        if op == "add_metanode_port":
            return [f"wf.metanode({int(a['id'])}).add_port({a['side']!r}, {a.get('type', 'table')!r})  # -> port {result}"]
        if op == "remove_metanode_port":
            return [f"wf.metanode({int(a['id'])}).remove_port({a['side']!r}, {int(a['index'])})"]
        if op == "move":
            return [f"{node(p['id'])}.move({round(p['x'])}, {round(p['y'])})" for p in a["positions"]]
        if op == "set_annotation":
            return [f"{node(a['id'])}.annotation = {a['text']!r}"]
        if op == "add_group":
            return [f"wf.add_group({', '.join(str(int(n)) for n in a['nodes'])}"
                    f"{_group_kwargs(a.get('text'), a.get('color'))})  # -> group {result['id']}"]
        if op == "update_group":
            g = f"wf.group({int(a['id'])})"
            out = []
            if a.get("text") is not None:
                out.append(f"{g}.text = {a['text']!r}")
            if a.get("color") is not None:
                out.append(f"{g}.color = {a['color']!r}")
            if a.get("nodes") is not None:
                out.append(f"{g}.nodes = [{', '.join(str(int(n)) for n in a['nodes'])}]")
            return out
        if op == "remove_group":
            return [f"wf.group({int(a['id'])}).remove()"]
        if op == "set_settings" and self.before is not None:
            nc = wf.nodes[int(a["id"])]
            settings, variables = self.before
            out = []
            changed = _changed_settings(nc, settings)
            if changed:
                out.append(f"{node(nc.id)}.configure({_kwargs(changed)})")
            return out + _binding_lines(node(nc.id), variables, nc.var_settings)
        if op == "set_workflow_variables":
            items = [(v.name, v.type, v.value) for v in wf.workflow_variables]
            return [f"wf.set_variables({_lit(items)})"]
        if op in ("execute", "reset"):
            ids = a.get("ids")
            return [f"wf.{op}({', '.join(str(int(i)) for i in ids)})" if ids else f"wf.{op}()"]
        if op in ("cancel", "undo", "redo", "clear", "reload_nodes"):
            return [f"wf.{op}()"]
        if op == "set_node_paths":
            extra = f", auto_reload={bool(a['autoReload'])}" if a.get("autoReload") is not None else ""
            return [f"wf.set_node_paths({wf.node_paths!r}{extra})"]
        if op == "create_node_template":
            return [f"wf.create_node_template({(a.get('folder') or 'nodes')!r})  # -> {result['file']}"]
        if op == "paste":
            return self._paste_lines(result)
        return []

    def _paste_lines(self, new_ids) -> list[str]:
        wf = self.wf
        if self.args["fragment"].get("metanodes"):
            return [f"# pasted {len(self.args['fragment']['nodes'])} nodes with metanodes: "
                    f"wf.paste(wf.copy(...)) repeats a paste from Python"]
        lines = [f"# paste = nodes {', '.join(map(str, new_ids))}:"]
        chosen = set(new_ids)
        for nid in new_ids:
            nc = wf.nodes[nid]
            extra = _add_node_settings(_changed_settings(nc))
            lines.append(f"wf.add_node({nc.name!r}, x={round(nc.x)}, y={round(nc.y)}{extra})  # -> node {nid}")
            lines += _binding_lines(f"wf.node({nid})", {}, nc.var_settings)
        for c in wf.connections:
            if c.source_id in chosen and c.dest_id in chosen:
                lines.append(f"wf.connect({c.source_id}, {c.dest_id}{_ports(c.source_port, c.dest_port)})")
        for g in wf.groups:
            if set(g["nodes"]) <= chosen and g["id"] not in self.groups_before:
                lines.append(f"wf.add_group({', '.join(map(str, g['nodes']))}"
                             f"{_group_kwargs(g['text'], g['color'])})  # -> group {g['id']}")
        return lines


def _endpoints(wf: Workflow, s: int, sp: int, d: int, dp: int) -> str:
    """Arguments of wf.connect(); metanode ports are written as the metanode (outside) or its
    inport()/outport() (inside)."""
    b = wf._boundaries()
    src, dst = str(s), str(d)
    if s in b:
        mid, side, k = b[s]
        if side == "out":
            src, sp = f"wf.metanode({mid})", k + 1
        else:
            src = f"wf.metanode({mid}).inport({k + 1})"
    if d in b:
        mid, side, k = b[d]
        if side == "in":
            dst, dp = f"wf.metanode({mid})", k + 1
        else:
            dst = f"wf.metanode({mid}).outport({k + 1})"
    return f"{src}, {dst}{_ports(sp, dp)}"


def _group_kwargs(text, color) -> str:
    out = ""
    if text:
        out += f", text={text!r}"
    if color and color.lower() != GROUP_COLORS[0]:
        out += f", color={color!r}"
    return out


def open_hint(path: str) -> str:
    return f"import verknode; wf = verknode.open({path!r})  # control this workflow from a notebook or console"


def to_python(wf: Workflow, path: str | None) -> str:
    """Script that rebuilds the workflow with the verknode API."""
    target = path or "workflow.vkf"
    lines = [
        '"""Rebuild of ' + os.path.basename(target) + ' generated by verknode."""',
        "import verknode",
        "",
        f"wf = verknode.open({target!r})  # change the path to build a copy",
        "",
    ]
    header = lines
    lines = ["wf.clear()"]  # (the rebuild, in a batch below)
    if wf.node_paths:
        lines.append(f"wf.set_node_paths({wf.node_paths!r})  # custom node folders")
    if wf.workflow_variables:
        lines.append(f"wf.set_variables({_lit([(v.name, v.type, v.value) for v in wf.workflow_variables])})")
    lines.append("")
    names = {}
    bounds = wf._boundaries()
    for nid in sorted((n for n in wf.nodes if n not in bounds), key=lambda n: (wf.nodes[n].x, wf.nodes[n].y, n)):
        nc = wf.nodes[nid]
        var = f"n{nid}"
        names[nid] = var
        extra = _add_node_settings(_changed_settings(nc))
        lines.append(f"{var} = wf.add_node({nc.name!r}, x={round(nc.x)}, y={round(nc.y)}{extra})")
        if nc.annotation and nc.annotation != f"Node {nid}":
            lines.append(f"{var}.annotation = {nc.annotation!r}")
        lines += _binding_lines(var, {}, nc.var_settings)
    if wf.connections:
        lines.append("")
    # connections through metanode ports are made directly; collapsing below recreates the ports
    def targets(d, dp):
        if d not in bounds:
            return [(d, dp)]
        return [t for c in wf.connections if c.source_id == d for t in targets(c.dest_id, c.dest_port)]
    for c in wf.connections:
        if c.source_id not in bounds:
            for d, dp in targets(c.dest_id, c.dest_port):
                lines.append(f"wf.connect({names[c.source_id]}, {names[d]}{_ports(c.source_port, dp)})")
    if wf.metanodes:
        lines.append("")

    def depth(mid):
        p = wf.metanodes[mid]["parent"]
        return 0 if p is None else 1 + depth(p)
    for mid in sorted(wf.metanodes, key=depth, reverse=True):  # innermost first
        m = wf.metanodes[mid]
        names[mid] = f"m{mid}"
        items = [names[n] for n, nc in wf.nodes.items() if nc.parent == mid and n not in bounds]
        items += [names[x] for x, xm in wf.metanodes.items() if xm["parent"] == mid and x in names]
        if items:
            lines.append(f"m{mid} = wf.collapse({', '.join(items)}, name={m['name']!r})")
        else:
            del names[mid]
    if wf.groups:
        lines.append("")
    for g in wf.groups:
        lines.append(f"wf.add_group({', '.join(names[n] for n in g['nodes'])}{_group_kwargs(g['text'], g['color'])})")
    while lines and not lines[-1]:
        lines.pop()
    body = ["with wf.batch():  # one undo step; fast also for big workflows"] + [f"    {l}" if l else "" for l in lines]
    return "\n".join(header + body + ["", "# wf.execute()", "# wf.save()", ""])
