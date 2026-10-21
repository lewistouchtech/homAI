"""
快速响应系统 - 规则驱动，零延迟
用于处理简单的问候、确认、退出等场景，避免调用 LLM
"""

from core.utils.assistant_identity import get_name


# 精确匹配 - 直接返回预定义回复
EXACT_MATCHES = {
    # 问候语
    '你好': '你好呀~',
    'hello': 'Hi~',
    'hi': '嗨~',
    '嗨': '嗨~',
    '早上好': '早上好~',
    '下午好': '下午好~',
    '晚上好': '晚上好~',

    # 确认语
    '好的': '好~',
    '是的': '好的~',
    '对': '对~',
    'ok': '好的~',
    '可以': '好的~',

    # 感谢语
    '谢谢': '不客气~',
    '感谢': '应该的~',
    'thanks': 'You are welcome~',
    'thx': '不客气~',

    # 退出语
    '再见': '再见~',
    '拜拜': '拜拜~',
    'bye': 'Bye~',
    '晚安': '晚安~',
}


# 前缀匹配 - 匹配开头
PREFIX_MATCHES = {
    '你好啊': '你好呀~',
    '早上好啊': '早上好~',
    '下午好啊': '下午好~',
    '晚上好啊': '晚上好~',
    '谢谢你': '不客气~',
    '非常感谢': '应该的~',
}


# 包含匹配 - 检测关键词
# 响应中的 {name} 会在运行时替换为后台「AI身份设定」配置的助手名字
KEYWORD_RESPONSES = [
    (['你是谁', '你叫什么'], '我是{name}，你的AI管家~'),
    (['在吗', '在不在'], '在的~'),
    (['你是谁啊'], '我是{name}呀~'),
]


def fast_response(user_input: str) -> str:
    """
    快速响应匹配

    Args:
        user_input: 用户输入文本

    Returns:
        匹配的回复文本，或 None（需要走 LLM）
    """
    if not user_input:
        return None

    input_stripped = user_input.strip()
    input_lower = input_stripped.lower()

    # 1. 精确匹配（最高优先级）
    if input_lower in EXACT_MATCHES:
        return EXACT_MATCHES[input_lower]

    # 2. 前缀匹配
    for prefix, response in PREFIX_MATCHES.items():
        if input_lower.startswith(prefix):
            return response

    # 3. 关键词匹配
    for keywords, response in KEYWORD_RESPONSES:
        if any(kw in input_stripped for kw in keywords):
            return response.format(name=get_name())

    # 4. 未匹配
    return None
