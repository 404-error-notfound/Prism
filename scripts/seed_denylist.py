#!/usr/bin/env python
"""Seed the denylist with known content-farm and spam domains.

DEPRECATED: The denylist is now managed via prism-admin (MySQL → Redis sync).
This script is kept for backward compatibility and returns empty.

Usage:
    python scripts/seed_denylist.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Initial seed list — expand this over time
# Sources: community content-farm lists, manual curation
DENYLIST_DOMAINS = [
    # Generic content farms
    "zhidao.baidu.com",   # low quality Q&A
    "wenku.baidu.com",    # paywall, low value
    # Add more as discovered in production
    # Format: one domain per line, no wildcards
]

# T1 whitelist seed — domains that should never be penalised
WHITELIST_DOMAINS = [
    "github.com",
    "stackoverflow.com",
    "docs.python.org",
    "arxiv.org",
    "wikipedia.org",
    "developer.mozilla.org",
    "docs.microsoft.com",
    "developer.android.com",
    "developer.apple.com",
    "zhihu.com",
    "juejin.cn",
    "csdn.net",
]


async def main(dry_run: bool = False) -> None:
    # DEPRECATED: 已迁移到 prism-admin，脚本保留但返回空
    print("Denylist is now managed via prism-admin (MySQL → Redis sync).")
    print("This script returns empty.")

    # from app.core.redis import get_redis
    # r = get_redis()
    #
    # print(f"Seeding denylist ({len(DENYLIST_DOMAINS)} domains)...")
    # for domain in DENYLIST_DOMAINS:
    #     if dry_run:
    #         print(f"  [dry-run] would add {domain} to denylist")
    #     else:
    #         await r.sadd("denylist:domains", domain)
    #         print(f"  + {domain}")
    #
    # print(f"\nSeeding T1 whitelist ({len(WHITELIST_DOMAINS)} domains)...")
    # for domain in WHITELIST_DOMAINS:
    #     if dry_run:
    #         print(f"  [dry-run] would add {domain} to whitelist")
    #     else:
    #         await r.sadd("whitelist:t1", domain)
    #         print(f"  + {domain}")
    #
    # if not dry_run:
    #     count = await r.scard("denylist:domains")
    #     print(f"\nDenylist now has {count} domains.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
