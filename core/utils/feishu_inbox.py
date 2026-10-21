"""飞书收藏入库（2026-09-29）

主人从飞书发来的网页链接 / 文件（PDF/Office 等）→ 抓取或转换为 Markdown →
自动分类 → 上传 RAGflow「我的收藏」知识库 → 回执确认。

入口由 core/utils/feishu_listener.py 调用：
  - handle_link(config, url)          文本消息里的链接
  - handle_file(config, data, name)   文件消息（下载后的字节）
批量反问（短时间内大量链接先确认）也在本模块内管理。
"""
import asyncio
import json
import os
import re
import tempfile
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from config.logger import setup_logging

logger = setup_logging()
TAG = "FeishuInbox"

# 「我的收藏」知识库（2026-09-29 经 RAGflow API 创建）
INBOX_DATASET_ID = 'YOUR_FEISHU_INBOX_DATASET_ID'

# 批量反问：滑动窗口内链接数达到阈值 → 先问一句再入库
BURST_WINDOW_S = 300      # 5 分钟窗口
BURST_THRESHOLD = 5       # 达到 5 个链接触发反问
BURST_CONFIRM_S = 600     # 反问后等 10 分钟，未确认也自动入库

# 分类规则（顺序即优先级，命中即止）
_CATEGORIES = [
    ("智能家居", ["智能家居", "智能家电", "米家", "homeassistant", "home assistant", "ha ",
                   "传感器", "开关面板", "智能灯", "窗帘电机", "门锁", "摄像头", "安防",
                   "iot", "物联网", "全屋智能", "matter", "zigbee", "esp32", "树莓派"]),
    ("AI与科技", ["ai", "llm", "大模型", "agent", "gpt", "claude", "gemini", "deepseek",
                   "模型", "算力", "芯片", "显卡", "gpu", "开源项目", "github", "编程",
                   "算法", "transformer", "rag", "向量"]),
    ("健康养生", ["健康", "医", "药", "养老", "急救", "养生", "血压", "血糖", "饮食",
                   "睡眠", "运动健身"]),
    ("教育育儿", ["教育", "育儿", "学习", "学校", "课程", "考试", "儿童", "小学"]),
    ("财经商业", ["股票", "基金", "投资", "理财", "商业", "创业", "公司", "财报", "经济"]),
]


def _cfg(config: dict) -> tuple[str, str]:
    """取 RAGflow base_url / api_key（复用 search_from_ragflow 的配置块）。"""
    rc = (config.get("plugins") or {}).get("search_from_ragflow") or {}
    return (rc.get("base_url") or "http://localhost:9380").rstrip("/"), rc.get("api_key") or ""


def classify(text: str) -> str:
    """关键词规则分类。返回分类名（未命中→综合杂项）。

    英文关键词用词边界匹配（防止 "ai" 误中 domain、"ha" 误中 challenge），
    中文关键词按子串匹配。
    """
    low = (text or "").lower()
    for name, kws in _CATEGORIES:
        for kw in kws:
            kw = kw.lower()
            if re.search(r"[^\x00-\x7f]", kw) is None:  # 纯 ASCII → 词边界
                if re.search(r"\b" + re.escape(kw) + r"\b", low):
                    return name
            elif kw in low:
                return name
    return "综合杂项"


def extract_urls(text: str) -> list[str]:
    """从文本里提取 http(s) 链接。"""
    return re.findall(r"https?://[^\s<>\"'）】]+", text or "")


def strip_urls(text: str) -> str:
    """去掉文本里的链接，判断主人是否还说了别的话。"""
    return re.sub(r"https?://[^\s<>\"'）】]+", "", text or "").strip()


