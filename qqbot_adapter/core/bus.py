"""MessageBus：通道层与 Agent 层的隔离层。

设计参考 akashic-agent 的 bus/queue.py：
- 入站队列：通道 → put → Agent Bridge get（阻塞等待）
- 出站队列：Agent Bridge put → dispatch_outbound 后台任务 → 路由到对应通道回调
- 订阅模式：每个通道注册自己的发送回调，Bus 按 channel_type 路由
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .events import InboundMessage, OutboundMessage

_logger = logging.getLogger(__name__)

# 回调签名: async def callback(msg: OutboundMessage) -> None
OutboundCallback = Callable[[OutboundMessage], Any]


class MessageBus:
    """异步消息总线，解耦通道层和 Agent 桥接层。"""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._subscribers: dict[str, OutboundCallback] = {}
        self._running = False
        self._dispatch_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Channel → Bus（入站）
    # ------------------------------------------------------------------

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """通道收到 QQ 消息后调用，放入入站队列。"""
        await self._inbound.put(msg)
        _logger.debug(
            "Inbound queued: channel=%s chat=%s user=%s content=%.60s",
            msg.channel_type, msg.chat_id, msg.user_id, msg.content,
        )

    async def consume_inbound(self) -> InboundMessage:
        """Agent Bridge 调用，阻塞等待下一条入站消息。"""
        return await self._inbound.get()

    # ------------------------------------------------------------------
    # Agent Bridge → Bus（出站）
    # ------------------------------------------------------------------

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Agent Bridge 生成回复后调用，放入出站队列。"""
        await self._outbound.put(msg)
        _logger.debug(
            "Outbound queued: channel=%s chat=%s content=%.60s",
            msg.channel_type, msg.chat_id, msg.content,
        )

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------

    def subscribe_outbound(self, channel_type: str, callback: OutboundCallback) -> None:
        """通道注册自己的发送回调。

        同一个 channel_type 多次注册会覆盖。
        """
        self._subscribers[channel_type] = callback
        _logger.info("Outbound subscriber registered: channel=%s", channel_type)

    def unsubscribe_outbound(self, channel_type: str) -> None:
        """取消通道的出站订阅。"""
        self._subscribers.pop(channel_type, None)
        _logger.info("Outbound subscriber removed: channel=%s", channel_type)

    # ------------------------------------------------------------------
    # 后台分发循环
    # ------------------------------------------------------------------

    async def dispatch_outbound(self) -> None:
        """后台任务：从出站队列取消息，路由到对应通道的回调。

        容错设计（参考 akashic-agent）：
        1. 首次发送失败 → 记录警告，重试 1 次（2s 后）
        2. 重试仍失败 → 记录错误，消息丢失（不阻塞后续消息）
        """
        while self._running:
            try:
                msg = await asyncio.wait_for(self._outbound.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            callback = self._subscribers.get(msg.channel_type)
            if callback is None:
                _logger.warning(
                    "No subscriber for channel_type=%s, message dropped: %.60s",
                    msg.channel_type, msg.content,
                )
                continue

            try:
                result = callback(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _logger.warning(
                    "Outbound send failed for channel=%s, retrying in 2s...",
                    msg.channel_type, exc_info=True,
                )
                await asyncio.sleep(2.0)
                try:
                    result = callback(msg)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    _logger.error(
                        "Outbound send failed permanently for channel=%s chat=%s, message lost.",
                        msg.channel_type, msg.chat_id, exc_info=True,
                    )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动后台分发循环。"""
        if self._running:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self.dispatch_outbound())
        _logger.info("MessageBus started")

    async def stop(self) -> None:
        """停止后台分发循环。"""
        self._running = False
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None
        _logger.info("MessageBus stopped")

    # ------------------------------------------------------------------
    # 监控
    # ------------------------------------------------------------------

    @property
    def inbound_size(self) -> int:
        """入站队列当前长度。"""
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """出站队列当前长度。"""
        return self._outbound.qsize()
