"""
Orchestrator: run all 4 Tier-1 benchmarks sequentially and write a combined report.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_bfcl import main as run_bfcl  # type: ignore
from bench_gsm8k import main as run_gsm8k  # type: ignore
from bench_squad import main as run_squad  # type: ignore
from bench_truthfulqa import main as run_truthfulqa  # type: ignore


def main(n: int = 100):
    logs = Path(__file__).parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    overall_start = time.perf_counter()

    runs = [
        ("GSM8K", run_gsm8k),
        ("TruthfulQA", run_truthfulqa),
        ("SQuAD v2", run_squad),
        ("BFCL", run_bfcl),
    ]

    summary: dict = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_per_benchmark": n,
        "benchmarks": {},
    }

    for name, fn in runs:
        print(f"\n{'#' * 60}\n# Running {name}\n{'#' * 60}\n")
        t0 = time.perf_counter()
        try:
            fn(n=n)
        except Exception as e:
            print(f"!! {name} failed: {e}")
            import traceback
            traceback.print_exc()
            continue
        elapsed = time.perf_counter() - t0
        print(f"\n{name} took {elapsed:.1f}s")

        # Load the per-benchmark summary
        s = logs / f"{name.lower().replace(' ', '_').replace('v2','v2').replace('.','')}.json"
        # file naming: gsm8k_summary.json, truthfulqa_summary.json, squad_summary.json, bfcl_summary.json
        s_path = logs / f"{name.split()[0].lower()}_summary.json"
        if not s_path.exists():
            # GSM8K -> gsm8k; TruthfulQA -> truthfulqa; SQuAD v2 -> squad; BFCL -> bfcl
            key = name.lower().replace(" v2", "").replace(" ", "")
            s_path = logs / f"{key}_summary.json"
        if s_path.exists():
            summary["benchmarks"][name] = json.loads(s_path.read_text())
            summary["benchmarks"][name]["elapsed_seconds"] = elapsed

    summary["total_elapsed_seconds"] = time.perf_counter() - overall_start
    summary["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    out = logs / "tier1_overall.json"
    out.write_text(json.dumps(summary, indent=2))

    print(f"\n{'=' * 60}")
    print(f"OVERALL: {summary['total_elapsed_seconds']:.1f}s")
    print(f"Overall summary -> {out}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    main(n=n)