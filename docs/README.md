# verknode documentation

- [The workflow editor](editor.md) - building and running workflows: nodes,
  ports, keys, views, themes, run times, scopes, groups, metanodes.
- [Nodes and workflow files](nodes.md) - the node categories, port types
  and the `.vkf` format.
- [Canvas widgets](widgets.md) - inputs and displays used directly on the
  canvas, and how to make new ones.
- [Web services, GIS and diagrams](web-gis-diagrams.md) - curl and HTTP
  requests, Wikipedia, PubMed, OpenStreetMap, weather, podcasts and feeds,
  ChEMBL, PDB, UCSC, Ensembl, Gene Ontology; GeoJSON, distances and spatial
  joins; maps and Mermaid diagrams on the canvas.
- [Custom nodes](custom-nodes.md) - nodes as Python classes next to a workflow.
- [Python API and notebooks](python-api.md) - controlling live workflows
  from Python, and the Jupyter Notebook node.
- [Chemistry](chemistry.md) - RDKit molecules, the RDKit nodes and the
  Molecule Sketcher.
- [LLM nodes](llm.md) - prompts to Ollama or OpenAI-compatible servers,
  one per row, and managing Ollama's models.
- [Building and evaluating agents](agents-build-evaluate.md) - data query
  agents with verknode's tool loop, LangGraph or Deep Agents; rubric-based
  evaluation, hill climbing, multi-arm model comparison, ablation studies.
- [Export](export.md) - workflows (or groups of them) as standalone scripts
  or self-contained folders with minimal requirements, a Dockerfile and an
  HTTP API; headless exports without widgets.
- [Agents: API and MCP server](agents.md) - tools for agents (Claude Code,
  Claude Desktop, any LLM) to build, run and read workflows; workflows as
  reusable tools.
- [Installing](install.md) - the pip-based Docker image (all extras, or a
  smaller core), pip, and what each node needs.
- [Deployment](deployment.md) - running it locally, on a network, behind
  Traefik or another reverse proxy.
- [Development](development.md) - working on the extension itself.
