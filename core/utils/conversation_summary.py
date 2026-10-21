"""
对话摘要模块 - HomeBrain AI 管家
三层过滤：关口1(互动意图+宽松预过滤) → 关口2(LLM结构化摘要+评分) → 关口3(质量校验)
声纹用于标注说话人，不用于过滤
"""

import sqlite3
import os
import json
import re
import time
from datetime import datetime
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


def get_db_path():
    """获取数据库路径"""
    paths = [
        os.path.expanduser("~/.openclaw/projects/homebrain/server/homebrain-server/data/devices.db"),
        os.path.expanduser("~/openclaw/projects/homebrain/server/homebrain-server/data/devices.db"),
        "data/devices.db",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


# ============================================================
# 关口1 — 宽松预过滤（单条）
# ============================================================

def _is_meaningless_repetition(text: str) -> bool:
    """检测无意义重复，如'哈哈哈哈'、'啊啊啊啊'"""
    if len(text) < 4:
        return False
    # 同一字符连续出现≥3次，且总长度中该字符占比>60%
    for char in set(text):
        if text.count(char) >= 3 and text.count(char) / len(text) > 0.6:
            return True
    return False


def _is_english_filler(text: str) -> bool:
    """检测英文语气词/填充词"""
    fillers = {
        'um', 'uh', 'ah', 'oh', 'er', 'mm', 'hmm', 'eh',
        'yeah', 'yea', 'yep', 'okay', 'ok', 'well', 'like',
    }
    return text.strip().lower() in fillers


def is_valuable_single_line(text: str) -> bool:
    """
    单条文本严格过滤（关口1）：只保留真正有价值的内容

    保留：
    - 重要提醒/待办（会议、约会、截止日期）
    - 重要事件（出差、生日、聚会）
    - 重要决策（决定、确定、方案）
    - 重要事务（金额、转账、报销、项目）

    过滤：
    - 所有系统回复（伊娃的回复）
    - ASR 识别错误/噪音
    - 日常问候、闲聊
    - 控制指令（开关灯、播放音乐）
    - 时间/天气/健康查询
    - 语气词、感叹词
    - 过短或不完整的句子
    """
    if not text or len(text.strip()) < 2:
        return False

    stripped = text.strip()

    # 无意义重复
    if _is_meaningless_repetition(stripped):
        return False

    # 英文语气词
    if _is_english_filler(stripped):
        return False

    # 纯空格/不可见字符
    if not stripped or all(c in ' \t\n\r' for c in stripped):
        return False

    # === 长度过滤 ===
    # 过短的内容（< 6 字符）基本无价值
    if len(stripped) < 6:
        return False

    # === ASR 噪音检测 ===
    # 包含日文、韩文等非中文字符
    if re.search(r'[぀-ゟ゠-ヿ가-힯]', stripped):
        return False

    # 乱码特征：连续无意义字符
    if len(re.findall(r'[一-鿿]', stripped)) < len(stripped) * 0.3:
        # 中文字符占比过低
        return False

    # === 系统回复过滤 ===
    # 注意：这个函数不接收 speaker 参数，所以无法直接过滤伊娃的回复
    # 需要在调用处判断

    # === 日常问候/闲聊过滤 ===
    # AI 名字取自后台设置，后台改名后自动同步
    try:
        from core.utils.assistant_identity import get_name

        _ai_name = re.escape(get_name() or '伊娃')
    except Exception:
        _ai_name = '伊娃'
    greeting_patterns = [
        r'^(你好|您好|早上好|下午好|晚上好|早安|晚安|拜拜|再见|hi|hello|hey)',
        r'^(嘿|哈|嗨|喂|诶|嗯|啊|哦|哎|呃)',
        r'^(好嘞|好的|好|行|OK|ok)',
        rf'你好[，,]?\s*({_ai_name}|小爱|小智)',
        r'^(早点睡|晚安|休息吧)',
    ]
    for pattern in greeting_patterns:
        if re.search(pattern, stripped, re.IGNORECASE):
            return False

    # === 时间/天气/健康查询 ===
    trivial_query_patterns = [
        r'(现在|今天|明天|昨天)?(几点|时间|星期|日期)',
        r'(几点了|什么时间|什么日期)',
        r'(今天|明天|后天|昨天)?天气',
        r'(天气预报|气温|温度|下雨|下雪)',
        r'(我的|主人的)?(体重|血压|心率|血糖|血氧)',
        r'(冷不冷|热不热|会不会下雨)',
    ]
    for pattern in trivial_query_patterns:
        if re.search(pattern, stripped):
            return False

    # === 控制指令过滤 ===
    control_patterns = [
        r'(打开|关闭|切换|调整)(灯|窗帘|空调|音乐|电视)',
        r'(播放|暂停|停止|下一首|上一首)',
        r'(音量|亮度)(调大|调小|增大|减小)',
        r'(设定|设置)(温度|音量|亮度|时间)',
    ]
    for pattern in control_patterns:
        if re.search(pattern, stripped):
            return False

    # === 简单数学/计算 ===
    math_patterns = [
        r'^\d+\s*(加|减|乘|除|plus|minus|times)\s*\d+',  # "1加1"、"2减1"
        r'\d+\s*[+\-*/]\s*\d+\s*(等于|是)',  # "1+1等于"、"2*3是"
        r'^(等于几|等于多少)$',  # 只有"等于几"（不带上下文）
    ]
    for pattern in math_patterns:
        if re.search(pattern, stripped, re.IGNORECASE):
            return False

    # === 有价值内容检测（必须包含以下之一）===
    valuable_keywords = [
        # 提醒/待办
        '提醒', '记得', '别忘了', '会议', '约会', '截止日期', 'deadline',
        '明天', '下周', '后天', '下周', '月底', '月初',

        # 重要事件
        '出差', '生日', '聚会', '结婚', '毕业', '考试', '面试',

        # 重要决策
        '决定', '确定', '同意', '采用', '方案', '计划',

        # 重要事务
        '转账', '报销', '付款', '收款', '万', '千',
        '项目', '客户', '业主', '合同', '协议',

        # 重要信息
        '地址', '电话', '联系方式', '账号', '卡号',
    ]

    has_valuable_keyword = any(kw in stripped for kw in valuable_keywords)

    # 如果没有关键词，检查是否是疑问句（可能是重要询问）
    is_question = '？' in stripped or '?' in stripped

    # 如果既没有关键词，也不是疑问句，过滤掉
    if not has_valuable_keyword and not is_question:
        return False

    return True


# ============================================================
# 关口1 — 会话级互动意图判断
# ============================================================

def is_interactive_intent(session_lines: list) -> tuple:
    """
    判断一段会话是否包含互动意图（关口1 - 会话级）

    Args:
        session_lines: 列表，每项为 {"speaker": str, "content": str, "timestamp": float}

    Returns:
        (is_interactive: bool, confidence: str, reason: str)
    """
    if not session_lines:
        return False, "none", "无内容"

    score = 0
    reasons = []

    # 规则1: 回合长度得分
    avg_length = sum(len(line["content"]) for line in session_lines) / len(session_lines)
    if avg_length < 30:
        score += 2
        reasons.append("短回合")
    elif avg_length < 50:
        score += 1
    else:
        score -= 2
        reasons.append("长叙述")

    # 规则2: 说话人数
    unique_speakers = set(line.get("speaker", "未知") for line in session_lines)
    if len(unique_speakers) >= 2:
        score += 2
        reasons.append(f"{len(unique_speakers)}人交替")

    # 规则3: 对话标记
    dialog_markers = ['？', '!', '！', '吗', '吧', '啊', '呢', '帮我', '记得', '别忘了']
    marker_count = 0
    for line in session_lines:
        if any(m in line["content"] for m in dialog_markers):
            marker_count += 1
    if marker_count > 0:
        score += min(marker_count, 3)
        reasons.append(f"对话标记×{marker_count}")

    # 规则4: 叙述体惩罚
    narrative_patterns = ['他说', '她走到', '据报道', '据悉', '于是', '接着说', '写道',
                          '推开门', '走进', '看了一眼', '突然', '与此同时', '然而']
    narrative_count = 0
    for line in session_lines:
        if any(p in line["content"] for p in narrative_patterns):
            narrative_count += 1
    if narrative_count >= 2:
        score -= 3
        reasons.append("叙述体")
    elif narrative_count == 1:
        score -= 1

    # 规则5: 长连续内容（单条>80字且无明显对话标记）
    long_lines = [l for l in session_lines if len(l["content"]) > 80
                  and not any(m in l["content"] for m in dialog_markers)]
    if len(long_lines) >= 2:
        score -= 2
        reasons.append("长连续内容")

    # 判定
    reason = " + ".join(reasons) if reasons else "无明显特征"
    if score >= 3:
        return True, "high", reason
    elif score >= 1:
        return True, "medium", reason
    elif score <= -2:
        return False, "high", f"媒体/叙述体 ({reason})"
    else:
        return True, "low", f"不明确 ({reason})"


# ============================================================
# 关口3 — 质量校验
# ============================================================

def validate_summary(summary_text: str) -> bool:
    """
    验证摘要质量（关口3）：确保入库摘要是高质量的
    """
    if not summary_text or not summary_text.strip():
        return False

    summary = summary_text.strip()

    # 1. 长度合理
    if len(summary) < 10 or len(summary) > 500:
        return False

    # 2. 不能是模糊描述
    vague_patterns = [
        '聊了', '说了些', '一些', '随便聊', '日常',
        '没有特别', '没什么', '无特别', '无重要',
    ]
    if any(p in summary for p in vague_patterns):
        return False

    # 3. 必须包含具体事实
    fact_indicators = [
        '明天', '后天', '下周', '今天', '周', '月', '日', '号',
        '去医院', '去学校', '去买', '去接', '去拿', '出发',
        '血压', '血糖', '体温', '感冒', '发烧', '不舒服',
        '喜欢', '不爱', '不吃', '想吃',
        '决定', '答应', '约定', '计划', '约了',
        '作业', '考试', '放学', '上课', '放学',
    ]
    if not any(indicator in summary for indicator in fact_indicators):
        return False

    # 4. 不能全是标点
    text_without_punct = re.sub(r'[，。！？、；：""''（） \t\n\r]', '', summary)
    if len(text_without_punct) < 5:
        return False

    return True


# ============================================================
# 核心函数
# ============================================================

def save_conversation_entry(session_id, speaker, text, timestamp=None):
    """
    保存单条对话记录到缓冲区（关口1：严格过滤）

    Args:
        session_id: 会话 ID
        speaker: 说话人（声纹标注）
        text: 对话内容
        timestamp: 时间戳
    """
    # 过滤系统回复（AI 管家自己的回复不保存）
    try:
        from core.utils.assistant_identity import get_name

        _assistant_names = {'伊娃', '助手', 'AI', '系统', get_name() or '伊娃'}
    except Exception:
        _assistant_names = {'伊娃', '助手', 'AI', '系统'}
    if speaker and speaker in _assistant_names:
        return

    # 关口1：严格预过滤
    if not is_valuable_single_line(text):
        return

    timestamp = timestamp or time.time()
    conn = None
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO conversation_summaries (session_id, timestamp, speaker, content, valuable) VALUES (?, ?, ?, ?, ?)",
            (session_id, timestamp, speaker or '未知', text, 0)
        )
        conn.commit()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"保存对话记录失败: {e}")
    finally:
        if conn:
            conn.close()


