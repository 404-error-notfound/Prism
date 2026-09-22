"""Summarizer — BM25 extractive only. No LLM, no network, no cost.

Kept as a thin module so callers (orchestrator, compat) can keep importing
`summarize_results` regardless of the summarization strategy.
"""
from __future__ import annotations

from app.services.extractive import summarize_results as _extractive

__all__ = ["summarize_results", "is_configured", "close_summarizer_client"]


async def summarize_results(query: str, results, max_chars: int = 400) -> str:
    """Async wrapper so callers can `await`. BM25 is fast enough to run inline."""
    return _extractive(query, results, max_chars=max_chars)


def is_configured() -> bool:
    """Always true — extractive summarization needs no config."""
    return True


async def close_summarizer_client() -> None:
    """Kept for shutdown-hook compatibility. No-op."""
    return
