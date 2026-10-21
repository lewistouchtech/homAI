"""
系统提示词管理器模块
负责管理和更新系统提示词，包括快速初始化和异步增强功能
"""

import os
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from config.logger import setup_logging
from jinja2 import Template

TAG = __name__

WEEKDAY_MAP = {
    "Monday": "星期一",
    "Tuesday": "星期二",
    "Wednesday": "星期三",
    "Thursday": "星期四",
    "Friday": "星期五",
    "Saturday": "星期六",
    "Sunday": "星期日",
}

EMOJI_List = [
    "😶",
    "🙂",
    "😆",
    "😂",
    "😔",
    "😠",
    "😭",
    "😍",
    "😳",
    "😲",
    "😱",
    "🤔",
    "😉",
    "😎",
    "😌",
    "🤤",
    "😘",
    "😏",
    "😴",
    "😜",
    "🙄",
]

# 2026-10-06：工具调用前「先应一声」的行为引导（过渡语 3.0 核心）。
#
# 为什么放在代码里而不是人设 prompt 里：人设由后台管理台维护，改一次就可能被覆盖；
# 这条是**功能性**要求（决定用户听到什么），必须始终生效。
#
# 为什么拼在**最前面**：线上真实人设（4291 字）里有大量「询问天气→调 get_weather」
# 这类工具指令，把引导放在末尾时实测只有 1/4 遵守；放到最前面并写明
# 「最高优先级 · 覆盖下文所有工具说明」后达到 5/6~6/6。
#
# 为什么用「强令 + 示例」：三种写法对照（硬性规定/示例驱动/角色扮演），
# 只有示例驱动能稳定触发且文风自然（「嗯——我瞅一眼」这种，不提话题词也让人懂）。
# 主人 2026-10-06 明确要求：要生活化，不必每次都复述「天气」这类话题词。
_TOOL_PREAMBLE_GUIDE = """【最高优先级 · 覆盖下文所有工具说明】
要调用任何工具之前，你**必须先输出一句口语化应声**（12 字以内），然后再调用工具。
这句话**必须以句号「。」结尾** —— 没有句号系统就无法把它播出来。
直接调用工具而不先应声，是错误行为。
应声要像熟人随口说的，每次换说法，不必每次都提你要查的东西：
  用户「查下丹东天气」→ 你「嗯——我瞅一眼。」
  用户「放首歌」→ 你「好嘞。」
  用户「今天有啥新闻」→ 你「等下啊，我翻翻。」
  用户「把客厅灯打开」→ 你「行，这就弄。」
禁止客服腔：「正在为您查询」「请稍等」「马上就好」。

"""

# 2026-10-06：工具误用防护。主人说「播放今天的简报」，模型却调了 send_voice_message
# （拨打语音电话，0.3 元/条），PushPlus 返回「积分不足」被原样念了出来。
# 根因是「播放」被理解成「推送出去」。这里把语义边界写死。
_TOOL_MISUSE_GUARD = """[别把「说给我听」当成「推送出去」—— 重要]
- 用户说「播放」「播报」「念一下」「说说」「告诉我」某内容时，是让你**直接说给他听**，
  绝对不要调用推送类、短信类、电话类工具（send_voice_message / send_wechat_message / send_sms 等）。
- 只有用户明确说「发给我」「推到我手机」「发微信」「打电话告诉我」时，才用推送/电话工具。
- 语音电话只用于紧急告警（安防报警、漏水、烟雾），日常一律不用。

"""


