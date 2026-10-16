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
