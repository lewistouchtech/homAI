#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数字人大脑代理端点（阶段2，2026-09-18）
- POST /v1/chat/completions：OpenAI 兼容流式端点，内部调用当前 selected LLM
  （websocket_server 的 _llm 实例，即 ESP32/看板同一大脑）
- "一次流式生成，两处消费"：完整回复同时写入 Ltm 记忆
- 断连=打断：客户端断开即停止生成，半截回复不再入库（对齐方案第 2.3 节）
"""
import asyncio
import json
import re
import threading
import time
import uuid
from collections import OrderedDict

from aiohttp import web

from core.api.config_api import get_websocket_server_instance
from config.logger import setup_logging

TAG = "llm_proxy"
logger = setup_logging()  # 2026-09-22 补日志：改用项目 loguru（原标准 logging INFO 不落盘，链路成黑盒）


def _content_text(msg: dict) -> str:
    """OpenAI message.content 兼容 string 与 [{type:text,text:...}]"""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _sse_chunk(chat_id: str, model: str, token: str) -> str:
    payload = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _save_to_memory(memory, user_text: str, assistant_text: str, session_id: str):
    """同一份流的第二处消费：写 Ltm 记忆（provider 兼容 dict）"""
    if memory is None or not assistant_text.strip():
        return
    try:
        await memory.save_memory(
            [{"role": "user", "content": user_text},
             {"role": "assistant", "content": assistant_text}],
            session_id,
        )
    except Exception as e:
        logger.bind(tag=TAG).warning(f"记忆保存失败: {e}")


def _save_dialogue_history_dh(user_text: str, assistant_text: str, session_id: str):
    """③对话归档（2026-09-22）：数字人对话写入 devices.db dialogue_history（与主链路同表）。

    device_id 列记数字人来源（session_id），使后台对话历史可查询到数字人链路；
    独立函数不依赖 conn 对象（llm_proxy 侧无真实连接）。"""
    import sqlite3
    from pathlib import Path
    db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
    conn_db = None
    try:
        conn_db = sqlite3.connect(db_path, timeout=5)
        cursor = conn_db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dialogue_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                session_id TEXT,
                user_text TEXT,
                ai_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute(
            'INSERT INTO dialogue_history (device_id, session_id, user_text, ai_text) VALUES (?, ?, ?, ?)',
            (session_id, session_id, user_text, assistant_text)
        )
        conn_db.commit()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"对话归档失败: {e}")
    finally:
        if conn_db:
            conn_db.close()


# ===== [P0-1 2026-09-22] 数字人记忆查询优化：智能触发 + 100ms 快速路径 + 跨轮复用 =====
# 对齐星球链路（chat_engine）的行为语义：不再每轮无条件硬等，但保留记忆能力。
# 数字人侧本地实现（不抽共享函数、不动星球代码）：
# - 智能触发：文本特征判断（关键词/疑问词/长度/含"我"），非记忆型请求不再发起查询
# - 快速路径：最多等 100ms，超时本轮不注入（对齐星球），不阻塞对话
# - 跨轮复用：shield 保证超时不误杀后台查询（实测 query_memory 可跑 9~10s），
#   同 query 下一轮可直接复用已完成结果；缓存绑定 query 文本校验，
#   杜绝上一轮结果串到不同问题的"串轮/污染"
# 实测依据：OpenTalking 的 LLM 请求不带 user 字段 → session_id 每轮随机生成，
# 故缓存 key 用 query 文本而非 session_id

_MEMORY_FAST_WAIT_S = 0.1   # 快速路径等待上限（对齐星球 chat_engine 的 100ms）
_MEMORY_CACHE_MAX = 8       # 同一时刻保留的后台查询数上限（数字人单用户场景，防膨胀）

# 模块级缓存：query 文本 -> asyncio.Task（保存引用防 GC + 供下轮复用）
_memory_task_cache: OrderedDict[str, asyncio.Task] = OrderedDict()


def _consume_memory_task_exception(task: asyncio.Task) -> None:
    """取出已完成任务的异常，防 'exception was never retrieved' 警告（成功时无副作用）"""
    if not task.cancelled():
        task.exception()


def _should_query_memory_dh(query: str) -> bool:
    """数字人侧记忆查询智能触发（本地实现，语义对齐星球 chat_engine._should_query_memory）。

    与星球的差异：数字人链路无意图分析环节（无 conn.intent_type），跳过 function_call 判断。
    """
    q = (query or "").strip()
    if not q:
        return False

    # 1. 明确触发关键词（优先级最高，不受长度限制）
    trigger_keywords = (
        "上次", "之前", "以前", "你说过", "我记得",
        "推荐", "喜欢", "讨厌", "偏好", "习惯",
        "心情", "感觉", "情绪",
        "我家", "我爸", "我妈", "我老婆", "我老公", "我儿子", "我女儿",
    )
    if any(kw in q for kw in trigger_keywords):
        return True

    # 2. 疑问词（短问句也查，如"你是谁"）
    question_words = ("怎么", "什么", "哪里", "谁", "为什么", "多少", "吗", "呢", "吧", "么", "啥", "咋")
    if any(w in q for w in question_words):
        return True

    # 3. 太短直接不查（指令/问候）
    if len(q) < 4:
        return False

    # 4. 含"我"但非典型指令 -> 可能需个性化
    command_verbs = ("开", "关", "放", "播", "查", "搜", "设置", "调", "打开", "关闭", "启动", "停止", "连接", "断开")
    if "我" in q and not any(cmd in q for cmd in command_verbs):
        return True

    # 5. 其他默认不查
    return False


def _remember_memory_task(query: str, task: asyncio.Task) -> None:
    """存入缓存（保存引用防 GC）+ 淘汰超限旧条目"""
    _memory_task_cache[query] = task
    _memory_task_cache.move_to_end(query)
    while len(_memory_task_cache) > _MEMORY_CACHE_MAX:
        _memory_task_cache.popitem(last=False)


async def _query_memory_fast(memory, last_user: str) -> str:
    """数字人记忆查询快速路径：智能触发 + 最多 100ms 等待 + 跨轮复用。

    永不抛异常、最多阻塞 100ms；后台查询超时后继续跑完存缓存，供下一轮同 query 复用。
    返回空串 = 本轮不注入记忆（与星球"超时跳过注入"同语义）。
    """
    try:
        q = (last_user or "").strip()
        if memory is None or not q:
            return ""

        # ① 复用缓存中同 query 的后台查询（未完成→等 100ms；已完成→零等待直接用）
        cached = _memory_task_cache.get(q)
        if cached is not None:
            if cached.cancelled():
                _memory_task_cache.pop(q, None)
            else:
                try:
                    mem = await asyncio.wait_for(asyncio.shield(cached), timeout=_MEMORY_FAST_WAIT_S)
                    logger.bind(tag=TAG).info(f"[LLM代理][记忆] 复用后台查询结果（{len(str(mem or ''))} 字）")
                    return str(mem or "").strip()
                except asyncio.TimeoutError:
                    logger.bind(tag=TAG).info("[LLM代理][记忆] 后台查询未完成，本轮跳过注入（继续后台跑，下轮可复用）")
                    return ""
                except Exception as e:
                    logger.bind(tag=TAG).debug(f"[LLM代理][记忆] 复用查询失败，移除缓存: {e}")
                    _memory_task_cache.pop(q, None)
                    return ""

        # ② 智能触发：非记忆型请求不再发起查询（消除每轮固定白等）
        if not _should_query_memory_dh(q):
            logger.bind(tag=TAG).info("[LLM代理][记忆] 智能触发判定：非记忆型请求，跳过查询")
            return ""

        # ③ 发起后台查询（不取消、跑完留缓存）+ 本轮 100ms 快速路径
        task = asyncio.create_task(memory.query_memory(q))
        task.add_done_callback(_consume_memory_task_exception)
        _remember_memory_task(q, task)
        try:
            mem = await asyncio.wait_for(asyncio.shield(task), timeout=_MEMORY_FAST_WAIT_S)
            logger.bind(tag=TAG).info(f"[LLM代理][记忆] 查询 100ms 内完成，注入（{len(str(mem or ''))} 字）")
            return str(mem or "").strip()
        except asyncio.TimeoutError:
            logger.bind(tag=TAG).info("[LLM代理][记忆] 查询未在 100ms 内完成，本轮跳过注入（后台继续，下轮同句可复用）")
            return ""
        except Exception as e:
            logger.bind(tag=TAG).debug(f"[LLM代理][记忆] 查询失败: {e}")
            return ""
    except Exception as e:
        logger.bind(tag=TAG).warning(f"[LLM代理][记忆] 查询链路异常（跳过注入）: {e}")
        return ""


async def _enhance_dialogue_context(ws, memory, dialogue: list, last_user: str) -> list:
    """[HomeBrain 2026-09-22] 数字人链路上下文增强（对齐星球/ESP32 链路）。

    星球链路的 system prompt 含：伊娃人设、当前时间/日期/农历、位置/天气、记忆、声纹档案。
    数字人链路此前纯透传（OpenTalking 默认"友好的数字人助手"），导致问时间靠模型幻觉。
    本函数补齐：①伊娃人设（config.prompt）②当前时间/日期/农历 ③长期记忆；
    位置/天气（依赖设备 IP）与声纹档案（数字人无声纹）不适用，跳过。
    原 system（OpenTalking 的 format_guard 输出约束）保留拼接在末尾。
    """
    originals = [d["content"] for d in dialogue if d["role"] == "system"]

    parts = []

    # ① 伊娃人设（config.yaml 的 prompt，与星球链路同源）
    persona = ""
    try:
        from config.config_loader import load_config
        persona = str((load_config() or {}).get("prompt") or "").strip()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"人设读取失败: {e}")

    # ② 当前时间/日期/农历（修复"问时间靠幻觉"）
    current_time = today_date = today_weekday = lunar_date = ""
    try:
        from core.utils.current_time import get_current_time_info
        current_time, today_date, today_weekday, lunar_date = get_current_time_info()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"时间信息获取失败: {e}")

    if persona:
        parts.append(persona.replace("{{current_time}}", current_time or ""))
    if today_date:
        parts.append(
            f"当前时间：{current_time}\n今天日期：{today_date} ({today_weekday})\n今天农历：{lunar_date}"
        )

    # ③ 长期记忆（Ltm）——[P0-1 2026-09-22] 智能触发 + 100ms 快速路径 + 跨轮复用
    #    （原实现无条件 wait_for 2.0s，每轮白等最多 2 秒；现对齐星球语义：最多等 100ms，
    #     超时本轮不注入，后台查询跑完留缓存供下轮同句复用，详见 _query_memory_fast）
    if memory is not None and last_user:
        try:
            mem = await _query_memory_fast(memory, last_user)
            if mem:
                parts.append(f"<memory>\n{mem}\n</memory>")
        except Exception as e:
            logger.bind(tag=TAG).debug(f"记忆查询跳过: {e}")

    enhanced = "\n\n".join(parts).strip()
    if originals:
        enhanced = (enhanced + "\n\n" + "\n\n".join(originals)).strip()
    if not enhanced:
        return dialogue

    rest = [d for d in dialogue if d["role"] != "system"]
    return [{"role": "system", "content": enhanced}] + rest


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    ws = get_websocket_server_instance()
    llm = getattr(ws, "_llm", None) if ws else None
    memory = getattr(ws, "_memory", None) if ws else None
    if llm is None:
        return web.json_response(
            {"error": {"message": "LLM 未初始化（服务启动中或配置缺失）", "type": "server_error"}},
            status=503,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "请求体必须是 JSON"}}, status=400)

    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    dialogue = [
        {"role": m.get("role"), "content": _content_text(m)}
        for m in messages if m.get("role") in ("system", "user", "assistant") and _content_text(m) != ""
    ]
    if not any(d["role"] == "user" for d in dialogue):
        return web.json_response({"error": {"message": "messages 里没有 user 消息"}}, status=400)

    session_id = str(body.get("user") or f"digital-human-{uuid.uuid4().hex[:8]}")
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model_name = str(body.get("model") or "homebrain-llm")
    last_user = next((d["content"] for d in reversed(dialogue) if d["role"] == "user"), "")
    stream = bool(body.get("stream", True))

    # 2026-09-22 补日志（②数字人链路可观测性）：入口留痕，黑盒变透明
    logger.bind(tag=TAG).info(f"[LLM代理] 收到对话请求 session={session_id} stream={stream} 用户: {last_user[:50]}")

    # 【2026-09-24 主人裁决】多轮对话滚动续期：每收到一轮新用户输入（chat 端点），
    # 把超时窗口往后推 session_timeout 秒。transcribe 端点已经在 863 行续期了，
    # chat 端点再续一次保证"AI 回复后 30 秒内用户开口"也算活跃。
    # 注意：数字人链路用自己的 _dh_wake_state，不能用 wakeword_provider 的 session_active_until（两套状态机不同步）
    if _dh_wake_state.get("active"):
        try:
            from core.utils.wakeword_provider import get_wakeword_detector
            _wp = get_wakeword_detector()
            _timeout = (_wp.session_timeout if _wp else 30) or 30
            _dh_wake_state["until"] = time.time() + _timeout
        except Exception:
            pass

    # [HomeBrain 2026-09-22] 上下文增强（对齐星球链路：伊娃人设/时间/记忆）——修复数字人"时间幻觉"
    try:
        dialogue = await _enhance_dialogue_context(ws, memory, dialogue, last_user)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"上下文增强失败（按原样继续）: {e}")

    # —— 流式（OpenTalking 走这条路）——
    if stream:
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)

        loop = asyncio.get_running_loop()
        stop_flag = threading.Event()  # 断连=打断：produce 侧看到即停止
        func_handler, conn = await _get_toolkit()

        async def _emit(text):
            nonlocal interrupted
            await resp.write(_sse_chunk(chat_id, model_name, text).encode("utf-8"))

        async def _reply_stream():
            """带工具循环的流式文本生成：yield 文本片段（语义对齐 chat_engine：RESPONSE 直接播、REQLLM 回填再生成）"""
            from plugins_func.register import Action
            from core.chat.chat_engine import _merge_tool_calls

            functions = func_handler.get_functions() if func_handler else None
            # 2026-09-28：数字人链路固定走云端大模型，按主人裁决旁路工具筛选
            # （云端模型无上下文限制，全量工具可见反而减少"没有新闻API"这类误判；
            # 原 2026-09-22 的弱模型乱选问题由云端模型能力覆盖）。
            if functions and last_user:
                try:
                    from core.utils.model_stack import tool_filter_active
                    from core.utils.tool_filter import filter_tools_by_query
                    if tool_filter_active(self.config if hasattr(self, "config") else {}):
                        functions = filter_tools_by_query(
                            functions, last_user, session_id or ""
                        )
                    # auto 模式下云端恒为旁路；显式 on 时维持筛选（尊重手动配置）
                    try:
                        _names = [
                            (t.get("function", {}) or {}).get("name") or t.get("name") or "?"
                            for t in functions
                        ]
                    except Exception:
                        _names = []
                    logger.bind(tag=TAG).info(f"[LLM代理] 工具过滤后 {len(functions)} 个: {_names}")
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"工具过滤失败，沿用全量工具: {e}")
            local = list(dialogue)
            for _round in range(8):  # 最多 8 轮工具循环（2026-09-22：3 轮在“无参失败→诊断→带参重试”场景会耗尽；批量删邮件等多步操作需要更多轮次）
                q: asyncio.Queue = asyncio.Queue()

                def _produce():
                    try:
                        if functions:
                            gen = llm.response_with_functions(session_id, local, functions=functions)
                        else:
                            gen = ((t, None) for t in llm.response(session_id, local))
                        for resp in gen:
                            if stop_flag.is_set():
                                return
                            asyncio.run_coroutine_threadsafe(q.put(("t", resp)), loop).result(timeout=5)
                    except Exception as e:
                        asyncio.run_coroutine_threadsafe(q.put(("e", str(e))), loop)
                    finally:
                        asyncio.run_coroutine_threadsafe(q.put(("done", None)), loop)

                threading.Thread(target=_produce, daemon=True, name=f"llm-proxy-{session_id}").start()

                text_acc, tool_calls_list, tool_flag = "", [], False
                while True:
                    kind, payload = await q.get()
                    if kind in ("e", "done"):
                        break
                    if isinstance(payload, dict):
                        content, tc = payload.get("content", ""), payload.get("tool_calls")
                    elif isinstance(payload, (list, tuple)) and len(payload) >= 2:
                        content, tc = payload[0], payload[1]
                    else:
                        content, tc = payload, None
                    if content:
                        if not tool_flag and isinstance(content, str) and content.startswith("<tool_call>"):
                            tool_flag = True  # qwen 风格 <tool_call> 文本：不上屏，走 JSON 提取
                        if not tool_flag:
                            text_acc += content
                            yield ("text", content)
                    if tc:
                        tool_flag = True
                        _merge_tool_calls(tool_calls_list, tc)  # 流式分片按 index 合并（对齐上游）

                if not tool_calls_list:
                    return

                # 2026-09-24：主人裁决删掉这句"我这就去处理"——退下/退出时不该说这句
                # if _round == 0:
                #     yield ("text", "好的，我这就去处理，请稍等。\n")

                local.append({
                    "role": "assistant",
                    "content": text_acc or None,
                    "tool_calls": [
                        {"id": tc["id"], "function": {"name": tc["name"], "arguments": tc.get("arguments") or "{}"}, "type": "function", "index": i}
                        for i, tc in enumerate(tool_calls_list)
                    ],
                })
                need_llm = False
                for tc in tool_calls_list:
                    logger.bind(tag=TAG).info(f"[LLM代理] 执行工具: {tc.get('name')} 参数: {tc.get('arguments')}")
                    try:
                        result = await func_handler.handle_llm_function_call(conn, tc)
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"工具 {tc.get('name')} 执行失败: {e}")
                        result = None
                    if tc.get("name") == "handle_exit_intent":
                        # 2026-09-24：LLM 误触发退出拦截——LLM 会把"早就该休息了"等普通
                        # 话语映射到退出工具（00:30:48 实证：说完即待机，主人感受"说没两
                        # 个字就待机"）。退出必须以前置退出词表为准：用户文本不命中退出
                        # 命令 → 忽略该工具调用，提示 LLM 继续正常应答。
                        _lu = ""
                        for _m in reversed(dialogue):
                            if _m.get("role") == "user":
                                _c = _m.get("content")
                                _lu = _c if isinstance(_c, str) else ""
                                break
                        _real_exit = False
                        try:
                            from core.utils.voiceprint_gate import _is_exit_command as _check_exit
                            _real_exit = bool(_lu) and _check_exit(conn, _lu)
                        except Exception:
                            _real_exit = False
                        if not _real_exit:
                            logger.bind(tag=TAG).info(
                                f"[唤醒词] LLM 退出工具被拦截(用户文本非退出命令，LLM 误判): {_lu[:30]}"
                            )
                            local.append({"role": "tool", "tool_call_id": tc.get("id") or str(uuid.uuid4()),
                                          "content": "用户没有要求退出或结束对话，请继续正常回答用户刚才的问题。"})
                            need_llm = True
                            continue
                        # 2026-09-23 晚：LLM 路径触发退出（"我说停止"曾被误判退出实证）时
                        # 同步唤醒状态机为待机，否则状态机仍认为会话激活，与星球退出后待机不一致
                        _dh_wake_state["active"] = False
                        _dh_wake_state["until"] = 0.0
                        logger.bind(tag=TAG).info("[唤醒词] LLM 退出工具执行，唤醒会话已置为待机")
                    action = getattr(result, "action", None)
                    reply = (getattr(result, "response", "") or getattr(result, "result", "") or "") if result else ""
                    if action in (Action.RESPONSE, Action.NOTFOUND, Action.ERROR):
                        if reply:
                            yield ("text", reply)   # 工具自带答复：直接播（对齐上游语义）
                    else:
                        local.append({"role": "tool", "tool_call_id": tc.get("id") or str(uuid.uuid4()), "content": getattr(result, "result", "") or ""})
                        need_llm = True
                if not need_llm:
                    return
            # 轮次耗尽自然结束

        full = []
        interrupted = False
        try:
            async for piece in _reply_stream():
                if piece[0] == "text":
                    full.append(piece[1])
                    await _emit(piece[1])
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            interrupted = True  # 客户端断连 = 打断
        finally:
            stop_flag.set()
            text = "".join(full)
            try:
                if not interrupted:
                    tail = {
                        "id": chat_id, "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    await resp.write(f"data: {json.dumps(tail)}\n\ndata: [DONE]\n\n".encode("utf-8"))
                await resp.write_eof()
            except Exception:
                interrupted = True
            if not interrupted:
                # 完整回复才入库；被打断的半截不入（对齐上游 abort 语义：丢弃半截）
                logger.bind(tag=TAG).info(f"[LLM代理] 回复完成（{len(text)} 字）session={session_id}")
                _extend_wake_after_reply(len(text))
                await _save_to_memory(memory, last_user, text, session_id)
                # 2026-09-22 ③对话归档：数字人对话写入 devices.db（此前完全不入库）
                await asyncio.to_thread(_save_dialogue_history_dh, last_user, text, session_id)
        return resp

    # —— 非流式兜底 ——
    full = []
    try:
        for token in llm.response(session_id, dialogue):
            if token:
                full.append(token)
    except Exception as e:
        return web.json_response({"error": {"message": f"LLM 调用失败: {e}"}}, status=502)
    text = "".join(full)
    logger.bind(tag=TAG).info(f"[LLM代理] 回复完成（非流式，{len(text)} 字）session={session_id}")
    _extend_wake_after_reply(len(text))
    await _save_to_memory(memory, last_user, text, session_id)
    await asyncio.to_thread(_save_dialogue_history_dh, last_user, text, session_id)
    return web.json_response({
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


def create_llm_proxy_routes() -> web.RouteTableDef:
    routes = web.RouteTableDef()
    routes.post("/v1/chat/completions")(handle_chat_completions)
    routes.post("/v1/audio/speech")(handle_audio_speech)
    routes.post("/v1/audio/transcriptions")(handle_audio_transcriptions)
    return routes


_toolkit_cache = {}  # {handler: conn}——数字人专用工具处理器（独立于 ESP32 连接）


async def _wait_toolkit_init(task):
    """等待工具处理器初始化完成（MCP 工具在 _initialize 里注册，apple-mail 连接约 1 秒）。
    超时不取消原 task（shield）：初始化继续在后台完成，后续请求可用完整工具表。"""
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=20)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"等待工具初始化未完成（沿用已注册工具）: {e}")


async def _get_toolkit():
    """数字人的工具处理器。优先复用活跃连接的 func_handler（HA 状态共享）；
    无活跃连接时构造独立处理器（stub conn：server 端插件可用，设备端 IoT/MCP 工具不适用）。
    返回 (func_handler, conn)；彻底失败返回 (None, None) 退化为纯 LLM。

    2026-09-22 修复竞态：此前 create_task(_initialize) 不等待就返回，_reply_stream 立刻
    get_functions()，而 MCP 工具（apple-mail）约 1 秒后才注册完成——服务重启后首个数字人
    请求拿到的工具表只剩核心 3 个，弱模型无邮件工具可选而误调 handle_exit_intent。
    现在改为 await 初始化完成（20 秒上限，超时沿用已注册工具）。"""
    from types import SimpleNamespace
    ws = get_websocket_server_instance()
    for sock in list(getattr(ws, "active_connections", None) or []):
        conn = getattr(sock, "_homebrain_handler", None)
        if conn is not None and getattr(conn, "func_handler", None) is not None:
            return conn.func_handler, conn
    # —— 独立构造（缓存）——
    if "handler" in _toolkit_cache:
        h = _toolkit_cache["handler"]
        # 并发请求共享同一次初始化等待（首请求初始化未完成时，后续请求也在此等）
        await _wait_toolkit_init(_toolkit_cache.get("init_task"))
        return (h, _toolkit_cache["conn"]) if h else (None, None)
    try:
        from core.providers.tools.unified_tool_handler import UnifiedToolHandler
        stub = SimpleNamespace(
            config=ws.config,
            logger=setup_logging(),
            iot_descriptors={},      # 数字人无绑定设备：设备端 IoT/MCP 工具不注册
            websocket=None,
            device_id=None,
            client_ip="digital-human",
            # 退出词表（与星球 conn.cmd_exit 同源）：_is_exit_command 从这里取词，
            # 供 LLM 退出工具误判校验/声纹闸门退出白名单使用（2026-09-24）
            cmd_exit=list(ws.config.get("exit_commands", []) or []),
        )
        handler = UnifiedToolHandler(stub)
        stub.func_handler = handler
        _toolkit_cache["handler"] = handler
        _toolkit_cache["conn"] = stub
        init_task = asyncio.ensure_future(handler._initialize())
        _toolkit_cache["init_task"] = init_task
        await _wait_toolkit_init(init_task)
        logger.bind(tag=TAG).info("工具桥接：已为数字人构造独立工具处理器")
        return handler, stub
    except Exception as e:
        logger.bind(tag=TAG).error(f"独立工具处理器构造失败: {e}")
        _toolkit_cache["handler"] = None
        return None, None


_tts_lock = asyncio.Lock()  # TTS provider 实例未必并发安全，串行化合成
_tts_cache = {}             # {provider_name: 实例}——数字人 TTS 跟随后台 selected_module.TTS


def _get_tts():
    """数字人 TTS 跟随后台设置：selected 是 MossNano 就用 MossNano，选豆包/其他就用那个。
    （架构定案：数字人窗口模式由数字人管线收听+播报，TTS 引擎跟随后台配置。）"""
    ws = get_websocket_server_instance()
    selected = ws.config["selected_module"]["TTS"]
    inst = _tts_cache.get(selected)
    if inst is None:
        from core.utils.modules_initialize import initialize_tts
        inst = initialize_tts(ws.config)   # 与主链路同一初始化函数（含 devices.db 合并）
        _tts_cache[selected] = inst
    return inst


async def handle_audio_speech(request: web.Request) -> web.Response:
    """OpenAI 兼容 /v1/audio/speech（阶段3）：文本 → 当前 selected TTS → wav bytes。
    数字人音色 = homebrain 当前 TTS（与 ESP32 听到的一致）。"""
    try:
        tts = _get_tts()
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"TTS 初始化失败: {e}"}}, status=503
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "请求体必须是 JSON"}}, status=400)
    text = str(body.get("input") or "").strip()
    if not text:
        return web.json_response({"error": {"message": "input 为空"}}, status=400)
    if len(text) > 1000:
        return web.json_response({"error": {"message": "文本过长（≤1000 字）"}}, status=400)

    async with _tts_lock:
        try:
            audio = await tts.text_to_speak(text, None)
        except Exception as e:
            return web.json_response(
                {"error": {"message": f"TTS 合成失败: {e}"}}, status=502
            )
    if not audio:
        return web.json_response({"error": {"message": "TTS 返回空音频"}}, status=502)
    logger.bind(tag=TAG).info(f"[LLM代理] TTS 合成完成（{len(text)} 字 → {len(audio)} 字节）")
    return web.Response(body=audio, content_type="audio/wav")


# ============ /v1/audio/transcriptions（阶段2.5：数字人语音输入走 homebrain 本地 ASR） ============

_asr_instance = None
_asr_lock = asyncio.Lock()


def _get_asr():
    """homebrain 本地 ASR（sherpa SenseVoice）缓存实例"""
    global _asr_instance
    if _asr_instance is None:
        from core.utils.modules_initialize import initialize_asr
        ws = get_websocket_server_instance()
        _asr_instance = initialize_asr(ws.config)
    return _asr_instance


# ---- 声纹全线闸门（数字人链路，2026-09-23）----
_voiceprint_instance = None


def _get_voiceprint():
    """声纹 provider 缓存实例（与星球 initializer 同构造；模型类级缓存，重复构造廉价）。"""
    global _voiceprint_instance
    if _voiceprint_instance is None:
        from core.utils.voiceprint_provider import VoiceprintProvider
        cfg = _get_ws_config().get("voiceprint") or {}
        _voiceprint_instance = VoiceprintProvider(cfg)
    return _voiceprint_instance


def _get_ws_config() -> dict:
    ws = get_websocket_server_instance()
    return ws.config if ws is not None else {}


# 数字人链路退出命令白名单（与星球 cmd_exit 语义一致：退出/再见绝不被闸门吞掉）
_CMD_EXIT = ("退出", "再见", "拜拜", "退下", "结束对话", "不聊了", "下次见")

# 数字人唤醒词会话状态（2026-09-23，主人裁决"数字人也加唤醒词，且加声纹识别"）。
# 数字人链路无 per-conn 状态（OpenTalking 每轮 session_id 随机），用进程内全局状态：
# 未激活时仅唤醒词（且声纹=家庭成员，上方闸门已保证）可激活；激活后 session_timeout 秒内
# 免唤醒（每轮刷新），超时回落未激活。语义对齐星球 idle_guard 会话。
_dh_wake_state = {"active": False, "until": 0.0}


def _detect_mode_switch(text: str):
    """语音切换中央形象（2026-09-23，主人批准；2026-09-24 起与星球链路共用
    center_window_api.detect_mode_switch 同一实现，含否定语境排除）。"""
    from core.api.center_window_api import detect_mode_switch
    return detect_mode_switch(text)


def _extend_wake_after_reply(text_len: int):
    """长回复（故事/新闻）播放期间用户只是在听，transcribe 无请求刷新会让唤醒态过期，
    用户下一句会被当未激活丢弃（主人反馈"聊到一半突然退出"的一类成因）。
    回复流结束时按文本量预估播放时长（中文播报约4.5字/秒），把唤醒有效期延长到
    播完（上限120s）；正常多轮会话仍由每轮 transcribe 刷新维持。"""
    try:
        if not _dh_wake_state["active"]:
            return
        from core.utils.wakeword_provider import get_wakeword_detector
        _wp = get_wakeword_detector()
        _base = (_wp.session_timeout if _wp else 30) or 30
        _est = min(120, int(text_len / 4.5)) if text_len > 0 else 0
        _until = time.time() + _base + _est
        if _until > _dh_wake_state["until"]:
            _dh_wake_state["until"] = _until
    except Exception:
        pass

# 打断意图白名单已统一收敛到 core/utils/voiceprint_gate.is_bargein_command
# （config voiceprint_gate.bargein_words，星球/数字人两条链路同源；去标点后
# 多字词包含匹配、单字精确匹配、短句限长8字）。命中后响应带 bargein=true，
# 前端改调 /interrupt（打断不应答，对齐星球语义）。


def _transcribe_file_sync(asr, wav_path: str) -> str:
    """线程池里跑：读 wav → sherpa 识别。"""
    samples, _sr = asr.read_wave(wav_path)
    if hasattr(asr, "_recognize_samples"):
        return asr._recognize_samples(samples.tolist() if hasattr(samples, "tolist") else samples)
    return ""


async def handle_audio_transcriptions(request: web.Request) -> web.Response:
    """OpenAI 兼容 /v1/audio/transcriptions：multipart 上传音频 → 文本。
    OpenTalking 的 audio_transcriptions 协议会上传 wav；其他格式先经 ffmpeg 转 16k mono wav。"""
    import subprocess
    import tempfile
    from pathlib import Path

    try:
        post = await request.post()
    except Exception:
        return web.json_response({"error": {"message": "请求必须是 multipart/form-data"}}, status=400)
    file = post.get("file")
    if file is None or isinstance(file, str):
        return web.json_response({"error": {"message": "缺少 file 字段"}}, status=400)

    # 2026-09-23 数字人 ducking 已按主人要求取消（触发源为"任何上传"，回声碎片
    # 也会压音量导致播报突变；系统级 ducking 开关 system_duck 也已关闭）。
    # 原 maybe_lower_volume + schedule_restore 调用已移除。

    try:
        asr = _get_asr()
    except Exception as e:
        return web.json_response({"error": {"message": f"ASR 初始化失败: {e}"}}, status=503)

    tmp_dir = Path(tempfile.mkdtemp(prefix="dh-asr-"))
    try:
        raw = file.file.read()
        src = tmp_dir / (file.filename or "speech.wav")
        src.write_bytes(raw)
        wav = tmp_dir / "in16k.wav"
        # 统一转 16k mono wav（sherpa 要求）；webm/mp3/m4a 也一并覆盖
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(wav),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not wav.exists() or wav.stat().st_size < 44:
            return web.json_response(
                {"error": {"message": f"音频解码失败: {err.decode(errors='ignore')[:200]}"}}, status=400
            )
        async with _asr_lock:
            text = await asyncio.to_thread(_transcribe_file_sync, asr, str(wav))
        text = (text or "").strip()
        speaker = None

        # 声纹闸门的 stub conn：唤醒词状态机（打断词表读取）与声纹闸门共用
        _conn_stub_for_gate = type("_G", (), {"config": _get_ws_config(), "voiceprint_provider": _get_voiceprint(), "cmd_exit": _CMD_EXIT})()

        # ===== 0. 退出命令优先检测（2026-09-23，主人裁决：退出=数字人待机，不是切星球）=====
        # 顺序对齐星球 intentHandler.check_direct_exit（在 LLM/意图/插件最前端拦截）：
        # 命中 → 停用会话（待机），不应答不播告别语（"我这就去处理"两遍问题的根因修复——
        # 旧路径下"退下吧"进了 LLM 走 handle_exit_intent 工具循环，第 0 轮先播固定衔接语）。
        # 词表与星球同源：config exit_commands，宽松匹配 text==cmd or startswith(cmd)。
        try:
            from core.utils.util import remove_punctuation_and_length
            _, _exit_text = remove_punctuation_and_length(text)
            _exit_cmds = _get_ws_config().get("exit_commands", []) or []
            for _cmd in _exit_cmds:
                if _exit_text == _cmd or _exit_text.startswith(_cmd):
                    _dh_wake_state["active"] = False
                    _dh_wake_state["until"] = 0.0
                    logger.bind(tag=TAG).info(f"[退出] 数字人识别到退出命令，进入待机: {text[:30]}")
                    return web.json_response({
                        "text": "", "speaker": None, "wake_active": False,
                        "bargein": False, "exit": True,
                        "display_text": text,   # 前端字幕显示用（text 必须为空防二次送 LLM）
                    })
        except Exception as _exit_err:
            logger.bind(tag=TAG).warning(f"[退出] 数字人退出判定异常，旁路放行: {_exit_err}")

        # ===== 0.5 语音切换中央形象（2026-09-23 主人批准；2026-09-24 前移 + 修正）=====
        # 前移原因：旧位置在唤醒状态机之后，待机状态说切换指令会被当"非唤醒词"丢弃，
        # 永远到不了切换判定（主人实测"切换到数字星球"无响应）。
        # 修正：响应 text 必须为空——旧实现带原文 text，旧版前端不认识 switch 字段会
        # 把原文送 LLM，被 change_role 插件抢答"不支持的角色"（主人实测 3）。
        # 不送 LLM（否则 LLM 会口头应答）。
        _switch = _detect_mode_switch(text)
        if _switch:
            logger.bind(tag=TAG).info(f"[切换形象] 命中切换指令: {text[:30]} → mode={_switch}")
            return web.json_response({
                "text": "", "speaker": None,
                "wake_active": _dh_wake_state["active"], "bargein": False,
                "switch": _switch,
                "display_text": text,   # 前端字幕显示用（text 必须为空防 change_role 抢答）
            })

        # ===== 1. 唤醒词状态机（2026-09-23，主人裁决"数字人也加唤醒词"）=====
        # 顺序与星球 base.py 的"唤醒词优先通道"一致：未激活会话先判唤醒词，
        # 再做声纹校验。若把声纹闸门排在前（旧结构），1 秒左右的短唤醒句
        # 声纹分数天然偏低（<0.25）会被闸门静默吞掉，永远到不了唤醒判定——
        # 这正是"数字人唤醒词不工作"的根因之一（18:47 起多轮实测无"你好"转写）。
        # 未激活：仅唤醒词可激活（命中后仍需下方声纹校验：非家庭成员喊唤醒词
        #         不激活）；打断意图白名单放行；其余丢弃。
        # 已激活：刷新会话有效期；超时回落未激活。唤醒词总开关关 → 保持旧行为全放行。
        _wake_hit = False
        _bargein = False
        try:
            from core.utils.wakeword_provider import get_wakeword_detector
            from core.utils.voiceprint_gate import is_bargein_command
            _wp = get_wakeword_detector()
            if _wp and _wp.enabled:
                _now = time.time()
                if (not _dh_wake_state["active"]) or _now > _dh_wake_state["until"]:
                    _dh_wake_state["active"] = False
                    if text and _wp.detect(text)[0]:
                        _wake_hit = True  # 声纹校验在下方声纹闸门处统一做
                    elif text and is_bargein_command(_conn_stub_for_gate, text):
                        # 打断意图：任何态命中都放行文本+标 bargein，前端改调
                        # /interrupt 打断当前播报且不应答（对齐星球，A+B 方案）
                        _bargein = True
                        logger.bind(tag=TAG).info(f"[唤醒词] 数字人未激活但命中打断意图，放行: {text[:30]}")
                    elif text:
                        logger.bind(tag=TAG).info(
                            f"[唤醒词] 数字人未激活，丢弃非唤醒词文本: {text[:30]}"
                        )
                        return web.json_response({"text": "", "wake_active": False, "bargein": False})
                else:
                    _dh_wake_state["until"] = _now + _wp.session_timeout
                    if text and is_bargein_command(_conn_stub_for_gate, text):
                        _bargein = True  # 已激活态：打断同样不应答（对齐星球）
        except Exception as _wake_err:
            logger.bind(tag=TAG).warning(f"[唤醒词] 数字人唤醒判定异常，旁路放行: {_wake_err}")

        # ===== 2. 声纹全线闸门（2026-09-23，主人裁决）：非家庭成员不听不答 =====
        # 与星球完全同函数同口径（should_gate_voiceprint，含 last_score≥0.25 兜底、
        # 退出命令白名单、总开关旁路）。唤醒词命中的段在此做声纹校验：
        # 校验通过 → 激活；不通过 → 丢弃（非家庭成员喊唤醒词不激活）。
        try:
            from core.utils.voiceprint_gate import gate_enabled, _is_exit_command, should_gate_voiceprint
            vp = _get_voiceprint()
            _conn_stub = _conn_stub_for_gate
            if gate_enabled(_conn_stub) and text and not _is_exit_command(_conn_stub, text):
                # 2026-09-24：已激活会话的中文段（≥2 个中文字，与前端 meaningful 门槛一致）
                # 免声纹直接放行——唤醒时已用声纹验证过家庭成员，30s 窗口内延续信任。
                # 背景：短句声纹分数天然偏低（实测"今天。"仅 0.18 < 0.25 被误杀），主人
                # 连续对话中频繁被吞（"说话听不见"）。英文/碎片仍走声纹（防 YouTube 回声）。
                _zh_n = len(re.findall(r'[一-鿿]', text))
                if _dh_wake_state["active"] and _zh_n >= 2:
                    logger.bind(tag=TAG).info(
                        f"[声纹闸门] 已激活会话中文段免声纹放行(唤醒时已验证): {text[:30]}"
                    )
                else:
                    wav_bytes = wav.read_bytes()
                    speaker = await vp.identify_speaker(wav_bytes)
                    _score = getattr(vp, "last_score", None)
                    if should_gate_voiceprint(_conn_stub, speaker, text):
                        logger.bind(tag=TAG).info(
                            f"[声纹闸门] 数字人链路丢弃非家庭成员声音"
                            f"(speaker={speaker}, score={_score}): {text[:30]}"
                        )
                        return web.json_response({"text": "", "wake_active": _dh_wake_state["active"]})
                    if _wake_hit:
                        _dh_wake_state["active"] = True
                        _dh_wake_state["until"] = time.time() + _wp.session_timeout
                        logger.bind(tag=TAG).info(
                            f"[唤醒词] 数字人唤醒命中且声纹校验通过，激活会话({_wp.session_timeout}s): {text[:30]}"
                        )
            elif _wake_hit:
                # 声纹不可用/总开关关：无校验手段，唤醒词命中直接激活（保持可用性）
                _dh_wake_state["active"] = True
                _dh_wake_state["until"] = time.time() + _wp.session_timeout
                logger.bind(tag=TAG).info(
                    f"[唤醒词] 数字人唤醒命中（声纹闸门旁路），激活会话: {text[:30]}"
                )
        except Exception as _gate_err:
            logger.bind(tag=TAG).warning(f"[声纹闸门] 数字人链路判定异常，旁路放行: {_gate_err}")

        logger.bind(tag=TAG).info(f"[LLM代理] ASR 识别: {text[:40]}")
        return web.json_response({
            "text": text, "speaker": speaker,
            "wake_active": _dh_wake_state["active"], "bargein": _bargein,
        })
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
