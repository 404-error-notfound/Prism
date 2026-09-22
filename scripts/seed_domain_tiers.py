#!/usr/bin/env python
"""Seed domain trust tiers into Redis.

T1 (0.9) — 政府/官方机构，通配后缀生效（如 gov.cn 覆盖所有 *.gov.cn）
T2 (0.7) — 专业质量站，独立域名精确匹配

Usage:
    python scripts/seed_domain_tiers.py [--dry-run] [--clear]
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ---------------------------------------------------------------------------
# T1：高权威域名后缀或独立域名（通配规则由 ranker._source_trust 实现）
# ---------------------------------------------------------------------------
T1_DOMAINS: list[str] = [
    # 政府/党政机构
    "gov.cn",
    # 学术/官方研究机构
    "edu.cn",
    "ac.cn",
    # 气象局直属
    "cma.gov.cn",
    "nmc.cn",            # 国家气象中心
    # 权威新闻（国家级媒体）
    "xinhuanet.com",
    "xinhua.net",
    "people.com.cn",
    "cctv.com",
    "chinadaily.com.cn",
    # 金融/货币官方发布渠道
    "chinamoney.com.cn",  # 中国货币网（中国外汇交易中心，LPR/Shibor 法定发布）
    "pbc.gov.cn",         # 中国人民银行（已被 gov.cn 通配覆盖，显式列出以防规则变更）
    "stats.gov.cn",       # 国家统计局（同上）
    # 国家标准/监管机构
    "samr.gov.cn",       # 国家市场监督管理总局
    "nhc.gov.cn",        # 国家卫生健康委
    "moa.gov.cn",        # 农业农村部
    "mps.gov.cn",        # 公安部
]

# ---------------------------------------------------------------------------
# T2：专业质量站，精确域名匹配
# ---------------------------------------------------------------------------
T2_DOMAINS: list[str] = [
    # ── 天气 ────────────────────────────────────────────────────────────────
    "weather.com.cn",    # 中国天气网（中国气象局主办）
    "tianqi.com",
    "tianqi.eastday.com",
    "tx114.com",         # 天象查询网
    "2345tianqi.com",
    "tianqihoubao.com",
    "aqicn.org",         # 空气质量指数
    "pm25.in",
    # ── 权威资讯/综合门户 ────────────────────────────────────────────────────
    "chinanews.com.cn",
    "china.com.cn",
    "cnr.cn",            # 央广网
    "cri.cn",            # 国际在线
    "gmw.cn",            # 光明网
    "haiwainet.cn",
    # ── 知识/百科 ────────────────────────────────────────────────────────────
    "baike.baidu.com",
    "baike.com",
    "wikipedia.org",
    "zhihu.com",
    # ── 科技/开发者 ──────────────────────────────────────────────────────────
    "github.com",
    "stackoverflow.com",
    "csdn.net",
    "juejin.cn",
    "segmentfault.com",
    "developer.mozilla.org",
    # ── 财经 ─────────────────────────────────────────────────────────────────
    "eastmoney.com",
    "10jqka.com.cn",     # 同花顺
    "cnstock.com",       # 中国证券网
    "caixin.com",        # 财新
    # ── 医疗/健康 ────────────────────────────────────────────────────────────
    "39.net",
    "dxy.cn",            # 丁香园
    "youlai.cn",
]


async def main(dry_run: bool = False, clear: bool = False) -> None:
    from app.core.redis import close_pool, get_redis
    r = get_redis()

    if clear and not dry_run:
        await r.delete("domain:tier:T1", "domain:tier:T2")
        print("Cleared existing T1 and T2 sets.")

    for tier, domains in [("T1", T1_DOMAINS), ("T2", T2_DOMAINS)]:
        key = f"domain:tier:{tier}"
        print(f"\nSeeding {key} ({len(domains)} domains):")
        for d in domains:
            if dry_run:
                print(f"  [dry-run] {d}")
            else:
                await r.sadd(key, d)
                print(f"  + {d}")

    if not dry_run:
        t1_members = await r.smembers("domain:tier:T1")
        t2_members = await r.smembers("domain:tier:T2")
        print(f"\nDone. Redis: T1={len(t1_members)} T2={len(t2_members)}")

    await close_pool()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写入 Redis")
    parser.add_argument("--clear", action="store_true", help="写入前清空原有集合")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, clear=args.clear))
