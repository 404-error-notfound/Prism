# 部署与运维

服务器自托管部署的操作手册。重点是几个反复踩过的坑——按这里来能少走弯路。

## 目录

- 前置：docker compose 命令
- 部署脚本（生产走这个，别手工敲 compose）
- 故障转移（F5 不可改：故障切换做在 nginx 层）
- 首次部署
- `.env` 是 gitignore 的——必须手动维护
- 模型权重在 `hf_cache` 命名卷里——别删卷挂载
- 改了东西之后，重启还是重建？
- 存储驱动是 vfs（老 CentOS7 机器）——部署慢/失败的根源
- proxy-hub（代理池服务，必须配套部署）
- 验证清单
- 代理调优旋钮（都在 proxy-hub 侧，热配置免重启）

## 前置：docker compose 命令

- 优先用 `docker compose`（带空格，Docker CLI 插件）。
- 老的 `docker-compose`（连字符，独立二进制）在部分老机器上是 Python 3.7 版本、个别子命令会崩；但 `restart`/`up`/`down` 通常可用，作为后备。
- 两者读同一个 `docker-compose.yml`，不要混用到一半。

## 部署脚本（生产走这个，别手工敲 compose）

```bash
bash deploy/deploy-prod.sh config       # .env / docker-compose.yml 变更 → 强制重建
bash deploy/deploy-prod.sh build        # Dockerfile / pyproject.toml 变更 → 构建并重建
bash deploy/deploy-prod.sh restart      # 只改了 app/**.py 代码（默认动作）
bash deploy/deploy-prod.sh stop|down    # 停机（stop 保留容器，down 销毁）

bash deploy/recover-docker.sh           # 部署卡死时的宿主机自救（见「存储驱动是 vfs」）
bash deploy/diagnose.sh                 # 部署后容器"消失/崩溃"的只读取证（不改任何状态）
```

脚本按 `prod -> .env.prod` 同步环境变量到 `.env`，日志落在 `deploy/logs/`。
**恒定逐服务串行部署**（顺序：baidu → bing → quark → sogou → wechat → gateway），
不再按存储驱动分流——串行才能做到"一个服务失败不牵连其他服务"，且日志不再交织。

**输出带时间轴**：每一步都有 `[HH:MM:SS]` 前缀，容器创建/清理还带 `耗时 Ns`，
结尾汇总 `总耗时`。daemon 超时是 300s 量级的静默等待，只靠内容看不出卡在哪一步——
翻 `deploy/logs/deploy-prod-config-*.log`，看时间戳跳到哪一步停住、上一个服务耗时多少，
就能直接定位。若单个服务耗时逼近 300s（`COMPOSE_HTTP_TIMEOUT`），说明宿主机已经到极限了。

**收工复核（`verify_stable`）**：健康检查通过 ≠ 服务稳定。出过这样一次事故——脚本报了
`全部服务正常运行`、`/health` 也通了，可随后 `docker ps` 里 gateway 已经不在了，它是在脚本
收工之后才崩的。所以收工前会再采样一次状态。

判失败（`exit 1`）只有三种情况，都是"服务确实没在正常服务"：

1. 采样时不在运行态（`exited`/`dead`/`created`/容器已不存在，即 `docker ps` 里看不到它）；
2. 处于崩溃循环（`running(restarting)`：`Status` 仍是 running，但容器在两次重启的间隙被采样到）；
3. 在复核窗口内重启次数增长（说明还没稳定下来）。

**只提示、不判失败的**：`RestartCount > 0` 但当前稳定 `running`——启动期崩过一次被
`restart: unless-stopped` 拉起后自愈了。**不要把它收紧成硬判据**：这台机器上 gateway 冷启动
要 200s+（vfs 下创建=整份镜像拷贝），首次启动偶发退出、重启后 `healthy` 是自愈。2026-09-17
就踩过这个坑：拿"新建容器 `RestartCount` 必须为 0"当硬判据，把一次**成功**的部署判成失败，
还会诱导人工重跑 `config`——vfs 上重跑一轮就是十几次整份镜像拷贝，纯亏。

采样窗口默认 20s，可调：`VERIFY_SETTLE_SECONDS=60 bash deploy/deploy-prod.sh config`。

## 故障转移（F5 不可改：故障切换做在 nginx 层）

