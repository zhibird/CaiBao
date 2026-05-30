"""图片工具：下载/上传/编码，供所有通道共用。

Phase 5 实现：
- URL → bytes 下载
- Base64 ↔ bytes 互转
- MIME 类型检测
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

# 最大图片下载大小（字节）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB

# 常见图片扩展名 → MIME 映射
_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# 从 URL 或 CQ 码提取图片 URL 的正则
_CQ_IMAGE_URL_RE = re.compile(r"url=([^,\]]+)")
_HTTP_URL_RE = re.compile(r"https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)", re.IGNORECASE)


async def download_images_from_urls(
    urls: list[str],
    *,
    http_client: httpx.AsyncClient | None = None,
    max_size: int = _MAX_IMAGE_BYTES,
) -> list[bytes]:
    """从 URL 列表下载图片，返回原始字节列表。

    失败的单张图片静默跳过（不阻塞消息处理）。
    """
    if not urls:
        return []

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    try:
        results: list[bytes] = []
        for url in urls:
            try:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                data = resp.content
                if len(data) <= max_size and len(data) > 0:
                    results.append(data)
            except Exception:
                _logger.debug("Failed to download image from %s", url[:100], exc_info=True)
        return results
    finally:
        if close_client:
            await client.aclose()


def extract_image_urls_from_cq(raw_message: str) -> list[str]:
    """从 CQ 码消息中提取图片 URL 列表。"""
    urls: list[str] = []

    # [CQ:image,file=...,url=https://...]
    for match in _CQ_IMAGE_URL_RE.finditer(raw_message):
        url = match.group(1).strip()
        if url.startswith("http"):
            urls.append(url)

    # 纯 HTTP URL（非 CQ 码场景，如官方 Bot）
    for match in _HTTP_URL_RE.finditer(raw_message):
        url = match.group(0).strip()
        if url not in urls:
            urls.append(url)

    return urls


def guess_mime_type(filename_or_url: str) -> str:
    """根据文件名/URL 猜测 MIME 类型。"""
    for ext, mime in _EXT_TO_MIME.items():
        if filename_or_url.lower().endswith(ext):
            return mime
    return "image/jpeg"  # 默认


def bytes_to_base64(data: bytes) -> str:
    """原始字节 → base64 字符串。"""
    return base64.b64encode(data).decode("ascii")


def base64_to_bytes(encoded: str) -> bytes:
    """base64 字符串 → 原始字节。"""
    return base64.b64decode(encoded)


def build_data_uri(data: bytes, filename_or_url: str = "") -> str:
    """构建 data:image/xxx;base64,... URI。"""
    mime = guess_mime_type(filename_or_url)
    return f"data:{mime};base64,{bytes_to_base64(data)}"


async def download_images_from_message(
    raw_message: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> list[bytes]:
    """从消息文本中提取图片 URL 并下载。

    同时支持 NapCat CQ 码和官方 Bot 附件格式。
    """
    urls = extract_image_urls_from_cq(raw_message)
    if not urls:
        return []

    _logger.debug("Downloading %d image(s) from message", len(urls))
    return await download_images_from_urls(urls, http_client=http_client)
