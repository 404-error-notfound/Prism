"""Session behavior signal recording and retrieval.

Signals are stored in Redis as a JSON LIST (most-recent first, max 5 items).
They feed into domain reputation scoring and the session_boost ranking factor.

Signal types:
  rescue       — user re-searched the same query  (strong negative)
  rephrase     — user modified query and re-searched (mild negative)
  drill_down   — user requested require_detail on a result (positive)
  silent_accept— no follow-up search in 2 min (mild positive)
"""
from __future__ import annotations

import json
import logging
import time

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "session:signals:"
_MAX_SIGNALS = 5
_TTL = 3600  # 1 hour

# Score adjustments applied to domain reputation when signals are processed
SIGNAL_REPUTATION_DELTA: dict[str, float] = {
    "rescue":        -0.25,
    "rephrase":      -0.12,
    "drill_down":    +0.08,
    "silent_accept": +0.10,
    "topic_shift":   +0.06,
}

# Score used in ranker session_boost factor
SIGNAL_BOOST: dict[str, float] = {
    "rescue":        0.0,
    "rephrase":      0.05,
    "drill_down":    0.15,
    "silent_accept": 0.05,
    "topic_shift":   0.03,
}


async def record_signal(
    session_id: str,
    action: str,
    domain: str | None = None,
    query: str | None = None,
) -> None:
    """Append a behavior signal to the session's Redis LIST."""
    if not session_id or session_id.startswith("stdio:"):
        return  # STDIO sessions have no meaningful session lifecycle

    payload = json.dumps({
        "action": action,
        "domain": domain or "",
        "query": query or "",
        "ts": int(time.time()),
    }, ensure_ascii=False)

    r = get_redis()
    key = _KEY_PREFIX + session_id
    pipe = r.pipeline()
    pipe.lpush(key, payload)
    pipe.ltrim(key, 0, _MAX_SIGNALS - 1)
    pipe.expire(key, _TTL)
    await pipe.execute()

    logger.debug("signal.recorded session=%s action=%s domain=%s", session_id, action, domain)

    # Propagate to domain reputation asynchronously
    if domain and action in SIGNAL_REPUTATION_DELTA:
        await _update_domain_reputation(domain, action)


async def get_recent_signals(session_id: str) -> list[dict]:
    """Return up to MAX_SIGNALS recent signals for a session."""
    if not session_id:
        return []
    r = get_redis()
    raw = await r.lrange(_KEY_PREFIX + session_id, 0, _MAX_SIGNALS - 1)
    signals = []
    for item in raw:
        try:
            signals.append(json.loads(item))
        except Exception:
            pass
    return signals


async def build_session_boost(session_id: str, domains: list[str]) -> dict[str, float]:
    """Return {domain: boost_score} for the current session's signals.

    boost_score is in [0, 1]; 0.5 is neutral.
    """
    if not session_id or not domains:
        return {d: 0.5 for d in domains}

    signals = await get_recent_signals(session_id)
    domain_scores: dict[str, float] = {}

    for sig in signals:
        d = sig.get("domain", "")
        if d not in domains:
            continue
        boost = SIGNAL_BOOST.get(sig.get("action", ""), 0.0)
        domain_scores[d] = min(1.0, domain_scores.get(d, 0.5) + boost)

    # Fill missing domains with neutral score
    return {d: domain_scores.get(d, 0.5) for d in domains}


# ---------------------------------------------------------------------------
# Internal: propagate signal to domain reputation hash
# ---------------------------------------------------------------------------

async def _update_domain_reputation(domain: str, action: str) -> None:
    delta = SIGNAL_REPUTATION_DELTA.get(action, 0.0)
    r = get_redis()
    key = f"reputation:domain:{domain}"
    if delta > 0:
        await r.hincrbyfloat(key, "good", delta)
    elif delta < 0:
        await r.hincrbyfloat(key, "bad", abs(delta))
