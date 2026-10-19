"""Harness nodes: the parts of an LLM harness - the loop around a model that gives it tools, checks what
it may do, runs the tools, keeps its context in budget and decides when it is done - as nodes, so the
harness is a workflow you can see, change and measure.

The conversation is a table, one message per row: role (system, user, assistant, tool), content,
tool_calls (JSON: [{"id", "name", "args"}]), tool_call_id, name (the tool), turn, tokens_in, tokens_out.
A Recursive Loop carries it from turn to turn:

    conversation -> Context Window -> LLM Step -> Tool Policy -> Tool Executor -> (append) -> loop end

- LLM Step: one model call with the tools of a tools table; the assistant's message and its tool
  calls. It sets the flow variable done (no tool calls: the answer).
- Tool Policy: rules over the calls - allow, deny (by tool, by a pattern of the arguments), limits per
  tool and per run; denied calls get a tool message that says why.
- Tool Executor: runs the calls by the tools table's kind - SQL over its input tables, Python in a
  separate process with time and memory limits, a verknode workflow (arguments as its variables), an
  HTTP GET - and gives a tool message per call.
- Context Window: keeps the conversation within a token budget - old tool results shortened, oldest
  turns dropped or summarized by the model; tool calls and their results stay together.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse

import polars as pl

from verknode.node import F, InvalidSettingsError, NodeModel, Port, TABLE

from .llm import chat, chat_tools, llm_port, server_fields, server_for, tool_message

CAT = "AI/Harness"
CONV = {"role": pl.String, "content": pl.String, "tool_calls": pl.String, "tool_call_id": pl.String,
        "name": pl.String, "turn": pl.Int64, "tokens_in": pl.Int64, "tokens_out": pl.Int64}
CALLS = {"call_id": pl.String, "name": pl.String, "args": pl.String, "turn": pl.Int64}
RESULTS = {**CONV, "ok": pl.Boolean, "seconds": pl.Float64}
KINDS = ["sql", "python", "workflow", "http_get"]


def conversation(df: pl.DataFrame | None) -> pl.DataFrame:
    """The table in the harness's conversation form (missing columns empty)."""
    if df is None:
        return pl.DataFrame(schema=CONV)
    if "role" not in df.columns or "content" not in df.columns:
        raise InvalidSettingsError("The conversation needs the columns role and content")
    out = df.with_columns([pl.lit(None, dtype=t).alias(c) for c, t in CONV.items() if c not in df.columns])
    return out.select([pl.col(c).cast(t, strict=False) for c, t in CONV.items()])


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def to_messages(srv: dict, conv: pl.DataFrame) -> list[dict]:
    """The rows as chat messages of the server's provider (tool calls in its form)."""
    out = []
    for r in conv.iter_rows(named=True):
        role = r["role"]
        if role == "tool":
            out.append(tool_message(srv, {"id": r["tool_call_id"] or "", "name": r["name"] or "tool"}, r["content"] or ""))
            continue
        msg = {"role": role if role in ("system", "user", "assistant") else "user", "content": r["content"] or ""}
        calls = json.loads(r["tool_calls"]) if role == "assistant" and r["tool_calls"] else []
        if calls:
            if srv["provider"] == "Ollama":
                msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("args") or {}}} for c in calls]
            else:
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {})}} for c in calls]
        out.append(msg)
    return out


def tool_specs(tools: pl.DataFrame | None) -> list[dict]:
    if tools is None or not tools.height:
        return []
    for c in ("name", "description", "parameters"):
        if c not in tools.columns:
            raise InvalidSettingsError(f"The tools table needs the columns name, description, parameters (JSON schema): no {c}")
    out = []
    for r in tools.iter_rows(named=True):
        try:
            params = json.loads(r["parameters"]) if r["parameters"] else {"type": "object", "properties": {}}
        except ValueError:
            raise InvalidSettingsError(f"Tool {r['name']}: parameters is not JSON") from None
        out.append({"name": r["name"], "description": r["description"] or "", "parameters": params})
    return out


