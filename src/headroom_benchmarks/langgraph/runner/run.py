"""Runner: execute the LangGraph agent on all 50 test cases and produce metrics.

Usage:
  # 1. (separate terminal) start an isolated proxy on :8788
  HOME=/tmp/headroom-bench-home-v2 \\
    ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \\
    ANTHROPIC_TARGET_API_URL="https://api.minimax.io/anthropic" \\
      uv run headroom proxy --port 8788 --no-cache --no-rate-limit

  # 2. Run this script
  ANTHROPIC_BASE_URL=http://127.0.0.1:8788 \\
  MINIMAX_API_KEY=... \\
    uv run python scratch/langgraph_bench/runner/run.py

v2: snapshot-based aggregation. The runner captures /stats before and
after the loop; the OVERALL block in summary.json is computed from the
diff. Per-case/per-category is SDK-side only (no per-call backfill).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Make the scratch package importable as `headroom_benchmarks.langgraph.*`
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from headroom_benchmarks.langgraph.agent.callbacks import UsageCapture
from headroom_benchmarks.langgraph.agent.graph import build_graph
from headroom_benchmarks.langgraph.agent.tools import mcp_session
from headroom_benchmarks.langgraph.runner.metrics import (
    CaseAggregate,
    aggregate_case,
    aggregate_run,
    write_outputs,
)


TEST_CASES_PATH = Path(__file__).parent / "test_cases.json"
RESULTS_ROOT = Path(__file__).parent.parent / "results"
PROXY_BASE_URL = os.environ.get("PROXY_BASE_URL", "http://127.0.0.1:8788")


def snapshot_proxy_stats() -> dict | None:
    """Best-effort fetch of the proxy's /stats snapshot.

    Returns the parsed JSON or None if the proxy isn't reachable.
    """
    try:
        url = f"{PROXY_BASE_URL}/stats"
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


async def run_one_case(graph, case: dict, capture: UsageCapture) -> CaseAggregate:
    """Invoke the agent once per test case. SDK-side usage capture only.

    Errors are caught and converted to a zero aggregate so the loop survives.
    No per-case proxy backfill — that was the source of v1's negative
    compression percentages.
    """
    case_id = case["id"]
    capture.records.clear()
    t0 = time.perf_counter()
    zero_agg = CaseAggregate(
        case_id=case_id, category=case["category"], prompt=case["prompt"],
        n_llm_calls=0, input_tokens=0, output_tokens=0,
        cache_read_tokens=0, input_tokens_original=0,
        cost_usd_with_compression=0.0, cost_usd_without_compression=0.0,
        saved_usd=0.0, compression_pct=0.0, tool_calls_used=0,
        trajectory_chars=0,
    )
    try:
        result = await graph.ainvoke({
            "messages": [{"role": "user", "content": case["prompt"]}],
            "case_id": case_id,
            "step": 0,
            "usage_records": [],
        })
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        print(f"  [{case_id:5s}] ERROR ainvoke after {elapsed:.1f}s: {type(exc).__name__}: {str(exc)[:120]}")
        return zero_agg

    elapsed = time.perf_counter() - t0

    try:
        agg = aggregate_case(
            case_id=case_id,
            category=case["category"],
            prompt=case["prompt"],
            records=[r.__dict__ for r in capture.records],
        )
    except Exception as exc:
        elapsed2 = time.perf_counter() - t0
        print(f"  [{case_id:5s}] ERROR post-ainvoke after {elapsed2:.1f}s: {type(exc).__name__}: {str(exc)[:120]}")
        return zero_agg

    last_msg = result["messages"][-1] if result.get("messages") else None
    snippet = ""
    if last_msg is not None and hasattr(last_msg, "content") and last_msg.content:
        snippet = str(last_msg.content).replace("\n", " ")[:90]
    print(
        f"  [{case_id:5s}] {agg.category:16s}  "
        f"calls={agg.n_llm_calls}  "
        f"in={agg.input_tokens:>6d}  "
        f"out={agg.output_tokens:>4d}  "
        f"cr={agg.cache_read_tokens:>5d}  "
        f"${agg.cost_usd_with_compression:.5f}  "
        f"{elapsed:5.1f}s  "
        f"\"{snippet}…\""
    )
    return agg


async def main_async():
    print(f"=== LangGraph × Headroom benchmark (v2 snapshot-based) ===")
    print(f"  proxy   : {PROXY_BASE_URL}")
    snap_initial = snapshot_proxy_stats()
    if snap_initial is None:
        print(f"  WARN    : proxy unreachable at {PROXY_BASE_URL}; agent calls will fail")
    else:
        primary = snap_initial.get("summary", {}).get("primary_model", "unknown")
        api = snap_initial.get("summary", {}).get("api_requests", 0)
        print(f"  proxy primary_model: {primary}  (api_requests so far: {api})")

    # Set up run directory
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = RESULTS_ROOT / f"bench_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  out_dir : {out_dir}")
    print()

    # Load test cases
    with TEST_CASES_PATH.open() as f:
        test_data = json.load(f)
    cases = test_data["cases"]
    print(f"  cases   : {len(cases)}")

    # ---- snapshot BEFORE the loop ----
    proxy_before = snapshot_proxy_stats()
    if proxy_before is None:
        print("  ERROR   : cannot snapshot proxy before run; aborting")
        return

    # Long-lived UsageCapture; we clear its buffer between cases.
    capture = UsageCapture(jsonl_path=str(out_dir / "per_request.jsonl"))

    case_aggs: list[CaseAggregate] = []
    async with mcp_session() as session:
        graph = await build_graph(session=session, capture=capture)
        for i, case in enumerate(cases, start=1):
            print(f"[{i:>2}/{len(cases)}]", end="")
            agg = await run_one_case(graph, case, capture)
            case_aggs.append(agg)

    # ---- snapshot AFTER the loop ----
    proxy_after = snapshot_proxy_stats()
    if proxy_after is None:
        print("  WARN    : cannot snapshot proxy after run; falling back to SDK-only aggregation")

    # Save both snapshots for diagnostic / future analysis
    (out_dir / "proxy_before.json").write_text(json.dumps(proxy_before or {}, indent=2))
    (out_dir / "proxy_after.json").write_text(json.dumps(proxy_after or {}, indent=2))

    # Aggregate + write outputs
    summary = aggregate_run(
        case_aggs,
        proxy_before=proxy_before,
        proxy_after=proxy_after,
    )
    write_outputs(out_dir, case_aggs, summary, model="MiniMax-M3")

    # Print headline
    o = summary["overall"]
    print()
    print(f"=== Headline (source: {o.get('source', '?')}) ===")
    print(f"  cases          : {o['n_cases']}")
    print(f"  llm calls      : {o['n_llm_calls']}")
    if "input_before" in o:
        print(f"  input before   : {o['input_before']:>8d} tokens")
        print(f"  input after    : {o['input_after']:>8d} tokens")
        print(f"  compression    : {o['compression_pct']:>5.1f}%")
        print(f"  cost with Hrm  : ${o['cost_with']:.4f}")
        print(f"  cost without   : ${o['cost_without']:.4f}")
        print(f"  saved          : ${o['saved_usd']:.4f}  ({o['savings_pct']:.1f}%)")
    else:
        print(f"  input (post)   : {o['input_after']:>8d} tokens")
        print(f"  output         : {o['output']:>8d} tokens")
        print(f"  cost with Hrm  : ${o['cost_with']:.4f}")
    print()
    print(f"  by category (SDK-side; cost_without estimated from overall ratio):")
    for cat, sub in summary["per_category"].items():
        est = sub.get("saved_usd_est", 0.0)
        print(
            f"    {cat:18s}  n={sub['n_cases']:>2}  "
            f"in={sub['input_after']:>6d}  "
            f"out={sub['output']:>5d}  "
            f"cr={sub['cache_read']:>5d}  "
            f"cost=${sub['cost_with']:.4f}  "
            f"saved≈${est:.4f}"
        )
    print()
    print(f"  outputs:")
    print(f"    {out_dir / 'summary.json'}")
    print(f"    {out_dir / 'per_case.json'}")
    print(f"    {out_dir / 'per_request.jsonl'}")
    print(f"    {out_dir / 'proxy_before.json'}")
    print(f"    {out_dir / 'proxy_after.json'}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
