"""Date&Time, JSON and XML nodes."""
from __future__ import annotations

import datetime as dt
import json
import re

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, require_column
from verknode.node import dtype_kind, json_value
from .manipulation import selected_columns

DT_CAT = "Manipulation/Date&Time"
JSON_CAT = "Structured Data/JSON"
XML_CAT = "Structured Data/XML"

DT_KINDS = ["date", "datetime", "time"]
IN = [Port(TABLE, "Input table")]

DT_TYPES = ["Local Date", "Local Time", "Local Date Time", "Zoned Date Time"]

DURATION_HELP = (
    "Duration such as <code>1d</code>, <code>-2h30m</code>, <code>1mo</code>, <code>1y</code> "
    "(units ns, us, ms, s, m, h, d, w, mo, q, y) or ISO 8601 like <code>P1DT2H</code>."
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
_JAVA_TOKENS = [
    ("yyyy", "%Y"), ("uuuu", "%Y"), ("yy", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
    ("M", "%m"), ("dd", "%d"), ("d", "%d"), ("EEEE", "%A"), ("EEE", "%a"), ("E", "%a"),
    ("HH", "%H"), ("H", "%H"), ("hh", "%I"), ("h", "%I"), ("mm", "%M"), ("m", "%M"),
    ("ss", "%S"), ("s", "%S"), ("SSSSSSSSS", "%9f"), ("SSSSSS", "%6f"), ("SSS", "%3f"),
    ("a", "%p"), ("XXX", "%:z"), ("xxx", "%:z"), ("XX", "%z"), ("xx", "%z"), ("X", "%z"),
    ("Z", "%z"), ("VV", "%Z"), ("z", "%Z"), ("D", "%j"), ("w", "%V"),
]


def to_strftime(pattern: str) -> str:
    """Translate a Java DateTimeFormatter pattern; strftime patterns pass through."""
    if not pattern or "%" in pattern:
        return pattern
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "'":  # quoted literal
            j = pattern.find("'", i + 1)
            j = len(pattern) if j < 0 else j
            out.append(pattern[i + 1:j].replace("%", "%%") or "'")
            i = j + 1
            continue
        for tok, rep in _JAVA_TOKENS:
            if pattern.startswith(tok, i):
                out.append(rep)
                i += len(tok)
                break
        else:
            out.append("%%" if ch == "%" else ch)
            i += 1
    return "".join(out)


_ISO = re.compile(r"^(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
                  re.I)
_POLARS_DUR = re.compile(r"^-?(\d+(ns|us|ms|mo|s|m|h|d|w|q|y|i))+$")


def to_duration(text: str) -> str:
    """Normalize a duration to polars offset syntax (accepts ISO 8601 periods)."""
    t = (text or "").strip().replace(" ", "")
    if not t:
        raise InvalidSettingsError("No duration specified")
    m = _ISO.match(t)
    if m and t.upper() != "P":
        neg, y, mo, w, d, h, mi, sec = m.groups()
        parts = []
        for v, u in ((y, "y"), (mo, "mo"), (w, "w"), (d, "d"), (h, "h"), (mi, "m")):
            if v:
                parts.append(f"{int(v)}{u}")
        if sec:
            us = round(float(sec) * 1_000_000)
            parts.append(f"{us // 1_000_000}s" if us % 1_000_000 == 0 else f"{us}us")
        if not parts:
            raise InvalidSettingsError(f"Invalid duration: {text!r}")
        return ("-" if neg else "") + "".join(parts)
    if _POLARS_DUR.match(t):
        return t
    raise InvalidSettingsError(f"Invalid duration: {text!r} (e.g. 1d, 2h30m, 1mo or P1DT2H)")


def _negate(duration: str) -> str:
    return duration[1:] if duration.startswith("-") else "-" + duration


def _scale(duration: str, k: int) -> str:
    """Multiply every component of a polars duration string by k."""
    neg = duration.startswith("-")
    parts = re.findall(r"(\d+)(ns|us|ms|mo|s|m|h|d|w|q|y|i)", duration)
    sign = -1 if neg else 1
    total = sign * k
    body = "".join(f"{int(n) * abs(total)}{u}" for n, u in parts)
    return ("-" if total < 0 else "") + body


def target_dtype(type_name: str, zone: str = "UTC"):
    return {
        "Local Date": pl.Date,
        "Local Time": pl.Time,
        "Local Date Time": pl.Datetime("us"),
        "Zoned Date Time": pl.Datetime("us", zone or "UTC"),
    }[type_name]


def parse_value(text: str, dtype):
    """Parse a dialog value (ISO 8601) for comparison with a column of dtype."""
    t = (text or "").strip()
    try:
        kind = dtype_kind(dtype)
        if kind == "date":
            return dt.date.fromisoformat(t[:10])
        if kind == "time":
            return dt.time.fromisoformat(t)
        v = dt.datetime.fromisoformat(t)
        tz = getattr(dtype, "time_zone", None)
        if tz and v.tzinfo is None:
            from zoneinfo import ZoneInfo

            v = v.replace(tzinfo=ZoneInfo(tz))
        if not tz and v.tzinfo is not None:
            v = v.replace(tzinfo=None)
        return v
    except ValueError:
        raise InvalidSettingsError(f"Invalid date/time value {text!r} (use ISO format, e.g. 2024-01-31T12:00)") from None


EPOCH = dt.date(1970, 1, 1)


def _as_datetime(expr: pl.Expr, dtype) -> pl.Expr:
    """Date/Time expressions as datetimes (times on 1970-01-01) for arithmetic."""
    kind = dtype_kind(dtype)
    if kind == "date":
        return expr.cast(pl.Datetime("us"))
    if kind == "time":
        return pl.lit(EPOCH).dt.combine(expr)
    return expr


def _back(expr: pl.Expr, dtype) -> pl.Expr:
    kind = dtype_kind(dtype)
    if kind == "date":
        return expr.dt.date()
    if kind == "time":
        return expr.dt.time()
    return expr


def output_fields(suffix: str):
    return [
        F("mode", "enum", "Output", "Replace", options=["Replace", "Append with suffix"]),
        F("suffix", "string", "Suffix of appended columns", suffix, show_if={"mode": "Append with suffix"}),
    ]


def apply_columns(df: pl.DataFrame, s: dict, exprs: dict) -> pl.DataFrame:
    """exprs: source column -> expression; replace or append with suffix."""
    if not exprs:
        return df
    if s.get("mode") == "Append with suffix":
        out = []
        for c, e in exprs.items():
            name = f"{c}{s.get('suffix') or ''}"
            if name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists (choose another suffix)')
            out.append(e.alias(name))
        return df.with_columns(out)
    return df.with_columns(e.alias(c) for c, e in exprs.items())


def chosen(s: dict, schema, kinds, key="columns") -> list:
    cols = [c for c in selected_columns(s[key], schema) if dtype_kind(schema[c]) in kinds]
    if not cols:
        raise InvalidSettingsError("No suitable columns selected")
    return cols


def cols_field(label="Columns", enforce="inclusion"):
    return F("columns", "columns", label, {"include": [], "exclude": [], "enforce": enforce})


# ----------------------------------------------------------------------
# Date&Time: Convert
# ----------------------------------------------------------------------
class StringToDateTime(NodeModel):
    factory = "verknode.datetime.StringToDateTime"
    name = "String to Date&Time"
    category = f"{DT_CAT}/Convert"
    icon = "S→⏲"
    short_description = "Parses strings into date, time or date&time columns."
    description = (
        "Converts String columns into Local Date, Local Time, Local Date Time or Zoned Date Time. "
        "The format accepts Java patterns (<code>yyyy-MM-dd HH:mm:ss</code>, <code>dd.MM.yyyy</code>) "
        "or strftime (<code>%Y-%m-%d</code>). With <i>Guess format</i> ISO-like values are detected automatically."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with converted columns")]
    fields = [
        cols_field("String columns"),
        F("type", "enum", "New type", "Local Date", options=DT_TYPES),
        F("guess", "bool", "Guess format", False),
        F("format", "string", "Date format", "yyyy-MM-dd", "Java pattern or strftime.", show_if={"guess": False}),
        F("zone", "string", "Time zone", "UTC", "Time zone for values without offset.",
          show_if={"type": "Zoned Date Time"}),
        F("on_error", "enum", "If a value cannot be parsed", "Fail", options=["Fail", "Set missing"]),
        *output_fields(" (Date&Time)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, ["string"])
        fmt = None if s["guess"] else to_strftime(s["format"])
        if not s["guess"] and not fmt:
            raise InvalidSettingsError("No date format specified")
        target = target_dtype(s["type"], s.get("zone"))
        exprs = {}
        for c in cols:
            e = pl.col(c).str.strip_chars()
            if fmt and "%:z" in fmt:  # Java "X" patterns accept "Z" for UTC
                e = e.str.replace(r"Z$", "+00:00")
            elif fmt and "%z" in fmt:
                e = e.str.replace(r"Z$", "+0000")
            if s["type"] == "Local Date":
                e = e.str.to_date(fmt, strict=False)
            elif s["type"] == "Local Time" and fmt is None:
                e = pl.coalesce([e.str.to_time(f, strict=False) for f in ("%H:%M:%S%.f", "%H:%M:%S", "%H:%M")])
            elif s["type"] == "Local Time":
                e = e.str.to_time(fmt, strict=False)
            else:
                e = e.str.to_datetime(fmt, strict=False, time_unit="us")
            exprs[c] = e
        parsed = df.select(**{f"__{i}": e for i, e in enumerate(exprs.values())})
        result = {}
        for i, c in enumerate(cols):
            ser = parsed[f"__{i}"]
            if s["type"] == "Zoned Date Time":
                tz = target.time_zone
                ser = ser.dt.convert_time_zone(tz) if ser.dtype.time_zone else ser.dt.replace_time_zone(tz)
            elif s["type"] == "Local Date Time" and getattr(ser.dtype, "time_zone", None):
                ser = ser.dt.replace_time_zone(None)
            bad = (ser.is_null() & df[c].is_not_null() & (df[c].str.strip_chars() != "")).sum()
            if bad:
                first = df.filter(ser.is_null() & df[c].is_not_null())[c][0]
                if s["on_error"] == "Fail":
                    raise ValueError(f'Cannot parse "{first}" in column "{c}" with format {fmt or "(guessed)"}')
                ctx.warn(f'{bad} value(s) in "{c}" could not be parsed')
            result[c] = pl.lit(ser.cast(target, strict=False))
        return [apply_columns(df, s, result)]


class DateTimeToString(NodeModel):
    factory = "verknode.datetime.DateTimeToString"
    name = "Date&Time to String"
    category = f"{DT_CAT}/Convert"
    icon = "⏲→S"
    short_description = "Formats date&time columns as strings."
    description = (
        "Converts date, time and date&time columns to String using a Java pattern "
        "(<code>yyyy-MM-dd'T'HH:mm</code>) or strftime pattern."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with converted columns")]
    fields = [
        cols_field("Date&Time columns"),
        F("format", "string", "Date format", "yyyy-MM-dd", "Java pattern or strftime."),
        *output_fields(" (String)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, DT_KINDS)
        fmt = to_strftime(s["format"])
        if not fmt:
            raise InvalidSettingsError("No date format specified")
        return [apply_columns(df, s, {c: pl.col(c).dt.strftime(fmt) for c in cols})]


class TimeZoneModifier(NodeModel):
    factory = "verknode.datetime.TimeZoneModifier"
    name = "Time Zone Modifier"
    category = f"{DT_CAT}/Convert"
    icon = "TZ"
    short_description = "Adds, changes, shifts or removes the time zone of date&time columns."
    description = (
        "<i>Add/change time zone</i> keeps the wall-clock time and sets the zone; <i>Shift time zone</i> "
        "converts the instant into the new zone; <i>Remove time zone</i> yields Local Date Time."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with modified columns")]
    fields = [
        cols_field("Date&Time columns"),
        F("action", "enum", "Modification", "Add/change time zone",
          options=["Add/change time zone", "Shift time zone", "Remove time zone"]),
        F("zone", "string", "Time zone", "UTC", "e.g. UTC, Europe/Berlin, America/New_York",
          show_if={"action": ["Add/change time zone", "Shift time zone"]}),
        *output_fields(" (modified time zone)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, ["datetime"])
        zone = (s.get("zone") or "UTC").strip()
        if s["action"] != "Remove time zone":
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(zone)
            except Exception:  # noqa: BLE001
                raise InvalidSettingsError(f"Unknown time zone: {zone}") from None
        exprs = {}
        for c in cols:
            e, has_tz = pl.col(c), bool(df.schema[c].time_zone)
            if s["action"] == "Add/change time zone":
                exprs[c] = e.dt.replace_time_zone(zone)
            elif s["action"] == "Shift time zone":
                exprs[c] = (e if has_tz else e.dt.replace_time_zone("UTC")).dt.convert_time_zone(zone)
            else:
                exprs[c] = e.dt.replace_time_zone(None)
        return [apply_columns(df, s, exprs)]


# ----------------------------------------------------------------------
# Date&Time: Other / Transform
# ----------------------------------------------------------------------
class CreateDateTimeRange(NodeModel):
    factory = "verknode.datetime.CreateDateTimeRange"
    name = "Create Date&Time Range"
    category = f"{DT_CAT}/Other"
    node_type = "Source"
    icon = "⏲…"
    short_description = "Generates a column of equidistant dates/times."
    description = (
        "Creates a table with one date&time column. Either a fixed number of rows from a start with an "
        "interval, all values between start and end with an interval, or a number of rows evenly spaced "
        "between start and end. Values use ISO format, e.g. <code>2024-01-31</code> or "
        "<code>2024-01-31T08:00</code>. " + DURATION_HELP
    )
    out_ports = [Port(TABLE, "Date&Time range")]
    fields = [
        F("type", "enum", "Output type", "Local Date Time", options=DT_TYPES),
        F("zone", "string", "Time zone", "UTC", show_if={"type": "Zoned Date Time"}),
        F("range_mode", "enum", "Range", "Number of rows + interval",
          options=["Number of rows + interval", "Start, end + interval", "Start, end + number of rows"]),
        F("start", "string", "Start", "2024-01-01T00:00"),
        F("end", "string", "End", "2024-01-31T00:00",
          show_if={"range_mode": ["Start, end + interval", "Start, end + number of rows"]}),
        F("rows", "int", "Number of rows", 10,
          show_if={"range_mode": ["Number of rows + interval", "Start, end + number of rows"]}),
        F("interval", "string", "Interval", "1d", DURATION_HELP,
          show_if={"range_mode": ["Number of rows + interval", "Start, end + interval"]}),
        F("column", "string", "Output column name", "Date&Time"),
    ]

    def execute(self, inputs, s, ctx):
        dtype = target_dtype(s["type"], s.get("zone"))
        local = pl.Datetime("us")
        start = parse_value(s["start"], pl.Time if s["type"] == "Local Time" else local)
        if isinstance(start, dt.time):
            start = dt.datetime.combine(EPOCH, start)
        name = s["column"] or "Date&Time"
        mode = s["range_mode"]
        if mode == "Number of rows + interval":
            n = max(0, s["rows"])
            step = to_duration(s["interval"])
            offsets = pl.Series([_scale(step, k) for k in range(n)], dtype=pl.String)
            values = pl.select(pl.lit(start, dtype=local).dt.offset_by(pl.lit(offsets)).alias(name)).to_series() \
                if n else pl.Series(name, [], dtype=local)
        else:
            end = parse_value(s["end"], pl.Time if s["type"] == "Local Time" else local)
            if isinstance(end, dt.time):
                end = dt.datetime.combine(EPOCH, end)
            if end < start:
                raise InvalidSettingsError("End must not be before start")
            if mode == "Start, end + interval":
                step = to_duration(s["interval"])
                if step.startswith("-"):
                    raise InvalidSettingsError("Interval must be positive")
                values = pl.datetime_range(start, end, step, eager=True, time_unit="us")
            else:
                n = max(0, s["rows"])
                total = (end - start).total_seconds() * 1_000_000
                us = [round(total * k / (n - 1)) if n > 1 else 0 for k in range(n)]
                values = pl.Series([start + dt.timedelta(microseconds=u) for u in us], dtype=local)
        values = values.alias(name)
        if s["type"] == "Local Date":
            values = values.dt.date()
        elif s["type"] == "Local Time":
            values = values.dt.time()
        elif s["type"] == "Zoned Date Time":
            values = values.dt.replace_time_zone(dtype.time_zone)
        return [pl.DataFrame([values])]


GRANULARITIES = ["Years", "Quarters", "Months", "Weeks", "Days", "Hours", "Minutes", "Seconds", "Milliseconds"]


def _month_diff(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """Whole months from a to b (truncated towards zero)."""
    m = (b.dt.year() * 12 + b.dt.month()) - (a.dt.year() * 12 + a.dt.month())
    shifted = a.dt.offset_by(m.cast(pl.String) + "mo")
    return (pl.when((m > 0) & (shifted > b)).then(m - 1)
            .when((m < 0) & (shifted < b)).then(m + 1)
            .otherwise(m)).cast(pl.Int64)


class DateTimeDifference(NodeModel):
    factory = "verknode.datetime.DateTimeDifference"
    name = "Date&Time Difference"
    category = f"{DT_CAT}/Transform"
    icon = "Δ⏲"
    short_description = "Calculates the difference between two date&time values."
    description = (
        "Computes <i>second − first</i> as a number in the chosen granularity (truncated) or as a Duration. "
        "The second value is another column, the execution date&time, or a fixed date&time (ISO format)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with difference column")]
    fields = [
        F("column", "column", "First column", None, kinds=DT_KINDS),
        F("second_mode", "enum", "Calculate difference to", "Second column",
          options=["Second column", "Execution date&time", "Fixed date&time"]),
        F("second_column", "column", "Second column", None, kinds=DT_KINDS, show_if={"second_mode": "Second column"}),
        F("fixed", "string", "Fixed date&time", "2024-01-01T00:00", show_if={"second_mode": "Fixed date&time"}),
        F("output", "enum", "Output", "Granularity", options=["Granularity", "Duration"]),
        F("granularity", "enum", "Granularity", "Days", options=GRANULARITIES, show_if={"output": "Granularity"}),
        F("new_column", "string", "New column name", "date&time diff"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        t1 = df.schema[col]
        if dtype_kind(t1) not in DT_KINDS:
            raise InvalidSettingsError(f'Column "{col}" is not a date&time column')
        a = _as_datetime(pl.col(col), t1)
        if s["second_mode"] == "Second column":
            col2 = require_column(s, "second_column", df.schema, "second column")
            t2 = df.schema[col2]
            if dtype_kind(t2) != dtype_kind(t1):
                raise InvalidSettingsError("Both columns must have the same date&time type")
            b = _as_datetime(pl.col(col2), t2)
        else:
            if s["second_mode"] == "Execution date&time":
                now = dt.datetime.now(dt.timezone.utc) if getattr(t1, "time_zone", None) else dt.datetime.now()
                value = {"date": now.date(), "time": now.time()}.get(dtype_kind(t1), now)
            else:
                value = parse_value(s["fixed"], t1)
            b = _as_datetime(pl.lit(value, dtype=t1), t1)
        name = s["new_column"] or "date&time diff"
        if name in df.columns:
            raise InvalidSettingsError(f'Column "{name}" already exists')
        if s["output"] == "Duration":
            return [df.with_columns((b - a).alias(name))]
        g = s["granularity"]
        if g in ("Years", "Quarters", "Months"):
            months = _month_diff(a, b)
            e = months if g == "Months" else (months / (12 if g == "Years" else 3)).cast(pl.Int64)
        else:
            d = b - a
            e = {
                "Weeks": lambda: (d.dt.total_days() / 7).cast(pl.Int64),
                "Days": lambda: d.dt.total_days(),
                "Hours": lambda: d.dt.total_hours(),
                "Minutes": lambda: d.dt.total_minutes(),
                "Seconds": lambda: d.dt.total_seconds(),
                "Milliseconds": lambda: d.dt.total_milliseconds(),
            }[g]().cast(pl.Int64)
        return [df.with_columns(e.alias(name))]


class DateTimeShift(NodeModel):
    factory = "verknode.datetime.DateTimeShift"
    name = "Date&Time Shift"
    category = f"{DT_CAT}/Transform"
    icon = "⏲±"
    short_description = "Shifts date&time values by a duration or period."
    description = (
        "Adds a duration/period to date&time columns: a fixed value, the values of a Duration column, "
        "or a numeric column interpreted in the selected granularity. " + DURATION_HELP
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with shifted columns")]
    fields = [
        cols_field("Date&Time columns"),
        F("shift_mode", "enum", "Shift by", "Duration value",
          options=["Duration value", "Duration column", "Numerical column"]),
        F("value", "string", "Duration value", "1d", DURATION_HELP, show_if={"shift_mode": "Duration value"}),
        F("duration_column", "column", "Duration column", None, kinds=["duration"],
          show_if={"shift_mode": "Duration column"}),
        F("number_column", "column", "Numerical column", None, kinds=["integer", "long", "double"],
          show_if={"shift_mode": "Numerical column"}),
        F("granularity", "enum", "Granularity", "Days", options=GRANULARITIES,
          show_if={"shift_mode": "Numerical column"}),
        *output_fields(" (shifted)"),
    ]

    UNITS = {"Years": "y", "Quarters": "q", "Months": "mo", "Weeks": "w", "Days": "d", "Hours": "h",
             "Minutes": "m", "Seconds": "s", "Milliseconds": "ms"}

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, DT_KINDS)
        if s["shift_mode"] == "Duration value":
            offset = pl.lit(to_duration(s["value"]))
        elif s["shift_mode"] == "Duration column":
            dc = require_column(s, "duration_column", df.schema, "duration column")
            if dtype_kind(df.schema[dc]) != "duration":
                raise InvalidSettingsError(f'Column "{dc}" is not a Duration column')
            offset = None
        else:
            nc = require_column(s, "number_column", df.schema, "numerical column")
            offset = pl.col(nc).cast(pl.Int64).cast(pl.String) + self.UNITS[s["granularity"]]
        exprs = {}
        for c in cols:
            t = df.schema[c]
            base = _as_datetime(pl.col(c), t)
            shifted = base + pl.col(dc) if offset is None else base.dt.offset_by(offset)
            exprs[c] = _back(shifted, t)
        return [apply_columns(df, s, exprs)]


FIELDS = ["Year", "Year (week-based)", "Quarter", "Month (number)", "Month (name)", "Week",
          "Day of year", "Day of month", "Day of week (number)", "Day of week (name)",
          "Hour", "Minute", "Second", "Millisecond", "Microsecond", "Time zone name"]
DATE_FIELDS = set(FIELDS[:10])
TIME_FIELDS = {"Hour", "Minute", "Second", "Millisecond", "Microsecond"}


class ExtractDateTimeFields(NodeModel):
    factory = "verknode.datetime.ExtractDateTimeFields"
    name = "Extract Date&Time Fields"
    category = f"{DT_CAT}/Transform"
    icon = "⏲▥"
    short_description = "Extracts fields such as year, month or hour into new columns."
    description = (
        "Appends one column per selected field of a date, time or date&time column. Day of week numbers "
        "follow ISO (Monday = 1)."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with extracted fields")]
    fields = [
        F("column", "column", "Date&Time column", None, kinds=DT_KINDS),
        F("fields", "multienum", "Fields", {"include": ["Year", "Month (number)", "Day of month"], "exclude": [],
                                            "enforce": "inclusion"}, options=FIELDS),
        F("prefix", "string", "Column name prefix", ""),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        t = df.schema[col]
        kind = dtype_kind(t)
        if kind not in DT_KINDS:
            raise InvalidSettingsError(f'Column "{col}" is not a date&time column')
        sel = s["fields"] or {}
        names = selected_columns(sel, dict.fromkeys(FIELDS))
        if not names:
            raise InvalidSettingsError("No fields selected")
        c = pl.col(col)
        exprs = []
        for f in names:
            if (kind == "date" and f in TIME_FIELDS) or (kind == "time" and f in DATE_FIELDS):
                raise InvalidSettingsError(f'Field "{f}" is not available for {kind} column "{col}"')
            e = {
                "Year": lambda: c.dt.year(),
                "Year (week-based)": lambda: c.dt.iso_year(),
                "Quarter": lambda: c.dt.quarter(),
                "Month (number)": lambda: c.dt.month(),
                "Month (name)": lambda: c.dt.strftime("%B"),
                "Week": lambda: c.dt.week(),
                "Day of year": lambda: c.dt.ordinal_day(),
                "Day of month": lambda: c.dt.day(),
                "Day of week (number)": lambda: c.dt.weekday(),
                "Day of week (name)": lambda: c.dt.strftime("%A"),
                "Hour": lambda: c.dt.hour(),
                "Minute": lambda: c.dt.minute(),
                "Second": lambda: c.dt.second(),
                "Millisecond": lambda: c.dt.millisecond(),
                "Microsecond": lambda: c.dt.microsecond(),
                "Time zone name": lambda: pl.when(c.is_not_null()).then(
                    pl.lit(getattr(t, "time_zone", None) or "", dtype=pl.String)),
            }[f]()
            if f not in ("Month (name)", "Day of week (name)", "Time zone name"):
                e = e.cast(pl.Int32)
            name = f"{s['prefix']}{f}"
            if name in df.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists (use a prefix)')
            exprs.append(e.alias(name))
        return [df.with_columns(exprs)]


def _parse_time(text):
    try:
        return dt.time.fromisoformat((text or "").strip())
    except ValueError:
        raise InvalidSettingsError(f"Invalid time {text!r} (use HH:mm[:ss[.SSS]])") from None


def _parse_date(text):
    try:
        return dt.date.fromisoformat((text or "").strip())
    except ValueError:
        raise InvalidSettingsError(f"Invalid date {text!r} (use yyyy-MM-dd)") from None


class ModifyTime(NodeModel):
    factory = "verknode.datetime.ModifyTime"
    name = "Modify Time"
    category = f"{DT_CAT}/Transform"
    icon = "⏲◷"
    short_description = "Appends, changes or removes the time of date&time columns."
    description = (
        "<i>Append time</i> turns Local Date columns into date&time; <i>Change time</i> sets the time of "
        "date&time columns; <i>Remove time</i> turns date&time into Local Date."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with modified columns")]
    fields = [
        cols_field("Columns"),
        F("action", "enum", "Modification", "Append time", options=["Append time", "Change time", "Remove time"]),
        F("time", "string", "Time", "00:00:00", show_if={"action": ["Append time", "Change time"]}),
        F("zone", "string", "Time zone (empty: local)", "", show_if={"action": "Append time"}),
        *output_fields(" (modified time)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        action = s["action"]
        kinds = ["date"] if action == "Append time" else ["datetime"]
        cols = chosen(s, df.schema, kinds)
        exprs = {}
        if action != "Remove time":
            t = _parse_time(s["time"])
        for c in cols:
            e = pl.col(c)
            if action == "Append time":
                e = e.dt.combine(t)
                if (s.get("zone") or "").strip():
                    e = e.dt.replace_time_zone(s["zone"].strip())
            elif action == "Change time":
                e = e.dt.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=t.microsecond)
            else:
                e = e.dt.date()
            exprs[c] = e
        return [apply_columns(df, s, exprs)]


class ModifyDate(NodeModel):
    factory = "verknode.datetime.ModifyDate"
    name = "Modify Date"
    category = f"{DT_CAT}/Transform"
    icon = "⏲▦"
    short_description = "Appends, changes or removes the date of date&time columns."
    description = (
        "<i>Append date</i> turns Local Time columns into date&time; <i>Change date</i> sets the date of "
        "date&time columns; <i>Remove date</i> turns date&time into Local Time."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with modified columns")]
    fields = [
        cols_field("Columns"),
        F("action", "enum", "Modification", "Append date", options=["Append date", "Change date", "Remove date"]),
        F("date", "string", "Date", "2024-01-01", show_if={"action": ["Append date", "Change date"]}),
        F("zone", "string", "Time zone (empty: local)", "", show_if={"action": "Append date"}),
        *output_fields(" (modified date)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        action = s["action"]
        kinds = ["time"] if action == "Append date" else ["datetime"]
        cols = chosen(s, df.schema, kinds)
        exprs = {}
        if action != "Remove date":
            d = _parse_date(s["date"])
        for c in cols:
            e = pl.col(c)
            if action == "Append date":
                e = pl.lit(d).dt.combine(e)
                if (s.get("zone") or "").strip():
                    e = e.dt.replace_time_zone(s["zone"].strip())
            elif action == "Change date":
                e = e.dt.replace(year=d.year, month=d.month, day=d.day)
            else:
                e = e.dt.time()
            exprs[c] = e
        return [apply_columns(df, s, exprs)]


class RoundDateTime(NodeModel):
    factory = "verknode.datetime.RoundDateTime"
    name = "Round Date&Time"
    category = f"{DT_CAT}/Transform"
    icon = "⏲≈"
    short_description = "Rounds date&time values to a granularity."
    description = (
        "Rounds to the first point in time (down), the last point in time (up) or the nearest point in "
        "time of the given granularity, e.g. <code>15m</code>, <code>1h</code>, <code>1d</code>, <code>1mo</code>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with rounded columns")]
    fields = [
        cols_field("Date&Time columns"),
        F("strategy", "enum", "Rounding", "First point in time",
          options=["First point in time", "Last point in time", "Nearest point in time"]),
        F("granularity", "string", "Granularity", "1h", DURATION_HELP),
        *output_fields(" (rounded)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, DT_KINDS)
        g = to_duration(s["granularity"])
        if g.startswith("-"):
            raise InvalidSettingsError("Granularity must be positive")
        exprs = {}
        for c in cols:
            t = df.schema[c]
            base = _as_datetime(pl.col(c), t)
            down = base.dt.truncate(g)
            if s["strategy"] == "First point in time":
                e = down
            elif s["strategy"] == "Last point in time":
                e = pl.when(down == base).then(base).otherwise(down.dt.offset_by(g))
            else:
                e = base.dt.round(g)
            exprs[c] = _back(e, t)
        return [apply_columns(df, s, exprs)]


class DurationToNumber(NodeModel):
    factory = "verknode.datetime.DurationToNumber"
    name = "Duration to Number"
    category = f"{DT_CAT}/Convert"
    icon = "Δ→#"
    short_description = "Converts Duration columns into numbers."
    description = "Converts Duration columns into whole numbers (truncated) of the selected granularity."
    in_ports = IN
    out_ports = [Port(TABLE, "Table with converted columns")]
    fields = [
        cols_field("Duration columns"),
        F("granularity", "enum", "Granularity", "Days",
          options=["Weeks", "Days", "Hours", "Minutes", "Seconds", "Milliseconds", "Microseconds"]),
        *output_fields(" (number)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, ["duration"])
        exprs = {}
        for c in cols:
            d = pl.col(c)
            e = {
                "Weeks": lambda: d.dt.total_days() / 7,
                "Days": lambda: d.dt.total_days(),
                "Hours": lambda: d.dt.total_hours(),
                "Minutes": lambda: d.dt.total_minutes(),
                "Seconds": lambda: d.dt.total_seconds(),
                "Milliseconds": lambda: d.dt.total_milliseconds(),
                "Microseconds": lambda: d.dt.total_microseconds(),
            }[s["granularity"]]()
            exprs[c] = e.cast(pl.Int64)
        return [apply_columns(df, s, exprs)]


# ----------------------------------------------------------------------
# Date&Time: Filter
# ----------------------------------------------------------------------
class DateTimeRowFilter(NodeModel):
    factory = "verknode.datetime.DateTimeRowFilter"
    name = "Date&Time-based Row Filter"
    category = f"{DT_CAT}/Filter"
    icon = "⏲▤"
    short_description = "Keeps rows whose date&time lies within a range."
    description = (
        "Keeps rows whose value in the selected column lies between start and end (ISO values, e.g. "
        "<code>2024-01-31</code>, <code>2024-01-31T12:00</code>, <code>08:30</code>). Leave start empty for no "
        "lower bound. The end can alternatively be given as a duration from the start. Missing values are removed."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Filtered table")]
    fields = [
        F("column", "column", "Date&Time column", None, kinds=DT_KINDS),
        F("start", "string", "Start (empty: none)", ""),
        F("start_inclusive", "bool", "Start inclusive", True),
        F("end_mode", "enum", "End", "Date&Time", options=["Date&Time", "Duration from start", "None"]),
        F("end", "string", "End date&time", "", show_if={"end_mode": "Date&Time"}),
        F("duration", "string", "Duration", "1d", DURATION_HELP, show_if={"end_mode": "Duration from start"}),
        F("end_inclusive", "bool", "End inclusive", True, show_if={"end_mode": ["Date&Time", "Duration from start"]}),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        t = df.schema[col]
        if dtype_kind(t) not in DT_KINDS:
            raise InvalidSettingsError(f'Column "{col}" is not a date&time column')
        c = pl.col(col)
        cond = c.is_not_null()
        start = parse_value(s["start"], t) if (s["start"] or "").strip() else None
        if start is not None:
            cond = cond & (c >= start if s["start_inclusive"] else c > start)
        end = None
        if s["end_mode"] == "Date&Time" and (s["end"] or "").strip():
            end = pl.lit(parse_value(s["end"], t), dtype=t)
        elif s["end_mode"] == "Duration from start":
            if start is None:
                raise InvalidSettingsError("A start is required for a duration")
            end = _back(_as_datetime(pl.lit(start, dtype=t), t).dt.offset_by(to_duration(s["duration"])), t)
        if end is not None:
            cond = cond & (c <= end if s["end_inclusive"] else c < end)
        return [df.filter(cond)]


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------
def _loads(text):
    if text is None:
        return None
    return json.loads(text)


class StringToJSON(NodeModel):
    factory = "verknode.json.StringToJSON"
    name = "String to JSON"
    category = JSON_CAT
    icon = "S→{}"
    short_description = "Validates and normalizes JSON text in String columns."
    description = (
        "Parses String columns as JSON. Valid values are written back as normalized JSON text (compact or "
        "pretty printed); invalid values fail the node or become missing."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with JSON columns")]
    fields = [
        cols_field("String columns"),
        F("pretty", "bool", "Pretty print", False),
        F("on_error", "enum", "If a value is not valid JSON", "Fail", options=["Fail", "Set missing"]),
        *output_fields(" (JSON)"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = chosen(s, df.schema, ["string"])
        exprs, bad = {}, 0
        for c in cols:
            out = []
            for i, v in enumerate(df[c].to_list()):
                if v is None:
                    out.append(None)
                    continue
                try:
                    obj = json.loads(v)
                except json.JSONDecodeError as e:
                    if s["on_error"] == "Fail":
                        raise ValueError(f'Row {i}, column "{c}": invalid JSON ({e})') from None
                    bad += 1
                    out.append(None)
                    continue
                out.append(json.dumps(obj, indent=2 if s["pretty"] else None, ensure_ascii=False))
            exprs[c] = pl.lit(pl.Series(c, out, dtype=pl.String))
        if bad:
            ctx.warn(f"{bad} value(s) are not valid JSON")
        return [apply_columns(df, s, exprs)]


RESULT_TYPES = ["String", "Integer", "Double", "Boolean", "JSON", "List of String"]
RESULT_DTYPES = {"String": pl.String, "Integer": pl.Int64, "Double": pl.Float64, "Boolean": pl.Boolean,
                 "JSON": pl.String, "List of String": pl.List(pl.String)}


def _convert_result(values: list, rtype: str):
    """Typed cell from a list of query matches (first match for scalar types)."""
    if rtype == "List of String":
        return [v if isinstance(v, str) else json.dumps(v) for v in values] if values else None
    if not values:
        return None
    v = values[0]
    if v is None:
        return None
    if rtype == "JSON":
        return json.dumps(v, ensure_ascii=False)
    if rtype == "String":
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    try:
        if rtype == "Integer":
            return int(float(v)) if not isinstance(v, bool) else int(v)
        if rtype == "Double":
            return float(v)
        if rtype == "Boolean":
            if isinstance(v, str):
                return v.strip().lower() in ("true", "1", "yes")
            return bool(v)
    except (TypeError, ValueError):
        return None
    return v


class JSONPath(NodeModel):
    factory = "verknode.json.JSONPath"
    name = "JSON Path"
    category = JSON_CAT
    icon = "{}$"
    short_description = "Extracts values from JSON with JSONPath queries."
    description = (
        "Evaluates JSONPath expressions (e.g. <code>$.store.book[*].author</code>, "
        "<code>$.items[?(@.price &lt; 10)].name</code>) on a JSON String column. Scalar result types use the "
        "first match; <i>List of String</i> collects all matches; <i>JSON</i> keeps the match as JSON text."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with extracted values")]
    fields = [
        F("column", "column", "JSON column", None, kinds=["string"]),
        F("queries", "records", "JSONPath queries", [{"column": "value", "path": "$.name", "type": "String"}],
          fields=[
              F("column", "string", "Output column", ""),
              F("path", "string", "JSONPath", ""),
              F("type", "enum", "Result type", "String", options=RESULT_TYPES),
          ]),
        F("remove_source", "bool", "Remove source column", False),
    ]

    def execute(self, inputs, s, ctx):
        from jsonpath_ng.exceptions import JsonPathParserError
        from jsonpath_ng.ext import parse

        df = inputs[0]
        col = require_column(s, "column", df.schema)
        queries = [q for q in s["queries"] if q.get("column") and q.get("path")]
        if not queries:
            raise InvalidSettingsError("No JSONPath queries defined")
        compiled = []
        for q in queries:
            try:
                compiled.append(parse(q["path"]))
            except (JsonPathParserError, Exception) as e:  # noqa: BLE001
                raise InvalidSettingsError(f'Invalid JSONPath "{q["path"]}": {e}') from None
        docs = []
        for i, v in enumerate(df[col].to_list()):
            try:
                docs.append(_loads(v))
            except json.JSONDecodeError:
                raise ValueError(f"Row {i}: invalid JSON") from None
        out = []
        for q, expr in zip(queries, compiled):
            if q["column"] in df.columns and not (s["remove_source"] and q["column"] == col):
                raise InvalidSettingsError(f'Column "{q["column"]}" already exists')
            values = [None if d is None else _convert_result([m.value for m in expr.find(d)], q["type"]) for d in docs]
            out.append(pl.Series(q["column"], values, dtype=RESULT_DTYPES[q["type"]]))
        if s["remove_source"]:
            df = df.drop(col)
        return [df.with_columns(out)]


def _flatten(obj, prefix, sep, out, depth_limit):
    if isinstance(obj, dict) and depth_limit != 0:
        for k, v in obj.items():
            _flatten(v, f"{prefix}{sep}{k}" if prefix else str(k), sep, out, depth_limit - 1)
    else:
        out[prefix or "value"] = obj


def _infer_series(name, values):
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, bool) for v in present):
        return pl.Series(name, values, dtype=pl.Boolean)
    if present and all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return pl.Series(name, values, dtype=pl.Int64)
    if present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return pl.Series(name, [None if v is None else float(v) for v in values], dtype=pl.Float64)
    if present and all(isinstance(v, list) for v in present):
        items = [x for v in present for x in v]
        if items and all(isinstance(x, str) for x in items):
            return pl.Series(name, values, dtype=pl.List(pl.String))
        if items and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in items):
            inner = pl.Int64 if all(isinstance(x, int) for x in items) else pl.Float64
            return pl.Series(name, values, dtype=pl.List(inner))
    return pl.Series(name, [None if v is None else v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                            for v in values], dtype=pl.String)


class JSONToTable(NodeModel):
    factory = "verknode.json.JSONToTable"
    name = "JSON to Table"
    category = JSON_CAT
    icon = "{}→▦"
    short_description = "Expands JSON objects into columns."
    description = (
        "Creates one column per key of the JSON objects in a column. <i>Only leaves</i> flattens nested "
        "objects with the separator (<code>address.city</code>); <i>Top-level keys</i> keeps nested values as "
        "JSON text. Arrays of strings or numbers become list columns, other arrays JSON text. Column types are "
        "detected from the values."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with expanded columns")]
    fields = [
        F("column", "column", "JSON column", None, kinds=["string"]),
        F("expand", "enum", "Expansion", "Only leaves", options=["Only leaves", "Top-level keys"]),
        F("separator", "string", "Path separator", ".", show_if={"expand": "Only leaves"}),
        F("remove_source", "bool", "Remove source column", True),
    ]

    def configure(self, in_specs, s, ctx):
        require_column(s, "column", in_specs[0])
        return [None]  # columns depend on the data

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        rows, keys = [], {}
        for i, v in enumerate(df[col].to_list()):
            try:
                obj = _loads(v)
            except json.JSONDecodeError:
                raise ValueError(f"Row {i}: invalid JSON") from None
            flat = {}
            if obj is not None:
                if not isinstance(obj, dict):
                    obj = {"value": obj}
                _flatten(obj, "", s["separator"] or ".", flat, -1 if s["expand"] == "Only leaves" else 1)
            for k in flat:
                keys.setdefault(k, None)
            rows.append(flat)
        base = df.drop(col) if s["remove_source"] else df
        new = []
        for k in keys:
            name = k if k not in base.columns else f"{k} (JSON)"
            new.append(_infer_series(name, [r.get(k) for r in rows]))
        return [base.with_columns(new) if new else base]


class TableToJSON(NodeModel):
    factory = "verknode.json.TableToJSON"
    name = "Table to JSON"
    category = JSON_CAT
    icon = "▦→{}"
    short_description = "Converts rows or the whole table into JSON."
    description = (
        "<i>Row-oriented</i> creates one JSON object per row (keys = column names). "
        "<i>Whole table</i> creates a single row holding an array of row objects; <i>Column-oriented</i> a "
        "single object mapping each column name to its array of values."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with JSON column")]
    fields = [
        cols_field("Columns", enforce="exclusion"),
        F("orientation", "enum", "Output", "Row-oriented (one JSON object per row)",
          options=["Row-oriented (one JSON object per row)", "Whole table (array of row objects)",
                   "Column-oriented (whole table)"]),
        F("remove_source", "bool", "Remove source columns", False,
          show_if={"orientation": "Row-oriented (one JSON object per row)"}),
        F("new_column", "string", "JSON column name", "JSON"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        cols = selected_columns(s["columns"], df.schema)
        name = s["new_column"] or "JSON"
        sub = df.select(cols)
        records = [{c: json_value(v) for c, v in zip(cols, r)} for r in sub.iter_rows()]
        if s["orientation"].startswith("Row-oriented"):
            base = df.drop(cols) if s["remove_source"] else df
            if name in base.columns:
                raise InvalidSettingsError(f'Column "{name}" already exists')
            values = [json.dumps(r, ensure_ascii=False) for r in records]
            return [base.with_columns(pl.Series(name, values, dtype=pl.String))]
        if s["orientation"].startswith("Whole table"):
            doc = records
        else:
            doc = {c: [json_value(v) for v in sub[c].to_list()] for c in cols}
        return [pl.DataFrame({name: pl.Series(name, [json.dumps(doc, ensure_ascii=False)], dtype=pl.String)})]


class JSONRowCombiner(NodeModel):
    factory = "verknode.json.JSONRowCombiner"
    name = "JSON Row Combiner"
    category = JSON_CAT
    icon = "{}∪"
    short_description = "Combines the JSON values of all rows into one value."
    description = (
        "Combines the JSON values of a column into one JSON array, or into an object whose keys come from "
        "a key column. Optionally wraps the result in an object with a root key."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with combined JSON")]
    fields = [
        F("column", "column", "JSON column", None, kinds=["string"]),
        F("mode", "enum", "Combine as", "Array", options=["Array", "Object with keys from column"]),
        F("key_column", "column", "Key column", None, show_if={"mode": "Object with keys from column"}),
        F("root", "string", "Root key (empty: none)", ""),
        F("new_column", "string", "JSON column name", "JSON"),
    ]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        values = []
        for i, v in enumerate(df[col].to_list()):
            try:
                values.append(_loads(v))
            except json.JSONDecodeError:
                raise ValueError(f"Row {i}: invalid JSON") from None
        if s["mode"] == "Array":
            doc = values
        else:
            key = require_column(s, "key_column", df.schema, "key column")
            doc = {}
            for k, v in zip(df[key].to_list(), values):
                if k is None:
                    raise ValueError("Key column contains missing values")
                doc[str(json_value(k))] = v
        if (s["root"] or "").strip():
            doc = {s["root"].strip(): doc}
        name = s["new_column"] or "JSON"
        return [pl.DataFrame({name: pl.Series(name, [json.dumps(doc, ensure_ascii=False)], dtype=pl.String)})]


# ----------------------------------------------------------------------
# XML
# ----------------------------------------------------------------------
XPATH_TYPES = ["String", "Integer", "Double", "Boolean", "List of String", "XML"]


class XPath(NodeModel):
    factory = "verknode.json.XPath"
    name = "XPath"
    category = XML_CAT
    icon = "</>"
    short_description = "Extracts values from XML with XPath queries."
    description = (
        "Evaluates XPath 1.0 queries (e.g. <code>/catalog/book/title/text()</code>, "
        "<code>count(//book)</code>, <code>//book/@id</code>) on a String column containing XML. Scalar types "
        "use the first match; <i>List of String</i> collects all matches; <i>XML</i> returns the first matching "
        "element as XML text. Elements are converted to their text content. Default namespaces must be "
        "addressed with <code>local-name()</code>."
    )
    in_ports = IN
    out_ports = [Port(TABLE, "Table with extracted values")]
    fields = [
        F("column", "column", "XML column", None, kinds=["string"]),
        F("queries", "records", "XPath queries", [{"column": "value", "xpath": "/*/text()", "type": "String"}],
          fields=[
              F("column", "string", "Output column", ""),
              F("xpath", "string", "XPath", ""),
              F("type", "enum", "Return type", "String", options=XPATH_TYPES),
          ]),
        F("remove_source", "bool", "Remove source column", False),
    ]

    def execute(self, inputs, s, ctx):
        from lxml import etree

        df = inputs[0]
        col = require_column(s, "column", df.schema)
        queries = [q for q in s["queries"] if q.get("column") and q.get("xpath")]
        if not queries:
            raise InvalidSettingsError("No XPath queries defined")
        compiled = []
        for q in queries:
            try:
                compiled.append(etree.XPath(q["xpath"]))
            except etree.XPathSyntaxError as e:
                raise InvalidSettingsError(f'Invalid XPath "{q["xpath"]}": {e}') from None
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        docs = []
        for i, v in enumerate(df[col].to_list()):
            if v is None:
                docs.append(None)
                continue
            try:
                docs.append(etree.fromstring(v.encode("utf-8"), parser))
            except etree.XMLSyntaxError as e:
                raise ValueError(f"Row {i}: invalid XML ({e})") from None

        def as_items(result, xml):
            if not isinstance(result, list):
                result = [result]
            out = []
            for r in result:
                if isinstance(r, etree._Element):
                    out.append(etree.tostring(r, encoding="unicode", with_tail=False) if xml else "".join(r.itertext()))
                elif isinstance(r, float) and r.is_integer():
                    out.append(int(r))
                else:
                    out.append(str(r) if isinstance(r, (bytes, etree._ElementUnicodeResult)) else r)
            return out

        out = []
        for q, xp in zip(queries, compiled):
            if q["column"] in df.columns and not (s["remove_source"] and q["column"] == col):
                raise InvalidSettingsError(f'Column "{q["column"]}" already exists')
            values = []
            for d in docs:
                if d is None:
                    values.append(None)
                    continue
                items = as_items(xp(d), q["type"] == "XML")
                if q["type"] == "XML":
                    values.append(items[0] if items else None)
                elif q["type"] == "List of String":
                    values.append([str(x) for x in items] if items else None)
                else:
                    values.append(_convert_result(items, q["type"]))
            out.append(pl.Series(q["column"], values,
                                 dtype=pl.String if q["type"] == "XML" else RESULT_DTYPES[q["type"]]))
        if s["remove_source"]:
            df = df.drop(col)
        return [df.with_columns(out)]
