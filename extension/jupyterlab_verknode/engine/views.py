"""Helpers for node views: static HTML pages with inline SVG charts and tables.

Views are shown in a sandboxed iframe without scripts, so they must be plain
HTML/CSS/SVG. Typical use in a node::

    fig, ax = views.figure()
    ax.bar(...)
    svg = views.svg(fig)
    ctx.set_view(views.page("Bar Chart", views.chart(svg)))
    return [ImagePortObject(svg.encode())]
"""
from __future__ import annotations

import html
import io

import polars as pl

from .types import json_value

# default palette
PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948",
           "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]

_CSS = """
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 13px;
       color: #222; margin: 12px; background: #fff; }
h1 { font-size: 16px; margin: 0 0 10px; }
h2 { font-size: 14px; margin: 18px 0 6px; }
table { border-collapse: collapse; margin: 4px 0 12px; }
th, td { border: 1px solid #ddd; padding: 3px 8px; text-align: left; white-space: nowrap; }
th { background: #f3f3f3; position: sticky; top: 0; }
td.num { text-align: right; font-family: monospace; }
td.missing { color: #d32f2f; text-align: center; font-weight: bold; }
.chart svg { max-width: 100%; height: auto; }
.kv td:first-child { font-weight: 600; background: #fafafa; }
.note { color: #666; }
"""


def figure(width: float = 8, height: float = 5):
    """A matplotlib figure/axes pair using the non-interactive SVG backend."""
    import matplotlib

    matplotlib.use("svg", force=False)
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width, height), layout="constrained")
    ax = fig.add_subplot()
    ax.set_prop_cycle(color=PALETTE)
    return fig, ax


def svg(fig) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg")
    text = buf.getvalue()
    return text[text.find("<svg"):]


def chart(svg_text: str) -> str:
    return f'<div class="chart">{svg_text}</div>'


def escape(text) -> str:
    return html.escape(str(text))


def table(df: pl.DataFrame, max_rows: int = 500, float_digits: int = 6) -> str:
    """HTML table of a DataFrame (missing values shown as red '?')."""
    head = "".join(f"<th>{escape(c)}</th>" for c in df.columns)
    numeric = [df.schema[c].is_numeric() for c in df.columns]
    rows = []
    for r in df.head(max_rows).iter_rows():
        cells = []
        for v, num in zip(r, numeric):
            if v is None:
                cells.append('<td class="missing">?</td>')
            else:
                if isinstance(v, float):
                    v = round(v, float_digits)
                cells.append(f'<td class="{"num" if num else ""}">{escape(json_value(v))}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")
    more = f'<p class="note">Showing {max_rows} of {df.height} rows.</p>' if df.height > max_rows else ""
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>{more}"


def key_values(items: dict) -> str:
    rows = "".join(f"<tr><td>{escape(k)}</td><td>{escape(json_value(v))}</td></tr>" for k, v in items.items())
    return f'<table class="kv">{rows}</table>'


def page(title: str, *parts: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style></head>"
            f"<body><h1>{escape(title)}</h1>{''.join(parts)}</body></html>")
