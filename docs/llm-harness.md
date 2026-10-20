# An LLM harness as a workflow

An LLM *harness* is the program around a model. It gives the model tools,
checks what it may do, runs the tools, keeps the context within budget, and
decides when the model is done. Claude Code, coding agents and chat
assistants each have one. In verknode the harness is a workflow: every part
is a node you can see, change, test and measure on its own.

`notebooks/examples/agents/llm_harness.vkf`:

![An LLM harness as a verknode workflow: the task, a tools table (SQL, Python, a verknode workflow, Wikipedia over HTTP), the data and an Ollama connector on the left; the loop of Context Window, LLM Step, Tool Policy, Tool Executor and two Concatenates between Recursive Loop Start and End; on the right the answer, the trace of every message, tool calls per tool and tokens per turn; below, the policy denying a Python import of os, a DROP TABLE and an unknown shell tool](images/llm-harness-v1.png)

## The conversation is a table

The conversation is a table with one message per row. Its columns are:
- `role`: system, user, assistant or tool;
- `content`;
- `tool_calls`: JSON, `[{"id", "name", "args"}]`;
- `tool_call_id` and `name`: which call a tool message answers;
- `turn`;
- `tokens_in` and `tokens_out`.

A Recursive Loop carries the table from turn to turn:

    task -> Recursive Loop Start -> Context Window -> LLM Step -> Tool Policy -> Tool Executor
         -> Concatenate (+ results) -> Concatenate (+ denied) -> Recursive Loop End (until done)

## The nodes (AI › Harness)

| Node | The harness's ... |
|---|---|
| **LLM Step** | **model call.** One turn with the tools of a tools table (name, description, a JSON schema of the parameters). It outputs the conversation with the model's message last, and its tool calls, one row each. It sets the flow variable `done` (no calls: it answered), plus `answer` and `turn`. |
| **Tool Policy** | **permissions.** It checks the calls against rules, top down: allow or deny, by tool, and by a regular expression over the arguments. It also enforces limits per tool and per run, denies tools that aren't known, and denies a repeat of an earlier identical call. A denied call gets a tool message that says why, so the model can try another way. |
| **Tool Executor** | **tools.** It runs each call by its tool's kind (below). Errors are results too: the model reads them. |
| **Context Window** | **context management.** It keeps the conversation within a token budget. It shortens old tool results, and drops or summarizes (with the model) the oldest turns. The system message and the task always stay, a tool call stays with its result, and the last turns are kept as they are. |

Tool Executor runs four kinds of tools:
- **sql:** polars SQL over its input tables.
- **python:** code run in a separate process, in an empty folder, with time
  and memory limits. It runs the model's `code`, or an implementation that
  defines `run(**args)`.
- **workflow:** a reviewed verknode workflow; the arguments become its
  workflow variables.
- **http_get:** a URL with `{arg}` placeholders.

The loop end stops on `done` or after a maximum number of turns. Its
output is the whole conversation, which is the trace of the run.

## The example run

The task asks for four things:
1. which region has the lowest revenue as a percent of its target, and who
   manages it;
2. the regions below 150% of target, from the reviewed `regional_check`
   workflow;
3. how many months of 5% monthly growth that region needs to reach North;
4. what a compound annual growth rate is, from Wikipedia.

With gemma4:26b on Ollama, the run took 9 turns and 21 seconds:
- **Turn 1:** the first SQL failed (there is no `revenue` column), and the
  model fixed it.
- **Turn 3:** it called a tool that doesn't exist
  (`regions_lagging_behind_target`). The policy told it so, and later it
  used the real one.
- **Python:** the Python tool computed 8 months.
- **The answer** had every part right: West, Dana Holt, 131.7% of target,
  the only region below 150%, 8 months, and CAGR.

The run stayed within Context Window's 6000-token budget, so it changed
nothing. On the same conversation (19 messages, about 1,550 tokens), smaller
budgets show its strategies:
- **Shorten old tool results** (a budget of 1200) cut one old result.
- **Drop the oldest turns** (900) kept the system message, the task and the
  last two turns: 5 messages.
- **Summarize the oldest turns** (900) replaced the dropped turns with a
  bulleted summary the model wrote.

Below the harness, the policy is tested on calls it must deny:
- Python that imports `os`;
- a `DROP TABLE`;
- a `shell` tool it doesn't know.

**Two things the first runs taught**, both now in the example:
- **Models loop.** With thinking off, gemma4 sent the same SQL query every
  turn until the call limit stopped it. Tool Policy now denies an exact
  repeat with "you made this exact call already (turn N)", and LLM Step lets
  the model think.
- **The trace shows why.** The trace table is the conversation itself, so
  every message of the run is in it. Tool calls per tool, and tokens per
  turn (the context grows as results pile up), are a GroupBy and a chart
  away.

## Changing the harness

Each part is a node, so each is a setting or a node away:
- **another model:** another connector;
- **a stricter policy:** a rule;
- **a new tool:** a row of the tools table. A reviewed workflow becomes a
  tool without code.
- **an approval step:** a node between Tool Policy and Tool Executor;
- **a smaller context:** a lower budget.

**Measuring a change:** feed tasks with expected answers through the
harness in a loop, and score the answers with Rubric Judge (see
[agents](agents-build-evaluate.md)).
