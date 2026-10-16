"""View nodes: static charts (matplotlib SVG) with image output ports, table and image views."""
from __future__ import annotations

import base64

import numpy as np
import polars as pl

from verknode.node import views as v
from verknode.node import F, IMAGE, ImagePortObject, InvalidSettingsError, NodeModel, Port, TABLE
from verknode.node import dtype_kind, is_numeric_kind
from .manipulation import selected_columns

SVG = "image/svg+xml"
NUMERIC = ["integer", "long", "double"]
NOMINAL = ["string", "boolean", "integer", "long", "date"]
XKINDS = ["integer", "long", "double", "date", "datetime", "string"]
AGGREGATIONS = ["Occurrence Count", "Sum", "Average", "Minimum", "Maximum"]
COLORMAPS = ["viridis", "plasma", "magma", "cividis", "coolwarm", "RdBu_r", "Blues", "Greens", "Reds", "YlOrRd"]
ALL = {"include": [], "exclude": [], "enforce": "exclusion"}
NONE = {"include": [], "exclude": [], "enforce": "inclusion"}


def common_fields(max_rows=10000, axes=True, legend=True):
    out = [
        F("title", "string", "Chart title", "", "Empty: the node name."),
        F("subtitle", "string", "Chart subtitle", ""),
    ]
    if axes:
        out += [F("x_label", "string", "X axis label", "", "Empty: column name."),
                F("y_label", "string", "Y axis label", "", "Empty: column name.")]
    if legend:
        out.append(F("legend", "bool", "Display legend", True))
    out += [
        F("width", "int", "Image width (px)", 800),
        F("height", "int", "Image height (px)", 500),
        F("max_rows", "int", "Maximum number of rows", max_rows,
          "Larger tables are sampled (randomly, order preserved) and a warning is shown."),
    ]
    return out


def _require(s, key, spec, label, kinds=None, optional=False):
    name = s.get(key)
    if not name:
        if optional:
            return None
        raise InvalidSettingsError(f"No {label} selected")
    if spec is not None and not isinstance(spec, ImagePortObject):
        if name not in spec:
            raise InvalidSettingsError(f'Column "{name}" not found in input table')
        if kinds and dtype_kind(spec[name]) not in kinds:
            raise InvalidSettingsError(f'Column "{name}" has unsupported type for {label}')
    return name


def _numeric_selection(s, key, spec, label="columns", minimum=1):
    cols = [c for c in selected_columns(s.get(key), spec) if is_numeric_kind(dtype_kind(spec[c]))]
    if len(cols) < minimum:
        raise InvalidSettingsError(f"Select at least {minimum} numeric {label}")
    return cols


def _style(ax):
    ax.grid(True, color="#e3e3e3", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#999")
    ax.tick_params(labelsize=9, colors="#333")


def _label(value):
    return "?" if value is None else str(value)


def _color_of(i):
    return v.PALETTE[i % len(v.PALETTE)]


def _float(series: pl.Series) -> np.ndarray:
    return series.cast(pl.Float64, strict=False).to_numpy()


def _xvalues(series: pl.Series):
    kind = dtype_kind(series.dtype)
    if kind in ("date", "datetime"):
        return series.to_numpy()
    if kind == "string":
        return np.array([_label(x) for x in series.to_list()], dtype=object)
    return _float(series)


class _Chart(NodeModel):
    category = "Views"
    node_type = "Visualizer"
    has_view = True
    multi_axes = False
    in_ports = [Port(TABLE, "Data table", "Data to display")]
    out_ports = [Port(IMAGE, "Image", "SVG image of the chart")]

    def check(self, s, spec):
        """Validate settings against the input schema (spec may be None)."""

    def validate(self, s, in_specs):
        self.check(s, in_specs[0] if in_specs else None)

    def configure(self, in_specs, s, ctx):
        self.validate(s, in_specs)
        return [ImagePortObject(b"", SVG)]

    def plot(self, fig, ax, df, s, ctx):
        raise NotImplementedError

    def _limit(self, df, s, ctx):
        n = max(1, s.get("max_rows") or 1)
        if df.height <= n:
            return df
        ctx.warn(f"Only {n} of {df.height} rows are displayed (random sample)")
        return df.with_row_index("__row").sample(n=n, seed=0).sort("__row").drop("__row")

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        self.check(s, df.schema)
        title = s.get("title") or self.name
        if ctx.dry:
            return [ImagePortObject(b"", SVG)]
        df = self._limit(df, s, ctx)
        fig, ax = v.figure(max(2, s["width"]) / 100, max(2, s["height"]) / 100)
        if df.height == 0:
            ax.axis("off")
            ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=16, color="#888",
                    transform=ax.transAxes)
        else:
            self.plot(fig, ax, df, s, ctx)
            for a in fig.axes:
                if a.get_label() != "<colorbar>":
                    _style(a)
        subtitle = s.get("subtitle")
        fig.suptitle(title + (f"\n{subtitle}" if subtitle else ""), fontsize=13, fontweight="bold",
                     x=0.01, ha="left", color="#222")
        svg = v.svg(fig)
        ctx.set_view(v.page(title, v.chart(svg)))
        return [ImagePortObject(svg.encode("utf-8"), SVG)]

    @staticmethod
    def _axes_labels(ax, s, x, y):
        ax.set_xlabel(s.get("x_label") or x or "", fontsize=10)
        ax.set_ylabel(s.get("y_label") or y or "", fontsize=10)

    @staticmethod
    def _legend(ax, s, **kw):
        if s.get("legend", True) and ax.get_legend_handles_labels()[0]:
            kw.setdefault("loc", "upper left")
            kw.setdefault("bbox_to_anchor", (1.01, 1))
            leg = ax.legend(frameon=False, fontsize=9, **kw)
            for h in leg.legend_handles:
                h.set_alpha(1)


