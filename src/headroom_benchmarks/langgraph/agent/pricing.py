"""LiteLLM pricing for `minimax/MiniMax-M3`.

Mirrors the rates that the proxy uses for its dashboard, so the
runner-side cost calculation agrees with what the proxy reports.

Source: `uv run python -c "import litellm; print(litellm.model_cost['minimax/MiniMax-M3'])"`

  input_cost_per_token          : 6e-07    -> $0.60 / M
  output_cost_per_token         : 2.4e-06  -> $2.40 / M
  cache_read_input_token_cost   : 1.2e-07  -> $0.12 / M

Cache creation is conservatively $0/M (LiteLLM's entry doesn't
specify one and we don't have a confirmed MiniMax rate).
"""

from __future__ import annotations

INPUT_PER_M = 0.60
OUTPUT_PER_M = 2.40
CACHE_READ_PER_M = 0.12
CACHE_WRITE_PER_M = 0.0


def cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Cost in USD for a single LLM call's usage.

    Matches the formula in `scratch/tier1/common.py:call_minimax()`
    but with the LiteLLM rates (2× the README tier).
    """
    cost = (
        input_tokens * INPUT_PER_M / 1_000_000
        + output_tokens * OUTPUT_PER_M / 1_000_000
        + cache_read_tokens * CACHE_READ_PER_M / 1_000_000
        + cache_write_tokens * CACHE_WRITE_PER_M / 1_000_000
    )
    return round(cost, 8)
