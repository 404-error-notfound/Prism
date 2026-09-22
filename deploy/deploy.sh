#!/usr/bin/env bash
# 部署脚本（按变更类型选择更新策略）
# 用法：
#   deploy.sh <环境> [restart|config|build|stop|down]
#
#   restart 只改了代码（默认）。执行 docker compose restart 重启容器。
#   config  改了 .env 或 docker-compose.yml。强制重建容器以重新读取配置。
#   build   改了 Dockerfile 或 pyproject.toml。强制 build 镜像并重建容器。
#   stop    停止容器（保留容器，可 up/start 恢复）。
#   down    停止并销毁容器（维护时使用）。
#
#   兼容旧习惯：code / start / up 会被视为 restart。
# 环境对应文件：test -> .env.test，prod -> .env.prod，其余环境取 .env.<环境>（需存在）
# 在任意目录下执行均可，效果与在项目根目录手工执行 docker compose 完全一致
set -euo pipefail

ENV="${1:-}"
ACTION="${2:-restart}"

# 项目根目录（脚本位于 deploy/ 下，向上取一级）
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 兼容旧写法：code / start / up 都视为 restart
if [[ "$ACTION" == "code" || "$ACTION" == "start" || "$ACTION" == "up" ]]; then
  ACTION="restart"
fi

