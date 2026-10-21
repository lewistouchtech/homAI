"""
HomeAssistant 音乐播放插件（基于 Music Assistant）

设计参考（2026-09-17 重写）：
    参照 Music Assistant 官方 voice-support 仓库推荐的调用流程：
        1) GET  /api/config/config_entries/entry                           取 music_assistant 的 config_entry_id
        2) POST /api/services/music_assistant/search?return_response=true  按歌名/歌手搜到确定的 URI
        3) POST /api/services/music_assistant/play_media                   播放 URI

    关键：HA 的 service 调用返回 200 并不代表真的播了。
    （对离线 / 不存在的播放器，HA 同样返回 200 + 空数组，只在日志里打 WARNING）
    所以这里发出指令后统一轮询 media_player 的真实状态再决定播报内容，
    不再出现「回复 OK 但什么都没发生」的假成功。

与 mac_music 的分工（两者都保留，互不冲突）：
    mac_music = 服务器所在 Mac 本机的 Apple Music（osascript 控制）
    ha_music  = 房间里的音箱（经 HomeAssistant + Music Assistant 播放）
"""
import asyncio
import random
import time
from typing import TYPE_CHECKING, Dict, Any

import aiohttp

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from plugins_func.functions.ha_control import (
    HASS_BASE_URL,
    HASS_ACCESS_TOKEN,
    HEADERS,
    _HTTP_TIMEOUT,
    _friendly,
    _short,
)

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = "HA音乐"

_MA_ENTRY_TTL = 300.0
_ma_entry_cache: Dict[str, Any] = {"ts": 0.0, "entry_id": None}

HA_MUSIC_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "ha_music",
        "description": (
            "控制房间里的音箱播放音乐（HomeAssistant + Music Assistant）。"
            "在硬件设备（ESP32 语音终端）上提出的一切音乐请求都用本工具："
            "「播放音乐」「放首歌」「播放XX的歌」「暂停」「下一首」「声音大一点」。"
            "支持点播（song）、随机连播（不填 song）、暂停/继续/切歌、音量、随机开关。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "resume", "next", "previous", "stop", "volume", "shuffle", "status"],
                    "description": (
                        "要执行的动作。play=播放（需给 song）；pause=暂停；resume=继续播放；"
                        "next=下一首；previous=上一首；stop=停止；volume=调音量（需给 volume）；"
                        "shuffle=切换随机播放开关（需给 shuffle）；status=查询当前在放什么。默认 play。"
                    ),
                },
                "song": {
                    "type": "string",
                    "description": "要播放的歌曲名 / 专辑名 / 歌手名 / 播放列表名，如「稻香」「周杰伦」。多首用分号分隔，如「稻香;晴天」",
                },
                "artist": {
                    "type": "string",
                    "description": "歌手名，可选。用户说了歌手时填上可以提高准确度，如「周杰伦的稻香」填 稻香 + 周杰伦",
                },
                "album": {
                    "type": "string",
                    "description": "专辑名，可选。用户指定专辑时填上，帮助消除同名歌曲歧义",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["track", "album", "artist", "playlist", "radio", "podcast", "audiobook"],
                    "description": "要搜的媒体类型。用户说「听XX的歌」用 artist；「听XX专辑」用 album；具体某首歌用 track（默认）",
                },
                "player": {
                    "type": "string",
                    "description": "要控制的音箱名称，可选。不填就用当前可用的那台",
                },
                "area": {
                    "type": "string",
                    "description": "要播放的房间/区域名，可选，如「客厅」「卧室」。用户指定房间时填上，会在该区域的音箱播放",
                },
                "shuffle": {
                    "type": "boolean",
                    "description": "是否打乱顺序播放。用户说「随机放」「打乱」时填 true；明确说按顺序放时填 false。action=shuffle 时用于开关随机",
                },
                "radio_mode": {
                    "type": "boolean",
                    "description": "是否无限连播（放完自动续同类歌曲）。用户说「一直放」「别停」时填 true",
                },
                "volume": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "音量百分比 0-100，仅 action=volume 时使用",
                },
            },
            "required": ["action"],
        },
    },
}


