# Export: run workflows anywhere

A workflow, or a part of it, exports in one of two forms:

- **A standalone script.** One `.py` file that rebuilds the workflow and
  runs it without Jupyter. It needs the `jupyterlab-verknode` package. See
  [standalone scripts](python-api.md#standalone-scripts-workflows-without-jupyter).
- **An export folder.** It runs on its own: a fresh Python with
  `pip install -r requirements.txt` is enough, with no verknode
  installation. The requirements are only the packages the workflow needs,
  measured by running it, so a workflow of table nodes needs nothing but
  polars. A Dockerfile and an HTTP API server for the workflow can come
  with it.

In the editor, use **Export as Python…** (Python menu, or the console) and
pick the **Standalone script** tab. Its options are:
- what to export: the whole workflow, a group, or the selected nodes;
- **Headless**;
- **Snapshot the data coming in**, for a part;
- for folders: **Data files**, **Dockerfile** and **API server**.

**Save as** writes the script, and **Export folder** writes
`<name>_export/` next to the workflow. The script below follows the
options, and the export reports what it did. Agents export with the MCP
tool `export_workflow` ([agents](agents.md)).

From a terminal:

    python -m jupyterlab_verknode.export report.vkf --headless --docker --api
    python -m jupyterlab_verknode.export report.vkf --group "Monthly totals" --snapshot --out totals_export
    python -m jupyterlab_verknode.export report.vkf --script-only --headless --out report.py

## The export folder

    report_export/
      report.py              the workflow (run it: python report.py --var NAME=VALUE --out output/)
      jupyterlab_verknode/   the engine and only the node modules the workflow uses
      verknode/node.py       the node API (custom node folders come along too)
      data/                  the files it reads (Data files: copied, settings pointed here)
      output/                where the files it writes go
      requirements.txt       only what it needs, with the versions it was tested with
      README.md              how to run it, what was changed for the export
      Dockerfile, server.py  if chosen

**How the requirements are kept to a minimum:**
1. The engine is plain Python on polars, and only the node modules the
   workflow's nodes are in are copied.
2. The folder is run in a fresh process: it rebuilds and runs the workflow
   with its own copy of verknode, in the file sandbox, so nothing real is
   written. Every package that run imports is recorded.
3. Each recorded package is then blocked in turn (group testing: half of
   them at a time). A package the run works without is optional and left
   out. For example, a Python Script node imports RDKit if it is there, but
   does not need it. This step stops after 60 s; packages it did not get to
   stay in.
4. Some needs cannot be measured, because they happen in other processes:
   a Jupyter Notebook node needs a kernel. Nodes declare those
   (`export_requirements`).

The export also notes optional packages and problems: a node that failed,
data it could not snapshot, or a node that needs the editor.

## A part of a workflow

Export a **group** (or the selected nodes, or `nodes=[...]` from Python and
MCP). The nodes before the part come with it (*upstream: include*). With
**Snapshot the data coming in** (*upstream: snapshot*), they don't: the
tables that flow into the part are saved as Parquet files in `data/`, and
Parquet Readers take their place. The export is then just the part, running
on a fixed copy of its inputs. Snapshots need a folder export.

## Headless

**Headless** removes what only the canvas needs. It works on a copy, so
the workflow does not change:

- **Displays** (output widgets) go: the table they show passes straight to
  the nodes after them. A display whose canvas selection feeds nodes stays,
  with the selection it has.
- **Input widgets** (sliders, text boxes, choices) become **workflow
  variables** with their current value, so they turn into `--var`
  parameters of the script and of the API. In the widgets example, nine
  inputs become variables such as `change=-10.0`, `points=120` and
  `noise=True`.
- **Chats** (nodes that wait for the canvas) go, with what only they feed.
  In standalone runs, a node that waits for the canvas fails at once rather
  than waiting.
- **Views, charts and leftover nodes** go when no result needs them. The
  results are the last table of each branch.

The README of the export lists each change.

## Docker

The Dockerfile uses a slim image of the Python it was made with. It
installs `requirements.txt`, plus the system libraries some packages need
in a slim image (RDKit's drawing needs libXrender, libXext and libexpat),
and runs as a non-root user.

    docker build -t report .
    docker run --rm -v "$PWD/output:/app/output" report --out output/ --var threshold=150

Two exports were built and run on plain `python:3.13-slim`:
- the regional check needed only polars;
- the widgets example needed polars, RDKit and numpy, with molecule
  descriptors.

## An API for the workflow

`server.py` is an HTTP API that uses only the standard library. Each
request runs the workflow with the workflow variables it gives and answers
with the result tables. Requests run in parallel, each on its own copy, up
to `MAX_RUNS` (default 2).

    python server.py                                    # PORT (8000), HOST (0.0.0.0)
    curl localhost:8000/                                # its variables (with defaults) and results
    curl -X POST localhost:8000/run -d '{"variables": {"threshold": 160}}'
    curl 'localhost:8000/run?threshold=250&format=csv&result=Below%20threshold'
    curl localhost:8000/openapi.json                    # an OpenAPI 3 description

- **Authentication:** set `API_KEY` and requests need
  `Authorization: Bearer <key>` (or `X-API-Key`); `/health` stays open.
- **Errors:** an unknown variable or a value of the wrong type gets a 400
  that says what is wrong, and a failing node gets a 500 with the reason.
- **Molecules** come back as SMILES.
- **Docker:** with both options, the image runs the server
  (`docker run -p 8000:8000 -e API_KEY=... report`). To run the script
  instead, use `docker run report python report.py ...`.
