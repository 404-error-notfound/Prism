#!/usr/bin/env bash
# 生产环境部署入口（发布平台逐台调用它：先一台、退出码 0 才下一台）。
#
# 职责划分（重要）：
#   - deploy.sh 是 test/本地/prod 共用的环境通用脚本，**不含任何 prod 特有逻辑**；
#   - 本文件承载全部 prod 特有逻辑：维护窗口（nginx 切对端/放回）、对端预检、
#     失败时保持切对端。改动本文件不影响 test 与本地环境。
#
# 前置条件：生产双机 + nginx 已按 deploy/nginx/ 配置（维护标记目录对部署用户可写、
#   两台互访 8081）；.env.prod 存在。单机/test 环境请直接用 deploy.sh。
#
# 用法（与原来完全一致）：
#   deploy-prod.sh              等价于 deploy.sh prod restart
#   deploy-prod.sh config       等价于 deploy.sh prod config
#   deploy-prod.sh build        等价于 deploy.sh prod build
#   deploy-prod.sh stop|down    停机动作（不进维护窗口）
#
# 维护窗口机制（常量已内置本脚本默认值，无需任何配置；临时调整用环境变量覆盖，
#   详见 docs/deploy.md「故障转移」）：
#   部署前放置 /opt/nginx/maintenance/prism.flag → 本机 nginx 把流量整体切到对端
#   （两台互为 backup）→ 部署+验证全过 → 移除标记切回本机。任何时刻流量都在健康的那台。
#   NGINX_MODE=flag      默认。未在 nginx 配置「切对端」片段时，touch/rm 是安全空操作。
#   NGINX_MODE=off       关闭维护窗口（行为退回纯 deploy.sh）。
#   NGINX_SERVER_NAME    本项目域名，带 Host 头自检用（默认 prism-search.web.internal）。
#   NGINX_ENTRY_URL      探测地址（默认 http://127.0.0.1/health，打健康接口而非根路径）。
#   NGINX_PEER_URL       对端 gateway 健康地址（http://<对端IP>/health），
#                        部署前硬性预检：对端不可达 = 没有流量承接者，拒绝部署。
#                        需在部署侧显式配置（环境变量或 .env.prod）；未配置则跳过对端预检。
#   NGINX_KEEP_FLAG_ON_FAILURE=1 默认：部署失败流量保持在对端（用户无感），
#                                重跑部署即自愈（重放标记→成功→自动清除）。
set -uo pipefail
# 注意：不用 set -e——部署失败要拿到退出码自己收尾（放流量/保持切对端），不能半路裸退。

ACTION="${1:-restart}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.prod"

case "$ACTION" in
  config | build | restart | stop | down) ;;
  *) echo "[ERROR] 用法: deploy-prod.sh [config|build|restart|stop|down]，收到: $ACTION" >&2; exit 1 ;;
esac

log()    { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
logerr() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

# ── 维护窗口配置：常量全部内置为默认值，.env.prod 无需任何 NGINX_* 配置。
#    优先级：环境变量 > .env.prod（加载逻辑只填充仍为空值的变量）> 内置默认值。
#    （.env.prod 解析容忍 CRLF/引号/行内注释）────────────────────────────────
NGINX_MODE="flag"
NGINX_UNIT="nginx"
NGINX_SERVER_NAME="prism-search.web.internal"
NGINX_ENTRY_URL="http://127.0.0.1/health"
NGINX_DRAIN_WAIT="3"
NGINX_KEEP_FLAG_ON_FAILURE="1"
NGINX_FLAG_FILE="/opt/nginx/maintenance/${COMPOSE_PROJECT_NAME:-prism}.flag"
NGINX_PEER_URL=""
if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in
      NGINX_MODE | NGINX_UNIT | NGINX_SERVER_NAME | NGINX_FLAG_FILE | NGINX_ENTRY_URL | NGINX_DRAIN_WAIT | NGINX_KEEP_FLAG_ON_FAILURE | NGINX_PEER_URL)
        [[ -z "${!_k:-}" ]] || continue                 # 环境变量已给值就不覆盖
        _v="${_v%%#*}"; _v="${_v#"${_v%%[![:space:]]*}"}"; _v="${_v%"${_v##*[![:space:]]}"}"
        _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
        [[ -n "$_v" ]] && printf -v "$_k" '%s' "$_v"
        ;;
    esac
  done < <(tr -d '\r' < "$ENV_FILE")
  unset _k _v
fi

# 对端地址与具体机器相关，本脚本不做任何自动推导：
# 需在部署侧显式配置 NGINX_PEER_URL（环境变量或 .env.prod）；保持为空时对端预检自动跳过。
NGINX_ENGAGED=0   # 本脚本是否真的摘过流量（幂等恢复的依据）

nginx_target() {
  case "$NGINX_MODE" in
    flag) printf '维护标记 %s' "$NGINX_FLAG_FILE" ;;
    systemd) printf 'systemd 服务 %s' "$NGINX_UNIT" ;;
    container) printf '容器 %s' "$NGINX_UNIT" ;;
    *) printf '自定义命令' ;;
  esac
}

