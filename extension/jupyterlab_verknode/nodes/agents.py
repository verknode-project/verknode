"""Agent nodes: data query agents built in the workflow - with verknode's own tool loop, with a
LangGraph graph, or as a Deep Agent (deepagents: planning, a virtual file system, subagents) - and
Agent Runner, which asks an agent a table of questions.

An agent is a port object (AgentPortObject, teal square port): its model connection, instructions
(## sections of a system prompt), examples, tools and data tables. The builder nodes make it; Agent
Runner runs it; the evaluation nodes (nodes/evaluation.py) score it, compare models on it, change it
and study what its parts are worth. All of them run an agent with run_agent().

The tools are read-only over the agent's tables: list_tables, describe, rows, sql (polars SQL).
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import polars as pl

from verknode.node import AGENT, F, AgentPortObject, InvalidSettingsError, LLMConnectionPortObject, NodeModel, Port, TABLE
from jupyterlab_verknode.engine.node import CanceledExecutionError

from .data_chat import TOOLS as TABLE_TOOLS
from .data_chat import Tables, _slug
from .llm import chat_tools, connection_of, llm_port, server, server_fields, tool_message

CAT = "AI/Agents"
TOOL_NAMES = [t["name"] for t in TABLE_TOOLS]
FRAMEWORKS = {"builtin": "verknode tool loop", "langgraph": "LangGraph", "deepagents": "Deep Agents"}

INSTRUCTIONS = """## Role
You answer questions about the data in the tables you can query, as a careful data analyst.

## Method
Look at the tables first (list_tables, describe), then compute every number you give with sql - polars SQL \
over the tables by their names. Filter on values exactly as list_tables spells them.

## Rules
Never do arithmetic in your head and never guess: every number in your answer comes from a query for this \
question. If the data cannot answer the question, say so.

## Answer format
The answer first, in one or two sentences with its numbers (rounded to 2 decimals); no preamble."""

ANSWER = {"question": pl.String, "answer": pl.String, "steps": pl.Int64, "tool_calls": pl.Int64, "sql": pl.String,
          "seconds": pl.Float64, "tokens": pl.Int64, "error": pl.String}


# ---------------------------------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------------------------------
def sections(text: str) -> list[tuple[str, str]]:
    """The ## sections of instructions: [(heading, text)] (text before the first: heading "")."""
    out, head, buf = [], "", []
    for line in (text or "").splitlines():
        m = re.match(r"^#{1,3}\s+(.+)", line)
        if m:
            if head or "".join(buf).strip():
                out.append((head, "\n".join(buf).strip()))
            head, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if head or "".join(buf).strip():
        out.append((head, "\n".join(buf).strip()))
    return out


def join_sections(parts: list[tuple[str, str]]) -> str:
    return "\n\n".join((f"## {h}\n{t}" if h else t) for h, t in parts).strip()


def overview(tables: dict) -> str:
    return "\n".join(f"- {n}: {df.height} rows; columns " + ", ".join(f"{c} ({t})" for c, t in list(df.schema.items())[:30])
                     for n, df in tables.items())


def system_prompt(agent: AgentPortObject) -> str:
    text = agent.instructions.strip()
    if agent.examples.strip():
        text += "\n\n## Examples\n" + agent.examples.strip()
    if agent.settings.get("overview", True) and agent.tables:
        text += "\n\n## The tables\n" + overview(agent.tables)
    return text


def _tables(agent: AgentPortObject) -> Tables:
    return Tables({n: {"df": df, "source": f"table {n}", "node": None} for n, df in agent.tables.items()})


def _settings(agent: AgentPortObject) -> dict:
    st = agent.settings
    return {"temperature": st.get("temperature", 0.0), "max_tokens": st.get("max_tokens", 1500),
            "timeout": st.get("timeout", 300), "seed": st.get("seed", 0), "think": st.get("think", False)}


def agent_server(agent: AgentPortObject) -> dict:
    if agent.connection is None:
        return server({})
    return agent.connection.server()


class _Tracked:
    """The tools of one question, with what was called (for the answer table and the evaluations)."""

    def __init__(self, agent: AgentPortObject, ctx):
        self.tables, self.ctx = _tables(agent), ctx
        self.allowed = set(agent.tools)
        self.calls: list[dict] = []

    def call(self, name, args) -> str:
        if self.ctx is not None:
            self.ctx.check_canceled()
        if name not in self.allowed:
            return f"Error: there is no tool {name!r}"
        out = self.tables.call(name, args)
        self.calls.append({"name": name, "args": args, "result": out[:600]})
        return out

    @property
    def sql(self) -> list[str]:
        return [c["args"].get("query", "") for c in self.calls if c["name"] == "sql" and not c["result"].startswith("Error")]

    def queried_since(self, n: int) -> bool:
        return any(c["name"] == "sql" for c in self.calls[n:])


