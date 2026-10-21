"""家居引擎（2026-10-13 新建）

统一的智能家居控制层：registry（实时状态）/ resolver（语义寻址）/ actions（动作表）
/ executor（前置检查+后置校验）/ guard（安全分级）/ session（上次控制记忆）/ speech（人话播报）。
所有入口（语音直通、LLM 工具 ha_control、场景、批量）都走 engine.run()。
"""
from core.home.engine import EngineResult, catalog_summary, confirm_or_cancel, device_area, run

__all__ = ["EngineResult", "run", "confirm_or_cancel", "catalog_summary", "device_area"]
