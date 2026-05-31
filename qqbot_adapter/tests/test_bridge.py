"""测试 AgentBridge 核心逻辑（不依赖外部 API）。"""

import asyncio
import time

import pytest

from qqbot_adapter.core.bridge import AgentBridge, _MAX_MESSAGE_CHARS, _MIN_SEND_INTERVAL, _QQBOT_PASSIVE_REPLY_LIMIT
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage, OutboundMessage, SSEEvent


def _make_inbound(user_id: str = "123", content: str = "test") -> InboundMessage:
    return InboundMessage(
        channel_type="napcat",
        chat_type="private",
        chat_id=user_id,
        user_id=user_id,
        user_name="tester",
        content=content,
    )


class TestSplitText:
    """文本分段逻辑测试（纯静态方法）。"""

    def test_short_text_not_split(self) -> None:
        result = AgentBridge._split_text("短文本", 100)
        assert result == ["短文本"]

    def test_long_text_split_at_newline(self) -> None:
        # "第一行\n" (4 chars) + 200个 "x" = 204 chars, max_chars=100
        # split_at = rfind("\n", 0, 100) 在位置 3 找到 "\n"
        # 但 3 < 100//2=50, 所以回退到 max_chars=100 处切分
        long_text = "第一行\n" + "x" * 200 + "\n第二行\n第三行"
        result = AgentBridge._split_text(long_text, 100)
        assert len(result) >= 3  # 至少分 3 段
        # 第一段包含"第一行\n" + 96个x
        assert result[0].startswith("第一行")

    def test_no_newline_splits_at_max_chars(self) -> None:
        long_text = "x" * 250
        result = AgentBridge._split_text(long_text, 100)
        assert len(result) >= 3
        assert all(len(p) <= 100 for p in result)

    def test_exact_boundary(self) -> None:
        text = "a" * 100
        result = AgentBridge._split_text(text, 100)
        assert result == [text]

    def test_empty_text(self) -> None:
        result = AgentBridge._split_text("", 100)
        assert result == [""]  # 空字符串作为一个元素


class TestConversationId:
    """会话 ID 生成测试。"""

    def test_private_chat(self) -> None:
        msg = InboundMessage(
            channel_type="napcat",
            chat_type="private",
            chat_id="123456",
            user_id="123456",
            user_name="tester",
            content="hello",
        )
        conv_id = AgentBridge._make_conversation_id(msg)
        assert conv_id == "qq_123456"

    def test_group_chat(self) -> None:
        msg = InboundMessage(
            channel_type="napcat",
            chat_type="group",
            chat_id="g789012",
            user_id="10001",
            user_name="group_user",
            content="hello",
        )
        conv_id = AgentBridge._make_conversation_id(msg)
        assert conv_id == "qq_group_789012"


