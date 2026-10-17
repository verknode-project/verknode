"""Workflow manager: node graph, configure analysis, execution and loops."""
from __future__ import annotations

import base64
import collections
import copy
import datetime as dt
import functools
import hashlib
import json
import math
import os
import re
import threading
import uuid
import time
import traceback

import polars as pl

from .custom import CustomNodes, capture_print, discard_print
from .flowvars import FlowVariable, coerce, make, stack_to_json
from .node import (
    FLOWVAR,
    FLOWVARIABLE,
    IMPLICIT_IN,
    IMPLICIT_OUT,
    INACTIVE,
    NODE_REGISTRY,
    TABLE,
    CanceledExecutionError,
    ExecContext,
    InvalidSettingsError,
    ImagePortObject,
    NodeModel,
    Port,
    PortObject,
)
from .metanode import boundary_class, boundary_factory, is_boundary
from .types import dtype_kind, json_value, spec_to_json


class WorkflowError(Exception):
    """User-facing error for an invalid workflow operation."""


class Connection:
    __slots__ = ("source_id", "source_port", "dest_id", "dest_port")

    def __init__(self, source_id, source_port, dest_id, dest_port):
        self.source_id = int(source_id)
        self.source_port = int(source_port)
        self.dest_id = int(dest_id)
        self.dest_port = int(dest_port)

    def key(self):
        return (self.source_id, self.source_port, self.dest_id, self.dest_port)

    def to_json(self) -> dict:
        return {
            "sourceID": self.source_id,
            "sourcePort": self.source_port,
            "destID": self.dest_id,
            "destPort": self.dest_port,
        }


class MissingNodeModel(NodeModel):
    """Placeholder for a node whose factory is not installed."""

    node_type = "Other"
    icon = "?"

    def __init__(self, factory, name, in_ports, out_ports):
        self.factory = factory
        self.name = name or "Missing Node"
        self.in_ports = in_ports
        self.out_ports = out_ports
        self.description = f"Node implementation not found: {factory}"

    def configure(self, in_specs, s, ctx):
        raise InvalidSettingsError(f"Node implementation not found: {self.factory}")

    def describe(self):
        d = NodeModel.describe.__func__(type(self))
        d.update(factory=self.factory, name=self.name, icon=self.icon, nodeType=self.node_type)
        d["inPorts"] = [p.to_json(i + 1) for i, p in enumerate(self.in_ports)]
        d["outPorts"] = [p.to_json(i + 1) for i, p in enumerate(self.out_ports)]
        return d


class NodeContainer:
    def __init__(self, nid: int, factory: str, x: float = 0, y: float = 0, model=None):
        if model is None:
            if factory not in NODE_REGISTRY:
                raise WorkflowError(f"Unknown node factory: {factory}")
            model = NODE_REGISTRY[factory]()
        self.id = nid
        self.factory = factory
        self.model: NodeModel = model
        self.settings = model.default_settings()
        self.var_settings: dict = {}  # key -> {"used": name|None, "exposed": name|None}
        self.x, self.y = x, y
        self.parent: int | None = None  # metanode containing this node
        self.annotation = f"Node {nid}"
        self.custom_description = ""
        # canvas widget placement: {"side": left|right|above|below|free, "x", "y", "w", "h", "minimized",
        # "anchored"} (anchored: kept where it is - not dragged, not moved along with nodes)
        # (a "free" widget is at x, y on the canvas: it stays there when the node moves)
        self.widget_layout: dict | None = None
        self.outputs: list | None = None  # index 0 = implicit flow variable port
        self.out_stack: dict | None = None
        self.inactive = False
        self.message: tuple[str, str] | None = None  # (level, text)
        self.runtime: str | None = None  # QUEUED | EXECUTING
        self.exec_time: float | None = None
        self.rows: int | None = None  # rows the node worked on (its first input table, else its first output)
        self.started: float | None = None  # time.time() when it started executing
        self.progress: float | None = None  # 0..1 while executing, when the node tells (or a loop)
        # time and rows of the last successful execution, kept across resets (for estimates)
        self.last_time: float | None = None
        self.last_rows: int | None = None
        self.last_iterations: int | None = None  # loop ends: iterations of the last run (loop progress)
        self.loop_state: dict | None = None  # loop ends while their loop runs (iteration, total, start)
        # input port -> (object, flow variables) it gets instead of what is connected (a parallel
        # chunk copy: its chunk, while the port it is connected to carries the whole table)
        self.fed: dict | None = None
        self.view: str | None = None  # node view HTML
        self.display: dict | None = None  # shown on the canvas (widget nodes, ExecContext.set_display)
        self.traceback: str | None = None  # of the last execution failure
        # state of files the node reads (NodeModel.external_state), at its last execution
        self.external_state: str | None = None
        # ... and when the workflow last looked at them (ports, resets)
        self.watched_state: str | None = None

    @property
    def executed(self) -> bool:
        return self.outputs is not None

    @property
    def name(self) -> str:
        return self.model.name

    def in_port(self, idx: int) -> Port | None:
        if idx == 0:
            return IMPLICIT_IN
        ports = self.model.in_ports
        return ports[idx - 1] if 0 < idx <= len(ports) else None

    def out_port(self, idx: int) -> Port | None:
        if idx == 0:
            return IMPLICIT_OUT
        ports = self.model.out_ports
        return ports[idx - 1] if 0 < idx <= len(ports) else None

    def clear(self):
        self.outputs = None
        self.out_stack = None
        self.inactive = False
        self.message = None
        self.exec_time = None
        self.rows = None
        self.view = None
        self.display = None
        self.traceback = None
        self.external_state = None


UNDO_LIMIT = 100

# colours offered for group annotations (the first is the default)
GROUP_COLORS = ["#e6b422", "#4caf7d", "#3d9be9", "#a46bd6", "#e5534b", "#ef8a3c", "#8c8c8c"]


