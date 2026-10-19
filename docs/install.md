# Installing verknode

## Distributions

| | What | For |
|---|---|---|
| **Docker, pip** (`docker/dist/Dockerfile`) | `python:3.13-slim`, JupyterLab and verknode from pip. No conda, and no Node.js in the image. | **Using verknode.** Smaller and simpler; choose what is installed with `EXTRAS`. |
| **Docker, development** (`docker/Dockerfile`, `docker/compose.yml`) | `quay.io/jupyter/scipy-notebook` (conda), with the extension source mounted and installed in development mode | Working on verknode ([development](development.md), [deployment](deployment.md)) |
| **In the browser** (`docker/lite/Dockerfile`, `make lite`) | A JupyterLite site: static files, no server; the engine runs in a Pyodide kernel in the page | Trying verknode, teaching, sharing workflows: open a link ([what works there](jupyterlite.md)) |
| **pip** | `pip install "./extension[all]"` into your own JupyterLab 4 (building from source needs Node.js) | An existing Python / JupyterLab setup |

### Docker, pip

From the repository root:

    docker build -f docker/dist/Dockerfile -t verknode .                         # everything
    docker build -f docker/dist/Dockerfile --build-arg EXTRAS= -t verknode-core .   # the core only
    docker build -f docker/dist/Dockerfile --build-arg EXTRAS=chem,formats -t verknode-chem .

    docker run --rm -p 8888:8888 -e JUPYTER_TOKEN=a-long-secret \
        -v "$PWD/work:/home/jovyan/work" verknode

JupyterLab is at `http://localhost:8888/lab?token=a-long-secret`. The
examples are in `examples/`. Keep your work in a mounted folder (`work/`
above), because the container's own files go when it does. The image runs
as user `jovyan` (uid 1000), and Ketcher for the Molecule Sketcher is
included.

The build has two stages:
1. Node.js builds the frontend and the wheel.
2. The image installs that wheel with JupyterLab and the chosen extras.
   It adds only the system libraries RDKit's drawing needs.

**The core variant has no compromise for the nodes it has.** It leaves out
the packages of the extras below, and the editor shows the nodes that need
them dimmed, with the package they need.

## What the nodes need

The core is JupyterLab, polars and numpy. It covers the editor, the engine,
workflow agents and MCP, exports, and 268 of the nodes:
- reading and writing CSV, Parquet, text files, databases (SQLite);
- rows, columns, dates, strings, joins, pivots, group-bys, rules;
- loops, switches, error handling, flow variables, time loops;
- widgets and charts;
- the LLM and Ollama nodes, which talk to their server over HTTP;
- the web-service nodes (HTTP with the standard library), the GIS nodes,
  Map Display and the Mermaid diagrams;
- the LLM connectors (but the local llama.cpp one), the Data Query Agent,
  Agent Runner and the evaluation nodes.

The rest come with extras (`pip install "jupyterlab_verknode[chem,ml]"`,
`EXTRAS=` in Docker):

| Extra | Packages | Nodes |
|---|---|---|
| `chem` | rdkit | **Chemistry / RDKit**: Molecule Sketcher, Descriptor Calculation, Diversity Picker, Find Murcko Scaffolds, Fingerprint, From / To Molecule, Generate Coordinates, One Component Reaction, Similarity Search, Structure Normalizer, Substructure Filter, SDF Reader / Writer, and the Molecule Input widget. **3D structures**: NGL / Mol* Structure View and Display. Molecules as a column type. |
| `ml` | scikit-learn, scipy, statsmodels, pandas, matplotlib | **Analytics / Mining**: Decision Tree, Random Forest, Gradient Boosted Trees, Logistic / Linear / Polynomial Regression, Naive Bayes, SVM, K Nearest Neighbor, RProp MLP and Simple Regression Tree learners, k-Means, DBSCAN, Hierarchical Clustering, PCA, ROC Curve. **Analytics / Statistics**: Statistics, Crosstab, Linear / Rank Correlation, t-tests, One-way ANOVA, Kruskal-Wallis, Shapiro-Wilk. **Views**: Density Plot. |
| `formats` | fastexcel, xlsxwriter, openpyxl, lxml, jsonpath-ng | Excel Reader / Writer, Read Excel Sheet Names, XML Reader, XPath, JSON Reader, JSON Path |
| `notebooks` | nbclient, nbformat, ipykernel | Jupyter Notebook (runs a notebook as a node, on a kernel) |
| `agents` | langgraph, deepagents | LangGraph Agent, Deep Agent |
| `local-llm` | llama-cpp-python (builds llama.cpp: needs a C++ compiler; not in `all`) | Local Model (llama.cpp) |
| `all` | all of the above but `local-llm` | |

**Optional packages.** Some nodes use a package if it is there and work
without it. Python Script and Workflow Script offer `chem` helpers with
RDKit, Jupyter Notebook uses nbconvert for its view, and HTTP Request uses
requests (else the standard library).

**In the editor**, every node's description lists what it needs, with ✓ for
installed and ✗ for missing. A node whose packages are missing is dimmed in
the Node Repository, and its tooltip gives the `pip install` command. Nodes
of a module that cannot load at all are not listed, for example the RDKit
nodes without RDKit.

The list comes from the nodes' code (`engine/requirements.py`):
- the imports in the node's class, in the module functions it uses, and of
  its module;
- imports inside `try / except ImportError`, which count as optional;
- for packages the code does not show, such as a notebook's kernel, a
  node's `requires` list.

Exports go further: they measure what a workflow imports when it runs
([export](export.md)).
