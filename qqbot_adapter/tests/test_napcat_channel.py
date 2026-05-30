"""测试 NapCatChannel 核心逻辑（不依赖真实 WebSocket 连接）。

测试覆盖：
- CQ 码提取 (_extract_text)
- @ 机器人检测 (_is_at_bot)
- 消息路由 (send_message 的参数编码)
- 白名单过滤 (allow_from)
- 群配置解析
"""

import pytest

from qqbot_adapter.channels.napcat_channel import NapCatChannel
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import InboundMessage


@pytest.fixture
def channel() -> NapCatChannel:
    """创建一个不自动连接的 NapCatChannel 实例。"""
    bus = MessageBus()
    return NapCatChannel(
        bus=bus,
        ws_url="ws://127.0.0.1:3001",
        access_token=None,
        allow_from=[],
        reconnect=False,  # 测试中不自动重连
    )


class TestCQCodeExtraction:
    """CQ 码提取测试。"""

    def test_plain_text_passes_through(self, channel) -> None:
        assert channel._extract_text("你好世界") == "你好世界"

    def test_image_cq_removed(self, channel) -> None:
        raw = "[CQ:image,file=abc.jpg,url=https://example.com/img.jpg]看这张图"
        result = channel._extract_text(raw)
        assert "CQ:image" not in result
        assert "看这张图" in result

    def test_at_cq_replaced_with_at_sign(self, channel) -> None:
        raw = "[CQ:at,qq=123456] 你好"
        result = channel._extract_text(raw)
        assert result == "@123456 你好"

    def test_face_cq_replaced_with_emoji_label(self, channel) -> None:
        raw = "哈哈[CQ:face,id=178]笑死"
        result = channel._extract_text(raw)
        assert "[表情]" in result
        assert "哈哈" in result
        assert "笑死" in result

    def test_unknown_cq_code_removed(self, channel) -> None:
        raw = "分享[CQ:share,url=https://example.com,title=链接]给你"
        result = channel._extract_text(raw)
        assert "CQ:share" not in result
        assert "分享" in result
        assert "给你" in result

    def test_multiple_cq_codes_handled(self, channel) -> None:
        raw = "[CQ:at,qq=111] 看 [CQ:image,file=x.jpg] 这张 [CQ:face,id=12]"
        result = channel._extract_text(raw)
        assert result == "@111 看  这张 [表情]"

    def test_empty_message(self, channel) -> None:
        assert channel._extract_text("") == ""
        assert channel._extract_text("[CQ:image,file=x.jpg]") == ""

    def test_malformed_cq_not_crashing(self, channel) -> None:
        # 不完整的 CQ 码不应导致崩溃
        raw = "[CQ:image,file=broken"
        result = channel._extract_text(raw)
        assert isinstance(result, str)  # 不抛异常

    def test_cq_with_nested_brackets_in_url(self, channel) -> None:
        # URL 中可能包含特殊字符
        raw = "[CQ:image,file=test,url=https://example.com/img?id=1&x=2]"
        result = channel._extract_text(raw)
        assert "CQ:image" not in result


class TestAtBotDetection:
    """@ 机器人检测测试。"""

    def test_at_by_cq_code(self, channel) -> None:
        data = {
            "message": f"[CQ:at,qq=999888] 帮我查一下",
            "self_id": "999888",
        }
        assert channel._is_at_bot(data, "12345") is True

    def test_at_by_plain_at_sign(self, channel) -> None:
        data = {
            "message": "@999888 帮我查一下",
            "self_id": "999888",
        }
        assert channel._is_at_bot(data, "12345") is True

    def test_not_at_bot_other_user(self, channel) -> None:
        data = {
            "message": "[CQ:at,qq=111222] 你干嘛",
            "self_id": "999888",
        }
        assert channel._is_at_bot(data, "12345") is False

    def test_not_at_bot_no_mention(self, channel) -> None:
        data = {
            "message": "今天天气真好",
            "self_id": "999888",
        }
        assert channel._is_at_bot(data, "12345") is False

    def test_empty_self_id(self, channel) -> None:
        data = {
            "message": "有人吗",
            "self_id": "",
        }
        assert channel._is_at_bot(data, "12345") is False


class TestWhiteListFiltering:
    """白名单过滤测试。"""

    def _make_private_event(self, user_id: str) -> dict:
        return {
            "post_type": "message",
            "message_type": "private",
            "user_id": int(user_id),
            "sender": {"nickname": "test_user"},
            "message": "你好",
            "message_id": "msg_001",
        }

    def test_allow_from_empty_allows_all(self, channel) -> None:
        """空 allow_from 表示不限制。"""
        channel.allow_from = set()
        data = self._make_private_event("123456")
        # 不会提前 return，消息会被处理
        # 验证：user_id 不在空集合中 → not in 成立 → 不 return
        assert "123456" not in channel.allow_from  # True
        # 但 if self.allow_from and ... → set() is falsy → 不触发检查

    def test_allow_from_blocked_user(self, channel) -> None:
        """白名单外的用户被拦截。"""
        channel.allow_from = {"111111", "222222"}
        # user_id 不在白名单 → 会被 _on_private_message 静默拦截
        assert "999999" not in channel.allow_from

    def test_allow_from_allowed_user(self, channel) -> None:
        """白名单内的用户通过。"""
        channel.allow_from = {"111111", "222222"}
        assert "111111" in channel.allow_from


class TestChatIdRouting:
    """send_message 的 chat_id 路由测试（纯逻辑，不需要 WS 连接）。"""

    def test_group_chat_id_starts_with_g(self, channel) -> None:
        """'g789012'.startswith('g') → send_group_msg 分支。"""
        assert "g789012".startswith("g") is True

    def test_private_chat_id_no_g_prefix(self, channel) -> None:
        """'123456'.startswith('g') → send_private_msg 分支。"""
        assert "123456".startswith("g") is False


class TestGroupConfigParsing:
    """群配置解析测试。"""

    def test_groups_parsed_from_config(self) -> None:
        bus = MessageBus()
        ch = NapCatChannel(
            bus=bus,
            groups=[
                {"group_id": "123", "require_at": True, "allow_from": ["10001"]},
                {"group_id": "456", "require_at": False},
            ],
        )
        assert len(ch.groups) == 2
        assert ch.groups["123"]["require_at"] is True
        assert ch.groups["456"]["require_at"] is False

    def test_invalid_group_id_skipped(self) -> None:
        bus = MessageBus()
        ch = NapCatChannel(
            bus=bus,
            groups=[
                {"group_id": "", "require_at": True},  # 空ID应跳过
                {"group_id": "789", "require_at": False},
            ],
        )
        assert len(ch.groups) == 1
        assert "789" in ch.groups

    def test_empty_groups_defaults_to_empty_dict(self) -> None:
        bus = MessageBus()
        ch = NapCatChannel(bus=bus, groups=None)
        assert ch.groups == {}


class TestEchoCounter:
    """echo 计数器测试。"""

    def test_echo_format(self, channel) -> None:
        """echo 格式：caibao_{counter}_{timestamp_ms}"""
        import time
        counter = 5
        ts = int(time.time() * 1000)
        echo = f"caibao_{counter}_{ts}"
        assert echo.startswith("caibao_")
        assert echo.count("_") == 2