# 统一带时间戳的步骤输出。每一步都打上本地时间——部署慢或 daemon 超时时，
# 这条时间轴是唯一能定位"卡在哪一步、那一步吃了多久"的东西。
# （2026-09-17 事故复盘：原始日志只有内容没有时刻，无法判断慢在哪个服务上）
# 空参数只输出空行，不打光秃秃的时间戳（分隔用）。日志文本避开破折号等易被
# 终端/locale/日志采集器转义成 ?? 的符号——生产日志里已经出现过一次。
log() {
  if [[ $# -eq 0 || -z "$*" ]]; then
    printf '\n'
    return 0
  fi
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}
logerr() {
  if [[ $# -eq 0 || -z "$*" ]]; then
    printf '\n' >&2
    return 0
  fi
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2
}

# 用于统计各步骤 / 整体耗时（bash 内建，自 shell 启动开始计数）
T0=$SECONDS

# 部署日志落盘到 /opt/logs/prism/prism-search/deploy 目录（尽早设置，捕获全部输出）
LOG_DIR="/opt/logs/prism/prism-search/deploy"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/deploy-${ENV:-unknown}-${ACTION}-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1
log ">>> 部署日志: $LOG_FILE"

if [ -z "$ENV" ]; then
  logerr "用法: $0 <环境> [restart|config|build|stop|down]，如: $0 test restart"
  exit 1
fi

case "$ACTION" in
  restart | config | build | stop | down) ;;
  *)
    logerr "用法: $0 <环境> [restart|config|build|stop|down]，如: $0 test restart"
    exit 1
    ;;
esac

# 环境变量文件：prod -> .env.prod，其余环境直接对应 .env.<环境>
ENV_FILE="$ROOT_DIR/.env.$ENV"
if [ "$ENV" = "prod" ]; then
  ENV_FILE="$ROOT_DIR/.env.prod"
fi
if [ ! -f "$ENV_FILE" ]; then
  logerr "[ERROR] 环境文件不存在: $ENV_FILE"
  exit 1
fi

# 兼容 docker compose v2 / docker-compose v1
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  COMPOSE=(docker-compose)
fi

# 前置检查：docker 命令存在且当前用户有权访问守护进程，
# 否则 compose 会以一屏 urllib3 traceback 报 Permission denied，难定位
command -v docker >/dev/null 2>&1 || { logerr "[ERROR] 未找到 docker 命令"; exit 1; }
docker info >/dev/null 2>&1 || {
  logerr "[ERROR] 无法访问 Docker 守护进程：当前用户（$(whoami)）无权限或 daemon 未运行。"
  logerr "        若是权限问题，请执行: sudo usermod -aG docker $(whoami) 并重新建立会话"
  exit 1
}

# 固定项目目录执行，并强制指定 compose project 名称为 prism，
# 避免从 tag 命名目录部署时容器名、网络名带上该前缀。
cd "$ROOT_DIR"
export COMPOSE_PROJECT_NAME=prism
# compose v1（Python 版，CentOS7 + Docker 18.09 上的常见组合）用这个变量控制等待
# daemon 响应的时长，超时抛 UnixHTTPConnectionPool Read timed out。vfs 驱动下一次
# 容器创建=整份镜像目录拷贝，gateway 镜像数 GB，300s 不够用，默认抬到 600；
# 现场可用环境变量覆盖：COMPOSE_HTTP_TIMEOUT=900 bash deploy/deploy-prod.sh config
export COMPOSE_HTTP_TIMEOUT="${COMPOSE_HTTP_TIMEOUT:-600}"

# compose 服务列表（与 docker-compose.yml 一致），顺序即串行部署顺序，gateway 必须在最后
SERVICES=(searxng-baidu searxng-bing searxng-quark searxng-sogou searxng-wechat gateway)

# 本次部署已成功重建的服务（跨重试轮次累积）。
# 不记这个的话，每轮重试都会把已就绪的服务再整份重建一遍，vfs 下等于自己打自己。
DEPLOYED=""
mark_deployed() { DEPLOYED="$DEPLOYED $1"; }
is_deployed() { [[ " $DEPLOYED " == *" $1 "* ]]; }

# 检测全部服务运行状态
check_all_running() {
  local failed=0
  for svc in "${SERVICES[@]}"; do
    local status
    # restart: always 下崩溃循环的容器 Running 也是 true（两次重启的间隙），
    # 必须看 Restarting 标志，否则发布脚本会把崩溃中的服务误判为正常
    status=$("${COMPOSE[@]}" ps --quiet "$svc" | xargs docker inspect -f '{{.State.Status}}{{if .State.Restarting}}(restarting){{end}}' 2>/dev/null | head -n1)
    if [[ "$status" != "running" ]]; then
      log ">>> [异常] 服务 $svc 状态异常: ${status:-missing}"
      failed=1
    fi
  done
  return $failed
}

# 输出最终服务状态
report_status() {
  log ""
  log ">>> 最终运行状态："
  local fail_cnt=0
  for svc in "${SERVICES[@]}"; do
    local cid name status restarts
    cid=$("${COMPOSE[@]}" ps --quiet "$svc")
    if [[ -z "$cid" ]]; then
      log "    [FAIL] $svc 无容器实例"
      fail_cnt=$((fail_cnt+1))
      continue
    fi
    name=$(docker inspect -f '{{.Name}}' "$cid")
    name="${name#/}"
    status=$(docker inspect -f '{{.State.Status}}{{if .State.Restarting}}(restarting){{end}}' "$cid")
    restarts=$(docker inspect -f '{{.RestartCount}}' "$cid")
    if [[ "$status" == "running" ]]; then
      log "    [OK]   $svc 容器:$name 重启次数:$restarts"
    else
      log "    [FAIL] $svc 容器:$name 状态:$status 重启次数:$restarts"
      log "    ---- $svc 最近 20 行日志 ----"
      docker logs --tail 20 "$cid" 2>&1 | sed 's/^/    /' >&2 || true
      fail_cnt=$((fail_cnt+1))
    fi
  done
  if [[ "$fail_cnt" -eq 0 ]]; then
    log ">>> 全部服务正常运行"
    return 0
  else
    logerr ">>> [严重] 存在 $fail_cnt 个服务异常，请查看日志排查（docker logs <容器名>）"
    return 1
  fi
}

# 采样一个服务的 "<状态> <重启次数>"，无容器时输出 "missing -"。
# 状态带上 Restarting 标志（restart: unless-stopped 下崩溃循环的容器 State.Status 仍是
# running、两次重启之间采样会漏判，只有这个标志能识别）。一次 inspect 取全，避免漂移。
sample_service() {
  local cid
  cid=$("${COMPOSE[@]}" ps --quiet "$1" 2>/dev/null | head -n1)
  if [[ -z "$cid" ]]; then
    echo "missing -"
    return 0
  fi
  docker inspect -f '{{.State.Status}}{{if .State.Restarting}}(restarting){{end}} {{.RestartCount}}' "$cid" 2>/dev/null || echo "unknown -"
}

# 收工复核：健康检查通过 ≠ 服务稳定。
# 2026-09-17 现场：脚本报了"全部服务正常运行"、/health 也通了，可用户随后执行 docker ps
# 发现 gateway 根本不在列表里——它是在脚本收工之后才崩的。所以收工前必须再采样一次。
#
# 判据（踩过一次坑才定下来，不要随意收紧）：
#   硬判失败 —— 服务确实没在正常服务：
#     a) 采样时不在运行态（exited/dead/created/missing，即 docker ps 里看不到它）；
#     b) 处于崩溃循环（running(restarting)）；
#     c) 在复核窗口内重启次数增长（说明还没稳定下来）。
#   仅提示，不判失败 —— 启动期崩过一次但已自愈：
#     RestartCount > 0 且当前稳定 running。**不能拿它当失败判据**：这台机器上
#     gateway 冷启动要 200s+（vfs 下创建=整份镜像拷贝），首次启动偶发退出、被
#     restart: unless-stopped 拉起后 healthy 是自愈，不是部署失败。2026-09-17 曾把
#     "新建容器 RestartCount 必须为 0"当硬判据，结果把一次成功的部署判成失败，
#     还会诱导人工重跑 config——vfs 上重跑一轮就是十几次整份镜像拷贝，纯亏。
verify_stable() {
  local settle="${VERIFY_SETTLE_SECONDS:-20}"
  local svc before after st rc bad=0 warn=0
  declare -A BEF=()
  log ">>> 收工复核：采样两次（间隔 ${settle}s）确认没有刚起来就崩的容器..."
  for svc in "${SERVICES[@]}"; do
    BEF["$svc"]="$(sample_service "$svc")"
  done
  if (( settle > 0 )); then
    sleep "$settle"
  fi
  for svc in "${SERVICES[@]}"; do
    before="${BEF[$svc]}"
    after="$(sample_service "$svc")"
    st="${after%% *}"
    rc="${after##* }"
    case "$st" in
      *restarting*)
        logerr "    [异常] $svc 状态:$st（正在崩溃循环：Status 仍是 running，但两次重启的间隙被采样到了）"
        bad=1
        continue
        ;;
      running) ;;
      *)
        logerr "    [异常] $svc 状态:$st（已经不在运行态，docker ps 里看不到它）"
        bad=1
        continue
        ;;
    esac
    if [[ "$rc" =~ ^[0-9]+$ && "${before##* }" =~ ^[0-9]+$ ]] && (( rc > ${before##* } )); then
      logerr "    [异常] $svc 在复核窗口内重启（${before##* } -> $rc）：还没稳定，请查 docker logs"
      bad=1
      continue
    fi
    if [[ "$rc" =~ ^[0-9]+$ ]] && (( rc > 0 )); then
      log "    [提示] $svc 启动过程中崩过 $rc 次并已自愈，当前持续运行（不算失败；反复出现请查 docker logs）"
      warn=$((warn + 1))
      continue
    fi
    log "    [OK]   $svc 持续运行（重启次数 $rc）"
  done
  if [[ "$bad" -eq 0 ]]; then
    if (( warn > 0 )); then
      log ">>> 收工复核通过（有 $warn 个服务启动期崩溃过并已自愈，当前均持续运行）"
    else
      log ">>> 收工复核通过（全部容器持续运行且无新增重启）"
    fi
    return 0
  fi
  logerr ">>> [严重] 收工复核不通过：服务不在正常服务状态，本次部署不能算成功"
  logerr "         排查: bash deploy/diagnose.sh（只读，一次收齐状态/事件/OOM/日志）"
  return 1
}

# 清理历史 project 残留容器（如发布平台旧 tag 项目）：
# 这些容器不在当前 project 下，compose 不会接管；不清理会一直占用端口等资源。
# 按 compose 服务标签识别（不看 working_dir——平台可能在 packet 目录里启动容器），
# 只删"别家的"，当前 project 的容器一律不动。
clean_legacy_projects() {
  local current svc cid name
  current="$("${COMPOSE[@]}" ps -q 2>/dev/null || true)"
  for svc in "${SERVICES[@]}"; do
    for cid in $(docker ps -aq --filter "label=com.docker.compose.service=$svc"); do
      if grep -qF "$cid" <<<"$current"; then
        continue
      fi
      name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null || echo "$cid")"
      log ">>> 清理历史项目容器: ${name#/}"
      docker rm -f "$cid" >/dev/null 2>&1 || true
    done
  done
}

# 删除本项目全部容器（含历史 tag project 残留），stop/down 停机时使用。
# 与 clean.sh 的兜底逻辑一致：按服务标签识别，不看 working_dir。
remove_all_containers() {
  local svc cid name
  for svc in "${SERVICES[@]}"; do
    for cid in $(docker ps -aq --filter "label=com.docker.compose.service=$svc"); do
      name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null || echo "$cid")"
      log ">>> 删除容器: ${name#/}"
      docker rm -f "$cid" >/dev/null 2>&1 || true
    done
  done
}