# ----------------------------------------------------------------------
class BarChart(_Chart):
    factory = "verknode.views.BarChart"
    name = "Bar Chart"
    icon = "▇▅"
    short_description = "Bar chart of category counts or aggregated values."
    description = (
        "Displays one bar per category. Bars show the <i>Occurrence Count</i> of each category or an "
        "aggregation (Sum, Average, Minimum, Maximum) of the selected frequency columns. Multiple frequency "
        "columns are drawn grouped or stacked."
    )
    fields = [
        F("category", "column", "Category column", None, kinds=NOMINAL + ["double", "datetime"]),
        F("aggregation", "enum", "Aggregation method", "Occurrence Count", options=AGGREGATIONS),
        F("frequency", "columns", "Frequency columns", NONE, show_if={"aggregation": AGGREGATIONS[1:]}),
        F("arrangement", "enum", "Bar arrangement", "Grouped", options=["Grouped", "Stacked"]),
        F("orientation", "enum", "Orientation", "Vertical", options=["Vertical", "Horizontal"]),
        F("order", "enum", "Category order", "Original", options=["Original", "Category", "Value descending"]),
        *common_fields(),
    ]

    def check(self, s, spec):
        _require(s, "category", spec, "category column")
        if s["aggregation"] != "Occurrence Count" and spec is not None:
            _numeric_selection(s, "frequency", spec, "frequency columns")

    def plot(self, fig, ax, df, s, ctx):
        cat = s["category"]
        df = df.with_columns(pl.col(cat).cast(pl.String).fill_null("?").alias("__cat"))
        agg = s["aggregation"]
        if agg == "Occurrence Count":
            cols = ["Occurrence Count"]
            g = df.group_by("__cat", maintain_order=True).agg(pl.len().cast(pl.Float64).alias(cols[0]))
        else:
            cols = _numeric_selection(s, "frequency", df.schema)
            fn = {"Sum": "sum", "Average": "mean", "Minimum": "min", "Maximum": "max"}[agg]
            g = df.group_by("__cat", maintain_order=True).agg(
                getattr(pl.col(c).cast(pl.Float64), fn)().alias(c) for c in cols)
        if s["order"] == "Category":
            g = g.sort("__cat")
        elif s["order"] == "Value descending":
            g = g.sort(pl.sum_horizontal(cols), descending=True)
        cats = g["__cat"].to_list()
        x = np.arange(len(cats))
        horizontal = s["orientation"] == "Horizontal"
        bar = ax.barh if horizontal else ax.bar
        stacked = s["arrangement"] == "Stacked"
        width = 0.8 if stacked else 0.8 / len(cols)
        bottom = np.zeros(len(cats))
        for i, c in enumerate(cols):
            vals = np.nan_to_num(g[c].to_numpy().astype(float))
            label = c if agg == "Occurrence Count" else f"{c} ({agg})"
            if stacked:
                kw = {"left": bottom} if horizontal else {"bottom": bottom}
                bar(x, vals, width, color=_color_of(i), label=label, **kw)
                bottom += vals
            else:
                bar(x - 0.4 + width * (i + 0.5), vals, width, color=_color_of(i), label=label)
        if horizontal:
            ax.set_yticks(x, cats)
            ax.invert_yaxis()
            self._axes_labels(ax, s, agg if agg != "Occurrence Count" else "Count", cat)
        else:
            ax.set_xticks(x, cats, rotation=45 if len(cats) > 8 else 0, ha="right" if len(cats) > 8 else "center")
            self._axes_labels(ax, s, cat, agg if agg != "Occurrence Count" else "Count")
        if len(cols) > 1 or agg != "Occurrence Count":
            self._legend(ax, s)


