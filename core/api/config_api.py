"""
配置持久化存储API
用于保存家庭看板、控制台、AI管家、技能、ASR、TTS等所有配置
"""
import asyncio
import json
import sqlite3
import time
import yaml
from aiohttp import web
from pathlib import Path
from datetime import datetime
import os
from config.config_loader import load_yaml_expanded
from config.logger import setup_logging

# 本模块历史上一直用 print 打日志，但 print 不进 tmp/server.log，
# 唤醒词热更新这种事必须事后能核对「内存里换没换」，所以这一条走 logger。
logger = setup_logging()

# 全局WebSocket服务器实例，用于重新加载ASR模块
_ws_server_instance = None

TAG = __name__

def set_websocket_server_instance(ws_server):
    """设置WebSocket服务器实例"""
    global _ws_server_instance
    _ws_server_instance = ws_server


def get_websocket_server_instance():
    """获取WebSocket服务器实例（插件广播用）"""
    return _ws_server_instance

DB_PATH = Path(__file__).parent.parent.parent / "data" / "devices.db"
CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


def update_llm_config_in_yaml(provider, api_key=None, url=None, model_name=None):
    """
    更新 config.yaml 文件中的 LLM 配置

    Args:
        provider: LLM 提供商 (e.g., 'google/gemma-3-1b', 'qwen3.6-plus')
        api_key: API 密钥
        url: API URL（云端模型的 API 地址）
        model_name: 模型名称
    """
    try:
        # 读取现有配置
        with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)

        # 确定提供商类型和模型名称
        provider_lower = provider.lower()
        if 'openai' in provider_lower or 'gpt' in provider_lower:
            llm_type = 'openai'
        elif 'google' in provider_lower:
            llm_type = 'openai'  # Google models typically use OpenAI-compatible API
        elif 'gemini' in provider_lower:
            llm_type = 'gemini'
        elif 'zhipu' in provider_lower:
            llm_type = 'openai'
        elif 'doubao' in provider_lower:
            llm_type = 'openai'
        elif 'ollama' in provider_lower:
            llm_type = 'ollama'
        else:
            llm_type = 'openai'  # 默认类型（OpenAI-compatible）

        # 如果没有提供 model_name，从 provider 中提取
        if not model_name:
            model_name = provider

        # 创建新的 LLM 配置
        user_defined_llm_config = {
            'type': llm_type,
            'model_name': model_name,
            'temperature': 0.7,
            'max_tokens': 500
        }

        # 添加 API 密钥
        if api_key:
            # 2026-10-05 密钥规范：真值归集到 .env，yaml 里只留 ${} 引用。
            # data/.config.yaml / config.yaml 都是被 git 跟踪的，明文密钥一旦提交
            # 就永久留在历史里。详见 core/utils/secret_store.py。
            from core.utils.secret_store import to_ref
            user_defined_llm_config['api_key'] = to_ref("LLM", provider, "api_key", api_key)

        # 处理 URL/base_url 配置
        # 关键修复：如果传入 url 参数，优先使用传入的 url，不使用默认值
        if url:
            # 用户指定的 URL，直接使用（适用于所有云端模型）
            user_defined_llm_config['url'] = url
            # 同时设置 base_url 以兼容不同的 LLM provider 实现
            user_defined_llm_config['base_url'] = url
        else:
            # 只有在用户没有指定 URL 时才使用默认值（本地模型场景）
            if llm_type == 'gemini':
                # Gemini 使用 api_key 认证，不需要 url
                pass
            elif llm_type == 'ollama':
                user_defined_llm_config['base_url'] = 'http://localhost:11434'
            elif llm_type == 'openai':
                # 根据 provider 设置默认 URL（仅用于本地模型）
                if 'ollama' in provider_lower:
                    user_defined_llm_config['base_url'] = 'http://localhost:11434'
                elif 'localhost' in provider_lower or 'local' in provider_lower:
                    # 本地模型，不设置默认 URL，让用户手动配置
                    pass
                # 云端模型如果没有传入 url，说明配置有问题，不设置默认值

        # 更新 LLM 部分：删除原有模型定义，只保留用户定义的模型
        if 'LLM' not in config:
            config['LLM'] = {}

        # 加固③（2026-09-17）：原实现 config['LLM'] = {...} 会把 config.yaml 里其它 LLM 引擎
        # （如 OllamaLLM / CustomLLM 等）整体清空，导致切换一次模型后原有引擎定义全部丢失、无法切回。
        # 改为只更新目标引擎 UserDefinedLLM，其余引擎原样保留。
        if not isinstance(config.get('LLM'), dict):
            config['LLM'] = {}
        config['LLM']['UserDefinedLLM'] = user_defined_llm_config

        # 更新 selected_module.LLM 配置
        if 'selected_module' not in config:
            config['selected_module'] = {}
        config['selected_module']['LLM'] = 'UserDefinedLLM'

        # 写回配置文件
        with open(CONFIG_PATH, 'w', encoding='utf-8') as file:
            yaml.dump(config, file, default_flow_style=False, allow_unicode=True, indent=2)

        # 同时更新 data/.config.yaml（实际运行时优先级更高的自定义配置）
        try:
            custom_config_path = Path(__file__).parent.parent.parent / "data" / ".config.yaml"
            custom_config = {}
            if custom_config_path.exists():
                with open(custom_config_path, 'r', encoding='utf-8') as file:
                    custom_config = yaml.safe_load(file) or {}

            # 同步 LLM 配置到 data/.config.yaml
            if 'LLM' not in custom_config:
                custom_config['LLM'] = {}
            if 'selected_module' not in custom_config:
                custom_config['selected_module'] = {}

            custom_config['LLM']['UserDefinedLLM'] = user_defined_llm_config
            custom_config['selected_module']['LLM'] = 'UserDefinedLLM'

            with open(custom_config_path, 'w', encoding='utf-8') as file:
                yaml.dump(custom_config, file, default_flow_style=False, allow_unicode=True, indent=2)
            print(f"LLM config also updated in data/.config.yaml")
        except Exception as custom_e:
            print(f"Warning: failed to update data/.config.yaml: {custom_e}")

        return True
    except Exception as e:
        print(f"Error updating config.yaml: {str(e)}")
        return False




def update_asr_config_in_yaml(provider=None, model_dir=None, asr_type=None, streaming=None, chunk_size=None, encoder_chunk_look_back=None, decoder_chunk_look_back=None, model_type=None):
    """
    更新 config.yaml 文件中的 ASR 配置
    支持两种模式：
    - Paraformer-zh 真流式（fun_local）
    - SenseVoice 整句离线（sherpa_onnx_local + sense_voice）

    Args:
        provider: ASR 配置项名称 (FunASR / SenseVoiceASR)
        model_dir: 模型路径
        asr_type: Provider 类型 (fun_local / sherpa_onnx_local)
        streaming: 是否流式 (true/false)
        chunk_size: chunk_size 列表
        encoder_chunk_look_back: encoder look back
        decoder_chunk_look_back: decoder look back
        model_type: 模型子类型 (sense_voice / paraformer)
    """
    try:
        import yaml
        with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)

        if 'ASR' not in config:
            config['ASR'] = {}
        if 'selected_module' not in config:
            config['selected_module'] = {}

        # 根据 asr_type 和 model_type 决定写入哪个配置项
        if asr_type == 'fun_local':
            # Paraformer-zh 真流式
            paraformer_config = {
                'chunk_size': [0, 10, 5],
                'decoder_chunk_look_back': 1,
                'encoder_chunk_look_back': 4,
                'model_dir': model_dir or 'models/paraformer-zh-streaming',
                'output_dir': 'tmp/',
                'streaming': True,
                'type': 'fun_local',
            }
            config['ASR']['FunASR'] = paraformer_config
            config['selected_module']['ASR'] = 'FunASR'

        elif asr_type == 'sherpa_onnx_local':
            if model_type == 'sense_voice':
                # SenseVoice 整句离线
                sensevoice_config = {
                    'model_dir': model_dir or 'models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',
                    'model_type': 'sense_voice',
                    'output_dir': 'tmp/',
                    'streaming': False,
                    'type': 'sherpa_onnx_local',
                }
                config['ASR']['SenseVoiceASR'] = sensevoice_config
                config['selected_module']['ASR'] = 'SenseVoiceASR'

            else:
                # 默认：sherpa-onnx paraformer small
                paraformer_config = {
                    'model_dir': model_dir or 'models/sherpa-onnx-paraformer-zh-small-2024-03-09',
                    'model_type': 'paraformer',
                    'output_dir': 'tmp/',
                    'streaming': True,
                    'type': 'sherpa_onnx_local',
                }
                config['ASR']['SherpaParaformerASR'] = paraformer_config
                config['selected_module']['ASR'] = 'SherpaParaformerASR'

        else:
            # 未知类型，默认写入 FunASR
            default_config = {
                'model_dir': model_dir or 'models/paraformer-zh-streaming',
                'output_dir': 'tmp/',
                'streaming': True,
                'type': asr_type or 'fun_local',
            }
            config['ASR']['FunASR'] = default_config
            config['selected_module']['ASR'] = 'FunASR'

        # 写回默认配置文件
        with open(CONFIG_PATH, 'w', encoding='utf-8') as file:
            yaml.dump(config, file, default_flow_style=False, allow_unicode=True, indent=2)

        # 同时更新 data/.config.yaml（实际运行时优先级更高的自定义配置）
        try:
            custom_config_path = Path(__file__).parent.parent.parent / "data" / ".config.yaml"
            custom_config = {}
            if custom_config_path.exists():
                with open(custom_config_path, 'r', encoding='utf-8') as file:
                    custom_config = yaml.safe_load(file) or {}

            if 'ASR' not in custom_config:
                custom_config['ASR'] = {}
            if 'selected_module' not in custom_config:
                custom_config['selected_module'] = {}

            # 同步 ASR 配置项和 selected_module（保持与 config.yaml 一致）
            if asr_type == 'fun_local':
                custom_config['ASR']['FunASR'] = {
                    'chunk_size': [0, 10, 5],
                    'decoder_chunk_look_back': 1,
                    'encoder_chunk_look_back': 4,
                    'model_dir': model_dir or 'models/paraformer-zh-streaming',
                    'output_dir': 'tmp/',
                    'streaming': True,
                    'type': 'fun_local',
                }
                custom_config['selected_module']['ASR'] = 'FunASR'
            elif asr_type == 'sherpa_onnx_local':
                if model_type == 'sense_voice':
                    custom_config['ASR']['SenseVoiceASR'] = {
                        'model_dir': model_dir or 'models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',
                        'model_type': 'sense_voice',
                        'output_dir': 'tmp/',
                        'streaming': False,
                        'type': 'sherpa_onnx_local',
                    }
                    custom_config['selected_module']['ASR'] = 'SenseVoiceASR'
                else:
                    custom_config['ASR']['SherpaParaformerASR'] = {
                        'model_dir': model_dir or 'models/sherpa-onnx-paraformer-zh-small-2024-03-09',
                        'model_type': 'paraformer',
                        'output_dir': 'tmp/',
                        'streaming': True,
                        'type': 'sherpa_onnx_local',
                    }
                    custom_config['selected_module']['ASR'] = 'SherpaParaformerASR'
            else:
                custom_config['ASR']['FunASR'] = {
                    'model_dir': model_dir or 'models/paraformer-zh-streaming',
                    'output_dir': 'tmp/',
                    'streaming': True,
                    'type': asr_type or 'fun_local',
                }
                custom_config['selected_module']['ASR'] = 'FunASR'

            with open(custom_config_path, 'w', encoding='utf-8') as file:
                yaml.dump(custom_config, file, default_flow_style=False, allow_unicode=True, indent=2)
            print(f"ASR config also updated in data/.config.yaml")
        except Exception as custom_e:
            print(f"Warning: failed to update data/.config.yaml: {custom_e}")

        print(f"ASR config updated in YAML: provider={provider}, model_dir={model_dir}, type={asr_type}, model_type={model_type}, selected={config['selected_module']['ASR']}")
        return True
    except Exception as e:
        print(f"Error updating ASR config in YAML: {str(e)}")
        return False

# ============================================================
# 原有 API（向后兼容）
# ============================================================