nginx_manual_hint() {
  case "$NGINX_MODE" in
    flag) printf 'rm -f %s' "$NGINX_FLAG_FILE" ;;
    systemd) printf 'sudo systemctl start %s' "$NGINX_UNIT" ;;
    container) printf 'docker start %s' "$NGINX_UNIT" ;;
    *) printf '按 NGINX_START_CMD 手工执行' ;;
  esac
}

# 带 Host 头探测本项目域名的 HTTP 码（000 = 连不上）。按域名转发的 nginx 必须带 Host，
# 否则打到的是默认站（别的项目）。
nginx_probe_code() {
  if [[ -n "$NGINX_SERVER_NAME" ]]; then
    curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H "Host: $NGINX_SERVER_NAME" "$NGINX_ENTRY_URL" 2>/dev/null
  else
    curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$NGINX_ENTRY_URL" 2>/dev/null
  fi
}

# 部署前的流量切换。flag 模式失败只告警不中断（没摘成流量≠出事故，退化成原有行为）；
# systemd/container/custom 模式失败则中断——"停得掉、起不来"才是灾难。
nginx_enter_maintenance() {
  if [[ "$NGINX_MODE" == "off" ]]; then
    log ">>> [维护窗口] NGINX_MODE=off，跳过"
    return 0
  fi
  local t0=$SECONDS flag_dir peer_code code
  case "$NGINX_MODE" in
    flag)
      flag_dir="$(dirname "$NGINX_FLAG_FILE")"
      if [[ ! -d "$flag_dir" ]]; then
        log "    [提示] 标记目录不存在（$flag_dir）：本次不摘流量。"
        log "           启用方法见 docs/deploy.md「故障转移」，需要宿主机先建目录并给部署用户写权限"
        return 0
      fi
      # 预检对端承接能力：双机环境下对端不可达 = 部署期间没有流量承接者。
      # 无人值守滚动部署里这一步必须硬性把关——此刻尚未动任何容器，中止零成本。
      if [[ -n "$NGINX_PEER_URL" ]]; then
        peer_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$NGINX_PEER_URL" 2>/dev/null || echo 000)"
        if [[ "$peer_code" == "200" ]]; then
          log "    对端健康（$NGINX_PEER_URL -> 200）：部署期间由对端承接本机流量"
        else
          logerr "    [中止] 对端不可达（$NGINX_PEER_URL -> $peer_code）——没有流量承接者，拒绝部署"
          logerr "           先恢复对端服务（或核对 NGINX_PEER_URL / 防火墙放行 8081 互访），再重试"
          logerr "           本次未做任何变更（尚未放置标记、未动容器）"
          return 1
        fi
      else
        log "    [提示] 未配置 NGINX_PEER_URL（对端 gateway 地址）：跳过对端预检"
      fi
      if ! touch "$NGINX_FLAG_FILE" 2>/dev/null; then
        log "    [警告] 标记文件写入失败（$NGINX_FLAG_FILE）：本次不切流量，直接部署"
        return 0
      fi
      NGINX_ENGAGED=1
      ;;
    systemd)
      if ! sudo -n true 2>/dev/null; then
        logerr ">>> [维护窗口] 当前用户无免密 sudo，无法停/起 systemd 服务——拒绝继续"
        logerr "         解决：加 sudoers 白名单；或改用 NGINX_MODE=flag（推荐，不需要 sudo）"
        return 1
      fi
      nginx_service_stop || return 1
      NGINX_ENGAGED=1
      ;;
    container | custom)
      nginx_service_stop || return 1
      NGINX_ENGAGED=1
      ;;
  esac

  # 给在途请求留一个排空窗口
  if (( NGINX_DRAIN_WAIT > 0 )); then
    sleep "$NGINX_DRAIN_WAIT"
  fi
  code="$(nginx_probe_code)"
  case "$NGINX_MODE" in
    flag)
      if [[ "$code" == "000" ]]; then
        log "    [警告] 域名无响应（HTTP 000）：nginx 未运行或 NGINX_ENTRY_URL 不通"
      elif [[ "$code" == "503" ]]; then
        log "    [提示] 域名返回 503：nginx 侧还是旧版「标记=503 维护页」配置。"
        log "           建议升级为「标记=切对端」（见 docs/deploy.md「故障转移」），部署期间用户无感"
      else
        log "    已确认域名正常响应（HTTP $code，耗时 $((SECONDS-t0))s）"
        log "    流量去向取决于 nginx 配置：已配「切对端」则由对端承接；未配置则仍走本机"
      fi
      ;;
    *)
      if [[ "$code" == "000" ]]; then
        log "    已确认入口不再响应（流量已摘，耗时 $((SECONDS-t0))s）"
      else
        log "    [警告] 停止后入口仍返回 $code：检查是否还有别的进程占着这个端口"
      fi
      ;;
  esac
  return 0
}