生产拓扑：**用户 → F5（VIP，两个 member）→ 两台机器各自 nginx:80 → 各自 gateway:8081**。
F5 归网络组管、**不能改** → 不能依赖 F5 的 monitor 摘节点。故障转移全部做在 nginx 层：
两台机器的 nginx 互为对方的备用后端（`upstream backup`），本机 gateway 挂了/部署中，
nginx 直接把请求转给对端。gateway 无状态（Redis 是远程集群），跨机转发安全。

| 故障范围 | 谁来救 | 调用方收到 |
|---|---|---|
| 本机 gateway 挂 / 重建中 | nginx：`backup` 自动转对端 | 无感（对端正常业务 JSON） |
| 部署期间（维护标记放置） | nginx：整站切到对端 | 无感（滚动部署） |
| 两台 gateway 都挂 | nginx：降级 JSON | `503 {"code":-32001,"message":"服务维护中，请稍后重试"}` |
| 本机 nginx 挂 / 整机宕 | **nginx 层无解**（本属 F5 的职责） | 取决于 F5 是否配了 monitor |

### nginx 配置（两台机器同一份，仅对端 IP 不同）

> **生产实际配置**：`deploy/<内部域名>.conf`（域名 `<内部域名>`，
> 标记路径 `/opt/nginx/maintenance/prism.flag`，nginx 装在 `/opt/nginx`）。
> <NODE1> 的对端是 <NODE2>，<NODE2> 上把两处 `<NODE2_IP>` 换成 `<NODE1_IP>`。
> 降级响应是**接口 JSON**（本服务是接口，不返回 HTML 页面），由 nginx 直接 `return`，
> 无需任何静态文件。`message` 文案按前端/调用方的约定改。下面为通用示例，与实际文件一致：

```nginx
upstream prism_gw {
    server 127.0.0.1:8081 max_fails=3 fail_timeout=30s;   # 本机 gateway
    server <对端IP>:8081 backup;                          # 对端：本机失败/部署中才用
}
upstream prism_peer_only {
    server <对端IP>:8081;                                  # 维护标记存在时全走对端
}

server {
    listen 80;
    server_name search.example.com;                        # 换成实际域名

    # 维护标记 = 本机流量整体切到对端（部署脚本 touch/rm，不需要 reload）。
    # if 里只能 set（安全用法）；proxy_pass 用变量选 upstream，不能写进 if 块。
    set $prism_backend prism_gw;
    if (-f /opt/nginx/maintenance/prism.flag) {
        set $prism_backend prism_peer_only;
    }

    # 双故障兜底：两台 gateway 都失败 → 降级 JSON（nginx 直接返回，无需静态文件）。
    # 502/504 统一转为 503（调用方语义 = 临时性可重试）；上游自己的 503 原样透传。
    # = 后必须显式带码：孤立的 = 会用替代资源的响应码（200），故障就被掩盖了。
    proxy_intercept_errors on;
    error_page 502 504 =503 @fallback;

    location / {
        proxy_pass http://$prism_backend;                  # 变量选 upstream
        proxy_http_version 1.1;
        proxy_connect_timeout 3s;                          # 默认 60s，故障时调用方干等
        proxy_read_timeout    30s;                         # 搜索请求 ~6s，按实际调
        proxy_next_upstream error timeout http_502 http_503 http_504;   # POST 不重试，防重复提交
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 降级响应：与 gateway 全局异常格式对齐（app/main.py 兜底是
    # {"code":-32000,"message":...}，-32001 = 服务维护中，JSON-RPC 保留区）。
    # ⚠️ 这里 return 的 503 不在 error_page 列表（列表只有 502/504）→ 不会递归触发。
    location @fallback {
        internal;
        default_type application/json;
        add_header Access-Control-Allow-Origin "*" always;    # 跨域调用方也要能读到降级响应
        add_header Retry-After 30 always;
        add_header X-Degraded "gateway-unreachable" always;   # 监控据此区分降级与正常
        add_header X-Prism-Node "$hostname" always;           # 一眼看出响应来自哪台
        return 503 '{"code":-32001,"message":"服务维护中，请稍后重试"}';
    }
}
```

**双故障时调用方收到的降级响应**：

```http
HTTP/1.1 503
Content-Type: application/json
X-Degraded: gateway-unreachable        ← 监控靠这个头区分"真正常"与"降级中"
X-Prism-Node: <机器名>                  ← 一眼看出响应来自哪台

{"code":-32001,"message":"服务维护中，请稍后重试"}
```

