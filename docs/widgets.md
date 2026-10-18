# Canvas widgets

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
- **Chat** (Displays) – a chat on the canvas, run in a loop: it shows the
  conversation (your messages right, the answers left, with lists, `code`
  and code blocks) and waits, while the loop runs, for the message typed
  into it (Enter sends, Shift+Enter: a new line); End chat stops the loop,
  Start chat / New chat runs it. The Chat Loop Start has the same chat on
  it ([LLM nodes](llm.md#a-chatbot)).
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
- **Selecting and anchoring** - the marquee (Shift+drag on the canvas)
  selects widgets as well as nodes; Ctrl/Shift+click on a title bar adds or
  removes one; selecting nodes selects their widgets too. Dragging a selected
  widget moves all selected widgets, and dragging selected nodes takes their
  moved widgets along. The anchor button on the title bar (grey while free)
  pins a widget where it is: it is no longer dragged, moved with its node or
  selected with it. With several widgets selected, the anchor button sets
  them all to the same state.
- **Docking widgets together** - a dragged widget snaps to the others when
  it comes close: next to one (touching) or with an edge in line with one;
  a widget being resized snaps its right / bottom edge to the other widgets'
  edges (e.g. to the width of the one above). Widgets that touch form a
  group: dragging the group's top widget moves the whole group, dragging any
  other member takes it out of the group. Hovering a title bar shows what a
  drag would do: one outline around everything that would move (a whole
  group when it is the top widget's, that widget alone otherwise) and a
  dotted one around the rest of its group, which would stay; the tooltip
  says so too. When a group moves, whichever of its widgets comes close to
  another widget docks it. A widget still beside its node joins a group
  when one docks to it: it is pinned where it is, so it stays with the group
  when its node moves. Anchored widgets are snapped to but never move.
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

    # vkn_widget_nodes/my_widgets.py
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
`notebooks/examples/vkn_widget_nodes/` (a date input reusing the text box, a
colour picker and a gauge with pages of their own). The base classes are
documented in `extension/jupyterlab_verknode/engine/widgets.py`.
