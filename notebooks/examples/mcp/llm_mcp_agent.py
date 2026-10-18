"""Any LLM as a verknode agent, through MCP: the model gets the MCP server's tools and answers a question
by building, running and checking a workflow.

    python notebooks/examples/mcp/llm_mcp_agent.py "Which product sold the most units in each region?"
    python notebooks/examples/mcp/llm_mcp_agent.py --http http://localhost:8888/verknode/mcp "..."

stdio (default): a headless verknode MCP server on notebooks/examples. --http: the MCP endpoint of a
running JupyterLab (token: JUPYTER_TOKEN) - open the workflow there and watch the nodes appear.
The model: VERKNODE_LLM_URL / VERKNODE_LLM_MODEL (Ollama or OpenAI-compatible, as the LLM nodes use).
"""
import argparse
import json
import os
import sys
import time

from mcp_client import MCPClient

from jupyterlab_verknode.nodes.llm import chat_tools, server, tool_message

HERE = os.path.dirname(os.path.abspath(__file__))

TASK = """{question}

Answer it with a verknode workflow: build it as {workflow} (data: list_files), run it, check the result
with get_table, save it, then answer me in two or three sentences with the numbers."""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("question", nargs="?", default="Which product sold the most units in each region (agent/sales.csv)?")
    p.add_argument("--workflow", default="mcp/top_products.vkf")
    p.add_argument("--http", help="the MCP URL of a running JupyterLab (live)")
    p.add_argument("--steps", type=int, default=60)
    a = p.parse_args()
    if a.http:
        mcp = MCPClient.http(a.http, token=os.environ.get("JUPYTER_TOKEN"))
    else:
        mcp = MCPClient.stdio(sys.executable, "-m", "jupyterlab_verknode.mcp_server", "--root", os.path.dirname(HERE))
    tools = [{"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]} for t in mcp.tools]
    srv = server({})
    settings = {"temperature": 0, "max_tokens": 2048, "timeout": 300}
    messages = [{"role": "system", "content": "You are a data analyst agent. " + mcp.instructions},
                {"role": "user", "content": TASK.format(question=a.question, workflow=a.workflow)}]
    print(f"model: {srv['model']} | {len(tools)} tools from {mcp.server.get('name')} | task: {a.question}\n")
    t0, nudges = time.time(), 0
    for step in range(a.steps):
        r = chat_tools(srv, messages, settings, tools)
        messages.append(r["message"])
        if not r["calls"]:
            if not r["text"].strip() and nudges < 2:  # (it stopped without a word: it goes on)
                nudges += 1
                messages.append({"role": "user", "content": "Go on: finish the workflow with the tools, then answer."})
                continue
            print(f"\nANSWER ({step} steps, {time.time() - t0:.0f} s):\n{r['text'].strip()}")
            break
        for call in r["calls"]:
            out = mcp.call(call["name"], call["args"])
            text = out["content"][0]["text"]
            flag = " ERROR" if out.get("isError") else ""
            print(f"- {call['name']}({json.dumps(call['args'])[:150]}){flag}: {text.splitlines()[0][:110] if text else ''}")
            messages.append(tool_message(srv, call, text))
    mcp.close()


if __name__ == "__main__":
    main()
