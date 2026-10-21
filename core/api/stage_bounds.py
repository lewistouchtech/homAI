# -*- coding: utf-8 -*-
"""舞台区域坐标存储 — 前端报告真实像素坐标，后端用来对齐应用窗口"""

# 默认值（基于 1728x1117 逻辑分辨率 Mac）
_stage_bounds = {
    "x": 280,
    "y": 60,
    "width": 1168,   # 1728 - 280*2
    "height": 1001,  # 1117 - 60 - 56
}


def get_stage_bounds() -> dict:
    return dict(_stage_bounds)


def set_stage_bounds(x: int, y: int, width: int, height: int):
    _stage_bounds["x"] = x
    _stage_bounds["y"] = y
    _stage_bounds["width"] = width
    _stage_bounds["height"] = height
