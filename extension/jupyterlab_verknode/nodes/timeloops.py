"""Loops in time: an iteration every X seconds (Every Interval Loop Start), or iterations one after another
until a time (Timed Loop Start). Close them with a Loop End like any loop.

The start tells the workflow after each iteration whether to go on (loop["should_stop"]): the
iteration running at the stop time is the last, and no iteration starts after it.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import time

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

from .flowcontrol import CountingLoopStart

STOPS = ["After a number of iterations", "After a duration", "At a time of day", "At a date and time",
         "Never (until canceled)"]
UNITS = {"ms": 0.001, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}


def seconds(text, what: str) -> float:
    """"90", "90s", "1.5m", "1h 30m" -> seconds."""
    t = str(text or "").strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|sec|min|s|m|h|d)", t)
    if not parts or re.sub(r"(\d+(?:\.\d+)?)\s*(ms|sec|min|s|m|h|d)|\s", "", t):
        raise InvalidSettingsError(f'{what}: e.g. 30s, 5m, 1h 30m (not "{text}")')
    return sum(float(n) * UNITS[u] for n, u in parts)


def _zone(s):
    name = (s.get("timezone") or "").strip()
    if not name:
        return None  # (the server's local time)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone names
        raise InvalidSettingsError(f'Unknown time zone "{name}" (e.g. Europe/Berlin, America/New_York)') from None


def _now(zone):
    return dt.datetime.now(zone) if zone else dt.datetime.now().astimezone()


def deadline(s: dict, t0: float) -> float | None:
    """When the loop stops (epoch seconds), or None (number of iterations / never)."""
    stop = s.get("stop")
    zone = _zone(s)
    if stop == "After a duration":
        return t0 + seconds(s.get("duration"), "Duration")
    if stop == "At a time of day":
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", s.get("time_of_day") or "")
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise InvalidSettingsError('Time of day: e.g. 17:30 or 06:00:00')
        now = _now(zone)
        at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
        if at <= now:
            at += dt.timedelta(days=1)  # (the next time it is that time)
        return at.timestamp()
    if stop == "At a date and time":
        try:
            at = dt.datetime.fromisoformat((s.get("date_time") or "").strip())
        except ValueError:
            raise InvalidSettingsError("Date and time: e.g. 2026-12-31 23:59") from None
        if at.tzinfo is None:
            at = at.replace(tzinfo=zone) if zone else at.astimezone()
        return at.timestamp()
    return None


def stop_fields():
    return [
        F("stop", "enum", "Stop", "After a duration", options=STOPS),
        F("count", "int", "Iterations", 10, show_if={"stop": "After a number of iterations"}),
        F("duration", "string", "Duration", "30s", "e.g. 30s, 5m, 1h 30m.", show_if={"stop": "After a duration"}),
        F("time_of_day", "string", "Time of day", "17:00", "HH:MM or HH:MM:SS; the next time it is that time.",
          show_if={"stop": "At a time of day"}),
        F("date_time", "string", "Date and time", "", "e.g. 2026-12-31 23:59 (ISO).", show_if={"stop": "At a date and time"}),
        F("timezone", "string", "Time zone", "", "For the times of day and the time columns, e.g. Europe/Berlin. "
          "Empty: the clock of the Jupyter server.",
          show_if={"stop": ["At a time of day", "At a date and time", "After a duration", "After a number of iterations",
                            "Never (until canceled)"]}),
        F("add_time", "bool", "Add time columns", True, "Loop time (when the iteration started) and Elapsed (s)."),
    ]


TIME_HELP = (
    " Stops after a number of iterations, after a duration, at a time of day (the next time it is that time), at "
    "a date and time, or never (until canceled; then the Loop End has nothing to give). The iteration running "
    "at the stop time finishes; no new one starts. Pushes <code>currentIteration</code>, <code>maxIterations</code> "
    "(-1 when not a number of iterations), <code>loopTime</code> (ISO), <code>loopElapsed</code> and "
    "<code>loopRemaining</code> (seconds; -1: no end). The input table (optional) is passed on, with the time "
    "columns if wanted. Close it with a Loop End."
)


class _TimeLoopStart(NodeModel):
    abstract = True
    closed_by = CountingLoopStart.closed_by
    category = "Flow Control/Loop Support"
    node_type = "LoopStart"
    loop_role = "start"
    in_ports = [Port(TABLE, "Any input table", optional=True)]
    out_ports = [Port(TABLE, "Input table, with the time columns")]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0] if in_specs and in_specs[0] is not None else pl.Schema()
        if s.get("stop") not in STOPS:
            raise InvalidSettingsError("Choose how the loop stops")
        if s.get("stop") == "After a duration":
            seconds(s.get("duration"), "Duration")
        cols = {"Loop time": pl.Datetime("us", "UTC"), "Elapsed (s)": pl.Float64} if s.get("add_time") else {}
        return [pl.Schema({**dict(spec), **cols})]

    def next_start(self, state, s, now) -> float:
        """When the next iteration would start (to tell whether it is before the stop time)."""
        return now

    def wait(self, state, s, ctx, it):
        """Wait before iteration `it` (interval loops)."""

    def execute(self, inputs, s, ctx):
        df = inputs[0]  # (None: no input table)
        if ctx.dry:
            return [self._with_time(df, s, time.time(), 0.0)]
        loop, state = ctx.loop, ctx.loop["state"]
        it = loop["iteration"]
        if it == 0:
            state["t0"] = time.time()
            state["deadline"] = deadline(s, state["t0"])
            state["count"] = max(1, int(s.get("count") or 1)) if s.get("stop") == "After a number of iterations" else None
            if state["count"]:
                loop["total"] = state["count"]
            ends = state["deadline"]
            loop["should_stop"] = (lambda: ends is not None and self.next_start(state, s, time.time()) >= ends)
            if ends is not None:
                span = ends - state["t0"]
                loop["time_progress"] = lambda: min(max((time.time() - state["t0"]) / span, 0.0), 1.0) if span > 0 else 1.0
        self.wait(state, s, ctx, it)
        started = time.time()
        if state["count"]:
            loop["last"] = it >= state["count"] - 1
        elif state["deadline"] is not None and started >= state["deadline"] and it == 0:
            loop["last"] = True  # (the stop time has passed already: one iteration)
        zone = _zone(s)
        stamp = dt.datetime.fromtimestamp(started, zone) if zone else dt.datetime.fromtimestamp(started).astimezone()
        ctx.push_variable("currentIteration", it, "INTEGER")
        ctx.push_variable("maxIterations", state["count"] or -1, "INTEGER")
        ctx.push_variable("loopTime", stamp.isoformat(timespec="seconds"), "STRING")
        ctx.push_variable("loopElapsed", started - state["t0"], "DOUBLE")
        ctx.push_variable("loopRemaining", max(state["deadline"] - started, 0.0) if state["deadline"] else -1.0, "DOUBLE")
        return [self._with_time(df, s, started, started - state["t0"])]

    @staticmethod
    def _with_time(df, s, started, elapsed):
        if not s.get("add_time"):
            return df if df is not None else pl.DataFrame()
        when = dt.datetime.fromtimestamp(started, dt.timezone.utc)
        cols = {"Loop time": pl.Datetime("us", "UTC"), "Elapsed (s)": pl.Float64}
        if df is None:  # (no input: one row with the time)
            return pl.DataFrame({"Loop time": [when], "Elapsed (s)": [elapsed]}, schema=cols)
        return df.with_columns(pl.lit(when, dtype=cols["Loop time"]).alias("Loop time"),
                               pl.lit(elapsed, dtype=pl.Float64).alias("Elapsed (s)"))


def _sleep_until(t, ctx):
    while True:
        left = t - time.time()
        if left <= 0:
            return
        ctx.check_canceled()
        time.sleep(min(left, 0.1))


class EveryIntervalLoopStart(_TimeLoopStart):
    factory = "verknode.flow.EveryIntervalLoopStart"
    name = "Every Interval Loop Start"
    icon = "⟳⏱"
    short_description = "Runs the loop every X seconds (e.g. to watch or poll something)."
    description = (
        "Starts an iteration every Interval (e.g. 5s, 1m): iteration n starts at n × Interval after the first, "
        "whatever the iteration takes. If an iteration takes longer than the interval, the next one starts right "
        "away, or at the next tick (missed ticks are skipped). (The Interval Loop Start steps a number instead.)"
        + TIME_HELP
    )
    fields = [
        F("interval", "string", "Every", "5s", "e.g. 500ms, 5s, 1m, 1h."),
        F("late", "enum", "If an iteration takes longer", "Start the next one right away",
          options=["Start the next one right away", "Skip to the next tick"]),
        F("wait_first", "bool", "Wait one interval before the first iteration", False),
        *stop_fields(),
    ]

    def configure(self, in_specs, s, ctx):
        if seconds(s.get("interval"), "Every") <= 0:
            raise InvalidSettingsError("Every: more than 0")
        return super().configure(in_specs, s, ctx)

    def _tick(self, state, s, k):
        return state["t0"] + k * seconds(s.get("interval"), "Every")

    def next_start(self, state, s, now):
        step = seconds(s.get("interval"), "Every")
        k = state.get("k", 0) + 1
        if s.get("late") == "Skip to the next tick":
            k = max(k, math.ceil((now - state["t0"]) / step))
        return max(self._tick(state, s, k), now)

    def wait(self, state, s, ctx, it):
        step = seconds(s.get("interval"), "Every")
        if it == 0:
            state["k"] = 1 if s.get("wait_first") else 0
        else:
            k = state["k"] + 1
            if s.get("late") == "Skip to the next tick":
                k = max(k, math.ceil((time.time() - state["t0"]) / step))
            state["k"] = k
        _sleep_until(self._tick(state, s, state["k"]), ctx)


class TimedLoopStart(_TimeLoopStart):
    factory = "verknode.flow.TimedLoopStart"
    name = "Timed Loop Start"
    icon = "⟳⌛"
    short_description = "Loops again and again until a time (or a duration, or forever)."
    description = (
        "Runs iteration after iteration - with a Pause between them if wanted - until the stop time: e.g. "
        "improve an estimate for 30 seconds, or work until 17:00." + TIME_HELP
    )
    fields = [
        F("pause", "string", "Pause between iterations", "0", "e.g. 0, 200ms, 2s."),
        *stop_fields(),
    ]

    def next_start(self, state, s, now):
        return now + seconds(s.get("pause"), "Pause")

    def wait(self, state, s, ctx, it):
        if it > 0:
            _sleep_until(time.time() + seconds(s.get("pause"), "Pause"), ctx)
