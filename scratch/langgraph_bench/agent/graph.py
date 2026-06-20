"""LangGraph ReAct loop, framed as supervisor + tool-worker.

Both nodes bind to MiniMax-M3 (per user requirement — no Claude Haiku
or other model substitutions). The "supervisor" is the LLM that decides
what to do next; the "tool-worker" executes MCP tool calls and returns
the results as ToolMessages.

Compression happens at the Headroom proxy layer when the supervisor's
messages (now containing large tool_result blocks) flow through it on
their way to MiniMax-M3. We don't compress in-process — that would
double-count savings.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from mcp import ClientSession

from .callbacks import UsageCapture
from .client import MODEL, SYSTEM_PROMPT, client
from .tools import execute_tool_call, fetch_anthropic_tools


# ---------------------------------------------------------------------------
# Message conversion: LangChain ↔ Anthropic
# ---------------------------------------------------------------------------

def _lc_to_anthropic(messages: list[Any]) -> list[dict]:
    """Convert LangChain messages to Anthropic's `messages` format.

    Anthropic expects:
      - `role: "user"` with `content: str | list[content_block]`
      - `role: "assistant"` with `content: list[content_block]`
      - Tool results travel as `role: "user"` content blocks of type
        `tool_result`, referencing the originating `tool_use_id`.
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            content = m.content
            if not isinstance(content, str):
                content = (
                    content if isinstance(content, list)
                    else [{"type": "text", "text": str(content)}]
                )
            out.append({"role": "user", "content": content})
            continue

        if isinstance(m, AIMessage):
            blocks: list[dict] = []
            # text content
            if isinstance(m.content, str) and m.content:
                blocks.append({"type": "text", "text": m.content})
            elif isinstance(m.content, list):
                for blk in m.content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        blocks.append({"type": "text", "text": blk.get("text", "")})
            # tool_use blocks
            for tc in (m.tool_calls or []):
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc.get("args") or {},
                })
            if not blocks:
                # Empty assistant message — skip; would be an API error
                continue
            out.append({"role": "assistant", "content": blocks})
            continue

        if isinstance(m, ToolMessage):
            content = m.content
            if not isinstance(content, str):
                content = str(content)
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": content,
                    # `is_error` flag is optional; we don't set it.
                }],
            })
            continue

        # Unknown message type — coerce to a user message
        out.append({"role": "user", "content": str(getattr(m, "content", ""))})

    return out


def _anthropic_response_to_ai(response) -> AIMessage:
    """Convert an Anthropic Message response to a LangChain AIMessage."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "args": dict(block.input) if block.input else {},
            })
        # ignore other block types (thinking, etc.)
    return AIMessage(
        content="\n".join(text_parts),
        tool_calls=tool_calls,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    case_id: str
    step: int
    # usage records are appended via the reducer below (concat, not replace)
    usage_records: Annotated[list[dict], lambda a, b: (a or []) + (b or [])]


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def _supervisor_node(
    anthropic_tools: list[dict],
    capture: UsageCapture,
):
    """Build the supervisor async node, closed over the tools + capture."""
    async def supervisor(state: AgentState) -> dict:
        c = client()
        anthropic_msgs = _lc_to_anthropic(state["messages"])

        # Tagging the request: the proxy forwards arbitrary x-* headers;
        # this lets us correlate per-case in the proxy's recent_requests.
        case_id = state.get("case_id", "")
        extra_headers = {
            "x-headroom-tags": f"case:{case_id}",
            "x-bench-case-id": case_id,
        }

        t0 = time.perf_counter()
        response = c.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=anthropic_msgs,
            tools=anthropic_tools,
            extra_headers=extra_headers,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        ai_msg = _anthropic_response_to_ai(response)
        rec = capture.record(
            case_id=case_id,
            step=state.get("step", 0),
            role="supervisor",
            model=MODEL,
            usage=response.usage,
            latency_ms=latency_ms,
        )

        return {
            "messages": [ai_msg],
            "step": state.get("step", 0) + 1,
            "usage_records": [rec.__dict__],
        }

    return supervisor


def _tool_worker_node(session: ClientSession):
    """Build the tool-worker async node, closed over the MCP session."""
    async def tool_worker(state: AgentState) -> dict:
        last = state["messages"][-1]
        # Coerce to AIMessage if it's a dict-like
        if not isinstance(last, AIMessage):
            return {"messages": []}
        if not last.tool_calls:
            return {"messages": []}

        out_messages: list[ToolMessage] = []
        for tc in last.tool_calls:
            content = await execute_tool_call(
                session,
                tc["name"],
                tc.get("args") or {},
            )
            out_messages.append(ToolMessage(
                content=content,
                tool_call_id=tc["id"],
            ))
        return {"messages": out_messages}

    return tool_worker


def _route_after_supervisor(state: AgentState) -> str:
    last = state["messages"][-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


async def build_graph(
    *,
    session: ClientSession,
    capture: UsageCapture,
) -> Any:
    """Compile a fresh StateGraph bound to the given MCP session and capture.

    Async because it needs to fetch the tool list from the MCP server
    before constructing the nodes. Caller is expected to be in an async
    context (the runner owns the event loop).
    """
    anthropic_tools = await fetch_anthropic_tools(session)

    workflow = StateGraph(AgentState)

    workflow.add_node("supervisor", _supervisor_node(anthropic_tools, capture))
    workflow.add_node("tools", _tool_worker_node(session))

    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        {"tools": "tools", END: END},
    )
    workflow.add_edge("tools", "supervisor")

    return workflow.compile()