class LinePlot(_Chart):
    factory = "verknode.views.LinePlot"
    name = "Line Plot"
    icon = "╱╲"
    short_description = "Line plot of numeric columns."
    description = (
        "Plots one line per selected numeric column against the x-axis column (numbers, dates and "
        "date&amp;times) or against the row index. Missing values create gaps."
    )
    fields = [
        F("x", "column", "X axis column", None, "Empty: row index.", kinds=XKINDS),
        F("y", "columns", "Y axis columns", NONE),
        F("markers", "bool", "Show markers", False),
        F("sort", "bool", "Sort by x axis", True),
        *common_fields(),
    ]

    def check(self, s, spec):
        _require(s, "x", spec, "x axis column", XKINDS, optional=True)
        if spec is not None:
            _numeric_selection(s, "y", spec, "y axis columns")

    def plot(self, fig, ax, df, s, ctx):
        x = s.get("x")
        ys = _numeric_selection(s, "y", df.schema)
        if x:
            df = df.filter(pl.col(x).is_not_null())
            if s["sort"] and dtype_kind(df.schema[x]) != "string":
                df = df.sort(x)
            xs = _xvalues(df[x])
        else:
            xs = np.arange(df.height)
        for i, c in enumerate(ys):
            ax.plot(xs, _float(df[c]), color=_color_of(i), label=c, linewidth=1.6,
                    marker="o" if s["markers"] else None, markersize=3)
        self._axes_labels(ax, s, x or "Row index", ys[0] if len(ys) == 1 else "")
        if x and dtype_kind(df.schema[x]) in ("date", "datetime"):
            fig.autofmt_xdate()
        self._legend(ax, s)


class ScatterPlot(_Chart):
    factory = "verknode.views.ScatterPlot"
    name = "Scatter Plot"
    icon = "⁙"
    short_description = "Scatter plot of two columns."
    description = (
        "Plots rows as points. Optionally colors points by a column (numeric: color scale, nominal: one "
        "color per value), scales them by a size column and labels them. Rows with missing x or y are skipped."
    )
    fields = [
        F("x", "column", "X axis column", None, kinds=["integer", "long", "double", "date", "datetime"]),
        F("y", "column", "Y axis column", None, kinds=["integer", "long", "double", "date", "datetime"]),
        F("color", "column", "Color column (optional)", None),
        F("colormap", "enum", "Color scale", "viridis", options=COLORMAPS),
        F("size", "column", "Size column (optional)", None, kinds=NUMERIC),
        F("label", "column", "Label column (optional)", None),
        F("marker_size", "int", "Marker size", 30),
        F("alpha", "float", "Opacity (0..1)", 0.8),
        F("log_x", "bool", "Logarithmic x axis", False),
        F("log_y", "bool", "Logarithmic y axis", False),
        *common_fields(),
    ]

    def check(self, s, spec):
        kinds = ["integer", "long", "double", "date", "datetime"]
        _require(s, "x", spec, "x axis column", kinds)
        _require(s, "y", spec, "y axis column", kinds)
        for k in ("color", "size", "label"):
            _require(s, k, spec, f"{k} column", optional=True)

    def plot(self, fig, ax, df, s, ctx):
        x, y = s["x"], s["y"]
        df = df.filter(pl.col(x).is_not_null() & pl.col(y).is_not_null())
        xs, ys = _xvalues(df[x]), _xvalues(df[y])
        size = s["marker_size"]
        if s.get("size"):
            raw = np.nan_to_num(_float(df[s["size"]]), nan=0.0)
            span = raw.max() - raw.min() if raw.size else 0
            size = size * (0.3 + 2.7 * ((raw - raw.min()) / span if span else np.ones_like(raw) * 0.5))
        color = s.get("color")
        alpha = min(max(s["alpha"], 0.05), 1.0)
        if color and is_numeric_kind(dtype_kind(df.schema[color])):
            sc = ax.scatter(xs, ys, c=_float(df[color]), s=size, cmap=s["colormap"], alpha=alpha,
                            edgecolors="none", plotnonfinite=True)
            cb = fig.colorbar(sc, ax=ax)
            cb.set_label(color, fontsize=9)
            cb.ax.set_label("<colorbar>")
        elif color:
            values = [_label(c) for c in df[color].to_list()]
            cats = list(dict.fromkeys(values))
            if len(cats) > 20:
                ctx.warn(f"{len(cats)} color categories; colors repeat")
            arr = np.array(values, dtype=object)
            for i, cat in enumerate(cats):
                mask = arr == cat
                sz = size[mask] if isinstance(size, np.ndarray) else size
                ax.scatter(xs[mask], ys[mask], s=sz, color=_color_of(i), alpha=alpha, label=cat, edgecolors="none")
            self._legend(ax, s, title=color, title_fontsize=9)
        else:
            ax.scatter(xs, ys, s=size, color=v.PALETTE[0], alpha=alpha, edgecolors="none")
        if s.get("label"):
            for xi, yi, t in zip(xs, ys, df[s["label"]].to_list()):
                ax.annotate(_label(t), (xi, yi), fontsize=7, color="#444", xytext=(3, 3), textcoords="offset points")
        if dtype_kind(df.schema[x]) in ("date", "datetime"):
            fig.autofmt_xdate()
        if s["log_x"]:
            ax.set_xscale("log")
        if s["log_y"]:
            ax.set_yscale("log")
        self._axes_labels(ax, s, x, y)


