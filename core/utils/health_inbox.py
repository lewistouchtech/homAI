"""飞书健康录入管道 + 主动问询闭环（2026-10-11 主人裁定方案）

入口：用户在飞书发"我去医院开了氨氯地平，一天一片饭后吃"
  1. maybe_handle() 预筛关键词命中
  2. LLM 按 EVENT_DETAIL_SPEC 抽取结构化 JSON
  3. 建 pending 事件 + 发确认卡："我理解为：开始用药 氨氯地平 5mg…回复'对'确认"
  4. 用户回"对" → confirmed → 自动联动 medications → 回执
非健康消息原样走原 LLM 对话，零干扰。

主动问询闭环：
  proactive_check()（定时跑）发现指标突变（如血压骤降且无用药可解释）
  → 飞书问"这两天血压降了不少，是开始吃药了吗？"
  → 用户回复 → 分类为 inquiry_reply 事件（"开始用药"还会联动 medications）
  → 报告分段自动更新。同类指标 3 天内只问一次。

确认/问询状态都在内存（重启即失效，安全侧不写未确认数据）。
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SERVER_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CONTACTS_PATH = os.path.join(SERVER_ROOT, "data", ".lark_contacts.json")
DEV_DB = os.path.join(SERVER_ROOT, "data", "devices.db")

_CONFIRM_RE = re.compile(r'^(是|对|确认|好|嗯|行|可以|是的|没错|yes|ok|要|存)', re.I)
_REJECT_RE = re.compile(r'^(不|否|错|取消|不是|不对|no|算了)', re.I)

# ── 注册区：由 FeishuListener 启动时注入，app.py 的巡检循环共用 ──
_send_fn = None      # async (open_id, text) -> None
_extract_fn = None   # async (prompt) -> str（LLM 抽取，见 FeishuListener）


def register(send_fn, extract_fn):
    global _send_fn, _extract_fn
    _send_fn, _extract_fn = send_fn, extract_fn
    logger.info("[健康录入] 管道已注册（send=%s extract=%s）",
                bool(send_fn), bool(extract_fn))


# ── 待确认 / 待答问状态（内存，按 open_id）────────────────────
_PENDING_CONFIRM: dict = {}   # open_id -> {event_id, text, awaiting_member, expire}
_PENDING_INQUIRY: dict = {}   # open_id -> {question, member_id, member_name, metric, expire}
_ASKED_RECENT: dict = {}      # (member_id, metric) -> ts  问询频率控制

_CONFIRM_TTL = 1800      # 确认卡 30 分钟有效
_INQUIRY_TTL = 86400     # 问询 24 小时有效
_ASK_COOLDOWN = 259200   # 同类问询 3 天冷却


def _resolve_member(open_id: str):
    """open_id → family_members 行。靠 .lark_contacts.json 的 name 匹配，禁止猜。"""
    try:
        import sqlite3
        if not os.path.exists(CONTACTS_PATH) or not os.path.exists(DEV_DB):
            return None
        contacts = json.load(open(CONTACTS_PATH, encoding="utf-8"))
        name = None
        owner = contacts.get("owner") or {}
        if owner.get("open_id") == open_id:
            name = owner.get("name") or "主人"
        if not name:
            for m in (contacts.get("family") or []):
                if m.get("open_id") == open_id:
                    name = m.get("name")
                    break
        if not name or name == "主人":
            return None  # "主人"不是成员名：确认卡里请用户指明给谁记录
        conn = sqlite3.connect(DEV_DB)
        try:
            row = conn.execute(
                'SELECT id, name, relation, age, gender, birthday, height, weight,'
                ' occupation, health_status FROM family_members WHERE name = ?',
                (name,)).fetchone()
            if row:
                return {'id': row[0], 'name': row[1], 'relation': row[2],
                        'age': row[3], 'gender': row[4], 'birthday': row[5],
                        'height': row[6], 'weight': row[7], 'occupation': row[8],
                        'health_status': row[9]}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[健康录入] 成员解析失败: {e}")
    return None


def _extract_prompt(text: str) -> str:
    from core.api.health_event_api import EVENT_DETAIL_SPEC
    spec_lines = []
    for et, s in EVENT_DETAIL_SPEC.items():
        fields = '，'.join(f"{k}({v})" for k, v in s['fields'].items())
        spec_lines.append(f"- {et}（{s['label']}）：{fields or '无特有字段'}")
    return (
        "把用户的这条消息解析为一条健康事件。只输出 JSON，不要输出其他内容。\n"
        "消息里没有时间就用今天的日期；判断不出属于健康事件就输出 {\"is_health_event\": false}。\n"
        "JSON 格式：{\"is_health_event\": true, \"event_type\": 类型, \"event_time\": \"YYYY-MM-DD\","
        " \"title\": \"一句话摘要\", \"member_hint\": \"提到的家庭成员名（没提可省）\","
        " \"detail\": {...按类型的字段，没有的省略...}}\n"
        "可选类型与字段：\n" + "\n".join(spec_lines) + "\n\n用户消息：" + text
    )


def _card_text(ev_type_label: str, detail: dict, mname: str | None) -> str:
    bits = [f"【{ev_type_label}】"]
    key_show = [('drug_name', '药名'), ('dose', '剂量'), ('frequency', '每日次数'),
                ('times', '时间'), ('meal_relation', '服法'), ('indication', '治什么'),
                ('hospital', '医院'), ('department', '科室'), ('diagnosis', '诊断'),
                ('symptom', '症状'), ('change', '内容'), ('new_dose', '新剂量')]
    for k, label in key_show:
        v = detail.get(k)
        if v:
            bits.append(f"{label}：{v if not isinstance(v, list) else '/'.join(map(str, v))}")
    if mname:
        bits.append(f"成员：{mname}")
    else:
        bits.append("成员：还没确定是谁（确认时请带上名字，如'对，李威的'）")
    return "我理解为：" + "，".join(bits) + "\n回复「对」确认记录；不对就回「不对」。"


async def maybe_handle(config: dict, open_id: str, text: str, client) -> bool:
    """飞书入站消息的健康管道。返回 True 表示已消费（不再走普通对话）。"""
    now = time.time()

    # ── 1. 有待答问询：优先处理（闭环回填）──────────────────
    inq = _PENDING_INQUIRY.get(open_id)
    if inq and inq['expire'] > now:
        return await _answer_inquiry(open_id, text, client)
    _PENDING_INQUIRY.pop(open_id, None)

    # ── 2. 有待确认事件：对/不对 → 确认或取消 ────────────────
    pd = _PENDING_CONFIRM.get(open_id)
    if pd and pd['expire'] > now:
        if _REJECT_RE.match(text.strip()):
            from core.api import health_event_api
            health_event_api.cancel_event(pd['event_id'])
            _PENDING_CONFIRM.pop(open_id, None)
            await client.send_text(open_id, "好的，这条就不记录了。")
            return True
        if _CONFIRM_RE.match(text.strip()):
            from core.api import health_event_api
            if pd.get('awaiting_member'):
                # "对，李威的" → 从回复里捡名字
                m = re.search(r'([\u4e00-\u9fa5]{2,4})的?$', text.strip())
                hint = (m.group(1) if m else '').rstrip('的')
                row = None
                if hint:
                    from core.utils.health_report_engine import find_family_member
                    row = find_family_member(DEV_DB, hint)
                if not row:
                    await client.send_text(
                        open_id, "这条是给哪位成员记录的？请回复名字，比如「对，李威的」。")
                    return True
                health_event_api.update_event(pd['event_id'], {'member_id': row['id']})
            err = health_event_api.confirm_event(pd['event_id'])
            _PENDING_CONFIRM.pop(open_id, None)
            if err:
                await client.send_text(open_id, f"记录失败了：{err}")
            else:
                await client.send_text(open_id, "已记录 ✅ 健康报告会自动纳入这条信息。")
            return True
        # 其他文本：放弃 pending，落到正常流程
        from core.api import health_event_api
        health_event_api.cancel_event(pd['event_id'])
        _PENDING_CONFIRM.pop(open_id, None)

    # ── 3. 预筛：不像健康内容 → 不消费 ──────────────────────
    from core.api.health_event_api import HEALTH_KEYWORDS
    if not HEALTH_KEYWORDS.search(text):
        return False
    if _extract_fn is None:
        return False

    # ── 4. LLM 抽取结构化事件 ───────────────────────────────
    try:
        raw = await _extract_fn(_extract_prompt(text))
        m = re.search(r'\{.*\}', raw, re.S)
        data = json.loads(m.group(0)) if m else None
    except Exception as e:
        logger.warning(f"[健康录入] 抽取失败（回落普通对话）: {e}")
        return False
    if not data or not data.get('is_health_event'):
        return False
    from core.api import health_event_api
    et = str(data.get('event_type') or '')
    if et not in health_event_api.EVENT_DETAIL_SPEC:
        return False
    detail = data.get('detail') or {}

    # 成员解析：member_hint → 家庭成员；解析不出 → 确认卡环节再要名字
    member = None
    hint = (data.get('member_hint') or '').strip().rstrip('的')
    if hint:
        from core.utils.health_report_engine import find_family_member
        member = find_family_member(DEV_DB, hint)
    if not member:
        member = _resolve_member(open_id)  # 发送者本人在通讯录里对得上才行

    label = health_event_api.EVENT_DETAIL_SPEC[et]['label']
    eid, err = health_event_api.create_event({
        'member_id': member['id'] if member else None,
        'event_type': et,
        'event_time': data.get('event_time') or datetime.now().strftime('%Y-%m-%d'),
        'title': data.get('title') or label,
        'detail': detail,
        'note': text[:2000],
        'source': 'feishu',
        'source_user': open_id,
        'status': 'pending',  # 未确认不生效（含 medications 联动也不发生）
    })
    if err:
        logger.warning(f"[健康录入] 建事件失败: {err}")
        return False
    _PENDING_CONFIRM[open_id] = {
        'event_id': eid,
        'awaiting_member': member is None,
        'expire': now + _CONFIRM_TTL,
    }
    await client.send_text(
        open_id, _card_text(label, detail, member['name'] if member else None))
    logger.info(f"[健康录入] pending 事件 {eid}（{et}）待确认")
    return True


# ── 主动问询闭环 ──────────────────────────────────────────────

def detect_sudden_changes(member_id: int, medications: list) -> list:
    """指标突变检测（方向不限）。返回 [{metric, desc, direction}]。

    触发条件（按天聚合序列）：
    - 血压：近3天均值较其前7天降 ≥10 mmHg 且无近14天开始用药可解释
    - 体重：7天内降 ≥2 kg
    - 血氧：近3天均值较前7天降 ≥3%
    """
    from core.utils.health_report_engine import load_member_series, _mean
    series = load_member_series(DEV_DB, member_id)
    if not series.get('latest'):
        return []
    out = []
    today = datetime.now()

    def _window(pts, days, end_offset=0):
        end = today - timedelta(days=end_offset)
        start = end - timedelta(days=days - 1)
        return [p for p in pts if start.strftime('%Y-%m-%d') <= p['date'] <= end.strftime('%Y-%m-%d')]

    med_start_recent = any(
        str(m.get('start_date') or '') >= (today - timedelta(days=14)).strftime('%Y-%m-%d')
        for m in medications)

    bp = series.get('blood_pressure') or []
    if len(bp) >= 4 and not med_start_recent:
        recent = _mean([p.get('sys') for p in _window(bp, 3)])
        before = _mean([p.get('sys') for p in _window(bp, 7, end_offset=3)])
        if recent and before and before - recent >= 10:
            out.append({'metric': '血压',
                        'desc': f'近3天收缩压均值 {recent:.0f}，较前一周（{before:.0f}）明显下降',
                        'direction': 'down'})

    wt = series.get('weight') or []
    if len(wt) >= 2:
        recent7 = _window(wt, 7)
        if len(recent7) >= 2:
            drop = recent7[0]['weight'] - recent7[-1]['weight']
            if drop >= 2:
                out.append({'metric': '体重',
                            'desc': f'7天内体重下降 {drop:.1f} kg（{recent7[0]["weight"]} → {recent7[-1]["weight"]}）',
                            'direction': 'down'})

    bo = series.get('blood_oxygen') or []
    if len(bo) >= 4:
        recent = _mean([p.get('spo2') for p in _window(bo, 3)])
        before = _mean([p.get('spo2') for p in _window(bo, 7, end_offset=3)])
        if recent and before and before - recent >= 3:
            out.append({'metric': '血氧',
                        'desc': f'近3天血氧均值 {recent:.1f}%，较前一周（{before:.1f}%）下降',
                        'direction': 'down'})
    return out


async def _answer_inquiry(open_id: str, text: str, client) -> bool:
    """问询回复 → 分类 → 记 inquiry_reply 事件（"开始用药"联动 medications）。"""
    inq = _PENDING_INQUIRY.pop(open_id)
    if _extract_fn is None:
        return False
    try:
        prompt = (
            "用户在回答健康问询。只输出 JSON：{\"interpretation\": 其中之一"
            "（开始用药/就诊/休息不足/测量误差/无异常/其他）,"
            " \"drug_name\": 若开始用药给出药名（没有可省）, \"detail\": \"一句话概括\"}\n"
            f"问询：{inq['question']}\n用户回答：{text}")
        raw = await _extract_fn(prompt)
        m = re.search(r'\{.*\}', raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    interp = data.get('interpretation') or '其他'
    from core.api import health_event_api
    ev_detail = {'question': inq['question'], 'answer': text[:500],
                 'interpretation': interp, 'related_metric': inq.get('metric', '')}
    if interp == '开始用药' and data.get('drug_name'):
        ev_detail['drug_name'] = data['drug_name']
    health_event_api.create_event({
        'member_id': inq['member_id'], 'event_type': 'inquiry_reply',
        'event_time': datetime.now().strftime('%Y-%m-%d'),
        'title': f"问询回复：{interp}",
        'detail': ev_detail, 'note': text[:2000],
        'source': 'assistant', 'source_user': open_id,
    })
    # 联动：开始用药 → medications（start_date=今天，剂量后续可补/调）
    if interp == '开始用药' and data.get('drug_name'):
        health_event_api._sync_medication(
            'medication_start', inq['member_id'],
            {'drug_name': data['drug_name'],
             'start_date': datetime.now().strftime('%Y-%m-%d')})
    await client.send_text(open_id, f"已记录：{interp}。健康报告会自动更新，谢谢告知 🙏")
    return True


async def proactive_check(config: dict):
    """定时巡检：突变 → 飞书问询（按 open_id 发给相关成员/主人）。app.py 每 30 分钟调一次。"""
    if _send_fn is None:
        return
    try:
        import sqlite3
        from core.api import medication_api
        conn = sqlite3.connect(DEV_DB)
        rows = conn.execute('SELECT id, name FROM family_members').fetchall()
        conn.close()
        now = time.time()
        for mid, mname in rows:
            meds = medication_api.list_medications(member_id=mid)
            for ch in detect_sudden_changes(mid, meds):
                key = (mid, ch['metric'])
                if now - _ASKED_RECENT.get(key, 0) < _ASK_COOLDOWN:
                    continue
                # 发给人：优先本人 open_id（通讯录），退而求其次发主人
                target = _open_id_of_member(mname) or _open_id_of_owner()
                if not target:
                    continue
                if target in _PENDING_INQUIRY and _PENDING_INQUIRY[target]['expire'] > now:
                    continue  # 已有未答问询，不叠加
                _ASKED_RECENT[key] = now
                question = (f"健康小提醒：{mname}的{ch['desc']}。"
                            f"想跟您确认一下：是开始吃药了、去过医院了，"
                            f"还是最近休息/测量的原因？简单回复一句就行。")
                _PENDING_INQUIRY[target] = {
                    'question': question, 'member_id': mid, 'member_name': mname,
                    'metric': ch['metric'], 'expire': now + _INQUIRY_TTL,
                }
                await _send_fn(target, question)
                logger.info(f"[健康问询] 已向 {target[:10]}… 询问 {mname} 的{ch['metric']}突变")
    except Exception as e:
        logger.warning(f"[健康问询] 巡检失败（不影响主服务）: {e}")


def _open_id_of_member(name: str):
    try:
        contacts = json.load(open(CONTACTS_PATH, encoding="utf-8"))
        for m in (contacts.get("family") or []):
            if m.get("name") == name:
                return m.get("open_id")
    except Exception:
        pass
    return None


def _open_id_of_owner():
    try:
        contacts = json.load(open(CONTACTS_PATH, encoding="utf-8"))
        return (contacts.get("owner") or {}).get("open_id")
    except Exception:
        return None