def fetch_webpage(url: str, timeout: int = 20) -> tuple[str, str]:
    """抓网页 → (标题, Markdown 正文)。失败抛异常。"""
    resp = requests.get(url, timeout=timeout, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    }, allow_redirects=True)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype:
        raise ValueError(f"非网页内容: {ctype}")

    # 网页几乎都是 UTF-8；requests 在无 charset 声明时会按 latin-1 猜导致中文乱码
    html = resp.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "") or url
    for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines, seen = [], set()
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre", "td"]):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if not t or t in seen or len(t) < 2:
            continue
        seen.add(t)
        prefix = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ",
                  "li": "- ", "blockquote": "> "}.get(el.name, "")
        lines.append(prefix + t)
    body = "\n\n".join(lines)
    if len(body) < 100:  # 正文太少（JS 渲染页），退回整页文本
        body = re.sub(r"\n{3,}", "\n\n", root.get_text("\n", strip=True))
    return title, body


def convert_file_to_markdown(data: bytes, filename: str) -> str:
    """本地文件（PDF/Word/PPT/Excel/图片等）→ Markdown。走已安装的 anydoc CLI。"""
    suffix = os.path.splitext(filename)[1] or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(data)
        tmp.close()
        import subprocess
        proc = subprocess.run(
            ["npx", "-y", "@firecrawl/anydoc", tmp.name],
            capture_output=True, text=True, timeout=180, cwd=os.path.expanduser("~"),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"anydoc 转换失败: {(proc.stderr or '')[:200]}")
        return proc.stdout
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _upload(base_url: str, api_key: str, filename: str, content: str) -> str:
    """上传 md 文档到「我的收藏」，返回 document_id。"""
    resp = requests.post(
        f"{base_url}/api/v1/datasets/{INBOX_DATASET_ID}/documents",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
        timeout=60,
    )
    body = resp.json()
    # RAGflow 版本差异：新版本返回 list，旧版本返回 {"code":0,"data":{"docs":[...]}}
    if isinstance(body, list):
        docs = body
    elif isinstance(body, dict):
        if body.get("code") != 0:
            raise RuntimeError(f"RAGflow 上传失败: {body.get('message') or body}")
        data = body.get("data")
        if isinstance(data, list):
            docs = data                      # 新版：data 直接是文档数组
        elif isinstance(data, dict):
            docs = data.get("docs") or []    # 旧版：data.docs
        else:
            docs = []
    else:
        raise RuntimeError(f"RAGflow 上传响应异常: {str(body)[:120]}")
    if not docs:
        raise RuntimeError(f"RAGflow 上传无返回文档: {str(body)[:200]}")
    doc = docs[0]
    return doc["id"] if isinstance(doc, dict) else str(doc)


def _parse(base_url: str, api_key: str, doc_id: str) -> None:
    """触发 RAGflow 解析（切片+向量化）。"""
    resp = requests.post(
        f"{base_url}/api/v1/datasets/{INBOX_DATASET_ID}/documents/parse",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"document_ids": [doc_id]},
        timeout=30,
    )
    body = resp.json()
    if isinstance(body, dict) and body.get("code") not in (0, None):
        raise RuntimeError(f"RAGflow 解析触发失败: {body.get('message') or body}")


def _save(base_url: str, api_key: str, title: str, url: str, body: str,
          extra_tags: str = "") -> str:
    """分类 + 落 md + 上传 + 解析。返回回执文案。"""
    source = f"{title} {url} {body[:2000]} {extra_tags}"
    cat = classify(source)
    md = (f"# {title}\n\n> 来源：{url}\n> 分类：{cat}\n> 收藏时间：{time.strftime('%Y-%m-%d %H:%M')}\n\n"
          f"{body}\n")
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:60]
    filename = f"[{cat}]{safe_title}.md"
    doc_id = _upload(base_url, api_key, filename, md)
    try:
        _parse(base_url, api_key, doc_id)
    except Exception as e:  # 解析失败不影响入库，文档已在库里
        logger.bind(tag=TAG).warning(f"触发解析失败（文档已入库）: {e}")
    logger.bind(tag=TAG).info(f"入库成功: [{cat}] {title[:40]} → doc={doc_id[:12]}…")
    return f"✅ 已收藏：【{cat}】{title}"


