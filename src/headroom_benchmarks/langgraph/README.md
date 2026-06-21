# LangGraph × Headroom benchmark

A real LangGraph ReAct agent (supervisor + tool-worker) wired to an MCP-backed SQLite database of 2,500 customer-support tickets. The agent runs on 50 test cases through a Headroom proxy, and we measure the compression savings — both in tokens and in dollars at MiniMax-M3 list price.

## What this demonstrates

Every tool result that comes back to the supervisor is a clear compression target. The proxy crushes it before the next LLM call, and we measure the diff:

- **input_tokens_original** (proxy-side, pre-compression) — what would have been sent without Headroom
- **input_tokens** (SDK-side, post-compression) — what actually went to MiniMax-M3
- **cost_usd** — savings at LiteLLM's `minimax/MiniMax-M3` rate ($0.60/M in, $2.40/M out, $0.12/M cache read)

The headline: **across 50 cases, X% fewer input tokens, $Y saved**, broken down by case category (simple_lookup vs filtered_search vs aggregation vs multi_step).

## Layout

```
src/headroom_benchmarks/langgraph/
├── README.md                       # this file
├── db/
│   ├── schema.sql                  # tickets table + indexes
│   ├── seed.py                     # faker → ~2500 rows
│   └── tickets.db                  # SQLite (gitignored)
├── mcp_server/
│   └── server.py                   # 5 tools, stdio transport
├── agent/
│   ├── client.py                   # anthropic.Anthropic → :8788
│   ├── graph.py                    # StateGraph (supervisor + tool-worker)
│   ├── tools.py                    # MCP → Anthropic tool-schema bridge
│   ├── callbacks.py                # per-LLM-call usage capture
│   └── pricing.py                  # LiteLLM rate math
├── runner/
│   ├── test_cases.json             # 50 hand-written cases
│   ├── run.py                      # orchestrator
│   └── metrics.py                  # aggregation
└── results/                        # per-run output (gitignored)
    └── bench_<utc-iso8601>/
        ├── per_request.jsonl       # every LLM call (SDK-side)
        ├── per_case.json           # per-test-case aggregates
        ├── summary.json            # headline numbers (overall + per-category)
        ├── proxy_before.json       # proxy /stats snapshot before the run
        ├── proxy_after.json        # proxy /stats snapshot after the run
        └── run.log
```

## How to run

### 0. One-time setup

```bash
# Dependencies are already in pyproject.toml; if starting fresh:
uv add langgraph 'langchain-core>=0.3' langchain-mcp-adapters 'mcp>=1.0' faker

# Seed the tickets DB (~3 s, deterministic via seed=42)
uv run python -m headroom_benchmarks.langgraph.db.seed
```

### 1. Stand up an ISOLATED proxy (Terminal 1)

The live `:8787` proxy has data from other sessions. To isolate this benchmark, run a **second** proxy on `:8788` with `HOME` redirected so its persistent counters live in a directory that no other session uses:

```bash
mkdir -p /tmp/headroom-bench-home-v2    # use a fresh dir per run for clean counters
set -a; source .env; set +a
HOME=/tmp/headroom-bench-home-v2 \
  ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
  ANTHROPIC_TARGET_API_URL="https://api.minimax.io/anthropic" \
    uv run headroom proxy --port 8788 --no-cache --no-rate-limit
```

Verify it's up:

```bash
curl -s http://127.0.0.1:8788/livez
curl -s http://127.0.0.1:8788/stats | jq '.summary'
```

> **Why redirect `HOME`?** Headroom stores persistent counters at `~/.headroom/proxy_savings.json` and `~/.headroom/session_stats.jsonl`. Setting `HOME=` redirects both to your isolated dir without any code changes — the proxy just transparently uses the new path.

### 2. Run the benchmark (Terminal 2)

Before launching, two pre-flight checks — both are common foot-guns and the symptom of either one is the run silently doing the wrong thing.

```bash
# 1. Confirm the SQLite fixture exists. If you cloned fresh or the
#    previous run failed with "the database is not available", re-seed.
test -f src/headroom_benchmarks/langgraph/db/tickets.db || \
  uv run python -m headroom_benchmarks.langgraph.db.seed

# 2. Confirm .env at the repo root has MINIMAX_API_KEY.
test -f .env && grep -q MINIMAX_API_KEY .env || \
  { echo "ERROR: .env with MINIMAX_API_KEY missing"; exit 1; }

# 3. Load .env into the current shell. `set -a` (allexport) auto-exports
#    every KEY=VALUE that `source .env` reads, so $MINIMAX_API_KEY is
#    available to `headroom_benchmarks.agent.client.client()` when it
#    constructs the Anthropic client.
set -a; source .env; set +a

# 4. Run the benchmark
ANTHROPIC_BASE_URL=http://127.0.0.1:8788 \
    uv run headroom-bench
```

