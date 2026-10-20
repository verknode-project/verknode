# Python API and notebooks

## Live workflows from Python

From a notebook or console in the same JupyterLab:

    import verknode
    wf = verknode.open("notebooks/examples/demo.vkf")   # live: shared with an open editor
    reader = wf.add_node("CSV Reader", x=100, y=100, path="data.csv")
    sorter = wf.add_node("Sorter", keys=[{"column": "a", "order": "Ascending"}])
    wf.connect(reader, sorter)            # src_port / dst_port; port 0 = flow variables
    wf.execute()
    sorter.table()                        # output as a polars DataFrame
    sorter.help()                         # ports and settings
    verknode.nodes("loop")                    # find node types
    meta = wf.collapse(reader, sorter, name="Load")   # metanode
    wf.connect(meta, wf.add_node("Statistics"))       # outside: the metanode's ports
    wf.add_node("Column Filter", metanode=meta)       # inside; meta.inport(1) / meta.outport(1)
    meta.expand()
    g = wf.add_group(reader, sorter, text="Load", color="#3d9be9")   # group annotation
    g.text = "Load data"; g.nodes = [reader]; g.remove()
    wf.undo(); wf.save()

Changes appear in the open editor immediately (and editor changes are seen
by Python). The editor console prints the equivalent `>>>` command for every
edit you make in the editor (click one to copy it), and
**Python › Export** shows a script that rebuilds the whole workflow.

Scripts that make many edits (e.g. build a big workflow) do them in a batch:

    with wf.batch():
        prev = wf.add_node("Table Creator")
        for i in range(1000):
            node = wf.add_node("Math Formula", expression="$a$ + 1")
            wf.connect(prev, node)
            prev = node

A batch is one undo step, and it is fast also in big workflows: the server
takes one undo snapshot for the whole batch instead of one per edit, and edits
do not send the whole workflow back (300 nodes: 1.3 s instead of 11.5 s). The
editor shows the edits as they happen; an undo ends the batch and undoes all
of it, and a batch that is never ended (the script stopped) ends by itself
after two minutes without edits. Exported scripts build in a batch. In the
engine, `Workflow.batch()` does the same.

## Standalone scripts: workflows without Jupyter

**Export as Python…** (in the Python menu or the console), tab **Standalone script**, turns the workflow into a Python
script that rebuilds it and runs it in the same process. It needs no Jupyter
server, so it runs from a terminal, cron, CI, a container or another program.
**Save as** writes it next to the workflow, so its file paths work as they
are; the dialog asks before replacing an existing file. It needs the
`jupyterlab-verknode` package and the packages its nodes use.

    python regional_check.py                          # run it, print the result tables
    python regional_check.py --var threshold=150      # with other workflow variables
    python regional_check.py --out results/ --format parquet   # write the result tables (csv, parquet, json)
    python regional_check.py --result "Below threshold" --rows 0 --quiet
    python regional_check.py --save copy.vkf          # write the workflow, to open it in the editor

The script reads like the workflow:
- a `VARIABLES` dict with the workflow variables;
- a `build(wf)` function that adds each node with the settings that differ
  from the defaults, binds settings to variables, and connects the nodes;
- `build` returns the result nodes, by default the last table of each branch.

Metanodes are flattened, and custom node folders come along (`NODE_PATHS`).
File paths are relative to `--base-dir`, which defaults to the script's
folder. A failing node ends the run with exit code 1 and says why.

The same API builds workflows in plain Python:

    from jupyterlab_verknode.standalone import Workflow
    wf = Workflow(base_dir="data", variables={"threshold": 150.0})
    sales = wf.add_node("CSV Reader", "Sales", path="sales.csv")
    big = wf.add_node("Row Filter", "Big", column="amount", operator=">")
    wf.bind(big, "value", "threshold")          # the setting from a workflow variable
    wf.connect(sales, big)
    wf.run(big)                                 # WorkflowRunError if a node fails
    big.table()                                 # polars DataFrame
    wf.save("big_sales.vkf")

