# Custom nodes (Python)

Write nodes as Python classes in a folder next to the workflow. verknode
names its node folders with a `vkn_` prefix (the example node file goes into
`vkn_nodes/` by default), so in the file browser they stand apart from your
data and notebooks:

    # vkn_nodes/my_nodes.py
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

- **Your node folder, `~/.verknode/nodes`**, is the default place for your
  nodes. Every workflow you open loads it (in [your verknode
  folder](install.md#your-verknode-folder); not saved with the workflows).
  ≡ Settings › Custom nodes › *Create example node there* starts it with an
  example file.
- **A workflow's own node folders** are for nodes that travel with the
  workflow. They are saved in the `.vkf`, relative to it (`workflow.json` →
  `custom_nodes`), and load after yours.
- **≡ Settings › Custom nodes** shows both kinds of folder, shows load errors
  with tracebacks, and can create an example node file in either.
- **Sharing:** a workflow that uses nodes from your folder needs them on the
  other machine too. Without them, those nodes show as missing. Put them in
  a workflow folder to share them, or make them a [package](packages.md)
  that others install.
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
- Example: `notebooks/examples/custom_nodes/custom_nodes.vkf` uses the separate folder
  `notebooks/examples/custom_nodes/vkn_custom_nodes/` (a source node, a node with a view that
  pushes a flow variable, and a node using a private helper module
  `_text.py`), mixed with standard nodes.
- Python: `wf.set_node_paths(["vkn_nodes"])`, `wf.reload_nodes()`,
  `wf.create_node_template("vkn_nodes")`, `wf.node_types("search")`.
- Custom node code runs inside the Jupyter server when a workflow that
  references the folder is opened — only open workflows you trust.

## Global defaults

Some setup belongs to the user, not to a workflow: the address of a
service, a list of servers, the name of a key variable. A node module
registers it once as a section of node defaults, with the same fields as a
dialog:

    from verknode.node import F, NodeModel, node_defaults, register_defaults

    register_defaults("my_service", "My service", [
        F("url", "string", "Service URL", "https://example.org/api"),
        F("key_env", "string", "API key variable", "MY_SERVICE_KEY"),
    ], description="Where the My Service nodes connect to.")

    class Lookup(NodeModel):
        ...
        def execute(self, inputs, s, ctx):
            url = s.get("url") or node_defaults("my_service")["url"]

- **≡ Settings › Node defaults** shows every registered section and saves it
  per user on the Jupyter server: `~/.verknode/defaults.json`, in [your
  verknode folder](install.md#your-verknode-folder) (another file:
  `VERKNODE_DEFAULTS`). Workflows don't contain it.
- **`node_defaults(key)`** returns the saved values, else each field's
  default.
- **Choices from a list:** an enum field can offer the names of a records
  field of the same section: `options_from={"field": "servers", "key":
  "name"}`.
- **Checks:** `register_defaults(..., check=fn)` refuses values that cannot
  be saved: `fn` raises `ValueError` with the message the editor shows.
- **No secrets:** store the names of environment variables, not keys. The
  file is plain JSON.
- **Example:** the saved LLM servers of the LLM nodes (`nodes/llm.py`, see
  [LLM nodes](llm.md#saved-llm-servers)).

## Standard nodes

New nodes subclass `NodeModel` in `extension/jupyterlab_verknode/nodes/` and are
registered with `@register` (see `engine/node.py`).