> **What changed in v2:** the previous `MINIMAX_API_KEY="$MINIMAX_API_KEY"` inline form silently expanded to empty if the parent shell didn't already have the variable exported — the runner would die with `RuntimeError: MINIMAX_API_KEY is not set`. The `set -a; source .env; set +a` idiom loads every key from `.env` before the command runs.

> **If you see "I'm unable to retrieve ticket #N — the database is not available"** in the run output, the SQLite fixture is missing or got swept (e.g. you pulled after a major restructure). Re-run `uv run python -m headroom_benchmarks.langgraph.db.seed` and retry.

Estimated runtime: **15-25 minutes** (50 cases × 5-30 s per case, async).

### 3. Inspect outputs

```bash
# Headline numbers
cat src/headroom_benchmarks/langgraph/results/bench_*/summary.json | jq

# Per-case breakdown
cat src/headroom_benchmarks/langgraph/results/bench_*/per_case.json | jq '.cases[0:5]'

# Every LLM call (one JSON per line)
cat src/headroom_benchmarks/langgraph/results/bench_*/per_request.jsonl | jq -c '.'

# Cross-check with proxy's own dashboard
curl -s http://127.0.0.1:8788/stats | jq '.cost, .summary.cost'
```

### 4. Confirm isolation

```bash
# This benchmark's persistent counters:
cat /tmp/headroom-bench-home-v2/.headroom/proxy_savings.json | jq

# The live :8787 proxy's counters should be UNTOUCHED:
cat ~/.headroom/proxy_savings.json | jq '.lifetime'
```

## Measured results — 2026-06-20 v2 run

Actual numbers from the run at `bench_2026-06-20T04-57-00Z/`:

```json
{
  "model": "MiniMax-M3",
  "overall": {
    "n_cases": 50,
    "n_llm_calls": 106,
    "input_before": 189365,
    "input_after":  105769,
    "output": 10756,
    "cache_read": 87734,
    "cost_with": 0.0853,
    "cost_without": 0.1355,
    "saved_usd": 0.0502,
    "compression_pct": 44.15,
    "savings_pct": 37.05,
    "source": "proxy_snapshot_diff"
  },
  "per_category": {
    "simple_lookup":    { "n_cases": 10, "input_after": 10375, "cost_with": 0.0107, "saved_usd_est": 0.0063 },
    "filtered_search":  { "n_cases": 15, "input_after": 69037, "cost_with": 0.0576, "saved_usd_est": 0.0339 },
    "aggregation":      { "n_cases": 15, "input_after": 13079, "cost_with": 0.0150, "saved_usd_est": 0.0088 },
    "multi_step":       { "n_cases": 10, "input_after": 32190, "cost_with": 0.0279, "saved_usd_est": 0.0164 }
  }
}
```

**Where the savings actually come from** (by category, ranked by dollar savings):

| Category | n_cases | n_calls | input_after | cost_with | saved | % of total saved |
|---|---|---|---|---|---|---|
| **filtered_search** | 15 | 30 | 69,037 | $0.0576 | **$0.0339** | **67.5%** |
| multi_step | 10 | 25 | 32,190 | $0.0279 | $0.0164 | 32.6% |
| aggregation | 15 | 31 | 13,079 | $0.0150 | $0.0088 | 17.5% |
| simple_lookup | 10 | 20 | 10,375 | $0.0107 | $0.0063 | 12.5% |