def undoable(method):
    """Record the workflow structure before an edit so it can be undone."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self._undo_depth:
            return method(self, *args, **kwargs)
        snapshot = self._snapshot()
        version = self.version
        self._undo_depth += 1
        try:
            result = method(self, *args, **kwargs)
        finally:
            self._undo_depth -= 1
        if self.version != version:
            self._undo.append(snapshot)
            del self._undo[:-UNDO_LIMIT]
            self._redo.clear()
        return result

    return wrapper


class Workflow:
    _undo_depth = 0

    def __init__(self):
        self.nodes: dict[int, NodeContainer] = {}
        self.connections: list[Connection] = []
        # metanodes: id -> {"id", "name", "parent", "x", "y", "inputs", "outputs"}; ids share
        # the node id space, "inputs"/"outputs" are the pass-through nodes of its ports
        self.metanodes: dict[int, dict] = {}
        # group annotations: {"id", "nodes", "text", "color"}, drawn as bubbles around their nodes
        self.groups: list[dict] = []
        self.next_group_id = 1
        self.workflow_variables: list[FlowVariable] = []
        self.meta = {"author": "", "description": "", "created": dt.datetime.now().isoformat(timespec="seconds")}
        self.next_id = 1
        self.base_dir = "."
        self.path: str | None = None  # absolute path of the .vkf file, if known
        self.rel_path: str | None = None  # path relative to the Jupyter root
        self.node_paths: list[str] = []  # custom node folders (relative to the workflow file)
        self.auto_reload = True  # reload changed custom node files before execution
        self.custom = CustomNodes()
        self.registry_version = 0
        self.custom_report: dict | None = None
        self.dirty = False
        self.version = 0
        self.log = collections.deque(maxlen=500)
        self._log_seq = 0
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._analysis_cache = (None, None)
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._frontend: dict[str, dict] = {}  # open requests to editors (frontend_request)
        self.editor_seen = 0.0  # last long poll of an editor (time.time())

    # ------------------------------------------------------------------
    # bookkeeping
    # ------------------------------------------------------------------
    @property
    def executing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _changed(self, dirty=True):
        self.version += 1
        if dirty:
            self.dirty = True

    def _log(self, level: str, text: str, node: int | None = None):
        self._log_seq += 1
        self.log.append({
            "seq": self._log_seq,
            "time": dt.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "node": node,
            "text": text,
        })
        self.version += 1

    def _require_idle(self):
        if self.executing:
            raise WorkflowError("Workflow is executing; cancel execution first")

    def node(self, nid) -> NodeContainer:
        nid = int(nid)
        if nid not in self.nodes:
            raise WorkflowError(f"No node with id {nid}")
        return self.nodes[nid]

    # ------------------------------------------------------------------
    # graph queries
    # ------------------------------------------------------------------
    def in_conns(self, nid) -> dict[int, Connection]:
        return {c.dest_port: c for c in self.connections if c.dest_id == nid}

    def out_conns(self, nid) -> list[Connection]:
        return [c for c in self.connections if c.source_id == nid]

    def descendants(self, nid) -> set:
        seen, todo = set(), [nid]
        while todo:
            n = todo.pop()
            for c in self.out_conns(n):
                if c.dest_id not in seen:
                    seen.add(c.dest_id)
                    todo.append(c.dest_id)
        return seen

    def ancestors(self, nid) -> set:
        seen, todo = set(), [nid]
        while todo:
            n = todo.pop()
            for c in self.in_conns(n).values():
                if c.source_id not in seen:
                    seen.add(c.source_id)
                    todo.append(c.source_id)
        return seen

    def topo_order(self, subset=None) -> list:
        ids = set(self.nodes) if subset is None else set(subset)
        indeg = {n: 0 for n in ids}
        for c in self.connections:
            if c.source_id in ids and c.dest_id in ids:
                indeg[c.dest_id] += 1
        ready = sorted((n for n, d in indeg.items() if d == 0), key=lambda n: (self.nodes[n].y, self.nodes[n].x, n))
        order = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in self.out_conns(n):
                if c.dest_id in indeg:
                    indeg[c.dest_id] -= 1
                    if indeg[c.dest_id] == 0:
                        ready.append(c.dest_id)
        return order

    def find_loop_start(self, end_id) -> int | None:
        """Walk upstream from a loop end, skipping over nested loops."""
        return self._find_scope_start(end_id, "loop_role", ("start", "end"), "Loop End", "Loop Start")

    def find_try_start(self, catch_id) -> int | None:
        """Walk upstream from a Catch Errors node to its Try node, skipping nested scopes."""
        return self._find_scope_start(catch_id, "scope_role", ("try", "catch"), "Catch Errors", "Try")

    def _find_scope_start(self, end_id, attr, roles, end_name, start_name) -> int | None:
        start_role, end_role = roles
        found, seen = set(), set()

        def walk(nid):
            for c in self.in_conns(nid).values():
                u = c.source_id
                if u in seen:
                    continue
                seen.add(u)
                role = getattr(self.nodes[u].model, attr)
                if role == start_role:
                    found.add(u)
                elif role == end_role:
                    inner = self._find_scope_start(u, attr, roles, end_name, start_name)
                    if inner is not None:
                        seen.add(inner)
                        walk(inner)
                else:
                    walk(u)

        walk(end_id)
        if len(found) > 1:
            raise WorkflowError(f"{end_name} is connected to more than one {start_name}")
        return next(iter(found), None)

    def _caught_error(self, catch_id) -> dict | None:
        """Error of a failed node in the try scope of a Catch node (None if no failure)."""
        start = self.find_try_start(catch_id)
        if start is None:
            return None
        scope = ({start} | self.descendants(start)) & self.ancestors(catch_id)
        for nid in self.topo_order(scope):
            nc = self.nodes[nid]
            if not nc.executed and nc.message and nc.message[0] == "ERROR":
                return {"id": nid, "node": f"{nc.name} {_label(nc)}", "message": nc.message[1],
                        "trace": nc.traceback or nc.message[1]}
        return None

    def loop_body(self, start_id, end_id) -> set:
        # nodes downstream of the start that are not downstream of the end,
        # including dangling branches inside the loop
        return self.descendants(start_id) - self.descendants(end_id) - {end_id, start_id}

    # ------------------------------------------------------------------
    # flow variables & settings
    # ------------------------------------------------------------------
    def _base_stack(self) -> dict:
        return {v.name: v for v in self.workflow_variables}

    def _merge_stacks(self, port_stacks: list[tuple[int, dict]]) -> dict:
        stack = self._base_stack()
        # lower port index has priority, the implicit variable port (0) most
        for _, st in sorted(port_stacks, key=lambda p: -p[0]):
            stack.update(st)
        return stack

    def _resolve_settings(self, nc: NodeContainer, stack: dict) -> tuple[dict, dict]:
        s = copy.deepcopy(nc.settings)
        exposed = {}
        for key, binding in nc.var_settings.items():
            f = nc.model.field(key)
            if f is None:
                continue
            used = (binding or {}).get("used")
            if used:
                if used not in stack:
                    raise InvalidSettingsError(f'Flow variable "{used}" used for "{f.label}" is not available')
                s[key] = _coerce_setting(stack[used].value, f)
            exp = (binding or {}).get("exposed")
            if exp and s.get(key) is not None and not isinstance(s.get(key), (list, dict)):
                exposed[exp] = make(exp, s[key])
        return s, exposed

    # ------------------------------------------------------------------
    # configure analysis (node status, specs for dialogs)
    # ------------------------------------------------------------------
    def analyze(self) -> dict:
        cached_version, cached = self._analysis_cache
        if cached_version == self.version and cached is not None:
            return cached
        version = self.version
        result = {}
        for nid in self.topo_order():
            result[nid] = self._analyze_node(nid, result)
        self._analysis_cache = (version, result)
        return result

    def _analyze_node(self, nid, result) -> dict:
        nc = self.nodes[nid]
        m = nc.model
        n_out = len(m.out_ports)
        port_stacks, in_specs, in_json = [], [], []
        ok, unknown, any_inactive, message = True, False, False, None
        conns = self.in_conns(nid)
        for idx in range(len(m.in_ports) + 1):
            c = conns.get(idx)
            port = nc.in_port(idx)
            if c is None:
                if idx > 0:
                    in_specs.append(None)
                    in_json.append(None)
                    if not port.optional:
                        ok = False
                continue
            up = result.get(c.source_id)
            if up is None:
                ok = False
                continue
            port_stacks.append((idx, up["stack"]))
            spec = up["specs"][c.source_port] if up["ok"] else None
            if not up["ok"]:
                ok = False
            if spec is INACTIVE:
                any_inactive = True
            if idx > 0:
                if spec is None and up["ok"]:
                    unknown = True
                in_specs.append(spec)
                in_json.append(_spec_json(spec))
        in_stack = self._merge_stacks(port_stacks)

        if nc.executed:
            specs = [FLOWVAR] + [_spec_of_output(o) for o in nc.outputs[1:]]
            return {"ok": True, "specs": specs, "stack": nc.out_stack or in_stack,
                    "in_stack": in_stack, "in_specs": in_json, "message": None}

        specs = [FLOWVAR] + [None] * n_out
        out_stack = dict(in_stack)
        if ok and any_inactive and not m.handles_inactive:
            specs = [INACTIVE] * (n_out + 1)
        elif ok:
            try:
                s, exposed = self._resolve_settings(nc, in_stack)
                if unknown:
                    m.validate(s, in_specs)
                else:
                    ctx = ExecContext(in_stack, self.base_dir, dry=True,
                                      loop={"iteration": 0, "last": True, "collected": [], "dry": True})
                    if getattr(m, "_verknode_custom", False):
                        with discard_print():
                            outs = m.configure(in_specs, s, ctx)
                    else:
                        outs = m.configure(in_specs, s, ctx)
                    if outs is not None:
                        specs = [FLOWVAR] + list(outs)
                    out_stack.update(ctx.pushed)
                out_stack.update(exposed)
            except BaseException as e:  # noqa: BLE001 - any configure failure means "not configured"
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                ok = False
                message = str(e) or type(e).__name__
        return {"ok": ok, "specs": specs, "stack": out_stack, "in_stack": in_stack,
                "in_specs": in_json, "message": message}

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    @undoable
    def add_node(self, factory: str, x: float = 0, y: float = 0, parent=None) -> int:
        """Add a node (inside metanode `parent`, if given); returns its id."""
        with self._lock:
            if parent is not None:
                parent = self.metanode(parent)["id"]
            nid = self.next_id
            self.nodes[nid] = NodeContainer(nid, factory, x, y, model=self._new_model(factory))
            self.nodes[nid].parent = parent
            self.next_id += 1
            self._changed()
            return nid

    @undoable
    def remove_nodes(self, ids):
        """Remove nodes and metanodes (with everything inside)."""
        self._require_idle()
        with self._lock:
            items = {int(i) for i in ids}
            metas = set()
            for i in items:
                if i in self.metanodes:
                    metas |= {i} | self._inner_metanodes(i)
                else:
                    self.node(i)
            ids = {i for i in items if i not in self.metanodes} | self.expand_ids(metas)
            for mid in metas:
                del self.metanodes[mid]
            for m in self.metanodes.values():  # ports of remaining metanodes that were removed
                m["inputs"] = [b for b in m["inputs"] if b not in ids]
                m["outputs"] = [b for b in m["outputs"] if b not in ids]
            for nid in ids:
                for d in self.descendants(nid):
                    self._reset_one(d)
            self.connections = [c for c in self.connections if c.source_id not in ids and c.dest_id not in ids]
            for nid in ids:
                del self.nodes[nid]
            for g in self.groups:
                g["nodes"] = [n for n in g["nodes"] if n not in ids and n not in metas]
            self.groups = [g for g in self.groups if g["nodes"]]
            self._changed()

    @undoable
    def connect(self, source_id, source_port, dest_id, dest_port):
        self._require_idle()
        with self._lock:
            conn = Connection(source_id, source_port, dest_id, dest_port)
            src, dst = self.node(conn.source_id), self.node(conn.dest_id)
            sp, dp = src.out_port(conn.source_port), dst.in_port(conn.dest_port)
            if sp is None or dp is None:
                raise WorkflowError("Invalid port")
            if sp.type != dp.type:
                raise WorkflowError(f"Incompatible ports: {sp.name} ({sp.type}) -> {dp.name} ({dp.type})")
            if self._level_out(src.id) != self._level_in(dst.id):
                raise WorkflowError("Connections cannot cross a metanode boundary; connect to the metanode's ports")
            if src.id == dst.id or src.id in self.descendants(dst.id):
                raise WorkflowError("Connection would create a cycle")
            self.connections = [
                c for c in self.connections
                if not (c.dest_id == conn.dest_id and c.dest_port == conn.dest_port)
            ]
            self.connections.append(conn)
            self._reset_with_descendants(dst.id)
            self._changed()

    @undoable
    def disconnect(self, source_id, source_port, dest_id, dest_port):
        self._require_idle()
        with self._lock:
            key = (int(source_id), int(source_port), int(dest_id), int(dest_port))
            before = len(self.connections)
            self.connections = [c for c in self.connections if c.key() != key]
            if len(self.connections) != before:
                self._reset_with_descendants(int(dest_id))
                self._changed()

    @undoable
    def rewire(self, remove=(), add=(), positions=None, delete=()):
        """Several edits as one undo step: move nodes, delete nodes, remove and add
        connections (in that order; all or nothing)."""
        self._require_idle()
        with self._lock:
            snapshot = self._snapshot()
            try:
                if positions:
                    self.move(positions)
                if delete:
                    self.remove_nodes(delete)
                for c in remove:
                    self.disconnect(c["sourceID"], c["sourcePort"], c["destID"], c["destPort"])
                for c in add:
                    self.connect(c["sourceID"], c["sourcePort"], c["destID"], c["destPort"])
            except Exception:
                self._restore(snapshot)
                raise

    @undoable
    def move(self, positions: list, layouts: list | None = None):
        """Move nodes (and metanodes); ``layouts``: widgets moved along (set_widget_layouts items)."""
        if layouts:
            self.set_widget_layouts(layouts)
        for p in positions:
            x, y = float(p["x"]), float(p["y"])
            if int(p["id"]) in self.metanodes:
                self.metanodes[int(p["id"])].update(x=x, y=y)
            else:
                nc = self.node(p["id"])
                nc.x, nc.y = x, y
        self._changed()

    @undoable
    def set_annotation(self, nid, text: str):
        self.node(nid).annotation = text
        self._changed()

    @undoable
    def set_widget_layout(self, nid, layout: dict | None):
        """Where a widget node's canvas widget sits, its size and whether it is minimized."""
        self.set_widget_layouts([{"id": nid, "layout": layout}])

    @undoable
    def set_widget_layouts(self, items: list):
        """set_widget_layout of several nodes at once ([{"id", "layout"}], one undo step)."""
        for it in items:
            nc = self.node(it["id"])
            new = clean_widget_layout(it.get("layout"), (nc.x, nc.y))
            if new != nc.widget_layout:
                nc.widget_layout = new
                self._changed()

    # ------------------------------------------------------------------
    # metanodes
    # ------------------------------------------------------------------
    def metanode(self, mid) -> dict:
        m = self.metanodes.get(int(mid))
        if m is None:
            raise WorkflowError(f"No metanode with id {mid}")
        return m

    def _boundaries(self) -> dict:
        """Pass-through node id -> (metanode id, "in"|"out", port index from 0)."""
        out = {}
        for m in self.metanodes.values():
            for side, key in (("in", "inputs"), ("out", "outputs")):
                for k, b in enumerate(m[key]):
                    out[b] = (m["id"], side, k)
        return out

    def parent_of(self, item) -> int | None:
        item = int(item)
        return self.metanodes[item]["parent"] if item in self.metanodes else self.node(item).parent

    def _level_out(self, nid) -> int | None:
        """Level (metanode id or None) that the outputs of a node feed into."""
        b = self._boundaries().get(int(nid))
        if b is None:
            return self.nodes[int(nid)].parent
        return b[0] if b[1] == "in" else self.metanodes[b[0]]["parent"]

    def _level_in(self, nid) -> int | None:
        """Level that the inputs of a node come from."""
        b = self._boundaries().get(int(nid))
        if b is None:
            return self.nodes[int(nid)].parent
        return self.metanodes[b[0]]["parent"] if b[1] == "in" else b[0]

    def _inner_metanodes(self, mid) -> set:
        inner = {m["id"] for m in self.metanodes.values() if m["parent"] == mid}
        for i in list(inner):
            inner |= self._inner_metanodes(i)
        return inner

    def expand_ids(self, ids) -> set:
        """Node ids for node and metanode ids (a metanode stands for every node inside)."""
        out = set()
        for i in ids:
            i = int(i)
            if i in self.metanodes:
                metas = {i} | self._inner_metanodes(i)
                out |= {nid for nid, nc in self.nodes.items() if nc.parent in metas}
            else:
                self.node(i)
                out.add(i)
        return out

    def _add_boundary(self, mid, side: str, port_type: str) -> int:
        factory = boundary_factory(side, port_type)
        nid = self.next_id
        self.next_id += 1
        nc = NodeContainer(nid, factory, model=boundary_class(factory)())
        nc.parent = mid
        self.nodes[nid] = nc
        self.metanodes[mid]["inputs" if side == "in" else "outputs"].append(nid)
        return nid

    def _prime_boundaries(self, ids):
        """Pass already computed results through new port nodes (keeps downstream results valid)."""
        for nid in self.topo_order(set(ids)):
            if all(self.nodes[c.source_id].executed for c in self.in_conns(nid).values()) and self.in_conns(nid):
                self._execute_single(nid)

    @undoable
    def collapse(self, ids, name: str | None = None) -> int:
        """Move nodes (and metanodes) of one level into a new metanode; returns its id.

        Connections from and to the rest of the workflow become metanode ports.
        """
        self._require_idle()
        with self._lock:
            items = list(dict.fromkeys(int(i) for i in ids))
            if not items:
                raise WorkflowError("Select the nodes to put into the metanode")
            bmap = self._boundaries()
            if any(i in bmap for i in items):
                raise WorkflowError("Metanode ports cannot be moved")
            levels = {self.parent_of(i) for i in items}
            if len(levels) != 1:
                raise WorkflowError("Select nodes of one level only")
            level = levels.pop()
            chosen = set(items)
            # node or metanode at `level` that a connection end belongs to
            src_item = lambda nid: bmap[nid][0] if nid in bmap and bmap[nid][1] == "out" else nid  # noqa: E731
            dst_item = lambda nid: bmap[nid][0] if nid in bmap and bmap[nid][1] == "in" else nid  # noqa: E731
            incoming: dict = {}
            outgoing: dict = {}
            crossing = []
            for c in self.connections:
                if self._level_out(c.source_id) != level:
                    continue
                inside_src, inside_dst = src_item(c.source_id) in chosen, dst_item(c.dest_id) in chosen
                if inside_dst and not inside_src:
                    incoming.setdefault((c.source_id, c.source_port), []).append((c.dest_id, c.dest_port))
                elif inside_src and not inside_dst:
                    outgoing.setdefault((c.source_id, c.source_port), []).append((c.dest_id, c.dest_port))
                else:
                    continue
                crossing.append(c)

            def pos(i):
                item = self.metanodes.get(i)
                return (item["x"], item["y"]) if item else (self.nodes[i].x, self.nodes[i].y)

            xs, ys = zip(*(pos(i) for i in items))
            mid = self.next_id
            self.next_id += 1
            self.metanodes[mid] = {"id": mid, "name": name or "Metanode", "parent": level,
                                   "x": sum(xs) / len(xs), "y": sum(ys) / len(ys), "inputs": [], "outputs": []}
            for i in items:
                if i in self.metanodes:
                    self.metanodes[i]["parent"] = mid
                else:
                    self.nodes[i].parent = mid
            self.connections = [c for c in self.connections if c not in crossing]
            new = []
            # ports ordered top to bottom by the nodes they connect to inside
            for (s, sp), dests in sorted(incoming.items(), key=lambda kv: min(pos(dst_item(d))[1] for d, _ in kv[1])):
                b = self._add_boundary(mid, "in", self.nodes[s].out_port(sp).type)
                self.connections.append(Connection(s, sp, b, 1))
                self.connections += [Connection(b, 1, d, dp) for d, dp in dests]
                new.append(b)
            for (s, sp), dests in sorted(outgoing.items(), key=lambda kv: pos(src_item(kv[0][0]))[1]):
                b = self._add_boundary(mid, "out", self.nodes[s].out_port(sp).type)
                self.connections.append(Connection(s, sp, b, 1))
                self.connections += [Connection(b, 1, d, dp) for d, dp in dests]
                new.append(b)
            self._prime_boundaries(new)
            # groups around all of the collapsed nodes now go around the metanode
            for g in self.groups:
                if chosen <= set(g["nodes"]):
                    g["nodes"] = [n for n in g["nodes"] if n not in chosen] + [mid]
            self._changed()
            return mid

    @undoable
    def expand(self, mid):
        """Dissolve a metanode: its nodes move to the enclosing level, ports become direct connections."""
        self._require_idle()
        with self._lock:
            m = self.metanode(mid)
            mid = m["id"]
            ports = set(m["inputs"]) | set(m["outputs"])
            keep = [c for c in self.connections if c.source_id not in ports and c.dest_id not in ports]
            for b in m["inputs"] + m["outputs"]:
                for ci in (c for c in self.connections if c.dest_id == b):
                    for co in (c for c in self.connections if c.source_id == b):
                        keep.append(Connection(ci.source_id, ci.source_port, co.dest_id, co.dest_port))
            self.connections = keep
            for b in ports:
                del self.nodes[b]
            nodes = [nc for nc in self.nodes.values() if nc.parent == mid]
            metas = [x for x in self.metanodes.values() if x["parent"] == mid]
            points = [(nc.x, nc.y) for nc in nodes] + [(x["x"], x["y"]) for x in metas]
            if points:
                # centre the contents where the metanode was
                dx = m["x"] - (min(p[0] for p in points) + max(p[0] for p in points)) / 2
                dy = m["y"] - (min(p[1] for p in points) + max(p[1] for p in points)) / 2
                for nc in nodes:
                    nc.x, nc.y, nc.parent = nc.x + dx, nc.y + dy, m["parent"]
                for x in metas:
                    x.update(x=x["x"] + dx, y=x["y"] + dy, parent=m["parent"])
            del self.metanodes[mid]
            # groups around the metanode go around its former contents
            contents = [nc.id for nc in nodes] + [x["id"] for x in metas]
            for g in self.groups:
                if mid in g["nodes"]:
                    g["nodes"] = [n for n in g["nodes"] if n != mid] + [n for n in contents if n not in g["nodes"]]
            self.groups = [g for g in self.groups if g["nodes"]]
            self._changed()

    @undoable
    def rename_metanode(self, mid, name: str):
        m = self.metanode(mid)
        if m["name"] != name:
            m["name"] = str(name)
            self._changed()

    @undoable
    def add_metanode_port(self, mid, side: str, port_type: str = TABLE) -> int:
        """Add an (unconnected) input or output port; returns its index (from 1)."""
        if side not in ("in", "out"):
            raise WorkflowError("side must be 'in' or 'out'")
        with self._lock:
            m = self.metanode(mid)
            self._add_boundary(m["id"], side, str(port_type))
            self._changed()
            return len(m["inputs" if side == "in" else "outputs"])

    @undoable
    def remove_metanode_port(self, mid, side: str, index: int):
        """Remove port `index` (from 1) and its connections inside and outside."""
        self._require_idle()
        m = self.metanode(mid)
        ports = m["inputs" if side == "in" else "outputs"]
        if not 1 <= int(index) <= len(ports):
            raise WorkflowError(f"Metanode {m['id']} has no {side}put port {index}")
        self.remove_nodes([ports[int(index) - 1]])

    # ------------------------------------------------------------------
    # group annotations
    # ------------------------------------------------------------------
    def group(self, gid) -> dict:
        for g in self.groups:
            if g["id"] == int(gid):
                return g
        raise WorkflowError(f"No group annotation with id {gid}")

    def _group_nodes(self, nodes) -> list:
        ids = []
        for n in nodes:
            if int(n) not in self.metanodes:
                self.node(n)
            if int(n) not in ids:
                ids.append(int(n))
        if not ids:
            raise WorkflowError("A group annotation needs at least one node")
        return ids

    @staticmethod
    def _group_color(color) -> str:
        color = (color or GROUP_COLORS[0]).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise WorkflowError(f"Group colour must look like #rrggbb, not {color!r}")
        return color.lower()

    @undoable
    def add_group(self, nodes, text: str = "", color: str | None = None) -> int:
        """Group annotation around the given nodes; returns its id."""
        with self._lock:
            gid = self.next_group_id
            self.groups.append({"id": gid, "nodes": self._group_nodes(nodes), "text": str(text or ""),
                                "color": self._group_color(color)})
            self.next_group_id += 1
            self._changed()
            return gid

    @undoable
    def update_group(self, gid, text: str | None = None, color: str | None = None, nodes=None):
        with self._lock:
            g = self.group(gid)
            new = dict(g)
            if text is not None:
                new["text"] = str(text)
            if color is not None:
                new["color"] = self._group_color(color)
            if nodes is not None:
                new["nodes"] = self._group_nodes(nodes)
            if new != g:
                g.update(new)
                self._changed()

    @undoable
    def remove_group(self, gid):
        with self._lock:
            g = self.group(gid)
            self.groups.remove(g)
            self._changed()

    @undoable
    def set_settings(self, nid, settings: dict, variables: dict | None = None):
        self._require_idle()
        with self._lock:
            nc = self.node(nid)
            keys = {f.key for f in nc.model.fields}
            new = copy.deepcopy(nc.settings)
            for k, v in (settings or {}).items():
                if k in keys:
                    new[k] = _coerce_setting(v, nc.model.field(k), strict=False)
            if hasattr(nc.model, "normalize_settings"):
                # e.g. a Direct Input forgets its value when its kind or default changes
                new = nc.model.normalize_settings(nc.settings, new)
            new_vars = {}
            for k, b in (variables or {}).items():
                if k in keys and b and (b.get("used") or b.get("exposed")):
                    new_vars[k] = {"used": b.get("used") or None, "exposed": b.get("exposed") or None}
            if new != nc.settings or new_vars != nc.var_settings:
                # e.g. a selection on an output widget: its chart stays while it runs again
                keep = getattr(nc.model, "keeps_display", None)
                display = nc.display if keep and new_vars == nc.var_settings and keep(nc.settings, new) else None
                nc.settings, nc.var_settings = new, new_vars
                self._reset_with_descendants(nc.id)
                if display is not None:
                    nc.display = display
                self.refresh_ports(nc.id)
                self._changed()

    @undoable
    def set_workflow_variables(self, items: list):
        self._require_idle()
        with self._lock:
            names = set()
            out = []
            for d in items:
                name = (d.get("name") or "").strip()
                if not name:
                    continue
                if name in names:
                    raise WorkflowError(f"Duplicate workflow variable: {name}")
                names.add(name)
                out.append(FlowVariable(name, d.get("type", "STRING"), d.get("value", "")))
            self.workflow_variables = out
            for nid in list(self.nodes):
                self._reset_one(nid)
            self._changed()

    def reset(self, ids=None):
        self._require_idle()
        with self._lock:
            for nid in (list(self.nodes) if ids is None else self.expand_ids(ids)):
                self._reset_with_descendants(int(nid))
            self._changed()

    def _reset_one(self, nid):
        nc = self.nodes.get(nid)
        if nc is not None:
            nc.clear()

    def _reset_with_descendants(self, nid):
        self._reset_one(nid)
        for d in self.descendants(nid):
            self._reset_one(d)

    @undoable
    def clear(self):
        """Remove all nodes."""
        if self.nodes:
            self.remove_nodes(list(self.nodes))

    # ------------------------------------------------------------------
    # custom nodes
    # ------------------------------------------------------------------
    def lookup(self, factory: str):
        """Node class for a factory id: this workflow's custom nodes, then built-ins."""
        return self.custom.registry.get(factory) or NODE_REGISTRY.get(factory) or boundary_class(factory)

    def _new_model(self, factory: str):
        cls = self.lookup(factory)
        if cls is None:
            raise WorkflowError(f"Unknown node factory: {factory}")
        return cls()

    def node_types(self) -> list:
        out = [dict(cls.describe(), custom=False) for cls in NODE_REGISTRY.values()]
        out += [dict(cls.describe(), custom=True, source=cls._verknode_source)
                for cls in self.custom.registry.values()]
        return out

    def resolved_node_paths(self) -> list:
        return [os.path.normpath(p if os.path.isabs(p) else os.path.join(self.base_dir, p))
                for p in self.node_paths]

    def set_node_paths(self, paths: list, auto_reload: bool | None = None) -> dict:
        self._require_idle()
        self.node_paths = [p.strip() for p in paths if p and p.strip()]
        if auto_reload is not None:
            self.auto_reload = bool(auto_reload)
        self._changed()
        return self.reload_nodes(reason="node folders changed")

    def reload_nodes(self, reason: str = "reload") -> dict:
        """(Re)import the custom node folders and switch nodes to the new implementations.

        Settings are kept; nodes whose implementation changed are reset.
        """
        self._require_idle()
        with self._lock:
            report = self.custom.load(self.resolved_node_paths(), NODE_REGISTRY)
            for nid, nc in list(self.nodes.items()):
                self._swap_model(nc)
            self.registry_version += 1
            self.custom_report = dict(report, time=dt.datetime.now().strftime("%H:%M:%S"), reason=reason)
            names = ", ".join(n["name"] for n in report["loaded"]) or "none"
            self._log("INFO", f"Custom nodes loaded ({reason}): {names}")
            for err in report["errors"]:
                self._log("ERROR", f"Custom node file {err['file']}:\n{err['error']}")
            self._changed(dirty=False)
            return report

    def _swap_model(self, nc: NodeContainer):
        cls = self.lookup(nc.factory)
        old = nc.model
        if cls is None:
            if not isinstance(old, MissingNodeModel) and getattr(old, "_verknode_custom", False):
                nc.model = MissingNodeModel(nc.factory, old.name, list(old.in_ports), list(old.out_ports))
                self._reset_with_descendants(nc.id)
                nc.message = ("ERROR", f"Custom node implementation not found: {nc.factory}")
            return
        if type(old) is cls:
            return
        new = cls()
        if isinstance(old, MissingNodeModel) or not getattr(old, "_verknode_custom", False) or \
                getattr(type(old), "_verknode_hash", None) != getattr(cls, "_verknode_hash", None):
            changed = True
        else:
            changed = False
        settings = new.default_settings()
        settings.update({k: v for k, v in nc.settings.items() if k in settings})
        nc.model = new
        nc.settings = settings
        nc.var_settings = {k: v for k, v in nc.var_settings.items() if k in settings}
        if changed:
            self._reset_with_descendants(nc.id)

    # ------------------------------------------------------------------
    # copy / paste
    # ------------------------------------------------------------------
    def copy(self, ids) -> dict:
        """Fragment with the given nodes and metanodes (with their contents), their settings,
        positions and the connections between them."""
        items = [int(i) for i in ids]
        metas = set()
        for i in items:
            if i in self.metanodes:
                metas |= {i} | self._inner_metanodes(i)
        node_ids = [i for i in items if i not in self.metanodes]
        node_ids += sorted(self.expand_ids(i for i in items if i in self.metanodes))
        node_ids = list(dict.fromkeys(node_ids))
        chosen = set(node_ids)
        return {
            "format": "verknode-fragment/1",
            "nodes": [
                {
                    "id": nid,
                    "factory": self.nodes[nid].factory,
                    "settings": copy.deepcopy(self.nodes[nid].settings),
                    "variables": copy.deepcopy(self.nodes[nid].var_settings),
                    "annotation": self.nodes[nid].annotation,
                    "widget_layout": copy.deepcopy(self.nodes[nid].widget_layout),
                    "x": self.nodes[nid].x,
                    "y": self.nodes[nid].y,
                    "parent": self.nodes[nid].parent,
                }
                for nid in node_ids
            ],
            "metanodes": [copy.deepcopy(self.metanodes[m]) for m in sorted(metas)],
            "connections": [c.to_json() for c in self.connections
                            if c.source_id in chosen and c.dest_id in chosen],
            # groups whose nodes are all copied come along
            "groups": [copy.deepcopy(g) for g in self.groups if set(g["nodes"]) <= chosen | metas],
        }

    @undoable
    def paste(self, fragment: dict, dx: float = 40, dy: float = 40, parent=None) -> list:
        """Insert a copied fragment with new ids (inside metanode `parent`, if given).

        Returns the ids of the pasted top-level nodes and metanodes.
        """
        if not isinstance(fragment, dict) or fragment.get("format") != "verknode-fragment/1":
            raise WorkflowError("Clipboard does not contain verknode nodes")
        for n in fragment["nodes"]:
            if self.lookup(n["factory"]) is None:
                raise WorkflowError(f"Unknown node factory: {n['factory']}")
        if parent is not None:
            parent = self.metanode(parent)["id"]
        with self._lock:
            mapping = {}
            for m in fragment.get("metanodes", []):
                mapping[int(m["id"])] = self.next_id
                self.next_id += 1
            new_metas = []
            for m in fragment.get("metanodes", []):
                mid = mapping[int(m["id"])]
                self.metanodes[mid] = {"id": mid, "name": m.get("name", "Metanode"),
                                       "parent": mapping.get(m.get("parent"), parent),
                                       "x": m["x"] + dx, "y": m["y"] + dy, "inputs": [], "outputs": []}
                new_metas.append((m, mid))
            for n in fragment["nodes"]:
                nid = self.add_node(n["factory"], n["x"] + dx, n["y"] + dy)
                nc = self.nodes[nid]
                nc.parent = mapping.get(n.get("parent"), parent)
                keys = {f.key for f in nc.model.fields}
                nc.settings.update({k: copy.deepcopy(v) for k, v in n.get("settings", {}).items() if k in keys})
                nc.var_settings = {k: dict(v) for k, v in (n.get("variables") or {}).items() if k in keys}
                old = int(n["id"])
                annotation = n.get("annotation", "")
                nc.annotation = f"Node {nid}" if annotation == f"Node {old}" else annotation
                nc.widget_layout = clean_widget_layout(n.get("widget_layout"), (n["x"], n["y"]), (dx, dy))
                self.refresh_ports(nid)
                mapping[old] = nid
            for m, mid in new_metas:
                for key in ("inputs", "outputs"):
                    self.metanodes[mid][key] = [mapping[b] for b in m.get(key, []) if b in mapping]
            for c in fragment.get("connections", []):
                if c["sourceID"] in mapping and c["destID"] in mapping:
                    self.connect(mapping[c["sourceID"]], c["sourcePort"], mapping[c["destID"]], c["destPort"])
            for g in fragment.get("groups", []):
                members = [mapping[int(n)] for n in g.get("nodes", []) if int(n) in mapping]
                if members:
                    self.add_group(members, g.get("text", ""), g.get("color"))
            self._changed()
            top = [mapping[int(n["id"])] for n in fragment["nodes"] if self.nodes[mapping[int(n["id"])]].parent == parent]
            return top + [mid for _, mid in new_metas if self.metanodes[mid]["parent"] == parent]

    # ------------------------------------------------------------------
    # undo / redo
    # ------------------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self):
        self._require_idle()
        if not self._undo:
            raise WorkflowError("Nothing to undo")
        with self._lock:
            current = self._snapshot()
            self._restore(self._undo.pop())
            self._redo.append(current)

    def redo(self):
        self._require_idle()
        if not self._redo:
            raise WorkflowError("Nothing to redo")
        with self._lock:
            current = self._snapshot()
            self._restore(self._redo.pop())
            self._undo.append(current)

    def _fingerprints(self) -> dict:
        """Per node hash of its configuration and everything upstream.

        Equal fingerprints mean equal results, so undo/redo can keep
        executed outputs of nodes that are not affected.
        """
        wf_vars = [v.to_json() for v in self.workflow_variables]
        fps = {}
        for nid in self.topo_order():
            nc = self.nodes[nid]
            inputs = sorted((p, c.source_port, fps[c.source_id]) for p, c in self.in_conns(nid).items())
            impl = getattr(type(nc.model), "_verknode_hash", "")
            payload = json.dumps([nc.factory, impl, nc.settings, nc.var_settings, inputs, wf_vars],
                                 sort_keys=True, default=str)
            fps[nid] = hashlib.sha1(payload.encode()).hexdigest()
        return fps

    _STATE = ("outputs", "out_stack", "inactive", "message", "view", "display", "exec_time", "rows", "traceback")

    def _snapshot(self) -> dict:
        fps = self._fingerprints()
        return {
            "nodes": [
                {
                    "id": nid,
                    "factory": nc.factory,
                    "model": nc.model,
                    "settings": copy.deepcopy(nc.settings),
                    "var_settings": copy.deepcopy(nc.var_settings),
                    "x": nc.x,
                    "y": nc.y,
                    "parent": nc.parent,
                    "annotation": nc.annotation,
                    "custom_description": nc.custom_description,
                    "widget_layout": copy.deepcopy(nc.widget_layout),
                    "last": (nc.last_time, nc.last_rows),
                    "fingerprint": fps[nid],
                    "state": {k: getattr(nc, k) for k in self._STATE},
                }
                for nid, nc in self.nodes.items()
            ],
            "connections": [c.key() for c in self.connections],
            "groups": copy.deepcopy(self.groups),
            "next_group_id": self.next_group_id,
            "metanodes": copy.deepcopy(self.metanodes),
            "workflow_variables": [v.to_json() for v in self.workflow_variables],
            "next_id": self.next_id,
        }

    def _restore(self, snap: dict):
        current, current_fps = self.nodes, self._fingerprints()
        self.nodes = {}
        for ns in snap["nodes"]:
            cls = self.lookup(ns["factory"])
            model = ns["model"] if cls is None or isinstance(ns["model"], cls) else cls()
            nc = NodeContainer(ns["id"], ns["factory"], ns["x"], ns["y"], model=model)
            nc.settings = copy.deepcopy(ns["settings"])
            nc.var_settings = copy.deepcopy(ns["var_settings"])
            nc.annotation = ns["annotation"]
            nc.custom_description = ns["custom_description"]
            nc.widget_layout = copy.deepcopy(ns.get("widget_layout"))
            # (the newest timings, also of nodes the undo brings back)
            cur = current.get(ns["id"])
            nc.last_time, nc.last_rows = (cur.last_time, cur.last_rows) if cur else ns.get("last", (None, None))
            nc.parent = ns.get("parent")
            self.nodes[ns["id"]] = nc
        self.connections = [Connection(*k) for k in snap["connections"]]
        self.groups = copy.deepcopy(snap.get("groups", []))
        self.metanodes = copy.deepcopy(snap.get("metanodes", {}))
        self.next_group_id = max(self.next_group_id, snap.get("next_group_id", 1))
        self.workflow_variables = [FlowVariable.from_json(v) for v in snap["workflow_variables"]]
        self.next_id = max(self.next_id, snap["next_id"])
        fps = self._fingerprints()
        by_id = {ns["id"]: ns for ns in snap["nodes"]}
        for nid, nc in self.nodes.items():
            cur = current.get(nid)
            if cur is not None and current_fps.get(nid) == fps[nid]:
                state = {k: getattr(cur, k) for k in self._STATE}
            elif by_id[nid]["fingerprint"] == fps[nid]:
                state = by_id[nid]["state"]
            else:
                continue
            for k, v in state.items():
                setattr(nc, k, v)
        self._changed()

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def execute(self, ids=None, wait=False):
        self._require_idle()
        if self.auto_reload and self.node_paths and self.custom.changed(self.resolved_node_paths()):
            self.reload_nodes(reason="changed files")
        with self._lock:
            targets = list(self.nodes) if ids is None else sorted(self.expand_ids(ids))
            self._reset_changed_externals()
            plan, loops = self._plan(targets)
            if not plan:
                return
            for nid in plan:
                self.nodes[nid].runtime = "QUEUED"
            self._run_order = self.topo_order(plan)  # (for the ETAs: nodes run one after another)
            self._cancel.clear()
            self._changed()
            self._thread = threading.Thread(target=self._run_plan, args=(plan, loops), daemon=True)
            self._thread.start()
        if wait:
            self._thread.join()

    def refresh_ports(self, nid) -> bool:
        """Let a node with ports of its own (e.g. from its notebook) update them; connections to
        ports that are gone are removed."""
        nc = self.nodes[nid]
        if not hasattr(nc.model, "update_ports") or not nc.model.update_ports(nc.settings, self.base_dir):
            return False
        n_in, n_out = len(nc.model.in_ports), len(nc.model.out_ports)
        gone = [c for c in self.connections
                if (c.dest_id == nid and c.dest_port > n_in) or (c.source_id == nid and c.source_port > n_out)]
        self.connections = [c for c in self.connections if c not in gone]
        self._reset_with_descendants(nid)
        for c in gone:
            self._reset_with_descendants(c.dest_id)
        self._log("INFO", f"{nc.name} {_label(nc)}: now {n_in} input / {n_out} output ports", nid)
        self._changed(dirty=bool(gone))
        return True

    def check_files(self):
        """Look at the files nodes read: changed ones update the node's ports and reset an
        executed node (e.g. a saved notebook). Called while editors are connected."""
        if self.executing:
            return
        with self._lock:
            for nid, nc in list(self.nodes.items()):
                if not hasattr(nc.model, "external_state"):
                    continue
                state = nc.model.external_state(nc.settings, self.base_dir)
                if state == nc.watched_state:
                    continue
                nc.watched_state = state
                self.refresh_ports(nid)
                if nc.executed and nc.external_state is not None and state != nc.external_state:
                    self._log("INFO", f"{nc.name} {_label(nc)}: its file changed, reset", nid)
                    self._reset_with_descendants(nid)
                    self._changed(dirty=False)

    def _reset_changed_externals(self):
        """Reset executed nodes whose files changed since (e.g. an edited notebook)."""
        for nid, nc in list(self.nodes.items()):
            if hasattr(nc.model, "update_ports"):
                self.refresh_ports(nid)
            if nc.executed and nc.external_state is not None and hasattr(nc.model, "external_state"):
                if nc.model.external_state(nc.settings, self.base_dir) != nc.external_state:
                    self._log("INFO", f"{nc.name} {_label(nc)}: its file changed, reset", nid)
                    self._reset_with_descendants(nid)

    # ------------------------------------------------------------------
    # requests to an open editor (e.g. run a notebook in its JupyterLab tab)
    # ------------------------------------------------------------------
    def frontend_request(self, payload: dict, claim_timeout: float = 4.0, canceled=None):
        """Ask an open editor to do something (it claims the request, then reports back).

        Returns the editor's result, or None when no editor claimed it in time.
        """
        if time.time() - self.editor_seen > 30:
            return None  # no editor connected
        req = {"id": uuid.uuid4().hex, **payload, "status": "pending", "result": None,
               "claimed": threading.Event(), "done": threading.Event()}
        self._frontend[req["id"]] = req
        self._changed(dirty=False)
        try:
            deadline = time.time() + claim_timeout
            while not req["claimed"].wait(0.1):
                if (canceled and canceled()) or time.time() > deadline:
                    return None
            while not req["done"].wait(0.2):
                if canceled and canceled() and req["status"] != "cancel":
                    req["status"] = "cancel"
                    self._changed(dirty=False)
            if req["status"] == "cancel":
                raise CanceledExecutionError("Execution canceled")
            return req["result"]
        finally:
            self._frontend.pop(req["id"], None)
            self._changed(dirty=False)

    def frontend_reply(self, rid: str, action: str, data: dict | None = None) -> bool:
        req = self._frontend.get(rid)
        if req is None:
            return False
        if action == "claim":
            if req["status"] != "pending":
                return False
            req["status"] = "claimed"
            req["claimed"].set()
            self._changed(dirty=False)
            return True
        if action == "done":
            req["result"] = data or {}
            req["done"].set()
            return True
        raise WorkflowError(f"Unknown reply {action}")

    def cancel(self):
        self._cancel.set()
        self._changed(dirty=False)

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _plan(self, targets) -> tuple[set, dict]:
        plan, loops, forced = set(), {}, set()
        todo = list(targets)
        while todo:
            nid = todo.pop()
            if nid in plan:
                continue
            nc = self.nodes[nid]
            if nc.executed and nid not in forced:
                continue
            plan.add(nid)
            todo.extend(c.source_id for c in self.in_conns(nid).values())
            if nc.model.loop_role == "end":
                start = self.find_loop_start(nid)
                if start is not None:
                    body = self.loop_body(start, nid)
                    loops[nid] = (start, body)
                    for b in body | {start}:
                        if b not in plan:
                            forced.add(b)
                            todo.append(b)
        return plan, loops

    def _run_plan(self, plan, loops):
        t0 = time.time()
        depth, self._next_run_depth = self._next_run_depth, 0
        self._run_depth = depth
        try:
            self._run(plan, loops)
        except CanceledExecutionError:
            self._log("WARN", "Execution canceled")
        except BaseException as e:  # noqa: BLE001
            self._log("ERROR", f"Execution failed: {e or type(e).__name__}")
        finally:
            for nc in self.nodes.values():
                nc.runtime = None
            self._changed()
            self._log("INFO", f"Execution finished in {time.time() - t0:.2f}s")
            # Workflow Script edits wait for the end of the run (in a thread of their own: this
            # one still counts as executing)
            edits, self._after_run = self._after_run, []
            if edits:
                threading.Thread(target=self._apply_after_run, args=(edits, threading.current_thread()), daemon=True).start()

    # ------------------------------------------------------------------
    # Workflow Script nodes: edits of the workflow when the run ends
    # ------------------------------------------------------------------
    _after_run: list = []
    _run_depth = 0
    _next_run_depth = 0

    def _script_workflow(self, nid):
        from .workflow_edits import ScriptWorkflow

        def record(fn, what):
            if self._run_depth:
                self._log("WARN", f"Workflow Script {_label(self.nodes[nid])}: edits of a run that a Workflow Script "
                                  f"started are ignored (no loops) - {what}", nid)
                return
            if not self._after_run:
                self._after_run = []
            self._after_run.append((fn, what, nid))
        return ScriptWorkflow(self, nid, record)

    def _apply_after_run(self, edits, run_thread):
        run_thread.join()
        try:
            request = self._apply_script_edits(edits)
        except Exception as e:  # noqa: BLE001
            self._log("ERROR", f"Workflow Script edits failed: {e}")
            return
        if request is not None:
            ids, nid = request
            try:
                self._next_run_depth = 1
                self.execute(ids)
            except Exception as e:  # noqa: BLE001
                self._next_run_depth = 0
                self._log("ERROR", f"Workflow Script: execute: {e}", nid)

    @undoable
    def _apply_script_edits(self, edits):
        """Apply the recorded edits in order (one undo step); stops at the first that fails.
        Returns the run asked for afterwards: (node ids or None for all, script node), or None."""
        request = None
        for fn, what, nid in edits:
            try:
                if isinstance(fn, tuple):  # ("execute", node refs)
                    refs = fn[1]
                    ids = [r._sw._id(r) for r in refs] if refs else None  # (None: everything)
                    if request is None:
                        request = (ids, nid)
                    elif request[0] is None or ids is None:
                        request = (None, nid)
                    else:
                        request = (request[0] + ids, nid)
                    continue
                fn(self)
            except Exception as e:  # noqa: BLE001
                self._log("ERROR", f"Workflow Script {_label(self.nodes[nid]) if nid in self.nodes else nid}: {what}: {e}", nid)
                break
        n = sum(1 for fn, _, _ in edits if not isinstance(fn, tuple))
        if n:
            self._log("INFO", f"Workflow Script: applied {n} edit(s)")
        return request

    def _run(self, subset, loops):
        deferred = set()
        for end, (start, body) in loops.items():
            if end in subset:
                deferred |= body | {start}
        for nid in self.topo_order(subset):
            if self._cancel.is_set():
                raise CanceledExecutionError()
            nc = self.nodes[nid]
            if nid in deferred or nc.executed:
                continue
            if nid in loops:
                start, body = loops[nid]
                if not self._inputs_ready(start):
                    for n in body | {start, nid}:
                        self.nodes[n].runtime = None
                    continue
                inner = {e: v for e, v in loops.items() if e in body}
                if getattr(nc.model, "parallel", False):
                    self._run_parallel(start, body, nid, inner)
                else:
                    self._run_loop(start, body, nid, inner)
            elif self._inputs_ready(nid):
                self._execute_single(nid)
            elif nc.model.scope_role == "catch" and (caught := self._caught_error(nid)) is not None:
                self._execute_single(nid, caught=caught)
            else:
                nc.runtime = None
                self.version += 1

    def _inputs_ready(self, nid) -> bool:
        nc = self.nodes[nid]
        conns = self.in_conns(nid)
        for idx in range(len(nc.model.in_ports) + 1):
            c = conns.get(idx)
            if c is None:
                if idx > 0 and not nc.in_port(idx).optional:
                    nc.message = ("WARNING", f'Input port "{nc.in_port(idx).name}" is not connected')
                    return False
                continue
            if not self.nodes[c.source_id].executed:
                return False
        return True

    def _run_loop(self, start, body, end, inner_loops):
        loop = {"iteration": 0, "last": False, "collected": [], "state": {}, "started": time.time()}
        end_nc = self.nodes[end]
        end_nc.started, end_nc.progress = loop["started"], None
        loop["start"] = start
        end_nc.loop_state = loop
        try:
            self._loop_iterations(start, body, end, inner_loops, loop, end_nc)
        finally:
            end_nc.loop_state = None

    def _loop_iterations(self, start, body, end, inner_loops, loop, end_nc):
        while True:
            if self._cancel.is_set():
                end_nc.message = ("WARNING", "Loop canceled")
                raise CanceledExecutionError()
            for b in body | {start}:
                self.nodes[b].clear()
                self.nodes[b].runtime = "QUEUED"
            end_nc.runtime = "EXECUTING"
            if not self._execute_single(start, loop):
                end_nc.message = ("ERROR", "Loop start failed")
                return
            self._run(set(body), inner_loops)
            if not self._inputs_ready(end):
                end_nc.message = ("ERROR", f"Loop body failed in iteration {loop['iteration']}")
                return
            if not self._execute_single(end, loop):
                return
            if loop["last"]:
                end_nc.last_iterations = loop["iteration"] + 1
                return
            loop["iteration"] += 1
            total = loop.get("total") or end_nc.last_iterations  # (from the loop start, else the last run)
            if total:
                end_nc.progress = min(loop["iteration"] / total, 1.0)

    # ------------------------------------------------------------------
    # parallel chunks (Parallel Chunk Start / End)
    # ------------------------------------------------------------------
    def _run_parallel(self, start, body, end, inner_loops):
        """The start splits its table into chunks; the visible body runs the first one, and a
        temporary metanode next to it holds a copy of the body for every other chunk (inspect it
        while it runs). All chunks run at the same time; the end joins the results and the
        temporary metanode goes away."""
        from concurrent.futures import ThreadPoolExecutor

        end_nc = self.nodes[end]
        # ("iteration" + 1 is what the canvas pill shows: the chunks done)
        loop = {"iteration": -1, "last": True, "collected": [], "state": {}, "started": time.time(), "start": start}
        end_nc.started, end_nc.progress, end_nc.loop_state = loop["started"], None, loop
        temp = None
        try:
            if inner_loops:
                end_nc.message = ("ERROR", "Loops inside a parallel chunk loop are not supported")
                return
            for b in body | {start}:
                self.nodes[b].clear()
                self.nodes[b].runtime = "QUEUED"
            end_nc.runtime = "EXECUTING"
            if not self._execute_single(start, loop):
                end_nc.message = ("ERROR", "Parallel Chunk Start failed")
                return
            chunks = loop.get("chunks") or []
            temp, copies = self._parallel_copies(start, body, chunks) if len(chunks) > 1 else (None, [])
            done, errors = [0], []
            lock = threading.Lock()

            def finished():
                with lock:
                    done[0] += 1
                    loop["iteration"] = done[0] - 1
                    end_nc.progress = done[0] / len(chunks)
                    self.version += 1

            def work(k, mapping):
                try:
                    for cid in self.topo_order(set(mapping.values())):
                        if self._cancel.is_set():
                            raise CanceledExecutionError()
                        if self._inputs_ready(cid) and not self._execute_single(cid):
                            raise WorkflowError(self.nodes[cid].message[1] if self.nodes[cid].message else "failed")
                except CanceledExecutionError:
                    raise
                except Exception as e:  # noqa: BLE001 - reported on the end node
                    errors.append((k, e))
                finally:
                    finished()

            with ThreadPoolExecutor(max_workers=max(1, min(loop.get("workers", 8), len(chunks)))) as pool:
                futures = [pool.submit(work, k, m) for k, m in enumerate(copies, start=1)]
                try:
                    self._run(set(body), {})  # the first chunk, through the visible nodes
                finally:
                    for f in futures:
                        f.result()
            finished()
            if errors:
                k, e = min(errors, key=lambda x: x[0])
                end_nc.message = ("ERROR", f"Chunk {k + 1} of {len(chunks)}: {e}")
                self._log("ERROR", f"{end_nc.name} {_label(end_nc)}: chunk {k + 1}: {e}", end)
                return
            if not self._inputs_ready(end):
                end_nc.message = ("ERROR", "The first chunk failed")
                return
            c = self.in_conns(end).get(1)
            results = []
            for mapping in copies:
                src = self.nodes[mapping.get(c.source_id, c.source_id)]
                results.append(src.outputs[c.source_port])
            loop["results"] = results
            loop["iteration"] = len(chunks) - 1
            self._execute_single(end, loop)
        finally:
            end_nc.loop_state = None
            if temp is not None:
                self._drop_temporary(temp)

    def _parallel_copies(self, start, body, chunks) -> tuple:
        """A temporary metanode below the loop with a copy of the body for chunks 2..n. One port
        per output of the start carries the whole table into it and branches out to the copies,
        each of which gets its own chunk (and chunk flow variables); other inputs of the body come
        in through ports of their own. Returns (metanode id, [{body node:
        copy}] per chunk). Built aside and swapped in at once: views read while this runs."""
        with self._lock:
            start_nc = self.nodes[start]
            order = self.topo_order(set(body))
            ys = [self.nodes[n].y for n in order] or [start_nc.y]
            xs = [self.nodes[n].x for n in order] or [start_nc.x]
            top = min(ys)
            mid = self.next_id
            self.next_id += 1
            meta = {"id": mid, "name": f"Parallel chunks 2-{len(chunks)} (temporary)", "parent": start_nc.parent,
                    "x": sum(xs) / len(xs), "y": max(ys) + 150, "inputs": [], "outputs": [], "temporary": True}
            nodes, conns = {}, []
            row = max(ys) - top + 130  # each copy one row below the other inside the metanode

            def port(source, source_port, outputs, stack):
                nonlocal nodes
                factory = boundary_factory("in", self.nodes[source].out_port(source_port).type)
                b = self.next_id
                self.next_id += 1
                nc = NodeContainer(b, factory, model=boundary_class(factory)())
                nc.parent = mid
                nc.outputs, nc.out_stack = outputs, stack  # (already "executed": it passes its table on)
                nodes[b] = nc
                meta["inputs"].append(b)
                conns.append(Connection(source, source_port, b, 1))
                return b

            shared = {}
            whole = self._collect_inputs(start)[0][0]  # the table the start splits
            for c in self.connections:
                if c.source_id == start and c.dest_id in body and (start, c.source_port) not in shared:
                    shared[(start, c.source_port)] = port(start, c.source_port,
                                                          [FLOWVAR, whole if c.source_port == 1 else FLOWVAR], start_nc.out_stack)
            copies = []
            for k in range(1, len(chunks)):
                stack = dict(start_nc.out_stack or {})
                stack["currentIteration"] = make("currentIteration", k, "INTEGER")
                stack["maxIterations"] = make("maxIterations", len(chunks), "INTEGER")
                mapping = {}
                for orig in order:
                    onc = self.nodes[orig]
                    cid = self.next_id
                    self.next_id += 1
                    nc = NodeContainer(cid, onc.factory, onc.x, onc.y - top + 60 + row * (k - 1), model=copy.copy(onc.model))
                    nc.settings = copy.deepcopy(onc.settings)
                    nc.var_settings = copy.deepcopy(onc.var_settings)
                    nc.parent = mid
                    nc.annotation = f"{onc.annotation} \u00b7 chunk {k + 1}"
                    nc.runtime = "QUEUED"
                    nodes[cid] = nc
                    mapping[orig] = cid
                for c in self.connections:
                    if c.dest_id not in mapping:
                        continue
                    if c.source_id in mapping:
                        conns.append(Connection(mapping[c.source_id], c.source_port, mapping[c.dest_id], c.dest_port))
                        continue
                    if c.source_id == start:
                        dest = nodes[mapping[c.dest_id]]
                        dest.fed = {**(dest.fed or {}), c.dest_port: (chunks[k] if c.source_port == 1 else FLOWVAR, stack)}
                    key = (c.source_id, c.source_port)
                    if key not in shared:
                        o = self.nodes[c.source_id]
                        shared[key] = port(c.source_id, c.source_port,
                                           [FLOWVAR, o.outputs[c.source_port]] if o.outputs else None, o.out_stack)
                    conns.append(Connection(shared[key], 1, mapping[c.dest_id], c.dest_port))
                copies.append(mapping)
            self.nodes = {**self.nodes, **nodes}
            self.connections = self.connections + conns
            self.metanodes = {**self.metanodes, mid: meta}
            self._changed(dirty=False)
            return mid, copies

    def _drop_temporary(self, mid):
        """Remove a temporary metanode with everything in it (also swapped in at once)."""
        with self._lock:
            gone = {nid for nid, nc in self.nodes.items() if nc.parent == mid}
            self.connections = [c for c in self.connections if c.source_id not in gone and c.dest_id not in gone]
            self.nodes = {nid: nc for nid, nc in self.nodes.items() if nid not in gone}
            self.metanodes = {m: v for m, v in self.metanodes.items() if m != mid}
            self._changed(dirty=False)

    def _collect_inputs(self, nid):
        nc = self.nodes[nid]
        conns = self.in_conns(nid)
        inputs, port_stacks, any_inactive = [], [], False
        for idx in range(len(nc.model.in_ports) + 1):
            c = conns.get(idx)
            if c is not None and nc.fed and idx in nc.fed:
                obj, fed_stack = nc.fed[idx]
                port_stacks.append((idx, fed_stack))
                if idx > 0:
                    inputs.append(obj)
                continue
            if c is None:
                if idx > 0:
                    inputs.append(None)
                continue
            src = self.nodes[c.source_id]
            if src.outputs is None:  # failed branch of a Catch Errors node
                if idx > 0:
                    inputs.append(None)
                continue
            obj = src.outputs[c.source_port]
            port_stacks.append((idx, src.out_stack or {}))
            if obj is INACTIVE:
                any_inactive = True
            if idx > 0:
                inputs.append(obj)
        return inputs, self._merge_stacks(port_stacks), any_inactive

    def _execute_single(self, nid, loop=None, caught=None) -> bool:
        nc = self.nodes[nid]
        m = nc.model
        nc.runtime = "EXECUTING"
        nc.message = None
        self.version += 1
        t0 = time.time()
        # (a loop end runs once per iteration; it is "executing" since the loop started)
        nc.started = loop["started"] if m.loop_role == "end" and loop is not None else t0
        if m.loop_role != "end" or loop is None or loop["iteration"] == 0:
            nc.progress = None
        inputs, stack, any_inactive = self._collect_inputs(nid)
        try:
            if any_inactive and not m.handles_inactive:
                nc.outputs = [INACTIVE] * (len(m.out_ports) + 1)
                nc.out_stack = stack
                nc.inactive = True
                return True
            settings, exposed = self._resolve_settings(nc, stack)
            if m.loop_role == "start" and loop is None:
                loop = {"iteration": 0, "last": False, "collected": [], "state": {}}
            if m.loop_role == "end" and loop is None:
                raise WorkflowError("No matching Loop Start node found")
            ctx = ExecContext(stack, self.base_dir, self._cancel, loop,
                              logger=lambda level, text: self._log(level, text, nid))
            ctx.caught_error = caught
            ctx.on_progress = lambda f, _nc=nc: setattr(_nc, "progress", f)
            ctx.script_workflow = lambda _nid=nid: self._script_workflow(_nid)
            ctx.workflow_path = self.path
            ctx.workflow_context = self.dialog_context(nid)
            ctx.frontend_request = lambda payload, timeout=4.0, _ctx=ctx: self.frontend_request(
                dict(payload, node=nid), timeout, _ctx.canceled)
            in_specs = [_spec_of_output(i) if i is not None else None for i in inputs]
            m.validate(settings, in_specs)
            if getattr(m, "_verknode_custom", False):
                with capture_print(ctx):
                    outs = m.execute(inputs, settings, ctx)
            else:
                outs = m.execute(inputs, settings, ctx)
            if outs is None:
                if m.loop_role == "end" and not loop["last"]:
                    nc.runtime = "EXECUTING"
                    return True
                raise WorkflowError("Node produced no output")
            outs = list(outs)
            if len(outs) != len(m.out_ports):
                raise WorkflowError(f"Node produced {len(outs)} outputs, expected {len(m.out_ports)}")
            for i, (port, obj) in enumerate(zip(m.out_ports, outs)):
                outs[i] = _normalize_output(port, obj)
            nc.inactive = bool(outs) and all(o is INACTIVE for o in outs)
            nc.outputs = [INACTIVE if nc.inactive else FLOWVAR] + outs
            out_stack = dict(stack)
            out_stack.update(exposed)
            out_stack.update(ctx.pushed)
            nc.out_stack = out_stack
            nc.view = ctx.view
            nc.display = ctx.display
            nc.exec_time = time.time() - t0
            if m.loop_role == "end" and loop is not None:
                nc.exec_time = time.time() - loop["started"]  # the whole loop
            nc.rows = self._rows_of(nid)
            if not (loop is not None and m.loop_role != "end"):
                nc.last_time, nc.last_rows = nc.exec_time, nc.rows
            if hasattr(m, "external_state"):
                nc.external_state = m.external_state(nc.settings, self.base_dir)
            if ctx.warning:
                nc.message = ("WARNING", ctx.warning)
            if (loop is None or m.loop_role == "end") and not is_boundary(m):
                self._log("INFO", f"{nc.name} {_label(nc)} executed in {nc.exec_time:.2f}s", nid)
            return True
        except CanceledExecutionError:
            nc.clear()
            nc.message = ("WARNING", "Execution canceled")
            raise
        except BaseException as e:  # noqa: BLE001 - incl. polars/pyo3 PanicException (a BaseException)
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            nc.clear()
            # nodes may leave a view of what failed (e.g. the executed notebook)
            failed_ctx = locals().get("ctx")
            if getattr(m, "view_on_error", False) and failed_ctx is not None and failed_ctx.view:
                nc.view = failed_ctx.view
            text = str(e) or type(e).__name__
            nc.message = ("ERROR", f"Execute failed: {text}")
            self._log("ERROR", f"{nc.name} {_label(nc)}: Execute failed: {text}", nid)
            nc.traceback = traceback.format_exc()
            self._log("DEBUG", nc.traceback, nid)
            return False
        finally:
            if nc.runtime == "EXECUTING" and not (m.loop_role == "end" and nc.outputs is None and loop and not loop["last"]):
                nc.runtime = None
            self._changed()

    # ------------------------------------------------------------------
    # run times
    # ------------------------------------------------------------------
    _run_order: list = []

    def _rows_of(self, nid) -> int | None:
        """Rows a node works on: its first connected input table, else its first output table."""
        nc = self.nodes[nid]
        for idx, c in sorted(self.in_conns(nid).items()):
            src = self.nodes[c.source_id]
            obj = src.outputs[c.source_port] if idx and src.outputs else None
            if isinstance(obj, pl.DataFrame):
                return obj.height
        for obj in (nc.outputs or [])[1:]:
            if isinstance(obj, pl.DataFrame):
                return obj.height
        return None

    def _estimate(self, nid) -> float | None:
        """Run time of a node: measured, or expected from its last run (scaled by its input rows)."""
        nc = self.nodes[nid]
        if nc.executed:
            return nc.exec_time
        if nc.last_time is None:
            return None
        rows = self._rows_of(nid)
        if rows is not None and nc.last_rows:
            return nc.last_time * rows / nc.last_rows
        return nc.last_time

    def _loop_pills(self) -> dict:
        """Loop start and end nodes of running loops -> {"iteration" (1-based), "total", "estimated"}
        (total: from the loop start, else the last run's count; None: unknown)."""
        out = {}
        for nid, nc in self.nodes.items():
            loop = nc.loop_state
            if loop is None:
                continue
            total, estimated = loop.get("total"), False
            if not total and nc.last_iterations:
                total, estimated = nc.last_iterations, True
            pill = {"iteration": loop["iteration"] + 1, "total": total, "estimated": estimated}
            out[nid] = out[loop["start"]] = pill
        return out

    def _loop_members(self) -> dict:
        """Loop start and body nodes -> their loop end (whose time covers the whole loop)."""
        out = {}
        for nid, nc in self.nodes.items():
            if nc.model.loop_role == "end":
                start = self.find_loop_start(nid)
                if start is not None:
                    for b in self.loop_body(start, nid) | {start}:
                        out.setdefault(b, nid)
        return out

    def _sum_times(self, ids, members) -> tuple[float, bool, bool]:
        """(total, estimated: not all executed, partial: some without any time)."""
        total, estimated, partial = 0.0, False, False
        for i in ids:
            if members.get(i) in ids:
                continue  # inside a loop: counted with its end
            t = self._estimate(i)
            estimated |= not self.nodes[i].executed
            if t is None:
                partial = True
            else:
                total += t
        return total, estimated, partial

    def _timings(self, analysis) -> dict:
        """Per node: its run time and time per row, an ETA (configured: running it with the
        nodes before it that are not executed; executing / queued: until it is done), and for
        start nodes the time of everything after them, for end nodes of everything before."""
        members = self._loop_members()
        out = {}
        running = {}
        if self.executing:
            # nodes run one after another: each one's ETA adds the remaining times before it
            acc, partial, behind, now = 0.0, False, False, time.time()
            for nid in self._run_order:
                nc = self.nodes.get(nid)
                if nc is None or not nc.runtime or nid in members:
                    continue
                est = self._estimate(nid)
                info = {"estimate": est, "elapsed": None, "progress": None, "overdue": False, "slow": False, "late": False}
                rem = est
                if nc.runtime == "EXECUTING" and nc.started:
                    el, p = now - nc.started, nc.progress
                    info["elapsed"], info["progress"] = el, p
                    by_progress = el * (1 - p) / p if p is not None and p > 0.02 and el > 0.5 else None
                    by_estimate = max(est - el, 0.0) if est is not None else None
                    if by_progress is not None and by_estimate is not None:
                        rem = p * by_progress + (1 - p) * by_estimate  # trust the progress more as it advances
                    elif by_progress is not None or by_estimate is not None:
                        rem = by_progress if by_progress is not None else by_estimate
                    if est:
                        info["overdue"] = el > est * 1.2 + 0.5
                        info["slow"] = not info["overdue"] and p is not None and el > 1 and p < 0.75 * min(el / est, 1.0)
                    behind = behind or info["overdue"] or info["slow"]
                else:
                    info["late"] = behind  # waits for nodes that take longer than expected
                if rem is None:
                    partial = True
                else:
                    acc += rem
                running[nid] = (acc, partial, info)
        for nid, nc in self.nodes.items():
            if is_boundary(nc.model):
                continue
            t = {"time": None, "rows": None, "perRow": None, "eta": None, "etaPartial": False,
                 "total": None, "totalKind": None, "totalEstimated": False, "totalPartial": False}
            if nc.executed and nc.exec_time is not None:
                t["time"], t["rows"] = nc.exec_time, nc.rows
                if nc.rows:
                    t["perRow"] = nc.exec_time / nc.rows
            if nid in running:
                t["eta"], t["etaPartial"], info = running[nid]
                t.update(info)
            elif not nc.runtime and not nc.executed and self.node_state(nc, analysis.get(nid)) == "CONFIGURED":
                ids = {nid} | {a for a in self.ancestors(nid) if not self.nodes[a].executed}
                total, _, partial = self._sum_times(ids, members)
                t["eta"], t["etaPartial"] = total, partial
            before, after = self.ancestors(nid), self.descendants(nid)
            if not before and after:
                t["totalKind"] = "workflow"
                t["total"], t["totalEstimated"], t["totalPartial"] = self._sum_times({nid} | after, members)
            elif before and not after:
                t["totalKind"] = "branch"
                t["total"], t["totalEstimated"], t["totalPartial"] = self._sum_times({nid} | before, members)
            out[nid] = t
        return out

    # ------------------------------------------------------------------
    # views for the frontend
    # ------------------------------------------------------------------
    def node_state(self, nc: NodeContainer, a: dict | None) -> str:
        if nc.runtime:
            return nc.runtime
        if nc.executed:
            return "EXECUTED"
        return "CONFIGURED" if a and a["ok"] else "IDLE"

    def view(self, log_since: int = 0) -> dict:
        analysis = self.analyze()
        boundaries = self._boundaries()
        timings = self._timings(analysis)
        pills = self._loop_pills()
        nodes = []
        for nid, nc in self.nodes.items():
            a = analysis.get(nid)
            message = nc.message
            if message is None and a and a["message"] and not nc.executed:
                message = ("WARNING", a["message"])
            d = nc.model.describe()
            nodes.append({
                "id": nid,
                "factory": nc.factory,
                "name": nc.name,
                "nodeType": d["nodeType"],
                "icon": d["icon"],
                "category": d["category"],
                "x": nc.x,
                "y": nc.y,
                "annotation": nc.annotation,
                "state": self.node_state(nc, a),
                "inactive": nc.inactive,
                "message": {"level": message[0], "text": message[1]} if message else None,
                # the model's own ports (some nodes change theirs, e.g. from their notebook)
                "inPorts": [_port_json(IMPLICIT_IN, 0, True)] + [p.to_json(i + 1) for i, p in enumerate(nc.model.in_ports)],
                "outPorts": [_port_json(IMPLICIT_OUT, 0, True)] + [p.to_json(i + 1) for i, p in enumerate(nc.model.out_ports)],
                "hasDialog": d["hasDialog"],
                "hasView": d["hasView"],
                "viewAvailable": nc.view is not None,
                "loopRole": nc.model.loop_role,
                "usesVariables": any((b or {}).get("used") for b in nc.var_settings.values()),
                "inactivePorts": [i for i, o in enumerate(nc.outputs or []) if o is INACTIVE],
                # rows of each table output (by port index; None for other ports)
                "outRows": [o.height if isinstance(o, pl.DataFrame) else None for o in (nc.outputs or [])],
                # canvas widget (Direct Input / Output nodes): what to show and edit on the node
                **({"widget": nc.model.widget(nc.settings, nc.display), "widgetLayout": nc.widget_layout or {}}
                   if hasattr(nc.model, "widget") else {}),
                "timing": timings.get(nid),
                "loopIteration": pills.get(nid),
                "parent": nc.parent,
                "boundary": dict(zip(("metanode", "side", "index"), boundaries[nid])) if nid in boundaries else None,
            })
        return {
            "version": self.version,
            "dirty": self.dirty,
            "executing": self.executing,
            "canUndo": self.can_undo,
            "nodePaths": self.node_paths,
            "autoReload": self.auto_reload,
            "registryVersion": self.registry_version,
            "customReport": self.custom_report,
            "canRedo": self.can_redo,
            "nodes": nodes,
            "connections": [
                dict(c.to_json(), type=self.nodes[c.source_id].out_port(c.source_port).type,
                     inactive=bool(self.nodes[c.source_id].outputs)
                     and self.nodes[c.source_id].outputs[c.source_port] is INACTIVE)
                for c in self.connections
            ],
            "groups": copy.deepcopy(self.groups),
            "groupColors": GROUP_COLORS,
            "metanodes": [copy.deepcopy(m) for m in self.metanodes.values()],
            "frontendRequests": [{k: v for k, v in r.items() if k not in ("claimed", "done", "result")}
                                 for r in list(self._frontend.values())],
            "workflowVariables": [v.to_json() for v in self.workflow_variables],
            "meta": self.meta,
            "log": [e for e in self.log if e["seq"] > log_since],
        }

    def dialog_context(self, nid) -> dict:
        """Where a node lives, for dialog data and actions (files next to the workflow)."""
        root = None
        if self.path and self.rel_path and os.path.abspath(self.path).endswith(self.rel_path):
            root = os.path.abspath(self.path)[: -len(self.rel_path)].rstrip("/") or "/"
        return {"node_id": int(nid), "base_dir": self.base_dir, "root_dir": root,
                "workflow_path": self.path, "workflow_rel_path": self.rel_path}

    def dialog_action(self, nid, name: str, settings: dict | None = None, args: dict | None = None):
        """Run a node's dialog action (e.g. create a notebook); returns its result."""
        nc = self.node(nid)
        if not hasattr(nc.model, "dialog_action"):
            raise WorkflowError(f"{nc.name} has no dialog actions")
        s = dict(nc.settings, **(settings or {}))
        return nc.model.dialog_action(name, s, args or {}, context=self.dialog_context(nid))

    def dialog_extra(self, nid, settings: dict | None = None):
        """Node-specific dialog data from the (executed) inputs, e.g. molecules to edit;
        `settings` are the unsaved dialog settings (default: the node's)."""
        nc = self.node(nid)
        if not hasattr(nc.model, "dialog_data"):
            return None
        conns = self.in_conns(nc.id)
        inputs = []
        for idx in range(1, len(nc.model.in_ports) + 1):
            c = conns.get(idx)
            src = self.nodes.get(c.source_id) if c else None
            inputs.append(src.outputs[c.source_port] if src is not None and src.outputs else None)
        s = dict(nc.settings, **(settings or {}))
        try:
            return nc.model.dialog_data(inputs, s, connected=[i + 1 in conns for i in range(len(inputs))],
                                        context=self.dialog_context(nid))
        except Exception as e:  # noqa: BLE001 - the dialog must open anyway
            return {"error": str(e)}

    def dialog(self, nid) -> dict:
        nc = self.node(nid)
        a = self.analyze().get(nc.id) or {}
        return {
            "extra": self.dialog_extra(nid),
            "node": nc.model.describe(),
            "id": nc.id,
            "annotation": nc.annotation,
            "settings": nc.settings,
            "variables": nc.var_settings,
            "inSpecs": a.get("in_specs", []),
            "flowVariables": stack_to_json(a.get("in_stack", self._base_stack())),
        }

    def node_view(self, nid) -> dict:
        nc = self.node(nid)
        return {"node": nc.id, "nodeName": nc.name, "html": nc.view}

    def port_data(self, nid, port: int, offset: int = 0, limit: int = 100, structures=()) -> dict:
        nc = self.node(nid)
        p = nc.out_port(port)
        if p is None:
            raise WorkflowError("Invalid port")
        out = {"node": nc.id, "nodeName": nc.name, "port": port, "portName": p.name, "type": p.type,
               "executed": nc.executed}
        b = self._boundaries().get(nc.id)
        if b is not None:  # data passing through a metanode port
            mid, side, k = b
            out.update(nodeName=self.metanodes[mid]["name"], metanode=mid,
                       portName=f"{'In' if side == 'in' else 'Out'} {k + 1}")
        if not nc.executed:
            spec = self.analyze().get(nc.id, {}).get("specs", [None])[port] if port > 0 else None
            out["spec"] = _spec_json(spec)
            return out
        out["flowVariables"] = stack_to_json(nc.out_stack or {})
        obj = nc.outputs[port]
        out["inactive"] = obj is INACTIVE
        if isinstance(obj, PortObject):
            out["object"] = {k: json_value(v) for k, v in obj.summary().items()}
            if isinstance(obj, ImagePortObject) and obj.data:
                out["image"] = {"mime": obj.mime, "data": base64.b64encode(obj.data).decode("ascii")}
            cols = obj.columns()
            out["spec"] = {"columns": spec_to_json(cols)} if cols is not None else None
        if isinstance(obj, pl.DataFrame):
            out["spec"] = {"columns": spec_to_json(obj.schema)}
            out["rowCount"] = obj.height
            out["offset"] = offset
            page = obj.slice(offset, limit)
            columns = [[json_value(v) for v in page[c].to_list()] for c in page.columns]
            for i, (c, t) in enumerate(page.schema.items()):
                if dtype_kind(t) == "molecule" or (c in structures and dtype_kind(t) == "string"):
                    columns[i] = _molecule_cells(page[c])
            out["rows"] = [list(r) for r in zip(*columns)] if columns else [[] for _ in range(page.height)]
        return out


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
WIDGET_SIDES = ("left", "right", "above", "below", "free")


