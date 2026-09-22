"""Session ID resolution across all three transport modes.

STDIO  → f"stdio:{pid}:{start_ts}"   (一个进程 = 一个 session)
SSE    → UUID 由 FastMCP SseServerTransport 在 connect_session 时生成
REST   → X-Session-Id header，或 f"rest:{api_key_prefix}:{date_bucket}"
"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar

from fastapi import Request

# SSE transport writes the session UUID here at connection time
sse_session_var: ContextVar[str] = ContextVar("sse_session_id", default="")

_PROCESS_START_TS = str(int(time.time()))
_PID = str(os.getpid())


def resolve_session_id(request: Request | None, transport: str, api_key: str = "") -> str:
    if transport == "stdio":
        return f"stdio:{_PID}:{_PROCESS_START_TS}"

    if transport == "sse":
        sid = sse_session_var.get()
        return sid if sid else f"sse:unknown:{_PROCESS_START_TS}"

    # REST
    if request is not None:
        header_sid = request.headers.get("X-Session-Id", "")
        if header_sid:
            return header_sid

    prefix = api_key[:8] if len(api_key) >= 8 else api_key
    bucket = str(int(time.time()) // 3600)  # 1-hour buckets
    return f"rest:{prefix}:{bucket}"
