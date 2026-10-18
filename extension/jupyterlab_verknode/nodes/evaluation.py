"""Evaluation nodes: score LLM answers and agents with a rubric, and improve agents by measurement.

- Rubric Judge: any table of answers scored against a rubric - checks of the answer against a
  reference answer, and criteria an LLM judge scores 1-5 - into one weighted score per row.
- Agent Evaluation: an agent run on an evaluation set (questions with reference answers), scored.
- Hill Climbing: an agent changed step by step - a proposer model rewrites its instructions and
  examples from the failures, a candidate is kept only if it scores higher (optionally on held-out
  questions) - out: the best agent and the history.
- Multi-Arm Evaluation: models (arms) compared on the agent - all on every question, or as a
  multi-armed bandit (UCB1, Thompson sampling) that spends the budget on the promising ones.
- Ablation Study: the agent without each of its parts - instruction sections, examples, tools,
  the table overview, the SQL check - and what each removal costs.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

import polars as pl

from verknode.node import AGENT, LLM, F, AgentPortObject, InvalidSettingsError, LLMConnectionPortObject, NodeModel, Port, TABLE
from jupyterlab_verknode.engine.node import CanceledExecutionError

from .agents import join_sections, run_agent, sections
from .llm import _json_answer, chat, server

CAT = "AI/Evaluation"
KINDS = ["LLM judge (1-5)", "Numbers match the reference", "Contains the reference", "Not empty"]
DEFAULT_RUBRIC = [
    {"criterion": "correct", "kind": "Numbers match the reference", "weight": 3.0,
     "description": "The numbers of the reference answer are in the answer (within 0.5%)."},
    {"criterion": "grounded", "kind": "LLM judge (1-5)", "weight": 2.0,
     "description": "Every number and claim is supported by the SQL results shown; nothing is invented."},
    {"criterion": "complete", "kind": "LLM judge (1-5)", "weight": 1.0,
     "description": "It answers every part of the question."},
    {"criterion": "concise", "kind": "LLM judge (1-5)", "weight": 1.0,
     "description": "The answer comes first, short and direct, without filler."},
]
NUM = re.compile(r"-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")


def rubric_field():
    return F("rubric", "records", "Rubric", DEFAULT_RUBRIC, "What a good answer is: criteria, how each is scored and "
             "its weight. Reference checks need a reference column; LLM judge criteria a judge model.", fields=[
                 F("criterion", "string", "Criterion", ""), F("kind", "enum", "Scored by", KINDS[0], options=KINDS),
                 F("weight", "float", "Weight", 1.0), F("description", "string", "What it means", "")])


def judge_fields():
    return [rubric_field(),
            F("tolerance", "float", "Numbers: relative tolerance", 0.005, "0.005: within 0.5%."),
            F("judge_notes", "code", "Judge instructions", "", "More for the judge (e.g. what counts as grounded)."),
            F("workers", "int", "Parallel runs", 2, "Questions run (and judged) at a time.")]


# ---------------------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------------------
def _numbers(text: str) -> list[float]:
    out = []
    for m in NUM.finditer(text or ""):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def numbers_match(answer: str, reference: str, tol: float) -> float:
    """The share of the reference's numbers found in the answer (a percentage also as a fraction)."""
    want = _numbers(reference)
    if not want:
        return 1.0
    got = _numbers(answer)

    def near(a, b):  # (rounded to 2 decimals passes too)
        return abs(a - b) <= abs(b) * tol + 0.006

    hit = sum(1 for w in want if any(near(g, w) or near(g * 100, w) or near(g / 100, w) for g in got))
    return hit / len(want)


def _criteria(s) -> list[dict]:
    rows = [r for r in (s.get("rubric") or DEFAULT_RUBRIC) if (r.get("criterion") or "").strip()]
    if not rows:
        raise InvalidSettingsError("The rubric is empty")
    for r in rows:
        r["key"] = re.sub(r"\W+", "_", r["criterion"].strip().lower()).strip("_") or "criterion"
    return rows


JUDGE = """You grade an answer against a rubric, strictly and consistently. First write feedback, two or three \
sentences: compare the answer with the reference answer (if any) and with the tool results the answering agent \
saw - is each number right and computed by a query, is anything missing, what went wrong (e.g. a wrong \
calculation in the SQL)? Then give each criterion an integer score from 1 (fails it) to 5 (fully meets it). \
Judge only what is shown."""


