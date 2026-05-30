"""BaseChannel：所有 QQ 通道实现的抽象基类。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from qqbot_adapter.core.bus import MessageBus

_logger = logging.getLogger(__name__)


class BaseChannel(ABC):
    """通道基类。

    子类需要实现：
    - start(): 建立连接，注册事件处理，注册出站回调
    - stop(): 断开连接，清理资源
    - send_message(): 发送文本消息的具体实现
    """

    channel_type: str = ""  # 子类定义，如 "napcat", "qqbot"

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    @abstractmethod
    async def start(self) -> None:
        """启动通道：建立连接 + 注册事件 + 注册出站回调。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止通道：断开连接 + 清理。"""
        ...

    @abstractmethod
    async def send_message(
        self,
        *,
        chat_id: str,
        content: str,
        chat_type: str | None = None,
    ) -> None:
        """发送文本消息到指定 chat_id。"""
        ...

    def _outbound_callback(self, msg):
        """默认出站回调：收到 OutboundMessage 时调用 send_message。"""
        return self.send_message(
            chat_id=msg.chat_id,
            content=msg.content,
        )
