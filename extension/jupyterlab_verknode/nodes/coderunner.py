"""Python Code Runner: runs the Python code of each row (e.g. written by an LLM) in a separate
process, with tests, a time limit and a memory limit, and reports whether it passed."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE, require_column

FENCE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S)
OUT = {"Passed": pl.Boolean, "Error": pl.String, "Output": pl.String, "Seconds": pl.Float64}


def code_of(text: str) -> str:
    """The code of a cell: the ```python blocks of a Markdown answer, or the text itself."""
    blocks = FENCE.findall(text or "")
    return "\n\n".join(b.strip("\n") for b in blocks) if blocks else (text or "")


def _limits(memory_mb: int, cpu_s: int) -> str:
    """First line of the program: its limits (set in the child: no preexec_fn in a threaded server)."""
    if os.name != "posix":
        return ""
    parts = []
    if memory_mb > 0:  # (the data segment: address space limits break libraries that reserve a lot)
        parts.append(f"_r.setrlimit(_r.RLIMIT_DATA, ({memory_mb * 2 ** 20}, {memory_mb * 2 ** 20}))")
    if cpu_s > 0:
        parts.append(f"_r.setrlimit(_r.RLIMIT_CPU, ({cpu_s}, {cpu_s + 1}))")
    return ("import resource as _r; " + "; ".join(parts) + "; del _r") if parts else ""


def run(code: str, tests: str, setup: str, timeout: float, memory_mb: int, max_output: int) -> dict:
    """Run setup + code + tests in a fresh `python -I` in an empty folder."""
    source = "\n\n".join(p for p in (_limits(memory_mb, int(timeout) + 1), setup, code, tests) if p and p.strip()) + "\n"
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="verknode-run-") as folder:
        path = os.path.join(folder, "main.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        try:
            p = subprocess.run([sys.executable, "-I", path], cwd=folder, capture_output=True, text=True,
                               timeout=timeout, start_new_session=True,
                               env={"PATH": os.environ.get("PATH", ""), "HOME": folder, "PYTHONHASHSEED": "0",
                                    "LANG": "C.UTF-8"})
            out, passed = (p.stdout + p.stderr), p.returncode == 0
            if p.returncode < 0:
                out += f"\n(killed by signal {-p.returncode}: memory or CPU time limit?)"
        except subprocess.TimeoutExpired as e:
            out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            out += f"\nTimed out after {timeout:g} s"
            passed = False
    out = out.replace(folder, ".").strip()
    if len(out) > max_output:
        out = "…" + out[-max_output:]  # (the end: where the error is)
    # the error in a line: the last line of what it printed (e.g. "AssertionError: ...")
    error = None if passed else (next((line.strip() for line in reversed(out.splitlines()) if line.strip()), "failed"))
    return {"Passed": passed, "Error": error, "Output": out if out else ("OK" if passed else ""),
            "Seconds": time.time() - t0}


class PythonCodeRunner(NodeModel):
    factory = "verknode.scripting.CodeRunner"
    name = "Python Code Runner"
    category = "Scripting"
    icon = "▶Py"
    node_type = "Manipulator"
    short_description = "Runs the Python code of each row with its tests; passed or the error."
    description = (
        "Runs the code of each row - e.g. written by an LLM (```python blocks of an answer are taken out of "
        "it) - followed by the row's tests (e.g. <code>assert f(2) == 4</code>) in a new Python process, in an "
        "empty temporary folder, with a time and a memory limit. Adds <b>Passed</b> (it ran to the end without "
        "an error), <b>Error</b> (the error in a line, e.g. the failing assert), <b>Output</b> (what it printed, "
        "with the whole error) and <b>Seconds</b>. "
        "The code can use the Python packages of the server (polars, RDKit, ...). It runs as the Jupyter "
        "server's user: the limits keep runaway code in check, but this is not a sandbox - run code you would "
        "run yourself."
    )
    in_ports = [Port(TABLE, "Code", "A code column (and optionally a tests column)")]
    out_ports = [Port(TABLE, "Results", "The input with Passed, Error, Output and Seconds")]
    fields = [
        F("column", "column", "Code column", None, kinds=["string"]),
        F("tests", "column", "Tests column (optional)", None, "Run after the code; empty: only the code.",
          kinds=["string"]),
        F("setup", "code", "Run before every code", "", "E.g. imports the code may rely on.", language="python"),
        F("timeout", "float", "Time limit (s)", 10.0),
        F("memory_mb", "int", "Memory limit (MB)", 2048, "0: none (Linux / macOS only)."),
        F("max_output", "int", "Longest output (characters)", 4000, "The end is kept: there the error is."),
        F("parallel", "int", "Runs at a time", 4),
    ]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0]
        require_column(s, "column", spec)
        if s.get("tests") and s["tests"] not in spec:
            raise InvalidSettingsError(f'Column "{s["tests"]}" is not in the input')
        return [pl.Schema({**dict(spec), **OUT})]  # (existing Passed / Output / Seconds are replaced)

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        col = require_column(s, "column", df.schema)
        codes = df[col].cast(pl.String).to_list()
        tests = df[s["tests"]].cast(pl.String).to_list() if s.get("tests") else [""] * df.height
        if ctx.dry:
            results = [dict.fromkeys(OUT)] * df.height
        else:
            done = [0]

            def one(i):
                ctx.check_canceled()
                r = run(code_of(codes[i]), tests[i] or "", s.get("setup") or "", float(s.get("timeout") or 10),
                        int(s.get("memory_mb") or 0), int(s.get("max_output") or 4000))
                done[0] += 1
                ctx.set_progress(done[0] / max(df.height, 1))
                return r
            with ThreadPoolExecutor(max_workers=max(1, int(s.get("parallel") or 1))) as pool:
                results = list(pool.map(one, range(df.height)))
            passed = sum(1 for r in results if r["Passed"])
            ctx.log(f"{passed} of {df.height} passed")
        new = pl.DataFrame(results, schema=OUT)
        return [df.with_columns(new[c] for c in OUT if c in df.columns).hstack(new.select(c for c in OUT if c not in df.columns))]