def judge(srv: dict, criteria: list[dict], question: str, answer: str, reference: str | None, evidence: str,
          notes: str = "") -> dict:
    """LLM judge scores (1-5) of the LLM criteria, and feedback."""
    llm = [c for c in criteria if c["kind"] == KINDS[0]]
    if not llm:
        return {}
    schema = {"type": "object", "properties": {"feedback": {"type": "string"},
                                               **{c["key"]: {"type": "integer", "minimum": 1, "maximum": 5} for c in llm}},
              "required": ["feedback"] + [c["key"] for c in llm]}
    rubric = "\n".join(f"- {c['key']}: {c.get('description') or c['criterion']}" for c in llm)
    prompt = (f"Rubric:\n{rubric}\n\nQuestion: {question}\n\n" + (f"Reference answer: {reference}\n\n" if reference else "")
              + f"Answer: {answer or '(no answer)'}\n\nTool results the agent saw:\n{evidence or '(none)'}\n\n"
              + "Reply with JSON: feedback first, then the score of each criterion.")
    msgs = [{"role": "system", "content": JUDGE + (f"\n\n{notes}" if notes else "")}, {"role": "user", "content": prompt}]
    r = chat(srv, msgs, {"temperature": 0, "max_tokens": 600, "timeout": 300}, schema)
    j = _scores(r["text"], llm)
    if j is None:  # (once more, firmly - some servers do not enforce the schema)
        msgs += [{"role": "assistant", "content": r["text"]},
                 {"role": "user", "content": "Reply with the JSON object only: " + json.dumps(list(schema["properties"]))}]
        r = chat(srv, msgs, {"temperature": 0, "max_tokens": 600, "timeout": 300}, schema)
        j = _scores(r["text"], llm)
    if j is None:
        return {"feedback": f"(the judge's answer had no scores: {r['text'][:200]})"}
    out = {"feedback": str(j.get("feedback") or "")}
    for c in llm:
        try:
            out[c["key"]] = max(1, min(5, int(j.get(c["key"]))))
        except (TypeError, ValueError):
            out[c["key"]] = None
    return out


def _scores(text: str, llm: list[dict]) -> dict | None:
    """The judge's JSON - or, when it wrote text, "key: 4" lines and the text before them as feedback."""
    try:
        j = _json_answer(text)
        if isinstance(j, dict) and any(c["key"] in j for c in llm):
            return j
    except ValueError:
        pass
    found, starts = {}, []
    for c in llm:  # (a score line: "concise: 4", "- **concise**: 4/5")
        m = re.search(rf"(?im)^[\W_]*{re.escape(c['key'])}[\W_]{{0,6}}([1-5])\b", text or "")
        if m:
            found[c["key"]] = int(m.group(1))
            starts.append(m.start())
    if len(found) < len(llm):
        return None
    first = min(starts)
    found["feedback"] = re.sub(r"^\W*feedback\W*", "", text[:first], flags=re.I).strip()
    return found


def score(srv: dict | None, criteria: list[dict], s: dict, question: str, answer: str, reference: str | None,
          evidence: str = "", error: str | None = None) -> dict:
    """{key: 0-1 per criterion, "score": weighted 0-1, "feedback"}."""
    out: dict = {}
    tol = float(s.get("tolerance") or 0.005)
    for c in criteria:
        if c["kind"] == KINDS[1]:
            out[c["key"]] = numbers_match(answer, reference or "", tol) if reference else None
        elif c["kind"] == KINDS[2]:
            out[c["key"]] = (1.0 if (reference or "").strip().lower() in (answer or "").lower() else 0.0) if reference else None
        elif c["kind"] == KINDS[3]:
            out[c["key"]] = 1.0 if (answer or "").strip() else 0.0
    j = {}
    if answer and not error and srv is not None:
        try:
            j = judge(srv, criteria, question, answer, reference, evidence, (s.get("judge_notes") or "").strip())
        except CanceledExecutionError:
            raise
        except Exception as e:  # noqa: BLE001 - a judge failure is feedback
            j = {"feedback": f"(judge failed: {e})"}
    for c in criteria:
        if c["kind"] == KINDS[0]:
            v = j.get(c["key"])
            out[c["key"]] = (v - 1) / 4 if isinstance(v, int) else (0.0 if not answer or error else None)
    weights = [(float(c.get("weight") or 1.0), out[c["key"]]) for c in criteria if out.get(c["key"]) is not None]
    total = sum(w for w, _ in weights)
    out["score"] = round(sum(w * v for w, v in weights) / total, 4) if total else 0.0
    out["feedback"] = j.get("feedback") or (error or "")
    return out


def evidence_of(result: dict) -> str:
    return "\n".join(f"{c['name']}({json.dumps(c['args'])[:300]}) ->\n{c['result'][:500]}" for c in result.get("calls", [])
                     if c["name"] == "sql")[:4000]


# ---------------------------------------------------------------------------------------------------
# an agent on an evaluation set
# ---------------------------------------------------------------------------------------------------
def eval_set(df: pl.DataFrame, s: dict) -> list[dict]:
    q = s.get("question_column") or ("question" if "question" in df.columns else None)
    r = s.get("reference_column") or ("reference" if "reference" in df.columns else None)
    if not q or q not in df.columns:
        raise InvalidSettingsError("Pick the question column of the evaluation set")
    return [{"question": str(row[q] or ""), "reference": None if not r or row.get(r) is None else str(row[r])}
            for row in df.iter_rows(named=True)]


