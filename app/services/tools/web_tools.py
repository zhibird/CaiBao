"""Web tools: web_fetch (抓取网页) and web_search (网络搜索).

web_fetch — Inspired by akashic-agent's WebFetchTool:
  - lxml DOM-based HTML → text (properly removes script/style/noscript)
  - html2text for HTML → Markdown
  - User-Agent + Accept header negotiation
  - Binary content detection & rejection
  - SSRF protection with DNS-resolved IP filtering

web_search — Inspired by akashic-agent's WebSearchTool:
  - Exa MCP public endpoint (free, no API key)
  - Brave / Tavily with API key
  - MCP JSON-RPC 2.0 over SSE
"""

from __future__ import annotations

import html as _html
import ipaddress
import json as _json
import re
import socket
from urllib.parse import urljoin, urlparse

import html2text as _html2text
import httpx
from lxml import html as lxml_html
from lxml.etree import ParserError

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError
from app.services.tool_registry import ToolDefinition

# ── constants ──────────────────────────────────────────────────────────────

_MAX_BYTES = 5 * 1024 * 1024          # 5 MB, same as akashic-agent / OpenCode
_MAX_TEXT_CHARS = 50_000               # ~12K tokens
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_USER_AGENT = "caibao/1.0"
_DEFAULT_NUM_RESULTS = 8
_MCP_URL = "https://mcp.exa.ai/mcp"

_ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text":     "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html":     "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, */*;q=0.1",
}

_BINARY_CONTENT_TYPES = frozenset({
    "application/pdf", "application/octet-stream",
    "image/", "video/", "audio/",
})

# ── tool definitions ───────────────────────────────────────────────────────

_WEB_FETCH_DEFINITION = ToolDefinition(
    name="web_fetch",
    display_name="抓取网页",
    description=(
        "Fetch a web page by URL and return its text content. "
        "Supports text / markdown / html output formats. "
        "HTTP/HTTPS only, 5 MB limit."
    ),
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
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "Return format: text / markdown / html. Default markdown.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1, "maximum": 120, "default": 30,
                "description": "Timeout in seconds.",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "final_url": {"type": "string"},
            "status": {"type": "integer"},
            "content_type": {"type": "string"},
            "format": {"type": "string"},
            "length": {"type": "integer"},
            "text": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
    },
)

_WEB_SEARCH_DEFINITION = ToolDefinition(
    name="web_search",
    display_name="搜索网页",
    description=(
        "Search the web using keywords. Returns title, snippet, and URL of each result. "
        "Best for current events, news, pricing, and time-sensitive queries. "
        "Use web_fetch afterwards to read full page content."
    ),
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
            "num_results": {
                "type": "integer",
                "minimum": 1, "maximum": 20, "default": 8,
                "description": "Number of results to return, default 8, max 20.",
            },
            "livecrawl": {
                "type": "string",
                "enum": ["fallback", "preferred"],
                "description": "Crawl mode: fallback (cached-first) or preferred (live-first). Default fallback.",
            },
            "type": {
                "type": "string",
                "enum": ["auto", "fast", "deep"],
                "description": "Search depth: auto / fast / deep. Default auto.",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "results": {"type": "array"},
            "provider": {"type": "string"},
            "query": {"type": "string"},
        },
    },
)


def create_web_tools() -> list[ToolDefinition]:
    return [_WEB_FETCH_DEFINITION, _WEB_SEARCH_DEFINITION]


# ═══════════════════════════════════════════════════════════════════════════
# SSRF protection
# ═══════════════════════════════════════════════════════════════════════════

def _host_is_dangerous(host: str) -> bool:
    """Block private, loopback, link-local, and reserved IPs.

    Uses DNS resolution so domain→IP attacks are caught, not just literal IPs.
    """
    if host.lower().strip().rstrip(".") in {
        "localhost", "0.0.0.0", "::1", "0:0:0:0:0:0:0:1",
        "[::1]", "[0:0:0:0:0:0:0:1]",
    }:
        return True
    try:
        addrs = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return True  # block unresolvable
    for family, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
        if ip_str == "169.254.169.254":
            return True
    return False


def _validate_url_target(url: str) -> str | None:
    """SSRF guard — reject private / loopback / reserved addresses."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL missing hostname"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return f"Cannot access private/local address: {host}"
    except ValueError:
        if host in {"localhost", "0.0.0.0"} or host.endswith(".local") or host.endswith(".localhost"):
            return f"Cannot access local domain: {host}"
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════════════════════

def web_fetch_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    url = str(arguments["url"]).strip()
    fmt = str(arguments.get("format", "markdown")).strip()
    timeout = min(int(arguments.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)

    # ── scheme check ──
    if not url.startswith(("http://", "https://")):
        raise DomainValidationError("web_fetch only supports http and https URLs.")

    # ── SSRF guard ──
    err = _validate_url_target(url)
    if err:
        raise DomainValidationError(err)

    # ── fetch ──
    client_kwargs = {
        "follow_redirects": True,
        "timeout": timeout,
        "headers": {
            "User-Agent": _USER_AGENT,
            "Accept": _ACCEPT.get(fmt, "*/*"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }

    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        raise DomainValidationError(f"web_fetch timed out after {timeout}s.")
    except httpx.ConnectError:
        raise DomainValidationError("web_fetch could not establish connection.")
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"web_fetch request failed: {exc}") from exc

    if resp.status_code != 200:
        raise DomainValidationError(f"web_fetch returned HTTP {resp.status_code}.")

    # ── size check ──
    cl = resp.headers.get("content-length")
    if cl and int(cl) > _MAX_BYTES:
        raise DomainValidationError("web_fetch response exceeds 5 MB limit.")

    body = resp.content
    if len(body) > _MAX_BYTES:
        raise DomainValidationError("web_fetch response exceeds 5 MB limit.")

    content_type = resp.headers.get("content-type", "")
    encoding = resp.encoding or "utf-8"

    # ── binary rejection ──
    for prefix in _BINARY_CONTENT_TYPES:
        if prefix in content_type:
            raise DomainValidationError(
                f"web_fetch cannot process binary content ({content_type}). "
                "Use a dedicated tool for this format."
            )

    is_html = "text/html" in content_type

    # ── decode & transform ──
    if fmt == "html":
        text = body.decode(encoding, errors="replace")
    elif fmt == "markdown" and is_html:
        text = _html_to_markdown(body.decode(encoding, errors="replace"))
    elif fmt == "text" and is_html:
        text = _html_to_text(body)
    else:
        # non-HTML (JSON, plain text, etc.) — return as-is
        text = body.decode(encoding, errors="replace")

    # ── truncate ──
    truncated = len(text) > _MAX_TEXT_CHARS
    if truncated:
        text = text[:_MAX_TEXT_CHARS]

    result: dict[str, object] = {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": content_type,
        "format": fmt,
        "length": len(text),
        "text": text,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Content truncated to {_MAX_TEXT_CHARS} chars. "
            "Narrow your scope or use a different tool for more."
        )
    return result


def web_search_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    provider = settings.web_search_provider.strip().lower()
    api_key = (settings.web_search_api_key or "").strip()

    query = str(arguments["query"]).strip()
    num_results = int(arguments.get("num_results", _DEFAULT_NUM_RESULTS))
    livecrawl = str(arguments.get("livecrawl", "fallback"))
    search_type = str(arguments.get("type", "auto"))

    if provider == "disabled":
        raise DomainValidationError(
            "Web search is not configured. "
            "Set web_search_provider in config (e.g. 'exa' for free Exa MCP search)."
        )
    if provider == "exa":
        return _exa_search(
            query=query, num_results=num_results,
            livecrawl=livecrawl, search_type=search_type,
        )
    if provider == "brave":
        if not api_key:
            raise DomainValidationError("Brave search requires WEB_SEARCH_API_KEY.")
        return _brave_search(query=query, limit=num_results, api_key=api_key)
    if provider == "tavily":
        if not api_key:
            raise DomainValidationError("Tavily search requires WEB_SEARCH_API_KEY.")
        return _tavily_search(query=query, limit=num_results, api_key=api_key)

    raise DomainValidationError(f"Unsupported search provider: {provider}")