def generate_summary_for_session(session_id, llm_chat_func=None, conn=None):
    """
    为指定会话生成摘要（关口2+3）
    用 LLM 把有意义的上下文整理成一段完整的描述

    Args:
        session_id: 会话 ID
        llm_chat_func: LLM 聊天函数（用于生成摘要，可选）
        conn: ConnectionHandler 对象（用于获取 LLM，可选）

    Returns:
        str: 生成的摘要内容（一段完整的描述）
    """
    db_conn = None
    try:
        db_path = get_db_path()
        db_conn = sqlite3.connect(db_path)
        cursor = db_conn.cursor()

        # AI 名字取自后台设置，后台改名后自动同步
        try:
            from core.utils.assistant_identity import get_name

            _assistant_name = get_name() or '伊娃'
        except Exception:
            _assistant_name = '伊娃'

        # 获取最近 30 分钟内的对话（优先从 conversation_summaries，降级到 dialogue_history）
        since = time.time() - 1800

        # 先尝试从 conversation_summaries 读取
        cursor.execute(
            "SELECT speaker, content, timestamp FROM conversation_summaries "
            "WHERE session_id = ? AND timestamp > ? "
            "ORDER BY timestamp",
            (session_id, since)
        )
        rows = cursor.fetchall()

        # 如果 conversation_summaries 没有数据，从 dialogue_history 读取
        if len(rows) < 2:
            cursor.execute(
                "SELECT '用户' as speaker, user_text as content, strftime('%s', created_at) as timestamp "
                "FROM dialogue_history WHERE session_id = ? ORDER BY id",
                (session_id,)
            )
            user_rows = cursor.fetchall()

            cursor.execute(
                "SELECT ? as speaker, ai_text as content, strftime('%s', created_at) as timestamp "
                "FROM dialogue_history WHERE session_id = ? ORDER BY id",
                (_assistant_name, session_id)
            )
            ai_rows = cursor.fetchall()

            # 合并并按时间排序
            rows = sorted(user_rows + ai_rows, key=lambda x: float(x[2]) if x[2] else 0)

        db_conn.close()

        if len(rows) < 2:
            logger.bind(tag=TAG).info(f"会话 {session_id} 对话太少({len(rows)}条)，暂不生成摘要")
            return None

        # 构建对话文本
        conversation_text = "\n".join([
            f"{r[0] or '未知'}: {r[1]}"
            for r in rows if r[1] and len(str(r[1]).strip()) > 0
        ])

        if not conversation_text.strip():
            logger.bind(tag=TAG).info(f"会话 {session_id} 无有效对话内容，跳过摘要生成")
            return None

        # 获取 LLM 函数
        if llm_chat_func is None and conn is not None and hasattr(conn, 'llm') and conn.llm:
            # 从 conn 创建 LLM 调用函数
            def llm_chat_func(prompt):
                try:
                    return conn.llm.response_no_stream(
                        "你是一个智能助手，负责整理对话摘要。",
                        prompt
                    )
                except Exception as e:
                    logger.bind(tag=TAG).error(f"LLM 调用失败: {e}")
                    return None

        # 生成摘要
        if llm_chat_func is None:
            summary = _simple_extract_summary(conversation_text)
        else:
            summary = _llm_generate_summary(conversation_text, llm_chat_func)

        if not summary:
            return None

        # 标记已处理
        db_conn = sqlite3.connect(db_path)
        cursor = db_conn.cursor()
        cursor.execute(
            "UPDATE conversation_summaries SET valuable = 1 WHERE session_id = ? AND timestamp > ? AND valuable = 0",
            (session_id, since)
        )
        db_conn.commit()

        logger.bind(tag=TAG).info(f"摘要生成成功: {summary[:100]}...")
        return summary

    except Exception as e:
        logger.bind(tag=TAG).error(f"生成摘要失败: {e}")
        return None
    finally:
        if db_conn:
            db_conn.close()