def evaluate(agent: AgentPortObject, items: list[dict], s: dict, judge_srv, ctx, label="") -> dict:
    """Runs and scores the agent on the items: {"rows": [per question], "score", "criteria": {key: mean}, ...}."""
    criteria = _criteria(s)

    def one(it):
        ctx.check_canceled()
        r = run_agent(agent, it["question"], ctx)
        sc = score(judge_srv, criteria, s, it["question"], r["answer"], it["reference"], evidence_of(r), r.get("error"))
        return {"question": it["question"], "reference": it["reference"], "answer": r["answer"], **{c["key"]: sc[c["key"]] for c in criteria},
                "score": sc["score"], "feedback": sc["feedback"], "steps": r["steps"], "tool_calls": r["tool_calls"],
                "sql": r["sql"], "seconds": r["seconds"], "tokens": r["tokens"], "error": r.get("error")}

    workers = max(1, int(s.get("workers") or 1))
    if workers > 1 and len(items) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(one, items))
    else:
        rows = [one(it) for it in items]
    n = max(1, len(rows))
    crit = {}
    for c in criteria:
        vals = [r[c["key"]] for r in rows if r[c["key"]] is not None]
        crit[c["key"]] = round(sum(vals) / len(vals), 4) if vals else None
    out = {"rows": rows, "score": round(sum(r["score"] for r in rows) / n, 4), "criteria": crit,
           "seconds": round(sum(r["seconds"] or 0 for r in rows) / n, 2), "tokens": int(sum(r["tokens"] or 0 for r in rows)),
           "errors": sum(1 for r in rows if r["error"])}
    if label:
        ctx.log(f"{label}: score {out['score']:.3f} " + " ".join(f"{k}={v}" for k, v in crit.items()))
    return out


def rows_frame(rows: list[dict], criteria: list[dict]) -> pl.DataFrame:
    schema = {"question": pl.String, "reference": pl.String, "answer": pl.String,
              **{c["key"]: pl.Float64 for c in criteria}, "score": pl.Float64, "feedback": pl.String,
              "steps": pl.Int64, "tool_calls": pl.Int64, "sql": pl.String, "seconds": pl.Float64, "tokens": pl.Int64,
              "error": pl.String}
    extra = sorted({k for r in rows for k in r} - set(schema))
    return pl.DataFrame([{k: r.get(k) for k in [*schema, *extra]} for r in rows],
                        schema={**schema, **{k: pl.String for k in extra}}, strict=False)


def judge_server(inputs, agent: AgentPortObject | None, port: int):
    """The judge: the connection on its port, else the agent's model."""
    conn = inputs[port] if len(inputs) > port and isinstance(inputs[port], LLMConnectionPortObject) else None
    if conn is not None:
        return conn.server()
    if agent is not None and agent.connection is not None:
        return agent.connection.server()
    return server({})


EVAL_COLS = [F("question_column", "column", "Question column", None, "Empty: question.", port=2, kinds=["string"]),
             F("reference_column", "column", "Reference answer column", None, "Empty: reference (if any).", port=2, kinds=["string"])]
AGENT_IN = Port(AGENT, "Agent", "The agent to evaluate")
EVAL_IN = Port(TABLE, "Evaluation set", "Questions, with reference answers")
JUDGE_IN = Port(LLM, "Judge", "The judge model (optional: the agent's own)", optional=True)


def summary_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None, strict=False)