def clean_widget_layout(layout, node_at=(0, 0), shift=(0, 0)) -> dict | None:
    """A widget layout with only known keys of the right types (None: the default placement).

    A moved widget keeps its place on the canvas (x, y); older layouts had an
    offset from the node at ``node_at`` (dx, dy). ``shift`` moves it (pasting).
    """
    if not isinstance(layout, dict):
        return None
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)  # noqa: E731
    layout = dict(layout)
    if not (number(layout.get("x")) and number(layout.get("y"))) and number(layout.get("dx")) and number(layout.get("dy")):
        layout["x"], layout["y"] = node_at[0] + layout["dx"], node_at[1] + layout["dy"]
    if number(layout.get("x")) and number(layout.get("y")):
        layout["x"], layout["y"] = layout["x"] + shift[0], layout["y"] + shift[1]
    out = {}
    if layout.get("side") in WIDGET_SIDES:
        out["side"] = layout["side"]
    for k, lo, hi in (("x", -1e6, 1e6), ("y", -1e6, 1e6), ("w", 60, 2000), ("h", 30, 1500)):
        v = layout.get(k)
        if number(v):
            out[k] = int(round(min(max(v, lo), hi)))
    if out.get("side") != "free":
        out.pop("x", None)
        out.pop("y", None)
    elif "x" not in out or "y" not in out:
        out.pop("side")
    if layout.get("minimized"):
        out["minimized"] = True
    if layout.get("anchored"):
        out["anchored"] = True
    return out or None


