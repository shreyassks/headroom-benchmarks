"""
Tier-1 benchmark: BFCL (Berkeley Function Calling Leaderboard, N=100).
Reproduces the README claim of 97% accuracy, 32% compression.

Single-call pattern (eval_mode="ground_truth"):
  - Compress function-schema JSON via headroom.compress()
  - Send compressed schemas + question to MiniMax-M3
  - Check if response contains the ground-truth function arguments
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
    load_bfcl_jsonl,
)

SYSTEM = (
    "You are a function-calling assistant. Given a set of available functions "
    "and a user request, decide which function to call and with what arguments. "
    "Reply with a single function call in JSON."
)


def build_messages(functions_json: str, question: str) -> list[dict]:
    return [{"role": "user",
             "content": (
                 "Available functions:\n"
                 f"{functions_json}\n\n"
                 f"User request: {question}\n\n"
                 "Respond with the function call to make, in JSON format:\n"
                 '{"name": "...", "arguments": {...}}'
             )}]


def ground_truth_param_values(gt_json_str: str) -> set[str]:
    """BFCL ground truth: [{"func_name": {"param": [accepted_values]}}]."""
    try:
        gt_list = json.loads(gt_json_str)
    except (json.JSONDecodeError, TypeError):
        return set()
    values: set[str] = set()
    for entry in gt_list:
        if not isinstance(entry, dict):
            continue
        for _fn_name, params in entry.items():
            if not isinstance(params, dict):
                continue
            for _pname, accepted in params.items():
                if isinstance(accepted, list):
                    for v in accepted:
                        values.add(str(v).lower())
                else:
                    values.add(str(accepted).lower())
    return values


def check_response(pred: str, gt_json_str: str) -> bool:
    """Pass if >= 1 ground-truth argument value appears in the prediction."""
    values = ground_truth_param_values(gt_json_str)
    if not values:
        return False
    pred_lower = pred.lower()
    return any(v in pred_lower for v in values)


def main(n: int = 100, category: str = "simple"):
    print(f"=== BFCL benchmark, N={n}, category={category}, model={MODEL} ===")
    print("Downloading BFCL...")
    items, gt_by_id = load_bfcl_jsonl(category)
    log = RunLog(Path(__file__).parent / "logs" / "bfcl.jsonl")

    totals = {
        "baseline": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "correct": 0},
        "compressed": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "correct": 0},
    }
    n_used = 0

    for i, item in enumerate(items[:n]):
        item_id = item.get("id", f"bfcl_{category}_{i}")
        # Question from nested [[{"role":"user","content":"..."}]]
        question = ""
        q_field = item.get("question")
        if isinstance(q_field, list) and q_field and isinstance(q_field[0], list):
            question = q_field[0][0].get("content", "") if q_field[0] else ""
        else:
            question = str(q_field or "")

        functions = item.get("function", [])
        if not functions:
            continue
        functions_json = json.dumps(functions, indent=2)

        gt_str = gt_by_id.get(item_id)
        if not gt_str or gt_str == "[]":
            continue

        msgs = build_messages(functions_json, question)

        # Baseline (no compression)
        base_resp = call_minimax(msgs, system=SYSTEM, max_tokens=256)
        base_ok = check_response(base_resp.text, gt_str)

        # Compressed
        comp = compress(msgs)
        comp_resp = call_minimax(comp.messages, system=SYSTEM, max_tokens=256)
        comp_ok = check_response(comp_resp.text, gt_str)

        totals["baseline"]["tokens_in"] += base_resp.usage.input_tokens
        totals["baseline"]["tokens_out"] += base_resp.usage.output_tokens
        totals["baseline"]["cost_usd"] += base_resp.usage.cost_usd
        totals["baseline"]["correct"] += int(base_ok)

        totals["compressed"]["tokens_in"] += comp_resp.usage.input_tokens
        totals["compressed"]["tokens_out"] += comp_resp.usage.output_tokens
        totals["compressed"]["cost_usd"] += comp_resp.usage.cost_usd
        totals["compressed"]["correct"] += int(comp_ok)

        log.write({"benchmark": "bfcl", "i": n_used, "id": item_id,
                   "question": question[:200],
                   "gt_values": list(ground_truth_param_values(gt_str)),
                   "baseline_pred": base_resp.text[:200],
                   "compressed_pred": comp_resp.text[:200],
                   "baseline_ok": base_ok, "compressed_ok": comp_ok,
                   "baseline_tokens_in": base_resp.usage.input_tokens,
                   "compressed_tokens_in": comp_resp.usage.input_tokens,
                   "headroom_before": comp.tokens_before,
                   "headroom_after": comp.tokens_after})

        n_used += 1
        if n_used % 10 == 0 or n_used == 1:
            print(f"  [{n_used}/{n}] baseline={base_ok} compressed={comp_ok}  "
                  f"Q: {question[:60]}…")

    log.close()

    print("\n=== BFCL SUMMARY ===")
    for label in ("baseline", "compressed"):
        t = totals[label]
        acc = t["correct"] / n_used if n_used else 0
        print(f"  {label:11s}: accuracy={acc:.3f}  "
              f"tokens_in={t['tokens_in']:,}  cost=${t['cost_usd']:.4f}")
    base_t = totals["baseline"]["tokens_in"]
    comp_t = totals["compressed"]["tokens_in"]
    saved_pct = (1 - comp_t / base_t) * 100 if base_t else 0.0
    print(f"  input tokens saved: {base_t - comp_t:,} ({saved_pct:.1f}%)")

    summary_path = Path(__file__).parent / "logs" / "bfcl_summary.json"
    summary_path.write_text(json.dumps({
        "benchmark": "bfcl",
        "n": n_used, "model": MODEL, "category": category,
        "baseline": {**totals["baseline"], "accuracy": totals["baseline"]["correct"] / n_used if n_used else 0},
        "compressed": {**totals["compressed"], "accuracy": totals["compressed"]["correct"] / n_used if n_used else 0,
                       "input_saved_pct": saved_pct},
    }, indent=2))
    print(f"  summary -> {summary_path}")


if __name__ == "__main__":
    main()