class Histogram(_Chart):
    factory = "verknode.views.Histogram"
    name = "Histogram"
    icon = "▁▆▃"
    short_description = "Histogram of a numeric column."
    description = (
        "Bins a numeric column into equal-width intervals. Bars show the occurrence count or the Sum / Average "
        "of an aggregation column per bin. Optionally split by a category column (overlaid or stacked)."
    )
    fields = [
        F("column", "column", "Binning column", None, kinds=NUMERIC),
        F("bins", "int", "Number of bins", 20),
        F("aggregation", "enum", "Aggregation method", "Occurrence Count", options=["Occurrence Count", "Sum", "Average"]),
        F("agg_column", "column", "Aggregation column", None, kinds=NUMERIC,
          show_if={"aggregation": ["Sum", "Average"]}),
        F("split", "column", "Split by category (optional)", None, kinds=NOMINAL),
        F("split_mode", "enum", "Split display", "Overlay", options=["Overlay", "Stacked"]),
        F("density", "bool", "Normalize (density)", False),
        *common_fields(),
    ]

    def check(self, s, spec):
        _require(s, "column", spec, "binning column", NUMERIC)
        if s["aggregation"] != "Occurrence Count":
            _require(s, "agg_column", spec, "aggregation column", NUMERIC)
        _require(s, "split", spec, "split column", optional=True)

    def plot(self, fig, ax, df, s, ctx):
        col = s["column"]
        df = df.filter(pl.col(col).is_not_null() & pl.col(col).cast(pl.Float64).is_finite())
        values = _float(df[col])
        if values.size == 0:
            ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, color="#888")
            return
        edges = np.histogram_bin_edges(values, bins=max(1, s["bins"]))
        agg = s["aggregation"]
        weights = None if agg == "Occurrence Count" else np.nan_to_num(_float(df[s["agg_column"]]))

        def binned(mask):
            vals = values[mask]
            if agg == "Average":
                sums, _ = np.histogram(vals, edges, weights=weights[mask])
                counts, _ = np.histogram(vals, edges)
                return np.divide(sums, counts, out=np.zeros_like(sums, dtype=float), where=counts > 0)
            h, _ = np.histogram(vals, edges, weights=None if weights is None else weights[mask])
            h = h.astype(float)
            if s["density"] and h.sum():
                h = h / (h.sum() * np.diff(edges))
            return h

        widths = np.diff(edges)
        if s.get("split"):
            groups = np.array([_label(c) for c in df[s["split"]].to_list()], dtype=object)
            bottom = np.zeros(len(widths))
            stacked = s["split_mode"] == "Stacked"
            for i, g in enumerate(dict.fromkeys(groups)):
                h = binned(groups == g)
                ax.bar(edges[:-1], h, widths, align="edge", bottom=bottom if stacked else 0,
                       color=_color_of(i), alpha=1.0 if stacked else 0.55, label=g, edgecolor="white", linewidth=0.5)
                if stacked:
                    bottom += h
            self._legend(ax, s, title=s["split"], title_fontsize=9)
        else:
            ax.bar(edges[:-1], binned(np.ones(values.size, bool)), widths, align="edge", color=v.PALETTE[0],
                   edgecolor="white", linewidth=0.5)
        ylabel = "Density" if s["density"] and agg == "Occurrence Count" else (
            "Count" if agg == "Occurrence Count" else f"{agg}({s['agg_column']})")
        self._axes_labels(ax, s, col, ylabel)


