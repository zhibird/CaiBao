"""测试危险操作确认交互闭环。

覆盖：
- 确认关键词识别（确认/是/yes/ok/执行）
- 取消关键词识别（取消/否/no/跳过/不执行）
- 待确认状态生命周期（保存→确认→清除 / 保存→取消→清除）
- 新消息中断确认流程
- PendingConfirmation 数据完整性
"""

import pytest

from qqbot_adapter.core.bridge import (
    AgentBridge,
    PendingConfirmation,
    _CANCEL_KEYWORDS,
    _CONFIRM_KEYWORDS,
    _match_keyword,
)
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage


def _make_inbound(user_id: str = "123", content: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel_type="napcat",
        chat_type="private",
        chat_id=user_id,
        user_id=user_id,
        user_name="tester",
        content=content,
    )


class TestConfirmationKeywords:
    """确认/取消关键词匹配测试。"""

    def test_confirm_keywords_match(self) -> None:
        for kw in ["确认", "是", "yes", "ok", "执行"]:
            assert _match_keyword(kw, _CONFIRM_KEYWORDS), (
                f"'{kw}' 应该匹配确认关键词"
            )

    def test_cancel_keywords_match(self) -> None:
        for kw in ["取消", "否", "no", "跳过", "不执行"]:
            assert _match_keyword(kw, _CANCEL_KEYWORDS), (
                f"'{kw}' 应该匹配取消关键词"
            )

    def test_exact_match_keywords_no_false_positive(self) -> None:
        """ok/no/yes 需精确匹配，防止 'ok' in 'smoke'。"""
        assert not _match_keyword("smoke", _CONFIRM_KEYWORDS)
        assert not _match_keyword("broken", _CONFIRM_KEYWORDS)
        assert not _match_keyword("note", _CANCEL_KEYWORDS)
        assert not _match_keyword("yesterday", _CONFIRM_KEYWORDS)
        assert _match_keyword("ok", _CONFIRM_KEYWORDS)
        assert _match_keyword("no", _CANCEL_KEYWORDS)
        assert _match_keyword("yes", _CONFIRM_KEYWORDS)

    def test_chinese_keyword_substring_match(self) -> None:
        """中文关键词（确认/取消/是/否/执行）支持子串匹配。"""
        assert _match_keyword("确认执行", _CONFIRM_KEYWORDS)
        assert _match_keyword("好的，确认", _CONFIRM_KEYWORDS)
        assert _match_keyword("取消吧", _CANCEL_KEYWORDS)
        assert _match_keyword("好的，是", _CONFIRM_KEYWORDS)
        assert _match_keyword("算了否", _CANCEL_KEYWORDS)

    def test_mixed_case_matched(self) -> None:
        """大小写不敏感，精确匹配关键词。"""
        assert _match_keyword("YES", _CONFIRM_KEYWORDS)
        assert _match_keyword("Ok", _CONFIRM_KEYWORDS)
        assert _match_keyword("NO", _CANCEL_KEYWORDS)

    def test_exact_keyword_not_matched_in_sentence(self) -> None:
        """精确匹配关键词不出现在句子中（如 'yes' 不在 'yesterday'）。"""
        assert not _match_keyword("YES please", _CONFIRM_KEYWORDS)
        assert not _match_keyword("no thanks", _CANCEL_KEYWORDS)
        assert not _match_keyword("ok let me check", _CONFIRM_KEYWORDS)
        # 中文仍支持句子内匹配
        assert _match_keyword("好的，确认", _CONFIRM_KEYWORDS)
        assert _match_keyword("请取消这个", _CANCEL_KEYWORDS)

    def test_whitespace_trimmed(self) -> None:
        """前后空格被 trim。"""
        assert _match_keyword("  确认  ", _CONFIRM_KEYWORDS)
        assert _match_keyword(" ok ", _CONFIRM_KEYWORDS)

    def test_normal_message_not_confirm(self) -> None:
        """普通消息不匹配确认或取消。"""
        for text in ["你好", "帮我查一下", "今天天气", "123"]:
            assert not _match_keyword(text, _CONFIRM_KEYWORDS), f"'{text}' 误匹配确认"
            assert not _match_keyword(text, _CANCEL_KEYWORDS), f"'{text}' 误匹配取消"


class TestPendingConfirmation:
    """待确认状态数据结构测试。"""

    def test_pending_holds_run_id_and_tool(self) -> None:
        p = PendingConfirmation(
            run_id="run-001",
            tool_name="create_incident",
            arguments={"title": "DB告警", "severity": "P1"},
        )
        assert p.run_id == "run-001"
        assert p.tool_name == "create_incident"
        assert p.arguments["severity"] == "P1"

    def test_pending_has_created_at(self) -> None:
        """创建时间应自动设置为当前时间。"""
        import time
        before = time.monotonic()
        p = PendingConfirmation(run_id="r1", tool_name="test")
        after = time.monotonic()
        assert before <= p.created_at <= after


