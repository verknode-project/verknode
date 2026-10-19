"""Read/write .vkf files: a zip archive of JSON (workflow, node settings), Parquet and pickles.

    workflow.json                     nodes, connections, workflow variables
    workflowset.meta.json             author, description, creation date
    <Node Name> (#<id>)/settings.json node settings, flow variable bindings, state
    <Node Name> (#<id>)/port_<n>/spec.json
    <Node Name> (#<id>)/port_<n>/data.parquet   saved output table (executed nodes)
"""
from __future__ import annotations

import io
import json
import pickle
import re
import zipfile

import polars as pl

from .compat import read_parquet, write_parquet

from .. import __version__
from .flowvars import FlowVariable, stack_from_json, stack_to_json
from .node import FLOWVAR, FLOWVARIABLE, INACTIVE, TABLE, Port, PortObject
from .types import spec_to_json
from .workflow import GROUP_COLORS, Connection, MissingNodeModel, NodeContainer, Workflow, clean_widget_layout

FORMAT = "vkf"
FORMAT_VERSION = "1.0.0"


def _folder(nc: NodeContainer) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", nc.name)
    return f"{name} (#{nc.id})"


def _dump(obj) -> bytes:
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def save_workflow(wf: Workflow) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        nodes = []
        # (a parallel chunk loop's temporary metanode is not saved)
        temporary = {m for m, v in wf.metanodes.items() if v.get("temporary")}
        kept = {nid for nid, nc in wf.nodes.items() if nc.parent not in temporary}
        for nc in wf.nodes.values():
            if nc.id not in kept:
                continue
            folder = _folder(nc)
            ports = []
            if nc.executed:
                for i, port in enumerate(nc.model.out_ports, start=1):
                    obj = nc.outputs[i]
                    loc = f"port_{i}"
                    entry = {"index": i, "type": port.type, "port_dir_location": loc}
                    if obj is INACTIVE:
                        entry["inactive"] = True
                    elif isinstance(obj, PortObject):
                        z.writestr(f"{folder}/{loc}/object.pkl", pickle.dumps(obj))
                    elif isinstance(obj, pl.DataFrame):
                        z.writestr(f"{folder}/{loc}/spec.json", _dump({"columns": spec_to_json(obj.schema)}))
                        pq = io.BytesIO()
                        write_parquet(obj, pq)
                        z.writestr(f"{folder}/{loc}/data.parquet", pq.getvalue(), compress_type=zipfile.ZIP_STORED)
                    ports.append(entry)
            settings = {
                "factory": nc.factory,
                "node_name": nc.name,
                "customDescription": nc.custom_description,
                "state": "EXECUTED" if nc.executed else "CONFIGURED",
                "inactive": nc.inactive,
                "message": {"level": nc.message[0], "text": nc.message[1]} if nc.message else None,
                "model": nc.settings,
                "variables": nc.var_settings,
                "flow_stack": stack_to_json(nc.out_stack) if nc.executed and nc.out_stack else [],
                "ports": ports,
                # run time and rows of this run, and of the last one (kept across resets, for estimates)
                "timing": {"time": nc.exec_time if nc.executed else None, "rows": nc.rows if nc.executed else None,
                           "last_time": nc.last_time, "last_rows": nc.last_rows,
                           **({"last_iterations": nc.last_iterations} if nc.last_iterations else {}),
                           **({"last_parallel": nc.last_parallel} if nc.last_parallel else {})},
                "implementation": getattr(type(nc.model), "_verknode_hash", None),
                "port_types": {
                    "in": [p.type for p in nc.model.in_ports],
                    "out": [p.type for p in nc.model.out_ports],
                },
            }
            z.writestr(f"{folder}/settings.json", _dump(settings))
            if nc.executed and nc.view is not None:
                z.writestr(f"{folder}/view.html", nc.view)
            if nc.display is not None and (nc.executed or getattr(nc.model, "keeps_display_on_reset", False)):
                z.writestr(f"{folder}/display.json", _dump(nc.display))
            nodes.append({
                "id": nc.id,
                "node_settings_file": f"{folder}/settings.json",
                "factory": nc.factory,
                "annotation": nc.annotation,
                "ui_settings": {"x": nc.x, "y": nc.y, **({"widget": nc.widget_layout} if nc.widget_layout else {})},
                **({"parent": nc.parent} if nc.parent is not None else {}),
            })
        workflow = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_by": f"jupyterlab_verknode {__version__}",
            "next_node_id": wf.next_id,
            "workflow_variables": [v.to_json() for v in wf.workflow_variables],
            "custom_nodes": {"paths": wf.node_paths, "auto_reload": wf.auto_reload},
            "nodes": nodes,
            "connections": [c.to_json() for c in wf.connections if c.source_id in kept and c.dest_id in kept],
            "annotations": [{"type": "group", **g} for g in wf.groups],
            # metanodes: their nodes carry "parent"; inputs/outputs are the port pass-through nodes
            "metanodes": [m for m in wf.metanodes.values() if m["id"] not in temporary],
        }
        z.writestr("workflow.json", _dump(workflow))
        z.writestr("workflowset.meta.json", _dump(wf.meta))
    return buf.getvalue()


