#!/usr/bin/env python3
"""构造 20 条政策/新闻类查询，调用测试服 /api/search，统计 0 返回比例。"""
import json
import time
import uuid
import httpx

API_KEY = "CHANGE_ME"
BASE_URL = "http://localhost:8081"

QUERIES = [
    "Python asyncio 最佳实践 2026",
    "Docker compose 自定义网络配置示例",
    "PostgreSQL 复合索引优化方法",
    "React useEffect 依赖数组注意事项",
    "Linux 查找大于 1GB 文件命令",
    "nginx 反向代理 WebSocket 配置",
    "Kubernetes pod 启动失败排查思路",
    "git rebase 合并冲突解决步骤",
    "pandas 按多列分组聚合示例",
    "PyTorch CUDA out of memory 解决方案",
    "Elasticsearch 日期直方图聚合语法",
    "FastAPI 自定义异常处理中间件",
    "Redis Cluster 槽位迁移命令",
    "TypeScript 泛型约束接口示例",
    "Go 语言 channel 关闭最佳实践",
    "Maven 依赖冲突排除方法",
    "Terraform AWS VPC 完整示例",
    "Prometheus 告警规则配置示例",
    "Airflow DAG 调度失败日志排查",
    "Jenkins Pipeline 参数化构建示例",
]

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

results = []
client = httpx.Client(timeout=30.0)

for i, q in enumerate(QUERIES, 1):
    req = {
        "Query": q,
        "Count": 10,
        "Filter": {"NeedContent": False},
        "user_id": f"test_{uuid.uuid4().hex[:8]}",
    }
    t0 = time.time()
    try:
        r = client.post(f"{BASE_URL}/api/search", headers=headers, json=req)
        data = r.json()
        result_count = len(data.get("Result", []))
        latency_ms = int((time.time() - t0) * 1000)
        status = r.status_code
    except Exception as e:
        result_count = -1
        latency_ms = int((time.time() - t0) * 1000)
        status = f"ERROR: {e}"

    results.append({
        "idx": i,
        "query": q,
        "status": status,
        "result_count": result_count,
        "latency_ms": latency_ms,
    })
    print(f"[{i}/20] {q[:30]:<30} status={status} count={result_count} latency={latency_ms}ms")
    # 避免触发 QPS 限流（默认 5）
    time.sleep(0.25)

zero_count = sum(1 for r in results if r["result_count"] == 0)
print("\n=== 统计 ===")
print(f"总查询数: {len(results)}")
print(f"0 结果数: {zero_count}")
print(f"0 结果比例: {zero_count / len(results) * 100:.1f}%")
print("\n详细结果:")
print(json.dumps(results, ensure_ascii=False, indent=2))
