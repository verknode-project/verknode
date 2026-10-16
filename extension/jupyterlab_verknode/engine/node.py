"""Node model API: ports, dialog fields, execution context and the node registry.

Port indices: index 0 is the implicit flow variable port that
every node has (the red "mickey mouse ears"), explicit ports start at 1.
"""
from __future__ import annotations

import contextlib
import copy
import os
from dataclasses import dataclass

import polars as pl

from .flowvars import FlowVariable, make

TABLE = "table"
FLOWVARIABLE = "flowvariable"
MODEL = "model"  # trained model (blue square port)
IMAGE = "image"  # rendered image (green square port)
DB_SESSION = "dbsession"  # database connection (dark red square port)
DB_DATA = "dbdata"  # database connection + SQL query (brown square port)


class _Marker:
    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name


# Output object of a flow variable port (the variables travel in the stack).
FLOWVAR = _Marker("FlowVariablePortObject")
# Output object / spec of a port on an inactive branch (IF Switch, ...).
INACTIVE = _Marker("InactiveBranch")


class PortObject:
    """Content of a non-table port. Must be picklable (it is saved with the workflow).

    spec() returns the lightweight object passed to downstream configure()
    (e.g. a model without the fitted estimator); columns() optionally gives a
    polars schema that dialogs of downstream nodes can offer.
    """

    port_type = ""

    def spec(self):
        return self

    def columns(self):
        return None

    def summary(self) -> dict:
        return {}


class ModelPortObject(PortObject):
    """A trained model: estimator plus the learning spec needed to apply it."""

    port_type = MODEL

    def __init__(self, model_type: str, estimator=None, learning_spec=None, target: str | None = None,
                 target_dtype=None, classes=None, info: dict | None = None):
        self.model_type = model_type
        self.estimator = estimator
        self.learning_spec = learning_spec  # polars Schema of the feature columns
        self.target = target
        self.target_dtype = target_dtype
        self.classes = classes
        self.info = info or {}

    def spec(self):
        return ModelPortObject(self.model_type, None, self.learning_spec, self.target, self.target_dtype,
                               self.classes, self.info)

    def columns(self):
        return self.learning_spec

    @property
    def features(self) -> list:
        return list(self.learning_spec or {})

    def summary(self) -> dict:
        out = {"Model": self.model_type}
        if self.target:
            out["Target column"] = f"{self.target} ({self.target_dtype})"
        if self.learning_spec is not None:
            out["Learning columns"] = ", ".join(self.learning_spec)
        if self.classes is not None:
            out["Classes"] = ", ".join(map(str, self.classes))
        out.update({k: v for k, v in self.info.items()})
        if self.estimator is not None:
            out["Estimator"] = repr(self.estimator)
        return out


class ImagePortObject(PortObject):
    port_type = IMAGE

    def __init__(self, data: bytes, mime: str = "image/svg+xml"):
        self.data = data
        self.mime = mime

    def spec(self):
        return ImagePortObject(b"", self.mime)

    def summary(self) -> dict:
        return {"Type": self.mime, "Size": f"{len(self.data)} bytes"}


