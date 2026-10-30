# The workflow editor

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
  settings (also in Settings › Settings Editor); the node defaults (setup
  that nodes share across workflows, such as your saved LLM servers, see
  [LLM nodes](llm.md#saved-llm-servers)); and the workflow's custom node
  folders.
- Traffic light: red = not configured, yellow = configured, green = executed.
- F6 (or Ctrl+Enter) configure, F7 execute (Shift: all), F8 reset (Shift: all), F9 cancel,
  Shift+F10 execute and open view, Del delete. Ctrl+S saves (including
  executed data).
- Ctrl+click the toolbar's ↺ Reset (or click it with nothing selected) to
  reset every node, after a confirmation.
- Double-click an output port (triangle, square or circle) to open its table
  / port object / flow variables (the context menu lists them too). Port
  tables and node views open in a separate browser window that can be moved
  and resized; hold Ctrl (Cmd) to open a tab instead. The windows update
  live when the workflow re-executes, and fall back to an in-editor dialog
  if the browser blocks pop-ups. They stay open when the node is reset: the
  last results remain visible (marked outdated) until it runs again. ≡
  Settings › "Open port and node views in" › JupyterLab tab opens them as
  JupyterLab tabs next to the editor (dock, split, drag like any notebook).
- **Inspect without opening.** Hover a port of an executed node and an
  inspection widget pops up after a moment, showing the first 25 rows of the
  data with the column names and the row count. On an input port, it shows
  the table that comes in, and its tail points at the output port it comes
  from. The tail is the same as a canvas widget's. A new inspection widget
  goes to a free spot near its port, clear of the nodes, the canvas widgets
  and the other inspection widgets where possible. It goes away when the pointer leaves the port and the widget.
  - Ctrl+click the port, or 📌 on the widget, to keep it.
  - Node numbers in chats (#7) do the same: hover one for the node's
    output beside the chat, click it to keep it. Each node shows its number
    in the corner of its icon.
  - **Inspect Outputs** in a node's context menu keeps one for each output
    of the selected nodes, beside them.
  - Kept widgets can be moved by their title bar, resized and closed (×);
    ⤢ opens the full view.
  - To close several at once, right-click one for **Close**, **Close Others**
    and **Close All Inspections**. A node's menu has **Close Inspections**
    for the selected nodes, and the canvas menu has **Close All
    Inspections**.
  - They show new results when a node runs again, and say so when it is
    reset. They are part of the editor, not of the workflow file.
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
  or no progress) to try this out: `notebooks/examples/loops_and_scripting/timing.vkf`.
- Settings can be bound to flow variables in the dialog's "Flow Variables"
  tab; workflow variables are set from the toolbar.
- **Tab** adds a node after the current one: a list of likely next nodes
  appears next to it (learned from what you added before, this workflow and
  common nodes; only nodes that accept the port's data). Type to narrow it
  down (also filters the Node Repository; acronyms like `gb` work), ↑ / ↓ to
  choose, Tab / Shift+Tab to pick another output port of the current node,
  Enter to add and connect, Esc to cancel. With nothing selected it suggests
  source nodes, and opens at the pointer: the new node goes there. The new node becomes current, so Tab again continues the chain.
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
- **Enter** shows or hides the selected nodes' canvas widgets. Showing makes
  the widgets follow the selection (a badge shows it): a minimized widget
  opens while its node is selected and minimizes again when the arrow keys
  move on, without changing the saved layout. Widgets that were already open
  stay open. A node without a canvas widget shows the inspection widget of
  its outputs instead, once executed. Enter again stops following; on an
  open widget it minimizes it. Clearing the selection stops following too.
- Once you navigate with the keyboard, the keys are listed at the bottom left
  of the canvas, until you use the mouse again.
- **Ctrl+Enter** (or F6) opens the selected node's dialog. In the dialog:
  - the first setting has the keyboard, and Tab / Shift+Tab go through the
    fields and buttons without leaving the dialog;
  - Enter in a field, or Ctrl+Enter anywhere (also in code), is OK; Escape
    cancels;
  - on the tabs (Settings, Flow Variables, Description), ← / → switch tabs
    and ↓ goes into the tab; Ctrl+PageDown / PageUp switch tabs from
    anywhere;
  - after closing, the keyboard is back on the node, so the arrow keys go
    on from there.
- Drag on the empty canvas pans the view (also Ctrl + drag or middle-drag);
  Shift + drag draws a selection rectangle (Ctrl + Shift + drag adds to the
  selection); Ctrl/Shift + click adds or removes a node from the selection.
  (≡ Settings can switch left-drag to selecting instead.) Ctrl + double-click on a node selects it and
  everything downstream, Shift + double-click everything upstream. Ctrl+A
  selects every node and widget on the canvas.
- **Layout ▾** (toolbar) tidies the canvas:
  - *Auto Layout* (Ctrl+Shift+L, also in the canvas' context menu) lays the
    workflow out from left to right along its connections. It orders the
    nodes of each column so that as few connections as possible cross, keeps
    group annotations and loop / try / switch blocks together, and puts each
    canvas widget beside its node (inputs before it, outputs after). With two
    or more nodes selected, only those are laid out, where they are. Ties go
    to the current arrangement, so a tidy workflow stays much as it was.
    Widgets anchored to the canvas stay put.
  - *Align Left / Centers / Right / Top / Middles / Bottom* lines up the
    selected nodes' icons; *Distribute Horizontally / Vertically* spaces
    three or more evenly. The node context menu has the same items when
    several nodes are selected.
  - Each is one step to undo (Ctrl+Z).
- Ctrl+C / Ctrl+X / Ctrl+V copy, cut and paste nodes with their settings and
  the connections between them (also between workflows); Ctrl+Z undo,
  Ctrl+Shift+Z / Ctrl+Y redo (edits only; execution is not undone, and
  results of nodes unaffected by an undo are kept).
