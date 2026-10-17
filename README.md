<p align="center">
  <img src="docs/assets/verknode_logo_cropped.svg" alt="verknode" width="360">
</p>

<p align="center">
  <b>Visual, node-based data workflows for Python, inside JupyterLab.</b>
</p>

Build data pipelines by connecting nodes on a canvas: read files, clean and
reshape tables, run statistics and machine learning, draw charts, work with
molecules - and see every intermediate result. Tables flow between nodes as
[polars](https://pola.rs) DataFrames, workflows are saved with their data,
and everything stays scriptable from Python.

![Canvas widgets: a what-if with a price slider and a bar chart you can click, and a signal generator driven by number inputs](docs/images/widgets-v2.png)

## Highlights

- **About 250 nodes** for files, tables, dates, JSON / XML, statistics,
  scikit-learn models, databases, charts, loops, switches and error
  handling ([nodes](docs/nodes.md)).
- **Widgets on the canvas** - sliders, inputs and editable tables next to
  their nodes; tables and charts that update as you change them, and whose
  selections feed the rest of the workflow ([widgets](docs/widgets.md)).
- **See it run** - status lights, time bars with estimates, progress and the
  expected end of every node, loop iteration counters
  ([editor](docs/editor.md)).
- **Python all the way** - write nodes as Python classes, run notebooks as
  nodes, drive an open workflow from a notebook, export a workflow as a
  script ([custom nodes](docs/custom-nodes.md),
  [Python API](docs/python-api.md)).
- **Chemistry built in** - RDKit molecules as a column type, drawn as
  structures, with a structure sketcher ([chemistry](docs/chemistry.md)).

## A quick look

**Build workflows** from the node repository, with loops, switches and
groups outlined on the canvas:

![The workflow editor with the node repository and a workflow with a loop and an IF switch](docs/images/editor-v2.png)

**Watch them run**: every node shows its run time, progress and when it
should be done; loops count their iterations:

![A running workflow: time bars, a loop at iteration 3 of 6, queued nodes with their expected end](docs/images/timing-v2.png)

**Look at any result**, including molecules drawn as structures:

![A table of molecules with their structures](docs/images/chemistry-v2.png)

## Quick start

With Docker:

    git clone https://github.com/verknode-project/verknode.git
    cd verknode/docker
    make build      # creates docker/credentials_norepo.env - set JUPYTER_TOKEN there
    make up         # http://127.0.0.1:8888/lab?token=<JUPYTER_TOKEN>

Open `notebooks/examples/demo.vkf` (more examples next to it:
`widgets.vkf`, `timing.vkf`, `analytics.vkf`, `chemistry.vkf`,
`custom_nodes.vkf`), or create a workflow from the Launcher.

Other setups (network, reverse proxy, without Docker):
[deployment](docs/deployment.md).

## Documentation

See [docs/](docs/README.md): the [editor](docs/editor.md),
[nodes](docs/nodes.md), [widgets](docs/widgets.md),
[custom nodes](docs/custom-nodes.md), [Python API](docs/python-api.md),
[chemistry](docs/chemistry.md), [deployment](docs/deployment.md) and
[development](docs/development.md).

## License

verknode is licensed under the [Apache License 2.0](LICENSE).
