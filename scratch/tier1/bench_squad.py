"""
Tier-1 benchmark: SQuAD v2 (reading comprehension, N=100).
Reproduces the README claim of 97% accuracy preservation, 19% compression.

Before/After pattern:
  - baseline: send raw context -> answer
  - compressed: headroom.compress() context, send -> answer
  - metric: exact-match vs gold answer
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # type: ignore
    MODEL,
    RunLog,
    call_minimax,
    compress,
    load_hf,
    squad_em,
    squad_f1,
)

SYSTEM = (
    "You are a precise reading-comprehension assistant. "
    "Answer the question using ONLY the given context. "
    "Reply with the shortest span from the context that answers it."
)


def build_messages(context: str, question: str) -> list[dict]:
    return [{"role": "user",
             "content": f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"}]


def main(n: int = 100):
    print(f"=== SQuAD v2 benchmark, N={n}, model={MODEL} ===")
    ds = load_hf("rajpurkar/squad_v2", "validation", n * 3)  # oversample, some have no answer
    log = RunLog(Path(__file__).parent / "logs" / "squad.jsonl")

    totals = {
        "baseline": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                     "em": 0, "f1_sum": 0.0},
        "compressed": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                       "em": 0, "f1_sum": 0.0},
    }
    n_used = 0

    for i, row in enumerate(ds):
        if n_used >= n:
            break
        # Skip unanswerable
        answers = row["answers"]["text"]
        if not answers:
            continue
        gold = answers[0]
        context = row["context"]
        question = row["question"]

        msgs = build_messages(context, question)

        # Baseline
        base_resp = call_minimax(msgs, system=SYSTEM, max_tokens=64)
        base_pred = base_resp.text.strip()
        base_em = squad_em(base_pred, gold)
        base_f1 = squad_f1(base_pred, gold)

        # Compressed
        comp = compress(msgs)
        comp_resp = call_minimax(comp.messages, system=SYSTEM, max_tokens=64)
        comp_pred = comp_resp.text.strip()
        comp_em = squad_em(comp_pred, gold)
        comp_f1 = squad_f1(comp_pred, gold)

        totals["baseline"]["tokens_in"] += base_resp.usage.input_tokens
        totals["baseline"]["tokens_out"] += base_resp.usage.output_tokens
        totals["baseline"]["cost_usd"] += base_resp.usage.cost_usd
        totals["baseline"]["em"] += int(base_em)
        totals["baseline"]["f1_sum"] += base_f1

        totals["compressed"]["tokens_in"] += comp_resp.usage.input_tokens
        totals["compressed"]["tokens_out"] += comp_resp.usage.output_tokens
        totals["compressed"]["cost_usd"] += comp_resp.usage.cost_usd
        totals["compressed"]["em"] += int(comp_em)
        totals["compressed"]["f1_sum"] += comp_f1

        log.write({"benchmark": "squad", "i": n_used, "question": question,
                   "gold": gold, "context_len": len(context),
                   "baseline_pred": base_pred[:120],
                   "compressed_pred": comp_pred[:120],
                   "baseline_em": base_em, "baseline_f1": base_f1,
                   "compressed_em": comp_em, "compressed_f1": comp_f1,
                   "baseline_tokens_in": base_resp.usage.input_tokens,
                   "compressed_tokens_in": comp_resp.usage.input_tokens,
                   "headroom_before": comp.tokens_before,
                   "headroom_after": comp.tokens_after})

        n_used += 1
        if (n_used) % 10 == 0 or n_used == 1:
            print(f"  [{n_used}/{n}] baseline_em={base_em} compressed_em={comp_em}  "
                  f"Q: {question[:60]}…")

    log.close()

    print("\n=== SQuAD v2 SUMMARY ===")
    for label in ("baseline", "compressed"):
        t = totals[label]
        em_rate = t["em"] / n_used
        f1 = t["f1_sum"] / n_used
        print(f"  {label:11s}: EM={em_rate:.3f}  F1={f1:.3f}  "
              f"tokens_in={t['tokens_in']:,}  cost=${t['cost_usd']:.4f}")

    base_t = totals["baseline"]["tokens_in"]
    comp_t = totals["compressed"]["tokens_in"]
    saved_pct = (1 - comp_t / base_t) * 100 if base_t else 0.0
    base_em = totals["baseline"]["em"] / n_used
    comp_em = totals["compressed"]["em"] / n_used
    accuracy_preserved = abs(base_em - comp_em) < 0.05  # within 5pp
    print(f"  input tokens saved: {base_t - comp_t:,} ({saved_pct:.1f}%)")
    print(f"  accuracy preserved (within 5pp): {accuracy_preserved}")

    summary_path = Path(__file__).parent / "logs" / "squad_summary.json"
    summary_path.write_text(json.dumps({
        "benchmark": "squad",
        "n": n_used, "model": MODEL,
        "baseline": {**totals["baseline"],
                     "em": totals["baseline"]["em"] / n_used,
                     "f1": totals["baseline"]["f1_sum"] / n_used},
        "compressed": {**totals["compressed"],
                       "em": totals["compressed"]["em"] / n_used,
                       "f1": totals["compressed"]["f1_sum"] / n_used,
                       "input_saved_pct": saved_pct,
                       "accuracy_preserved": accuracy_preserved},
    }, indent=2))
    print(f"  summary -> {summary_path}")


if __name__ == "__main__":
    main()