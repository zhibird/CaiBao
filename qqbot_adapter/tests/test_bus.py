"""测试 MessageBus 异步消息总线。"""

import asyncio

import pytest

from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage, OutboundMessage


def _make_inbound(user_id: str = "123", content: str = "test") -> InboundMessage:
    return InboundMessage(
        channel_type="napcat",
        chat_type="private",
        chat_id=user_id,
        user_id=user_id,
        user_name="tester",
        content=content,
    )


def _make_outbound(chat_id: str = "123", content: str = "reply") -> OutboundMessage:
    return OutboundMessage(
        channel_type="napcat",
        chat_id=chat_id,
        content=content,
    )


class TestMessageBus:
    """MessageBus 单元测试。"""

    @pytest.mark.asyncio
    async def test_publish_and_consume_inbound(self) -> None:
        bus = MessageBus()
        msg = _make_inbound()
        await bus.publish_inbound(msg)
        consumed = await bus.consume_inbound()
        assert consumed.user_id == msg.user_id
        assert consumed.content == msg.content

    @pytest.mark.asyncio
    async def test_consume_inbound_blocks_until_message(self) -> None:
        bus = MessageBus()

        async def delayed_publish() -> None:
            await asyncio.sleep(0.05)
            await bus.publish_inbound(_make_inbound(content="delayed"))

        task = asyncio.create_task(delayed_publish())
        # 应该阻塞直到 delayed_publish 完成
        consumed = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
        assert consumed.content == "delayed"
        await task

    @pytest.mark.asyncio
    async def test_dispatch_outbound_routes_to_subscriber(self) -> None:
        bus = MessageBus()
        received: list[OutboundMessage] = []

        async def callback(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("napcat", callback)
        await bus.start()

        msg = _make_outbound(content="hello")
        await bus.publish_outbound(msg)

        # 等待分发
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 1
        assert received[0].content == "hello"

    @pytest.mark.asyncio
    async def test_dispatch_outbound_drops_message_without_subscriber(self) -> None:
        bus = MessageBus()
        await bus.start()

        msg = _make_outbound()
        msg.channel_type = "nonexistent"
        await bus.publish_outbound(msg)

        await asyncio.sleep(0.1)
        await bus.stop()
        # 消息被丢弃，不抛异常

    @pytest.mark.asyncio
    async def test_subscribe_overwrites_previous(self) -> None:
        bus = MessageBus()
        received1: list[OutboundMessage] = []
        received2: list[OutboundMessage] = []

        async def cb1(msg: OutboundMessage) -> None:
            received1.append(msg)

        async def cb2(msg: OutboundMessage) -> None:
            received2.append(msg)

        bus.subscribe_outbound("napcat", cb1)
        bus.subscribe_outbound("napcat", cb2)  # 覆盖
        await bus.start()

        await bus.publish_outbound(_make_outbound())
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received1) == 0  # 被覆盖
        assert len(received2) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_callback(self) -> None:
        bus = MessageBus()
        received: list[OutboundMessage] = []

        async def cb(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("napcat", cb)
        bus.unsubscribe_outbound("napcat")
        await bus.start()

        await bus.publish_outbound(_make_outbound())
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_outbound_retry_on_failure(self) -> None:
        bus = MessageBus()
        call_count = 0

        async def failing_callback(msg: OutboundMessage) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")
            # second attempt succeeds silently

        bus.subscribe_outbound("napcat", failing_callback)
        await bus.start()

        await bus.publish_outbound(_make_outbound(content="retry test"))
        # Bus 会在首次失败后 sleep 2s 再重试，等待足够时间
        await asyncio.sleep(2.5)
        await bus.stop()

        assert call_count >= 2  # 至少尝试 2 次（首次 + 重试）

    @pytest.mark.asyncio
    async def test_inbound_and_outbound_queue_sizes(self) -> None:
        bus = MessageBus()
        assert bus.inbound_size == 0
        assert bus.outbound_size == 0

        await bus.publish_inbound(_make_inbound())
        assert bus.inbound_size == 1

        await bus.consume_inbound()
        assert bus.inbound_size == 0

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        bus = MessageBus()
        await bus.start()
        await bus.stop()
        await bus.stop()  # 第二次不应抛异常