async def handle_link(config: dict, url: str, note: str = "") -> str:
    """处理一个链接：抓取 → 分类 → 入库。返回回执（失败返回原因，不抛）。"""
    loop = asyncio.get_event_loop()
    try:
        base_url, api_key = _cfg(config)
        if not api_key:
            return f"❌ 入库失败：未配置 RAGflow api_key（{url[:50]}）"
        title, body = await loop.run_in_executor(None, fetch_webpage, url)
        if not body.strip():
            return f"⚠️ 网页抓不到正文，未入库：{title[:40]}"
        return await loop.run_in_executor(None, _save, base_url, api_key, title, url, body, note)
    except Exception as e:
        logger.bind(tag=TAG).error(f"链接入库失败 {url}: {type(e).__name__}: {e}")
        return f"❌ 收藏失败：{type(e).__name__}: {str(e)[:120]}"


async def handle_file(config: dict, data: bytes, filename: str) -> str:
    """处理一个文件：anydoc → 分类 → 入库。"""
    loop = asyncio.get_event_loop()
    try:
        base_url, api_key = _cfg(config)
        if not api_key:
            return "❌ 入库失败：未配置 RAGflow api_key"
        md = await loop.run_in_executor(None, convert_file_to_markdown, data, filename)
        title = os.path.splitext(os.path.basename(filename))[0]
        return await loop.run_in_executor(
            None, _save, base_url, api_key, title, f"飞书文件：{filename}", md, filename)
    except Exception as e:
        logger.bind(tag=TAG).error(f"文件入库失败 {filename}: {type(e).__name__}: {e}")
        return f"❌ 收藏失败（{filename}）：{type(e).__name__}: {str(e)[:120]}"


# ── 批量反问（短时间涌入大量链接时先确认一次）─────────────────────
class LinkBurstGuard:
    """滑动窗口计数 + 暂存 + 延迟自动入库。全部在主 event loop 内调用。"""

    def __init__(self):
        self.times: list[float] = []
        self.pending: list[str] = []
        self.asked = False
        self.waiter: asyncio.Task | None = None

    async def offer(self, config: dict, urls: list[str],
                    send) -> tuple[bool, list[str]]:
        """登记链接。返回 (已直接入库, 回执列表)。

        窗口内未达阈值 → 立即入库；已达阈值且未问过 → 暂存并反问；
        反问等待期 → 继续暂存。
        """
        now = time.monotonic()
        self.times = [t for t in self.times if now - t < BURST_WINDOW_S]
        self.times.extend([now] * len(urls))

        if not self.asked and len(self.times) < BURST_THRESHOLD:
            receipts = [await handle_link(config, u) for u in urls]
            return True, receipts

        self.pending.extend(urls)
        if not self.asked:
            self.asked = True
            await send(f"你 {BURST_WINDOW_S // 60} 分钟内发了 {len(self.times)} 个链接——"
                       f"从现在起我先攒着，要都收藏吗？回「是/存」我就入库；"
                       f"不回我 {BURST_CONFIRM_S // 60} 分钟后也会自动存。")
            self.waiter = asyncio.get_event_loop().call_later(
                BURST_CONFIRM_S, lambda: asyncio.ensure_future(self._auto_flush(config, send)))
        return False, []

    def confirm(self) -> bool:
        """收到确认词。返回是否有待入库的暂存。"""
        return bool(self.pending)

    def take_pending(self) -> list[str]:
        urls, self.pending, self.asked = list(self.pending), [], False
        if self.waiter:
            self.waiter.cancel()
            self.waiter = None
        return urls

    async def _auto_flush(self, config: dict, send) -> None:
        urls = self.take_pending()
        if not urls:
            return
        for u in urls:
            await handle_link(config, u)
        try:
            await send(f"（超时未确认，已自动收藏 {len(urls)} 个链接）")
        except Exception:
            pass


guard = LinkBurstGuard()