class RubricJudge(NodeModel):
    factory = "verknode.eval.RubricJudge"
    name = "Rubric Judge"
    category = CAT
    icon = "⚖"
    in_ports = [Port(TABLE, "Answers", "Questions and answers (and reference answers) to score"),
                Port(LLM, "Judge", "The judge model (optional: the server settings of LLM Prompter's defaults)", optional=True)]
    out_ports = [Port(TABLE, "Scores", "The input with a column per criterion (0-1), the score and the judge's feedback")]
    short_description = "Scores answers against a rubric: reference checks and an LLM judge, into one weighted score."
    description = (
        "For each row - a question, an answer and, if you have one, a reference answer - the rubric's criteria are "
        "scored 0 to 1: Numbers match the reference (the share of the reference's numbers in the answer), "
        "Contains the reference, Not empty, and LLM judge criteria a judge model scores 1-5 (with feedback). The "
        "score is their weighted mean. The judge sees an evidence column too if you pick one (e.g. the SQL the "
        "agent ran). Works on any table of LLM answers - an LLM Prompter's, an Agent Runner's."
    )
    fields = [
        F("question_column", "column", "Question column", None, port=1, kinds=["string"]),
        F("answer_column", "column", "Answer column", None, "Empty: answer.", port=1, kinds=["string"]),
        F("reference_column", "column", "Reference answer column", None, "Empty: reference (if any).", port=1, kinds=["string"]),
        F("evidence_column", "column", "Evidence column", None, "What the judge may check against (e.g. sql). Optional.", port=1, kinds=["string"]),
        *judge_fields(),
    ]

    def configure(self, in_specs, s, ctx):
        spec = in_specs[0]
        crit = _criteria(s)
        return [pl.Schema({**dict(spec or {}), **{c["key"]: pl.Float64 for c in crit}, "score": pl.Float64, "feedback": pl.String})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        crit = _criteria(s)
        if ctx.dry:
            return [pl.DataFrame(schema=self.configure([df.schema], s, ctx)[0])]
        pick = lambda key, default: s.get(key) or (default if default in df.columns else None)  # noqa: E731
        qc, ac, rc, ec = pick("question_column", "question"), pick("answer_column", "answer"), pick("reference_column", "reference"), s.get("evidence_column")
        if not ac:
            raise InvalidSettingsError("Pick the answer column")
        conn = inputs[1] if len(inputs) > 1 and isinstance(inputs[1], LLMConnectionPortObject) else None
        srv = conn.server() if conn is not None else (server({}) if any(c["kind"] == KINDS[0] for c in crit) else None)
        rows = df.to_dicts()

        def one(r):
            ctx.check_canceled()
            return score(srv, crit, s, str(r.get(qc) or "") if qc else "", str(r.get(ac) or ""),
                         None if not rc or r.get(rc) is None else str(r[rc]), str(r.get(ec) or "") if ec else "")

        with ThreadPoolExecutor(max_workers=max(1, int(s.get("workers") or 1))) as ex:
            scores = list(ex.map(one, rows))
        add = pl.DataFrame([{**{c["key"]: sc.get(c["key"]) for c in crit}, "score": sc["score"], "feedback": sc["feedback"]} for sc in scores],
                           schema={**{c["key"]: pl.Float64 for c in crit}, "score": pl.Float64, "feedback": pl.String})
        return [pl.concat([df.drop([c for c in add.columns if c in df.columns]), add], how="horizontal")]


class AgentEvaluation(NodeModel):
    factory = "verknode.eval.AgentEvaluation"
    name = "Agent Evaluation"
    category = CAT
    icon = "✓🤖"
    node_type = "Other"
    in_ports = [AGENT_IN, EVAL_IN, JUDGE_IN]
    out_ports = [Port(TABLE, "Results", "Each question: the answer, the rubric's scores, the judge's feedback, SQL, steps"),
                 Port(TABLE, "Summary", "One row: the score, the mean of each criterion, time, tokens, errors")]
    short_description = "Runs an agent on an evaluation set and scores its answers with a rubric."
    description = (
        "Asks the agent each question of the evaluation set and scores the answers with the rubric (see Rubric "
        "Judge): reference checks and LLM judge criteria, which see the SQL results the agent saw. The summary row "
        "is the agent's score (0-1) - the number Hill Climbing raises and Ablation Study compares."
    )
    fields = [*EVAL_COLS, *judge_fields()]

    def configure(self, in_specs, s, ctx):
        return [None, None]

    def execute(self, inputs, s, ctx):
        agent, df = inputs[0], inputs[1]
        items = eval_set(df, s)
        crit = _criteria(s)
        if ctx.dry:
            return [rows_frame([], crit), pl.DataFrame()]
        ev = evaluate(agent, items, s, judge_server(inputs, agent, 2), ctx, agent.name)
        summary = {"agent": agent.name, "model": agent.connection.label if agent.connection else "", "questions": len(items),
                   "score": ev["score"], **ev["criteria"], "seconds_per_question": ev["seconds"], "tokens": ev["tokens"],
                   "errors": ev["errors"]}
        return [rows_frame(ev["rows"], crit), summary_frame([summary])]


# ---------------------------------------------------------------------------------------------------
# hill climbing
# ---------------------------------------------------------------------------------------------------
PROPOSER = """You improve the instructions of a data query agent (an LLM with SQL tools over tables). You get its \
current instructions and examples, the tables it queries (columns and first rows), its score on an evaluation set, \
and the questions it got wrong: its answers, the reference answers (correct, computed from the same tables), the \
reference numbers its answer missed, the judge's feedback and the SQL it ran. Work out the cause: which \
computation over these tables gives the reference numbers, and where the agent's SQL differs - e.g. what a measure \
means (a column may be a unit price, not a total), aggregating before a join so that per-group values are not \
repeated, the date range the data covers, how a share or a percent is computed. Then write improved instructions \
that teach this in general: rules, or a "## Data notes" section on what the columns mean and how to compute \
measures. Never put the reference numbers or the evaluation questions into the instructions. Keep the ## \
sections that work; stay short. Optionally improve the examples (a worked question, its SQL and answer - not one \
of the evaluation questions). Reply with JSON: {"change": "one sentence: what you changed and why", \
"instructions": "...", "examples": "..."}."""


def _missed(answer: str, reference: str | None, tol: float = 0.005) -> list:
    got = _numbers(answer)
    return [w for w in _numbers(reference or "") if not any(abs(g - w) <= abs(w) * tol + 0.006 or abs(g * 100 - w) <= abs(w) * tol + 0.006
                                                         for g in got)]


def _failures(ev: dict, n: int = 5) -> str:
    worst = sorted(ev["rows"], key=lambda r: r["score"])[:n]
    out = []
    for r in worst:
        if r["score"] >= 0.999:
            continue
        missed = _missed(r["answer"], r["reference"])
        out.append(f"Q: {r['question']}\nReference: {r['reference']}\nIts answer: {r['answer'][:600]}\n"
                   + (f"Reference numbers missing from its answer: {', '.join(f'{m:g}' for m in missed)}\n" if missed else "")
                   + f"Score: {r['score']:.2f}; judge: {r['feedback'][:300]}\nIts SQL: {r['sql'][:700]}")
    return "\n\n".join(out) or "(no failures: improve robustness and concision)"


def _tables_text(agent: AgentPortObject) -> str:
    from .data_chat import markdown

    return "\n\n".join(f"Table {n} ({df.height} rows):\n{markdown(df, 6, 20)}" for n, df in agent.tables.items())


def leaks(text: str, items: list[dict]) -> list[str]:
    """Reference numbers (3+ digits) written into proposed instructions: learning the answers, not the method."""
    out = []
    for it in items:
        for m in NUM.finditer(it.get("reference") or ""):
            v = m.group(0).replace(",", "")
            if len(v.replace(".", "").replace("-", "").lstrip("0")) >= 3 and v in (text or "").replace(",", ""):
                out.append(v)
    return out


INVESTIGATE = """You find out why a data query agent got questions wrong. You have the same read-only tools \
over the same tables (list_tables, describe, rows, sql). For each failed question, find a SQL query whose result \
matches the reference answer (the references are correct), and compare it with the agent's SQL. Then report, in \
general terms that hold for other questions too, what the agent misunderstood or computed wrong - what a column \
means, how to compute a measure, the order of aggregation and joins, the dates the data covers - one bullet per \
finding, each with the correct way. Do not stop at a guess: run queries until one gives the reference number \
(within rounding) - try other definitions of the measure (e.g. a unit price times a quantity instead of the price \
alone), aggregating before a join, the dates that are really in the data - and report only what you verified. \
Do not report the reference numbers themselves."""


def investigate(srv: dict, agent: AgentPortObject, ev: dict, ctx, steps: int = 24) -> str:
    """Findings of the proposer model, which reproduces the reference answers with the table tools."""
    from .agents import _tables
    from .data_chat import TOOLS as TABLE_TOOLS
    from .llm import chat_tools, tool_message

    tables = _tables(agent)
    msgs = [{"role": "system", "content": INVESTIGATE},
            {"role": "user", "content": f"The tables:\n{_tables_text(agent)}\n\nThe failures:\n{_failures(ev)}"}]
    for _ in range(steps):
        ctx.check_canceled()
        r = chat_tools(srv, msgs, {"temperature": 0.2, "max_tokens": 4000, "timeout": 900, "think": True}, TABLE_TOOLS)
        msgs.append(r["message"])
        if not r["calls"]:
            return (r["text"] or "").strip()
        for c in r["calls"]:
            msgs.append(tool_message(srv, c, tables.call(c["name"], c["args"])))
    msgs.append({"role": "user", "content": "Report your findings now."})
    return (chat(srv, msgs, {"temperature": 0.2, "max_tokens": 1500, "timeout": 600})["text"] or "").strip()


def propose(srv: dict, agent: AgentPortObject, ev: dict, k: int, temperature: float, tried: list[str],
            findings: str = "") -> dict:
    prompt = (f"Current instructions:\n{agent.instructions}\n\nCurrent examples:\n{agent.examples or '(none)'}\n\n"
              f"The tables:\n{_tables_text(agent)}\n\n"
              + (f"What an investigation of the failures found (it reproduced the reference answers with SQL):\n{findings}\n\n" if findings else "")
              + f"Score: {ev['score']:.3f}; per criterion: {json.dumps(ev['criteria'])}\n\nFailures:\n{_failures(ev)}\n\n"
              + (f"Already tried (not better): {'; '.join(tried[-6:])}\n\n" if tried else "")
              + f"Write proposal {k + 1}: a different change than the ones already tried.")
    schema = {"type": "object", "properties": {"change": {"type": "string"}, "instructions": {"type": "string"},
                                                "examples": {"type": "string"}}, "required": ["change", "instructions"]}
    r = chat(srv, [{"role": "system", "content": PROPOSER}, {"role": "user", "content": prompt}],
             {"temperature": temperature, "max_tokens": 2500, "timeout": 600}, schema)
    j = _json_answer(r["text"])
    return {"change": str(j.get("change") or "")[:400], "instructions": str(j.get("instructions") or agent.instructions),
            "examples": str(j.get("examples") if j.get("examples") is not None else agent.examples)}


class HillClimbing(NodeModel):
    factory = "verknode.eval.HillClimbing"
    name = "Hill Climbing"
    category = CAT
    icon = "⛰"
    node_type = "Learner"
    in_ports = [AGENT_IN, EVAL_IN, JUDGE_IN,
                Port(LLM, "Proposer", "The model that proposes changes (optional: the judge)", optional=True)]
    out_ports = [Port(AGENT, "Best agent", "The agent with the best score found"),
                 Port(TABLE, "History", "Every candidate: step, the change, its score, accepted"),
                 Port(TABLE, "Best results", "The best agent's results on the evaluation set")]
    short_description = "Improves an agent step by step: proposed changes are kept only when they score higher."
    description = (
        "Hill climbing on the agent's instructions (and examples): each step, a proposer model reads the failures of "
        "the best agent so far - the answers, reference answers, the judge's feedback, the SQL - and proposes a few "
        "changed versions; each is evaluated with the rubric, and the best one replaces the current agent only if it "
        "scores higher by at least Minimum gain. With Hold out, some questions are kept out of the climb and only "
        "reported (a check against overfitting to the questions it learns from). The history has every candidate "
        "(line chart: score per step); the first output is the best agent - for Agent Runner, Ablation Study."
    )
    fields = [*EVAL_COLS,
              F("steps", "int", "Steps", 3), F("candidates", "int", "Candidates per step", 2),
              F("min_gain", "float", "Minimum gain", 0.01, "A candidate must beat the current score by this much."),
              F("temperature", "float", "Proposer temperature", 0.7, "Higher: more different proposals."),
              F("holdout", "int", "Hold out (questions)", 0, "Questions kept out of the climb, only scored."),
              F("seed", "int", "Seed", 1, "For the hold-out split."),
              F("investigate", "bool", "Investigate the failures first", True,
                "The proposer reproduces the reference answers with the table tools and reports what the agent got "
                "wrong, before it proposes changes."),
              *judge_fields()]

    def configure(self, in_specs, s, ctx):
        return [in_specs[0], None, None]

    def execute(self, inputs, s, ctx):
        agent, df = inputs[0], inputs[1]
        crit = _criteria(s)
        items = eval_set(df, s)
        if ctx.dry:
            return [agent, pl.DataFrame(), rows_frame([], crit)]
        jsrv = judge_server(inputs, agent, 2)
        psrv = inputs[3].server() if len(inputs) > 3 and isinstance(inputs[3], LLMConnectionPortObject) else jsrv
        hold = max(0, min(int(s.get("holdout") or 0), len(items) - 1))
        rng = random.Random(int(s.get("seed") or 1))
        order = list(range(len(items)))
        rng.shuffle(order)
        held = [items[i] for i in sorted(order[:hold])]
        train = [items[i] for i in sorted(order[hold:])]
        steps, k_cand = max(1, int(s.get("steps") or 3)), max(1, int(s.get("candidates") or 2))
        total = 1 + steps * k_cand
        history, done = [], [0]

        def tick():
            done[0] += 1
            ctx.set_progress(done[0] / total)

        def held_score(a):
            return evaluate(a, held, s, jsrv, ctx)["score"] if held else None

        best = agent
        best_ev = evaluate(best, train, s, jsrv, ctx, "baseline")
        tick()
        history.append({"step": 0, "candidate": 0, "change": "baseline", "score": best_ev["score"], "holdout_score": held_score(best),
                        **best_ev["criteria"], "accepted": True, "best_score": best_ev["score"], "instructions": best.instructions,
                        "examples": best.examples})
        tried: list[str] = []
        for step in range(1, steps + 1):
            cands = []
            findings = ""
            if s.get("investigate", True) and best.tables:
                try:
                    findings = investigate(psrv, best, best_ev, ctx)
                    ctx.log(f"Step {step} findings: {findings[:500]}")
                except CanceledExecutionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    ctx.warn(f"Step {step}: the investigation failed: {e}")
            for k in range(k_cand):
                ctx.check_canceled()
                try:
                    p = propose(psrv, best, best_ev, k, float(s.get("temperature") or 0.7), tried, findings)
                except CanceledExecutionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    ctx.warn(f"Step {step}: the proposer failed: {e}")
                    tick()
                    continue
                leaked = leaks(p["instructions"] + "\n" + p["examples"], items)
                if leaked:
                    ctx.warn(f"Step {step}.{k + 1}: the proposal wrote reference numbers ({', '.join(leaked[:3])}) into the "
                             "instructions - left out")
                    history.append({"step": step, "candidate": k + 1, "change": f"(left out: it copied reference numbers) {p['change']}",
                                    "score": None, "accepted": False, "best_score": best_ev["score"],
                                    "instructions": p["instructions"], "examples": p["examples"]})
                    tried.append(p["change"])
                    tick()
                    continue
                cand = best.with_changes(instructions=p["instructions"], examples=p["examples"],
                                         name=f"{agent.name} (step {step}.{k + 1})")
                ev = evaluate(cand, train, s, jsrv, ctx, f"step {step}.{k + 1}")
                tick()
                cands.append((ev["score"], k, cand, ev, p["change"]))
            if not cands:
                continue
            top = max(cands, key=lambda c: c[0])
            accept = top[0] >= best_ev["score"] + float(s.get("min_gain") or 0.0)
            for sc, k, cand, ev, change in cands:
                win = accept and k == top[1]
                history.append({"step": step, "candidate": k + 1, "change": change, "score": sc,
                                "holdout_score": held_score(cand) if win else None, **ev["criteria"], "accepted": win,
                                "best_score": top[0] if accept else best_ev["score"], "instructions": cand.instructions,
                                "examples": cand.examples})
                if not win:
                    tried.append(change)
            if accept:
                best, best_ev = top[2], top[3]
                ctx.log(f"Step {step}: accepted {top[0]:.3f} ({top[4]})")
            else:
                ctx.log(f"Step {step}: no candidate beat {best_ev['score']:.3f}")
        best = best.with_changes(name=f"{agent.name} (climbed)")
        hist = pl.DataFrame([{"trial": i, **h} for i, h in enumerate(history)], infer_schema_length=None, strict=False)
        return [best, hist, rows_frame(best_ev["rows"], crit)]


# ---------------------------------------------------------------------------------------------------
# multi-armed bandit
# ---------------------------------------------------------------------------------------------------
STRATEGIES = ["All arms on every question", "UCB1 (bandit)", "Thompson sampling (bandit)"]


class MultiArmEvaluation(NodeModel):
    factory = "verknode.eval.MultiArmEvaluation"
    name = "Multi-Arm Evaluation"
    category = CAT
    icon = "🎰"
    node_type = "Other"
    in_ports = [AGENT_IN, EVAL_IN,
                *[Port(LLM, f"Arm {i}", "A model to compare (an LLM connector)" if i == 1 else "(optional)", optional=True)
                  for i in range(1, 5)],
                JUDGE_IN]
    out_ports = [Port(TABLE, "Arms", "Each arm: model, pulls, mean score, 95% interval, best"),
                 Port(TABLE, "Pulls", "Each pull: the arm, the question, the score"),
                 Port(TABLE, "Running means", "The mean score of each arm after each pull (line chart)")]
    short_description = "Compares models (arms) on an agent: all on every question, or a bandit (UCB1, Thompson)."
    description = (
        "The arms are models: the LLM connections on the Arm ports, the models listed in More arms (on the agent's "
        "server), and the agent's own model if wanted. Each pull runs the agent with an arm's model on a question "
        "and scores the answer with the rubric (the reward). All arms on every question pulls each arm on each "
        "question. The bandits spend a budget of pulls where it pays: UCB1 pulls the arm with the best upper "
        "confidence bound, Thompson sampling the arm a Beta posterior draw favours - so a weak model costs few "
        "pulls. The first output ranks the arms with 95% intervals; the third is for a line chart of the running means."
    )
    fields = [*EVAL_COLS,
              F("include_own", "bool", "The agent's own model is an arm", True),
              F("more_arms", "code", "More arms (models of the agent's server)", "", "One model name per line."),
              F("strategy", "enum", "Strategy", STRATEGIES[0], options=STRATEGIES),
              F("budget", "int", "Pulls (bandits)", 24),
              F("seed", "int", "Seed", 1),
              *judge_fields()]

    def configure(self, in_specs, s, ctx):
        return [None, None, None]

    def _arms(self, inputs, agent, s) -> list[tuple[str, LLMConnectionPortObject]]:
        arms = []
        if s.get("include_own", True) and agent.connection is not None:
            arms.append(agent.connection)
        arms += [x for x in inputs[2:6] if isinstance(x, LLMConnectionPortObject)]
        base = agent.connection
        for line in (s.get("more_arms") or "").splitlines():
            if line.strip() and base is not None:
                arms.append(base.with_model(line.strip()))
        out, seen = [], set()
        for a in arms:
            label = a.label if a.label not in seen else f"{a.label} ({len(out) + 1})"
            seen.add(label)
            out.append((label, a))
        if len(out) < 2:
            raise InvalidSettingsError("Give at least two arms: Arm ports, More arms, the agent's own model")
        return out

    def execute(self, inputs, s, ctx):
        agent, df = inputs[0], inputs[1]
        if ctx.dry:
            return [pl.DataFrame(), pl.DataFrame(), pl.DataFrame()]
        items = eval_set(df, s)
        arms = self._arms(inputs, agent, s)
        jsrv = judge_server(inputs, agent, 6)
        crit = _criteria(s)
        rng = random.Random(int(s.get("seed") or 1))
        strategy = s.get("strategy") or STRATEGIES[0]
        rewards: list[list[float]] = [[] for _ in arms]
        queues = [rng.sample(range(len(items)), len(items)) for _ in arms]
        pulls: list[dict] = []

        def pull(a: int):
            ctx.check_canceled()
            if not queues[a]:
                queues[a] = rng.sample(range(len(items)), len(items))
            it = items[queues[a].pop()]
            agent_a = agent.with_changes(connection=arms[a][1], name=f"{agent.name} · {arms[a][0]}")
            r = run_agent(agent_a, it["question"], ctx)
            sc = score(jsrv, crit, s, it["question"], r["answer"], it["reference"], evidence_of(r), r.get("error"))
            return a, it, r, sc

        def record(a, it, r, sc):
            rewards[a].append(sc["score"])
            pulls.append({"pull": len(pulls) + 1, "arm": arms[a][0], "question": it["question"], "score": sc["score"],
                          **{c["key"]: sc[c["key"]] for c in crit}, "seconds": r["seconds"], "answer": r["answer"][:500],
                          "feedback": sc["feedback"][:300]})
            ctx.set_progress(len(pulls) / total)

        if strategy == STRATEGIES[0]:
            total = len(items) * len(arms)
            # (arm by arm: a server keeps one model loaded at a time)
            for a in range(len(arms)):
                for _ in items:
                    record(*pull(a))
            pulls.sort(key=lambda p: (items.index(next(i for i in items if i["question"] == p["question"])), p["arm"]))
            for i, p in enumerate(pulls, 1):
                p["pull"] = i
        else:
            total = max(len(arms), int(s.get("budget") or 24))
            for a in range(len(arms)):  # (each arm once first)
                record(*pull(a))
            while len(pulls) < total:
                n = len(pulls)
                if strategy.startswith("UCB1"):
                    a = max(range(len(arms)), key=lambda i: sum(rewards[i]) / len(rewards[i])
                            + math.sqrt(2 * math.log(n) / len(rewards[i])))
                else:  # Thompson: Beta(1 + successes, 1 + failures), a reward r counting as r successes
                    a = max(range(len(arms)), key=lambda i: rng.betavariate(1 + sum(rewards[i]), 1 + len(rewards[i]) - sum(rewards[i])))
                record(*pull(a))
        rows = []
        best_mean = max((sum(r) / len(r)) for r in rewards if r)
        for (label, conn), r in zip(arms, rewards):
            m = sum(r) / len(r) if r else None
            sd = (sum((x - m) ** 2 for x in r) / (len(r) - 1)) ** 0.5 if len(r) > 1 else None
            half = 1.96 * sd / math.sqrt(len(r)) if sd is not None else None
            rows.append({"arm": label, "model": conn.model, "pulls": len(r), "mean_score": round(m, 4) if m is not None else None,
                         "ci_low": round(max(0.0, m - half), 4) if half is not None else None,
                         "ci_high": round(min(1.0, m + half), 4) if half is not None else None,
                         "best": m == best_mean})
        arms_df = pl.DataFrame(rows, strict=False).sort("mean_score", descending=True, nulls_last=True)
        pulls_df = pl.DataFrame(pulls, infer_schema_length=None, strict=False)
        running, sums, counts = [], {lbl: 0.0 for lbl, _ in arms}, {lbl: 0 for lbl, _ in arms}
        for p in pulls:
            sums[p["arm"]] += p["score"]
            counts[p["arm"]] += 1
            running.append({"pull": p["pull"], **{lbl: (sums[lbl] / counts[lbl] if counts[lbl] else None) for lbl, _ in arms}})
        return [arms_df, pulls_df, pl.DataFrame(running, strict=False)]


# ---------------------------------------------------------------------------------------------------
# ablation
# ---------------------------------------------------------------------------------------------------
FAMILIES = ["Instruction sections", "Examples", "Tools", "Table overview", "SQL check"]


def variants(agent: AgentPortObject, families: list[str]) -> list[tuple[str, str, AgentPortObject]]:
    """(variant, what was removed, the agent without it)."""
    out = [("full", "-", agent)]
    if "Instruction sections" in families:
        parts = sections(agent.instructions)
        for i, (head, _) in enumerate(parts):
            if len(parts) > 1:
                out.append((f"- {head or 'intro'}", f"instruction section '{head or 'intro'}'",
                            agent.with_changes(instructions=join_sections(parts[:i] + parts[i + 1:]))))
    if "Examples" in families and agent.examples.strip():
        out.append(("- examples", "the examples", agent.with_changes(examples="")))
    if "Tools" in families:
        for t in agent.tools:
            out.append((f"- tool {t}", f"the {t} tool", agent.with_changes(tools=[x for x in agent.tools if x != t])))
    if "Table overview" in families and agent.settings.get("overview", True):
        out.append(("- table overview", "the list of tables in the prompt", agent.with_changes(settings={**agent.settings, "overview": False})))
    if "SQL check" in families and agent.settings.get("verify", True):
        out.append(("- SQL check", "sending back answers with numbers but no query", agent.with_changes(settings={**agent.settings, "verify": False})))
    return out


class AblationStudy(NodeModel):
    factory = "verknode.eval.AblationStudy"
    name = "Ablation Study"
    category = CAT
    icon = "✂"
    node_type = "Other"
    in_ports = [AGENT_IN, EVAL_IN, JUDGE_IN]
    out_ports = [Port(TABLE, "Variants", "The full agent and each one without a part: score, change vs. full, criteria"),
                 Port(TABLE, "Results", "Each variant's results per question")]
    short_description = "Removes the agent's parts one at a time and measures what each is worth."
    description = (
        "Evaluates the full agent and, for each part, the agent without it: each ## section of its instructions, "
        "its examples, each tool, the table overview in its prompt, and the SQL check. The change of score against "
        "the full agent says what a part is worth: a big drop - it matters; about zero - it may go (a shorter, "
        "cheaper prompt); a gain - it hurts. Repeats evaluates each variant several times (models vary); the "
        "Variants table is for a bar chart of the change."
    )
    fields = [*EVAL_COLS,
              F("families", "multienum", "Remove", FAMILIES, options=FAMILIES),
              F("repeats", "int", "Repeats", 1),
              *judge_fields()]

    def configure(self, in_specs, s, ctx):
        return [None, None]

    def execute(self, inputs, s, ctx):
        agent, df = inputs[0], inputs[1]
        crit = _criteria(s)
        if ctx.dry:
            return [pl.DataFrame(), rows_frame([], crit)]
        items = eval_set(df, s)
        jsrv = judge_server(inputs, agent, 2)
        vs = variants(agent, s.get("families") or FAMILIES)
        reps = max(1, int(s.get("repeats") or 1))
        out, results = [], []
        for i, (label, removed, a) in enumerate(vs):
            scores, crits = [], []
            for rep in range(reps):
                ev = evaluate(a, items, s, jsrv, ctx, f"{label}" + (f" #{rep + 1}" if reps > 1 else ""))
                scores.append(ev["score"])
                crits.append(ev["criteria"])
                results += [{"variant": label, "repeat": rep + 1, **r} for r in ev["rows"]]
                ctx.set_progress((i * reps + rep + 1) / (len(vs) * reps))
            mean = sum(scores) / len(scores)
            out.append({"variant": label, "removed": removed, "score": round(mean, 4),
                        "score_sd": round((sum((x - mean) ** 2 for x in scores) / (len(scores) - 1)) ** 0.5, 4) if len(scores) > 1 else None,
                        **{c["key"]: round(sum((cr[c["key"]] or 0) for cr in crits) / len(crits), 4) for c in crit},
                        "seconds": None})
        full = out[0]["score"]
        for r in out:
            r["change"] = round(r["score"] - full, 4)
        table = pl.DataFrame(out, infer_schema_length=None, strict=False).drop("seconds")
        res = pl.DataFrame([{"variant": r["variant"], "repeat": r["repeat"], **{k: v for k, v in r.items() if k not in ("variant", "repeat")}}
                            for r in results], infer_schema_length=None, strict=False)
        return [table, res]
