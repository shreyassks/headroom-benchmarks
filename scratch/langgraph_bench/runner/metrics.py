"""Metrics aggregation for the LangGraph × Headroom benchmark.

v2: snapshot-based for accurate overall numbers.

The previous version backfilled per-case `input_tokens_original` by
matching SDK records to proxy `recent_requests` chronologically within
each case's time window. That broke for short cases (1-2 LLM calls)
because the proxy's request buffer doesn't align with our case boundaries
when cases run back-to-back, producing nonsense negative compression
percentages.

New design:
  - Overall: snapshot `/stats` BEFORE and AFTER the run; compute pre/post
    totals from the deltas. The proxy knows exactly what it compressed,
    so this is the authoritative number.
  - Per-case / per-category: SDK-side only. We report post-compression
    input_tokens, output_tokens, cache_read_tokens, and the actual cost
    paid. No per-case `input_tokens_original` — the proxy doesn't tag
    requests by our case_id, so per-case pre-compression isn't reliably
    recoverable from the snapshot alone.
  - The cost-without-compression for the OVERALL still uses the proxy's
    own calculation (which knows about cache, prompts, etc.). For per-
    category, we estimate cost_without by scaling cost_with by the
    proxy's overall savings_ratio.

Cost math uses pricing.py (LiteLLM rates) for cross-validation against
the proxy dashboard.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.pricing import cost_usd


@dataclass
class CaseAggregate:
    case_id: str
    category: str
    prompt: str
    n_llm_calls: int
    input_tokens: int                 # SDK-side, post-compression
    output_tokens: int
    cache_read_tokens: int
    input_tokens_original: int        # 0 in v2 — proxy-side pre-compression dropped
    cost_usd_with_compression: float
    cost_usd_without_compression: float
    saved_usd: float
    compression_pct: float            # 0.0 in v2 — see docstring
    tool_calls_used: int
    trajectory_chars: int


def aggregate_case(
    *,
    case_id: str,
    category: str,
    prompt: str,
    records: list[dict],
) -> CaseAggregate:
    """SDK-side only. No per-case backfill."""
    n = len(records)
    in_post = sum(r["input_tokens"] for r in records)
    out = sum(r["output_tokens"] for r in records)
    cr = sum(r.get("cache_read_input_tokens", 0) for r in records)

    cost_with = sum(r["cost_usd"] for r in records)

    # Tool-call heuristic (same as before): count distinct LLM calls that
    # had any tool result follow. Without the message trajectory we use
    # the rough heuristic that any case with input > 500 tokens had
    # at least one tool call land in context.
    tool_calls = sum(1 for r in records if r["input_tokens"] > 500) or (
        1 if any(r["input_tokens"] > 500 for r in records) else 0
    )

    return CaseAggregate(
        case_id=case_id,
        category=category,
        prompt=prompt,
        n_llm_calls=n,
        input_tokens=in_post,
        output_tokens=out,
        cache_read_tokens=cr,
        input_tokens_original=0,            # v2: not measured per-case
        cost_usd_with_compression=round(cost_with, 6),
        cost_usd_without_compression=0.0,   # v2: filled in for overall only
        saved_usd=0.0,                      # v2: filled in for overall only
        compression_pct=0.0,                # v2: not measured per-case
        tool_calls_used=tool_calls,
        trajectory_chars=in_post + out,
    )


def _snapshot_diff(before: dict, after: dict, key_path: list[str]) -> float:
    """Read a nested key from both snapshots and return the delta."""
    def _dig(d: dict, path: list[str]) -> float:
        cur: Any = d
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return 0.0
            cur = cur[k]
        return float(cur) if cur is not None else 0.0
    return _dig(after, key_path) - _dig(before, key_path)


def aggregate_run(
    case_aggs: list[CaseAggregate],
    *,
    proxy_before: dict | None = None,
    proxy_after: dict | None = None,
) -> dict[str, Any]:
    """Build the top-level summary.

    If both proxy_before and proxy_after are provided, the OVERALL block
    is computed from the snapshot diff (authoritative). Per-category
    stays SDK-side only.
    """
    by_cat: dict[str, list[CaseAggregate]] = defaultdict(list)
    for c in case_aggs:
        by_cat[c.category].append(c)

    # ---- per-category (SDK-side, post-compression) ----
    per_category: dict[str, Any] = {}
    for cat, cases in sorted(by_cat.items()):
        sub: dict[str, Any] = {
            "n_cases":      len(cases),
            "n_llm_calls":  sum(c.n_llm_calls for c in cases),
            "input_after":  sum(c.input_tokens for c in cases),
            "output":       sum(c.output_tokens for c in cases),
            "cache_read":   sum(c.cache_read_tokens for c in cases),
            "cost_with":    round(sum(c.cost_usd_with_compression for c in cases), 6),
        }
        per_category[cat] = sub

    # ---- overall ----
    # SDK-side totals (always available)
    sdk_input_after = sum(c.input_tokens for c in case_aggs)
    sdk_output = sum(c.output_tokens for c in case_aggs)
    sdk_cache_read = sum(c.cache_read_tokens for c in case_aggs)
    sdk_cost_with = round(sum(c.cost_usd_with_compression for c in case_aggs), 6)
    sdk_n_calls = sum(c.n_llm_calls for c in case_aggs)

    overall: dict[str, Any] = {
        "n_cases":       len(case_aggs),
        "n_llm_calls":   sdk_n_calls,
        "input_after":   sdk_input_after,
        "output":        sdk_output,
        "cache_read":    sdk_cache_read,
        "cost_with":     sdk_cost_with,
        "source":        "sdk_only",
    }

    # If we have proxy snapshots, override overall with the authoritative diff
    if proxy_before is not None and proxy_after is not None:
        # Tokens
        # total_input_tokens is cumulative post-compression input tokens sent.
        # total_tokens_saved is cumulative tokens removed by compression.
        # So pre-compression = post + saved.
        # Note: the proxy uses cumulative counters, so we take the delta.
        input_after_delta = _snapshot_diff(proxy_before, proxy_after,
                                          ["cost", "total_input_tokens"])
        tokens_saved_delta = _snapshot_diff(proxy_before, proxy_after,
                                            ["cost", "total_tokens_saved"])
        input_before_delta = int(input_after_delta + tokens_saved_delta)

        # Tokens output (proxy stores this at /stats.tokens.output)
        output_delta = _snapshot_diff(proxy_before, proxy_after,
                                      ["tokens", "output"])
        # Cache read input tokens (proxy at /stats.tokens.cache_read_input_tokens)
        cache_read_delta = _snapshot_diff(proxy_before, proxy_after,
                                          ["tokens", "cache_read_input_tokens"])

        # Costs
        #   cost_with       : cost.cost_with_headroom_usd (raw, 6-decimal precision)
        #   compression_save: cost.compression_savings_usd (raw, what compression saved)
        #   cost_without    = cost_with + compression_save (so the math is exact)
        cost_with_delta = _snapshot_diff(proxy_before, proxy_after,
                                         ["cost", "cost_with_headroom_usd"])
        compression_savings_delta = _snapshot_diff(proxy_before, proxy_after,
                                                   ["cost", "compression_savings_usd"])
        cost_without_delta = cost_with_delta + compression_savings_delta

        # Total LLM calls (proxy at /stats.summary.api_requests or /stats.requests.total)
        api_requests_delta = _snapshot_diff(proxy_before, proxy_after,
                                            ["summary", "api_requests"])

        overall.update({
            "n_cases":           len(case_aggs),
            "n_llm_calls":       int(api_requests_delta) or sdk_n_calls,
            "input_before":      input_before_delta,
            "input_after":       int(input_after_delta),
            "output":            int(output_delta) or sdk_output,
            "cache_read":        int(cache_read_delta) or sdk_cache_read,
            "cost_with":         round(cost_with_delta, 6),
            "cost_without":      round(cost_without_delta, 6),
            "saved_usd":         round(compression_savings_delta, 6),
            "source":            "proxy_snapshot_diff",
        })
        overall["compression_pct"] = (
            round((overall["input_before"] - overall["input_after"]) / overall["input_before"] * 100, 2)
            if overall["input_before"] > 0 else 0.0
        )
        overall["savings_pct"] = (
            round(overall["saved_usd"] / overall["cost_without"] * 100, 2)
            if overall["cost_without"] > 0 else 0.0
        )

        # Back-fill per-category cost_without using the overall savings ratio
        ratio = (overall["cost_without"] / overall["cost_with"]) if overall["cost_with"] > 0 else 1.0
        for cat, sub in per_category.items():
            if sub["cost_with"] > 0:
                sub["cost_without_est"] = round(sub["cost_with"] * ratio, 6)
                sub["saved_usd_est"] = round(sub["cost_without_est"] - sub["cost_with"], 6)

    return {
        "overall": overall,
        "per_category": per_category,
    }


def write_outputs(
    out_dir: Path,
    case_aggs: list[CaseAggregate],
    summary: dict[str, Any],
    model: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    per_case_path = out_dir / "per_case.json"
    with per_case_path.open("w") as f:
        json.dump(
            {
                "model": model,
                "cases": [
                    {k: getattr(c, k) for k in CaseAggregate.__dataclass_fields__}
                    for c in case_aggs
                ],
            },
            f,
            indent=2,
        )

    summary_path = out_dir / "summary.json"
    with summary_path.open("w") as f:
        json.dump(
            {"model": model, **summary},
            f,
            indent=2,
        )
