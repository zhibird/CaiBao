"""QQ 官方 Bot 通道：QQ 开放平台 WebSocket + HTTP API。

协议要点：
- 认证：POST /app/getAppAccessToken 获取 access_token（7200s 过期，自动刷新）
- 收消息：wss://api.sgroup.qq.com/websocket（opcode 分发）
- 发消息：POST /v2/users/{openid}/messages（私聊）
          POST /v2/groups/{group_openid}/messages（群聊）
- 图片：POST /v2/users/{openid}/files（上传）+ media 字段引用
- 消息 ID：字符串格式 msg_id

文档：https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import websockets

from qqbot_adapter.channels.base import BaseChannel
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage, OutboundMessage
from qqbot_adapter.utils.image_utils import (
    download_images_from_message,
    bytes_to_base64,
    guess_mime_type,
)

_logger = logging.getLogger(__name__)

# QQ 官方 Bot API 基础 URL
_QQ_API_BASE = "https://api.sgroup.qq.com"
_QQ_AUTH_URL = "https://bots.qq.com/app/getAppAccessToken"

# 重连参数
_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_BACKOFF = 2.0

# Token 提前刷新时间（秒）
_TOKEN_REFRESH_MARGIN = 300


class QQBotChannel(BaseChannel):
    """QQ 官方 Bot API 通道（WebSocket 收 + HTTP 发）。"""

    channel_type = "qqbot"

    def __init__(
        self,
        bus: MessageBus,
        *,
        app_id: str,
        client_secret: str,
        allow_from: list[str] | None = None,
        groups: list[dict[str, Any]] | None = None,
        reconnect: bool = True,
    ) -> None:
        super().__init__(bus)
        self.app_id = app_id
        self.client_secret = client_secret
        self.allow_from: set[str] = set(allow_from or [])
        self.groups: dict[str, dict[str, Any]] = {}
        if groups:
            for g in groups:
                gid = str(g.get("group_openid", ""))
                if gid:
                    self.groups[gid] = g
        self.reconnect_enabled = reconnect

        # 运行时状态
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._ws: websockets.ClientConnection | None = None
        self._http: httpx.AsyncClient | None = None
        self._running = False
        self._connect_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._session_id: str = ""
        self._last_seq: int = 0
        self._ws_url: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self.bus.subscribe_outbound(self.channel_type, self._outbound_callback)
        self._connect_task = asyncio.create_task(self._connect_loop())
        _logger.info("QQBotChannel started (app_id=%s)", self.app_id)

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self.channel_type)

        for task in (self._connect_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
        self._connect_task = None
        self._heartbeat_task = None

        if self._ws is not None:
            try: await self._ws.close()
            except Exception: pass
            self._ws = None

        if self._http is not None:
            await self._http.aclose()
            self._http = None

        _logger.info("QQBotChannel stopped")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        delay = _RECONNECT_BASE_DELAY
        while self._running:
            try:
                await self._ensure_token()
                await self._get_ws_url()
                await self._connect_ws()
                delay = _RECONNECT_BASE_DELAY
                await self._message_loop()
            except websockets.ConnectionClosed as exc:
                _logger.warning("WebSocket closed: code=%s reason=%s", exc.code, exc.reason)
            except OSError as exc:
                _logger.warning("WebSocket connection failed: %s", exc)
            except Exception:
                _logger.exception("Unexpected error in QQBot connection loop")

            if not self._running or not self.reconnect_enabled:
                break

            _logger.info("Reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * _RECONNECT_BACKOFF, _RECONNECT_MAX_DELAY)

    async def _ensure_token(self) -> None:
        """获取或刷新 access_token。"""
        now = time.monotonic()
        if self._access_token and (self._token_expires_at - now) > _TOKEN_REFRESH_MARGIN:
            return

        assert self._http is not None
        resp = await self._http.post(
            _QQ_AUTH_URL,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = now + expires_in
        _logger.info(
            "QQBot access_token obtained (expires_in=%ss)", expires_in,
        )

    async def _get_ws_url(self) -> None:
        """获取 WebSocket 连接地址（带 token）。"""
        assert self._http is not None
        resp = await self._http.get(
            f"{_QQ_API_BASE}/gateway",
            headers={"Authorization": f"QQBot {self._access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._ws_url = data["url"]
        _logger.info("QQBot gateway URL obtained")

    async def _connect_ws(self) -> None:
        """建立 WebSocket 连接。"""
        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=20.0,
            ping_timeout=10.0,
            close_timeout=5.0,
            max_size=2**20,
        )
        _logger.info("Connected to QQBot WebSocket")

    async def _message_loop(self) -> None:
        """WebSocket 消息循环。"""
        assert self._ws is not None
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                _logger.debug("Non-JSON WS message: %.100s", raw)
                continue

            op = data.get("op", -1)
            payload = data.get("d", {})

            if op == 10:  # Hello
                self._session_id = data.get("s", "")
                interval = payload.get("heartbeat_interval", 41250)
                self._start_heartbeat(interval)
                _logger.info("QQBot Hello received, heartbeat=%sms", interval)

            elif op == 0:  # Dispatch
                self._last_seq = data.get("s", self._last_seq)
                await self._dispatch_event(payload)

            elif op == 11:  # Heartbeat ACK
                _logger.debug("Heartbeat ACK")

    def _start_heartbeat(self, interval_ms: int) -> None:
        """启动心跳任务。"""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(interval_ms / 1000.0)
        )

    async def _heartbeat_loop(self, interval: float) -> None:
        while self._running and self._ws is not None:
            try:
                await asyncio.sleep(interval)
                await self._ws.send(json.dumps({
                    "op": 1,  # Heartbeat
                    "d": self._last_seq,
                }))
            except asyncio.CancelledError:
                break
            except Exception:
                _logger.warning("Heartbeat send failed", exc_info=True)

    # ------------------------------------------------------------------
    # 事件分发
    # ------------------------------------------------------------------

    async def _dispatch_event(self, payload: dict[str, Any]) -> None:
        """根据事件类型分发到对应 handler。"""
        event_type = payload.get("t", "")

        if event_type == "C2C_MESSAGE_CREATE":
            await self._on_private_message(payload)
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            await self._on_group_message(payload)
        elif event_type == "READY":
            _logger.info("QQBot Ready: session=%s", payload.get("session_id", ""))

    async def _on_private_message(self, payload: dict[str, Any]) -> None:
        """处理官方 Bot 私聊消息。"""
        author = payload.get("author", {})
        user_id = str(author.get("id", ""))
        content = str(payload.get("content", ""))

        if self.allow_from and user_id not in self.allow_from:
            return
        if not content.strip():
            return

        # 下载图片附件
        attachments = payload.get("attachments") or []
        image_urls = [
            str(a.get("url", ""))
            for a in attachments
            if a.get("content_type", "").startswith("image/")
        ]
        images = await download_images_from_message(
            "\n".join(image_urls), http_client=self._http,
        )

        inbound = InboundMessage(
            channel_type=self.channel_type,
            chat_type="private",
            chat_id=user_id,
            user_id=user_id,
            user_name=str(author.get("username", "") or user_id),
            content=content,
            images=images,
            message_id=str(payload.get("id", "")),
            raw_event=payload,
        )
        await self.bus.publish_inbound(inbound)
        _logger.info("QQBot private: from=%s content=%.60s", user_id, content)

    async def _on_group_message(self, payload: dict[str, Any]) -> None:
        """处理官方 Bot 群聊消息（@ 触发）。"""
        group_openid = str(payload.get("group_openid", ""))
        group_config = self.groups.get(group_openid)
        if group_config is None:
            return

        author = payload.get("author", {})
        user_id = str(author.get("member_openid", ""))
        allow_from = group_config.get("allow_from", [])
        if allow_from and user_id not in allow_from:
            return

        content = str(payload.get("content", ""))
        if not content.strip():
            return

        # 去掉消息中的 @机器人 部分
        content = self._strip_at_mention(content)

        chat_id = f"g{group_openid}"
        inbound = InboundMessage(
            channel_type=self.channel_type,
            chat_type="group",
            chat_id=chat_id,
            user_id=user_id,
            user_name=str(author.get("username", "") or user_id),
            content=content,
            message_id=str(payload.get("id", "")),
            raw_event=payload,
        )
        await self.bus.publish_inbound(inbound)
        _logger.info("QQBot group: group=%s from=%s content=%.60s", group_openid, user_id, content)

    @staticmethod
    def _strip_at_mention(content: str) -> str:
        """去掉官方 Bot 消息中的 <@!xxx> @提及。"""
        import re
        return re.sub(r"<@!\d+>", "", content).strip()

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------

    async def send_message(
        self,
        *,
        chat_id: str,
        content: str,
        chat_type: str | None = None,
    ) -> dict[str, Any]:
        """发送文本消息（HTTP POST）。"""
        await self._ensure_token()
        assert self._http is not None

        headers = {
            "Authorization": f"QQBot {self._access_token}",
            "Content-Type": "application/json",
        }
        msg_id = self._generate_msg_id()

        if chat_id.startswith("g"):
            # 群聊
            group_openid = chat_id[1:]
            body = {
                "content": content,
                "msg_type": 0,
                "msg_id": msg_id,
                "msg_seq": 1,
            }
            url = f"{_QQ_API_BASE}/v2/groups/{group_openid}/messages"
        else:
            # 私聊
            body = {
                "content": content,
                "msg_type": 0,
                "msg_id": msg_id,
            }
            url = f"{_QQ_API_BASE}/v2/users/{chat_id}/messages"

        resp = await self._http.post(url, json=body, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            _logger.warning(
                "QQBot send failed: status=%s body=%s",
                resp.status_code, resp.text[:200],
            )
            raise
        return resp.json()

    async def send_image(
        self,
        *,
        chat_id: str,
        image_data: bytes,
        filename: str = "image.png",
    ) -> str:
        """上传并发送图片。返回 file_info 标识符。"""
        await self._ensure_token()
        assert self._http is not None

        # 1. 上传图片
        mime = guess_mime_type(filename)
        target = "users" if not chat_id.startswith("g") else "groups"
        target_id = chat_id[1:] if chat_id.startswith("g") else chat_id

        upload_url = f"{_QQ_API_BASE}/v2/{target}/{target_id}/files"

        # QQ Bot upload uses multipart form
        files = {
            "file": (filename, image_data, mime),
        }
        data = {"file_type": "1"}  # 1 = 图片

        resp = await self._http.post(
            upload_url,
            data=data,
            files=files,
            headers={"Authorization": f"QQBot {self._access_token}"},
        )
        resp.raise_for_status()
        result = resp.json()
        file_info = result.get("file_info", "")
        _logger.info("QQBot image uploaded: file_info=%.50s", file_info)
        return file_info

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_msg_id() -> str:
        """生成唯一消息 ID。"""
        import uuid
        return uuid.uuid4().hex[:32]

    # ------------------------------------------------------------------
    # 出站回调
    # ------------------------------------------------------------------

    async def _outbound_callback(self, msg: OutboundMessage) -> None:
        """MessageBus 出站回调 → send_message。"""
        await self.send_message(chat_id=msg.chat_id, content=msg.content)
