# 致谢与第三方开源项目

homAI 站在许多开源项目的肩膀上。再次感谢这些项目的贡献者 —— 没有它们，
「住在你家里的 AI 管家」不可能实现。

本项目采用 **AGPL-3.0** 协议。依据该协议与各依赖的原始许可，
以下第三方项目的版权声明予以保留。

---

## 一、项目起源

homAI 的服务端与 ESP32 固件基于以下项目二次开发：

| 项目 | 协议 | 说明 |
|---|---|---|
| [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | MIT | 服务端架构基础（深圳新智未来） |
| [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) | MIT | ESP32 固件基础（深圳新智未来） |

```
MIT License
Copyright (c) 2025 Shenzhen Xinzhi Future Technology Co., Ltd.
Copyright (c) 2025 Project Contributors
```
> 完整文本见 `LICENSE` 同级目录的 `THIRD_PARTY_LICENSES/` 或上游仓库。

---

## 二、核心依赖

### 语音链路

| 项目 | 协议 | 用途 |
|---|---|---|
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0 | 本地语音识别（离线 ASR） |
| [FunASR](https://github.com/modelscope/FunASR) | MIT | 阿里达摩院语音工具包 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | MIT | 语音活动检测（VAD） |
| [edge-tts](https://github.com/rany2/edge-tts) | LGPL-3.0 | 微软 Edge 语音合成 |
| [pydub](https://github.com/jiajiaowang/pydub) | MIT | 音频处理 |
| [opuslib-next](https://github.com/ikalchev/opuslib) | BSD-3-Clause | Opus 编解码 |

### AI 与记忆

| 项目 | 协议 | 用途 |
|---|---|---|
| [ltm](https://github.com/ltm_ai/ltm) | Apache-2.0 | 长期记忆引擎（**商业版功能**） |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT | MCP 协议支持（Anthropic） |
| [CozePy](https://github.com/chyroc/cozepy) | MIT | 扣子（Coze）接入 |
| [DashScope](https://github.com/QwenLM/Qwen3-TTS) | Apache-2.0 | 阿里云百炼 / 通义千问 |
| [Baidu AIP SDK](https://github.com/baidubce/bce-sdk-python) | Apache-2.0 | 百度智能云 |
| [OpenAI Python](https://github.com/openai/openai-python) | Apache-2.0 | OpenAI 兼容接口 |
| [Google Generative AI](https://github.com/google/generative-ai-python) | Apache-2.0 | Gemini 接入 |

### 基础设施

| 项目 | 协议 | 用途 |
|---|---|---|
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 / MIT | 异步 HTTP 框架 |
| [aiohttp-cors](https://github.com/aio-libs/aiohttp-cors) | Apache-2.0 | CORS 中间件 |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP 客户端 |
| [requests](https://github.com/psf/requests) | Apache-2.0 | HTTP 客户端 |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | WebSocket |
| [loguru](https://github.com/Delgan/loguru) | MIT | 日志 |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | YAML 解析 |
| [ruamel.yaml](https://sourceforge.net/projects/ruamel-yaml/) | MIT | YAML 解析（配置编辑） |
| [ormsgpack](https://github.com/aviramha/ormsgpack) | Apache-2.0 | MessagePack 序列化 |
| [chardet](https://github.com/chardet/chardet) | LGPL | 字符编码检测 |
| [cnLunar](https://github.com/OPN48/cnLunar) | GPL-3.0 | 农历/节日/干支 |
| [BeautifulSoup](https://github.com/beautifulsoup/bs4) | MIT | HTML 解析 |
| [markitdown](https://github.com/microsoft/markitdown) | MIT | 文档转 Markdown |
| [ModelScope](https://github.com/modelscope/modelscope) | Apache-2.0 | 模型下载 |
| [HuggingFace Datasets](https://github.com/huggingface/datasets) | Apache-2.0 | 数据集加载 |
| [PyTorch](https://github.com/pytorch/pytorch) | BSD-3-Clause | 深度学习运行时 |
| [torchaudio](https://github.com/pytorch/audio) | BSD-3-Clause | 音频处理 |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | 数值计算 |

### 前端与工具

| 项目 | 协议 | 用途 |
|---|---|---|
| [aioconsole](https://github.com/vxgmichel/aioconsole) | GPL-3.0 | 交互式控制台 |
| [PySocks](https://github.com/Anorov/PySocks) | BSD | SOCKS 代理支持 |

---

## 三、模型与数据

| 资源 | 说明 |
|---|---|
| [Meta Muse Gadgets](https://github.com/meta-p-p/muse-gadgets) | Apache-2.0 · 硬件生态参考 |
| Qwen / Qwen3-TTS | Apache-2.0 · 语音合成模型 |
| MOSS-TTSD | Apache-2.0 · 本地语音合成 |
| SenseVoice | 用于本地语音识别 |
| Live2D Cubism | ⚠️ **专有许可**，需在 Live2D 官网单独获得授权，未包含在本仓库中 |

> **模型权重不随本仓库分发。** 各项模型的许可与使用条款以其官方说明为准。

---

## 四、标准与协议

| 规范 | 说明 |
|---|---|
| [Model Context Protocol](https://modelcontextprotocol.io) | MIT · 工具与数据源接入协议 |
| [ESP-IDF](https://github.com/espressif/esp-idf) | Apache-2.0 · ESP32 开发框架 |
| [ESP-SR](https://github.com/espressif/esp-sr) | Apache-2.0 · ESP32 语音唤醒 |
| [Home Assistant](https://www.home-assistant.io) | Apache-2.0 · 智能家居平台 |
| [OneNET](https://open.iot.10086.cn/) | 中国移动物联网平台 |

---

## 五、我们的立场

- **不 fork 不改协议**：homAI 保留上游 MIT 声明
- **不隐藏依赖**：所有直接依赖都列在 `requirements.txt`
- **不捆绑商业组件**：需要商业授权的能力（ltm 长期记忆、健康监测硬件等）不在社区版内
- **商标独立**：「homAI」名称与形象素材为项目所有，代码协议不包含商标授权

如果你发现本文件遗漏了某个依赖，或某个许可标注有误 —— **请提 issue 告诉我们**，这属于必须修正的问题。

---

## 六、再次致谢

这些项目背后是成千上万的贡献者。homAI 只是它们的一个下游使用者。

如果你也想支持这些项目，请去它们的仓库点个 star 或提个 PR。
