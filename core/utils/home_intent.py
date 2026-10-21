# -*- coding: utf-8 -*-
"""智能家居直通（JEV 推测性扇出，云端先行）— P1/P2/P3 全量。

2026-10-02 方案（docs/plans/2026-10-02_智能家居JEV推测性扇出方案.md，主人已拍板）：
- 云端先行：判定端点复用 Decision.jev.shadow 配置（api.typesafe.ai / jev-latest），
  key 走 TYPESAFE_SHADOW_API_KEY（环境变量 → 独立读 .env 兜底），可用
  Decision.jev.home_intent 段覆盖。评测基线 tests/eval_home_intent_p1.py。
- 安全闸（第 2 条）：开关灯/空调/窗帘/媒体/风扇/开关直接执行；lock、
  alarm_control_panel 等不在白名单 → 永不直通（门锁必须确认）。

分期：
- P1：单设备单动作直通 + 单设备查询（已验收 2026-10-02）。
- P2：多设备 fan-out（一次判定两组 device/action/scope，同请求并行）、
  scope 展开（single/同类全部/同房间全部）、亮度数值（句子含数字才加问）、
  场景（「观影模式」，内置+可配置）。
- P3：聚合查询直答（「家里几盏灯开着」→ ha_summary）+ 判定日志
  （data/home_intent_log.jsonl + 后台日志页）。

判定流程：
1. 代码层预筛候选池（名称子串/分词 + 类别词→domain），0 候选直接降级
   （闲聊、与家无关的句子天然不触发，省一次云端调用）；
2. 一次请求 fan-out：intent / scene / device+action+scope × 2 组 /（含数字时）brightness；
3. 全部校验通过 → 按 scope 展开实体 → 并发执行 → 一句播报；
4. 任何一步失败/超时/异常 → 返回 None，行为与接入前完全一致。
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

TAG = "HomeIntent"

# ==================== 判定问题 ====================

INTENT_CRITERIA = {
    "control": "要控制/改变设备状态（打开、关闭、调亮度、播放暂停、进入场景等），"
               "包括『灯都关了』『把X打开』这类集合句",
    "query": "只想了解状态或数量、不改变任何东西（如『几盏灯开着』『开着吗』）",
    "chitchat": "闲聊、知识问答、生活问题，与控制/查询家里设备无关",
    "other": "其他无法归类的情况",
}

ACTION_CRITERIA = {
    "turn_on": "打开/开启某物，或把某物调到具体数值（如调到60%、亮度百分之六十）",
    "turn_off": "关闭某物（『关了』『关上』『都关了』）", "toggle": "切换开/关",
    "open_cover": "打开窗帘/窗", "close_cover": "关闭窗帘/窗",
    "brightness_up": "调亮（『亮一点』，无具体目标值）",
    "brightness_down": "调暗（『暗一点』，无具体目标值）",
    "pause": "暂停", "play": "播放/继续", "volume_up": "调大音量", "volume_down": "调小音量",
    "query_state": "仅查询状态/内容/数量，不改变任何东西", "none": "无动作或不针对设备",
}

SCOPE_CRITERIA = {
    "single": "只操作/查询选中的这一个设备",
    "all_domain": "操作/查询候选中与选中设备同类（同类型）的全部设备，如「所有窗帘」「灯都关了」",
    "all_room": "操作/查询选中设备所在房间的全部候选设备，如「客厅的都关了」",
    "none": "这句话不指向任何具体设备",
}

# 内置场景（HA 无 scene 实体，HomeBrain 自建联动；配置 Decision.jev.home_intent.scenes 可覆盖）。
# 内置「观影模式」只动灯和播放器（可逆、无安防风险）；窗帘数量大且开合度难复原，列入配置待主人定义。
DEFAULT_SCENES = {
    "观影模式": {"desc": "关掉家里全部灯并暂停播放，准备看电影"},
}

# 类别词 → domain（泛称句预筛：「窗帘」→ 全部 cover 入候选池，配合 scope 展开）
_DOMAIN_WORDS = [
    ("窗帘", "cover"), ("窗", "cover"),
    ("射灯", "light"), ("灯", "light"),
    ("音箱", "media_player"), ("播放器", "media_player"),
    ("空调", "climate"), ("风扇", "fan"), ("开关", "switch"),
]

# 直通允许的设备域（正向白名单）：门锁/报警等永不直通（拍板第 2 条）
CONTROL_DOMAINS = {"light", "cover", "media_player", "climate", "fan", "switch"}
QUERY_DOMAINS = CONTROL_DOMAINS | {"sensor"}
MEDIA_ACTIONS = {"pause", "play", "volume_up", "volume_down"}
EXECUTABLE_ACTIONS = {
    "turn_on", "turn_off", "open_cover", "close_cover",
    "brightness_up", "brightness_down",
} | MEDIA_ACTIONS

_THRESHOLD_INTENT = 0.5    # intent 概率低于此值降级
_THRESHOLD_DEVICE = 0.4    # device 概率低于此值降级
_DISPLAY_LIMIT = 15        # 展示给模型的候选上限（选择集；展开不受限）
# [2026-10-11 B7] 3.0 → 1.5。实测总计 0.8~1.2s，1.5s 留了余量；超时就静默降级走
# 大模型链路（设备仍会被 LLM 工具调用控制），只是慢一点，不会出错。
# 原来 3s 意味着「本该 1 秒答完的事，最多先空等 3 秒」。
_PIPELESS_TIMEOUT_S = 1.5  # 同步外壳总超时（判定 ≤1.5s + 执行约 1s，实测总计 0.8~1.2s）

_BRIGHTNESS_RE = re.compile(
    r"(\d{1,3})\s*%|百分之\s*(\d{1,3})|百分之\s*[零〇一二两三四五六七八九十百]+"
    r"|亮度\D{0,4}(\d{1,3})"
)


@dataclass
class DeviceGroup:
    device: str            # 代表设备 eid（none/空=该组无效）
    action: str
    scope: str             # single / all_domain / all_room / none


@dataclass
class HomeDecision:
    intent: str
    groups: List[DeviceGroup] = field(default_factory=list)
    scene: Optional[str] = None
    brightness: Optional[int] = None   # 0-100 绝对亮度（句中带数字时）
    p_intent: Optional[float] = None
    p_device: Optional[float] = None
    n_pool: int = 0
    latency_ms: int = 0
    pool: List[str] = field(default_factory=list)  # 判定时的候选池（scope 展开依据）


# ==================== 配置 ====================

def _read_env_file(name: str) -> str:
    """独立读 .env（server 根目录）。不依赖 config_loader 的 load_dotenv 副作用，
    保证本模块无论以何种顺序被 import 都能拿到 key。"""
    try:
        from pathlib import Path
        env_path = Path(__file__).resolve().parents[2] / ".env"
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _load_endpoint(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """解析直通判定端点：Decision.jev.home_intent（可选覆盖）→ Decision.jev.shadow。
    返回 None 表示未配置/未启用（旁路整体关闭，零开销降级）。"""
    cfg = config or {}
    jev = (cfg.get("Decision") or {}).get("jev") or {}
    hi = jev.get("home_intent") or {}
    shadow = jev.get("shadow") or {}

    base_url = str(hi.get("base_url") or shadow.get("base_url") or "").rstrip("/")
    model = str(hi.get("model") or shadow.get("model") or "")
    if not base_url or not model:
        return None

    key = str(hi.get("api_key") or shadow.get("api_key") or "").strip()
    if key.startswith("${"):  # ${VAR} 占位 → 先环境变量，再独立读 .env
        var = key[2:-1] if key.endswith("}") else ""
        key = (os.getenv(var, "").strip() or _read_env_file(var)) if var else ""
    if not key:  # 未给 key / 占位解析失败 → 直接找默认 key
        key = (os.getenv("TYPESAFE_SHADOW_API_KEY", "").strip()
               or _read_env_file("TYPESAFE_SHADOW_API_KEY"))
    if not key:
        return None

    timeout_s = max(0.3, float(hi.get("timeout_ms") or shadow.get("timeout_ms") or 1500) / 1000)
    timeout_s = min(timeout_s, 1.5)  # 直通判定必须快，超过 1.5s 就不值得短路
    return {
        "base_url": base_url,
        "model": model,
        "api_key": key,
        "timeout_s": timeout_s,
        "thr_intent": float(hi.get("threshold_intent", _THRESHOLD_INTENT)),
        "thr_device": float(hi.get("threshold_device", _THRESHOLD_DEVICE)),
    }


def _load_scenes(config: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """场景词表：Decision.jev.home_intent.scenes 覆盖内置默认。"""
    try:
        hi = ((config or {}).get("Decision") or {}).get("jev") or {}
        hi = hi.get("home_intent") or {}
        scenes = hi.get("scenes")
        if isinstance(scenes, dict) and scenes:
            return scenes
    except Exception:
        pass
    return dict(DEFAULT_SCENES)


# ==================== 代码层预筛 ====================

def prefilter(text: str, mapping: Dict[str, Dict[str, Any]]) -> List[str]:
    """候选池（全匹配，不截断）：设备名子串/分词匹配 + 类别词→domain 扩展。
    返回与 mapping 插入序一致的 eid 列表；0 候选=闲聊/无关句天然降级。"""
    u = (text or "").lower()
    hits: List[str] = []
    seen = set()

    def _add(eid: str):
        if eid not in seen:
            seen.add(eid)
            hits.append(eid)

    # 1) 名称精确/分词匹配（优先，排前面）
    for eid, info in mapping.items():
        name = str(info.get("name") or eid)
        parts = {name, name.split("_")[0], re.sub(r"_?\d+$", "", name)}
        parts |= {t for t in re.split(r"[\s_]+", name) if t and not t.isdigit()}
        if any(p and len(p) >= 2 and p.lower() in u for p in parts):
            _add(eid)

    # 2) 类别词扩展（泛称句「所有窗帘」→ 全部 cover 入池，配合 scope 展开）
    for word, dom in _DOMAIN_WORDS:
        if word in u:
            for eid in mapping:
                if eid.split(".")[0] == dom:
                    _add(eid)
    return hits


def _display_cands(text: str, pool: List[str], mapping: Dict[str, Dict[str, Any]],
                   limit: int = _DISPLAY_LIMIT) -> List[str]:
    """从池中选展示给模型的代表集：句中直接点名的排前，然后按 eid 稳定截断。"""
    u = (text or "").lower()

    def _score(eid: str) -> int:
        info = mapping.get(eid) or {}
        name = str(info.get("name") or eid)
        if name.lower() in u:
            return 2
        base = name.split("_")[0]
        if base and base.lower() in u:
            return 1
        return 0

    ranked = sorted(pool, key=lambda e: (-_score(e), e))
    return ranked[:limit]


def _build_questions(text: str, display: List[str], mapping: Dict[str, Dict[str, Any]],
                     scenes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """fan-out 问题集：intent + scene + 两组(device/action/scope) +（句含数字时）brightness。
    组1 键沿用 P1 的 device/action（答案兼容）；scope 缺省视为 single（P1 行为不变）。"""
    questions: Dict[str, Any] = {
        "intent": {"type": "choice", "instructions": "用户这句话想干什么？",
                   "criteria": INTENT_CRITERIA},
        "action": {"type": "choice", "instructions": "第一个要执行的动作是什么？",
                   "criteria": ACTION_CRITERIA},
        "scope": {"type": "choice",
                  "instructions": "操作范围？只动选中设备选 single；「所有/都/全部+类别」选 all_domain；「某房间的」选 all_room",
                  "criteria": SCOPE_CRITERIA},
    }
    if scenes:
        scene_criteria = {name: str(sc.get("desc") or name) for name, sc in scenes.items()}
        scene_criteria["none"] = "这句话没有要求进入任何场景"
        questions["scene"] = {
            "type": "choice",
            "instructions": "用户是否明确要求进入某个场景模式？仅在明确说出/明确要求时选择，普通句子选 none",
            "criteria": scene_criteria,
        }
    if display:
        crit = {eid: f"{mapping[eid].get('name') or eid}（{mapping[eid].get('room') or '未知'}）"
                for eid in display}
        crit["none"] = "句子未指向候选中的任何设备"
        dev_q = {"type": "choice",
                 "instructions": "要操作/查询哪个设备？从候选中选最匹配的一个；"
                                 "『灯都关了』这类集合句也必须选一盏代表设备（范围由 scope 决定）；"
                                 "完全不针对候选设备才选 none",
                 "criteria": crit}
        questions["device"] = dict(dev_q)
        # 第二组（多设备句；单设备句自然答 none）
        questions["device2"] = {"type": "choice",
                                "instructions": "句子还提到第二个设备吗？有则选它，没有选 none",
                                "criteria": dict(crit)}
        questions["action2"] = {"type": "choice",
                                "instructions": "第二个设备要执行什么动作？没有第二个设备选 none",
                                "criteria": ACTION_CRITERIA}
        questions["scope2"] = {"type": "choice",
                               "instructions": "第二个设备的操作范围？",
                               "criteria": SCOPE_CRITERIA}
    # 数值亮度：句中带数字/百分比才问（多数句子零开销）
    if _BRIGHTNESS_RE.search(text or ""):
        b = {str(v): f"百分之{v}" for v in range(0, 101, 10)}
        b["none"] = "不涉及具体亮度数值"
        questions["brightness"] = {"type": "choice",
                                   "instructions": "句子要求的目标亮度百分比？没有具体数值选 none",
                                   "criteria": b}
    return questions


def _parse_choice(answer: Any):
    """choice 答案 → (value, probability)。兼容多种返回结构（评测同款）。"""
    if answer is None:
        return None, None
    if isinstance(answer, str):
        return answer, None
    if isinstance(answer, dict):
        for k in ("value", "label", "answer", "selected", "choice", "key"):
            if k in answer and isinstance(answer[k], str):
                prob = None
                for pk in ("probability", "confidence", "score"):
                    if isinstance(answer.get(pk), (int, float)):
                        prob = float(answer[pk])
                        break
                if prob is None and isinstance(answer.get("distribution"), dict) \
                        and answer[k] in answer["distribution"]:
                    prob = float(answer["distribution"][answer[k]])
                return answer[k], prob
        dist = answer.get("distribution")
        if isinstance(dist, dict) and dist:
            k = max(dist, key=lambda x: dist[x])
            return k, float(dist[k])
    return None, None


def _dev_row(eid: str, info: Dict[str, Any]) -> Dict[str, Any]:
    return {"eid": eid, "name": info.get("name") or eid,
            "room": info.get("room") or "", "dom": eid.split(".")[0]}


# ==================== 判定 ====================

def _valid_group(device: Optional[str], action: Optional[str], scope: Optional[str],
                 pool: set, intent: str) -> Optional[DeviceGroup]:
    """单组校验；返回 None=该组无效（不构成直通理由）。"""
    if not device or device == "none" or device not in pool:
        return None
    scope = scope if scope in SCOPE_CRITERIA else "single"
    action = action or ""
    domain = device.split(".")[0]
    if intent == "control":
        if domain not in CONTROL_DOMAINS:
            return None  # 门锁/报警等 → 白名单外（拍板第 2 条）
        if action not in EXECUTABLE_ACTIONS:
            return None  # toggle/none/未知 → 交回大模型
    else:  # query
        if domain not in QUERY_DOMAINS:
            return None
        if action not in ("query_state", "", "none"):
            return None
        if scope not in ("single", "all_domain"):
            return None  # 房间聚合查询无现成实现 → 降级
    return DeviceGroup(device=device, action=action or "query_state", scope=scope)


def scene_hit(text: str, scenes: Optional[Dict[str, Any]]) -> bool:
    """句中是否出现场景名（场景句常无设备词，预筛须放行）。"""
    if not scenes or not text:
        return False
    return any(name and name in text for name in scenes)


def _domain_from_text(text: str, pool: List[str]) -> Optional[str]:
    """句中类别词 → 池内唯一命中 domain（「灯」→light）；多域命中不猜。"""
    pool_doms = {e.split(".")[0] for e in pool}
    hit = {dom for word, dom in _DOMAIN_WORDS if word in text and dom in pool_doms}
    return hit.pop() if len(hit) == 1 else None


_CONTROL_LIKE = {"turn_on", "turn_off", "open_cover", "close_cover",
                 "brightness_up", "brightness_down"} | MEDIA_ACTIONS


# ==================== 家居判定影子（本地只记录不干预） ====================

_DIGEST_KEYS = ("intent", "scene", "device", "device2", "action", "action2",
                "scope", "scope2", "brightness")


def _answer_digest(answers: Dict[str, Any]) -> Dict[str, Any]:
    """answers → 精简摘要（报告对比用）。"""
    out: Dict[str, Any] = {}
    for key in _DIGEST_KEYS:
        if answers.get(key) is not None:
            value, _ = _parse_choice(answers.get(key))
            out[key] = value
    return out


def _count_options(questions: Dict[str, Any]) -> int:
    return sum(len(q.get("criteria") or {}) for q in questions.values()
               if isinstance(q, dict))


async def _local_shadow_ask(
    base_url: str, model: str, api_key: Optional[str],
    state: Dict[str, Any], questions: Dict[str, Any],
) -> tuple:
    """本地判定影子请求 → (answers | None, error | None, latency_ms)。

    超时 10s：影子是异步只记录（不阻塞主链路），本地模型在大选项集上
    可能需要数秒甚至超时——完整延迟数据正是影子报告要的。
    """
    started = time.perf_counter()
    answers: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/v1/systemone",
                headers={"Authorization": f"Bearer {api_key or 'ollaya-local'}"},
                json={"state": state, "model": model, "questions": questions},
            )
            if resp.status_code != 200:
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                raw = (resp.json() or {}).get("answers")
                answers = raw if isinstance(raw, dict) else None
                if answers is None:
                    error = "no answers"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return answers, error, int((time.perf_counter() - started) * 1000)


def _spawn_local_shadow(config: Dict[str, Any], utterance: str,
                        state: Dict[str, Any], questions: Dict[str, Any],
                        primary_answers: Dict[str, Any], primary_latency_ms: int,
                        endpoint: Dict[str, Any]) -> None:
    """云端主判后 fire-and-forget：同一 payload 发本地只记录。静默失败。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _worker() -> None:
        try:
            jev_cfg = ((config or {}).get("Decision") or {}).get("jev") or {}
            shadow_cfg = jev_cfg.get("shadow") or {}
            # 复用影子总开关与日志路径（判停影子同款配置）
            if not (jev_cfg.get("enabled") and jev_cfg.get("base_url")):
                return
            if not shadow_cfg.get("enabled"):
                return
            log_path = str(shadow_cfg.get("log_path") or "data/decision_shadow_log.jsonl")
            local_base = str(jev_cfg["base_url"])
            local_model = str(jev_cfg.get("model") or "")

            answers, error, latency_ms = await _local_shadow_ask(
                local_base, local_model, jev_cfg.get("api_key"), state, questions
            )

            record = {
                "ts": time.time(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "scene": "home_intent",
                "utterance": utterance,
                "n_options": _count_options(questions),
                "primary": {
                    "provider": "typesafe-cloud",
                    "base_url": endpoint["base_url"],
                    "model": endpoint["model"],
                    "latency_ms": primary_latency_ms,
                    "ok": True,
                    "digest": _answer_digest(primary_answers),
                },
                "shadow": {
                    "provider": "ollaya-local",
                    "base_url": local_base,
                    "model": local_model,
                    "latency_ms": latency_ms,
                    "ok": answers is not None and error is None,
                    "error": error,
                    "digest": _answer_digest(answers or {}),
                },
            }
            try:
                from pathlib import Path
                path = Path(log_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception:
            pass

    task = loop.create_task(_worker())


async def judge_home_intent(
    text: str,
    mapping: Dict[str, Dict[str, Any]],
    endpoint: Dict[str, Any],
    scenes: Optional[Dict[str, Any]] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[HomeDecision]:
    """云端 JEV fan-out 判定。返回 HomeDecision（是否直通还要过执行层白名单），
    网络/格式/阈值不满足一律返回 None。transport 供单测注入 MockTransport。"""
    pool = prefilter(text, mapping)
    if not pool and not scene_hit(text, scenes or {}):
        return None  # 预筛无候选且无场景词：闲聊/无关句天然降级，且省一次云端调用
    display = _display_cands(text, pool, mapping)
    questions = _build_questions(text, display, mapping, scenes or {})

    state = {
        "utterance": text,
        "assistant_name": "Eva / 伊娃",
        "candidate_devices": [_dev_row(eid, mapping[eid]) for eid in display],
        "devices": [_dev_row(eid, mapping[eid]) for eid in mapping] if len(display) <= 8 else None,
    }
    payload = {"state": state, "model": endpoint["model"], "questions": questions}

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(endpoint["timeout_s"])
        ) as client:
            resp = await client.post(
                f"{endpoint['base_url']}/v1/systemone",
                headers={"Authorization": f"Bearer {endpoint['api_key']}"},
                json=payload,
            )
    except Exception:
        return None  # 断网/超时静默降级
    latency_ms = int((time.perf_counter() - t0) * 1000)
    if resp.status_code != 200:
        return None

    try:
        answers = resp.json().get("answers") or {}
    except Exception:
        return None

    # 家居判定影子（2026-10-02 主人指示）：云端主判照常，本地只记录不干预
    # （含本地 422 option budget 超限证据，供影子报告对比）
    if config is not None and answers:
        _spawn_local_shadow(config, text, state, questions, answers, latency_ms, endpoint)

    intent, p_i = _parse_choice(answers.get("intent"))
    scene, _ = _parse_choice(answers.get("scene"))
    device1, p_d1 = _parse_choice(answers.get("device"))
    action1, _ = _parse_choice(answers.get("action"))
    scope1, _ = _parse_choice(answers.get("scope"))
    device2, p_d2 = _parse_choice(answers.get("device2"))
    action2, _ = _parse_choice(answers.get("action2"))
    scope2, _ = _parse_choice(answers.get("scope2"))
    bright_raw, _ = _parse_choice(answers.get("brightness"))

    # ---- 场景命中：独立直通（不要求 intent=device 校验链） ----
    if scenes and scene and scene in scenes:
            return HomeDecision(intent="control", scene=scene, p_intent=p_i,
                            n_pool=len(pool), latency_ms=latency_ms, pool=pool)

    # ---- 常规校验（任何一条不满足 → 降级） ----
    # intent 兜底放最前：模型犹豫时以明确动作为准（「X都关了」曾被低概率答成 query/chitchat）
    if action1 in _CONTROL_LIKE:
        intent = "control"
    elif action1 == "query_state" and intent == "control":
        intent = "query"
    if intent not in ("control", "query"):
        return None
    if p_i is not None and p_i < endpoint["thr_intent"] and intent != "control":
        return None
    pool_set = set(pool)
    g1 = _valid_group(device1, action1, scope1, pool_set, intent)
    # device=none 但 scope=all_domain：类别词推代表设备（「灯都关了」「几盏灯开着」）
    if g1 is None and scope1 == "all_domain" and device1 in (None, "", "none"):
        dom = _domain_from_text(text, pool)
        if dom:
            rep = next((e for e in pool if e.split(".")[0] == dom), None)
            if rep:
                g1 = _valid_group(rep, action1, scope1, pool_set, intent)
    g2 = _valid_group(device2, action2, scope2, pool_set, intent) if device2 else None
    groups = [g for g in (g1, g2) if g]
    if not groups:
        return None  # 无有效设备组（none / 幻觉 / 白名单外 / 动作不可执行）
    p_d = p_d1 if (p_d1 is not None) else p_d2
    if p_d is not None and p_d < endpoint["thr_device"] and g1 is not None \
            and device1 not in (None, "", "none"):
        return None

    brightness: Optional[int] = None
    if bright_raw not in (None, "", "none"):
        try:
            v = int(str(bright_raw).rstrip("%"))
            if 0 <= v <= 100:
                brightness = v
        except ValueError:
            pass

    return HomeDecision(
        intent=intent, groups=groups, brightness=brightness,
        p_intent=p_i, p_device=p_d, n_pool=len(pool), latency_ms=latency_ms,
        pool=pool,
    )


# ==================== 执行 ====================

def _control_spec(eid: str, action: str, brightness: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """control 动作 → apply_entities 的 entities 单条 spec；None=该组合不支持直通。"""
    domain = eid.split(".")[0]
    # 绝对亮度优先：「调到60%」即使模型答 brightness_up 也按目标值执行
    if brightness is not None and domain == "light" \
            and action in ("turn_on", "brightness_up", "brightness_down"):
        return {eid: {"state": "on", "brightness_pct": brightness}}
    if action == "turn_on" and domain in ("light", "switch", "media_player", "fan", "climate"):
        return {eid: {"state": "on"}}
    if action == "turn_off" and domain in ("light", "switch", "media_player", "fan", "climate"):
        return {eid: {"state": "off"}}
    if action == "open_cover" and domain == "cover":
        return {eid: {"state": "open"}}
    if action == "close_cover" and domain == "cover":
        return {eid: {"state": "close"}}
    if action == "brightness_up" and domain == "light":
        return {eid: {"state": "on", "brightness_step_pct": 30}}
    if action == "brightness_down" and domain == "light":
        return {eid: {"state": "on", "brightness_step_pct": -30}}
    return None


def _expand_group(group: DeviceGroup, pool: List[str],
                  mapping: Dict[str, Dict[str, Any]]) -> List[str]:
    """按 scope 把代表设备展开成实体列表（白名单内）。"""
    dev = group.device
    domain = dev.split(".")[0]
    if group.scope == "all_domain":
        cands = [e for e in pool
                 if e.split(".")[0] == domain and e.split(".")[0] in CONTROL_DOMAINS | QUERY_DOMAINS]
    elif group.scope == "all_room":
        room = (mapping.get(dev) or {}).get("room") or ""
        cands = [e for e in pool
                 if ((mapping.get(e) or {}).get("room") or "") == room
                 and e.split(".")[0] in CONTROL_DOMAINS | QUERY_DOMAINS]
    else:
        cands = [dev] if domain in CONTROL_DOMAINS | QUERY_DOMAINS else []
    if dev in pool and dev not in cands and domain in CONTROL_DOMAINS | QUERY_DOMAINS:
        cands.append(dev)
    return cands


# ── media_player 选设备（2026-10-12 主人实测） ──────────────────────
# 现象：说「播放第一条」「继续播放」一律回「继续播放音箱」，而且选中的
#      `media_player.web_chrome_on_windows` 的 HA 状态是 **unavailable**（根本不可用）。
#      主人原话：「我说继续播放，它多次出现了继续播放音箱的说法，这个不对」。
#
# 成因：`_DOMAIN_WORDS` 里「音箱」→ media_player，候选池里 4 个 media_player，
#      JEV 挑中第一个（或可用性未参与筛选），于是「音箱」= 那个不可用的 Chrome。
#
# 主人补充「以后会接真音箱，现在先预留」→ 所以不能写死某个 eid，
# 正确做法是**按可用性排序**：可用优先于不可用；真音箱（Mac mini / 有名字的）
# 优先于匿名的网页媒体器。
_MEDIA_OFFLINE = ("unavailable", "unknown", "unreachable")


async def _friendly_name(eid: str) -> str:
    """取 HA 的 friendly_name；取不到返回空串（调用方决定用不用泛称）。

    为什么不用现成的 `_friendly()`：它不同步请求 HA，拿不到 friendly_name 时
    **原样返回 entity_id**（`media_player.ban_gong_shi`）。于是 `_media_action`
    里 `name == eid` 恒成立 → 永远走「泛称=音箱」分支 → 主人听到「继续播放音箱」，
    而实际上有 friendly_name 的设备叫「办公室」。这是主人实测报的问题。
    """
    import aiohttp
    from plugins_func.functions.ha_control import HASS_BASE_URL, HEADERS, _HTTP_TIMEOUT

    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.get(f"{HASS_BASE_URL}/api/states/{eid}",
                                   headers=HEADERS, timeout=_HTTP_TIMEOUT) as r:
                st = await r.json()
        if isinstance(st, dict):
            return str((st.get("attributes") or {}).get("friendly_name") or "").strip()
    except Exception:
        pass
    return ""


# ┌─────────────────────────────────────────────────────────────────────┐
# │ 电脑本体不是音箱（2026-10-12 主人裁定）                                │
# ├─────────────────────────────────────────────────────────────────────┤
# │ 主人原话：「Mac Mini 从开发机里面排除，这不是个音箱，不用再问了。」    │
# │ 以及：「Mac Mini 本身就是一个 home brain 系统的主体，它跟我们的        │
# │ MacBook Pro 不应该发生关系，对吧？它只是开发机跟生产机的关系。」       │
# │                                                                     │
# │ HA 里 macOS Companion App 会把电脑自己暴露成一个 media_player         │
# │ （实测清单：media_player.bot_evas_mac_mini = Bot-Eva's Mac mini、     │
# │  media_player.macbook_pro = MacBook Pro），它们能被「播放」是因为      │
# │ 系统音量可控，但**不是音箱**。真音箱只有 media_player.ban_gong_shi     │
# │ （办公室）。选错就会出现「让 Mac Mini 播音乐」这种跨机调用 ——          │
# │ 生产机是独立系统主体，不该被开发机当输出设备使唤。                     │
# └─────────────────────────────────────────────────────────────────────┘
_COMPUTER_PLAYER_RE = re.compile(
    r"(mac[_\-]?mini|mac[_\-]?book|mac[_\-]?pro|mac[_\-]?studio"
    r"|mac[_\-]?air|imac|mac[_\-]?os|desktop|laptop)",
    re.IGNORECASE,
)


def _is_computer_player(eid: str) -> bool:
    """这台 media_player 是不是「电脑本体」而不是真音箱。

    只看 entity_id 就够了（不需要额外查 HA）：Companion App 生成的
    entity_id 一定带机型特征，而真音箱是主人自己命名的（如 ban_gong_shi）。
    """
    tail = str(eid or "").split(".", 1)[-1]
    return bool(_COMPUTER_PLAYER_RE.search(tail))


async def _rank_media_players(eids):
    """给候选 media_player 排序：**先剔掉电脑本体**，再按「能不能真用」排。

    规则：
      0. 电脑本体（Mac mini / MacBook 等）→ 直接剔除，不是音箱
      1. 状态 unavailable/unknown → 排最后（主人实测踩的就是这个）
      2. 有 friendly_name 的 → 优先（那是主人自己命名的设备，如「办公室」）
      3. 名字里带 web/chrome/browser 这类网页媒体器特征 → 再往后

    返回剔除+排序后的列表；**可能为空**（全被剔除）。调用方绝不能写
    `... or expanded` 兜底 —— 空列表 falsy，会回退成原列表，
    等于又把电脑本体当音箱选中。
    """
    eids = [e for e in eids if not _is_computer_player(e)]
    if len(eids) <= 1:
        return list(eids)
    import aiohttp
    from plugins_func.functions.ha_control import HASS_BASE_URL, HEADERS, _HTTP_TIMEOUT

    scored = []
    async with aiohttp.ClientSession(trust_env=False) as session:
        for eid in eids:
            score = 0
            try:
                async with session.get(f"{HASS_BASE_URL}/api/states/{eid}",
                                       headers=HEADERS, timeout=_HTTP_TIMEOUT) as r:
                    st = await r.json()
                if str(st.get("state", "")).lower() in _MEDIA_OFFLINE:
                    score += 100          # 不可用：最大惩罚
                fname = str((st.get("attributes") or {}).get("friendly_name") or "")
                if fname:
                    score -= 10          # 主人命名的设备优先
                if any(k in eid.lower() for k in ("web", "chrome", "browser", "cast")):
                    score += 20          # 网页媒体器特征
            except Exception:
                score += 50              # 查不到就当不可靠
            scored.append((score, eid))
    # 稳定排序：分数小的优先，同分保持原顺序
    return [eid for _s, eid in sorted(scored, key=lambda x: x[0])]


async def _media_action(eid: str, action: str) -> Optional[str]:
    """media_player 播放/暂停/音量：改走 core/home 引擎（2026-10-13）。

    原来这里直连 HA 服务，POST 成功就播报「继续播放音箱」——但设备 unavailable 时
    HA 一样返回 200，主人实测就被骗过（web_chrome_on_windows 离线也回「继续播放」）。
    引擎会先查可用性（离线/未接入如实说），发完指令再回读状态确认，播报不含 entity_id。
    """
    from core.home import engine as home_engine
    from plugins_func.functions.ha_control import (
        _check_entity, _ensure_allowed_entities, _short,
    )

    await _ensure_allowed_entities()
    if _check_entity(eid):
        return None
    try:
        res = await home_engine.run(None, commands=[{"action": action, "entity_id": eid}])
    except Exception as exc:  # 引擎异常 → 降级原链路，不猜
        print(f"[家居直通] media 动作异常 {eid}/{action}: {exc!r}")
        return None
    if res is None or not res.spoken:
        return None
    return _short(res.spoken, 200)


async def _execute_plan(entities: Dict[str, Any], medias: List[tuple],
                        prefix: str = "") -> Optional[str]:
    """统一执行：普通实体走 apply_entities 并发，media 动作逐个直连；合并一句播报。"""
    parts: List[str] = []
    result_ok = False
    if entities:
        from plugins_func.functions.ha_control import _summarize_entities, apply_entities
        result = await apply_entities(entities)
        if not isinstance(result, dict) or result.get("error"):
            return None  # 白名单拦截/入参错误 → 降级（交大模型向用户解释）
        parts.append(_summarize_entities(entities, result))
        result_ok = True
    for eid, action in medias:
        spoken = await _media_action(eid, action)
        if spoken:
            parts.append(spoken)
            result_ok = True
    if not result_ok:
        return None
    from plugins_func.functions.ha_control import _short
    return _short((prefix + "，".join(parts)) if prefix else "，".join(parts), 300)


def _scene_plan(scene_name: str, scenes: Dict[str, Dict[str, Any]],
                mapping: Dict[str, Dict[str, Any]]):
    """场景 → (entities, medias)。配置 actions 优先；否则内置联动（观影=全部灯关+播放暂停）。"""
    cfg = scenes.get(scene_name) or {}
    entities: Dict[str, Any] = {}
    medias: List[tuple] = []
    actions = cfg.get("actions") if isinstance(cfg.get("actions"), list) else None
    if actions:
        for a in actions:
            if not isinstance(a, dict):
                continue
            eid = str(a.get("entity") or "")
            if eid not in mapping or eid.split(".")[0] not in CONTROL_DOMAINS:
                continue
            act = a.get("action")
            if act in MEDIA_ACTIONS:
                medias.append((eid, act))
            else:
                entities[eid] = {"state": a.get("state", "off")}
    else:
        # 内置默认联动（仅白名单域；实体从实时映射动态取）
        for eid in mapping:
            dom = eid.split(".")[0]
            if dom == "light":
                entities[eid] = {"state": "off"}
            elif dom == "media_player":
                medias.append((eid, "pause"))
    return entities, medias


async def _execute(decision: HomeDecision, mapping: Dict[str, Dict[str, Any]],
                   scenes: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """按判定执行，返回播报文案；失败/不支持返回 None（降级，不执行半截操作）。"""
    # ---- 场景 ----
    if decision.scene:
        entities, medias = _scene_plan(decision.scene, scenes, mapping)
        if not entities and not medias:
            return None
        return await _execute_plan(entities, medias, prefix=f"{decision.scene}已开启：")

    # ---- 查询 ----
    if decision.intent == "query":
        g = decision.groups[0]
        if g.scope == "all_domain":
            from plugins_func.functions.ha_control import ha_summary, spoken_summary
            summary = await ha_summary([g.device.split(".")[0]])
            if not isinstance(summary, dict) or summary.get("error"):
                return None
            return spoken_summary(summary)
        from plugins_func.functions.ha_control import _spoken_state, ha_get_state
        state_obj = await ha_get_state(g.device)
        if not isinstance(state_obj, dict) or "error" in state_obj or "state" not in state_obj:
            return None
        return _spoken_state(g.device, state_obj)

    # ---- 控制（单/多设备，按 scope 展开；decision.pool=判定时候选池） ----
    entities: Dict[str, Any] = {}
    medias: List[tuple] = []
    for g in decision.groups:
        expanded = _expand_group(g, decision.pool, mapping)
        # [2026-10-12] media_player 候选按可用性排序：主人说「播放第一条」曾选中
        # unavailable 的 web_chrome_on_windows → 回「继续播放音箱」。
        if g.action in MEDIA_ACTIONS:
            # ⚠️ 不能写 `await _rank_media_players(...) or expanded`：
            # 候选全是电脑本体时函数返回空列表（falsy），`or` 会回退成原
            # expanded，等于又把 Mac mini / MacBook 当音箱选中。
            _media = [e for e in expanded if e.split(".")[0] == "media_player"]
            _others = [e for e in expanded if e.split(".")[0] != "media_player"]
            expanded = _others + await _rank_media_players(_media)
        for eid in expanded:
            if eid in entities or any(eid == m[0] for m in medias):
                continue  # 多组重叠时先到先得
            dom = eid.split(".")[0]
            if g.action in MEDIA_ACTIONS:
                if dom == "media_player":
                    medias.append((eid, g.action))
            else:
                spec = _control_spec(eid, g.action, decision.brightness)
                if spec:
                    entities.update(spec)
    if not entities and not medias:
        return None
    return await _execute_plan(entities, medias)


# ==================== 判定日志（P3） ====================

_LOG_MAX_LINES = 2000
_LOG_KEEP_LINES = 1000


def _log_path():
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "data" / "home_intent_log.jsonl"


def _log_record(record: Dict[str, Any]) -> None:
    """追加判定日志 JSONL（含降级原因），失败静默；超限轮转。"""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > _LOG_MAX_LINES:
                path.write_text("\n".join(lines[-_LOG_KEEP_LINES:]) + "\n", encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


def read_log_records(limit: int = 50) -> List[Dict[str, Any]]:
    """读判定日志尾部（新→旧），供后台日志页 API 使用。"""
    try:
        path = _log_path()
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, min(int(limit), 500)):]
        out: List[Dict[str, Any]] = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


async def handle_home_intent_logs(request):
    """GET /api/home-intent/logs?limit=50 — P3 判定日志（后台日志页用）。"""
    from aiohttp import web
    try:
        limit = int(request.query.get("limit", "50"))
    except Exception:
        limit = 50
    return web.json_response({"success": True, "records": read_log_records(limit)})


# ==================== 编排（async 主体 + 同步外壳） ====================

NON_DEVICE_TOPICS = (
    "相册", "照片", "相片", "摄像头", "监控", "新闻", "资讯", "健康", "血压", "血糖", "报告",
    "菜谱", "食谱", "地图", "导航", "邮件", "邮箱", "备忘录", "日历", "日程", "浏览器", "网页",
    "天气", "提醒", "飞书", "微信",
    # [2026-10-12 主人实测] 音乐点播被家居直通劫持。
    # 现象：说「播放王菲的歌」「播放林俊杰的歌」「播放音乐」「播放下一首」，
    #   全部被判成 media_player 音箱（web_chrome_on_windows）→ 回「继续播放音箱」，
    #   **大模型根本没被调用**，所以既搜不到歌也点不了播。
    # 成因：`_DOMAIN_WORDS` 里「音箱」映射到 media_player，而 HA 里恰好有一个
    #   `media_player.web_chrome_on_windows` 实体，JEV 看到「播放」+音箱候选就判它是控制。
    # 但「播放**谁的**歌」是**点播**（要走 mac_music / 音乐搜索工具），不是控制设备。
    # 判据：出现「歌/曲/音乐/下一首/上一首/单曲」或「某人 + 的 + 歌」一律不是设备控制。
    # 只保留纯控制意图：「打开音箱」「音箱声音大一点」「暂停」「继续播放」这类才直通。
)

# [2026-10-12] 点播**本身**就是排除依据，不依赖上面那张字面表 ——
# 「播放林俊杰的rise without sound」「来首周杰伦的」里没有「歌/音乐」字样，
# 但它们显然是点播。所以下面判定用 `音乐点播信号 or 字面命中` 两个来源。

# 音乐**控制**词：出现这些说明确实是在控制音箱/播放器本身（而非点播某首歌）
_MEDIA_CONTROL_WORDS = ("音箱", "播放器", "音响", "音量", "声音", "大声", "小声", "静音")

# 点播意图的补充信号（2026-10-12 主人实测）：
# 「播放林俊杰的rise without sound」「来首周杰伦的」这类句子里**没有「歌/音乐」字样**，
# 但显然是点播。只靠字面词会漏，所以补一层正则。
# 列表播报信号：「第 N 条 / 第 N 个 / 播第 N 条」
_LIST_ORDER_RES = (
    re.compile(r"第\s*[0-9一二三四五六七八九十]+\s*[条个首]"),
    re.compile(r"播放.{0,6}第"),
    re.compile(r"(播|放|念|读)第"),
)

_MUSIC_ORDER_RES = (
    re.compile(r"来[一1]?[首个]"),            # 来首 / 来一个
    re.compile(r"播放.{0,12}的"),          # 播放林俊杰的…
    re.compile(r"[唱听放点播].{0,8}[歌曲]"),  # 唱首歌 / 听首歌
)


async def _pipeline(conn, text: str) -> Optional[str]:
    rec: Dict[str, Any] = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "utter": text, "outcome": "", "intent": "", "scene": "",
        "targets": [], "reply": "", "jms": 0, "tms": 0,
    }
    t_start = time.perf_counter()
    try:
        config = getattr(conn, "config", None) or {}
        endpoint = _load_endpoint(config)
        if not endpoint:
            rec["outcome"] = "disabled"
            return None  # 云端未配置/key 缺失 → 旁路整体关闭

        from core.api.smart_home_api import get_device_mapping
        mapping = await get_device_mapping()
        if not mapping:
            rec["outcome"] = "no_mapping"
            return None

        # 其他领域的展示/查询指令不是控制设备（2026-10-12 主人实测）：
        # 「打开家庭相册」被「打开…家…」猜成设备控制，直通去开了 media_player.web_chrome_on_windows，
        # 还把实体 id 念了出来。这类话必须交给大模型走相册/摄像头/新闻等工具。
        #
        # [2026-10-12] 音乐特殊处理：点播（「播放王菲的歌」）不是设备控制，
        # 但「打开音箱」「把音量调大」是。所以音乐词只在**没有媒体控制词**时排除 ——
        # 否则「播放音乐，音量大一点」这种整句会被误放给大模型。
        _music_word = (
            any(k in text for k in ("歌", "歌曲", "音乐", "单曲", "下一首", "上一首", "点歌"))
            or any(rx.search(text) for rx in _MUSIC_ORDER_RES)
        )
        _media_ctl = any(k in text for k in _MEDIA_CONTROL_WORDS)
        # [2026-10-12 主人实测] 「播放第一条」「播放第一条新闻」被判成音箱播放。
        # 这是**播报列表的第 N 条**（新闻/简报/歌单），该走大模型的展示类工具，
        # 不是控制音箱 —— 与点播同理。
        if any(rx.search(text) for rx in _LIST_ORDER_RES):
            rec["outcome"] = "list_order"
            return None
        # 点播一律交给大模型（除非同句还有媒体控制词，那就是控制+播放的复合指令）
        if _music_word and not _media_ctl:
            rec["outcome"] = "music_order"
            return None
        if any(k in text for k in NON_DEVICE_TOPICS):
            rec["outcome"] = "other_domain"
            return None

        scenes = _load_scenes(config)
        if not prefilter(text, mapping) and not scene_hit(text, scenes):
            rec["outcome"] = "no_candidate"  # 闲聊/无关句（未打云端）
            return None

        decision = await judge_home_intent(text, mapping, endpoint, scenes=scenes, config=config)
        if not decision:
            rec["outcome"] = "judge_rejected"  # 判定未通过/网络失败/阈值不足
            rec["jms"] = int((time.perf_counter() - t_start) * 1000)
            return None

        rec["intent"] = decision.intent
        rec["scene"] = decision.scene or ""
        rec["jms"] = decision.latency_ms
        reply = await _execute(decision, mapping, scenes)
        rec["tms"] = int((time.perf_counter() - t_start) * 1000)
        if decision.scene:
            rec["targets"] = [f"scene:{decision.scene}"]
        else:
            rec["targets"] = [f"{g.scope}:{g.action}:{g.device}" for g in decision.groups]
        if not reply:
            rec["outcome"] = "execute_degrade"
            conn.logger.bind(tag=TAG).info(
                f"判定通过但执行层降级: intent={decision.intent} scene={decision.scene} "
                f"groups={[(g.device, g.action, g.scope) for g in decision.groups]}"
            )
            return None
        rec["outcome"] = "executed"
        rec["reply"] = reply
        conn.logger.bind(tag=TAG).info(
            f"直通命中: [{decision.intent}/{decision.scene or ''}] "
            f"{[(g.device, g.action, g.scope) for g in decision.groups]} | "
            f"判定 {decision.latency_ms}ms 总计 {rec['tms']}ms | {reply}"
        )
        return reply
    except Exception as e:
        # 旁路任何异常都不得影响原链路
        rec["outcome"] = "error"
        try:
            conn.logger.bind(tag=TAG).warning(f"直通异常，降级原链路: {e!r}")
        except Exception:
            pass
        return None
    finally:
        rec["tms"] = rec["tms"] or int((time.perf_counter() - t_start) * 1000)
        _log_record(rec)


async def _confirm_first_then_pipeline(conn, text: str) -> Optional[str]:
    """待确认优先 → 再走判定管线（2026-10-13 门锁/车库门二次确认）。

    开锁这类动作引擎不会直接执行，而是先记一条待确认并回问「要开锁吗」。
    用户下一句「确认」「开吧」必须在这里被吃掉：
      - 交给判定管线：JEV 大概率判不出设备意图 → 降级到大模型 → 可能重复控制或答非所问；
      - 「取消」「算了」也要在这里明确回「好的，不动它」，不能留给大模型瞎猜。
    没有待确认时原样走 _pipeline，行为与改动前一致。
    """
    try:
        from core.home import engine as home_engine
        if home_engine.has_pending(conn):
            res = await home_engine.confirm_or_cancel(conn, text)
            if res is not None and res.spoken:
                _log_record({
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "utter": text, "outcome": "confirm_pending", "intent": "control",
                    "scene": "", "targets": list(res.entity_ids or []),
                    "reply": res.spoken, "jms": 0,
                    "tms": 0,
                })
                try:
                    conn.logger.bind(tag=TAG).info(
                        f"[家居直通] 待确认处理: '{text}' -> {res.spoken} "
                        f"| targets={list(res.entity_ids or [])}"
                    )
                except Exception:
                    pass
                return res.spoken
    except Exception as e:
        try:
            conn.logger.bind(tag=TAG).warning(f"[家居直通] 待确认处理异常，继续判定: {e!r}")
        except Exception:
            pass
    return await _pipeline(conn, text)


def run_home_direct(conn, text: str) -> Optional[str]:
    """chat() 同步入口：把判定+执行投到主事件循环并限时等待。
    返回播报文案 = 直通命中（调用方短路 return）；None = 走原有大模型链路。"""
    coro = _confirm_first_then_pipeline(conn, text)
    future = None
    try:
        future = asyncio.run_coroutine_threadsafe(coro, conn.loop)
        # 2026-10-11：此处原写成未定义的 _PIPELINE_TIMEOUT_S（NameError 被下面的 except 吞掉），
        # 导致直通结果永远被丢弃——而判定+执行协程其实已投递并照常执行，
        # 随后大模型链路又执行一遍（设备被重复控制）。
        return future.result(timeout=_PIPELESS_TIMEOUT_S)
    except Exception:
        if future is not None:
            future.cancel()  # 超时：取消在途判定/执行，避免与随后的大模型链路重复控制设备
        else:
            try:
                coro.close()  # 未调度成功时关闭协程，避免 "never awaited" 警告
            except Exception:
                pass
        return None  # 超时/无事件循环/任何异常 → 静默降级