无需任何静态文件、无需 fallback 目录。

**落地前提：两台互访 8081（必须先验证）**——
`curl -s -m 3 http://<对端IP>:8081/health` 两台互测，不通就开 firewalld 与云安全组。
**backup 必须指对端的 8081（gateway），不要指对端的 80（nginx）**，否则绕环。

### 部署流程（发布平台逐台调用 = 全自动滚动）

生产机是发布平台的 rsync 副本，**平台会逐台执行 `bash deploy/deploy-prod.sh <action>`**：
先一台、退出码 0（部署+验证全过）才执行下一台。prod 特有逻辑**全部在 `deploy-prod.sh`**
（维护窗口、对端预检、失败保持切对端）；`deploy.sh` 是 test/本地/prod 共用的环境通用脚本，
**不含任何 prod 逻辑，改动它必须兼容 test 与本地**。每台的完整滚动语义：

```text
放置标记 → 本机 nginx 全量切对端 → 逐服务串行重建 → 状态校验+健康检查+收工复核 → 移除标记切回本机
```

- 部署中、部署失败的机器，流量都在对端——**用户全程无感**
- 部署失败时流量**保持**在对端（`NGINX_KEEP_FLAG_ON_FAILURE` 默认 1，无人值守安全取值）；
  平台重试或人工重跑即自愈：重放同一标记 → 成功 → 自动清除
- 每台部署前**硬性预检对端健康**：`NGINX_PEER_URL` 不用配置——`deploy-prod.sh` 未取到值时
  按本机 IP 自动推导镜像对端（<NODE1_IP>↔<NODE2_IP>），预检非 200 直接中止（此刻未动任何标记
  与容器）——双机环境对端不可达 = 没有流量承接者，绝不硬上
- `test` 环境（单机）自动跳过维护窗口，无需任何配置

维护窗口常量（域名 `<内部域名>`、标记路径 `/opt/nginx/maintenance/prism.flag`、
探测地址 `http://127.0.0.1/health`）全部内置在 `deploy-prod.sh` 的默认值里，
`.env.prod` 无需任何 NGINX_* 配置；`NGINX_PEER_URL` 按本机 IP 自动推导。

**残余风险**（nginx 层无解）：本机 nginx 挂 / 整机宕只能靠 F5 的 monitor（网络组管）。
验证方法：低峰期停一台 nginx 30 秒看流量是否自动全落另一台。

上线后各演练一次：

```bash
docker stop prism-gateway      # 本机故障 → 请求应仍 200（来自对端），数秒内切换
docker start prism-gateway
touch /opt/nginx/maintenance/prism.flag
curl -sI -H 'Host: <内部域名>' http://127.0.0.1/health | head -1   # 200（由对端响应）
rm /opt/nginx/maintenance/prism.flag
```

误留标记的急救：`rm -f /opt/nginx/maintenance/prism.flag`（删掉即恢复，不需要 reload）。

> **改完 `deploy/` 下的脚本，记得同步到生产机再执行。** 这台服务器是发布平台 rsync 过去的
> 副本，本地未提交/未同步的改动生产上根本不存在——2026-09-17 就出现过"本地已修、生产重跑
> 旧脚本、同一个 runc EOF 原样复发"的情况。改脚本后先看 `git status` 是否干净，再上生产。

### 部署报成功但容器不在 `docker ps` 里，怎么查

部署脚本只在收工那一刻采样一次，容器在那之后崩掉它不会知道。先跑一条命令收齐证据：

```bash
bash deploy/diagnose.sh           # 只读取证，默认看最近 60 分钟
bash deploy/diagnose.sh 19:10:00  # 指定起始时刻（本地时间）
```

它只读、不改任何状态，输出 8 段：容器清单 / 关键字段（状态·退出码·OOM·重启次数·起止时间）/
容器事件（含 `destroy`）/ 内核 OOM / dockerd 日志 / 磁盘与 inode / 内存 / 各服务日志尾部。
手工等价命令（想单独跑某一段时用）：