# 等待 gateway 健康检查通过（模型 warm_up 完成）。
# 容器 running ≠ 可服务：模型加载 1~3 分钟内 /health 不响应。
check_health() {
  local i t0=$SECONDS
  if ! command -v curl >/dev/null 2>&1; then
    log ">>> [警告] 未找到 curl，跳过健康检查"
    return 0
  fi
  log ">>> 等待 gateway 健康检查通过（模型加载中，最长 5 分钟）..."
  for i in $(seq 1 30); do
    if curl -fsS http://localhost:8081/health >/dev/null 2>&1; then
      log ">>> gateway 健康检查通过（等待 $((SECONDS-t0))s）"
      return 0
    fi
    sleep 10
  done
  logerr ">>> [严重] gateway 健康检查超时（已等 $((SECONDS-t0))s），请执行 ${COMPOSE[*]} logs gateway 排查"
  return 1
}

# 列出本项目各服务处于 created/dead 状态的残留容器 ID。
# runc 创建失败（EOF）会留下一具 created 状态的空壳，容器名仍被占用，下一轮
# compose 只能用 <hash>_<原名> 的临时名重建——日志里出现 b08eb3f55cc8_prism_searxng-quark_1
# 这种前缀就是它。残留不清会一轮比一轮糟。
list_dead_containers() {
  local svc st
  for svc in "${SERVICES[@]}"; do
    for st in created dead; do
      docker ps -aq \
        --filter "label=com.docker.compose.service=$svc" \
        --filter "status=$st" 2>/dev/null || true
    done
  done
}

