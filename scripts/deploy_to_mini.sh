#!/usr/bin/env bash
# HomeBrain 部署脚本：MacBook（本机）→ Mac mini 生产
# 用法: bash scripts/deploy_to_mini.sh          # 同步代码并重启生产
#       bash scripts/deploy_to_mini.sh --check  # 只比对不修改（dry-run）
#
# 规则：
#   1. 只同步代码文件，不同步配置（config.yaml / data/.config.yaml 两机差异是刻意的：
#      本机 9800 端口 + oMLX，mini 8000 端口 + 生产 LLM/TTS）
#   2. 不同步数据（data/ 各机独立）
#   3. 前端 console 整目录同步（两机内容应一致）
#   4. 部署后自动重启生产并冒烟验证

set -e
MINI="YOUR_USER@YOUR_SERVER_IP"
REMOTE_BASE="~/Projects/homebrain"
LOCAL_BASE="$(cd "$(dirname "$0")/../../.." && pwd)"
DRY_FLAG="-n"
DEPLOY=0
[[ "$1" == "--check" ]] && DRY_FLAG="" && DEPLOY=1

echo "═══ HomeBrain 部署到 Mac mini ═══"

# 1. 服务端代码（rsync 排除配置/数据/模型/依赖）
echo "── 1/4 同步 server/homebrain-server ..."
rsync -rc $DRY_FLAG --itemize-changes \
  --exclude venv --exclude data --exclude tmp --exclude models --exclude asset \
  --exclude __pycache__ --exclude .git --exclude "*.log" --exclude "*.pyc" \
  --exclude ".DS_Store" --exclude "*.bak*" --exclude "*.broken" --exclude ".backup_*" \
  --exclude .pytest_cache --exclude music --exclude config_backups \
  --exclude config.yaml --exclude data/.config.yaml \
  "$LOCAL_BASE/server/homebrain-server/" "$MINI:$REMOTE_BASE/server/homebrain-server/" 2>/dev/null | grep -vE "^\.d"
  [ ${PIPESTATUS[0]:-0} -ne 0 ] && echo "  ⚠️ 同步失败（mini 不可达或出错）" || true

# 2. 前端（console）
echo "── 2/4 同步 console/V15_Current/web ..."
rsync -rc $DRY_FLAG --itemize-changes --exclude .DS_Store \
  "$LOCAL_BASE/console/V15_Current/web/" "$MINI:$REMOTE_BASE/console/V15_Current/web/" 2>/dev/null | grep -vE "^\.d"
  [ ${PIPESTATUS[0]:-0} -ne 0 ] && echo "  ⚠️ 同步失败（mini 不可达或出错）" || true

[[ -n "$DRY" ]] && { echo "═══ dry-run 完成，未做任何修改 ═══"; exit 0; }

# 3. 重启生产服务
echo "── 3/4 重启生产服务 ..."
ssh "$MINI" "cd $REMOTE_BASE/server/homebrain-server && bash restart.sh >/dev/null 2>&1; sleep 15"

# 4. 冒烟验证
echo "── 4/4 冒烟验证 ..."
ssh "$MINI" "
  WSCOUNT=\$(lsof -nP -i :8003 -i :9800 2>/dev/null | grep -ci listen)
  HEALTH=\$(curl -s -m 5 http://localhost:8003/api/health | head -c 40)
  DASH=\$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://localhost:8003/dashboard)
  CHATTTS=\$(curl -s -m 3 http://127.0.0.1:8082/ 2>/dev/null | head -c 20)
  echo \"  端口监听(应为2): \$WSCOUNT\"
  echo \"  /api/health: \$HEALTH\"
  echo \"  dashboard: \$DASH\"
  echo \"  ChatTTS: \$CHATTTS\"
  if [ \"\$WSCOUNT\" = \"2\" ] && [ \"\$DASH\" = \"200\" ]; then
    echo \"═══ 部署成功 ═══\"
  else
    echo \"═══ ⚠️ 冒烟未全部通过，请检查 ═══\"
    exit 1
  fi
"