def load_workflow(data: bytes, base_dir: str | None = None) -> Workflow:
    """Load a .vkf archive; custom node folders are resolved against base_dir."""
    wf = Workflow()
    if base_dir:
        wf.base_dir = base_dir
    if not data:
        return wf
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"Not a .vkf workflow archive: {e}") from None
    with z:
        names = set(z.namelist())
        if "workflow.json" not in names:
            raise ValueError("Not a .vkf workflow archive: workflow.json missing")
        workflow = json.loads(z.read("workflow.json"))
        if "workflowset.meta.json" in names:
            wf.meta.update(json.loads(z.read("workflowset.meta.json")))
        wf.workflow_variables = [FlowVariable.from_json(v) for v in workflow.get("workflow_variables", [])]
        custom = workflow.get("custom_nodes") or {}
        wf.node_paths = list(custom.get("paths") or [])
        wf.auto_reload = bool(custom.get("auto_reload", True))
        if wf.node_paths:
            wf.reload_nodes(reason="workflow opened")
        dropped = []  # saved as executed, but the results could not be used
        for entry in workflow.get("nodes", []):
            nid = int(entry["id"])
            settings_file = entry["node_settings_file"]
            s = json.loads(z.read(settings_file)) if settings_file in names else {}
            factory = entry.get("factory") or s.get("factory")
            ui = entry.get("ui_settings", {})
            missing = None
            cls = wf.lookup(factory)
            if cls is None:
                pt = s.get("port_types", {})
                missing = MissingNodeModel(
                    factory, s.get("node_name"),
                    [Port(t, f"In {i + 1}") for i, t in enumerate(pt.get("in", []))],
                    [Port(t, f"Out {i + 1}") for i, t in enumerate(pt.get("out", []))],
                )
            nc = NodeContainer(nid, factory, ui.get("x", 0), ui.get("y", 0), model=missing or cls())
            nc.annotation = entry.get("annotation", nc.annotation)
            nc.widget_layout = clean_widget_layout(ui.get("widget"), (nc.x, nc.y))
            nc.parent = entry.get("parent")
            nc.custom_description = s.get("customDescription", "")
            known = {f.key for f in nc.model.fields}
            nc.settings.update({k: v for k, v in s.get("model", {}).items() if k in known or missing})
            nc.var_settings = s.get("variables", {}) or {}
            if s.get("message"):
                nc.message = (s["message"]["level"], s["message"]["text"])
            # results of custom nodes are only valid for the implementation that produced them
            same_impl = s.get("implementation") == getattr(cls, "_verknode_hash", None)
            if s.get("state") == "EXECUTED" and missing is None and same_impl:
                folder = settings_file.rsplit("/", 1)[0]
                _load_outputs(z, names, folder, nc, s)
            elif getattr(nc.model, "keeps_display_on_reset", False):
                # (e.g. a chat that was reset or canceled: its conversation, to resume it)
                folder = settings_file.rsplit("/", 1)[0]
                if f"{folder}/display.json" in names:
                    nc.display = json.loads(z.read(f"{folder}/display.json").decode("utf-8"))
            if s.get("state") == "EXECUTED" and not nc.executed:
                dropped.append(nid)
            timing = s.get("timing") or {}
            nc.last_time, nc.last_rows = timing.get("last_time"), timing.get("last_rows")
            nc.last_iterations = timing.get("last_iterations")
            nc.last_parallel = timing.get("last_parallel")
            if nc.executed:
                nc.exec_time, nc.rows = timing.get("time"), timing.get("rows")
            wf.nodes[nid] = nc
        # nodes with ports of their own (e.g. from their notebook) set them up first
        for nc in wf.nodes.values():
            if hasattr(nc.model, "update_ports"):
                nc.model.update_ports(nc.settings, wf.base_dir)
                # results saved for other ports (e.g. the notebook changed since) are stale
                if nc.executed and len(nc.outputs) != len(nc.model.out_ports) + 1:
                    nc.clear()
                    dropped.append(nc.id)
        for c in workflow.get("connections", []):
            conn = Connection(c["sourceID"], c["sourcePort"], c["destID"], c["destPort"])
            src, dst = wf.nodes.get(conn.source_id), wf.nodes.get(conn.dest_id)
            if src and dst and src.out_port(conn.source_port) and dst.in_port(conn.dest_port):
                wf.connections.append(conn)
        # results downstream of dropped ones are stale (e.g. a changed custom node)
        for nid in dropped:
            for d in wf.descendants(nid):
                wf.nodes[d].clear()
        wf.next_id = max([workflow.get("next_node_id", 1)] + [n + 1 for n in wf.nodes])
        for a in workflow.get("annotations", []):
            members = [int(n) for n in a.get("nodes", []) if int(n) in wf.nodes]
            if a.get("type", "group") == "group" and members:
                wf.groups.append({"id": int(a["id"]), "nodes": members, "text": a.get("text", ""),
                                  "color": a.get("color") or GROUP_COLORS[0]})
        wf.next_group_id = max([1] + [g["id"] + 1 for g in wf.groups])
        for m in workflow.get("metanodes", []):
            mid = int(m["id"])
            wf.metanodes[mid] = {"id": mid, "name": m.get("name", "Metanode"), "parent": m.get("parent"),
                                 "x": m.get("x", 0), "y": m.get("y", 0),
                                 "inputs": [b for b in m.get("inputs", []) if b in wf.nodes],
                                 "outputs": [b for b in m.get("outputs", []) if b in wf.nodes]}
        wf.next_id = max([wf.next_id] + [m + 1 for m in wf.metanodes])
    return wf


