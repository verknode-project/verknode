"""Column split & combine, binning and transformation nodes."""
from __future__ import annotations

import csv
import math
import re

import polars as pl

from verknode.node import (
    F,
    InvalidSettingsError,
    MODEL,
    ModelPortObject,
    NodeModel,
    Port,
    TABLE,
    require_column,
)
from verknode.node import dtype_kind, is_numeric_kind
from .manipulation import selected_columns

IN = [Port(TABLE, "Input table")]
ALL = {"include": [], "exclude": [], "enforce": "exclusion"}
NONE = {"include": [], "exclude": [], "enforce": "inclusion"}
NUMERIC = ["integer", "long", "double"]
CAT_SPLIT = "Manipulation/Column/Split & Combine"
CAT_BIN = "Manipulation/Column/Binning"
CAT_TRANSFORM = "Manipulation/Column/Transform"


def _unescape(sep: str) -> str:
    return sep.encode("utf-8").decode("unicode_escape") if sep and "\\" in sep else sep


def _numeric_columns(sel, schema) -> list:
    cols = selected_columns(sel, schema)
    bad = [c for c in cols if not is_numeric_kind(dtype_kind(schema[c]))]
    if bad:
        raise InvalidSettingsError(f"Columns are not numeric: {', '.join(bad)}")
    return cols


def _unique_name(name: str, existing) -> str:
    existing = set(existing)
    if name not in existing:
        return name
    i = 1
    while f"{name} (#{i})" in existing:
        i += 1
    return f"{name} (#{i})"


# ----------------------------------------------------------------------
# Split & Combine
# ----------------------------------------------------------------------
class ColumnSplitter(NodeModel):
    factory = "verknode.manipulation.ColumnSplitter"
    name = "Column Splitter"
    category = CAT_SPLIT
    icon = "▥|▥"
    short_description = "Splits the columns of a table into two tables."
    description = "Included columns go to the bottom output, all others to the top output. Rows are unchanged."
    in_ports = IN
    out_ports = [Port(TABLE, "Top table", "Columns not selected"), Port(TABLE, "Bottom table", "Selected columns")]
    fields = [F("columns", "columns", "Columns for the bottom table", dict(NONE))]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        bottom = selected_columns(s["columns"], df.schema)
        return [df.select([c for c in df.columns if c not in bottom]), df.select(bottom)]


def _split_values(series: pl.Series, delimiter: str, quote: str, trim: bool) -> list:
    out = []
    for v in series.to_list():
        if v is None:
            out.append(None)
            continue
        if quote:
            parts = next(csv.reader([v], delimiter=delimiter, quotechar=quote), [])
        else:
            parts = v.split(delimiter)
        out.append([p.strip() for p in parts] if trim else parts)
    return out