```bash
docker ps -a | grep prism                          # 终态：Exited/Dead/Created/根本没有
docker inspect prism_gateway_1 --format \
  '状态={{.State.Status}} 退出码={{.State.ExitCode}} OOM={{.State.OOMKilled}} 重启={{.RestartCount}} 结束={{.State.FinishedAt}} 错误={{.State.Error}}'
docker events --since '2026-09-17T19:00:00' --filter container=prism_gateway_1   # 看有没有 destroy/oom 事件
dmesg -T | grep -iE 'oom|killed process' | tail -20
journalctl -u docker --since '19:00' --no-pager | tail -50
df -h /var/lib/docker; df -i /var/lib/docker; free -m
docker logs --tail 50 prism_gateway_1
```

对照判读：

| 现象 | 结论 |
|---|---|
| `Exited (137)` / `OOMKilled=true` | 内存被打爆，容器被 OOM kill——vfs 下多容器并发重建 + torch 模型是放大器 |
| `Exited (1)` 且有应用日志 | gateway 自身崩（模型加载、Redis/ES 连接等），看 `docker logs` |
| `Dead` 状态 | runc/daemon 层创建失败留下的不可恢复态，与 searxng 的 `runc EOF` 同源 |
| `Created` 状态 | 又是空壳：进程根本没起来，同 `runc EOF` 一族 |
| `docker ps -a` 里压根没有 | 容器是被 `docker rm` 掉的，`docker events` 里会有 `destroy` 事件（`clean.sh`、`clean_legacy_projects`、compose 的 rename-then-remove 三段都可能） |

## 首次部署

```bash
git clone <repo> /opt/web/prism && cd /opt/web/prism
cp .env.example .env          # 然后填真实值（见下）
docker compose build gateway  # 首次构建：torch + 烘焙模型，约 10-20 分钟（走国内镜像源）
docker compose up -d
docker logs prism-gateway -f 2>&1 | grep -iE "embedder ready|reranker ready|Prism ready"
```

`Prism ready at ...` 出现即就绪（这台机器冷启动加载模型要几分钟，期间 `unhealthy` 是正常的）。

## `.env` 是 gitignore 的——必须手动维护

`git clone`/`git pull`/同步代码**都不会创建或更新 `.env`**（它装密钥，不进版本库）。模板是 `.env.example`，真实值在 `.env`，由你维护。**删目录重拉代码后 `.env` 会丢**，记得备份/还原。

必须正确填写、且只存在于 `.env` 的关键项：

| 变量 | 丢了的后果 |
|---|---|
| `PROXY_SERVICE_URL`（proxy-hub 服务地址） | 活跃上报/强制刷新停用；proxy-hub 侧代理池断供后 IP 逐步死光 → 零结果。供应商 API/账密在 proxy-hub 的 `.env`（同样别丢） |
| `RERANKER_MODEL=BAAI/bge-reranker-base` | 默认 `none`，reranker 不加载，排序质量下降 |
| `REDIS_URL` / `REDIS_PASSWORD` / `REDIS_CLUSTER_MODE=true` | 连不上 Redis，启动直接退出 |
| `ES_URL` / `ES_USER` / `ES_PASSWORD` | 热点索引/日志写入失败 |
| `HMAC_SALT` / `ADMIN_BOOTSTRAP_KEY` | 鉴权异常（`.env.example` 里这俩是空的，必须填）|

`OMP_NUM_THREADS=1` / `TOKENIZERS_PARALLELISM=false` 已写进 `docker-compose.yml` 的 gateway `environment`，compose 会注入，`.env` 没有也生效。

## 模型权重在 `hf_cache` 命名卷里——别删卷挂载

gateway 的模型（embedder + reranker）存在 `hf_cache` 命名卷。命名卷首次挂载会从镜像内容播种；旧镜像若未成功烘焙模型，模型就**只**存在卷里。

- **不要**删 `docker-compose.yml` 里 `- hf_cache:/root/.cache/huggingface` 这行——删了新容器会去 HuggingFace 重新下载，`cas-bridge.xethub.hf.co` 是国际节点、会超时卡死启动。
- `docker compose down` **不要加 `-v`**（`-v` 会连命名卷一起删，模型没了又得重下）。
- 确认卷里有模型：`ls "$(docker volume inspect prism_hf_cache --format '{{.Mountpoint}}')/hub/"`

## 改了东西之后，重启还是重建？

