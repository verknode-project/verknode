"""LLM nodes that read results: a chat that answers questions about the workflow's data, and a
summary of input tables by a prompt.

Data Q&A Chat is one node that carries a chat (like the Workflow Agent Chat): the model answers with
read-only tools over the tables it can see - its inputs, and the outputs of the workflow's executed
nodes - and computes the numbers it gives with SQL (polars), so they are the data's, not guesses.

LLM Summarizer sends a prompt and its input tables (columns, statistics, the first rows) to the model
and outputs the answer as a table row (and the flow variable summary) - for a Text Display, a report,
an e-mail node.
"""
from __future__ import annotations

import json
import re
import time

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE
from jupyterlab_verknode.engine.node import CanceledExecutionError

from .llm import LLMNode, chat, chat_tools, llm_port, server, server_fields, server_for, tool_message

CONVERSATION = {"role": pl.String, "content": pl.String}


def _short(v, n=300):
    text = json.dumps(v, default=str)
    return text if len(text) <= n else text[:n] + "…"


def _slug(text: str) -> str:
    s = re.sub(r"\W+", "_", (text or "").strip().lower()).strip("_")
    return s if s and not s[0].isdigit() else f"t_{s}"


def markdown(df: pl.DataFrame, rows: int = 20, cols: int = 12) -> str:
    shown = df.columns[:cols]
    lines = ["| " + " | ".join(shown) + " |", "|" + "---|" * len(shown)]
    for r in df.head(rows).iter_rows(named=True):
        cells = []
        for c in shown:
            v = r[c]
            v = f"{v:.6g}" if isinstance(v, float) else ("" if v is None else str(v))
            cells.append(v.replace("|", "/").replace("\n", " ")[:60])
        lines.append("| " + " | ".join(cells) + " |")
    more = []
    if df.height > rows:
        more.append(f"{df.height} rows")
    if df.width > cols:
        more.append(f"{df.width} columns")
    return "\n".join(lines) + (f"\n({', '.join(more)})" if more else "")


def profile(df: pl.DataFrame, rows: int) -> str:
    """A table for a model: its columns, statistics of the numeric ones, value counts of the short
    text ones, and the first rows (all of them when there are few)."""
    out = [f"{df.height} rows, {df.width} columns: " + ", ".join(f"{c} ({t})" for c, t in df.schema.items())]
    num = [c for c, t in df.schema.items() if t.is_numeric()]
    if num and df.height:
        stats = df.select(num).describe()
        out.append("Statistics:\n" + markdown(stats, 12, 13))
    for c, t in df.schema.items():
        if t == pl.String and df.height:
            n = df[c].n_unique()
            if 1 < n <= 12 and n < df.height:
                vc = df[c].value_counts(sort=True)
                out.append(f"{c}: " + ", ".join(f"{v} ({k})" for v, k in vc.iter_rows()))
    out.append(("All rows:\n" if df.height <= rows else f"The first {rows} rows:\n") + markdown(df, rows, 40))
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------
# the tables a chat or a summary sees
# ---------------------------------------------------------------------------------------------------
def visible_tables(ctx, inputs, s, me=None) -> dict[str, dict]:
    """{name: {"df", "source", "node"}}: the inputs (input1...), then the executed nodes' tables."""
    tables: dict[str, dict] = {}
    for i, df in enumerate(inputs or [], 1):
        if isinstance(df, pl.DataFrame):
            tables[f"input{i}"] = {"df": df, "source": f"input port {i}", "node": None}
    if (s.get("scope") or "Inputs and the workflow's results") == "Only its inputs":
        return tables
    sw = ctx.script_workflow() if getattr(ctx, "script_workflow", None) else None
    if sw is None:
        return tables
    wf = sw._wf
    bounds = wf._boundaries()
    for nid in wf.topo_order():
        nc = wf.nodes[nid]
        if nid == me or nid in bounds or not nc.executed:
            continue
        for port, out in enumerate((nc.outputs or [])[1:], 1):
            if not isinstance(out, pl.DataFrame) or not out.width:
                continue
            base = _slug(nc.annotation if nc.annotation and nc.annotation != f"Node {nid}" else nc.name)
            name = base if port == 1 else f"{base}_{port}"
            if name in tables:
                name = f"{name}_{nid}"
            tables[name] = {"df": out, "source": f"node #{nid} {nc.name} \"{nc.annotation}\""
                            + (f" output {port}" if port > 1 else ""), "node": nid}
    return tables


