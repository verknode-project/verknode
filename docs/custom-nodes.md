# Custom nodes (Python)

Write nodes as Python classes in a folder next to the workflow:

    # nodes/my_nodes.py
    import polars as pl
    from verknode.node import F, NodeModel, Port, TABLE

    class DoubleColumn(NodeModel):
        name = "Double Column"
        category = "Custom/Examples"
        in_ports = [Port(TABLE, "Input table")]
        out_ports = [Port(TABLE, "Output table")]
        fields = [F("column", "column", "Column")]

        def execute(self, inputs, s, ctx):
            print("doubling", s["column"])        # shown in the editor console
            return [inputs[0].with_columns(pl.col(s["column"]) * 2)]

- **≡ Settings › Custom nodes** lists the workflow's node folders (saved
  in the `.vkf`, relative to it; `workflow.json` → `custom_nodes`), shows
  load errors with tracebacks, and can create an example node file.
- **⟳ Reload nodes** (toolbar, shown when the workflow has node folders)
  re-imports the folders. Nodes switch to the new code and
  keep their settings; nodes whose code changed are reset. Changed files are
  also reloaded automatically before execution (can be switched off).
- Every non-abstract `NodeModel` subclass in a top-level `.py` file is a
  node; files can share helpers with relative imports (`from .utils import f`).
  Factory ids default to `custom.<file>.<Class>`; set `factory` to keep saved
  workflows working after renaming. `help(verknode.node)` documents the API.
- The standard nodes are a node folder with exactly the same conventions:
  `extension/jupyterlab_verknode/nodes/*.py` only use the public `verknode.node`
  API and sibling modules, and are discovered the same way — read them as
  examples (base classes are marked `abstract = True`).
- Example: `notebooks/examples/custom_nodes.vkf` uses the separate folder
  `notebooks/examples/custom_nodes/` (a source node, a node with a view that
  pushes a flow variable, and a node using a private helper module
  `_text.py`), mixed with standard nodes.
- Python: `wf.set_node_paths(["nodes"])`, `wf.reload_nodes()`,
  `wf.create_node_template("nodes")`, `wf.node_types("search")`.
- Custom node code runs inside the Jupyter server when a workflow that
  references the folder is opened — only open workflows you trust.

## Standard nodes

New nodes subclass `NodeModel` in `extension/jupyterlab_verknode/nodes/` and are
registered with `@register` (see `engine/node.py`).
