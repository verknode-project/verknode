"""An agent's session with the verknode MCP server, step by step (no LLM: the calls an agent makes).

    python notebooks/examples/agents/mcp/mcp_demo.py

Use case: "Which regions are below their sales target?" - asked every month, with different
thresholds. Instead of answering with a one-off script, the agent builds a workflow once:
  1. it finds node types and reads their help,
  2. builds sales -> revenue -> per region -> join targets -> % of target -> below threshold,
     with the threshold as a workflow variable,
  3. runs it, checks the result, and saves it (regional_check.vkf: open it in JupyterLab to see,
     review and change every step),
and then - the same day, or another agent next month - uses it as a tool: run_workflow with
threshold=90 answers from the reviewed workflow, without changing it.

mcp_client.MCPClient is a minimal MCP client (JSON-RPC over stdio); any MCP client works the same way (Claude Code, Claude Desktop, agent frameworks).
"""
import json
import os
import sys

from mcp_client import MCPClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # notebooks/examples/agents: the workspace
WORKFLOW = "mcp/regional_check.vkf"


def call(c, name, **args):
    """A tool call, printed as an agent's transcript."""
    r = c.call(name, args)
    text = r["content"][0]["text"]
    print(f"\n>>> {name}({', '.join(f'{k}={json.dumps(v)}' for k, v in args.items())})")
    print(("ERROR: " if r.get("isError") else "") + text[:1500] + (" …" if len(text) > 1500 else ""))
    if r.get("isError"):
        raise SystemExit(f"{name} failed")
    return r.get("structuredContent") or {}


def main():
    c = MCPClient.stdio(sys.executable, "-m", "jupyterlab_verknode.mcp_server", "--root", ROOT)
    print(f"{len(c.tools)} tools:", ", ".join(t["name"] for t in c.tools))

    # 1. look around: data files, node types and their settings
    call(c, "list_files", folder="agent")
    call(c, "find_node_types", query="join two tables")
    call(c, "node_type_help", types=["CSV Reader", "Math Formula", "GroupBy", "Joiner", "Row Filter"])

    # 2. build
    call(c, "create_workflow", workflow=WORKFLOW, overwrite=True)
    w = {"workflow": WORKFLOW}
    call(c, "set_variables", **w, variables={"threshold": 200.0})
    sales = call(c, "add_node", **w, type="CSV Reader", label="Sales", settings={"path": "../data/sales.csv"})["id"]
    revenue = call(c, "add_node", **w, type="Math Formula", label="Revenue", after=sales,
                     settings={"expression": "$amount$ * $quantity$", "new_column": "revenue"})["id"]
    call(c, "connect", **w, from_node=sales, to_node=revenue)
    per_region = call(c, "add_node", **w, type="GroupBy", label="Revenue per region", after=revenue, settings={
        "groups": ["region"], "aggregations": [{"column": "revenue", "method": "Sum", "name": "revenue"}]})["id"]
    call(c, "connect", **w, from_node=revenue, to_node=per_region)
    targets = call(c, "add_node", **w, type="CSV Reader", label="Targets", settings={"path": "../data/regions.csv"})["id"]
    joined = call(c, "add_node", **w, type="Joiner", label="With targets", after=per_region,
                    settings={"keys": [{"left": "region", "right": "region"}]})["id"]
    call(c, "connect", **w, from_node=per_region, to_node=joined, to_port=1)
    call(c, "connect", **w, from_node=targets, to_node=joined, to_port=2)
    pct = call(c, "add_node", **w, type="Math Formula", label="% of target", after=joined,
                 settings={"expression": "round($revenue$ / $target$ * 100, 1)", "new_column": "pct_of_target"})["id"]
    call(c, "connect", **w, from_node=joined, to_node=pct)
    below = call(c, "add_node", **w, type="Row Filter", label="Below threshold", after=pct,
                   settings={"column": "pct_of_target", "operator": "<"}, variables={"value": "threshold"})["id"]
    call(c, "connect", **w, from_node=pct, to_node=below)

    # 3. run, check, save
    call(c, "run", **w)
    call(c, "get_table", **w, node=pct)
    call(c, "get_table", **w, node="Below threshold")
    call(c, "save_workflow", **w)

    # 4. later: the workflow as a tool - other thresholds, the workflow itself does not change
    for threshold in (150.0, 250.0):
        call(c, "run_workflow", **w, variables={"threshold": threshold}, outputs=["Below threshold"])
    c.close()


if __name__ == "__main__":
    main()
