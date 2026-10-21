"""地理坐标工具（2026-10-04）

背景：以前 HomeBrain 只能拿到城市级的 default_location（天气用），
无法回答「我家附近的饭店哪家最近」。现在房屋状态后台存了精确地址，
这里把地址解析成经纬度存进 houses 表，后续所有距离查询直接带坐标，
不再重复调用地理编码。

坐标系说明：高德 Web 服务 API 用 gcj02（火星坐标）。
本模块所有坐标统一为 gcj02，和高德 place/around、direction/driving 一致，
可以直接互传，不要再做坐标转换。
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode
from urllib.request import urlopen, Request

TAG = "geo"
AMAP_BASE = "https://restapi.amap.com/v3"

# 地理编码失败时的重试次数
_RETRY = 3

# 高德 QPS 限流实测：连续快打第 4 次起会返回
#   status=0, info=CUQPS_HAS_EXCEEDED_THE_LIMIT
# 加 0.5 秒间隔则 100% 正常。所以限流必须重试退避，不能当失败直接返回，
# 否则会随机把合法地址解析失败（2026-10-04 实测踩过）。
_BACKOFF_SECONDS = (0.5, 1.0, 2.0)


def _load_amap_key() -> Optional[str]:
    """读取 data/.amap_config.json 里的高德 Web 服务 Key

    [2026-10-04] 注意：这里读的是 Web 服务 Key（后端调接口用）。
    与之相对，高德 URI API（uri.amap.com，浏览器直接跳转看地图/路线）
    完全不需要 Key，两者不要混淆。
    """
    path = Path(__file__).parent.parent.parent / "data" / ".amap_config.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("key")
    except Exception:
        return None


def _looks_like_cn(lng: float, lat: float) -> bool:
    """粗略判断坐标是否落在中国境内（兜底防线）"""
    return 72.0 <= lng <= 136.0 and 3.0 <= lat <= 54.5


def _region_consistent(address: str, geo: dict) -> bool:
    """校验高德解析结果与输入地址是否属于同一行政区

    [2026-10-04] 这是挡掉「高德尽力猜」的关键。
    高德 geocode 对无法识别的地址不会失败，而是去匹配同名的真实 POI：
        输入「火星基地A区」    → 新疆哈密市伊州区「哈密火星基地」92.045924,42.889637
        输入「阿凡达星球」      → 四川省广安市武胜县「阿凡达」    106.286106,30.339651
        输入「不存在的路999号」→ 湖南省怀化市鹤城区的「大道」    109.951687,27.542758
    这些结果 status 都是 1（成功），坐标也落在境内，靠范围校验抓不住。
    但共同点是：返回的省/市/区县跟输入的完全对不上。

    [2026-10-05] 补上「同名区县」的漏网（实测踩到）：
        输入「北京市朝阳区测试路1号」→ 高德返回 吉林省长春市朝阳区 125.288168,43.833845
      为什么漏网：长春市也有一个「朝阳区」，所以「第 3 条：输入含返回的区县名 → 一致」
      命中了，于是判定通过。可实际上省、市完全对不上，坐标在东北，
      算出来的距离会离谱到没法用。
      补的规则：**如果输入里明确写了省或市，而高德返回的省/市与之矛盾，直接判为无效。**
      具体做法是从输入里正则提取「XX省 / XX自治区 / XX市」，再和高德返回的
      province / city 逐一比对，出现矛盾即拒绝。

    判定规则（逐条全过才算一致）：
      0. 输入里写了「XX省/XX市」时，返回的 province/city 不能与之矛盾（本次新增）
      1. 输入里出现了返回的「省」名 → 一致
      2. 输入里出现了返回的「市」名 → 一致
      3. 输入里出现了返回的「区县」名 → 一致
    第 1~3 条都不命中说明高德匹配到了别处的同名 POI，判为无效。

    注意必须用「输入原文包含」而不是「切词后匹配」：
    早先版本把「深圳市南山区」切成碎片「深圳市南山」导致误杀合法地址。
    """
    # [2026-10-04] 高德返回的空字段有时是空列表 [] 而不是空字符串（实测
    # 「广东省广州市天河区」「杭州市西湖区」会间歇性返回 city=[] / district=[]），
    # 所以必须先做类型归一，只处理非空字符串，否则下面 len()/in 判断会抛异常
    # 被 except 吞掉、导致合法地址随机解析失败。
    fields = {}
    for key in ("province", "city", "district"):
        v = geo.get(key)
        if isinstance(v, str):
            fields[key] = v.strip()
        elif isinstance(v, (list, tuple)):
            # 少数情况是 ['北京市'] 这样的单元素列表
            fields[key] = (v[0].strip() if v and isinstance(v[0], str) else "")
        else:
            fields[key] = ""

    # 三级字段全空则无法判定，交由上层按「无坐标」处理
    if not any(fields.values()):
        return False

    def _bare(v: str) -> str:
        """去掉行政区后缀：「新疆维吾尔自治区」→「新疆」，「北京市」→「北京」"""
        name = re.sub(r"(省|市|区|县|自治区|特别行政区)$", "", v).strip()
        name = re.sub(r"(维吾尔|壮族|回族)$", "", name).strip()
        return name

    # —— 第 0 条：输入里写了省/市，就不许返回矛盾的省/市 ——
    # 从输入里抠出「XX省」「XX自治区」「XX市」。
    # 之所以要抠而不能靠「输入包含返回的省名」，是因为长春市朝阳区那个例子：
    # 区县名对上了，但省名根本没在输入里出现过，旧的规则只会看到「朝阳区」命中就放行。
    #
    # ⚠️ 这里踩过一次坑，正则边界必须卡准，否则合法地址会被误杀：
    #   ① 市名后面常跟「区/县/镇/村/路/街/号」，贪婪匹配会把
    #      「杭州市西湖区文三路」抠成「杭州西湖区文三」→ 误杀。
    #      所以用惰性 + 负向前瞻。
    #   ② 但惰性匹配会从地址**开头**开始，于是「浙江省杭州市西湖区」的市名被抠成
    #      「浙江省杭州」（把省名一起吃了）→ 同样误杀。
    #      所以要先用正则把省名整段抠掉、再从剩余部分里找市名 ——
    #      现实里市名总是紧跟在省名后面，剥掉省名再找，边界就干净了。
    _PROV_RE = r"[一-龥]{2,6}?(?:省|自治区|特别行政区)"
    addr_provinces = re.findall(f"({_PROV_RE})", address)
    rest = re.sub(_PROV_RE, "", address)
    addr_cities = re.findall(r"([一-龥]{2,6}?)市(?![区县镇村路街道号弄巷])", rest)

    got_province = _bare(fields.get("province") or "")
    got_city = _bare(fields.get("city") or "")

    if addr_provinces and got_province:
        # 输入写了省，返回的省必须在其中之一（比对去后缀的裸名，兼容「广东省」/「广东」）
        if not any(_bare(p) == got_province or p == fields.get("province") for p in addr_provinces):
            return False
    if addr_cities and got_city:
        if not any(_bare(ct) == got_city or ct == fields.get("city") for ct in addr_cities):
            return False

    for val in fields.values():
        if not val:
            continue
        # 「新疆维吾尔自治区」→「新疆」，「北京市」→「北京」
        name = _bare(val)
        # 保留带后缀的原名一起比对，兼容用户写「顺义区」而高德返回「顺义区」
        for cand in {val, name}:
            if len(cand) >= 2 and cand in address:
                return True
    return False


def geocode_detail(address: str, city: str = "") -> Optional[dict]:
    """地址 → 坐标 + 结构化行政区划（推荐使用）

    返回 dict:
        {"lng","lat","province","city","district","adcode","formatted_address"}

    [2026-10-10] 为什么需要这个：
    后台「房屋状态」编辑弹窗要回显省/市/区县。以前前端是拿 address 字符串
    猜的（showEditHouseModal 里只匹配出 province，剩下的全当详细地址），
    结果城市和区县永远为空、保存后又丢。这里让后端把结构化字段一起存库，
    编辑时直接读值回显，就不会再丢。

    高德 geocode/geo 的返回里本来就带 province/city/district/adcode，
    实测「北京市顺义区牛山地区」→ province=北京市 district=顺义区 adcode=110113。
    直辖市（如北京市）的 city 为空列表，属正常，代码里用省名兜底。
    """
    r = _geocode_raw(address, city)
    if not r:
        return None
    geo = r["geo"]

    province = _as_str(geo.get("province"))
    city_v = _as_str(geo.get("city"))
    district = _as_str(geo.get("district"))

    # 直辖市（北京/上海/天津/重庆）高德不返回 city，用省名兜底，
    # 否则前端下拉里城市是空的
    if not city_v:
        city_v = province if any(province.startswith(m) for m in _MUNICIPALITIES) else ""

    return {
        "lng": r["lng"],
        "lat": r["lat"],
        "province": province,
        "city": city_v,
        "district": district,
        "adcode": _as_str(geo.get("adcode")),
        "formatted_address": _as_str(geo.get("formatted_address")),
    }


def geocode(address: str, city: str = "") -> Optional[Tuple[float, float]]:
    """地址 → 经纬度（gcj02）

    实测（2026-10-04）：
        北京市顺义区        → 116.661474, 40.149891
        北京市顺义区李遂镇  → 116.756620, 40.091066

    返回 (lng, lat)，失败返回 None（不抛异常，调用方决定是否降级）。
    需要省/市/区县请用 geocode_detail()。
    """
    r = _geocode_raw(address, city)
    return (r["lng"], r["lat"]) if r else None


# 直辖市：高德不返回 city，用省名兜底
_MUNICIPALITIES = ("北京", "上海", "天津", "重庆")


def _as_str(v) -> str:
    """把高德返回的字段统一成字符串

    高德的空字段有时是 ""，有时是 []（实测「广东省广州市天河区」会返回
    city=[]），不做归一会让下游 len()/in 判断抛异常。
    """
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return v[0].strip() if v and isinstance(v[0], str) else ""
    return ""


def _name_consistent(address: str, formatted: str) -> bool:
    """校验高德返回的地址里，是否真的出现了用户输入的那个地点

    [2026-10-05] 这是第三道防线，专治「行政区对、但地点完全不对」。

    实测踩到的情况（主人发现）：
        填「北京市朝阳区金盏八十中学睿志分校」
        → 高德返回 formatted_address = 「北京市朝阳区金盏乡」，坐标落在**金盏乡人民政府**
        → 前两道关卡（范围、行政区）全都放行，因为省市区的确一致
        → 结果导航到乡政府，而不是学校

    另一例更离谱：
        填「北京八十中睿智分校」（没写省、没写市）
        → 高德只返回「北京市」，坐标 116.407387,39.904179 落在**东城区**
        → 离金盏乡 15 公里，但因为输入里没写省市，省市校验根本没生效，
          又靠「输入含返回的 province 名（北京市→北京）」侥幸通过

    判定规则：把输入里的省/市/区/县剥掉，剩下的才是「有区分度的核心词」
    （如上例剥完是「金盏八十中学睿志分校」）。要求返回的地址里出现核心词的
    某一段连续子串（≥3 字）。3 字是折中：2 字太松（「金盏」这种到处都有），
    4 字以上又会把正常的门牌号变化判成不匹配。

    核心词不足 3 字时跳过本检查 —— 短地名（「天安门」「回龙观」）本身就只有 3 字，
    再抠就没了，硬判反而会误杀合法地址。
    """
    if not formatted:
        # 高德连 formatted 都没返回，说明它自己都没定位到具体地点
        return False

    # 剥掉省/市/区/县/乡/镇/街道，只留有区分度的核心部分。
    #
    # ⚠️ 「乡/镇/街道」也必须剥，否则会误放行。实测踩到：
    #   填「北京市朝阳区金盏乡金盏八十中学」
    #   → 高德 formatted = 「北京市朝阳区金盏乡金盏镇」（**仍然只到镇一级，没解析到学校**）
    #   → core 若不剥「金盏乡」，「金盏乡金盏」这 5 字在 formatted 里碰巧命中 → 误判为通过
    #   剥掉后 core = 「金盏八十中学」，formatted 里没有 → 正确拒绝
    core = address
    core = re.sub(r"[一-龥]{2,6}?(?:特别行政区|自治区|省)", "", core)
    core = re.sub(r"[一-龥]{2,6}?市", "", core)
    core = re.sub(r"[一-龥]{2,6}?[区县]", "", core)
    core = re.sub(r"[一-龥]{2,6}?(?:乡|镇|街道|街道办事处)", "", core)
    # 数字、字母、标点都不参与比对（门牌号常有各种写法差异）
    core = re.sub(r"[^0-9A-Za-z一-龥]", "", core)
    if len(core) < 3:
        return True

    fa = re.sub(r"[^0-9A-Za-z一-龥]", "", formatted)

    # 从长到短找连续命中：能匹配 6 字自然比只匹配 3 字可靠
    for length in range(min(len(core), 14), 2, -1):
        for i in range(len(core) - length + 1):
            if core[i:i + length] in fa:
                return True
    return False


def _geocode_raw(address: str, city: str = "") -> Optional[dict]:
    """geocode 的内部实现，返回 {"lng","lat","geo"} 或 None

    抽出来是为了让 geocode() 和 geocode_detail() 共用同一套限流退避与
    行政区一致性校验，避免两份实现漂移。
    """
    if not address or not address.strip():
        return None

    key = _load_amap_key()
    if not key:
        return None

    addr = address.strip()

    # [2026-10-05] 地理编码策略：先精确试，失败了再逐级放大 city 范围重试。
    #
    # 起因是我自己写错了调用方式，还把锅甩给高德。实测对比同一个地址：
    #   geocode("北京市第八十中学睿志分校")            → 解析不出（formatted 只有区名）
    #   geocode("北京市第八十中学睿志分校", city="北京市朝阳区")
    #                                                  → ✅ 116.557219,40.004271
    #                                                    formatted = 「…金盏乡北京市第八十中学睿志分校」
    # 差别只在 `city` 参数。高德 Web 服务文档里 geocode/geo 的 city 是「城市名或编码」，
    # 传了它之后高德会把搜索**限定在该城市内**，从而能匹配到城市里的具体 POI；
    # 不传就退化成行政区级别的粗定位。
    # 我之前一次都没传过 city，却把「解析不到」怪成高德不准，是我的错。
    #
    # 现在按「越精确越优先」的顺序重试：
    #   1) 调用方显式传了 city      → 用它
    #   2) 从地址里提取的区县        → 「北京市朝阳区金盏乡…」→ 用「北京市朝阳区」
    #   3) 从地址里提取的城市        → 「浙江省杭州市…」→ 用「杭州市」
    #   4) 全都不传                  → 最后的兜底
    attempts = []
    if city:
        attempts.append((addr, city))
    else:
        for candidate in _city_candidates(addr):
            attempts.append((addr, candidate))
        # [2026-10-05] 关键：还要试「剥掉乡/镇/街道」的精简地址。
        #
        # 实测（主人的「北京市第八十中学睿志分校」）：
        #   「北京市朝阳区金盏乡八十中学睿志分校」 + city=北京市朝阳区
        #        → formatted 只有「北京市朝阳区金盏乡」，坐标落在金盏乡人民政府（粗）
        #   「北京市第八十中学睿志分校」           + city=北京市朝阳区
        #        → formatted「…北京市第八十中学睿志分校」，坐标精确到学校 ✅
        # 原因是高德 POI 里登记的地址**没有「金盏乡」这一层**，
        # 原样带上去反而让它降级成行政区级粗定位；去掉就能命中具体 POI。
        # 所以这里把「原地址」和「精简地址」都试一遍，谁先通过校验用谁。
        for simplified in _simplified_addresses(addr):
            for candidate in _city_candidates(addr):
                attempts.append((simplified, candidate))
    if not attempts:
        attempts = [(addr, "")]

    last_reason = None
    seen = set()
    for try_addr, try_city in attempts:
        sig = (try_addr, try_city)
        if sig in seen:
            continue
        seen.add(sig)
        result, last_reason = _geocode_once(try_addr, key, try_city)
        if result is not None:
            return result
    return None


def _simplified_addresses(address: str) -> list:
    """生成「换个写法再试」的地址候选

    [2026-10-05] 实测记录（主人的真实地址）：
        「北京市朝阳区金盏八十中学睿志分校」（库里实际填的）
              → ❌ 解析不出，只落到金盏乡
        「北京市朝阳区第八十中学睿志分校」
              → ✅ 116.557219,40.004271，formatted = 「…北京市第八十中学睿志分校」
        「北京市朝阳区金盏乡八十中学睿志分校」
              → ❌ 带「乡」反而降级成粗定位
        「北京市朝阳区八十中学睿志分校」
              → ❌ 还是不行

    两个手法，各解决一类写法：
      ① 中文数字补全：「八十中」→「第八十中」。
         高德 POI 里登记的正式名称是「北京市第八十中学…」，少了「第」字它匹配不上。
      ② 剥掉「乡/镇/街道办事处」这一级。
         POI 登记的地址往往没有「金盏乡」这一层，多写反而让它降级成行政区级粗定位。

    实现要点：**不能直接用正则全局匹配「XX乡/XX镇」** ——
    试过 `[一-龥]{2,4}(?:乡|镇)`，它会跨过「朝阳|区|金盏」去咬「阳区金盏乡」，
    把「北京市朝阳区」吃成「北京市朝」，地址反而更糟。
    这里先用「省+市+区/县」切前缀，再只处理剩余部分的开头，精准不误伤。
    """
    out = []

    def push(v):
        v = v.strip(" ,，、")
        if v and v not in out and v != address and len(v) >= 6:
            out.append(v)

    # ① 中文数字补全（对原地址直接做）
    full = re.sub(r"(?<!第)([一二三四五六七八九十])十中", r"第\1十中", address)
    push(full)

    m = re.search(r"([一-龥]{2,6}?省)?([一-龥]{2,6}?市)([一-龥]{2,6}?[区县])", address)
    if not m:
        return out
    prefix_end = m.end()                       # 「北京市朝阳区」之后
    prefix = address[:prefix_end]
    rest = address[prefix_end:]                # 「金盏乡八十中学睿志分校」

    # ② 剥掉 rest 开头的乡/镇/街道
    stripped = re.sub(r"^[一-龥]{2,4}(?:街道办事处|街道|乡|镇)\s*", "", rest)
    if stripped and stripped != rest:
        push(prefix + stripped)
        # ③ 剥掉之后再补一次「第」
        push(prefix + re.sub(r"(?<!第)([一二三四五六七八九十])十中",
                             r"第\1十中", stripped))
    return out


def _city_candidates(address: str) -> list:
    """从地址里提取高德 geocode 的 city 参数候选，按「细 → 粗」顺序

    [2026-10-05] 高德 geocode/geo 的 city 传得越具体，定位越准。实测同一条地址：
        geocode("北京市第八十中学睿志分校")                      → 解析不出
        geocode(..., city="北京市朝阳区")                          → 精确到学校 ✅
        geocode("北京市朝阳区金盏乡八十中学睿志分校")             → 只到金盏乡（粗）
        geocode("北京市朝阳区金盏乡八十中学睿志分校", city="北京市朝阳区金盏乡") → 精确 ✅

    所以候选要覆盖到「乡/镇」这一级，并按从细到粗排列逐个重试。
    高德文档里 city 允许传「城市名或编码」，同时也接受区县/乡镇名，
    实测传区县、乡镇都能显著提高精度（比只传「北京市」或完全不传好得多）。
    """
    out = []

    def push(v):
        if v and v not in out:
            out.append(v)

    # ① 「省+市+区/县+乡/镇/街道」全串，最精确
    m = re.search(
        r"([一-龥]{2,6}?省)?([一-龥]{2,6}?市)([一-龥]{2,6}?[区县])"
        r"([一-龥]{2,6}?(?:乡|镇|街道|街道办事处))?",
        address,
    )
    if m:
        prov, cty, dist, town = m.group(1) or "", m.group(2), m.group(3), m.group(4)
        if town:
            push(f"{prov}{cty}{dist}{town}")   # 北京市朝阳区金盏乡
            push(f"{cty}{dist}{town}")
        push(f"{prov}{cty}{dist}")              # 北京市朝阳区
        push(f"{cty}{dist}")
        push(cty)                              # 北京市
    else:
        m2 = re.search(r"([一-龥]{2,6}?市)([一-龥]{2,6}?[区县])"
                       r"([一-龥]{2,6}?(?:乡|镇|街道|街道办事处))?", address)
        if m2:
            cty, dist, town = m2.group(1), m2.group(2), m2.group(3)
            if town:
                push(f"{cty}{dist}{town}")
            push(f"{cty}{dist}")
            push(cty)
        else:
            m3 = re.search(r"([一-龥]{2,6}?市)", address)
            if m3:
                push(m3.group(1))
    return out


def _geocode_once(address: str, key: str, city: str):
    """调一次高德 geocode/geo，返回 (结果dict, 失败原因)

    失败原因仅用于调用方决策是否换更宽的 city 重试，这里不做成异常。
    """
    params = {"address": address, "key": key, "output": "json", "extensions": "all"}
    if city:
        params["city"] = city

    url = f"{AMAP_BASE}/geocode/geo?{urlencode(params)}"

    for attempt in range(_RETRY):
        try:
            req = Request(url, headers={"User-Agent": "HomeBrain/1.0"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("status") != "1":
                info = str(data.get("info") or "")
                # [2026-10-04] 限流要退避重试；其余失败（地址不认识）是真失败，重试无意义
                if "QPS" in info.upper() or "CUQPS" in info:
                    if attempt < _RETRY - 1:
                        time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
                        continue
                return None, f"高德返回失败：{info}"

            geocodes = data.get("geocodes") or []
            if not geocodes:
                return None, "高德没有返回任何结果"
            geo = geocodes[0]

            loc = (geo.get("location") or "").split(",")
            if len(loc) != 2:
                return None, "返回的坐标格式不对"
            lng, lat = round(float(loc[0]), 6), round(float(loc[1]), 6)

            if not _looks_like_cn(lng, lat):
                return None, "坐标不在中国境内"

            # [2026-10-04] 行政区一致性校验，挡掉高德匹配到别处同名 POI 的情况
            if not _region_consistent(address, geo):
                return None, "行政区对不上（可能匹配到别处的同名地点）"

            # [2026-10-05] 名称相关性校验，挡掉「行政区对但地点错」的情况。
            if not _name_consistent(address, _as_str(geo.get("formatted_address"))):
                return None, (
                    "高德只解析到行政区级别、没定位到具体地点"
                    f"（返回的是「{_as_str(geo.get('formatted_address'))}」）"
                )

            return {"lng": lng, "lat": lat, "geo": geo}, None
        except Exception as e:
            return None, f"请求异常：{e}"

    return None, "重试用尽"


def get_house_location(db_path: str, house_id: Optional[int] = None) -> Optional[Tuple[float, float]]:
    """读取家庭坐标（「家」的位置）

    [2026-10-04] 优先用已存的 lng/lat；若为空则用地址现算一次并回写，
    这样老数据（迁移前录入的房屋）也能自动补上坐标，不需要手工重录。

    house_id 为 None 时取第一栋房屋。

    [2026-10-05] 地点簿上线后，「家」搬到 places 表（is_primary=1），
    所以这里改成 **places 优先、houses 回退**：
        - places 里有 is_primary=1 → 用它（唯一数据源）
        - places 表还没建 / 没有 home 行 → 回退读 houses 表首条
    这样 show_map.py 和 travel_reminder 这两个调用方一行都不用改、行为完全不变，
    即使迁移脚本没跑也只是走回退分支，不会挂。
    """
    if not os.path.exists(db_path):
        return None

    # —— 第一优先：places 表里的「家」——
    if not house_id:
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT lng, lat, address FROM places WHERE is_primary=1 LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row and row["lng"] is not None and row["lat"] is not None:
                return (float(row["lng"]), float(row["lat"]))
            if row and row["address"]:
                # 有地址没坐标：现算一次（不写库，写库归管理台的事）
                loc = geocode(row["address"])
                if loc:
                    return loc
        except Exception:
            pass

    conn = None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        if house_id:
            row = conn.execute(
                "SELECT id, address, lng, lat FROM houses WHERE id = ?", (house_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, address, lng, lat FROM houses ORDER BY id LIMIT 1"
            ).fetchone()

        if not row:
            return None

        # 已有坐标直接用
        if row["lng"] is not None and row["lat"] is not None:
            return (float(row["lng"]), float(row["lat"]))

        # 没坐标则现算并回写
        loc = geocode(row["address"] or "")
        if loc:
            try:
                conn.execute(
                    "UPDATE houses SET lng = ?, lat = ? WHERE id = ?",
                    (loc[0], loc[1], row["id"]),
                )
                conn.commit()
            except Exception:
                pass
        return loc
    except Exception:
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
