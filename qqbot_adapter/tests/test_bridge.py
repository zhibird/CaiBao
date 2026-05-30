"""测试 AgentBridge 核心逻辑（不依赖外部 API）。"""

import pytest

from qqbot_adapter.core.bridge import AgentBridge
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage, SSEEvent


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
