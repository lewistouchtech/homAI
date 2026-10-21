#!/bin/bash
# HomeBrain Server 启动脚本
# 路径自定位：脚本放在哪台机器哪个目录都能跑（Mac Mini 部署依赖这一点）
# - 启动前清理端口，防止 launchd 重启时端口冲突
# - HomeBrain 起来后，若配了 OpenViking（data/openviking/ov.conf），也拉起它

set -e

# launchd 环境的 PATH 不含 /usr/sbin（plist 里只配了 venv/bin:/opt/homebrew/bin:
# /usr/local/bin:/usr/bin:/bin），导致下面的 lsof 找不到命令、端口清理静默失败：
# 旧进程杀不掉 → 新 app.py 撞端口退出 → launchd KeepAlive 每 10 秒重复拉起（死循环）。
# 2026-10-01 修复：脚本内补齐系统工具路径，保证任何环境下 lsof/ps 都可用。
export PATH="/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 需要清理的端口：http_port + WebSocket port
# 必须与 config.yaml / data/.config.yaml 的 server.http_port、server.port 保持一致。
# 注意：历史遗留的 8000 是 oMLX 推理服务端口，绝不能杀（P0-01 端口单一事实源在 Phase 2 处理）。
PORTS="8003 9800"

# 判断 PID 是否属于 HomeBrain 自己的进程（避免误杀 oMLX 等其它服务）
is_homebrain_process() {
    local pid="$1" cmd cwd
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
    [ -z "$cmd" ] && return 1
    # 必须是 python 在跑 app.py
    case "$cmd" in
        *python*app.py*) ;;
        *) return 1 ;;
    esac
    # 且命令行路径或工作目录必须指向本仓库
    case "$cmd" in
        *homebrain-server*) return 0 ;;
    esac
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    [ "$cwd" = "$SCRIPT_DIR" ] && return 0
    return 1
}

for port in $PORTS; do
    pids=$(lsof -ti :$port 2>/dev/null || true)
    for pid in $pids; do
        if is_homebrain_process "$pid"; then
            echo "[$(date)] Port $port held by HomeBrain PID $pid, stopping..."
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        else
            echo "[$(date)] WARNING: port $port held by PID $pid ($(ps -p "$pid" -o comm= 2>/dev/null || echo unknown)), not HomeBrain - skip"
        fi
    done
done

# 拉起 OpenViking（如果配置了）— 失败不阻塞 HomeBrain 启动
if [ -f "$SCRIPT_DIR/data/openviking/ov.conf" ]; then
    if launchctl print "gui/$(id -u)/com.homebrain.openviking" >/dev/null 2>&1; then
        echo "[$(date)] OpenViking 由 launchd 管理，跳过手动启动"
    elif ! lsof -i :8765 2>/dev/null | grep -q LISTEN; then
        echo "[$(date)] Starting OpenViking service..."
        bash "$SCRIPT_DIR/start_openviking.sh" &
        # 不等它完成，允许 HomeBrain 先起来（OpenViking 有懒加载重连兜底）
    else
        echo "[$(date)] OpenViking already running"
    fi
fi

echo "[$(date)] Starting HomeBrain Server..."
exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/app.py"