| 改动 | 操作 |
|---|---|
| `app/**.py` 代码（volume 挂载） | `docker compose restart gateway`（进程重启即重新 import）|
| **`.env`（env_file）** | `docker compose up -d gateway`（**`restart` 不重读 .env！** env_file 只在容器创建时读一次，restart 沿用旧环境——改了 .env 用 restart 等于没改，必须 up -d 重建）|
| `docker-compose.yml` 的 `environment`/`volumes` | `docker compose up -d --force-recreate gateway`（restart 不读 compose 变更）|
| `searxng/settings.template.yml` 或 `limiter.toml` | `docker compose up -d --force-recreate searxng-baidu searxng-bing searxng-quark searxng-sogou searxng-wechat`（settings.yml 由 entrypoint 渲染，`restart` 不重新渲染，必须重建）|
| **宿主机 repo 目录被删除重建** | `docker compose up -d --force-recreate`（bind mount 指向旧 inode 已失效，`restart` 救不回，症状：容器内 `getcwd() failed` / `No module named app`）|

> 注意：日常**换 IP 不需要任何重启**——proxy-hub 隧道后端自动轮询活 IP 池，零窗口。只有上面这些配置结构变更才需重启。

## 存储驱动是 vfs（老 CentOS7 机器）——部署慢/失败的根源

先确认自己在不在这个坑里：

```bash
docker info -f '{{.Driver}}'     # 返回 vfs 就是
docker version --format '{{.Server.Version}}'   # 18.09 之类的老版本通常配套 vfs
```

vfs **没有层共享**：每创建一个容器就是把整份镜像目录完整拷一遍。gateway 镜像带 torch
加烘焙模型（数 GB），一个容器就是数 GB 的拷贝量，并发创建直接压垮老 daemon。

2026-09-17 生产事故的原始报错：

```text
ERROR: for prism_gateway_1  UnixHTTPConnectionPool(host='localhost', port=None): Read timed out. (read timeout=300)
ERROR: for prism_searxng-quark_1  Cannot start service searxng-quark: OCI runtime create failed:
  container_linux.go:345: starting container process caused "process_linux.go:303:
  getting the final child's pid from pipe caused \"EOF\"": unknown
```

三层原因，按重要性排：

1. **`depends_on` 把串行策略打穿了。** `docker-compose.yml` 里 `gateway` 声明了 5 个
   searxng 依赖，`docker compose up -d --force-recreate gateway` 会把这 5 个依赖**一起
   重建**——脚本自己的"串行循环"走到最后一步就塌成 6 容器并发。日志里的证据是：
   `>>> 部署 gateway` 之后紧跟着 5 个 `Recreating prism_searxng-*`。
   修法：串行分支加 `--no-deps`（依赖已在前序循环部署完）。
2. **runc EOF 是资源被榨干的结果，不是配置错误。** daemon 在并发整份拷贝下内存/磁盘/
   inode 吃紧，runc 的子进程被 OOM 或 fork 失败，在写回 pid 之前就死了，daemon 只能报
   `pid from pipe caused "EOF"`。每次失败还会留下一具 `created` 状态的空壳占着容器名，
   下一轮 compose 只能用 `<hash>_<原名>` 的临时名绕过——日志里出现
   `b08eb3f55cc8_prism_searxng-quark_1` 这种前缀就是这个，说明状态已经滚乱了。
3. **重试会放大问题。** 每轮把 6 个服务全量重建，5 轮 = 30 次整份拷贝，越重试越差。

`deploy/deploy.sh` 已按上述三点改过：串行分支 `--no-deps`、跨轮次跳过已成功重建的服务、
每轮开跑前清理 `created`/`dead` 残留、`COMPOSE_HTTP_TIMEOUT` 默认 600（可用环境变量覆盖）。

### 现场卡死了怎么办

部署脚本自己在 daemon 卡死时是跑不动的，先跑宿主机侧自救：

```bash
bash deploy/recover-docker.sh                  # 体检 + 清残留（安全，不动运行中的容器）
bash deploy/recover-docker.sh --restart-daemon # 清理完仍 runc EOF 才需要；会重启整机 Docker
bash deploy/deploy-prod.sh config              # 再重新部署
```

体检输出里重点看两个数：`df -h <DockerRootDir>` 的可用空间、`df -i` 的 inode。
vfs 下磁盘消耗是其他驱动的数倍，余量低于镜像总量就该扩盘，或把 `/var/lib/docker`
换到更大的盘。

### 根治：切 overlay2