# ---------------------------------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------------------------------
class LLMStep(NodeModel):
    factory = "verknode.harness.LLMStep"
    name = "LLM Step"
    category = CAT
    icon = "🧠"
    in_ports = [Port(TABLE, "Conversation", "role, content (and tool_calls, tool_call_id, name, turn): what was said"),
                Port(TABLE, "Tools", "The tools: name, description, parameters (a JSON schema). Optional: none", optional=True),
                llm_port()]
    out_ports = [Port(TABLE, "Conversation", "The conversation with the model's message last"),
                 Port(TABLE, "Tool calls", "The calls of this turn: call_id, name, args (JSON)")]
    short_description = "One turn of a model with tools: its message and the tool calls it makes."
    description = (
        "Sends the conversation (and the system prompt, if it has none) to the model with the tools of the tools "
        "table, and outputs the conversation with the model's message last, and its tool calls, one row each. The "
        "flow variable <code>done</code> is true when the model made no call (it answered), "
        "<code>answer</code> holds its text, <code>turn</code> the turn number. In a Recursive Loop with Tool "
        "Executor it makes a harness (see the harness example)."
    )
    fields = [
        F("system", "code", "System prompt", "You are a careful assistant. Use the tools when they help; answer "
          "briefly when you are done.", "Used when the conversation has no system message."),
        F("temperature", "float", "Temperature", 0.0),
        F("max_tokens", "int", "Longest answer (tokens)", 2000),
        *server_fields()[:4],
        F("think", "bool", "Let reasoning models think first", False),
        F("timeout", "int", "Timeout per request (s)", 300),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(CONV), pl.Schema(CALLS)]

    def execute(self, inputs, s, ctx):
        conv = conversation(inputs[0])
        if ctx.dry:
            return [conv, pl.DataFrame(schema=CALLS)]
        tools = tool_specs(inputs[1] if len(inputs) > 1 else None)
        srv = server_for(s, inputs)
        if not conv.filter(pl.col("role") == "system").height and (s.get("system") or "").strip():
            conv = pl.concat([conversation(pl.DataFrame({"role": ["system"], "content": [s["system"].strip()]})), conv])
        turn = int((conv["turn"].max() or 0) + 1) if conv.height else 1
        msgs = to_messages(srv, conv)
        if tools:
            r = chat_tools(srv, msgs, s, tools)
        else:
            r = {**chat(srv, msgs, s), "calls": []}
        calls = [{"id": c["id"] or f"call_{turn}_{i}", "name": c["name"], "args": c["args"]} for i, c in enumerate(r["calls"])]
        row = {"role": "assistant", "content": (r.get("text") or "").strip(), "tool_calls": json.dumps(calls) if calls else None,
               "tool_call_id": None, "name": None, "turn": turn, "tokens_in": r.get("tokens_in"), "tokens_out": r.get("tokens_out")}
        out = pl.concat([conv, pl.DataFrame([row], schema=CONV)])
        ctx.push_variable("done", "false" if calls else "true", "STRING")
        ctx.push_variable("answer", row["content"] if not calls else "", "STRING")
        ctx.push_variable("turn", turn, "INTEGER")
        ctx.log(f"turn {turn}: {len(calls)} tool call(s)" + (": " + ", ".join(c["name"] for c in calls) if calls else " - the answer"))
        return [out, pl.DataFrame([{"call_id": c["id"], "name": c["name"], "args": json.dumps(c["args"]), "turn": turn} for c in calls],
                                  schema=CALLS)]


# ---------------------------------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------------------------------
DEFAULT_RULES = [
    {"tool": "python", "action": "Deny", "pattern": r"\bimport\s+(os|subprocess|socket|shutil)|\bopen\(|__import__",
     "reason": "no file system, processes or network from Python"},
    {"tool": "sql", "action": "Deny", "pattern": r"(?i)\b(drop|delete|insert|update|attach|copy)\b", "reason": "read-only SQL"},
    {"tool": "*", "action": "Allow", "pattern": "", "reason": ""},
]


