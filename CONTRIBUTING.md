# 贡献指南

感谢你愿意为 homAI 出力！这个项目想做成**一个真正属于每个家庭的 AI 管家**，
你的每一份贡献都会让它更好用一点。

本文档回答四个问题：**改哪里、怎么改、怎么测、怎么提**。

---

## 一、先说清楚：没有「指定的模块」

很多项目会让贡献者只改某个目录，其实这**弊大于利** —— 好的想法常常来自你意想不到的地方。

homAI 的规则很简单：

> **整个仓库都是开放的。你想改哪里就改哪里。**

维护者 review 的时候只判断三件事：

| 判断维度 | 谁来判 | 怎么判 |
|---|---|---|
| **能不能跑** | 🤖 机器人 | CI 自动跑测试、语法检查、密钥扫描 |
| **会不会破坏别的** | 🤖 机器人 + 👤 维护者 | 测试是否通过；改动是否影响已有功能 |
| **符不符合方向** | 👤 维护者 | 是否让 homAI 更好用、是否与项目理念一致 |

**技术问题 90% 由机器判**，维护者只花时间判断"该不该合"。

---

## 二、代码结构速查（帮你快速找到该改的地方）

```
homAI/
├── app.py                      服务入口（HTTP 8003 + WebSocket 9800）
├── core/
│   ├── chat/                   对话引擎（多轮对话、打断、工具调用）
│   │   ├── chat_engine.py         核心对话循环
│   │   ├── router.py              消息路由（文本/音频分发）
│   │   └── initializer.py         组件初始化（TTS/ASR/Memory）
│   ├── handle/                 消息处理器（音频入口、意图、打断）
│   ├── providers/              能力实现（可插拔、多厂商）
│   │   ├── asr/                    语音识别
│   │   ├── tts/                    语音合成
│   │   ├── llm/                    大模型接入
│   │   ├── vad/                    语音活动检测
│   │   ├── memory/                 记忆系统（社区版只含短期记忆）
│   │   └── tools/                  工具系统（含 MCP 通道）
│   ├── api/                    REST 接口
│   ├── utils/                  工具函数
│   └── home/                   🏠 自研 Home Assistant 深度集成（社区版不含）
├── plugins_func/
│   ├── register.py             插件注册表（工具必须用 dict 格式注册）
│   └── functions/              ⭐ 各技能插件（新增能力放这里）
├── config/                     配置加载与初始化
├── tests/unit/                 单元测试
├── console/                    Web 界面（控制台 / 语音终端）
└── docs/                       文档
```

### 新增一个技能（最常见的贡献）

在 `plugins_func/functions/` 下新建一个 `.py` 文件，结构如下：

```python
from plugins_func.register import register_function, ActionResponse, Action, ToolType

GET_TEA_FUNC_DESC = {
    "type": "function",
    "function": {
        "name": "make_tea",
        "description": "泡一杯茶",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "茶的类型：绿茶/红茶/乌龙"},
            },
            "required": [],
        },
    },
}


@register_function("make_tea", GET_TEA_FUNC_DESC, ToolType.SYSTEM_CTL)
def make_tea(conn, type: str = "绿茶"):
    return ActionResponse(action=Action.REQLLM, result=f"正在泡{type}")
```

⚠️ **最容易踩的坑**：工具必须用 **dict 格式**注册（OpenAI 标准）。
用字符串格式注册会导致 LLM API 报错：`Invalid type for 'tools.X', expected an json object.`

---

## 三、提交前自查（3 分钟，省掉来回往返）

```bash
# 1. 语法检查（必须过）
python -m py_compile plugins_func/functions/你的文件.py

# 2. 密钥检查（开源仓库红线）
grep -nE "['\"][0-9a-f]{32,}['\"]" 你的文件.py    # 应该查不到东西

# 3. 内网地址检查
grep -nE "\b(192\.168|10)\.[0-9]+\.[0-9]+\.[0-9]+\b" 你的文件.py

# 4. 跑测试
python -m pytest tests/unit -q
```

### 绝对不能提交的东西

| ❌ 不能提交 | 原因 | ✅ 应该怎么做 |
|---|---|---|
| 真实的 API key / token / 密码 | 一旦公开无法撤回，密钥会被人盗用 | 用 `YOUR_XXX_KEY` 占位，让用户自己填 |
| `config.yaml`（含你的真实配置） | 里面有各种密钥 | 改 `config.yaml.example` |
| `data/`、`models/` | 运行时数据和大模型文件 | 加进 `.gitignore` |
| 你的内网地址 | 暴露你的家庭网络 | 用 `YOUR_HA_HOST` 之类的占位 |
| `__pycache__/`、`.pyc` | 垃圾文件 | 加进 `.gitignore` |

---

## 四、Pull Request 流程

1. **Fork** 本仓库 → 在自己分支上改
2. 提交 PR，标题写清做了什么：`[插件] 新增快递查询技能`
3. **等 CI 跑完** —— 4 道门禁：
   - 🧪 语法检查
   - 🔐 密钥扫描
   - 🧪 单元测试
   - 📦 构建检查
4. CI 全绿后，维护者会来看。可能会有讨论 —— **这是正常的**，不是拒绝
5. 讨论通过就合并

### 分支保护

`main` 分支受保护：不能直接推，必须走 PR。这样每个版本都可追溯。

---

## 五、常见问题

**Q：我改的东西不在"指定模块"里，会被拒吗？**
不会。这是本项目明确鼓励的 —— 意外之处的改进往往最有价值。

**Q：CI 报密钥扫描失败，但我没写密钥啊？**
大概率是你代码里有个像密钥的长字符串（比如 hash、base64）。用 `YOUR_XXX` 替换掉即可。

**Q：我加的技能需要调用收费 API，可以吗？**
可以，但**不能要求用户必须付费才能用**。做法是：key 由用户在配置页自己填，填了就能用。

**Q：能提商业版的功能吗？**
可以提，开源社区版和商业版的边界由维护者定。你在 issue 里描述清楚需求即可。

**Q：我的 PR 一直没被回复？**
可能太忙。可以 ping 一下，或者在 issue 里说明情况。

---

## 六、行为准则

我们希望这是一个**友善、专业、包容**的社区：

- 对事不对人，讨论技术不讨论人
- 尊重不同经验水平的贡献者
- 接受建设性的批评，也给出建设性的反馈
- 以项目整体利益为先

违反准则会被临时或永久封禁。

---

## 七、许可

你贡献的代码默认以 **AGPL-3.0** 授权（与本项目一致），详见 [LICENSE](LICENSE)。

「homAI」名称、标识与形象素材为项目所有，代码协议不包含商标授权。