async def get_config(request):
    """获取配置"""
    try:
        config_key = request.query.get('key', 'dashboard')
        
        conn = sqlite3.connect(DB_PATH)
        cursor = None
        try:
            cursor = conn.cursor()
            
            cursor.execute("SELECT value, updated_at FROM config_storage WHERE key=?", (config_key,))
            row = cursor.fetchone()
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass
        
        if row:
            return web.json_response({
                'success': True,
                'key': config_key,
                'value': json.loads(row[0]),
                'updated_at': row[1]
            })
        else:
            return web.json_response({
                'success': True,
                'key': config_key,
                'value': {},
                'message': '配置不存在，返回默认值'
            })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def save_config(request):
    """保存配置"""
    try:
        data = await request.json()
        config_key = data.get('key', 'dashboard')
        config_value = data.get('value', {})
        
        conn = sqlite3.connect(DB_PATH)
        cursor = None
        try:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO config_storage (key, value, updated_at)
                VALUES (?, ?, ?)
            """, (config_key, json.dumps(config_value, ensure_ascii=False), datetime.now().isoformat()))
            
            conn.commit()
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass
        
        return web.json_response({
            'success': True,
            'message': f'配置 {config_key} 已保存'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def get_dashboard_config(request):
    """获取家庭看板配置（从 dashboard_config 表读取，与控制台共享数据源）"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = None
        try:
            cursor = conn.cursor()
            
            # 从 dashboard_config 表读取所有配置项（控制台保存的数据源）
            cursor.execute("SELECT config_key, config_value FROM dashboard_config WHERE user_id='default'")
            rows = cursor.fetchall()
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass
        
        config = {}
        for row in rows:
            try:
                config[row[0]] = json.loads(row[1])
            except:
                config[row[0]] = row[1]
        
        if config:
            # 返回看板前端期望的扁平格式
            return web.json_response({
                'success': True,
                'weather': config.get('weather', {}),
                'services': config.get('services', {}),
                'serviceDetails': config.get('serviceDetails', {}),
                'album': config.get('album', {}),
                'bills': config.get('bills', {}),
                'express': config.get('express', {}),
                'modules': config.get('modules', {}),
                'layout': config.get('layout', {}),
                'theme': config.get('theme', 'light')
            })
        else:
            return web.json_response({
                'success': True,
                'weather': {},
                'services': {},
                'serviceDetails': {},
                'album': {},
                'bills': {},
                'express': {},
                'modules': {},
                'layout': {},
                'theme': 'light',
                'message': '使用默认配置'
            })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def save_dashboard_config(request):
    """保存家庭看板配置（同时写入 dashboard_config 表，与控制台共享）"""
    try:
        data = await request.json()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = None
        try:
            cursor = conn.cursor()
            
            # 写入 config_storage（向后兼容）
            cursor.execute("""
                INSERT OR REPLACE INTO config_storage (key, value, updated_at)
                VALUES ('dashboard', ?, ?)
            """, (json.dumps(data, ensure_ascii=False), datetime.now().isoformat()))
            
            # 同时写入 dashboard_config 表（与控制台共享数据源）
            for key, value in data.items():
                cursor.execute("""
                    INSERT OR REPLACE INTO dashboard_config 
                    (user_id, config_key, config_value, updated_at)
                    VALUES ('default', ?, ?, ?)
                """, (key, json.dumps(value, ensure_ascii=False), datetime.now().isoformat()))
            
            conn.commit()
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass
        
        return web.json_response({
            'success': True,
            'message': '家庭看板配置已保存'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

async def get_all_configs(request):
    """获取所有配置"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = None
        try:
            cursor = conn.cursor()
            
            cursor.execute("SELECT key, value, updated_at FROM config_storage")
            rows = cursor.fetchall()
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass
        
        configs = {}
        for row in rows:
            configs[row[0]] = {
                'value': json.loads(row[1]),
                'updated_at': row[2]
            }
        
        return web.json_response({
            'success': True,
            'configs': configs
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': str(e)
        })

# ============================================================
# 统一配置 API（新增 - 商用级）
# ============================================================

# 分类到数据库表的映射
CATEGORY_TABLE_MAP = {
    'ai_butler': 'ai_butler_config',
    'skills': 'skills_config',
    'asr': 'asr_config',
    'tts': 'tts_config',
    'llm': 'llm_config',
    'vad': 'vad_config',
    'dashboard': 'dashboard_settings',
}

# 各表的 key/value 列名
TABLE_KEY_MAP = {
    'ai_butler_config': ('config_key', 'config_value'),
    'skills_config': ('skill_id', 'config'),
    'asr_config': ('config_key', 'config_value'),
    'tts_config': ('config_key', 'config_value'),
    'llm_config': ('config_key', 'config_value'),
    'vad_config': ('config_key', 'config_value'),
    'dashboard_settings': ('setting_key', 'setting_value'),
}


def _get_db_connection():
    """获取数据库连接（统一入口，启用WAL模式支持并发）"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # 启用 WAL 模式，支持并发读写
    c1 = conn.execute("PRAGMA journal_mode=WAL")
    c1.close()
    # 设置繁忙超时 30秒
    c2 = conn.execute("PRAGMA busy_timeout=30000")
    c2.close()
    return conn


def _get_table_and_columns(category):
    """根据分类获取表名和列名"""
    table = CATEGORY_TABLE_MAP.get(category)
    if not table:
        return None, None, None
    key_col, val_col = TABLE_KEY_MAP[table]
    return table, key_col, val_col


async def config_save(request):
    """
    保存单个配置项
    POST /api/config/save
    Body: { "category": "ai_butler", "key": "voice", "value": "5002" }
    """
    try:
        data = await request.json()
        category = data.get('category', '').strip()
        key = data.get('key', '').strip()
        value = data.get('value')

        if not category or not key:
            return web.json_response({
                'success': False,
                'message': '缺少必要参数: category, key'
            }, status=400)

        table, key_col, val_col = _get_table_and_columns(category)
        if not table:
            return web.json_response({
                'success': False,
                'message': f'不支持的分类: {category}'
            }, status=400)

        # 2026-10-05 加固：非 ASR 表不允许写 type 键，理由同 config_save_batch。
        # 引擎切换只走 /api/config/switch-provider（改 selected_module）。
        if table != 'asr_config' and key == 'type':
            return web.json_response({
                'success': False,
                'message': f'{category} 的引擎类型不允许在此修改，请使用「应用」按钮切换 provider'
            }, status=400)

        # skills_config 特殊处理：value 是 JSON 对象 {enabled, config, deleted}
        if table == 'skills_config':
            enabled = 1
            config_json = None
            deleted = 0
            if isinstance(value, dict):
                enabled = 1 if value.get('enabled', True) else 0
                config_json = json.dumps(value.get('config', {}), ensure_ascii=False)
                deleted = 1 if value.get('deleted', False) else 0
            else:
                config_json = json.dumps(value, ensure_ascii=False) if value is not None else None

            conn = _get_db_connection()
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute(f"""
                INSERT INTO skills_config (skill_id, enabled, config, deleted, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    config = excluded.config,
                    deleted = excluded.deleted,
                    updated_at = excluded.updated_at
            """, (key, enabled, config_json, deleted, datetime.now().isoformat()))
                conn.commit()
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass
        else:
            value_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value

            conn = _get_db_connection()
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute(f"""
                INSERT INTO {table} ({key_col}, {val_col}, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT({key_col}) DO UPDATE SET
                    {val_col} = excluded.{val_col},
                    updated_at = excluded.updated_at
            """, (key, value_str, datetime.now().isoformat()))
                # TTS 参数是引擎专属的（voice/volume/speed 各引擎刻度与取值域不同），
                # 保存时记下当前选中的引擎，供 initialize_tts 判断是否该合并（2026-09-12）
                if table == 'tts_config' and key != 'provider':
                    try:
                        from config.config_loader import load_config
                        cur_tts = str((load_config().get("selected_module") or {}).get("TTS", ""))
                        if cur_tts:
                            cursor.execute("""
                            INSERT INTO tts_config (config_key, config_value, updated_at)
                            VALUES ('provider', ?, ?)
                            ON CONFLICT(config_key) DO UPDATE SET
                                config_value = excluded.config_value,
                                updated_at = excluded.updated_at
                        """, (cur_tts, datetime.now().isoformat()))
                    except Exception:
                        pass
                conn.commit()
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass

        # 如果是 LLM 配置，同时更新 config.yaml 文件并触发模块重载
        if category == 'llm':
            # 重新读取所有 LLM 配置并更新 YAML 文件
            conn = _get_db_connection()
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_key, config_value FROM llm_config")
                rows = cursor.fetchall()

                # 构建配置字典
                llm_config = {}
                for row in rows:
                    try:
                        # 尝试解析 JSON 值，否则直接使用字符串值
                        llm_config[row[0]] = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        llm_config[row[0]] = row[1]

                # 提取关键配置项
                provider = llm_config.get('provider', '')
                api_key = llm_config.get('api_key')
                # 优先使用控制台保存的 model/base_url，兼容旧的 model_name/url
                url = llm_config.get('base_url') or llm_config.get('url')
                model_name = llm_config.get('model') or llm_config.get('model_name')

                # 更新 YAML 配置文件
                update_llm_config_in_yaml(provider, api_key, url, model_name)

                # 触发服务器重新加载 LLM 模块
                if _ws_server_instance:
                    try:
                        import asyncio
                        # 使用 asyncio.create_task 在后台执行异步更新
                        asyncio.create_task(_ws_server_instance.update_config())
                        print(f"[config_api] 已触发服务器 LLM 模块重载")
                    except Exception as e:
                        print(f"[config_api] 触发模块重载失败: {str(e)}")

            except Exception as e:
                print(f"Error updating YAML config: {str(e)}")
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass


        # 如果是 ASR 配置，同时更新 config.yaml 文件
        if category == 'asr':
            conn = _get_db_connection()
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_key, config_value FROM asr_config")
                rows = cursor.fetchall()

                asr_config = {}
                for row in rows:
                    try:
                        asr_config[row[0]] = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        asr_config[row[0]] = row[1]

                # 确保 model 和 model_dir 一致
                if 'model' in asr_config and 'model_dir' not in asr_config:
                    asr_config['model_dir'] = asr_config['model']
                    cursor.execute('INSERT OR REPLACE INTO asr_config (config_key, config_value, updated_at) VALUES (?, ?, ?)',
                                   ('model_dir', asr_config['model'], datetime.now().isoformat()))
                elif 'model_dir' in asr_config and 'model' not in asr_config:
                    asr_config['model'] = asr_config['model_dir']
                    cursor.execute('INSERT OR REPLACE INTO asr_config (config_key, config_value, updated_at) VALUES (?, ?, ?)',
                                   ('model', asr_config['model_dir'], datetime.now().isoformat()))
                elif 'model' in asr_config and 'model_dir' in asr_config and asr_config['model'] != asr_config['model_dir']:
                    # 不一致时，以 model_dir 为准
                    asr_config['model'] = asr_config['model_dir']
                    cursor.execute('INSERT OR REPLACE INTO asr_config (config_key, config_value, updated_at) VALUES (?, ?, ?)',
                                   ('model', asr_config['model_dir'], datetime.now().isoformat()))
                    conn.commit()

                provider = asr_config.get('provider', 'FunASR')
                model_dir = asr_config.get('model_dir', '')
                asr_type = asr_config.get('type', 'fun_local')
                streaming = asr_config.get('streaming', 'true')
                model_type = asr_config.get('model_type', '')

                update_asr_config_in_yaml(
                    provider=provider,
                    model_dir=model_dir,
                    asr_type=asr_type,
                    streaming=streaming,
                    model_type=model_type
                )

                # 触发服务器重新加载 ASR 模块
                if _ws_server_instance:
                    try:
                        import asyncio
                        asyncio.create_task(_ws_server_instance.update_config())
                        print(f"[config_api] 已触发服务器 ASR 模块重载")
                    except Exception as e:
                        print(f"[config_api] 触发模块重载失败: {str(e)}")

            except Exception as e:
                print(f"Error updating ASR YAML config: {str(e)}")
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass

        # 如果是 TTS 配置，触发模块重载
        if category == 'tts':
            if _ws_server_instance:
                try:
                    import asyncio
                    asyncio.create_task(_ws_server_instance.update_config())
                    print(f"[config_api] 已触发服务器 TTS 模块重载")
                except Exception as e:
                    print(f"[config_api] 触发 TTS 模块重载失败: {str(e)}")

        return web.json_response({
            'success': True,
            'message': f'配置已保存: {category}.{key}'
        })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'保存配置失败: {str(e)}'
        }, status=500)


async def config_load(request):
    """
    读取单个配置项
    GET /api/config/load?category=ai_butler&key=voice
    """
    try:
        category = request.query.get('category', '').strip()
        key = request.query.get('key', '').strip()

        if not category or not key:
            return web.json_response({
                'success': False,
                'message': '缺少必要参数: category, key'
            }, status=400)

        table, key_col, val_col = _get_table_and_columns(category)
        if not table:
            return web.json_response({
                'success': False,
                'message': f'不支持的分类: {category}'
            }, status=400)

        conn = _get_db_connection()
        cursor = None
        try:
            cursor = conn.cursor()

            if table == 'skills_config':
                cursor.execute(f"""
                    SELECT skill_id, enabled, config, updated_at
                    FROM {table} WHERE {key_col} = ?
                """, (key,))
                row = cursor.fetchone()
                if row:
                    result = {
                        'skill_id': row['skill_id'],
                        'enabled': bool(row['enabled']),
                        'config': json.loads(row['config']) if row['config'] else {},
                        'updated_at': row['updated_at']
                    }
                else:
                    result = None
            else:
                cursor.execute(f"""
                    SELECT {key_col}, {val_col}, updated_at
                    FROM {table} WHERE {key_col} = ?
                """, (key,))
                row = cursor.fetchone()
                if row:
                    raw_value = row[val_col]
                    try:
                        parsed_value = json.loads(raw_value)
                    except (json.JSONDecodeError, TypeError):
                        parsed_value = raw_value
                    result = {
                        'key': row[key_col],
                        'value': parsed_value,
                        'updated_at': row['updated_at']
                    }
                else:
                    result = None

        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass

        if result:
            return web.json_response({
                'success': True,
                'category': category,
                'data': result
            })
        else:
            return web.json_response({
                'success': True,
                'category': category,
                'data': None,
                'message': '配置不存在'
            })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'读取配置失败: {str(e)}'
        }, status=500)