TOOLS = [
    {"name": "list_tables", "description": "The tables you can use: their names, where they come from (node), rows "
     "and columns.", "parameters": {"type": "object", "properties": {}}},
    {"name": "describe", "description": "A table's columns, statistics of its numeric columns and the values of its "
     "short text columns.", "parameters": {"type": "object", "properties": {"table": {"type": "string"}},
                                           "required": ["table"]}},
    {"name": "rows", "description": "Rows of a table.", "parameters": {"type": "object", "properties": {
        "table": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
        "offset": {"type": "integer"}, "rows": {"type": "integer", "description": "at most 100 (default 20)"}},
        "required": ["table"]}},
    {"name": "sql", "description": "Run a SQL query (polars SQL: SELECT, WHERE, GROUP BY, JOIN, ORDER BY, window "
     "functions...) over the tables, by their names. Use it for every number you report.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

SYSTEM = """You answer questions about the data of a verknode workflow: the tables of its executed nodes \
(and the tables connected to this node). Use the tools: list_tables (what there is), describe (columns, \
statistics), rows (look at rows) and sql (compute - polars SQL over the tables by their names; quote names \
with double quotes when needed). Compute every number you report with sql - for each question, also shares, differences and totals; \
never do arithmetic yourself, never answer numbers from memory or an earlier answer. Filter on values as \
list_tables spells them. \
Answer briefly: the answer first, with its numbers; a small Markdown table when it helps; then which table \
(node) it comes from. If the data cannot answer the question, say so and say what is there."""


class Tables:
    """The read-only tools over the visible tables."""

    def __init__(self, tables: dict[str, dict]):
        self.tables = tables
        self.queried = False  # (sql ran for this question)

    def df(self, name) -> pl.DataFrame:
        t = self.tables.get(str(name)) or self.tables.get(_slug(str(name)))
        if t is None:
            raise ValueError(f"there is no table {name!r}; the tables are {', '.join(self.tables) or 'none'}")
        return t["df"]

    def call(self, name, args) -> str:
        try:
            if name == "list_tables":
                if not self.tables:
                    return "There are no tables: run the workflow (or connect tables to this node) first."
                return "\n".join(f"- {n}: {t['source']}, {t['df'].height} rows; columns "
                                 + ", ".join(f"{c} ({d})" + _values(t["df"], c) for c, d in list(t["df"].schema.items())[:30])
                                 for n, t in self.tables.items())
            if name == "describe":
                return profile(self.df(args.get("table")), 5)
            if name == "rows":
                df = self.df(args.get("table"))
                cols = args.get("columns") or None
                if cols:
                    df = df.select([c for c in cols if c in df.columns] or df.columns)
                off, n = max(0, int(args.get("offset") or 0)), max(1, min(int(args.get("rows") or 20), 100))
                return markdown(df.slice(off, n), n, 30) + (f"\n(rows {off}-{off + n - 1} of {df.height})" if df.height > n else "")
            if name == "sql":
                frames = {n: _plain(t["df"]) for n, t in self.tables.items()}
                out = pl.SQLContext(frames).execute(str(args.get("query") or ""), eager=True)
                self.queried = True
                hint = ""
                if not out.height:
                    hint = "\n(No rows: check the values you filter on - list_tables shows them, or SELECT DISTINCT.)"
                elif out.height == 1 and all(v in (0, None) for v in out.row(0)):
                    hint = "\n(The result is 0 / empty: check the values you filter on - list_tables shows them.)"
                return f"{out.height} rows:\n" + markdown(out, 50, 30) + hint
            return f"Error: there is no tool {name!r}"
        except CanceledExecutionError:
            raise
        except Exception as e:  # noqa: BLE001 - errors go back to the model
            return f"Error: {e}"


def _values(df: pl.DataFrame, col: str) -> str:
    """The values of a short text column (so the model filters on them as they are spelled)."""
    if df.schema[col] != pl.String or not df.height:
        return ""
    values = df[col].drop_nulls().unique().sort().to_list()
    return f" = {values}" if 0 < len(values) <= 12 else ""


def _plain(df: pl.DataFrame) -> pl.DataFrame:
    """Molecules as SMILES (SQL and text know no molecules)."""
    try:
        from jupyterlab_verknode.standalone import plain

        return plain(df)
    except Exception:  # noqa: BLE001
        return df


def answer(ctx, s, tables: Tables, question: str, history: list, status=lambda t: None, srv=None) -> str:
    """The model's answer to a question, with the tools."""
    srv = srv or server(s)
    guidance = (s.get("guidance") or "").strip()
    listing = tables.call("list_tables", {})
    messages = [{"role": "system", "content": SYSTEM + (f"\n\nMore instructions:\n{guidance}" if guidance else "")}]
    messages += history + [{"role": "user", "content": f"The tables:\n{listing}\n\nMy question: {question}"}]
    steps, nudges = max(3, int(s.get("steps") or 20)), 0
    for step in range(steps):
        ctx.check_canceled()
        status("Thinking…" if step == 0 else f"Looking at the data… (step {step + 1})")
        r = chat_tools(srv, messages, s, TOOLS)
        messages.append(r["message"])
        if r["calls"]:
            for c in r["calls"]:
                ctx.log(f"Q&A tool {c['name']} {_short(c['args'], 200)}")
                messages.append(tool_message(srv, c, tables.call(c["name"], c["args"])))
            continue
        text = (r["text"] or "").strip()
        if not text and nudges < 2:
            nudges += 1
            messages.append({"role": "user", "content": "Go on: look at the data with the tools, then answer."})
            continue
        if text and re.search(r"\d", text) and not tables.queried and nudges < 2 and tables.tables:
            # (numbers without a query for this question: computed, not remembered)
            nudges += 1
            messages.append({"role": "user", "content": "Compute the numbers of your answer with sql (for this "
                             "question), then answer again."})
            continue
        return text or "I have no answer to that."
    return "(I stopped after the steps I may take - ask again, more precisely.)"


def _history(msgs, memory):
    out = [{"role": m["role"], "content": m["content"]} for m in msgs[-max(0, int(memory or 20)):] if m["role"] in ("user", "assistant")]
    while out and out[0]["role"] == "assistant":
        out = out[1:]
    return out


class DataQAChat(LLMNode, NodeModel):
    factory = "verknode.llm.DataChat"
    name = "Data Q&A Chat"
    category = "AI/LLM"
    icon = "?💬"
    node_type = "Manipulator"
    sandbox_unsafe = True  # (waits for the canvas)
    needs_canvas = True  # (not in headless runs: export)
    keeps_display_on_reset = True  # (the conversation stays, to resume it)
    renderer = "chat"
    width, height = 420, 520
    short_description = "A chat on the canvas that answers questions about the workflow's results."
    description = (
        "Ask about the data: \"which region did best against its target?\", \"what share of North's revenue are "
        "headlamps?\". The model sees the tables of the workflow's executed nodes (and those connected to this "
        "node - or only those, with Tables) and answers with read-only tools: it lists and describes the "
        "tables, looks at rows and computes with SQL (polars), so the numbers it gives are computed from the data. "
        "Nothing in the workflow changes. Run the workflow first (the nodes before it run when it starts). Like "
        "the Workflow Agent Chat it keeps its conversation (Resume chat / New chat), and Tab adds the nodes "
        "selected on the canvas (their tables) to the question. Check the answers: models make mistakes."
    )
    in_ports = [Port(TABLE, "Table 1", "A table to ask about (optional)", optional=True),
                Port(TABLE, "Table 2", "(optional)", optional=True),
                Port(TABLE, "Table 3", "(optional)", optional=True), llm_port()]
    out_ports = [Port(TABLE, "Conversation", "role and content: what was said")]
    fields = [
        F("title", "string", "Title", "Ask about the data", "Shown above the chat on the canvas."),
        F("greeting", "string", "Greeting", "Hi! Ask me about the results of this workflow - I look them up and "
          "compute the numbers from the data.", "The first message (empty: none)."),
        F("placeholder", "string", "Placeholder", "Ask about the data…"),
        F("scope", "enum", "Tables", "Inputs and the workflow's results",
          "What it can see: its input tables and the outputs of the executed nodes, or only its inputs.",
          options=["Inputs and the workflow's results", "Only its inputs"]),
        F("guidance", "code", "Guidance", "", "More instructions (e.g. what the columns mean, the currency)."),
        F("steps", "int", "Tool calls per question at most", 20),
        F("memory", "int", "Memory (messages)", 20),
        *server_fields(),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(CONVERSATION)]

    def keeps_display(self, old: dict, new: dict) -> bool:
        return True

    def widget(self, s: dict, display) -> dict:
        data = {**(display or {}), "resumable": True}
        return {"type": "output", "renderer": self.renderer, "title": s.get("title") or "", "width": self.width,
                "height": self.height, "data": data, "interactive": False, "selection": None}

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=CONVERSATION)]
        sw = ctx.script_workflow()
        me = sw._me if sw is not None else None
        msgs: list[dict] = []
        got0 = [x for x in ctx.received() if isinstance(x, dict)]
        if not any(x.get("new") for x in got0):
            prev = (ctx.previous_display or {}).get("messages")
            if isinstance(prev, list) and any(isinstance(m, dict) and m.get("role") == "user" for m in prev):
                msgs = [{k: m[k] for k in ("role", "content", "meta") if k in m} for m in prev if isinstance(m, dict)]
        greeting = (s.get("greeting") or "").strip()
        if greeting and not msgs:
            msgs.append({"role": "assistant", "content": greeting})
        base = {"placeholder": s.get("placeholder") or "", "title": s.get("title") or ""}

        def show(**kw):
            ctx.set_display({**base, "messages": list(msgs), **kw})

        srv = server_for(s, inputs)
        model = srv["model"]
        try:
            while True:
                show(waiting=True)
                got = ctx.wait_for_input() or {}
                if got.get("end"):
                    show(waiting=False, ended=True)
                    break
                text = str(got.get("text") or "").strip()
                if not text:
                    continue
                msgs.append({"role": "user", "content": text})
                show(waiting=False, status="Thinking…")
                t0 = time.time()
                # (the tables as they are now; @#id: the tables of those nodes)
                tables = Tables(visible_tables(ctx, inputs, s, me))
                question = text
                ids = [int(m) for m in re.findall(r"@#(\d+)", text)]
                named = [n for n, t in tables.tables.items() if t["node"] in ids]
                if named:
                    question += f"\n(@#id: the tables {', '.join(named)})"
                try:
                    reply = answer(ctx, s, tables, question, _history(msgs[:-1], s.get("memory", 20)),
                                   status=lambda t: show(waiting=False, status=t), srv=srv)
                except CanceledExecutionError:
                    raise
                except Exception as e:  # noqa: BLE001 - the chat goes on
                    reply = f"**Something went wrong:** {e}"
                msgs.append({"role": "assistant", "content": reply, "meta": f"{model} · {time.time() - t0:.1f} s"})
        except CanceledExecutionError:
            show(waiting=False)
            raise
        return [pl.DataFrame({"role": [m["role"] for m in msgs], "content": [m["content"] for m in msgs]},
                             schema=CONVERSATION)]