class DBSessionPortObject(PortObject):
    """Connection to a database (SQLite file)."""

    port_type = DB_SESSION

    def __init__(self, path: str, dialect: str = "sqlite"):
        self.dialect = dialect
        self.path = path

    def connect(self):
        import sqlite3

        return sqlite3.connect(self.path)

    def tables(self) -> list:
        try:
            with self.connect() as con:
                return [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name")]
        except Exception:  # noqa: BLE001
            return []

    def summary(self) -> dict:
        return {"Database": self.dialect, "Path": self.path, "Tables": ", ".join(self.tables())}


class DBDataPortObject(PortObject):
    """A lazily evaluated SQL query on a database session (DB data port)."""

    port_type = DB_DATA

    def __init__(self, session: DBSessionPortObject, query: str, schema=None):
        self.session = session
        self.query = query
        self.schema = schema  # polars Schema of the query result, if known

    def columns(self):
        return self.schema

    def summary(self) -> dict:
        out = self.session.summary()
        out["SQL query"] = self.query
        return out


class InvalidSettingsError(Exception):
    """Raised by configure/validate when the node cannot be configured."""


class CanceledExecutionError(Exception):
    pass


@dataclass
class Port:
    type: str
    name: str
    description: str = ""
    optional: bool = False

    def to_json(self, index: int) -> dict:
        return {
            "index": index,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "optional": self.optional,
            "implicit": False,
        }


IMPLICIT_IN = Port(FLOWVARIABLE, "Variable Inport", "Flow variables (implicit port)", optional=True)
IMPLICIT_OUT = Port(FLOWVARIABLE, "Variable Outport", "Flow variables (implicit port)", optional=True)


@dataclass
class Field:
    """A dialog setting.

    type: string | int | float | bool | enum | column | columns | code |
          records | tablecreator | varlist | multienum | variable (flow variable name) |
          widget (not in the dialog: state edited on the canvas, e.g. a Direct Input's value)
    """

    key: str
    type: str
    label: str
    default: object = None
    description: str = ""
    options: list | None = None  # enum choices
    port: int = 1  # input port providing columns for column/columns fields
    kinds: list | None = None  # restrict column fields to these column kinds
    fields: list | None = None  # sub-fields of a records field
    language: str | None = None  # code fields
    insertables: bool = False  # code field: offer $column$ / $${Svar}$$ insertion
    path: bool = False  # string field holding a file path
    show_if: dict | None = None  # {"key": value-or-list} visibility condition
    # sketcher fields: other settings whose change invalidates edits, {"column": "structure"|"name"}
    depends: dict | None = None

    def to_json(self) -> dict:
        d = {
            "key": self.key,
            "type": self.type,
            "label": self.label,
            "default": self.default,
            "description": self.description,
        }
        for k in ("options", "kinds", "language", "show_if", "depends"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.type in ("column", "columns"):
            d["port"] = self.port
        if self.fields is not None:
            d["fields"] = [f.to_json() for f in self.fields]
        if self.insertables:
            d["insertables"] = True
        if self.path:
            d["path"] = True
        return d

    @property
    def scalar(self) -> bool:
        return self.type in ("string", "int", "float", "bool", "enum", "column", "code")


def F(key, type, label, default=None, description="", **kw) -> Field:
    return Field(key, type, label, default, description, **kw)


class ExecContext:
    """Passed to configure/execute: flow variables, paths, logging, loops."""

    def __init__(self, stack: dict, base_dir: str, cancel_event=None, loop=None,
                 dry=False, logger=None):
        self.stack = dict(stack)
        self.pushed: dict[str, FlowVariable] = {}
        self.base_dir = base_dir
        self.loop = loop
        self.dry = dry
        self.warning: str | None = None
        self.view: str | None = None  # HTML of the node view
        self.display: dict | None = None  # JSON shown on the canvas (widget nodes)
        self.caught_error: dict | None = None  # set for Catch Errors nodes
        self.workflow_path: str | None = None  # absolute path of the .vkf file, if saved
        self._cancel = cancel_event
        self._logger = logger
        self.on_progress = None  # set by the workflow: shows set_progress on the canvas

    # -- flow variables -------------------------------------------------
    def variables(self) -> dict:
        out = {k: v.value for k, v in self.stack.items()}
        out.update({k: v.value for k, v in self.pushed.items()})
        return out

    def variable(self, name: str):
        if name in self.pushed:
            return self.pushed[name]
        return self.stack.get(name)

    def push_variable(self, name: str, value, vtype: str | None = None):
        self.pushed[name] = make(name, value, vtype)

    # -- misc -----------------------------------------------------------
    def resolve_path(self, path: str) -> str:
        if not path:
            raise InvalidSettingsError("No file path specified")
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.base_dir, path)
        return os.path.normpath(path)

    def canceled(self) -> bool:
        """Has the user canceled the execution? (for nodes that wait on other processes)"""
        return self._cancel is not None and self._cancel.is_set()

    def check_canceled(self):
        if self.canceled():
            raise CanceledExecutionError("Execution canceled")

    def log(self, text: str, level: str = "INFO"):
        if self._logger and not self.dry:
            self._logger(level, text)

    def set_view(self, html: str):
        """Set the node view (HTML; see engine.views for helpers)."""
        if not self.dry:
            self.view = html

    def set_display(self, data: dict):
        """Set what the node shows on the canvas (JSON; nodes with a widget() method)."""
        if not self.dry:
            self.display = data

    def set_progress(self, fraction: float):
        """How far the node is (0..1), for long nodes: the canvas shows it and the ETA follows it."""
        if not self.dry and self.on_progress is not None:
            self.on_progress(min(max(float(fraction), 0.0), 1.0))

    def warn(self, text: str):
        if not self.dry:
            self.warning = text
            self.log(text, "WARN")


NODE_REGISTRY: dict[str, type] = {}
_collecting: list | None = None  # set while custom node files are imported


def register(cls):
    """Register a built-in node (inside custom node files: mark the class as a node)."""
    if _collecting is not None:
        _collecting.append(cls)
        return cls
    NODE_REGISTRY[cls.factory] = cls
    return cls


@contextlib.contextmanager
def collect_registrations():
    """Capture @register calls (custom node files) instead of registering globally."""
    global _collecting
    previous, _collecting = _collecting, []
    try:
        yield _collecting
    finally:
        _collecting = previous


class NodeModel:
    factory = ""
    name = ""
    category = ""  # "/" separated node repository path
    node_type = "Manipulator"  # Source|Sink|Manipulator|LoopStart|LoopEnd|ScopeStart|ScopeEnd|Other
    icon = ""
    short_description = ""
    description = ""
    in_ports: list[Port] = []
    out_ports: list[Port] = []
    fields: list[Field] = []
    loop_role: str | None = None  # "start" | "end"
    scope_role: str | None = None  # "try" | "catch" (error handling)
    handles_inactive = False  # receives INACTIVE inputs instead of being skipped
    has_view = False  # execute() sets ctx.set_view(html)
    # nodes that close the scope this node opens (loop end, End IF, Catch ...),
    # most likely first; suggested first by the Tab quick-add while open
    closed_by: list = []

    def default_settings(self) -> dict:
        return {f.key: copy.deepcopy(f.default) for f in self.fields}

    def field(self, key: str) -> Field | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def validate(self, s: dict, in_specs: list):
        """Raise InvalidSettingsError if settings are incomplete."""

    def configure(self, in_specs: list, s: dict, ctx: ExecContext) -> list:
        """Return output specs (polars Schema, FLOWVAR, INACTIVE or None=unknown).

        in_specs holds one entry per explicit input port: a schema, FLOWVAR,
        INACTIVE, or None for an unconnected optional port.
        Default: execute on empty tables of the input schemas (dry run).
        Nodes with side effects or expensive execution must override.
        """
        self.validate(s, in_specs)
        inputs = []
        for spec in in_specs:
            if spec is None or spec is FLOWVAR or spec is INACTIVE or isinstance(spec, PortObject):
                inputs.append(spec)
            else:
                inputs.append(pl.DataFrame(schema=spec))
        outs = self.execute(inputs, s, ctx)
        if outs is None:
            return [None] * len(self.out_ports)
        return [spec_of(o) for o in outs]

    def execute(self, inputs: list, s: dict, ctx: ExecContext) -> list | None:
        raise NotImplementedError

    @classmethod
    def describe(cls) -> dict:
        return {
            "factory": cls.factory,
            "name": cls.name,
            "category": cls.category,
            "nodeType": cls.node_type,
            "icon": cls.icon,
            "shortDescription": cls.short_description,
            "description": cls.description,
            "inPorts": [p.to_json(i + 1) for i, p in enumerate(cls.in_ports)],
            "outPorts": [p.to_json(i + 1) for i, p in enumerate(cls.out_ports)],
            "fields": [f.to_json() for f in cls.fields],
            "hasDialog": bool(cls.fields),
            "hasView": cls.has_view,
            "closedBy": list(cls.closed_by),
        }


def spec_of(obj):
    if obj is None or obj is FLOWVAR:
        return FLOWVAR
    if obj is INACTIVE:
        return INACTIVE
    if isinstance(obj, pl.DataFrame):
        return obj.schema
    if isinstance(obj, PortObject):
        return obj.spec()
    return None


def require_column(s: dict, key: str, spec, label: str = "column"):
    name = s.get(key)
    if not name:
        raise InvalidSettingsError(f"No {label} selected")
    if spec is not None and spec is not FLOWVAR and name not in spec:
        raise InvalidSettingsError(f'Column "{name}" not found in input table')
    return name
