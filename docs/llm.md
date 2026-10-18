# LLM nodes

**LLM Prompter** (AI › LLM) sends a prompt to a large language model for
every row of a table and adds the answers as columns. It works with
[Ollama](https://ollama.com) and with any OpenAI-compatible server
(llama.cpp's `llama-server`, vLLM, LM Studio, OpenAI and the like).

## The prompt

The prompt is a template: `{Column}` is replaced by the row's value
(`{{` and `}}` for literal braces). An optional system prompt gives
instructions for every request. Without an input table the prompt is sent
once and the answer is a one-row table.

    System:  Classify the sentiment of a product review as positive,
             negative or mixed, with a score from -1 to 1.
    Prompt:  Review: {Review}

**Answer** is either *Text* (one column) or *JSON fields*: name the fields,
each optionally typed - `sentiment, score:number` gives a String column
`sentiment` and a Float column `score` (types: string, number, integer,
boolean). The model is held to that shape where the server supports it
(Ollama's structured outputs, llama.cpp); otherwise the first JSON object of
the answer is used.

Other settings: requests at a time (Ollama serves several at once with
`OLLAMA_NUM_PARALLEL`), a row limit (100 by default, against sending a big
table by mistake), what to do when a request fails (fail the node, or a
missing answer and an `LLM error` column), time and token columns,
temperature, the longest answer, a seed, and whether reasoning models may
think before answering (off by default: faster, and only the answer comes
back).

Answers depend on the model and can be wrong: check them before you rely
on them. The example below does this for chemical structures.

## The server

Each node can name its server URL and model; empty settings use these
environment variables of the Jupyter server:

| Variable | Meaning | Default |
|---|---|---|
| `VERKNODE_LLM_URL` | server URL | `http://localhost:11434` |
| `VERKNODE_LLM_MODEL` | model, e.g. `llama3.2`, `qwen3:8b` | - |
| `VERKNODE_LLM_PROVIDER` | `ollama` or `openai` (OpenAI-compatible) | `ollama` |
| `VERKNODE_LLM_API_KEY` | key sent as a Bearer token, if the server needs one | - |

A variable that is not set in the environment is read from
`~/.config/verknode/llm.env` (the Jupyter server's user; `KEY=VALUE` lines;
another file: `VERKNODE_LLM_CONFIG`) - handy for a server that is already
running: the file is read each time a node runs.

The configure dialog lists the models of the server. API keys are read
from an environment variable (the node names which one) and are never
stored in the workflow - put them in `docker/credentials_norepo.env`.

With Docker, set the variables in `docker/.env` (gitignored), for example:

    # Ollama on the Docker host
    VERKNODE_LLM_URL=http://host.docker.internal:11434
    VERKNODE_LLM_MODEL=llama3.2

    # Ollama as a container on a network shared with verknode
    # (join the network in compose.override.yml): its service name
    VERKNODE_LLM_URL=http://ollama:11434

    # an OpenAI-compatible server
    VERKNODE_LLM_PROVIDER=openai
    VERKNODE_LLM_URL=http://llama-server:8080
    VERKNODE_LLM_MODEL=my-model

then `make up` (the container is created again with them). An Ollama on the
Docker host must listen on more than localhost (`OLLAMA_HOST=0.0.0.0`).

## Managing Ollama

The nodes in AI › Ollama look after an Ollama server (the same server
settings: a URL on the node, else `VERKNODE_LLM_URL`):

- **Ollama Models** - the models on the server: family, parameters,
  quantization, size, when they changed, whether they are loaded; with
  *Details* also their capabilities (completion, vision, tools, thinking,
  embedding) and context length.
- **Ollama Running Models** - the loaded models: size in memory, how much is
  in GPU memory (VRAM) and how much in CPU memory, the share on the GPU, the
  context length and when they unload. Pushes `ollama_gpu_memory_gb`,
  `ollama_cpu_memory_gb` and `ollama_loaded_models`. (Ollama reports the
  memory of its models, not the use of the GPU as a whole.)
- **Ollama Model Info** - per model: capabilities, context and embedding
  length, parameter count, default parameters, system prompt, license.
- **Ollama Load / Unload** - load models (and keep them loaded for a while,
  e.g. `30m`, or `-1` until unloaded) so the first answer comes quickly, or
  unload them to free GPU memory.
- **Ollama Download Model** - pull models from the Ollama library, with the
  progress on the node.
- **Ollama Create Model** - a model of your own from a base model with a
  system prompt and parameters (like a Modelfile with FROM, SYSTEM and
  PARAMETER); it shares the base model's weights.
- **Ollama Delete Model** - delete models.

The models to work on come from the node (comma separated) and / or a model
column of the input table - e.g. the rows picked in a Table Display of Ollama
Models. Download, Create and Delete change the server only with *Make the
change* on (Delete starts with it off); otherwise they list what they would
do. Each returns a row per model with its status (OK or the error) and time.

## A chatbot

A chat is a loop: the conversation - a table with role and content
columns - goes round a Recursive Loop and grows by your message and the
answer each time:

    start (greeting) -> Recursive Loop Start -> Chat -> LLM Chat -> Concatenate -> Recursive Loop End
                                                  |____________________^              (feeds it back)

- **Chat** (Widgets › Displays) shows the conversation on the canvas and,
  while the loop runs, waits for the message you type into it; it outputs
  the conversation with your message last (and the message as the flow
  variable `message`). *End chat* sets the flow variable `chat_end`, which
  the Recursive Loop End's *End loop with variable* stops on; Cancel stops
  it too. When the loop is not running, *Start chat* (or *New chat*) runs it.
- **LLM Chat** (AI › LLM) sends the conversation to the model - the last
  *Memory* messages, after a system prompt - and outputs the answer as one
  row (role, content, model, seconds, tokens). Its optional second port
  takes a table the model answers from: a chat about your data (each row
  becomes a line of information).
- **Concatenate** adds the answer to the conversation, and the Recursive
  Loop End (*Collect data from last iteration only*) feeds it back to the
  start - so the model sees everything said so far - and outputs the whole
  conversation when the chat ends.

**Chat Loop Start** (AI › LLM) is the Recursive Loop Start and the Chat in
one node:

    Chat Loop Start -> LLM Chat -> Concatenate -> Recursive Loop End

It starts from its *Greeting* (or a conversation table at its optional
input), shows the conversation on the canvas each time round and waits for
your message; *End chat* ends the loop by itself (no end variable needed).

`notebooks/examples/chatbot.vkf` has three: a plain chat and a shop
assistant that answers from a product table ("What could I get for under
50 EUR?"), both with a Chat Loop Start, and the same loop built from a
Recursive Loop Start and a Chat. Nodes can wait for the canvas like the Chat does with
`ctx.wait_for_input()`, and show what they are doing while they run with
`ctx.set_display(...)`.

## A coding agent

`notebooks/examples/coding_agent.vkf` is a small coding agent made of
ordinary nodes, with one built for it - **Python Code Runner** (Scripting):

    Tasks -> Recursive Loop Start -> LLM Prompter -> Python Code Runner -> Recursive Loop End
                                      (write the code)  (run the tests)     (until all pass)
                                                              |
                                                     Row Filter (failed) -> Column Filter -> back to the start

Each row of the tasks table is a text manipulation task - normalize phone
numbers, headline case, dates as ISO dates, find email addresses - with
test cases the model does not see. The task says little (*"normalizes a US
phone number"*); the cases say exactly what is wanted. The LLM Prompter
writes the code from the task, its previous attempt and what the test said
about it (*Replace answer columns* lets it answer again in the same column).
The Python Code Runner runs the code with the cases in a new Python process
- in an empty temporary folder, with a time and a memory limit - and adds
*Passed*, *Error*, the whole *Output* and *Seconds*. A small `check()`
helper (the runner's *Run before every code*) stops at the first failing
case and says what was wrong, e.g.

    case 4 of 6: normalize_phone('+1 (555) 123 4567 ext. 89') returned None,
    expected '+1-555-123-4567 x89'

so each attempt learns one more case. Tasks that fail go round again with
that message; the loop ends when all pass, or after eight attempts. In the
saved run (gemma4:26b) the tasks took two to four attempts. The results show
every attempt and the code that passed.

The code runs as the Jupyter server's user and can use its Python packages
(e.g. RDKit, in one of the tasks): the limits keep runaway code in check,
but it is not a sandbox - run code you would run yourself.

## Examples

`notebooks/examples/llm.vkf` (set `VERKNODE_LLM_MODEL` first, or a model on
each LLM node):

- **Classify** customer reviews: sentiment and score as JSON fields, shown
  in a table and counted in a bar chart.
- **Summarize** all reviews in one prompt (GroupBy concatenates them).
- **Extract** compound, amount, unit, solvent and temperature from free-text
  lab notes into typed columns.
- **Names to structures**: the model's SMILES for drug names, parsed by
  RDKit and compared with known structures - models get some of them wrong.

`notebooks/examples/coding_agent.vkf`: the coding agent above - four text
manipulation tasks whose hidden cases ask for more than the task says, so
the agent learns from the failing cases, one per attempt.

`notebooks/examples/chatbot.vkf`: the two chats above.

`notebooks/examples/ollama.vkf`:

- **Models**: the server's models with their details; click rows in the
  table to load those models (switch the node to Unload to free the memory).
- **Loaded models**: their GPU and CPU memory as a table, a total and a chart.
- **Details** of every model.
- **Download, create, delete** with *Make the change* off: what they would do.
