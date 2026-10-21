"""网络环境兜底：本机服务（LM Studio / oMLX / JEV / TTS 等）的请求绝不能走系统代理。

背景（2026-10-13 实测）：macOS 系统代理（如 Clash 127.0.0.1:7897）开启时，
httpx/openai 会读系统代理设置，且**不看系统的「绕过列表」**；只有环境变量 NO_PROXY
才能让 localhost 直连。开发机 shell 里有 NO_PROXY，但 launchd 拉起的服务没有 →
所有对 localhost 的 openai/httpx 请求被送进代理，代理回 502，表现为
「mem0 抽取/检索 502」「模型明明在线却一直失败」。
"""
import os

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def ensure_local_no_proxy() -> str:
    """把本机地址并入 NO_PROXY / no_proxy（保留已有值，幂等）。返回最终值。"""
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    items = [x.strip() for x in existing.split(",") if x.strip()]
    for host in _LOCAL_HOSTS:
        if host not in items:
            items.append(host)
    value = ",".join(items)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value
    return value
