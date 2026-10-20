"""The verknode API for agents: tools to find node types, build, run and read workflows.

    api = AgentAPI(LocalWorkspace("notebooks"))          # .vkf files under a folder
    await api.call("add_node", {"workflow": "report.vkf", "type": "CSV Reader", "label": "Sales",
                                "settings": {"path": "data/sales.csv"}})

Each tool takes JSON arguments and answers with text written for a model (what happened, the ids,
columns and problems to go on with) and data for programs. The same tools are served

- by MCP (mcp_server.py): over stdio, headless on .vkf files (`python -m jupyterlab_verknode.mcp_server
  --root DIR`), and over HTTP inside the Jupyter server (/verknode/mcp), live: the edits of an agent
  appear in the open editors, as those of the editor and of Python clients do;
- as function specs for any LLM API (AgentAPI.openai_tools()).

A workspace gives the workflows: LocalWorkspace loads and saves files itself (saving after each edit
unless autosave is off), LiveWorkspace (handlers.py) uses the Jupyter server's open sessions.
"""
from __future__ import annotations

import asyncio
import json
import os
import posixpath
import time

import polars as pl

from .engine.persistence import load_workflow, save_workflow
from .engine.workflow import Workflow


class ToolError(Exception):
    """A tool could not do what it was asked: the message goes back to the agent."""


# ---------------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------------
WF = {"type": "string", "description": "The workflow file (.vkf), relative to the workspace folder, "
      "e.g. \"reports/sales.vkf\""}
NODE = {"type": ["integer", "string"], "description": "A node: its id (a number), or its label"}
ANY_OBJECT = {"type": "object", "additionalProperties": True}


def _tool(name, title, description, props=None, required=(), read_only=False, destructive=False,
          idempotent=False):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)},
            "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                            "idempotentHint": idempotent or read_only, "openWorldHint": False}}