class LLMSummarizer(LLMNode, NodeModel):
    factory = "verknode.llm.Summarize"
    name = "LLM Summarizer"
    category = "AI/LLM"
    icon = "LLM"
    node_type = "Manipulator"
    short_description = "A summary of the input tables by a prompt (e.g. findings for a report)."
    description = (
        "Sends the prompt and the input tables to the model - for each table its columns, statistics of the "
        "numeric columns, the values of short text columns and its first rows (all of them when there are few) - "
        "and outputs the answer as one row (summary, model, seconds); the summary is also the flow variable "
        "summary. Show it with a Text Display, put it into a report or an e-mail. The tables are named by the "
        "nodes they come from. Check the summary: models make mistakes."
    )
    in_ports = [Port(TABLE, "Table 1", "The data to summarize"),
                Port(TABLE, "Table 2", "(optional)", optional=True),
                Port(TABLE, "Table 3", "(optional)", optional=True), llm_port()]
    out_ports = [Port(TABLE, "Summary", "One row: summary, model, seconds")]
    OUT = {"summary": pl.String, "model": pl.String, "seconds": pl.Float64}
    fields = [
        F("prompt", "code", "Prompt", "Summarize the key findings in this data in three short bullet points, with "
          "the numbers.", "What to write (the tables follow it)."),
        F("system", "code", "System prompt", "You are a careful data analyst. Use only the data given, quote its "
          "numbers exactly and do not invent anything. Write plain Markdown."),
        F("rows", "int", "Rows sent per table", 100, "Tables with more rows are sent as statistics and their first rows."),
        *server_fields(),
    ]

    def configure(self, in_specs, s, ctx):
        if not (s.get("prompt") or "").strip():
            raise InvalidSettingsError("Write a prompt")
        return [pl.Schema(self.OUT)]

    def execute(self, inputs, s, ctx):
        if ctx.dry:
            return [pl.DataFrame(schema=self.OUT)]
        names = self._names(ctx, len(inputs))
        parts = []
        for i, df in enumerate(inputs):
            if isinstance(df, pl.DataFrame):
                parts.append(f"## Table {i + 1}: {names[i]}\n{profile(_plain(df), max(1, int(s.get('rows') or 100)))}")
        srv = server_for(s, inputs)
        msgs = [{"role": "system", "content": (s.get("system") or "").strip()},
                {"role": "user", "content": f"{s['prompt'].strip()}\n\n# The data\n\n" + "\n\n".join(parts)}]
        r = chat(srv, [m for m in msgs if m["content"]], s)
        text = (r["text"] or "").strip()
        ctx.log(f"{srv['model']}: {r.get('tokens_out') or 0} tokens in {r['seconds']:.1f} s")
        ctx.push_variable("summary", text, "STRING")
        return [pl.DataFrame({"summary": [text], "model": [srv["model"]], "seconds": [r["seconds"]]}, schema=self.OUT)]

    @staticmethod
    def _names(ctx, n):
        """The labels of the nodes the tables come from (else Table 1...)."""
        out = [f"Table {i + 1}" for i in range(n)]
        try:
            sw = ctx.script_workflow()
            wf = sw._wf
            for port, c in wf.in_conns(sw._me).items():
                if 1 <= port <= n:
                    nc = wf.nodes[c.source_id]
                    out[port - 1] = nc.annotation if nc.annotation and nc.annotation != f"Node {nc.id}" else nc.name
        except Exception:  # noqa: BLE001 - names are a nicety
            pass
        return out
