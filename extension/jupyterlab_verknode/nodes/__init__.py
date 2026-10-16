"""Standard nodes.

This folder is a node folder like any custom node folder: every module
imports the public authoring API from ``verknode.node`` (plus sibling modules
and libraries) and every non-abstract NodeModel subclass becomes a node.
Importing this package registers them in NODE_REGISTRY. Modules whose
imports fail (e.g. chemistry.py without RDKit) are skipped.
"""
import logging as _logging
import os as _os

from ..engine.custom import load_standard_nodes as _load
from ..engine.node import NODE_REGISTRY as _REGISTRY

SKIPPED = _load(__name__, _os.path.dirname(__file__), _REGISTRY)
for _path, _error in SKIPPED:
    _logging.getLogger(__name__).info("verknode: skipped standard node file %s: %s", _path, _error)
