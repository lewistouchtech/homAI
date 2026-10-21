#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@file: provider.py
@desc: mem0 记忆提供者 - HomeBrain AI 管家

mem0 是纯库形态（跑在 HomeBrain 主进程内），与 OpenViking 的服务形态不同：
不需要独立服务、不需要管端口健康。

选型依据（2026-10-06）：
- OpenViking 实测 `find()` 恒返回 0 条（上游 issue #4482），记忆能写不能用
- mem0 中文实测可召回，检索延迟 13~22ms，接本地服务无需外部 key

════════════════════════════════════════════════════════════════════
2026-10-13 大修（WP-1/2/3/4，方案见 docs/plans/2026-10-13_记忆系统调整方案.md）
════════════════════════════════════════════════════════════════════
体检发现的四个 P0/P1 问题及本次修法：

① **身份被全局改写**（P0，真跑复现）
   本 provider 是 websocket_server 的**单例**，所有连接共用。原来
   `init_memory(role_id=device_id)` 会执行 `self.user_id = role_id`：
       init_memory("web-terminal-56ops5ll") → user_id = web-terminal-56ops5ll
       init_memory("admin-console")         → user_id = admin-console
   → 管理台 WS 一连，所有终端的查询与抽取全部换成 admin-console。
   库里 user_id 因此裂成 5 种（admin-console 27 条，还有 1 条是会话 UUID）。
   **修法**：init_memory 不再写 self.user_id；scope 由调用方按「说话人」传入，
   并在**入队那一刻**固定下来（见 core/utils/memory_scope.py）。

② **检索阈值方向写反**（P0，实验确认）
   chroma 返回的 `score` 是**距离**（越小越近），原来写
   `if score < min_confidence: continue` —— 等于丢掉最相关的、留下最不相关的，
   而且没有任何相关性门槛（每轮注入约 5 条近似随机的记忆）。
   实验：问「红烧大虾的菜谱」，距离 0.23 的正确命中被丢，距离 1.29~1.43 的
   4 条无关记忆（「帮我客厅汤」「讨论关于运行hobra的本体」）全部保留。
   **修法**：距离换算成相似度（L2 归一化向量 sim = 1 − d/2；cosine sim = 1 − d），
   按 `min_similarity` 过滤。

③ **抽取用的是 mem0 英文默认提示词**（P0）
   原来只传了 custom_update_memory_prompt，没传 custom_fact_extraction_prompt。
   mem0 默认提示词是英文「Personal Information Organizer」，few-shot 把每个请求
   都当事实（"Looking for a restaurant in San Francisco"），于是库里出现
   `Asked to open health report`、`Requested a joke`、`Prefers jokes`；
   160 条里 85 条被改写成英文；「AI 叫汤圆」被记成主人的名字。
   **修法**：中文家庭场景提示词 + 说话人前缀 + 助手上一句作消歧上下文 +
   前置过滤升级 + 未识别说话人的敏感类前置门。

④ **写入会丢、会重、会漏**（P1）
   原 `_extract_now()` 先清空缓冲再 `mem.add()`，失败整批丢失；
   `_save_and_close` 关闭时整段对话再提交一遍（现网 28 条完全重复）；
   实时保存取 `[-2:]`，工具调用轮次末两条是 tool+assistant → user 消息丢失。
   **修法**：改用 SQLite outbox（core/utils/memory_outbox.py），msg_id 幂等、
   失败退避重试、崩溃恢复、死信可查；计数按 mem0 真实返回统计。

契约与 openviking/provider.py 保持一致，切换只需改 config 的 Memory.type。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config.logger import setup_logging
from core.providers.memory.base import BaseMemoryProvider

logger = setup_logging()
TAG = __name__

# 噪音过滤：太短、纯客套、纯标点的一律不记（与 openviking provider 同口径）
MIN_USER_FACT_CHARS = 4
# 单条待抽取消息的字数上限（2026-10-12）：LM Studio 的 llama-server 是 ctx 8192 / parallel 4，
# 每个槽位只有 2048 token；抽取提示词约 1.3K，4 条长消息一塞就 `Context size has been exceeded`
MAX_FACT_MSG_CHARS = 200
# 助手上一句（消歧上下文）的字数上限
MAX_ASSISTANT_CTX_CHARS = 60
# 一批消息合计字数上限：超了就自动切小批，保证 提示词+输入+输出 不超槽位上下文
MAX_BATCH_CHARS = 700
# 攒批积压上限的默认值（legacy 写入模式）
DEFAULT_MAX_BACKLOG = 24
# 兼容旧引用名
MAX_BACKLOG = DEFAULT_MAX_BACKLOG
# 错峰参数（2026-10-12）：前台安静多久才开始抽取 / 最多等多久（到点也放行，避免永远不抽）
EXTRACT_IDLE_SECONDS = 30.0
EXTRACT_MAX_WAIT_SECONDS = 600.0
BATCH_GAP_IDLE_SECONDS = 10.0  # 批与批之间：前台又忙起来就让路
_EXTRACT_LOCK = threading.Lock()  # 全局同一时刻只跑一个抽取，避免多批/多会话并发抢 GPU

# ── 2026-10-13 新增：写入可靠性 ──
OUTBOX_POLL_SECONDS = 20.0        # worker 轮询间隔
OUTBOX_BATCH_LIMIT = 8            # 单次最多取几条
CIRCUIT_FAIL_THRESHOLD = 3        # 连续失败几次就熔断
CIRCUIT_PAUSE_SECONDS = 600.0     # 熔断时长（LM Studio 卸载模型期间不要疯狂重试）
EPISODE_DUP_SIMILARITY = 0.90      # 整理出的「情节记忆」与已有记忆的向量相似度超过就视为重复
DEDUP_SIMILARITY = 0.86           # 与已有记忆的文本相似度超过就跳过（去重）
ERROR_BODY_LIMIT = 200            # 错误响应体安全截断长度（便于 502 归因，不落敏感内容）

_NOISE_PATTERNS = re.compile(
    r"^(嗯+|啊+|哦+|哈+|谢谢|不客气|好的?|收到|明白|知道了|再见|"
    r"你好|您好|请问|麻烦|辛苦了?|ok|OK|yes|no)+[。，！!？?~、\s]*$"
)

# ── 2026-10-13 新增：前置过滤（原来只挡「<4 字」和「纯客套」，太松）──
# 现网 160 条记忆里大量是这类一次性指令：「打开新闻」「跳到第十部」「下一条」
# 「打开主入口摄像头」「播放第一条」。它们不是关于家庭的事实，记了就是污染。
_COMMAND_PATTERN = re.compile(
    r"^(帮我|给我|请|麻烦)?(打开|关闭|关掉|开启|播放|放一下|放个|放首|听|看|"
    r"调高|调低|调大|调小|切换|换一|换成|跳到|跳转|下一|上一|暂停|继续|停止|重来|"
    r"刷新|搜索|搜一下|查一下|查询|显示|展示|生成|"
    # ⚠️ 「第N」必须跟量词才算指令（下一条/第3首/第十部）。
    # 不能只写 `第[一二三…0-9]+`：会误杀「第一条事实」这类正常陈述
    # （我第一版就踩了，单测当场抓到）。
    r"第[一二三四五六七八九十百0-9]+(?=[个首集章页部曲张段篇]))"
)
# 个人陈述的标志：命中任一就认为「这句里有关于人的信息」，即使以命令词开头也保留
_PERSONAL_MARKERS = (
    "我", "我们", "我家", "我爸", "我妈", "我老婆", "我老公", "我儿子", "我女儿",
    "喜欢", "讨厌", "不爱", "偏好", "习惯", "过敏", "忌口", "生日", "记得", "以前",
    "小时候", "上次", "之前", "打算", "计划", "准备", "想", "需要", "不要", "别再",
)
# ASR 乱码特征：同一个字连续重复（现网实例「长12天，完了这这个这个这个过道」）
_GARBAGE_REPEAT = re.compile(r"(.)\1{3,}")


def _msg_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "user")
    return str(getattr(msg, "role", "user"))


def _extract_content(msg: Any) -> str:
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, list):  # 多模态内容块
            parts = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
            return "".join(parts)
        return str(c or "")
    return str(getattr(msg, "content", "") or "")


