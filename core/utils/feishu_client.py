"""飞书 API 异步客户端（HomeBrain 用）

职责：
  - tenant_access_token 管理（自动缓存 + 提前 5 分钟刷新）
  - 发消息（im.v1.message.create）
  - aiohttp 单 session 复用

复用约定：
  - 凭证来源：data/.mcp_server_settings.json 的 mcpServers.lark.args
  - 与 lark-mcp（Node 出站 MCP 服务）共用同一 App，互不冲突
  - 本客户端只负责出站 API 调用；入站事件接收在 feishu_listener.py
"""

from __future__ import annotations

import json
import time
from typing import Optional

import aiohttp

from config.logger import setup_logging

logger = setup_logging()
TAG = "FeishuClient"

BASE = "https://open.feishu.cn"


class FeishuClient:
    """异步飞书 API 客户端。"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: Optional[str] = None
        self._token_expires: float = 0
        self._session: Optional[aiohttp.ClientSession] = None

    # ── 生命周期 ──────────────────────────────────────────────────
    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            logger.bind(tag=TAG).info("aiohttp session 已关闭")

    # ── token ─────────────────────────────────────────────────────
    async def get_tenant_token(self) -> str:
        """取 tenant_access_token，缓存并在过期前 5 分钟自动刷新。"""
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        await self._ensure_session()
        async with self._session.post(
            f"{BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        ) as r:
            data = await r.json()
        code = data.get("code")
        if code != 0:
            raise RuntimeError(f"飞书 tenant_access_token 失败: code={code} msg={data.get('msg')}")
        self._token = data["tenant_access_token"]
        expire = data.get("expire", 7200)
        self._token_expires = time.time() + expire
        logger.bind(tag=TAG).info(f"tenant_access_token 已刷新，{expire}s 后过期")
        return self._token

    # ── 发消息 ────────────────────────────────────────────────────
    async def send_text(self, open_id: str, text: str) -> bool:
        """给指定 open_id 发一条文本消息。成功返回 True。"""
        token = await self.get_tenant_token()
        await self._ensure_session()
        body = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        try:
            async with self._session.post(
                f"{BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.bind(tag=TAG).error(f"发送请求异常: {e}")
            return False

        code = data.get("code")
        if code != 0:
            logger.bind(tag=TAG).error(f"发送失败 code={code} msg={data.get('msg')}")
            return False

        mid = (data.get("data") or {}).get("message_id", "")
        logger.bind(tag=TAG).info(f"已发送文本 → {open_id[:10]}… msg_id={mid[:12]}… ({len(text)} 字)")
        return True

    # ── 音频下载（语音消息转文字用）──────────────────────────────
    async def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str = "audio"
    ) -> bytes:
        """下载消息里的音频/文件资源，返回原始字节（飞书语音一般是 opus）。"""
        token = await self.get_tenant_token()
        await self._ensure_session()
        url = (
            f"{BASE}/open-apis/im/v1/messages/{message_id}"
            f"/resources/{file_key}?type={resource_type}"
        )
        logger.bind(tag=TAG).info(f"下载资源 URL: {url}")
        try:
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as r:
                body = await r.read()
                if r.status != 200:
                    logger.bind(tag=TAG).error(
                        f"下载资源失败 status={r.status} body={body.decode('utf-8','replace')[:400]}"
                    )
                    return b""
                logger.bind(tag=TAG).info(
                    f"已下载资源 file_key={file_key[:16]}… type={resource_type} ({len(body)} 字节)"
                )
                return body
        except Exception as e:
            logger.bind(tag=TAG).error(f"下载资源异常: {e}")
            return b""

    # ── 音频上传（文字转语音回利用）──────────────────────────────
    async def upload_audio(self, file_path: str, duration_ms: int = 0) -> str | None:
        """上传音频文件到飞书，返回 file_key。失败返回 None。
        飞书语音消息要求 opus 格式，调用方需保证文件是 opus。
        """
        import os
        token = await self.get_tenant_token()
        await self._ensure_session()

        if not os.path.exists(file_path):
            logger.bind(tag=TAG).error(f"上传文件不存在: {file_path}")
            return None

        form = aiohttp.FormData()
        form.add_field("file_type", "opus")
        form.add_field("file_name", os.path.basename(file_path))
        if duration_ms:
            form.add_field("duration", str(duration_ms))
        form.add_field(
            "file", open(file_path, "rb"),
            filename=os.path.basename(file_path),
            content_type="audio/opus",
        )
        try:
            async with self._session.post(
                f"{BASE}/open-apis/im/v1/files",
                data=form,
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.bind(tag=TAG).error(f"上传音频异常: {e}")
            return None

        if data.get("code") != 0:
            logger.bind(tag=TAG).error(f"上传失败 code={data.get('code')} msg={data.get('msg')}")
            return None
        file_key = (data.get("data") or {}).get("file_key")
        logger.bind(tag=TAG).info(f"已上传音频 → file_key={file_key}")
        return file_key

    async def send_audio(self, open_id: str, file_key: str) -> bool:
        """给指定 open_id 发一条语音消息（file_key 来自 upload_audio）。"""
        token = await self.get_tenant_token()
        await self._ensure_session()
        body = {
            "receive_id": open_id,
            "msg_type": "audio",
            "content": json.dumps({"file_key": file_key}),
        }
        try:
            async with self._session.post(
                f"{BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.bind(tag=TAG).error(f"发送音频异常: {e}")
            return False

        if data.get("code") != 0:
            logger.bind(tag=TAG).error(f"发送音频失败 code={data.get('code')} msg={data.get('msg')}")
            return False
        mid = (data.get("data") or {}).get("message_id", "")
        logger.bind(tag=TAG).info(f"已发送语音 → {open_id[:10]}… msg_id={mid[:12]}…")
        return True

    # ── 读消息（查飞书消息工具用，2026-09-21 新增）────────────────
    async def list_p2p_chats(self) -> list:
        """列出机器人所在的单聊（p2p）会话，返回 items 列表。"""
        token = await self.get_tenant_token()
        await self._ensure_session()
        try:
            async with self._session.get(
                f"{BASE}/open-apis/im/v1/chats?page_size=50",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取会话列表异常: {e}")
            return []
        if data.get("code") != 0:
            logger.bind(tag=TAG).error(
                f"获取会话列表失败 code={data.get('code')} msg={data.get('msg')}"
            )
            return []
        items = (data.get("data") or {}).get("items") or []
        return [c for c in items if c.get("chat_type") == "p2p"]

    async def get_chat_members(self, chat_id: str) -> list:
        """取单聊/群成员的 open_id 列表。"""
        token = await self.get_tenant_token()
        await self._ensure_session()
        try:
            async with self._session.get(
                f"{BASE}/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id&page_size=50",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取会话成员异常: {e}")
            return []
        if data.get("code") != 0:
            return []
        items = (data.get("data") or {}).get("items") or []
        return [m.get("member_id", "") for m in items if m.get("member_id")]

    async def list_messages(self, chat_id: str, page_size: int = 20) -> list:
        """列出会话最新消息（按创建时间倒序）。

        返回 [{"type", "text", "sender_id", "time_ms"}]；非文本消息转占位标签。
        """
        token = await self.get_tenant_token()
        await self._ensure_session()
        try:
            async with self._session.get(
                f"{BASE}/open-apis/im/v1/messages",
                params={
                    "container_id_type": "chat",
                    "container_id": chat_id,
                    "page_size": max(1, min(int(page_size or 20), 50)),
                    "sort_type": "ByCreateTimeDesc",
                },
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.bind(tag=TAG).error(f"获取消息列表异常: {e}")
            return []
        if data.get("code") != 0:
            logger.bind(tag=TAG).error(
                f"获取消息列表失败 code={data.get('code')} msg={data.get('msg')}"
            )
            return []
        items = (data.get("data") or {}).get("items") or []
        out = []
        for m in items:
            msg_type = m.get("msg_type", "")
            body_content = ((m.get("body") or {}).get("content") or "").strip()
            if msg_type == "text":
                try:
                    text = json.loads(body_content).get("text", "")
                except Exception:
                    text = body_content
            else:
                text = {
                    "post": "[图文消息]", "image": "[图片]", "audio": "[语音]",
                    "file": "[文件]", "media": "[视频]", "sticker": "[表情]",
                }.get(msg_type, f"[{msg_type}]")
            out.append({
                "type": msg_type,
                "text": text,
                "sender_id": ((m.get("sender") or {}).get("id") or ""),
                "time_ms": int(m.get("create_time", "0") or 0),
            })
        return out
