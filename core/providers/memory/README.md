# 记忆系统（社区版）

社区版提供**短期记忆**：

| 模块 | 说明 |
| --- | --- |
| `mem_local_short` | 对话上下文与短期事实，本地 SQLite 存储，隐私不出门 |
| `base.py` / `factory.py` | 记忆提供者接口与工厂 |

## 接入自己的长期记忆

`factory.py` 里注册一个提供者即可接入任意长期记忆后端：

```python
_PROVIDERS = {
    "my_backend": (
        "your_package.provider",
        ("YourProvider", "MemoryProvider"),
        "我的长期记忆",
    ),
}
```

注册后在 `data/.config.yaml` 里把 `selected_module.Memory` 设为 `my_backend` 即可。

> 需要开箱即用的完整长期记忆（含家庭语义检索、成员隔离、可靠写入），
> 请使用商业版。