def _unwrap_speaker_json(text: str) -> str:
    """语音链路的用户消息是 {"speaker": "李威", "content": "播放新闻。"} 这种 JSON。

    原来指令过滤直接拿整串去匹配「^播放…」，以 `{` 开头永远不命中，
    「播放今天的新闻」「继续修复未完成的」这类指令全被当事实送去抽取（2026-10-13 实测）。
    必须先取出 content 再过滤、再入队。
    """
    t = (text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
        except (ValueError, TypeError):
            return t
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            return data["content"].strip()
    return t


def _is_garbage_asr(text: str) -> bool:
    """疑似 ASR 乱码：同字连续重复 4 次以上，或「无标点且中英数字混杂的长串」。"""
    if _GARBAGE_REPEAT.search(text):
        return True
    # 长句里一个标点都没有、且不是完整中文语义（现网实例「我得回去但还出来呀」
    # 这类交给抽取模型判，不在这里硬挡，避免误杀口语）
    return False


def _is_worth_remembering(role: str, text: str) -> bool:
    """过滤掉不值得记的消息（2026-10-13 升级）。

    原来只有「<4 字」+「纯客套」两道，结果现网 160 条记忆里一大半是
    一次性指令和 ASR 乱码。这里加两道：
    - 命令式开头且**不含任何个人陈述标志** → 是操作指令，不是家庭事实
    - 疑似 ASR 乱码
    """
    t = (text or "").strip()
    if len(t) < MIN_USER_FACT_CHARS:
        return False
    if _NOISE_PATTERNS.match(t):
        return False
    if _is_garbage_asr(t):
        return False
    if _COMMAND_PATTERN.match(t) and not any(m in t for m in _PERSONAL_MARKERS):
        return False
    return True


# ══════════════════════════════════════════════════════════════════
# 中文家庭场景抽取提示词（2026-10-13 WP-2 步骤 A）
#
# 为什么必须自定义：mem0 默认提示词是英文「Personal Information Organizer」，
# few-shot 明确把「用户的每个请求」都当事实记（"Looking for a restaurant in
# San Francisco" / "Had a meeting with John at 3pm"），并且要求 "Track Plans
# and Intentions"。结果现网库里全是 `Asked to open health report`、
# `Requested a joke`、`Prefers jokes` 这类一次性动作，160 条里 85 条还是英文。
#
# 输入格式约定（见 _build_payload）：
#   （说话人：李威）今天去医院复查了，医生开了降压药
#   （助手回应，仅供理解语境，不是事实来源）好的，我记下来了
# ══════════════════════════════════════════════════════════════════
_CUSTOM_FACT_PROMPT_TEMPLATE = """你是「家庭信息整理员」，为家庭智能管家 HomeBrain 从对话里提取**值得长期记住的家庭事实**。

【输出要求】
- 只输出 JSON：{{"facts": ["...", "..."]}}，不要输出任何解释、前言或 markdown。
- **全部用中文**，禁止把中文事实改写成英文。
- 每条事实是**完整独立的一句话**，主语用说话人的姓名或称呼（如「李威喜欢喝美式咖啡」），
  不要用「用户」「他」「我」这类指代。
- 相对时间必须换算成绝对日期（今天是 {today}）。例如「昨天去了医院」→「{today_minus_1} 去了医院」。
- 一条事实只讲一件事，不要把多件事合并成长句。

【只记这些】
1. 家庭成员的身份与关系：姓名、称呼、生日、职业、住在哪、家庭成员之间的关系。
2. 稳定的偏好与禁忌：口味、忌口、过敏、喜欢的音乐/节目/品牌、讨厌的东西、生活习惯。
3. 健康情况：慢性病、正在吃的药、就诊安排、身体状况（**只在说话人明确说自己时才记**）。
4. 重要日期与固定安排：纪念日、每周固定的事、接送时间。
5. 明确的计划与意图：带日期的出行、要办的事、要买的东西（写成「李威计划 X 月 X 日 …」）。
6. 购物偏好：常买的品牌、规格、预算习惯、在哪儿买。
7. 用户对既有事实的**明确纠正**：如「不是 A，是 B」「以后别叫我 X」，必须原样保留纠正后的说法。

【绝不记这些 —— 命中任一条就返回 {{"facts": []}}】
- 对设备/系统的操作指令：开灯关灯、播放暂停、下一条、跳到第几个、打开某页面、查天气、调音量。
- 向助手提出的提问或要求（「帮我查一下」「这是什么意思」「你再说一遍」）。
- 讨论系统/模型/代码/接口/部署/调试的内容（这是开发者对话，不是家庭生活）。
- 工具或环境返回的信息：天气、新闻内容、设备列表、订单号、时间戳、报错信息。
- **助手自己的名字、昵称、人设、能力**（例如「AI 叫汤圆」「你叫伊娃」——这是助手的信息，
  不是家庭成员的信息，绝不能记成某个人的名字或偏好）。
- 一次性寒暄、语气词、无实质内容的短句（「好的」「在吗」「随便」）。
- 语句不通、疑似语音识别错误的乱码（如「长12天，完了这这个这个这个过道」）。
- 对当前操作的即时反馈（「太慢了」「怎么还没好」「你控制的是哪四个设备」）。

【示例】
输入：（说话人：李威）跳到第十部
输出：{{"facts": []}}

输入：（说话人：李威）打开主入口摄像头
输出：{{"facts": []}}

输入：（说话人：李威）帮我查一下今天的天气
输出：{{"facts": []}}

输入：（说话人：李威）AI 叫汤圆，住在家里
输出：{{"facts": []}}

输入：（说话人：李威）我得回去但还出来呀
输出：{{"facts": []}}

输入：（说话人：李威）长12天，完了这这个这个这个过道
输出：{{"facts": []}}

输入：（说话人：李威）我讨厌香菜，以后做菜别放
输出：{{"facts": ["李威讨厌香菜，做菜不要放香菜"]}}

输入：（说话人：李威）昨天去医院复查了，医生开了降压药
输出：{{"facts": ["{today_minus_1} 李威去医院复查，医生开了降压药"]}}

输入：（说话人：李威）我们家买牛奶一直认伊利
输出：{{"facts": ["这个家庭买牛奶固定选伊利品牌"]}}

输入：（说话人：李威）不是美式，我喝的是拿铁
输出：{{"facts": ["李威喝咖啡喝拿铁，不是美式"]}}

输入：（说话人：李威）下周六我们全家去顺义爬山
输出：{{"facts": ["李威一家计划下周六去顺义爬山"]}}

以下是需要处理的对话。括号里的「助手回应」只用于理解语境（例如判断「他」指谁、
「那个」指什么），**助手说的话本身不是事实来源，绝不能把助手的内容记成家庭事实**。
今天是 {today}。
"""


_ONE_DAY = timedelta(days=1)


def _build_fact_prompt() -> str:
    today = datetime.now()
    return _CUSTOM_FACT_PROMPT_TEMPLATE.format(
        today=today.strftime("%Y-%m-%d"),
        today_minus_1=(today - _ONE_DAY).strftime("%Y-%m-%d"),
    )


def _similarity_to_score(raw: Any) -> Optional[float]:
    """把向量库返回的 score 换算成「相似度」（越大越相关）。

    ⚠️ 2026-10-13 修正：chroma 的 `score` 是**距离**，越小越近
    （mem0/vector_stores/chroma.py 里字段注释就写着 `# distance`）。
    原来直接 `score < min_confidence` 过滤，方向是反的：
    实验里问「红烧大虾的菜谱」，距离 0.23 的正确命中被丢掉，
    距离 1.29~1.43 的 4 条无关记忆全部保留。

    换算：
    - L2（chroma 默认空间，向量已归一化）：d² = 2(1 − cos) → sim = 1 − d/2
    - cosine 空间（`hnsw:space=cosine`，d ∈ [0,2]）：sim = 1 − d
    两者在 d ∈ [0,2] 区间用同一个公式即可，超出区间按边界裁剪。
    """
    if raw is None:
        return None
    try:
        d = float(raw)
    except (TypeError, ValueError):
        return None
    if d < 0:
        return None
    d = min(d, 2.0)
    return 1.0 - d / 2.0


class Mem0Provider(BaseMemoryProvider):
    """mem0 本地记忆提供者。

    同步 SDK 调用（mem0 是同步库）统一走 ``asyncio.to_thread``，
    避免阻塞 HomeBrain 的语音主链路。
    """

    name = "mem0"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        # ⚠️ 2026-10-13：user_id 只作「没传 scope 时的兜底」，**不再被 init_memory 改写**。
        # 真实归属由调用方按说话人传 scope（见 core/utils/memory_scope.py）。
        self.user_id = str(cfg.get("user_id") or "homebrain")
        # agent_id：mem0 记忆命名空间。默认 "1"（主人 2026-10-06 指定，
        # 不用 "eva" —— 助理名会变，命名空间变了等于旧记忆全部读不到）。
        self.agent_id = str(cfg.get("agent_id") or "1")
        self.retrieve_limit = int(cfg.get("retrieve_limit", 5))
        # 相似度门槛（越大越严格）。旧配置项 min_confidence 兼容读取。
        # 默认 0.55：实验里相关命中换算后约 0.57~0.72，无关的在 0.28~0.55。
        # 最终值由 WP-6 黄金集标定，不拍脑袋。
        self.min_similarity = float(
            cfg.get("min_similarity", cfg.get("min_confidence", 0.55))
        )
        # scope 模式：v2 = 按说话人分 family / m:<id>（主人 2026-10-13 裁决 D1）
        #            legacy = 恢复原「user_id = device_id」行为（回滚用）
        self.scope_mode = str(cfg.get("scope_mode", "v2")).lower()
        # 写入模式：outbox = 落盘队列（默认，可靠）；legacy = 内存缓冲（回滚用）
        self.write_mode = str(cfg.get("write_mode", "outbox")).lower()
        # 是否启用中文自定义抽取提示词（关掉就回落 mem0 英文默认提示词，便于 A/B）
        self.use_custom_fact_prompt = bool(cfg.get("custom_fact_prompt", True))

        # 端点（mem0 只支持 OpenAI 兼容接口）。
        # base_url/api_key 是历史字段，等价于「LLM 与 embedding 同端点」；
        # 2026-10-06 起两者分家：抽取用 LM Studio（记忆专用），embedding 留 omlx。
        self.base_url = str(cfg.get("base_url") or "http://localhost:8001/v1")
        self.api_key = str(cfg.get("api_key") or "omlx-local")
        self.llm_base_url = str(cfg.get("llm_base_url") or self.base_url)
        self.llm_api_key = str(cfg.get("llm_api_key") or self.api_key)
        self.embed_base_url = str(cfg.get("embed_base_url") or self.base_url)
        self.embed_api_key = str(cfg.get("embed_api_key") or self.api_key)
        self.llm_model = str(cfg.get("llm_model") or "qwen3-8b@q4_k_m")
        self.embed_model = str(cfg.get("embed_model") or "Qwen3-Embedding-0.6B-8bit")
        self.embedding_dims = int(cfg.get("embedding_dims", 1024))
        # 是否给 mem0 打「关思考」补丁（Qwen3 系列必须，见 llm_patch.py）
        self._disable_thinking = bool(cfg.get("disable_thinking", True))
        # ⚠️ 抽取时的输出上限。mem0 默认 2000 不够（2026-10-06 实测）：
        # 模型会先写一段说明再输出 JSON，2000 token 在 JSON 写完前就用完 →
        # 60 条事实的批量写入只成功 34/60。提高到 4096 给足余量。
        self._llm_max_tokens = int(cfg.get("llm_max_tokens", 4096))
        # 可选：自定义「更新记忆」提示词（mem0 用它决定 ADD/UPDATE/DELETE）
        self._custom_update_prompt = cfg.get("custom_update_memory_prompt")
        self.auto_commit = bool(cfg.get("auto_commit", True))
        # 分批提交的批大小：实测 5 条正常、8 条会因 LLM 输出 JSON 截断而整批丢失
        self._batch_size = max(1, int(cfg.get("batch_size", 4)))
        # 攒够多少条才触发抽取（legacy 写入模式用；outbox 模式由 worker 定时取）
        self._flush_threshold = max(1, int(cfg.get("flush_threshold", 4)))
        # 待抽取积压上限（legacy 模式）
        self._max_backlog = max(4, int(cfg.get("max_backlog", DEFAULT_MAX_BACKLOG)))

        self._mem = None
        self._init_lock = threading.Lock()
        # legacy 写入模式的内存缓冲：每个来源（device_id）独立记账
        self._session_msgs: Dict[str, List[Dict[str, Any]]] = {}
        # outbox
        self._outbox = None
        self._worker_task: Optional[asyncio.Task] = None
        self._worker_loop: Optional[asyncio.AbstractEventLoop] = None
        self._consecutive_failures = 0
        self._circuit_until = 0.0
        # 观测计数（2026-10-13：不再拿「输入消息数」冒充抽取数）
        self.counters: Dict[str, int] = {
            "enqueued": 0, "extracted": 0, "created": 0, "updated": 0,
            "deduped": 0, "empty": 0, "failed": 0, "skipped_command": 0,
            "skipped_sensitive": 0,
        }

    # ------------------------------------------------------------------ 初始化

    def _build_config(self) -> dict:
        """mem0 1.0.0 的配置格式为 {provider, config:{...}}。

        注意参数必须放在 ``config`` 子字典里，否则 openai client 收不到 api_key
        会抛 Missing credentials。
        """
        built = {
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "path": str(self._store_dir()),
                    "collection_name": "homebrain",
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.llm_model,
                    "api_key": self.llm_api_key,
                    "openai_base_url": self.llm_base_url,
                    "max_tokens": self._llm_max_tokens,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": self.embed_model,
                    "api_key": self.embed_api_key,
                    "openai_base_url": self.embed_base_url,
                    "embedding_dims": self.embedding_dims,
                },
            },
        }
        # ⚠️ 2026-10-13 WP-2：中文家庭场景抽取提示词。
        # 原来只传了 update 提示词，抽取一直用 mem0 的英文默认提示词 ——
        # 这是「160 条记忆里 85 条英文、大量一次性指令、AI 名字记成主人名字」的根因。
        if self.use_custom_fact_prompt:
            built["custom_fact_extraction_prompt"] = _build_fact_prompt()
        if self._custom_update_prompt:
            built["custom_update_memory_prompt"] = self._custom_update_prompt
        return built

    def _store_dir(self):
        """向量库持久化目录。

        ⚠️ 2026-10-06 实测：**不能用 faiss**。mem0 1.0.0 的 faiss 后端有
        index_to_id 映射丢失的 bug——7条记忆只存下 3 条映射，搜到向量却取不到
        文本被静默丢弃。改用 chromadb 后同样数据 7/7 全部召回。
        """
        from pathlib import Path
        return Path.home() / ".mem0" / "homebrain_chroma"

    def _ensure_mem(self):
        """懒加载 mem0 实例（构造会读本地向量库，不要放在 __init__ 里）。"""
        if self._mem is not None:
            return self._mem
        with self._init_lock:
            if self._mem is not None:
                return self._mem
            if not self.enabled:
                return None
            try:
                from mem0 import Memory

                from core.providers.memory.mem0.llm_patch import patch_openai_llm

                # 必须在 Memory.from_config 之前打：补丁包的是 OpenAILLM.__init__，
                # 而实例是在 from_config 内部构造的。
                patch_openai_llm(disable_thinking=self._disable_thinking)

                self._store_dir().parent.mkdir(parents=True, exist_ok=True)
                self._mem = Memory.from_config(self._build_config())
                logger.bind(tag=TAG).info(
                    f"mem0 初始化完成（LLM={self.llm_model}@{self.llm_base_url} "
                    f"embed={self.embed_model}@{self.embed_base_url} "
                    f"scope_mode={self.scope_mode} write_mode={self.write_mode} "
                    f"中文抽取提示词={'开' if self.use_custom_fact_prompt else '关'}）"
                )
                return self._mem
            except Exception as e:
                logger.bind(tag=TAG).error(f"mem0 初始化失败，记忆功能降级: {e}")
                return None

    def _ensure_outbox(self):
        """懒加载 outbox，并把上次进程没跑完的任务恢复成 pending（崩溃恢复）。"""
        if self._outbox is not None:
            return self._outbox
        try:
            from core.utils.memory_outbox import MemoryOutbox

            self._outbox = MemoryOutbox()
            self._outbox.restore_in_flight()
            return self._outbox
        except Exception as e:
            logger.bind(tag=TAG).error(f"记忆待办队列初始化失败，退回内存缓冲: {e}")
            self.write_mode = "legacy"
            return None

    def init_memory(self, role_id, llm, summary_memory=None, save_to_file=True, **kwargs):
        """绑定角色。

        ⚠️⚠️ 2026-10-13 WP-1：**不再执行 `self.user_id = role_id`**。

        本 provider 是 websocket_server 的单例，所有连接共用；原来这一行会让
        「最后一个连上的设备」决定全家的记忆归属（真跑复现：管理台 WS 一连，
        所有终端的查询与抽取都变成 admin-console）。
        role_id（= device_id）现在只用于日志与 metadata。

        legacy 模式（scope_mode=legacy）保留原行为，供回滚对比。
        """
        self.llm = llm
        self.save_to_file = save_to_file
        self._device_hint = str(role_id or "")
        if self.scope_mode == "legacy" and role_id:
            self.user_id = str(role_id)
        self._ensure_mem()
        if self.write_mode == "outbox":
            self._ensure_outbox()
        logger.bind(tag=TAG).debug(
            f"mem0 绑定设备 {self._device_hint}（scope_mode={self.scope_mode}，"
            f"记忆归属由说话人决定，不再由设备号决定）"
        )

    def is_healthy(self) -> bool:
        return self._ensure_mem() is not None

    # ------------------------------------------------------------------ scope

    def _resolve_scope(self, speaker: Optional[str], explicit: Optional[str]) -> str:
        """确定这批/这次记忆归属哪个 scope。"""
        if explicit:
            return str(explicit)
        if self.scope_mode != "v2":
            return self.user_id
        try:
            from core.utils.memory_scope import resolve_scope

            return resolve_scope(speaker)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"scope 解析失败，按家庭共享处理: {e}")
            return "family"

    @staticmethod
    def _speaker_known(speaker: Optional[str]) -> bool:
        """说话人是否被声纹可靠识别（决定敏感类能不能写）。"""
        if not speaker:
            return False
        try:
            from core.utils.memory_scope import member_id_of

            return member_id_of(speaker) is not None
        except Exception:
            return False

    def _build_payload(self, text: str, speaker: Optional[str],
                       assistant_ctx: Optional[str]) -> str:
        """拼抽取输入：说话人前缀 + 原话 + 助手上一句（消歧上下文）。

        ⚠️ 为什么需要助手上一句：现网库里出现 `Name is 汤圆`、
        `Prefers to be called by their name`、`Resides in 静家` ——
        那是主人说「AI 叫汤圆」，抽取模型没有语境，把**助手的名字**记成了
        **主人的名字**。给它助手上一句 + 提示词里明确「助手的话不是事实来源」，
        就能挡住这类错记。

        ⚠️ 仍然只用 user role 提交：mem0 1.0.0 见到 assistant 消息且带 agent_id
        会走「agent 记忆抽取」分支，对「assistant 只是简单确认」的对话会判定
        整批不值得记，返回空数组（2026-10-06 实测 4 条含 1 条 assistant → 0 条）。
        """
        head = f"（说话人：{speaker}）" if speaker else "（说话人：未识别）"
        body = text[:MAX_FACT_MSG_CHARS]
        if assistant_ctx:
            ctx = str(assistant_ctx).strip().replace("\n", " ")[:MAX_ASSISTANT_CTX_CHARS]
            if ctx:
                return f"{head}{body}\n（助手回应，仅供理解语境，不是事实来源）{ctx}"
        return f"{head}{body}"

    # ------------------------------------------------------------------ 写入

    async def save_memory(self, msgs, session_id=None, *, scope: Optional[str] = None,
                          speaker: Optional[str] = None,
                          assistant_ctx: Optional[str] = None,
                          device_id: Optional[str] = None,
                          turn: Any = None) -> Optional[int]:
        """把值得记的对话内容写入 mem0。

        参数（2026-10-13 扩展，全部关键字可选，老调用方不受影响）：
        - scope：记忆归属（``family`` / ``m:<id>``）。不传则按 msgs 里的说话人解析。
        - speaker：声纹识别出的说话人姓名。
        - assistant_ctx：助手上一句，只作消歧上下文。
        - device_id / turn：幂等键与 metadata 用。
        """
        if not msgs or not getattr(self, "save_to_file", True):
            return None
        if self._ensure_mem() is None:
            return None

        # 从消息里兜底解析说话人/助手上下文（调用方没显式传时）
        if speaker is None or assistant_ctx is None:
            for msg in msgs:
                role = _msg_role(msg)
                content = _extract_content(msg)
                if role == "user" and speaker is None:
                    sp = None
                    if isinstance(msg, dict):
                        sp = msg.get("speaker")
                    else:
                        sp = getattr(msg, "speaker", None)
                    if sp:
                        speaker = str(sp)
                elif role == "assistant" and assistant_ctx is None and content:
                    assistant_ctx = content

        resolved_scope = self._resolve_scope(speaker, scope)
        known = self._speaker_known(speaker)

        # 入口白名单：只收 user 消息（原因见 _flush 的历史注释：assistant 会触发
        # mem0 的 agent 分支导致整批 0 条；tool 消息会把天气/设备列表当记忆，
        # 还会撑爆 LM Studio 的 2048 token 槽位）
        picked: List[str] = []
        skipped_cmd = skipped_sensitive = 0
        for msg in msgs:
            if _msg_role(msg) != "user":
                continue
            text = _unwrap_speaker_json(_extract_content(msg))
            if not text:
                continue
            if not _is_worth_remembering("user", text):
                skipped_cmd += 1
                continue
            # D2 裁决 a：声纹未识别时，敏感类（健康/财务/儿童/个人行程）**不写**，
            # 避免把陌生人或旁人的隐私写进家庭共享记忆。
            if not known:
                try:
                    from core.utils.memory_scope import is_sensitive_text

                    if is_sensitive_text(text):
                        skipped_sensitive += 1
                        continue
                except Exception:
                    pass
            picked.append(text)

        self.counters["skipped_command"] += skipped_cmd
        self.counters["skipped_sensitive"] += skipped_sensitive
        if skipped_sensitive:
            logger.bind(tag=TAG).info(
                f"mem0 跳过 {skipped_sensitive} 条敏感内容（说话人未经声纹识别，"
                f"按裁决不写入家庭共享记忆）"
            )

        if not picked:
            # 记一笔，方便主人判断「为什么没记住」（2026-10-06 踩过：
            # 说话 4 句却一条日志都没有，分不清是门禁没放行还是被噪音过滤挡了）
            logger.bind(tag=TAG).info(
                f"mem0 本轮无新增可记内容（scope={resolved_scope}，收到 {len(msgs)} 条，"
                f"指令/噪音过滤 {skipped_cmd} 条，敏感过滤 {skipped_sensitive} 条）"
            )
            return 0

        if self.write_mode == "outbox":
            return self._enqueue_outbox(
                picked, scope=resolved_scope, speaker=speaker, speaker_known=known,
                device_id=device_id or getattr(self, "_device_hint", None),
                session_id=session_id, assistant_ctx=assistant_ctx, turn=turn,
            )
        return await self._save_legacy(
            picked, source=str(session_id or resolved_scope), scope=resolved_scope,
            speaker=speaker, assistant_ctx=assistant_ctx,
        )

    def _enqueue_outbox(self, texts: List[str], *, scope: str, speaker: Optional[str],
                        speaker_known: bool, device_id: Optional[str],
                        session_id: Optional[str], assistant_ctx: Optional[str],
                        turn: Any) -> int:
        """写入落盘待办队列（WP-3）。msg_id 幂等 → 重复提交不再产生重复记忆。"""
        outbox = self._ensure_outbox()
        if outbox is None:
            return 0
        added = 0
        for text in texts:
            msg_id = None
            try:
                from core.utils.memory_outbox import make_msg_id

                msg_id = make_msg_id(
                    str(device_id or ""), str(session_id or ""), turn, text
                )
            except Exception as e:
                logger.bind(tag=TAG).warning(f"生成幂等键失败: {e}")
                continue
            if outbox.enqueue(
                msg_id=msg_id, scope=scope, text=text, speaker=speaker,
                speaker_known=speaker_known, device_id=device_id,
                session_id=session_id, assistant_ctx=assistant_ctx,
            ):
                added += 1
        self.counters["enqueued"] += added
        if added:
            logger.bind(tag=TAG).info(
                f"mem0 已入队 {added} 条待抽取（scope={scope}，说话人={speaker or '未识别'}）"
            )
        self._kick_worker()
        return added

    def resume_pending(self) -> int:
        """服务启动后调用：有积压的待抽取任务就直接启动 worker（必须在事件循环里调）。

        原来 worker 只在「有新消息入队」时才启动，重启后积压要等下一句话才处理。
        """
        if self.write_mode != "outbox" or self._ensure_mem() is None:
            return 0
        outbox = self._ensure_outbox()
        if outbox is None:
            return 0
        try:
            counts = outbox.counts() or {}
        except Exception as e:
            logger.bind(tag=TAG).warning(f"读取待办队列统计失败: {e}")
            return 0
        pending = int(counts.get("pending", 0))
        if pending:
            logger.bind(tag=TAG).info(f"mem0 启动恢复：{pending} 条待抽取积压，启动 worker")
            self._kick_worker()
        return pending

    def _kick_worker(self) -> None:
        """确保后台抽取 worker 在跑（挂在主事件循环上，只启一次）。"""
        if self.write_mode != "outbox":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有运行中的循环（例如独立探针进程），交给下次调用
        if self._worker_task is not None and not self._worker_task.done() \
                and self._worker_loop is loop:
            return
        self._worker_loop = loop
        self._worker_task = loop.create_task(self._outbox_worker())
        logger.bind(tag=TAG).info("mem0 抽取 worker 已启动（错峰抽取，前台优先）")

    async def _outbox_worker(self) -> None:
        """后台抽取循环：错峰（前台安静 30 秒才抽）、退避重试、熔断。"""
        while True:
            try:
                await asyncio.sleep(OUTBOX_POLL_SECONDS)
                if self._ensure_mem() is None:
                    continue
                # 熔断：LM Studio 卸载模型 / 连续 502 时不要疯狂重试放大故障
                if time.time() < self._circuit_until:
                    continue
                outbox = self._ensure_outbox()
                if outbox is None:
                    continue
                batch = await asyncio.to_thread(
                    outbox.claim_batch, OUTBOX_BATCH_LIMIT, MAX_BATCH_CHARS
                )
                if not batch:
                    continue
                # 错峰：抽取没有时间压力，等一个「对话间歇」再抽，最多等 10 分钟
                from core.utils.foreground_activity import wait_until_idle

                waited = await wait_until_idle(
                    EXTRACT_IDLE_SECONDS, EXTRACT_MAX_WAIT_SECONDS
                )
                if waited >= 1.0:
                    logger.bind(tag=TAG).info(
                        f"mem0 抽取等待对话间歇 {waited:.1f}s 后开始（{len(batch)} 条）"
                    )
                await self._process_outbox_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.bind(tag=TAG).error(f"mem0 抽取 worker 异常（继续运行）: {e}")
                await asyncio.sleep(OUTBOX_POLL_SECONDS)

    async def _process_outbox_batch(self, batch: List[Dict[str, Any]]) -> None:
        """worker 用的版本：批与批之间等前台空闲（错峰，不抢 TTS/判停的 GPU）。"""
        await self._run_batch(batch, wait_idle=True)

    async def _add_with_dedup(self, mem, payload: List[Dict[str, str]],
                              scope: str) -> Tuple[int, int, Optional[str]]:
        """调 mem0 抽取并落库，返回 (新增数, 更新数, 错误信息)。

        ⚠️ 计数口径（2026-10-13 修正）：原来 `mem.add()` 不抛异常就
        `total += len(chunk)`，那是**输入消息数**，不是记忆条数。
        模型判定「不值得记」时 mem0 返回 `{'results': []}` 且不抛异常 ——
        原来会把它记成「已从 N 条消息抽取记忆」，属于假成功。
        现在严格按返回的 results 里 event=ADD/UPDATE 统计。

        去重：先把该 scope 已有的记忆文本取出来，若这批输入的语义已被覆盖
        （文本相似度 ≥ DEDUP_SIMILARITY）就跳过，避免现网那种 28 条完全重复。
        """
        try:
            existing = await asyncio.to_thread(self._existing_texts, mem, scope)
        except Exception:
            existing = []

        # 逐条判重：整批里只要有一条是新的就提交那一条（mem0 单次调用较慢，
        # 但重复提交造成的污染更贵；批量仍按 _batch_size 切块）
        fresh: List[Dict[str, str]] = []
        for m in payload:
            content = m.get("content") or ""
            if existing and self._is_duplicate(content, existing):
                self.counters["deduped"] += 1
                logger.bind(tag=TAG).info(
                    f"mem0 跳过重复内容（与已有记忆高度相似）: {content[:40]}…"
                )
                continue
            fresh.append(m)
        if not fresh:
            return 0, 0, None

        created = updated = 0
        batch_size = int(getattr(self, "_batch_size", 4))
        chunks = self._chunk_by_size(fresh, batch_size)
        for idx, chunk in enumerate(chunks):
            try:
                result = await asyncio.to_thread(
                    self._add_serialized, mem, chunk, scope
                )
            except Exception as e:
                # 记录错误类型与安全截断的响应体，便于 502 归因
                # （原来只留 "Error code: 502"，无法区分是模型卸载还是上下文超限）
                err = f"{type(e).__name__}: {str(e)[:ERROR_BODY_LIMIT]}"
                logger.bind(tag=TAG).error(
                    f"mem0 抽取失败（scope={scope}，第 {idx + 1}/{len(chunks)} 批，"
                    f"{len(chunk)} 条）: {err}"
                )
                return created, updated, err
            rows = []
            if isinstance(result, dict):
                rows = result.get("results") or []
            elif isinstance(result, list):
                rows = result
            for row in rows:
                event = str((row or {}).get("event") or "").upper()
                if event == "ADD":
                    created += 1
                elif event in ("UPDATE", "DELETE"):
                    updated += 1
        return created, updated, None

    @staticmethod
    def _chunk_by_size(payload: List[Dict[str, str]], batch_size: int) -> List[List[Dict[str, str]]]:
        """按「条数 + 合计字数」切批（2026-10-12 的教训，2026-10-13 保留）。

        LM Studio 的 llama-server 是 ctx 8192 / parallel 4 → 每槽位只有 2048 token，
        抽取提示词约 1.3K，塞 4 条长消息就 `Context size has been exceeded`
        （近一天 71 次）。所以除了条数上限，还要按合计字数切。
        ⚠️ 字数按 **payload 全文**算（含「（说话人：X）」前缀与助手上下文），
        不能只按用户原话算 —— 那才是真正发给模型的长度。
        """
        chunks: List[List[Dict[str, str]]] = []
        cur: List[Dict[str, str]] = []
        chars = 0
        for m in payload:
            n = len(m.get("content") or "")
            if cur and (len(cur) >= batch_size or chars + n > MAX_BATCH_CHARS):
                chunks.append(cur)
                cur, chars = [], 0
            cur.append(m)
            chars += n
        if cur:
            chunks.append(cur)
        return chunks

    def _add_serialized(self, mem, chunk: List[Dict[str, str]], scope: str):
        with _EXTRACT_LOCK:
            return mem.add(messages=chunk, user_id=scope)

    def _existing_texts(self, mem, scope: str, limit: int = 200) -> List[str]:
        """取该 scope 已有记忆文本（用于去重）。"""
        items = mem.get_all(user_id=scope, limit=limit)
        rows = items.get("results", items) if isinstance(items, dict) else items
        out = []
        for it in rows or []:
            t = str((it or {}).get("memory") or "").strip()
            if t:
                out.append(t)
        return out

    @staticmethod
    def _is_duplicate(content: str, existing: List[str]) -> bool:
        """输入内容是否已被已有记忆覆盖。

        抽取输入带「（说话人：X）」前缀，比对前先剥掉，否则永远比不上。
        """
        body = re.sub(r"^（说话人：[^）]*）", "", content or "").strip()
        body = re.split(r"\n（助手回应", body)[0].strip()
        if len(body) < MIN_USER_FACT_CHARS:
            return False
        for old in existing:
            if difflib.SequenceMatcher(None, body, old).ratio() >= DEDUP_SIMILARITY:
                return True
        return False

    # ------------------------------------------------------------ legacy 写入

    async def _save_legacy(self, texts: List[str], *, source: str, scope: str,
                           speaker: Optional[str], assistant_ctx: Optional[str]) -> int:
        """旧的内存缓冲路径（write_mode=legacy 时走，保留供回滚对比）。"""
        bucket = self._session_msgs.setdefault(source, [])
        for text in texts:
            bucket.append({
                "scope": scope, "speaker": speaker,
                "assistant_ctx": assistant_ctx, "text": text,
            })
        if len(bucket) > self._max_backlog:
            del bucket[:len(bucket) - self._max_backlog]
        logger.bind(tag=TAG).info(
            f"mem0 待抽取 {len(bucket)}/{self._flush_threshold} 条（来源 {source}）"
        )
        if not self.auto_commit or len(bucket) < self._flush_threshold:
            return len(texts)
        return await self._flush(source)

    async def _flush(self, source: str):
        """把攒下的消息交给 mem0 抽取记忆（legacy 路径）。

        ⚠️⚠️ **只提交 `role == "user"` 的消息**（2026-10-06 真实语音链路实测）：
        ① assistant 会触发 mem0 的 agent 抽取分支 → 整批返回空数组；
        ② tool 消息会把天气/设备列表/灯光规则当记忆，还会撑爆上下文
           （实测 4 次 `Context size has been exceeded`）。
        """
        mem = self._ensure_mem()
        if mem is None or not (self._session_msgs.get(source) or []):
            return 0
        # 同一来源同时只留一个「等空闲再抽」的任务
        flushing = self.__dict__.setdefault("_flushing", set())
        if source in flushing:
            return 0
        flushing.add(source)
        try:
            from core.utils.foreground_activity import wait_until_idle

            waited = await wait_until_idle(EXTRACT_IDLE_SECONDS, EXTRACT_MAX_WAIT_SECONDS)
            if waited >= 1.0:
                logger.bind(tag=TAG).info(f"mem0 抽取等待对话间歇 {waited:.1f}s 后开始")
            return await self._extract_now(source, mem)
        finally:
            flushing.discard(source)

    async def _extract_now(self, source: str, mem):
        """legacy 路径的抽取。

        ⚠️ 已知缺陷（outbox 模式已修，这里保留原样以便回滚对比）：
        先清空缓冲再 add，失败时这批消息会丢；计数用输入条数。
        """
        bucket = self._session_msgs.get(source) or []
        self._session_msgs[source] = []
        if not bucket:
            return 0
        # 按 scope 分组（一批里可能混着不同成员）
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for it in bucket:
            groups.setdefault(str(it.get("scope") or "family"), []).append(it)
        total = 0
        for scope, items in groups.items():
            payload = [
                {"role": "user", "content": self._build_payload(
                    it["text"], it.get("speaker"), it.get("assistant_ctx"))}
                for it in items
            ]
            created, updated, err = await self._add_with_dedup(mem, payload, scope)
            if err is None:
                total += len(items)
                self.counters["extracted"] += len(items)
                self.counters["created"] += created
                self.counters["updated"] += updated
            else:
                self.counters["failed"] += len(items)
        if total:
            logger.bind(tag=TAG).info(
                f"mem0 已提交 {total} 条用户消息（来源 {source}，"
                f"新增 {self.counters['created']} / 更新 {self.counters['updated']}）"
            )
        return total or None

    async def flush_now(self, timeout: float = 25.0) -> Dict[str, Any]:
        """连接关闭 / 服务退出时的收尾：**只触发一次抽取，不重新提交历史**。

        ⚠️ 2026-10-13 WP-3：原来 `connection._save_and_close` 会把整段
        `dialogue.dialogue` 再交给 save_memory 一遍（daemon 线程、无完成确认），
        与实时保存重叠 → 现网 160 条里 28 条完全重复。
        现在关闭只做 flush：outbox 模式下把到期任务立刻抽掉（不等错峰），
        并返回明确的完成/超时结果，供调用方记日志。
        """
        result: Dict[str, Any] = {"mode": self.write_mode, "processed": 0, "timeout": False}
        if self.write_mode != "outbox":
            # legacy（内存缓冲）模式：把攒下的各来源缓冲抽掉
            mem = self._ensure_mem()
            if mem is None:
                return result
            for source in list(self._session_msgs.keys()):
                if self._session_msgs.get(source):
                    try:
                        n = await self._extract_now(source, mem) or 0
                        result["processed"] += int(n)
                    except Exception as e:
                        logger.bind(tag=TAG).warning(f"legacy 收尾抽取失败: {e}")
            return result
        outbox = self._ensure_outbox()
        mem = self._ensure_mem()
        if outbox is None or mem is None:
            return result
        deadline = time.time() + max(1.0, timeout)
        try:
            drained = False
            while time.time() < deadline:
                batch = await asyncio.to_thread(outbox.claim_batch, OUTBOX_BATCH_LIMIT,
                                                MAX_BATCH_CHARS)
                if not batch:
                    drained = True
                    break
                await self._process_outbox_batch_nowait(batch)
                result["processed"] += len(batch)
            # 没抽完就超时：剩余任务仍留在 outbox（落盘），下次连接/重启会继续
            result["timeout"] = not drained
        except Exception as e:
            logger.bind(tag=TAG).warning(f"收尾 flush 失败（任务仍在队列，重启后会恢复）: {e}")
        if result["processed"]:
            logger.bind(tag=TAG).info(
                f"mem0 收尾 flush 完成：处理 {result['processed']} 条待抽取任务"
            )
        return result

    async def _process_outbox_batch_nowait(self, batch: List[Dict[str, Any]]) -> None:
        """flush_now 用的版本：不再等前台空闲（连接都要关了，没必要错峰）。"""
        await self._run_batch(batch, wait_idle=False)

    async def _run_batch(self, batch: List[Dict[str, Any]], wait_idle: bool = True) -> None:
        """执行一批抽取：按 scope 分组 → 调 mem0 → 写回状态与计数。

        worker 与 flush_now 共用这一份实现，避免「收尾路径不更新计数」这类分叉
        （我第一版就是两份代码，收尾那份漏了计数，单测当场抓到）。
        """
        outbox = self._ensure_outbox()
        mem = self._ensure_mem()
        if outbox is None or mem is None:
            return
        # 同一批里可能混着不同 scope（不同成员同时说话），必须分开提交，
        # 否则 A 的隐私会写进 B 的记忆里。
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in batch:
            groups.setdefault(str(item.get("scope") or "family"), []).append(item)

        for scope, items in groups.items():
            payload = [
                {"role": "user", "content": self._build_payload(
                    it["text"], it.get("speaker"), it.get("assistant_ctx"))}
                for it in items
            ]
            created, updated, err = await self._add_with_dedup(mem, payload, scope)
            if err is None:
                self._consecutive_failures = 0
                self.counters["extracted"] += len(items)
                self.counters["created"] += created
                self.counters["updated"] += updated
                if created or updated:
                    logger.bind(tag=TAG).info(
                        f"mem0 抽取完成：新增 {created} 条、更新 {updated} 条"
                        f"（scope={scope}，输入 {len(items)} 条用户消息）"
                    )
                else:
                    self.counters["empty"] += 1
                    logger.bind(tag=TAG).info(
                        f"mem0 抽取返回空（模型判定这批不值得记，scope={scope}，"
                        f"输入 {len(items)} 条）"
                    )
                for it in items:
                    outbox.mark_done(it["msg_id"], created, updated)
                    created, updated = 0, 0
            else:
                self._consecutive_failures += 1
                self.counters["failed"] += len(items)
                if self._consecutive_failures >= CIRCUIT_FAIL_THRESHOLD:
                    self._circuit_until = time.time() + CIRCUIT_PAUSE_SECONDS
                    logger.bind(tag=TAG).error(
                        f"mem0 抽取连续失败 {self._consecutive_failures} 次，"
                        f"熔断 {int(CIRCUIT_PAUSE_SECONDS)}s（避免模型不可用时疯狂重试）"
                    )
                for it in items:
                    outbox.mark_failed(it["msg_id"], err, int(it.get("tries") or 0))
            if not wait_idle:
                continue
            # 批与批之间让出 GPU：前台若又忙起来就继续等
            try:
                from core.utils.foreground_activity import wait_until_idle

                await wait_until_idle(BATCH_GAP_IDLE_SECONDS, 60.0)
            except Exception:
                pass

    # ------------------------------------------------------------------ 查询

    async def query_memory(self, query: str, *, scope: Optional[str] = None,
                           speaker: Optional[str] = None) -> str:
        """返回注入系统提示词的记忆文本（契约要求 async + 返回 str）。

        ⚠️ 2026-10-13 WP-1/WP-4 两处关键修正：

        ① **按 scope 检索**：识别出成员 → 并发查「个人 + 家庭共享」；
           未识别 → 只查家庭共享（不越权读任何人的个人记忆）。
           原来一律用 `self.user_id`，而它会被最后一个连上的设备改写。

        ② **阈值方向**：chroma 的 score 是距离，原来 `score < min_confidence`
           过滤等于「丢掉最相关的、留下最不相关的」，且没有相关性上限。
           现在换算成相似度后按 `min_similarity` 过滤。

        精排（判定模型）实测中位 1861ms，而 chat_engine 只用 100~200ms 快速路径
        取本轮记忆，所以维持 2026-10-06 的两段策略：候选没取满就不精排
        （纯向量 22ms，稳稳落在快速路径内）；候选取满才多取一批交给判定模型。
        """
        mem = self._ensure_mem()
        if mem is None or not (query or "").strip():
            return ""

        scopes = self._query_scopes(scope, speaker)

        decision_layer = None
        try:
            from core.providers.decision import get_decision_provider

            decision_layer = get_decision_provider()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"判定层不可用，退回纯向量检索: {e}")

        rerank_on = bool(
            decision_layer is not None
            and decision_layer.enabled
            and decision_layer.memory_enabled
        )

        try:
            # 并发查多个 scope（个人优先、家庭兜底），纯向量检索各约 22ms
            results = await asyncio.gather(
                *[asyncio.to_thread(
                    mem.search, query=query, user_id=s, limit=self.retrieve_limit
                ) for s in scopes],
                return_exceptions=True,
            )
            lines: List[str] = []
            seen = set()
            raw_count = 0
            for s, res in zip(scopes, results):
                if isinstance(res, Exception):
                    logger.bind(tag=TAG).warning(f"mem0 检索失败（scope={s}）: {res}")
                    continue
                items = res.get("results", res) if isinstance(res, dict) else res
                for it in items or []:
                    raw_count += 1
                    text = str((it or {}).get("memory", "")).strip()
                    if not text or text in seen:
                        continue
                    sim = _similarity_to_score((it or {}).get("score"))
                    if sim is not None and sim < self.min_similarity:
                        continue
                    seen.add(text)
                    lines.append(self._format_memory_line(it, text, s))
            if raw_count and not lines:
                logger.bind(tag=TAG).info(
                    f"mem0 检索到 {raw_count} 条候选，但都低于相似度门槛 "
                    f"{self.min_similarity:.2f}，本轮不注入（避免注入无关记忆）"
                )
                return ""
            if not lines:
                return ""

            # 候选取满时才值得多取一批精排（判定模型 1.9s，只在本来就要等的时候用）
            if rerank_on and len(lines) >= self.retrieve_limit:
                wide_lines = await self._wide_candidates(mem, query, scopes, decision_layer)
                if len(wide_lines) > self.retrieve_limit:
                    try:
                        selected = await decision_layer.rerank_memory_lines(
                            query, wide_lines
                        )
                    except Exception as e:
                        logger.bind(tag=TAG).warning(f"判定层精排失败，用原始候选: {e}")
                        selected = None
                    if selected is not None:
                        lines = selected[: self.retrieve_limit]
                        if not lines:
                            logger.bind(tag=TAG).info(
                                "判定层精排后没有高相关记忆，本轮不注入"
                            )
                            return ""

            return "【长期记忆】\n" + "\n".join(lines[: self.retrieve_limit])
        except Exception as e:
            logger.bind(tag=TAG).error(f"mem0 查询记忆失败: {e}")
            return ""

    def _query_scopes(self, scope: Optional[str], speaker: Optional[str]) -> Tuple[str, ...]:
        if scope:
            return (str(scope),)
        if self.scope_mode != "v2":
            return (self.user_id,)
        try:
            from core.utils.memory_scope import scopes_for_query

            return scopes_for_query(speaker)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"查询 scope 解析失败，用家庭共享: {e}")
            return ("family",)

    async def _wide_candidates(self, mem, query: str, scopes: Tuple[str, ...],
                               decision_layer) -> List[str]:
        """精排用的宽候选（跨 scope 合并、同样过相似度门槛）。"""
        limit = int(getattr(decision_layer, "memory_candidate_limit", 20) or 20)
        results = await asyncio.gather(
            *[asyncio.to_thread(mem.search, query=query, user_id=s, limit=limit)
              for s in scopes],
            return_exceptions=True,
        )
        out: List[str] = []
        seen = set()
        for res in results:
            if isinstance(res, Exception):
                continue
            items = res.get("results", res) if isinstance(res, dict) else res
            for it in items or []:
                text = str((it or {}).get("memory", "")).strip()
                if not text or text in seen:
                    continue
                sim = _similarity_to_score((it or {}).get("score"))
                if sim is not None and sim < self.min_similarity:
                    continue
                seen.add(text)
                out.append(self._format_memory_line(it, text, ""))
        return out

    @staticmethod
    def _format_memory_line(item: Dict[str, Any], text: str, scope: str) -> str:
        """注入格式：带日期与归属。

        产品要求「事实带来源、时间、确定程度」（《整体产品与家庭购物系统规划》
        第六部分证据分层）。mem0 的 metadata 里有 created_at，原来注入时丢掉了，
        于是「最近去医院就诊并被开了药」这种时效性事实无法判断新旧。
        """
        prefix = ""
        meta = (item or {}).get("metadata") or {}
        created = str(meta.get("created_at") or (item or {}).get("created_at") or "")
        if created:
            # mem0 1.0 用 US/Pacific 打 created_at（带 -07:00），直接截日期会比本地晚一天
            # （北京早晨写入显示成前一天），必须先换算到本地时区。
            try:
                dt = datetime.fromisoformat(created)
                if dt.tzinfo is not None:
                    dt = dt.astimezone()
                prefix = f"({dt.month:02d}-{dt.day:02d}) "
            except ValueError:
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})", created)
                if m:
                    prefix = f"({m.group(2)}-{m.group(3)}) "
        who = str(meta.get("speaker") or "").strip()
        if who and scope and scope != "family":
            return f"- {prefix}{text}（来源：{who}）"
        return f"- {prefix}{text}"

    # ------------------------------------------------------------ 会话上下文

    def get_session_context(self, token_budget: int = 128000) -> Dict[str, Any]:
        """mem0 只存提炼后的记忆，不存原始对话，故无会话可恢复。

        短期上下文的续接由 WP-5（L1）负责：从 dialogue_history 回灌最近几轮。
        """
        return {}

    def get_recent_messages(self, limit: int = 10) -> List[Dict[str, str]]:
        """同上，mem0 无原始对话历史。"""
        return []

    def clear_session(self):
        """清空待抽取缓冲（legacy 模式的内存缓冲）。outbox 是落盘的，不清。"""
        self._session_msgs.clear()

    def clear_memory(self, session_id: str = None, scope: Optional[str] = None):
        """清空长期记忆。不传 scope 时清所有已知 scope（家庭 + 各成员）。"""
        mem = self._ensure_mem()
        if mem is None:
            return 0
        targets = [scope] if scope else self._all_scopes(mem)
        deleted = 0
        for s in targets:
            if not s:
                continue
            try:
                items = mem.get_all(user_id=s)
                rows = items.get("results", items) if isinstance(items, dict) else items
                for it in rows or []:
                    mid = (it or {}).get("id")
                    if mid:
                        mem.delete(memory_id=mid)
                        deleted += 1
            except Exception as e:
                logger.bind(tag=TAG).error(f"mem0 清空记忆失败（scope={s}）: {e}")
        if deleted:
            logger.bind(tag=TAG).info(f"mem0 已清空 {deleted} 条记忆")
        return deleted

    def list_memories(self, scope: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """列出长期记忆条目（管理台「长期记忆」列表用，2026-10-13 WP-6）。

        返回统一结构：id / memory(内容) / scope / created_at / updated_at / metadata。
        不传 scope 时列出所有已知 scope；条目多时按 scope 各自取 limit 条。
        """
        mem = self._ensure_mem()
        if mem is None:
            return []
        targets = [scope] if scope else self._all_scopes(mem)
        out: List[Dict[str, Any]] = []
        for s in targets:
            if not s:
                continue
            try:
                items = mem.get_all(user_id=s, limit=limit)
                rows = items.get("results", items) if isinstance(items, dict) else items
                for it in rows or []:
                    it = it or {}
                    mid = it.get("id")
                    text = str(it.get("memory") or "").strip()
                    if not mid or not text:
                        continue
                    out.append({
                        "id": mid,
                        "memory": text,
                        "scope": s,
                        "created_at": str(it.get("created_at") or ""),
                        "updated_at": str(it.get("updated_at") or ""),
                        "metadata": it.get("metadata") or {},
                    })
            except Exception as e:
                logger.bind(tag=TAG).warning(f"mem0 列出记忆失败（scope={s}）: {e}")
        # 新记忆排前面（created_at 为 ISO 字符串，可直接字典序）
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return out

    def delete_memory_entry(self, memory_id: str) -> Dict[str, Any]:
        """删除单条长期记忆，返回被删条目内容（管理台 D6 权限：删前留痕）。

        找不到该 id 时返回 {'success': False, 'error': ...}，不抛异常。
        """
        mem = self._ensure_mem()
        if mem is None:
            return {"success": False, "error": "mem0 未初始化"}
        if not memory_id:
            return {"success": False, "error": "缺少记忆 id"}
        # 先找到这条记忆属于哪个 scope、内容是什么（删了就查不到了，必须先留痕）
        found_scope = ""
        found_text = ""
        for s in self._all_scopes(mem):
            try:
                items = mem.get_all(user_id=s, limit=500)
                rows = items.get("results", items) if isinstance(items, dict) else items
                for it in rows or []:
                    if str((it or {}).get("id") or "") == str(memory_id):
                        found_scope = s
                        found_text = str((it or {}).get("memory") or "")
                        break
            except Exception:
                continue
            if found_scope:
                break
        if not found_scope:
            return {"success": False, "error": "未找到该记忆（可能已被删除）"}
        try:
            mem.delete(memory_id=memory_id)
            logger.bind(tag=TAG).info(
                f"mem0 已删除单条记忆 scope={found_scope} id={memory_id} 内容={found_text[:50]}"
            )
            return {"success": True, "scope": found_scope, "memory": found_text}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    def _all_scopes(self, mem) -> List[str]:
        """列出库里实际存在的所有 scope（用于清空/统计）。"""
        scopes = {"family", self.user_id}
        try:
            from core.utils.memory_scope import _load_members

            for mid in set(_load_members().values()):
                scopes.add(f"m:{mid}")
        except Exception:
            pass
        return sorted(s for s in scopes if s)

    def stats(self) -> Dict[str, Any]:
        mem = self._ensure_mem()
        if mem is None:
            return {"total": 0, "error": "mem0 未初始化"}
        try:
            per_scope: Dict[str, int] = {}
            for s in self._all_scopes(mem):
                try:
                    items = mem.get_all(user_id=s)
                    rows = items.get("results", items) if isinstance(items, dict) else items
                    n = len(rows or [])
                    if n:
                        per_scope[s] = n
                except Exception:
                    continue
            out = {
                "total": sum(per_scope.values()),
                "backend": "mem0",
                "llm_model": self.llm_model,
                "embed_model": self.embed_model,
                "scope_mode": self.scope_mode,
                "write_mode": self.write_mode,
                "min_similarity": self.min_similarity,
                "custom_fact_prompt": self.use_custom_fact_prompt,
                "per_scope": per_scope,
                "counters": dict(self.counters),
            }
            if self.write_mode == "outbox" and self._outbox is not None:
                out["outbox"] = self._outbox.counts()
            return out
        except Exception as e:
            return {"total": 0, "error": str(e)}

    def set_llm(self, llm):
        self.llm = llm

    # ------------------------------------------------------- 基类兼容方法

    def store(self, key: str, value: str, metadata: Optional[Dict[str, Any]] = None,
              scope: Optional[str] = None) -> bool:
        """基类接口：直接存一条记忆。

        ⚠️ 2026-10-06 修正返回值语义：原来只要 `mem.add()` 没抛异常就 return True，
        但 **mem0 的 add() 永远走 LLM 抽取**，判定不记时返回 `{'results': []}`
        且不抛异常（实测 3/3 复现「假成功」）。现在按实际返回判断。

        另外：即使存进去，文本也可能被改写（mem0 会去主语、合并句子），
        所以这个接口的语义是「提交一条待抽取的事实」，不是「原样存入 KV」。
        """
        mem = self._ensure_mem()
        if mem is None:
            return False
        target = str(scope or (self.user_id if self.scope_mode != "v2" else "family"))
        try:
            result = mem.add(
                messages=[{"role": "user", "content": f"{key}: {value}"}],
                user_id=target, agent_id=self.agent_id,
                metadata=metadata or {},
            )
            rows = result.get("results", []) if isinstance(result, dict) else (result or [])
            if not rows:
                logger.bind(tag=TAG).warning(
                    f"mem0 store 未落库：{key}（模型判定这条不值得记，或抽取返回空）"
                )
                return False
            return True
        except Exception as e:
            logger.bind(tag=TAG).error(f"mem0 store 失败: {e}")
            return False

    def retrieve(self, key: str, scope: Optional[str] = None) -> Optional[str]:
        mem = self._ensure_mem()
        if mem is None:
            return None
        target = str(scope or (self.user_id if self.scope_mode != "v2" else "family"))
        try:
            res = mem.search(query=key, user_id=target, limit=1)
            items = res.get("results", res) if isinstance(res, dict) else res
            return str(items[0].get("memory")) if items else None
        except Exception:
            return None

    def store_episode(self, summary: str, *, scope: str, speaker: Optional[str] = None,
                      meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """写入一条「主人明确要求记下」且已整理好的完整记忆（夜间梳理/逐句抽取之外的第三条路）。

        内容已由整理模型写成完整一句话，所以 infer=False 直接落库，不再过抽取模型；
        但仍要比对去重：文本相似度 + 向量相似度（≥EPISODE_DUP_SIMILARITY）任一命中就不重复写。
        返回 {"status": "added"|"duplicate"|"unavailable"|"failed", "id": ..., "error": ...}
        """
        text = str(summary or "").strip()
        if not text:
            return {"status": "failed", "error": "空内容"}
        mem = self._ensure_mem()
        if mem is None:
            return {"status": "unavailable"}
        try:
            existing = self._existing_texts(mem, scope)
            if self._is_duplicate(text, existing):
                return {"status": "duplicate", "reason": "text"}
            try:
                res = mem.search(query=text, user_id=scope, limit=3)
                for it in (res.get("results", res) if isinstance(res, dict) else res) or []:
                    sim = _similarity_to_score((it or {}).get("score"))
                    if sim is not None and sim >= EPISODE_DUP_SIMILARITY:
                        return {"status": "duplicate", "reason": "vector",
                                "existing": str((it or {}).get("memory") or "")[:80]}
            except Exception as e:
                logger.bind(tag=TAG).warning(f"情节记忆去重检索失败（继续写入）: {e}")
            metadata = {"source": "episode", "user_requested": True}
            if speaker:
                metadata["speaker"] = speaker
            metadata.update(meta or {})
            with _EXTRACT_LOCK:
                out = mem.add(messages=[{"role": "user", "content": text}], user_id=scope,
                              metadata=metadata, infer=False)
            rows = out.get("results", out) if isinstance(out, dict) else out
            mid = (rows[0] or {}).get("id") if rows else None
            self.counters["created"] += 1
            logger.bind(tag=TAG).info(f"mem0 已写入情节记忆（scope={scope}）: {text[:50]}")
            return {"status": "added", "id": mid}
        except Exception as e:
            logger.bind(tag=TAG).error(f"mem0 写入情节记忆失败: {e}")
            return {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:120]}"}

    def search(self, query: str, limit: int = 10,
               scope: Optional[str] = None) -> List[Dict[str, Any]]:
        mem = self._ensure_mem()
        if mem is None:
            return []
        target = str(scope or (self.user_id if self.scope_mode != "v2" else "family"))
        try:
            res = mem.search(query=query, user_id=target, limit=limit)
            return res.get("results", res) if isinstance(res, dict) else res
        except Exception:
            return []

    def delete(self, key: str) -> bool:
        mem = self._ensure_mem()
        if mem is None:
            return False
        try:
            mem.delete(memory_id=key)
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._session_msgs.clear()
        if self._worker_task is not None:
            try:
                self._worker_task.cancel()
            except Exception:
                pass
        self._worker_task = None
        self._mem = None
