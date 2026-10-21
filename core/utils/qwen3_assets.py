# -*- coding: utf-8 -*-
"""Qwen3-TTS 资源目录定位（2026-10-07）

克隆音色/音色设计产出的文件放在**仓库根**的 `tts-qwen3/` 下 ——
注意它不在 `homebrain-server` 子模块内，所以不能用固定层数的
`Path(__file__).parent.parent...` 去推：

- `core/providers/tts/qwen3_tts.py` 里写 `parent×4` 得到的是 `homebrain-server`，
  于是 `_is_valid_speaker()` 永远读不到克隆音色表 → 后台选的克隆音色被静默忽略；
- `core/http_server.py` 里写 `parent×3` 得到的是 `server/`，
  于是克隆文件落到了 `server/tts-qwen3/`（实测踩到）。

改为「从当前文件向上找含 tts-qwen3 的目录」，找不到再按仓库根兜底。
"""

import pathlib

_SUBDIR = "tts-qwen3"


def find_tts_dir(name: str = _SUBDIR) -> pathlib.Path:
    """返回仓库根下的 tts-qwen3 目录（可能尚不存在，调用方自行 mkdir）。"""
    here = pathlib.Path(__file__).resolve()
    for p in here.parents:
        cand = p / name
        if cand.is_dir():
            return cand
    # 兜底：找「含 server/homebrain-server 的祖先」= 仓库根
    for p in here.parents:
        if (p / "server" / "homebrain-server").is_dir():
            return p / name
    # 再兜底：core/utils/xxx.py → parents[4] = 仓库根
    return here.parents[4] / name


def clone_file() -> pathlib.Path:
    return find_tts_dir() / "cloned_voices.json"


def clone_dir() -> pathlib.Path:
    return find_tts_dir() / "cloned_voices"
