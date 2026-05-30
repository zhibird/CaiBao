"""测试图片工具：URL 提取、MIME 检测、base64 编解码。"""

import pytest

from qqbot_adapter.utils.image_utils import (
    base64_to_bytes,
    bytes_to_base64,
    build_data_uri,
    extract_image_urls_from_cq,
    guess_mime_type,
)


class TestExtractImageUrls:
    """从消息中提取图片 URL 测试。"""

    def test_cq_image_url_extracted(self) -> None:
        raw = "[CQ:image,file=test.jpg,url=https://example.com/img.jpg]"
        urls = extract_image_urls_from_cq(raw)
        assert urls == ["https://example.com/img.jpg"]

    def test_multiple_cq_images(self) -> None:
        raw = (
            "[CQ:image,url=https://a.com/1.png] "
            "[CQ:image,url=https://b.com/2.jpg]"
        )
        urls = extract_image_urls_from_cq(raw)
        assert len(urls) == 2

    def test_cq_image_without_url_ignored(self) -> None:
        raw = "[CQ:image,file=test.jpg]"
        urls = extract_image_urls_from_cq(raw)
        assert urls == []

    def test_plain_http_url_extracted(self) -> None:
        raw = "看这张图 https://example.com/photo.png 怎么样"
        urls = extract_image_urls_from_cq(raw)
        assert "https://example.com/photo.png" in urls

    def test_non_image_url_ignored(self) -> None:
        raw = "访问 https://example.com/page 查看"
        urls = extract_image_urls_from_cq(raw)
        assert urls == []

    def test_no_urls_returns_empty(self) -> None:
        raw = "纯文本消息"
        assert extract_image_urls_from_cq(raw) == []

    def test_duplicate_urls_deduplicated(self) -> None:
        urls = extract_image_urls_from_cq(
            "[CQ:image,url=https://a.com/1.png] https://a.com/1.png"
        )
        assert len(urls) == 1


class TestMimeType:
    """MIME 类型猜测测试。"""

    def test_jpg(self) -> None:
        assert guess_mime_type("photo.jpg") == "image/jpeg"

    def test_png(self) -> None:
        assert guess_mime_type("screenshot.png") == "image/png"

    def test_gif(self) -> None:
        assert guess_mime_type("anim.gif") == "image/gif"

    def test_webp(self) -> None:
        assert guess_mime_type("img.webp") == "image/webp"

    def test_url_extension(self) -> None:
        assert guess_mime_type("https://example.com/photo.PNG") == "image/png"

    def test_unknown_returns_default(self) -> None:
        assert guess_mime_type("file.xyz") == "image/jpeg"


class TestBase64:
    """Base64 编解码测试。"""

    def test_roundtrip(self) -> None:
        data = b"hello image data"
        encoded = bytes_to_base64(data)
        decoded = base64_to_bytes(encoded)
        assert decoded == data

    def test_empty_bytes(self) -> None:
        encoded = bytes_to_base64(b"")
        assert base64_to_bytes(encoded) == b""

    def test_binary_data(self) -> None:
        data = bytes(range(256))
        encoded = bytes_to_base64(data)
        decoded = base64_to_bytes(encoded)
        assert decoded == data

    def test_data_uri_format(self) -> None:
        uri = build_data_uri(b"\x89PNG", "test.png")
        assert uri.startswith("data:image/png;base64,")


class TestStripAtMention:
    """QQBot 官方 @ 提及清理测试。"""

    def test_strip_single_at(self) -> None:
        from qqbot_adapter.channels.qqbot_channel import QQBotChannel
        result = QQBotChannel._strip_at_mention("<@!123456> 你好")
        assert result == "你好"

    def test_strip_multiple_at(self) -> None:
        from qqbot_adapter.channels.qqbot_channel import QQBotChannel
        result = QQBotChannel._strip_at_mention("<@!111> <@!222> 大家好")
        assert result == "大家好"

    def test_no_at_unchanged(self) -> None:
        from qqbot_adapter.channels.qqbot_channel import QQBotChannel
        result = QQBotChannel._strip_at_mention("普通消息")
        assert result == "普通消息"
