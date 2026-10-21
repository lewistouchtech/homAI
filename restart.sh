#!/usr/bin/env bash
# HomeBrain 服务器重启脚本
# 用法: bash restart.sh

set -e

LOG_FILE="/tmp/homebrain_restart.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始重启服务器..." >> "$LOG_FILE"

# Mac mini/本机 homebrew 工具路径（ffmpeg 等），服务进程需要
export PATH="$PATH:/opt/homebrew/bin"

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 确保日志目录存在
mkdir -p "$SCRIPT_DIR/tmp"

# 1. 查找并杀掉旧的 HomeBrain 进程
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 查找旧进程..." >> "$LOG_FILE"

kill_port_process() {
    local port=$1
    local pids=""

    # 方式1: lsof
    pids=$(lsof -ti:${port} 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 端口 ${port} 占用进程 (lsof): $pids" >> "$LOG_FILE"
        echo "$pids" | xargs -r kill -9 2>/dev/null || true
        sleep 1
    fi

    # 方式2: fuser (更直接)
    if command -v fuser >/dev/null 2>&1; then
        pids=$(fuser ${port}/tcp 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 端口 ${port} 占用进程 (fuser): $pids" >> "$LOG_FILE"
            fuser -k -9 ${port}/tcp 2>/dev/null || true
            sleep 1
        fi
    fi

    # 方式3: netstat/ss + sudo 强制
    if lsof -i:${port} >/dev/null 2>&1 || (command -v ss >/dev/null 2>&1 && ss -tlnp | grep -q ":${port} "); then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 警告: 端口 ${port} 仍被占用，尝试 sudo 强制释放..." >> "$LOG_FILE"
        pids=$(lsof -ti:${port} 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs -r sudo kill -9 2>/dev/null || true
            sleep 2
        fi
    fi
}

# 杀掉端口 9800（WebSocket）和 8003（HTTP）的进程
kill_port_process 9800
kill_port_process 8003

# 再尝试通过进程名查找并杀掉（避免残留）
# 注意：这里只杀当前目录下的 python 进程，避免误杀其他项目
MY_PPID=$$
for pid in $(ps aux | grep -E "python.*app\.py|python.*main\.py|python.*run\.py" | grep -v grep | grep "$SCRIPT_DIR" | awk '{print $2}'); do
    # 不要杀掉自己或自己的子进程
    if [ "$pid" != "$MY_PPID" ] && [ "$pid" != "$$" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 杀掉残留进程: $pid" >> "$LOG_FILE"
        kill -9 "$pid" 2>/dev/null || true
    fi
done

# 杀掉可能残留的 nohup 进程（排除 LTM-server，避免误杀记忆服务）
for pid in $(pgrep -f "homebrain-server" 2>/dev/null || true); do
    if [ "$pid" != "$$" ] && [ "$pid" != "$MY_PPID" ]; then
        # 检查是否是 LTM 进程，是则跳过
        if ps -p "$pid" -o command= 2>/dev/null | grep -q "LTM-server"; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 跳过 LTM 进程: $pid" >> "$LOG_FILE"
            continue
        fi
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 杀掉 homebrain 残留进程: $pid" >> "$LOG_FILE"
        kill -9 "$pid" 2>/dev/null || true
    fi
done

sleep 2

# 杀掉遗留的 capture_sysaudio 子进程。
# 2026-09-18 补：主进程用 kill -9 结束（SIGKILL 不可捕获），app.py 的优雅
# 清理钩子不会执行，capture_sysaudio 会变成孤儿继续采集系统音频；
# 每次重启遗留一个，多个进程重复喂 AEC 参考信号会互相干扰。
if pgrep -f "capture_sysaudio" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 清理遗留的 capture_sysaudio 子进程" >> "$LOG_FILE"
    pkill -f "capture_sysaudio" 2>/dev/null || true
    sleep 1
fi

# 2. 检查端口是否释放
for port in 9800 8003; do
    if lsof -i:${port} >/dev/null 2>&1; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 严重警告: 端口 ${port} 仍被占用，尝试最后手段..." >> "$LOG_FILE"
        lsof -ti:${port} | xargs -r kill -9 2>/dev/null || true
        sleep 2
    fi
done

# 3. 重新启动服务器
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动服务器..." >> "$LOG_FILE"

# 激活虚拟环境（如果存在）
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已激活虚拟环境" >> "$LOG_FILE"
fi

# 尝试多种启动方式
START_CMD=""
# 优先使用 venv 中的 python（避免用到系统 python 导致依赖缺失）
PY_BIN="$SCRIPT_DIR/venv/bin/python3"
if [ ! -x "$PY_BIN" ]; then
    PY_BIN="python3"
fi
if [ -f "$SCRIPT_DIR/app.py" ]; then
    START_CMD="\"$PY_BIN\" \"$SCRIPT_DIR/app.py\""
elif [ -f "$SCRIPT_DIR/main.py" ]; then
    START_CMD="python3 $SCRIPT_DIR/main.py"
elif [ -f "$SCRIPT_DIR/run.py" ]; then
    START_CMD="python3 $SCRIPT_DIR/run.py"
else
    # 查找任意包含启动逻辑的 Python 文件
    START_FILE=$(find "$SCRIPT_DIR" -maxdepth 1 -name "*.py" -exec grep -l "asyncio.run\|web.run_app\|app.run\|serve(" {} \; 2>/dev/null | head -1)
    if [ -n "$START_FILE" ]; then
        START_CMD="\"$PY_BIN\" \"$START_FILE\""
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 错误: 找不到启动文件" >> "$LOG_FILE"
        exit 1
    fi
fi

# 使用 nohup 启动，脱离当前 shell
nohup bash -c "$START_CMD" > "$SCRIPT_DIR/tmp/server.log" 2>&1 &

NEW_PID=$!
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 服务器已启动 (PID: $NEW_PID)" >> "$LOG_FILE"
echo "$NEW_PID" > "$SCRIPT_DIR/tmp/server.pid"

sleep 3
# 检查是否成功启动
if ps -p "$NEW_PID" > /dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 重启成功! PID=$NEW_PID" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 警告: 进程可能启动失败，请检查日志: $SCRIPT_DIR/tmp/server.log" >> "$LOG_FILE"
fi

# 自动启动 Ltm（如果配置了 LTM 记忆）
if [ -f "$SCRIPT_DIR/data/LTM/ov.conf" ]; then
    if launchctl print "gui/$(id -u)/com.homebrain.LTM" >/dev/null 2>&1; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ltm 由 launchd 管理，跳过手动启动" >> "$LOG_FILE"
    elif ! lsof -i :8765 2>/dev/null | grep -q LISTEN; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 Ltm 服务..." >> "$LOG_FILE"
        bash "$SCRIPT_DIR/start_LTM.sh" >> "$LOG_FILE" 2>&1
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ltm 服务已在运行" >> "$LOG_FILE"
    fi
fi