TOOLS = [
    _tool("list_workflows", "List workflows", "The workflow files (.vkf) in the workspace (or a folder of it).",
          {"folder": {"type": "string", "description": "A folder in the workspace (default: all)"}}, read_only=True),
    _tool("list_files", "List data files", "Files a workflow can read: in the workflow's folder (paths as "
          "its nodes use them) or in a folder of the workspace.",
          {"workflow": WF, "folder": {"type": "string", "description": "A sub folder"}}, read_only=True),
    _tool("find_node_types", "Find node types", "Search the about 300 node types by what they do, e.g. "
          "\"read csv\", \"group by\", \"join\", \"pivot\", \"bar chart\", \"remove duplicates\".",
          {"query": {"type": "string"}}, ["query"], read_only=True),
    _tool("node_type_help", "Node type help", "The ports and settings (names, formats, defaults, options) of "
          "node types. Read it before adding a type for the first time: settings have exact names.",
          {"types": {"type": "array", "items": {"type": "string"}, "description": "Exact node type names"}},
          ["types"], read_only=True),
    _tool("create_workflow", "Create a workflow", "Make a new, empty workflow file. It exists already: go on "
          "with it (show_workflow) - overwrite replaces it and its work.",
          {"workflow": WF, "overwrite": {"type": "boolean", "description": "Replace an existing workflow (default false)"}},
          ["workflow"], destructive=True),
    _tool("show_workflow", "Show a workflow", "The nodes of a workflow (id, type, label, settings, output "
          "columns, what is not ready and why), its connections, groups and workflow variables.",
          {"workflow": WF}, ["workflow"], read_only=True),
    _tool("add_node", "Add a node", "Add a node; returns its id, ports and (when ready) its output columns. "
          "Connect it with connect.",
          {"workflow": WF, "type": {"type": "string", "description": "Exact node type name"},
           "label": {"type": "string", "description": "A short label saying what it does here"},
           "settings": {**ANY_OBJECT, "description": "Settings that differ from the defaults (see node_type_help)"},
           "variables": {**ANY_OBJECT, "description": "Settings taken from workflow variables: {setting: variable name}"},
           "after": {**NODE, "description": "Place it to the right of this node"},
           "group": {"type": "string", "description": "A group annotation to put it in (made if new)"}},
          ["workflow", "type"]),
    _tool("configure_node", "Configure a node", "Change settings (and/or the label) of a node.",
          {"workflow": WF, "node": NODE, "settings": ANY_OBJECT, "variables": ANY_OBJECT,
           "label": {"type": "string"}}, ["workflow", "node"], idempotent=True),
    _tool("connect", "Connect nodes", "Connect an output port to an input port (ports count from 1). A connection "
          "already into that input is replaced.",
          {"workflow": WF, "from_node": NODE, "to_node": NODE, "from_port": {"type": "integer", "default": 1},
           "to_port": {"type": "integer", "default": 1}}, ["workflow", "from_node", "to_node"], idempotent=True),
    _tool("disconnect", "Disconnect nodes", "Remove a connection.",
          {"workflow": WF, "from_node": NODE, "to_node": NODE, "from_port": {"type": "integer", "default": 1},
           "to_port": {"type": "integer", "default": 1}}, ["workflow", "from_node", "to_node"], destructive=True),
    _tool("remove_node", "Remove a node", "Remove a node with its connections (undo brings it back).",
          {"workflow": WF, "node": NODE}, ["workflow", "node"], destructive=True),
    _tool("set_variables", "Set workflow variables", "Set (or add) workflow variables, which settings can be "
          "taken from (add_node / configure_node variables): {name: value}; types follow the values.",
          {"workflow": WF, "variables": ANY_OBJECT}, ["workflow", "variables"], idempotent=True),
    _tool("run", "Run", "Run the workflow (or some nodes, with what they need) and report each node: done, "
          "failed (why), rows, time. sandbox: run a copy in a file sandbox - files are read, nothing is written; "
          "the workflow itself does not change.",
          {"workflow": WF, "nodes": {"type": "array", "items": NODE, "description": "Default: all"},
           "rerun": {"type": "boolean", "description": "Run nodes again that have run already"},
           "sandbox": {"type": "boolean"}, "timeout": {"type": "number", "description": "Seconds (default 600)"}},
          ["workflow"]),
    _tool("get_table", "Get a table", "Rows of a node's output table (it runs first when needed).",
          {"workflow": WF, "node": NODE, "port": {"type": "integer", "default": 1},
           "offset": {"type": "integer", "default": 0}, "rows": {"type": "integer", "default": 20},
           "columns": {"type": "array", "items": {"type": "string"}},
           "format": {"type": "string", "enum": ["markdown", "json", "csv"], "default": "markdown"}},
          ["workflow", "node"], read_only=True),
    _tool("run_workflow", "Run a workflow as a tool", "Run a finished workflow on a private copy with the given "
          "workflow variables and return its result tables - a workflow as a reusable, reviewed tool. Nothing "
          "in the workflow changes.",
          {"workflow": WF, "variables": ANY_OBJECT,
           "outputs": {"type": "array", "items": NODE, "description": "The result nodes (default: the last table "
                       "of each branch)"},
           "rows": {"type": "integer", "default": 50}, "sandbox": {"type": "boolean"},
           "timeout": {"type": "number"}}, ["workflow"], read_only=True),
    _tool("export_workflow", "Export a workflow to run without Jupyter",
          "Export the workflow (or a group of it) to run anywhere. kind \"folder\" (default): a self-contained "
          "folder - the script, the parts of verknode it uses and a requirements.txt of only what it needs (measured "
          "by running it), its data files, optionally a Dockerfile and an HTTP API server (server.py). kind "
          "\"script\": one .py file (needs the jupyterlab-verknode package). headless: drop widgets, views and what "
          "no result needs; input widgets become workflow variables (--var parameters).",
          {"workflow": WF, "kind": {"type": "string", "enum": ["folder", "script"]},
           "path": {"type": "string", "description": "Where (default: <name>_export/ or <name>.py next to the workflow)"},
           "group": {"type": "string", "description": "Export only this group annotation"},
           "nodes": {"type": "array", "items": NODE, "description": "Export only these nodes"},
           "upstream": {"type": "string", "enum": ["include", "snapshot"], "description": "With group / nodes: the "
                        "nodes before them come along (include), or the data coming in is saved (snapshot, folder)"},
           "headless": {"type": "boolean"}, "data": {"type": "boolean", "description": "Copy the files it reads (folder; default true)"},
           "docker": {"type": "boolean"}, "api": {"type": "boolean"}, "overwrite": {"type": "boolean"}},
          ["workflow"]),
    _tool("save_workflow", "Save a workflow", "Save the workflow (to its file, or to another path).",
          {"workflow": WF, "path": {"type": "string", "description": "Save as (default: its own file)"}},
          ["workflow"], idempotent=True),
    _tool("undo", "Undo", "Undo the last edit of the workflow.", {"workflow": WF}, ["workflow"]),
]
SPECIAL = {"create_workflow", "save_workflow", "run_workflow", "export_workflow"}  # (async: they open or save files)
NEEDS_WORKFLOW = {t["name"] for t in TOOLS if "workflow" in t["inputSchema"]["required"]}
EDITS = {"add_node", "configure_node", "connect", "disconnect", "remove_node", "set_variables", "undo"}

