"""Canvas widgets: base classes of nodes used directly on the workflow canvas.

Input widgets show a control on the node (the user changes the value without
opening a dialog) and output it; output widgets show their input table on the
node (a table, a chart, a value) once executed. Every kind is its own node
class; the standard ones are in nodes/widgets.py, and custom node files add
more the same way (exported by ``verknode.node``)::

    from verknode.node import F, InputWidget, InvalidSettingsError
    import datetime as dt
    import polars as pl

    class DateInput(InputWidget):
        name = "Date Input"
        control = "text"                      # reuse the text box on the canvas
        variable_type = "STRING"
        widget_fields = [F("default", "string", "Default date", "2024-01-01")]

        def valid(self, s, value):
            try:
                dt.date.fromisoformat(value)
                return True
            except (TypeError, ValueError):
                return False

        def to_table(self, value, s, ctx):
            return pl.DataFrame({self.column_name(s): [dt.date.fromisoformat(value)]})

Canvas controls of input widgets (``control``): text, textarea, number,
integer, slider, checkbox, dropdown, table, molecule - or "html": a page of
your own (``html(s, value)``) that calls ``verknode.setValue(v)``.
Canvas displays of output widgets (``renderer``): table, bar, line, scatter,
value, image - or "html" (``render`` returns {"html": ...}). Both html kinds
run in a sandboxed frame on the node. Where the widget sits relative to its
node, its size and whether it is minimized are canvas layout (not settings).
"""
from __future__ import annotations

import polars as pl

from .node import TABLE, F, NodeModel, Port
from .types import json_value

INPUT_CONTROLS = ["text", "textarea", "number", "integer", "slider", "checkbox", "dropdown", "table", "molecule", "html"]
OUTPUT_RENDERERS = ["table", "bar", "line", "scatter", "value", "image", "chat", "html"]


class InputWidget(NodeModel):
    """Base of input widget nodes: a control on the canvas whose value the node outputs.

    Subclasses set ``control`` (how the canvas shows it), ``widget_fields``
    (dialog settings such as the default value) and override some of:

    - ``default_value(s)``: the value before any edit (default: setting "default")
    - ``valid(s, value)``: can an edited value be used with these settings?
      (invalid edits fall back to the default)
    - ``coerce(s, value)``: final form of a valid value (e.g. int)
    - ``control_props(s)``: extra data for the control (choices, bounds ...)
    - ``to_table(value, s, ctx)``: the output table (default: one row, one
      column named after the "Name" setting, of type ``dtype``)
    - ``html(s, value)``: the page of an "html" control

    The value is also pushed as a flow variable named like the column unless
    ``variable_type`` is None. The edited value is the hidden setting "value"
    (None = the default); ``auto_run`` executes the output widgets downstream
    when it changes.
    """

    abstract = True
    category = "Widgets/Inputs"
    node_type = "Source"
    icon = "✎"
    control = "text"
    dtype = pl.String
    variable_type: str | None = "STRING"  # flow variable type (STRING, INTEGER, DOUBLE, BOOLEAN) or None
    default_name = "value"
    widget_fields: list = []
    out_ports = [Port(TABLE, "Value", "The value entered on the canvas")]

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if "fields" not in cls.__dict__:
            cls.fields = [
                F("label", "string", "Label", "", "Shown above the control on the canvas. Empty: none."),
                F("name", "string", "Name", cls.default_name, "Output column (and flow variable) name."),
                *cls.widget_fields,
                F("auto_run", "bool", "Run on change", True,
                  "Execute the output widgets downstream whenever the value is changed on the canvas."),
                F("value", "widget", "Value", None, "Edited on the canvas (None: the default)."),
            ]

    # -- hooks ------------------------------------------------------------
    def default_value(self, s: dict):
        return s.get("default")

    def valid(self, s: dict, value) -> bool:
        return value is not None

    def coerce(self, s: dict, value):
        return value

    def control_props(self, s: dict) -> dict:
        return {}

    def html(self, s: dict, value) -> str:
        return ""

    def to_table(self, value, s: dict, ctx) -> pl.DataFrame:
        return pl.DataFrame([pl.Series(self.column_name(s), [value], dtype=self.dtype)])

    # -- machinery --------------------------------------------------------
    @staticmethod
    def column_name(s: dict) -> str:
        return (s.get("name") or "").strip() or "value"

    def _valid(self, s, value) -> bool:
        try:
            return value is not None and bool(self.valid(s, value))
        except Exception:  # noqa: BLE001 - a broken check means "use the default"
            return False

    def value_of(self, s: dict):
        """The value in use: the canvas edit if it fits the settings, else the default."""
        v = s.get("value")
        return self.coerce(s, v if self._valid(s, v) else self.default_value(s))

    def normalize_settings(self, old: dict, new: dict) -> dict:
        # an edit is dropped when it no longer fits, or when it equals the old default
        # and the default changed (it follows the new default)
        v = new.get("value")
        if v is None:
            return new
        try:
            follows = v == self.default_value(old) and self.default_value(old) != self.default_value(new)
        except Exception:  # noqa: BLE001
            follows = False
        return dict(new, value=None) if follows or not self._valid(new, v) else new

    def widget(self, s: dict, display) -> dict:
        value = self.value_of(s)
        out = {
            "type": "input",
            "control": self.control,
            "label": s.get("label") or "",
            "name": self.column_name(s),
            "value": json_value(value),
            "edited": self._valid(s, s.get("value")),
            "autoRun": bool(s.get("auto_run", True)),
            "props": self.control_props(s),
        }
        if self.control == "html":
            out["html"] = self.html(s, value)
        return out

    def execute(self, inputs, s, ctx):
        value = self.value_of(s)
        if self.variable_type:
            ctx.push_variable(self.column_name(s), value, self.variable_type)
        return [self.to_table(value, s, ctx)]


class OutputWidget(NodeModel):
    """Base of output widget nodes: show the input on the canvas.

    Subclasses set ``renderer`` (how the canvas draws the data),
    ``widget_fields`` and implement ``render(obj, s, ctx)`` returning the JSON
    for the renderer (see nodes/widgets.py for each built-in renderer's
    format; "html" takes {"html": page}). ``width`` / ``height`` are the
    starting size of the display (the user resizes it on the canvas).

    The first output port passes the input on unchanged. ``interactive``
    displays let the user select on the canvas (rows of a table, points of a
    chart): the selection is the hidden setting "selection", a dict the
    renderer sends ({"rows": [row indices]} for the built-in ones), and
    ``select(df, s, selection)`` turns it into the table of the second port.
    """

    abstract = True
    category = "Widgets/Displays"
    node_type = "Visualizer"
    icon = "▦"
    renderer = "table"
    width, height = 320, 200
    interactive = False
    widget_fields: list = []
    in_ports = [Port(TABLE, "Table to show")]

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if "fields" not in cls.__dict__:
            cls.fields = [
                F("title", "string", "Title", "", "Shown above the display on the canvas. Empty: none."),
                *cls.widget_fields,
                *([F("selection", "widget", "Selection", None, "Made on the canvas (None: nothing selected).")]
                  if cls.interactive else []),
            ]
        if "out_ports" not in cls.__dict__:
            kind = cls.in_ports[0].type if cls.in_ports else TABLE
            cls.out_ports = [Port(kind, "Input", "The input, unchanged")] + (
                [Port(TABLE, "Selection", "The rows selected on the canvas")] if cls.interactive else [])

    def render(self, obj, s: dict, ctx) -> dict:
        raise NotImplementedError

    def select(self, df: pl.DataFrame, s: dict, selection) -> pl.DataFrame:
        """The rows of the selection (default: {"rows": [row indices]})."""
        rows = {i for i in (selection or {}).get("rows", []) if isinstance(i, int) and 0 <= i < df.height}
        return df.filter(pl.Series([i in rows for i in range(df.height)], dtype=pl.Boolean))

    # -- machinery --------------------------------------------------------
    def normalize_settings(self, old: dict, new: dict) -> dict:
        # a selection belongs to what is shown: other changes drop it
        if new.get("selection") is not None and not self.keeps_display(old, new):
            return dict(new, selection=None)
        return new

    def keeps_display(self, old: dict, new: dict) -> bool:
        """Only the selection changed: the display stays while the node runs again."""
        return all(old.get(k) == new.get(k) for k in set(old) | set(new) if k != "selection")

    def widget(self, s: dict, display) -> dict:
        return {
            "type": "output",
            "renderer": self.renderer,
            "title": s.get("title") or "",
            "width": self.width,
            "height": self.height,
            "data": display,
            "interactive": self.interactive,
            "selection": s.get("selection") if self.interactive else None,
        }

    def execute(self, inputs, s, ctx):
        obj = inputs[0]
        # (also on dry runs: render checks the settings against the input)
        ctx.set_display(self.render(obj, s, ctx))
        out = [obj]
        if self.interactive:
            out.append(self.select(obj, s, s.get("selection")))
        return out
