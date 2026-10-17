"""Build and run verknode workflows (.vkf) from Python, live.

A workflow opened with :func:`open` is the same live workflow that is shown in
an open JupyterLab workflow editor: every change made here appears in the
editor immediately, and vice versa. The editor's console shows the Python
command for every edit made in the editor, so you can learn the API by using
the editor::

    import verknode
    wf = verknode.open("notebooks/examples/demo.vkf")

    reader = wf.add_node("CSV Reader", path="data/iris.csv")
    stats = wf.add_node("Statistics")
    wf.connect(reader, stats)
    wf.execute()
    stats.table()                 # output table as a polars DataFrame

    wf.add_group(reader, stats, text="Load data")   # group annotation (bubble)
    meta = wf.collapse(reader, stats, name="Load")  # metanode; meta.expand() undoes it

    verknode.nodes("loop")            # find node types
    stats.help()                  # settings of a node
"""
from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["open", "nodes", "connect", "notebook_inputs", "Workflow", "Node", "Metanode", "Group", "VerknodeError"]


class VerknodeError(RuntimeError):
    """Error reported by the workflow engine."""


# ----------------------------------------------------------------------
# server connection
# ----------------------------------------------------------------------
class _Server:
    def __init__(self, url: str, token: str, root_dir: str | None):
        self.url = url.rstrip("/") + "/"
        self.token = token
        self.root_dir = root_dir

    def request(self, method: str, path: str, body=None, raw: bool = False, timeout: float = 120):
        url = urllib.parse.urljoin(self.url, "verknode/api/" + path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                text = json.loads(text).get("message") or text
            except ValueError:
                pass
            raise VerknodeError(text) from None
        if raw:
            return payload
        return json.loads(payload) if payload else None


_server: _Server | None = None


def connect(url: str | None = None, token: str | None = None) -> None:
    """Choose the Jupyter server to talk to.

    Defaults: $VERKNODE_URL / $VERKNODE_TOKEN, otherwise the running Jupyter server
    of this kernel (found via jupyter_server's runtime files).
    """
    global _server
    url = url or os.environ.get("VERKNODE_URL")
    token = token if token is not None else os.environ.get("VERKNODE_TOKEN")
    root_dir = None
    if not url:
        from jupyter_server.serverapp import list_running_servers

        servers = list(list_running_servers())
        if not servers:
            raise VerknodeError("No running Jupyter server found; call verknode.connect(url, token)")
        info = servers[0]
        url = info["url"]
        token = token if token is not None else info.get("token", "")
        root_dir = info.get("root_dir") or info.get("notebook_dir")
    _server = _Server(url, token or os.environ.get("JUPYTER_TOKEN", ""), root_dir)


def _srv() -> _Server:
    if _server is None:
        connect()
    return _server


def _root_relative(path: str) -> str:
    """Path relative to the Jupyter root for a user-given path.

    Existing files are found relative to the current directory first (as in
    a notebook), then relative to the Jupyter root (the form shown by the
    editor); new files are created relative to the current directory.
    """
    root = _srv().root_dir
    if not root:
        return path.lstrip("/")
    root = os.path.abspath(root)
    inside = lambda p: p == root or p.startswith(root + os.sep)  # noqa: E731
    local = os.path.abspath(path)
    rooted = os.path.join(root, path.lstrip("/"))
    if os.path.isabs(path) or os.path.exists(local):
        candidate = local
    elif os.path.exists(rooted):
        candidate = rooted
    else:
        candidate = local if inside(local) else rooted
    if not inside(candidate):
        raise VerknodeError(f"{path!r} is outside the Jupyter root {root}")
    return os.path.relpath(candidate, root).replace(os.sep, "/")


# ----------------------------------------------------------------------
# node types
# ----------------------------------------------------------------------
_types_cache: list | None = None


def _types() -> list:
    global _types_cache
    if _types_cache is None:
        _types_cache = _srv().request("GET", "nodes")
    return _types_cache


def _resolve_type(name: str, types: list | None = None) -> dict:
    types = types if types is not None else _types()
    for t in types:
        if t["factory"] == name or t["name"] == name:
            return t
    low = name.lower()
    matches = [t for t in types if t["name"].lower() == low]
    if len(matches) == 1:
        return matches[0]
    close = [t["name"] for t in types if low in t["name"].lower()][:10]
    raise VerknodeError(f"Unknown node type {name!r}" + (f"; did you mean: {', '.join(close)}" if close else ""))


class NodeType(dict):
    def __repr__(self):
        return f"<{self['name']} ({self['category'].replace('/', ' › ')})>"


def nodes(search: str | None = None) -> list:
    """Available node types, optionally filtered by a search string (name or category)."""
    low = (search or "").lower()
    return [NodeType(t) for t in _types()
            if not low or low in t["name"].lower() or low in t["category"].lower()]


#: set by the workflow while it runs a notebook in its open JupyterLab tab
_live_inputs: dict | None = None


class _NotebookInputs(dict):
    """notebook_inputs() result: input tables of ports that are not there are None."""

    def __missing__(self, key):
        return None


def notebook_inputs(workflow: str, node: int) -> dict:
    """Inputs of a node of a live workflow, for working on a Jupyter Notebook node's notebook.

    Returns {"input_table_1": DataFrame | None, "input_table_2": ..., "flow_variables": dict};
    upstream nodes that have not run yet are executed first. (When the workflow runs the
    notebook, these values are injected instead.)
    """
    if _live_inputs is not None:
        # the workflow runs this notebook in its open JupyterLab tab: its inputs
        return _NotebookInputs(_live_inputs)
    if os.environ.get("VERKNODE_NOTEBOOK_RUN"):
        # run headless by the workflow: the real inputs are injected after the parameters cell
        return _NotebookInputs(flow_variables={})
    wf = open(workflow, create=False)
    try:
        target = wf.node(node)
        out = {}
        ports = len(next(n for n in wf._view["nodes"] if n["id"] == target.id)["inPorts"]) - 1
        for port in range(1, ports + 1):
            conn = next(((s, sp) for s, sp, d, dp in wf.connections if d == target.id and dp == port), None)
            if conn is None:
                out[f"input_table_{port}"] = None
                continue
            source = wf.node(conn[0])
            if source.state != "EXECUTED":
                wf.execute(source)
            out[f"input_table_{port}"] = source.table(conn[1])
        data = wf._request("GET", f"/nodes/{target.id}/dialog")
        out["flow_variables"] = {v["name"]: v["value"] for v in data.get("flowVariables") or []}
        return _NotebookInputs(out)
    finally:
        wf.close()


def open(path: str, create: bool = True) -> "Workflow":  # noqa: A001 - mirrors builtins.open on purpose
    """Attach to the live workflow at `path` (relative to the Jupyter root).

    If the file is open in the editor, both share the same workflow.
    With create=True a missing file starts as an empty workflow (saved by wf.save()).
    """
    rel = _root_relative(path)
    view = _srv().request("POST", "sessions", {"path": rel, "create": create})
    wf = Workflow(rel, view)
    _open_workflows.append(wf)
    return wf


_open_workflows: list = []


@atexit.register
def _close_all():
    for wf in list(_open_workflows):
        try:
            wf.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# workflow / node proxies
# ----------------------------------------------------------------------
NodeRef = "int | str | Node"


class Workflow:
    """A live workflow. Node arguments may be Node objects, node ids or node names/labels."""

    def __init__(self, path: str, view: dict):
        self.path = path
        self._view = view
        self._session = view["session"]
        self._closed = False
        self._batching = False
        self._batch_types = None  # (node types, cached while a batch is open)
        self._last_added = None  # (x, y) of the last node added in a batch (its view is not fetched)

    # -- plumbing --------------------------------------------------------
    def _request(self, method: str, path: str, body=None, raw: bool = False):
        """Request on this workflow's session; re-attaches by path if the server dropped it."""
        if self._closed:
            raise VerknodeError("Workflow is closed")
        try:
            return _srv().request(method, f"sessions/{self._session}{path}", body, raw=raw)
        except VerknodeError as e:
            if "Unknown workflow session" not in str(e):
                raise
            self._session = _srv().request("POST", "sessions", {"path": self.path, "create": True})["session"]
            return _srv().request(method, f"sessions/{self._session}{path}", body, raw=raw)

    def _op(self, op: str, **args):
        if self._batching:
            # (no full view per edit: readers refresh it)
            reply = self._request("POST", "/ops", {"op": op, "batch": True, "quiet": True, **args})
            return reply.get("result") or {}
        self._view = self._request("POST", "/ops", {"op": op, **args})
        return (self._view.get("result") or {})

    @contextlib.contextmanager
    def batch(self):
        """Many edits as one undo step, fast also in big workflows::

            with wf.batch():
                prev = wf.add_node("Table Creator")
                for i in range(1000):
                    node = wf.add_node("Math Formula", expression="$a$ + 1")
                    wf.connect(prev, node)
                    prev = node

        The server takes one undo snapshot for the whole batch instead of one per edit, and
        edits do not send the whole workflow back. The editor shows the edits as they happen;
        an undo there (or wf.undo()) ends the batch and undoes all of it. A batch that is not
        ended (the script stopped) ends by itself after two minutes without edits.
        """
        if self._batching:
            yield self
            return
        self._op("begin_batch")
        self._batching, self._batch_types, self._last_added = True, None, None
        try:
            yield self
        finally:
            self._batching, self._batch_types = False, None
            self._op("end_batch")

    def refresh(self) -> "Workflow":
        self._view = self._request("GET", "")
        return self

    def _info(self, nid: int) -> dict:
        for n in self._view["nodes"]:
            if n["id"] == nid:
                return n
        raise VerknodeError(f"No node with id {nid}")

    def _id(self, ref) -> int:
        if isinstance(ref, (Node, Metanode)):
            return ref.id
        if isinstance(ref, int):
            return ref
        return self.node(ref).id

    # -- nodes -------------------------------------------------------------
    def _visible(self) -> list:
        """Node infos without the hidden pass-through nodes of metanode ports."""
        return [n for n in self._view["nodes"] if not n.get("boundary")]

    @property
    def nodes(self) -> list:
        """All nodes (also those inside metanodes)."""
        self.refresh()
        return [Node(self, n["id"]) for n in sorted(self._visible(), key=lambda n: n["id"])]

    def node(self, ref) -> "Node":
        """Node by id, name (e.g. "CSV Reader") or label (e.g. "Node 3")."""
        self.refresh()
        if isinstance(ref, Node):
            return ref
        if isinstance(ref, int):
            self._info(ref)
            return Node(self, ref)
        found = [n for n in self._visible() if ref in (n["annotation"], n["name"])]
        if len(found) != 1:
            raise VerknodeError(f"{len(found)} nodes match {ref!r}; use the node id")
        return Node(self, found[0]["id"])

    def add_node(self, node_type: str, x: float | None = None, y: float | None = None,
                 settings: dict | None = None, metanode=None, **kwargs) -> "Node":
        """Add a node by name or factory id (inside `metanode`, if given); keyword arguments are its settings.

        Settings whose names clash with the parameters (e.g. a Scatter Plot's
        "x" and "y") are passed in the `settings` dict instead.
        """
        settings = {**(settings or {}), **kwargs}
        if self._batching:
            if self._batch_types is None:
                self._batch_types = self._request("GET", "/nodes")
            types = self._batch_types
        else:
            types = self._request("GET", "/nodes")
        t = _resolve_type(node_type, types)
        if x is None or y is None:
            last = self._last_added if self._batching else None
            if last is None and self._view["nodes"]:
                n = max(self._view["nodes"], key=lambda n: n["id"])
                last = (n["x"], n["y"])
            if last is not None:
                x = last[0] + 150 if x is None else x
                y = last[1] if y is None else y
            else:
                x, y = 40 if x is None else x, 40 if y is None else y
        self._last_added = (x, y)
        parent = self._id(metanode) if metanode is not None else None
        nid = self._op("add_node", factory=t["factory"], x=x, y=y, parent=parent)["id"]
        node = Node(self, nid)
        if settings:
            node.configure(**settings)
        return node

    def remove(self, *nodes) -> None:
        self._op("remove_nodes", ids=[self._id(n) for n in nodes])

    def _ends(self, source, dest, src_port: int, dst_port: int) -> dict:
        """Connection ends; a Metanode stands for its output (source) or input (dest) ports."""
        if isinstance(source, Metanode):
            source, src_port = source._port("outputs", src_port), 1
        if isinstance(dest, Metanode):
            dest, dst_port = dest._port("inputs", dst_port), 1
        return dict(sourceID=self._id(source), sourcePort=src_port, destID=self._id(dest), destPort=dst_port)

    def connect(self, source, dest, src_port: int = 1, dst_port: int = 1) -> None:
        """Connect an output port of `source` to an input port of `dest` (port 0 = flow variables).

        A metanode is connected through its ports: ``wf.connect(reader, meta, dst_port=1)``
        outside, ``wf.connect(meta.inport(1), node)`` inside.
        """
        self._op("connect", **self._ends(source, dest, src_port, dst_port))

    def disconnect(self, source, dest, src_port: int = 1, dst_port: int = 1) -> None:
        self._op("disconnect", **self._ends(source, dest, src_port, dst_port))

    @property
    def connections(self) -> list:
        self.refresh()
        return [(c["sourceID"], c["sourcePort"], c["destID"], c["destPort"]) for c in self._view["connections"]]

    # -- metanodes ------------------------------------------------------------
    def collapse(self, *items, name: str | None = None) -> "Metanode":
        """Put nodes (and metanodes) of one level into a new metanode."""
        mid = self._op("collapse", ids=[self._id(i) for i in items], name=name)["id"]
        return Metanode(self, mid)

    @property
    def metanodes(self) -> list:
        self.refresh()
        return [Metanode(self, m["id"]) for m in self._view.get("metanodes", [])]

    def metanode(self, ref) -> "Metanode":
        """Metanode by id or name."""
        self.refresh()
        found = [m for m in self._view.get("metanodes", [])
                 if (m["id"] == ref if isinstance(ref, int) else m["name"] == ref)]
        if len(found) != 1:
            raise VerknodeError(f"{len(found)} metanodes match {ref!r}")
        return Metanode(self, found[0]["id"])

    # -- group annotations ---------------------------------------------------
    def add_group(self, *nodes, text: str = "", color: str | None = None) -> "Group":
        """Group annotation: a bubble around the nodes with a text (color "#rrggbb")."""
        gid = self._op("add_group", nodes=[self._id(n) for n in nodes], text=text, color=color)["id"]
        return Group(self, gid)

    @property
    def groups(self) -> list:
        self.refresh()
        return [Group(self, g["id"]) for g in self._view.get("groups", [])]

    def group(self, ref) -> "Group":
        """Group annotation by id or text."""
        self.refresh()
        groups = self._view.get("groups", [])
        found = [g for g in groups if (g["id"] == ref if isinstance(ref, int) else g["text"] == ref)]
        if len(found) != 1:
            raise VerknodeError(f"{len(found)} group annotations match {ref!r}")
        return Group(self, found[0]["id"])

    def clear(self) -> None:
        """Remove all nodes (undoable)."""
        self._op("clear")

    # -- execution ---------------------------------------------------------
    def execute(self, *nodes, wait: bool = True, timeout: float | None = None) -> dict:
        """Execute the given nodes (all if none) and, with wait=True, block until done.

        Returns {node id: state}; nodes that failed are reported with a warning.
        """
        ids = [self._id(n) for n in nodes] or None
        if self._batching:
            self.refresh()  # (the version to wait from)
        self._op("execute", **({"ids": ids} if ids else {}))
        if not wait:
            return {}
        start = time.time()
        while True:
            view = self._request("GET", f"/wait?version={self._view['version']}&timeout=10")
            self._view = view
            if not view["executing"]:
                break
            if timeout is not None and time.time() - start > timeout:
                raise TimeoutError("Workflow still executing")
        failed = [n for n in view["nodes"] if n["message"] and n["message"]["level"] == "ERROR"
                  and (ids is None or n["id"] in ids or n["state"] != "EXECUTED")]
        for n in failed:
            print(f"⚠ {n['name']} (#{n['id']}): {n['message']['text']}")
        return {n["id"]: n["state"] for n in self._visible() if ids is None or n["id"] in ids
                or n.get("parent") in ids}

    def reset(self, *nodes) -> None:
        ids = [self._id(n) for n in nodes]
        self._op("reset", **({"ids": ids} if ids else {}))

    def cancel(self) -> None:
        self._op("cancel")

    # -- editing -------------------------------------------------------------
    def undo(self) -> None:
        self._op("undo")

    def redo(self) -> None:
        self._op("redo")

    def copy(self, *nodes) -> dict:
        """Copy nodes (with settings and the connections between them) to a fragment."""
        return self._op("copy", ids=[self._id(n) for n in nodes])["fragment"]

    def paste(self, fragment: dict, dx: float = 40, dy: float = 40) -> list:
        """Paste a fragment (from copy(), also of another workflow); returns the new nodes."""
        return [Node(self, i) for i in self._op("paste", fragment=fragment, dx=dx, dy=dy)["ids"]]

    # -- custom nodes ----------------------------------------------------------
    def node_types(self, search: str | None = None) -> list:
        """Node types available in this workflow (built-in plus custom nodes)."""
        low = (search or "").lower()
        return [NodeType(t) for t in self._request("GET", "/nodes")
                if not low or low in t["name"].lower() or low in t["category"].lower()]

    @property
    def node_paths(self) -> list:
        """Custom node folders (relative to the workflow file)."""
        return list(self.refresh()._view["nodePaths"])

    def set_node_paths(self, paths: list, auto_reload: bool | None = None) -> dict:
        """Set the custom node folders (saved with the workflow) and load them."""
        args = {"paths": list(paths)}
        if auto_reload is not None:
            args["autoReload"] = auto_reload
        return self._report(self._op("set_node_paths", **args)["report"])

    def reload_nodes(self) -> dict:
        """Re-import the custom node files; nodes switch to the new code (changed ones are reset)."""
        return self._report(self._op("reload_nodes")["report"])

    def create_node_template(self, folder: str = "nodes") -> str:
        """Create `folder` with an example custom node file, add it to the node folders; returns the file."""
        result = self._op("create_node_template", folder=folder)["template"]
        self._report(result["report"])
        return result["file"]

    @staticmethod
    def _report(report: dict) -> dict:
        for err in report["errors"]:
            print(f"⚠ {err['file']}:\n{err['error']}")
        return report

    # -- workflow variables --------------------------------------------------
    @property
    def variables(self) -> dict:
        self.refresh()
        return {v["name"]: v["value"] for v in self._view["workflowVariables"]}

    def set_variables(self, items) -> None:
        """Replace workflow variables: [(name, type, value), ...] or {name: value} (type inferred)."""
        if isinstance(items, dict):
            items = [(k, _var_type(v), v) for k, v in items.items()]
        self._op("set_workflow_variables",
                 variables=[{"name": n, "type": t, "value": v} for n, t, v in items])

    def set_variable(self, name: str, value, vtype: str | None = None) -> None:
        current = [(v["name"], v["type"], v["value"]) for v in self.refresh()._view["workflowVariables"]
                   if v["name"] != name]
        self.set_variables(current + [(name, vtype or _var_type(value), value)])

    # -- files -----------------------------------------------------------------
    def save(self, path: str | None = None) -> None:
        """Save the workflow (including executed data) to its file or `path`."""
        if path:
            self.path = _root_relative(path)
        self._view = self._request("POST", "/save", {"path": self.path})

    def to_python(self) -> str:
        """Python script that rebuilds this workflow."""
        return self._request("GET", "/python")["script"]

    def close(self) -> None:
        """Detach from the workflow (unsaved changes stay while an editor has it open)."""
        if not self._closed:
            self._closed = True
            if self in _open_workflows:
                _open_workflows.remove(self)
            _srv().request("DELETE", f"sessions/{self._session}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        v = self._view
        executed = sum(n["state"] == "EXECUTED" for n in v["nodes"])
        return (f"<verknode.Workflow {self.path!r}: {len(v['nodes'])} nodes, {executed} executed"
                f"{', unsaved' if v['dirty'] else ''}>")

    def _repr_markdown_(self):
        v = self.refresh()._view
        rows = [f"| {n['id']} | {n['name']} | {n['annotation']} | {n['state']} | "
                f"{(n['message'] or {}).get('text', '')} |" for n in sorted(v["nodes"], key=lambda n: n["id"])]
        return (f"**{self.path}** — {len(v['nodes'])} nodes\n\n| id | node | label | state | message |\n"
                "|---|---|---|---|---|\n" + "\n".join(rows))


def _var_type(value) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "DOUBLE"
    return "STRING"


class Metanode:
    """A metanode: a container of nodes, connected through its ports."""

    def __init__(self, wf: Workflow, mid: int):
        self.wf = wf
        self.id = mid

    def _info(self) -> dict:
        for m in self.wf.refresh()._view.get("metanodes", []):
            if m["id"] == self.id:
                return m
        raise VerknodeError(f"No metanode with id {self.id}")

    def _port(self, key: str, index: int) -> int:
        ports = self._info()[key]
        if not 1 <= index <= len(ports):
            raise VerknodeError(f"Metanode {self.id} has no {key[:-1]} port {index} (it has {len(ports)})")
        return ports[index - 1]

    @property
    def name(self) -> str:
        return self._info()["name"]

    @name.setter
    def name(self, name: str) -> None:
        self.wf._op("rename_metanode", id=self.id, name=name)

    @property
    def nodes(self) -> list:
        """Nodes and metanodes directly inside."""
        self.wf.refresh()
        view = self.wf._view
        return [Node(self.wf, n["id"]) for n in self.wf._visible() if n.get("parent") == self.id] + \
               [Metanode(self.wf, m["id"]) for m in view.get("metanodes", []) if m["parent"] == self.id]

    def inport(self, index: int) -> "Node":
        """Input port `index` seen from inside (connect it to nodes in the metanode)."""
        return Node(self.wf, self._port("inputs", index))

    def outport(self, index: int) -> "Node":
        """Output port `index` seen from inside (connect nodes in the metanode to it)."""
        return Node(self.wf, self._port("outputs", index))

    def add_port(self, side: str, port_type: str = "table") -> int:
        """Add an input ("in") or output ("out") port; returns its index."""
        return self.wf._op("add_metanode_port", id=self.id, side=side, type=port_type)["index"]

    def remove_port(self, side: str, index: int) -> None:
        self.wf._op("remove_metanode_port", id=self.id, side=side, index=index)

    def expand(self) -> None:
        """Dissolve the metanode; its nodes move to the enclosing level."""
        self.wf._op("expand", id=self.id)

    def execute(self, wait: bool = True) -> dict:
        return self.wf.execute(self, wait=wait)

    def reset(self) -> None:
        self.wf.reset(self)

    def remove(self) -> None:
        """Delete the metanode with everything inside."""
        self.wf.remove(self)

    def __repr__(self):
        m = self._info()
        return f"<verknode Metanode {self.id} {m['name']!r} in={len(m['inputs'])} out={len(m['outputs'])}>"


class Group:
    """A group annotation (bubble around nodes) of a live workflow."""

    def __init__(self, wf: Workflow, gid: int):
        self.wf = wf
        self.id = gid

    def _info(self) -> dict:
        for g in self.wf.refresh()._view.get("groups", []):
            if g["id"] == self.id:
                return g
        raise VerknodeError(f"No group annotation with id {self.id}")

    @property
    def text(self) -> str:
        return self._info()["text"]

    @text.setter
    def text(self, text: str) -> None:
        self.wf._op("update_group", id=self.id, text=text)

    @property
    def color(self) -> str:
        return self._info()["color"]

    @color.setter
    def color(self, color: str) -> None:
        self.wf._op("update_group", id=self.id, color=color)

    @property
    def nodes(self) -> list:
        return [Node(self.wf, n) for n in self._info()["nodes"]]

    @nodes.setter
    def nodes(self, nodes) -> None:
        self.wf._op("update_group", id=self.id, nodes=[self.wf._id(n) for n in nodes])

    def remove(self) -> None:
        """Delete the annotation (the nodes stay)."""
        self.wf._op("remove_group", id=self.id)

    def __repr__(self):
        g = self._info()
        return f"<verknode Group {self.id} {g['text']!r} nodes={g['nodes']} color={g['color']}>"


class Node:
    """A node of a live workflow."""

    def __init__(self, wf: Workflow, nid: int):
        self.wf = wf
        self.id = nid

    def _info(self) -> dict:
        return self.wf.refresh()._info(self.id)

    @property
    def name(self) -> str:
        return self._info()["name"]

    @property
    def state(self) -> str:
        return self._info()["state"]

    @property
    def message(self) -> str | None:
        m = self._info()["message"]
        return m["text"] if m else None

    @property
    def annotation(self) -> str:
        return self._info()["annotation"]

    @annotation.setter
    def annotation(self, text: str) -> None:
        self.wf._op("set_annotation", id=self.id, text=text)

    @property
    def position(self) -> tuple:
        n = self._info()
        return n["x"], n["y"]

    def move(self, x: float, y: float) -> None:
        self.wf._op("move", positions=[{"id": self.id, "x": x, "y": y}])

    def _dialog(self) -> dict:
        return self.wf._request("GET", f"/nodes/{self.id}/dialog")

    @property
    def settings(self) -> dict:
        return self._dialog()["settings"]

    def configure(self, **settings) -> "Node":
        """Change settings (only the given keys); unknown keys raise an error."""
        d = self._dialog()
        keys = {f["key"] for f in d["node"]["fields"]}
        unknown = set(settings) - keys
        if unknown:
            raise VerknodeError(f"{d['node']['name']} has no setting(s) {sorted(unknown)}; available: {sorted(keys)}")
        self.wf._op("set_settings", id=self.id, settings=settings, variables=d["variables"])
        return self

    def bind(self, setting: str, variable: str | None) -> "Node":
        """Control a setting by a flow variable (the dialog's "Flow Variables" tab); None removes it."""
        d = self._dialog()
        variables = dict(d["variables"])
        entry = dict(variables.get(setting) or {})
        entry["used"] = variable
        variables[setting] = entry
        self.wf._op("set_settings", id=self.id, settings={}, variables=variables)
        return self

    def unbind(self, setting: str) -> "Node":
        return self.bind(setting, None)

    def expose(self, setting: str, variable: str | None) -> "Node":
        """Publish a setting's value as a flow variable named `variable`."""
        d = self._dialog()
        variables = dict(d["variables"])
        entry = dict(variables.get(setting) or {})
        entry["exposed"] = variable
        variables[setting] = entry
        self.wf._op("set_settings", id=self.id, settings={}, variables=variables)
        return self

    def help(self) -> None:
        """Print the node description, ports and settings with current values."""
        d = self._dialog()
        node = d["node"]
        print(f"{node['name']}  ({node['category'].replace('/', ' › ')})\n  {node['shortDescription']}")
        for title, ports in (("Input ports", node["inPorts"]), ("Output ports", node["outPorts"])):
            if ports:
                print(f"{title}:")
                for p in ports:
                    print(f"  {p['index']}: {p['name']} [{p['type']}]{' (optional)' if p['optional'] else ''}")
        print("Settings:")
        for f in node["fields"]:
            extra = f" options={f['options']}" if f.get("options") and len(f["options"]) <= 12 else ""
            print(f"  {f['key']} = {d['settings'].get(f['key'])!r}   # {f['label']} ({f['type']}){extra}")

    def execute(self, wait: bool = True) -> str:
        self.wf.execute(self, wait=wait)
        return self.state

    def reset(self) -> None:
        self.wf.reset(self)

    def table(self, port: int = 1):
        """Output table of `port` as a polars DataFrame (node must be executed)."""
        import polars as pl

        data = self.wf._request("GET", f"/nodes/{self.id}/ports/{port}/table", raw=True)
        return pl.read_parquet(io.BytesIO(data))

    def variables(self) -> dict:
        """Flow variables at the node's output."""
        data = self.wf._request("GET", f"/nodes/{self.id}/ports/0")
        return {v["name"]: v["value"] for v in data.get("flowVariables") or []}

    def port(self, port: int = 1) -> dict:
        """Summary of an output port (spec, port object info)."""
        return self.wf._request("GET", f"/nodes/{self.id}/ports/{port}?limit=0")

    def view(self):
        """Node view (HTML) for display in a notebook."""
        html = self.wf._request("GET", f"/nodes/{self.id}/view")["html"]
        if html is None:
            raise VerknodeError("No view available (execute the node first)")
        try:
            from IPython.display import HTML
        except ImportError:
            return html
        import html as _html

        return HTML(f'<iframe style="width:100%;height:600px;border:0" srcdoc="{_html.escape(html)}"></iframe>')

    def __eq__(self, other):
        return isinstance(other, Node) and other.wf is self.wf and other.id == self.id

    def __hash__(self):
        return hash((id(self.wf), self.id))

    def __repr__(self):
        try:
            n = self._info()
        except VerknodeError:
            return f"<verknode.Node {self.id} (removed)>"
        msg = f" – {n['message']['text']}" if n["message"] else ""
        return f"<verknode.Node {self.id}: {n['name']} [{n['state']}] {n['annotation']!r}{msg}>"
