"""NapCat 通道：通过 OneBot v11 WebSocket 协议连接 QQ。

NapCat (https://github.com/NapNeko/NapCatQQ) 是 QQ Bot 运行时，
启动后暴露一个 OneBot v11 正向 WebSocket 端口，本适配器作为客户端连接。

协议要点：
- 连接：ws://{host}:{port} （NapCat 默认 ws://127.0.0.1:3001）
- 事件接收：服务器推送 JSON 格式的事件（message.private, message.group 等）
- API 调用：客户端发送 {"action": "...", "params": {...}, "echo": "..."}
  服务器返回 {"status": "ok"|"failed", "retcode": 0, "data": {...}, "echo": "..."}
- 认证：NapCat 可配置 access_token，通过 HTTP Header "Authorization: Bearer {token}" 传递

设计要点：
- 异步 WebSocket 长连接
- 自动重连（指数退避）
- 心跳保活
- 私聊 + 群聊 @ 触发
- 白名单过滤
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from qqbot_adapter.channels.base import BaseChannel
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage, OutboundMessage

_logger = logging.getLogger(__name__)

# 重连参数
_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_BACKOFF = 2.0

# CQ 码正则：提取纯文本、图片等
_CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*url=([^,\]]+)[^\]]*\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)[^\]]*\]")
_CQ_FACE_RE = re.compile(r"\[CQ:face,id=(\d+)[^\]]*\]")
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]+\]")


class NapCatChannel(BaseChannel):
    """NapCat / OneBot v11 WebSocket 通道。"""

    channel_type = "napcat"

    def __init__(
        self,
        bus: MessageBus,
        *,
        ws_url: str = "ws://127.0.0.1:3001",
        access_token: str | None = None,
        allow_from: list[str] | None = None,
        groups: list[dict[str, Any]] | None = None,
        reconnect: bool = True,
    ) -> None:
        super().__init__(bus)
        self.ws_url = ws_url
        self.access_token = access_token
        self.allow_from: set[str] = set(allow_from or [])
        self.groups: dict[str, dict[str, Any]] = {}
        if groups:
            for g in groups:
                gid = str(g.get("group_id", ""))
                if gid:
                    self.groups[gid] = g

        self.reconnect_enabled = reconnect
        self._ws: ClientConnection | None = None
        self._echo_counter = 0
        self._running = False
        self._connect_task: asyncio.Task[None] | None = None
        self._pending_echo: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动通道：连接 WebSocket → 注册出站回调 → 开始消息循环。"""
        self._running = True

        # 注册出站回调：Bus 的 OutboundMessage → send_message
        self.bus.subscribe_outbound(self.channel_type, self._outbound_callback)

        # 连接循环（自动重连），保存 task 以便 stop 时取消
        self._connect_task = asyncio.create_task(self._connect_loop())

        _logger.info("NapCatChannel started (url=%s)", self.ws_url)

    async def stop(self) -> None:
        """停止通道：取消连接任务 → 关闭 WebSocket → 取消出站订阅。"""
        self._running = False

        # 取消后台连接任务
        if self._connect_task is not None:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
            self._connect_task = None

        self.bus.unsubscribe_outbound(self.channel_type)

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        _logger.info("NapCatChannel stopped")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        """连接循环：不断尝试连接，断开后自动重连。"""
        delay = _RECONNECT_BASE_DELAY
        while self._running:
            try:
                await self._connect()
                delay = _RECONNECT_BASE_DELAY  # 重置退避
                await self._message_loop()
            except websockets.ConnectionClosed as exc:
                _logger.warning("WebSocket closed: code=%s reason=%s", exc.code, exc.reason)
            except OSError as exc:
                _logger.warning("WebSocket connection failed: %s", exc)
            except Exception:
                _logger.exception("Unexpected error in connection loop")

            if not self._running or not self.reconnect_enabled:
                break

            _logger.info("Reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * _RECONNECT_BACKOFF, _RECONNECT_MAX_DELAY)

    async def _connect(self) -> None:
        """建立 WebSocket 连接。"""
        extra_headers: dict[str, str] = {}
        if self.access_token:
            extra_headers["Authorization"] = f"Bearer {self.access_token}"

        self._ws = await websockets.connect(
            self.ws_url,
            additional_headers=extra_headers if extra_headers else None,
            ping_interval=20.0,
            ping_timeout=10.0,
            close_timeout=5.0,
            max_size=2**20,  # 1MB max message
        )
        _logger.info("Connected to NapCat at %s", self.ws_url)

    async def _message_loop(self) -> None:
        """消息循环：接收 WebSocket 消息 → 分发处理。"""
        assert self._ws is not None
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                _logger.debug("Non-JSON message received: %.100s", raw)
                continue

            await self._dispatch(data)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        """分发 OneBot 消息。"""
        post_type = data.get("post_type", "")

        if post_type == "message":
            await self._handle_message_event(data)
        elif post_type == "meta_event":
            await self._handle_meta_event(data)
        elif "echo" in data:
            # API 调用响应
            self._handle_api_response(data)

    # ------------------------------------------------------------------
    # 消息事件处理
    # ------------------------------------------------------------------

    async def _handle_message_event(self, data: dict[str, Any]) -> None:
        """处理 message.private / message.group 事件。"""
        message_type = data.get("message_type", "")

        if message_type == "private":
            await self._on_private_message(data)
        elif message_type == "group":
            await self._on_group_message(data)

    async def _on_private_message(self, data: dict[str, Any]) -> None:
        """处理私聊消息。"""
        sender = data.get("sender", {})
        user_id = str(data.get("user_id", ""))
        user_name = sender.get("nickname", "") or user_id

        # 白名单检查
        if self.allow_from and user_id not in self.allow_from:
            _logger.debug("Private message from %s blocked (not in allow_from)", user_id)
            return

        raw_message = str(data.get("message", ""))
        content = self._extract_text(raw_message)

        if not content.strip():
            return

        inbound = InboundMessage(
            channel_type=self.channel_type,
            chat_type="private",
            chat_id=user_id,
            user_id=user_id,
            user_name=user_name,
            content=content,
            images=self._extract_image_urls(raw_message),
            message_id=str(data.get("message_id", "")),
            raw_event=data,
        )

        await self.bus.publish_inbound(inbound)
        _logger.info(
            "Private message: from=%s(%s) content=%.60s",
            user_name, user_id, content,
        )

    async def _on_group_message(self, data: dict[str, Any]) -> None:
        """处理群聊消息（仅响应 @机器人 的消息）。"""
        group_id = str(data.get("group_id", ""))

        # 群配置检查
        group_config = self.groups.get(group_id)
        if group_config is None:
            # 没有配置该群，忽略
            return

        # 白名单检查（群成员）
        sender = data.get("sender", {})
        user_id = str(sender.get("user_id", ""))
        allow_from = group_config.get("allow_from", [])
        if allow_from and user_id not in allow_from:
            return

        # @ 触发检查
        if group_config.get("require_at", True):
            if not self._is_at_bot(data, group_id):
                return

        user_name = sender.get("nickname", "") or user_id
        raw_message = str(data.get("message", ""))
        content = self._extract_text(raw_message)

        if not content.strip():
            return

        chat_id = f"g{group_id}"
        inbound = InboundMessage(
            channel_type=self.channel_type,
            chat_type="group",
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            content=content,
            images=self._extract_image_urls(raw_message),
            message_id=str(data.get("message_id", "")),
            raw_event=data,
        )

        await self.bus.publish_inbound(inbound)
        _logger.info(
            "Group message: group=%s from=%s(%s) content=%.60s",
            group_id, user_name, user_id, content,
        )

    # ------------------------------------------------------------------
    # Meta 事件处理
    # ------------------------------------------------------------------

    async def _handle_meta_event(self, data: dict[str, Any]) -> None:
        """处理 heartbeat / lifecycle 等 meta 事件。"""
        meta_type = data.get("meta_event_type", "")
        if meta_type == "heartbeat":
            _logger.debug(
                "Heartbeat: status=%s interval=%s",
                data.get("status", {}).get("online"),
                data.get("interval"),
            )
        elif meta_type == "lifecycle":
            _logger.info(
                "Lifecycle: sub_type=%s self_id=%s",
                data.get("sub_type"), data.get("self_id"),
            )

    # ------------------------------------------------------------------
    # API 调用
    # ------------------------------------------------------------------

    async def send_message(
        self,
        *,
        chat_id: str,
        content: str,
        chat_type: str | None = None,
    ) -> dict[str, Any]:
        """发送文本消息。

        chat_id:
          - 私聊：QQ 号字符串
          - 群聊："g{群号}" 格式
        """
        if chat_id.startswith("g"):
            return await self._call_api(
                "send_group_msg",
                {"group_id": int(chat_id[1:]), "message": content, "auto_escape": False},
            )
        else:
            return await self._call_api(
                "send_private_msg",
                {"user_id": int(chat_id), "message": content, "auto_escape": False},
            )

    async def _call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """调用 OneBot API，返回响应 data。"""
        if self._ws is None:
            raise ConnectionError("WebSocket not connected")

        self._echo_counter += 1
        echo = f"caibao_{self._echo_counter}_{int(time.time() * 1000)}"

        request = {
            "action": action,
            "params": params,
            "echo": echo,
        }

        # 创建 Future 等待响应
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_echo[echo] = future

        try:
            await self._ws.send(json.dumps(request, ensure_ascii=False))
            _logger.debug("API call: %s params=%.120s echo=%s", action, params, echo)

            # 等待响应（超时 30s）
            result = await asyncio.wait_for(future, timeout=30.0)

            if result.get("status") == "failed":
                _logger.warning(
                    "API call failed: action=%s retcode=%s",
                    action, result.get("retcode"),
                )
            return result.get("data", result)
        except asyncio.TimeoutError:
            _logger.error("API call timeout: action=%s echo=%s", action, echo)
            return {"error": "timeout"}
        finally:
            self._pending_echo.pop(echo, None)

    def _handle_api_response(self, data: dict[str, Any]) -> None:
        """处理 API 响应，完成对应的 Future。"""
        echo = data.get("echo", "")
        if echo and echo in self._pending_echo:
            future = self._pending_echo[echo]
            if not future.done():
                future.set_result(data)

    # ------------------------------------------------------------------
    # 消息解析辅助
    # ------------------------------------------------------------------

    def _extract_text(self, raw: str) -> str:
        """从 CQ 码混合文本中提取纯文本。

        - [CQ:image,...] → 移除（图片单独处理）
        - [CQ:at,qq=xxx] → @xxx
        - [CQ:face,id=xxx] → [表情]
        - 其他 [CQ:xxx] → 移除
        """
        text = _CQ_IMAGE_RE.sub("", raw)
        text = _CQ_AT_RE.sub(r"@\1", text)
        text = _CQ_FACE_RE.sub("[表情]", text)
        text = _CQ_CODE_RE.sub("", text)
        return text.strip()

    def _extract_image_urls(self, raw: str) -> list[bytes]:
        """从 CQ 码中提取图片 URL 列表。

        Phase 1 暂不下载图片，仅记录 URL。
        """
        urls = _CQ_IMAGE_RE.findall(raw)
        if urls:
            _logger.debug("Images in message (not yet downloaded): %s", urls)
        return []  # Phase 1 暂不处理图片

    def _is_at_bot(self, data: dict[str, Any], group_id: str) -> bool:
        """检查群消息是否 @ 了机器人。"""
        raw_message = str(data.get("message", ""))
        self_id = str(data.get("self_id", ""))

        # OneBot 中 @ 机器人是 [CQ:at,qq={self_id}]
        at_pattern = f"[CQ:at,qq={self_id}]"
        if at_pattern in raw_message:
            return True

        # 有些实现用 @ 加 QQ 号
        if f"@{self_id}" in raw_message:
            return True

        return False

    # ------------------------------------------------------------------
    # 出站回调
    # ------------------------------------------------------------------

    async def _outbound_callback(self, msg: OutboundMessage) -> None:
        """MessageBus 出站回调：OutboundMessage → send_message。"""
        await self.send_message(
            chat_id=msg.chat_id,
            content=msg.content,
        )
