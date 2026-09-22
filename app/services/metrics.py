"""Prometheus metrics registry."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Request counters
search_requests_total = Counter(
    "prism_search_requests_total",
    "Total search requests",
    ["tenant", "status"],
)

# Latency
search_latency_seconds = Histogram(
    "prism_search_latency_seconds",
    "Search request latency in seconds",
    ["engine", "tier"],
    buckets=[0.1, 0.25, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0],
)

search_subengine_latency_seconds = Histogram(
    "prism_search_subengine_latency_seconds",
    "Search latency per sub-engine (e.g. searxng/baidu)",
    ["parent_engine", "sub_engine"],
    buckets=[0.1, 0.25, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0],
)

# Cache
cache_hits_total = Counter(
    "prism_cache_hits_total",
    "Cache hit count",
    ["tenant"],
)
cache_misses_total = Counter(
    "prism_cache_misses_total",
    "Cache miss count",
    ["tenant"],
)

# Engine health
engine_failures_total = Counter(
    "prism_engine_failures_total",
    "Engine failure count",
    ["engine"],
)
circuit_breaker_open = Gauge(
    "prism_circuit_breaker_open",
    "1 if circuit breaker is open for an engine",
    ["engine"],
)

# Quota
tenant_quota_remaining = Gauge(
    "prism_tenant_quota_remaining",
    "Remaining daily quota for tenant",
    ["tenant"],
)

# Governance path distribution (Stage 2+)
governance_path_total = Counter(
    "prism_governance_path_total",
    "Count of requests per governance path",
    ["path"],
)

# Playwright pool (Stage 3+)
playwright_pool_in_use = Gauge(
    "prism_playwright_pool_in_use",
    "Number of browser contexts currently in use",
)
