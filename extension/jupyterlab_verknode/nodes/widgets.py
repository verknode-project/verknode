"""Canvas widget nodes: inputs edited and outputs shown directly on the workflow canvas.

One node per kind, built on InputWidget / OutputWidget (engine/widgets.py);
custom node files add more kinds the same way.
"""
from __future__ import annotations

import base64
import math

import polars as pl

from verknode.node import CELL_DTYPES, IMAGE, F, InputWidget, InvalidSettingsError, OutputWidget, Port
from verknode.node import dtype_kind, is_numeric_kind, json_value, parse_cell, selected_columns

_INPUT_HELP = (
    " The value is edited directly on the node; changing it resets the nodes downstream and (with "
    "<i>Run on change</i>) executes the output widgets after it. ↺ on the node goes back to the default. "
    "The dialog sets the label, the output name and the default."
)


def _number(v, fallback=0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return fallback
    return f if math.isfinite(f) else fallback


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ----------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------
class TextInput(InputWidget):
    factory = "verknode.widgets.TextInput"
    name = "Text Input"
    icon = "Aa"
    control = "text"
    short_description = "A line of text entered on the canvas."
    description = "Outputs the text (one row) and pushes it as a String flow variable." + _INPUT_HELP
    widget_fields = [F("default", "string", "Default text", "")]

    def valid(self, s, value):
        return isinstance(value, str)

    def default_value(self, s):
        return s.get("default") or ""


class TextAreaInput(TextInput):
    factory = "verknode.widgets.TextAreaInput"
    name = "Text Area Input"
    icon = "¶"
    control = "textarea"
    short_description = "Several lines of text entered on the canvas."
    widget_fields = [F("default", "code", "Default text", "", language="text")]


class NumberInput(InputWidget):
    factory = "verknode.widgets.NumberInput"
    name = "Number Input"
    icon = "1.5"
    control = "number"
    dtype = pl.Float64
    variable_type = "DOUBLE"
    short_description = "A number entered on the canvas."
    description = "Outputs the number (one row, Double) and pushes it as a Double flow variable." + _INPUT_HELP
    widget_fields = [F("default", "float", "Default value", 0.0)]

    def valid(self, s, value):
        return _is_number(value)

    def default_value(self, s):
        return _number(s.get("default"))

    def coerce(self, s, value):
        return float(value)


class IntegerInput(NumberInput):
    factory = "verknode.widgets.IntegerInput"
    name = "Integer Input"
    icon = "123"
    control = "integer"
    dtype = pl.Int64
    variable_type = "INTEGER"
    short_description = "A whole number entered on the canvas."
    description = "Outputs the number (one row, Integer) and pushes it as an Integer flow variable." + _INPUT_HELP
    widget_fields = [F("default", "int", "Default value", 0)]

    def coerce(self, s, value):
        return int(round(value))


class SliderInput(NumberInput):
    factory = "verknode.widgets.SliderInput"
    name = "Slider Input"
    icon = "⊷"
    control = "slider"
    short_description = "A number chosen with a slider on the canvas."
    description = (
        "Outputs the slider position (one row, Double) and pushes it as a Double flow variable." + _INPUT_HELP
    )
    widget_fields = [
        F("default", "float", "Default value", 50.0),
        F("minimum", "float", "Minimum", 0.0),
        F("maximum", "float", "Maximum", 100.0),
        F("step", "float", "Step", 1.0),
    ]

    @staticmethod
    def bounds(s) -> tuple:
        lo, hi = _number(s.get("minimum"), 0.0), _number(s.get("maximum"), 100.0)
        return (lo, hi) if lo <= hi else (hi, lo)

    def valid(self, s, value):
        lo, hi = self.bounds(s)
        return _is_number(value) and lo <= value <= hi

    def default_value(self, s):
        lo, hi = self.bounds(s)
        return min(max(_number(s.get("default"), lo), lo), hi)

    def control_props(self, s):
        lo, hi = self.bounds(s)
        step = _number(s.get("step"), 1.0)
        return {"min": lo, "max": hi, "step": step if step > 0 else 1.0}


class CheckboxInput(InputWidget):
    factory = "verknode.widgets.CheckboxInput"
    name = "Checkbox Input"
    icon = "☑"
    control = "checkbox"
    dtype = pl.Boolean
    variable_type = "BOOLEAN"
    short_description = "A yes / no choice on the canvas."
    description = "Outputs true or false (one row, Boolean) and pushes it as a Boolean flow variable." + _INPUT_HELP
    widget_fields = [F("default", "bool", "Checked by default", False)]

    def valid(self, s, value):
        return isinstance(value, bool)

    def default_value(self, s):
        return bool(s.get("default"))


class DropdownInput(InputWidget):
    factory = "verknode.widgets.DropdownInput"
    name = "Drop-down Input"
    icon = "▾"
    control = "dropdown"
    short_description = "One of several choices, picked on the canvas."
    description = "Outputs the chosen text (one row) and pushes it as a String flow variable." + _INPUT_HELP
    widget_fields = [
        F("choices", "string", "Choices", "red, green, blue", "Comma-separated."),
        F("default", "string", "Default choice", "", "Empty: the first choice."),
    ]

    @staticmethod
    def choices(s) -> list:
        out = []
        for part in (s.get("choices") or "").replace("\n", ",").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
        return out

    def valid(self, s, value):
        return value in self.choices(s)

    def default_value(self, s):
        opts = self.choices(s)
        d = s.get("default") or ""
        return d if d in opts or not opts else opts[0]

    def control_props(self, s):
        return {"options": self.choices(s)}


DEFAULT_TABLE = {
    "columns": [{"name": "name", "type": "String"}, {"name": "amount", "type": "Double"}],
    "rows": [["a", "1"], ["b", "2"]],
}


class TableInput(InputWidget):
    factory = "verknode.widgets.TableInput"
    name = "Table Input"
    icon = "▤"
    control = "table"
    variable_type = None
    short_description = "A small table edited on the canvas."
    description = (
        "Outputs the table. The dialog sets the columns (names and types) and the starting cells; on the "
        "canvas cells are edited and rows added or removed." + _INPUT_HELP
    )
    widget_fields = [F("default", "tablecreator", "Default table", DEFAULT_TABLE, options=list(CELL_DTYPES))]

    def valid(self, s, value):
        # the columns come from the dialog; the canvas edits the cells
        return isinstance(value, dict) and value.get("columns") == (s.get("default") or {}).get("columns")

    def default_value(self, s):
        return s.get("default") or {"columns": [], "rows": []}

    def to_table(self, value, s, ctx):
        cols = value.get("columns", [])
        rows = value.get("rows", [])
        names = [c["name"] for c in cols]
        if len(set(names)) != len(names):
            raise InvalidSettingsError("Column names must be unique")
        data = {}
        for j, c in enumerate(cols):
            ctype = c.get("type", "String")
            values = []
            for i, row in enumerate(rows):
                try:
                    values.append(parse_cell(row[j] if j < len(row) else None, ctype))
                except (TypeError, ValueError) as e:
                    raise InvalidSettingsError(f'Row {i + 1}, column "{c["name"]}": {e}') from None
            data[c["name"]] = pl.Series(c["name"], values, dtype=CELL_DTYPES[ctype])
        return pl.DataFrame(data)


class MoleculeInput(InputWidget):
    factory = "verknode.widgets.MoleculeInput"
    name = "Molecule Input"
    icon = "⌬"
    control = "molecule"
    default_name = "Molecule"
    short_description = "A structure drawn with the sketcher from the canvas."
    description = (
        "Outputs the structure as a molecule column (one row) and pushes its SMILES as a String flow "
        "variable. The node shows the structure; double-click it to draw or edit it in the sketcher." + _INPUT_HELP
    )
    widget_fields = [F("default", "string", "Default molecule", "c1ccccc1", "SMILES or MolBlock.")]

    def valid(self, s, value):
        return isinstance(value, str)

    def default_value(self, s):
        return s.get("default") or ""

    def execute(self, inputs, s, ctx):
        from verknode.node import chem

        text = self.value_of(s)
        mol = chem.parse(text) if text.strip() else None
        if mol is None and text.strip():
            raise InvalidSettingsError("The molecule is not a valid structure")
        name = self.column_name(s)
        ctx.push_variable(name, chem.Chem.MolToSmiles(mol) if mol is not None else "", "STRING")
        return [pl.DataFrame([chem.mol_series(name, [mol])])]


# ----------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------
ALL = {"include": [], "exclude": [], "enforce": "exclusion"}
TIME_KINDS = ("date", "datetime")
_OUTPUT_HELP = (
    " Shown next to the node once executed (drag its title bar to move it, its corner to resize it). "
    "The first output passes the input on unchanged."
)
_SELECT_HELP = " The second output has the rows selected on the canvas"
_ROW = "__verknode_row__"


class TableDisplay(OutputWidget):
    """Renderer "table": {"columns": [{name, kind}], "rows": [[cell]], "total": n}; molecule cells
    are {"__molecule__": true, "svg", "smiles"}."""

    factory = "verknode.widgets.TableDisplay"
    name = "Table Display"
    icon = "▦"
    renderer = "table"
    interactive = True
    short_description = "Shows the first rows of a table on the canvas."
    description = (
        "Shows the first rows of the input table (molecules as structures)." + _OUTPUT_HELP + _SELECT_HELP +
        " (click a row; Ctrl+click adds or removes one, Shift+click a range)."
    )
    widget_fields = [F("rows", "int", "Rows shown", 10, "At most 100.")]

    def render(self, df, s, ctx):
        page = df.head(min(max(int(s.get("rows") or 10), 1), 100))
        columns = []
        for c, t in page.schema.items():
            k = dtype_kind(t)
            if k == "molecule":
                from verknode.node import chem

                cells = chem.view_cells(page[c])
            else:
                cells = [json_value(v) for v in page[c].to_list()]
            columns.append((c, k, cells))
        return {
            "columns": [{"name": c, "kind": k} for c, k, _ in columns],
            "rows": [list(r) for r in zip(*(cells for _, _, cells in columns))] if columns else [],
            "total": df.height,
        }


def _numeric(kinds: dict) -> list:
    return [c for c, k in kinds.items() if is_numeric_kind(k)]


class _ChartDisplay(OutputWidget):
    """Renderers "bar" / "line" / "scatter": {"x", "xKind", "series": [{name, values}]} plus
    "categories" (bar) or "xValues" (line, scatter)."""

    abstract = True
    max_series = 8  # more series cannot be told apart by colour
    widget_fields = [
        F("x", "column", "X axis column", None, "Empty: picked automatically."),
        F("y", "columns", "Y axis columns", ALL, "Numeric columns plotted against x."),
    ]

    def pick_x(self, kinds: dict) -> list:
        """Candidate x columns when none is set (the first one is used)."""
        return list(kinds)

    def x_of(self, df, s):
        """The x column in use (None: none fits)."""
        x = s.get("x") or (self.pick_x({c: dtype_kind(t) for c, t in df.schema.items()}) or [None])[0]
        return x if x in df.columns else None

    def axes(self, df, s, ctx) -> tuple:
        x = s.get("x")
        if x and x not in df.columns:
            raise InvalidSettingsError(f'Column "{x}" not found in input table')
        if not x:
            pick = self.pick_x({c: dtype_kind(t) for c, t in df.schema.items()})
            if not pick:
                raise InvalidSettingsError("The input has no column for the x axis")
            x = pick[0]
        ys = [c for c in selected_columns(s.get("y") or ALL, df.schema)
              if c != x and is_numeric_kind(dtype_kind(df.schema[c]))]
        if len(ys) > self.max_series:
            ctx.warn(f"Showing the first {self.max_series} of {len(ys)} y columns")
            ys = ys[: self.max_series]
        return x, ys

    @staticmethod
    def series(data, ys) -> list:
        return [{"name": c, "values": [json_value(v) for v in data[c].cast(pl.Float64).to_list()]} for c in ys]


class BarChartDisplay(_ChartDisplay):
    factory = "verknode.widgets.BarChartDisplay"
    name = "Bar Chart Display"
    icon = "▥"
    renderer = "bar"
    interactive = True
    max_bars = 40
    short_description = "Bar chart of the input on the canvas."
    description = (
        "Groups the rows by the x column and combines the y columns (sum, mean or count; <i>None</i> "
        "draws one bar per row). At most 40 bars and 8 series." + _OUTPUT_HELP + _SELECT_HELP +
        " (click a bar; Ctrl+click adds or removes one): the rows of its x value."
    )
    widget_fields = _ChartDisplay.widget_fields + [
        F("aggregate", "enum", "Combine rows with the same x", "Sum", options=["Sum", "Mean", "Count", "None"]),
    ]

    def pick_x(self, kinds):
        return [c for c, k in kinds.items() if k in ("string", "boolean")] or list(kinds)

    def render(self, df, s, ctx):
        x, ys = self.axes(df, s, ctx)
        how = s.get("aggregate") or "Sum"
        if how == "Count":
            data = df.group_by(x, maintain_order=True).agg(pl.len().alias("count"))
            ys = ["count"]
        else:
            if not ys:
                raise InvalidSettingsError("Select at least one numeric column for the y axis")
            if how == "None":
                data = df.with_row_index(_ROW).select([_ROW, x, *ys])
            else:
                agg = pl.Expr.sum if how == "Sum" else pl.Expr.mean
                data = df.group_by(x, maintain_order=True).agg([agg(pl.col(c)).alias(c) for c in ys])
        if data.height > self.max_bars:
            ctx.warn(f"Showing the first {self.max_bars} of {data.height} bars")
            data = data.head(self.max_bars)
        out = {"x": x, "xKind": dtype_kind(df.schema[x]), "categories": self.categories(data[x]),
               "series": self.series(data, ys)}
        if _ROW in data.columns:  # one bar per row
            out["rows"] = data[_ROW].to_list()
        return out

    @staticmethod
    def categories(series) -> list:
        return ["(missing)" if v is None else str(json_value(v)) for v in series.to_list()]

    def select(self, df, s, selection):
        # {"categories": [...]}: the rows of those bars; {"rows": [...]}: bars of single rows
        cats = set((selection or {}).get("categories") or [])
        x = self.x_of(df, s)
        if not cats or x is None:
            return super().select(df, s, selection)
        return df.filter(pl.Series([c in cats for c in self.categories(df[x])], dtype=pl.Boolean))


class LineChartDisplay(_ChartDisplay):
    factory = "verknode.widgets.LineChartDisplay"
    name = "Line Chart Display"
    icon = "〽"
    renderer = "line"
    max_points = 500
    short_description = "Line chart of the input on the canvas."
    description = (
        "Plots the y columns against the x column (sorted when numeric or a date; text keeps the row "
        "order). At most 8 series; long tables are thinned to 500 points." + _OUTPUT_HELP + _SELECT_HELP +
        " (drag across the chart): the rows in that x range."
    )
    interactive = True

    def pick_x(self, kinds):
        times = [c for c, k in kinds.items() if k in TIME_KINDS]
        numeric = _numeric(kinds)
        # (a numeric x only when another numeric column is left for y)
        return times or (numeric if len(numeric) >= 2 else [c for c in kinds if c not in numeric])

    def render(self, df, s, ctx):
        x, ys = self.axes(df, s, ctx)
        if not ys:
            raise InvalidSettingsError("Select at least one numeric column for the y axis")
        xkind = dtype_kind(df.schema[x])
        data = df.with_row_index(_ROW).select([_ROW, x, *ys]).filter(pl.col(x).is_not_null())
        if is_numeric_kind(xkind) or xkind in TIME_KINDS:
            data = data.sort(x)
        if data.height > self.max_points:
            ctx.warn(f"Showing {self.max_points} of {data.height} points")
            data = data.gather_every(math.ceil(data.height / self.max_points))
        return {"x": x, "xKind": xkind, "xValues": [json_value(v) for v in data[x].to_list()],
                "series": self.series(data, ys), "rows": data[_ROW].to_list()}

    def select(self, df, s, selection):
        # the frontend sends the points in the dragged range; thinned-out rows between them count too
        rows = [i for i in (selection or {}).get("rows", []) if isinstance(i, int) and 0 <= i < df.height]
        x = self.x_of(df, s)
        if not rows or x is None:
            return super().select(df, s, selection)
        kind = dtype_kind(df.schema[x])
        if is_numeric_kind(kind) or kind in TIME_KINDS:
            picked = df[x].gather(rows)
            return df.filter(pl.col(x).is_between(picked.min(), picked.max()))
        return df.slice(min(rows), max(rows) - min(rows) + 1)


class ScatterPlotDisplay(LineChartDisplay):
    factory = "verknode.widgets.ScatterPlotDisplay"
    name = "Scatter Plot Display"
    icon = "⁙"
    renderer = "scatter"
    max_series = 3  # point clouds in more colours cannot be told apart
    max_points = 1000
    short_description = "Scatter plot of the input on the canvas."
    description = (
        "Plots the y columns against a numeric (or date) x column as points. At most 3 series and "
        "1000 points." + _OUTPUT_HELP + _SELECT_HELP +
        " (drag a rectangle, or click a point; Ctrl adds or removes)."
    )

    def pick_x(self, kinds):
        numeric = _numeric(kinds)
        return numeric if len(numeric) >= 2 else []

    def select(self, df, s, selection):
        return OutputWidget.select(self, df, s, selection)

    def render(self, df, s, ctx):
        x = s.get("x")
        if x and x in df.columns:
            k = dtype_kind(df.schema[x])
            if not (is_numeric_kind(k) or k in TIME_KINDS):
                raise InvalidSettingsError(f'The x axis column "{x}" must be numeric or a date')
        return super().render(df, s, ctx)


class ValueDisplay(OutputWidget):
    """Renderer "value": {"column", "how", "value", "numeric"}."""

    factory = "verknode.widgets.ValueDisplay"
    name = "Value Display"
    icon = "#"
    renderer = "value"
    width, height = 200, 90
    short_description = "One value of the input, in large type on the canvas."
    description = (
        "Shows one value of a column: the first or last row, or the sum, mean, minimum, maximum or "
        "count of the column." + _OUTPUT_HELP
    )
    widget_fields = [
        F("column", "column", "Column", None, "Empty: the first column."),
        F("value_of", "enum", "Value", "First row",
          options=["First row", "Last row", "Sum", "Mean", "Minimum", "Maximum", "Count"]),
    ]

    def render(self, df, s, ctx):
        col = s.get("column") or (df.columns[0] if df.columns else None)
        if col is None:
            raise InvalidSettingsError("The input has no columns")
        if col not in df.columns:
            raise InvalidSettingsError(f'Column "{col}" not found in input table')
        how = s.get("value_of") or "First row"
        series = df[col]
        numeric = is_numeric_kind(dtype_kind(series.dtype))
        if how in ("Sum", "Mean", "Minimum", "Maximum") and not numeric:
            raise InvalidSettingsError(f'"{how}" needs a numeric column')
        if how == "Count":
            v = series.drop_nulls().len()
        elif how in ("First row", "Last row"):
            v = (series[0] if how == "First row" else series[-1]) if df.height else None
        else:
            v = {"Sum": series.sum, "Mean": series.mean, "Minimum": series.min, "Maximum": series.max}[how]()
        return {"column": col, "how": how, "value": json_value(v), "numeric": numeric or how == "Count"}


class TextDisplay(OutputWidget):
    """Renderer "markdown": {"text"}."""

    factory = "verknode.widgets.TextDisplay"
    name = "Text Display"
    icon = "¶"
    renderer = "markdown"
    width, height = 380, 240
    short_description = "Text of the input on the canvas, as Markdown (e.g. an LLM summary)."
    description = (
        "Shows the text of a column - the first row, or all rows one after another - formatted as Markdown "
        "(paragraphs, lists, **bold**, `code`, tables): for summaries, notes, reports." + _OUTPUT_HELP
    )
    widget_fields = [
        F("column", "column", "Column", None, "Empty: the first text column.", kinds=["string"]),
        F("rows", "enum", "Rows", "First row", options=["First row", "All rows"]),
    ]

    def render(self, df, s, ctx):
        col = s.get("column") or next((c for c, t in df.schema.items() if t == pl.String), None)
        if col is None or col not in df.columns:
            raise InvalidSettingsError("The input has no text column" if col is None else f'Column "{col}" not found')
        values = [str(v) for v in df[col].to_list() if v is not None]
        text = values[0] if values and s.get("rows") != "All rows" else "\n\n".join(values)
        return {"text": text, "column": col}


class ImageDisplay(OutputWidget):
    """Renderer "image": {"mime", "data" (base64)}."""

    factory = "verknode.widgets.ImageDisplay"
    name = "Image Display"
    icon = "IMG"
    renderer = "image"
    width, height = 320, 240
    max_bytes = 8 * 1024 * 1024
    in_ports = [Port(IMAGE, "Image to show")]
    short_description = "Shows an image (from a square image port) on the canvas."
    description = (
        "Shows the image arriving at the image input port (SVG, PNG, ...), e.g. of a chart node, scaled "
        "to the display or at its own size." + _OUTPUT_HELP
    )
    widget_fields = [F("fit", "enum", "Size", "Fit to the display", options=["Fit to the display", "Actual size"])]

    def render(self, img, s, ctx):
        if not img.data:  # (the spec while configuring)
            return None
        if len(img.data) > self.max_bytes:
            raise InvalidSettingsError(f"The image is too large to show ({len(img.data) // 1024} kB)")
        return {"mime": img.mime, "data": base64.b64encode(img.data).decode("ascii"),
                "fit": (s.get("fit") or "Fit to the display") == "Fit to the display"}
