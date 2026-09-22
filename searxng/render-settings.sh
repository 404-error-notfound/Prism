#!/bin/sh
# searxng settings.yml 渲染（单一事实源，docker-compose.yml entrypoint 调用；
# tests/test_searxng_render.py 直接跑本脚本做回归）。
#
# 从 PROXY_SERVICE_URL（.env，proxy-hub 拓扑唯一事实源）推导隧道列表：
# host 相同、端口固定换 8101（proxy-hub 隧道端口契约），账密用 PROXY_PROJECT_TOKEN
# 注入，替换模板中的 __PROXY_TUNNEL_PROXIES__ 占位符行。PROXY_SERVICE_URL 为空 =
# 无 proxies 段（直连出网）。
#
# 用法: render-settings.sh [模板路径] [输出路径]
# ⚠️ 占位符匹配必须整行锚定（^…$）：子串匹配会误中注释里提到的占位符名
# （2026-09-17 生产事故：proxies 块被插进文件头注释中间，YAML 非法，searxng 全灭）。
# ⚠️ 输入必须去 CR：Linux 上 ^…$ 不会把 \r 当行尾，CRLF 模板会让锚定匹配整体失效，
# 且"没替换"和"没残留"同时成立、静默通过，直到 YAML 校验才炸
# （2026-09-17 本地事故：core.autocrlf=true → 模板 CRLF → 5 个 searxng crash loop）。
set -e

TEMPLATE="${1:-/etc/searxng/settings.template.yml}"
OUTPUT="${2:-/etc/searxng/settings.yml}"
PLACEHOLDER_RE='^  __PROXY_TUNNEL_PROXIES__$'
# 残留检查单独用容忍行尾 CR 的模式：Linux 的 grep/sed 不会把 \r 当行尾，模板若是
# CRLF，"既没替换、也不报残留"就会一路静默到 YAML 校验才炸（2026-09-17 本地事故）
PLACEHOLDER_RESIDUE_RE='^  __PROXY_TUNNEL_PROXIES__\r?$'

# 输入统一去 CR。本仓库在 Windows 上开发（core.autocrlf=true 会把检出文件转 CRLF），
# 产物却跑在 Linux 容器里：模板带 CR 会让上面的整行锚定匹配失效；env 值带 CR 更糟——
# 它被插进 YAML 行中间，一个 \r 就能把行劈成两半、直接破坏结构（token 尾巴带 CR 就够）。
PROXY_SERVICE_URL="$(printf '%s' "$PROXY_SERVICE_URL" | tr -d '\r')"
PROXY_PROJECT_TOKEN="$(printf '%s' "$PROXY_PROJECT_TOKEN" | tr -d '\r')"

if [ -n "$PROXY_SERVICE_URL" ] && [ -z "$PROXY_PROJECT_TOKEN" ]; then
  echo "FATAL: PROXY_PROJECT_TOKEN 未配置（.env）——拒绝以裸配置启动" >&2
  exit 1
fi

PROXIES_FILE="$(mktemp)"
trap 'rm -f "$PROXIES_FILE"' EXIT

if [ -n "$PROXY_SERVICE_URL" ]; then
  {
    echo "  proxies:"
    echo "    # proxy-hub 自建转发代理（由 render-settings.sh 从 PROXY_SERVICE_URL 推导渲染，端口换 8101）。"
    echo "    # 每连接按目标站防重轮换挑上游 IP；多副本任一连通即可。"
    echo '    "http://":'
    for u in $(echo "$PROXY_SERVICE_URL" | tr ',' ' '); do
      host=$(echo "$u" | sed 's|^[a-zA-Z]*://||' | sed 's|:[0-9]*$||' | sed 's|/$||')
      echo "      - http://prism:${PROXY_PROJECT_TOKEN}@${host}:8101"
    done
    echo '    "https://":'
    for u in $(echo "$PROXY_SERVICE_URL" | tr ',' ' '); do
      host=$(echo "$u" | sed 's|^[a-zA-Z]*://||' | sed 's|:[0-9]*$||' | sed 's|/$||')
      echo "      - http://prism:${PROXY_PROJECT_TOKEN}@${host}:8101"
    done
  } > "$PROXIES_FILE"
else
  echo "WARNING: PROXY_SERVICE_URL 未配置——SearXNG 无代理隧道，直连出网" >&2
fi

# 模板先 tr -d '\r' 再交给 sed：即使模板是 CRLF（Windows 检出/挂载），
# 占位符那行也是干净的一行，^…$ 锚定照常生效。
tr -d '\r' < "$TEMPLATE" | sed -e "/$PLACEHOLDER_RE/r $PROXIES_FILE" -e "/$PLACEHOLDER_RE/d" \
  > "$OUTPUT"

# 产物为空说明模板没读到（tr/sed 环节出错），别把空文件当"渲染成功"
if [ ! -s "$OUTPUT" ]; then
  echo "FATAL: settings 渲染产物为空——检查模板路径与读取权限: $TEMPLATE" >&2
  exit 1
fi

# 残留检查（锚定整行 + 容忍行尾 CR；注释里允许出现占位符名，不算残留）
if grep -qE "$PLACEHOLDER_RESIDUE_RE" "$OUTPUT"; then
  echo "FATAL: settings 渲染失败（占位符残留）" >&2
  exit 1
fi