async def config_load_all(request):
    """
    读取某分类下的所有配置
    GET /api/config/load-all?category=ai_butler
    """
    try:
        category = request.query.get('category', '').strip()

        if not category:
            return web.json_response({
                'success': False,
                'message': '缺少必要参数: category'
            }, status=400)

        table, key_col, val_col = _get_table_and_columns(category)
        if not table:
            return web.json_response({
                'success': False,
                'message': f'不支持的分类: {category}'
            }, status=400)

        conn = _get_db_connection()
        cursor = None
        try:
            cursor = conn.cursor()

            if table == 'skills_config':
                cursor.execute(f"""
                    SELECT skill_id, enabled, config, deleted, updated_at
                    FROM {table} ORDER BY skill_id
                """)
                rows = cursor.fetchall()
                result = {}
                for row in rows:
                    result[row['skill_id']] = {
                        'enabled': bool(row['enabled']),
                        'config': json.loads(row['config']) if row['config'] else {},
                        'deleted': bool(row['deleted']),
                        'updated_at': row['updated_at']
                    }
            else:
                cursor.execute(f"""
                    SELECT {key_col}, {val_col}, updated_at
                    FROM {table} ORDER BY {key_col}
                """)
                rows = cursor.fetchall()
                result = {}
                for row in rows:
                    raw_value = row[val_col]
                    try:
                        parsed_value = json.loads(raw_value)
                    except (json.JSONDecodeError, TypeError):
                        parsed_value = raw_value
                    result[row[key_col]] = {
                        'value': parsed_value,
                        'updated_at': row['updated_at']
                    }

        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass

        return web.json_response({
            'success': True,
            'category': category,
            'data': result
        })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'读取配置失败: {str(e)}'
        }, status=500)


async def config_save_batch(request):
    """
    批量保存配置
    POST /api/config/save-batch
    Body: {
        "category": "ai_butler",
        "configs": {
            "voice": "5002",
            "name": "小智",
            "nickname": "智智"
        }
    }
    """
    try:
        data = await request.json()
        category = data.get('category', '').strip()
        configs = data.get('configs', {})

        if not category:
            return web.json_response({
                'success': False,
                'message': '缺少必要参数: category'
            }, status=400)

        if not isinstance(configs, dict):
            return web.json_response({
                'success': False,
                'message': 'configs 必须是对象'
            }, status=400)

        table, key_col, val_col = _get_table_and_columns(category)
        if not table:
            return web.json_response({
                'success': False,
                'message': f'不支持的分类: {category}'
            }, status=400)

        # 2026-10-05 加固：tts/llm/vad 等模块的引擎选择一律由 selected_module +
        # data/.config.yaml 决定（后台下拉框走 /api/config/switch-provider）。
        # 这些表里**不允许**出现 type 键 —— 一旦写进去，
        # core/utils/modules_initialize.py 里「数据库优先于配置文件」的规则
        # 就会反过来架空后台的切换操作，表现为「点应用提示成功、重启后没变」。
        # ASR 是唯一例外：它的 type 本来就该在 asr_config 表里
        # （切换时由 http_server._sync_asr_config_table 统一写入）。
        if table != 'asr_config':
            configs = {k: v for k, v in configs.items() if k != 'type'}

        conn = _get_db_connection()
        cursor = None
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            saved_count = 0

            if table == 'skills_config':
                for skill_id, value in configs.items():
                    enabled = 1
                    config_json = None
                    deleted = 0
                    if isinstance(value, dict):
                        enabled = 1 if value.get('enabled', True) else 0
                        config_json = json.dumps(value.get('config', {}), ensure_ascii=False)
                        deleted = 1 if value.get('deleted', False) else 0
                    else:
                        config_json = json.dumps(value, ensure_ascii=False) if value is not None else None

                    cursor.execute(f"""
                        INSERT INTO {table} (skill_id, enabled, config, deleted, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(skill_id) DO UPDATE SET
                            enabled = excluded.enabled,
                            config = excluded.config,
                            deleted = excluded.deleted,
                            updated_at = excluded.updated_at
                    """, (skill_id, enabled, config_json, deleted, now))
                    saved_count += 1
            else:
                for key, value in configs.items():
                    value_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                    cursor.execute(f"""
                        INSERT INTO {table} ({key_col}, {val_col}, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT({key_col}) DO UPDATE SET
                            {val_col} = excluded.{val_col},
                            updated_at = excluded.updated_at
                    """, (key, value_str, now))
                    saved_count += 1

            # 提交事务（在循环外部）
            conn.commit()
            
            # 强制 WAL checkpoint，释放锁
            checkpoint_cursor = None
            try:
                checkpoint_cursor = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except:
                pass
            finally:
                if checkpoint_cursor:
                    try:
                        checkpoint_cursor.close()
                    except:
                        pass

        finally:
            # 确保 cursor 在 conn 之前关闭
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            # 确保 conn 总是关闭
            if conn:
                try:
                    conn.close()
                except:
                    pass

        # 如果是 LLM 配置，同时更新 config.yaml 文件并触发模块重载
        if category == 'llm':
            # 重新读取所有 LLM 配置并更新 YAML 文件
            conn = _get_db_connection()
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_key, config_value FROM llm_config")
                rows = cursor.fetchall()

                # 构建配置字典
                llm_config = {}
                for row in rows:
                    try:
                        # 尝试解析 JSON 值，否则直接使用字符串值
                        llm_config[row[0]] = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        llm_config[row[0]] = row[1]

                # 提取关键配置项
                provider = llm_config.get('provider', '')
                api_key = llm_config.get('api_key')
                # 优先使用控制台保存的 model/base_url，兼容旧的 model_name/url
                url = llm_config.get('base_url') or llm_config.get('url')
                model_name = llm_config.get('model') or llm_config.get('model_name')

                # 更新 YAML 配置文件
                update_llm_config_in_yaml(provider, api_key, url, model_name)

                # 触发服务器重新加载 LLM 模块
                if _ws_server_instance:
                    try:
                        import asyncio
                        asyncio.create_task(_ws_server_instance.update_config())
                        print(f"[config_api] 已触发服务器 LLM 模块重载")
                    except Exception as e:
                        print(f"[config_api] 触发模块重载失败: {str(e)}")

            except Exception as e:
                print(f"Error updating YAML config: {str(e)}")
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass

        # 如果是 ASR 配置，同时更新 config.yaml 文件
        if category == 'asr':
            conn = _get_db_connection()
            cursor = None
            provider = 'FunASR'
            model_dir = ''
            asr_type = 'fun_local'
            streaming = 'true'
            model_type = ''
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config_key, config_value FROM asr_config")
                rows = cursor.fetchall()

                asr_config = {}
                for row in rows:
                    try:
                        asr_config[row[0]] = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        asr_config[row[0]] = row[1]

                # 确保 model 和 model_dir 一致
                need_sync = False
                if 'model' in asr_config and 'model_dir' not in asr_config:
                    asr_config['model_dir'] = asr_config['model']
                    cursor.execute('INSERT OR REPLACE INTO asr_config (config_key, config_value, updated_at) VALUES (?, ?, ?)',
                                   ('model_dir', asr_config['model'], datetime.now().isoformat()))
                    need_sync = True
                elif 'model_dir' in asr_config and 'model' not in asr_config:
                    asr_config['model'] = asr_config['model_dir']
                    cursor.execute('INSERT OR REPLACE INTO asr_config (config_key, config_value, updated_at) VALUES (?, ?, ?)',
                                   ('model', asr_config['model_dir'], datetime.now().isoformat()))
                    need_sync = True
                elif 'model' in asr_config and 'model_dir' in asr_config and asr_config['model'] != asr_config['model_dir']:
                    # 不一致时，以 model_dir 为准
                    asr_config['model'] = asr_config['model_dir']
                    cursor.execute('INSERT OR REPLACE INTO asr_config (config_key, config_value, updated_at) VALUES (?, ?, ?)',
                                   ('model', asr_config['model_dir'], datetime.now().isoformat()))
                    need_sync = True

                # 有变更时才 commit
                if need_sync:
                    conn.commit()

                provider = asr_config.get('provider', 'FunASR')
                model_dir = asr_config.get('model_dir', '')
                asr_type = asr_config.get('type', 'fun_local')
                streaming = asr_config.get('streaming', 'true')
                model_type = asr_config.get('model_type', '')

            except Exception as e:
                print(f"Error reading ASR config from DB: {str(e)}")
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass

            # YAML 更新放在 try/finally 外面，确保数据库连接已关闭后再操作文件
            try:
                update_asr_config_in_yaml(
                    provider=provider,
                    model_dir=model_dir,
                    asr_type=asr_type,
                    streaming=streaming,
                    model_type=model_type
                )

                # 触发服务器重新加载 ASR 模块
                if _ws_server_instance:
                    try:
                        import asyncio
                        asyncio.create_task(_ws_server_instance.update_config())
                        print(f"[config_api] 已触发服务器 ASR 模块重载")
                    except Exception as e:
                        print(f"[config_api] 触发模块重载失败: {str(e)}")

            except Exception as e:
                print(f"Error updating ASR YAML config: {str(e)}")

        # 如果是 TTS 配置，触发模块重载
        if category == 'tts':
            if _ws_server_instance:
                try:
                    import asyncio
                    asyncio.create_task(_ws_server_instance.update_config())
                    print(f"[config_api] 已触发服务器 TTS 模块重载")
                except Exception as e:
                    print(f"[config_api] 触发 TTS 模块重载失败: {str(e)}")

        # ========================================================================
        # AI 管家配置：保存后必须让内存里的单例失效，否则后台改了当场不生效
        #
        # 2026-10-16 修复（主人实测：后台把唤醒词改成「你好伊娃」后喊多少次都不醒，
        # 重启服务也没用）。根因是热更新代码一直写在从不被调用的入口上：
        #   - http_server.handle_save_ai_butler_config（路由 /api/ai-butler-config，
        #     10-13 修过一次，带热更新）
        #   - config_api.save_ai_butler_configs（死代码，register_routes 从未调用）
        # 而管理台「保存配置」按钮实际走的是本函数 /api/config/save-batch，
        # 它只写库、不碰内存。wakeword_provider 的检测器是进程级单例
        # （_wakeword_detector，首次使用时才从库里加载），于是内存里一直
        # 攥着改之前的旧唤醒词，说新词永远不匹配。
        #
        # 教训：写热更新必须落在「前端真正调用的那个入口」上，
        # 同一个功能有多个保存入口时，每个入口都得各自处理。
        # ========================================================================
        if category == 'ai_butler':
            # 唤醒词相关键：与 http_server.handle_save_ai_butler_config 保持同一集合
            _WAKEWORD_KEYS = {'wakeWord', 'enable_wakeWord', 'wakeword_session_timeout',
                              'enable_auto_end_dialogue', 'name', 'nickname'}
            try:
                if set(configs) & _WAKEWORD_KEYS:
                    from core.utils.wakeword_provider import reload_wakeword_detector
                    _wp = reload_wakeword_detector()
                    logger.bind(tag=TAG).info(
                        f"已热更新唤醒词检测器: wakeWord={getattr(_wp, 'wake_word', None)}, "
                        f"主词={getattr(_wp, 'wakewords', None)}, "
                        f"timeout={getattr(_wp, 'session_timeout', None)}s, "
                        f"enabled={getattr(_wp, 'enabled', None)}")
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"唤醒词热更新失败（不影响保存，重启后生效）: {e}")

            # 人设缓存同样有 30s 内存缓存，改 name/nickname 后 prompt 里的 {name}
            # 仍可能用旧值（http_server 那个入口 10-13 已加，这里补齐）。
            if set(configs) & {'name', 'nickname', 'soul', 'identity'}:
                try:
                    from core.utils.assistant_identity import invalidate_cache
                    invalidate_cache()
                except Exception as e:
                    print(f"[config_api] 人设缓存失效失败（不影响保存）: {e}")

        return web.json_response({
            'success': True,
            'message': f'已保存 {saved_count} 条配置，配置将通过WebSocket消息更新',
            'saved_count': saved_count
        })

    except Exception as e:
        import traceback
        error_stack = traceback.format_exc()
        print(f"[config_save_batch] 批量保存失败: {e}\n堆栈: {error_stack}")
        return web.json_response({
            'success': False,
            'message': f'批量保存配置失败: {str(e)}'
        }, status=500)