class ToolPolicy(NodeModel):
    factory = "verknode.harness.ToolPolicy"
    name = "Tool Policy"
    category = CAT
    icon = "🛡"
    in_ports = [Port(TABLE, "Tool calls", "call_id, name, args (of LLM Step)"),
                Port(TABLE, "Conversation", "The conversation so far (optional): for the limits per run", optional=True)]
    out_ports = [Port(TABLE, "Allowed", "The calls that may run"),
                 Port(TABLE, "Denied", "A tool message for each denied call, saying why")]
    short_description = "Allows or denies tool calls by rules: tools, argument patterns, limits per tool and run."
    description = (
        "The rules are checked top down; the first that matches a call decides. A rule names a tool (or * for any) "
        "and, optionally, a regular expression the call's arguments (JSON) must match. Limits: calls per tool and "
        "calls in all, counting the earlier calls of the conversation (second input). Unknown tools are denied. "
        "A denied call gets a tool message (Denied: why), so the model learns and can try another way."
    )
    fields = [
        F("rules", "records", "Rules", DEFAULT_RULES, fields=[
            F("tool", "string", "Tool", "*"), F("action", "enum", "Action", "Allow", options=["Allow", "Deny"]),
            F("pattern", "string", "Arguments match (regex)", ""), F("reason", "string", "Why (denied)", "")]),
        F("tools", "string", "Known tools", "", "Comma separated; empty: any. Calls to other tools are denied."),
        F("per_tool", "int", "Calls per tool at most", 8, "In the whole conversation. 0: no limit."),
        F("total", "int", "Calls in all at most", 20, "0: no limit."),
        F("repeats", "bool", "Deny repeated calls", True,
          "A call with the same tool and arguments as an earlier one is denied, with the turn of its result: "
          "models that loop get unstuck."),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(CALLS), pl.Schema(RESULTS)]

    def execute(self, inputs, s, ctx):
        calls = inputs[0]
        conv = conversation(inputs[1]) if len(inputs) > 1 and inputs[1] is not None else None
        earlier: dict = {}
        seen: dict = {}  # (tool, arguments) -> the turn of the first such call
        this_turn = set(calls["call_id"].to_list())
        if conv is not None:
            for tc, turn in conv.select("tool_calls", "turn").drop_nulls("tool_calls").iter_rows():
                for c in json.loads(tc):
                    earlier[c["name"]] = earlier.get(c["name"], 0) + 1
                    if c.get("id") not in this_turn:
                        seen.setdefault((c["name"], json.dumps(c.get("args") or {}, sort_keys=True)), turn)
            for name in calls["name"].to_list():  # (this turn's calls are in the conversation already)
                earlier[name] = max(0, earlier.get(name, 0) - 1)
        known = {t.strip() for t in (s.get("tools") or "").split(",") if t.strip()}
        rules = s.get("rules") or DEFAULT_RULES
        allowed, denied, count = [], [], dict(earlier)
        total = sum(earlier.values())
        for r in calls.iter_rows(named=True):
            why = None
            if known and r["name"] not in known:
                why = f"there is no tool {r['name']!r}"
            if why is None:
                for rule in rules:
                    if (rule.get("tool") or "*") not in ("*", r["name"]):
                        continue
                    pat = (rule.get("pattern") or "").strip()
                    if pat and not re.search(pat, r["args"] or ""):
                        continue
                    if rule.get("action") == "Deny":
                        why = rule.get("reason") or "denied by the policy"
                    break
            key = (r["name"], json.dumps(json.loads(r["args"] or "{}"), sort_keys=True))
            if why is None and s.get("repeats", True) and key in seen:
                why = (f"you made this exact call already (turn {seen[key]}): its result is in the conversation - use it, "
                       "or call something else")
            if why is None and s.get("per_tool") and count.get(r["name"], 0) >= int(s["per_tool"]):
                why = f"{r['name']} was called {count[r['name']]} times already (the limit)"
            if why is None and s.get("total") and total >= int(s["total"]):
                why = f"{total} tool calls already (the limit): answer with what you have"
            if why is None:
                seen.setdefault(key, r["turn"])
                allowed.append(r)
                count[r["name"]] = count.get(r["name"], 0) + 1
                total += 1
            else:
                denied.append({"role": "tool", "content": f"Denied: {why}.", "tool_calls": None, "tool_call_id": r["call_id"],
                               "name": r["name"], "turn": r["turn"], "tokens_in": None, "tokens_out": None, "ok": False, "seconds": 0.0})
                ctx.log(f"denied {r['name']}: {why}")
        return [pl.DataFrame(allowed, schema=CALLS), pl.DataFrame(denied, schema=RESULTS)]


