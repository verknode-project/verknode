# Building and evaluating agents

verknode builds data query agents as nodes, and measures and improves
them as nodes. An **agent** travels on a teal square port: its model
connection, instructions, examples, tools and data tables.

## Data query agents

Three nodes make the same kind of agent: one that answers questions about
the tables connected to it. They only differ in the framework that runs it.

| Node | Framework | What it is |
|---|---|---|
| **Data Query Agent** | verknode's tool loop | The model calls the tools until it answers. Nothing to install. |
| **LangGraph Agent** | [LangGraph](https://langchain-ai.github.io/langgraph/) | A `StateGraph`: an *agent* node (the model with the tools), a `ToolNode`, and a *verify* node that sends an answer back when its numbers were not computed by a query. |
| **Deep Agent** | [Deep Agents](https://github.com/langchain-ai/deepagents) (`deepagents`) | A deep agent: it plans with a todo list, keeps notes in a virtual file system (in its state, not on disk), and can hand a computation to a *sql-analyst* subagent. |

- **Tools:** all three use the same read-only tools over their tables:
  `list_tables`, `describe`, `rows` and `sql` (polars SQL).
- **Models:** they use any model connection (see
  [model connections](llm.md#model-connections-the-llm-port)). For LangGraph
  and Deep Agents this goes through a LangChain chat model of verknode's, so
  Ollama, vLLM, the commercial APIs, Claude and a local llama.cpp model all
  work.
- **Graph:** the second output is the agent's graph as Mermaid, the one
  LangGraph draws; show it with a Mermaid Display.
- **Installing:** LangGraph Agent needs `langgraph`, and Deep Agent needs
  `deepagents` (the `agents` extra).

**Settings:**
- **Instructions:** the system prompt, in `## ` sections.
- **Examples:** optional worked questions.
- **Tools:** which ones it gets.
- **Tell it the tables and columns:** a list of the tables in the prompt.
- **Numbers must come from SQL:** an answer with numbers but no query for
  the question is sent back.
- **Model calls per question at most**, the temperature and the longest
  answer.

**Agent Runner** asks an agent a table of questions. For each one it
returns:
- the answer;
- the model and tool calls;
- the SQL queries it ran;
- seconds, tokens and the error, if any;
- if wanted, the trace: the messages, and for a Deep Agent its todos and
  files.

`notebooks/examples/data_query_agent.vkf` builds one agent in each
framework. The three answer the same questions about sales and targets:

With a short *Data notes* section (revenue is amount × quantity; aggregate before joining the targets), all three answer all three correctly. For example, East reached 266.99% of its target.

![Three data query agents on the canvas - verknode's tool loop, LangGraph and Deep Agents - on the same sales and targets tables and the same Ollama model, each asked three questions by an Agent Runner, their answers in tables; the LangGraph graph (start, agent, tools, verify, end) drawn by Mermaid Display](images/data-query-agent-v1.png)

## Measuring and improving them

The evaluation nodes (AI › Evaluation) take an agent and an **evaluation
set**: questions with reference answers.

**A rubric** says what a good answer is. It is a list of criteria, each
scored 0-1 and weighted:
- **Numbers match the reference:** the share of the reference's numbers
  found in the answer (within 0.5%; a ratio counts for a percentage).
- **Contains the reference**, and **Not empty**.
- **LLM judge (1-5):** a judge model scores criteria such as *grounded*
  (every number is supported by the SQL results the agent saw), *complete*
  or *concise*. It writes feedback first, then the scores.

The weighted mean is the answer's score. **Rubric Judge** scores any table
of answers this way, for example an LLM Prompter's. **Agent Evaluation**
runs the agent on the set and scores it.

**Hill Climbing** improves the agent's instructions and examples:
1. It evaluates the agent.
2. A *proposer* model investigates the failures. With the same table tools,
   it runs queries until it reproduces the reference answers, then reports
   what the agent got wrong, in general terms. For example: "revenue is
   amount × quantity, not the amount", or "the data is from 2026".
3. It proposes a few rewrites from these findings, the answers, the judge's
   feedback and the SQL. A rewrite that copies reference numbers into the
   instructions is left out: it would learn the answers, not the method.
4. Each rewrite is evaluated. The best one replaces the agent only if it
   scores higher by at least *Minimum gain*.
5. Repeat.

Without the investigation, the proposer only read the failures. In
testing, its rewrites then never helped: it once wrote that the amount
*was* the revenue.

*Hold out* keeps some questions out of the climb and only reports them, as
a check against overfitting to the questions it learns from. The outputs
are the best agent, the history of every candidate (a line chart: score per
trial) and the best agent's results.

**Multi-Arm Evaluation** compares models (*arms*) on the same agent. The
arms come from:
- the LLM connections on its arm ports;
- model names on the agent's server;
- the agent's own model.

*All arms on every question* runs every arm on every question, arm by arm,
because a server keeps one big model loaded at a time. The bandits spend a
budget of pulls on the promising arms:
- **UCB1** pulls the arm with the highest upper confidence bound.
- **Thompson sampling** pulls the arm that a draw from its Beta posterior
  favours (a reward r counts as r successes).

The outputs are:
- the arms with mean scores and 95% intervals;
- every pull;
- the running means per arm, for a line chart.

**Ablation Study** removes the agent's parts one at a time and evaluates
each variant. The parts are:
- each `##` section of its instructions;
- its examples;
- each tool;
- the table overview;
- the SQL check.

Each variant's change of score against the full agent says what that part
is worth:
- **a large drop:** it matters;
- **about zero:** it could go, for a shorter and cheaper prompt;
- **a gain:** it hurts.

Run each variant more than once (*Repeats*): models vary from run to run.

`notebooks/examples/agent_improvement.vkf` puts it together on the sales
data, with an evaluation set of 8 questions with reference answers. These
are the results of one run with gemma4:26b on Ollama.

1. **Rubric evaluation of the baseline.** The baseline has the default
   instructions. It scores **0.75**, but only 41% of the reference numbers
   are right. It takes the amount alone as revenue (the amount is the unit
   price), sums the targets over joined sale rows, and filters on the wrong
   year. The LLM judge rated every answer as grounded: it checks the answer
   against the SQL results, not whether the SQL was the right SQL. The
   reference checks catch what the judge misses.
2. **Hill climbing.** In step 1, the proposer's investigation found that
   revenue is amount × quantity and that the data is from 2026. Its rewrite
   added a *Data notes* section and scored **0.99** (97% of the reference
   numbers right), so it was accepted. The four proposals of steps 2 and 3
   were rules about dates. None scored higher, so they were not kept.

![Rubric evaluation of the baseline agent (score 0.7455), and hill climbing: each candidate's score and the best so far, rising to 0.9866 at the first step; the table of proposed changes, the first adding a Data notes section on revenue as amount times quantity](images/agent-improvement-v1.png)

3. **Multi-arm evaluation** of the climbed agent, all arms on every question:

   | Arm | Mean score |
   |---|---|
   | gemma4:26b | 0.99 |
   | gemma4:26b at temperature 0.8 | 0.99 |
   | qwen2.5-coder:32b | 0.24 |
   | Qwen2.5-1.5B in-process (llama.cpp) | 0.13 |

   The Thompson-sampling bandit, with 16 pulls over the two gemma arms and
   the 1.5B model, gave the 1.5B model a single pull after its first answer
   failed.
4. **Ablation study** of the climbed agent. Without the sql tool, the score
   drops to 0. Without the new *Data notes* section, it drops by 0.05 (84%
   of the reference numbers right instead of 97%). Every other part (the
   other sections, the other tools, the table overview, the SQL check)
   changes nothing on this set. A leaner prompt would do here; the SQL check
   matters for models that answer from memory.

![Multi-arm evaluation (mean rubric score per model: two gemma4 arms near 1.0, qwen2.5-coder and the local 1.5B model far lower), the arms with 95% intervals, Thompson sampling's running means and pulls per arm, and the ablation study's change of score without each part (the sql tool: -0.99)](images/agent-arms-ablation-v1.png)

## Caveats

- Scores are from small evaluation sets and models that vary: compare with
  repeats, and with a hold-out set, before you trust a gain.
- An LLM judge is a model too: it can be lenient or wrong. The reference
  checks don't depend on it.
- Evaluations call the model many times. Mind the cost with paid APIs, and
  the time with large local models.
