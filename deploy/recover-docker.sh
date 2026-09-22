#!/usr/bin/env bash
# 宿主机 Docker 环境自救（vfs 驱动下部署失败后的第一步）
#
# 适用症状（通常几条一起出现）：
#   UnixHTTPConnectionPool(host='localhost', port=None): Read timed out. (read timeout=300)
#   OCI runtime create failed: ... getting the final child's pid from pipe caused "EOF"
#   部署日志里容器名带 <hash>_ 前缀（b08eb3f55cc8_prism_searxng-quark_1）
#   同一服务反复 exited，重试 5 轮都不收敛
#
# 这个脚本只动宿主机侧状态，不碰项目文件、不改 .env：
#   bash deploy/recover-docker.sh                  # 体检 + 清理残留（安全，不动运行中的容器）
#   bash deploy/recover-docker.sh --restart-daemon # 上面基础上再重启 Docker 守护进程
#
# ⚠️ --restart-daemon 会重启整台机器的 Docker，本机所有容器（含其他项目）都会短暂中断，
#    只有在"清理完残留仍然 runc EOF"时才需要，执行前请确认这个时间窗可以接受。
set -euo pipefail

# 统一带时间戳的步骤输出（与 deploy.sh 同一套约定，便于两份日志对照看时间轴）
log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
logerr() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

T0=$SECONDS

RESTART_DAEMON=0
for arg in "$@"; do
  case "$arg" in
    --restart-daemon) RESTART_DAEMON=1 ;;
    -h | --help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      logerr "[ERROR] 未知参数: $arg"
      exit 1
      ;;
  esac
done

# 与 deploy.sh 保持一致的项目服务列表（新增服务时两处都要改）
SERVICES=(searxng-baidu searxng-bing searxng-quark searxng-sogou searxng-wechat gateway)

command -v docker >/dev/null 2>&1 || {
  logerr "[ERROR] 未找到 docker 命令"
  exit 1
}
docker info >/dev/null 2>&1 || {
  logerr "[ERROR] Docker 守护进程不可达（未运行 / 当前用户无权限），先解决这一层再谈部署"
  logerr "        查看守护进程状态: systemctl status docker"
  exit 1
}

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  COMPOSE=(docker-compose)
fi

DOCKER_ROOT="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"

echo "=================================================="
log ">>> 1. 宿主机体检"
echo "=================================================="
echo "Docker 版本   : $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
echo "存储驱动      : $(docker info -f '{{.Driver}}' 2>/dev/null || echo unknown)"
echo "数据目录      : $DOCKER_ROOT"
echo "compose 命令  : ${COMPOSE[*]}"
echo
echo "--- 磁盘空间（vfs 驱动每层都是整份拷贝，这里是首要怀疑对象）---"
df -h "$DOCKER_ROOT" 2>/dev/null || echo "  [警告] 读不到 $DOCKER_ROOT 的磁盘信息"
echo "--- inode（vfs 同样吃 inode）---"
df -i "$DOCKER_ROOT" 2>/dev/null || true
echo "--- 内存 ---"
free -m 2>/dev/null || true
echo "--- 内核是否发生过 OOM 击杀（runc EOF 的常见推手）---"
{ dmesg -T 2>/dev/null | grep -iE 'out of memory|oom-kill' | tail -n 10; } || echo "  （读不到 dmesg 或无记录）"

echo
echo "--- Docker 资源占用 ---"
docker system df 2>/dev/null || true

echo
echo "--- 本项目容器状态 ---"
for svc in "${SERVICES[@]}"; do
  ids="$(docker ps -aq --filter "label=com.docker.compose.service=$svc" 2>/dev/null || true)"
  if [[ -z "$ids" ]]; then
    echo "  [--]   $svc 无容器"
    continue
  fi
  while read -r cid; do
    [[ -z "$cid" ]] && continue
    docker inspect -f '  [{{.State.Status}}] {{.Name}} created={{.Created}}' "$cid" 2>/dev/null || true
  done <<<"$ids"
done

echo
echo "=================================================="
log ">>> 2. 清理 created/dead 残留容器"
echo "=================================================="
echo "说明：runc 创建失败（EOF）会留下一具 created 状态的空壳，容器名仍被占用，"
echo "      下一轮 compose 只能用 <hash>_<原名> 的临时名重建，状态越滚越乱。"
echo "      这里只删 created/dead 的，运行中的容器一律不动。"
echo
removed=0
for svc in "${SERVICES[@]}"; do
  for st in created dead; do
    while read -r cid; do
      [[ -z "$cid" ]] && continue
      name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null || echo "$cid")"
      log "  删除 [${st}] ${name#/}"
      docker rm -f "$cid" >/dev/null 2>&1 || true
      removed=$((removed + 1))
    done < <(docker ps -aq \
      --filter "label=com.docker.compose.service=$svc" \
      --filter "status=$st" 2>/dev/null || true)
  done
done
if [[ "$removed" -eq 0 ]]; then
  log "  （无残留）"
else
  log "  已清理 $removed 个残留容器"
fi

echo
log ">>> 3. 清理悬空镜像层（不带 -a，只删 untagged，不会动 prism-gateway:latest）"
docker image prune -f 2>/dev/null || true

if [[ "$RESTART_DAEMON" -eq 1 ]]; then
  echo
  echo "=================================================="
  log ">>> 4. 重启 Docker 守护进程（本机所有容器会短暂中断）"
  echo "=================================================="
  if ! command -v systemctl >/dev/null 2>&1; then
    logerr "[ERROR] 没有 systemctl，请手工重启 dockerd（service docker restart）"
    exit 1
  fi
  daemon_t0=$SECONDS
  systemctl restart docker
  log "  已下发 restart，等待守护进程就绪 ..."
  ok=0
  for _ in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 2
  done
  if [[ "$ok" -eq 1 ]]; then
    log "  守护进程已就绪（耗时 $((SECONDS-daemon_t0))s）"
    docker ps --format '  {{.Names}}\t{{.Status}}' 2>/dev/null || true
  else
    logerr "[ERROR] 60s 内守护进程未就绪，检查: journalctl -u docker -n 100 --no-pager"
    exit 1
  fi
fi

echo
echo "=================================================="
log ">>> 完成。接下来："
echo "=================================================="
echo "1) 重新部署（脚本已改为串行 + --no-deps，gateway 不再连带重建 5 个 searxng）："
echo "     bash deploy/deploy-prod.sh config"
echo "2) 若仍报 runc EOF 或 300s 超时，说明宿主机资源确实不够，按顺序排查："
echo "     - 磁盘是否还有余量（上面 df 结果）：vfs 每个容器都是整份镜像拷贝，"
echo "       余量低于镜像总量就该扩盘，或把 /var/lib/docker 换到更大的盘"
echo "     - 上面 df -i 的 inode 是否耗尽"
echo "     - 最后手段：bash deploy/recover-docker.sh --restart-daemon"
echo "3) 根治（本次事故的底层原因）：vfs 是 Docker 最差的存储驱动，只应作为兜底。"
echo "   确认宿主机的 /var/lib/docker 所在文件系统支持 overlay2（xfs 需 ftype=1，"
echo "   用 xfs_info <挂载点> 看 ftype 是否为 1；ext4 直接支持），支持就切 overlay2 并"
echo "   升级 Docker 到 20.10+，部署速度与稳定性都会有量级改善。"
echo "   overlay2 切换步骤见 docs/deploy.md「存储驱动是 vfs」一节。"
echo
log ">>> 自救脚本结束（总耗时 $((SECONDS-T0))s）"
