"""Image utilities shared by QQ adapter channels."""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

_logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 3
_BLOCKED_HOST_NAMES = {"localhost", "localhost.localdomain"}

_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_CQ_IMAGE_URL_RE = re.compile(r"url=([^,\]]+)")
_HTTP_URL_RE = re.compile(r"https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)", re.IGNORECASE)
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


def _ip_is_blocked(address: ipaddress._BaseAddress) -> bool:
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _host_is_blocked(host: str) -> bool:
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if not normalized:
        return True
    if normalized in _BLOCKED_HOST_NAMES or normalized.endswith(".localhost"):
        return True

    try:
        return _ip_is_blocked(ipaddress.ip_address(normalized))
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return True

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return True
        try:
            address = ipaddress.ip_address(str(sockaddr[0]))
        except ValueError:
            return True
        if _ip_is_blocked(address):
            return True
    return False


def _validate_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not parsed.hostname:
        return False
    return not _host_is_blocked(parsed.hostname)


async def download_images_from_urls(
    urls: list[str],
    *,
    http_client: httpx.AsyncClient | None = None,
    max_size: int = _MAX_IMAGE_BYTES,
) -> list[bytes]:
    """Download images with SSRF checks, redirect limits, and streaming size caps."""
    if not urls or max_size <= 0:
        return []

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    try:
        results: list[bytes] = []
        for url in urls:
            current_url = url.strip()
            redirects = 0
            try:
                while redirects <= _MAX_REDIRECTS:
                    if not _validate_image_url(current_url):
                        break

                    async with client.stream("GET", current_url, follow_redirects=False) as resp:
                        if resp.status_code in _REDIRECT_STATUS_CODES:
                            location = resp.headers.get("location")
                            if not location:
                                break
                            current_url = urljoin(current_url, location)
                            redirects += 1
                            continue

                        resp.raise_for_status()
                        content_length = resp.headers.get("content-length")
                        if content_length is not None:
                            try:
                                if int(content_length) > max_size:
                                    break
                            except ValueError:
                                pass

                        data = bytearray()
                        async for chunk in resp.aiter_bytes():
                            if not chunk:
                                continue
                            data.extend(chunk)
                            if len(data) > max_size:
                                data.clear()
                                break

                        if 0 < len(data) <= max_size:
                            results.append(bytes(data))
                    break
            except Exception:
                _logger.debug("Failed to download image from %s", current_url[:100], exc_info=True)
        return results
    finally:
        if close_client:
            await client.aclose()


def extract_image_urls_from_cq(raw_message: str) -> list[str]:
    """Extract image URLs from CQ image tags or plain image links."""
    urls: list[str] = []

    for match in _CQ_IMAGE_URL_RE.finditer(raw_message):
        url = match.group(1).strip()
        if url.startswith("http"):
            urls.append(url)

    for match in _HTTP_URL_RE.finditer(raw_message):
        url = match.group(0).strip()
        if url not in urls:
            urls.append(url)

    return urls


def guess_mime_type(filename_or_url: str) -> str:
    """Guess MIME type from a filename or URL suffix."""
    for ext, mime in _EXT_TO_MIME.items():
        if filename_or_url.lower().endswith(ext):
            return mime
    return "image/jpeg"


def bytes_to_base64(data: bytes) -> str:
    """Encode raw bytes to base64 text."""
    return base64.b64encode(data).decode("ascii")


def base64_to_bytes(encoded: str) -> bytes:
    """Decode base64 text to raw bytes."""
    return base64.b64decode(encoded)


def build_data_uri(data: bytes, filename_or_url: str = "") -> str:
    """Build a data:image/... URI."""
    mime = guess_mime_type(filename_or_url)
    return f"data:{mime};base64,{bytes_to_base64(data)}"


async def download_images_from_message(
    raw_message: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> list[bytes]:
    """Extract and download image URLs from a message body."""
    urls = extract_image_urls_from_cq(raw_message)
    if not urls:
        return []

    _logger.debug("Downloading %d image(s) from message", len(urls))
    return await download_images_from_urls(urls, http_client=http_client)