def _simple_extract_summary(conversation_text):
    """
    简单摘要提取（无 LLM 时备用）
    扩展关键词 + 上下文保留
    """
    lines = conversation_text.strip().split('\n')
    valuable_lines = []

    # 扩展关键词库
    valuable_keywords = [
        # 时间/计划
        '明天', '后天', '周末', '下周', '下个月', '今天', '几点',
        '早上', '下午', '晚上', '记得', '别忘了',
        # 行动
        '买', '交', '去', '来', '到', '约定', '决定', '计划', '约了',
        # 健康
        '医院', '医生', '药', '血压', '血糖', '感冒', '发烧', '不舒服',
        '头痛', '肚子', '过敏', '失眠', '咳嗽',
        # 学校
        '学校', '作业', '考试', '成绩', '老师', '放学', '上课',
        # 偏好
        '喜欢', '讨厌', '爱吃', '不吃', '想吃', '不想',
        # 决定
        '决定', '定了', '说好', '答应', '同意',
    ]

    # 保留包含关键词的行 + 其上下文（前后各1行）
    for i, line in enumerate(lines):
        for kw in valuable_keywords:
            if kw in line:
                # 保留上一行（上下文）
                if i > 0:
                    prev_content = lines[i-1].split(':', 2)[-1].strip()
                    if prev_content and prev_content not in valuable_lines:
                        valuable_lines.append(prev_content)
                # 保留当前行
                content = line.split(':', 2)[-1] if ':' in line else line
                valuable_lines.append(content.strip())
                # 保留下一行（上下文）
                if i < len(lines) - 1:
                    next_content = lines[i+1].split(':', 2)[-1].strip()
                    if next_content and next_content not in valuable_lines:
                        valuable_lines.append(next_content)
                break

    if not valuable_lines:
        return None

    return "；".join(list(dict.fromkeys(valuable_lines)))  # 去重