class TestSaveAndClearConfirmation:
    """待确认状态保存/清除测试。"""

    def test_save_pending_confirmation(self) -> None:
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a", "查一下数据库")
        bridge._save_pending_confirmation(
            inbound=inbound,
            run_id="run-001",
            tool_name="create_incident",
            arguments={"title": "DB告警"},
        )
        assert "user_a" in bridge._pending_confirmations
        assert bridge._pending_confirmations["user_a"].run_id == "run-001"

    def test_overwrite_previous_pending(self) -> None:
        """同一 chat_id 的新待确认覆盖旧的。"""
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")

        bridge._save_pending_confirmation(
            inbound=inbound, run_id="run-1", tool_name="tool_a", arguments={},
        )
        bridge._save_pending_confirmation(
            inbound=inbound, run_id="run-2", tool_name="tool_b", arguments={},
        )
        assert bridge._pending_confirmations["user_a"].run_id == "run-2"

    def test_clear_after_pop(self) -> None:
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")
        bridge._save_pending_confirmation(
            inbound=inbound, run_id="r1", tool_name="t", arguments={},
        )
        bridge._pending_confirmations.pop("user_a", None)
        assert "user_a" not in bridge._pending_confirmations


class TestConfirmationFlow:
    """确认/取消交互流程测试。"""

    @pytest.mark.asyncio
    async def test_confirm_dispatches_to_handler(self) -> None:
        """用户回复「确认」应触发 _handle_confirm 逻辑。"""
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")
        bridge._save_pending_confirmation(
            inbound=inbound, run_id="run-x", tool_name="create_incident",
            arguments={"title": "test"},
        )
        # 确认消息
        confirm_msg = _make_inbound("user_a", "确认")
        assert bridge._pending_confirmations.get("user_a") is not None

        result = await bridge._try_handle_confirmation(confirm_msg)
        assert result is True
        # 确认后状态应清除（在 _handle_confirm 中 pop）
        assert "user_a" not in bridge._pending_confirmations

    @pytest.mark.asyncio
    async def test_cancel_clears_pending(self) -> None:
        """用户回复「取消」应清除待确认状态。"""
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")
        bridge._save_pending_confirmation(
            inbound=inbound, run_id="run-x", tool_name="test_tool", arguments={},
        )

        cancel_msg = _make_inbound("user_a", "取消")
        result = await bridge._try_handle_confirmation(cancel_msg)
        assert result is True
        assert "user_a" not in bridge._pending_confirmations

    @pytest.mark.asyncio
    async def test_no_pending_returns_false(self) -> None:
        """无待确认状态时 _try_handle_confirmation 返回 False。"""
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        msg = _make_inbound("user_a", "确认")
        result = await bridge._try_handle_confirmation(msg)
        assert result is False  # 没有待确认操作，消息应走正常流程

    @pytest.mark.asyncio
    async def test_other_message_dismisses_pending(self) -> None:
        """用户发其他消息时，待确认状态被清除。"""
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        inbound = _make_inbound("user_a")
        bridge._save_pending_confirmation(
            inbound=inbound, run_id="run-x", tool_name="test_tool", arguments={},
        )

        other_msg = _make_inbound("user_a", "帮我另查一个东西")
        result = await bridge._try_handle_confirmation(other_msg)
        assert result is False
        # 待确认状态被清除（发新消息视为取消）
        assert "user_a" not in bridge._pending_confirmations

    @pytest.mark.asyncio
    async def test_different_chat_id_no_interference(self) -> None:
        """不同 chat_id 的待确认不互相干扰。"""
        bus = MessageBus()
        bridge = AgentBridge(
            bus=bus, caibao_base_url="http://test",
            bot_user_id="bot", bot_password="pw",
        )
        bridge._save_pending_confirmation(
            inbound=_make_inbound("user_a"), run_id="r1", tool_name="ta", arguments={},
        )
        bridge._save_pending_confirmation(
            inbound=_make_inbound("user_b"), run_id="r2", tool_name="tb", arguments={},
        )

        # user_a 确认
        msg_a = _make_inbound("user_a", "确认")
        result_a = await bridge._try_handle_confirmation(msg_a)
        assert result_a is True
        assert "user_a" not in bridge._pending_confirmations
        # user_b 的待确认不受影响
        assert "user_b" in bridge._pending_confirmations
        assert bridge._pending_confirmations["user_b"].tool_name == "tb"


class TestGroupChatConversationId:
    """群聊会话 ID 测试。"""

    def test_group_chat_id_prefix_stripped(self) -> None:
        """群聊 'g789012' → conversation_id = 'qq_group_789012'。"""
        msg = InboundMessage(
            channel_type="napcat",
            chat_type="group",
            chat_id="g789012",
            user_id="10001",
            user_name="group_member",
            content="hello",
        )
        conv_id = AgentBridge._make_conversation_id(msg)
        assert conv_id == "qq_group_789012"

    def test_group_uses_chat_id_not_user_id(self) -> None:
        """群聊 conversation_id 使用群号，非用户 QQ 号。"""
        msg = InboundMessage(
            channel_type="napcat",
            chat_type="group",
            chat_id="g12345",
            user_id="99999",
            user_name="member",
            content="hi",
        )
        conv_id = AgentBridge._make_conversation_id(msg)
        assert "99999" not in conv_id
        assert "12345" in conv_id
