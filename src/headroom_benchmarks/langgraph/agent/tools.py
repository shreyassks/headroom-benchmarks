"""MCP ↔ LangGraph / Anthropic tool bridging.

The MCP server runs as a subprocess over stdio. We hold one connection
for the lifetime of the agent run and reuse it for every tool call.

Two responsibilities:
  1. Convert MCP `Tool` objects to Anthropic's `tools=[...]` schema
     (so `client.messages.create(tools=[...])` accepts them directly).
  2. Execute a tool call: route it to the MCP server and return a
     string suitable for inclusion in a `ToolMessage.content`.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# Repo root, computed from this file's path. Used as the MCP server's
# working directory so it can resolve `headroom_benchmarks.langgraph.mcp_server.server`.
# Layout: tools.py → agent/ → langgraph_bench/ → scratch/ → REPO_ROOT
REPO_ROOT = Path(__file__).resolve().parents[4]


def server_command() -> list[str]:
    """How to launch the MCP server as a subprocess."""
    return [sys.executable, "-m", "headroom_benchmarks.langgraph.mcp_server.server"]


@asynccontextmanager
async def mcp_session() -> AsyncIterator[ClientSession]:
    """Yield a connected MCP ClientSession. Caller is responsible for
    running `await session.initialize()` before use."""
    params = StdioServerParameters(
        command=server_command()[0],
        args=server_command()[1:],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def mcp_tool_to_anthropic(tool) -> dict:
    """Convert an MCP `Tool` to Anthropic's `tools=[...]` entry.

    Anthropic's expected shape (as of the SDK used here):
      {
        "name": str,
        "description": str,
        "input_schema": {   # JSON Schema object
          "type": "object",
          "properties": {...},
          "required": [...]
        }
      }

    MCP `Tool.inputSchema` is already a JSON Schema object, so the
    shape maps cleanly.
    """
    schema = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": schema,
    }


async def fetch_anthropic_tools(session: ClientSession) -> list[dict]:
    """Pull tools from MCP and convert to Anthropic format."""
    listed = await session.list_tools()
    return [mcp_tool_to_anthropic(t) for t in listed.tools]


async def execute_tool_call(session: ClientSession, name: str, arguments: dict) -> str:
    """Invoke a tool on the MCP server; return a string suitable for ToolMessage.content.

    The MCP server already returns JSON-encoded strings; we pass them through.
    On error, we surface the exception as JSON so the LLM sees it as data.
    """
    try:
        result = await session.call_tool(name, arguments or {})
        # result.content is a list of content blocks; we expect TextContent
        if result.content and getattr(result.content[0], "text", None) is not None:
            return result.content[0].text
        # Fallback: serialize whatever the server returned
        return json.dumps({
            "tool": name,
            "arguments": arguments,
            "raw": [c.model_dump() for c in result.content],
        })
    except Exception as exc:  # noqa: BLE001 — surface to LLM, don't crash the graph
        return json.dumps({
            "tool": name,
            "arguments": arguments,
            "error": str(exc),
        })
