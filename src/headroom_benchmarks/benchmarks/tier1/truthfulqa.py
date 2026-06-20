"""
Tier-1 benchmark: TruthfulQA (factual accuracy, N=100).
Reproduces the README claim of 0.560 accuracy (baseline 0.530, delta +0.030).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from headroom_benchmarks.benchmarks.tier1.common import (  # type: ignore
    MODEL,
    RunLog,
    call_minimax,
    compress,
    load_hf,
    truthfulqa_match,
)

SYSTEM = (
    "You are a helpful, honest assistant. Answer the question concisely. "
    "Avoid common misconceptions."
)


def build_messages(question: str) -> list[dict]:
    return [{"role": "user", "content": f"Q: {question}\nA:"}]


def main(n: int = 100):
    print(f"=== TruthfulQA benchmark, N={n}, model={MODEL} ===")
    ds = load_hf("truthfulqa/truthful_qa", "validation", n, config_name="generation")
    log = RunLog(Path(__file__).parent / "logs" / "truthfulqa.jsonl")

    totals = {
        "baseline": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "correct": 0},
        "compressed": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "correct": 0},
    }
    results = {"baseline": [], "compressed": []}

    for i, row in enumerate(ds):
        question = row["question"]
        # generation config schema: best_answer, correct_answers[], incorrect_answers[]
        # Combine best_answer + correct_answers into one gold list — match if F1 >= 0.4 against any
        correct_answers = [row["best_answer"]] + row.get("correct_answers", [])
        if not correct_answers or not correct_answers[0]:
            continue

        msgs = build_messages(question)

        base_resp = call_minimax(msgs, system=SYSTEM, max_tokens=256)
        base_ok = truthfulqa_match(base_resp.text, correct_answers)

        comp = compress(msgs)
        comp_resp = call_minimax(comp.messages, system=SYSTEM, max_tokens=256)
        comp_ok = truthfulqa_match(comp_resp.text, correct_answers)

        results["baseline"].append({"i": i, "pred": base_resp.text[:120],
                                     "correct": base_ok,
                                     "tokens_in": base_resp.usage.input_tokens,
                                     "tokens_out": base_resp.usage.output_tokens,
                                     "cost_usd": base_resp.usage.cost_usd})
        results["compressed"].append({"i": i, "pred": comp_resp.text[:120],
                                       "correct": comp_ok,
                                       "tokens_in": comp_resp.usage.input_tokens,
                                       "tokens_out": comp_resp.usage.output_tokens,
                                       "cost_usd": comp_resp.usage.cost_usd,
                                       "headroom_before": comp.tokens_before,
                                       "headroom_after": comp.tokens_after})

        totals["baseline"]["tokens_in"] += base_resp.usage.input_tokens
        totals["baseline"]["tokens_out"] += base_resp.usage.output_tokens
        totals["baseline"]["cost_usd"] += base_resp.usage.cost_usd
        totals["baseline"]["correct"] += int(base_ok)

        totals["compressed"]["tokens_in"] += comp_resp.usage.input_tokens
        totals["compressed"]["tokens_out"] += comp_resp.usage.output_tokens
        totals["compressed"]["cost_usd"] += comp_resp.usage.cost_usd
        totals["compressed"]["correct"] += int(comp_ok)

        log.write({"benchmark": "truthfulqa", "i": i, "question": question,
                   "correct_answers": correct_answers,
                   "baseline_pred": base_resp.text[:200],
                   "compressed_pred": comp_resp.text[:200],
                   "baseline_ok": base_ok, "compressed_ok": comp_ok})

        if (i + 1) % 10 == 0 or i == 0:
            print(f"  [{i+1}/{n}] baseline={base_ok} compressed={comp_ok}  "
                  f"Q: {question[:60]}…")

    log.close()

    print("\n=== TruthfulQA SUMMARY ===")
    for label in ("baseline", "compressed"):
        t = totals[label]
        acc = t["correct"] / n
        print(f"  {label:11s}: accuracy={acc:.3f}  "
              f"tokens_in={t['tokens_in']:,}  tokens_out={t['tokens_out']:,}  "
              f"cost=${t['cost_usd']:.4f}")
    base_t = totals["baseline"]["tokens_in"]
    comp_t = totals["compressed"]["tokens_in"]
    saved_pct = (1 - comp_t / base_t) * 100 if base_t else 0.0
    print(f"  input tokens saved: {base_t - comp_t:,} ({saved_pct:.1f}%)")

    summary_path = Path(__file__).parent / "logs" / "truthfulqa_summary.json"
    summary_path.write_text(json.dumps({
        "benchmark": "truthfulqa",
        "n": n, "model": MODEL,
        "baseline": {**totals["baseline"], "accuracy": totals["baseline"]["correct"] / n},
        "compressed": {**totals["compressed"], "accuracy": totals["compressed"]["correct"] / n,
                       "input_saved_pct": saved_pct},
    }, indent=2))
    print(f"  summary -> {summary_path}")


if __name__ == "__main__":
    main()