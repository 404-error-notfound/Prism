#!/usr/bin/env bash
# 服务"消失 / 崩溃"取证（只读：不删容器、不改文件、不重启任何东西）
#
# 适用症状：部署脚本报了 `全部服务正常运行`、/health 也通，但随后 docker ps 里某个容器
# （多为 gateway）不见了。部署脚本只在收工那一刻采样一次，那之后的死活它不会知道。
#
# 用法：
#   bash deploy/diagnose.sh            # 取证最近 60 分钟
#   bash deploy/diagnose.sh 19:10:00   # 从指定本地时刻开始取证
#
# 前置条件：docker 命令可用、当前用户可访问 Docker 守护进程；无额外环境变量。
# 输出 8 段：容器清单 / 关键字段 / 容器事件 / 内核 OOM / dockerd 日志 / 磁盘与 inode /
#            内存 / 各服务日志尾部。整段贴回即可定位。
set -uo pipefail

log() { printf '\n[%s] ===== %s =====\n' "$(date '+%H:%M:%S')" "$*"; }
note() { printf '    %s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    -h | --help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

# 与 deploy.sh 保持一致的项目服务列表（新增服务时两处都要改）
SERVICES=(searxng-baidu searxng-bing searxng-quark searxng-sogou searxng-wechat gateway)

NOW="$(date '+%Y-%m-%dT%H:%M:%S')"
SINCE="${1:-}"
if [[ -z "$SINCE" ]]; then
  SINCE="$(date -d '60 minutes ago' '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || echo "$NOW")"
fi

command -v docker >/dev/null 2>&1 || {
  printf '[ERROR] 未找到 docker 命令\n' >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  printf '[ERROR] Docker 守护进程不可达（未运行 / 当前用户无权限）\n' >&2
  exit 1
}

log "取证窗口 $SINCE ~ $NOW（只读）"

log "1/8 容器清单 docker ps -a（看目标容器是否还在、处于什么终态）"
docker ps -a --format 'table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null || true

log "2/8 关键字段（状态 / 退出码 / OOM / 重启次数 / 起止时间 / health）"
found=0
for svc in "${SERVICES[@]}"; do
  for cid in $(docker ps -aq --filter "label=com.docker.compose.service=$svc" 2>/dev/null); do
    found=1
    docker inspect -f '    {{.Name}} 状态={{.State.Status}} 退出码={{.State.ExitCode}} OOM={{.State.OOMKilled}} 重启={{.RestartCount}} 启动={{.State.StartedAt}} 结束={{.State.FinishedAt}} 错误={{.State.Error}}' "$cid" 2>/dev/null || true
  done
done
if [[ "$found" -eq 0 ]]; then
  note "没有任何带 compose 服务标签的容器：容器很可能已被 docker rm 删除（看第 3 段的 destroy 事件）"
fi

log "3/8 容器事件（die / kill / oom / destroy；出现 destroy 即被 rm 过）"
timeout 15 docker events --since "$SINCE" --until "$NOW" 2>/dev/null \
  | grep -Ei 'prism|gateway|searxng|oom|destroy|die|kill' \
  || note "（窗口内无相关事件，或权限不足／daemon 未保留事件缓冲）"

log "4/8 内核 OOM 记录（容器被 OOM kill 会在这里留痕）"
dmesg -T 2>/dev/null | grep -iE 'oom|killed process' | tail -20 \
  || note "（无 dmesg 权限，或无 OOM 记录）"

log "5/8 dockerd 日志尾部（daemon 层的创建失败 / 看门狗动作）"
journalctl -u docker --since "$SINCE" --no-pager 2>/dev/null | tail -40 \
  || note "（无 journalctl 权限，或无记录）"

log "6/8 磁盘与 inode（vfs 驱动下创建容器=整份镜像拷贝，空间或 inode 满会直接 runc EOF）"
docker info -f '    存储驱动={{.Driver}} DockerRootDir={{.DockerRootDir}} 容器数={{.Containers}} 镜像数={{.Images}}' 2>/dev/null || true
root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
df -h "$root" 2>/dev/null | sed 's/^/    /' || true
df -i "$root" 2>/dev/null | sed 's/^/    /' || true

log "7/8 内存"
free -m 2>/dev/null | sed 's/^/    /' || note "（无 free 命令）"

log "8/8 各服务日志尾部（最后 30 行）"
for svc in "${SERVICES[@]}"; do
  cid="$(docker ps -aq --filter "label=com.docker.compose.service=$svc" 2>/dev/null | head -n1)"
  [[ -z "$cid" ]] && continue
  name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null || echo "$svc")"
  printf '\n    ---- %s ----\n' "${name#/}"
  docker logs --tail 30 "$cid" 2>&1 | sed 's/^/    /' || true
done

printf '\n>>> 取证完成，把以上整段贴回即可定位\n'
