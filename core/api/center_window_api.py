#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数字人中间窗口配置 API（2026-09-18 阶段1 新增；同日追加 形象/背景分离）
- 存储独立于 data/.config.yaml（避免 yaml 全文重排踩坑），存 data/.center_window.json
- mode: default = 现有星系窗口；digital_human = OpenTalking 数字人窗口
- avatar: OpenTalking 形象 id（digital_human 模式下生效）
- background: 背景 id（见 data/backgrounds/；空/未选 => 默认 starfield 星空）
- 形象列表经本服务代理 OpenTalking /avatars，后台页面无需直连 8210
- 背景通过改写所选形象 manifest 的 metadata.composition.background（绝对路径）生效；
  已抠图形象（matting_status=transparent_ready 或已有遮罩）才支持换背景，
  新会话即时生效、无需重启 OpenTalking。
"""
import json
import os
import re
import time
from pathlib import Path

import aiohttp
from aiohttp import web

TAG = "center_window_api"

_SERVER_ROOT = Path(__file__).parent.parent.parent          # server/homebrain-server
DATA_FILE = _SERVER_ROOT / "data" / ".center_window.json"
BACKGROUNDS_DIR = _SERVER_ROOT / "data" / "backgrounds"

_REPO_ROOT = _SERVER_ROOT.parent.parent                     # 项目根
_DEFAULT_AVATARS_ROOT = _REPO_ROOT / "services" / "opentalking" / "examples" / "avatars"

OPENTALKING_BASE = os.environ.get("OPENTALKING_URL", "http://127.0.0.1:8210").rstrip("/")

# led_planet = L 型 LED 屏专用「LED 星球」界面（2026-10-03 主人新增第三个中央形象版本）
#   1920×960 拼接屏专用布局，待机时自动退回水族馆鱼缸界面。
_VALID_MODES = ("default", "digital_human", "led_planet")
_LOCAL_MODELS = ("mock", "quicktalk", "wav2lip", "ultralight")   # 本机（Mac MPS）能跑的渲染后端；wav2lip 需权重已下载且服务带 OPENTALKING_WAV2LIP_DEVICE=mps；ultralight 需训练产物 services/ultralight/checkpoint/v1/last.pth
_DEFAULT_BACKGROUND = "starfield"
_BUILTIN_NAMES = {"starfield": "星空", "study": "书房", "livingroom": "客厅", "bedroom": "卧室"}
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_BG_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_AVATAR_ID_RE = re.compile(r"^[^/\\\x00]{1,160}$")

# 特殊背景：透明（人像合成到纯黑底，前端用 mix-blend-mode:lighten 吃掉黑色 → 浮于看板原背景）
_TRANSPARENT_ID = "transparent"
_KEY_BLACK = BACKGROUNDS_DIR / "_key_black.png"
_TRANSPARENT_THUMB = BACKGROUNDS_DIR / "_transparent_thumb.png"


def _avatars_root() -> Path:
    return Path(os.environ.get("OPENTALKING_AVATARS_ROOT", str(_DEFAULT_AVATARS_ROOT)))


def _read_cfg() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ============================================================================
# 语音切换中央形象（2026-09-24，主人批准）：星球/数字人两条链路共用同一套
# 检测与写配置逻辑，保证两边行为一致（主人要求：不要两边各写一套）。
# 检测规则：去标点短句（≤12字）+ 切换动词（切换/切回/切到/换成/换到）
# + 目标（星球→default / 数字人→digital_human）；
# 否定语境排除（"切换到数字星球不管用"是抱怨不是指令，2026-09-24 08:29 实测曾误切换）。
# ============================================================================
_SWITCH_VERBS = ("切换", "切回", "切到", "换成", "换到")
_SWITCH_NEGATE = ("不管用", "没用", "失效", "失灵", "不工作", "没反应", "没有反应", "失败")


def detect_mode_switch(text: str):
    """检测切换中央形象指令，返回目标 mode（default/digital_human/led_planet），非指令返回 None。"""
    plain = re.sub(r"[^\w一-鿿]", "", text or "")
    if not plain or len(plain) > 12:
        return None
    if any(w in plain for w in _SWITCH_NEGATE):
        return None
    if any(v in plain for v in _SWITCH_VERBS):
        # 顺序敏感：LED 星球必须在裸"星球"之前判断，
        # 否则 "切换到LED星球" 会被下面的 "星球" 分支抢先返回 default。
        if "led" in plain.lower() or "LED星球".lower() in plain.lower() or "拼接屏" in plain:
            return "led_planet"
        if "星球" in plain:
            return "default"
        if "数字人" in plain:
            return "digital_human"
    return None


def switch_mode_to(mode: str) -> tuple:
    """语音切换共用写配置入口：只改 mode，保留已配置的 avatar/background。
    返回 (ok, message)。与 save_center_window（后台手动切换）共用配置文件。"""
    if mode not in _VALID_MODES:
        return False, f"mode 只能是 {_VALID_MODES}"
    cfg = _read_cfg()
    if cfg.get("mode") == mode:
        return True, "已在目标模式"
    if mode == "digital_human" and not (cfg.get("avatar") or "").strip():
        return False, "尚未配置数字人形象，请先在后台选择"
    # 星球模式（default / led_planet）不做背景存在性校验，理由同 save_center_window
    cfg["mode"] = mode
    DATA_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    applied, detail = (False, "非数字人模式，未应用背景")
    if mode == "digital_human":
        applied, detail = _apply_background(cfg["avatar"], cfg.get("background") or _DEFAULT_BACKGROUND)
    return True, f"已切换到 {mode}（背景应用: {applied}）"


def _bg_path(bg_id: str):
    """按 id 找到背景图文件（支持多扩展名），非法/不存在返回 None。"""
    if not bg_id or not _BG_ID_RE.match(bg_id):
        return None
    if bg_id == _TRANSPARENT_ID:
        return _KEY_BLACK if _KEY_BLACK.is_file() else None
    for ext in _IMG_EXTS:
        p = BACKGROUNDS_DIR / f"{bg_id}{ext}"
        if p.is_file():
            return p
    return None


def _bg_dict(p: Path) -> dict:
    stem = p.stem
    return {
        "id": stem,
        "name": _BUILTIN_NAMES.get(stem, stem),
        "builtin": stem in _BUILTIN_NAMES,
        "url": f"/api/center-window/backgrounds/{stem}",
    }


def _apply_background(avatar: str, bg_id: str):
    """把所选背景写入形象 manifest 的 metadata.composition.background（绝对路径）。"""
    if not avatar:
        return False, "未选择形象"
    bg = _bg_path(bg_id)
    if bg is None:
        return False, f"背景不存在：{bg_id}"
    manifest_path = _avatars_root() / avatar / "manifest.json"
    if not manifest_path.is_file():
        return False, f"形象目录不存在：{manifest_path}"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = manifest.setdefault("metadata", {})
        comp = metadata.setdefault("composition", {})
        comp["background"] = str(bg)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True, str(bg)
    except Exception as e:  # noqa: BLE001
        return False, f"写入 manifest 失败：{e}"


def _supports_background(avatar_dir: Path) -> bool:
    """形象是否能换背景：需已抠图（有透明通道/遮罩），否则合成器无法分离人像。"""
    try:
        manifest = json.loads((avatar_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    metadata = manifest.get("metadata") or {}
    if metadata.get("matting_status") == "transparent_ready":
        return True
    comp = metadata.get("composition") or {}
    mask_rel = str(comp.get("mask") or "")
    if mask_rel and (avatar_dir / mask_rel).is_file():
        return True
    src = avatar_dir / "source" / "source.png"
    try:
        from PIL import Image  # 局部导入，避免影响服务启动
        if src.is_file():
            img = Image.open(src)
            return img.mode == "RGBA" or "transparency" in img.info
    except Exception:  # noqa: BLE001
        return False
    return False


def _is_locally_supported(avatar: dict) -> bool:
    """本机渲染后端只有 mock / quicktalk；其余（wav2lip、flashhead 等）建会话会失败。"""
    if (avatar.get("client_renderer") or {}).get("type"):
        return True
    return str(avatar.get("model_type") or "").lower() in _LOCAL_MODELS


# ---------------------------------------------------------------- 配置读写

def _digital_human_vad_params() -> tuple:
    """读 VAD.DigitalHumanVAD 两段式判停参数（候选静音 + 确认窗口 + 滞回比例 + 爆发切分）。

    缺省 300/500ms、hold_ratio 0.6（对齐星球 SileroVAD threshold_low/threshold = 0.3/0.5），
    near_burst_ratio 4.0 / near_burst_frames 5（近场主人插话切分，基线=环境噪声 EMA）。
    """
    try:
        from config.config_loader import load_config
        vad = (load_config().get("VAD") or {}).get("DigitalHumanVAD") or {}
        cand = int(vad.get("candidate_silence_ms", 300))
        conf = int(vad.get("confirmation_window_ms", 500))
        hold = float(vad.get("hold_ratio", 0.6))
        nbr = float(vad.get("near_burst_ratio", 4.0))
        nbf = int(vad.get("near_burst_frames", 5))
        return (max(50, min(2000, cand)), max(50, min(2000, conf)),
                max(0.1, min(0.9, hold)), max(0, min(10.0, nbr)),
                max(0, min(30, nbf)))
    except Exception:
        return 300, 500, 0.6, 4.0, 5


async def get_center_window(request: web.Request) -> web.Response:
    cfg = _read_cfg()
    vad_cand, vad_conf, vad_hold, vad_nbr, vad_nbf = _digital_human_vad_params()
    return web.json_response({
        "success": True,
        "mode": cfg.get("mode", "default"),
        "avatar": cfg.get("avatar", ""),
        "background": cfg.get("background", "") or _DEFAULT_BACKGROUND,
        "vad_candidate_ms": vad_cand,
        "vad_confirm_ms": vad_conf,
        "vad_hold_ratio": vad_hold,
        "vad_near_burst_ratio": vad_nbr,
        "vad_near_burst_frames": vad_nbf,
    })


async def save_center_window(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "请求体必须是 JSON"}, status=400)
    mode = body.get("mode", "default")
    if mode not in _VALID_MODES:
        return web.json_response({"success": False, "message": f"mode 只能是 {_VALID_MODES}"}, status=400)
    avatar = str(body.get("avatar") or "").strip()
    if mode == "digital_human" and not avatar:
        return web.json_response({"success": False, "message": "数字人模式必须选择形象"}, status=400)

    # 背景图（抠像合成底图）只对「数字人」模式有意义：
    # 数字星球 / LED 星球都是自绘全屏画面，不做人物抠像合成，
    # 因此这两个模式跳过背景存在性校验（2026-10-03 主人裁决方案B）。
    # 修的既有问题：data/backgrounds/ 目录不存在时，_DEFAULT_BACKGROUND("starfield")
    # 找不到文件会让「保存配置」一律 400，两个星球模式根本存不进去。
    if mode == "digital_human":
        background = str(body.get("background") or "").strip() or _DEFAULT_BACKGROUND
        if _bg_path(background) is None:
            return web.json_response({"success": False, "message": f"背景不存在：{background}"}, status=400)
    else:
        # 星球模式：原样保留已配置值，不因缺目录报错
        background = str(body.get("background") or "").strip() or _read_cfg().get("background", "") or _DEFAULT_BACKGROUND

    cfg = _read_cfg()
    cfg["mode"] = mode
    cfg["avatar"] = avatar
    cfg["background"] = background
    DATA_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    applied, detail = (False, "非数字人模式，未应用背景")
    if mode == "digital_human":
        applied, detail = _apply_background(avatar, background)

    return web.json_response({
        "success": True,
        "mode": mode,
        "avatar": avatar,
        "background": background,
        "background_applied": applied,
        "background_detail": detail,
    })


async def list_avatars(request: web.Request) -> web.Response:
    """代理 OpenTalking /avatars（后台下拉框数据源）"""
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{OPENTALKING_BASE}/avatars") as resp:
                data = await resp.json(content_type=None)
        items = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "model_type": a.get("model_type"),
                "client_renderer": bool((a.get("client_renderer") or {}).get("type")),
                "supported": _is_locally_supported(a),
                "supports_background": _supports_background(_avatars_root() / str(a.get("id"))),
            }
            for a in data if isinstance(a, dict) and a.get("id")
        ]
        return web.json_response({"success": True, "avatars": items})
    except Exception as e:
        return web.json_response(
            {"success": False, "message": f"OpenTalking 不可达（{e}），请确认已启动 services/opentalking/start.sh"},
            status=502,
        )


async def get_avatar_preview(request: web.Request) -> web.Response:
    """返回形象缩略图（preview.png 优先，回落 reference.png），供后台平铺选择。"""
    avatar_id = request.match_info.get("avatar_id", "")
    if not _AVATAR_ID_RE.match(avatar_id):
        return web.json_response({"success": False, "message": "非法形象 ID"}, status=400)
    root = _avatars_root()
    avatar_dir = root / avatar_id
    for name in ("preview.png", "reference.png"):
        p = avatar_dir / name
        try:
            if p.is_file() and root.resolve() in p.resolve().parents:
                return web.FileResponse(p, headers={"Cache-Control": "no-cache"})
        except OSError:
            continue
    return web.json_response({"success": False, "message": "无预览图"}, status=404)


# ---------------------------------------------------------------- 背景库

async def list_backgrounds(request: web.Request) -> web.Response:
    if not BACKGROUNDS_DIR.is_dir():
        return web.json_response({"success": True, "backgrounds": []})
    files = [
        p for p in BACKGROUNDS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMG_EXTS and not p.stem.startswith("_")
    ]
    files.sort(key=lambda p: (p.stem not in _BUILTIN_NAMES, p.name))
    items = [_bg_dict(p) for p in files]
    if _KEY_BLACK.is_file():
        items.insert(0, {
            "id": _TRANSPARENT_ID,
            "name": "透明（浮于看板原背景）",
            "builtin": True,
            "transparent": True,
            "url": f"/api/center-window/backgrounds/{_TRANSPARENT_ID}",
        })
    return web.json_response({"success": True, "backgrounds": items})


async def get_background_image(request: web.Request) -> web.Response:
    bg_id = request.match_info.get("bg_id", "")
    if bg_id == _TRANSPARENT_ID:
        # 缩略图用棋盘格表示“透明”
        if _TRANSPARENT_THUMB.is_file():
            return web.FileResponse(_TRANSPARENT_THUMB, headers={"Cache-Control": "no-cache"})
        return web.json_response({"success": False, "message": "背景不存在"}, status=404)
    p = _bg_path(bg_id)
    if p is None:
        return web.json_response({"success": False, "message": "背景不存在"}, status=404)
    return web.FileResponse(p, headers={"Cache-Control": "no-cache"})


async def upload_background(request: web.Request) -> web.Response:
    try:
        data = await request.post()
        field = data.get("file")
        if not isinstance(field, web.FileField):
            return web.json_response({"success": False, "message": "缺少文件字段 file"}, status=400)
        ext = Path(field.filename or "").suffix.lower()
        if ext not in _IMG_EXTS:
            return web.json_response({"success": False, "message": f"仅支持 {_IMG_EXTS}"}, status=400)
        BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
        bg_id = f"custom-{int(time.time())}"
        dest = BACKGROUNDS_DIR / f"{bg_id}{ext}"
        dest.write_bytes(field.file.read())
        return web.json_response({"success": True, "background": _bg_dict(dest)})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"success": False, "message": f"上传失败：{e}"}, status=500)
