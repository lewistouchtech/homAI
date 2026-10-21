#!/usr/bin/env python3
"""
清理 ASR 模型目录脚本
只保留三个目标模型：paraformer-zh-streaming, Fun-ASR-Nano-2512, SenseVoice
删除其他所有模型和缓存
"""
import os
import shutil
import sys

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")

# 保留的模型目录（相对 models/ 的路径）
KEEP_MODELS = {
    "paraformer-zh-streaming",
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
    "FunAudioLLM/Fun-ASR-Nano-2512",
}

def get_all_subdirs(root):
    """获取 root 下所有一级子目录"""
    if not os.path.isdir(root):
        return []
    return [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]

def is_in_keep_list(rel_path):
    """检查相对路径是否在保留列表中"""
    for keep in KEEP_MODELS:
        if rel_path == keep or rel_path.startswith(keep + os.sep):
            return True
    return False

def cleanup_models_dir():
    if not os.path.exists(MODELS_DIR):
        print(f"模型目录不存在: {MODELS_DIR}")
        return

    removed = []
    kept = []

    for name in get_all_subdirs(MODELS_DIR):
        full_path = os.path.join(MODELS_DIR, name)
        rel_path = os.path.relpath(full_path, MODELS_DIR)

        if is_in_keep_list(rel_path):
            kept.append(rel_path)
            print(f"[保留] {rel_path}")
        else:
            print(f"[删除] {rel_path}")
            try:
                shutil.rmtree(full_path)
                removed.append(rel_path)
            except Exception as e:
                print(f"  删除失败: {e}")

    print(f"\n清理完成: 保留 {len(kept)} 个, 删除 {len(removed)} 个")
    if removed:
        print("已删除的目录:")
        for r in removed:
            print(f"  - {r}")

def cleanup_cache_dirs():
    """清理常见缓存目录中的 ASR 相关缓存"""
    cache_dirs = [
        os.path.expanduser("~/.cache/modelscope"),
        os.path.expanduser("~/.cache/huggingface"),
        os.path.expanduser("~/.cache/torch"),
        os.path.expanduser("~/.cache/opus"),
    ]

    for cache_dir in cache_dirs:
        if not os.path.exists(cache_dir):
            continue
        print(f"\n检查缓存目录: {cache_dir}")
        for name in get_all_subdirs(cache_dir):
            name_lower = name.lower()
            # 保留与三个模型相关的缓存
            if any(k.lower().replace("/", "_") in name_lower for k in KEEP_MODELS):
                print(f"  [保留缓存] {name}")
                continue
            # 删除其他 ASR/TTS/LLM 相关缓存
            if any(keyword in name_lower for keyword in [
                "asr", "tts", "whisper", "paraformer", "sensevoice",
                "qwen", "gemma", "chatglm", "llm", "model",
            ]):
                full_path = os.path.join(cache_dir, name)
                print(f"  [删除缓存] {name}")
                try:
                    shutil.rmtree(full_path)
                except Exception as e:
                    print(f"    删除失败: {e}")

if __name__ == "__main__":
    print("=" * 60)
    print("HomeBrain ASR 模型目录清理工具")
    print("=" * 60)
    print(f"目标模型: {', '.join(KEEP_MODELS)}")
    print(f"模型目录: {MODELS_DIR}")
    print("=" * 60)

    confirm = input("\n确认清理？这将删除不在保留列表中的所有模型文件 (yes/no): ")
    if confirm.lower() != "yes":
        print("已取消")
        sys.exit(0)

    cleanup_models_dir()
    cleanup_cache_dirs()
    print("\n清理完成!")