def _load_outputs(z, names, folder, nc: NodeContainer, s: dict):
    outputs = [INACTIVE if s.get("inactive") else FLOWVAR]
    saved = {p["index"]: p for p in s.get("ports", [])}
    for i, port in enumerate(nc.model.out_ports, start=1):
        entry = saved.get(i, {})
        if entry.get("inactive"):
            outputs.append(INACTIVE)
        elif port.type == FLOWVARIABLE:
            outputs.append(FLOWVAR)
        elif port.type != TABLE:
            path = f"{folder}/{entry.get('port_dir_location', f'port_{i}')}/object.pkl"
            if path not in names:
                return
            outputs.append(pickle.loads(z.read(path)))  # noqa: S301 - only trusted workflow files are opened
        else:
            path = f"{folder}/{entry.get('port_dir_location', f'port_{i}')}/data.parquet"
            if path not in names:
                return  # incomplete data: load node as not executed
            outputs.append(read_parquet(io.BytesIO(z.read(path))))
    nc.outputs = outputs
    nc.inactive = bool(s.get("inactive"))
    nc.out_stack = stack_from_json(s.get("flow_stack", []))
    if f"{folder}/view.html" in names:
        nc.view = z.read(f"{folder}/view.html").decode("utf-8")
    if f"{folder}/display.json" in names:
        nc.display = json.loads(z.read(f"{folder}/display.json").decode("utf-8"))