async def config_delete(request):
    """
    删除单个配置项
    DELETE /api/config/delete?category=ai_butler&key=voice
    """
    try:
        data = await request.json()
        category = data.get('category', '').strip()
        key = data.get('key', '').strip()

        if not category or not key:
            return web.json_response({
                'success': False,
                'message': '缺少必要参数: category, key'
            }, status=400)

        table, key_col, _ = _get_table_and_columns(category)
        if not table:
            return web.json_response({
                'success': False,
                'message': f'不支持的分类: {category}'
            }, status=400)

        conn = _get_db_connection()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {table} WHERE {key_col} = ?", (key,))
            deleted = cursor.rowcount
            conn.commit()
        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass

        return web.json_response({
            'success': True,
            'message': f'已删除 {deleted} 条配置',
            'deleted': deleted
        })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'删除配置失败: {str(e)}'
        }, status=500)


async def get_memory_config(request):
    """
    读取记忆模块当前选中项
    GET /api/config/memory
    优先读 data/.config.yaml（运行时自定义配置），缺失时回退 config.yaml。
    """
    try:
        import yaml

        custom_config_path = Path(__file__).parent.parent.parent / "data" / ".config.yaml"
        if custom_config_path.exists():
            with open(custom_config_path, 'r', encoding='utf-8') as file:
                # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
                custom_config = load_yaml_expanded(file) or {}
            memory_provider = (custom_config.get('selected_module') or {}).get('Memory')
            if memory_provider:
                return web.json_response({
                    'success': True,
                    'memory_provider': memory_provider,
                    'source': 'data/.config.yaml'
                })

        with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
            # 2026-10-05：读出来是要拿去调云端的，必须展开 ${}，否则拿到字面串会 401
            config = load_yaml_expanded(file) or {}
        memory_provider = (config.get('selected_module') or {}).get('Memory')

        return web.json_response({
            'success': True,
            'memory_provider': memory_provider or '',
            'source': 'config.yaml'
        })
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'读取记忆模块配置失败: {str(e)}'
        }, status=500)


async def update_memory_config(request):
    """
    更新记忆模块配置（同时更新 config.yaml 和 data/.config.yaml）
    POST /api/config/update-memory
    Body: { "memory_provider": "mem_local_short" }
    """
    try:
        data = await request.json()
        memory_provider = data.get('memory_provider', '').strip()

        if not memory_provider:
            return web.json_response({
                'success': False,
                'message': '缺少必要参数: memory_provider'
            }, status=400)

        import yaml

        # 更新 config.yaml
        with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)

        if 'selected_module' not in config:
            config['selected_module'] = {}
        config['selected_module']['Memory'] = memory_provider

        with open(CONFIG_PATH, 'w', encoding='utf-8') as file:
            yaml.dump(config, file, default_flow_style=False, allow_unicode=True, indent=2)

        # 同时更新 data/.config.yaml
        try:
            custom_config_path = Path(__file__).parent.parent.parent / "data" / ".config.yaml"
            custom_config = {}
            if custom_config_path.exists():
                with open(custom_config_path, 'r', encoding='utf-8') as file:
                    custom_config = yaml.safe_load(file) or {}

            if 'selected_module' not in custom_config:
                custom_config['selected_module'] = {}
            custom_config['selected_module']['Memory'] = memory_provider

            with open(custom_config_path, 'w', encoding='utf-8') as file:
                yaml.dump(custom_config, file, default_flow_style=False, allow_unicode=True, indent=2)
        except Exception as custom_e:
            print(f"Warning: failed to update data/.config.yaml: {custom_e}")

        return web.json_response({
            'success': True,
            'message': f'记忆模块已更新为 {memory_provider}，重启服务器后生效'
        })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'更新记忆模块失败: {str(e)}'
        }, status=500)


# ==================== 长期记忆（ltm 向量库）列表 / 删除 / 清空（2026-10-13 WP-6 / D6） ====================
# 背景：此前管理台「记忆管理」卡片调用的是不存在的 /api/memory 假接口，长期记忆
# （ltm 向量库）没有任何列表/删除入口。D6 裁决：成员私有记忆（m:<id>）只能删自己的，
# 家庭共享记忆（family）必须经主人确认（接口层用 confirm_family 显式确认参数兜底）。

_FAMILY_SCOPE = 'family'


def _get_running_ltm_provider():
    """拿正在运行的 ltm provider 实例（不是新建一个，避免读到不同的库句柄）。

    返回 (provider, error_msg)。当前记忆模块不是 ltm 时返回错误说明。
    """
    server = get_websocket_server_instance()
    memory = getattr(server, '_memory', None) if server else None
    if memory is None:
        return None, '拿不到运行中的记忆实例（服务未就绪或独立进程）'
    if not hasattr(memory, 'list_memories'):
        return None, f'当前记忆模块（{type(memory).__name__}）不支持长期记忆列表，仅 ltm 支持'
    return memory, None


async def get_memory_curator_config(request):
    """记忆整理模型配置：GET /api/config/memory/curator（不返回密钥）"""
    from core.utils import memory_curator_config as cc
    from config import config_loader
    try:
        return web.json_response({'success': True, **cc.public_view(config_loader.load_config())})
    except Exception as e:
        return web.json_response({'success': False, 'message': f'读取失败: {e}'}, status=500)


async def save_memory_curator_config(request):
    """保存记忆整理模型配置：POST /api/config/memory/curator
    Body: {mode: local|main|extractor, model?, base_url?, api_key?}；api_key 留空 = 不改。
    存 data/.memory_curator.json，热生效，无需重启。
    """
    from core.utils import memory_curator_config as cc
    from config import config_loader
    try:
        data = await request.json()
        mode = str(data.get('mode') or '').strip()
        if mode not in cc.MODES:
            return web.json_response({'success': False, 'message': f'模式不合法: {mode}'}, status=400)
        raw = cc.load_raw()
        raw['mode'] = mode
        for k in ('model', 'base_url'):
            if k in data:
                raw[k] = str(data.get(k) or '').strip()
        if str(data.get('api_key') or '').strip():
            raw['api_key'] = str(data['api_key']).strip()
        cc.save_raw(raw)
        return web.json_response({'success': True, 'message': '记忆整理模型已保存（立即生效）',
                                  **cc.public_view(config_loader.load_config())})
    except Exception as e:
        return web.json_response({'success': False, 'message': f'保存失败: {e}'}, status=500)


async def check_memory_curator_config(request):
    """测试当前配置的整理模型能否正常返回：POST /api/config/memory/curator/test"""
    from core.utils import memory_curator_config as cc, memory_curator as mcur
    from config import config_loader
    try:
        r = cc.resolve(config_loader.load_config())
        t0 = time.time()
        out = await asyncio.get_event_loop().run_in_executor(
            None, lambda: mcur._call_llm(r, '只输出 JSON。', '输出 {"ok":true}', mode=r['mode']))
        ok = bool(out and out.get('ok'))
        return web.json_response({'success': ok, 'model': r['model'], 'seconds': round(time.time() - t0, 1),
                                  'message': '连接正常' if ok else '没有收到有效回复，请检查地址/模型名/密钥（整理时会自动退回 8B）'})
    except Exception as e:
        return web.json_response({'success': False, 'message': f'测试失败: {e}'}, status=500)


async def trigger_memory_consolidation(request):
    """手动触发一次夜间梳理（后台执行，立即返回）。
    POST /api/config/memory/consolidate   Body 可选: {"lookback_hours": 24}
    不传 lookback_hours 则按游标续跑（与每天 03:30 的定时任务同一逻辑）。
    """
    try:
        provider, err = _get_running_ltm_provider()
        if provider is None:
            return web.json_response({'success': False, 'message': err})
        try:
            body = await request.json()
        except Exception:
            body = {}
        hours = (body or {}).get('lookback_hours')
        hours = float(hours) if hours not in (None, '') else None

        from core.utils.memory_consolidation import run_once

        async def _job():
            try:
                await run_once(provider, lookback_hours=hours)
            except Exception as e:
                print(f'Warning: 手动梳理失败: {e}')

        asyncio.get_event_loop().create_task(_job())
        return web.json_response({'success': True, 'message': '已开始梳理，结果见状态接口或服务日志'})
    except Exception as e:
        return web.json_response({'success': False, 'message': f'触发失败: {e}'}, status=500)


async def memory_consolidation_status(request):
    """夜间梳理状态：GET /api/config/memory/consolidate/status"""
    from core.utils.memory_consolidation import load_state

    st = load_state()
    return web.json_response({'success': True, 'last_run': st.get('last_run'),
                              'last_id': st.get('last_id'), 'history': st.get('history', [])})


async def list_long_term_memories(request):
    """列出长期记忆条目。
    GET /api/config/memory/entries?scope=family|all&m=<成员id>
    返回条目 + 家庭成员清单（供前端「我是谁」下拉做 D6 权限判定）。
    """
    try:
        provider, err = _get_running_ltm_provider()
        if provider is None:
            return web.json_response({'success': False, 'message': err})
        scope_q = (request.query.get('scope') or 'all').strip()
        scope = None if scope_q in ('', 'all') else scope_q

        # ltm 的 get_all 是同步阻塞调用，放线程里跑，别卡住事件循环
        entries = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provider.list_memories(scope=scope)
        )

        # 家庭成员清单（D6：前端据此判断「我是谁」能删哪些 m:<id>）
        members = []
        try:
            from core.utils.memory_scope import _load_members
            # _load_members 返回 {显示名: 成员id}，同一个人可能名字+小名各占一条
            name_to_id = _load_members() or {}
            by_id = {}
            for name, mid in name_to_id.items():
                by_id.setdefault(str(mid), []).append(name)
            members = [
                {'id': mid, 'scope': f'm:{mid}', 'names': names}
                for mid, names in sorted(by_id.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)
            ]
        except Exception as me:
            print(f'Warning: 加载家庭成员失败: {me}')

        per_scope = {}
        for e in entries:
            per_scope[e['scope']] = per_scope.get(e['scope'], 0) + 1

        return web.json_response({
            'success': True,
            'entries': entries,
            'total': len(entries),
            'per_scope': per_scope,
            'members': members,
            'family_scope': _FAMILY_SCOPE,
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': f'{type(e).__name__}: {e}'}, status=500)


async def delete_long_term_memory(request):
    """删除单条长期记忆（D6 权限）。
    POST /api/config/memory/entries/delete
    Body: { "id": "...", "actor": "family" | "m:<成员id>", "confirm_family": true }

    权限规则（主人 2026-10-13 裁决 D6）：
      - actor=m:<id>：只能删自己 scope（m:<id>）下的记忆；
      - family 共享记忆：必须主人确认 —— 请求要带 confirm_family=true，
        且 actor 必须是 family（管理台是主人本机使用，family 即代表主人在操作）。
    """
    try:
        data = await request.json()
        memory_id = str(data.get('id') or '').strip()
        actor = str(data.get('actor') or _FAMILY_SCOPE).strip()
        confirm_family = bool(data.get('confirm_family'))
        if not memory_id:
            return web.json_response({'success': False, 'message': '缺少记忆 id'}, status=400)

        provider, err = _get_running_ltm_provider()
        if provider is None:
            return web.json_response({'success': False, 'message': err})

        # 先查这条记忆的 scope，再做权限判定（删了就查不到，必须先定位）
        entries = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provider.list_memories()
        )
        target = next((e for e in entries if str(e.get('id')) == memory_id), None)
        if target is None:
            return web.json_response({'success': False, 'message': '未找到该记忆（可能已被删除）'})

        entry_scope = target.get('scope') or ''
        if entry_scope == _FAMILY_SCOPE:
            # 家庭共享记忆：必须主人显式确认
            if actor != _FAMILY_SCOPE or not confirm_family:
                return web.json_response({
                    'success': False,
                    'message': '家庭共享记忆需要主人确认后才能删除（confirm_family）',
                    'entry_scope': entry_scope,
                }, status=403)
        else:
            # 成员私有记忆：只能删自己 scope 的
            if actor != entry_scope:
                return web.json_response({
                    'success': False,
                    'message': f'成员只能删除自己的私有记忆（该条属于 {entry_scope}，当前身份 {actor}）',
                    'entry_scope': entry_scope,
                }, status=403)

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provider.delete_memory_entry(memory_id)
        )
        if not result.get('success'):
            return web.json_response({'success': False, 'message': result.get('error', '删除失败')}, status=500)
        return web.json_response({
            'success': True,
            'deleted': {'id': memory_id, 'scope': entry_scope, 'memory': result.get('memory', '')},
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': f'{type(e).__name__}: {e}'}, status=500)


async def clear_long_term_memories(request):
    """清空长期记忆（D5 重建入口 / 日常维护）。
    POST /api/config/memory/entries/clear
    Body: { "scope": "all"|"family"|"m:<id>", "confirm": true }
    清空是不可逆操作，必须带 confirm=true。
    """
    try:
        data = await request.json()
        scope_q = str(data.get('scope') or 'all').strip()
        confirm = bool(data.get('confirm'))
        if not confirm:
            return web.json_response({'success': False, 'message': '清空不可逆，必须带 confirm=true'}, status=400)

        provider, err = _get_running_ltm_provider()
        if provider is None:
            return web.json_response({'success': False, 'message': err})

        scope = None if scope_q in ('', 'all') else scope_q
        deleted = await asyncio.get_event_loop().run_in_executor(
            None, lambda: provider.clear_memory(scope=scope)
        )
        return web.json_response({'success': True, 'deleted': deleted, 'scope': scope_q})
    except Exception as e:
        return web.json_response({'success': False, 'message': f'{type(e).__name__}: {e}'}, status=500)


async def config_load_all_categories(request):
    """
    读取所有分类的配置（一次性加载全部）
    GET /api/config/load-all-categories
    """
    try:
        result = {}

        for category in CATEGORY_TABLE_MAP.keys():
            table, key_col, val_col = _get_table_and_columns(category)
            if not table:
                continue

            conn = _get_db_connection()
            cursor = None
            try:
                cursor = conn.cursor()

                if table == 'skills_config':
                    cursor.execute(f"SELECT skill_id, enabled, config, updated_at FROM {table}")
                    rows = cursor.fetchall()
                    cat_data = {}
                    for row in rows:
                        cat_data[row['skill_id']] = {
                            'enabled': bool(row['enabled']),
                            'config': json.loads(row['config']) if row['config'] else {},
                            'updated_at': row['updated_at']
                        }
                else:
                    cursor.execute(f"SELECT {key_col}, {val_col}, updated_at FROM {table}")
                    rows = cursor.fetchall()
                    cat_data = {}
                    for row in rows:
                        raw_value = row[val_col]
                        try:
                            parsed_value = json.loads(raw_value)
                        except (json.JSONDecodeError, TypeError):
                            parsed_value = raw_value
                        cat_data[row[key_col]] = {
                            'value': parsed_value,
                            'updated_at': row['updated_at']
                        }

            finally:
                if cursor:
                    try:
                        cursor.close()
                    except:
                        pass
                try:
                    conn.close()
                except:
                    pass
            result[category] = cat_data

        return web.json_response({
            'success': True,
            'data': result
        })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'读取全部配置失败: {str(e)}'
        }, status=500)


