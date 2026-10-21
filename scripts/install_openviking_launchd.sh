#!/bin/bash
# 安装 OpenViking 的 macOS LaunchAgent。
# 用法：bash scripts/install_openviking_launchd.sh

set -euo pipefail

LABEL="com.homebrain.openviking"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$SCRIPT_DIR/tmp"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST_DST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DIR/scripts/openviking_launchd.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/openviking_launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/openviking_launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST_DST"

# 已加载时先 bootout，避免 bootstrap 冲突。
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo "已安装并加载 $LABEL"
echo "日志: $LOG_DIR/openviking_launchd.out.log / $LOG_DIR/openviking_launchd.err.log"
