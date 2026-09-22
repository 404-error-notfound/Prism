#!/usr/bin/env python
"""Create an API key for a tenant.

Usage:
    python scripts/create_api_key.py --tenant my-app --quota 5000 --qps 10

The plaintext key is printed once to stdout. Store it securely.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Prism API key")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--quota", type=int, default=1000, help="Quota per day")
    parser.add_argument("--qps", type=int, default=5, help="Max QPS")
    parser.add_argument("--allow-browser", action="store_true")
    args = parser.parse_args()

    from app.core.auth import TenantConfig, create_api_key

    cfg = TenantConfig(
        tenant_id=args.tenant,
        quota_per_day=args.quota,
        rate_limit_qps=args.qps,
        allow_browser=args.allow_browser,
    )
    key = await create_api_key(args.tenant, cfg)
    print(f"\nAPI Key for tenant '{args.tenant}':")
    print(f"  {key}")
    print("\nThis key is shown only once. Store it securely.")


if __name__ == "__main__":
    asyncio.run(main())