# 停/起"整台 nginx"型动作（systemd / container / custom 三种模式共用）
nginx_service_stop() {
  case "$NGINX_MODE" in
    systemd)
      if ! systemctl is-active --quiet "$NGINX_UNIT"; then
        log ">>> [维护窗口] $(nginx_target) 当前已是停止态（仍会接管恢复责任）"
        return 0
      fi
      log ">>> [维护窗口] 摘流量：停止 $(nginx_target) ..."
      sudo -n systemctl stop "$NGINX_UNIT" || { logerr ">>> 停止失败——尚未动任何容器，放弃本次部署（入口保持原状）"; return 1; }
      ;;
    container)
      log ">>> [维护窗口] 摘流量：停止 $(nginx_target) ..."
      docker stop "$NGINX_UNIT" >/dev/null || { logerr ">>> 停止失败——尚未动任何容器，放弃本次部署"; return 1; }
      ;;
    custom)
      log ">>> [维护窗口] 摘流量：执行 NGINX_STOP_CMD ..."
      bash -c "${NGINX_STOP_CMD:?未设置 NGINX_STOP_CMD}" || { logerr ">>> 停止失败——尚未动任何容器，放弃本次部署"; return 1; }
      ;;
  esac
  return 0
}

# 部署验证通过后的流量切回（幂等）
nginx_exit_maintenance() {
  [[ "${NGINX_ENGAGED:-0}" -eq 1 ]] || return 0
  local code i
  case "$NGINX_MODE" in
    flag)
      log ">>> [维护窗口] 放流量：移除标记 $NGINX_FLAG_FILE ..."
      if ! rm -f "$NGINX_FLAG_FILE" 2>/dev/null; then
        logerr ">>> [维护窗口] 移除标记失败！请手工执行: $(nginx_manual_hint)"
        return 1
      fi
      # HTTP 探测发现不了标记残留（切对端配置下残留也返回 200，流量静默走对端），
      # 所以本地文件必须二次确认——这是防"部署完本机被静默绕过"的唯一手段。
      if [[ -e "$NGINX_FLAG_FILE" ]]; then
        logerr ">>> [维护窗口] 标记文件仍存在（$NGINX_FLAG_FILE）：流量仍被切到对端！请手工删除"
        return 1
      fi
      ;;
    systemd)
      log ">>> [维护窗口] 放流量：启动 $(nginx_target) ..."
      if ! sudo -n systemctl start "$NGINX_UNIT"; then
        logerr ">>> [维护窗口] 启动失败！请立即手工执行: $(nginx_manual_hint)"
        return 1
      fi
      ;;
    container)
      log ">>> [维护窗口] 放流量：启动 $(nginx_target) ..."
      if ! docker start "$NGINX_UNIT" >/dev/null; then
        logerr ">>> [维护窗口] 启动失败！请立即手工执行: $(nginx_manual_hint)"
        return 1
      fi
      ;;
    custom)
      log ">>> [维护窗口] 放流量：执行 NGINX_START_CMD ..."
      if ! bash -c "${NGINX_START_CMD:?未设置 NGINX_START_CMD}"; then
        logerr ">>> [维护窗口] 启动失败！请立即手工执行: $(nginx_manual_hint)"
        return 1
      fi
      ;;
  esac

  # 放流量后的判定：只确认"域名有正常响应"（000/502/504 只告警不判失败——后端就绪
  # 与否已由 deploy.sh 的 check_health / verify_stable 判过，别把成功部署误判成失败）。
  for i in $(seq 1 10); do
    code="$(nginx_probe_code)"
    case "$code" in
      200 | 301 | 302 | 401 | 403)
        log ">>> [维护窗口] 域名已恢复（HTTP $code，耗时 ${i}s）"
        NGINX_ENGAGED=0
        return 0
        ;;
    esac
    sleep 1
  done
  logerr ">>> [维护窗口] 已切回本机，但 10s 内探测返回 ${code}（不判失败）"
  logerr "         000 = 探测地址不通；502 = 本机与对端都未就绪，请求由降级页承接"
  logerr "         自查: docker ps ; curl -sI http://127.0.0.1:8081/health ; nginx -T | grep -n proxy_pass"
  NGINX_ENGAGED=0
  return 0
}