INSTRUCTIONS = """verknode builds and runs visual data workflows (like KNIME): nodes - readers, filters, \
joins, group-bys, pivots, formulas, charts, models, ~300 types - connected port to port; tables flow along \
the connections. Build a workflow when the user wants a data pipeline they can see, check, rerun and change \
(it opens in JupyterLab's workflow editor).

How to work: find_node_types -> node_type_help (settings have exact names and formats) -> create_workflow -> \
add_node / connect (each result shows the node's output columns or what is wrong) -> run -> get_table to check \
the result -> save_workflow. A node that is wrong: fix it (configure_node, reading the error), or remove_node it - \
leave no failing or unconnected nodes behind; when the same fix fails twice, look at node_type_help again. File paths in settings are relative to the workflow's folder (list_files). \
Workflow variables (set_variables, and settings taken from them) make a workflow a reusable tool: \
run_workflow runs it with other values without changing it."""


# ---------------------------------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------------------------------
class LocalWorkspace:
    """Workflow files under a folder, loaded and saved here (no Jupyter server needed)."""

    live = False

    def __init__(self, root: str, autosave: bool = True):
        import jupyterlab_verknode.nodes  # noqa: F401 - registers the node types

        self.root = os.path.abspath(root)
        self.autosave = autosave
        self.open_workflows: dict[str, Workflow] = {}
        self.mtimes: dict[str, float] = {}

    def rel(self, path: str, vkf: bool = True) -> str:
        key = posixpath.normpath(str(path or "").replace("\\", "/").lstrip("/"))
        if key.startswith("..") or key == ".." or os.path.isabs(key):
            raise ToolError(f"{path!r} is outside the workspace")
        if vkf and not key.endswith(".vkf"):
            raise ToolError(f"{path!r} is not a workflow file (.vkf)")
        return key

    def abs(self, key: str) -> str:
        return os.path.join(self.root, key)

    def exists(self, path: str) -> bool:
        return os.path.isfile(self.abs(self.rel(path)))

    async def open(self, path: str, create: bool = False) -> Workflow:
        key = self.rel(path)
        full = self.abs(key)
        wf = self.open_workflows.get(key)
        mtime = os.path.getmtime(full) if os.path.isfile(full) else None
        if wf is not None and (mtime is None or mtime == self.mtimes.get(key) or wf.dirty or wf.executing):
            return wf
        if mtime is not None:
            with open(full, "rb") as f:
                wf = load_workflow(f.read(), os.path.dirname(full))
        elif create:
            wf = Workflow()
            wf.base_dir = os.path.dirname(full)
        else:
            raise ToolError(f"There is no workflow {key} (create_workflow makes one)")
        wf.path, wf.rel_path = full, key
        self.open_workflows[key], self.mtimes[key] = wf, mtime
        return wf

    async def save(self, path: str, to: str | None = None) -> str:
        key = self.rel(path)
        wf = await self.open(key)
        dest = self.rel(to) if to else key
        full = self.abs(dest)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        data = save_workflow(wf)
        with open(full, "wb") as f:
            f.write(data)
        wf.dirty = False
        if dest != key:
            wf.path, wf.rel_path, wf.base_dir = full, dest, os.path.dirname(full)
            self.open_workflows.pop(key, None)
        self.open_workflows[dest], self.mtimes[dest] = wf, os.path.getmtime(full)
        return dest

    def link(self, key: str) -> str | None:
        return None


# ---------------------------------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------------------------------
def _short(v, n=300):
    text = json.dumps(v, default=str)
    return text if len(text) <= n else text[:n] + "…"