def _llm_generate_summary(conversation_text, llm_chat_func):
    """
    使用 LLM 生成摘要（关口2）
    要求：把有意义的上下文整理成一段完整的描述（不是逐条拼接原文）
    """
    prompt = f"""请从以下对话中提取有价值的信息，整理成一段完整的摘要描述。

规则：
1. 只提取有长期价值的信息：日程安排、待办事项、健康信息、重要决定、个人偏好、重要事实、财务事务
2. 过滤掉：闲聊、寒暄、问候、时间/天气查询、控制指令（开关灯等）、语气词、无意义内容
3. 用第三人称客观描述，不要逐条复制原文
4. 整理成一段连贯的文字，简洁明了（50-200字）
5. 如果没有有价值的信息，直接返回"无"

示例输入：
用户: 记得提醒我明天下午3点开会
伊娃: 好的，已经设置提醒
用户: 这个项目有10万块钱的回扣
用户: 你给我转8万块钱吧，转到招行

示例输出：
用户明天下午3点有会议需要参加。项目涉及10万元回扣，用户要求转账8万元到招商银行。

对话内容：
{conversation_text}

请直接输出摘要描述，不要添加其他内容："""

    try:
        summary = llm_chat_func(prompt)
        if not summary or not summary.strip():
            return None

        summary = summary.strip()

        # 过滤无效返回
        if summary in ['无', '没有', '无有价值信息', '无摘要', 'None', 'null', '']:
            return None

        # 长度检查
        if len(summary) < 10:
            return None

        # 如果 LLM 返回了多段，只取第一段
        if '\n\n' in summary:
            summary = summary.split('\n\n')[0].strip()

        return summary

    except Exception as e:
        logger.bind(tag=TAG).warning(f"LLM 摘要生成失败: {e}")
        return None
        return None


