"""Metanode ports.

A metanode is a container: its nodes stay in the workflow's (flat) graph
with ``parent`` set to the metanode, so execution, loops, scopes and flow
variables work exactly as without it (metanodes only organize a
workflow). Every port of a metanode is a hidden pass-through
node on its boundary: an input port is a node inside the metanode whose
input is connected from outside and whose output feeds the nodes inside,
an output port the other way round. Connections never cross a boundary
any other way.
"""
from __future__ import annotations

import re

from .node import FLOWVAR, NodeModel, Port

_FACTORY = re.compile(r"verknode\.metanode\.(in|out)\.([A-Za-z0-9_.-]+)")
_CLASSES: dict[str, type] = {}


class BoundaryNodeModel(NodeModel):
    """Pass-through node of a metanode port (never shown as a node)."""

    boundary = True
    node_type = "Other"
    side = "in"
    port_type = ""

    def configure(self, in_specs, s, ctx):
        return [in_specs[0] if in_specs[0] is not None else None]

    def execute(self, inputs, s, ctx):
        obj = inputs[0]
        return [FLOWVAR if obj is None and self.port_type == "flowvariable" else obj]


def boundary_factory(side: str, port_type: str) -> str:
    return f"verknode.metanode.{side}.{port_type}"


def boundary_class(factory: str):
    """Node class of a metanode port factory id, or None for other factories."""
    m = _FACTORY.fullmatch(factory or "")
    if not m:
        return None
    if factory not in _CLASSES:
        side, port_type = m.groups()
        label = "Metanode Input" if side == "in" else "Metanode Output"
        _CLASSES[factory] = type(
            "MetanodeInput" if side == "in" else "MetanodeOutput",
            (BoundaryNodeModel,),
            {
                "factory": factory,
                "name": label,
                "side": side,
                "port_type": port_type,
                "in_ports": [Port(port_type, "In")],
                "out_ports": [Port(port_type, "Out")],
            },
        )
    return _CLASSES[factory]


def is_boundary(model) -> bool:
    return getattr(model, "boundary", False)
