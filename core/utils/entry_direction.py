# -*- coding: utf-8 -*-
"""进出方向判定（主入口摄像头）

依据 Frigate 事件的 path_data（人体脚底中心的归一化轨迹）。
主入口摄像头的画面里，门在**右侧**（x≈0.6~0.7），室内在**左下**：
    进门：起点在右侧（门口）→ 终点在左侧（室内）
    出门：起点在左侧（室内）→ 终点在右侧（门口）
其余（在室内走动、坐在桌前、在门口徘徊）一律判 unknown。

2026-10-13 用近 8 天 911 条真实事件校准（默认参数）：
    已识别（必然是正面进门）190 条 → 判进门 88、未知 97、出门 5
    未识别 721 条 → 进门 71、出门 111、未知 539
进门/出门数量级吻合；**拿不准就判 unknown，unknown 时不播报**（宁可少说）。
摄像头位置变了需要重新校准，参数走配置 security.arrival_policy.direction。
"""
from typing import Any, Dict, Iterable, List, Optional

IN = "in"
OUT = "out"
UNKNOWN = "unknown"

DEFAULT_PARAMS = {
    "door_x": 0.5,     # x ≥ 此值视为"门口一侧"
    "room_x": 0.35,    # x ≤ 此值视为"室内一侧"
    "min_move": 0.2,   # 起终点 x 方向位移至少这么多才算有方向
}


def _points(path_data: Iterable[Any]) -> List[List[float]]:
    """兼容 [[x,y],ts] 与 [x,y] 两种元素格式，返回 [[x,y],...]"""
    pts: List[List[float]] = []
    for item in path_data or []:
        try:
            first = item[0]
            if isinstance(first, (list, tuple)):
                pts.append([float(first[0]), float(first[1])])
            else:
                pts.append([float(item[0]), float(item[1])])
        except Exception:
            continue
    return pts


def classify_direction(path_data, params: Optional[Dict[str, Any]] = None) -> str:
    """返回 in / out / unknown。轨迹少于 2 个点一律 unknown。"""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: float(v) for k, v in params.items() if k in DEFAULT_PARAMS})
    pts = _points(path_data)
    if len(pts) < 2:
        return UNKNOWN
    sx, ex = pts[0][0], pts[-1][0]
    if sx >= p["door_x"] and ex <= p["room_x"] and sx - ex >= p["min_move"]:
        return IN
    if sx <= p["room_x"] and ex >= p["door_x"] and ex - sx >= p["min_move"]:
        return OUT
    return UNKNOWN
