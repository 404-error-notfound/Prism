#!/bin/bash
set -e

# Jenkins 部署脚本
# 用途：由 Jenkins 构建触发，rsync 推送代码到目标机并远程执行 deploy/deploy.sh。
# 参数（Jenkins 构建参数 / 自动注入）：
#   branchs    Git 分支，当前支持 origin/test
#   WORKSPACE  Jenkins 自动注入的工作区路径
# 前置条件：本机有 rsync/ssh；构建节点对目标机 ${WEB_USER}@${IP} 免密 ssh；
#   目标机 /opt/web/prism-search 存在且含对应环境的 .env.<环境> 文件。
# 说明：本脚本统一调用 deploy/deploy.sh <环境> config（按 APP_ENV 选择 .env.<环境>）

PROJECT=prism-search
SOURCE_CODE=${WORKSPACE}
WEB_PATH=/opt/web/prism-search
# 部署机登录用户与目标机 IP 与具体环境相关，由 Jenkins 凭据/构建参数注入，不写进仓库
WEB_USER=${WEB_USER:?请通过 WEB_USER 环境变量指定部署机登录用户}

# ── 1. 根据分支确定目标 IP 和环境 ──────────────────
echo ">>> 当前分支: ${branchs}"

if [ "${branchs}" == "origin/test" ]; then
    IP=${TARGET_IP:?请通过 TARGET_IP 环境变量指定 test 目标服务器 IP}
    APP_ENV="test"
else
    echo "[ERROR] 未识别的分支: ${branchs}，终止部署"
    exit 1
fi

echo ">>> 目标服务器: ${IP}  环境: ${APP_ENV}"

# ── 2. 推送代码（含点文件）─────────────────────────
echo ">>> 推送代码至 ${WEB_USER}@${IP}:${WEB_PATH}"
rsync -avzP \
    --include='.*' \
    --exclude='.git/' \
    --exclude='.idea/' \
    --exclude='.env' \
    -e "ssh -p22" \
    "${SOURCE_CODE}/" \
    "${WEB_USER}@${IP}:${WEB_PATH}/"


# ── 3. 远程执行部署脚本（deploy.sh 按 APP_ENV 选择 .env.<环境>）──
echo ">>> 远程执行 deploy/deploy.sh ${APP_ENV} config"
ssh "${WEB_USER}@${IP}" WEB_PATH="$WEB_PATH" APP_ENV="$APP_ENV" bash <<'EOF'
chmod +x "$WEB_PATH/deploy/deploy.sh"
"$WEB_PATH/deploy/deploy.sh" "$APP_ENV" config
EOF
echo ">>> 部署完成 [${PROJECT}] [${APP_ENV}] [${IP}]"
