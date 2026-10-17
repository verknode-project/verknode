"""The workflow as seen from a Workflow Script node: read it, and change it.

Nodes cannot change the workflow while it runs (connections, settings and
the run plan would shift under the running nodes). So a script's edits are
recorded and applied when the run ends - in order, as one undo step - and
`execute()` asks for a run after them. Reading is immediate::

    src = workflow.find("Sales")                  # by label (or workflow.node(id))
    copy = workflow.copy([src], dy=150)[0]        # pending: usable in later edits
    workflow.configure(copy, table=...)
    workflow.connect(copy, workflow.find("Report"), dst_port=2)
    workflow.execute(copy)
"""
from __future__ import annotations

import copy as _copy


class WorkflowEditError(RuntimeError):
    pass


class NodeRef:
    """A node of the workflow, or one a script adds (its id is known once the edits are applied)."""

    def __init__(self, sw: "ScriptWorkflow", nid: int | None = None, label: str | None = None):
        self._sw = sw
        self._id = nid
        self._pending_label = label

    @property
    def id(self) -> int | None:
        """The node id (None for a node added by this script until its edits are applied)."""
        return self._id

    @property
    def pending(self) -> bool:
        return self._id is None or self._id not in self._sw._wf.nodes

    def _nc(self):
        if self.pending:
            raise WorkflowEditError("This node is added by the script: it exists once the run ends")
        return self._sw._wf.nodes[self._id]

    @property
    def label(self) -> str:
        return self._pending_label or "" if self.pending else self._nc().annotation

    @property
    def name(self) -> str:
        return self._nc().name

    @property
    def factory(self) -> str:
        return self._nc().factory

    @property
    def x(self) -> float:
        return self._nc().x

    @property
    def y(self) -> float:
        return self._nc().y

    @property
    def settings(self) -> dict:
        """A copy of the node's settings."""
        return _copy.deepcopy(self._nc().settings)

    @property
    def executed(self) -> bool:
        return self._nc().executed

    @property
    def widget_layout(self) -> dict:
        """Where its canvas widget sits ({} : the default place; see ScriptWorkflow.widget)."""
        return dict(self._nc().widget_layout or {})

    def inputs(self) -> list:
        """[(source node, source port, this node's port)] of its incoming connections."""
        wf = self._sw._wf
        return [(self._sw._ref(c.source_id), c.source_port, c.dest_port) for c in wf.connections if c.dest_id == self._id]

    def outputs(self) -> list:
        """[(this node's port, target node, target port)] of its outgoing connections."""
        wf = self._sw._wf
        return [(c.source_port, self._sw._ref(c.dest_id), c.dest_port) for c in wf.connections if c.source_id == self._id]

    def __repr__(self):
        return f"<node {self._id if self._id is not None else '(new)'} {self.label!r}>"


