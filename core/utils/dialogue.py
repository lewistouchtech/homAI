import uuid
import re
from typing import List, Dict
from datetime import datetime


class Message:
    def __init__(
            self,
            role: str,
            content: str = None,
            uniq_id: str = None,
            tool_calls=None,
            tool_call_id=None,
            is_temporary=False,
    ):
        self.uniq_id = uniq_id if uniq_id is not None else str(uuid.uuid4())
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.is_temporary = is_temporary  # 标记临时消息（如工具调用提醒）


class Dialogue:
    def __init__(self):
        self.dialogue: List[Message] = []
        # 获取当前时间
        self.current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def put(self, message: Message):
        self.dialogue.append(message)

    def extend(self, messages):
        """批量添加消息（兼容 list.extend 语义，支持 dict 和 Message 对象）"""
        for msg in messages:
            if isinstance(msg, dict):
                self.put(Message(role=msg.get("role", "user"), content=msg.get("content", "")))
            elif isinstance(msg, Message):
                self.put(msg)
            else:
                self.put(msg)

    def getMessages(self, m, dialogue):
        if m.tool_calls is not None:
            # 确保 content 字段存在（某些 LLM API 要求）
            msg = {"role": m.role, "tool_calls": m.tool_calls}
            if m.content is not None:
                msg["content"] = m.content
            else:
                msg["content"] = ""  # 空 content 满足 API 要求
            dialogue.append(msg)
        elif m.role == "tool":
            dialogue.append(
                {
                    "role": m.role,
                    "tool_call_id": (
                        str(uuid.uuid4()) if m.tool_call_id is None else m.tool_call_id
                    ),
                    "content": m.content,
                }
            )
        else:
            dialogue.append({"role": m.role, "content": m.content})

    def get_llm_dialogue(self) -> List[Dict[str, str]]:
        # 直接调用get_llm_dialogue_with_memory，传入None作为memory_str
        # 这样确保说话人功能在所有调用路径下都生效
        return self.get_llm_dialogue_with_memory(None, None)

    def update_system_message(self, new_content: str):
        """更新或添加系统消息"""
        # 查找第一个系统消息
        system_msg = next((msg for msg in self.dialogue if msg.role == "system"), None)
        if system_msg:
            system_msg.content = new_content
        else:
            self.put(Message(role="system", content=new_content))

    def trim_history(self, max_turns: int = 10) -> int:
        """
        智能截断对话历史，保留工具调用的完整性

        Args:
            max_turns: 保留的最大对话轮数（每轮 = user + assistant/tool 相关消息）

        Returns:
            int: 被移除的消息数量
        """
        if len(self.dialogue) <= max_turns * 2 + 1:  # +1 是系统消息
            return 0

        # 分离系统消息和对话消息
        system_messages = [msg for msg in self.dialogue if msg.role == "system"]
        conversation_messages = [msg for msg in self.dialogue if msg.role != "system"]

        if len(conversation_messages) <= max_turns * 2:
            return 0

        # 智能截断：保留完整的工具调用链路
        keep_messages = []
        i = len(conversation_messages) - 1
        turn_count = 0

        while i >= 0 and turn_count < max_turns:
            msg = conversation_messages[i]

            # 从后向前收集消息
            if msg.role == "user":
                # 遇到 user 消息，说明一轮对话开始
                keep_messages.insert(0, msg)
                turn_count += 1
                i -= 1
            elif msg.role == "assistant":
                # 收集 assistant 消息
                keep_messages.insert(0, msg)

                # 如果这个 assistant 有 tool_calls，需要收集对应的 tool 响应
                if msg.tool_calls is not None:
                    i -= 1
                    # 继续向后收集所有相关的 tool 消息
                    while i >= 0 and conversation_messages[i].role == "tool":
                        keep_messages.insert(0, conversation_messages[i])
                        i -= 1
                else:
                    i -= 1
            elif msg.role == "tool":
                # tool 消息应该已经被上面的逻辑收集了
                # 如果单独遇到，也要保留（防止边界情况）
                keep_messages.insert(0, msg)
                i -= 1
            else:
                i -= 1

        removed_count = len(conversation_messages) - len(keep_messages)

        # 重建对话列表
        self.dialogue = system_messages + keep_messages

        return removed_count

    def clear_temporary_messages(self) -> int:
        """
        清理所有临时消息（如工具调用提醒）

        Returns:
            int: 被移除的消息数量
        """
        original_len = len(self.dialogue)
        self.dialogue = [msg for msg in self.dialogue if not msg.is_temporary]
        return original_len - len(self.dialogue)

    def get_llm_dialogue_with_memory(
            self, memory_str: str = None, voiceprint_config: dict = None,
            member_profile: str = None, recent_context: str = None,
            allow_greeting_name: bool = True
    ) -> List[Dict[str, str]]:
        # 构建对话
        dialogue = []

        # 添加系统提示和记忆
        system_message = next(
            (msg for msg in self.dialogue if msg.role == "system"), None
        )

        if system_message:
            # 基础系统提示
            enhanced_system_prompt = system_message.content
            # 替换时间占位符
            enhanced_system_prompt = enhanced_system_prompt.replace(
                "{{current_time}}", datetime.now().strftime("%H:%M")
            )

            # 添加说话人个性化描述
            try:
                speakers = voiceprint_config.get("speakers", [])
                if speakers:
                    enhanced_system_prompt += "\n\n<speakers_info>"
                    for speaker_str in speakers:
                        try:
                            parts = speaker_str.split(",", 2)
                            if len(parts) >= 2:
                                name = parts[1].strip()
                                # 如果描述为空，则为""
                                description = (
                                    parts[2].strip() if len(parts) >= 3 else ""
                                )
                                enhanced_system_prompt += f"\n- {name}：{description}"
                        except:
                            pass
                    enhanced_system_prompt += "\n\n</speakers_info>"
            except:
                # 配置读取失败时忽略错误，不影响其他功能
                pass

            # 当前说话人完整档案（声纹识别出的成员：基本信息/关系/健康数据）
            if member_profile:
                enhanced_system_prompt += (
                    f"\n\n<member_profile>\n当前说话人的完整档案（仅供理解语境"
                    f"与关怀，仅供理解语境。称呼时**必须**使用档案中的"
                    f"「昵称（伊娃对他的称呼）」字段值（如「主人」「小花朵」），"
                    f"**禁止**使用「姓名」或「家庭关系」字段来称呼用户"
                    f"（如不得喊「李威」「父亲」「女儿」），"
                    f"也不得自行拼接「X先生/X女士」「爸爸/妈妈」。"
                    + (
                        "\n【称呼频率·重要】这是本次唤醒后的第一句话，可以在开头"
                        "叫一次称呼（如「小花朵，……」）。之后同一段对话里不要再叫，"
                        "直接说内容即可。反复叫名字会让人觉得很别扭、很机械。"
                        if allow_greeting_name else ""
                    )
                    + f"\n完整档案：\n{member_profile}"
                    f"\n</member_profile>")

            # [2026-10-19] 本次唤醒已经打过招呼：后续轮次明确禁止再叫称呼。
            # 不依赖声纹是否识别成功，避免未识别时 LLM 仍按历史习惯反复叫名字。
            if not allow_greeting_name:
                enhanced_system_prompt += (
                    "\n\n【称呼频率·重要】本次唤醒已经打过招呼，这一轮不要叫用户称呼，"
                    "直接说内容。反复叫名字会让人觉得很别扭、很机械。"
                )

            # [2026-10-05] 家庭地点总览：全员可见、且不依赖声纹是否识别成功。
            #   为什么要单独一段而不是塞进个人档案：
            #     1) 小花朵问「妈妈上班要多久」时妈妈没说话，妈妈的档案根本没被注入；
            #     2) 声纹没识别出说话人时个人档案是空的，但家庭地点总览照样在。
            #   有了它，LLM 听到「我上学了」才能对上「小花朵的学校」并调 plan_commute，
            #   而不是反问「你学校在哪儿」—— 那正是要消除的割裂感。
            try:
                from core.utils.member_profile import get_family_places_text
                _family_places = get_family_places_text()
            except Exception:
                _family_places = ""
            if _family_places:
                enhanced_system_prompt += (
                    f"\n\n<family_places>\n"
                    f"家庭成员的常去地点（**全员共享的共同知识**，"
                    f"问任何人关于任何人的地点都可以查这里）。\n"
                    f"【重要·不要反问地址】用户说「我上班了」「我上学了」"
                    f"「接孩子放学」这类话时，**不要问「你公司在哪」「学校在哪儿」** —— "
                    f"地点就在下面，你应当直接调用 plan_commute 工具"
                    f"（destination 传「公司」「学校」这样的词，不要自己填地址）。\n"
                    f"若某人的地点确实没在上面，**明确告诉用户这个地点还没设置**、"
                    f"并提示去管理后台的「房屋状态 → 地点簿」添加，"
                    f"同样**不要反问地址**。\n"
                    f"地点簿：\n{_family_places}"
                    f"\n</family_places>")

            # 注入长期记忆：优先替换 <memory> 标签；prompt 里没有标签时追加到末尾
            if memory_str:
                if "<memory>" in enhanced_system_prompt:
                    enhanced_system_prompt = re.sub(
                        r"<memory>.*?</memory>",
                        f"<memory>\n{memory_str}\n</memory>",
                        enhanced_system_prompt,
                        flags=re.DOTALL,
                    )
                else:
                    enhanced_system_prompt += (
                        f"\n\n<memory>\n{memory_str}\n</memory>"
                    )

            # [2026-10-13 WP-5] 短期上下文续接（L1）：被 trim_history 截掉的更早轮次
            #   + 新连接时从 dialogue_history 回灌的最近几轮。
            #   ⚠️ 必须放在 system prompt 段，**不能塞回 dialogue 消息列表** ——
            #   塞回去会破坏 assistant.tool_calls 与 tool 消息的配对结构，
            #   云端 LLM 会直接报 400。
            if recent_context:
                enhanced_system_prompt += (
                    f"\n\n<recent_context>\n{recent_context}\n</recent_context>"
                )
            dialogue.append({"role": "system", "content": enhanced_system_prompt})

        # 添加用户和助手的对话
        for m in self.dialogue:
            if m.role != "system":  # 跳过原始的系统消息
                self.getMessages(m, dialogue)

        return dialogue
