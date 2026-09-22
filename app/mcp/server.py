"""MCP server — exposes web_search and search_feedback as MCP tools.

Supports both:
  - STDIO transport (for Claude Desktop / Claude Code)
  - HTTP + SSE transport (for multi-client HTTP deployment)
"""
from __future__ import annotations

import logging

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

from app.schemas.search import FeedbackRequest, SearchRequest
from app.services.orchestrator import get_orchestrator

logger = logging.getLogger(__name__)

server = Server("prism")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web and return structured results with titles, URLs, snippets, "
                "and metadata. Uses multi-engine aggregation with governance filtering.\n\n"
                "Constraints (please respect):\n"
                "- query ≤ 512 characters\n"
                "- top_n ≤ 50\n"
                "- require_detail=true may add 5-10s latency (uses headless browser)\n"
                "- If reachable=false, avoid directly recommending the URL to end users\n"
                "- For time-sensitive queries, set recency_days=7\n"
                "- On 429 errors, respect the Retry-After header\n"
                "- Daily quota exists; use results efficiently"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (max 512 chars)",
                    },
                    "top_n": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Number of results to return",
                    },
                    "recency_days": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 3650,
                        "description": "Only include results from the last N days",
                    },
                    "require_detail": {
                        "type": "boolean",
                        "default": False,
                        "description": "Fetch full page content via headless browser (slow)",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["auto", "knowledge", "commercial", "news", "academic"],
                        "default": "auto",
                    },
                    "user_id": {
                        "type": "string",
                        "default": "",
                        "description": "Opaque user identifier for personalization",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["hybrid", "time"],
                        "default": "hybrid",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_feedback",
            description=(
                "Submit explicit feedback on a search result URL. "
                "Optional supplement to automatic session behavior signals."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The result URL"},
                    "useful": {"type": "boolean", "description": "Was this result useful?"},
                    "user_id": {"type": "string", "default": ""},
                    "query_id": {"type": "string", "default": ""},
                },
                "required": ["url", "useful"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    import json

    if name == "web_search":
        from app.schemas.search import time_range_to_days
        # Accept either time_range (Volcengine) or legacy recency_days
        time_range = arguments.get("time_range", "")
        if not time_range and "recency_days" in arguments:
            days = int(arguments["recency_days"])
            time_range = (
                "OneDay" if days <= 1 else
                "OneWeek" if days <= 7 else
                "OneMonth" if days <= 30 else
                "OneYear" if days <= 365 else ""
            )
        req = SearchRequest(
            Query=arguments["query"],
            Count=arguments.get("top_n", 10),
            TimeRange=time_range,
            Filter={"NeedContent": arguments.get("require_detail", False)},
            intent=arguments.get("intent", "auto"),
            user_id=arguments.get("user_id", ""),
            sort_by=arguments.get("sort_by", "hybrid"),
        )
        orch = get_orchestrator()
        # In MCP context, use "admin" tenant (no per-tool auth in STDIO mode)
        # HTTP mode validates at transport layer
        resp = await orch.search(req, tenant_id="mcp", session_id="stdio", api_key="mcp")
        return [TextContent(type="text", text=json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))]

    if name == "search_feedback":
        from app.core.redis import get_redis
        from app.services.dedup import extract_domain
        domain = extract_domain(arguments["url"])
        r = get_redis()
        field = "good" if arguments.get("useful") else "bad"
        await r.hincrby(f"reputation:domain:{domain}", field, 1)
        return [TextContent(type="text", text='{"status": "ok"}')]

    raise ValueError(f"Unknown tool: {name}")


async def run_stdio() -> None:
    """Run MCP server over STDIO transport."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="prism",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )
