# Nodes and workflow files

## Nodes

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
  generic and recursive loops; parallel chunk loops (Parallel Chunk Start /
  End: the chunks go through the nodes between them at the same time, the
  visible nodes showing the first chunk, and a temporary metanode the others
  while it runs: one port brings the table in and branches out to a copy of the
  nodes per chunk; chunks run in threads, or in worker processes so that plain
  Python code also uses several cores, or Auto: measured on each run, with a note
  on Parallel Chunk End when the other way would be faster); IF/CASE/empty-table switches; Try/Catch error
  handling, breakpoint, wait; flow variable creation and conversion.
- **Scripting** – Python Script (polars; `chem` helpers when RDKit is present),
  Workflow Script (changes the workflow it is in), Jupyter Notebook (see [Python API and notebooks](python-api.md#jupyter-notebook-node)).

Port types: black triangle = table, red circle = flow variables, blue
square = model, green square = image, dark red square = DB session, brown
square = DB data. Non-table port objects (models etc.) are pickled inside
the `.vkf`, so only open workflow files you trust.

Examples: `notebooks/examples/demo.vkf`, `chemistry.vkf`, `analytics.vkf`,
`custom_nodes.vkf`, `widgets.vkf`, `timing.vkf`, `workflow_scripts.vkf`.

## Workflow files (.vkf)

A `.vkf` file is a zip archive of JSON, Parquet and pickled port objects:

    workflow.json                       nodes (with their metanode), connections,
                                        workflow variables, annotations, metanodes
    workflowset.meta.json
    <Node Name> (#<id>)/settings.json   settings, flow variable bindings, state
    <Node Name> (#<id>)/port_<n>/data.parquet, spec.json   saved output tables
    <Node Name> (#<id>)/port_<n>/object.pkl  saved models / images / DB ports
    <Node Name> (#<id>)/view.html             saved node view