def _spec_columns(spec):
    cols = spec.columns() if isinstance(spec, PortObject) else None
    return spec_to_json(cols) if cols is not None else []


def _molecule_cells(series):
    try:
        from .. import chem
    except ImportError:
        return [None if v is None else "(molecule)" for v in series.to_list()]
    return chem.view_cells(series)


def _label(nc) -> str:
    return f"(#{nc.id})"


def _port_json(port: Port, index: int, implicit: bool) -> dict:
    d = port.to_json(index)
    d["implicit"] = implicit
    return d


def _spec_of_output(obj):
    if isinstance(obj, pl.DataFrame):
        return obj.schema
    if isinstance(obj, PortObject):
        return obj.spec()
    if obj is INACTIVE:
        return INACTIVE
    return FLOWVAR


def _spec_json(spec):
    if spec is None:
        return None
    if spec is INACTIVE:
        return {"inactive": True, "columns": []}
    if spec is FLOWVAR:
        return {"flowvariable": True, "columns": []}
    if isinstance(spec, PortObject):
        return {"portObject": spec.port_type, "columns": _spec_columns(spec)}
    return {"columns": spec_to_json(spec)}


def _normalize_output(port: Port, obj):
    if obj is INACTIVE:
        return INACTIVE
    if port.type == FLOWVARIABLE:
        return FLOWVAR
    if port.type != TABLE:
        if not isinstance(obj, PortObject) or obj.port_type != port.type:
            raise WorkflowError(f'Output "{port.name}" must be a {port.type} port object (got {type(obj).__name__})')
        return obj
    if isinstance(obj, pl.LazyFrame):
        obj = obj.collect()
    if not isinstance(obj, pl.DataFrame):
        try:
            import pandas as pd

            if isinstance(obj, pd.DataFrame):
                return pl.from_pandas(obj)
        except ImportError:
            pass
        raise WorkflowError(f'Output "{port.name}" is not a table (got {type(obj).__name__})')
    return obj


def _coerce_setting(value, f, strict=True):
    """Coerce a flow variable value / dialog input to a setting field type."""
    if f is None:
        return value
    try:
        if f.type == "int":
            return coerce(value, "INTEGER")
        if f.type == "float":
            return coerce(value, "DOUBLE")
        if f.type == "bool":
            return coerce(value, "BOOLEAN")
        if f.type in ("string", "enum", "column", "code"):
            if f.type == "column" and value is None:
                return None
            return coerce(value, "STRING")
    except (TypeError, ValueError):
        if strict:
            raise InvalidSettingsError(f'Cannot use value {value!r} for setting "{f.label}"')
        return f.default
    return value


TABLE_PORT = TABLE  # re-export for convenience
