"""Anthropic client wrapper for the LangGraph agent.

Single point of contact with the LLM. All nodes (supervisor, any
future worker) bind to this same client and use the same model —
**MiniMax-M3, never Claude Haiku, never any other model**. This
keeps cost/pricing consistent across the savings analysis.

The client is pointed at the local Headroom proxy (`ANTHROPIC_BASE_URL`,
default :8787). Compression of large tool results happens at that proxy
layer when LLM messages flow through it.
"""

from __future__ import annotations

import os
from functools import lru_cache

import anthropic

# Hardcoded — never substitute a different model here. The user requested
# MiniMax-M3 for ALL agent nodes; mixing in Haiku for "easy" subtasks
# would muddy the cost comparison.
MODEL = "MiniMax-M3"

DEFAULT_PROXY_URL = "http://127.0.0.1:8787"
DEFAULT_MAX_TOKENS = 2048


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    base_url = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_PROXY_URL)
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY is not set; cannot construct Anthropic client")
    return anthropic.Anthropic(
        base_url=base_url,
        api_key=api_key,
        # Pass case-id headers through; the proxy reads x-* request headers
        # and forwards them to upstream (with the transforms header isolated
        # as a comma-joined tag list).
        default_headers={},
    )


SYSTEM_PROMPT = (
    "You are a customer support analyst with access to a database of 2,500 "
    "tickets via the tools provided. Always use tools to answer questions "
    "rather than guessing or making up ticket data. Be concise — one to "
    "three sentences in your final answer unless asked for detail. When "
    "you have enough information, respond directly without invoking more "
    "tools. Use only the data the tools return."
)