class AgentAPI:
    def __init__(self, workspace):
        self.ws = workspace

    # -- descriptions ---------------------------------------------------------------------------
    @staticmethod
    def tools() -> list[dict]:
        return TOOLS

    @staticmethod
    def openai_tools() -> list[dict]:
        """The tools as function specs for chat APIs (OpenAI-compatible, Ollama)."""
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["inputSchema"]}} for t in TOOLS]

    # -- calls ----------------------------------------------------------------------------------
    async def call(self, name: str, args: dict | None) -> dict:
        """Run a tool: {"text": for the model, "data": for programs, "error": bool}."""
        args = dict(args or {})
        fn = getattr(self, f"_t_{name}", None)
        if name not in {t["name"] for t in TOOLS} or (fn is None and name not in SPECIAL):
            return {"text": f"There is no tool {name!r}; the tools are {', '.join(t['name'] for t in TOOLS)}",
                    "data": None, "error": True}
        if name == "save_workflow" and not args.get("workflow") and args.get("path"):
            args["workflow"] = args.pop("path")  # (save it)
        try:
            if name == "create_workflow":
                return await self._create(**args)
            wf = None
            if args.get("workflow") or name in NEEDS_WORKFLOW:
                if not args.get("workflow"):
                    raise ToolError(f"{name} needs the workflow (a .vkf path)")
                wf = await self.ws.open(args.pop("workflow"))
            args.pop("workflow", None)
            if name == "save_workflow":
                dest = await self.ws.save(wf.rel_path, args.get("path"))
                link = self.ws.link(dest)
                return {"text": f"Saved {dest}." + (f" Open it: {link}" if link else ""), "data": {"path": dest},
                        "error": False}
            if name == "run_workflow":
                return await self._run_workflow(wf, **args)
            if name == "export_workflow":
                return await self._export(wf, **args)
            loop = asyncio.get_running_loop()
            text, data = await loop.run_in_executor(None, lambda: fn(wf, **args) if wf is not None else fn(**args))
            if name in EDITS and self.ws.autosave and wf is not None:
                await self.ws.save(wf.rel_path)
            return {"text": text, "data": data, "error": False}
        except TypeError as e:
            return {"text": f"Wrong arguments for {name}: {e}", "data": None, "error": True}
        except ToolError as e:
            return {"text": str(e), "data": None, "error": True}
        except Exception as e:  # noqa: BLE001 - every failure goes back to the agent
            return {"text": f"{type(e).__name__}: {e}", "data": None, "error": True}

    # -- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _node(wf, ref) -> int:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().lstrip("#").isdigit()):
            nid = int(str(ref).strip().lstrip("#"))
            if nid in wf.nodes:
                return nid
            raise ToolError(f"There is no node #{nid} (show_workflow lists them)")
        hits = [n for n, nc in wf.nodes.items() if nc.annotation.strip().lower() == str(ref).strip().lower()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ToolError(f"Several nodes are labelled {ref!r} ({', '.join(f'#{n}' for n in hits)}): use the id")
        raise ToolError(f"There is no node {ref!r} (show_workflow lists them)")

    @staticmethod
    def _edit(wf, group=None):
        from .nodes.agent import Edit

        gid = next((g["id"] for g in wf.groups if group and g["text"].strip().lower() == group.strip().lower()), None)
        return Edit(wf, set(wf.nodes), gid, -1, (), group or None)

    def _bind(self, wf, nid, variables):
        """Settings taken from workflow variables: {setting: variable name}."""
        if not variables:
            return
        if not isinstance(variables, dict):
            raise ToolError("variables must be an object {setting: variable name}")
        nc = wf.nodes[nid]
        keys = {f.key for f in nc.model.fields}
        names = {v.name for v in wf.workflow_variables}
        bad = [k for k in variables if k not in keys]
        if bad:
            raise ToolError(f"{nc.name} has no setting(s) {bad}")
        missing = [v for v in variables.values() if v not in names]
        if missing:
            raise ToolError(f"There is no workflow variable {missing} (set_variables makes one)")
        vs = dict(nc.var_settings)
        vs.update({k: {"used": v, "exposed": None} for k, v in variables.items()})
        wf.set_settings(nid, {}, vs)

    PATH_KEYS = ("path", "file", "filename", "file_path", "folder", "directory")

    def _path_hints(self, wf, nid) -> str:
        """Settings with file paths that are not there: where such a file is (relative to the workflow)."""
        nc = wf.nodes[nid]
        hints = []
        for f in nc.model.fields:
            v = nc.settings.get(f.key)
            if not (isinstance(v, str) and v.strip() and (f.key in self.PATH_KEYS or f.key.endswith(("_path", "_file")))):
                continue
            if "://" in v or os.path.exists(os.path.join(wf.base_dir or "", v)):
                continue
            name = os.path.basename(v.rstrip("/"))
            found = []
            for d, dirs, files in os.walk(self.ws.root):
                dirs[:] = [x for x in dirs if not x.startswith((".", "__")) and x != "node_modules"]
                if name in files or name in dirs:
                    found.append(os.path.relpath(os.path.join(d, name), wf.base_dir))
                if len(found) >= 3 or d.count(os.sep) - self.ws.root.count(os.sep) > 6:
                    break
            hints.append(f"\nNOTE: {f.key} {v!r} does not exist relative to the workflow's folder "
                         f"({os.path.relpath(wf.base_dir, self.ws.root)}/)"
                         + (f" - did you mean {' or '.join(repr(x) for x in found)}?" if found else "."))
        return "".join(hints)

    def _status(self, wf, nid) -> str:
        from .nodes.agent import node_line

        out = node_line(wf, nid) + self._path_hints(wf, nid)
        for c in wf.connections:
            if c.source_id == nid:
                out += "\nNext: " + node_line(wf, c.dest_id, settings=False)
        return out

    async def _create(self, workflow, overwrite=False):
        key = self.ws.rel(workflow)
        if self.ws.exists(key) and not overwrite:
            raise ToolError(f"{key} exists already: go on with it (show_workflow shows it), or overwrite: true "
                            "replaces it")
        if overwrite and self.ws.exists(key) and not self.ws.live:
            os.remove(self.ws.abs(key))  # (a new workflow: ids from 1 again)
            self.ws.open_workflows.pop(key, None)
        wf = await self.ws.open(key, create=True)
        if overwrite and wf.nodes:  # (live: the open session is emptied - one undo step)
            with wf.batch():
                wf.clear()
                wf.set_workflow_variables([])
                wf.next_id = 1  # (a new workflow: ids from 1 again; undo brings the old ones back)
        await self.ws.save(key)
        link = self.ws.link(key)
        return {"text": f"Created {key} (empty)." + (f" The user can watch it: {link}" if link else ""),
                "data": {"path": key, "link": link}, "error": False}

    # -- read --------------------------------------------------------------------------------------
    def _t_list_workflows(self, folder=""):
        base = self.ws.abs(self.ws.rel(folder, vkf=False)) if folder else self.ws.root
        out = []
        for d, dirs, files in os.walk(base):
            dirs[:] = sorted(x for x in dirs if not x.startswith((".", "__")) and x != "node_modules")
            for f in sorted(files):
                if f.endswith(".vkf"):
                    p = os.path.join(d, f)
                    out.append({"path": os.path.relpath(p, self.ws.root), "bytes": os.path.getsize(p),
                                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))})
            if len(out) >= 300:
                break
        text = "\n".join(f"- {w['path']} ({w['modified']})" for w in out) or "No workflows."
        return text, {"workflows": out}

    def _t_list_files(self, wf=None, folder=""):
        if wf is not None:
            base = wf.base_dir
            root = os.path.abspath(os.path.join(base, folder or ""))
            if not root.startswith(os.path.abspath(self.ws.root)):
                raise ToolError(f"{folder!r} is outside the workspace")
        else:
            base = self.ws.root
            root = self.ws.abs(self.ws.rel(folder, vkf=False)) if folder else base
        if not os.path.isdir(root):
            raise ToolError(f"There is no folder {folder!r}")
        out = []
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith((".", "__")))
            for f in sorted(files):
                if not f.startswith(".") and not f.endswith(".vkf"):
                    p = os.path.join(d, f)
                    out.append({"path": os.path.relpath(p, base), "bytes": os.path.getsize(p)})
            if len(out) >= 200:
                break
        where = "relative to the workflow's folder, as settings take them" if wf is not None else \
            "relative to the workspace; settings take paths relative to the workflow's folder - list_files with " \
            "the workflow shows them so"
        return (f"Files ({where}):\n" + "\n".join(f"- {f['path']} ({f['bytes']:,} bytes)" for f in out)
                if out else "No files."), {"files": out}

    def _t_find_node_types(self, query=""):
        from .nodes.agent import search_types

        return search_types(Workflow(), query, limit=15), None

    def _t_node_type_help(self, types=()):
        from .nodes.agent import FORMATS, type_specs

        if isinstance(types, str):
            types = [types]
        wf = Workflow()
        known = {t["name"].lower(): t["name"] for t in wf.node_types()}
        found = [known[str(t).lower()] for t in types if str(t).lower() in known]
        unknown = [t for t in types if str(t).lower() not in known]
        text = type_specs(wf, found)
        if unknown:
            import difflib

            text += ("\n\n" if text else "") + "; ".join(
                f"no node type {u!r}" + (f" (did you mean {', '.join(c)}?)" if (c := difflib.get_close_matches(
                    str(u), list(known.values()), n=3, cutoff=0.4)) else "") for u in unknown)
        return (text + ("\n\n" + FORMATS if found else "")), None

    def _t_show_workflow(self, wf):
        from .nodes.agent import describe

        text = describe(wf, set(wf.nodes), -1).replace("Nodes you may change:", "Nodes:").replace("  * ", "  ")
        if wf.groups:
            text += "\nGroups:\n" + "\n".join(f"  \"{g['text']}\": {', '.join(f'#{n}' for n in g['nodes'])}" for g in wf.groups)
        if wf.workflow_variables:
            text += "\nWorkflow variables:\n" + "\n".join(f"  {v.name} ({v.type}) = {v.value!r}" for v in wf.workflow_variables)
        if not wf.nodes:
            text = "The workflow is empty."
        nodes = [{"id": n, "type": nc.name, "label": nc.annotation, "executed": nc.executed} for n, nc in wf.nodes.items()]
        return text, {"nodes": nodes, "connections": [list(c.key()) for c in wf.connections]}

    # -- edit --------------------------------------------------------------------------------------
    def _t_add_node(self, wf, type="", label="", settings=None, variables=None, after=None, group=None):
        ed = self._edit(wf, group)
        nid = ed.one({"op": "add", "type": type, "label": label, "settings": settings or {},
                      "after": self._node(wf, after) if after not in (None, "") else None})
        try:
            self._bind(wf, nid, variables)
        except ToolError:
            wf.remove_nodes([nid])
            raise
        return f"Added #{nid}. {self._status(wf, nid)}", {"id": nid}

    def _t_configure_node(self, wf, node=None, settings=None, variables=None, label=None):
        nid = self._node(wf, node)
        ed = self._edit(wf)
        ed.one({"op": "configure", "node": nid, "settings": settings or {}, "label": label})
        self._bind(wf, nid, variables)
        return f"Configured #{nid}. {self._status(wf, nid)}", {"id": nid}

    def _t_connect(self, wf, from_node=None, to_node=None, from_port=1, to_port=1):
        src, dst = self._node(wf, from_node), self._node(wf, to_node)
        self._edit(wf).one({"op": "connect", "from": src, "from_port": from_port, "to": dst, "to_port": to_port})
        return f"Connected #{src}:{from_port or 1} -> #{dst}:{to_port or 1}. {self._status(wf, dst)}", None

    def _t_disconnect(self, wf, from_node=None, to_node=None, from_port=1, to_port=1):
        src, dst = self._node(wf, from_node), self._node(wf, to_node)
        self._edit(wf).one({"op": "disconnect", "from": src, "from_port": from_port, "to": dst, "to_port": to_port})
        return f"Disconnected #{src}:{from_port or 1} -> #{dst}:{to_port or 1}.", None

    def _t_remove_node(self, wf, node=None):
        nid = self._node(wf, node)
        name = f"#{nid} {wf.nodes[nid].name} \"{wf.nodes[nid].annotation}\""
        wf.remove_nodes([nid])
        return f"Removed {name} (undo brings it back).", None

    def _t_set_variables(self, wf, variables=None):
        if not isinstance(variables, dict) or not variables:
            raise ToolError("variables must be an object {name: value}")
        items = {v.name: v.to_json() for v in wf.workflow_variables}
        for name, value in variables.items():
            items[str(name)] = {"name": str(name), "type": _var_type(value), "value": value}
        wf.set_workflow_variables(list(items.values()))
        return "Workflow variables: " + ", ".join(f"{v.name} ({v.type}) = {v.value!r}" for v in wf.workflow_variables), \
            {"variables": [v.to_json() for v in wf.workflow_variables]}

    def _t_undo(self, wf):
        wf.undo()
        return "Undone. " + self._t_show_workflow(wf)[0], None

    # -- run ---------------------------------------------------------------------------------------
    @staticmethod
    def _wait(wf, timeout):
        t0 = time.time()
        time.sleep(0.05)
        while wf.executing:
            if time.time() - t0 > timeout:
                return False
            time.sleep(0.1)
        wf.wait()
        return True

    def _report(self, wf, ids) -> tuple[str, list, list]:
        from .nodes.agent import why_not_run

        rows, problems = [], []
        for nid in wf.topo_order(set(ids)):
            nc = wf.nodes[nid]
            msg = nc.message
            state = "done" if nc.executed else "failed" if msg and msg[0] == "ERROR" else \
                "skipped" if msg and "sandbox" in msg[1] else "not run"
            out_rows = next((o.height for o in (nc.outputs or [])[1:] if isinstance(o, pl.DataFrame)), None)
            rows.append({"id": nid, "type": nc.name, "label": nc.annotation, "state": state, "rows": out_rows,
                         "seconds": round(nc.exec_time, 3) if nc.exec_time is not None else None,
                         "message": msg[1] if msg else None})
            if state == "failed":
                problems.append(f"#{nid} {nc.name} \"{nc.annotation}\" failed: {msg[1]}")
            elif state == "not run":
                problems.append(f"#{nid} {nc.name} \"{nc.annotation}\" did not run: "
                                f"{msg[1] if msg else why_not_run(wf, nid)}")
            elif msg and msg[0] == "WARNING":
                problems.append(f"#{nid} {nc.name} \"{nc.annotation}\" warns: {msg[1]}")
        text = "\n".join(f"#{r['id']} {r['type']} \"{r['label']}\": {r['state']}"
                         + (f", {r['rows']} rows" if r["rows"] is not None else "")
                         + (f", {r['seconds']} s" if r["seconds"] is not None else "") for r in rows)
        return text, rows, problems

    def _targets(self, wf, nodes):
        if not nodes:
            return list(wf.nodes)
        ids = [self._node(wf, n) for n in nodes]
        return sorted(set(ids) | set().union(*(wf.ancestors(i) for i in ids)))

    def _t_run(self, wf, nodes=None, rerun=False, sandbox=False, timeout=600):
        from .nodes.agent import clone, run_sandboxed

        ids = self._targets(wf, nodes)
        if not ids:
            return "The workflow is empty.", None
        if sandbox:
            copy = clone(wf)
            report, problems, files = run_sandboxed(copy, ids, {"sandbox": True})
            text, rows, problems = self._report(copy, ids)
            verb = {"new": "write", "modified": "overwrite", "deleted": "delete"}
            changes = "".join(f"\nRun for real, it would {verb.get(f['change'], f['change'])} "
                              f"{os.path.relpath(f['path'], wf.base_dir)}" for f in files)
            return (f"Ran a copy in a sandbox (the workflow is unchanged):\n{text}{changes}"
                    + ("\nProblems:\n- " + "\n- ".join(problems) if problems else "\nNo problems.")), \
                {"nodes": rows, "problems": problems, "files": files}
        if rerun:
            wf.reset(ids)
        t0 = time.time()
        wf.execute(ids)
        finished = self._wait(wf, float(timeout or 600))
        text, rows, problems = self._report(wf, ids)
        head = f"Ran in {time.time() - t0:.1f} s:" if finished else \
            f"Still running after {timeout} s (run again to wait, or the editor's Cancel):"
        return (head + "\n" + text + ("\nProblems:\n- " + "\n- ".join(problems) if problems else "\nNo problems.")), \
            {"nodes": rows, "problems": problems, "finished": finished}

    def _t_get_table(self, wf, node=None, port=1, offset=0, rows=20, columns=None, format="markdown"):
        nid = self._node(wf, node)
        nc = wf.nodes[nid]
        if not nc.executed:
            self._t_run(wf, [nid])
        if not nc.executed:
            from .nodes.agent import why_not_run

            raise ToolError(f"#{nid} did not run: {nc.message[1] if nc.message else why_not_run(wf, nid)}")
        outs = nc.outputs or []
        port = int(port or 1)
        if port < 1 or port >= len(outs):
            raise ToolError(f"#{nid} has {len(outs) - 1} output port(s)")
        o = outs[port]
        if not isinstance(o, pl.DataFrame):
            summary = getattr(o, "summary", lambda: {})()
            return f"#{nid} output {port} is not a table: {type(o).__name__} {_short(summary, 500)}", {"summary": summary}
        df = o
        if columns:
            bad = [c for c in columns if c not in df.columns]
            if bad:
                raise ToolError(f"No column(s) {bad}; the columns are {df.columns}")
            df = df.select(columns)
        part = df.slice(max(0, int(offset or 0)), max(1, min(int(rows or 20), 500)))
        schema = ", ".join(f"{c}:{t}" for c, t in df.schema.items())
        head = f"#{nid} \"{nc.annotation}\" output {port}: {df.height} rows; columns {schema}\n"
        if format == "json":
            body = json.dumps(part.to_dicts(), default=str)
        elif format == "csv":
            body = part.write_csv()
        else:
            from .nodes.agent import markdown_table

            body = markdown_table(part, part.height, cols=40)
        more = f"\n(rows {offset}-{offset + part.height - 1} of {df.height})" if df.height > part.height else ""
        return head + body + more, {"rows": part.to_dicts(), "total": df.height, "columns": df.columns}

    async def _export(self, wf, kind="folder", path=None, group=None, nodes=None, upstream="include",
                      headless=False, data=True, docker=False, api=False, overwrite=False):
        from .export import ExportError, export_bundle, format_report, prepare
        from .standalone import to_standalone

        stem = os.path.splitext(os.path.basename(wf.rel_path))[0]
        folder = posixpath.dirname(wf.rel_path)
        loop = asyncio.get_running_loop()
        try:
            if kind == "script":
                if upstream == "snapshot":
                    raise ToolError("upstream \"snapshot\" saves data: export a folder")
                dest = self.ws.rel(path or posixpath.join(folder, stem + ".py"), vkf=False)
                if not dest.endswith(".py"):
                    raise ToolError("a script path ends with .py")
                full = self.ws.abs(dest)
                if os.path.exists(full) and not overwrite:
                    raise ToolError(f"{dest} exists (overwrite replaces it)")
                e, notes, problems = await loop.run_in_executor(None, lambda: prepare(wf, group, nodes, "include", headless))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as f:
                    f.write(to_standalone(e, wf.rel_path, notes=notes))
                text = (f"Wrote {dest}: python {posixpath.basename(dest)} [--var NAME=VALUE] [--out DIR] - it needs the "
                        "jupyterlab-verknode package (kind \"folder\" needs only what the workflow uses)."
                        + "".join(f"\n- {n}" for n in notes) + "".join(f"\nPROBLEM: {p}" for p in problems))
                return {"text": text, "data": {"path": dest, "notes": notes, "problems": problems}, "error": False}
            dest = self.ws.rel(path or posixpath.join(folder, stem + "_export"), vkf=False)
            report = await loop.run_in_executor(None, lambda: export_bundle(
                wf, self.ws.abs(dest), stem, group, nodes, upstream, headless, data, docker, api, True, overwrite))
        except ExportError as e:
            raise ToolError(str(e)) from None
        report["folder"] = dest
        return {"text": format_report(report), "data": report, "error": bool(report["check"] and not report["check"]["ok"])}

    async def _run_workflow(self, wf, variables=None, outputs=None, rows=50, sandbox=False, timeout=600):
        from .engine.sandbox import Sandbox
        from .nodes.agent import clone

        copy = clone(wf)  # (private: the workflow, open or not, does not change)
        if variables:
            names = {v.name for v in copy.workflow_variables}
            unknown = [k for k in variables if k not in names]
            if unknown:
                raise ToolError(f"{wf.rel_path} has no workflow variable(s) {unknown}; it has "
                                f"{sorted(names) or 'none'}")
            self._t_set_variables(copy, variables)
        if outputs:
            outs = [self._node(copy, o) for o in outputs]
        else:  # (the last table of each branch)
            outs = [n for n, nc in copy.nodes.items() if not any(c.source_id == n for c in copy.connections)
                    and any(p.type == "table" for p in nc.model.out_ports)]
        if not outs:
            raise ToolError(f"{wf.rel_path} has no result table (give outputs)")

        def run():
            box = Sandbox() if sandbox else None
            copy.sandbox = box
            try:
                text, data = self._t_run(copy, outs, rerun=bool(sandbox), timeout=timeout)
                files = box.changes() if box is not None else []
            finally:
                if box is not None:
                    box.cleanup()
                copy.sandbox = None
            tables = []
            for n in outs:
                if copy.nodes[n].executed:
                    t, d = self._t_get_table(copy, n, rows=rows)
                    tables.append({"id": n, "label": copy.nodes[n].annotation, **(d or {})})
                    text += "\n\n" + t
            return text, {**data, "tables": tables, "files": files}

        text, data = await asyncio.get_running_loop().run_in_executor(None, run)
        return {"text": text, "data": data, "error": not data["tables"]}


def _var_type(value) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "DOUBLE"
    return "STRING"
