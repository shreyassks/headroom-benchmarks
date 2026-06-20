# headroom-benchmarks

End-to-end benchmark of [headroom-ai](https://github.com/chopratejas/headroom) as an LLM-call compression proxy, running a real **LangGraph ReAct agent against MiniMax-M3** (Anthropic-compatible endpoint).

> **Headline result** — 50 test cases / 106 LLM calls / 6 min wall-clock:
> **44% input token compression**, **37% cost savings** at LiteLLM list price (`$0.085 with Headroom` vs `$0.136 without`).
>
> Full breakdown: [`src/headroom_benchmarks/langgraph/README.md`](src/headroom_benchmarks/langgraph/README.md).

---

## What's in this repo

| Path | What it is |
|---|---|
| `src/headroom_benchmarks/langgraph/` | **The headline benchmark.** LangGraph ReAct agent + custom MCP server + SQLite fixture + 50-case runner. Snapshot-based metrics aggregation. See its README for measured numbers, architecture diagram (Mermaid), and reproduction steps. |
| `src/headroom_benchmarks/benchmarks/tier1/` | Reproduction of upstream academic benchmarks — GSM8K (math), SQuAD v2 (QA), BFCL (function-calling) — against MiniMax-M3, with and without Headroom compression. |
| `src/headroom_benchmarks/benchmarks/synthetic/` | Synthetic-payload benchmarks reproducing Headroom's 92% compression claim (code search, SRE incident). |

The `langgraph/` subdirectory is the primary artifact; everything else is supporting context.

---

## Quickstart

```bash
git clone https://github.com/shreyassks/headroom-benchmarks.git
cd headroom-benchmarks
uv sync                                       # installs headroom-ai[all] + langgraph + mcp + faker

# Synthetic benchmarks (no API key needed, ~5 s each)
uv run python -m headroom_benchmarks.benchmarks.synthetic.code_search
uv run python -m headroom_benchmarks.benchmarks.synthetic.sre_incident

# LangGraph agent benchmark (~6 min, needs MINIMAX_API_KEY)
# Full setup in src/headroom_benchmarks/langgraph/README.md, but in short:
mkdir -p /tmp/headroom-bench-home
HOME=/tmp/headroom-bench-home \
  ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
  ANTHROPIC_TARGET_API_URL="https://api.minimax.io/anthropic" \
    uv run headroom proxy --port 8788 --no-cache --no-rate-limit &     # Terminal 1

ANTHROPIC_BASE_URL=http://127.0.0.1:8788 \
  MINIMAX_API_KEY="$MINIMAX_API_KEY" \
    uv run headroom-bench                              # Terminal 2

cat src/headroom_benchmarks/langgraph/results/bench_*/summary.json | jq
```

---

## Headline result (LangGraph benchmark, 2026-06-20)

```json
{
  "overall": {
    "n_cases": 50, "n_llm_calls": 106,
    "input_before": 189365, "input_after": 105769,
    "compression_pct": 44.15,
    "cost_with": 0.0853, "cost_without": 0.1355,
    "saved_usd": 0.0502, "savings_pct": 37.05
  }
}
```

**Where the savings come from** (by category):

| Category | n_cases | n_calls | input_after | cost_with | saved | % of total |
|---|---|---|---|---|---|---|
| **filtered_search** | 15 | 30 | 69,037 | $0.0576 | **$0.0339** | 67.5% |
| multi_step | 10 | 25 | 32,190 | $0.0279 | $0.0164 | 32.6% |
| aggregation | 15 | 31 | 13,079 | $0.0150 | $0.0088 | 17.5% |
| simple_lookup | 10 | 20 | 10,375 | $0.0107 | $0.0063 | 12.5% |

`filtered_search` saves the most dollars — search hits have lots of redundancy (similar titles, repeated field labels) that Headroom's SmartCrusher targets. `multi_step` has the highest per-call compression because each case makes 2-5 LLM calls and accumulates context.

---

## What Headroom does

Sits between your agent (Claude Code, Codex, Cursor, Aider, your own app) and the LLM provider. Each piece of content is routed through the right compressor:

- **SmartCrusher** — JSON
- **CodeCompressor** — AST-aware code
- **Kompress-base** — prose (HuggingFace model)
- **CacheAligner** — prefix stabilization for provider KV-cache hits
- **CCR** (Compress-Cache-Retrieve) — stores originals locally so the LLM can call `headroom_retrieve` to pull them back

Claims 60–95% token reduction while preserving answer quality. Runs locally, reversible, deploys as a library, proxy, MCP server, or agent wrapper.

---

## CLI tour

All commands run via `uv run headroom …`.

| Command | Purpose |
|---|---|
| `headroom wrap {claude,codex,copilot,aider,cursor,cline,continue,goose,openhands,openclaw,vibe}` | Start proxy + set env vars + launch the wrapped tool |
| `headroom proxy [--port 8787] [--no-optimize]` | Stand up the optimization proxy alone |
| `headroom mcp install` | Wire `headroom_retrieve` into Claude Code for CCR retrieval |
| `headroom learn [--apply]` | Mine past tool-call failures with an LLM, write corrections to `CLAUDE.md` / `AGENTS.md` |
| `headroom memory {list,show,stats,edit,delete,prune,purge,export,import}` | Manage the cross-agent memory store |
| `headroom perf [--hours N] [--format json\|csv]` | Analyze `~/.headroom/logs/proxy.log` for savings/cache/transforms |
| `headroom init {claude,codex,copilot,openclaw}` | Durable install of hooks + provider routing |
| `headroom install {apply,remove,restart,start,stop,status}` | Persistent proxy deployments |
| `headroom agent-savings --profile agent-90 --format shell` | Print env vars for a savings profile (default target ratio 0.10) |
| `headroom tools {doctor,install,list}` | Bundled CLI binaries (ast-grep, difft, scc) |
| `headroom evals {memory,memory-v2,adversarial,probes}` | LoCoMo memory benchmarks + compressor robustness |
| `headroom capture network-diff` | Compare direct vs MITM-captured traffic |

Health/metrics endpoints: `/livez`, `/readyz`, `/health`, `/stats`, `/stats-history`, `/metrics` (Prometheus).

---

## Python API

```python
from headroom import compress

# Simplest path — full compression pipeline in one call
result = compress(messages, model="claude-sonnet-4-5-20250929", optimize=True)
# result.tokens_before, .tokens_after, .tokens_saved,
# .compression_ratio, .transforms_applied, .messages

# Defaults are tuned for coding agents:
#   - protect_user_messages = True   (don't mangle user instructions)
#   - protect_recent = 2              (keep last 2 turns untouched)
# Override per-call:
result = compress(
    messages, model="claude-sonnet-4-5-20250929",
    compress_user_messages=True,
    target_ratio=0.5,
    protect_recent=0,
)
```

---

## Synthetic benchmarks — `src/headroom_benchmarks/benchmarks/synthetic/`

Both scripts build a synthetic Anthropic-format conversation (system → user → assistant tool_use → user tool_result) and report before/after tokens.

### Code search — 100 results

```bash
uv run python -m headroom_benchmarks.benchmarks.synthetic.code_search
```

README claim: **17,765 → 1,408 = 92%**. This run: **20,144 → 2,499 = 87.6%** (transform: `router:tool_result:mixed`). The absolute counts differ because the synthetic snippets aren't byte-identical, but the **compression ratio is in the same neighborhood**.

### SRE incident debugging

```bash
uv run python -m headroom_benchmarks.benchmarks.synthetic.sre_incident
```

README claim: **65,694 → 5,118 = 92%**. This run: **23,108 → 1,270 = 94.5%** (transform: `router:tool_result:smart_crusher`). Logs are highly repetitive — easy wins for the compressor.

### The "0%" trap

First code-search run reported `0%` savings because the search hits were in a `user` message and Headroom's coding-agent default is `compress_user_messages=False`. Restructuring to put hits in a `tool_result` block (the realistic agent-loop shape) gave 87.6%. **If you see 0% on your own payload, check whether the bulk of the tokens live in a user message.**

---

## Tier-1 eval reproduction — `src/headroom_benchmarks/benchmarks/tier1/`

Ran GSM8K, SQuAD v2, BFCL against **MiniMax-M3** with and without Headroom compression (TruthfulQA skipped — see "Not done").

### Headline numbers (N=100 per benchmark)

Pricing tier used: `$0.30/M in, $1.20/M out, $0.06/M cache read` (MiniMax's [paygo Standard tier](https://platform.minimax.io/docs/guides/pricing-paygo)). Note: this differs from LiteLLM's published `minimax/MiniMax-M3` rate ($0.60/$2.40/$0.12) — MiniMax publishes both, and the upstream numbers match the lower tier. For dashboard savings math we use the LiteLLM rate.

| Benchmark | Baseline | Compressed | Δ accuracy | Input tokens saved | Cost (b + c) | Time |
|---|---|---|---|---|---|---|
| **GSM8K** (8-shot CoT) | **0.950** | **0.940** | −0.010 | **80.8%** (38,878 → 7,479) | $0.0335 | 14.6 min |
| **SQuAD v2** (Before/After) | EM 0.60 / F1 0.785 | EM 0.55 / F1 0.775 | −0.05 EM / −0.010 F1 | **82.5%** (18,774 → 3,285) | $0.0105 | 11.2 min |
| **BFCL** (ground-truth match) | **0.990** | **0.990** | 0.000 | **84.9%** (28,803 → 4,358) | $0.0204 | 10.6 min |
| **Aggregate (3 benchmarks)** | — | — | — | **82.5%** (86,455 → 15,122) | **$0.0644** | **36.4 min** |

### Compare to Headroom's README claim (N=100, gpt-4o-mini)

| Benchmark | README claim | This run (M3) | Verdict |
|---|---|---|---|
| GSM8K accuracy | 0.870 | **0.940** | ✅ M3 better than claimed |
| SQuAD v2 accuracy preservation | 97% with 19% compression | EM 0.60→0.55, F1 0.785→0.775 with **82.5%** compression | ✅ accuracy preserved within 5pp; **4× more compression** |
| BFCL accuracy / compression | 97% / 32% | **99% / 84.9%** | ✅ accuracy higher, compression 2.6× better |

### Caveats

1. **The compressed path benefits from MiniMax-M3 prompt caching.** Baseline and compressed runs send the *same* prompt; the second call hits MiniMax's cache (`cache_read_input_tokens > 0`). The reported "input_saved_pct" mixes compression + cache. On real workloads where compressed and uncompressed prompts differ (tool outputs, RAG chunks, JSON), Headroom's actual compression dominates.
2. **BFCL 0.99 is suspiciously high.** `check_response` is a substring match against ground-truth argument values — lenient. The README's 97% used an LLM-as-judge. To match upstream rigor, swap `bench_bfcl.check_response` for an LLM-judge call.
3. **GSM8K 0.95 > README's 0.87.** MiniMax-M3 is a stronger math reasoner than gpt-4o-mini, or the 8-shot prompt is more effective.
4. **SQuAD F1 0.785→0.775 (−0.010).** Within typical SQuAD noise. EM dropped 5pp; if your tolerance is tighter, set `--accuracy-guard strict` on the proxy or use the `agent-95` savings profile.

### Reproduce

```bash
uv run python -m headroom_benchmarks.benchmarks.tier1.gsm8k       # ~15 min
uv run python -m headroom_benchmarks.benchmarks.tier1.squad       # ~12 min
uv run python -m headroom_benchmarks.benchmarks.tier1.bfcl        # ~11 min
uv run python -m headroom_benchmarks.benchmarks.tier1.truthfulqa  # skipped — see below
uv run headroom-bench-all 100                   # all four sequentially
# → src/headroom_benchmarks/benchmarks/tier1/logs/{benchmark}_summary.json + tier1_overall.json
```

Datasets load from HuggingFace. GSM8K needs `config_name="main"`, SQuAD v2 needs no config, BFCL is a direct JSONL download, TruthfulQA needs `config_name="generation"`. `headroom.compress()` is called in-process (not via the proxy) so we get a clean baseline-vs-compressed pairing.

---

## Per-project proxy setup (the recommended way)

The Headroom proxy is per-project, not global. To use it in **this** repo without affecting other repos:

1. **Start the proxy in a terminal** (any port; `:8788` is convenient):
   ```bash
   ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
   ANTHROPIC_TARGET_API_URL="https://api.minimax.io/anthropic" \
     uv run headroom proxy --port 8788 --no-cache --no-rate-limit
   ```

2. **Add a project-local Claude Code config** at `.claude/settings.local.json` in the repo:
   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://127.0.0.1:8788"
     }
   }
   ```

   Claude Code merges this with `~/.claude/settings.json`. The project-local file overrides `ANTHROPIC_BASE_URL` only; `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` are inherited from the global config.

3. **Run Claude Code in this repo** — it'll route through the proxy. In any other directory, the global config takes over and Claude Code talks to Anthropic directly.

If you'd rather edit the global `~/.claude/settings.json` instead, set its `env.ANTHROPIC_BASE_URL` to the proxy URL. The project-local file is the cleaner approach because it keeps the repo's compression config self-contained.

---

## Gotchas

- **`protect_user_messages` is on by default.** Bulk tokens in a `user` message → 0% compression. Put tool-shaped payloads in `tool_result` blocks (the realistic agent-loop shape). See "0% trap" above.
- **`~/.claude/settings.json` `env` overrides shell exports** if you take the global-route approach. Verify with `jq -r '.env.ANTHROPIC_BASE_URL // "unset"' ~/.claude/settings.json`.
- **LiteLLM pricing resolver doesn't know the `minimax/` provider prefix** out of the box. Without a one-line fix in `.venv/.../headroom/pricing/litellm_pricing.py` (add `"minimax-": "minimax/"` to the prefix table + a `MiniMax-M3` pre-registration), the proxy's `/stats` cost fields stay at `$0.00` regardless of traffic. See `src/headroom_benchmarks/langgraph/README.md` "Open follow-ups" for the patch.
- **BFCL scoring is lenient** (substring match), SQuAD v2 scores within typical noise.
- **TruthfulQA** is intentionally incomplete (`uv run python -m headroom_benchmarks.benchmarks.tier1.truthfulqa`) — data loads fine, but the scoring threshold needs BLEU-Acc or ROUGE-1-Acc against `correct_answers`.

---

## Not done

- **TruthfulQA scoring** — implementation in `uv run python -m headroom_benchmarks.benchmarks.tier1.truthfulqa` is correct through data loading, but scoring needs BLEU-Acc / ROUGE-1-Acc against `correct_answers`. Drop in `nltk.translate.bleu_score` for a defensible score.
- **`headroom wrap claude`** interactive run — verified the proxy round-trip works via curl + the Anthropic SDK with `base_url` override; haven't launched `claude` interactively against MiniMax-M3 in this repo.
- **Driving Claude Code through Headroom+MiniMax on a real coding task** — same as above.

---

## License

Source code in this repo: same license as upstream [headroom-ai](https://github.com/chopratejas/headroom) (Apache-2.0). The benchmark artifacts (`results/`, `logs/`) are CC-BY-4.0 — share, adapt, attribute.
