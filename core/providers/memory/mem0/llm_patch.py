#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@file: llm_patch.py
@desc: 给 mem0 的 OpenAI LLM 适配「带思维链的模型」（Qwen3 系列）

背景（2026-10-06 实测）：
Qwen3-8B 默认开启 thinking，会把推理写进 ``reasoning_content``，正文 ``content``
要么为空、要么在 JSON 输出完之前就被 ``max_tokens`` 截断（``finish_reason=length``）。
mem0 解析不到 JSON 就静默丢掉这一批，表现为「一条记忆都抽不出来」。

实测结论（同一个提示词，只改这两个参数）：
- 只传 ``chat_template_kwargs.enable_thinking=false``：正常输出 JSON（finish=stop）
- llama.cpp 系端点（LM Studio）在关思考后会拒绝 ``response_format={"type":"json_object"}``，
  报 400 ``'response_format.type' must be 'json_schema' or 'text'``，
  两者**不能共存**。提示词本身已要求纯 JSON，去掉 response_format 即可。

⚠️ 为什么必须走 ``extra_body``：openai SDK 会拦截未声明的顶层参数，
直接塞 ``chat_template_kwargs=...`` 会抛
``TypeError: Completions.create() got an unexpected keyword argument``。

为什么不直接改 site-packages：venv 不受 git 管理，重装依赖就丢。
这里改成运行时打补丁，改动随代码走、可回滚、可单测。
"""
from __future__ import annotations

from typing import Any

from config.logger import setup_logging

logger = setup_logging()
TAG = __name__

_patched = False


def patch_openai_llm(disable_thinking: bool = True) -> bool:
    """给 mem0 的 OpenAILLM 打补丁：关思考 + 去掉 json_object 约束。

    幂等，可重复调用。返回是否成功打上补丁。
    """
    global _patched
    if _patched:
        return True

    try:
        from mem0.llms.openai import OpenAILLM
    except Exception as e:  # pragma: no cover - mem0 结构变化时
        logger.bind(tag=TAG).warning(f"mem0 LLM 补丁未生效（无法导入 OpenAILLM）: {e}")
        return False

    original_init = OpenAILLM.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if not disable_thinking:
            return
        try:
            completions = self.client.chat.completions
            original_create = completions.create

            def create(*c_args: Any, **c_kwargs: Any):
                if c_kwargs.get("response_format") is not None:
                    # 与 enable_thinking=false 冲突，llama.cpp 端点会 400
                    c_kwargs.pop("response_format", None)
                extra = dict(c_kwargs.get("extra_body") or {})
                extra["chat_template_kwargs"] = {"enable_thinking": False}
                c_kwargs["extra_body"] = extra
                return original_create(*c_args, **c_kwargs)

            completions.create = create
        except Exception as e:  # pragma: no cover
            logger.bind(tag=TAG).warning(f"mem0 LLM 补丁未生效（包装 create 失败）: {e}")

    OpenAILLM.__init__ = patched_init
    _patched = True
    # ⚠️ 日志必须区分「真的改了请求」和「只是挂了个空壳」：
    # disable_thinking=False 时 patched_init 直接 return，请求原样发出，
    # 早期这里无条件打印「补丁已启用」，排查时会被误导成「已经关了思考」。
    if disable_thinking:
        logger.bind(tag=TAG).info(
            "mem0 LLM 补丁已启用：chat_template_kwargs.enable_thinking=false，"
            "并移除 response_format=json_object"
        )
    else:
        logger.bind(tag=TAG).info(
            "mem0 LLM 补丁未生效（disable_thinking=false）：保留 response_format=json_object，"
            "不改请求体（非 Qwen3 系列如豆包应走此分支）"
        )
    return True