A `.vkf` file runs as it is, too:

    python -m jupyterlab_verknode.standalone report.vkf --var threshold=150 --out results/

It runs afresh by default (`--cached` keeps the results saved in the file).
Nodes that need a server (LLM nodes) need it there too. Nodes that need the
editor (chats on the canvas) fail at once.

To run where verknode is not installed, use an **export folder**. It holds
the script, the parts of verknode it uses, its data and a requirements.txt
of only what it needs, and can include a Dockerfile and an HTTP API server.
You can also export only a group, and *Headless* removes the widgets. See
[Export](export.md).

## Jupyter Notebook node

Runs a notebook as a workflow step (Scripting › Python). Its inputs
(`input_table_1`, `input_table_2`, … as polars DataFrames or `None`, and
`flow_variables`) are injected after the cell tagged `parameters`
(papermill convention) and the notebook assigns `output_table_1`, …
(pandas works too); new or changed flow variables are passed on. The node's
ports follow the notebook: the highest `input_table_N` / `output_table_N`
it uses (write `input_table_3` or assign `output_table_2`, save, and the
node shows that port; connections to ports that went away are removed). When the workflow runs, the notebook runs
headless on its kernel (or the one chosen in the dialog); printed output
goes to the console, the executed notebook becomes the node view (also when
a cell fails), and a copy can be saved as `<name>.executed.ipynb`.

In the dialog, **Create notebook** writes a notebook next to the workflow
with the kernel set, the usual imports and the inputs/outputs prepared;
**Open in JupyterLab** (also in the node's context menu) opens it for
editing. Run there, its parameters cell reads the node's inputs from the
live workflow with `verknode.notebook_inputs("workflow.vkf", node=7)`
(upstream nodes are executed if needed). Editing the notebook resets the
node, so the next run uses the new version.

With **Run in the open notebook** (node option), a run while the notebook
is open in JupyterLab happens there: the editor gives that notebook's
kernel the node's inputs and runs all cells, so you watch them execute and
keep the variables to explore afterwards; outputs go back to the workflow,
a failing cell fails the node, Cancel interrupts the kernel. When the
notebook is not open, the node runs headless as usual.

## Workflow Script node

**Workflow Script** (Scripting › Python) runs Python with `workflow`: the
workflow the node is in. The script reads it right away - nodes by label
(`workflow.find("Sales")`, `workflow.find_all("Variant ")`, `workflow.me`),
their settings, positions, widget layouts and connections - and changes it:

    f = workflow.add_node("Row Filter", 300, 200, label="Big", column="units", operator=">", value="100")
    workflow.connect(workflow.find("Sales"), f)          # replaces what fed that input
    copies = workflow.copy([a, b], dy=150, labels={"A": "A copy"})
    workflow.configure(workflow.find("Report"), title="Report - TEST DATA")
    workflow.widget(chart, side="free", x=600, y=300, w=300, h=170)
    workflow.group(*copies, text="built by a script")
    workflow.remove(old); workflow.move(n, x, y); workflow.label(n, "...")
    workflow.execute(f)                                  # run after the edits

Edits are applied when the run ends - in order, as one undo step, stopping at
the first that fails (the console says which) - so the running workflow never
changes under its nodes; nodes added by the script can be used in later edits
of the same script. A run that a Workflow Script started does not apply
further script edits, so scripts cannot loop. Changing a node upstream of the
script node resets it too. Like Python Script it has `input_table_1`,
`flow_variables` (changes are passed on) and `output_table_1` (default: the
input table). Input widgets with *Run on change* run the Workflow Scripts
after them, so a checkbox or drop-down can rebuild part of the workflow.

Example: `notebooks/examples/loops_and_scripting/workflow_scripts.vkf` - a filter reconfigured
from the data, a checkbox that switches the source for testing, a drop-down
that changes a widget and adds a chart, and a parallel chunk loop next to
the same parallel structure built by a script (one branch per chunk, joined
by Concatenate nodes; the engine runs such branches one after another, while
the Parallel Chunk Start / End nodes run their chunks at the same time).
