"""
Tier-1 benchmark: GSM8K (math reasoning, N=100).
Reproduces the README claim of 0.870 accuracy preserved.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # type: ignore
    MODEL,
    MiniMaxUsage,
    RunLog,
    call_minimax,
    compress,
    extract_numeric,
    load_hf,
)

# 8-shot CoT prompt (lm-eval-harness style)
SYSTEM = "You are a helpful assistant. Solve grade-school math problems step by step."

FEW_SHOT = """\
Q: Natalia sold clips to 48 of her friends in April, and then she sold half as many clips in May. How many clips did Natalia sell altogether in April and May?
A: Natalia sold 48/2 = 24 clips in May. Total = 48 + 24 = 72. #### 72

Q: Weng earns $12 an hour for babysitting. Yesterday, she babysat for 50 minutes. How much did she earn?
A: 50 minutes = 50/60 hours. 12 * 50/60 = 10. #### 10

Q: Betty is saving money for a new wallet which costs $100. She has only half of the money. Her parents decided to give her $15, and her grandparents twice as much as her parents. How much more money does Betty need?
A: Wallet = $100. Betty has $50. Parents give $15, grandparents give $30. Total = 50+15+30 = $95. Need 100-95 = $5. #### 5

Q: A robe takes 2 bolts of blue fiber and half that much white fiber. How many bolts in total?
A: 2 blue + 1 white = 3 bolts. #### 3

Q: Josh decides to try flipping a house. He buys a house for $80,000 and then puts in $50,000 in repairs. After the repairs the house value increased by 150%. How much profit did he make?
A: Value after repairs = 80000 + 50000 = 130000. Increased by 150% means final = 130000 * 2.5 = 325000. Profit = 325000 - 130000 = 195000. #### 195000

Q: James decides to run 3 sprints 3 times a week. He runs 60 meters each sprint. How many meters does he run a week?
A: 3 sprints * 3 times = 9 sprints. 9 * 60 = 540 meters. #### 540

Q: Every day, Wendi feeds each of her chickens three cups of chicken feed. If she has 20 chickens, how many cups will she need to give her chickens in a day?
A: 20 * 3 = 60 cups. #### 60

Q: Kylar went to the store to buy glasses for his new apartment. One glass costs $5, but every second glass costs only 60% of the price. Kylar wants to buy 16 glasses. How much does he need to pay for them?
A: For every pair of glasses, the second is 60% of $5 = $3. So 16 glasses = 8 pairs. Cost = 8 * (5+3) = $64. #### 64
"""


def build_messages(question: str) -> list[dict]:
    return [
        {"role": "user", "content": f"{FEW_SHOT}\n\nQ: {question}\nA:"},
    ]


def main(n: int = 100):
    print(f"=== GSM8K benchmark, N={n}, model={MODEL} ===")
    print("Loading dataset...")
    ds = load_hf("openai/gsm8k", "test", n, config_name="main")
    log = RunLog(Path(__file__).parent / "logs" / "gsm8k.jsonl")

    results = {"baseline": [], "compressed": []}
    totals = {
        "baseline": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "correct": 0},
        "compressed": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "correct": 0},
    }

    for i, row in enumerate(ds):
        question = row["question"]
        gold_text = row["answer"]
        gold = extract_numeric(gold_text)

        msgs = build_messages(question)

        # --- Baseline (no compression) ---
        base_resp = call_minimax(msgs, system=SYSTEM, max_tokens=512)
        base_pred = extract_numeric(base_resp.text)
        base_correct = base_pred == gold

        # --- Compressed ---
        comp = compress(msgs)
        comp_resp = call_minimax(comp.messages, system=SYSTEM, max_tokens=512)
        comp_pred = extract_numeric(comp_resp.text)
        comp_correct = comp_pred == gold

        results["baseline"].append({"i": i, "pred": base_pred, "gold": gold,
                                     "correct": base_correct,
                                     "tokens_in": base_resp.usage.input_tokens,
                                     "tokens_out": base_resp.usage.output_tokens,
                                     "cost_usd": base_resp.usage.cost_usd,
                                     "latency_ms": base_resp.usage.latency_ms})
        results["compressed"].append({"i": i, "pred": comp_pred, "gold": gold,
                                       "correct": comp_correct,
                                       "tokens_in": comp_resp.usage.input_tokens,
                                       "tokens_out": comp_resp.usage.output_tokens,
                                       "cost_usd": comp_resp.usage.cost_usd,
                                       "latency_ms": comp_resp.usage.latency_ms,
                                       "headroom_before": comp.tokens_before,
                                       "headroom_after": comp.tokens_after,
                                       "transforms": comp.transforms_applied})

        totals["baseline"]["tokens_in"] += base_resp.usage.input_tokens
        totals["baseline"]["tokens_out"] += base_resp.usage.output_tokens
        totals["baseline"]["cost_usd"] += base_resp.usage.cost_usd
        totals["baseline"]["correct"] += int(base_correct)

        totals["compressed"]["tokens_in"] += comp_resp.usage.input_tokens
        totals["compressed"]["tokens_out"] += comp_resp.usage.output_tokens
        totals["compressed"]["cost_usd"] += comp_resp.usage.cost_usd
        totals["compressed"]["correct"] += int(comp_correct)

        log.write({"benchmark": "gsm8k", "i": i, "question": question,
                   "gold": gold, "baseline": results["baseline"][-1],
                   "compressed": results["compressed"][-1]})

        if (i + 1) % 10 == 0 or i == 0:
            print(f"  [{i+1}/{n}] baseline={base_correct} compressed={comp_correct} "
                  f"gold={gold} pred_b={base_pred} pred_c={comp_pred}")

    log.close()

    # Summary
    print("\n=== GSM8K SUMMARY ===")
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

    # Persist summary
    summary_path = Path(__file__).parent / "logs" / "gsm8k_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({
        "benchmark": "gsm8k",
        "n": n,
        "model": MODEL,
        "baseline": {**totals["baseline"], "accuracy": totals["baseline"]["correct"] / n},
        "compressed": {**totals["compressed"], "accuracy": totals["compressed"]["correct"] / n,
                       "input_saved_pct": saved_pct},
    }, indent=2))
    print(f"  summary -> {summary_path}")


if __name__ == "__main__":
    main()