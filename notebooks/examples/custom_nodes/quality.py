"""Example nodes for data quality: a view, a flow variable output, and a helper import."""
import polars as pl

from verknode.node import (
    F,
    InvalidSettingsError,
    NodeModel,
    Port,
    TABLE,
    selected_columns,
    views,
)

from ._text import clean_text


class ZScoreOutlierFlagger(NodeModel):
    factory = "example.ZScoreOutlierFlagger"
    name = "Z-Score Outlier Flagger"
    category = "Custom/Examples"
    icon = "z!"
    has_view = True  # right-click > Open View
    short_description = "Flags rows whose values lie far from the column mean."
    description = (
        "Computes z-scores (value - mean) / std for the selected numeric columns and appends a "
        "Boolean column that is true when any |z| exceeds the threshold. Pushes the flow "
        "variable <code>outlier_count</code>. The view shows the z-score distributions."
    )
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Table with outlier flag")]
    fields = [
        F("columns", "columns", "Numeric columns", {"include": [], "exclude": [], "enforce": "inclusion"},
          kinds=["integer", "long", "double"]),
        F("threshold", "float", "|z| threshold", 3.0),
        F("flag_column", "string", "Flag column name", "outlier"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = [c for c in selected_columns(s["columns"], df.schema) if df.schema[c].is_numeric()]
        if not cols:
            raise InvalidSettingsError("Select at least one numeric column")
        z = df.select(((pl.col(c) - pl.col(c).mean()) / pl.col(c).std()).alias(c) for c in cols)
        flag = pl.any_horizontal([pl.col(c).abs() > s["threshold"] for c in cols]).fill_null(False)
        out = df.with_columns(z.select(flag).to_series().alias(s["flag_column"] or "outlier"))
        count = int(out[s["flag_column"] or "outlier"].sum()) if out.height else 0
        ctx.push_variable("outlier_count", count, "INTEGER")
        if not ctx.dry:
            print(f"{count} of {df.height} rows flagged (|z| > {s['threshold']})")
            fig, ax = views.figure(8, 4)
            for c in cols:
                ax.hist(z[c].drop_nulls().to_list(), bins=30, alpha=0.6, label=c)
            for t in (-s["threshold"], s["threshold"]):
                ax.axvline(t, color="#e15759", linestyle="--")
            ax.set_xlabel("z-score")
            ax.set_ylabel("rows")
            ax.legend()
            ctx.set_view(views.page(
                "Z-Score Outlier Flagger",
                views.key_values({"Rows": df.height, "Flagged": count, "Threshold": s["threshold"]}),
                views.chart(views.svg(fig)),
                "<h2>Flagged rows</h2>",
                views.table(out.filter(pl.col(s["flag_column"] or "outlier")), max_rows=50),
            ))
        return [out]


class TextCleaner(NodeModel):
    factory = "example.TextCleaner"
    name = "Text Cleaner"
    category = "Custom/Examples"
    icon = "Aa"
    short_description = "Trims, collapses whitespace, lower-cases and strips non-ASCII text."
    description = "Cleans the selected String columns in place (implementation in the helper module _text.py)."
    in_ports = [Port(TABLE, "Input table")]
    out_ports = [Port(TABLE, "Cleaned table")]
    fields = [
        F("columns", "columns", "String columns", {"include": [], "exclude": [], "enforce": "inclusion"},
          kinds=["string"]),
        F("lower", "bool", "Lower case", True),
        F("collapse", "bool", "Collapse whitespace", True),
        F("ascii_only", "bool", "Remove non-ASCII characters", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = [c for c in selected_columns(s["columns"], df.schema) if df.schema[c] == pl.String]
        return [df.with_columns(
            clean_text(pl.col(c), s["lower"], s["collapse"], s["ascii_only"]).alias(c) for c in cols)]