async def save_ai_butler_configs(request):
    """
    保存 AI 管家配置（批量）
    POST /api/ai-butler-config

    Body: {
        "configs": {
            "name": "伊娃",
            "nickname": "小伊",
            "wakeWord": "你好伊娃",
            "enable_wakeWord": "true",
            "enable_conversation_summaries": "true",
            "enable_speaker_identification": "false",
            "summary_retention_days": "30",
            "summary_min_length": "10",
            "enable_daily_summary_report": "false"
        }
    }
    """
    try:
        data = await request.json()
        configs = data.get('configs', {})

        if not isinstance(configs, dict):
            return web.json_response({
                'success': False,
                'message': 'configs 必须是对象'
            }, status=400)

        conn = _get_db_connection()
        cursor = None
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            saved_count = 0

            for key, value in configs.items():
                # 数据库中字段名已经是 snake_case，直接使用前端字段名
                # 数据库字段名与前端一致：enable_conversation_summaries, summary_retention_days, etc.
                db_key = key  # 不需要转换，直接使用前端字段名
                value_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else str(value)
                cursor.execute("""
                    INSERT INTO ai_butler_config (config_key, config_value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_value = excluded.config_value,
                        updated_at = excluded.updated_at
                """, (db_key, value_str, now))
                saved_count += 1

            conn.commit()

            # 强制 WAL checkpoint，释放锁
            checkpoint_cursor = None
            try:
                checkpoint_cursor = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except:
                pass
            finally:
                if checkpoint_cursor:
                    try:
                        checkpoint_cursor.close()
                    except:
                        pass

        finally:
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            try:
                conn.close()
            except:
                pass

        # 后台保存 AI 身份后必须清缓存，否则 prompt 模板里的 {name} 占位符
        # 还会用旧的 name（assistant_identity 有 30s 内存缓存）。
        try:
            from core.utils.assistant_identity import invalidate_cache
            invalidate_cache()
        except Exception:
            pass

        # 唤醒词热更新：后台改 wakeWord/enable_wakeWord 后不必重启服务。
        # 2026-10-10 修复：原实现 detector 单例只在首次使用时加载一次，
        # 后台改唤醒词永不生效（主人改「你好汤圆」后说汤圆唤不醒、说旧词仍唤醒）。
        try:
            # 2026-10-13 修复：这里原来写的 request_data 在本函数里不存在
            # （函数开头的变量名是 data），NameError 被下面的 except 静默吞掉，
            # 导致 10-10 加的「唤醒词热更新」从未执行过 —— 主人后台改唤醒词后
            # detector 单例一直缓存旧词，说新唤醒词永远不醒，只能重启服务。
            saved_keys = set((data.get('configs') or {}).keys())
            if saved_keys & {'wakeWord', 'enable_wakeWord', 'wakeword_session_timeout',
                             'enable_auto_end_dialogue', 'name', 'nickname'}:
                from core.utils.wakeword_provider import reload_wakeword_detector
                reload_wakeword_detector()
        except Exception:
            pass  # 热更新失败不阻断保存（下次访问 detector 会重载）

        return web.json_response({
            'success': True,
            'saved': saved_count,
            'message': f'已保存 {saved_count} 个 AI 管家配置项'
        })

    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'保存 AI 管家配置失败: {str(e)}'
        }, status=500)


# API 路由注册（在应用启动时）
def register_routes(app):
    """注册配置 API 路由"""
    app.router.add_post('/api/config/save', save_config)
    app.router.add_post('/api/config/save-dashboard', save_dashboard_config)
    app.router.add_post('/api/config/save-all', save_configs)
    app.router.add_get('/api/config/load', load_config)
    app.router.add_get('/api/config/load-all', load_all_configs)
    app.router.add_get('/api/config/load-category', load_category_configs)
    app.router.add_post('/api/config/delete', delete_config)
    app.router.add_get('/api/config/load-all-categories', config_load_all_categories)
    app.router.add_get('/api/config/memory', get_memory_config)
    app.router.add_post('/api/config/update-memory', update_memory_config)  # 更新记忆模块
    # 长期记忆（ltm 向量库）列表/删除/清空（2026-10-13 WP-6 / D6 权限）
    app.router.add_get('/api/config/memory/entries', list_long_term_memories)
    app.router.add_post('/api/config/memory/entries/delete', delete_long_term_memory)
    app.router.add_post('/api/config/memory/entries/clear', clear_long_term_memories)
    app.router.add_post('/api/ai-butler-config', save_ai_butler_configs)  # 新增 AI 管家配置保存接口
    # 已保存模型列表（后端持久化，替代浏览器 localStorage，避免清缓存丢配置）
    app.router.add_get('/api/saved-models', get_saved_models)
    app.router.add_post('/api/saved-models', save_saved_models)


# 默认预置模型（首次启动时填充；之后用户在后台的增删改都以数据库为准）
_DEFAULT_SAVED_MODELS = [
    {
        "provider": "oMLX 本地",
        "name": "Qwen3-8B-4bit",
        "baseUrl": "http://localhost:8001/v1",
        "apiKey": "1234",
        "isActive": True,
    },
    {
        "provider": "oMLX 本地",
        "name": "gpt-oss-20b-MLX-4bit",
        "baseUrl": "http://localhost:8001/v1",
        "apiKey": "1234",
        "isActive": False,
    },
]


async def get_saved_models(request):
    """读取已保存的模型列表（system_config 表，key=saved_models）。
    首次无数据时返回预置列表，前端拿到后可直接渲染。"""
    import json as _json
    db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT config_value FROM system_config WHERE config_key='saved_models'"
            ).fetchone()
        finally:
            conn.close()
        if row:
            data = _json.loads(row[0])
            return web.json_response({"success": True, "models": data, "source": "db"})
        # 首次返回预置
        return web.json_response({"success": True, "models": _DEFAULT_SAVED_MODELS, "source": "default"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e), "models": _DEFAULT_SAVED_MODELS}, status=500)