class CellSplitter(NodeModel):
    factory = "verknode.manipulation.CellSplitter"
    name = "Cell Splitter"
    category = CAT_SPLIT
    icon = "a|b"
    short_description = "Splits the content of a String column at a delimiter."
    description = (
        "Splits each cell of the selected column at the delimiter and outputs the parts as new columns "
        "(named <code>column_Arr[i]</code>), as a list, or as a set. Quote characters protect delimiters "
        "inside quoted parts."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with split column")]
    fields = [
        F("column", "column", "Column to split", None, kinds=["string"]),
        F("delimiter", "string", "Enter a delimiter", ",", "Use \\t for tab."),
        F("quote", "string", "Quotation character", "", "Leave empty for no quoting."),
        F("trim", "bool", "Remove leading and trailing white space chars (trim)", True),
        F("output", "enum", "Output", "As new columns", options=["As new columns", "As list", "As set (remove duplicates)"]),
        F("size_mode", "enum", "Number of output columns", "Set array size",
          options=["Set array size", "Guess size and column types"], show_if={"output": "As new columns"},
          description="Guessing requires execution before downstream nodes see the columns."),
        F("size", "int", "Array size", 2, show_if={"output": "As new columns", "size_mode": "Set array size"}),
        F("missing", "enum", "Missing value handling", "Create missing cells",
          options=["Create missing cells", "Create empty string cells"], show_if={"output": "As new columns"},
          description="For rows with fewer parts than output columns."),
        F("remove_input", "bool", "Remove input column", False),
    ]

    def configure(self, in_specs, s, ctx):
        if s["output"] == "As new columns" and s["size_mode"] != "Set array size":
            require_column(s, "column", in_specs[0])
            return [None]
        return super().configure(in_specs, s, ctx)

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        if dtype_kind(df.schema[col]) != "string":
            raise InvalidSettingsError(f'Column "{col}" is not a String column')
        delim = _unescape(s["delimiter"])
        if not delim:
            raise InvalidSettingsError("No delimiter specified")
        quote = (s["quote"] or "")[:1]
        parts = _split_values(df[col], delim, quote, s["trim"])
        if s["output"] == "As list":
            new = [pl.Series(f"{col}_SplitResultList", parts, dtype=pl.List(pl.String))]
        elif s["output"].startswith("As set"):
            sets = [None if p is None else list(dict.fromkeys(p)) for p in parts]
            new = [pl.Series(f"{col}_SplitResultSet", sets, dtype=pl.List(pl.String))]
        else:
            if s["size_mode"] == "Set array size":
                n = max(1, s["size"])
            else:
                n = max([len(p) for p in parts if p is not None] or [0])
            fill = "" if s["missing"] == "Create empty string cells" else None
            new = []
            for i in range(n):
                values = [None if p is None else (p[i] if i < len(p) else fill) for p in parts]
                new.append(pl.Series(f"{col}_Arr[{i}]", values, dtype=pl.String))
            if s["size_mode"] != "Set array size" and n:
                new = [_guess_type(x) for x in new]
        names = [c for c in df.columns if not (s["remove_input"] and c == col)]
        out = df.select(names)
        for series in new:
            out = out.with_columns(series.alias(_unique_name(series.name, out.columns)))
        return [out]


def _guess_type(series: pl.Series) -> pl.Series:
    non_null = series.drop_nulls()
    if non_null.len() == 0:
        return series
    stripped = series.str.strip_chars()
    for dtype in (pl.Int64, pl.Float64):
        cast = stripped.cast(dtype, strict=False)
        if cast.null_count() == series.null_count():
            return cast
    return series


class CellSplitterByPosition(NodeModel):
    factory = "verknode.manipulation.CellSplitterByPosition"
    name = "Cell Splitter By Position"
    category = CAT_SPLIT
    icon = "ab|c"
    short_description = "Splits String cells at fixed character positions."
    description = (
        "Splits the selected String column at the given split indices (e.g. <code>3,5</code> creates three "
        "parts: characters 0-2, 3-4 and 5-end). Provide one new column name per part."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with split columns")]
    fields = [
        F("column", "column", "Column to split", None, kinds=["string"]),
        F("positions", "string", "Split indices", "3", "Comma separated, ascending character positions."),
        F("names", "string", "New column names", "split_0,split_1", "Comma separated, one more than split indices."),
        F("remove_input", "bool", "Remove input column", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        try:
            pos = [int(p) for p in (s["positions"] or "").split(",") if p.strip()]
        except ValueError:
            raise InvalidSettingsError("Split indices must be integers") from None
        if not pos or pos != sorted(pos) or pos[0] <= 0:
            raise InvalidSettingsError("Split indices must be positive and ascending")
        names = [n.strip() for n in (s["names"] or "").split(",") if n.strip()]
        if len(names) != len(pos) + 1:
            raise InvalidSettingsError(f"Expected {len(pos) + 1} column names, got {len(names)}")
        bounds = [0] + pos + [None]
        c = pl.col(col).cast(pl.String)
        exprs = [
            (c.str.slice(a, None) if b is None else c.str.slice(a, b - a)).alias(n)
            for a, b, n in zip(bounds[:-1], bounds[1:], names)
        ]
        out = df.drop(col) if s["remove_input"] else df
        for n in names:
            if n in out.columns:
                raise InvalidSettingsError(f'Column "{n}" already exists')
        return [out.with_columns(exprs)]


class RegexSplit(NodeModel):
    factory = "verknode.manipulation.RegexSplit"
    name = "Regex Split"
    category = CAT_SPLIT
    icon = "(.*)"
    short_description = "Splits a String column using capture groups of a regular expression."
    description = (
        "Each capture group of the pattern becomes a new column (<code>split_0</code>, <code>split_1</code>, "
        "...). Rows that do not match get missing values."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with split columns")]
    fields = [
        F("column", "column", "Target column", None, kinds=["string"]),
        F("pattern", "string", "Pattern", r"(.*)-(.*)", "Regular expression with capture groups ( )."),
        F("case_insensitive", "bool", "Case insensitive", False),
        F("output", "enum", "Output", "As new columns", options=["As new columns", "As list"]),
        F("remove_input", "bool", "Remove input column", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        pattern = s["pattern"] or ""
        try:
            groups = re.compile(pattern).groups
        except re.error as e:
            raise InvalidSettingsError(f"Invalid pattern: {e}") from None
        if groups == 0:
            raise InvalidSettingsError("Pattern has no capture groups")
        full = f"^(?:{pattern})$"
        if s["case_insensitive"]:
            full = "(?i)" + full
        c = pl.col(col).cast(pl.String)
        exprs = [c.str.extract(full, i + 1).alias(f"split_{i}") for i in range(groups)]
        out = df.drop(col) if s["remove_input"] else df
        if s["output"] == "As list":
            name = _unique_name("split", out.columns)
            return [out.with_columns(pl.concat_list(exprs).alias(name))]
        for i in range(groups):
            if f"split_{i}" in out.columns:
                raise InvalidSettingsError(f'Column "split_{i}" already exists')
        return [out.with_columns(exprs)]


class ColumnCombiner(NodeModel):
    factory = "verknode.manipulation.ColumnCombiner"
    name = "Column Combiner"
    category = CAT_SPLIT
    icon = "a+b"
    short_description = "Combines the content of columns into a new String column."
    description = (
        "Concatenates the string representation of the selected columns using the delimiter. "
        "Missing values are written as <code>?</code>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with combined column")]
    fields = [
        F("columns", "columns", "Columns to combine", dict(ALL)),
        F("delimiter", "string", "Delimiter", ","),
        F("quoting", "enum", "Quoting", "Quote only when needed",
          options=["Quote only when needed", "Quote always", "Replace delimiter"]),
        F("quote", "string", "Quote character", '"', show_if={"quoting": ["Quote only when needed", "Quote always"]}),
        F("replacement", "string", "Replace delimiter by", "", show_if={"quoting": "Replace delimiter"}),
        F("new_column", "string", "Name of appended column", "combined string"),
        F("remove_included", "bool", "Remove included columns", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        delim = _unescape(s["delimiter"] or "")
        quote = (s["quote"] or "")[:1]
        mode = s["quoting"]

        def fmt(v):
            text = "?" if v is None else str(v)
            if mode == "Replace delimiter":
                return text.replace(delim, s["replacement"] or "") if delim else text
            if mode == "Quote always" or (delim and delim in text) or (quote and quote in text):
                if quote:
                    return quote + text.replace(quote, quote + quote) + quote
            return text

        texts = [[fmt(v) for v in df[c].to_list()] for c in cols]
        values = [delim.join(parts) for parts in zip(*texts)] if texts else []
        out = df.drop(cols) if s["remove_included"] else df
        name = s["new_column"] or "combined string"
        if name in out.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        return [out.with_columns(pl.Series(name, values, dtype=pl.String))]


AGGREGATORS = {
    "Sum": (True, lambda l: l.list.sum()),
    "Mean": (True, lambda l: l.list.mean()),
    "Median": (True, lambda l: l.list.median()),
    "Minimum": (True, lambda l: l.list.min()),
    "Maximum": (True, lambda l: l.list.max()),
    "Range": (True, lambda l: l.list.max() - l.list.min()),
    "Standard deviation": (True, lambda l: l.list.std()),
    "Variance": (True, lambda l: l.list.var()),
    "Count": (False, lambda l: l.list.drop_nulls().list.len().cast(pl.Int32)),
    "Missing value count": (False, lambda l: (l.list.len() - l.list.drop_nulls().list.len()).cast(pl.Int32)),
    "Unique count": (False, lambda l: l.list.drop_nulls().list.n_unique().cast(pl.Int32)),
    "First": (False, lambda l: l.list.first()),
    "Last": (False, lambda l: l.list.last()),
    "Concatenate": (False, lambda l: l.list.drop_nulls().list.join(", ")),
    "List": (False, lambda l: l),
    "Set": (False, lambda l: l.list.drop_nulls().list.unique(maintain_order=True)),
}


class ColumnAggregator(NodeModel):
    factory = "verknode.manipulation.ColumnAggregator"
    name = "Column Aggregator"
    category = CAT_SPLIT
    icon = "Σ→"
    short_description = "Aggregates the values of selected columns row by row."
    description = (
        "For each row, aggregates the values of the selected columns (sum, mean, count, concatenate, list, ...). "
        "Numeric aggregations require numeric columns; other aggregations convert values to String when the "
        "column types differ. Output columns are named <code>Method</code> unless a name is given."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with aggregated columns")]
    fields = [
        F("columns", "columns", "Aggregation columns", dict(NONE)),
        F("aggregations", "records", "Aggregation settings", [{"method": "Sum", "name": ""}], fields=[
            F("method", "enum", "Aggregation", "Sum", options=list(AGGREGATORS)),
            F("name", "string", "Output name", ""),
        ]),
        F("remove_aggregated", "bool", "Remove aggregation columns", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No aggregation columns selected")
        aggs = [a for a in s["aggregations"] if a.get("method")]
        if not aggs:
            raise InvalidSettingsError("No aggregation method selected")
        numeric = all(is_numeric_kind(dtype_kind(df.schema[c])) for c in cols)
        same = len({df.schema[c] for c in cols}) == 1
        out = df.drop(cols) if s["remove_aggregated"] else df
        exprs = []
        for a in aggs:
            needs_numeric, fn = AGGREGATORS[a["method"]]
            if needs_numeric:
                if not numeric:
                    raise InvalidSettingsError(f'"{a["method"]}" requires numeric columns')
                lst = pl.concat_list([pl.col(c).cast(pl.Float64) for c in cols])
            elif a["method"] == "Concatenate" or (not same and not numeric):
                lst = pl.concat_list([pl.col(c).cast(pl.String) for c in cols])
            elif not same:
                lst = pl.concat_list([pl.col(c).cast(pl.Float64) for c in cols])
            else:
                lst = pl.concat_list([pl.col(c) for c in cols])
            name = a.get("name") or a["method"]
            if name in out.columns or name in [e.meta.output_name() for e in exprs]:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            exprs.append(fn(lst).alias(name))
        return [out.with_columns(exprs)]


class ColumnAppender(NodeModel):
    factory = "verknode.manipulation.ColumnAppender"
    name = "Column Appender"
    category = CAT_SPLIT
    icon = "▥+▥"
    short_description = "Appends the columns of the input tables side by side."
    description = (
        "Combines the columns of two (or three) tables row by row by position. Duplicate column names "
        "get a suffix <code>(#1)</code>."
    )
    in_ports = [Port(TABLE, "First table"), Port(TABLE, "Second table"), Port(TABLE, "Third table", optional=True)]
    out_ports = [Port(TABLE, "Table with appended columns")]
    fields = [
        F("mismatch", "enum", "If row counts differ", "Fill with missing values",
          options=["Fill with missing values", "Cut to shortest table", "Fail"]),
    ]

    def execute(self, inputs, s, ctx):
        tables = [t for t in inputs if t is not None]
        heights = {t.height for t in tables}
        if len(heights) > 1:
            if s["mismatch"] == "Fail":
                raise ValueError(f"Tables have different row counts: {sorted(heights)}")
            if s["mismatch"] == "Cut to shortest table":
                n = min(heights)
                tables = [t.head(n) for t in tables]
        result = tables[0]
        for t in tables[1:]:
            renamed = {}
            for c in t.columns:
                if c in result.columns or c in renamed.values():
                    renamed[c] = _unique_name(c, list(result.columns) + list(renamed.values()) + t.columns)
            t = t.rename(renamed)
            n = max(result.height, t.height)
            result = pl.concat([_pad(result, n), _pad(t, n)], how="horizontal")
        return [result]


def _pad(df: pl.DataFrame, n: int) -> pl.DataFrame:
    if df.height >= n:
        return df
    filler = pl.DataFrame({c: pl.Series(c, [None] * (n - df.height), dtype=t) for c, t in df.schema.items()})
    return pl.concat([df, filler]) if df.width else df


class CrossJoiner(NodeModel):
    factory = "verknode.manipulation.CrossJoiner"
    name = "Cross Joiner"
    category = CAT_SPLIT
    icon = "×"
    short_description = "Builds the cross product (Cartesian product) of two tables."
    description = "Joins every row of the top table with every row of the bottom table."
    in_ports = [Port(TABLE, "Top table"), Port(TABLE, "Bottom table")]
    out_ports = [Port(TABLE, "Cross product")]
    fields = [F("suffix", "string", "Suffix for duplicate columns", " (#1)")]

    def execute(self, inputs, s, ctx):
        top, bottom = inputs
        return [top.join(bottom, how="cross", suffix=s["suffix"] or " (#1)")]


class SplitCollectionColumn(NodeModel):
    factory = "verknode.manipulation.SplitCollectionColumn"
    name = "Split Collection Column"
    category = CAT_SPLIT
    icon = "[]→"
    short_description = "Splits a collection (list) column into one column per element."
    description = (
        "Creates one column per element of the selected collection column (<code>Split Value 1</code>, ...). "
        "With <i>Number of columns</i> 0 the count is determined from the data, so downstream nodes only "
        "see the columns after execution."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with split columns")]
    fields = [
        F("column", "column", "Collection column", None, kinds=["list"]),
        F("count", "int", "Number of columns (0: from data)", 0),
        F("remove_input", "bool", "Remove collection column", True),
    ]

    def configure(self, in_specs, s, ctx):
        if s["count"] <= 0:
            require_column(s, "column", in_specs[0])
            return [None]
        return super().configure(in_specs, s, ctx)

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        if dtype_kind(df.schema[col]) != "list":
            raise InvalidSettingsError(f'Column "{col}" is not a collection column')
        c = pl.col(col)
        if isinstance(df.schema[col], pl.Array):
            c = c.arr.to_list()
        n = s["count"]
        if n <= 0:
            lens = df.select(c.list.len().max()).item() if df.height else 0
            n = int(lens or 0)
        out = df.drop(col) if s["remove_input"] else df
        exprs = [c.list.get(i, null_on_oob=True).alias(_unique_name(f"Split Value {i + 1}", out.columns))
                 for i in range(n)]
        return [df.with_columns(exprs).select(list(out.columns) + [e.meta.output_name() for e in exprs])]


class CreateCollectionColumn(NodeModel):
    factory = "verknode.manipulation.CreateCollectionColumn"
    name = "Create Collection Column"
    category = CAT_SPLIT
    icon = "→[]"
    short_description = "Combines columns into a collection (list or set) column."
    description = "Creates a List or Set column from the selected columns. Values are converted to String if types differ."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with collection column")]
    fields = [
        F("columns", "columns", "Columns", dict(NONE)),
        F("type", "enum", "Collection type", "List", options=["List", "Set"]),
        F("ignore_missing", "bool", "Ignore missing values", True),
        F("remove_included", "bool", "Remove included columns", False),
        F("new_column", "string", "New collection column name", "AggregatedValues"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        types = {df.schema[c] for c in cols}
        if len(types) == 1:
            parts = [pl.col(c) for c in cols]
        elif all(is_numeric_kind(dtype_kind(t)) for t in types):
            parts = [pl.col(c).cast(pl.Float64) for c in cols]
        else:
            parts = [pl.col(c).cast(pl.String) for c in cols]
        lst = pl.concat_list(parts)
        if s["ignore_missing"]:
            lst = lst.list.drop_nulls()
        if s["type"] == "Set":
            lst = lst.list.unique(maintain_order=True)
        out = df.drop(cols) if s["remove_included"] else df
        name = s["new_column"] or "AggregatedValues"
        if name in out.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        return [df.with_columns(lst.alias(name)).select(list(out.columns) + [name])]


# ----------------------------------------------------------------------
# Binning
# ----------------------------------------------------------------------
def _bound(text, default):
    text = "" if text is None else str(text).strip()
    if text.lower() in ("", "-inf", "inf", "+inf", "-infinity", "infinity"):
        return default
    try:
        return float(text)
    except ValueError:
        raise InvalidSettingsError(f"Invalid bin boundary: {text!r}") from None


class NumericBinner(NodeModel):
    factory = "verknode.manipulation.NumericBinner"
    name = "Numeric Binner"
    category = CAT_BIN
    icon = "▁▃▅"
    short_description = "Assigns numeric values to user-defined intervals (bins)."
    description = (
        "Defines named intervals per column. Lower/upper bounds may be empty for -∞/+∞. Values in no "
        "interval become missing. The binned column is appended as <code>column [Binned]</code> or replaces "
        "the original column."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Binned data")]
    fields = [
        F("bins", "records", "Intervals", [
            {"column": None, "bin": "Bin 1", "lower": "", "lower_inclusive": True, "upper": "0", "upper_inclusive": False},
            {"column": None, "bin": "Bin 2", "lower": "0", "lower_inclusive": True, "upper": "", "upper_inclusive": True},
        ], fields=[
            F("column", "column", "Column", kinds=NUMERIC),
            F("bin", "string", "Bin name", ""),
            F("lower", "string", "Lower bound", ""),
            F("lower_inclusive", "bool", "[", True),
            F("upper", "string", "Upper bound", ""),
            F("upper_inclusive", "bool", "]", False),
        ]),
        F("mode", "enum", "Output", "Append column", options=["Append column", "Replace column"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        by_col: dict[str, list] = {}
        for b in s["bins"]:
            if not b.get("column"):
                continue
            if b["column"] not in df.schema:
                raise InvalidSettingsError(f'Column "{b["column"]}" not found')
            if not is_numeric_kind(dtype_kind(df.schema[b["column"]])):
                raise InvalidSettingsError(f'Column "{b["column"]}" is not numeric')
            by_col.setdefault(b["column"], []).append(b)
        if not by_col:
            raise InvalidSettingsError("No intervals defined")
        exprs = []
        for col, bins in by_col.items():
            c = pl.col(col)
            chain = None
            for b in bins:
                lo, hi = _bound(b.get("lower"), -math.inf), _bound(b.get("upper"), math.inf)
                cond = (c >= lo if b.get("lower_inclusive") else c > lo) & (c <= hi if b.get("upper_inclusive") else c < hi)
                chain = pl.when(cond).then(pl.lit(b.get("bin") or "")) if chain is None else chain.when(cond).then(pl.lit(b.get("bin") or ""))
            expr = chain.otherwise(pl.lit(None, dtype=pl.String)).cast(pl.String)
            name = col if s["mode"] == "Replace column" else f"{col} [Binned]"
            if s["mode"] != "Replace column" and name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            exprs.append(expr.alias(name))
        return [df.with_columns(exprs)]


def _fmt(x: float) -> str:
    return f"{x:.6g}"


class AutoBinner(NodeModel):
    factory = "verknode.manipulation.AutoBinner"
    name = "Auto-Binner"
    category = CAT_BIN
    icon = "▁▅▃"
    short_description = "Bins numeric columns automatically into equal width or equal frequency bins."
    description = (
        "Creates bins from the data: <i>Equal width</i> splits the value range into intervals of the same "
        "size, <i>Equal frequency</i> uses quantiles so bins hold about the same number of rows. Bin names "
        "are <code>Bin 1</code>, ..., the interval borders, or the interval midpoints."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Binned data")]
    fields = [
        F("columns", "columns", "Target columns", dict(NONE), kinds=NUMERIC),
        F("method", "enum", "Binning method", "Equal width", options=["Equal width", "Equal frequency"]),
        F("bins", "int", "Number of bins", 5),
        F("naming", "enum", "Bin naming", "Numbered", options=["Numbered", "Borders", "Midpoints"]),
        F("mode", "enum", "Output", "Append columns", options=["Append columns", "Replace columns"]),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = _numeric_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No target columns selected")
        n = max(1, s["bins"])
        exprs = []
        for col in cols:
            values = df[col].cast(pl.Float64).drop_nulls()
            values = values.filter(values.is_finite())
            if values.len() == 0:
                edges = []
            elif s["method"] == "Equal width":
                lo, hi = values.min(), values.max()
                step = (hi - lo) / n
                edges = [lo + i * step for i in range(n)] + [hi]
            else:
                edges = [values.quantile(i / n, interpolation="linear") for i in range(n + 1)]
                edges = [e for i, e in enumerate(edges) if i == 0 or e > edges[i - 1]]
            c = pl.col(col).cast(pl.Float64)
            expr = pl.lit(None, dtype=pl.String)
            if len(edges) >= 2:
                chain = None
                for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
                    cond = (c >= a if i == 0 else c > a) & (c <= b)
                    if s["naming"] == "Borders":
                        label = f"{'[' if i == 0 else '('}{_fmt(a)}, {_fmt(b)}]"
                    elif s["naming"] == "Midpoints":
                        label = _fmt((a + b) / 2)
                    else:
                        label = f"Bin {i + 1}"
                    chain = pl.when(cond).then(pl.lit(label)) if chain is None else chain.when(cond).then(pl.lit(label))
                expr = chain.otherwise(pl.lit(None, dtype=pl.String))
            elif len(edges) == 1:
                expr = pl.when(c.is_not_null()).then(pl.lit("Bin 1")).otherwise(pl.lit(None, dtype=pl.String))
            name = col if s["mode"] == "Replace columns" else f"{col} [Binned]"
            if s["mode"] != "Replace columns" and name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            exprs.append(expr.cast(pl.String).alias(name))
        return [df.with_columns(exprs)]


# ----------------------------------------------------------------------
# Transform: normalization
# ----------------------------------------------------------------------
NORM_METHODS = ["Min-Max Normalization", "Z-Score Normalization (Gaussian)", "Normalization by Decimal Scaling"]


def _normalizer_params(df: pl.DataFrame, cols: list, s: dict) -> dict:
    params = {}
    for col in cols:
        v = df[col].cast(pl.Float64)
        a, b = 1.0, 0.0
        if v.drop_nulls().len():
            if s["method"] == NORM_METHODS[0]:
                lo, hi = v.min(), v.max()
                nmin, nmax = s["new_min"], s["new_max"]
                if hi != lo:
                    a = (nmax - nmin) / (hi - lo)
                    b = nmin - lo * a
                else:
                    a, b = 0.0, nmin
            elif s["method"] == NORM_METHODS[1]:
                mean, std = v.mean(), v.std()
                if std and std > 0:
                    a, b = 1.0 / std, -mean / std
                else:
                    a, b = 1.0, -mean
            else:
                peak = max(abs(v.min()), abs(v.max()))
                j = 0
                while peak / 10 ** j >= 1:
                    j += 1
                a = 10.0 ** -j
        params[col] = (a, b)
    return params


def _apply_linear(df: pl.DataFrame, params: dict, inverse=False) -> pl.DataFrame:
    exprs = []
    for col, (a, b) in params.items():
        if col not in df.schema:
            raise InvalidSettingsError(f'Column "{col}" of the model is missing in the input table')
        c = pl.col(col).cast(pl.Float64)
        if inverse:
            expr = (c - b) / a if a != 0 else pl.lit(None, dtype=pl.Float64)
        else:
            expr = c * a + b
        exprs.append(expr.cast(pl.Float64).alias(col))
    return df.with_columns(exprs) if exprs else df


def _normalizer_model(inputs) -> tuple:
    model, df = inputs
    if not isinstance(model, ModelPortObject) or model.model_type != "Normalizer":
        raise InvalidSettingsError("Input model is not a Normalizer model")
    if model.estimator is None:  # configure: parameters unknown, columns become Double
        params = {c: (1.0, 0.0) for c in model.features}
    else:
        params = model.estimator["params"]
    return model, df, params


class Normalizer(NodeModel):
    factory = "verknode.manipulation.Normalizer"
    name = "Normalizer"
    category = CAT_TRANSFORM
    icon = "↕"
    short_description = "Normalizes numeric columns (min-max, z-score or decimal scaling)."
    description = (
        "Normalizes the selected numeric columns. Min-Max maps values linearly to [new min, new max], "
        "Z-Score subtracts the mean and divides by the standard deviation, Decimal Scaling divides by the "
        "smallest power of 10 that brings all values into (-1, 1). The parameters are output as a model "
        "for <i>Normalizer (Apply)</i> and <i>Denormalizer</i>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Normalized table"), Port(MODEL, "Normalizer model", "Normalization parameters")]
    fields = [
        F("columns", "columns", "Columns to normalize", dict(ALL), kinds=NUMERIC),
        F("method", "enum", "Method", NORM_METHODS[0], options=NORM_METHODS),
        F("new_min", "float", "Min", 0.0, show_if={"method": NORM_METHODS[0]}),
        F("new_max", "float", "Max", 1.0, show_if={"method": NORM_METHODS[0]}),
    ]

    def _columns(self, df, s):
        sel = s["columns"] or ALL
        cols = [c for c in selected_columns(sel, df.schema) if is_numeric_kind(dtype_kind(df.schema[c]))]
        if sel.get("enforce") != "exclusion":
            _numeric_columns(sel, df.schema)
        return cols

    def configure(self, in_specs, s, ctx):
        df = pl.DataFrame(schema=in_specs[0])
        cols = self._columns(df, s)
        out = _apply_linear(df, {c: (1.0, 0.0) for c in cols})
        return [out.schema, ModelPortObject("Normalizer", None, pl.Schema({c: df.schema[c] for c in cols}),
                                            info={"Method": s["method"]})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = self._columns(df, s)
        params = _normalizer_params(df, cols, s)
        model = ModelPortObject(
            "Normalizer", {"method": s["method"], "params": params},
            pl.Schema({c: df.schema[c] for c in cols}),
            info={"Method": s["method"], **{f"{c} (scale, offset)": f"{a:.6g}, {b:.6g}" for c, (a, b) in params.items()}},
        )
        return [_apply_linear(df, params), model]


class NormalizerApply(NodeModel):
    factory = "verknode.manipulation.NormalizerApply"
    name = "Normalizer (Apply)"
    category = CAT_TRANSFORM
    icon = "↕▶"
    short_description = "Normalizes a table using the parameters of a Normalizer model."
    description = "Applies the normalization learned by a <i>Normalizer</i> node to another table (e.g. test data)."
    in_ports = [Port(MODEL, "Normalizer model"), Port(TABLE, "Table to normalize")]
    out_ports = [Port(MODEL, "Normalizer model", "The input model, unchanged"), Port(TABLE, "Normalized table")]
    fields = []

    def execute(self, inputs, s, ctx):
        model, df, params = _normalizer_model(inputs)
        return [model, _apply_linear(df, params)]


class Denormalizer(NodeModel):
    factory = "verknode.manipulation.Denormalizer"
    name = "Denormalizer"
    category = CAT_TRANSFORM
    icon = "↕◀"
    short_description = "Reverts a normalization using a Normalizer model."
    description = "Maps normalized values back to the original scale using the parameters of a <i>Normalizer</i> model."
    in_ports = [Port(MODEL, "Normalizer model"), Port(TABLE, "Table to denormalize")]
    out_ports = [Port(TABLE, "Denormalized table")]
    fields = []

    def execute(self, inputs, s, ctx):
        _, df, params = _normalizer_model(inputs)
        return [_apply_linear(df, params, inverse=True)]


# ----------------------------------------------------------------------
# Transform: time series style
# ----------------------------------------------------------------------
class LagColumn(NodeModel):
    factory = "verknode.manipulation.LagColumn"
    name = "Lag Column"
    category = CAT_TRANSFORM
    icon = "t-1"
    short_description = "Appends copies of a column shifted by previous rows."
    description = (
        "Appends <i>Lag</i> copies of the column, each shifted by a multiple of the lag interval: "
        "<code>column(-1)</code>, <code>column(-2)</code>, ... Useful for time series features."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with lagged columns")]
    fields = [
        F("column", "column", "Column to lag"),
        F("lag", "int", "Lag", 1, "Number of lagged copies."),
        F("interval", "int", "Lag interval", 1, "Row distance between copies."),
        F("skip_initial", "bool", "Skip initial incomplete rows", False),
        F("skip_last", "bool", "Skip last incomplete rows", False,
          "Drop the last rows whose values do not appear in any lagged copy."),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        lag, interval = max(1, s["lag"]), max(1, s["interval"])
        exprs = []
        for i in range(1, lag + 1):
            name = f"{col}(-{i * interval})"
            if name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            exprs.append(pl.col(col).shift(i * interval).alias(name))
        out = df.with_columns(exprs)
        n = lag * interval
        if s["skip_initial"]:
            out = out.slice(n)
        if s["skip_last"]:
            out = out.head(max(0, out.height - n))
        return [out]


MA_METHODS = ["Backward simple", "Center simple", "Forward simple", "Center Gaussian",
              "Simple exponential", "Double exponential"]


def _gaussian_weights(n: int) -> list:
    sigma = max(n / 4.0, 1e-9)
    mid = (n - 1) / 2
    w = [math.exp(-((i - mid) ** 2) / (2 * sigma ** 2)) for i in range(n)]
    total = sum(w)
    return [x / total * n for x in w]


class MovingAverage(NodeModel):
    factory = "verknode.manipulation.MovingAverage"
    name = "Moving Average"
    category = CAT_TRANSFORM
    icon = "~"
    short_description = "Computes moving averages of numeric columns."
    description = (
        "Smooths numeric columns with a moving average over a window of rows. Simple averages leave rows "
        "with incomplete windows missing; exponential averages use alpha = 2 / (window + 1). Center methods "
        "require an odd window length."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with moving averages")]
    fields = [
        F("columns", "columns", "Columns", dict(NONE), kinds=NUMERIC),
        F("method", "enum", "Type of moving average", "Backward simple", options=MA_METHODS),
        F("window", "int", "Window length", 21),
        F("remove_original", "bool", "Remove original columns", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = _numeric_columns(s["columns"], df.schema)
        if not cols:
            raise InvalidSettingsError("No columns selected")
        w = max(1, s["window"])
        method = s["method"]
        if method.startswith("Center") and w % 2 == 0:
            raise InvalidSettingsError("Center moving averages need an odd window length")
        exprs = []
        for col in cols:
            c = pl.col(col).cast(pl.Float64)
            if method == "Backward simple":
                e = c.rolling_mean(w, min_samples=w)
            elif method == "Center simple":
                e = c.rolling_mean(w, min_samples=w, center=True)
            elif method == "Forward simple":
                e = c.reverse().rolling_mean(w, min_samples=w).reverse()
            elif method == "Center Gaussian":
                # weighted window built from shifted columns (polars' weighted rolling rejects nulls)
                weights, half = _gaussian_weights(w), w // 2
                e = pl.sum_horizontal(
                    [c.shift(half - i) * (wt / w) for i, wt in enumerate(weights)], ignore_nulls=False)
            elif method == "Simple exponential":
                e = c.ewm_mean(alpha=2 / (w + 1), adjust=False)
            else:
                e1 = c.ewm_mean(alpha=2 / (w + 1), adjust=False)
                e = 2 * e1 - e1.ewm_mean(alpha=2 / (w + 1), adjust=False)
            name = col if s["remove_original"] else f"{method} MA({col})"
            exprs.append(e.alias(name))
        return [df.with_columns(exprs)]


MOVING_AGGS = ["Sum", "Mean", "Median", "Minimum", "Maximum", "Standard deviation", "Variance", "Count"]


def _rolling(c: pl.Expr, method: str, w: int, min_samples: int, center: bool) -> pl.Expr:
    if method == "Count":
        return c.is_not_null().cast(pl.Int32).rolling_sum(w, min_samples=min_samples, center=center)
    fn = {"Sum": c.rolling_sum, "Mean": c.rolling_mean, "Median": c.rolling_median,
          "Minimum": c.rolling_min, "Maximum": c.rolling_max, "Standard deviation": c.rolling_std,
          "Variance": c.rolling_var}[method]
    return fn(w, min_samples=min_samples, center=center)


class MovingAggregation(NodeModel):
    factory = "verknode.manipulation.MovingAggregation"
    name = "Moving Aggregation"
    category = CAT_TRANSFORM
    icon = "~Σ"
    short_description = "Aggregates values over a moving window or cumulatively."
    description = (
        "Computes aggregations over a window of rows (backward, central or forward) or cumulatively from "
        "the first row. Output columns are named <code>Method(column)</code> unless a name is given."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with aggregated columns")]
    fields = [
        F("window_type", "enum", "Window type", "Backward", options=["Backward", "Central", "Forward"],
          show_if={"cumulative": False}),
        F("window", "int", "Window length", 21, show_if={"cumulative": False}),
        F("handle_incomplete", "bool", "Resolve missing values for incomplete windows", False,
          "Aggregate over the available rows instead of returning missing values.",
          show_if={"cumulative": False}),
        F("cumulative", "bool", "Cumulative computation", False, "Aggregate from the first row to the current row."),
        F("aggregations", "records", "Aggregation settings", [{"column": None, "method": "Mean", "name": ""}], fields=[
            F("column", "column", "Column", kinds=NUMERIC),
            F("method", "enum", "Aggregation", "Mean", options=MOVING_AGGS),
            F("name", "string", "Output name", ""),
        ]),
        F("remove_aggregated", "bool", "Remove aggregation columns", False),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        aggs = [a for a in s["aggregations"] if a.get("column")]
        if not aggs:
            raise InvalidSettingsError("No aggregation columns selected")
        exprs = []
        for a in aggs:
            col = a["column"]
            if col not in df.schema:
                raise InvalidSettingsError(f'Column "{col}" not found')
            if not is_numeric_kind(dtype_kind(df.schema[col])):
                raise InvalidSettingsError(f'Column "{col}" is not numeric')
            c = pl.col(col).cast(pl.Float64)
            if s["cumulative"]:
                n = max(1, df.height)
                e = _rolling(c, a["method"], n, 1, False)
            else:
                w = max(1, s["window"])
                min_samples = 1 if s["handle_incomplete"] else w
                if s["window_type"] == "Forward":
                    e = _rolling(c.reverse(), a["method"], w, min_samples, False).reverse()
                else:
                    e = _rolling(c, a["method"], w, min_samples, s["window_type"] == "Central")
            exprs.append(e.alias(a.get("name") or f'{a["method"]}({col})'))
        names = [e.meta.output_name() for e in exprs]
        if len(set(names)) != len(names):
            raise InvalidSettingsError("Duplicate output column names")
        out = df.drop({a["column"] for a in aggs}) if s["remove_aggregated"] else df
        for n in names:
            if n in out.columns:
                raise InvalidSettingsError(f'Column "{n}" already exists')
        return [df.with_columns(exprs).select(list(out.columns) + names)]


# ----------------------------------------------------------------------
# Column order / reference filter
# ----------------------------------------------------------------------
TYPE_ORDER = ["string", "integer", "long", "double", "boolean", "date", "datetime", "time", "duration",
              "list", "struct", "molecule", "binary", "null", "other"]


class ColumnResorter(NodeModel):
    factory = "verknode.manipulation.ColumnResorter"
    name = "Column Resorter"
    category = CAT_TRANSFORM
    icon = "⇄"
    short_description = "Changes the order of the columns."
    description = (
        "Orders columns manually (columns not listed keep their relative order and are placed at the end "
        "or the start), alphabetically, or grouped by type."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with reordered columns")]
    fields = [
        F("mode", "enum", "Order", "Manual", options=["Manual", "Alphabetical (A-Z)", "Alphabetical (Z-A)", "By type"]),
        F("order", "records", "Column order", [], fields=[F("column", "column", "Column")],
          show_if={"mode": "Manual"}),
        F("unknown", "enum", "Columns not listed", "At the end", options=["At the end", "At the start"],
          show_if={"mode": "Manual"}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = list(df.columns)
        mode = s["mode"]
        if mode == "Alphabetical (A-Z)":
            order = sorted(cols, key=str.lower)
        elif mode == "Alphabetical (Z-A)":
            order = sorted(cols, key=str.lower, reverse=True)
        elif mode == "By type":
            order = sorted(cols, key=lambda c: TYPE_ORDER.index(dtype_kind(df.schema[c])))
        else:
            listed = []
            for r in s["order"]:
                c = r.get("column")
                if c and c in df.schema and c not in listed:
                    listed.append(c)
            rest = [c for c in cols if c not in listed]
            order = listed + rest if s["unknown"] == "At the end" else rest + listed
        return [df.select(order)]


class ReferenceColumnFilter(NodeModel):
    factory = "verknode.manipulation.ReferenceColumnFilter"
    name = "Reference Column Filter"
    category = "Manipulation/Column/Filter"
    icon = "▥≡"
    short_description = "Filters columns based on the columns of a reference table."
    description = (
        "Includes (or excludes) the columns of the first table that also exist in the reference table. "
        "With <i>Ensure compatibility of column types</i> a column only matches if the types are equal."
    )
    in_ports = [Port(TABLE, "Table to be filtered"), Port(TABLE, "Reference table")]
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [
        F("mode", "enum", "Columns in reference table", "Include", options=["Include", "Exclude"]),
        F("ensure_types", "bool", "Ensure compatibility of column types", False),
    ]

    def execute(self, inputs, s, ctx):
        df, ref = inputs

        def matches(c):
            return c in ref.schema and (not s["ensure_types"] or ref.schema[c] == df.schema[c])

        keep = [c for c in df.columns if matches(c) == (s["mode"] == "Include")]
        return [df.select(keep)]
