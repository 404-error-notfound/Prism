#!/usr/bin/env python
"""离线回放测试集：从测试报告 xlsx 提取 query 批量回放，验证排序改动效果。

用途：
  1. dump 每个 query 的 top-10（标题/域名/URL/RankScore）到对照表 xlsx，供人工抽检打标
  2. 对已确认的失分用例做自动断言（字典页/B站导航页/无关新闻/导航FP 不得再现）
  3. 产出 reranker 分数分布，用于校准 RERANKER_SCORE_FLOOR

依赖：pip install openpyxl httpx redis

用法：
  python scripts/replay_testset.py 444_结果.xlsx 333_结果.xlsx \
      --api-key sk-xxx [--base-url http://localhost:8000] [--flush-cache] [--out replay.xlsx]

  --flush-cache  回放前删除 Redis 中 cache:search:*（避免命中改动前的旧缓存）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# 自动断言：已确认的失分用例 → top-10 中不得出现的域名
# 来源：333/444 测试报告（2026-06-10）
# ---------------------------------------------------------------------------

_DICT_SPAM = {
    "zidian.gushici.net", "gushici.net", "dict.revised.moe.edu.tw", "hancibao.com",
    "zdic.net", "qianp.com", "hgcha.com", "chagushici.com", "hanyuguoxue.com",
}

# SEO 内容农场：乱码抛弃域 + 热搜拼盘 snippet（spam_gate A∧B 应硬丢）。
# 来源：本轮"刚刚 央行降准降息政策"返回的农场页（2026-06-17）。
_FARM_SPAM = {
    "bjxxhz.cn", "hqkj66.com", "hfteng.com", "mssslawyer.com",
    "fuxiangtai.cn", "szzxcf.com",
}

FORBIDDEN_DOMAINS: dict[str, set[str]] = {
    # BM25 单字中毒：字典站靠 query 首字的单字释义页拿高分
    "戚风蛋糕 零失败家常烘焙教程": _DICT_SPAM,
    "低风险银行存款理财产品 2026推荐": _DICT_SPAM,
    "顺丰快递单号SF123456789（中通快递78998338182579查询）": _DICT_SPAM,
    # bilibili 引擎填充噪音：非视频查询返回平台导航页
    "南京 同城搬家 口碑推荐 报价透明": {"bilibili.com", "app.bilibili.com", "jobs.bilibili.com"},
    # 内容农场 + 字典毒（本轮代表性 case）
    "刚刚 央行降准降息政策": _FARM_SPAM,
    "特斯拉 股价 今日实时走势": _DICT_SPAM,
}

# 零相关填充新闻：top-10 每条 title+snippet 必须命中至少一个核心词
MUST_HIT_TOKENS: dict[str, set[str]] = {
    "新冠疫苗最新加强针政策": {"新冠", "疫苗", "加强针", "接种"},
    "手机#价格%2026*最新款": {"手机"},
    # 本轮代表性 case：关键词注入 / 答非所问
    "刚刚 央行降准降息政策": {"央行", "降准", "降息", "货币政策"},
    "特斯拉 股价 今日实时走势": {"特斯拉"},
    "最新 诺贝尔文学奖 得主": {"诺贝尔", "文学奖"},
    "SpaceX 星舰 最新发射结果": {"SpaceX", "星舰", "星舟", "火箭", "发射"},
    "最新 医保报销目录 调整细则": {"医保", "报销", "医疗保险"},
}

# 导航查询：目标站点（注册域）必须出现在 top-3
NAV_TOP3: dict[str, str] = {
    "字节跳动官网": "bytedance.com",
}


def _site_matches(site: str, domain: str) -> bool:
    return site == domain or site.endswith("." + domain)


def extract_queries(xlsx_paths: list[str]) -> list[str]:
    import openpyxl

    queries: list[str] = []
    seen: set[str] = set()
    for path in xlsx_paths:
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = next(rows)
        q_col = next(i for i, h in enumerate(header) if h == "Query")
        for row in rows:
            q = row[q_col]
            if q and str(q).strip() and str(q) not in seen:
                seen.add(str(q))
                queries.append(str(q))
    return queries


def flush_search_cache(redis_url: str) -> int:
    import redis

    r = redis.Redis.from_url(redis_url)
    deleted = 0
    for key in r.scan_iter(match="*cache:search:*", count=500):
        r.delete(key)
        deleted += 1
    return deleted


def replay(
    queries: list[str], base_url: str, api_key: str
) -> list[dict]:
    """逐 query 调用 /api/search，返回 [{query, results: [...], latency_ms, error}]。"""
    out: list[dict] = []
    client = httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    for i, q in enumerate(queries, 1):
        t0 = time.monotonic()
        record: dict = {"query": q, "results": [], "latency_ms": 0, "error": ""}
        try:
            resp = client.post("/api/search", json={"Query": q, "Count": 10})
            record["latency_ms"] = int((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                record["error"] = f"HTTP {resp.status_code}"
            else:
                web = resp.json().get("Result", {}).get("WebResults", []) or []
                record["results"] = [
                    {
                        "rank": r.get("SortId"),
                        "title": r.get("Title", ""),
                        "site": r.get("SiteName", ""),
                        "url": r.get("Url", ""),
                        "score": r.get("RankScore", ""),
                        "snippet": r.get("Snippet", "")[:120],
                    }
                    for r in web
                ]
        except httpx.HTTPError as e:
            record["error"] = str(e)
            record["latency_ms"] = int((time.monotonic() - t0) * 1000)
        out.append(record)
        status = record["error"] or f"{len(record['results'])} results"
        print(f"[{i}/{len(queries)}] {record['latency_ms']:>6}ms  {status:<14} {q[:50]}")
    client.close()
    return out


def run_assertions(records: list[dict]) -> list[str]:
    """返回失败信息列表；空 = 全部通过。"""
    failures: list[str] = []
    by_query = {rec["query"]: rec for rec in records}

    for q, forbidden in FORBIDDEN_DOMAINS.items():
        rec = by_query.get(q)
        if rec is None:
            continue
        for r in rec["results"]:
            if any(_site_matches(r["site"], d) for d in forbidden):
                failures.append(f"[禁止域名] {q[:40]} → top{r['rank']} 仍是 {r['site']}")

    for q, tokens in MUST_HIT_TOKENS.items():
        rec = by_query.get(q)
        if rec is None:
            continue
        for r in rec["results"]:
            text = f"{r['title']} {r['snippet']}"
            if not any(t in text for t in tokens):
                failures.append(
                    f"[零相关残留] {q[:40]} → top{r['rank']} 未命中任何核心词: {r['title'][:40]}"
                )

    for q, domain in NAV_TOP3.items():
        rec = by_query.get(q)
        if rec is None:
            continue
        top3 = rec["results"][:3]
        if top3 and not any(_site_matches(r["site"], domain) for r in top3):
            failures.append(f"[导航FP] {q[:40]} → top3 不含 {domain}: "
                            f"{[r['site'] for r in top3]}")

    return failures


def write_xlsx(records: list[dict], out_path: str) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "replay"
    ws.append(["Query", "Rank", "Title", "Site", "Url", "RankScore",
               "Snippet", "LatencyMs", "Error", "人工判定"])
    for rec in records:
        if not rec["results"]:
            ws.append([rec["query"], "", "", "", "", "", "",
                       rec["latency_ms"], rec["error"] or "(0 results)", ""])
        for r in rec["results"]:
            ws.append([rec["query"], r["rank"], r["title"], r["site"], r["url"],
                       r["score"], r["snippet"], rec["latency_ms"], rec["error"], ""])
    wb.save(out_path)
    print(f"\n对照表已写入: {out_path}")


def main() -> None:
    # Windows 控制台默认 GBK，✗/中文标题会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", nargs="*", help="测试报告 xlsx（取 Query 列）；省略则只跑内置断言用例")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default=os.environ.get("PRISM_API_KEY", ""))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--flush-cache", action="store_true", help="回放前清空搜索结果缓存")
    parser.add_argument("--out", default="replay_results.xlsx")
    args = parser.parse_args()

    if not args.api_key:
        sys.exit("需要 --api-key 或环境变量 PRISM_API_KEY")

    if args.flush_cache:
        n = flush_search_cache(args.redis_url)
        print(f"已清空 {n} 个搜索缓存 key")

    queries = extract_queries(args.xlsx) if args.xlsx else []
    # 内置断言用例（FORBIDDEN/MUST_HIT/NAV）始终回放——否则断言因找不到记录被静默跳过
    seen = set(queries)
    for q in (*FORBIDDEN_DOMAINS, *MUST_HIT_TOKENS, *NAV_TOP3):
        if q not in seen:
            seen.add(q)
            queries.append(q)
    print(f"提取到 {len(queries)} 个不重复 query，开始回放 {args.base_url} ...\n")

    records = replay(queries, args.base_url, args.api_key)
    write_xlsx(records, args.out)

    errors = [r for r in records if r["error"]]
    zero = [r for r in records if not r["error"] and not r["results"]]
    print(f"\n回放完成: {len(records)} queries, {len(errors)} 错误, {len(zero)} 零结果")

    failures = run_assertions(records)
    if failures:
        print(f"\n断言失败 {len(failures)} 项:")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("\n全部断言通过 ✓（已知失分用例均已修复）")


if __name__ == "__main__":
    main()
