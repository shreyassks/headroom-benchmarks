"""Customer-support tickets MCP server.

Run as: `python -m scratch.langgraph_bench.mcp_server.server`
(or invoked by langchain-mcp-adapters via stdio)

The server exposes five SQLite-backed tools. The output of each tool
is a JSON-encoded string — JSON because:
  1. It round-trips cleanly through the LLM's context (parsed without ambiguity),
  2. Headroom's SmartCrusher has unambiguous text to crush (no Markdown noise).

Compression happens at the *proxy* layer (when the LLM message containing
the tool result is sent through the proxy on its way to MiniMax-M3) —
the server itself just returns raw results.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import mcp.server.stdio
from mcp.server import Server
from mcp.types import TextContent, Tool

DB_PATH = Path(__file__).parent.parent / "db" / "tickets.db"

app = Server("tickets-mcp")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"tickets.db not found at {DB_PATH}. "
            "Run `uv run python scratch/langgraph_bench/db/seed.py` first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ticket_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _find_ticket(ticket_id: int) -> str:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        return json.dumps({"found": False, "ticket_id": ticket_id})
    return json.dumps({"found": True, "ticket": _ticket_to_dict(row)})


def _search_tickets(
    query: str,
    status: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> str:
    limit = max(1, min(limit, 100))
    where = ["(subject LIKE ? OR body LIKE ?)"]
    args: list[Any] = [f"%{query}%", f"%{query}%"]
    if status:
        where.append("status = ?")
        args.append(status)
    if category:
        where.append("category = ?")
        args.append(category)
    sql = (
        "SELECT * FROM tickets WHERE "
        + " AND ".join(where)
        + " ORDER BY created_at DESC LIMIT ?"
    )
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return json.dumps({
        "query": query,
        "filters": {"status": status, "category": category},
        "count": len(rows),
        "tickets": [_ticket_to_dict(r) for r in rows],
    })


def _list_recent_tickets(
    days: int = 7,
    status: str | None = None,
    limit: int = 50,
) -> str:
    limit = max(1, min(limit, 200))
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    where = ["created_at >= ?"]
    args: list[Any] = [cutoff]
    if status:
        where.append("status = ?")
        args.append(status)
    sql = (
        "SELECT * FROM tickets WHERE "
        + " AND ".join(where)
        + " ORDER BY created_at DESC LIMIT ?"
    )
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return json.dumps({
        "days": days,
        "since": cutoff,
        "filters": {"status": status},
        "count": len(rows),
        "tickets": [_ticket_to_dict(r) for r in rows],
    })


def _aggregate_tickets(
    group_by: str = "category",
    status: str | None = None,
    category: str | None = None,
    customer_tier: str | None = None,
    since: str | None = None,
) -> str:
    """GROUP BY one of: category, status, priority, customer_tier."""
    allowed = {"category", "status", "priority", "customer_tier"}
    if group_by not in allowed:
        return json.dumps({
            "error": f"group_by must be one of {sorted(allowed)}, got {group_by!r}",
        })
    where = ["1=1"]
    args: list[Any] = []
    if status:
        where.append("status = ?")
        args.append(status)
    if category:
        where.append("category = ?")
        args.append(category)
    if customer_tier:
        where.append("customer_tier = ?")
        args.append(customer_tier)
    if since:
        where.append("created_at >= ?")
        args.append(since)
    sql = (
        f"SELECT {group_by} AS bucket, COUNT(*) AS n "
        f"FROM tickets WHERE {' AND '.join(where)} "
        f"GROUP BY {group_by} ORDER BY n DESC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return json.dumps({
        "group_by": group_by,
        "filters": {
            "status": status,
            "category": category,
            "customer_tier": customer_tier,
            "since": since,
        },
        "buckets": [{group_by: r["bucket"], "count": r["n"]} for r in rows],
    })


def _customer_history(customer_id: int) -> str:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE customer_id = ? ORDER BY created_at DESC LIMIT 200",
            (customer_id,),
        ).fetchall()
    return json.dumps({
        "customer_id": customer_id,
        "ticket_count": len(rows),
        "tickets": [_ticket_to_dict(r) for r in rows],
    })


# ---------------------------------------------------------------------------
# MCP schema
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="find_ticket",
            description=(
                "Fetch a single ticket by its numeric ID. Returns a small JSON "
                "object with the full ticket record or {found: false} if no "
                "such ticket exists."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer", "minimum": 1, "description": "Numeric ticket ID"},
                },
                "required": ["ticket_id"],
            },
        ),
        Tool(
            name="search_tickets",
            description=(
                "Full-text search tickets whose subject OR body contains the "
                "given query string (case-insensitive substring match). "
                "Optionally filter by status and/or category. Returns up to "
                "`limit` tickets (default 20, max 100), newest first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":     {"type": "string", "description": "Substring to search in subject and body"},
                    "status":    {"type": "string", "enum": ["open", "in_progress", "resolved", "closed"]},
                    "category":  {"type": "string", "enum": ["billing", "technical", "account", "shipping", "feature_request", "other"]},
                    "limit":     {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_recent_tickets",
            description=(
                "List tickets created within the last `days` days (default 7), "
                "optionally filtered by status. Returns up to `limit` tickets "
                "(default 50, max 200), newest first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days":   {"type": "integer", "minimum": 1, "maximum": 365, "default": 7},
                    "status": {"type": "string", "enum": ["open", "in_progress", "resolved", "closed"]},
                    "limit":  {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="aggregate_tickets",
            description=(
                "Aggregate tickets by one of {category, status, priority, "
                "customer_tier}. Returns bucket counts, sorted descending. "
                "Optional filters narrow the population before aggregation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_by":      {"type": "string", "enum": ["category", "status", "priority", "customer_tier"], "default": "category"},
                    "status":        {"type": "string", "enum": ["open", "in_progress", "resolved", "closed"]},
                    "category":      {"type": "string", "enum": ["billing", "technical", "account", "shipping", "feature_request", "other"]},
                    "customer_tier": {"type": "string", "enum": ["free", "plus", "premium", "enterprise"]},
                    "since":         {"type": "string", "description": "ISO date lower bound on created_at"},
                },
                "required": ["group_by"],
            },
        ),
        Tool(
            name="customer_history",
            description=(
                "Fetch all tickets filed by a single customer_id, newest "
                "first. Useful for 'show everything customer X has reported'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer", "minimum": 1},
                },
                "required": ["customer_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "find_ticket":
            out = _find_ticket(int(arguments["ticket_id"]))
        elif name == "search_tickets":
            out = _search_tickets(
                query=arguments["query"],
                status=arguments.get("status"),
                category=arguments.get("category"),
                limit=int(arguments.get("limit", 20)),
            )
        elif name == "list_recent_tickets":
            out = _list_recent_tickets(
                days=int(arguments.get("days", 7)),
                status=arguments.get("status"),
                limit=int(arguments.get("limit", 50)),
            )
        elif name == "aggregate_tickets":
            out = _aggregate_tickets(
                group_by=arguments["group_by"],
                status=arguments.get("status"),
                category=arguments.get("category"),
                customer_tier=arguments.get("customer_tier"),
                since=arguments.get("since"),
            )
        elif name == "customer_history":
            out = _customer_history(int(arguments["customer_id"]))
        else:
            out = json.dumps({"error": f"unknown tool: {name}"})
    except Exception as exc:  # noqa: BLE001 — surface to LLM as data, not crash
        out = json.dumps({"error": str(exc), "tool": name, "arguments": arguments})
    return [TextContent(type="text", text=out)]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
