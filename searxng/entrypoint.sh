#!/bin/sh
# searxng 容器 entrypoint（docker-compose.yml 以常量形式调用本脚本，
# compose 内不再内联任何含 $ 的脚本——v1 插值器对大段 $$ 转义会
# "Invalid interpolation format" 拒收，2026-09-17 踩过）。
#
# 职责：恢复 /etc/searxng 写位 → 调 render-settings.sh 渲染 settings.yml →
# YAML 硬校验（非法拒启动，不留 searxng "not a valid file" 谜团）→
# 还原属主 → exec 镜像官方 entrypoint。
set -e

# /etc/searxng 属 searxng:searxng 且 CAP_DAC_OVERRIDE 已 drop，root 也无写权限；
# 借 CAP_CHOWN 临时改属主 + chmod 恢复写位（真实 entrypoint 运行期会收紧写位，
# 且 v1 重建默认复用匿名卷，卷里目录可能已是无写位状态），完成后还原属主。
chown root:root /etc/searxng
chmod 755 /etc/searxng
rm -f /etc/searxng/settings.yml

# 引擎 override 挂载（如 searxng-baidu 的 baidu.py HTML 版）配套：镜像内置
# __pycache__ 是 unchecked-hash pyc（flags=1，完全不校验源文件 mtime/hash），
# bind mount 换了源码也会被旧字节码静默盖掉（2026-09 实测：挂载生效、文件内容
# 已是新版，跑的还是 tn=json 旧码）。检测 override 标记后逐出对应 pyc，让挂载
# 源码重新编译。同样借 CAP_CHOWN 拿 __pycache__ 写位，完成后还原属主。
# 未挂载 override 的实例（其他 4 片）grep 不中标记，整段跳过。
if grep -q "parse_general_html" /usr/local/searxng/searx/engines/baidu.py 2>/dev/null; then
  chown root:root /usr/local/searxng/searx/engines/__pycache__
  rm -f /usr/local/searxng/searx/engines/__pycache__/baidu.cpython-*.pyc
  chown searxng:searxng /usr/local/searxng/searx/engines/__pycache__
fi
# quark override（去掉上游硬编码 suspended_time=900，走 settings 10s）同款逐出
if grep -q "PRISM_OVERRIDE" /usr/local/searxng/searx/engines/quark.py 2>/dev/null; then
  chown root:root /usr/local/searxng/searx/engines/__pycache__
  rm -f /usr/local/searxng/searx/engines/__pycache__/quark.cpython-*.pyc
  chown searxng:searxng /usr/local/searxng/searx/engines/__pycache__
fi

sh /etc/searxng/render-settings.sh

# 渲染产物 YAML 硬校验：非法直接 FATAL。venv 路径漂移时跳过
# （render-settings.sh 内有占位符残留检查兜底），不因此拒启动
PY=/usr/local/searxng/.venv/bin/python
if [ -x "$PY" ]; then
  if ! "$PY" -c "import yaml; yaml.safe_load(open('/etc/searxng/settings.yml'))"; then
    echo "FATAL: settings.yml 渲染产物 YAML 非法——检查 render-settings.sh 与模板" >&2
    exit 1
  fi
fi

chown searxng:searxng /etc/searxng /etc/searxng/settings.yml

# 官方镜像 entrypoint 实测为 /usr/local/searxng/entrypoint.sh（docker inspect 验证）；
# 其余候选为镜像路径漂移的防御。镜像 CMD 为 null，"$@" 无附加参数。
for ep in /usr/local/searxng/entrypoint.sh /usr/local/bin/docker-entrypoint.sh /usr/bin/docker-entrypoint.sh; do
  if [ -x "$ep" ]; then exec "$ep" "$@"; fi
done
echo "FATAL: 未找到 searxng 镜像 entrypoint" >&2
exit 1