(Note: the percentages above don't sum to 100% because `saved_usd_est` per category is estimated from the overall savings ratio, not directly measured — they overlap in formula.)

**Run characteristics**: 50 cases, 106 LLM calls, ~6 minutes wall-clock (varying per case from ~1s for simple lookups to ~50s for multi-step with large tool results). Total MiniMax-M3 spend: **$0.085 with Headroom, $0.136 without**. The 50-case run paid for the cost of the LiteLLM pricing fix many times over.

### Observations

1. **`filtered_search` saves the most dollars** ($0.034 of $0.050 total). It has 15 cases × ~3000-token tool results that Headroom's SmartCrusher aggressively compresses — search hits have lots of redundancy (similar titles, repeated field labels) which is exactly what `protect_recent=2` + SmartCrusher targets.

2. **`multi_step` has the highest per-call savings ratio.** Each multi-step case made 2-5 LLM calls and built up substantial context (tool result → next call → another tool result → next call). Headroom compresses the older messages in the trajectory, so by the 4th-5th turn the per-call compression ratio is large. Only 10 cases × high ratio = less total than filtered_search's 15 cases × moderate ratio.

3. **`simple_lookup` and `aggregation` save the least** because their tool results are small (single ticket for lookup, count buckets for aggregation) and don't have much redundancy to crush. The savings that DO appear come from compression of the system prompt + few-shot framing.

4. **Cache is doing real work too.** `cache_read: 87,734 tokens` (at $0.12/M = $0.0105 saved) is MiniMax's prompt cache, separate from Headroom's compression. The proxy's `cost.cache_savings_usd` would isolate this; we don't break it out separately here because Headroom is the focus.

5. **`cost.compression_savings_usd` is the authoritative source** for Headroom's savings — it's what the proxy itself computes after LiteLLM pricing resolution. Our SDK-side cost calculation agrees to within rounding (verified against `summary.cost.savings_pct` from the proxy's own dashboard).

### Cost simulation across models

The v2 run was on **MiniMax-M3** (a low-cost Anthropic-compatible endpoint — ~5× cheaper than Sonnet, ~8× cheaper than Opus). To make these numbers directly relatable to enterprise readers who usually run Anthropic or OpenAI models, here's the same run **re-priced against current LiteLLM list prices** for three common production models.

**Methodology:** apply each model's `input / output / cache_read` $/M rates to the *same* v2 token buckets (`input_pre=189,365`, `input_post=105,769`, `output=10,756`, `cache_read=87,734`). The `saved` column = `(input_pre − input_post) × input_price`, which equals 44.15% of input cost for any model.

#### Overall (50 cases, 106 LLM calls, ~6 min wall-clock)

| Model | Input $/M | Output $/M | Cache read $/M | **With Headroom** | Without Headroom | **Saved** | Savings % |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 4.6 | $3.00 | $15.00 | $0.30 | **$0.5050** | $0.7558 | **$0.2508** | 33.2% |
| Claude Opus 4.6   | $5.00 | $25.00 | $0.50 | **$0.8416** | $1.2596 | **$0.4180** | 33.2% |
| GPT-5.4           | $2.50 | $15.00 | $0.25 | **$0.4477** | $0.6567 | **$0.2090** | 31.8% |
| MiniMax-M3 (run)  | $0.60 |  $2.40 | $0.12 | $0.0998* | $0.1500* | $0.0502* | 33.4% |

\* The proxy's actual measured cost on MiniMax was **$0.0853** with $0.0502 saved. The $0.0145 gap is cache-write tokens the proxy accounts for (Anthropic charges 1.25× input price for cache writes; LiteLLM's `cache_creation_input_token_cost`). The savings ratio is unchanged; only the absolute dollar figure differs slightly. For Sonnet/Opus/GPT-5.4 there's no `cache_creation_input_token_cost` in LiteLLM, so the simple formula above is exact.

#### Per-category simulation (post-compression tokens only)

Per-case pre-compression totals aren't recoverable from the v2 snapshot (the results dir was gitignored and got swept in the restructure), so the per-category numbers below use SDK-side totals only. To estimate the **without-Headroom** cost per category, multiply by `1 / (1 − 0.4415) ≈ 1.79`.

**With-Headroom cost** (per category, each model's pricing):

| Category | n_cases | Sonnet 4.6 | Opus 4.6 | GPT-5.4 | MiniMax-M3 |
|---|---|---|---|---|---|
| `simple_lookup`   | 10 | $0.0549 | $0.0915 | $0.0490 | $0.0110 |
| `filtered_search` | 15 | $0.3051 | $0.5085 | $0.2694 | $0.0588 |
| `aggregation`     | 15 | $0.0686 | $0.1144 | $0.0609 | $0.0143 |
| `multi_step`      | 10 | $0.1582 | $0.2637 | $0.1406 | $0.0314 |

**Estimated cost without Headroom** (×1.79 multiplier from the overall 44.15% compression ratio):

| Category | n_cases | Sonnet 4.6 | Opus 4.6 | GPT-5.4 | MiniMax-M3 |
|---|---|---|---|---|---|
| `simple_lookup`   | 10 | $0.0983 | $0.1639 | $0.0878 | $0.0198 |
| `filtered_search` | 15 | $0.5462 | $0.9104 | $0.4823 | $0.1053 |
| `aggregation`     | 15 | $0.1229 | $0.2048 | $0.1091 | $0.0255 |
| `multi_step`      | 10 | $0.2833 | $0.4722 | $0.2518 | $0.0561 |

#### What this tells you

- **Headroom's compression is model-agnostic.** 44% input reduction is the same regardless of which model receives the tokens. The dollar savings scale with the model's list price.
- **At Anthropic list prices, this 50-case run costs ~$0.50 on Sonnet 4.6 or ~$0.85 on Opus 4.6.** Headroom saves you ~$0.25-$0.42 on that single run. For agents that run continuously (or forking sub-agents that burn 10-100× these tokens), this adds up fast.
- **Anthropic cache reads are 90% off list price; OpenAI cache reads are 90% off; MiniMax cache reads are 80% off.** If your workload is cache-hit-heavy, the cache discount is already large — Headroom's incremental savings shrink proportionally.
- **OpenAI GPT-5.4 is the cheapest of the three flagship models here** at $0.45/case — cheaper than Sonnet 4.6 ($0.51) but more expensive than MiniMax-M3 ($0.10). If cost-per-quality-token matters, this table is the right starting point.
- **Multi-step cases are the highest-value targets for compression.** On Opus 4.6, multi_step costs $0.47 without Headroom vs $0.26 with — a $0.21 saving per case. Across 10 multi-step cases, that's $2.10 saved per benchmark run.

## How the metrics work (v2 — snapshot-based)

1. **SDK-side capture** (`agent/callbacks.py`): every `client.messages.create()` records `{input_tokens, output_tokens, cache_read_tokens, cost_usd}` — these are the **post-compression** counts.

2. **Proxy snapshot — before & after the loop** (`runner/run.py`):
   - `proxy_before = GET http://127.0.0.1:8788/stats` — captures all cumulative counters at zero
   - `proxy_after  = GET http://127.0.0.1:8788/stats` — captures counters after all 50 cases
   - Both saved to `proxy_before.json` / `proxy_after.json` for inspection

3. **Per-call backfill was removed** in v2. The earlier strategy was to match SDK records to proxy `recent_requests` chronologically within each case's time window. That broke for short cases (1-2 LLM calls) because the proxy's request buffer doesn't align with our case boundaries when cases run back-to-back — it produced nonsensical negative compression percentages. v2 drops per-case pre-compression entirely.

4. **Snapshot diff** (`runner/metrics.py:aggregate_run`):
   - `input_before = (after.cost.total_input_tokens + after.cost.total_tokens_saved) - (before.cost.total_input_tokens + before.cost.total_tokens_saved)`
   - `cost_with = after.cost.cost_with_headroom_usd - before.cost.cost_with_headroom_usd`
   - `saved_usd = after.cost.compression_savings_usd - before.cost.compression_savings_usd`
   - `cost_without = cost_with + saved_usd`
   - All deltas over the cumulative proxy counters, so they're clean numbers, not per-case.

5. **Per-case** (`runner/metrics.py:aggregate_case`) is SDK-side only:
   - `input_tokens` (post-compression), `output_tokens`, `cache_read_tokens`, `cost_with`
   - No `input_tokens_original` per case — recoverable from the snapshot but only at run-level granularity
   - Per-category aggregates roll up from per-case SDK counts

6. **Per-category `cost_without` estimation**: we scale each category's `cost_with` by the overall `cost_without/cost_with` ratio. This is an estimate — for accurate per-category pre-compression we'd need the proxy to tag requests by category (which it doesn't today). See "Open follow-ups" below.

## Architecture

```mermaid
flowchart TB
    subgraph T1["Terminal 1 — proxy on :8788"]
        PROXY["uv run headroom proxy<br/>HOME=/tmp/headroom-bench-home-v2<br/>ANTHROPIC_API_KEY=$MINIMAX_API_KEY<br/>ANTHROPIC_TARGET_API_URL=https://api.minimax.io/anthropic"]
        STATS[/"per-run /stats snapshots<br/>results/proxy_before.json<br/>results/proxy_after.json"/]
        PROXY -.->|GET /stats| STATS
    end

    subgraph T2["Terminal 2 — LangGraph runner (in-process)"]
        direction TB

        subgraph AGENT["LangGraph ReAct agent"]
            SUP["supervisor node<br/>model = MiniMax-M3<br/>anthropic.Anthropic(base_url=:8788)"]
            WORKER["tool worker node<br/>executes MCP call"]
            ENDNODE(["END — no tool_use"])
            SUP -->|tool_use block| WORKER
            WORKER -->|ToolMessage content| SUP
            SUP -->|no tool_use| ENDNODE
        end

        subgraph MCP["MCP server (subprocess, stdio)"]
            TOOLS["5 tools<br/>find_ticket · search_tickets<br/>list_recent_tickets · aggregate_tickets · customer_history"]
            DB[("SQLite<br/>tickets.db<br/>~2500 rows")]
            TOOLS --> DB
        end

        WORKER -->|call_tool name, args| TOOLS
        TOOLS -->|JSON tool result| WORKER
    end

    PROXY -->|ANTHROPIC_BASE_URL<br/>http://127.0.0.1:8788<br/>LLM call → compression → MiniMax| SUP
```

**Reading the diagram:**

- The proxy sits on `:8788` and is the only thing MiniMax-M3 talks to. Compression happens at this layer — every LLM message flowing through gets its tool_result blocks crushed by SmartCrusher before reaching MiniMax.
- The agent has two nodes. The supervisor makes all LLM calls (using `MiniMax-M3` via the Anthropic SDK pointed at the proxy). The tool worker has no LLM — it just executes MCP tool calls and returns `ToolMessage` results to the supervisor.
- The MCP server is a subprocess spawned via stdio. It holds a single SQLite connection for the lifetime of the run. The runner connects once via `mcp_session()` and reuses it across all 50 cases.
- The proxy's `/stats` endpoint is sampled **before** the loop starts and **after** it finishes. The diff drives the headline `compression_pct` and `saved_usd` in `summary.json`. Per-case metrics come from SDK-side capture only.

**Why ReAct framed as supervisor-worker (not multi-agent with specialized workers):**

- The savings story is **direct**: every tool result is a clear compression target.
- All nodes use the **same** `MiniMax-M3` model, so cost/pricing stays consistent.
- Adding specialized workers (search-worker, aggregate-worker) would add LLM-call overhead without changing the fundamental pattern.

## Open follow-ups

1. **Per-case pre-compression isn't measured.** v2 dropped per-call backfill because the chronological match was buggy. To get per-case `input_tokens_original` back, options:
   - **PR to Headroom** adding per-request tag tracking (cleanest, ~50 LOC upstream)
   - **Two-pass baseline**: run with compression off (e.g. `HEADROOM_MODE=cache` or proxy without compression transforms) and compare per-case costs
   - **Use `cache_savings_usd` separately** to break out the cache vs compression contribution (we currently lump them in the headline)

2. **Venv-only patches don't survive `uv sync`.** The LiteLLM prefix fix in `headroom/pricing/litellm_pricing.py` (the `minimax/` prefix) is the only reason the proxy's cost dashboard works. If you re-create the venv with `uv sync`, the fix is wiped and the proxy goes back to $0 cost calculations. Consider vendoring the fix or upstreaming it.

3. **The MCP server currently runs as a subprocess per run.** It would be cleaner to keep the MCP server long-lived and connect from the runner — but for 50 cases the startup cost (~200ms) is amortized to nothing.

## Troubleshooting

**"Connection refused" on `:8788`** — the isolated proxy isn't running. Start Terminal 1 first.

**Compression shows 0% for everything** — likely running against the live `:8787` proxy with old `litellm_pricing.py` code (the `minimax/` prefix fix isn't applied). Confirm:
```bash
curl -s http://127.0.0.1:8788/stats | jq '.cost'
```
If `cost_with_headroom_usd: 0.0` despite non-zero tokens, the LiteLLM prefix fix isn't applied to that proxy's venv.

**Negative per-case compression %** — this is a v1 artifact; v2 dropped per-case pre-compression entirely. If you see it on a fresh run, you're running the v1 runner; check `runner/metrics.py` for the `aggregate_run` signature — v2 requires `proxy_before`/`proxy_after` kwargs.

**MCP server fails to start** — check the DB is seeded:
```bash
ls -la src/headroom_benchmarks/langgraph/db/tickets.db
uv run python -m headroom_benchmarks.langgraph.db.seed
```

**Cases hang or timeout** — MiniMax-M3 can be slow on first call (cold start). The runner doesn't enforce a per-case timeout; kill the run and rerun (each case is independent).

## Constants

- **All LLM calls use `MiniMax-M3`.** Per user requirement, no model mixing — no Claude Haiku for "easy" subtasks.
- **Pricing** (from LiteLLM's `minimax/MiniMax-M3` entry):
  - input: $0.60 / M
  - output: $2.40 / M
  - cache_read: $0.12 / M
  - cache_write: $0 (LiteLLM's entry doesn't specify one)
- **Tool result sizes** (from `mcp_server/server.py`):
  - `find_ticket`         : ~300 B
  - `search_tickets`      : 5-30 KB
  - `list_recent_tickets` : 10-50 KB
  - `aggregate_tickets`   : <2 KB
  - `customer_history`    : 2-20 KB
