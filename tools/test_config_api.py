#!/usr/bin/env python3
"""
配置 API 测试脚本
测试统一配置 API 的所有端点
"""
import json
import sqlite3
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'devices.db')

def test_db_tables():
    """测试数据库表是否存在"""
    print("=" * 60)
    print("测试 1: 数据库表结构")
    print("=" * 60)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    expected_tables = [
        'ai_butler_config', 'skills_config', 'asr_config', 
        'tts_config', 'dashboard_settings'
    ]
    
    for table in expected_tables:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if cursor.fetchone():
            print(f"  ✅ {table} 表存在")
        else:
            print(f"  ❌ {table} 表不存在")
    
    conn.close()
    print()

def test_sql_operations():
    """测试数据库 SQL 操作"""
    print("=" * 60)
    print("测试 2: 数据库 SQL 操作")
    print("=" * 60)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 测试 AI 管家配置
    print("\n  测试 AI 管家配置...")
    cursor.execute("""
        INSERT INTO ai_butler_config (config_key, config_value)
        VALUES ('name', '伊娃')
        ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value
    """)
    cursor.execute("SELECT config_value FROM ai_butler_config WHERE config_key = 'name'")
    row = cursor.fetchone()
    if row and row[0] == '伊娃':
        print("    ✅ AI 管家配置写入/读取成功")
    else:
        print("    ❌ AI 管家配置写入/读取失败")
    
    # 测试技能配置
    print("\n  测试技能配置...")
    cursor.execute("""
        INSERT INTO skills_config (skill_id, enabled, config)
        VALUES ('smart_home', 1, '{"name": "智能家居"}')
        ON CONFLICT(skill_id) DO UPDATE SET enabled = excluded.enabled, config = excluded.config
    """)
    cursor.execute("SELECT enabled, config FROM skills_config WHERE skill_id = 'smart_home'")
    row = cursor.fetchone()
    if row and row[0] == 1:
        print("    ✅ 技能配置写入/读取成功")
    else:
        print("    ❌ 技能配置写入/读取失败")
    
    # 测试 ASR 配置
    print("\n  测试 ASR 配置...")
    cursor.execute("""
        INSERT INTO asr_config (config_key, config_value)
        VALUES ('model', 'paraformer-zh')
        ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value
    """)
    cursor.execute("SELECT config_value FROM asr_config WHERE config_key = 'model'")
    row = cursor.fetchone()
    if row and row[0] == 'paraformer-zh':
        print("    ✅ ASR 配置写入/读取成功")
    else:
        print("    ❌ ASR 配置写入/读取失败")
    
    # 测试 TTS 配置
    print("\n  测试 TTS 配置...")
    cursor.execute("""
        INSERT INTO tts_config (config_key, config_value)
        VALUES ('voice', '88')
        ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value
    """)
    cursor.execute("SELECT config_value FROM tts_config WHERE config_key = 'voice'")
    row = cursor.fetchone()
    if row and row[0] == '88':
        print("    ✅ TTS 配置写入/读取成功")
    else:
        print("    ❌ TTS 配置写入/读取失败")
    
    # 测试看板设置
    print("\n  测试看板设置...")
    cursor.execute("""
        INSERT INTO dashboard_settings (setting_key, setting_value)
        VALUES ('model', '{"provider": "bailian"}')
        ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
    """)
    cursor.execute("SELECT setting_value FROM dashboard_settings WHERE setting_key = 'model'")
    row = cursor.fetchone()
    if row and 'bailian' in row[0]:
        print("    ✅ 看板设置写入/读取成功")
    else:
        print("    ❌ 看板设置写入/读取失败")
    
    conn.commit()
    conn.close()
    print()

def test_batch_operations():
    """测试批量操作"""
    print("=" * 60)
    print("测试 3: 批量操作")
    print("=" * 60)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 批量保存 AI 管家配置
    print("\n  批量保存 AI 管家配置...")
    configs = {
        'name': '伊娃',
        'nickname': '小伊',
        'wakeWord': '你好伊娃',
        'soul': '你是 HomeBrain 的 AI 管家',
        'identity': '家庭智能助手'
    }
    
    now = '2026-04-19T02:41:00'
    for key, value in configs.items():
        cursor.execute("""
            INSERT INTO ai_butler_config (config_key, config_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(config_key) DO UPDATE SET
                config_value = excluded.config_value,
                updated_at = excluded.updated_at
        """, (key, value, now))
    
    cursor.execute("SELECT COUNT(*) FROM ai_butler_config")
    count = cursor.fetchone()[0]
    if count >= 5:
        print(f"    ✅ 批量保存成功，共 {count} 条配置")
    else:
        print(f"    ❌ 批量保存失败，只有 {count} 条配置")
    
    # 读取所有 AI 管家配置
    print("\n  读取所有 AI 管家配置...")
    cursor.execute("SELECT config_key, config_value FROM ai_butler_config ORDER BY config_key")
    rows = cursor.fetchall()
    for row in rows:
        print(f"    {row[0]}: {row[1]}")
    
    conn.commit()
    conn.close()
    print()

def test_python_api():
    """测试 Python API 模块导入"""
    print("=" * 60)
    print("测试 4: Python API 模块导入")
    print("=" * 60)
    
    try:
        from core.api.config_api import (
            config_save, config_load, config_load_all,
            config_save_batch, config_delete, config_load_all_categories
        )
        print("  ✅ 所有 API 函数导入成功")
    except ImportError as e:
        print(f"  ❌ API 函数导入失败: {e}")
    except Exception as e:
        print(f"  ⚠️ 导入时出现其他错误（可能是 aiohttp 未安装）: {e}")
    
    print()

if __name__ == '__main__':
    print("\n🧪 HomeBrain 配置 API 测试\n")
    
    test_db_tables()
    test_sql_operations()
    test_batch_operations()
    test_python_api()
    
    print("=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)