async def save_saved_models(request):
    """整体覆盖保存已保存的模型列表。"""
    import json as _json
    try:
        body = await request.json()
        models = body.get("models")
        if not isinstance(models, list):
            return web.json_response({"success": False, "error": "models 必须是数组"}, status=400)
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO system_config(config_key, config_value, updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value, updated_at=CURRENT_TIMESTAMP",
                ("saved_models", _json.dumps(models, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        return web.json_response({"success": True, "count": len(models)})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


# ========== 统一模型提供商管理（model_providers 表） ==========

_MODEL_ROLES = ("text", "vllm", "welcome", "embedding")


def _model_providers_db():
    db_path = Path(__file__).resolve().parent.parent.parent / "data" / "devices.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


async def get_model_providers(request):
    """读取所有模型提供商；role 参数可选过滤。"""
    role = request.query.get("role")
    try:
        conn = _model_providers_db()
        try:
            if role:
                rows = conn.execute(
                    "SELECT * FROM model_providers WHERE role=? ORDER BY role, id", (role,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM model_providers ORDER BY role, id").fetchall()
        finally:
            conn.close()
        items = [dict(r) for r in rows]
        # extra_config 从 JSON 字符串还原成 dict
        for it in items:
            if it.get("extra_config"):
                try:
                    it["extra_config"] = json.loads(it["extra_config"])
                except Exception:
                    pass
        return web.json_response({"success": True, "models": items})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def save_model_provider(request):
    """新增或更新一条模型记录；带 id 则更新，否则新增。"""
    try:
        body = await request.json()
        rid = body.get("id")
        role = body.get("role")
        if role not in _MODEL_ROLES:
            return web.json_response({"success": False, "error": f"role 必须是 {_MODEL_ROLES}"}, status=400)
        provider_name = (body.get("provider_name") or "").strip()
        model_name = (body.get("model_name") or "").strip()
        if not provider_name or not model_name:
            return web.json_response({"success": False, "error": "provider_name / model_name 必填"}, status=400)
        extra = body.get("extra_config")
        if isinstance(extra, dict):
            extra = json.dumps(extra, ensure_ascii=False)
        fields = (
            role, provider_name,
            body.get("display_name") or provider_name,
            body.get("base_url") or "",
            body.get("api_key") or "",
            model_name,
            extra,
            1 if body.get("is_active") else 0,
        )
        conn = _model_providers_db()
        try:
            if rid:
                conn.execute(
                    "UPDATE model_providers SET role=?, provider_name=?, display_name=?, base_url=?, "
                    "api_key=?, model_name=?, extra_config=?, is_active=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?", (*fields, rid))
            else:
                conn.execute(
                    "INSERT INTO model_providers(role, provider_name, display_name, base_url, api_key, "
                    "model_name, extra_config, is_active) VALUES(?,?,?,?,?,?,?,?)", fields)
            conn.commit()
        finally:
            conn.close()
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def delete_model_provider(request):
    try:
        rid = int(request.query.get("id", 0))
        conn = _model_providers_db()
        try:
            conn.execute("DELETE FROM model_providers WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def activate_model_provider(request):
    """切换某 role 的激活模型；同 role 其他记录 is_active=0。"""
    try:
        body = await request.json()
        rid = body.get("id")
        conn = _model_providers_db()
        try:
            row = conn.execute("SELECT role FROM model_providers WHERE id=?", (rid,)).fetchone()
            if not row:
                return web.json_response({"success": False, "error": "记录不存在"}, status=404)
            role = row["role"]
            conn.execute("UPDATE model_providers SET is_active=0 WHERE role=?", (role,))
            conn.execute("UPDATE model_providers SET is_active=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        # 热更新：把 DB 激活模型同步到内存 config + yaml + 触发组件重载
        hot_result = _apply_active_provider_to_runtime(role)
        return web.json_response({"success": True, "role": role, **hot_result})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


def _apply_active_provider_to_runtime(role_filter=None):
    """把 DB 里 is_active=1 的模型刷到运行时（内存 config + yaml 双写 + 触发 LLM 热重载）。

    role_filter: 只刷指定 role（如 'text'）；None 表示全刷。
    返回 {hot_reload: bool, message: str}。
    """
    import json as _j
    import sqlite3 as _sql
    import yaml as _y
    from pathlib import Path as _P
    try:
        db_path = _P(__file__).resolve().parent.parent.parent / "data" / "devices.db"
        if not db_path.exists():
            return {"hot_reload": False, "message": "DB 不存在"}
        conn = _sql.connect(str(db_path))
        conn.row_factory = _sql.Row
        try:
            if role_filter:
                rows = conn.execute(
                    "SELECT * FROM model_providers WHERE is_active=1 AND role=?", (role_filter,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM model_providers WHERE is_active=1").fetchall()
        finally:
            conn.close()

        # 把 role 对应的激活项写到内存 config（通过全局 ws_server 引用）
        ws = globals().get("_ws_server_instance")
        if ws is None or getattr(ws, "config", None) is None:
            return {"hot_reload": False, "message": "ws_server 未就绪"}

        applied = []
        for r in rows:
            role, provider = r["role"], r["provider_name"]
            base_url = r["base_url"] or ""
            api_key = r["api_key"] or ""
            model_name = r["model_name"] or ""
            try:
                extra = _j.loads(r["extra_config"]) if r["extra_config"] else {}
            except Exception:
                extra = {}
            cfg = ws.config
            if role == "text":
                existing = cfg.setdefault("LLM", {}).get(provider) or {}
                cfg["LLM"][provider] = {
                    **existing,
                    "type": existing.get("type") or "openai",
                    "api_key": api_key or existing.get("api_key", ""),
                    "base_url": base_url or existing.get("base_url", ""),
                    "url": base_url or existing.get("url", ""),
                    "model_name": model_name or existing.get("model_name", ""),
                    **extra,
                }
                cfg.setdefault("selected_module", {})["LLM"] = provider
                applied.append(f"text→{provider}/{model_name}")
            elif role == "vllm":
                existing = cfg.setdefault("VLLM", {}).get(provider) or {}
                cfg["VLLM"][provider] = {
                    **existing,
                    "type": existing.get("type") or "openai",
                    "api_key": api_key or existing.get("api_key", ""),
                    "url": base_url or existing.get("url", ""),
                    "base_url": base_url or existing.get("base_url", ""),
                    "model_name": model_name or existing.get("model_name", ""),
                }
                cfg.setdefault("selected_module", {})["VLLM"] = provider
                applied.append(f"vllm→{provider}/{model_name}")
            elif role == "welcome":
                cfg.setdefault("security", {})["welcome_llm"] = {
                    "_provider": provider, "api_key": api_key,
                    "base_url": base_url, "model_name": model_name,
                }
                applied.append(f"welcome→{provider}/{model_name}")

        # 触发主文本 LLM 热重载（异步，不阻塞 HTTP 响应）
        hot_reload = False
        if "text" in [r["role"] for r in rows]:
            import asyncio
            try:
                asyncio.create_task(ws.update_config())
                hot_reload = True
            except Exception as e:
                logger.bind(tag="model_providers").warning(f"触发 LLM 热重载失败: {e}")
        return {"hot_reload": hot_reload, "message": f"已应用: {', '.join(applied) or '(空)'}"}
    except Exception as e:
        return {"hot_reload": False, "message": f"应用失败: {e}"}


# ==================== 服务器配置只读镜像（2026-10-06） ====================
# 背景：控制台「系统设置 → 服务器设置」页原本提供 12 项可编辑表单（端口 / OTA 地址 /
# API 密钥 / 日志级别 / 数据路径 / HTTPS / CORS / 自动重启 等），保存后写进 sqlite 的
# dashboard_settings 表 setting_key='server' 行。但全项目**没有任何代码读这一行**，
# 且该行在生产库里从未被创建过 —— 是一个只写不读的假功能，页面上还把端口默认值写成
# 8083（真实是 8003），导致「测试服务器连接」按钮必然失败。
#
# 真实地址与端口的唯一真值来源是 data/.config.yaml 的 server: 段，由
# core/utils/util.py:get_local_ip() 和 core/http_server.py:start() 实际消费。
# 因此本页改为**只读镜像**：如实展示当前生效值 + 告诉主人去哪个文件改。
# 本接口只读，不写任何配置文件。

_SERVER_CONFIG_FILE_HINT = 'server/homebrain-server/data/.config.yaml'


def _resolve_vision_url(config: dict, local_ip: str) -> str:
    """解析视觉分析接口地址（vision_explain 含占位符时按传入的本机 IP 拼）。

    local_ip 由调用方传入（本机网卡实测值），不再自己去读配置里的
    server.local_ip —— 那一路会拿到别的机器的地址。
    """
    server_config = config.get("server", {}) or {}
    vision = server_config.get("vision_explain", "") or ""
    if "你的" in vision:
        port = int(server_config.get("http_port", 8003))
        return f"http://{local_ip}:{port}/mcp/vision/explain"
    return vision


def _resolve_websocket_url(config: dict, local_ip: str) -> str:
    """解析 WebSocket 地址，逻辑对齐 core/http_server.py:_get_websocket_url()。

    注意：http_server 读的是 config 顶层的 `websocket:` 段（不在 server: 里），
    与 server.port 是两处独立配置；顶层段不存在时才回落到 server.port。
    """
    server_config = config.get("server", {}) or {}
    http_port = int(server_config.get("http_port", 8003))
    ws_config = config.get("websocket", {}) or {}
    if ws_config:
        ws_ip = ws_config.get("ip", local_ip)
        ws_port = int(ws_config.get("port", server_config.get("port", http_port)))
    else:
        ws_ip = local_ip
        ws_port = int(server_config.get("port", http_port))
    return f"ws://{ws_ip}:{ws_port}/homebrain/v1/"


def _probe_port(host: str, port: int, timeout: float = 0.35) -> bool:
    """探测某 TCP 端口是否在监听。只判断「有没有人在听」，不关心协议内容。"""
    import socket as _socket
    s = None
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def _collect_memory_systems(config: dict) -> dict:
    """汇总记忆系统的详细信息（主人 2026-10-06：「这事还比较重要」）。

    记忆系统比别的服务重要，所以单独成块，除了端口还给出：
      - 当前是否在用（selected_module.Memory）
      - 服务是否可达
      - **存储是否正常**（provider 初始化 + 已存条数）
      - **抽取是否正常**（provider 能否接受对话并落库的能力自检）
      - 依赖哪些模型（LTM 需要 oMLX 提供 LLM + embedding）

    注意：探测用的provider 是独立实例，走懒加载构造，不会污染运行中的记忆实例。
    """
    from urllib.parse import urlparse

    memory_cfg = config.get("Memory", {}) or {}
    selected = (config.get("selected_module", {}) or {}).get("Memory", "")

    # ── 每个记忆模块的元信息 ──
    # 只列主人关心的三个（2026-10-06 原话「我只需要统计 LTM 和Ltm
    # 和本地短期记忆的状态。这3 个就可以」）：
    # ltm_store / mem_report_only / nomem 已从面板移除。
    # tier 用来在页面上标出定位（主人 2026-10-06 澄清）：
    #   商用版= 长期记忆主系统，商用环境必须用它
    #   开源版 = 给开源部署准备的兜底方案（没跑 Ltm 这类服务时用）
    MODULES = [
        ("LTM", "Ltm", "向量记忆库，长期记忆的主系统", "商用版"),
        ("ltm", "LTM", "本地向量库，不需要外部 API key", "商用版"),
        ("mem_local_short", "本地短期记忆", "LLM 总结存本地文件，不依赖记忆服务", "开源版"),
    ]

    systems = []
    for key, label, desc, tier in MODULES:
        cfg_item = memory_cfg.get(key) or {}
        entry = {
            'key': key,
            'name': label,
            'desc': desc,
            'tier': tier,
            'inUse': (key == selected),
            'configured': bool(cfg_item),
            'serviceOnline': None,
            'storageOk': None,
            'extractOk': None,
            'memoryCount': None,
            'detail': '',
            'dependsOn': [],
        }

        # —— Ltm：走独立服务端口 ——
        if key == 'LTM':
            port = None
            try:
                url = (config.get("LTM", {}) or {}).get("server_url", "")
                if url:
                    port = urlparse(url).port
            except Exception:
                port = None
            entry['servicePort'] = port or 8765
            entry['serviceOnline'] = _probe_port('127.0.0.1', entry['servicePort'])
            entry['dependsOn'] = [{'name': 'Ltm 服务', 'port': entry['servicePort'],
                                   'online': entry['serviceOnline']}]
            if entry['inUse']:
                entry['storageOk'], entry['memoryCount'], entry['detail'] = \
                    _probe_LTM(cfg_item)
                entry['extractOk'] = entry['storageOk']

        # —— LTM：不监听端口，靠外部 OpenAI 兼容端点提供 LLM + embedding ——
        # 2026-10-06 起两者分端点：抽取走 LM Studio（记忆专用），embedding 留 oMLX。
        elif key == 'ltm':
            def _cfg_port(cfg_key, fallback):
                try:
                    return urlparse(cfg_item.get(cfg_key, "") or "").port or fallback
                except Exception:
                    return fallback
            llm_port = _cfg_port("llm_base_url", 1234)
            embed_port = _cfg_port("embed_base_url", 8001)
            # servicePort 取抽取端点——LTM 挂不挂主要看它（embedding 是毫秒级小模型）
            entry['servicePort'] = llm_port
            entry['serviceOnline'] = _probe_port('127.0.0.1', llm_port)
            dep = [{'name': 'LM Studio（记忆抽取）', 'port': llm_port,
                    'online': entry['serviceOnline']}]
            if embed_port != llm_port:
                dep.append({'name': 'oMLX（向量）', 'port': embed_port,
                            'online': _probe_port('127.0.0.1', embed_port)})
            entry['dependsOn'] = dep
            entry['llmModel'] = cfg_item.get("llm_model", "")
            entry['embedModel'] = cfg_item.get("embed_model", "")
            entry['vecDir'] = '~/.ltm'
            # LTM 的记忆抽取由 oMLX 里的 LLM 完成（它没有自带抽取模型），
            # 所以「抽取正常」等价于「LLM+embedding 都通」= storageOk。
            # ⚠️ 不能留None，LTM 恰恰是靠 LLM 抽取的模块。
            entry['extractOk'] = None
            # 抽取耗时与批量上限（provider 里实测踩过的坑，显示出来方便调参）
            entry['batchSize'] = int(cfg_item.get("batch_size", 4))
            entry['flushThreshold'] = int(cfg_item.get("flush_threshold", 4))
            # ⚠️ 2026-10-06 纠正：原先这里写「单次约 30-45 秒」，那是provider
            # 注释里的旧记录，我直接搬来显示给主人了。实测初始化只要 ~1.2 秒，
            # 早已改善。抽取调用方是 asyncio.run_coroutine_threadsafe(...)，
            # **不等结果**，属后台异步，不会阻塞对话。
            entry['note'] = (
                f"抽取走 LM Studio 的 {entry['llmModel']}（记忆专用，不与开发用的模型抢资源），"
                f"攒够 {entry['flushThreshold']} 条触发一次；"
                f"单批上限 {entry['batchSize']} 条"
                '（批量过大会因 LLM 输出 JSON 截断而整批丢失）。'
                '抽取在后台异步进行，不阻塞对话'
            )
            if entry['inUse'] or cfg_item.get('enabled'):
                entry['storageOk'], entry['memoryCount'], entry['detail'] = \
                    _probe_ltm(cfg_item)
                entry['extractOk'] = entry['storageOk']

        # —— 本地短期记忆：进程内实现，不依赖任何外部服务 ——
        elif key == 'mem_local_short':
            entry['serviceOnline'] = None
            entry['dependsOn'] = []
            entry['storageOk'] = True
            entry['extractOk'] = None     # 只存当前会话，不做长期记忆抽取

        systems.append(entry)

    return {
        'selected': selected,
        'selectedName': next((s['name'] for s in systems if s['inUse']), selected or '未选择'),
        'systems': systems,
    }


def _probe_LTM(cfg_item: dict):
    """实测 Ltm：能否连上、能否取到记忆。

    返回 (storage_ok, memory_count, detail)。任何一步失败都返回 False 而不抛异常——
    这个接口是给页面看的，不能因为记忆服务挂了就把整个页面搞崩。

    ⚠️ query_memory 是协程，而本接口跑在 aiohttp 自己的事件循环里，
    不能直接 run_until_complete（会抛 "Cannot run the event loop while another
    loop is running"，2026-10-06 实测踩到）。必须在独立线程里跑自己的循环。
    """
    try:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        from core.providers.memory.LTM.provider import Ltmprovider

        provider = Ltmprovider(dict(cfg_item or {}))
        if not provider._ensure_client():
            return False, None, '无法连接 Ltm 服务'
        provider.init_memory('homebrain', None)

        # 真跑一次检索：能返回内容说明存储层可读
        def _run():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(provider.query_memory('最近的对话'))
            finally:
                loop.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_run).result(timeout=25)

        count = None
        if isinstance(result, str):
            count = len([x for x in result.split('\n') if x.strip()])
        return True, count, '可连接、可检索'
    except Exception as e:
        return False, None, f'{type(e).__name__}: {e}'


def _probe_ltm(cfg_item: dict):
    """实测 LTM：能否初始化（= LLM/embedding 通）、已存多少条。

    返回 (storage_ok, memory_count, detail)。
    """
    try:
        from core.providers.memory.ltm.provider import LtmProvider
        provider = LtmProvider(dict(cfg_item or {}))
        if not provider.is_healthy():
            return False, None, '初始化失败（检查 oMLX 是否在跑、模型名是否对）'
        stats = provider.stats()
        return True, stats.get('total'), f"LLM={stats.get('llm_model','')} embed={stats.get('embed_model','')}"
    except Exception as e:
        return False, None, f'{type(e).__name__}: {e}'


# 判停后端的显示名（配置里的 key → 页面上好懂的名字）
_DECISION_LABELS = {
    "jev": "JEV",
    "local_qwen": "本地千问",
    "local": "本地千问",
    "qwen": "本地千问",
    "ollaya": "ollaya",
}


def _collect_decision_services(config: dict) -> list:
    """列出 Decision 段下**全部**判停后端，供页面归成一组「判停服务」。

    主人 2026-10-06：判停有四种，页面上要**分开列**，并注明每个是否启用：
      1. 云端 TypeSafe JEV（`jev.shadow`，api.typesafe.ai，只记录不干预）
      2. 本地 StartLux-Decision-9B（`jev` 指向本地时承载它，当前主判）
      3. 本地千问（`local_qwen`，未启用）
      4. ollaya

    关键点（主人明确）：云端 TypeSafe 和本地 StartLux 在配置里**共用 `jev` 这个键**
    （StartLux 走 JEV 线兼容协议），但一个是云端、一个是本地，**必须分开列**，
    不能因为共用键就混作一条。

    Decision 段结构：
        Decision:
          jev:         {enabled, base_url, model, type, shadow:{enabled, base_url, model}}
          local_qwen:  {enabled, base_url, model, type}
          ollaya:      {enabled, base_url, model, type}
          provider: jev        # 当前主判是哪个

    - `provider` 是元字段（当前主判），跳过。
    - **未启用的后端不探测端口**（必然离线，探了反而误导），状态只显示「未启用」。
    """
    from urllib.parse import urlparse

    dec = config.get("Decision", {}) or {}
    current = str(dec.get("provider") or "").strip().lower()
    out = []

    def _emit(name, base, *, enabled, in_use, note, model=""):
        base = str(base or "").strip()
        if not base:
            return
        try:
            parsed = urlparse(base)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 0)
        except Exception:
            return
        online = _probe_port(host, port) if (enabled and port) else False
        out.append({
            'name': f"{name}·{model}" if model else name,
            'host': host,
            'port': port,
            'note': note,
            'online': online,
            'enabled': enabled,        # 页面据此标注「启用 / 未启用」
            'inUse': in_use,           # 页面据此标出「当前主判」
            'group': '判停服务',        # 页面据此归为一组
        })

    jev = dec.get("jev") if isinstance(dec.get("jev"), dict) else {}
    jev_base = str(jev.get("base_url") or "")
    shadow = jev.get("shadow") if isinstance(jev.get("shadow"), dict) else {}

    # ① 云端 TypeSafe JEV —— 单独列。
    #    jev 直接指云端时就是它本身；jev 指本地时，云端以 shadow 形式并存。
    if "typesafe.ai" in jev_base:
        _emit("TypeSafe JEV（云端）", jev_base,
              enabled=bool(jev.get("enabled")),
              in_use=(current == "jev"),
              model=str(jev.get("model") or ""),
              note="云端判停：判断用户一句话是否说完")
    elif shadow.get("enabled") and shadow.get("base_url"):
        _emit("TypeSafe JEV（云端）", shadow["base_url"],
              enabled=True, in_use=False,
              model=str(shadow.get("model") or ""),
              note="影子模式：只记录不干预（与主判做对比）")

    # ② 本地判停后端 —— jev 指向本地时实际承载 StartLux-Decision-9B，单独列。
    if jev_base and "typesafe.ai" not in jev_base:
        _model = str(jev.get("model") or "").strip()
        _emit(f"{_model}（本地）" if _model else "本地判停后端（JEV 线兼容）", jev_base,
              enabled=bool(jev.get("enabled")),
              in_use=(current == "jev"),
              note="判断用户一句话是否说完")

    # ③④ 其余 provider 条目（本地千问 / ollaya …）
    for key, cfg in dec.items():
        if key in ("provider", "jev") or not isinstance(cfg, dict):
            continue
        _emit(_DECISION_LABELS.get(key, key), cfg.get("base_url"),
              enabled=bool(cfg.get("enabled")),
              in_use=(key == current),
              model=str(cfg.get("model") or "").strip(),
              note="本地备用判停后端")

    return out


def _collect_dependent_services(config: dict) -> list:
    """汇总 HomeBrain 依赖的其他外部服务与它们的端口 + 在线状态。

    记忆系统不在这里——它更重要，单独由 _collect_memory_systems() 出一块详细面板。
    这里从配置文件里逐项读出来，而不是写死端口号（配置改了这里跟着变）。
    """
    from urllib.parse import urlparse

    services = []

    def _add(name, host, port, note, enabled=True, group='其他依赖服务'):
        if not enabled:
            return
        port = int(port)
        services.append({
            'name': name,
            'host': host,
            'port': port,
            'note': note,
            'online': _probe_port(host, port),
            'group': group,   # 页面据此分组（判停服务 / 其他依赖服务）
        })

    # 记忆系统已移出本表——它更重要，由 _collect_memory_systems() 单独出详细面板

    # —— 判停（Decision）：主人 2026-10-06 要求作为一组「判停服务」单列 ——
    # 判停有多种后端（JEV / 本地千问 / ollaya …），要**全部**列出并标注启用/未启用，
    # 而不是只显示当前启用的那个。放在最前面，因为它是语音链路的核心依赖。
    services.extend(_collect_decision_services(config))

    # —— 本地大模型（oMLX，OpenAI 兼容）——
    llm_name = (config.get("selected_module", {}) or {}).get("LLM", "")
    llm_port = None
    llm_cfg = (config.get("LLM", {}) or {}).get(llm_name, {}) or {}
    if llm_cfg.get("type") == "openai" and llm_cfg.get("url"):
        try:
            llm_port = urlparse(llm_cfg["url"]).port
        except Exception:
            llm_port = None
    _add("oMLX 本地模型", "127.0.0.1", llm_port or 8001,
         f"本地推理，当前 LLM：{llm_name or '未选择'}",
         enabled=llm_port is not None)

    # —— 摄像头 / 门铃 / MQTT（家用设备网关）——
    # 实测（2026-10-06）：frigate_api 在 plugins.describe_camera_event 下，
    # mqtt_host/mqtt_port 在顶层 security 下，不是我最初以为的 smart 段。
    frigate = None
    plugins_cfg = config.get("plugins", {}) or {}
    for _v in plugins_cfg.values():
        if isinstance(_v, dict) and _v.get("frigate_api"):
            frigate = _v["frigate_api"]
            break
    if frigate:
        try:
            _add("Frigate 摄像头", "127.0.0.1", urlparse(frigate).port or 15000,
                 "摄像头画面与人形/人脸检测")
        except Exception:
            pass

    security_cfg = config.get("security", {}) or {}
    mqtt_host = security_cfg.get("mqtt_host")
    mqtt_port = security_cfg.get("mqtt_port")
    if mqtt_port and server_gateway_configured(config):
        _add("MQTT 设备网关", mqtt_host or "127.0.0.1", mqtt_port, "ESP32 等设备接入")

    # —— 配置里存在但当前未启用的本地模型服务 ——
    # 主人 2026-10-06 问「是不是还有记忆系统等七八八的端口」→ 要全列。
    # 当前 selected LLM 是豆包云端，本地 oMLX(8001) 未被选中，但它在配置里存在，
    # 切回本地模型时就会用到，所以列出来备查。
    llm_name = (config.get("selected_module", {}) or {}).get("LLM", "")
    llm_cfg = (config.get("LLM", {}) or {}).get(llm_name, {}) or {}
    cur_llm_url = llm_cfg.get("url") or llm_cfg.get("base_url") or ""
    is_local_llm = False
    if cur_llm_url:
        try:
            _p = urlparse(cur_llm_url)
            is_local_llm = _p.hostname in ("127.0.0.1", "localhost", "::1")
        except Exception:
            pass

    if not is_local_llm:
        # 当前 LLM 走云端，把配置里的本地模型端口列出来
        for _name, _cfg in (config.get("LLM", {}) or {}).items():
            if not isinstance(_cfg, dict):
                continue
            _url = _cfg.get("url") or _cfg.get("base_url") or ""
            if not _url:
                continue
            try:
                _p = urlparse(_url)
                if _p.hostname in ("127.0.0.1", "localhost", "::1") and _p.port:
                    _add(f"本地模型 · {_name}", "127.0.0.1", _p.port,
                         f"配置中的本地模型（当前 LLM 用的是 {llm_name}，走云端）")
            except Exception:
                pass

    return services


def server_gateway_configured(config: dict) -> bool:
    """MQTT 网关是否真的启用（server.mqtt_gateway 非空才启用）"""
    return bool((config.get("server", {}) or {}).get("mqtt_gateway"))


def _read_data_json(name: str) -> dict:
    """读 data/ 下的 json（通知通道 / 中央画面 / 高德等），失败返回空 dict。"""
    import json as _json
    try:
        p = Path(__file__).resolve().parent.parent.parent / "data" / name
        return _json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


# 网页端（浏览器终端）device_id 前缀：任何 `web-*` 都是浏览器页面
# （`web-terminal-*` 数字星球/终端页、`web-led-*` 旧版 LED 星球页）。
# ⚠️ `admin-console` 是**管理台页面自己**的 WS 连接：它也是浏览器，但不是「AI 终端」
# （不会跟助手对话）。若把它算进去，主人一打开设置页在线数就 +1，纯属噪音 →
# 计入忽略名单（主人 2026-10-07）。
_WEB_DEVICE_PREFIXES = ("web-",)
_IGNORED_DEVICE_PREFIXES = ("admin-console", "ai-audio")


def _is_web_device(device_id) -> bool:
    return str(device_id or "").startswith(_WEB_DEVICE_PREFIXES)


def _is_ignored_device(device_id) -> bool:
    return str(device_id or "").startswith(_IGNORED_DEVICE_PREFIXES)


def get_live_device_ids():
    """服务端内存中**当前真实在线**的 device_id 集合（已按设备去重）。

    ⚠️ `devices` 表的 `status` 列会残留 `'online'`：只在**正常断开**时才调
    `update_device_offline()`，异常中断（进程崩溃 / 断网 / 快关标签页）不会调用。
    实测表里 11 台 `status='online'`，其中 9 台 `last_seen` 停在一天前 ——
    主控台「通道状态」「AI 终端设备」因此显示「10/31 台设备在线」（主人 2026-10-07 纠正）。

    返回 **None** 表示拿不到服务实例（无法判断），调用方应自行兜底；
    返回 **set()** 表示服务在跑但当前确实没有连接。
    """
    server = get_websocket_server_instance()
    if server is None:
        return None
    ids = set()
    try:
        # 每个 device_id 只保留最新一条连接（同设备多标签页去重）
        for ws in server.get_stage_target_connections():
            handler = getattr(ws, "_homebrain_handler", None)
            did = getattr(handler, "device_id", None)
            if did:
                ids.add(str(did))
    except Exception:
        pass
    return ids


def _collect_terminal_stats() -> dict:
    """统计**当前真实在线**的终端：网页端（device_id 以 web- 开头）与其它（ESP32 等）。

    ⚠️ 不能信 devices 表的 status 列（主人 2026-10-06 实测纠正）：
    连接异常中断（进程崩溃 / 网络断）时 update_device_offline() 不会被调用，
    status 会残留 'online' —— 实测表里有 9 台 status='online' 但 last_seen
    停在一天前，页面因此显示「网页端 10 台在线」，而主人实际只开了 1 个页面。

    正确判据 = **服务端内存里的活跃 WebSocket 连接**（active_connections），
    它随连接建立/断开实时增减，不受残留状态影响。按 device_id 去重
    （同一浏览器多标签页算 1 台终端），与 get_stage_target_connections 口径一致。

    拿不到服务实例时（理论上不会：本接口就跑在服务进程内）退回 devices 表
    + last_seen 时间窗（5 分钟），至少不会把一天前的残留算成在线。
    """
    stats = {"webOnline": 0, "webTotal": 0, "otherOnline": 0, "otherTotal": 0}

    live_ids = get_live_device_ids()
    if live_ids is not None:
        for device_id in live_ids:
            if _is_ignored_device(device_id):
                continue          # 管理台自身 / 内部音频设备，不算终端
            if _is_web_device(device_id):
                stats["webOnline"] += 1
            else:
                stats["otherOnline"] += 1
        stats["webTotal"] = stats["webOnline"]
        stats["otherTotal"] = stats["otherOnline"]
        return stats

    # —— 兜底：拿不到内存连接时，用 devices 表 + last_seen 时间窗 ——
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        try:
            rows = conn.execute("SELECT device_id, last_seen FROM devices").fetchall()
        finally:
            conn.close()
        now = time.time()
        for device_id, last_seen in rows:
            if _is_ignored_device(device_id):
                continue
            is_web = _is_web_device(device_id)
            try:
                fresh = (now - float(last_seen or 0)) <= 300
            except Exception:
                fresh = False
            if is_web:
                stats["webTotal"] += 1
                stats["webOnline"] += int(fresh)
            else:
                stats["otherTotal"] += 1
                stats["otherOnline"] += int(fresh)
    except Exception:
        pass
    return stats


def _curator_overview_item(config: dict) -> dict:
    """「记忆整理模型」在系统总览里的一行（2026-10-13 主人要求同步到服务器信息）。"""
    try:
        from core.utils import memory_curator_config as cc

        v = cc.public_view(config)
        return {"label": "记忆整理模型", "value": v["label"],
                "note": f"整理「把这段记下来」的多轮对话；{v['mode_label']}，失败退回 {v['fallback_model'] or '8B'}"}
    except Exception as e:
        return {"label": "记忆整理模型", "value": "读取失败", "note": str(e)[:60]}


def _collect_https_info(config: dict, real_ip: str) -> dict:
    """浏览器「安全来源」信息（2026-10-14 新增）。

    为什么单列一块：用户在自己的手机/电脑上用「家里 IP 或域名 + http」打开管理台时，
    浏览器不给 navigator.mediaDevices —— 声纹录入、人脸/头像拍照全部点了没反应。
    https 端口（自签证书）就是为解决它而加；这一页要能回答「这台机器的 https 地址是什么、
    证书到什么时候、当前 IP 在不在证书里」。
    """
    from core.utils import tls_certs

    server_config = config.get("server", {}) or {}
    enabled = bool(server_config.get("https_enabled", True))
    try:
        https_port = int(server_config.get("https_port", tls_certs.DEFAULT_HTTPS_PORT))
    except (TypeError, ValueError):
        https_port = tls_certs.DEFAULT_HTTPS_PORT
    try:
        wss_port = int(server_config.get("wss_port", tls_certs.DEFAULT_WSS_PORT))
    except (TypeError, ValueError):
        wss_port = tls_certs.DEFAULT_WSS_PORT

    active = enabled and https_port > 0
    cert = tls_certs.describe() or {}
    hosts = list(cert.get("dns_names") or []) + list(cert.get("ips") or [])
    return {
        'enabled': active,
        'httpsPort': https_port,
        'wssPort': wss_port,
        # 地址一律由网卡实测的 real_ip 派生（与这一页其他地址同口径）
        'httpsURL': f'https://{real_ip}:{https_port}/console' if active else '',
        'wssURL': f'wss://{real_ip}:{wss_port}/homebrain/v1/' if active else '',
        'certDownloadURL': '/api/tls/cert',
        'certExists': bool(cert),
        'certNotAfter': cert.get('not_after'),
        'certDaysLeft': cert.get('days_left'),
        'certHosts': hosts,
        'certCoversThisIP': real_ip in (cert.get('ips') or []),
    }


def _collect_system_overview(config: dict) -> dict:
    """汇总系统「用了什么、启用了什么」，供页面「系统功能总览」区块展示。

    主人 2026-10-06：这一页要能一眼看清系统的整体结构 —— 用了哪些服务、
    启用了哪些功能。数据全部是运行时真值，不写死：
      - 配置：selected_module（各模块当前引擎）、plugins（搜索/天气/新闻/家居/健康）
      - data/ 下的 json：.notification_channels.json（通知通道）、
        .center_window.json（中央画面/数字人）、.amap_config.json（高德）
      - sqlite：devices 表（在线终端统计）
    """
    sm = config.get("selected_module", {}) or {}
    plugins = config.get("plugins", {}) or {}

    def _mod(key, fallback="未选择"):
        return sm.get(key) or fallback

    def _truthy(v):
        """把 'false'/'0'/''/None 都当假。

        ⚠️ 实测 data/.notification_channels.json 里 electricity.enabled 是字符串
        'false'，直接 bool() 会得到 True —— 必须显式解析。
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "enabled")
        return bool(v)

    def _has_value(v):
        """判断字段是否填了实质内容（token/key/provider 等）。

        与 _truthy 不同：任意非空字符串都算「有值」（凭证不是布尔值）。
        """
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() not in ("", "false", "0", "no", "none")
        return bool(v)

    # ① 语音链路：各模块当前选中的引擎
    voice_items = [
        {"label": "语音识别 ASR", "value": _mod("ASR")},
        {"label": "语音合成 TTS", "value": _mod("TTS")},
        {"label": "大模型 LLM", "value": _mod("LLM")},
        {"label": "语音活动检测 VAD", "value": _mod("VAD")},
        {"label": "视觉理解", "value": _mod("VLLM")},
        {"label": "记忆系统", "value": _mod("Memory")},
        _curator_overview_item(config),
    ]

    # ② 联网能力：搜索 / 天气 / 地图 / 新闻
    web_search = plugins.get("web_search", {}) or {}
    weather = plugins.get("get_weather", {}) or {}
    news_now = plugins.get("get_news_from_newsnow", {}) or {}
    amap = _read_data_json(".amap_config.json")
    search_provider = str(web_search.get("provider") or "").strip()
    search_label = {"tavily": "Tavily", "metaso": "秘塔", "brave": "Brave",
                    "serper": "Serper", "jina": "Jina",
                    "duckduckgo": "DuckDuckGo", "searxng": "SearXNG"}.get(
        search_provider.lower(), search_provider or "未配置")
    cap_items = [
        {"label": "联网搜索", "value": search_label,
         "note": "web_search 工具，多引擎可切换"},
        {"label": "天气预报", "value": "和风天气（QWeather）",
         "note": f"默认城市：{weather.get('default_location') or '未设置'}"},
        {"label": "地图导航", "value": "高德地图" if amap else "未配置",
         "note": "URI API（免 key）+ Web 服务 API"},
        {"label": "新闻资讯",
         "value": "中新网 RSS" + ("＋newsnow" if news_now else ""),
         "note": (f"newsnow 源：{news_now.get('news_sources')}"
                  if news_now.get("news_sources") else "中新网社会/财经/国际")},
    ]

    # ③ 智能家居：HA（在用）+ Crestron Home（经 HA 桥接，暂未启用）
    ha = plugins.get("home_assistant", {}) or {}
    ha_url = str(ha.get("base_url") or "").strip()
    smart_items = [
        {"label": "Home Assistant", "value": ha_url,
         "note": "全屋设备抽象层，统一接管灯光/窗帘/传感器等",
         "enabled": bool(ha_url)},
        {"label": "Crestron Home（快思聪）", "value": "",
         "note": "高端存量系统，计划经 HA 社区集成桥接", "enabled": False},
    ]

    # ④ 中央画面 / 数字人 —— 三种画面方案，标注当前启用哪个
    #    主人 2026-10-06 追加：不只是「当前是哪个」，三种方案都要列出启用/未启用。
    cw = _read_data_json(".center_window.json")
    mode = cw.get("mode") or "default"
    _CENTER_PLANES = [
        ("default", "数字星球", "经典数字星球界面，不依赖数字人服务"),
        ("digital_human", "数字人", "使用 OpenTalking 数字人形象"),
        ("led_planet", "LED 星球", "L 型 LED 大屏专用星球界面"),
    ]
    dh_items = [
        {"label": "数字人引擎", "value": "OpenTalking",
         "note": "本地数字人服务，默认端口 8210"},
    ]
    for _key, _label, _note in _CENTER_PLANES:
        _on = (mode == _key)
        dh_items.append({
            "label": _label,
            "value": "",
            "enabled": _on,
            "note": _note + ("（当前使用）" if _on else ""),
        })

    # ⑤ 健康管理：OneNet（中国移动物联网平台）
    health = plugins.get("health_monitor", {}) or {}
    members = health.get("members", {}) or {}
    health_items = [
        {"label": "健康数据通道", "value": "OneNet（中国移动物联网平台）",
         "note": "血压计等健康设备上报"},
        {"label": "已绑定成员", "value": f"{len(members)} 人" if members else "未绑定",
         "note": "、".join(members.keys()) if members else "—"},
    ]

    # ⑥ 通知通道（data/.notification_channels.json）
    notify = _read_data_json(".notification_channels.json")
    _CH_LABELS = {"wechat": "微信", "sms": "短信", "electricity": "电费查询",
                  "phone": "电话呼叫", "email": "邮件", "feishu": "飞书"}
    notify_items = []
    for ch, label in _CH_LABELS.items():
        if ch not in notify:
            continue
        cfg_ch = notify.get(ch) or {}
        # 有 enabled 字段就按它判断；没有就按「配了凭证即启用」判断。
        # ⚠️ 布尔值可能被写成字符串（electricity.enabled='false'），走 _truthy 解析。
        if "enabled" in cfg_ch:
            enabled = _truthy(cfg_ch.get("enabled"))
        else:
            # 没有 enabled 字段 → 按「配了凭证类字段」判断。
            # 只认 token/key/secret/provider 等凭证字段：像 phone 的
            # emergency_contacts 是个非空字符串（联系人列表），不能当启用依据。
            _CRED_HINTS = ("token", "key", "secret", "provider",
                           "sign_name", "template_code", "password")
            enabled = any(
                _has_value(v) for k, v in cfg_ch.items()
                if any(h in k.lower() for h in _CRED_HINTS)
            )
        notify_items.append({
            "label": label,
            "value": "",
            "enabled": enabled,
        })
    if not notify_items:
        notify_items.append({"label": "通知通道", "value": "未配置"})

    # 飞书不在 .notification_channels.json 里，它配在 config.yaml 的 feishu 段
    # （主人 2026-10-06 指出通知通道还漏了飞书）。
    feishu = config.get("feishu", {}) or {}
    if feishu:
        _fe = _truthy(feishu.get("enabled", False))
        notify_items.append({
            "label": "飞书",
            "value": "",
            "enabled": _fe,
            "note": "飞书机器人接入（config.yaml: feishu）",
        })

    # ⑦ 在线终端（口径：服务端内存活跃连接，同设备多标签页算 1 台）
    term = _collect_terminal_stats()
    term_items = [
        {"label": "网页端设备", "value": f"{term['webOnline']} 台在线",
         "note": "浏览器终端（web-* 页面），打开即在线"},
        {"label": "AI 终端设备（ESP32）", "value": f"{term['otherOnline']} 台在线",
         "note": "ESP32 等无屏设备；管理台自身不计入"},
    ]

    return {"sections": [
        {"title": "语音链路", "items": voice_items},
        {"title": "联网能力", "items": cap_items},
        {"title": "智能家居", "items": smart_items},
        {"title": "中央画面与数字人", "items": dh_items},
        {"title": "健康管理", "items": health_items},
        {"title": "通知通道", "items": notify_items},
        {"title": "在线终端", "items": term_items},
    ]}


async def get_server_effective_config(request):
    """GET /api/config/server-effective —— 只读返回当前真正生效的服务器地址/端口。

    页面「系统设置 → 服务器信息」的**唯一**数据源。不做任何写入。
    """
    try:
        from config.settings import load_config
        config = load_config()
    except Exception as e:
        return web.json_response({
            'success': False,
            'message': f'读取配置失败: {e}'
        }, status=500)

    server_config = config.get("server", {}) or {}

    # 本机网卡 IP（UDP 探测）—— 这一页要回答的是「**这台机器**是什么」，
    # 所以一律用网卡实测值派生所有地址。
    #
    # 为什么不直接用 config 的 server.local_ip（主人 2026-10-06 纠正）：
    # 开发机上那个值是从 Mac Mini 同步来的 .214，在开发机上根本不成立，
    # 拿它派生地址等于把「另一台机器」的地址显示成本机状态，毫无意义。
    # 网卡实测值天然自洽 —— 在 Mac Mini 上跑得到 .214，在 MacBook 上跑得到 .209。
    real_ip = "127.0.0.1"
    try:
        import socket as _socket
        _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        real_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        pass

    http_port = int(server_config.get("http_port", 8003))
    bind_ip = server_config.get("ip", "0.0.0.0")
    auth_config = server_config.get("auth", {}) or {}

    # 数据库实际路径（页面原本的「数据存储路径」是纯装饰，真实路径是常量）
    try:
        db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "devices.db")
    except Exception:
        db_path = str(DB_PATH)

    payload = {
        # —— 本机身份：网卡实测，所有地址都从它派生 ——
        'realIP': real_ip,
        'bindIP': bind_ip,
        'httpPort': http_port,
        'wsPort': int(server_config.get("port", http_port)),
        'timezoneOffset': server_config.get("timezone_offset", 8),
        'authEnabled': bool(auth_config.get("enabled", False)),
        # —— 本机地址（全部由 realIP 派生）——
        # [2026-10-14] 对外统一说 /console（/admin/、/admin/index.html 后台仍兼容，
        # 但页面/文档/提示一律只出现 /console，避免"一会儿 console 一会儿 admin"）
        'consoleURL': f'http://{real_ip}:{http_port}/console',
        'apiBaseURL': f'http://{real_ip}:{http_port}',
        'wsURL': _resolve_websocket_url(config, real_ip),
        'otaURL': f'http://{real_ip}:{http_port}/homebrain/ota/',
        'visionURL': _resolve_vision_url(config, real_ip),
        'healthURL': f'http://{real_ip}:{http_port}/api/health',
        # —— 数据位置 ——
        'dataDir': str(Path(db_path).parent),
        'dbFile': db_path,
        # —— 浏览器安全来源：用户端录声纹/拍照必须走 https（2026-10-14）——
        'https': _collect_https_info(config, real_ip),
        # —— 依赖服务端口 ——
        'services': _collect_dependent_services(config),
        # —— 记忆系统单独一块（主人：这事比较重要，要看存储/抽取是否正常）——
        'memory': _collect_memory_systems(config),
        # —— 系统功能总览（主人 2026-10-06：一眼看清系统整体结构）——
        'systemOverview': _collect_system_overview(config),
        'memoryCurator': _curator_overview_item(config),
        # —— 去哪儿改 ——
        'configFile': _SERVER_CONFIG_FILE_HINT,
        'configSection': 'server:',
        'readConfigFromApi': bool(config.get("read_config_from_api", False)),
    }

    return web.json_response({'success': True, 'data': payload})
