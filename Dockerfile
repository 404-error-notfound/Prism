# syntax=docker/dockerfile:1
# ----------------------------------------------------------------
# Prism — 开发环境镜像
# 基于 python:3.11-slim（Debian Bookworm），全量依赖
# 代码通过 volume 挂载，支持热重载，无需重新 build
# ----------------------------------------------------------------
FROM docker.m.daocloud.io/library/python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# ---- 切换阿里云 apt 镜像（加速国内构建）----
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null \
    || true

# ---- 系统依赖 ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 构建工具（部分 C 扩展需要）
    build-essential \
    curl \
    git \
    # trafilatura / lxml 依赖
    libxml2-dev \
    libxslt1-dev \
    # Playwright / Crawl4AI 浏览器依赖
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    fonts-noto-cjk \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- CPU-only PyTorch（拆成两层，torch 体积大单独缓存）----
# --mount=type=cache 把下载的 whl 留在宿主机；即使本层失败，下次重试也不用重下。
# 必须在 sentence-transformers 之前装，否则 pip 会拉 CUDA 全家桶（~4GB）。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --timeout 120 \
    "torch>=2.0.0"

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --timeout 120 \
    "torchvision>=0.15.0"

# ---- Python 依赖（全量：dev + governance + browser）----
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -e ".[dev,governance,browser]"

# ---- 预下载 HF 模型（烘焙进镜像，运行时无需出网）----
# ModelScope (modelscope.cn) 走阿里云 CDN，Docker build 容器可直连，无需代理。
# 下载后重组为 HF cache 目录结构，sentence_transformers 按名称加载时可直接命中。
# 两个模型独立 RUN 层，各自缓存，互不影响。
RUN python3 - <<'PYEOF'
import os, requests
MS = "https://modelscope.cn/api/v1/models"
CACHE = "/root/.cache/huggingface/hub"
COMMIT = "baked"
FILES = [
    "config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "vocab.txt", "model.safetensors",
    "sentence_bert_config.json", "modules.json",
    "config_sentence_transformers.json", "1_Pooling/config.json",
]
snap = f"{CACHE}/models--BAAI--bge-small-zh-v1.5/snapshots/{COMMIT}"
refs = f"{CACHE}/models--BAAI--bge-small-zh-v1.5/refs"
for d in {os.path.dirname(f"{snap}/{f}") for f in FILES} | {snap, refs}:
    os.makedirs(d, exist_ok=True)
with open(f"{refs}/main", "w") as fh:
    fh.write(COMMIT)
for f in FILES:
    out = f"{snap}/{f}"
    if os.path.exists(out) and os.path.getsize(out) > 0: print(f"skip {f}"); continue
    print(f"GET {f}", flush=True)
    with requests.get(f"{MS}/BAAI/bge-small-zh-v1.5/repo?Revision=master&FilePath={f}", stream=True, timeout=600) as r:
        if r.status_code == 404: print(f"404 skip {f}"); continue
        r.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                fh.write(chunk)
    print(f"OK {f} ({os.path.getsize(out)//1024}KB)", flush=True)
PYEOF

RUN python3 - <<'PYEOF'
import os, requests
MS = "https://modelscope.cn/api/v1/models"
CACHE = "/root/.cache/huggingface/hub"
COMMIT = "baked"
FILES = [
    "config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "sentencepiece.bpe.model", "model.safetensors",
]
snap = f"{CACHE}/models--BAAI--bge-reranker-base/snapshots/{COMMIT}"
refs = f"{CACHE}/models--BAAI--bge-reranker-base/refs"
for d in {os.path.dirname(f"{snap}/{f}") for f in FILES} | {snap, refs}:
    os.makedirs(d, exist_ok=True)
with open(f"{refs}/main", "w") as fh:
    fh.write(COMMIT)
for f in FILES:
    out = f"{snap}/{f}"
    if os.path.exists(out) and os.path.getsize(out) > 0: print(f"skip {f}"); continue
    print(f"GET {f}", flush=True)
    with requests.get(f"{MS}/BAAI/bge-reranker-base/repo?Revision=master&FilePath={f}", stream=True, timeout=600) as r:
        if r.status_code == 404: print(f"404 skip {f}"); continue
        r.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                fh.write(chunk)
    print(f"OK {f} ({os.path.getsize(out)//1024}KB)", flush=True)
PYEOF

# ---- Crawl4AI 初始化（只初始化数据库，不下载 chromium）----
# chromium 在国内无法在 build 阶段下载（playwright.download.prss.microsoft.com 被 GFW 拦截）
# 需要浏览器抓取时，进容器手动执行：
#   docker exec -it prism-gateway playwright install chromium --with-deps
RUN crawl4ai-setup || true

# ---- 日志目录 ----
RUN mkdir -p /app/logs

EXPOSE 8000

# 热重载启动（volume 挂载代码后生效）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app"]
