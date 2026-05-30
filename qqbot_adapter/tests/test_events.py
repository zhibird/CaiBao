"""测试 SSEEvent 解析、InboundMessage / OutboundMessage 数据结构。"""

import json

import pytest

from qqbot_adapter.core.events import InboundMessage, OutboundMessage, SSEEvent


class TestSSEEvent:
    """SSE 事件解析测试。"""

    def test_from_sse_line_parses_complete_event(self) -> None:
        raw = (
            'event: llm.delta\n'
            'data: {"event":"llm.delta","run_id":"abc-123","seq":5,"payload":{"text":"你好"}}\n'
            '\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "llm.delta"
        assert event.run_id == "abc-123"
        assert event.seq == 5
        assert event.payload == {"text": "你好"}

    def test_from_sse_line_uses_data_field_event_when_no_event_line(self) -> None:
        raw = (
            'data: {"event":"tool.started","run_id":"x","seq":1,"payload":{"tool_name":"search"}}\n'
            '\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "tool.started"

    def test_from_sse_line_returns_none_for_empty_data(self) -> None:
        event = SSEEvent.from_sse_line("event: empty\n\n")
        assert event is None

    def test_from_sse_line_returns_none_for_invalid_json(self) -> None:
        event = SSEEvent.from_sse_line("data: not valid json\n\n")
        assert event is None

    def test_from_sse_line_handles_multi_line_sse_block(self) -> None:
        raw = (
            'event: run.completed\n'
            'data: {"event":"run.completed","run_id":"r1","seq":10,"payload":{"answer":"Done"}}\n'
            '\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "run.completed"

    def test_from_sse_line_handles_missing_event_field_in_data(self) -> None:
        raw = 'data: {"run_id":"r1","seq":1,"payload":{}}\n\n'
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == ""  # 无 event 字段时为空字符串

    def test_from_sse_line_handles_confirmation_required(self) -> None:
        payload = {"tool_name": "create_incident", "arguments": {"title": "DB告警"}}
        data = json.dumps({
            "event": "confirmation.required",
            "run_id": "run-confirm",
            "seq": 7,
            "payload": payload,
        })
        raw = f"event: confirmation.required\ndata: {data}\n\n"
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "confirmation.required"
        assert event.payload["tool_name"] == "create_incident"

    def test_from_sse_line_ignores_comment_lines(self) -> None:
        raw = (
            ': this is a comment\n'
            'data: {"event":"run.failed","run_id":"r1","seq":1,"payload":{"error":"boom"}}\n'
            '\n'
        )
        event = SSEEvent.from_sse_line(raw)
        assert event is not None
        assert event.event == "run.failed"


class TestInboundMessage:
    """入站消息数据结构测试。"""

    def test_defaults(self) -> None:
        msg = InboundMessage(
            channel_type="napcat",
            chat_type="private",
            chat_id="123456",
            user_id="123456",
            user_name="测试用户",
            content="你好",
        )
        assert msg.images == []
        assert msg.message_id == ""
        assert msg.raw_event == {}


class TestOutboundMessage:
    """出站消息数据结构测试。"""

    def test_defaults(self) -> None:
        msg = OutboundMessage(
            channel_type="napcat",
            chat_id="123456",
            content="回复内容",
        )
        assert msg.reply_to is None
        assert msg.tool_status is None