def _tool_specs(agent):
    return [t for t in TABLE_TOOLS if t["name"] in agent.tools]


NUDGE = "Compute the numbers of your answer with sql (for this question), then answer again."


# ---------------------------------------------------------------------------------------------------
# the frameworks
# ---------------------------------------------------------------------------------------------------
def _run_builtin(agent, question, ctx, tracked) -> dict:
    srv, s = agent_server(agent), _settings(agent)
    messages = [{"role": "system", "content": system_prompt(agent)}, {"role": "user", "content": question}]
    steps, nudges, tokens = max(2, int(agent.settings.get("steps") or 12)), 0, 0
    for step in range(steps):
        if ctx is not None:
            ctx.check_canceled()
        r = chat_tools(srv, messages, s, _tool_specs(agent))
        messages.append(r["message"])
        tokens += int(r.get("tokens_out") or 0)
        if r["calls"]:
            for c in r["calls"]:
                messages.append(tool_message(srv, c, tracked.call(c["name"], c["args"])))
            continue
        text = (r["text"] or "").strip()
        if agent.settings.get("verify", True) and text and re.search(r"\d", text) and not tracked.sql and nudges < 2:
            nudges += 1
            messages.append({"role": "user", "content": NUDGE})
            continue
        return {"answer": text, "steps": step + 1, "tokens": tokens}
    return {"answer": "", "steps": steps, "tokens": tokens, "error": "no answer within the steps"}


def _lc_parts(agent, tracked):
    from ._lc import VerknodeChatModel, make_tool

    model = VerknodeChatModel(srv=agent_server(agent), settings=_settings(agent), usage={})
    tools = [make_tool(t["name"], t["description"], t["parameters"], tracked.call) for t in _tool_specs(agent)]
    return model, tools