def get_recent_summaries(session_id=None, hours=24, limit=10):
    """
    获取最近的摘要列表

    Args:
        session_id: 会话 ID（None 表示所有会话）
        hours: 最近多少小时
        limit: 最大条数

    Returns:
        list: 摘要记录列表
    """
    conn = None
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        since = time.time() - hours * 3600
        if session_id:
            cursor.execute(
                "SELECT session_id, timestamp, content FROM conversation_summaries "
                "WHERE session_id = ? AND timestamp > ? AND valuable = 1 "
                "ORDER BY timestamp DESC LIMIT ?",
                (session_id, since, limit)
            )
        else:
            cursor.execute(
                "SELECT session_id, timestamp, content FROM conversation_summaries "
                "WHERE timestamp > ? AND valuable = 1 "
                "ORDER BY timestamp DESC LIMIT ?",
                (since, limit)
            )

        rows = cursor.fetchall()

        return [
            {
                'session_id': r[0],
                'timestamp': r[1],
                'datetime': datetime.fromtimestamp(r[1]).strftime('%Y-%m-%d %H:%M:%S'),
                'content': r[2],
            }
            for r in rows
        ]
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取摘要失败: {e}")
        return []
    finally:
        if conn:
            conn.close()


def delete_summaries(ids: list) -> int:
    """
    删除指定ID的摘要记录

    Args:
        ids: 要删除的记录ID列表

    Returns:
        int: 实际删除的条数
    """
    if not ids:
        return 0

    conn = None
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        placeholders = ','.join('?' for _ in ids)
        cursor.execute(
            f"DELETE FROM conversation_summaries WHERE id IN ({placeholders})",
            ids
        )
        deleted = cursor.rowcount
        conn.commit()

        logger.bind(tag=TAG).info(f"已删除 {deleted} 条摘要记录")
        return deleted
    except Exception as e:
        logger.bind(tag=TAG).error(f"删除摘要记录失败: {e}")
        return 0
    finally:
        if conn:
            conn.close()


def get_summaries_for_api(device_id='', start_date='', end_date='',
                          keyword='', page=1, limit=20):
    """
    获取摘要列表（供控制台API调用）

    Returns:
        dict: {records: [...], total: int, page: int, limit: int}
    """
    conn = None
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        conditions = []
        params = []
        if device_id:
            conditions.append("session_id LIKE ?")
            params.append(f"%{device_id}%")
        if start_date:
            conditions.append("timestamp >= ?")
            from datetime import datetime as dt
            params.append(dt.strptime(start_date, "%Y-%m-%d").timestamp())
        if end_date:
            conditions.append("timestamp <= ?")
            from datetime import datetime as dt
            end_ts = dt.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp()
            params.append(end_ts)
        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        offset = (page - 1) * limit

        cursor.execute(f'SELECT COUNT(*) FROM conversation_summaries {where_clause}', params)
        total = cursor.fetchone()[0]

        cursor.execute(
            f'SELECT id, session_id, timestamp, speaker, topic, content, keywords, valuable, created_at '
            f'FROM conversation_summaries {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?',
            params + [limit, offset]
        )
        rows = cursor.fetchall()

        records = []
        for row in rows:
            records.append({
                "id": row[0],
                "session_id": row[1],
                "timestamp": row[2],
                "timestamp_str": datetime.fromtimestamp(row[2]).strftime("%Y-%m-%d %H:%M:%S") if row[2] else "",
                "speaker": row[3] or "",
                "topic": row[4] or "",
                "content": row[5],
                "keywords": row[6] or "",
                "valuable": row[7],
                "created_at": row[8],
            })

        return {
            "success": True,
            "records": records,
            "total": total,
            "page": page,
            "limit": limit,
        }
    except Exception as e:
        logger.bind(tag=TAG).error(f"获取摘要列表失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "records": [],
            "total": 0,
        }
    finally:
        if conn:
            conn.close()
