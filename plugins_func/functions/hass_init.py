"""Home Assistant 设备清单 → 提示词（2026-10-13 改造）

改造前的问题（实测）：
  1. 这里自己拉一遍 HA「暴露给语音助手」清单，与 ha_control 的 ALLOWED_ENTITIES、
     core/home/registry 三份来源各拉一次，谁新谁旧说不清；
  2. 提示词里逐个列 `- 名字：entity_id`，79 个实体≈2500 字，白占上下文；
  3. 把 entity_id 摆在眼前反而诱导大模型自己编 ID —— 实测它编过
     `light.living_room`、`light.light_entity`、`camera.main_entrance`，
     ha_control 只能回「不在允许控制列表中」。

改造后：
  1. 设备状态唯一来源 = `core/home/registry`（一条 WS 长连接 + 实时订阅）；
  2. 注入提示词的是「房间 → 类型×数量」紧凑摘要（实测 52 字）；
  3. 设备名由用户口语提供，交给 `core/home/resolver` 做语义寻址
     （拼音同音、中文数字、别名、区域/楼层收窄都能匹配）；
  4. ha_control 的工具 schema 已不再接受 entity_id，大模型无从编造。
"""

from config.logger import setup_logging
from core.home import engine as home_engine
from core.home.registry import Entity, get_registry

TAG = __name__
logger = setup_logging()

# 提示词里的行为约束（与 ha_control 工具描述保持一致，两处都写是为了
# 「提示词」和「工具 schema」任一被裁剪时约束仍在）
_RULES = (
    "说明：控制/查询家里的设备一律用 ha_control 工具。commands 里写"
    "「用户原话里的设备名、房间、楼层、类型」，绝对不要写 entity_id，也不要自己编设备名——"
    "名字对不上时系统会自动做同音/别名匹配，实在找不到它会如实告诉用户。\n"
    "重要：用户要求开/关/调节设备时，必须调用 ha_control 执行，"
    "不要因为猜测设备当前状态而跳过调用——状态由工具实时校验，重复执行开关是安全的。\n"
    "默认规则：用户说「开灯/关灯」未指明具体灯时，直接对该位置所有灯执行，"
    "不要反问确认，执行完简要告知结果。\n"
    "亮度：说得出具体数值就用 set_brightness + value（0-100，「最亮/百分之百」=100、「一半」=50）；"
    "「调亮一点/亮一些」用 brightness_up，「调暗一点/暗一些」用 brightness_down，不要自己算数值。\n"
    "窗帘：「停/别动了」用 stop（不是关）。空调：set_temperature 给度数，"
    "「高一点/低一点」用 temperature_up/temperature_down。\n"
    "门锁：必须明确 lock（上锁）或 unlock（开锁），不能用 on/off；"
    "开锁系统会先向用户本人确认，你不需要也不要替用户确认。\n"
    "设备离线或没接入时，工具会如实说明，你要照实转告用户，不要说「已打开」。\n"
    "组合场景（如心情不好、观影、浪漫晚餐）可在一次回复里并发调用多个工具搭配多个设备"
    "（如调暗灯光+关窗帘+放音乐）。\n"
)


def _as_entities(devices):
    """把各种形态的设备清单统一成 core/home/registry.Entity 列表"""
    out = []
    for d in devices or []:
        if isinstance(d, Entity):
            out.append(d)
        elif isinstance(d, dict):
            out.append(Entity(
                entity_id=str(d.get("entity_id") or ""),
                name=str(d.get("name") or ""),
                state=str(d.get("state") or "unknown"),
                attrs=d.get("attributes") or {},
                area=str(d.get("area") or d.get("room") or ""),
                floor=str(d.get("floor") or ""),
                device_class=str(d.get("device_class") or ""),
            ))
    return [e for e in out if e.entity_id]


def format_device_catalog(devices=None):
    """把设备清单压成「房间 → 类型×数量」摘要（注入提示词用）

    2026-09-15 的老规矩仍然保留：不带当前状态。此前「（当前 on）」会让 LLM 在用户开灯时
    自作主张跳过工具调用（"灯已经开着呢"），实测灯控"完全没反应"。状态判断交给工具。

    2026-09-17 剔除 unavailable/unknown 的老规矩也保留（在 catalog_summary 里做）：
    列出来也控制不了，还白占 token；离线数量单独提示一句，让模型如实转告。
    """
    ents = _as_entities(devices) if devices is not None else get_registry().all()
    if not ents:
        return ""
    return home_engine.catalog_summary(entities=ents)


async def append_devices_to_prompt(conn):
    """把家里设备概况 + 控制规则追加进系统提示词"""
    if conn.intent_type != "function_call":
        return
    funcs = conn.config["Intent"][conn.config["selected_module"]["Intent"]].get(
        "functions", []
    )
    # 2026-09-15 的老修复保留：门槛是 ha_control（hass_get_state/hass_set_state 已下线，
    # 原先按旧工具名判断导致清单注入随工具移除而失效）
    if "ha_control" not in funcs:
        return

    reg = get_registry()
    try:
        await reg.ensure_ready()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"拉取家居设备清单失败: {e}")
    ents = reg.all()

    if ents:
        catalog = format_device_catalog(ents)
        conn.prompt += f"\n家里现有的设备（按房间统计）：{catalog}\n" + _RULES
        # 同步刷新 ha_control 的设备边界与工具描述（与本提示词同源，都来自 registry）
        try:
            from plugins_func.functions.ha_control import sync_allowed_entities
            await sync_allowed_entities()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"同步ha_control设备清单失败: {e}")
    else:
        # HA 不可达时的兜底：退回静态配置清单（老行为，保留）
        plugins_config = conn.config.get("plugins", {})
        config_source = (
            "home_assistant" if plugins_config.get("home_assistant") else "hass_get_state"
        )
        plugin_config = plugins_config.get(config_source) or {}
        devices_cfg = plugin_config.get("devices", "")
        if isinstance(devices_cfg, list):
            device_str = "\n".join(str(d) for d in devices_cfg)
        else:
            device_str = str(devices_cfg)
        conn.prompt += (
            "\n下面是我家智能设备列表（位置，设备名），可以通过 ha_control 控制；"
            "清单可能已过时，控制结果以工具返回为准：\n" + device_str + "\n"
        )
    conn.dialogue.update_system_message(conn.prompt)