class BoxPlot(_Chart):
    factory = "verknode.views.BoxPlot"
    name = "Box Plot"
    icon = "⊟"
    short_description = "Box plots of numeric columns."
    description = (
        "Shows median, quartiles, whiskers (1.5 IQR) and outliers for each selected numeric column, or for one "
        "column grouped by the values of a category column."
    )
    fields = [
        F("mode", "enum", "Box plot of", "Multiple columns", options=["Multiple columns", "One column grouped by category"]),
        F("columns", "columns", "Columns", NONE, show_if={"mode": "Multiple columns"}),
        F("column", "column", "Value column", None, kinds=NUMERIC, show_if={"mode": "One column grouped by category"}),
        F("group", "column", "Category column", None, kinds=NOMINAL, show_if={"mode": "One column grouped by category"}),
        F("outliers", "bool", "Show outliers", True),
        F("horizontal", "bool", "Horizontal boxes", False),
        *common_fields(legend=False),
    ]

    def check(self, s, spec):
        if s["mode"] == "Multiple columns":
            if spec is not None:
                _numeric_selection(s, "columns", spec)
        else:
            _require(s, "column", spec, "value column", NUMERIC)
            _require(s, "group", spec, "category column")

    def plot(self, fig, ax, df, s, ctx):
        if s["mode"] == "Multiple columns":
            cols = _numeric_selection(s, "columns", df.schema)
            data = [df[c].drop_nulls().cast(pl.Float64).to_numpy() for c in cols]
            labels = cols
            xl, yl = "", "Value"
        else:
            col, grp = s["column"], s["group"]
            sub = df.filter(pl.col(col).is_not_null())
            keys = np.array([_label(g) for g in sub[grp].to_list()], dtype=object)
            values = _float(sub[col])
            labels = list(dict.fromkeys(keys))
            data = [values[keys == k] for k in labels]
            xl, yl = grp, col
        bp = ax.boxplot(data, tick_labels=labels, showfliers=s["outliers"], vert=not s["horizontal"],
                        patch_artist=True, medianprops={"color": "#222"})
        for i, patch in enumerate(bp["boxes"]):
            patch.set_facecolor(_color_of(i))
            patch.set_alpha(0.75)
        if s["horizontal"]:
            xl, yl = yl, xl
        self._axes_labels(ax, s, xl, yl)


class PieChart(_Chart):
    factory = "verknode.views.PieChart"
    name = "Pie Chart"
    icon = "◔"
    short_description = "Pie (or donut) chart of category shares."
    description = (
        "Shows the share of each category, by occurrence count or by the Sum / Average of a frequency column. "
        "Small categories beyond the maximum number of sections are merged into <i>Other</i>."
    )
    fields = [
        F("category", "column", "Category column", None, kinds=NOMINAL),
        F("aggregation", "enum", "Aggregation method", "Occurrence Count", options=["Occurrence Count", "Sum", "Average"]),
        F("frequency", "column", "Frequency column", None, kinds=NUMERIC, show_if={"aggregation": ["Sum", "Average"]}),
        F("donut", "bool", "Donut chart", False),
        F("percentages", "bool", "Show percentages", True),
        F("max_sections", "int", "Maximum number of sections", 10),
        *common_fields(axes=False),
    ]

    def check(self, s, spec):
        _require(s, "category", spec, "category column")
        if s["aggregation"] != "Occurrence Count":
            _require(s, "frequency", spec, "frequency column", NUMERIC)

    def plot(self, fig, ax, df, s, ctx):
        cat = s["category"]
        df = df.with_columns(pl.col(cat).cast(pl.String).fill_null("?").alias("__cat"))
        if s["aggregation"] == "Occurrence Count":
            g = df.group_by("__cat", maintain_order=True).agg(pl.len().cast(pl.Float64).alias("v"))
        else:
            fn = "sum" if s["aggregation"] == "Sum" else "mean"
            g = df.group_by("__cat", maintain_order=True).agg(getattr(pl.col(s["frequency"]).cast(pl.Float64), fn)().alias("v"))
        g = g.with_columns(pl.col("v").fill_null(0.0)).filter(pl.col("v") > 0).sort("v", descending=True)
        n = max(1, s["max_sections"])
        labels, vals = g["__cat"].to_list(), g["v"].to_list()
        if len(labels) > n:
            labels, vals = labels[: n - 1] + ["Other"], vals[: n - 1] + [sum(vals[n - 1:])]
        if not vals:
            ax.axis("off")
            ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, color="#888")
            return
        wedges, *_ = ax.pie(
            vals, labels=None if s["legend"] else labels, colors=[_color_of(i) for i in range(len(vals))],
            autopct="%1.1f%%" if s["percentages"] else None, startangle=90, counterclock=False,
            wedgeprops={"width": 0.45 if s["donut"] else 1, "edgecolor": "white"}, pctdistance=0.78 if s["donut"] else 0.6,
            textprops={"fontsize": 9})
        ax.axis("equal")
        ax.grid(False)
        if s["legend"]:
            ax.legend(wedges, labels, frameon=False, fontsize=9, loc="center left", bbox_to_anchor=(1, 0.5))


