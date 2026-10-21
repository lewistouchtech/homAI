# ⑨双重触发去重单测：验证 finish_streaming 1.5s 内相同文本只触发一次 startToChat
import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from core.providers.asr import fun_local
from core.providers.asr.base import ASRProviderBase

KEY = "test-dedup-001"


async def fake_starttochat(conn, text):
    conn.starttochat_calls.append(text)
    return True


async def fake_generate(input, cache, is_final, chunk_size, **kw):
    return []


async def main():
    # 跳过 __init__（避免加载真模型），手工组装最小可用实例
    asr = object.__new__(fun_local.ASRProvider)
    asr.SESSIONS = {}
    asr.model = SimpleNamespace(generate=fake_generate)
    asr.bind = lambda t, fn: fn

    asr._extract_text_from_result = lambda result: ""
    asr._is_meaningful_text = lambda text: len(text.replace(" ", "")) > 1

    conn = SimpleNamespace(
        idle_guard=True, _session_active=True,  # 守卫开启且会话已激活，全部放行
        starttochat_calls=[],
    )

    # 绑定基类方法（免唤醒守卫）
    asr._idle_guard_allow = ASRProviderBase._idle_guard_allow.__get__(asr)

    # patch startToChat
    fun_local.startToChat = fake_starttochat

    # 第一次 finish_streaming（VAD 停止触发）
    asr.SESSIONS[KEY] = SimpleNamespace(
        accumulated_text="今天天气怎么样", pcm_buffer=[], asr_cache={}, chunk_count=0, last_seen=0
    )
    await asr.finish_streaming(KEY, conn)
    assert len(conn.starttochat_calls) == 1, f"第1次应触发，实际 {conn.starttochat_calls}"
    print("[1] 第一次 finish_streaming → startToChat 触发 1 次 ✓")

    # 模拟常开推流重建 session（双发根因关键步骤）
    asr.SESSIONS[KEY] = SimpleNamespace(
        accumulated_text="今天天气怎么样", pcm_buffer=[], asr_cache={}, chunk_count=0, last_seen=0
    )

    # 第二次 finish_streaming（listen stop 消息触发）
    await asr.finish_streaming(KEY, conn)
    assert len(conn.starttochat_calls) == 1, f"第2次应被去重，实际 {conn.starttochat_calls}"
    print("[2] 1.5s 内相同文本第二次 → 被去重拦截 ✓")

    # 超过 1.5s 窗口后再次触发 → 应放行
    conn._last_starttochat_ts = conn._last_starttochat_ts - 2.0
    asr.SESSIONS[KEY] = SimpleNamespace(
        accumulated_text="今天天气怎么样", pcm_buffer=[], asr_cache={}, chunk_count=0, last_seen=0
    )
    await asr.finish_streaming(KEY, conn)
    assert len(conn.starttochat_calls) == 2, f"超窗口应放行，实际 {conn.starttochat_calls}"
    print("[3] 超过 1.5s 窗口 → 正常放行 ✓")

    # 不同文本（正常多轮）→ 应放行
    asr.SESSIONS[KEY] = SimpleNamespace(
        accumulated_text="现在几点了", pcm_buffer=[], asr_cache={}, chunk_count=0, last_seen=0
    )
    await asr.finish_streaming(KEY, conn)
    assert len(conn.starttochat_calls) == 3, f"不同文本应放行，实际 {conn.starttochat_calls}"
    print("[4] 不同文本（正常多轮）→ 正常放行 ✓")

    print("\n===== 去重单测判定: PASS =====")


asyncio.run(main())
