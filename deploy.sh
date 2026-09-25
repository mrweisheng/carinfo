#!/usr/bin/env bash
# GitHub push → 自动部署。由 webhook 监听器调用（见 DEPLOY.md §6），不要手工跑。
#
# 需要的授权（/etc/sudoers.d/carinfo-deploy）：
#   carinfo ALL=(root) NOPASSWD: /bin/systemctl restart carinfo-api.service
#   carinfo ALL=(root) NOPASSWD: /bin/systemctl restart --no-block carinfo-service.service
#   carinfo ALL=(root) NOPASSWD: /bin/systemctl is-active carinfo-api.service
#
# ⚠️ 部署前请按实际环境改这三个变量：
REPO_DIR="${CARINFO_REPO_DIR:-/opt/carinfo}"
BRANCH="${CARINFO_BRANCH:-main}"
UV_BIN="${CARINFO_UV_BIN:-/home/carinfo/.local/bin/uv}"

set -euo pipefail

LOCK="/tmp/carinfo_deploy.lock"
LOG_TAG="carinfo-deploy"

log() { echo "[$(date '+%F %T')] [$LOG_TAG] $*"; }

# ---- 单实例：并发 push 时后到的直接退出，避免两个 git pull 打架 ----
exec 9>"$LOCK"
if ! flock -n 9; then
    log "已有部署在进行中，本次跳过"
    exit 0
fi

cd "$REPO_DIR"

# ---- 1. 拉代码 ----
# 用 fetch + reset --hard 而不是 pull：服务器上是纯部署目录，不需要保留本地修改，
# reset --hard 能保证「服务器上跑的一定就是远端那个 commit」
BEFORE=$(git rev-parse HEAD)
git fetch --prune origin
git reset --hard "origin/$BRANCH"
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    log "代码无变化（$AFTER），跳过重启"
    exit 0
fi
log "代码更新：$BEFORE → $AFTER"

# ---- 2. 判断依赖是否变化 ----
DEPS_CHANGED=0
if ! git diff --quiet "$BEFORE" "$AFTER" -- uv.lock pyproject.toml; then
    DEPS_CHANGED=1
fi

# ---- 3. 配置模板变了就提醒（config.json 不在库里，不能自动覆盖）----
if git diff --name-only "$BEFORE" "$AFTER" | grep -qx "config.json.example"; then
    log "⚠️ config.json.example 有变动，请人工核对服务器上的 config.json 是否需要同步更新！"
fi

if [ "$DEPS_CHANGED" = "1" ]; then
    log "依赖有变化，执行 uv sync ..."
    if ! "$UV_BIN" sync --frozen; then
        log "✗ uv sync 失败，回滚代码并中止部署"
        git reset --hard "$BEFORE"
        exit 1
    fi
else
    log "依赖无变化，跳过 uv sync"
fi

# ---- 4. 导入自检（坏代码不进服务）----
IMPORT_LOG="/tmp/carinfo_deploy_import.log"
if ! "$UV_BIN" run --no-sync python -c "
import sys
sys.path.insert(0, 'src')
import carinfo.service        # 调度服务
import carinfo.search.api     # 搜索 API
" >"$IMPORT_LOG" 2>&1; then
    log "✗ 导入自检失败，回滚代码并中止部署："
    cat "$IMPORT_LOG"
    git reset --hard "$BEFORE"
    if [ "$DEPS_CHANGED" = "1" ]; then
        "$UV_BIN" sync --frozen || true
    fi
    exit 1
fi
log "导入自检通过"

# ---- 5. 重启服务 ----
# carinfo-api 是短生命周期的 HTTP 服务，直接重启。
systemctl restart carinfo-api.service
log "✓ carinfo-api 已重启"

# ⚠️ carinfo-service 是爬虫调度服务：若此刻正有一轮在跑，restart 会发 SIGTERM，
#    而它的单元里配了 TimeoutStopSec=infinity（等本轮跑完才退）。
#    所以用 --no-block：不阻塞部署流程，服务会在本轮结束后自行完成切换。
systemctl restart --no-block carinfo-service.service
log "✓ carinfo-service 重启已下发（若正有一轮在跑，会等它跑完再切换）"

# 给一点时间让 API 起来，失败就报出来（而不是静默成功）
sleep 3
if systemctl is-active --quiet carinfo-api.service; then
    log "✓ 部署完成：$AFTER"
else
    log "✗ carinfo-api 启动失败！请查 journalctl -u carinfo-api -n 50"
    exit 1
fi