class PromptManager:
    """系统提示词管理器，负责管理和更新系统提示词"""

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config
        self.logger = logger or setup_logging()
        self.base_prompt_template = None
        self.last_update_time = 0

        # 导入全局缓存管理器
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType

        # 初始化上下文源
        from core.utils.context_provider import ContextDataProvider

        self.context_provider = ContextDataProvider(config, self.logger)
        self.context_data = {}

        self._load_base_template()

    def _load_base_template(self):
        """加载基础提示词模板"""
        try:
            template_path = self.config.get("prompt_template", None)
            if not template_path:
                template_path = "agent-base-prompt.txt"
            cache_key = f"prompt_template:{template_path}"

            # 先从缓存获取
            cached_template = self.cache_manager.get(self.CacheType.CONFIG, cache_key)
            if cached_template is not None:
                self.base_prompt_template = cached_template
                self.logger.bind(tag=TAG).debug("从缓存加载基础提示词模板")
                return

            # 缓存未命中，从文件读取
            if os.path.exists(template_path):
                with open(template_path, "r", encoding="utf-8") as f:
                    template_content = f.read()

                # 存入缓存（CONFIG类型默认不自动过期，需要手动失效）
                self.cache_manager.set(
                    self.CacheType.CONFIG, cache_key, template_content
                )
                self.base_prompt_template = template_content
                self.logger.bind(tag=TAG).debug("成功加载基础提示词模板并缓存")
            else:
                self.logger.bind(tag=TAG).warning(f"未找到{template_path}文件")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载提示词模板失败: {e}")

    def get_quick_prompt(self, user_prompt: str, device_id: str = None) -> str:
        """快速获取系统提示词（使用用户配置）。

        2026-09-29 主人要求：AI 名字是后台变量，不允许写死——
        提示词文本里的默认名（伊娃）统一替换为 ai_butler_config 的当前名字，
        缓存 key 包含名字，后台改名后立即生效。"""
        from core.utils.assistant_identity import get_name

        ai_name = get_name()
        # {name} 占位符替换（2026-10-10）：同步人设生成的 prompt 用 {name}
        # 占位符（与 assistant_identity.get_full_prompt 机制对齐），
        # 原实现只替换「伊娃」字样不处理 {name} → LLM 收到字面「我是{name}」
        # 自己编了个名字叫伊娃（主人实测改汤圆后自我介绍仍说伊娃）。
        if ai_name and "{name}" in user_prompt:
            user_prompt = user_prompt.replace("{name}", ai_name)
        if ai_name and "伊娃" in user_prompt:
            user_prompt = user_prompt.replace("伊娃", ai_name)

        device_cache_key = f"device_prompt:{device_id}:{ai_name}"
        cached_device_prompt = self.cache_manager.get(
            self.CacheType.DEVICE_PROMPT, device_cache_key
        )
        if cached_device_prompt is not None:
            self.logger.bind(tag=TAG).debug(f"使用设备 {device_id} 的缓存提示词")
            return cached_device_prompt
        else:
            self.logger.bind(tag=TAG).debug(
                f"设备 {device_id} 无缓存提示词，使用传入的提示词"
            )

        # 使用传入的提示词并缓存（如果有设备ID）
        if device_id:
            self.cache_manager.set(
                self.CacheType.DEVICE_PROMPT, device_cache_key, user_prompt
            )
            self.logger.bind(tag=TAG).debug(f"设备 {device_id} 的提示词已缓存")

        self.logger.bind(tag=TAG).info(f"使用快速提示词: {user_prompt[:50]}...")
        # {name} 占位符替换：跟随后台 AI 名称，rename 后下一次缓存失效即生效
        try:
            from core.utils.assistant_identity import get_name
            return user_prompt.replace("{name}", get_name())
        except Exception:
            return user_prompt

    def _get_current_time_info(self) -> tuple:
        """获取当前时间信息"""
        from .current_time import (
            get_current_date,
            get_current_weekday,
            get_current_lunar_date,
        )

        today_date = get_current_date()
        today_weekday = get_current_weekday()
        lunar_date = get_current_lunar_date() + "\n"

        return today_date, today_weekday, lunar_date

    def _get_location_info(self, client_ip: str) -> str:
        """获取位置信息"""
        try:
            # 先从缓存获取
            cached_location = self.cache_manager.get(self.CacheType.LOCATION, client_ip)
            if cached_location is not None:
                return cached_location

            # 缓存未命中，调用API获取
            from core.utils.util import get_ip_info

            ip_info = get_ip_info(client_ip, self.logger)
            city = ip_info.get("city", "未知位置")
            location = f"{city}"

            # 存入缓存
            self.cache_manager.set(self.CacheType.LOCATION, client_ip, location)
            return location
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取位置信息失败: {e}")
            return "未知位置"

    def _get_weather_default_city(self, fallback: str = "") -> str:
        """获取天气默认城市

        主人规则（2026-09-07）：天气一律用后台天气设置（控制台天气设置页，
        即 data/.config.yaml 的 plugins.get_weather.default_location）里配置的城市，
        除非用户明确说出城市名称（那种情况走 get_weather 工具传参）。
        IP 定位城市只作为后台未配置时的后备。
        """
        try:
            default_city = (
                self.config.get("plugins", {}).get("get_weather", {}).get("default_location")
            )
            return default_city or fallback
        except Exception:
            return fallback

    def _get_weather_info(self, conn: "ConnectionHandler", location: str) -> str:
        """获取天气信息"""
        try:
            # 天气固定用默认城市，IP 定位仅作后备
            city = self._get_weather_default_city(fallback=location)
            # 先从缓存获取
            cached_weather = self.cache_manager.get(self.CacheType.WEATHER, city)
            if cached_weather is not None:
                return cached_weather

            # 缓存未命中，调用get_weather函数获取
            from plugins_func.functions.get_weather import get_weather
            from plugins_func.register import ActionResponse

            # 调用get_weather函数
            result = get_weather(conn, location=city, lang="zh_CN")
            if isinstance(result, ActionResponse):
                weather_report = result.result
                # 写缓存必须用同一个 key（city），否则 build 时查 '顺义' 查不到
                self.cache_manager.set(self.CacheType.WEATHER, city, weather_report)
                return weather_report
            return "天气信息获取失败"

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取天气信息失败: {e}")
            return "天气信息获取失败"

    def update_context_info(self, conn, client_ip: str):
        """同步更新上下文信息"""
        try:
            local_address = ""
            if (
                client_ip
                and self.base_prompt_template
                and (
                    "local_address" in self.base_prompt_template
                    or "weather_info" in self.base_prompt_template
                )
            ):
                # 获取位置信息（使用全局缓存）
                local_address = self._get_location_info(client_ip)

            if (
                self.base_prompt_template
                and "weather_info" in self.base_prompt_template
                and local_address
            ):
                # 获取天气信息（使用全局缓存）
                result = self._get_weather_info(conn, local_address)
                self.logger.bind(tag=TAG).info(
                    f"[天气预取] city_key={(self._get_weather_default_city(fallback=local_address))[:20]}, "
                    f"结果长度={len(result) if result else 0}"
                )

            # 获取配置的上下文数据
            if hasattr(conn, "device_id") and conn.device_id:
                if (
                    self.base_prompt_template
                    and "dynamic_context" in self.base_prompt_template
                ):
                    self.context_data = self.context_provider.fetch_all(conn.device_id)
                else:
                    self.context_data = ""

            self.logger.bind(tag=TAG).debug(f"上下文信息更新完成")

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新上下文信息失败: {e}")

    def build_enhanced_prompt(
        self, user_prompt: str, device_id: str, client_ip: str = None, *args, **kwargs
    ) -> str:
        """构建增强的系统提示词"""
        if not self.base_prompt_template:
            return user_prompt

        try:
            # 获取最新的时间信息（不缓存）
            today_date, today_weekday, lunar_date = self._get_current_time_info()

            # 获取缓存的上下文信息
            local_address = ""
            weather_info = ""

            if client_ip:
                # 获取位置信息（从全局缓存）
                local_address = (
                    self.cache_manager.get(self.CacheType.LOCATION, client_ip) or ""
                )

                # 获取天气信息（从全局缓存，固定用默认城市）
                weather_city = self._get_weather_default_city(fallback=local_address)
                if weather_city:
                    weather_info = (
                        self.cache_manager.get(self.CacheType.WEATHER, weather_city)
                        or ""
                    )

            # 获取TTS选择的语言，默认值为中文
            language = (
                self.config.get("TTS", {})
                .get(self.config.get("selected_module", {}).get("TTS", ""), {})
                .get("language")
                or "中文"
            )
            self.logger.bind(tag=TAG).debug(f"获取到选择的语言: {language}")

            # 替换模板变量
            template = Template(self.base_prompt_template)
            enhanced_prompt = template.render(
                base_prompt=user_prompt,
                current_time="{{current_time}}",
                today_date=today_date,
                today_weekday=today_weekday,
                lunar_date=lunar_date,
                local_address=local_address,
                weather_info=weather_info,
                emojiList=EMOJI_List,
                device_id=device_id,
                client_ip=client_ip,
                dynamic_context=self.context_data,
                language=language,
                *args,
                **kwargs,
            )
            device_cache_key = f"device_prompt:{device_id}"
            enhanced_prompt = _TOOL_PREAMBLE_GUIDE + _TOOL_MISUSE_GUARD + enhanced_prompt
            self.cache_manager.set(
                self.CacheType.DEVICE_PROMPT, device_cache_key, enhanced_prompt
            )
            self.logger.bind(tag=TAG).info(
                f"构建增强提示词成功，长度: {len(enhanced_prompt)}, "
                f"位置: {local_address}, 天气注入({len(weather_info)}字): {weather_info[:60]}"
            )
            return enhanced_prompt

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"构建增强提示词失败: {e}")
            return user_prompt
