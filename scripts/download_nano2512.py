#!/usr/bin/env python3
"""
Fun-ASR-Nano-2512 模型下载脚本

由于 Nano-2512 模型的公开下载源不确定，此脚本提供两种下载方式：
1. modelscope 下载（如果模型已上传到 modelscope）
2. 手动放置（如果模型来自其他来源）

需要的文件结构（models/FunAudioLLM/Fun-ASR-Nano-2512/）：
    encoder_adaptor.onnx
    llm.fp32.onnx
    embedding.onnx
    Qwen3-0.6B/  (tokenizer 目录)

用法:
    cd server/homebrain-server
    python scripts/download_nano2512.py --source modelscope --model_id <模型ID>
    python scripts/download_nano2512.py --source local --from_dir <本地模型路径>
"""
import os
import sys
import shutil
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(PROJECT_ROOT, "models", "FunAudioLLM", "Fun-ASR-Nano-2512")

REQUIRED_FILES = [
    "encoder_adaptor.onnx",
    "llm.fp32.onnx",
    "embedding.onnx",
]
REQUIRED_DIRS = [
    "Qwen3-0.6B",
]


def check_model_complete():
    """检查模型是否已完整下载"""
    if not os.path.exists(TARGET_DIR):
        return False
    for f in REQUIRED_FILES:
        if not os.path.exists(os.path.join(TARGET_DIR, f)):
            print(f"[缺失] {f}")
            return False
    for d in REQUIRED_DIRS:
        if not os.path.exists(os.path.join(TARGET_DIR, d)):
            print(f"[缺失] {d}/")
            return False
    print(f"[完成] 模型已存在于: {TARGET_DIR}")
    return True


def download_from_modelscope(model_id):
    """从 modelscope 下载模型"""
    try:
        from modelscope import snapshot_download
        print(f"[下载] 从 modelscope 下载: {model_id}")
        os.makedirs(TARGET_DIR, exist_ok=True)
        snapshot_download(model_id, local_dir=TARGET_DIR)
        print(f"[完成] 模型已下载到: {TARGET_DIR}")
        return True
    except ImportError:
        print("[错误] 未安装 modelscope，请先执行: pip install modelscope")
        return False
    except Exception as e:
        print(f"[错误] 下载失败: {e}")
        return False


def copy_from_local(source_dir):
    """从本地目录复制模型文件"""
    if not os.path.exists(source_dir):
        print(f"[错误] 源目录不存在: {source_dir}")
        return False

    os.makedirs(TARGET_DIR, exist_ok=True)
    for f in REQUIRED_FILES:
        src = os.path.join(source_dir, f)
        dst = os.path.join(TARGET_DIR, f)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"[复制] {f}")
        else:
            print(f"[警告] 源目录缺少: {f}")

    for d in REQUIRED_DIRS:
        src = os.path.join(source_dir, d)
        dst = os.path.join(TARGET_DIR, d)
        if os.path.exists(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"[复制] {d}/")
        else:
            print(f"[警告] 源目录缺少: {d}/")

    print(f"[完成] 模型已复制到: {TARGET_DIR}")
    return True


def main():
    parser = argparse.ArgumentParser(description="下载 Fun-ASR-Nano-2512 模型")
    parser.add_argument("--source", choices=["modelscope", "local"], required=True,
                        help="下载来源")
    parser.add_argument("--model_id", default="FunAudioLLM/Fun-ASR-Nano-2512",
                        help="modelscope 模型 ID")
    parser.add_argument("--from_dir", help="本地模型源目录路径")
    args = parser.parse_args()

    print("=" * 60)
    print("Fun-ASR-Nano-2512 模型下载工具")
    print("=" * 60)
    print(f"目标目录: {TARGET_DIR}")

    if check_model_complete():
        overwrite = input("\n模型已存在，是否重新下载/复制? (yes/no): ")
        if overwrite.lower() != "yes":
            print("已取消")
            return

    if args.source == "modelscope":
        download_from_modelscope(args.model_id)
    elif args.source == "local":
        if not args.from_dir:
            print("[错误] --from_dir 是必需的")
            sys.exit(1)
        copy_from_local(args.from_dir)

    print("\n验证模型完整性...")
    if check_model_complete():
        print("✅ 模型完整!")
    else:
        print("⚠️ 模型不完整，请检查下载/复制过程")


if __name__ == "__main__":
    main()
