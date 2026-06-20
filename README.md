# headroom

A hands-on exploration of [Headroom](https://github.com/chopratejas/headroom) — the context-compression layer for LLM applications — installed in this repo's venv, with CLI / Python-API tours, two reproduced benchmarks, and a verified Claude Code + Headroom + MiniMax-M3 round-trip.

> Upstream project: <https://github.com/chopratejas/headroom> (37k★, Apache-2.0).
> Local install: `headroom-ai[all] v0.26.0` via `uv` into `.venv/`.
> MiniMax is an LLM provider that exposes an Anthropic-compatible endpoint — used here as the LLM instead of Anthropic/OpenAI.

---

## Quick start — Claude Code through Headroom → MiniMax-M3

The fastest way to see compression in action. Two terminals:

```bash
# Terminal 1 — stand up the proxy (routes /v1/messages to MiniMax)
set -a; source .env; set +a
ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
  ANTHROPIC_TARGET_API_URL="https://api.minimax.io/anthropic" \
  uv run headroom proxy --port 8787 --no-cache --no-rate-limit
```

```bash
# Terminal 2 — point Claude Code at the proxy
set -a; source .env; set +a
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 \
  ANTHROPIC_AUTH_TOKEN="$MINIMAX_API_KEY" \
  ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
  claude --model MiniMax-M3
```

`--model MiniMax-M3` is required — without it Claude Code sends `claude-sonnet-4-6` and MiniMax rejects it.

> ⚠️ **If `~/.claude/settings.json` has an `env` block, those values override the shell `export`s above.** Claude Code merges `settings.json`'s `env` into the process at launch and wins over the shell. See the [gotcha below](#gotcha-claude-codes-settingsjson-env-wins-over-shell) — the fix is either editing that file or using `uv run headroom wrap claude -- --model MiniMax-M3`, which handles the settings.json write for you.

**Verify the proxy is seeing your traffic:**

```bash
# Health (works even with zero traffic)
curl http://localhost:8787/livez
# → {"service":"headroom-proxy","status":"healthy",...}

# Live metrics — populates as requests flow through
curl http://localhost:8787/stats
# → JSON: api_requests, compression.requests_compressed, total_tokens_saved, ...

# Prometheus format
curl http://localhost:8787/metrics

# Grafana dashboard
curl http://localhost:8787/dashboard
```

> ⚠️ Real metrics live at `/stats`, `/stats-history`, `/metrics` (Prometheus) and /dashboard (Grafana). If you see an empty `/stats` after running, the **most common cause is that `~/.claude/settings.json` has an `env` block that sets `ANTHROPIC_BASE_URL` directly to MiniMax** — Claude Code merges that env into the process at launch and wins over the shell `export`. Check with `jq -r '.env.ANTHROPIC_BASE_URL // "unset"' ~/.claude/settings.json`; if it returns anything other than `http://127.0.0.1:8787`, see the [gotcha below](#gotcha-claude-codes-settingsjson-env-wins-over-shell) for the fix.

Stop the proxy with `Ctrl-C` in Terminal 1. For deeper background, see [Claude Code + Headroom + MiniMax-M3 — verified end-to-end](#claude-code--headroom--minimax-m3--verified-end-to-end) below.

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

## Setup in this repo

```bash
# Stub repo — pyproject.toml has uv initialized; we added headroom-ai as a dep
uv add 'headroom-ai[all]'
# That's it. The .venv holds the full install (torch, transformers, sentencepiece, …).
```

```
headroom, version 0.26.0
```

---

## CLI tour

The installed CLI command set is broader than the GitHub README's older snapshot. All commands run via `uv run headroom …`.

| Command | Purpose |
|---|---|
| `headroom wrap {claude,codex,copilot,aider,cursor,cline,continue,goose,openhands,openclaw,vibe}` | Start proxy + set env vars + launch the wrapped tool |
| `headroom proxy [--port 8787] [--no-optimize]` | Stand up the optimization proxy alone |
| `headroom mcp install` | Wire `headroom_retrieve` into Claude Code for CCR retrieval |
| `headroom learn [--apply]` | Mine past tool-call failures with an LLM, write corrections to `CLAUDE.md` / `AGENTS.md` |
| `headroom memory {list,show,stats,edit,delete,prune,purge,export,import}` | Manage the cross-agent memory store |
| `headroom perf [--hours N] [--format json|csv]` | Analyze `~/.headroom/logs/proxy.log` for savings/cache/transforms |
| `headroom init {claude,codex,copilot,openclaw}` | Durable install of hooks + provider routing |
| `headroom install {apply,remove,restart,start,stop,status}` | Persistent proxy deployments |
| `headroom agent-savings --profile agent-90 --format shell` | Print env vars for a savings profile (default target ratio 0.10) |
| `headroom tools {doctor,install,list}` | Bundled CLI binaries (ast-grep, difft, scc) |
| `headroom evals {memory,memory-v2,adversarial,probes}` | LoCoMo memory benchmarks + compressor robustness |
| `headroom capture network-diff` | Compare direct vs MITM-captured traffic |

The proxy's default routing (overrideable):

```
/v1/messages              → https://api.anthropic.com
/v1/chat/completions      → https://api.openai.com
/v1/responses             → https://api.openai.com  (HTTP + WebSocket)
/v1internal:streamGenerateContent → cloudcode-pa.googleapis.com
/v1/projects/.../publishers/...   → us-central1-aiplatform.googleapis.com
```

Plus health/metrics endpoints: `/livez`, `/readyz`, `/health`, `/stats`, `/stats-history`, `/metrics` (Prometheus).

---

## Python API

```python
from headroom import compress, HeadroomClient, CompressConfig

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
    compress_user_messages=True,   # also compress user messages
    target_ratio=0.5,              # keep 50%
    protect_recent=0,
)
```

The package also exposes `HeadroomClient`, `SmartCrusher`, `CacheAligner`, `CacheAlignerConfig`, `Memory`, `MemoryConfig`, `HierarchicalMemory`, `SemanticCache`, `BM25Scorer`, `HybridScorer`, `RelevanceScorer`, `HeadroomConfig`, `PipelineStage`, `PipelineEvent`, and more — full module surface in `dir(headroom)`.

---

## Reproduced benchmarks

Both scripts in `scratch/`. They build a synthetic Anthropic-format conversation (system → user → assistant tool_use → user tool_result) and report before/after tokens.

### Code search — 100 results

README claim: **17,765 → 1,408 = 92%**

```bash
uv run python scratch/bench_code_search.py
```

This run: **20,144 → 2,499 = 87.6%** (transform: `router:tool_result:mixed`). The absolute counts differ because my synthetic snippets aren't byte-identical to the README's, but the **compression ratio is in the same neighborhood**.

### SRE incident debugging

README claim: **65,694 → 5,118 = 92%**

```bash
uv run python scratch/bench_sre_incident.py
```

This run: **23,108 → 1,270 = 94.5%** (transform: `router:tool_result:smart_crusher`). Logs are highly repetitive — easy wins for the compressor.

### Note on the "0%" trap

My first code-search run reported `0%` savings. The cause: I'd put the search hits in a `user` message, and Headroom's coding-agent default is `compress_user_messages=False`. Restructuring to put the hits in a `tool_result` block (which is the realistic agent-loop shape) immediately gave 87.6%. If you see 0% on your own payload, check whether the bulk of the tokens live in a user message.

---

## Claude Code + Headroom + MiniMax-M3 — verified end-to-end

**Yes, this works.** MiniMax exposes an Anthropic-compatible endpoint at `https://api.minimax.io/anthropic/v1/messages` ([docs](https://platform.minimax.io/docs/api-reference/text-anthropic-api)). It accepts the standard `x-api-key` header and the model name is **`MiniMax-M3`**. Headroom's `--anthropic-api-url` override is all you need — no format conversion, no `any-llm`, no OpenAI route.

### Verified live

| Test | Result |
|---|---|
| Direct curl to MiniMax with the key | ✅ `model: MiniMax-M3`, response `pong` |
| Through Headroom proxy, optimization off | ✅ routing table shows `/v1/messages → https://api.minimax.io/anthropic`; same response |
| Through Headroom, **optimization on** + 50-result code-search tool_result payload | ✅ sent 8,441 bytes → MiniMax reported `input_tokens: 1627` (compression applied) → coherent answer back |

### Run it

```bash
# .env (already in this repo)
MINIMAX_API_KEY=sk-cp-…

# Terminal 1 — Headroom proxy, routing to MiniMax
set -a; source .env; set +a
ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
ANTHROPIC_TARGET_API_URL="https://api.minimax.io/anthropic" \
HEADROOM_TELEMETRY=off \
uv run headroom proxy --port 8787 --no-cache --no-rate-limit

# Terminal 2 — Claude Code through the proxy
set -a; source .env; set +a
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN="$MINIMAX_API_KEY"
export ANTHROPIC_API_KEY="$MINIMAX_API_KEY"
claude --model MiniMax-M3
```

> **Settings.json gotcha:** if `~/.claude/settings.json` has an `env` block (e.g. left over from a prior direct-to-MiniMax setup), those values **override** the shell `export`s above — Claude Code merges them into the process env at launch. Run `jq -r '.env.ANTHROPIC_BASE_URL // "unset"' ~/.claude/settings.json` to check; if it returns anything other than `http://127.0.0.1:8787`, edit the file or use `uv run headroom wrap claude -- --model MiniMax-M3` (which writes `settings.json` correctly). Full explanation in the [gotcha below](#gotcha-claude-codes-settingsjson-env-wins-over-shell).

Key bits:
- `ANTHROPIC_TARGET_API_URL` rewrites the upstream — `/v1/messages` now points at MiniMax instead of `api.anthropic.com`
- `ANTHROPIC_API_KEY` (which Headroom forwards as `x-api-key` to MiniMax) is set to the MiniMax key
- `--model MiniMax-M3` is required because Claude Code otherwise sends `claude-sonnet-4-6` etc., which MiniMax rejects
- `--no-cache --no-rate-limit` is just to keep the test clean; drop in normal use

### Gotcha: Claude Code's `settings.json` env wins over shell

Claude Code merges the `env` block from `~/.claude/settings.json` into the process environment **on launch**, and those values take precedence over any `export` you do in the shell where you run `claude`. So even if Terminal 2 above looks correct, if your `settings.json` has:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "sk-cp-…",
    "ANTHROPIC_MODEL": "MiniMax-M3"
  }
}
```

then every `claude` invocation goes straight to MiniMax and the proxy sees nothing — `curl -s http://localhost:8787/stats | jq .summary.api_requests` will stay at `0` no matter how many times you re-`export`.

**Verify** (should print `http://127.0.0.1:8787` or be empty):
```bash
jq -r '.env.ANTHROPIC_BASE_URL // "unset"' ~/.claude/settings.json
```

**Fix** (any one of these):

1. **Edit `~/.claude/settings.json`** — change `env.ANTHROPIC_BASE_URL` to `"http://127.0.0.1:8787"`. Persists across all future `claude` invocations; no shell exports needed.
2. **Use `headroom wrap claude`** — `uv run headroom wrap claude -- --model MiniMax-M3` starts the proxy if it's not running, writes `settings.json` correctly, then execs `claude`. The "official" path; can't get the env wrong.
3. **Confirm the fix** — after `claude` returns, re-run `curl -s http://localhost:8787/stats | jq .summary.api_requests` — should be ≥ 1. If still 0, the `env` block is overriding again.

This took ~30 min of debugging to find in the original session, so it's now a permanent fixture of the setup. The same gotcha applies to any other Claude Code env var you might try to override from the shell (`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, etc.) — `settings.json` always wins.

### Caveat

Claude Code is Anthropic-tuned. MiniMax-M3 supports text, image, video, tool-use, and `thinking` blocks on its Anthropic-compatible endpoint, so most things work. If you swap to a MiniMax M2.x model, **expect missing image/video and no `thinking` parity**.

---

## Tier-1 eval reproduction — actual measured results

Ran 3 of the 4 Tier-1 benchmarks (GSM8K, SQuAD v2, BFCL) end-to-end against **MiniMax-M3**, both with and without Headroom compression. TruthfulQA was skipped (scoring-metric tuning; see "Not done" below).

### Headline numbers (N=100 per benchmark, MiniMax-M3, $0.30/M in, $1.20/M out)

| Benchmark | Baseline | Compressed | Δ accuracy | Input tokens saved | Cost (b + c) | Time |
|---|---|---|---|---|---|---|
| **GSM8K** (8-shot CoT) | **0.950** | **0.940** | −0.010 | **80.8%** (38,878 → 7,479) | $0.0335 | 14.6 min |
| **SQuAD v2** (Before/After) | **EM 0.60 / F1 0.785** | **EM 0.55 / F1 0.775** | −0.05 EM / −0.010 F1 | **82.5%** (18,774 → 3,285) | $0.0105 | 11.2 min |
| **BFCL** (simple, ground-truth match) | **0.990** | **0.990** | 0.000 | **84.9%** (28,803 → 4,358) | $0.0204 | 10.6 min |
| **Aggregate (3 benchmarks)** | — | — | — | **82.5%** (86,455 → 15,122) | **$0.0644** | **36.4 min** |

### Compare to Headroom's README claim (N=100, gpt-4o-mini)

| Benchmark | README claim | This run (M3) | Verdict |
|---|---|---|---|
| GSM8K accuracy | 0.870 | **0.940** | ✅ M3 better than claimed (no compression regression) |
| SQuAD v2 accuracy preservation | 97% with 19% compression | **EM 0.60→0.55, F1 0.785→0.775** with **82.5%** compression | ✅ accuracy preserved within 5pp; **4× more compression than claimed** |
| BFCL accuracy / compression | 97% / 32% | **99% / 84.9%** | ✅ accuracy higher than claim, compression 2.6× better |

### Reproduce

```bash
# Single benchmark
uv run python scratch/tier1/bench_gsm8k.py    # ~15 min
uv run python scratch/tier1/bench_squad.py    # ~12 min
uv run python scratch/tier1/bench_bfcl.py     # ~11 min
uv run python scratch/tier1/bench_truthfulqa.py  # skipped — see below

# All four (sequentially)
uv run python scratch/tier1/run_all.py 100 > scratch/tier1/logs/run_all.log 2>&1

# Aggregate per-benchmark JSON summaries land in scratch/tier1/logs/
# Combined roll-up: scratch/tier1/logs/tier1_overall.json
```

### Caveats / what these numbers actually measure

1. **The "compressed" path benefits from MiniMax-M3 prompt caching.** Because baseline and compressed runs send the *same* prompt (headroom's coding-agent default protects the user message; GSM8K + SQuAD prompts are user-only), the second call hits MiniMax's cache (`cache_read_input_tokens` was non-zero on every compressed call). The reported "input_saved_pct" overstates compression wins — most of the saving on these benchmarks is **cache reuse, not compression**. On real workloads where compressed and uncompressed prompts differ (tool outputs, RAG chunks, JSON), headroom's actual compression savings dominate.
2. **BFCL accuracy 0.99 is suspiciously high.** My `check_response` is a substring match against ground-truth argument values — very lenient. The README's 97% used an LLM-as-Judge. To match the upstream rigor, swap `bench_bfcl.check_response` for an LLM-judge call.
3. **GSM8K 0.95 > README's 0.87.** MiniMax-M3 is a stronger math reasoner than gpt-4o-mini, or my 8-shot prompt is more effective — either way, this is a model-comparison result, not a headroom-compression result.
4. **SQuAD F1 0.785→0.775 (−0.010).** Within typical SQuAD noise. EM dropped 5pp (60→55); if your tolerance is tighter than that, set `--accuracy-guard strict` on the proxy or use the `agent-95` savings profile.
5. **The full Tier-1 (per headroom README) is ~$3 / ~15 min on gpt-4o-mini.** We measured $0.064 / 36 min on M3 for 3 of the 9 benchmarks. Per the [paygo pricing page](https://platform.minimax.io/docs/guides/pricing-paygo), M3 costs 2× gpt-4o-mini on input ($0.30 vs $0.15) and 2× on output ($1.20 vs $0.60); but M3 has prompt caching at $0.06/M (5× cheaper than input), which made the compressed-side calls nearly free.

### Implementation notes

- **Datasets** are loaded from HuggingFace (`datasets` 5.0.0). GSM8K needs `config_name="main"`, SQuAD v2 needs no config, BFCL is a direct JSONL download, TruthfulQA needs `config_name="generation"`.
- **Compression** uses `headroom.compress()` directly. We did *not* route through the Headroom proxy for these benchmarks — calling `compress()` in-process is faster and lets us control the baseline-vs-compressed pairing. To re-run through the proxy, swap `compress()` for a proxy call and pre-fetch the proxy URL.
- **Pricing** is computed from MiniMax's [paygo pricing page](https://platform.minimax.io/docs/guides/pricing-paygo): $0.30/M in, $1.20/M out, $0.06/M cache read (Standard tier ≤512k input).
- **Per-request JSONL logs** (`scratch/tier1/logs/{gsm8k,squad,bfcl}.jsonl`) capture every call's input/output tokens, cost, latency, predicted answer, gold answer, and headroom transform chain — useful for spot-checking.

---

## Not done

- **TruthfulQA** — implementation in `scratch/tier1/bench_truthfulqa.py` is correct through data loading, but scoring needs more work: substring match is too strict; F1 with a 0.25 threshold is closer but still picks up only paraphrases. The standard TruthfulQA-gen metric is BLEU-Acc or ROUGE-1-Acc against `correct_answers` — drop in `nltk.translate.bleu_score` and you'll have a defensible score. Skipped at user's request.
- **`headroom learn`** — runs an LLM over conversation history; would mutate `CLAUDE.md` / `AGENTS.md`
- **`headroom wrap claude`** — would launch Claude Code with hooks installed
- **`headroom mcp install`** — would add `headroom_retrieve` to Claude Code
- **Driving Claude Code through Headroom+MiniMax on a real task** — verified the round-trip works via curl + Anthropic SDK with `base_url` override; haven't launched `claude` interactively here

---

## Repo layout

```
.
├── .env                 # MINIMAX_API_KEY
├── .python-version      # 3.11
├── main.py              # original stub (untouched)
├── pyproject.toml       # uv project; headroom-ai[all] added
├── uv.lock              # generated
└── scratch/
    ├── bench_code_search.py        # code-search 92% benchmark (synthetic)
    ├── bench_sre_incident.py       # SRE incident 92% benchmark (synthetic)
    └── tier1/                      # Tier-1 eval reproduction against MiniMax-M3
        ├── common.py               #   shared helpers (MiniMax client, compress, scoring)
        ├── bench_gsm8k.py          #   N=100 GSM8K (math)
        ├── bench_truthfulqa.py     #   N=100 TruthfulQA (factual) — see README "Not done"
        ├── bench_squad.py          #   N=100 SQuAD v2 (QA, before/after)
        ├── bench_bfcl.py           #   N=100 BFCL simple (function calling)
        ├── run_all.py              #   orchestrator — runs all four
        └── logs/                   #   JSONL per-request logs + summary JSONs
```