class Heatmap(_Chart):
    factory = "verknode.views.Heatmap"
    name = "Heatmap"
    icon = "▦"
    short_description = "Color-coded matrix of numeric values."
    description = "Displays the selected numeric columns (x axis) of each row (y axis) as colored cells."
    fields = [
        F("columns", "columns", "Columns", ALL),
        F("row_label", "column", "Row label column (optional)", None, "Empty: row index."),
        F("colormap", "enum", "Color scale", "viridis", options=COLORMAPS),
        F("values", "bool", "Show values in cells", False),
        *common_fields(max_rows=100, legend=False),
    ]

    def check(self, s, spec):
        if spec is not None:
            _numeric_selection(s, "columns", spec)
        _require(s, "row_label", spec, "row label column", optional=True)

    def plot(self, fig, ax, df, s, ctx):
        cols = _numeric_selection(s, "columns", df.schema)
        m = np.column_stack([_float(df[c]) for c in cols])
        im = ax.imshow(np.ma.masked_invalid(m), aspect="auto", cmap=s["colormap"], interpolation="nearest")
        cb = fig.colorbar(im, ax=ax)
        cb.ax.set_label("<colorbar>")
        ax.set_xticks(range(len(cols)), cols, rotation=45 if len(cols) > 5 else 0, ha="right" if len(cols) > 5 else "center")
        labels = [_label(x) for x in df[s["row_label"]].to_list()] if s.get("row_label") else [f"Row{i}" for i in range(df.height)]
        max_labels = max(2, s["height"] // 14)
        step = max(1, -(-df.height // max_labels))
        ax.set_yticks(range(0, df.height, step), labels[::step])
        if s["values"] and df.height > max_labels:
            ctx.warn("Too many rows to show cell values")
        elif s["values"]:
            lo, hi = np.nanmin(m) if np.isfinite(m).any() else 0, np.nanmax(m) if np.isfinite(m).any() else 1
            for (i, j), val in np.ndenumerate(m):
                if np.isfinite(val):
                    light = (val - lo) / (hi - lo) < 0.5 if hi > lo else True
                    ax.text(j, i, f"{val:.3g}", ha="center", va="center", fontsize=7,
                            color="white" if light else "#111")
        ax.grid(False)
        self._axes_labels(ax, s, "", s.get("row_label") or "")


class ScatterPlotMatrix(_Chart):
    factory = "verknode.views.ScatterPlotMatrix"
    name = "Scatter Plot Matrix"
    icon = "⁘"
    short_description = "Pairwise scatter plots of numeric columns."
    description = (
        "Shows a scatter plot for each pair of selected numeric columns and a histogram on the diagonal. "
        "Points can be colored by a category column. At most 8 columns are shown."
    )
    fields = [
        F("columns", "columns", "Columns", ALL),
        F("color", "column", "Color by category (optional)", None, kinds=NOMINAL),
        F("alpha", "float", "Opacity (0..1)", 0.7),
        *common_fields(max_rows=2000, axes=False),
    ]

    def check(self, s, spec):
        if spec is not None:
            _numeric_selection(s, "columns", spec, minimum=2)
        _require(s, "color", spec, "color column", optional=True)

    def plot(self, fig, ax, df, s, ctx):
        cols = _numeric_selection(s, "columns", df.schema, minimum=2)
        if len(cols) > 8:
            ctx.warn(f"Only the first 8 of {len(cols)} columns are shown")
            cols = cols[:8]
        fig.delaxes(ax)
        n = len(cols)
        axes = fig.subplots(n, n, squeeze=False)
        data = {c: _float(df[c]) for c in cols}
        groups = np.array([_label(g) for g in df[s["color"]].to_list()], dtype=object) if s.get("color") else None
        cats = list(dict.fromkeys(groups)) if groups is not None else [None]
        alpha = min(max(s["alpha"], 0.05), 1.0)
        for i, cy in enumerate(cols):
            for j, cx in enumerate(cols):
                a = axes[i][j]
                for k, cat in enumerate(cats):
                    mask = np.ones(df.height, bool) if cat is None else groups == cat
                    if i == j:
                        vals = data[cx][mask]
                        vals = vals[np.isfinite(vals)]
                        if vals.size:
                            a.hist(vals, bins=15, color=_color_of(k), alpha=0.6 if cat is not None else 1)
                    else:
                        a.scatter(data[cx][mask], data[cy][mask], s=8, color=_color_of(k), alpha=alpha,
                                  edgecolors="none", label=cat if (i, j) == (0, 1) else None)
                if i == n - 1:
                    a.set_xlabel(cx, fontsize=8)
                else:
                    a.tick_params(labelbottom=False)
                if j == 0:
                    a.set_ylabel(cy, fontsize=8)
                else:
                    a.tick_params(labelleft=False)
        if groups is not None and s["legend"]:
            handles, labels = axes[0][1].get_legend_handles_labels()
            fig.legend(handles, labels, loc="outside right center", frameon=False, fontsize=8, title=s["color"])


class StackedAreaChart(_Chart):
    factory = "verknode.views.StackedAreaChart"
    name = "Stacked Area Chart"
    icon = "◭"
    short_description = "Stacked areas of numeric columns."
    description = "Stacks the selected numeric columns as areas along the x axis column (or row index); optionally normalized to 100%."
    fields = [
        F("x", "column", "X axis column", None, "Empty: row index.", kinds=XKINDS[:-1]),
        F("y", "columns", "Y axis columns", NONE),
        F("normalize", "bool", "Normalize to 100%", False),
        *common_fields(),
    ]

    def check(self, s, spec):
        _require(s, "x", spec, "x axis column", XKINDS[:-1], optional=True)
        if spec is not None:
            _numeric_selection(s, "y", spec, "y axis columns")

    def plot(self, fig, ax, df, s, ctx):
        x = s.get("x")
        ys = _numeric_selection(s, "y", df.schema)
        if x:
            df = df.filter(pl.col(x).is_not_null()).sort(x)
            xs = _xvalues(df[x])
        else:
            xs = np.arange(df.height)
        m = np.vstack([np.nan_to_num(_float(df[c])) for c in ys])
        if s["normalize"]:
            tot = m.sum(axis=0)
            m = np.divide(m, tot, out=np.zeros_like(m), where=tot != 0) * 100
        ax.stackplot(xs, m, labels=ys, colors=[_color_of(i) for i in range(len(ys))], alpha=0.85)
        self._axes_labels(ax, s, x or "Row index", "Percent" if s["normalize"] else "")
        if x and dtype_kind(df.schema[x]) in ("date", "datetime"):
            fig.autofmt_xdate()
        self._legend(ax, s, loc="upper left")


class DensityPlot(_Chart):
    factory = "verknode.views.DensityPlot"
    name = "Density Plot"
    icon = "∿"
    short_description = "Kernel density estimates of numeric columns."
    description = "Draws a Gaussian kernel density estimate for each selected numeric column (missing values ignored)."
    fields = [
        F("columns", "columns", "Columns", NONE),
        F("bandwidth", "enum", "Bandwidth", "scott", options=["scott", "silverman"]),
        F("fill", "bool", "Fill area", True),
        *common_fields(),
    ]

    def check(self, s, spec):
        if spec is not None:
            _numeric_selection(s, "columns", spec)

    def plot(self, fig, ax, df, s, ctx):
        from scipy.stats import gaussian_kde

        cols = _numeric_selection(s, "columns", df.schema)
        series = {c: _float(df[c]) for c in cols}
        series = {c: a[np.isfinite(a)] for c, a in series.items()}
        valid = {c: a for c, a in series.items() if a.size > 1 and np.ptp(a) > 0}
        skipped = set(cols) - set(valid)
        if skipped:
            ctx.warn(f"Too few distinct values for: {', '.join(sorted(skipped))}")
        if not valid:
            ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, color="#888")
            return
        lo = min(a.min() for a in valid.values())
        hi = max(a.max() for a in valid.values())
        pad = (hi - lo) * 0.1
        grid = np.linspace(lo - pad, hi + pad, 300)
        for i, (c, a) in enumerate(valid.items()):
            y = gaussian_kde(a, bw_method=s["bandwidth"])(grid)
            ax.plot(grid, y, color=_color_of(i), label=c, linewidth=1.6)
            if s["fill"]:
                ax.fill_between(grid, y, color=_color_of(i), alpha=0.2)
        self._axes_labels(ax, s, cols[0] if len(cols) == 1 else "Value", "Density")
        self._legend(ax, s)


class ParallelCoordinates(_Chart):
    factory = "verknode.views.ParallelCoordinates"
    name = "Parallel Coordinates"
    icon = "⫴"
    short_description = "Parallel coordinates of numeric columns."
    description = (
        "Draws each row as a line across one vertical axis per selected numeric column. Axes are min-max "
        "normalized by default; lines can be colored by a category column."
    )
    fields = [
        F("columns", "columns", "Columns", ALL),
        F("color", "column", "Color by category (optional)", None, kinds=NOMINAL),
        F("normalize", "bool", "Normalize axes (min-max)", True),
        F("alpha", "float", "Opacity (0..1)", 0.5),
        *common_fields(max_rows=2000, axes=False),
    ]

    def check(self, s, spec):
        if spec is not None:
            _numeric_selection(s, "columns", spec, minimum=2)
        _require(s, "color", spec, "color column", optional=True)

    def plot(self, fig, ax, df, s, ctx):
        cols = _numeric_selection(s, "columns", df.schema, minimum=2)
        m = np.column_stack([_float(df[c]) for c in cols])
        if s["normalize"]:
            lo, hi = np.nanmin(m, axis=0), np.nanmax(m, axis=0)
            span = np.where(hi > lo, hi - lo, 1)
            m = (m - lo) / span
        xs = np.arange(len(cols))
        alpha = min(max(s["alpha"], 0.02), 1.0)
        if s.get("color"):
            groups = np.array([_label(g) for g in df[s["color"]].to_list()], dtype=object)
            for k, cat in enumerate(dict.fromkeys(groups)):
                rows = m[groups == cat]
                for r_i, row in enumerate(rows):
                    ax.plot(xs, row, color=_color_of(k), alpha=alpha, linewidth=1, label=cat if r_i == 0 else None)
            self._legend(ax, s, title=s["color"], title_fontsize=9)
        else:
            for row in m:
                ax.plot(xs, row, color=v.PALETTE[0], alpha=alpha, linewidth=1)
        for xi in xs:
            ax.axvline(xi, color="#888", linewidth=0.8)
        ax.set_xticks(xs, cols, rotation=30 if len(cols) > 6 else 0)
        ax.set_xlim(-0.1, len(cols) - 0.9)
        if s["normalize"]:
            ax.set_ylabel("Normalized value", fontsize=10)


# ----------------------------------------------------------------------
class TableView(NodeModel):
    factory = "verknode.views.TableView"
    name = "Table View"
    category = "Views"
    node_type = "Visualizer"
    icon = "▤"
    has_view = True
    short_description = "Displays the input table."
    description = "Shows the selected columns of the first rows of the input table in the node view."
    in_ports = [Port(TABLE, "Data table", "Table to display")]
    out_ports = []
    fields = [
        F("title", "string", "Title", ""),
        F("columns", "columns", "Columns", ALL),
        F("max_rows", "int", "Maximum number of rows", 100),
    ]

    def configure(self, in_specs, s, ctx):
        return []

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        shown = df.select(cols)
        n = max(1, s["max_rows"])
        body = (f'<p class="note">Rows: {df.height} | Columns: {len(cols)}</p>'
                + (v.table(shown, max_rows=n) if df.height else '<p class="note">No data</p>'))
        if not ctx.dry:
            ctx.set_view(v.page(s.get("title") or "Table View", body))
        return []


class ImageView(NodeModel):
    factory = "verknode.views.ImageView"
    name = "Image View"
    category = "Views"
    node_type = "Visualizer"
    icon = "IMG"
    has_view = True
    short_description = "Displays an image from an image port."
    description = "Shows the image (SVG, PNG, ...) arriving at the image input port in the node view."
    in_ports = [Port(IMAGE, "Image", "Image to display")]
    out_ports = []
    fields = [F("title", "string", "Title", "")]

    def configure(self, in_specs, s, ctx):
        return []

    def execute(self, inputs, s, ctx):
        img = inputs[0]
        if ctx.dry:
            return []
        if not img.data:
            body = '<p class="note">No image</p>'
        elif img.mime == SVG:
            text = img.data.decode("utf-8", errors="replace")
            body = v.chart(text[text.find("<svg"):] if "<svg" in text else text)
        else:
            data = base64.b64encode(img.data).decode("ascii")
            body = f'<div class="chart"><img src="data:{v.escape(img.mime)};base64,{data}" style="max-width:100%"/></div>'
        ctx.set_view(v.page(s.get("title") or "Image View", body))
        return []
