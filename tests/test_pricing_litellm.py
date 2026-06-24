"""Tests for headroom.pricing.litellm_pricing.

These tests validate a venv-only patch that the upstream PR
`headroomlabs-ai/headroom#1186` applies. The repo is a benchmark, not the
upstream library, so the actual code under test is the installed copy in
``.venv/lib/python3.11/site-packages/headroom/pricing/litellm_pricing.py``.

If ``uv sync`` ever wipes the venv fix (it will), these tests fail loudly so
you know to re-apply the three edits documented in CLAUDE.md. See the
"Gotchas" section of CLAUDE.md for the exact patch and the `minimax-` /
case-insensitive / pre-registration changes.
"""

from __future__ import annotations

from types import SimpleNamespace

from headroom.pricing import litellm_pricing


def test_litellm_minimax_mixed_case_with_provider_prefix(monkeypatch) -> None:
    """MiniMax-M3 must resolve via the `minimax/` prefix even though its
    model name uses mixed case.

    `resolve_litellm_model()` is what callers in `proxy/cost.py`,
    `proxy/savings_tracker.py`, and `perf/analyzer.py` use to get a
    key LiteLLM's own cost DB recognises. The upstream DB only stores
    the entry under `minimax/MiniMax-M3`, so bare `MiniMax-M3` would
    otherwise miss and the resolver would return the input unchanged.
    """

    def fake_cost_per_token(
        model: str, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> tuple[float, float]:
        if model in fake_litellm.model_cost:
            entry = fake_litellm.model_cost[model]
            return (
                entry["input_cost_per_token"] * prompt_tokens,
                entry["output_cost_per_token"] * completion_tokens,
            )
        raise KeyError(f"unknown model: {model}")

    fake_litellm = SimpleNamespace(
        model_cost={
            "minimax/MiniMax-M3": {
                "input_cost_per_token": 0.0000006,
                "output_cost_per_token": 0.0000024,
            }
        },
        cost_per_token=fake_cost_per_token,
    )
    monkeypatch.setattr(litellm_pricing, "LITELLM_AVAILABLE", True)
    monkeypatch.setattr(litellm_pricing, "litellm", fake_litellm)

    # Bare mixed-case name resolves via the case-insensitive `minimax-` prefix.
    assert litellm_pricing.resolve_litellm_model("MiniMax-M3") == "minimax/MiniMax-M3"


def test_litellm_minimax_preregistration_safety_net(monkeypatch) -> None:
    """When LiteLLM only ships the prefixed `minimax/MiniMax-M3` entry, the
    module-load pre-registration should also expose the bare `MiniMax-M3`
    key so `estimate_cost()` works on a cold resolver cache (since
    `get_model_pricing` does not know about the `minimax/` prefix).
    """
    fake_litellm = SimpleNamespace(
        model_cost={
            "minimax/MiniMax-M3": {
                "input_cost_per_token": 0.0000006,
                "output_cost_per_token": 0.0000024,
            }
        }
    )
    monkeypatch.setattr(litellm_pricing, "LITELLM_AVAILABLE", True)
    monkeypatch.setattr(litellm_pricing, "litellm", fake_litellm)

    litellm_pricing._register_minimax_pricing()

    assert "MiniMax-M3" in fake_litellm.model_cost
    assert fake_litellm.model_cost["MiniMax-M3"]["input_cost_per_token"] == 0.0000006
    # After pre-registration, bare-name estimate_cost works end-to-end.
    assert (
        litellm_pricing.estimate_cost(
            "MiniMax-M3", input_tokens=1_000_000, output_tokens=100_000
        )
        == 0.84
    )
    # Pre-registration must not clobber a user-customised bare entry.
    fake_litellm.model_cost["MiniMax-M3"] = {"customised": True}
    litellm_pricing._register_minimax_pricing()
    assert fake_litellm.model_cost["MiniMax-M3"] == {"customised": True}
