"""headroom-benchmarks — end-to-end benchmark of headroom-ai as an LLM-call
compression proxy, running a real LangGraph ReAct agent against MiniMax-M3.

Modules:
    headroom_benchmarks.benchmarks.synthetic
        Reproductions of headroom's 92% synthetic-payload claims.
    headroom_benchmarks.benchmarks.tier1
        GSM8K, SQuAD v2, BFCL, TruthfulQA (academic benchmarks).
    headroom_benchmarks.langgraph
        Real LangGraph ReAct agent + MCP server + 50-case runner.

Console scripts:
    uv run headroom-bench        # the headline LangGraph × Headroom benchmark
    uv run headroom-bench-all    # the Tier-1 suite (N=100 by default)
"""

__version__ = "0.1.0"
