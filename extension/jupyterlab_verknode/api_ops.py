"""The editor's operations on a workflow, shared by the Jupyter server's REST API (handlers.py) and
the browser's (lite.py, JupyterLite): no web framework here."""
from __future__ import annotations

import os
import posixpath

from .engine.workflow import Workflow, WorkflowError


OPS = {
    "add_node": lambda wf, a: wf.add_node(a["factory"], a.get("x", 0), a.get("y", 0), a.get("parent")),
    "remove_nodes": lambda wf, a: wf.remove_nodes(a["ids"]),
    "connect": lambda wf, a: wf.connect(a["sourceID"], a["sourcePort"], a["destID"], a["destPort"]),
    "disconnect": lambda wf, a: wf.disconnect(a["sourceID"], a["sourcePort"], a["destID"], a["destPort"]),
    "move": lambda wf, a: wf.move(a["positions"], a.get("layouts")),
    "rewire": lambda wf, a: wf.rewire(a.get("remove", []), a.get("add", []), a.get("positions"), a.get("delete", [])),
    "set_annotation": lambda wf, a: wf.set_annotation(a["id"], a["text"]),
    "set_widget_layouts": lambda wf, a: wf.set_widget_layouts(a["layouts"]),
    "add_group": lambda wf, a: wf.add_group(a["nodes"], a.get("text", ""), a.get("color")),
    "update_group": lambda wf, a: wf.update_group(a["id"], a.get("text"), a.get("color"), a.get("nodes")),
    "remove_group": lambda wf, a: wf.remove_group(a["id"]),
    "set_settings": lambda wf, a: wf.set_settings(a["id"], a.get("settings", {}), a.get("variables", {})),
    "set_workflow_variables": lambda wf, a: wf.set_workflow_variables(a["variables"]),
    "reset": lambda wf, a: wf.reset(a.get("ids")),
    "execute": lambda wf, a: wf.execute(a.get("ids")),
    "cancel": lambda wf, a: wf.cancel(),
    "undo": lambda wf, a: wf.undo(),
    "redo": lambda wf, a: wf.redo(),
    "clear": lambda wf, a: wf.clear(),
    "copy": lambda wf, a: wf.copy(a["ids"]),
    "paste": lambda wf, a: wf.paste(a["fragment"], a.get("dx", 40), a.get("dy", 40), a.get("parent")),
    "collapse": lambda wf, a: wf.collapse(a["ids"], a.get("name")),
    "expand": lambda wf, a: wf.expand(a["id"]),
    "rename_metanode": lambda wf, a: wf.rename_metanode(a["id"], a["name"]),
    "add_metanode_port": lambda wf, a: wf.add_metanode_port(a["id"], a["side"], a.get("type", "table")),
    "remove_metanode_port": lambda wf, a: wf.remove_metanode_port(a["id"], a["side"], a["index"]),
    "set_node_paths": lambda wf, a: wf.set_node_paths(a["paths"], a.get("autoReload")),
    "reload_nodes": lambda wf, a: wf.reload_nodes(),
    "create_node_template": lambda wf, a: _create_node_template(wf, a.get("folder") or "~"),
    "deliver": lambda wf, a: wf.deliver(a["id"], a.get("value")),
    "begin_batch": lambda wf, a: wf.begin_batch(),
    "end_batch": lambda wf, a: wf.end_batch(),
}


def _create_node_template(wf: Workflow, folder: str) -> dict:
    """Create `folder` (relative to the workflow) with an example node file and use it - or, for "~" (the
    default), an example in the user's node folder (~/.verknode/nodes), which every workflow loads."""
    from .engine.custom import create_template
    from .engine.home import home

    if folder in ("", "~", "user"):
        path = create_template(os.path.join(home(), "nodes"))
        wf.reload_nodes(reason="example created")
        return {"file": path, "report": wf.custom.report()}
    if os.path.isabs(folder) or ".." in folder.split("/"):
        raise WorkflowError("Use a folder inside the workflow's directory, e.g. 'vkn_nodes'")
    path = create_template(os.path.join(wf.base_dir, folder))
    if folder not in wf.node_paths:
        wf.set_node_paths(wf.node_paths + [folder])
    else:
        wf.reload_nodes(reason="example created")
    return {"file": path, "report": wf.custom.report()}
# how results are returned to the client
RESULTS = {"add_node": "id", "add_group": "id", "collapse": "id", "add_metanode_port": "index", "paste": "ids", "copy": "fragment", "reload_nodes": "report",
           "set_node_paths": "report", "create_node_template": "template"}


def _export_options(body: dict) -> dict:
    return {"group": body.get("group") or None, "nodes": body.get("nodes") or None,
            "headless": bool(body.get("headless")), "upstream": "snapshot" if body.get("snapshot") else "include"}


def _script_path(rel: str | None) -> str:
    return posixpath.splitext(rel or "workflow.vkf")[0] + ".py"