# 兜底：无论怎么退出（成功 / 部署失败 / Ctrl-C 意外中断），都不能把流量留在摘掉的状态。
nginx_cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "${NGINX_ENGAGED:-0}" -eq 1 ]]; then
    if [[ "$rc" -ne 0 && "$NGINX_KEEP_FLAG_ON_FAILURE" == "1" ]]; then
      logerr ">>> [维护窗口] 部署失败(rc=$rc)：按 NGINX_KEEP_FLAG_ON_FAILURE=1 保持切对端状态，本机不接流量"
      logerr "         重跑部署即自愈（重放标记→成功→自动清除）；手工切回: $(nginx_manual_hint)"
      NGINX_ENGAGED=0   # 标记保留在磁盘上，属预期状态，trap 不再重复处理
    else
      logerr ">>> [维护窗口] 脚本退出(rc=$rc)，兜底放流量 ..."
      nginx_exit_maintenance || true
    fi
  fi
  exit "$rc"   # 必须显式 exit：trap 内命令的返回值会覆盖原退出码，发布平台会误判成功
}
trap nginx_cleanup EXIT

# ── 主流程 ──────────────────────────────────────────────────────────────────────
# stop/down 是主动停机动作，不进维护窗口，直接透传给环境通用的 deploy.sh
case "$ACTION" in
  stop | down)
    exec bash "$ROOT_DIR/deploy/deploy.sh" prod "$ACTION"
    ;;
esac

nginx_enter_maintenance || exit 1

deploy_rc=0
# deploy.sh 是环境通用脚本（test/本地/prod 共用），prod 特有逻辑全部在上面完成。
# 它的退出码 = 部署+状态校验+健康检查+收工复核 的综合结果。
bash "$ROOT_DIR/deploy/deploy.sh" prod "$ACTION" || deploy_rc=$?

if [[ "$deploy_rc" -eq 0 ]]; then
  nginx_exit_maintenance || deploy_rc=1   # 放流量失败同样算部署失败
elif [[ "$NGINX_KEEP_FLAG_ON_FAILURE" == "1" ]]; then
  # 部署失败：流量保持在对端（标记留在磁盘上，属预期状态），重跑部署即自愈
  logerr ">>> [维护窗口] 部署失败(rc=$deploy_rc)：保持切对端状态，本机不接流量"
  logerr "         重跑部署即自愈（重放标记→成功→自动清除）；手工切回: $(nginx_manual_hint)"
  NGINX_ENGAGED=0
else
  # KEEP=0：失败也切回本机，靠 nginx upstream backup（max_fails）兜底转对端
  nginx_exit_maintenance || true
fi

exit "$deploy_rc"