class ScriptWorkflow:
    """The workflow a Workflow Script node runs in (see the module docstring)."""

    def __init__(self, wf, me: int, record):
        self._wf = wf
        self._me = me
        self._record = record  # record(fn): fn(engine workflow) runs when the run ends
        self._added = 0

    # -- reading --------------------------------------------------------
    def _ref(self, nid: int) -> NodeRef:
        return NodeRef(self, nid)

    @property
    def me(self) -> NodeRef:
        """This Workflow Script node."""
        return self._ref(self._me)

    def nodes(self) -> list:
        return [self._ref(nid) for nid, nc in self._wf.nodes.items() if nc.parent is None and not _hidden(nc)]

    def find(self, label: str) -> NodeRef | None:
        """The node with this label (annotation), or None."""
        for nid, nc in self._wf.nodes.items():
            if nc.annotation == label and not _hidden(nc):
                return self._ref(nid)
        return None

    def find_all(self, prefix: str) -> list:
        """The nodes whose label starts with `prefix` (e.g. the ones an earlier run added)."""
        return [self._ref(nid) for nid, nc in self._wf.nodes.items() if nc.annotation.startswith(prefix) and not _hidden(nc)]

    def node(self, ref) -> NodeRef:
        """A node by id, label or NodeRef."""
        if isinstance(ref, NodeRef):
            return ref
        if isinstance(ref, int) and ref in self._wf.nodes:
            return self._ref(ref)
        found = self.find(str(ref))
        if found is None:
            raise WorkflowEditError(f"No node {ref!r}")
        return found

    # -- editing (applied when the run ends) ----------------------------
    def _id(self, ref) -> int:
        n = self.node(ref)
        if n._id is None:
            raise WorkflowEditError(f"{n!r} was not added (an earlier edit failed?)")
        return n._id

    def add_node(self, node_type: str, x: float | None = None, y: float | None = None,
                 label: str | None = None, settings: dict | None = None, **more) -> NodeRef:
        """Add a node by name or factory id; `settings` and keyword arguments are its settings
        (use `settings` for keys like "x"). Default position: in a row below this node."""
        factory = self._factory(node_type)
        settings = {**(settings or {}), **more}
        me = self._wf.nodes[self._me]
        if x is None or y is None:
            x, y = me.x + 140 * self._added, me.y + 150
        self._added += 1
        ref = NodeRef(self, None, label)

        def apply(wf):
            ref._id = wf.add_node(factory, x, y, me.parent)
            if label is not None:
                wf.set_annotation(ref._id, label)
            if settings:
                wf.set_settings(ref._id, settings)
        self._record(apply, f"add {node_type!r}")
        return ref

    def copy(self, nodes, dx: float = 0, dy: float = 150, labels=None) -> list:
        """Copies of nodes (with their settings and the connections among them), shifted by
        dx / dy; `labels` renames them: {old label: new label} or a function. Same order as `nodes`."""
        refs = [self.node(n) for n in nodes]
        new = [NodeRef(self, None, None) for _ in refs]

        def apply(wf):
            ids = [self._id(r) for r in refs]
            fragment = wf.copy(ids)
            pasted = wf.paste(fragment, dx, dy)
            by_old = dict(zip([n["id"] for n in fragment["nodes"]], pasted))
            for r, n in zip(refs, new):
                n._id = by_old[r._id]
                old = wf.nodes[r._id].annotation
                name = labels(old) if callable(labels) else (labels or {}).get(old)
                if name:
                    wf.set_annotation(n._id, name)
        self._record(apply, f"copy {len(refs)} node(s)")
        return new

    def remove(self, *nodes) -> None:
        refs = [self.node(n) for n in nodes]
        self._record(lambda wf: wf.remove_nodes([self._id(r) for r in refs]), "remove")

    def connect(self, source, target, src_port: int = 1, dst_port: int = 1) -> None:
        """Connect an output port to an input port (port 0: the flow variable ports)."""
        a, b = self.node(source), self.node(target)
        self._record(lambda wf: wf.connect(self._id(a), src_port, self._id(b), dst_port), f"connect {a!r} -> {b!r}")

    def disconnect(self, source, target, src_port: int = 1, dst_port: int = 1) -> None:
        a, b = self.node(source), self.node(target)
        self._record(lambda wf: wf.disconnect(self._id(a), src_port, self._id(b), dst_port), f"disconnect {a!r} -> {b!r}")

    def configure(self, node, **settings) -> None:
        """Change some of a node's settings (the others stay)."""
        n = self.node(node)
        self._record(lambda wf: wf.set_settings(self._id(n), settings), f"configure {n!r}")

    def move(self, node, x: float, y: float) -> None:
        n = self.node(node)
        self._record(lambda wf: wf.move([{"id": self._id(n), "x": x, "y": y}]), f"move {n!r}")

    def label(self, node, text: str) -> None:
        n = self.node(node)
        self._record(lambda wf: wf.set_annotation(self._id(n), text), f"label {n!r}")

    def widget(self, node, **layout) -> None:
        """Where a widget node's canvas widget sits: side (left / right / above / below / free),
        x / y (free), w / h (size), minimized, anchored. Unnamed keys keep their value."""
        n = self.node(node)

        def apply(wf):
            nid = self._id(n)
            wf.set_widget_layout(nid, {**(wf.nodes[nid].widget_layout or {}), **layout})
        self._record(apply, f"widget {n!r}")

    def group(self, *nodes, text: str = "", color: str | None = None) -> None:
        """A group annotation around the nodes."""
        refs = [self.node(n) for n in nodes]
        self._record(lambda wf: wf.add_group([self._id(r) for r in refs], text, color), "group")

    def execute(self, *nodes) -> None:
        """Run these nodes (none: the whole workflow) once the edits are applied."""
        refs = [self.node(n) for n in nodes]
        self._record(("execute", refs), "execute")

    # -- helpers ----------------------------------------------------------
    def _factory(self, node_type: str) -> str:
        types = self._wf.node_types()
        for t in types:
            if t["factory"] == node_type or t["name"] == node_type:
                return t["factory"]
        low = node_type.lower()
        matches = [t for t in types if t["name"].lower() == low]
        if len(matches) == 1:
            return matches[0]["factory"]
        raise WorkflowEditError(f"Unknown node type {node_type!r}")

    def __repr__(self):
        return f"<workflow, run by node {self._me}>"


def _hidden(nc) -> bool:
    from .metanode import is_boundary

    return is_boundary(nc.model)
