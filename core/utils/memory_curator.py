"""记忆整理（2026-10-13，主人定的方案）：「这段对话很有意义，把它记录下来」。

流程：
  识别请求（规则）→ 取候选对话窗口（硬规则：同设备、相邻间隔≤10分钟、最多20轮）
  → 整理模型一次调用：自己判断这件事从哪一轮开始、写成一条完整的话
    （开头像半截话就再往前扩，最多扩 2 次；**边界由大模型判断，不用 GEV**）
  → 念给主人确认 → 主人说「对」才写：mem0 长期记忆（带标签）+ 原文归档表
  → 主人说「不对，……」则带着纠正重新整理；说别的事则放弃草稿。

第一步只做三类：fact 事实偏好 / plan 约定计划 / howto 步骤经验。
整理模型由后台「记忆整理模型」配置决定（见 memory_curator_config）；失败自动退回 mem0 抽取模型。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.logger import setup_logging

TAG = "memory_curator"
logger = setup_logging()

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

GAP_MINUTES = 10
WINDOW_ROWS = 20
MAX_EXPANSIONS = 2
PENDING_TTL_SECONDS = 120
LLM_TIMEOUT_SECONDS = 60
TURN_CHARS = 200
TYPES = ("fact", "plan", "howto")
TYPE_LABELS = {"fact": "事实偏好", "plan": "约定计划", "howto": "步骤经验"}

# ── 1. 识别「把前面那段记下来」 ─────────────────────────────────────
_ACTION = re.compile(r"(记录|记下|记住|记一下|记着|记牢|存下|存起|保存|整理(一下)?并?(记|存))")
_REF = re.compile(
    r"(这段|这个|这些|这件|这事|这次|刚才|刚刚|前面|上面|上述|方才|我们(刚|才)?聊|咱们(刚|才)?聊|"
    r"那个|那段|它|他们的决定|这样)"
)
_NOTE_WORDS = re.compile(r"(备忘录|笔记|提醒|日历|闹钟|待办|日程|清单)")
_BARE = re.compile(r"^(帮我|请|麻烦)?(把)?(它|这个|这段)?(记录|记)(下来|住|一下|着)?[吧啊哦呀，。！!~\s]*$")


def is_capture_request(text: str) -> bool:
    """是不是「请把前面聊的内容记下来」。自带内容的「记住，我女儿对芒果过敏」不算（走逐句抽取）。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    if _NOTE_WORDS.search(t):
        return False
    if _BARE.match(t):
        return True
    return bool(_ACTION.search(t) and _REF.search(t))


_AFFIRM = re.compile(r"^(对|对的|对对|是|是的|嗯|好|好的|行|可以|没问题|记吧|就这样|确认|没错|正确|ok|OK)[吧啊哦呀的，。！!~\s]*$")
_DENY_WORDS = ("不对", "不是", "不要", "别记", "不用", "算了", "取消", "不行", "错了", "不准确", "不太对")


def classify_reply(text: str) -> str:
    """对「这样记可以吗」的回应：affirm / deny / correct / other。"""
    t = (text or "").strip()
    if _AFFIRM.match(t):
        return "affirm"
    if any(t.startswith(w) or (w in t and len(t) <= 8) for w in _DENY_WORDS):
        rest = re.sub(r"^(不对|不是|不要|别记|不用|算了|取消|不行|错了|不准确|不太对)[，,。！!\s]*", "", t)
        return "correct" if len(rest) >= 5 else "deny"
    return "other"


# ── 2. 相对日期解析（只给模型提示，不让它自己算） ───────────────────
_WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEK_NAME = "一二三四五六日"


def _fmt(d: date) -> str:
    return f"{d.isoformat()}（周{_WEEK_NAME[d.weekday()]}）"


def resolve_date_hints(text: str, today: date) -> List[str]:
    """把文本里的相对日期换算成确切日期。「下周X」按日历周算：下周的周X，而不是最近的那个周X。"""
    hints: List[str] = []
    t = text or ""
    for word, delta in (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0), ("昨天", -1), ("前天", -2)):
        if word in t:
            hints.append(f"{word}={_fmt(today + timedelta(days=delta))}")
    monday = today - timedelta(days=today.weekday())
    for m in re.finditer(r"(下下|下|这|本)?(?:个)?(?:周|星期)([一二三四五六日天])", t):
        pre, wd = m.group(1) or "", _WEEK[m.group(2)]
        if pre == "下下":
            d = monday + timedelta(days=14 + wd)
        elif pre == "下":
            d = monday + timedelta(days=7 + wd)
        elif pre in ("这", "本"):
            d = monday + timedelta(days=wd)
        else:  # 单说「周六」：取今天起最近的那个
            d = today + timedelta(days=(wd - today.weekday()) % 7)
        hints.append(f"{m.group(0)}={_fmt(d)}")
    seen, out = set(), []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# ── 3. 候选对话窗口 ────────────────────────────────────────────────
