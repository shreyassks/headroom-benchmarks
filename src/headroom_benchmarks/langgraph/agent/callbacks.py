"""Per-LLM-call usage capture.

Every call to the supervisor (or any future worker) goes through this
recorder. After a test case completes, the buffer is written to the
run's `per_request.jsonl` and aggregated into `per_case.json`.

Each record captures:
  - what the SDK returned (post-compression counts from `usage`)
  - what the proxy recorded for the same request (`input_tokens_original`)
    — fetched later from the proxy's `recent_requests` ring buffer

This split lets us report "tokens before compression" (proxy-side)
alongside "tokens after compression" (SDK-side) per LLM call, which
is the whole point of the benchmark.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .pricing import cost_usd


@dataclass
class UsageRecord:
    ts: float                                    # wall-clock when record was made
    case_id: str                                 # which test case
    step: int                                    # 0-indexed LLM call within the case
    role: str                                    # "supervisor" (always, in v1)
    model: str
    input_tokens: int                            # SDK-returned (= post-compression)
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0

    # Filled in post-hoc from proxy /stats. None until the runner correlates.
    input_tokens_original: int | None = None      # proxy-side, pre-compression
    compression_pct: float | None = None          # (orig - sdk) / orig * 100


class UsageCapture:
    """Per-run recorder. Pass one instance per test case (or share across
    cases and split by `case_id`)."""

    def __init__(self, jsonl_path: str | None = None):
        self.records: list[UsageRecord] = []
        self.jsonl_path = jsonl_path
        if jsonl_path:
            # Truncate, write sentinel
            with open(jsonl_path, "w") as f:
                f.write(json.dumps({"_sentinel": True, "path": jsonl_path}) + "\n")

    def record(
        self,
        *,
        case_id: str,
        step: int,
        role: str,
        model: str,
        usage: Any,                # anthropic.types.Usage
        latency_ms: int = 0,
    ) -> UsageRecord:
        rec = UsageRecord(
            ts=time.time(),
            case_id=case_id,
            step=step,
            role=role,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            latency_ms=latency_ms,
            cost_usd=cost_usd(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
        )
        self.records.append(rec)
        if self.jsonl_path:
            with open(self.jsonl_path, "a") as f:
                f.write(json.dumps(asdict(rec)) + "\n")
        return rec

    def backfill_proxy_metrics_for_case(
        self,
        case_id: str,
        case_proxy_records: list[dict],
    ):
        """Assign proxy-side `input_tokens_original` to this case's SDK records
        by chronological order. Both lists are time-ordered within a case
        (one LLM call → one SDK record → one proxy record), so a simple
        positional match works.

        `case_proxy_records` should already be filtered to this case's window
        (e.g. between case_start_ts and case_end_ts).
        """
        case_recs = [r for r in self.records if r.case_id == case_id]
        n = min(len(case_recs), len(case_proxy_records))
        for i in range(n):
            rec = case_recs[i]
            pr = case_proxy_records[i]
            orig = pr.get("input_tokens_original")
            if orig is None:
                continue
            rec.input_tokens_original = int(orig)
            if orig > 0:
                saved = max(0, orig - rec.input_tokens)
                rec.compression_pct = round(saved / orig * 100, 2)
            if self.jsonl_path:
                with open(self.jsonl_path, "a") as f:
                    f.write(json.dumps({**asdict(rec), "_updated": True}) + "\n")

    def case_totals(self, case_id: str) -> dict[str, int]:
        rows = [r for r in self.records if r.case_id == case_id]
        return {
            "n_llm_calls":     len(rows),
            "input_tokens":    sum(r.input_tokens for r in rows),
            "output_tokens":   sum(r.output_tokens for r in rows),
            "cache_read_tokens": sum(r.cache_read_input_tokens for r in rows),
            "input_tokens_original": sum(r.input_tokens_original or r.input_tokens for r in rows),
            "cost_usd":        round(sum(r.cost_usd for r in rows), 6),
        }