def build_langgraph(agent, tracked):
    """The agent's LangGraph: START -> agent -> tools -> agent ... -> verify -> END (verify sends it back
    to the agent once more when its answer has numbers that no query of this question computed)."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langgraph.graph import END, START, MessagesState, StateGraph
        from langgraph.prebuilt import ToolNode
    except ImportError:
        raise InvalidSettingsError("LangGraph agents need langgraph (pip install langgraph)") from None

    model, tools = _lc_parts(agent, tracked)
    bound = model.bind_tools(tools) if tools else model
    system = SystemMessage(system_prompt(agent))
    verify = agent.settings.get("verify", True)

    class State(MessagesState):
        nudges: int

    def call_model(state):
        return {"messages": [bound.invoke([system] + state["messages"])]}

    def after_model(state):
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return "verify" if verify else END

    def check(state):
        last = state["messages"][-1]
        text = str(last.content or "")
        if re.search(r"\d", text) and not tracked.sql and int(state.get("nudges") or 0) < 2:
            return {"messages": [HumanMessage(NUDGE)], "nudges": int(state.get("nudges") or 0) + 1}
        return {}

    def after_check(state):
        return "agent" if state["messages"][-1].type == "human" else END

    g = StateGraph(State)
    g.add_node("agent", call_model)
    g.add_edge(START, "agent")
    if tools:
        g.add_node("tools", ToolNode(tools))
        g.add_edge("tools", "agent")
    targets = (["tools"] if tools else []) + (["verify"] if verify else [END])
    g.add_conditional_edges("agent", after_model, targets)
    if verify:
        g.add_node("verify", check)
        g.add_conditional_edges("verify", after_check, ["agent", END])
    return g.compile(), model


def build_deepagent(agent, tracked):
    """A Deep Agent (deepagents) with the table tools - planning (write_todos), a virtual file system,
    and a sql-analyst subagent if wanted."""
    try:
        from deepagents import create_deep_agent
    except ImportError:
        raise InvalidSettingsError("Deep Agents need deepagents (pip install deepagents)") from None

    model, tools = _lc_parts(agent, tracked)
    subagents = None
    if agent.settings.get("subagent", True) and tools:
        subagents = [{"name": "sql-analyst", "description": "Computes one number or small table from the data with SQL. "
                      "Give it one precise request (what to compute, from which table, filters).",
                      "system_prompt": "You compute what you are asked with the sql tool over the tables (list_tables "
                      "shows them) and reply with the result table and the query. Never guess numbers.",
                      "tools": tools, "model": model}]
    graph = create_deep_agent(model=model, tools=tools, system_prompt=system_prompt(agent), subagents=subagents,
                              name=agent.name or "agent")
    return graph, model


def _run_graph(agent, question, ctx, tracked, deep: bool) -> dict:
    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        raise InvalidSettingsError("This agent needs langchain-core (pip install langgraph)") from None

    from ._lc import message_log

    graph, model = (build_deepagent if deep else build_langgraph)(agent, tracked)
    steps = max(2, int(agent.settings.get("steps") or 12))
    state = graph.invoke({"messages": [HumanMessage(question)]}, {"recursion_limit": steps * 3 + 10})
    msgs = state.get("messages") or []
    final = next((m for m in reversed(msgs) if m.type == "ai" and not getattr(m, "tool_calls", None)), None)
    out = {"answer": str(final.content or "").strip() if final else "", "steps": model.usage.get("calls", 0),
           "tokens": model.usage.get("tokens_out", 0), "log": message_log(msgs)}
    if deep:
        out["todos"] = state.get("todos") or []
        out["files"] = sorted((state.get("files") or {}).keys())
    return out


def run_agent(agent: AgentPortObject, question: str, ctx=None) -> dict:
    """One question: {answer, steps, tool_calls, sql, seconds, tokens, error, calls, ...}."""
    tracked = _Tracked(agent, ctx)
    t0 = time.time()
    try:
        if agent.framework == "langgraph":
            out = _run_graph(agent, question, ctx, tracked, deep=False)
        elif agent.framework == "deepagents":
            out = _run_graph(agent, question, ctx, tracked, deep=True)
        else:
            out = _run_builtin(agent, question, ctx, tracked)
    except CanceledExecutionError:
        raise
    except InvalidSettingsError:
        raise
    except Exception as e:  # noqa: BLE001 - one question's failure is a row
        out = {"answer": "", "steps": 0, "tokens": 0, "error": f"{type(e).__name__}: {e}"[:500]}
    return {"question": question, "answer": out.get("answer") or "", "steps": out.get("steps"),
            "tool_calls": len(tracked.calls), "sql": json.dumps(tracked.sql), "seconds": round(time.time() - t0, 2),
            "tokens": out.get("tokens"), "error": out.get("error"), "calls": tracked.calls,
            **{k: out[k] for k in ("log", "todos", "files") if k in out}}


def run_many(agent, questions: list[str], ctx=None, workers: int = 1) -> list[dict]:
    """run_agent for each question (workers at a time), in their order."""
    if workers <= 1 or len(questions) <= 1:
        return [run_agent(agent, q, ctx) for q in questions]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda q: run_agent(agent, q, ctx), questions))


def graph_mermaid(agent: AgentPortObject) -> str:
    """The agent's graph as Mermaid (LangGraph's drawing; the built-in loop drawn the same way)."""
    try:
        if agent.framework == "langgraph":
            return build_langgraph(agent, _Tracked(agent, None))[0].get_graph().draw_mermaid()
        if agent.framework == "deepagents":
            return build_deepagent(agent, _Tracked(agent, None))[0].get_graph().draw_mermaid()
    except Exception as e:  # noqa: BLE001 - only a drawing
        return f"flowchart LR\n  e[\"(no drawing: {str(e)[:80]})\"]"
    verify = agent.settings.get("verify", True)
    return ("flowchart TD\n  start([__start__]) --> model[model with tools]\n  model -- tool calls --> tools[" +
            ", ".join(agent.tools) + "]\n  tools --> model\n" +
            ("  model -- answer --> verify{numbers from sql?}\n  verify -- no --> model\n  verify -- yes --> stop([__end__])\n"
             if verify else "  model -- answer --> stop([__end__])\n"))


# ---------------------------------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------------------------------
DATA_IN = [Port(TABLE, f"Table {i}", "Data the agent can query (optional)" if i == 1 else "(optional)", optional=True)
           for i in range(1, 5)]


def agent_fields(default_name="Data query agent"):
    return [
        F("agent_name", "string", "Name", default_name),
        F("instructions", "code", "Instructions", INSTRUCTIONS,
          "The system prompt, in ## sections (Ablation Study removes them one by one; Hill Climbing rewrites them)."),
        F("examples", "code", "Examples", "", "Questions with good answers, for the model to follow (optional)."),
        F("tools", "multienum", "Tools", TOOL_NAMES, options=TOOL_NAMES),
        F("overview", "bool", "Tell it the tables and columns", True, "A list of the tables in the system prompt."),
        F("verify", "bool", "Numbers must come from SQL", True,
          "An answer with numbers but no query for the question is sent back once or twice."),
        F("steps", "int", "Model calls per question at most", 12),
        F("temperature", "float", "Temperature", 0.0),
        F("max_tokens", "int", "Longest answer (tokens)", 1500),
    ]


class _AgentBuilder(NodeModel):
    """Makes an AgentPortObject of its settings, tables and LLM connection. (Name starts with "_".)"""

    category = CAT
    node_type = "Learner"
    framework = "builtin"
    in_ports = [*DATA_IN, llm_port("The model (an LLM connector node); none: the server settings below")]
    out_ports = [Port(AGENT, "Agent", "The agent: for Agent Runner and the evaluation nodes"),
                 Port(TABLE, "Graph", "One row: the agent's graph as Mermaid (for Mermaid Display)")]
    extra_fields: list = []

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if "fields" not in cls.__dict__:
            cls.fields = [*agent_fields(), *cls.extra_fields, *server_fields()[:4]]

    def _names(self, ctx, n) -> list[str]:
        out = [f"table{i + 1}" for i in range(n)]
        try:
            sw = ctx.script_workflow()
            wf = sw._wf
            for port, c in wf.in_conns(sw._me).items():
                if 1 <= port <= n:
                    nc = wf.nodes[c.source_id]
                    label = nc.annotation if nc.annotation and nc.annotation != f"Node {nc.id}" else nc.name
                    out[port - 1] = _slug(label)
        except Exception:  # noqa: BLE001 - names are a nicety
            pass
        return out

    def make(self, inputs, s, ctx) -> AgentPortObject:
        frames = list(inputs[:4]) if inputs else []
        names = self._names(ctx, len(frames))
        tables = {}
        for name, df in zip(names, frames):
            if isinstance(df, pl.DataFrame):
                tables[name if name not in tables else f"{name}_{len(tables) + 1}"] = df
        conn = connection_of(inputs)
        if conn is None:
            srv = server(s)
            url = (s.get("url") or "").strip() or "${VERKNODE_LLM_URL}"  # (resolved when used: not saved)
            conn = LLMConnectionPortObject(srv["provider"], url, srv["model"],
                                           (s.get("api_key_env") or "").strip(), f"{srv['provider']} · {srv['model']}")
        tools = [t for t in (s.get("tools") or TOOL_NAMES) if t in TOOL_NAMES]
        settings = {k: s.get(k) for k in ("overview", "verify", "steps", "temperature", "max_tokens") if k in s}
        settings.update({f.key: s.get(f.key) for f in self.extra_fields})
        return AgentPortObject(self.framework, conn, s.get("instructions") or "", tools, tables, s.get("examples") or "",
                               settings, (s.get("agent_name") or "agent").strip())

    def configure(self, in_specs, s, ctx):
        spec = AgentPortObject(self.framework, None, s.get("instructions") or "", s.get("tools") or TOOL_NAMES, {},
                               s.get("examples") or "", {}, (s.get("agent_name") or "agent").strip())
        return [spec, pl.Schema({"mermaid": pl.String, "framework": pl.String})]

    def execute(self, inputs, s, ctx):
        agent = self.make(inputs, s, ctx)
        if not agent.tables and not ctx.dry:
            ctx.warn("The agent has no tables: connect the data it should answer from")
        agent.graph = graph_mermaid(agent) if not ctx.dry else ""
        return [agent, pl.DataFrame({"mermaid": [agent.graph], "framework": [FRAMEWORKS[self.framework]]})]


class DataQueryAgent(_AgentBuilder):
    factory = "verknode.agents.DataQueryAgent"
    name = "Data Query Agent"
    icon = "🤖SQL"
    framework = "builtin"
    short_description = "An agent that answers questions about its tables with SQL (verknode's tool loop)."
    description = (
        "Makes an agent from its instructions, tools and the tables connected to it: it answers questions by "
        "looking at the tables and computing with SQL (polars), in verknode's own tool loop - no framework to "
        "install. Its model comes from the LLM port (an LLM connector) or the server settings. Ask it questions "
        "with Agent Runner; score it with Agent Evaluation; improve it with Hill Climbing; compare models with "
        "Multi-Arm Evaluation; see what its parts are worth with Ablation Study. LangGraph Agent and Deep Agent "
        "make the same agent with those frameworks."
    )


class LangGraphAgent(_AgentBuilder):
    factory = "verknode.agents.LangGraphAgent"
    name = "LangGraph Agent"
    icon = "LG"
    framework = "langgraph"
    requires = ["langgraph"]
    short_description = "A data query agent as a LangGraph graph: model, tools and a verify step."
    description = (
        "The data query agent built as a LangGraph StateGraph: an agent node (the model with the table tools), a "
        "ToolNode, and - with Numbers must come from SQL - a verify node that sends an answer back when its "
        "numbers were not computed by a query. The second output is the graph LangGraph draws (Mermaid). The "
        "model is any verknode connection (Ollama, inference servers, APIs, Anthropic, llama.cpp), through a "
        "LangChain chat model of verknode's. Needs langgraph (pip install langgraph)."
    )


class DeepAgent(_AgentBuilder):
    factory = "verknode.agents.DeepAgent"
    name = "Deep Agent"
    icon = "DA"
    framework = "deepagents"
    requires = ["deepagents"]
    short_description = "A data query agent made with Deep Agents: planning, a virtual file system, a SQL subagent."
    description = (
        "The data query agent made with deepagents (LangChain's Deep Agents, on LangGraph): besides the table tools "
        "it plans with a todo list, keeps notes in a virtual file system (in its state, not on disk) and can hand "
        "a computation to a sql-analyst subagent. Agent Runner lists its todos and files. Slower than the other "
        "two - meant for questions with several steps. Needs deepagents (pip install deepagents)."
    )
    extra_fields = [F("subagent", "bool", "A sql-analyst subagent", True)]


class AgentRunner(NodeModel):
    factory = "verknode.agents.AgentRunner"
    name = "Agent Runner"
    category = CAT
    icon = "▶🤖"
    node_type = "Predictor"
    in_ports = [Port(AGENT, "Agent", "An agent (Data Query Agent, LangGraph Agent, Deep Agent, Hill Climbing...)"),
                Port(TABLE, "Questions", "A question per row")]
    out_ports = [Port(TABLE, "Answers", "The questions with the answers, the SQL the agent ran, steps, time")]
    short_description = "Asks an agent a table of questions: its answers, the SQL it ran, steps and time."
    description = (
        "Runs the agent on each question (a column of the input): the answer, the number of model calls and tool "
        "calls, the SQL queries it ran (JSON), seconds, tokens and the error if any - and, if wanted, its trace "
        "(the messages, for LangGraph and Deep Agents also their todos and files). Several questions at a time "
        "with Parallel questions (if the model server takes parallel requests)."
    )
    fields = [
        F("column", "column", "Question column", None, "Empty: question, else the first text column.", port=2, kinds=["string"]),
        F("workers", "int", "Parallel questions", 1),
        F("trace", "bool", "Add the trace (JSON)", False),
    ]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[1] if len(in_specs) > 1 else None
        out = dict(ANSWER)
        if s.get("trace"):
            out["trace"] = pl.String
        if spec is not None:
            out = {**{c: t for c, t in spec.items() if c not in out}, **out}
        return [pl.Schema(out)]

    def execute(self, inputs, s, ctx):
        agent, df = inputs[0], inputs[1]
        col = s.get("column") or ("question" if "question" in df.columns else next(
            (c for c, t in df.schema.items() if t == pl.String), None))
        if not col or col not in df.columns:
            raise InvalidSettingsError("Pick the question column")
        if ctx.dry:
            return [pl.DataFrame(schema=self.configure([None, df.schema], s, ctx)[0])]
        questions = [str(q or "") for q in df[col].to_list()]
        done = [0]

        def one(q):
            r = run_agent(agent, q, ctx)
            done[0] += 1
            ctx.set_progress(done[0] / max(1, len(questions)))
            return r

        workers = max(1, int(s.get("workers") or 1))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(one, questions))
        else:
            results = [one(q) for q in questions]
        rows = []
        for r in results:
            row = {k: r.get(k) for k in ANSWER}
            if s.get("trace"):
                row["trace"] = json.dumps({k: r[k] for k in ("calls", "log", "todos", "files") if k in r}, default=str)
            rows.append(row)
        out = pl.DataFrame(rows, schema=self.configure([None, None], s, ctx)[0], strict=False)
        rest = df.drop([c for c in df.columns if c in out.columns])
        ctx.log(f"{agent.name}: {len(rows)} question(s), {sum(1 for r in results if r.get('error'))} error(s)")
        return [pl.concat([rest, out], how="horizontal")]