def _plain(text: Any) -> str:
    t = str(text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            d = json.loads(t)
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                return d["content"].strip()
        except (ValueError, TypeError):
            pass
    return t


def fetch_window(device_id: str, *, before_id: Optional[int] = None, rows: int = WINDOW_ROWS,
                 gap_minutes: int = GAP_MINUTES, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """取该设备最近的对话轮次（时间正序）。从最新往回走，相邻两轮间隔超过 gap 就截断（换话题了）。"""
    p = Path(db_path) if db_path else _DATA_DIR / "devices.db"
    conn = None
    try:
        conn = sqlite3.connect(str(p), timeout=5)
        sql = ("SELECT id, user_text, ai_text, strftime('%s', created_at) FROM dialogue_history "
               "WHERE device_id = ? ")
        params: List[Any] = [device_id]
        if before_id is not None:
            sql += "AND id < ? "
            params.append(int(before_id))
        sql += "ORDER BY id DESC LIMIT ?"
        params.append(int(rows))
        got = conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取对话窗口失败: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()
    out: List[Dict[str, Any]] = []
    last_ts: Optional[int] = None
    for rid, u, a, ts in got:  # 新→旧
        ts = int(ts or 0)
        if last_ts is not None and last_ts - ts > gap_minutes * 60:
            break
        last_ts = ts
        out.append({"id": rid, "user": _plain(u)[:TURN_CHARS], "ai": str(a or "").strip()[:TURN_CHARS]})
    out.reverse()
    return out


# ── 4. 整理模型调用 ────────────────────────────────────────────────
SYSTEM_PROMPT = """你是家庭助手的「记忆整理员」。主人说了「把刚才那段记下来」，你要从最近的对话里找出主人想记的那件事，写成一条可以长期保存的完整记忆。

要求：
1. 先判断主人要记的是哪一段：通常是请求之前**最近的一个连续话题**；与它无关的更早对话、纯操作指令（播放、开关灯、查天气）一律忽略。
2. 判断这件事是否**从头开始**：如果最早的一轮明显是话说到一半（比如以「然后呢」「接着」「那后来」开头，或在回应一件前面没出现的事），starts_mid_topic 填 true。
3. 类型只能三选一：fact=事实或偏好（谁喜欢/不喜欢/过敏/习惯什么）；plan=约定或计划（时间、地点、人、分工）；howto=步骤或经验（做法、排查办法，要保留关键步骤和注意点）。都不是就 found=false。
4. summary 是「一句完整的话」，不依赖上下文也能看懂：写清是谁、什么事；不能写「用户」「主人」「刚才」「这段对话」；不要编造对话里没有的信息；只写最终结论，被否掉的选项不写。fact ≤60字；plan ≤100字，必须带确切日期/时间（用我给你的日期提示，不要自己推算）；howto ≤150字。
5. 只输出一个 JSON，不要任何别的文字：
{"found":true,"type":"fact|plan|howto","title":"≤12字标题","summary":"……","first_turn":最早包含的那一轮序号,"starts_mid_topic":false,"date":"YYYY-MM-DD或null"}
找不到可记的内容时：{"found":false,"reason":"一句话原因"}"""


def build_user_prompt(turns: List[Dict[str, Any]], *, request: str, speaker: Optional[str],
                      today: date, hints: List[str], correction: Optional[str] = None) -> str:
    lines = [f"今天是 {_fmt(today)}。", f"说话人：{speaker or '未识别（用「家里人」指代）'}。"]
    if hints:
        lines.append("日期提示（已换算好，直接用）：" + "；".join(hints))
    lines.append("\n最近的对话（序号越大越新）：")
    for i, t in enumerate(turns, 1):
        lines.append(f"[{i}] 用户：{t['user']}")
        if t.get("ai"):
            lines.append(f"    助手：{t['ai']}")
    lines.append(f"\n主人现在说：「{request}」")
    if correction:
        lines.append(f"主人对上一版整理的纠正：「{correction}」——请按纠正重新整理。")
    return "\n".join(lines)


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


def _call_llm(ep: Dict[str, str], system: str, user: str, *, mode: str) -> Optional[Dict[str, Any]]:
    """调一次端点，返回解析后的 JSON；任何失败返回 None。Qwen 系（local/extractor）关思考。"""
    if not ep.get("base_url") or not ep.get("model"):
        return None
    try:
        import openai

        client = openai.OpenAI(base_url=ep["base_url"], api_key=ep.get("api_key") or "x",
                               timeout=LLM_TIMEOUT_SECONDS, max_retries=0)
        kw: Dict[str, Any] = {}
        if mode in ("local", "extractor"):
            kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = client.chat.completions.create(
            model=ep["model"], temperature=0.1, max_tokens=900,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
        return _parse_json(resp.choices[0].message.content or "")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"整理模型调用失败（{ep.get('model')}）: {type(e).__name__}: {str(e)[:120]}")
        return None


def curate(config: Dict[str, Any], turns: List[Dict[str, Any]], *, request: str,
           speaker: Optional[str], today: Optional[date] = None,
           correction: Optional[str] = None) -> Dict[str, Any]:
    """一次整理。返回 {found, type, title, summary, first_turn, starts_mid_topic, date, _model, _fallback}。"""
    from core.utils import memory_curator_config as cc

    today = today or date.today()
    all_text = " ".join(t["user"] for t in turns) + " " + request + " " + (correction or "")
    hints = resolve_date_hints(all_text, today)
    user_prompt = build_user_prompt(turns, request=request, speaker=speaker, today=today,
                                    hints=hints, correction=correction)
    r = cc.resolve(config)
    result = _call_llm(r, SYSTEM_PROMPT, user_prompt, mode=r["mode"])
    used, fell_back = r["model"], False
    if result is None and r["mode"] != "extractor":
        logger.bind(tag=TAG).warning("整理模型不可用，退回 mem0 抽取模型")
        result = _call_llm(r["fallback"], SYSTEM_PROMPT, user_prompt, mode="extractor")
        used, fell_back = r["fallback"].get("model", ""), True
    if result is None:
        return {"found": False, "reason": "整理模型暂时不可用", "_error": True}
    result["_model"], result["_fallback"] = used, fell_back
    return result


def curate_with_expansion(config: Dict[str, Any], device_id: str, *, request: str,
                          speaker: Optional[str], today: Optional[date] = None,
                          correction: Optional[str] = None,
                          db_path: Optional[Path] = None) -> Dict[str, Any]:
    """取窗口 → 整理；模型认为开头是半截话就往前再取一段合并重来（最多 MAX_EXPANSIONS 次）。"""
    turns = fetch_window(device_id, db_path=db_path)
    if not turns:
        return {"found": False, "reason": "最近没有可整理的对话"}
    result: Dict[str, Any] = {}
    for attempt in range(MAX_EXPANSIONS + 1):
        result = curate(config, turns, request=request, speaker=speaker, today=today, correction=correction)
        if not result.get("found") or not result.get("starts_mid_topic") or attempt == MAX_EXPANSIONS:
            break
        earlier = fetch_window(device_id, before_id=turns[0]["id"], db_path=db_path)
        if not earlier:
            break
        logger.bind(tag=TAG).info(f"整理模型判定开头是半截话，向前扩 {len(earlier)} 轮（第 {attempt + 1} 次）")
        turns = earlier + turns
    if result.get("found"):
        result["_incomplete"] = bool(result.get("starts_mid_topic"))  # 扩到头仍是半截话：读回时提醒
        try:
            first = max(1, int(result.get("first_turn") or 1))
        except (TypeError, ValueError):
            first = 1
        result["_turns"] = turns[first - 1:]
    return result


# ── 5. 原文归档 ───────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    device_id TEXT, speaker TEXT, scope TEXT,
    ep_type TEXT, title TEXT, summary TEXT,
    ep_date TEXT, transcript TEXT, source_ids TEXT,
    mem0_id TEXT, model TEXT
)"""


def archive_episode(draft: Dict[str, Any], *, device_id: str, speaker: Optional[str], scope: str,
                    mem0_id: Optional[str], db_path: Optional[Path] = None) -> Optional[int]:
    p = Path(db_path) if db_path else _DATA_DIR / "memory_episodes.db"
    conn = None
    try:
        conn = sqlite3.connect(str(p), timeout=5)
        conn.execute(_SCHEMA)
        turns = draft.get("_turns") or []
        cur = conn.execute(
            "INSERT INTO memory_episodes (device_id, speaker, scope, ep_type, title, summary, ep_date,"
            " transcript, source_ids, mem0_id, model) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, speaker, scope, draft.get("type"), draft.get("title"), draft.get("summary"),
             draft.get("date"), json.dumps(turns, ensure_ascii=False),
             json.dumps([t.get("id") for t in turns]), mem0_id, draft.get("_model")))
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.bind(tag=TAG).warning(f"情节原文归档失败: {e}")
        return None
    finally:
        if conn is not None:
            conn.close()


# ── 6. 对话回合处理（chat 入口调用） ─────────────────────────────────
def handle_turn(conn, query: str, say: Callable[[str], None], *, config: Optional[Dict[str, Any]] = None,
                today: Optional[date] = None) -> Optional[str]:
    """处理「记下来」请求与随后的确认。返回要播报的话；与本功能无关返回 None（走原对话链路）。"""
    config = config if config is not None else getattr(conn, "config", {}) or {}
    pending = getattr(conn, "_pending_episode", None)
    query = _plain(query)  # 语音链路的话带 {"speaker":..,"content":..} 外壳
    speaker = getattr(conn, "current_speaker", None)
    speaker = None if (not speaker or speaker in ("未知说话人", "unknown")) else str(speaker)

    if pending:
        conn._pending_episode = None
        if time.time() - pending["ts"] <= PENDING_TTL_SECONDS:
            kind = classify_reply(query)
            if kind == "affirm":
                return _commit(conn, pending["draft"], speaker)
            if kind == "deny":
                return "好的，那这条我不记了。"
            if kind == "correct":
                say("好的，我按你说的重新整理一下。")
                return _draft_and_ask(conn, config, pending["request"], speaker, today, correction=query)
        # 其他话题：草稿作废，交还给正常对话
        logger.bind(tag=TAG).info("待确认的整理草稿因主人转了话题而作废")

    if is_capture_request(query):
        say("好的，我来整理一下。")
        return _draft_and_ask(conn, config, query, speaker, today)
    return None


def _draft_and_ask(conn, config, request, speaker, today, correction=None) -> str:
    device_id = getattr(conn, "device_id", "") or ""
    draft = curate_with_expansion(config, device_id, request=request, speaker=speaker,
                                  today=today, correction=correction)
    if not draft.get("found"):
        if draft.get("_error"):
            return "抱歉，整理的模型这会儿没响应，稍后再试一次吧。"
        return "我没找到需要记的内容，你可以说得具体一点，比如「把刚才定的周末计划记下来」。"
    if draft.get("type") not in TYPES or not str(draft.get("summary") or "").strip():
        return "我没能把这段整理成一条清楚的记忆，先不记了。"
    summary = str(draft["summary"]).strip()
    try:
        from core.utils.memory_scope import is_sensitive_text

        if speaker is None and is_sensitive_text(summary):
            return "这段内容涉及隐私，我没认出是谁在说话，就先不记了。"
    except Exception:
        pass
    conn._pending_episode = {"draft": draft, "request": request, "ts": time.time()}
    warn = "前面的内容我没找全，可能不完整。" if draft.get("_incomplete") else ""
    return f"{warn}我整理的是：{summary.rstrip('。.！!')}。这样记可以吗？"


def _commit(conn, draft: Dict[str, Any], speaker: Optional[str]) -> str:
    provider = getattr(conn, "memory", None)
    if provider is None or not hasattr(provider, "store_episode"):
        return "当前的记忆系统不支持这种记法，没记成。"
    scope = provider._resolve_scope(speaker, None)
    device_id = getattr(conn, "device_id", "") or ""
    meta = {"episode_type": draft.get("type"), "title": draft.get("title"), "episode_date": draft.get("date")}
    res = provider.store_episode(str(draft["summary"]).strip(), scope=scope, speaker=speaker, meta=meta)
    status = res.get("status")
    if status == "added":
        eid = archive_episode(draft, device_id=device_id, speaker=speaker, scope=scope, mem0_id=res.get("id"))
        logger.bind(tag=TAG).info(f"情节记忆已保存：类型={draft.get('type')} scope={scope} 归档编号={eid}")
        return f"好的，已经记下了（{TYPE_LABELS.get(draft.get('type'), '')}）。"
    if status == "duplicate":
        return "这件事我之前已经记过了。"
    return "抱歉，这次没记成功，稍后可以再说一次。"