count_dead_containers() {
  list_dead_containers | grep -c . || true
}

clean_dead_containers() {
  local cid name n=0
  while read -r cid; do
    [[ -z "$cid" ]] && continue
    name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null || echo "$cid")"
    log ">>> 清理残留容器: ${name#/}"
    docker rm -f "$cid" >/dev/null 2>&1 || true
    n=$((n+1))
  done < <(list_dead_containers)
  if [[ "$n" -gt 0 ]]; then
    log ">>> 已清理 $n 个残留容器"
  fi
  return 0
}

# 宿主机资源体检。创建容器在 vfs 下是整份镜像拷贝（overlayfs 也吃 I/O），磁盘/内存/inode
# 吃紧时 daemon 会以 300s 超时或 runc EOF 的形式失败——报错文本指向 daemon，根因却在宿主机，
# 所以开跑前先把体检结果摆出来，省得对着 compose 的报错瞎猜。
preflight_report() {
  local docker_root dead
  docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  log ">>> 宿主机体检（存储驱动 $(docker info -f '{{.Driver}}' 2>/dev/null || echo unknown)）："
  df -h "$docker_root" 2>/dev/null | sed 's/^/    /' || true
  free -m 2>/dev/null | sed 's/^/    /' || true
  dead="$(count_dead_containers)"
  if [[ "$dead" -gt 0 ]]; then
    log "    [警告] 有 $dead 个本项目 created/dead 残留容器（多为上次 runc EOF 留下的空壳）"
    log "           本轮开始前会自动清理；若清理后仍反复出现，先跑 deploy/recover-docker.sh"
  fi
}

