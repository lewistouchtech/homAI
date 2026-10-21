"""
唤醒词检测模块 - HomeBrain AI 管家
支持近音词匹配、多唤醒词、唤醒/摘要双模式

========================================================================
禁止修改：唤醒词检测核心逻辑
功能：从数据库加载自定义唤醒词，支持中文和拼音形式
负责人：bot-eva
修改需获得 bot-eva 同意
实现要点：
1. 从数据库 ai_butler_config 表读取 wakeWord 配置
2. 支持逗号分隔的多个唤醒词
3. 同时支持中文（如"小不点"）和拼音（如"xiao bu dian"）
4. detect 方法会将输入和唤醒词都转为拼音（移除空格）后匹配
5. 不要修改拼音转换、空格处理、唤醒词列表构建逻辑
========================================================================
"""

import sqlite3
import os
import re
import time
from pathlib import Path
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 唤醒词检测使用拼音编辑距离，不再需要中文单字近音映射


class WakewordDetector:
    """唤醒词检测器"""

    def __init__(self, db_path=None):
        self.wakewords = []  # 主唤醒词列表
        self.near_sound_words = set()  # 近音词扩展集合
        self.ai_name = "伊娃"  # AI 名称
        self.nickname = "小伊"  # 昵称
        # 【修复 2026-09-06】wake_word 必须有默认值：数据库缺表/读取异常时，
        # 否则 helloHandle 访问 wake_word 抛 AttributeError，导致唤醒词配置无法下发给设备
        self.wake_word = "ni hao yi wa"
        self.enabled = True  # 是否启用唤醒词（新增开关）
        self.session_active_until = 0  # 唤醒后会话结束时间（新增）
        self.session_timeout = 120  # 会话超时时间（秒）——2026-09-24 主人裁决：30s 太短，添加日程多轮追问来不及，改为 120s
        self.auto_end_enabled = False  # 是否启用自动结束对话（新增实验功能）
        self._load_from_db(db_path)
        self._build_near_sound_words()

    def _get_db_path(self):
        """获取数据库路径"""
        # 尝试多个可能的路径
        paths = [
            os.path.expanduser("~/.openclaw/projects/homebrain/server/homebrain-server/data/devices.db"),
            os.path.expanduser("~/openclaw/projects/homebrain/server/homebrain-server/data/devices.db"),
            "server/homebrain-server/data/devices.db",
            "data/devices.db",
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        return paths[0]

    def _load_from_db(self, db_path=None):
        """从数据库加载 AI 管家配置"""
        db_path = db_path or self._get_db_path()
        try:
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_key, config_value FROM ai_butler_config")
                rows = {k: v for k, v in cursor.fetchall()}
            finally:
                conn.close()

            self.ai_name = rows.get('name', '伊娃')
            self.nickname = rows.get('nickname', '小伊')
            self.wake_word = rows.get('wakeWord', 'yi wa ni hao')
            # 读取启用状态
            self.enabled = rows.get('enable_wakeWord', 'true').lower() == 'true'
            # 读取会话超时时间
            # 2026-10-13 修复：库里没配这个键时，旧写法兜底 30 秒，把 __init__ 里
            # 2026-09-24 主人裁决的 120 秒（commit ff6b8f4「30s 太短，多轮追问不被踢待机」）
            # 整个架空了 —— 实测开发机一直是「唤醒会话激活，将在 30 秒后超时」，
            # 放音乐时回复后过 37 秒再说话就被 idle_guard 当作未唤醒静默丢弃。
            # 兜底必须沿用 __init__ 的默认值，库里显式配置了才覆盖。
            self.session_timeout = int(rows.get('wakeword_session_timeout', self.session_timeout))
            # 读取自动结束功能状态
            self.auto_end_enabled = rows.get('enable_auto_end_dialogue', 'false').lower() == 'true'
            # 2026-10-04 移除 wake_threshold 死配置。
            # 原意是"灵敏度阈值"，但 detect() 全程没用过它，实际匹配用的是写死的
            # max_distance = max(1, int(len(word_pinyin) * 0.1))。留着只会误导：
            # Mac Mini 后台存的是 12（÷10 得 1.2，超出 0.0-1.0 上限），
            # 开发机存 5，两台不一致；将来哪个版本真启用它，1.2 会让任何唤醒词都匹配不上。
            # 后台管理台也没有对应输入框，当年应是手工调 API 写进去的。

            # ========================================================================
            # 唤醒词列表构建逻辑
            # 负责人：bot-eva
            # 修改需获得 bot-eva 同意
            # 实现要点：
            # 1. 唤醒词只取后台"唤醒词"框（ai_butler_config.wakeWord），不硬编码任何具体唤醒词
            # 2. 对多词拼音唤醒词自动补充"词序颠倒"变体（如 ni hao yi wa -> yi wa ni hao）
            # 3. 2026-09-21 按主人指示修改（方案 1：后台所见即所得）：
            #    不再自动把 AI 名字(ai_name)/昵称(nickname) 加入唤醒词列表。
            #    旧行为下名字"伊娃"会被自动加入，导致后台只设了 "ni hao yi wa"
            #    也会多出短词 "yi wa"，使"一晚/依哇"等环境音频繁误唤醒。
            # ========================================================================
            self.wakewords = []
            if self.wake_word:
                # 兼容中英文逗号分割
                normalized = self.wake_word.replace('，', ',')
                for word in normalized.split(','):
                    word = word.strip()
                    if word and word not in self.wakewords:
                        self.wakewords.append(word)
            # 唤醒词完全跟随后台设置（ai_butler_config.wakeWord），不硬编码具体唤醒词。
            # 仅按 AI 名字补充"名字挪到另一端"的变体（如 ni hao yi wa -> yi wa ni hao）。
            self.wakewords = self._expand_wakewords(self.wakewords, self.ai_name)

            logger.bind(tag=TAG).info(
                f"唤醒词配置加载: ai_name={self.ai_name}, nickname={self.nickname}, "
                f"wakeWord={self.wake_word}, enabled={self.enabled}, session_timeout={self.session_timeout}s, "
                f"auto_end_enabled={self.auto_end_enabled}, 主词={self.wakewords}"
            )
        except Exception as e:
            logger.bind(tag=TAG).warning(f"从数据库加载唤醒词失败: {e}, 使用默认值")
            # 2026-09-21 方案 1：兜底也不再用 AI 名字/昵称，只用默认唤醒词（ni hao yi wa）
            self.wakewords = self._expand_wakewords([self.wake_word], self.ai_name)

    def _expand_wakewords(self, base_words, ai_name=None):
        """按 AI 名字补充"名字挪到另一端"的唤醒词变体，不硬编码任何具体唤醒词。

        后台唤醒词设什么这里就是什么（主人 2026-09-17 指示）。
        例：ai_name='伊娃'、wakeWord='ni hao yi wa' -> 追加 'yi wa ni hao'
        （即"你好伊娃"和"伊娃你好"都能唤醒）。名字不在唤醒词内时不产生变体。
        """
        expanded = []
        for w in base_words:
            if w and w not in expanded:
                expanded.append(w)

        ai_name = (ai_name or self.ai_name or '').strip()
        if not ai_name:
            return expanded
        try:
            from pypinyin import lazy_pinyin

            name_parts = [p.lower() for p in lazy_pinyin(ai_name) if p.strip()]
        except Exception:
            return expanded
        if not name_parts:
            return expanded

        n = len(name_parts)
        for word in list(expanded):
            parts = word.split()
            if len(parts) <= n:
                continue
            lower_parts = [p.lower() for p in parts]
            if lower_parts[-n:] == name_parts:
                variant = ' '.join(name_parts + parts[:-n])
            elif lower_parts[:n] == name_parts:
                variant = ' '.join(parts[n:] + name_parts)
            else:
                continue
            if variant not in expanded:
                expanded.append(variant)
        return expanded

    def _build_near_sound_words(self):
        """构建近音词扩展集合（基于拼音编辑距离）"""
        self.near_sound_words = set(self.wakewords)
        logger.bind(tag=TAG).info(f"唤醒词列表: {sorted(self.near_sound_words)}")

    def _pinyin_distance(self, text, word):
        """计算文本拼音与唤醒词拼音的编辑距离（支持空格分隔的拼音词）"""
        try:
            from pypinyin import pinyin, Style
            text_pinyin = ''.join([p[0] for p in pinyin(text, style=Style.NORMAL)]).lower()
            word_no_space = word.replace(' ', '').lower()
            return self._levenshtein_distance(text_pinyin, word_no_space)
        except Exception:
            return self._levenshtein_distance(text.lower(), word.lower())

    @staticmethod
    def _levenshtein_distance(s1, s2):
        """计算编辑距离"""
        if len(s1) < len(s2):
            # 避免递归调用：交换参数直接计算
            s1, s2 = s2, s1
        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    @staticmethod
    def _similarity(s1, s2):
        """计算字符串相似度 (0.0 - 1.0)"""
        if s1 == s2:
            return 1.0
        max_len = max(len(s1), len(s2))
        if max_len == 0:
            return 0.0
        distance = WakewordDetector._levenshtein_distance(s1, s2)
        return 1.0 - (distance / max_len)

    # 剥离唤醒词时的最大迭代次数（2026-10-05 主人定案）。
    # 主人原话：「实际生活中说个两三遍，或者宽泛一点定义，肯定不会超过 5 遍」。
    # 这条上限不是防御攻击，而是 Python 的实际陷阱兜底：
    # 若唤醒词配置为空字符串，''.replace('', '', 1) 不会收敛会死循环，
    # 一行判断即可挡住，不引入额外复杂度。
    _STRIP_MAX_ROUNDS = 5

    def _pinyin_spans(self, text):
        """把文本切成元组序列
        ``[(该字拼音, 拼音串起, 拼音串止, 原文起, 原文止), ...]``。

        剥离的核心工具。detect() 只回答「有没有命中」，它把整句转成一个连续
        拼音串然后做子串匹配 —— 但那之后就丢失了「命中的是原文哪一段」的
        信息，无法据此切除。本方法保留每个片段的原文下标，于是：

            '你好伊娃现在几点了'
            → '你'=('nihao',0,5,0,1), '好'=('hao',5,8,1,2),
               '伊'=('yi',8,10,2,3),  '娃'=('wa',10,12,3,4) ...

        在拼音串上定位到 'nihaoyiwa' 占 [0,12)，据此切掉原文 [0:4]，
        剩下的 '现在几点了' 原样交回 LLM。

        拼音串下标与原文下标**必须分开记录**：个别字符经 pypinyin 转换后
        拼音长度与字符数不等（多音字、空结果），只记一个坐标会越界。

        为什么不在中文上做子串匹配：唤醒词配置存的是拼音
        （实测 wakewords=['ni hao yi wa', 'yi wa ni hao']），中文形态并不在
        配置里（_expand_wakewords 全程在拼音层面工作），用中文形态直接
        replace 永远剥不掉。而拼音→汉字的逆转换有多音字歧义
        （'nihao' 可能是 '泥豪'），也不可靠。走拼音序列是唯一稳妥路径。
        """
        try:
            from pypinyin import pinyin, Style
        except ImportError:
            return []

        spans = []
        idx = 0        # 原文下标
        py_idx = 0     # 拼音串下标
        for ch in text:
            py = ''.join(
                p[0] for p in pinyin(ch, style=Style.NORMAL)
            ).lower()
            py = re.sub(r'[^a-z0-9]', '', py)
            if py:
                spans.append((py, py_idx, py_idx + len(py), idx, idx + len(ch)))
                py_idx += len(py)
            idx += len(ch)
        return spans

    @staticmethod
    def _span_at(spans, py_pos):
        """二分查找覆盖拼音串下标 py_pos 的那个字符片段。

        spans 元素按字符排列，其 [1]=拼音起 [2]=拼音止（见 _pinyin_spans）。
        拼音串下标与数组下标不是一回事，故必须按拼音区间二分查找。
        """
        lo, hi = 0, len(spans) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s, e = spans[mid][1], spans[mid][2]
            if py_pos < s:
                hi = mid - 1
            elif py_pos >= e:
                lo = mid + 1
            else:
                return spans[mid]
        # 兜底：越界时返回最后一个片段，避免 IndexError 中断整条链路
        return spans[-1]

    def strip_wakewords(self, text):
        """从文本中剥离所有唤醒词，返回 (剩余文本, 剥离次数)。

        为什么需要（2026-10-05 主人实测后定案）
        ------------------------------------------------
        主人在唤醒未激活时连喊多遍唤醒词，VAD 会把多遍粘连成一个超长句：
            '你好，伊娃你好，伊娃你好，伊娃你好伊娃你好，伊娃。'（20 字）
        旧逻辑用**长度门槛**（len(text) <= len(词) + 3）判断「是否纯唤醒」，
        于是这段必然被判成「包含指令」，把 20 个字整个丢给 LLM。
        偏偏云端 LLM 首token 延迟实测 14.4 秒，这段时间里音频继续粘连、
        识别串扰，导致唤醒与对话双双错乱。

        规则（主人定案）
        ----------------
        把唤醒词逐个剥离干净：
            '你好伊娃你好伊娃你好伊娃你好伊娃你好伊娃' → ''      → 纯唤醒
            '你好伊娃你好伊娃现在几点了'                → '现在几点了' → 有指令
        剥完只剩标点和空白即视为「纯唤醒」，不再喂给 LLM。

        实现要点
        --------
        1. **必须在原文上切**，不能只比拼音 —— 切完才能把中文原文交回 LLM。
        2. 一遍 replace 只能去掉一个，必须循环剥离。
        3. 返回前用正则只保留中文/字母/数字，等价于「去标点后是否为空」，
           这样 '你好，伊娃你好，伊娃。' 也能正确判为空。
        4. 剥离本身是纯字符串操作，实测 0.0008ms/次，比 detect() 已有的
           拼音转换（0.065ms）还便宜两个数量级，不引入可感知延迟。

        返回：``(stripped_text, count)``。count == 0 表示没剥掉任何唤醒词。
        """
        if not text:
            return "", 0

        variants = [w for w in (getattr(self, "wakewords", []) or []) if w]
        if not variants:
            return text, 0

        # 与 detect() 完全一致的拼音规范化口径，否则剥不掉
        try:
            from pypinyin import pinyin, Style
        except ImportError:
            return text, 0

        targets = []
        for w in variants:
            if any('一' <= c <= '鿿' for c in w):
                pw = ''.join(
                    p[0] for p in pinyin(w, style=Style.NORMAL)
                ).lower()
            else:
                pw = w.replace(' ', '').lower()
            pw = re.sub(r'[^a-z0-9]', '', pw)
            if pw:
                targets.append(pw)
        if not targets:
            return text, 0

        # 长词优先：先剥 'nihaoyiwa' 再剥 'yiwa'，避免长词被切碎后匹配不到
        targets.sort(key=len, reverse=True)

        rest = text
        count = 0
        for _ in range(self._STRIP_MAX_ROUNDS):
            spans = self._pinyin_spans(rest)
            if not spans:
                break
            joined = ''.join(s[0] for s in spans)

            cut = []          # 本轮要切掉的原文区间 [(start, end), ...]
            hit_any = False
            for t in targets:
                start = 0
                while True:
                    pos = joined.find(t, start)
                    if pos < 0:
                        break
                    # 拼音串区间 [pos, pos+len(t)) → 原文区间。
                    # 注意：不能用 pos+len(t)-1 直接索引 spans，
                    # 因为 spans 是「按字符」的元素个数，与拼音串长度不等。
                    # 用二分找到覆盖该拼音区间的首尾字符。
                    lo = self._span_at(spans, pos)[3]
                    hi = self._span_at(spans, pos + len(t) - 1)[4]
                    # 不与本轮已切区间重叠
                    if not any(not (hi <= a or lo >= b) for a, b in cut):
                        cut.append((lo, hi))
                        hit_any = True
                    start = pos + 1

            if not hit_any:
                break  # 已剥干净

            # 从后往前切，避免前面的切除影响后面的下标
            for lo, hi in sorted(cut, reverse=True):
                rest = rest[:lo] + " " + rest[hi:]
            count += len(cut)

        # 去标点与空白：只有中文/字母/数字才算「还有内容」
        remainder = re.sub(r"[^\u4e00-\u9fff a-zA-Z0-9]", "", rest).strip()
        return remainder, count

    def detect(self, text):
        """
        检测文本中是否包含唤醒词

        ========================================================================
        禁止修改：唤醒词检测核心逻辑
        功能：检测输入文本是否匹配唤醒词（支持中文和拼音形式）
        负责人：bot-eva
        修改需获得 bot-eva 同意
        实现要点：
        1. 将输入文本和唤醒词都转换为拼音（移除空格、无音调）
        2. 支持中文输入（如"小不点"）和拼音输入（如"xiao bu dian"）
        3. 支持精确匹配和近似匹配（编辑距离）
        4. 不要修改拼音转换和空格处理逻辑，会导致匹配失败
        ========================================================================

        所有匹配都基于拼音：
        1. 如果唤醒词是拼音形式，与文本转拼音后的结果比对
        2. 如果唤醒词是中文形式，也统一转成拼音比对
        3. 支持近似匹配：拼音编辑距离不超过阈值

        Returns:
            tuple: (is_wakeword, matched_word, confidence)
        """
        if not text:
            return False, None, 0.0

        try:
            from pypinyin import pinyin, Style
        except ImportError:
            return False, None, 0.0

        # ====================================================================
        # 禁止修改：拼音转换和空格处理逻辑
        # 原因：控制系统中的唤醒词格式为 "xiao bu dian"（有空格）
        #       但 pypinyin 对中文输入生成无空格拼音 "xiaobudian"
        #       必须统一移除空格才能正确匹配
        # ====================================================================
        # 将输入文本转为连续拼音（无空格、无音调）
        text_pinyin = ''.join([p[0] for p in pinyin(text, style=Style.NORMAL)]).lower()
        # 移除所有空格（支持拼音形式的输入，如 "xiao bu dian"）
        text_pinyin = text_pinyin.replace(' ', '')
        # 【2026-10-01 主人批准修复】清洗标点：ASR 输出带「。」「，」等标点会被 pypinyin
        # 原样带入拼音串，污染编辑距离（如「你好一吧。」距离=2 无法命中，去标点后距离=1 命中）。
        # 仅保留 a-z0-9，不改变拼音转换本身。
        text_pinyin = re.sub(r'[^a-z0-9]', '', text_pinyin)
        if not text_pinyin:
            return False, None, 0.0

        best_match = None
        best_confidence = 0.0

        for word in self.wakewords:
            # ====================================================================
            # 禁止修改：唤醒词拼音转换逻辑
            # 原因：必须与输入文本的处理方式保持一致（移除空格）
            # ====================================================================
            # 把唤醒词也转成连续拼音
            if any('一' <= c <= '鿿' for c in word):
                word_pinyin = ''.join([p[0] for p in pinyin(word, style=Style.NORMAL)]).lower()
            else:
                word_pinyin = word.replace(' ', '').lower()
            # 【2026-10-01 主人批准修复】与输入文本一致：清洗标点（防止后台唤醒词里带标点）
            word_pinyin = re.sub(r'[^a-z0-9]', '', word_pinyin)

            if not word_pinyin:
                continue

            # 1. 精确包含：唤醒词拼音是文本拼音的子串
            if word_pinyin in text_pinyin:
                confidence = min(len(word_pinyin) / len(text_pinyin) * 1.5, 1.0)
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_match = word
                    logger.bind(tag=TAG).info(
                        f"唤醒词精确匹配: '{text}' (拼音: {text_pinyin}) -> '{word}' (拼音: {word_pinyin}), "
                        f"confidence={best_confidence:.2f}"
                    )
                continue

            # 2. 拼音编辑距离近似匹配（兜底，用于轻微口音/ASR 误差）
            # 约束：前两个字符必须一致，且相对偏差不超过 10%
            # 【2026-10-13 主人批准修改（禁改区负责人 bot-eva 本人裁决）】
            # 容错下限从 1 放宽到 2 个字母：远场+音箱外放回声场景下，本地
            # SenseVoice 把「伊娃」识别成「伊文/伊凡」（拼音距离 2），容错 1
            # 时命中率从 6/7 跌到 0/4（Mini 22:26 后实测）。主人确认接受
            # 误唤醒略微上升的代价，换取唤醒命中率。
            max_distance = max(2, int(len(word_pinyin) * 0.1))
            window_start = 0
            window_end = min(len(text_pinyin), len(word_pinyin) + max_distance)

            while window_end <= len(text_pinyin):
                substring = text_pinyin[window_start:window_end]
                if len(substring) < 2 or substring[:2] != word_pinyin[:2]:
                    window_start += 1
                    window_end += 1
                    continue

                distance = self._levenshtein_distance(substring, word_pinyin)
                if distance <= max_distance:
                    confidence = max(0.5, 1.0 - (distance / len(word_pinyin)))
                    if confidence > best_confidence:
                        best_confidence = confidence
                        best_match = word
                        logger.bind(tag=TAG).info(
                            f"唤醒词拼音近似匹配: '{text}' (拼音: {text_pinyin}) -> '{word}' (拼音: {word_pinyin}), "
                            f"distance={distance}, confidence={confidence:.2f}"
                        )
                    break

                window_start += 1
                window_end += 1

        if best_match:
            return True, best_match, best_confidence

        # 2026-09-29 诊断补记：未命中时打出实际拼音与目标词列表，
        # 避免"识别对了却莫名不激活"无从定位（今天排查主诉求）
        logger.bind(tag=TAG).info(
            f"唤醒词未命中: '{text}' (拼音: {text_pinyin}) | 目标词拼音: "
            f"{[w.replace(' ', '').lower() if not any('一' <= c <= '鿿' for c in w) else ''.join([p[0] for p in pinyin(w, style=Style.NORMAL)]).lower() for w in self.wakewords]}"
        )
        return False, None, 0.0

    def is_session_active(self):
        """检查当前是否处于唤醒会话状态"""
        if not self.enabled:
            return False
        return time.time() < self.session_active_until

    def activate_session(self):
        """激活唤醒会话（设置会话结束时间）"""
        if self.enabled:
            self.session_active_until = time.time() + self.session_timeout
            logger.bind(tag=TAG).info(f"唤醒会话激活，将在 {self.session_timeout} 秒后超时")

    def extend_session(self):
        """延长会话时间（在AI回复后调用）"""
        if self.enabled and self.is_session_active():
            self.session_active_until = time.time() + self.session_timeout
            logger.bind(tag=TAG).info(f"唤醒会话延长 {self.session_timeout} 秒")

    def deactivate_session(self):
        """停用会话"""
        self.session_active_until = 0
        logger.bind(tag=TAG).info("唤醒会话已停用")


# 全局单例
_wakeword_detector = None


def get_wakeword_detector(db_path=None):
    """获取全局唤醒词检测器（单例）"""
    global _wakeword_detector
    if _wakeword_detector is None:
        _wakeword_detector = WakewordDetector(db_path)
    return _wakeword_detector


def reload_wakeword_detector():
    """重新加载唤醒词配置"""
    global _wakeword_detector
    _wakeword_detector = None
    return get_wakeword_detector()