async def _get_ma_entry_id(session: aiohttp.ClientSession):
    """取 Music Assistant 集成的 config_entry_id（search 必填，带缓存）"""
    now = time.time()
    if _ma_entry_cache["entry_id"] and now - _ma_entry_cache["ts"] < _MA_ENTRY_TTL:
        return _ma_entry_cache["entry_id"]
    try:
        url = f"{HASS_BASE_URL}/api/config/config_entries/entry"
        async with session.get(url, headers=HEADERS, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            entries = await resp.json()
        for entry in entries or []:
            if entry.get("domain") == "music_assistant":
                _ma_entry_cache["entry_id"] = entry.get("entry_id")
                _ma_entry_cache["ts"] = now
                return _ma_entry_cache["entry_id"]
        print(f"[{TAG}] 未找到 music_assistant 集成配置项")
    except Exception as e:
        print(f"[{TAG}] 读取 Music Assistant 配置失败: {e}")
    return None


async def _list_players(session: aiohttp.ClientSession):
    """列出可用的 media_player，Music Assistant 播放器排前面"""
    try:
        async with session.get(
            f"{HASS_BASE_URL}/api/states", headers=HEADERS, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return []
            states = await resp.json()
    except Exception as e:
        print(f"[{TAG}] 读取设备状态失败: {e}")
        return []

    players = []
    for state in states or []:
        eid = state.get("entity_id", "")
        if not eid.startswith("media_player."):
            continue
        if state.get("state") in ("unavailable", "unknown"):
            continue
        attrs = state.get("attributes", {}) or {}
        players.append(
            {
                "entity_id": eid,
                "name": attrs.get("friendly_name") or eid,
                "state": state.get("state"),
                "is_ma": "mass_player_type" in attrs,
                "attrs": attrs,
            }
        )
    players.sort(key=lambda p: (not p["is_ma"], p["entity_id"]))
    return players


async def _pick_player(session: aiohttp.ClientSession, wanted: str = None, area: str = None):
    """选播放器。area 优先于 wanted（用户指定房间时在该房间的音箱播放）。

    区域匹配用 HA 的 area registry（音箱实体所属区域），
    参考 MA 官方 voice-support 蓝图的目标判定顺序：
    指定房间 > 指定设备 > 请求来源房间 > 默认播放器。
    """
    players = await _list_players(session)
    if not players:
        return None, "房间里没有可用的音箱（HomeAssistant 里没有在线的播放器）"

    if area:
        area_key = str(area).strip().lower()
        # 取 area registry：entity_id -> area_id，以及 area_id -> 名称
        try:
            async with session.get(
                f"{HASS_BASE_URL}/api/config/area_registry/list",
                headers=HEADERS,
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                areas = await resp.json() if resp.status == 200 else []
            async with session.get(
                f"{HASS_BASE_URL}/api/config/entity_registry/list",
                headers=HEADERS,
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                entities = await resp.json() if resp.status == 200 else []
        except Exception:
            areas, entities = [], []
        area_ids = {
            a["area_id"]: (a.get("name") or "")
            for a in (areas or [])
        }
        matched_area_ids = {
            aid for aid, name in area_ids.items()
            if area_key in name.lower() or name.lower() in area_key
        }
        if matched_area_ids:
            entity_area = {
                e["entity_id"]: e.get("area_id")
                for e in (entities or [])
                if e.get("entity_id", "").startswith("media_player.")
            }
            in_area = [p for p in players if entity_area.get(p["entity_id"]) in matched_area_ids]
            if in_area:
                return in_area[0], None
        # 区域没匹配到音箱 → 退回设备名匹配，再退回默认
        names = "，".join(p["name"] for p in players)
        return None, f"「{area}」这个房间没有可用的音箱，可用的是：{names}"

    if wanted:
        key = str(wanted).strip().lower()
        for p in players:
            if (
                p["entity_id"].lower() == key
                or p["name"].lower() == key
                or key in p["name"].lower()
                or key in p["entity_id"].lower()
            ):
                return p, None
        names = "，".join(p["name"] for p in players)
        return None, f"没找到叫「{wanted}」的音箱，可用的是：{names}"
    return players[0], None


async def _search_media(session, entry_id, name, media_type=None, artist=None, album=None):
    """搜索媒体，返回 (item, error)。artist/album 透传给 MA 做歧义消解
    （MA 官方 voice-support 蓝图同样把 artist/album 作为独立参数传给 play_media）。"""
    payload: Dict[str, Any] = {"config_entry_id": entry_id, "name": name, "limit": 5}
    if media_type:
        payload["media_type"] = [media_type]
    if artist:
        payload["artist"] = artist
    if album:
        payload["album"] = album

    url = f"{HASS_BASE_URL}/api/services/music_assistant/search?return_response=true"
    try:
        async with session.post(
            url, headers=HEADERS, json=payload, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                return None, f"搜索音乐失败: HTTP {resp.status} {body[:120]}"
            data = await resp.json()
    except Exception as e:
        return None, f"搜索音乐失败: {e}"

    service_response = (data or {}).get("service_response") or {}
    keys = ["tracks", "albums", "playlists", "artists", "radio", "audiobooks", "podcasts"]
    if media_type:
        order = [media_type] + [k for k in keys if k != media_type]
        keys = [k for k in order if k in service_response]
    for key in keys:
        items = service_response.get(key) or []
        if items:
            return items[0], None
    return None, f"音乐库里没有找到「{name}」"


async def _search_tracks(session, entry_id, name, artist=None, limit=8):
    """只搜曲目，返回 track 列表（随机播放用，需要多首里挑）"""
    payload: Dict[str, Any] = {
        "config_entry_id": entry_id,
        "name": name,
        "media_type": ["track"],
        "limit": limit,
    }
    if artist:
        payload["artist"] = artist
    url = f"{HASS_BASE_URL}/api/services/music_assistant/search?return_response=true"
    try:
        async with session.post(
            url, headers=HEADERS, json=payload, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []
    return (data or {}).get("service_response", {}).get("tracks") or []


def _item_label(item: Dict[str, Any]) -> str:
    """把搜索结果拼成「歌名 - 歌手」这种便于播报的文字"""
    name = item.get("name") or item.get("title") or "这首歌"
    artists = item.get("artists") or []
    if isinstance(artists, list) and artists:
        first = artists[0]
        artist = first.get("name") if isinstance(first, dict) else str(first)
        if artist:
            return f"{name} - {artist}"
    return name


async def _wait_playing(session, entity_id: str, before: tuple, tries: int = 8, interval: float = 0.5):
    """轮询播放器真实状态，返回播报文案；超时返回 None（不谎报成功）"""
    for _ in range(tries):
        await asyncio.sleep(interval)
        try:
            async with session.get(
                f"{HASS_BASE_URL}/api/states/{entity_id}",
                headers=HEADERS,
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    continue
                state = await resp.json()
        except Exception:
            continue
        attrs = state.get("attributes", {}) or {}
        if state.get("state") == "playing":
            title = attrs.get("media_title")
            artist = attrs.get("media_artist") or attrs.get("media_album_artist")
            if title:
                return f"正在播放 {title}" + (f"，{artist}" if artist else "")
            return "开始播放了"
        now = (state.get("state"), attrs.get("media_content_id"))
        if now != before and state.get("state") in ("paused", "idle", "off"):
            return "已收到播放指令，但音箱还没出声"
    return None


async def handle_ha_music(conn: "ConnectionHandler", action: str, song: str = None,
                          artist: str = None, media_type: str = None,
                          player: str = None, volume: int = None,
                          album: str = None, area: str = None,
                          shuffle: bool = None, radio_mode: bool = None):
    """执行音乐相关操作，返回播报文案"""
    action = (action or "play").strip().lower()

    async with aiohttp.ClientSession(trust_env=False) as session:
        target, err = await _pick_player(session, player, area)
        if err:
            return err
        entity_id = target["entity_id"]
        display = _friendly(entity_id) if _friendly(entity_id) != entity_id else target["name"]
        before = (target["state"], target["attrs"].get("media_content_id"))

        # ---------- 播放 ----------
        if action == "play":
            entry_id = await _get_ma_entry_id(session)
            if not entry_id:
                return "HomeAssistant 里没有找到 Music Assistant 集成，无法搜索音乐"

            if not song:
                # 随机播放（2026-09-17 新增）：种子词搜 Apple Music 目录 →
                # 随机挑一首 → radio_mode 无限连播。种子词可用
                # plugins.home_assistant.music_random_seeds 覆盖。
                try:
                    seeds = (conn.config.get("plugins", {}).get("home_assistant", {}) or {}).get(
                        "music_random_seeds"
                    )
                except Exception:
                    seeds = None
                seeds = seeds or ["华语流行", "经典老歌", "流行", "轻音乐", "热门歌曲"]
                seed = random.choice(seeds)
                tracks = await _search_tracks(session, entry_id, seed)
                if not tracks:
                    return f"随机曲库搜索「{seed}」没有结果，稍后再试"
                item = random.choice(tracks[:8])
                payload = {
                    "entity_id": entity_id,
                    "media_id": item.get("uri"),
                    "enqueue": "play",
                    "radio_mode": True,
                }
                label = _item_label(item)
                spoken = await _wait_playing(session, entity_id, before)
                if spoken:
                    return f"{display} {spoken}（随机连播）"
                return f"已把「{label}」发给{display}（随机连播），音箱还没开始出声"

            # 多首点播（官方蓝图中 media_id 支持分号分隔的列表，MA 会依次入队）
            names = [n.strip() for n in str(song).split(";") if n.strip()]
            uris: list = []
            labels: list = []
            for one in names[:10]:
                item, err = await _search_media(
                    session, entry_id, one, media_type, artist, album
                )
                if err:
                    if not uris:
                        return err
                    continue
                uris.append(item.get("uri") or item.get("name"))
                labels.append(_item_label(item))
            if not uris:
                return f"音乐库里没有找到「{song}」"

            payload = {
                "entity_id": entity_id,
                "media_id": uris[0] if len(uris) == 1 else uris,
                "enqueue": "play",
            }
            # 官方蓝图：radio_mode 由用户/配置决定；点播时不强制
            if radio_mode is True:
                payload["radio_mode"] = True

            url = f"{HASS_BASE_URL}/api/services/music_assistant/play_media"
            async with session.post(
                url, headers=HEADERS, json=payload, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return f"播放失败: HTTP {resp.status} {body[:120]}"

            # shuffle（官方蓝图：播放后单独调 media_player.shuffle_set）
            if shuffle is not None:
                try:
                    async with session.post(
                        f"{HASS_BASE_URL}/api/services/media_player/shuffle_set",
                        headers=HEADERS,
                        json={"entity_id": entity_id, "shuffle": bool(shuffle)},
                        timeout=_HTTP_TIMEOUT,
                    ) as resp:
                        pass
                except Exception:
                    pass

            label = "、".join(labels[:3]) + ("等" if len(labels) > 3 else "")
            spoken = await _wait_playing(session, entity_id, before)
            if spoken:
                return f"{display} {spoken}"
            return f"已把「{label}」发给{display}，但音箱还没开始出声"

        # ---------- 查询 ----------
        if action == "status":
            attrs = target["attrs"]
            title = attrs.get("media_title")
            if target["state"] == "playing" and title:
                artist_name = attrs.get("media_artist") or attrs.get("media_album_artist")
                return f"{display} 正在播放 {title}" + (f"，{artist_name}" if artist_name else "")
            if target["state"] == "paused" and title:
                return f"{display} 暂停在 {title}"
            return f"{display} 当前没有在放音乐"

        # ---------- 音量 ----------
        if action == "volume":
            if volume is None:
                return "要把音量调到多少？"
            level = max(0, min(100, int(volume))) / 100.0
            url = f"{HASS_BASE_URL}/api/services/media_player/volume_set"
            payload = {"entity_id": entity_id, "volume_level": level}
            async with session.post(
                url, headers=HEADERS, json=payload, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return f"设置音量失败: HTTP {resp.status} {body[:120]}"

            await asyncio.sleep(1)
            try:
                async with session.get(
                    f"{HASS_BASE_URL}/api/states/{entity_id}",
                    headers=HEADERS,
                    timeout=_HTTP_TIMEOUT,
                ) as resp:
                    state = await resp.json() if resp.status == 200 else {}
            except Exception:
                state = {}
            real = ((state.get("attributes") or {}).get("volume_level"))
            if isinstance(real, (int, float)):
                return f"{display} 音量已调到百分之{round(real * 100)}"
            return f"已把{display}的音量调到百分之{level * 100:.0f}"

        # ---------- 随机开关（官方蓝图的 shuffle 能力） ----------
        if action == "shuffle":
            if shuffle is None:
                # 没给值就查询当前状态
                cur = target["attrs"].get("shuffle")
                if cur is None:
                    return f"{display} 不支持查询随机状态"
                return f"{display} 的随机播放当前是{'开' if cur else '关'}"
            url = f"{HASS_BASE_URL}/api/services/media_player/shuffle_set"
            payload = {"entity_id": entity_id, "shuffle": bool(shuffle)}
            async with session.post(
                url, headers=HEADERS, json=payload, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return f"设置随机播放失败: HTTP {resp.status} {body[:120]}"
            return f"已{'开启' if shuffle else '关闭'}{display}的随机播放"

        # ---------- 播放控制 ----------
        service_map = {
            "pause": "media_pause",
            "resume": "media_play",
            "next": "media_next_track",
            "previous": "media_previous_track",
            "stop": "media_stop",
        }
        service = service_map.get(action)
        if not service:
            return f"不支持的操作：{action}"

        url = f"{HASS_BASE_URL}/api/services/media_player/{service}"
        async with session.post(
            url, headers=HEADERS, json={"entity_id": entity_id}, timeout=_HTTP_TIMEOUT
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                return f"操作失败: HTTP {resp.status} {body[:120]}"

        await asyncio.sleep(1)
        spoken_map = {
            "pause": f"已暂停{display}",
            "resume": f"已让{display}继续播放",
            "next": f"已切到下一首",
            "previous": f"已切回上一首",
            "stop": f"已停止{display}的播放",
        }
        return spoken_map.get(action, f"{display} 操作完成")


@register_function("ha_music", HA_MUSIC_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def ha_music(conn: "ConnectionHandler", action: str = "play", song: str = None,
             artist: str = None, media_type: str = None, player: str = None,
             volume: int = None, album: str = None, area: str = None,
             shuffle: bool = None, radio_mode: bool = None):
    """工具入口（同步函数，内部投递到事件循环执行）"""
    print(f"[{TAG}] action={action}, song={song}, artist={artist}, media_type={media_type}, "
          f"player={player}, volume={volume}, album={album}, area={area}, "
          f"shuffle={shuffle}, radio_mode={radio_mode}")
    try:
        loop = getattr(conn, "loop", None)
        if loop is None or not loop.is_running():
            return ActionResponse(action=Action.RESPONSE, response="系统繁忙，请稍后再试")

        future = asyncio.run_coroutine_threadsafe(
            handle_ha_music(conn, action, song, artist, media_type, player, volume,
                            album, area, shuffle, radio_mode), loop
        )
        spoken = future.result(timeout=30)
        print(f"[{TAG}] 播报: {spoken}")
        return ActionResponse(action=Action.RESPONSE, response=_short(spoken, 200))
    except Exception as e:
        print(f"[{TAG}] 失败: {e}")
        return ActionResponse(action=Action.ERROR, response=_short(f"音乐操作失败: {e}"))
