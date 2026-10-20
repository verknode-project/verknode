"""API for verknode packages: sets of nodes, port types, canvas widgets, views and node defaults that
other developers distribute as Python packages (docs/packages.md).

    from verknode.package import Package, API_VERSION

    package = Package(name="printing3d", title="3D Printing", version="0.1.0", api=">=1.0,<2",
                      modules=["verknode_printing3d.nodes"], static="static")

and in pyproject.toml::

    [project.entry-points."verknode.packages"]
    printing3d = "verknode_printing3d:package"

Nodes use the node API (``verknode.node``); a port type of the package: ``register_port_type``; static
files for views (``package.static_url(path)``); node defaults: ``register_defaults``.
"""
from jupyterlab_verknode.engine.node import PORT_TYPES, PortObject, register_port_type  # noqa: F401
from jupyterlab_verknode.engine.packages import API_VERSION, Package, api_compatible  # noqa: F401