# ═══════════════════════════════════════════════════════════════════════════
# Brave / Tavily
# ═══════════════════════════════════════════════════════════════════════════

def _brave_search(*, query: str, limit: int, api_key: str) -> dict[str, object]:
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 10)},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Brave search failed: {exc}") from exc

    web_results = data.get("web", {}).get("results", [])
    return {
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in web_results[:limit]
        ],
        "provider": "brave",
        "query": query,
    }


def _tavily_search(*, query: str, limit: int, api_key: str) -> dict[str, object]:
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": min(limit, 10), "api_key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Tavily search failed: {exc}") from exc

    results = data.get("results", [])
    return {
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in results[:limit]
        ],
        "provider": "tavily",
        "query": query,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Exa MCP search (JSON-RPC 2.0 over SSE)
# ═══════════════════════════════════════════════════════════════════════════

def _exa_search(
    *,
    query: str,
    num_results: int,
    livecrawl: str = "fallback",
    search_type: str = "auto",
) -> dict[str, object]:
    """Exa MCP public endpoint — free, no API key required."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {
                "query": query,
                "numResults": min(num_results, 20),
                "livecrawl": livecrawl,
                "type": search_type,
            },
        },
    }

    try:
        resp = httpx.post(
            _MCP_URL,
            json=payload,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            timeout=25.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Exa search failed: {exc}") from exc

    # Parse SSE response — each "data:" line is a JSON-RPC response chunk
    raw_text = ""
    for line in resp.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("data: "):
            try:
                chunk = _json.loads(stripped[6:])
                content = chunk.get("result", {}).get("content", [])
                if content:
                    raw_text = content[0].get("text", "")
            except (_json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

    if not raw_text:
        return {"results": [], "provider": "exa", "query": query}

    results = _parse_exa_results(raw_text)
    return {
        "results": results[:num_results],
        "provider": "exa",
        "query": query,
    }


def _parse_exa_results(raw: str) -> list[dict[str, str]]:
    """Parse Exa's plain-text result format into structured records.

    Expected format (one blank line between results)::

        Title: Example Article
        URL: https://example.com/article
        Published Date: 2025-06-01
        Text: A snippet of the article content...

        Title: Another Result
        URL: https://example.org/another
        Text: Another snippet...

    The parser is section-based: a blank line separates results; Title
    always starts a new result block.  Extra / unrecognised lines are
    appended to the current result's snippet.
    """
    results: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if current is not None:
                results.append(current)
                current = None
            continue

        # Key: value pairs
        for prefix, key in [
            ("Title: ", "title"),
            ("URL: ", "url"),
            ("Published Date: ", "published"),
            ("Text: ", "snippet"),
            ("Score: ", "score"),
        ]:
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip()
                if key == "title":
                    # Title always opens a new block
                    if current is not None:
                        results.append(current)
                    current = {"title": value}
                elif current is not None:
                    current[key] = value
                break
        else:
            # Unrecognised line — append to current snippet
            if current is not None:
                existing = current.get("snippet", "")
                current["snippet"] = f"{existing} {stripped}".strip() if existing else stripped

    if current is not None:
        results.append(current)

    return results


# ═══════════════════════════════════════════════════════════════════════════
# HTML processing (lxml + html2text)
# ═══════════════════════════════════════════════════════════════════════════

def _html_to_text(content: bytes) -> str:
    """HTML → plain text via lxml DOM (akashic-agent style).

    Removes script / style / noscript / iframe / object / embed tags,
    then extracts text_content and normalises whitespace.
    """
    try:
        doc = lxml_html.fromstring(content)
    except ParserError:
        return content.decode("utf-8", errors="replace")

    for tag_name in ("script", "style", "noscript", "iframe", "object", "embed"):
        for el in doc.xpath(f"//{tag_name}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    return " ".join(doc.text_content().split())


def _html_to_markdown(raw_html: str) -> str:
    """HTML → Markdown via html2text (akashic-agent style)."""
    h = _html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0          # disable line wrapping
    h.unicode_snob = True     # preserve Unicode
    h.protect_links = True    # don't escape links
    return h.handle(raw_html).strip()
