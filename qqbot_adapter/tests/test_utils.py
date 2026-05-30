"""测试会话 ID 映射和消息格式化。"""

import pytest

from qqbot_adapter.utils.formatter import (
    build_confirmation_prompt,
    markdown_to_qq,
)
from qqbot_adapter.utils.session import (
    make_group_conversation_id,
    make_private_conversation_id,
    parse_conversation_id,
)


class TestSessionMapping:
    """会话 ID 映射测试。"""

    def test_private_conversation_id(self) -> None:
        assert make_private_conversation_id("123456") == "qq_123456"

    def test_group_conversation_id(self) -> None:
        assert make_group_conversation_id("789012") == "qq_group_789012"

    def test_parse_private(self) -> None:
        chat_type, identifier = parse_conversation_id("qq_123456")
        assert chat_type == "private"
        assert identifier == "123456"

    def test_parse_group(self) -> None:
        chat_type, identifier = parse_conversation_id("qq_group_789012")
        assert chat_type == "group"
        assert identifier == "789012"

    def test_parse_unknown(self) -> None:
        chat_type, identifier = parse_conversation_id("weird_format")
        assert chat_type == "unknown"
        assert identifier == "weird_format"


class TestMarkdownToQQ:
    """Markdown → QQ 文本转换测试。"""

    def test_bold_to_bracket(self) -> None:
        result = markdown_to_qq("这是**重要的**消息")
        assert "「重要的」" in result

    def test_header_to_decorated(self) -> None:
        result = markdown_to_qq("## 标题内容")
        assert "▎标题内容" in result

    def test_code_to_bracket(self) -> None:
        result = markdown_to_qq("使用 `create_incident` 工具")
        assert "[create_incident]" in result

    def test_link_preserved(self) -> None:
        result = markdown_to_qq("[点击这里](https://example.com)")
        assert "点击这里" in result
        assert "https://example.com" in result

    def test_code_block_replaced(self) -> None:
        result = markdown_to_qq("```\ncode block\n```")
        assert "[代码块]" in result


class TestConfirmationPrompt:
    """危险操作确认提示测试。"""

    def test_build_prompt(self) -> None:
        prompt = build_confirmation_prompt("create_incident", {"title": "DB告警"})
        assert "create_incident" in prompt
        assert "DB告警" in prompt
        assert "确认" in prompt
        assert "取消" in prompt

    def test_long_arguments_truncated(self) -> None:
        long_arg = {"content": "x" * 500}
        prompt = build_confirmation_prompt("create_memory_card", long_arg)
        assert len(prompt) < 600  # 被截断
        assert "..." in prompt
