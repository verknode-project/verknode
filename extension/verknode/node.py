"""API for writing custom verknode nodes.

Put a .py file into a node folder of the workflow (editor: "Custom nodes…",
Python: ``wf.set_node_paths(["nodes"])``) and subclass NodeModel::

    import polars as pl
    from verknode.node import F, NodeModel, Port, TABLE

    class Double(NodeModel):
        name = "Double Column"
        category = "Custom"
        in_ports = [Port(TABLE, "Input table")]
        out_ports = [Port(TABLE, "Output table")]
        fields = [F("column", "column", "Column")]

        def execute(self, inputs, s, ctx):
            df = inputs[0]
            return [df.with_columns(pl.col(s["column"]) * 2)]

The standard nodes (jupyterlab_verknode/nodes/*.py) are written with exactly
this API and the same conventions - read them for examples.

Reference
---------
- Ports: ``Port(type, name, description="", optional=False)`` with types TABLE,
  FLOWVARIABLE, MODEL, IMAGE, DB_SESSION, DB_DATA, LLM (a model connection), AGENT. Explicit ports
  start at index 1.
- Dialog fields: ``F(key, type, label, default, description, **options)``; types
  string, int, float, bool, enum (options=[...]), column (port=, kinds=[...]),
  columns, multienum, varlist, variable, records (fields=[...]), code, tablecreator.
  Settings arrive in ``execute(inputs, s, ctx)`` as the dict ``s``.
- ``execute`` returns one object per output port (polars DataFrame for tables,
  ModelPortObject / ImagePortObject ..., FLOWVAR for flow variable ports).
  It also runs on empty tables while configuring (to compute output columns);
  override ``configure(in_specs, s, ctx)`` for expensive nodes or side effects.
- ``ctx``: variables(), push_variable(name, value), log(text), warn(text),
  resolve_path(path), check_canceled(), set_view(html) (with has_view = True),
  set_progress(fraction) (0..1, for long nodes: the canvas shows it and the ETA follows it),
  dry (True while configuring). Raise InvalidSettingsError for bad settings.
- Canvas widgets: subclass ``InputWidget`` (a control on the node whose value
  it outputs) or ``OutputWidget`` (shows the input on the node); see
  jupyterlab_verknode/engine/widgets.py and the standard ones in nodes/widgets.py.
- Optional class attributes: factory (stable id), node_type (Source, Sink,
  Manipulator, Learner, Predictor, Visualizer, Other), icon (<= 4 chars),
  short_description, description (HTML), has_view, closed_by (factory ids
  of the nodes closing a scope the node opens, e.g. its loop end; the Tab
  quick-add suggests them first while the scope is open).
"""
from jupyterlab_verknode.engine import views  # noqa: F401 - chart/table helpers for node views
from jupyterlab_verknode.engine.flowvars import VAR_TYPES, FlowVariable, coerce, infer_type, make  # noqa: F401
from jupyterlab_verknode.engine.custom import capture_stdout  # noqa: F401 - this thread's print() into a buffer
from jupyterlab_verknode.engine.helpers import selected_columns, substitute_variables  # noqa: F401
from jupyterlab_verknode.engine.node import (  # noqa: F401
    DB_DATA,
    AGENT,
    AgentPortObject,
    DB_SESSION,
    FLOWVAR,
    FLOWVARIABLE,
    IMAGE,
    INACTIVE,
    MODEL,
    TABLE,
    CanceledExecutionError,
    DBDataPortObject,
    DBSessionPortObject,
    ExecContext,
    F,
    Field,
    ImagePortObject,
    InvalidSettingsError,
    LLM,
    LLMConnectionPortObject,
    ModelPortObject,
    NodeModel,
    Port,
    PortObject,
    register,
    require_column,
    spec_of,
)
from jupyterlab_verknode.engine.widgets import InputWidget, OutputWidget  # noqa: F401 - canvas widget nodes
from jupyterlab_verknode.engine.rules import (  # noqa: F401
    RuleSyntaxError,
    compile_rules,
    rules_expression,
    wildcard_to_regex,
)
from jupyterlab_verknode.engine.types import (  # noqa: F401
    CELL_DTYPES,
    CELL_TYPES,
    MOL_DTYPE,
    TYPE_NAMES,
    dtype_kind,
    is_numeric_kind,
    json_value,
    parse_cell,
    spec_to_json,
    type_name,
)

try:  # RDKit helpers (molecule columns), if RDKit is installed
    from jupyterlab_verknode import chem  # noqa: F401
except ImportError:  # pragma: no cover
    chem = None
