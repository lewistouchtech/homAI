# Memory Provider（记忆提供者）

本目录存放记忆提供者（Memory Provider）插件。运行时由配置 `selected_module.Memory`
决定使用哪一个（`factory.py` 的 `create_provider()` 创建）。

## 可用提供者

| 名称 | 实现 | 说明 |
|------|------|------|
| `mem0` | `mem0/provider.py` | **当前生效（2026-10-13 起）**。mem0 语义长期记忆：中文事实抽取、family/成员 scope 隔离、outbox 可靠写入、相似度门槛检索 |
| `openviking` | `openviking/provider.py` | 已停用（2026-10-06 起）。火山 OpenViking 上下文数据库。**停用原因：检索恒返回 total:0（上游 issue #4482），能写不能用**；可切回（改 selected_module.Memory + 重启），每周一 09:17 launchd 自动查上游修复 |
| `mem_local_short` | `mem_local_short/mem_local_short.py` | 本地短期记忆，LLM 总结 JSON 存盘 |
| `powermem` | `powermem/` | PowerMem 提供者 |
| `mem_report_only` | `mem_report_only/mem_report_only.py` | 仅上报记忆 |
| `nomem` | `nomem/nomem.py` | 不启用记忆 |

## mem0 现行架构（2026-10-13 大修，WP-0~WP-6）

详细方案与实测证据见父仓库：
- `docs/plans/2026-10-13_记忆系统调整方案.md`（问题证据 + 设计 + 实施记录）
- `docs/测试记录/2026-10-13_记忆系统黄金集评测报告.md`（写入召回 7/7、检索 6/6、泄漏 0）

### 分层
- **身份 scope**（`core/utils/memory_scope.py`）：记忆归属 `family`（家庭共享）或
  `m:<member_id>`（成员私有，映射 `devices.db` 的 `family_members` 表）。
  **device_id 只作 metadata，不再决定身份**（修复了旧版 init_memory 全局改写 user_id 的 P0）。
  声纹未识别的说话人只写 family，且敏感内容（医疗/财务/行程/儿童相关）不写。
- **抽取**：中文 `custom_fact_extraction_prompt`（只记 7 类家庭事实，绝不记设备指令/
  提问/开发调试/工具返回/助手人设/寒暄/乱码/对操作的反馈），payload 带说话人前缀 +
  助手上句语境；入库前四层过滤（噪声 → 指令 → 个人标记 → 敏感门）。
  LLM = LM Studio `qwen3-8b@q4_k_m`（1234），embedding = oMLX `Qwen3-Embedding-0.6B-8bit`（8001）。
  ⚠️ Qwen3 思考模式由 `llm_patch.py` 运行时关闭（extra_body + 移除 json_object）。
- **写入**：SQLite outbox（`data/memory_outbox.db`，`core/utils/memory_outbox.py`）：
  msg_id 幂等、30/120/600/3600 秒退避、5 次进 dead 可 replay、崩溃恢复、连续失败熔断；
  全局串行抽取、前台活跃时错峰（`foreground_activity` 空闲 30 秒才抽）。
- **检索**：chroma 的 score 是**距离**，换算相似度 `sim = 1 − d/2`，
  `min_similarity: 0.46`（实测标定）过滤；识别出的成员并发查 `m:<id>` + `family`；
  候选取满 limit 才触发 GEV 精排。检索 query 带上一句 user 文本（解「他呢」类追问）。
- **管理**：管理台「记忆系统」页长期记忆卡。接口 `GET /api/config/memory/entries`、
  `POST .../entries/delete`、`POST .../entries/clear`（路由注册在 `core/http_server.py`，
  `config_api.register_routes` 是死代码勿用）。D6 权限：成员条目仅本人（actor==scope）可删，
  family 条目需主人身份 + 二次确认。

### 配置（`data/.config.yaml` → `Memory.mem0`）

⚠️ 根 `config.yaml` **没有** `Memory.mem0` 段，记忆配置只在 data 那份（LFS 文件）。

| 键 | 当前值 | 说明 |
|---|---|---|
| `scope_mode` | `v2` | 身份 scope；`legacy` 回退旧行为（device_id 当身份） |
| `write_mode` | `outbox` | 可靠写入；`legacy` 回退旧内存缓冲 |
| `custom_fact_prompt` | `true` | 中文家庭事实抽取提示词 |
| `min_similarity` | `0.46` | 相似度门槛（实测标定：相关 0.577~0.790 / 无关 0.289~0.349） |
| `llm_base_url` / `llm_api_key` | localhost:1234 | 抽取 LLM；不配回落 `base_url`/`api_key` |
| `embed_base_url` / `embed_api_key` | localhost:8001 | embedding |
| `batch_size` / `flush_threshold` | 4 / 4 | 攒批提交 |
| `llm_max_tokens` | 4096 | ⚠️ 默认 2000 会截断 JSON 导致落库率 33% |
| `user_id` / `agent_id` | homebrain / 1 | 历史键，v2 模式下不再当身份用 |

## 历史迁移记录

- 2026-09-15：MemPalace → OpenViking（`docs/plans/2026-09-15_OpenViking记忆系统迁移记录.md`）
- 2026-10-06：OpenViking → mem0（检索恒 0，issue #4482）
- 2026-10-13：mem0 体系大修 WP-0~WP-6（见上方两份文档）