# ---------------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------------
class ToolExecutor(NodeModel):
    factory = "verknode.harness.ToolExecutor"
    name = "Tool Executor"
    category = CAT
    icon = "🔧"
    sandbox_unsafe = True  # (runs code, workflows, HTTP)
    in_ports = [Port(TABLE, "Tool calls", "call_id, name, args (allowed by Tool Policy)"),
                Port(TABLE, "Tools", "name, kind (sql, python, workflow, http_get), implementation"),
                Port(TABLE, "Table 1", "Data for sql tools (optional)", optional=True),
                Port(TABLE, "Table 2", "(optional)", optional=True)]
    out_ports = [Port(TABLE, "Results", "A tool message per call: content, ok, seconds")]
    short_description = "Runs tool calls: SQL over its tables, sandboxed Python, verknode workflows, HTTP GET."
    description = (
        "Each call runs by the kind of its tool in the tools table:<br>"
        "<b>sql</b>: the argument <code>query</code>, polars SQL over the input tables (named like the nodes they "
        "come from, or table1, table2);<br>"
        "<b>python</b>: the implementation is Python that defines <code>run(**args)</code> (or, empty, the argument "
        "<code>code</code> is run as it is); it runs in a separate process in an empty folder with time and memory "
        "limits; what it returns or prints is the result;<br>"
        "<b>workflow</b>: the implementation is a .vkf file; the arguments are its workflow variables; the result "
        "is its result table;<br>"
        "<b>http_get</b>: the implementation is a URL with {arg} placeholders; the result is the answer's text.<br>"
        "Errors are results too (the model reads them and can try again). Results longer than Result at most are cut."
    )
    fields = [
        F("max_chars", "int", "Result at most (characters)", 4000),
        F("timeout", "int", "Time per call (s)", 30),
        F("memory_mb", "int", "Python memory (MB)", 512),
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(RESULTS)]

    def _names(self, ctx, n) -> list[str]:
        from .data_chat import _slug

        out = [f"table{i + 1}" for i in range(n)]
        try:
            sw = ctx.script_workflow()
            wf = sw._wf
            for port, c in wf.in_conns(sw._me).items():
                if 3 <= port < 3 + n:
                    nc = wf.nodes[c.source_id]
                    out[port - 3] = _slug(nc.annotation if nc.annotation and nc.annotation != f"Node {nc.id}" else nc.name)
        except Exception:  # noqa: BLE001
            pass
        return out

    def run_sql(self, query, frames) -> str:
        from .data_chat import markdown

        out = pl.SQLContext(frames).execute(str(query or ""), eager=True)
        return f"{out.height} rows:\n" + markdown(out, 50, 30)

    def run_python(self, impl, args, s) -> tuple[bool, str]:
        from .coderunner import run

        if (impl or "").strip():
            code = impl + "\n\nimport json as _j\n_r = run(**" + repr(args) + ")\nif _r is not None:\n    print(_r if isinstance(_r, str) else _j.dumps(_r, default=str))\n"
        else:
            code = str(args.get("code") or "")
        r = run(code, "", "", float(s.get("timeout") or 30), int(s.get("memory_mb") or 512), int(s.get("max_chars") or 4000))
        return r["Passed"], r["Output"] if r["Passed"] else f"Error: {r['Error']}\n{r['Output'][-1500:]}"

    def run_workflow(self, impl, args, ctx) -> str:
        from jupyterlab_verknode.standalone import Workflow

        from .data_chat import markdown

        path = ctx.resolve_path(impl)
        if not os.path.isfile(path):
            raise InvalidSettingsError(f"There is no workflow {path}")
        wf = Workflow.load(path)
        known = wf.variables
        unknown = [k for k in args if k not in known]
        if unknown:
            return f"Error: the workflow has no variable(s) {', '.join(unknown)}; it has {', '.join(known) or 'none'}"
        wf.set_variables({**known, **args})
        results = wf.results()
        wf.run(*results.values(), rerun=True)
        return "\n\n".join(f"{label}: {n.table().height} rows\n{markdown(n.table(), 30, 20)}" for label, n in results.items())

    def run_http(self, impl, args, ctx) -> str:
        from ._http import request

        url = re.sub(r"\{(\w+)\}", lambda m: urllib.parse.quote(str(args.get(m.group(1), "")), safe=""), impl)
        r = request("GET", url, timeout=20, ctx=ctx, check=False)
        return f"HTTP {r.status}\n{r.text}"

    def execute(self, inputs, s, ctx):
        calls, tools = inputs[0], inputs[1]
        if ctx.dry:
            return [pl.DataFrame(schema=RESULTS)]
        for c in ("name", "kind"):
            if c not in tools.columns:
                raise InvalidSettingsError(f"The tools table needs the columns name, kind, implementation: no {c}")
        by = {r["name"]: r for r in tools.iter_rows(named=True)}
        data = [x for x in inputs[2:4] if isinstance(x, pl.DataFrame)]
        names = self._names(ctx, len(inputs[2:4]))
        frames = {n: df for n, df in zip(names, inputs[2:4]) if isinstance(df, pl.DataFrame)}
        limit = int(s.get("max_chars") or 4000)
        rows = []
        for r in calls.iter_rows(named=True):
            ctx.check_canceled()
            t0, ok = time.time(), True
            tool = by.get(r["name"])
            try:
                args = json.loads(r["args"] or "{}")
            except ValueError:
                args = {}
            try:
                if tool is None:
                    ok, text = False, f"Error: there is no tool {r['name']!r}"
                elif tool["kind"] == "sql":
                    text = self.run_sql(args.get("query"), frames) if data else "Error: no tables to query"
                elif tool["kind"] == "python":
                    ok, text = self.run_python(tool.get("implementation"), args, s)
                elif tool["kind"] == "workflow":
                    text = self.run_workflow(tool.get("implementation") or "", args, ctx)
                elif tool["kind"] == "http_get":
                    text = self.run_http(tool.get("implementation") or "", args, ctx)
                else:
                    ok, text = False, f"Error: tool kind {tool['kind']!r} is not one of {', '.join(KINDS)}"
            except Exception as e:  # noqa: BLE001 - the model reads the error
                ok, text = False, f"Error: {e}"
            if text.startswith("Error"):
                ok = False
            if len(text) > limit:
                text = text[:limit] + f"\n… ({len(text) - limit} more characters cut)"
            rows.append({"role": "tool", "content": text, "tool_calls": None, "tool_call_id": r["call_id"], "name": r["name"],
                         "turn": r["turn"], "tokens_in": None, "tokens_out": None, "ok": ok, "seconds": round(time.time() - t0, 3)})
            ctx.log(f"{r['name']}: {'ok' if ok else 'error'} in {time.time() - t0:.1f} s")
        return [pl.DataFrame(rows, schema=RESULTS)]


