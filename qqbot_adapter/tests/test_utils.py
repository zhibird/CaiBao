"""测试会话 ID 映射和消息格式化。"""

import pytest

from qqbot_adapter.utils.formatter import (
    build_confirmation_prompt,
    find_flush_point,
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


class TestFindFlushPoint:
    """智能分段断点查找测试。"""

    def test_too_short_returns_none(self) -> None:
        """低于 MIN_FLUSH_CHARS(120) 时不急于分段。"""
        text = "短文本。"
        assert find_flush_point(text) is None

    def test_paragraph_break_preferred(self) -> None:
        """双换行符（段落结束）优先级最高（文本需超过 MIN_FLUSH_CHARS）。"""
        # MIN_FLUSH_CHARS = 120，需要文本长度超过此值
        prefix = "A" * 130  # 填充到超过 min flush 阈值
        text = prefix + "第一段内容在这里。\n\n第二段开始写更多的东西。"
        point = find_flush_point(text)
        assert point is not None
        # 断点应在第一个 \n\n 之后
        flushed = text[:point].rstrip()
        assert flushed.endswith("。")

    def test_sentence_end_after_soft_threshold(self) -> None:
        """超过 SOFT_FLUSH_CHARS(400) 时在句子结尾分段。"""
        # ~450 chars, 句子以。结束
        text = "A" * 200 + "这是第一句话。" + "B" * 200 + "这是第二句话。"
        point = find_flush_point(text)
        assert point is not None
        # 期望在"这是第一句话。"之后分段
        flushed = text[:point]
        assert flushed.endswith("。")
        assert len(flushed) <= 700  # 不超过 hard_max

    def test_hard_flush_at_space_when_over_limit(self) -> None:
        """超过 HARD_FLUSH_CHARS(700) 在空格处强制截断。"""
        # 很长的无标点文本
        text = "word " * 300  # ~1500 chars
        point = find_flush_point(text)
        assert point is not None
        assert point <= 700
        assert text[point - 1] in (" ", "d")  # 在空格或硬截断处

    def test_hard_flush_without_space(self) -> None:
        """超过硬阈值且无空格时，在 hard_max 处直接截断。"""
        text = "x" * 800
        point = find_flush_point(text)
        assert point is not None
        assert point == 700  # HARD_FLUSH_CHARS

    def test_multiple_paragraphs_picks_best(self) -> None:
        """多个段落结束时，选择不超过 hard_max 的最佳断点。"""
        text = (
            "A" * 50 + "第一段。\n\n" +
            "B" * 80 + "第二段。\n\n" +
            "C" * 600 + "第三段还在写。\n\n" +
            "D" * 100 + "最后一段。"
        )
        point = find_flush_point(text)
        assert point is not None
        flushed = text[:point]
        # 应该在前两个段落之一结束，不超过 700
        assert point <= 700
        assert flushed.strip().endswith("。")

    def test_newline_as_sentence_boundary(self) -> None:
        """单个换行 + 超过软阈值 → 作为句子结束符分段。"""
        text = "A" * 250 + "标题行\n" + "B" * 200 + "正文内容"
        point = find_flush_point(text)
        assert point is not None
        # \n 被视为句子结束符
        flushed = text[:point]
        assert "\n" in flushed

    def test_mixed_punctuation(self) -> None:
        """中英文混合标点都能正确识别。"""
        text = "A" * 150 + "这句话结束了！" + "B" * 150 + "Another sentence ends?" + "C" * 150 + "最后一句."
        point = find_flush_point(text)
        assert point is not None
        assert point <= 700

    def test_below_soft_threshold_no_flush(self) -> None:
        """在 MIN 和 SOFT 之间、有句子结束但未到软阈值时暂不 flush。"""
        text = "A" * 130 + "结束了。"
        # 130 + 4 = 134 chars, 超过 MIN(120) 且有句子结束
        # 但不到 SOFT(400)，且距离 hard_max(700) 还远
        # 段落结束优先检测过了，句子结束检测需要 len >= SOFT
        point = find_flush_point(text)
        # len(text) = 134 < SOFT_FLUSH_CHARS(400)，句子结束检测不会被触发
        # 但由于有 \n\n 才触发段落检测，这里没有段落
        # 实际上应该返回 None，因为没有触发任何 flush 条件
        assert point is None

    def test_sentence_at_min_boundary(self) -> None:
        """刚好在 MIN_FLUSH_CHARS 处有句子结束。"""
        text = "A" * 117 + "结束。"  # 120 chars 正好
        # 有段落结束吗？没有 \n\n
        # 句子结束检测需要 len >= SOFT(400)，不满足
        # 硬截断需要 >= HARD(700)，不满足
        point = find_flush_point(text)
        assert point is None  # 所有条件都不满足


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