vfs 只应作为兜底，别当长期方案。切之前先确认文件系统支持：

```bash
df -h /var/lib/docker                      # 找到挂载点
xfs_info /var/lib/docker                   # XFS 需 ftype=1；ext4 直接支持
```

`ftype=1` 就改 `/etc/docker/daemon.json` 加 `"storage-driver": "overlay2"`，
停 docker、清空 `/var/lib/docker`（**镜像和容器全丢，模型在 `hf_cache` 卷里也会一起丢，
切之前先 `docker save` 出镜像或准备好重建**）再启动。

如果是根分区本身用 overlay（在容器里跑 Docker）或 XFS `ftype=0`，只能重做文件系统或
把 `/var/lib/docker` 挪到一块独立盘上。

顺带把 Docker 升到 20.10+：老版本的 runc 更容易在压力下抛 EOF，升上去之后
`docker-compose.yml` 顶部那段 `seccomp:unconfined` + `pids_limit: 0` 的 clone3 兼容
补丁也可以一并删掉。

## proxy-hub（代理池服务，必须配套部署）

代理池已拆分为独立 proxy-hub 服务（独立仓库），gateway 不再自维护代理池。proxy-hub 自建
HTTP 转发代理提供隧道（固定隧道地址，每连接按目标站防重轮换挑上游 IP），prism 机器上不再有
本机 forwarder，也没有 gost sidecar。proxy-hub 按项目隔离池：先在其侧 `POST /admin/projects`
注册 "prism" 项目拿 token，然后每台 gateway 的 `.env` 只需：

```bash
PROXY_SERVICE_URL=http://<任一 proxy-hub 副本>:8100   # 拓扑唯一事实源：控制面 + 隧道推导（端口换 8101）
PROXY_PROJECT_TOKEN=<注册 prism 项目获得的 token>     # /activity /refresh /status 鉴权 + 隧道账密注入
```

隧道地址无需单独配置：SearXNG 的 `outgoing.proxies` 由容器 entrypoint 从
`PROXY_SERVICE_URL` 推导渲染（host 相同、端口换 8101，账密 `prism:<token>` 注入），
gateway 侧同样按此规则推导。加/换 proxy-hub 机器只改 `PROXY_SERVICE_URL` 一行。

- proxy-hub 部署见 proxy-hub 仓库 README（同镜像跑 2 副本即主备，Redis 锁协调，无需选主）。
- proxy-hub 需可达：Redis、供应商 API。
- proxy-hub 宕机时：隧道后端保留最后一批活 IP 继续工作（风险有界 = IP 剩余寿命），
  gateway 的活跃上报/强制刷新静默失效，搜索主流程不受影响。

## 验证清单

```bash
# 1. 健康
docker exec prism-gateway curl -s -m 3 http://localhost:8001/health   # {"status":"ok"}

# 2. 隧道代理活着（出网全靠它）
docker exec prism-gateway curl -s -m 5 -x http://<PROXY_HUB_IP>:8101 -o /dev/null -w "%{http_code}\n" https://cn.bing.com/

# 3. proxy-hub 健康与池状态（IP 轮换由它驱动，换批日志看它的容器）
docker exec proxy-hub curl -s -m 3 http://localhost:8100/status

# 4. 端到端：连发不同词（避开 Redis 缓存），看零结果率
#    （同一 query 1h 内会命中缓存；0 结果不缓存，可重试）
```

## 代理调优旋钮（都在 proxy-hub 侧，热配置免重启）

短效轮换 IP 的零结果率有物理下限（IP 会超时/中途死/被 CAPTCHA）。调优项已移到 proxy-hub：
探测超时/探测目标/提取数量/巡检频率/冷热时段表，经 `PUT /admin/config` 热生效（≤15s），
默认值在 proxy-hub 的 env（见 proxy-hub 仓库 README 配置全表）。探测目标务必用 bing
（`cn.bing.com/search?q=新闻`），**别用百度**会全误杀。

想近乎零零结果，只能换住宅 IP，或 `TIER2_DEFAULT` 开付费兜底（**去重/denylist/spam 过滤后**结果 < 5 才触发 Tier2——垃圾占位不再算数）。免费侧也可 `CHINASO_ENABLED=true` 加中国搜索权威源（需代理池有活 IP）。

排错速查见 [`../CLAUDE.md`](../CLAUDE.md) 的「常见错误与排查」表。
