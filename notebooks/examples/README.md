# Examples

Start with **`demo.vkf`**. It's a small tour of the editor: a table, cleaning,
rules, a loop, a switch and a CSV file written to `output/`.

The other examples are grouped by topic. Each folder has the data its
workflows read (`data/`), and they write their results to `output/` there.

| Folder | Workflows | What they show |
|---|---|---|
| `analytics/` | `analytics.vkf` | statistics, machine learning (random forest, k-means), a SQLite database, loops, try/catch |
| `loops_and_scripting/` | `time_loops.vkf`, `timing.vkf`, `workflow_scripts.vkf` | timed and interval loops, execution timing and progress, Workflow Script nodes that edit and run the workflow |
| `widgets_and_diagrams/` | `widgets.vkf`, `mermaid.vkf` | inputs, charts and displays on the canvas, a widget node of your own (`vkn_widget_nodes/`), Mermaid diagrams |
| `web_and_maps/` | `web_services.vkf`, `gis.vkf` | web services (Wikipedia, OpenStreetMap, weather, feeds, HTTP), GIS nodes and the map display |
| `life_science/` | `chemistry.vkf`, `structures.vkf`, `life_science_web.vkf` | RDKit nodes, 3D structures (PDB 1HSG with indinavir poses), Ensembl, ChEMBL, PDB, PubMed |
| `llm/` | `llm.vkf`, `llm_connections.vkf`, `chatbot.vkf`, `ollama.vkf` | LLM Prompter, the LLM connectors (Ollama, inference servers, APIs, a local model), chats, managing Ollama |
| `agents/` | `workflow_agent.vkf`, `data_chat.vkf`, `coding_agent.vkf`, `data_query_agent.vkf`, `agent_improvement.vkf` (+ `.ipynb`), `llm_harness.vkf`, `mcp/` | a chat that builds the workflow, questions about the results, a coding agent, data query agents (LangGraph, Deep Agents), evaluation and improvement, an LLM harness, agents over MCP |
| `custom_nodes/` | `custom_nodes.vkf` | nodes of your own, in a folder next to the workflow (`vkn_custom_nodes/`) |

The LLM and agent examples need a model. Connect an LLM connector node to a
node's LLM port, set the node's Model settings, or set `VERKNODE_LLM_URL`
and `VERKNODE_LLM_MODEL` for the Jupyter server (see `docs/llm.md`).