# 根据变更类型构造 up 参数，恒定逐服务串行执行。
# 不再按存储驱动分流（原实现：非 vfs 走并行 up）。恒定串行的三个理由：
#   1) 串行才谈得上"一个服务失败不影响其他服务"——并行 up 一失败就是整批回滚式重试；
#   2) 非 vfs 机器（开发机 overlayfs）并行创建同样会互相抢 I/O，日志也完全交织在一起，
#      出问题时看不出哪个容器先崩（2026-09-17 本地部署现场）；
#   3) 生产中 vfs 是必须串行的（每容器=整份镜像拷贝），两套路径只会在"只在生产才走到"
#      的地方藏 bug，统一成一条路径更好维护。
compose_up() {
  local extra_args=()
  case "$ACTION" in
    config)
      extra_args=(--force-recreate)
      ;;
    build)
      extra_args=(--build --force-recreate)
      ;;
  esac

  log ">>> 逐服务串行部署（顺序：${SERVICES[*]}）"
  local failed=0 svc t0
  for svc in "${SERVICES[@]}"; do
    if is_deployed "$svc"; then
      log ">>> 跳过 $svc（本次部署已成功重建）"
      continue
    fi
    log ">>> 部署 $svc"
    t0=$SECONDS
    # --no-deps 是这里的命门：gateway 的 depends_on 声明了 5 个 searxng，
    # 不带 --no-deps 时 `up -d --force-recreate gateway` 会把这 5 个依赖一起
    # 重建，串行策略在最后一步塌成 6 容器并发——vfs 下每个都是整份镜像拷贝，
    # daemon 直接 300s 超时（2026-09-17 生产事故现场就是这个）。
    # 依赖已在本循环前序部署完成，这里跳过依赖解析是安全的。
    if "${COMPOSE[@]}" up -d "${extra_args[@]}" --no-deps "$svc"; then
      log ">>> $svc 完成（耗时 $((SECONDS-t0))s）"
      mark_deployed "$svc"
    else
      log ">>> [警告] $svc 重建失败（耗时 $((SECONDS-t0))s），继续部署其余服务（下一轮重试）"
      failed=1
    fi
    # 给 Docker daemon 一点喘息时间，降低创建期的 I/O 峰值
    sleep 2
  done
  return $failed
}

case "$ACTION" in
  restart)
    # 同步环境变量（compose env_file 读取 .env）
    cp "$ENV_FILE" "$ROOT_DIR/.env"
    log ">>> 环境变量文件已就绪（来源：$(basename "$ENV_FILE")）："
    log "  $ROOT_DIR/.env"
    log ">>> 部署模式: $ACTION"

    clean_legacy_projects
    log ">>> 执行 ${COMPOSE[*]} restart"
    restart_t0=$SECONDS
    "${COMPOSE[@]}" restart
    log ">>> restart 完成（耗时 $((SECONDS-restart_t0))s）"

    if ! report_status; then
      exit 1
    fi
    check_health || exit 1
    verify_stable || exit 1
    ;;
  config | build)
    # 同步环境变量（compose env_file 读取 .env）
    cp "$ENV_FILE" "$ROOT_DIR/.env"
    log ">>> 环境变量文件已就绪（来源：$(basename "$ENV_FILE")）："
    log "  $ROOT_DIR/.env"
    log ">>> 部署模式: $ACTION"

    max_retry=5
    clean_legacy_projects
    preflight_report
    for ((attempt=1; attempt<=max_retry; attempt++)); do
      round_t0=$SECONDS
      log ">>> 第 $attempt/$max_retry 轮执行 ${COMPOSE[*]} up -d ${ACTION}（累计 ${round_t0}s）"
      # 每轮开跑前清掉上一轮 runc EOF 留下的 created/dead 空壳：
      # 容器名被占着，compose 只会用 <hash>_ 前缀临时名绕过去，状态越滚越乱
      clean_dead_containers
      if compose_up; then
        log ">>> up 执行完成，校验服务状态"
      else
        log ">>> up 执行出现告警，等待5秒后重试"
      fi
      if check_all_running; then
        log ">>> 第 $attempt 轮已收敛（本轮耗时 $((SECONDS-round_t0))s）"
        break
      fi
      log ">>> 第 $attempt 轮仍有服务异常（本轮耗时 $((SECONDS-round_t0))s），5 秒后重试"
      sleep 5
    done

    if ! report_status; then
      exit 1
    fi
    check_health || exit 1
    verify_stable || exit 1
    ;;
  stop)
    log ">>> 执行停止操作 ${COMPOSE[*]} stop（保留容器，可 up/start 恢复）"
    stop_t0=$SECONDS
    "${COMPOSE[@]}" stop || true
    log ">>> 容器已停止（耗时 $((SECONDS-stop_t0))s）"
    ;;
  down)
    log ">>> 执行销毁操作 ${COMPOSE[*]} down（会中断业务，仅维护使用）"
    down_t0=$SECONDS
    # down 清理当前 prism project；历史 tag project 容器按服务标签兜底删除
    "${COMPOSE[@]}" down --remove-orphans || true
    remove_all_containers
    log ">>> 所有容器已销毁（耗时 $((SECONDS-down_t0))s）"
    ;;
esac

log ">>> 完成操作: $ENV $ACTION（总耗时 $((SECONDS-T0))s）"
