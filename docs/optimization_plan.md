# homAI LLM 响应速度优化方案

## 当前问题分析

### 当前流程
```
用户说话 → ASR识别 → 组装输入 → LLM推理 → TTS合成 → 播放
                        ↓
              ┌─────────────────────────┐
              │ System Prompt (4KB+)    │
              │ + 工具定义 (JSON)       │
              │ + 对话历史 (10轮)       │
              │ + Memory 查询结果       │
              └─────────────────────────┘
```

### 瓶颈定位
1. **Prefill 阶段慢**：LLM 处理长 context 的时间是 O(n²)
2. **System Prompt 过长**：4KB+ 的身份/情感/沟通规则
3. **工具定义每次必传**：即使简单对话也带完整工具列表
4. **对话历史累积**：保留 10 轮对话

---

## 优化方案

### 方案 A：精简 System Prompt（优先级：高）

**目标**：4KB → 1KB

**当前 agent-base-prompt.txt 内容分析**：
- `<identity>`: 身份描述（来自 API，约 500 字）
- `<language>`: 语言规范（~100 字）
- `<wakeup_response_rule>`: 唤醒规则（~300 字）
- `<emotion>`: 情感表达规则（~500 字）
- `<communication_style>`: 沟通风格（~300 字）
- `<communication_length_constraint>`: 长度限制（~400 字）
- `<speaker_recognition>`: 说话人识别（~200 字）
- `<context>`: 动态上下文（时间/天气等）

**优化策略**：
1. 合并重复规则
2. 删除冗余说明
3. 用简洁指令替代详细描述

**精简版 Prompt 示例**：
```
<identity>
{{base_prompt}}
</identity>

<rules>
- 使用中文，口语化表达，语气词自然穿插
- 感知情绪，有温度回应
- 禁止 markdown/列表/标题格式
- 单次回复不超过300字
- 唤醒时简短回应（15字内），不自我介绍
</rules>

<context>
- 时间：{{current_time}}
- 日期：{{today_date}} {{today_weekday}}
- 位置：{{local_address}}
- 天气：{{weather_info}}
</context>

<memory>
</memory>
```

**预期效果**：响应时间减少 30-40%

---

### 方案 B：意图前置 + 分层处理（优先级：高）

**目标**：简单对话不走完整流程

**实现思路**：
```python
# 1. 关键词快速匹配
TOOL_KEYWORDS = ['开灯', '关灯', '天气', '时间', '播放', '控制', '窗帘', '空调']

def needs_tools(user_text):
    """快速判断是否需要工具"""
    return any(kw in user_text for kw in TOOL_KEYWORDS)

# 2. 分层处理
if needs_tools(user_text):
    # 完整流程：prompt + 工具 + 历史
    llm_input = full_prompt + tools + history
else:
    # 精简流程：精简prompt + 少量历史
    llm_input = simple_prompt + recent_history
```

**简单对话模式**：
- System Prompt：精简版（~500 字）
- 工具定义：不传
- 对话历史：只保留最近 2 轮

**预期效果**：简单对话响应时间减少 50-60%

---

### 方案 C：工具定义按需加载（优先级：中）

**目标**：只传相关工具

**实现思路**：
```python
# 工具分类
TOOL_CATEGORIES = {
    'home_control': ['开灯', '关灯', '控制', '空调', '窗帘'],
    'query': ['天气', '时间', '新闻', '搜索'],
    'media': ['播放', '音乐', '视频'],
}

def get_relevant_tools(user_text):
    """根据用户输入获取相关工具"""
    categories = set()
    for kw, cats in TOOL_CATEGORIES.items():
        if any(c in user_text for c in cats):
            categories.add(kw)
    return filter_tools_by_category(categories)
```

**预期效果**：工具定义体积减少 50-70%

---

### 方案 D：对话历史智能压缩（优先级：中）

**目标**：减少对话历史长度

**实现思路**：
1. 简单对话：只保留 2 轮
2. 工具调用：保留完整上下文
3. 长对话：摘要压缩早期历史

**预期效果**：对话历史体积减少 40-60%

---

## 实施计划

### 第一阶段（立即可做）
1. ✅ 精简 `agent-base-prompt.txt`
2. ✅ 添加意图前置判断

### 第二阶段（需要测试）
3. 工具定义按需加载
4. 对话历史智能压缩

### 第三阶段（长期优化）
5. RAG 动态注入设备状态
6. Prefix Caching 优化

---

## 文件修改清单

| 文件 | 修改内容 | 影响 |
|------|----------|------|
| `agent-base-prompt.txt` | 精简规则 | 低 |
| `core/connection.py` | 添加意图判断 | 中 |
| `core/utils/dialogue.py` | 智能历史压缩 | 中 |
| `core/providers/tools/unified_tool_handler.py` | 工具分类 | 低 |

---

## 预期效果

| 优化项 | 响应时间改善 |
|--------|--------------|
| 精简 Prompt | -30~40% |
| 意图前置 | -50~60%（简单对话）|
| 工具按需加载 | -20~30% |
| 历史压缩 | -10~20% |
| **综合** | **-50~70%** |

---

## 备份位置
```
backup_20260705_223506/
├── agent-base-prompt.txt
└── core/
    ├── connection.py
    ├── utils/
    │   ├── dialogue.py
    │   └── prompt_manager.py
    └── providers/
        └── llm/
            ├── base.py
            ├── openai.py
            └── system_prompt.py
```
