from __future__ import annotations

import html
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError
from app.services.tool_registry import ToolDefinition


_WEB_FETCH_DEFINITION = ToolDefinition(
    name="web_fetch",
    display_name="抓取网页",
    description="Fetch a web page by URL and return its text content (HTML converted to plain text).",
    dangerous=False,
    handler_key="generic.web_fetch",
    permission_scope="team",
    source="generic",
    provider="web_tools",
    input_schema={
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 2048},
            "max_chars": {"type": "integer", "minimum": 100, "maximum": 50000, "default": 12000},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30, "default": 15},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "status_code": {"type": "integer"},
            "final_url": {"type": "string"},
            "content": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
    },
)

_WEB_SEARCH_DEFINITION = ToolDefinition(
    name="web_search",
    display_name="搜索网页",
    description="Search the web and return results (title, URL, snippet). Requires a search provider API key.",
    dangerous=False,
    handler_key="generic.web_search",
    permission_scope="team",
    source="generic",
    provider="web_tools",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "results": {"type": "array"},
            "provider": {"type": "string"},
        },
    },
)


def create_web_tools() -> list[ToolDefinition]:
    return [_WEB_FETCH_DEFINITION, _WEB_SEARCH_DEFINITION]


# ------------------------------------------------------------------
# SSRF protection: DNS-resolved IP filtering
# ------------------------------------------------------------------

def _host_is_dangerous(host: str) -> bool:
    """Block private, loopback, link-local, and reserved IPs.
    Uses DNS resolution so domain→IP attacks are caught, not just literal IPs."""
    # Block literal localhost variants
    if host.lower() in {"localhost", "0.0.0.0", "::1", "0:0:0:0:0:0:0:1", "[::1]", "[0:0:0:0:0:0:0:1]"}:
        return True

    # Resolve DNS
    try:
        addrs = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return True  # Block unresolvable hosts

    for family, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        # Strip scope ID from IPv6 (e.g., fe80::1%eth0)
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if (
            ip.is_private       # 10.x, 172.16-31, 192.168
            or ip.is_loopback   # 127.x, ::1
            or ip.is_link_local # 169.254.x, fe80::
            or ip.is_reserved   # 240.0.0.0/4 and others
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
        # Block AWS/cloud metadata endpoints
        if ip_str == "169.254.169.254":
            return True
    return False


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------

def web_fetch_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    url = str(arguments["url"]).strip()
    max_chars = int(arguments.get("max_chars", 12000))
    timeout = min(int(arguments.get("timeout_seconds", 15)), 30)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise DomainValidationError("web_fetch only supports http and https URLs.")

    # Block private IPs
    host = parsed.hostname
    if host and settings.web_fetch_block_private_ips:
        if _host_is_dangerous(host) or host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            raise DomainValidationError(f"web_fetch cannot access private/local host: {host}")

    # Fetch with manual redirect following — check each hop before following.
    # Stream response bodies so web_fetch_max_bytes is enforced; otherwise
    # response.text would buffer the full response before truncation.
    max_redirects = 3
    current_url = url
    response = None
    raw_text = ""
    max_bytes = settings.web_fetch_max_bytes

    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for _ in range(max_redirects + 1):
            hop_host = urlparse(current_url).hostname
            if hop_host and settings.web_fetch_block_private_ips:
                if _host_is_dangerous(hop_host) or hop_host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
                    raise DomainValidationError(f"web_fetch cannot access private/local host: {hop_host}")

            try:
                with client.stream("GET", current_url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        if not location:
                            break
                        from urllib.parse import urljoin
                        current_url = urljoin(current_url, location)
                        parsed = urlparse(current_url)
                        if parsed.scheme not in {"http", "https"}:
                            raise DomainValidationError("web_fetch redirect target is not http/https.")
                        continue  # follow redirect without reading body

                    # Read body in chunks, abort when exceeding max_bytes
                    chunks: list[str] = []
                    total = 0
                    for chunk in response.iter_text(chunk_size=8192):
                        total += len(chunk.encode("utf-8", errors="replace"))
                        if total > max_bytes:
                            raw_text = "".join(chunks)
                            raw_text += "\n\n[web_fetch aborted: response exceeded max_bytes limit]"
                            break
                        chunks.append(chunk)
                    else:
                        raw_text = "".join(chunks)
            except httpx.HTTPError as exc:
                raise DomainValidationError(f"web_fetch failed: {exc}") from exc

            break  # exit redirect loop after handling the first non-redirect response

    content_type = response.headers.get("content-type", "") if response else ""
    is_html = "text/html" in content_type.lower() or raw_text.lstrip().startswith("<")

    title = _extract_title(raw_text) if is_html else ""
    title = title or parsed.hostname or ""

    if is_html:
        text = _html_to_text(raw_text)
    else:
        text = raw_text

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    return {
        "title": title,
        "status_code": response.status_code,
        "final_url": str(response.url),
        "content": text,
        "truncated": truncated,
    }


def web_search_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    provider = settings.web_search_provider.strip().lower()
    api_key = (settings.web_search_api_key or "").strip()

    if provider == "disabled" or not api_key:
        raise DomainValidationError(
            "Web search is not configured. Set WEB_SEARCH_PROVIDER and WEB_SEARCH_API_KEY in .env."
        )

    query = str(arguments["query"]).strip()
    limit = int(arguments.get("limit", 5))

    if provider == "brave":
        return _brave_search(query=query, limit=limit, api_key=api_key)
    if provider == "tavily":
        return _tavily_search(query=query, limit=limit, api_key=api_key)

    raise DomainValidationError(f"Unsupported search provider: {provider}")


def _brave_search(*, query: str, limit: int, api_key: str) -> dict[str, object]:
    try:
        response = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 10)},
            headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": api_key},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Brave search failed: {exc}") from exc

    web_results = data.get("web", {}).get("results", [])
    return {
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in web_results[:limit]
        ],
        "provider": "brave",
    }


def _tavily_search(*, query: str, limit: int, api_key: str) -> dict[str, object]:
    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": min(limit, 10), "api_key": api_key},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Tavily search failed: {exc}") from exc

    results = data.get("results", [])
    return {
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in results[:limit]
        ],
        "provider": "tavily",
    }


# ------------------------------------------------------------------
# HTML-to-text (lightweight, no extra dependency)
# ------------------------------------------------------------------

def _html_to_text(html_text: str) -> str:
    # Remove script and style
    cleaned = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html_text, flags=re.IGNORECASE)
    # Remove tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode entities
    cleaned = html.unescape(cleaned)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if match:
        return html.unescape(re.sub(r"\s+", " ", match.group("title")).strip())
    return ""
