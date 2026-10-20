"""Custom canvas widgets: examples of extending the widget nodes.

- Date Input reuses the built-in text box and checks the date itself.
- Colour Input brings its own control: an "html" page that calls
  verknode.setValue(value) when the colour changes.
- Gauge Display brings its own display: render() returns {"html": page}.
"""
import datetime as dt
import html
import math

import polars as pl

from verknode.node import F, InputWidget, InvalidSettingsError, OutputWidget


class DateInput(InputWidget):
    name = "Date Input"
    category = "Widgets/Inputs"
    icon = "📅"
    control = "text"  # a built-in control with checks of our own
    short_description = "A date (YYYY-MM-DD) entered on the canvas."
    widget_fields = [F("default", "string", "Default date", "2024-01-01", "YYYY-MM-DD")]

    def valid(self, s, value):
        dt.date.fromisoformat(value)  # raises for anything else: the default is used
        return True

    def to_table(self, value, s, ctx):
        return pl.DataFrame({self.column_name(s): [dt.date.fromisoformat(value)]})


class ColourInput(InputWidget):
    name = "Colour Input"
    category = "Widgets/Inputs"
    icon = "🎨"
    control = "html"  # our own control, shown in a sandboxed frame on the node
    default_name = "colour"
    short_description = "A colour picked on the canvas (hex code)."
    widget_fields = [F("default", "string", "Default colour", "#3d9be9")]

    def valid(self, s, value):
        return isinstance(value, str) and len(value) == 7 and value.startswith("#")

    def html(self, s, value):
        return f"""
<style>body {{ margin: 0; display: flex; gap: 8px; align-items: center; font: 13px sans-serif; }}
input {{ width: 44px; height: 28px; border: 0; padding: 0; background: none; }}</style>
<input type="color" value="{html.escape(value)}" oninput="code.textContent = this.value"
       onchange="verknode.setValue(this.value)">
<code id="code">{html.escape(value)}</code>"""


class GaugeDisplay(OutputWidget):
    name = "Gauge Display"
    category = "Widgets/Displays"
    icon = "◔"
    renderer = "html"  # our own display, shown in a sandboxed frame on the node
    width, height = 180, 110
    short_description = "A value of the first row as a gauge between a minimum and a maximum."
    widget_fields = [
        F("column", "column", "Column", None, "Empty: the first numeric column.", kinds=["integer", "long", "double"]),
        F("minimum", "float", "Minimum", 0.0),
        F("maximum", "float", "Maximum", 100.0),
    ]

    def render(self, df, s, ctx):
        numeric = [c for c, t in df.schema.items() if t.is_numeric()]
        col = s.get("column") or (numeric[0] if numeric else None)
        if col is None or col not in df.columns:
            raise InvalidSettingsError("Choose a numeric column")
        value = df[col][0] if df.height else None
        lo, hi = s["minimum"], s["maximum"]
        frac = 0.0 if value is None or hi <= lo else min(max((value - lo) / (hi - lo), 0.0), 1.0)
        # half circle from the left (0) to the right (1)
        a = math.pi * (1 - frac)
        x, y = 90 + 70 * math.cos(a), 85 - 70 * math.sin(a)
        text = "–" if value is None else f"{value:,.4g}"
        return {"html": f"""
<style>body {{ margin: 0; font: 12px sans-serif; color: #555; }} svg {{ display: block; }}</style>
<svg viewBox="0 0 180 105" width="100%" height="100%">
  <path d="M20 85 A70 70 0 0 1 160 85" fill="none" stroke="#d9dde3" stroke-width="12" stroke-linecap="round"/>
  <path d="M20 85 A70 70 0 0 1 {x:.1f} {y:.1f}" fill="none" stroke="#2a78d6" stroke-width="12"
        stroke-linecap="round"/>
  <text x="90" y="80" text-anchor="middle" font-size="22" font-weight="600" fill="#222">{html.escape(text)}</text>
  <text x="20" y="102" text-anchor="middle">{lo:g}</text><text x="160" y="102" text-anchor="middle">{hi:g}</text>
</svg>"""}