# ---------------------------------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------------------------------
STRATEGIES = ["Shorten old tool results", "Drop the oldest turns", "Summarize the oldest turns (LLM)"]


class ContextWindow(NodeModel):
    factory = "verknode.harness.ContextWindow"
    name = "Context Window"
    category = CAT
    icon = "⧖"
    in_ports = [Port(TABLE, "Conversation"), llm_port("For summaries (optional: the server settings)")]
    out_ports = [Port(TABLE, "Conversation", "Within the token budget")]
    short_description = "Keeps a conversation within a token budget: shortens old tool results, drops or summarizes old turns."
    description = (
        "Tokens are estimated (4 characters a token). Over the budget: <b>Shorten old tool results</b> cuts the tool "
        "results of all but the last turns to a few hundred characters (what models rarely read again); <b>Drop the "
        "oldest turns</b> also leaves out whole old turns; <b>Summarize the oldest turns</b> replaces them by a "
        "summary the model writes. The system message and the first user message always stay, a tool call and "
        "its results stay together, and the last turns are kept as they are. Flow variables: context_tokens (after), "
        "context_trimmed (messages changed or dropped)."
    )
    fields = [
        F("budget", "int", "Token budget", 6000),
        F("keep_turns", "int", "Last turns kept as they are", 2),
        F("strategy", "enum", "Over budget", STRATEGIES[0], options=STRATEGIES),
        F("short", "int", "Shortened tool results (characters)", 300),
        *server_fields()[:3],
    ]

    def configure(self, in_specs, s, ctx):
        return [pl.Schema(CONV)]

    def execute(self, inputs, s, ctx):
        conv = conversation(inputs[0])
        rows = conv.to_dicts()
        size = lambda rs: sum(estimate_tokens((r["content"] or "") + (r["tool_calls"] or "")) for r in rs)  # noqa: E731
        budget, keep = int(s.get("budget") or 6000), max(0, int(s.get("keep_turns") or 2))
        before, trimmed = size(rows), 0
        last_turn = max([r["turn"] or 0 for r in rows] or [0])
        old = lambda r: (r["turn"] or 0) and (r["turn"] or 0) <= last_turn - keep  # noqa: E731
        if before > budget:
            short = int(s.get("short") or 300)
            for r in rows:
                if r["role"] == "tool" and old(r) and len(r["content"] or "") > short:
                    r["content"] = r["content"][:short] + " … (shortened)"
                    trimmed += 1
        strategy = s.get("strategy") or STRATEGIES[0]
        if size(rows) > budget and strategy != STRATEGIES[0]:
            fixed = [i for i, r in enumerate(rows) if r["role"] == "system"]
            first_user = next((i for i, r in enumerate(rows) if r["role"] == "user"), None)
            keep_i = set(fixed + ([first_user] if first_user is not None else []))
            turns = sorted({r["turn"] for r in rows if r["turn"] and old(r)})
            dropped: list[dict] = []
            for t in turns:
                if size(rows) - size(dropped) <= budget:
                    break
                dropped += [r for i, r in enumerate(rows) if r["turn"] == t and i not in keep_i]
            if dropped:
                ids = {id(r) for r in dropped}
                rest = [r for r in rows if id(r) not in ids]
                trimmed += len(dropped)
                if strategy == STRATEGIES[2] and not ctx.dry:
                    text = "\n".join(f"{r['role']}{' ' + r['name'] if r['name'] else ''}: {(r['content'] or r['tool_calls'] or '')[:800]}"
                                     for r in dropped)
                    srv = server_for(s, inputs)
                    summ = chat(srv, [{"role": "system", "content": "Summarize this part of a conversation between a user, "
                                       "an assistant and its tools in a few bullets: what was asked, found and decided, "
                                       "with the numbers that matter."}, {"role": "user", "content": text}],
                                {"temperature": 0, "max_tokens": 500})["text"].strip()
                    at = (first_user + 1) if first_user is not None else len(fixed)
                    rest.insert(min(at, len(rest)), {"role": "user", "content": f"(Summary of earlier turns)\n{summ}", "tool_calls": None,
                                                     "tool_call_id": None, "name": None, "turn": dropped[0]["turn"],
                                                     "tokens_in": None, "tokens_out": None})
                rows = rest
        after = size(rows)
        ctx.push_variable("context_tokens", after, "INTEGER")
        ctx.push_variable("context_trimmed", trimmed, "INTEGER")
        if trimmed:
            ctx.log(f"context: {before} -> {after} tokens ({trimmed} messages shortened or dropped)")
        return [pl.DataFrame(rows, schema=CONV) if rows else pl.DataFrame(schema=CONV)]
