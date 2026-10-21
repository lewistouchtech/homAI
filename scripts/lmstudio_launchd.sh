#!/bin/bash
# LM Studio 本地 API 服务（默认端口 1234）的 launchd 包装脚本。
#
# 为什么需要它：ltm 的记忆抽取走 LM Studio 的 OpenAI 兼容端点
# （data/.config.yaml 的 Memory.ltm.llm_base_url）。服务不在线 = 长期记忆抽不出东西，
# 而 lms server start 是手动敲的，重启机器就没了。
#
# 为什么不能直接 exec：`lms server start` 没有 --foreground 选项，它会自己 daemonize
# 并立刻返回。若直接放进 ProgramArguments，launchd 认为进程已退出 → 按 KeepAlive 疯狂重启。
# 所以这里启动后阻塞等待服务退出，退出即结束本进程，交给 launchd 重新拉起。

set -uo pipefail

LMS="$HOME/.lmstudio/bin/lms"
PORT=1234

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$SERVER_DIR/tmp"

if [ ! -x "$LMS" ]; then
    echo "错误: 找不到 $LMS（LM Studio 未安装？）" >&2
    exit 1
fi

server_running() {
    # ⚠️ 必须用 TCP 探测，不要解析 `lms server status` 的文本：
    # ① 它的输出走 **stderr**——用 2>/dev/null 会把唯一线索丢掉，判断永远为假 → 死循环重启
    # ② 文案可能随版本变化，不可靠
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
        exec 3<&- 3>&-
        return 0
    fi
    return 1
}

# 收到 launchd 的停止信号时把 daemon 一并收掉，避免留下孤儿占着端口
cleanup() {
    echo "收到停止信号，关闭 LM Studio 服务"
    "$LMS" server stop >/dev/null 2>&1
    exit 0
}
trap cleanup TERM INT

if server_running; then
    echo "LM Studio 服务已在运行（端口 $PORT），直接进入守护"
else
    echo "启动 LM Studio 服务（端口 $PORT）..."
    if ! "$LMS" server start -p "$PORT"; then
        echo "错误: LM Studio 服务启动失败，5 秒后重试" >&2
        sleep 5
        "$LMS" server start -p "$PORT" || exit 1
    fi
    # 等它真正监听上，否则 HomeBrain 可能在端口就绪前就来探活
    for _ in $(seq 1 30); do
        server_running && break
        sleep 1
    done
    server_running || { echo "错误: 等待 30 秒后服务仍未就绪" >&2; exit 1; }
    echo "LM Studio 服务已就绪"
fi

# 模型不用预加载：实测 LM Studio 支持按需自动装载（JIT），
# 收到请求才把 GGUF 装进内存，空闲约 1 小时后自动卸载，不白占 5GB。

while server_running; do
    sleep 20
done

echo "LM Studio 服务已退出，本进程随之结束，由 launchd 重新拉起"
