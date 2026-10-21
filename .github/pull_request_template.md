name: Pull Request
description: 提交代码改动
body:
  - type: markdown
    attributes:
      value: |
        🎉 感谢你的贡献！

        **你不需要改「指定模块」。** homAI 开放的是整个对话引擎和插件框架 ——
        任何一个模块你都能改，改动不在预期范围内也很正常，那往往是最有价值的贡献。

        维护者会重点看三件事：**能不能跑 / 会不会破坏别的 / 符不符合项目方向**。

  - type: textarea
    id: what
    attributes:
      label: 这个 PR 做了什么？
      description: 一两句话说清楚改了什么、为什么改
      placeholder: |
        例：新增「快递查询」技能，调用快递100 API 实时查询，支持按手机号查。
    validations:
      required: true

  - type: textarea
    id: how
    attributes:
      label: 怎么验证它是对的？
      description: |
        写清楚**别人怎么复现和验证**你的改动。哪怕只是手动操作几步，也比不写好。
        如果能加自动化测试就更好（见下方 checklist）。
      placeholder: |
        例：启动后说"查快递 123456789"，会播报最新一条物流信息。
        例：python -m pytest tests/unit/test_xxx.py
    validations:
      required: true

  - type: dropdown
    id: area
    attributes:
      label: 改动落在哪个区域？
      description: 只是帮忙归类，方便维护者快速判断影响面
      options:
        - 核心对话引擎（core/chat、core/handle）
        - 语音链路（ASR / LLM / TTS / VAD / 声纹）
        - 插件（plugins_func/functions）
        - 记忆系统
        - Web 界面（console/）
        - 工具与 MCP
        - 固件 / 硬件
        - 文档 / 测试 / 脚本
        - 不确定 / 跨多个区域
    validations:
      required: true

  - type: checkboxes
    id: checklist
    attributes:
      label: 提交前自查
      options:
        - label: 我本地跑过，确认没有语法错误（`python -m py_compile`）
          required: true
        - label: 我**没有**在代码里写入任何真实的 API key、token、密码（用 YOUR_XXX 占位）
          required: true
        - label: 我没有提交 `config.yaml`、`.env`、`data/`、`models/` 等含密钥或大文件的目录
          required: true
        - label: 如果改了行为，我补充或更新了对应的测试
          required: false
        - label: 如果是新增能力，我在 README 的「核心能力」里补了说明
          required: false
        - label: 我读过 [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md)，了解项目约定
          required: false

  - type: textarea
    id: notes
    attributes:
      label: 补充说明（可选）
      description: 截图、演示视频、设计思路、或者你不确定的地方 —— 都欢迎写
    validations:
      required: false
