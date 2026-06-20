"""
Shared helpers for Tier-1 benchmark reproduction against MiniMax-M3.

Loads MiniMax API key from ../.env, provides:
  - call_minimax(messages, model, max_tokens) -> (text, usage_dict)
  - compress(messages, model) -> headroom CompressResult
  - Per-request JSONL logger
  - Dataset loading helpers
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- .env loader (avoids needing python-dotenv at runtime) -----------------
REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE = REPO_ROOT / ".env"


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_env()

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")
MINIMAX_BASE_URL = os.environ.get(
    "MINIMAX_BASE_URL", "https://api.minimax.io/anthropic"
)
MODEL = "MiniMax-M3"
# Pricing per platform.minimax.io/docs/guides/pricing-paygo (Standard tier, <=512k)
PRICE_INPUT_PER_M = 0.30
PRICE_OUTPUT_PER_M = 1.20

if not MINIMAX_API_KEY:
    raise SystemExit("MINIMAX_API_KEY not set — check .env")


# --- MiniMax client via anthropic SDK with base_url override ---------------
def _client():
    import anthropic

    return anthropic.Anthropic(
        base_url=MINIMAX_BASE_URL,
        api_key=MINIMAX_API_KEY,
    )


@dataclass
class MiniMaxUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0


@dataclass
class CallResult:
    text: str
    usage: MiniMaxUsage
    raw_response: Any = None


def call_minimax(
    messages: list[dict],
    *,
    system: str = "",
    model: str = MODEL,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> CallResult:
    """Call MiniMax-M3 via the Anthropic SDK. Returns text + usage stats."""
    c = _client()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    t0 = time.perf_counter()
    resp = c.messages.create(**kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)

    text = ""
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text += block.text
        elif btype == "thinking":
            # Surface thinking as a prefix so logs show what the model did
            text += f"[thinking] {getattr(block, 'thinking', '')}\n"

    u = resp.usage
    cost = (
        (u.input_tokens + u.cache_creation_input_tokens) * PRICE_INPUT_PER_M / 1_000_000
        + u.cache_read_input_tokens * 0.06 / 1_000_000
        + u.output_tokens * PRICE_OUTPUT_PER_M / 1_000_000
    )
    usage = MiniMaxUsage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_input_tokens=u.cache_read_input_tokens,
        cache_creation_input_tokens=u.cache_creation_input_tokens,
        latency_ms=latency_ms,
        cost_usd=cost,
    )
    return CallResult(text=text, usage=usage, raw_response=resp)


# --- Headroom compression ----------------------------------------------------
def compress(messages: list[dict], *, model: str = MODEL, **kwargs) -> Any:
    from headroom import compress as _compress

    return _compress(messages, model=model, optimize=True, **kwargs)


# --- Per-request JSONL log --------------------------------------------------
@dataclass
class RunLog:
    """Per-benchmark JSONL log. Opens in WRITE mode (truncates) so any
    duplicate-run is detectable via the sentinel rather than producing
    silently doubled records."""

    path: Path
    fp: Any = field(init=False)
    _sentinel_run_id: str = ""

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 'w' mode truncates. We write a sentinel header so a duplicate
        # invocation is obvious in the log.
        import time as _t

        self._sentinel_run_id = _t.strftime("%Y-%m-%d %H:%M:%S")
        self.fp = open(self.path, "w", encoding="utf-8")
        self.fp.write(
            json.dumps({"_sentinel": True, "run_id": self._sentinel_run_id,
                        "path": str(self.path)}) + "\n"
        )
        self.fp.flush()

    def write(self, record: dict) -> None:
        self.fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.fp.flush()

    def close(self) -> None:
        try:
            self.fp.close()
        except Exception:
            pass


# --- Dataset loaders (HuggingFace + BFCL direct download) -------------------
def load_hf(name: str, split: str, n: int, config_name: str | None = None):
    from datasets import load_dataset

    if config_name:
        ds = load_dataset(name, config_name, split=split)
    else:
        ds = load_dataset(name, split=split)
    return list(ds.select(range(min(n, len(ds)))))


def load_bfcl_jsonl(category: str = "simple") -> list[dict]:
    base = (
        "https://huggingface.co/datasets/gorilla-llm/"
        "Berkeley-Function-Calling-Leaderboard/resolve/main"
    )
    raw = (
        urllib.request.urlopen(f"{base}/BFCL_v3_{category}.json")
        .read()
        .decode("utf-8")
    )
    items = [json.loads(line) for line in raw.strip().split("\n") if line.strip()]
    gt_raw = (
        urllib.request.urlopen(f"{base}/possible_answer/BFCL_v3_{category}.json")
        .read()
        .decode("utf-8")
    )
    gt_by_id: dict[str, str] = {}
    for line in gt_raw.strip().split("\n"):
        if line.strip():
            obj = json.loads(line)
            gt_by_id[obj["id"]] = json.dumps(obj.get("ground_truth", []))
    return items, gt_by_id


# --- Scoring utilities -------------------------------------------------------
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def extract_numeric(text: str) -> str | None:
    """GSM8K gold answers look like '... #### 18'. We pull the last number."""
    # Prefer the format GSM8K uses: "#### <number>"
    if "####" in text:
        tail = text.split("####")[-1].strip().split("\n")[0].strip()
        return tail.replace(",", "")
    # Else last number in the text
    nums = _NUM_RE.findall(text.replace(",", ""))
    return nums[-1] if nums else None


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def truthfulqa_match(pred: str, gold_list: list[str], threshold: float = 0.25) -> bool:
    """TruthfulQA gold has multiple correct answers + multiple incorrect.
    Pass if any gold answer shares >= threshold token-F1 with the prediction.
    """
    if not gold_list:
        return False
    pred_n = normalize(pred)
    if not pred_n:
        return False
    return any(squad_f1(pred_n, normalize(g)) >= threshold for g in gold_list if g)


def squad_em(pred: str, gold: str) -> bool:
    """SQuAD exact match (normalized)."""
    return normalize(pred) == normalize(gold)


def squad_f1(pred: str, gold: str) -> float:
    """SQuAD token-level F1 (multiset overlap via Counter)."""
    from collections import Counter

    p_toks = normalize(pred).split()
    g_toks = normalize(gold).split()
    if not p_toks or not g_toks:
        return float(p_toks == g_toks)
    pc = Counter(p_toks)
    gc = Counter(g_toks)
    overlap = sum((pc & gc).values())
    if overlap == 0:
        return 0.0
    p = overlap / sum(pc.values())
    r = overlap / sum(gc.values())
    return 2 * p * r / (p + r)