<p align="center">
  <img src="verknode_logo_cropped.svg" alt="verknode" width="360">
</p>

JupyterLab environment with a node-based visual programming
extension for Python (`extension/`, package `jupyterlab_verknode`). Data flows
between nodes as [polars](https://pola.rs) DataFrames.

## Usage

    make build      # creates credentials_norepo.env from the example if missing
    make up         # http://127.0.0.1:8888/lab
    make logs

Token: `JUPYTER_TOKEN` in `credentials_norepo.env`. Ports, host names and
reverse proxies (e.g. Traefik) are set per machine in gitignored files: see
[DEPLOYMENT.md](DEPLOYMENT.md).

## Extension development

`./extension` is mounted at `/home/jovyan/extension`. On every container
start `docker/before-notebook.d/10-verknode-extension.sh` runs `jlpm install` /
`jlpm build` (first time only), `pip install -e .` and
`jupyter labextension develop`, so the extension is live in JupyterLab.

    make watch      # rebuild on source change, then reload the browser
    make ext-build  # one-off rebuild

`make restart` reloads the Python side (workflow engine / server extension).

## Workflows (.vkf)

Create one from the Launcher ("Workflow") or the command palette
("New Verknode Workflow"); open `notebooks/examples/demo.vkf` for an example.

- Drag nodes from the Node Repository (or double-click; with one node
  selected the new node is connected to it).
- Ports: black triangles = data tables, red circles = flow variables.
  Every node has implicit flow variable ports at its top corners (shown on
  hover, or always via ≡ Settings).
- The mouse wheel zooms.
- **≡ Settings** (toolbar): editor behaviour — left-drag pan/select,
  mouse wheel zoom/pan, where
  views open (window / tab / dialog), flow variable ports, snap to grid,
  minimap, Python commands in the console — stored per user in JupyterLab's
  settings (also in Settings › Settings Editor); and the workflow's custom
  node folders.
- Traffic light: red = not configured, yellow = configured, green = executed.
- F6 configure, F7 execute (Shift: all), F8 reset (Shift: all), F9 cancel,
  Shift+F10 execute and open view, Del delete. Ctrl+S saves (including
  executed data).
- Double-click an output port (triangle, square or circle) to open its table
  / port object / flow variables (the context menu lists them too). Port
  tables and node views open in a separate browser window that can be moved
  and resized; hold Ctrl (Cmd) to open a tab instead. The windows update
  live when the workflow re-executes, and fall back to an in-editor dialog
  if the browser blocks pop-ups. They stay open when the node is reset: the
  last results remain visible (marked outdated) until it runs again. ≡
  Settings › "Open port and node views in" › JupyterLab tab opens them as
  JupyterLab tabs next to the editor (dock, split, drag like any notebook).
  Every view has buttons to move it elsewhere: ⧉ Window, ⇗ Browser tab, ⇱
  JupyterLab tab, ▣ Dialog (so a docked view can be windowed again, and
  back).
- Table outputs show their row count next to the port (e.g. `1.2k`).
- Canvas themes (≡ Settings › Theme): **Verkaround** (default) colours
  nodes by what they do - input / ingestion emerald and cyan,
  transformation indigo and cobalt, chemistry and statistics violet, ML
  amber (learners) and rose (predictors), outputs teal, views light teal,
  flow control slate - in light tints with dark glyphs on a light slate
  canvas; **Knife** has the colours of a popular workflow engine (by node
  type, on the JupyterLab background); **Butter** is between Knife and
  Haze (the Knife hues a little muted, on a warm cream canvas); **Haze** is Knife muted (half the
  saturation, softer text and connections on an off-white canvas);
  **Knightmare** puts the Knife colours on a dark canvas; **Pipeline Cowboy** has the look of a classic workflow
  engine (readers green, writers red, manipulators blue, filters gold,
  viewers purple, in lighter tints with dark glyphs; heavy pipes on light
  grey). Themes live in
  `extension/src/editor/themes.ts`.
- Node status lights (≡ Settings › Status lights): **LED** shows
  three slanted bars - the middle one red ` / ` (not configured), the outer
  two yellow `/ /` (configured), all three green `///` (executed);
  **LED orange** (default) is the same with reddish orange instead of red;
  **Traffic** is a traffic light with one red, yellow or green dot.
- Hold Ctrl and hover a node's traffic light to see what a Ctrl+click does:
  ▶ execute (yellow), ↺ reset (green), ■ cancel (running). Double-clicking
  a yellow light also executes the node.
- A tiny bar under each light shows the node's run time on a log scale
  (1 ms to 1 h; ticks at 1 s and 1 min) - dotted for yellow nodes: the
  estimated time to run them with the not executed nodes before them (from
  their last runs, scaled by input rows). Hover it for the numbers: time
  per row, the whole workflow's time on start nodes, everything before on
  end nodes. While running it shows progress (nodes can report it with
  `ctx.set_progress(f)`, Python Script with `set_progress(f)`; loops by
  iteration) and the expected end in local time below; amber means slower
  than expected (or waiting on such a node), red past the expected time.
  A running loop's start and end nodes show a pill with the iteration
  (`3/6`; `~6`: the count of the last run when the loop start cannot tell).
  The **Delay** node (Flow Control › Wait) passes its table on after some
  seconds (plus per row, optionally varied at random, with steady, slow-start
  or no progress) to try this out: `notebooks/examples/timing.vkf`.
- Settings can be bound to flow variables in the dialog's "Flow Variables"
  tab; workflow variables are set from the toolbar.
- **Tab** adds a node after the current one: a list of likely next nodes
  appears next to it (learned from what you added before, this workflow and
  common nodes; only nodes that accept the port's data). Type to narrow it
  down (also filters the Node Repository; acronyms like `gb` work), ↑ / ↓ to
  choose, Tab / Shift+Tab to pick another output port of the current node,
  Enter to add and connect, Esc to cancel. With nothing selected it suggests
  source nodes. The new node becomes current, so Tab again continues the chain.
  Inside an open scope (a loop, try/catch, IF/CASE switch, cross-validation)
  its closing node is suggested first (innermost scope first). Scope-opening
  nodes declare their closers with the class attribute `closed_by`
  (factory ids, most likely first). The ranking rules live in
  `extension/src/editor/hinting/` (`registerSuggestionRule()` adds rules).
- With several nodes selected, **Tab** connects them instead: from left to
  right, each free input is fed by the nearest compatible output of a
  selected node on its left (a row becomes a chain, one node feeding two
  becomes a fork, a Joiner gets its two inputs in vertical order). Nodes
  that already follow each other are not connected again; one undo step.
- Shift+Del (or right-click › Delete and Reconnect) deletes the selected
  nodes and closes the gap: every connection that left them is fed by the
  connection that came into them on the way (same port type), so removing
  the middle of a chain or a whole section keeps the flow connected.
- Alt+Del (or right-click › Detach and Reconnect) does the same without
  deleting: the selected nodes lose their connections to the rest of the
  workflow (connections among them stay), the gap is closed, and they move
  to the nearest free spot above or below; one undo step.
- Drag a node - or a selection of connected nodes - onto a connection to
  insert it: the connection is highlighted while you hover, and on drop its
  source feeds the first node and the last node feeds its target.
- Deleting nodes selects the nearest remaining upstream node.
- Loops, try/catch, IF/CASE switches and other scopes get a faint colored
  bubble around their nodes (loop teal, try/catch orange, switch lavender;
  dashed while the scope is not closed yet). Bubbles are smooth outlines
  computed from a distance field: gaps between nearby nodes are filled
  (compact groups look like a squircle), large blank areas become rounded
  concave bays, and nodes outside the scope are kept out where possible -
  without ever splitting a bubble. Shallow dents along runs of nodes are
  bridged with straight lines. Nested scopes always stay inside their
  parent. Toggle in ≡ Settings.
- Group annotations: select nodes and press Ctrl+G (or right-click ›
  Create Group Annotation…) to draw a coloured bubble with a text around
  them; the first line of the text is shown on the bubble. Double-click the
  label or a blank part of a bubble, or right-click there, to edit the
  text, colour and nodes or delete the annotation; Ctrl + double-click
  on a blank part of any bubble (group, loop, try/catch, switch) selects
  its nodes. Nodes and connections inside a bubble keep
  their own clicks. A node
  can belong to several groups: a group around the same (or more) nodes
  wraps the other one, and groups that share only some nodes are offset so
  each outline and label stays visible.
- Metanodes: select nodes and right-click › Collapse into Metanode… to
  put them into a metanode (named after the group annotation when exactly
  a group's nodes are selected), or right-click a blank part of a group
  bubble › Collapse Group into Metanode…; connections to the rest of the
  workflow become its ports, and groups around the nodes now go around the
  metanode (Expand reverses both). Double-click a metanode to open it (a breadcrumb at the top
  leads back); inside, its ports are the IN / OUT bars at the sides.
  Right-click a metanode (or the canvas inside one) to rename it, add or
  remove ports, or Expand it again. Metanodes only organize the workflow:
  execution, loops and flow variables work as without them, and the traffic
  light is green once everything inside is executed.
- Arrow keys select the nearest node in that direction (by position).
  Hold Alt to follow the connections instead (lineage): ← / → upstream /
  downstream, ↑ / ↓ a sibling (shares a parent or child; the one above /
  below). Double-tap Alt to stay in lineage mode (a badge shows it) until
  Alt is tapped again or nothing is selected. Small arrows on the nodes
  show where each key leads. Shift + arrow extends the selection (the
  newest node is where the next jump starts). ≡ Settings › Arrow keys
  switches lineage to strict port order (first parent, first child,
  sibling order).
- Drag on the empty canvas pans the view (also Ctrl + drag or middle-drag);
  Shift + drag draws a selection rectangle (Ctrl + Shift + drag adds to the
  selection); Ctrl/Shift + click adds or removes a node from the selection.
  (≡ Settings can switch left-drag to selecting instead.) Ctrl + double-click on a node selects it and
  everything downstream, Shift + double-click everything upstream.
- Ctrl+C / Ctrl+X / Ctrl+V copy, cut and paste nodes with their settings and
  the connections between them (also between workflows); Ctrl+Z undo,
  Ctrl+Shift+Z / Ctrl+Y redo (edits only; execution is not undone, and
  results of nodes unaffected by an undo are kept).

## Custom nodes (Python)

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

## Canvas widgets

Widget nodes are used directly on the canvas, without opening a dialog.
Input widgets show a control next to the node; output widgets show their
input there once executed. Each kind is its own node (repository ›
Widgets):

- **Inputs** – Text, Text Area, Number, Integer, Slider, Checkbox,
  Drop-down, Table (cells edited on the node, rows added or removed) and
  Molecule (double-click the structure to draw it in the sketcher). Each
  outputs its value as a one-row table and (except Table) a flow variable
  named like the column. Changing the value resets the nodes downstream
  and, with *Run on change* (default), executes the output widgets after
  it; ↺ goes back to the default. Text and numbers apply on Enter or when
  the field loses focus, sliders when released.
- **Displays** – Table, Bar Chart, Line Chart, Scatter Plot, Value (first/last
  row, sum, mean, min, max or count of a column, in large type) and Image
  (the picture on a square image port, e.g. of a chart node). Empty column
  settings are picked automatically; hovering a chart shows the values. The
  first output port passes the input on unchanged; Table, Bar, Line and
  Scatter also have a *Selection* port with the rows selected on the canvas
  (click table rows or bars, drag across a line chart, drag a box on a
  scatter plot or click its points; Ctrl adds). Selecting runs the display
  again with the output widgets after it.
- **Placement** – inputs sit left of their node, displays right; a widget
  points to its node's icon like a speech bubble and moves with the node.
  Drag its title bar to put it anywhere: it then stays at that place when
  the node moves, until it is dropped on its node again (back to the
  default place). Drag its corner to resize it; the title bar button (or
  a double-click on it) minimizes it. The node's context menu docks it
  left, right, above or below, and resets its size.
- The dialog sets the kind-specific options: label, output name and default
  value of inputs (choices, slider bounds, table columns ...), title and
  columns of displays.

New kinds are custom nodes subclassing `InputWidget` / `OutputWidget`
(`verknode.node`). They pick a canvas control (`control` = text, textarea,
number, integer, slider, checkbox, dropdown, table, molecule) or display
(`renderer` = table, bar, line, scatter, value, image) and override hooks
such as `default_value`, `valid`, `to_table` and `render` (`interactive =
True` with `select` adds the Selection port); `control = "html"` /
`renderer = "html"` show a page of their own in a sandboxed frame (an input
page calls `verknode.setValue(v)`):

    # widget_nodes/my_widgets.py
    from verknode.node import F, InputWidget

    class ColourInput(InputWidget):
        name = "Colour Input"
        control = "html"
        widget_fields = [F("default", "string", "Default colour", "#3d9be9")]

        def html(self, s, value):
            return f'<input type="color" value="{value}" onchange="verknode.setValue(this.value)">'

`notebooks/examples/widgets.vkf` shows every widget: a what-if (table and
slider into a bar chart and a total; clicked bars into a table), a signal
generator (inputs as flow variables of a Python Script, line and scatter
charts whose selections feed value displays), molecule properties, an image
display of a chart node, simple inputs, and custom widgets from
`notebooks/examples/widget_nodes/` (a date input reusing the text box, a
colour picker and a gauge with pages of their own). The base classes are
documented in `extension/jupyterlab_verknode/engine/widgets.py`.

## Python API (live workflows)

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

A `.vkf` file is a zip archive of JSON, Parquet and pickled port objects:

    workflow.json                       nodes (with their metanode), connections,
                                        workflow variables, annotations, metanodes
    workflowset.meta.json
    <Node Name> (#<id>)/settings.json   settings, flow variable bindings, state
    <Node Name> (#<id>)/port_<n>/data.parquet, spec.json   saved output tables
    <Node Name> (#<id>)/port_<n>/object.pkl  saved models / images / DB ports
    <Node Name> (#<id>)/view.html             saved node view

About 250 nodes, by category:

- **IO** – CSV, Excel, Parquet, JSON, XML, fixed-width, line and native
  table readers/writers, model and image writers, file handling (list,
  copy/move, delete, compress/decompress, folders, path variables).
- **Manipulation** – column filter/rename/convert, String Manipulation and
  Math Formula (expression languages), Rule Engine, Column Expression,
  splitters/combiners, binning, normalizer (with model port), moving
  aggregation; row filter/splitter, partitioning, sampling, pivot/unpivot,
  GroupBy, joiner, rank, transpose; Date&Time conversion/arithmetic.
- **Structured Data** – JSON Path, JSON to Table, Table to JSON, XPath.
- **Views** – bar, line, scatter, histogram, box, pie, heatmap, scatter
  matrix, density, parallel coordinates, table and image views
  (right-click › Open View, Shift+F10); charts also output an image port.
- **Analytics** – statistics, correlation, crosstab, hypothesis tests,
  scorers, ROC curve, cross-validation (X-Partitioner/X-Aggregator), and
  scikit-learn learner/predictor pairs (decision tree, random forest,
  gradient boosting, naive Bayes, linear/polynomial/logistic regression,
  SVM, MLP, kNN), k-Means, DBSCAN, hierarchical clustering, PCA.
- **DB** – an in-database framework on SQLite: connector, table
  selector, in-database row filter/GroupBy/joiner/sorter/query, reader,
  writer, insert/update/delete, SQL executor.
- **Flow Control** – counting, chunk, group, interval, column list, window,
  generic and recursive loops; IF/CASE/empty-table switches; Try/Catch error
  handling, breakpoint, wait; flow variable creation and conversion.
- **Scripting** – Python Script (polars; `chem` helpers when RDKit is present),
  Jupyter Notebook (see below).

Port types: black triangle = table, red circle = flow variables, blue
square = model, green square = image, dark red square = DB session, brown
square = DB data. Non-table port objects (models etc.) are pickled inside
the `.vkf`, so only open workflow files you trust.

Examples: `notebooks/examples/demo.vkf`, `chemistry.vkf`, `analytics.vkf`,
`custom_nodes.vkf`, `widgets.vkf`, `timing.vkf`.

### Jupyter Notebook node

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

### Chemistry (RDKit)

Molecules are a column type of their own ("RDKit Molecule", badge `M`),
stored as pickled RDKit molecules so they keep properties and conformers
through saves. Port views draw molecule columns as structures; the `⌬`
button on a String column header draws SMILES / MolBlock values too. RDKit
nodes accept a molecule column or a SMILES/MolBlock String column.

Nodes (Chemistry › RDKit): SDF Reader/Writer, RDKit From Molecule (SMILES,
SDF, SMARTS, InChI → molecule, with error port), RDKit To Molecule
(→ SMILES, MolBlock, SMARTS, InChI, InChIKey, formula), Descriptor
Calculation, Fingerprint, Substructure Filter, Similarity Search, Diversity
Picker, Structure Normalizer, Find Murcko Scaffolds, Generate Coordinates
(2D / 3D ETKDG + MMFF/UFF), One Component Reaction. CSV Writer writes
molecules as SMILES; the Python Script node has `chem` helpers
(`chem.to_mols(series)`, `chem.mol_series(name, mols)`) and `Chem`.

**Molecule Sketcher** (Chemistry › RDKit › IO) makes a table of sketches
(Index, Name, Molecule, SMILES, optional MolBlock) from structures drawn in
its dialog; they are stored as MolBlocks in the node settings (from Python:
`wf.add_node("Molecule Sketcher", molecules=[{"name": "Phenol", "smiles": "Oc1ccccc1"}])`).
With a table connected to its optional input (molecule or SMILES/MolBlock
column, optional name column) it edits that table instead: once the
upstream node has run, the dialog lists every input molecule with its
index, each can be edited in the sketcher, renamed or reset to the
original (or all at once with Reset all), and exactly the input rows come
out - edited or not - with the other input columns kept (optional). The
dialog shows which columns the structures and names come from and updates
the list as soon as another column is chosen; if that would discard edited
molecules or names, it asks first. `{"row": 2, ...}` in the settings is the
edit of input row 3.
"Draw structure…" opens the sketcher with two editors (Ketcher by default;
the dialog switches between them and remembers the last one; also in
≡ Settings):

- **Ketcher** (EPAM, standalone build with Indigo in the browser). The
  Dockerfile downloads it to `/opt/ketcher`; the server serves it at
  `<base_url>/verknode/ketcher/`.
- **Built-in sketcher** with common drawing conventions: fixed bond length, new
  bonds at 120° (zigzag), drag to draw in 15° steps or onto an atom; click
  a bond to cycle single → double → triple; double / triple / wedge / hash
  tools (wedge again flips it); chain tool (drag, bond count shown); 3–8
  rings and benzene on empty space, an atom or fused onto a bond (Kekulé
  structure kept valid); hover hotkeys on an atom `c n o s p f i h`,
  `l` = Cl, `b` = Br, `+` / `-` charge, on a bond `1 2 3`, `w` wedge,
  `h` hash, Delete removes what is under the pointer; double-click an atom
  to type a label (`NH2`, `O-`, `13C`); select tool to move / delete
  atoms; Clean (RDKit 2D layout), undo / redo, wheel zoom, middle-drag pan.
  Heteroatoms show their implicit hydrogens (`OH`, `H2N`), double bonds sit
  inside rings and are centred when terminal.

SMILES or MolBlock text can be pasted into the sketcher, and several SMILES
(one per line, optionally with a name) added at once in the node dialog.
Double-click a structure in the list to edit it; the sketcher shows the
molecule's index and name, and Previous / Next (PageUp / PageDown) go
through the list without closing it - changes are kept as you go, and
molecules you only look at stay unedited (compared by canonical SMILES).

Example: `notebooks/examples/chemistry.vkf`.

New nodes subclass `NodeModel` in `extension/jupyterlab_verknode/nodes/` and are
registered with `@register` (see `engine/node.py`).