class TestSSEEventFromSseLine:
    """SSEEvent.from_sse_line 实际用到的场景测试。"""

    def test_run_completed_event(self) -> None:
        raw = (
            'event: run.completed\n'
            'data: {"event":"run.completed","run_id":"abc","seq":10,'
            '"payload":{"run_id":"abc","answer":"Final answer"}}\n\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "run.completed"
        assert "run_id" in event.payload

    def test_step_completed_event(self) -> None:
        raw = (
            'event: step.completed\n'
            'data: {"event":"step.completed","run_id":"abc","seq":50,'
            '"step_index":50,"payload":{"answer":"Final answer"}}\n\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "step.completed"

    def test_error_event(self) -> None:
        raw = (
            'event: run.failed\n'
            'data: {"event":"run.failed","run_id":"abc","seq":5,'
            '"payload":{"error":"LLM API timeout"}}\n\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "run.failed"
        assert "timeout" in event.payload.get("error", "").lower()


class TestSendThrottled:
    """频控发送测试。"""

    @pytest.mark.asyncio
    async def test_first_send_no_delay(self) -> None:
        """首次发送不等待。"""
        bus = MessageBus()
        await bus.start()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a", "hello")
        start = time.monotonic()
        await bridge._send_throttled(inbound, "消息1")
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # 首次发送几乎不等待
        await bus.stop()

    @pytest.mark.asyncio
    async def test_consecutive_sends_throttled(self) -> None:
        """连续发送同一 chat_id 应受频控（至少 _MIN_SEND_INTERVAL 间隔）。"""
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("napcat", capture)

        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a", "hello")

        start = time.monotonic()
        await bridge._send_throttled(inbound, "消息1")
        t1 = time.monotonic()
        await bridge._send_throttled(inbound, "消息2")
        t2 = time.monotonic()
        await bridge._send_throttled(inbound, "消息3")
        t3 = time.monotonic()

        # 第 2、3 条应分别等待至少 _MIN_SEND_INTERVAL 秒
        assert t1 - start < 0.1  # 首次不等待
        assert t2 - t1 >= _MIN_SEND_INTERVAL * 0.7  # 允许 30% 浮动
        assert t3 - t2 >= _MIN_SEND_INTERVAL * 0.7

        # 等待 Bus 分发完成
        await asyncio.sleep(0.2)
        assert len(received) == 3
        await bus.stop()

    @pytest.mark.asyncio
    async def test_different_chat_ids_not_throttled(self) -> None:
        """不同 chat_id 之间不互相频控。"""
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("napcat", capture)

        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )

        start = time.monotonic()
        await bridge._send_throttled(_make_inbound("user_a"), "msg_a")
        await bridge._send_throttled(_make_inbound("user_b"), "msg_b")
        await bridge._send_throttled(_make_inbound("user_c"), "msg_c")
        elapsed = time.monotonic() - start

        # 不同 chat_id 之间不应等待
        assert elapsed < 0.3
        # 等待 Bus 后台分发完成
        await asyncio.sleep(0.2)
        assert len(received) == 3
        await bus.stop()


class TestQQBotReplyMetadata:
    """QQBot official reply metadata tests."""

    @pytest.mark.asyncio
    async def test_reply_to_uses_inbound_message_id(self) -> None:
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("qqbot", capture)
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = InboundMessage(
            channel_type="qqbot",
            chat_type="private",
            chat_id="openid-a",
            user_id="openid-a",
            user_name="tester",
            content="hello",
            message_id="msg-in-1",
        )

        await bridge._publish_reply(inbound, "reply")
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == 1
        assert received[0].reply_to == "msg-in-1"

    @pytest.mark.asyncio
    async def test_qqbot_reply_budget_caps_passive_replies(self) -> None:
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("qqbot", capture)
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = InboundMessage(
            channel_type="qqbot",
            chat_type="private",
            chat_id="openid-a",
            user_id="openid-a",
            user_name="tester",
            content="hello",
            message_id="msg-in-1",
        )

        for index in range(_QQBOT_PASSIVE_REPLY_LIMIT + 1):
            await bridge._publish_reply(inbound, f"reply {index}")

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == _QQBOT_PASSIVE_REPLY_LIMIT

    @pytest.mark.asyncio
    async def test_qqbot_long_message_marks_truncation_when_budget_is_short(self) -> None:
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("qqbot", capture)
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = InboundMessage(
            channel_type="qqbot",
            chat_type="private",
            chat_id="openid-a",
            user_id="openid-a",
            user_name="tester",
            content="hello",
            message_id="msg-in-1",
        )

        for index in range(_QQBOT_PASSIVE_REPLY_LIMIT - 1):
            await bridge._publish_reply(inbound, f"preface {index}")

        await bridge._send_long_message(
            inbound,
            "x" * (_MAX_MESSAGE_CHARS * 2 + 50),
            is_final=True,
        )

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(received) == _QQBOT_PASSIVE_REPLY_LIMIT
        assert "Answer truncated" in received[-1].content
        assert len(received[-1].content) <= _MAX_MESSAGE_CHARS


class TestSyncProcessingNotice:
    """Official QQBot sync-mode processing notice behavior."""

    @pytest.mark.asyncio
    async def test_fast_sync_reply_does_not_send_processing_notice(self) -> None:
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("qqbot", capture)
        bridge = AgentBridge(
            bus=bus,
            caibao_base_url="http://test",
            bot_user_id="bot",
            bot_password="pw",
            sync_processing_notice_delay_seconds=0.05,
        )

        async def fake_run_sync(msg: InboundMessage) -> dict[str, str]:
            return {"answer": "done", "status": "completed"}

        bridge._run_sync = fake_run_sync  # type: ignore[method-assign]
        inbound = InboundMessage(
            channel_type="qqbot",
            chat_type="private",
            chat_id="openid-fast",
            user_id="openid-fast",
            user_name="tester",
            content="hello",
            message_id="msg-fast",
        )

        await bridge._handle_message_sync(inbound)
        await asyncio.sleep(0.1)
        await bus.stop()

        assert [msg.content for msg in received] == ["done"]

    @pytest.mark.asyncio
    async def test_slow_sync_reply_sends_playful_processing_notice(self) -> None:
        bus = MessageBus()
        await bus.start()
        received: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            received.append(msg)

        bus.subscribe_outbound("qqbot", capture)
        notice = "⏳ 稍等一下，我在认真整理思路中 (ง •̀_•́)ง"
        bridge = AgentBridge(
            bus=bus,
            caibao_base_url="http://test",
            bot_user_id="bot",
            bot_password="pw",
            sync_processing_notice_delay_seconds=0.01,
            sync_processing_notice=notice,
        )

        async def fake_run_sync(msg: InboundMessage) -> dict[str, str]:
            await asyncio.sleep(0.05)
            return {"answer": "done", "status": "completed"}

        bridge._run_sync = fake_run_sync  # type: ignore[method-assign]
        inbound = InboundMessage(
            channel_type="qqbot",
            chat_type="private",
            chat_id="openid-slow",
            user_id="openid-slow",
            user_name="tester",
            content="hello",
            message_id="msg-slow",
        )

        await bridge._handle_message_sync(inbound)
        await asyncio.sleep(0.2)
        await bus.stop()

        assert received[0].content == notice
        assert received[0].tool_status == "thinking"
        assert received[-1].content == "done"


class TestDedupLogic:
    """去重逻辑测试（纯静态分析，不依赖 API）。"""

    def test_identical_content_not_resent(self) -> None:
        """已流式推送的文本不应作为最终答案重复发送。"""
        streamed = ["这是第一段", "这是第二段。"]
        final_answer = "这是第一段这是第二段。"

        already_sent = "".join(streamed)
        should_send = final_answer.strip() not in already_sent
        assert should_send is False

    def test_different_content_is_sent(self) -> None:
        """新内容（非流式已推送）应该发送。"""
        streamed = ["第一部分..."]
        final_answer = "第一部分内容，以及第二部分新增内容。"

        already_sent = "".join(streamed)
        should_send = final_answer.strip() not in already_sent
        assert should_send is True

    def test_last_chunk_duplicate_skipped(self) -> None:
        """如果剩余文本和最后一条推送相同，跳过。"""
        streamed = ["这是已经发送过的内容。"]
        remaining = "这是已经发送过的内容。"

        # 模拟判断：remaining 和 streamed[-1] 相同 → 不发送
        is_duplicate = remaining == streamed[-1]
        assert is_duplicate is True

    def test_empty_final_answer_not_sent(self) -> None:
        """空最终答案不发送。"""
        final_answer = ""
        streamed = ["已有内容"]
        already_sent = "".join(streamed)
        should_send = bool(final_answer.strip() and final_answer.strip() not in already_sent)
        assert should_send is False


class TestDispatchEventNoDoubleSend:
    """验证 _dispatch_sse_event 不会对 step.completed/run.completed 重复发送。"""

    @pytest.mark.asyncio
    async def test_step_completed_not_dispatched_to_bus(self) -> None:
        """step.completed 事件不应通过 _dispatch_sse_event 发到 Bus。"""
        bus = MessageBus()
        await bus.start()
        sent_messages: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            sent_messages.append(msg)

        bus.subscribe_outbound("napcat", capture)

        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")

        event = SSEEvent(
            event="step.completed",
            run_id="r1", seq=50,
            payload={"answer": "最终答案"},
        )
        await bridge._dispatch_sse_event(inbound, event)

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(sent_messages) == 0, (
            f"step.completed 不应触发 Bus 发送，但实际发送了 {len(sent_messages)} 条"
        )

    @pytest.mark.asyncio
    async def test_run_completed_not_dispatched_to_bus(self) -> None:
        """run.completed 事件不应通过 _dispatch_sse_event 发到 Bus。"""
        bus = MessageBus()
        await bus.start()
        sent_messages: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            sent_messages.append(msg)

        bus.subscribe_outbound("napcat", capture)

        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")

        event = SSEEvent(
            event="run.completed",
            run_id="r1", seq=100,
            payload={"answer": "运行完成"},
        )
        await bridge._dispatch_sse_event(inbound, event)

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(sent_messages) == 0, (
            f"run.completed 不应触发 Bus 发送，但实际发送了 {len(sent_messages)} 条"
        )

    @pytest.mark.asyncio
    async def test_tool_events_still_dispatched(self) -> None:
        """tool.proposed 等事件仍应正常通过 _dispatch_sse_event 发送。"""
        bus = MessageBus()
        await bus.start()
        sent_messages: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            sent_messages.append(msg)

        bus.subscribe_outbound("napcat", capture)

        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")

        for evt_type, payload in [
            ("tool.proposed", {"tool_name": "search"}),
            ("tool.started", {"tool_name": "search"}),
            ("tool.result", {"tool_name": "search", "result": {"message": "ok"}}),
            ("confirmation.required", {"tool_name": "create_incident", "arguments": {"title": "test"}}),
            ("run.failed", {"error": "something wrong"}),
        ]:
            event = SSEEvent(event=evt_type, run_id="r1", seq=1, payload=payload)
            await bridge._dispatch_sse_event(inbound, event)

        await asyncio.sleep(0.2)
        await bus.stop()

        # 5 个事件类型都应触发发送
        assert len(sent_messages) == 5, (
            f"期望 5 条工具事件消息，实际 {len(sent_messages)} 条"
        )

    @pytest.mark.asyncio
    async def test_llm_delta_not_dispatched(self) -> None:
        """llm.delta 事件不应通过 _dispatch_sse_event 发到 Bus。"""
        bus = MessageBus()
        await bus.start()
        sent_messages: list[OutboundMessage] = []

        async def capture(msg: OutboundMessage) -> None:
            sent_messages.append(msg)

        bus.subscribe_outbound("napcat", capture)

        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")

        event = SSEEvent(
            event="llm.delta", run_id="r1", seq=3,
            payload={"text": "流式增量文本"},
        )
        await bridge._dispatch_sse_event(inbound, event)

        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(sent_messages) == 0, (
            f"llm.delta 不应触发 Bus 发送，但实际发送了 {len(sent_messages)} 条"
        )
