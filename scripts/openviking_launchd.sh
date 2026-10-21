#!/bin/bash
# Ltm 的 launchd 包装脚本：清掉过期 pid 后前台运行服务。
# launchd 负责 KeepAlive，因此这里必须 exec，让服务成为 launchd 直接管理的进程。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

OV_CONF="data/LTM/ov.conf"
if [ ! -f "$OV_CONF" ]; then
    echo "错误: 找不到 $OV_CONF" >&2
    exit 1
fi

WORKSPACE="$("$SCRIPT_DIR/venv/bin/python" -c "import json; print(json.load(open('$OV_CONF')).get('storage', {}).get('workspace', ''))")"
if [ -z "$WORKSPACE" ]; then
    echo "错误: ov.conf 中 storage.workspace 未配置" >&2
    exit 1
fi

mkdir -p "$WORKSPACE" "$SCRIPT_DIR/tmp"

PID_FILE="$WORKSPACE/.LTM.pid"
if [ -f "$PID_FILE" ]; then
    STALE_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$STALE_PID" ] && ! ps -p "$STALE_PID" >/dev/null 2>&1; then
        echo "清理过期 pid 文件: $PID_FILE (PID=$STALE_PID 已不在)"
        rm -f "$PID_FILE"
    fi
fi

exec "$SCRIPT_DIR/venv/bin/LTM-server" --config "$OV_CONF"
